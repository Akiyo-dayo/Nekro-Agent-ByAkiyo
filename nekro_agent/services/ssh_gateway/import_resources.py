"""把存量「工作区资源」里的三个 SSH 模板导进服务器凭据表

## 为什么要导

这三类资源（`server_ssh_key` / `server_ssh_password` / `server_root_password`）以前会把
主机、用户名与私钥 / 密码注入 CC 进程环境，凭据因此进过容器。凭据表与网关落地之后，
平台方的服务器改由 NA 进程登录、容器只看得到别名；存量资源必须有一条对应的凭据行，
否则闸一开这些服务器就从工作区里消失了。

## 旧资源一行都不改

导入是**只读旧表 + 新建新行**：资源行与绑定行原样留着，回滚到旧版本时照旧能注入。
平台方确认网关好用以后，可以在资源页手动删掉这些旧资源。导入失败也只影响这一次，
下次启动会按 `import_ref` 幂等重来。

## 幂等与归属

`import_ref = resource:<资源 id>` 就是幂等键：同一工作区上已有同一来源的行时计入
`already`，不再导。导出的行一律是平台方归属（`owner_kind = platform`），因为存量资源
本来就是平台方建立、绑定给工作区用的；工作区必须先过 `platform_credentials_allowed`，
不过的今天本来也不会注入这些资源，导了就只是把凭据放到一个租户读得到的工作区上。
"""

from __future__ import annotations

import re
from typing import Dict, Optional

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_workspace_resource import DBWorkspaceResource
from nekro_agent.models.db_workspace_resource_binding import DBWorkspaceResourceBinding
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.schemas.ssh_gateway import ImportReport, ImportReportEntry, ImportReportSkip
from nekro_agent.services.resources.crypto import decrypt_secret_payload
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway.credentials import ssh_credential_service
from nekro_agent.services.ssh_gateway.validation import (
    validate_host,
    validate_private_key,
    validate_username,
)
from nekro_agent.services.workspace.credential_policy import platform_credentials_allowed

logger = get_sub_logger("ssh_gateway.import")

# 三个把 SSH 凭据注入容器环境的旧模板；其余模板与 SSH 网关无关
SSH_RESOURCE_TEMPLATES = ("server_ssh_key", "server_ssh_password", "server_root_password")

# 资源名转别名：连续的非小写字母 / 数字字符换成一个连字符
_ALIAS_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_MAX_ALIAS_LENGTH = 32
_FALLBACK_ALIAS_PREFIX = "srv-"

STARTUP_ACTOR = "import"


def _payload_text(payload: Dict[str, str], key: str) -> str:
    """从一个载荷字典里取文本；缺字段与 `None` 都按空串，避免把 `"None"` 导成主机名"""
    value = payload.get(key)
    return "" if value is None else str(value).strip()


def _payload_secret(payload: Dict[str, str], key: str) -> str:
    """凭据原样取：密码与口令短语首尾的空白也是凭据的一部分，去掉就成了另一个口令"""
    value = payload.get(key)
    return "" if value is None else str(value)


def _resource_port(public: Dict[str, str]) -> int:
    """端口：空值或不合法的取值一律按 22

    旧资源页对端口是自由文本，存量里存在空串、带空白甚至 `"22 "` 这类值。导入是存量
    修复，不该为一条填得不规范的老数据跳过整台服务器——SSH 的默认端口正是 22。
    """
    raw = _payload_text(public, "port")
    if not raw:
        return 22
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 22
    return value if 1 <= value <= 65535 else 22


def _slug_alias(name: str) -> str:
    """资源名转成合法的 SSH 别名；转不出合法别名时返回空串交给调用方兜底

    `nk-` 前缀要剥到干净为止（`nk-nk-gateway` 剥一次还是 `nk-gateway`，正好撞上容器
    ssh 配置里网关的 `HostKeyAlias`），最后再按别名规则核一遍。
    """
    lowered = name.strip().lower()
    hyphenated = _ALIAS_SEPARATOR_RE.sub("-", lowered).strip("-")
    if not hyphenated:
        return ""
    alias = hyphenated[: _MAX_ALIAS_LENGTH].strip("-")
    while alias.startswith(C.ALIAS_RESERVED_PREFIX):
        alias = alias[len(C.ALIAS_RESERVED_PREFIX) :].strip("-")
    return alias if C.is_valid_alias(alias) else ""


def _truncate_alias(alias: str, length: int) -> str:
    """按剩余长度截断并去掉末尾连字符；截完为空时由调用方兜底"""
    return alias[: max(length, 0)].strip("-")


async def _unique_alias(workspace_id: int, resource: DBWorkspaceResource) -> str:
    """这个工作区里可用的别名：同名冲突依次加 `-2`、`-3`，总长不超过 32"""
    base = _slug_alias(resource.name) or f"{_FALLBACK_ALIAS_PREFIX}{resource.id}"
    candidate = base
    suffix_no = 2
    while await DBWsSshCredential.filter(workspace_id=workspace_id, alias=candidate).exists():
        suffix = f"-{suffix_no}"
        truncated = _truncate_alias(base, _MAX_ALIAS_LENGTH - len(suffix))
        # 极端情况下截断把兜底别名也削空了；`srv-<id>` 本身很短，正常走不到这里
        candidate = f"{truncated or _FALLBACK_ALIAS_PREFIX}{suffix}"
        suffix_no += 1
    return candidate


class _MappedCredential:
    """一条资源映射出来的、还没落库的凭据字段"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        private_key: str,
        passphrase: str,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.private_key = private_key
        self.passphrase = passphrase


def _map_resource(resource: DBWorkspaceResource, secret: Dict[str, str]) -> Optional[_MappedCredential]:
    """按模板把公开字段与解密后的敏感字段映射成凭据字段；字段不全时返回 None

    只做映射与格式校验，不在这里写库：调用方要把"为什么跳过"统一收进报告。
    """
    public = resource.public_payload or {}
    host = _payload_text(public, "host")
    port = _resource_port(public)
    password = ""
    private_key = ""
    passphrase = ""

    if resource.template_key == "server_root_password":
        # root 模板只有主机、端口与口令：登录名固定 root
        username = "root"
        password = _payload_secret(secret, "password")
    elif resource.template_key == "server_ssh_password":
        username = _payload_text(public, "username")
        password = _payload_secret(secret, "password")
    else:
        username = _payload_text(public, "username")
        # 私钥文本的首尾空白不影响解析，照旧去掉；口令短语原样取
        private_key = _payload_text(secret, "private_key")
        passphrase = _payload_secret(secret, "passphrase")

    if not host:
        return None
    if not username:
        return None
    if not password and not private_key:
        return None

    return _MappedCredential(
        host=validate_host(host),
        port=port,
        username=validate_username(username),
        password=password,
        private_key=validate_private_key(private_key),
        passphrase=passphrase,
    )


def _skip(report: ImportReport, resource: DBWorkspaceResource, workspace_id: int, reason: str) -> None:
    report.skipped.append(
        ImportReportSkip(resource_name=resource.name, workspace_id=workspace_id, reason=reason),
    )


async def _import_one_binding(
    resource: DBWorkspaceResource,
    workspace: DBWorkspace,
    *,
    binding_enabled: bool,
    actor: str,
    report: ImportReport,
) -> None:
    """把一条（资源, 工作区）导入成一行凭据；幂等键是 `resource:<资源 id>`"""
    import_ref = f"resource:{resource.id}"
    if await DBWsSshCredential.filter(workspace_id=workspace.id, import_ref=import_ref).exists():
        report.already += 1
        return
    if not await platform_credentials_allowed(workspace):
        _skip(report, resource, workspace.id, "工作区不归平台方")
        return
    if not binding_enabled:
        _skip(report, resource, workspace.id, "绑定已停用")
        return
    if not resource.enabled:
        _skip(report, resource, workspace.id, "资源已停用")
        return

    try:
        secret = decrypt_secret_payload(resource.secret_payload_encrypted or "")
    except Exception as exc:  # noqa: BLE001 - 解码异常的种类由资源模块决定，文案就是原因
        # 不打印载荷本身：解不开的密文也是密文，日志里只留资源名与工作区
        _skip(report, resource, workspace.id, str(exc))
        return

    try:
        mapped = _map_resource(resource, secret)
    except Exception as exc:  # noqa: BLE001 - 字段格式问题同样按"这条跳过"处理
        _skip(report, resource, workspace.id, str(exc))
        return
    if mapped is None:
        _skip(report, resource, workspace.id, "主机、用户名或凭据字段不全（密码与私钥至少要有一项）")
        return

    alias = await _unique_alias(workspace.id, resource)
    try:
        await ssh_credential_service.create_credential_row(
            workspace_id=workspace.id,
            owner_kind=C.OWNER_PLATFORM,
            created_via=C.CREATED_VIA_IMPORT,
            import_ref=import_ref,
            alias=alias,
            host=mapped.host,
            port=mapped.port,
            username=mapped.username,
            password=mapped.password,
            private_key=mapped.private_key,
            passphrase=mapped.passphrase,
            # 首次连接时由网关记下目标主机公钥；导入阶段不猜、不连目标
            host_keys=[],
            note=f"导入自工作区资源 #{resource.id}：{resource.name}",
            created_by=actor,
        )
    except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整次导入
        _skip(report, resource, workspace.id, str(exc))
        return
    report.imported.append(
        ImportReportEntry(workspace_id=workspace.id, alias=alias, resource_name=resource.name),
    )


async def import_ssh_resources(*, actor: str) -> ImportReport:
    """把三个 SSH 模板的存量资源导入成平台方凭据行

    导入只发生在**启用的绑定**上：停用的绑定今天不注入，导过来等于凭空多出一台可用
    服务器；它仍然会进 `skipped` 并写清原因，平台方才知道那一条为什么没导。旧资源与
    绑定一行都不改；同一个 `(工作区, import_ref)` 已经存在时只计 `already`。

    Args:
        actor: 写进 `created_by` 与审计的操作者（启动导入是 `import`，手动触发是后台用户名）。
    """
    report = ImportReport()
    resources = await DBWorkspaceResource.filter(template_key__in=SSH_RESOURCE_TEMPLATES).order_by("id")
    for resource in resources:
        # 停用的绑定也走一遍报告：它今天不注入，但平台方需要看到"这条为什么没导"，
        # 而不是在结果页里凭空少一条。真正的导入仍然只发生在启用的绑定上
        bindings = await DBWorkspaceResourceBinding.filter(resource_id=resource.id).order_by("workspace_id")
        for binding in bindings:
            workspace = await DBWorkspace.get_or_none(id=binding.workspace_id)
            if workspace is None:
                _skip(report, resource, binding.workspace_id, "工作区不存在")
                continue
            await _import_one_binding(
                resource,
                workspace,
                binding_enabled=bool(binding.enabled),
                actor=actor,
                report=report,
            )

    logger.info(
        f"存量 SSH 工作区资源导入完成：导入 {len(report.imported)} 条，"
        f"已存在 {report.already} 条，跳过 {len(report.skipped)} 条",
    )
    if report.skipped:
        # 一条 WARNING 里逐个点名，运维才知道去改哪条资源 / 哪个工作区
        detail = "；".join(
            f"{item.resource_name}（工作区 {item.workspace_id}）：{item.reason}" for item in report.skipped
        )
        logger.warning(f"以下存量 SSH 工作区资源没有导入：{detail}")
    return report


async def import_ssh_resources_on_startup() -> ImportReport:
    """启动时跑一次；总开关关着时什么都不做

    闸关时旧资源照旧注入容器，此时导入只会让同一台服务器在资源页与凭据页各出现一次，
    而且凭据页的入口在网关关着时没有实际用途。手动接口不受这个开关限制：先把凭据备好
    再开闸是正常用法。

    调用点（`nekro_agent/__init__.py`）外面也有一层同条件的判断；这里再判一次是为了让
    这个函数单独调用时同样安全，用例也能直接钉住「闸关时什么都不导」。
    """
    if not config.SSH_GATEWAY_ENABLED:
        return ImportReport()
    return await import_ssh_resources(actor=STARTUP_ACTOR)


__all__ = [
    "SSH_RESOURCE_TEMPLATES",
    "STARTUP_ACTOR",
    "import_ssh_resources",
    "import_ssh_resources_on_startup",
]
