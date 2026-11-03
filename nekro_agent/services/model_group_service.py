"""模型组的归属判定、凭据隔离与运行时视图同步

## 事实来源与运行时视图

`DBModelGroup` 是模型组的唯一事实来源；`CoreConfig.MODEL_GROUPS` 降级为由本模块同步填充的
**运行时视图**。这样 `config.MODEL_GROUPS[config.USE_MODEL_GROUP]` 这类遍布全项目的同步读取
（`services/agent/run_agent.py`、`plugins/builtin/draw` 等）完全不用改，也不用被迫改成 async。

写入顺序固定为：**改库 → 重建运行时视图 → dump 配置文件 → bump 有效配置版本**。
配置文件里的 `MODEL_GROUPS` 仍是数据库的镜像，但其中的 `API_KEY` 不含明文（#61，见
`core/config.py`）：有 Key 的组是占位符 `nkmask:1`，没配 Key 的组是空串，两者在文件里
永远可区分——整目录打包的备份不再带出明文 Key，凭据的明文只存在于进程内的运行时视图里；
镜像也因此**不再是凭据的恢复来源**，丢密钥后的恢复路径只剩"找回密钥材料"
（docs/operations.md §1.5）。

## 存量迁移

`bootstrap_model_groups()` 在启动时把配置文件里已有的模型组按 `name` 逐个导入为平台组，
已存在同名行就跳过，因此重复启动不会重复导入，也不会覆盖管理员在后台改过的值。

数据库接管完成后，只要库里**没有**解不开的行，它就**每次启动都落盘一次**：升级部署里
YAML 的明文在首次启动即被掩码；有解不开的行时不落盘，保住 YAML 里可能是唯一副本的明文
（理由与日志见该函数的 docstring）。

YAML 里手填的 Key 与库里同名行的值不一致时（手填的没导入）：掩码之前先把这些组手填的原值备份到
`<DATA_DIR>/configs/` 下的仅属主可访问文件（`backup_handwritten_keys()`，**一份新的明文副本**，POSIX 上 0600，
Windows 上用 `icacls` 收紧 ACL——`0o600` 在那里只影响只读位；日志点名路径、如实说明保护到了什么程度、
要求取出后删除，之后**每次启动**发现它还在都再提醒），再照常掩码；备份失败才退回「不落盘」。
只跳过启动那一次落盘是漏的——后台编辑模型组同样会落盘并抹掉它。

## 凭据可见性

见 `schemas/tenant.ModelGroupInfo` 的契约。本模块的 `build_model_group_info()` 是该契约的
唯一实现处，任何对外暴露模型组的地方都必须走它，不要自己拼 `ModelGroupInfo`。

## 凭据的静态加密

`api_key` 在数据库里是密文，加解密由模型层的 `EncryptedTextField` 收口，本模块**不出现任何
解密调用**：`can_view_group()` / `mask_config_group()` / `to_config_group()` 拿到的
`row.api_key` 恒为明文。本模块只多做两件事——启动时把"还不是用当前主密钥加密"的行整批重写
（`upgrade_credentials_at_rest()`），以及 `DBModelGroup.save()` 上的回查守卫（#64：解不开的
密文不因一次普通保存被空串覆盖，见模型模块文档）。威胁模型（挡库 dump、挡不住宿主被拿下）
见 `tools/credential_cipher.py`。

## 新建组的可见性默认值

**private**，且**只有一份**：`models/db_model_group.DEFAULT_VISIBILITY`。
字段默认、本模块 `create_group()` 的形参兜底、`upsert_platform_group()`、
`import_config_group()` 全部引用它；WebUI 表单那一份由
`tests/test_model_group_default_visibility.py` 读 `.tsx` 钉在一起。
默认 shared 意味着新开一个租户即可选用平台方的模型组 —— 那是一条**授权**路径，
写入侧的越权检查管不到它。**接口层不许自带 `or <某个默认>`**（改造前
`routers/console/model_groups.py` 就有一句，它盖过了字段默认）。

## 「谁能引用哪个模型组」

可见性不只是读接口的过滤条件，更是**授权与计费边界**：`USE_MODEL_GROUP` 这类配置项决定
每一轮对话花谁的 API Key。因此除了读路径的 `can_view_group()`，还有两道由同一个判定派生的闸门：

- **写路径**：`assert_model_group_selectable()` —— 租户把组名写进配置项时校验一次，
  `routers/config.py:_assert_tenant_may_write` 调用；
- **运行时兜底**：`enforce_channel_model_group_scope()` —— 存量覆盖文件里可能早就写着越权的
  组名，接口层挡不住已落盘的，因此 `services/agent/run_agent.py` 取模型组前再校验一次。

两道闸门都**不维护字段名单**，一律由 `ExtraField(ref_model_groups=True)` 这个标记驱动，
见 `iter_model_group_ref_fields()`。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple, Type

import yaml
from pydantic import BaseModel
from tortoise import Tortoise

from nekro_agent.core.config import (
    MODEL_GROUP_API_KEY_PLACEHOLDER,
    CoreConfig,
    ModelConfigGroup,
    config,
    is_model_group_api_key_placeholder,
    save_config,
)
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_model_group import (
    DEFAULT_VISIBILITY,
    DBModelGroup,
    ModelGroupVisibility,
)
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    ConflictError,
    ModelGroupNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from nekro_agent.schemas.tenant import (
    CurrentIdentity,
    ModelGroupInfo,
    ModelGroupOwnership,
    TenantRole,
)
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.user.tenant_perm import get_tenant_scope, tenant_role_of
from nekro_agent.tools.common_util import (
    UnsafeDownloadUrlError,
    assert_tenant_endpoint_url_safe,
)
from nekro_agent.tools.credential_cipher import (
    REENCRYPT_SOURCES,
    classify_stored_credential,
    credential_cipher_status,
)
from nekro_agent.tools.redaction import is_credential_mask, mask_credential

logger = get_sub_logger("model_group")

# 组名字符集，最长 64 字符：
#
# - 文字：半角字母数字 + 中日韩文字。中日韩文字用**显式码段**而不是 `\w`：`\w` 把西里尔 / 希腊同形字
#   （`dеfault`）、全角字母（`ｄｅｆａｕｌｔ`）、甚至肉眼不可见的韩文填充符（U+3164 / U+FFA0 / U+115F，
#   类别 Lo）都算作字母。显式码段也让前后端不受各自 Unicode 版本影响（Python 3.11 与浏览器差一万多个码点）。
# - 标点：上游原版几乎不限字符，`GPT-4o（备用）`、`DeepSeek V3`、`qwen2.5:7b`、`通义千问·Max`、`【主】模型`
#   都是常见命名，这些标点照样放行；`/ \ ? # % & =` 这类 URL 结构字符、控制字符与零宽字符不放。
# - 首字符须是文字或左括号；不许连续空格（`a  b` 与 `a b` 肉眼几乎分不出），首尾空白在校验前去掉。
#
# 字符集挡不住所有形近字（片假名 カ 与汉字 力、英文 I 与 l）。防止平台方选错组的主手段是配置下拉框里
# 标出租户自建的组（`ConfigTable.tsx`），字符集只挡掉代价最低的那几类。
# 前端 `ModelGroupFormDialog.tsx` 的 `GROUP_NAME_PATTERN` 是同一条规则的 JS 写法，
# 两处由 `tests/test_model_group_name_and_batch_delete.py` 逐码点比对钉在一起
_NAME_LETTERS = (
    "A-Za-z0-9"
    "\u3400-\u4dbf\u4e00-\u9fff"  # 汉字：扩展 A、基本区
    "\U00020000-\U0002a6df\U0002a700-\U0002ee5f\U00030000-\U000323af"  # 汉字：扩展 B–I
    "\u3041-\u3096\u309d-\u309f"  # 平假名
    "\u30a1-\u30fa\u30fc-\u30ff"  # 片假名（含长音符 ー，不含中点 ・）
    "\uac00-\ud7a3"  # 韩文音节（不含填充符与单独字母）
)
# 可出现在文字之间的标点（含空格）：_ . - ( ) + : @ · （ ） 【 】 、 ：
_NAME_PUNCT = "_.\\-()+:@ \u00b7\uff08\uff09\u3010\u3011\u3001\uff1a"
# 可以打头的标点只有左括号：( （ 【
_NAME_OPENERS = "(\uff08\u3010"
_NAME_RE = re.compile(f"(?!.*  )[{_NAME_LETTERS}{_NAME_OPENERS}][{_NAME_LETTERS}{_NAME_PUNCT}]{{0,63}}")

# `DBModelGroup.name` 的列宽。更长的名字不可能存在，按不存在处理；不先拦下的话 Tortoise 在
# 过滤时就抛 `tortoise.exceptions.ValidationError`，一路冒成 500
_NAME_COLUMN_MAX = 128

# 该组名被 `USE_MODEL_GROUP` 等配置项当作兜底默认值，删掉会让全局配置直接指空
DEFAULT_GROUP_NAME = "default"


# ============================================================
# 身份解析
# ============================================================


def resolve_console_identity(user: DBUser) -> CurrentIdentity:
    """把登录用户解析成多租户身份

    归属判定一律委托给 `services/user/tenant_perm`，不在这里另写一套：凭据可见性是整个
    多租户里最敏感的判定，两处实现一旦出现分歧就是一次泄露。

    `tenant_id` 为 None 即平台方，也是未启用多租户时的唯一分支——此时全部账号都是平台方，
    看到的模型组与凭据与改造前完全一致。

    这里不用 `tenant_perm.resolve_current_identity()` 是因为那个版本是 async 且会为
    `tenant_name` / `accessible_instance_keys` 额外查库，而模型组的判定只用得到 `tenant_id`
    与 `role` 两个字段。
    """
    return CurrentIdentity(
        user_id=user.id,
        username=user.username,
        role=tenant_role_of(user),
        tenant_id=get_tenant_scope(user),
    )


def is_platform_owner(identity: CurrentIdentity) -> bool:
    """当前身份是否为平台方"""
    return identity.tenant_id is None


# ============================================================
# 可见性与凭据掩码
# ============================================================


def can_view_group(group: DBModelGroup, identity: CurrentIdentity) -> bool:
    """当前身份能否看到该模型组

    租户看不到的组不是"只读展示"，而是**根本不出现在列表里**，单个查询也按不存在处理，
    避免用 404/403 的差异反推出别的租户配了哪些组。
    """
    if is_platform_owner(identity):
        return True
    if group.is_platform_owned:
        return group.visibility == ModelGroupVisibility.SHARED.value
    return group.owner_tenant_id == identity.tenant_id


def can_see_credential(group: DBModelGroup, identity: CurrentIdentity) -> bool:
    """当前身份能否看到该模型组的真实凭据

    平台组的凭据只有平台方能看。租户即便能选用平台组，也只拿得到掩码。
    """
    if is_platform_owner(identity):
        return True
    return not group.is_platform_owned and group.owner_tenant_id == identity.tenant_id


def can_edit_group(group: DBModelGroup, identity: CurrentIdentity) -> bool:
    """当前身份能否修改该模型组"""
    if is_platform_owner(identity):
        return True
    if identity.role != TenantRole.TENANT_ADMIN:
        return False
    return not group.is_platform_owned and group.owner_tenant_id == identity.tenant_id


def build_model_group_info(group: DBModelGroup, identity: CurrentIdentity) -> ModelGroupInfo:
    """按 `schemas/tenant.ModelGroupInfo` 的契约构造对外形态"""
    credential_visible = can_see_credential(group, identity)
    return ModelGroupInfo(
        name=group.name,
        ownership=(
            ModelGroupOwnership.PLATFORM if group.is_platform_owned else ModelGroupOwnership.TENANT
        ),
        owner_tenant_id=group.owner_tenant_id,
        visibility=group.visibility,
        base_url=group.base_url if credential_visible else mask_credential(group.base_url),
        api_key=group.api_key if credential_visible else mask_credential(group.api_key),
        chat_model=group.chat_model,
        model_type=group.model_type,
        editable=can_edit_group(group, identity),
        credential_visible=credential_visible,
        # 编辑表单要回填全部字段；掩码规则与上面的顶层字段同源，见 `mask_config_group`
        config=mask_config_group(group, identity),
    )


def mask_config_group(group: DBModelGroup, identity: CurrentIdentity) -> ModelConfigGroup:
    """构造对外可见的完整 `ModelConfigGroup`

    凭据不可见时把 `API_KEY` / `BASE_URL` / `CHAT_PROXY` 一并换成掩码串：地址同样暴露平台方
    接的是哪家上游、走的什么代理，契约里与凭据同等对待。
    """
    cfg = group.to_config_group()
    if can_see_credential(group, identity):
        return cfg
    cfg.API_KEY = mask_credential(cfg.API_KEY)
    cfg.BASE_URL = mask_credential(cfg.BASE_URL)
    cfg.CHAT_PROXY = mask_credential(cfg.CHAT_PROXY)
    return cfg


# ============================================================
# 出站地址校验
# ============================================================


async def assert_outbound_urls_allowed(identity: CurrentIdentity, base_url: str, chat_proxy: str = "") -> None:
    """校验模型组的出站地址

    租户填的 `base_url` / `chat_proxy` 等于让 NA 服务器代为发起出站请求，必须强制公网，
    否则 `http://127.0.0.1:8021/api/...` 就能以 NA 自己的网络身份打内网。

    平台方豁免这项校验，理由是平台方本来就是宿主的所有者：自建部署常见把 vLLM / Ollama /
    LiteLLM 之类的推理服务放在同一台机器或内网另一台机器上，用 `http://127.0.0.1:11434/v1`
    接入是正常用法；对平台方强制公网等于禁掉本地推理这个主流场景，而平台方本就能直接
    操作宿主，拦这一层不增加任何实际安全边界。
    """
    if is_platform_owner(identity):
        return
    for url in (base_url, chat_proxy):
        if not url.strip():
            continue
        try:
            await assert_tenant_endpoint_url_safe(url.strip())
        except UnsafeDownloadUrlError as e:
            raise ValidationError(reason=str(e)) from e


# ============================================================
# 运行时视图与存量迁移
# ============================================================


async def refresh_runtime_view(persist: bool = True) -> Dict[str, ModelConfigGroup]:
    """用数据库重建 `CoreConfig.MODEL_GROUPS` 运行时视图

    Args:
        persist: 是否顺带把视图 dump 回配置文件。启动期只有一种情形会关掉它：库里存在
            当前密钥解不开的行（见 `bootstrap_model_groups`）。
    """
    rows = await DBModelGroup.all().order_by("id")
    view: Dict[str, ModelConfigGroup] = {row.name: row.to_config_group() for row in rows}
    config.MODEL_GROUPS = view
    # 「库里解不开、YAML 原值暂由落盘钩子原样写回」的记账按视图的真值修剪：组被删了、库里的 Key 已可用
    # 或被后台显式换成新 Key（视图里 Key 非空），库已接管，旧的 YAML 原值不再钉着（A4）
    config.prune_held_model_groups()
    if persist:
        save_config()
    # 模型组是全局配置，已缓存有效配置的频道必须重新解析
    config_resolver.bump_version()
    return view


class CredentialUpgradeReport(NamedTuple):
    """`upgrade_credentials_report()` 的结果：重写了谁、有谁解不开"""

    upgraded: List[str]
    # 库里是密文但当前密钥解不开的组名。`bootstrap_model_groups()` 靠它决定要不要落盘 YAML
    unreadable: List[str]


async def upgrade_credentials_at_rest() -> List[str]:
    """`upgrade_credentials_report()` 的旧入口：只回「被重写的组名」（测试与日志用）"""
    return (await upgrade_credentials_report()).upgraded


async def upgrade_credentials_report() -> CredentialUpgradeReport:
    """把库里还不是"用当前主密钥加密"的 `api_key` 整批重写一遍

    两种命中情形（判据是 `tools/credential_cipher.REENCRYPT_SOURCES`，
    与 `needs_reencrypt()` 共用同一份，不在这里另写一套）：

    - **存量明文**：加密上线之前写进去的行。读路径本来就兼容它（原样返回），
      但只兼容不升级的话，存量部署的 Key 会永远裸着——而那正是要解决的问题。
    - **用历史密钥加密**：密钥从数据目录搬进环境变量之后的第一次启动。

    解不开的密文**跳过**（不在 `REENCRYPT_SOURCES` 里）：重写等于把一个还可能被原密钥
    救回来的值彻底抹掉。跳过的组会在启动日志里**逐个点名**——读取时那条 ERROR 只说
    "有一行解不开"，不说是哪个组，运维拿着它没法行动；而这里本来就查着整张表，
    组名就在手上，不点名等于把已经拿到的信息扔掉。

    跳过不是只在这一条路径上孤立成立的：`DBModelGroup.save()` 上有一道回查守卫，
    解不开的行在**任何一次普通保存**里也会原样保留
    `api_key` 原列——后台改个名字、走一遍"掩码=保持不变"都不再毁掉密文。
    仍然会覆盖它的只剩"显式填入新 Key"（那本来就是覆盖指令）。

    直接读原始列而不是读 ORM 属性：`row.api_key` 已经被模型层解密过，
    从它身上看不出"库里存的是明文还是密文"。

    幂等：跑第二遍时没有任何行命中。返回被重写的组名与解不开的组名，供启动日志、
    落盘决策（`bootstrap_model_groups`：有解不开的行就不落盘）与测试断言。
    """
    # 没有可用密钥时不重写（只会把明文再写一遍明文，白白 bump 一次 update_time），
    # 但**仍要扫一遍**：库里若躺着旧密钥时代留下的密文，它们此刻同样解不开，
    # 而落盘决策需要知道这件事。
    rewrite_allowed = credential_cipher_status().available

    conn = Tortoise.get_connection("default")
    rows = await conn.execute_query_dict('SELECT "id", "name", "api_key" FROM "model_group"')
    stale_ids: List[int] = []
    unreadable: List[str] = []
    for row in rows:
        stored = row.get("api_key")
        if not isinstance(stored, str) or not stored:
            continue
        # 一趟分档，两个答案：要重写的、以及解不开只能点名的
        source = classify_stored_credential(stored)
        if source in REENCRYPT_SOURCES:
            stale_ids.append(int(row["id"]))
        elif source == "undecryptable":
            unreadable.append(str(row["name"]))
    if not rewrite_allowed:
        stale_ids = []

    if unreadable:
        logger.error(
            f"以下模型组的凭据当前密钥解不开，本次启动**不会**重写它们: {', '.join(unreadable)}。"
            "请先把原来的 NEKRO_CREDENTIAL_SECRET_KEY（或数据目录里那把密钥文件）放回去再重启；"
            "在这些组上保存其他字段不会覆盖原密文（save 守卫会原样保留），"
            "但**填入新 Key 仍会覆盖**——想找回原值就先恢复密钥。",
        )

    if not stale_ids:
        return CredentialUpgradeReport(upgraded=[], unreadable=unreadable)

    upgraded: List[str] = []
    for group_id in stale_ids:
        row = await DBModelGroup.get_or_none(id=group_id)
        if row is None:  # pragma: no cover - 启动期没有并发删除
            continue
        # `row.api_key` 此刻是明文（模型层已解密），存回去时由 `EncryptedTextField` 用主密钥加密
        await row.save(update_fields=["api_key"])
        upgraded.append(row.name)
    return CredentialUpgradeReport(upgraded=upgraded, unreadable=unreadable)


# 手填 Key 备份文件名前缀；文件落在 `<DATA_DIR>/configs/`，名字里带时间戳，已有的同名文件不覆盖
HANDWRITTEN_KEYS_BACKUP_PREFIX = "nekro-agent.model-groups-handwritten-keys"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _restrict_to_current_user_windows(path: Path) -> bool:
    """Windows：去掉继承、只授予当前用户完全控制（`icacls`），成功返回 True

    `os.open(..., 0o600)` 在 Windows 上只影响只读位，文件实际继承目录的 ACL（通常 `Users` 组可读）。
    失败（没有 `icacls`、取不到用户名、命令返回非 0）不抛：调用方据返回值在日志里如实说明，不再声称已受保护。
    """
    username = os.environ.get("USERNAME", "")
    if not username:
        return False
    domain = os.environ.get("USERDOMAIN", "")
    principal = f"{domain}\\{username}" if domain else username
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:F"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _write_private_file(path: Path, content: str) -> bool:
    """**新建**并写入一份仅属主可访问的文件，返回「访问权限是否确实收紧了」

    `O_EXCL`：不覆盖已有文件。POSIX 上权限在创建那一刻就是 0600，不存在先 644 再 chmod 的窗口，返回 True；
    Windows 上 0600 不是 ACL，写完后用 `icacls` 显式收紧，返回收紧是否成功（False = 文件继承了目录权限）。
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    if _is_windows():
        return _restrict_to_current_user_windows(path)
    return True


class HandwrittenKeysBackup(NamedTuple):
    """`backup_handwritten_keys()` 的结果：写在哪、访问权限是否确实收紧了"""

    path: Path
    owner_only: bool


def describe_backup_protection(owner_only: bool) -> str:
    """日志里说备份文件「保护到了什么程度」的那半句：随平台与实际结果走，不写死 0600"""
    if not _is_windows():
        return "权限 0600，仅属主可读写"
    if owner_only:
        return "已用 icacls 把访问权限收紧为仅当前用户"
    return "**Windows 上未能收紧访问权限**，文件继承了数据目录的权限，同机的其他账户可能读到它"


def find_handwritten_keys_backups() -> List[Path]:
    """数据目录里现存的手填 Key 备份文件（按名字排序）"""
    directory = Path(OsEnv.DATA_DIR) / "configs"
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{HANDWRITTEN_KEYS_BACKUP_PREFIX}.*"))


def backup_handwritten_keys(groups: Dict[str, ModelConfigGroup]) -> HandwrittenKeysBackup:
    """把 YAML 里手填、却没导入数据库的模型组原值备份成一个仅属主可访问的文件，返回路径与保护结果

    **这是一份新的明文副本**，与「整目录打包的备份不再带出明文 Key」的目标有张力，所以：只含分歧的组
    （与库值一致的组明文本来就在库里、不进来）；一次性（掩码之后重启不再分歧，不会越攒越多）；
    调用方在日志里逐字点名路径、如实说明保护程度并要求取出后删除，之后每次启动还在就再提醒；
    失败不吞（调用方据此退回「不落盘」）。
    不选「运行期保留该组 YAML 原值」，是因为后者要让明文无限期留在 YAML 里，
    而且操作者在后台改了 Key 之后那份旧明文仍会赖着不走。
    """
    directory = Path(OsEnv.DATA_DIR) / "configs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{HANDWRITTEN_KEYS_BACKUP_PREFIX}.{stamp}.yaml"
    header = (
        "# 含明文 API Key！由 NekroAgent 在启动时生成：这些模型组在数据库里已有同名记录，配置文件里手填的 Key 与库里的值不一致，\n"
        "# 没有被导入；配置文件随后按常规掩码。取出需要的 Key（在后台编辑该模型组填入）后请删除本文件。\n"
    )
    body = yaml.safe_dump(
        {"MODEL_GROUPS": {name: group.model_dump(mode="json") for name, group in groups.items()}},
        allow_unicode=True,
        sort_keys=False,
    )
    owner_only = _write_private_file(path, header + body)
    return HandwrittenKeysBackup(path=path, owner_only=owner_only)


async def bootstrap_model_groups() -> None:
    """启动时把配置文件里的存量模型组导入数据库，并重建运行时视图

    幂等：导入以 `name` 是否已存在为准，重复启动只会命中"已存在"分支。

    **落盘规则**：数据库接管完成、且库里**没有**解不开的行时，**每次启动
    都落盘一次**——YAML 里的 `API_KEY` 因此在升级后的**首次启动**就被掩码成占位符/空串，
    而不是等到下一次碰巧的配置落盘（旧实现 `persist=bool(imported)`：普通重启不落盘，
    升级部署的明文 YAML 会一直留着）。**存在解不开的行时不落盘**：那些组在 YAML 里可能
    还躺着明文，而它很可能是该凭据仅剩的一份副本，此刻抹掉就再也找不回来了；日志里逐个
    点名并说明，原密钥恢复后重启即自动掩码。
    """
    # 加载时 YAML 里是占位符的组（`CoreConfig.load_config` 记下的）。它们在内存里已被归一成
    # 空串——不还原回占位符的话，下面「拿占位符 YAML 配全新数据库恢复」那条告警在生产上
    # 永远走不到（`import_config_group` 看到的只会是空串，无从区分「没配 Key」与「Key 不在
    # 这份文件里」）。
    placeholder_names = config.placeholder_model_group_names()

    # 启动之前就留在数据目录里的手填 Key 备份（上一次启动写的）：那是一份明文副本，会被整目录备份带走，
    # 只在写出的那一次叮嘱「取出后删除」不够——错过那一条之后没人再提醒。每次启动还在就点名再说一遍。
    # 必须在本次启动可能写出新备份**之前**扫：新写的那份由下面「手填」那条告警负责说明。
    leftovers = find_handwritten_keys_backups()
    if leftovers:
        logger.warning(
            f"数据目录里残留 {len(leftovers)} 份手填 API Key 的备份（**含明文**，会被整目录备份一并带走）: "
            f"{'；'.join(str(path) for path in leftovers)}。需要的 Key 请在后台编辑对应模型组填入，"
            "取出后请删除这些文件。",
        )

    # 库里已有同名行时导入会跳过（库内行绝不被覆盖），若此时 YAML 里手填的明文 Key 与库值不一致，
    # 那份明文既没导入、又会在落盘里被掩码抹掉——静默丢失。
    # 在导入之前比对（`import_config_group` 之后同名行必然已存在，信息就没了）。占位符 / 空串不是手填的 Key，不参与。
    # 处理：只跳过启动这一次落盘是漏的——「后台编辑该模型组」同样会落盘并抹掉它。
    # 所以首次掩码落盘之前先把分歧组手填的原值备份成一个 0600 文件（见 `backup_handwritten_keys`），
    # 然后照常掩码；备份写不出来才退回「不落盘」并如实告知。
    diverged: List[str] = []
    for name, group in config.MODEL_GROUPS.items():
        if not group.API_KEY or is_model_group_api_key_placeholder(group.API_KEY):
            continue
        existing = await DBModelGroup.get_or_none(name=name)
        if existing is not None and existing.api_key != group.API_KEY:
            diverged.append(name)

    imported: List[str] = []
    for name, group in list(config.MODEL_GROUPS.items()):
        if name in placeholder_names and not group.API_KEY:
            group = group.model_copy(update={"API_KEY": MODEL_GROUP_API_KEY_PLACEHOLDER})
        if await DBModelGroup.import_config_group(name, group) is not None:
            imported.append(name)

    if imported:
        logger.info(f"已将配置文件中的存量模型组导入数据库（归属平台方，可见性 private）: {', '.join(imported)}")

    # 凭据静态加密的状态每次启动都说一遍：它决定了库里那一列是密文还是明文，
    # 而"以为加密了其实没有"是这类功能最贵的失败形态
    status = credential_cipher_status()
    describe = f"模型组凭据静态加密：密钥来源 = {status.describe()}"
    if status.available:
        logger.info(describe)
    else:
        logger.error(f"{describe} —— 凭据将以**明文**落库，与改造前一致。请配置 NEKRO_CREDENTIAL_SECRET_KEY。")

    report = await upgrade_credentials_report()
    if report.upgraded:
        logger.info(f"已将 {len(report.upgraded)} 个模型组的凭据重新加密落库: {', '.join(report.upgraded)}")

    if report.unreadable:
        logger.warning(
            f"模型组 {', '.join(report.unreadable)} 的库内凭据当前密钥解不开，本次启动**未落盘**配置文件："
            "YAML 里这些组的 API_KEY 可能是该凭据仅剩的明文副本，保持原样不动（此时 YAML 里可能仍有明文，"
            "注意备份的保管）。恢复原密钥（NEKRO_CREDENTIAL_SECRET_KEY 或数据目录里的密钥文件）后重启，"
            "YAML 会自动掩码。",
        )
    # 备份只在「这次确实要掩码落盘」时做：有解不开的行时本来就不落盘（明文留在 YAML 里），再备份只会多出一份明文
    backup_path: Optional[Path] = None
    backup_owner_only = False
    backup_error: Optional[Exception] = None
    if diverged and not report.unreadable:
        try:
            backup = backup_handwritten_keys({name: config.MODEL_GROUPS[name] for name in diverged})
            backup_path, backup_owner_only = backup.path, backup.owner_only
        except Exception as e:
            backup_error = e
    if diverged:
        names = ", ".join(diverged)
        head = (
            f"模型组 {names} 在数据库里已有同名记录，而配置文件里手填的 API_KEY 与库里的值不一致："
            "库内行不会被配置文件覆盖，这份手填的 Key **没有**被导入，运行时用的是库里的值。"
        )
        if backup_path is not None:
            logger.warning(
                f"{head}配置文件已按常规掩码落盘；掩码之前已把手填的原值备份到 {backup_path}"
                f"（{describe_backup_protection(backup_owner_only)}；**含明文**，位于数据目录下，会被整目录备份一并带走）。"
                "需要这把 Key 就在后台编辑该模型组并填入，取出后请删除该备份文件。",
            )
        else:
            reason = (
                f"手填的原值备份失败（{backup_error!r}），"
                if backup_error is not None
                else "库里有当前密钥解不开的行（见上），"
            )
            logger.warning(
                f"{head}{reason}本次启动因此**未落盘**配置文件，YAML 里的明文保持原样。"
                "注意：在后台编辑任一模型组都会触发落盘，并把 YAML 里这份手填的明文掩码掉——"
                "请先自行保存该 Key（或先把 YAML 里的 API_KEY 删掉/改成占位符再处理）。",
            )
    # 库里解不开的组，其 YAML 原值（明文或占位符）此刻是该凭据在库外仅剩的痕迹：记下来，之后任何一次
    # 别的配置落盘（WebUI 保存系统配置、后台改别的模型组）都对它们原样写回，而不是随内存里 fail-closed 的
    # 空串一起抹掉。每次启动整体重记：原密钥放回去重启后 `report.unreadable` 为空，记账随之清零。
    # 取舍：只管「别的落盘」，启动这一趟的整体不落盘保持原状——那条已有用例钉着，且
    # 同时落盘「别的组照常掩码」要拆开那条语义，方案变大。
    held_values: Dict[str, str] = {}
    for name in report.unreadable:
        group = config.MODEL_GROUPS.get(name)
        if group is None:
            continue
        if group.API_KEY:
            held_values[name] = group.API_KEY
        elif name in placeholder_names:
            held_values[name] = MODEL_GROUP_API_KEY_PLACEHOLDER
    config.hold_model_group_yaml_values(held_values)

    # 数据库从这里起接管模型组：落盘形态只由视图里的真值决定，不再按「加载时是占位符」写回
    config.release_model_group_placeholders()
    protect_yaml = bool(report.unreadable) or (bool(diverged) and backup_path is None)
    view = await refresh_runtime_view(persist=not protect_yaml)
    logger.info(f"模型组运行时视图就绪，共 {len(view)} 组")


# ============================================================
# 查询与增删改
# ============================================================


async def list_visible_groups(identity: CurrentIdentity) -> List[ModelGroupInfo]:
    """列出当前身份可见的模型组"""
    rows = await DBModelGroup.all().order_by("id")
    return [build_model_group_info(row, identity) for row in rows if can_view_group(row, identity)]


async def get_visible_group(name: str, identity: CurrentIdentity) -> DBModelGroup:
    """取出当前身份可见的模型组；不可见时按不存在处理"""
    if len(name) > _NAME_COLUMN_MAX:
        raise ModelGroupNotFoundError(name=f"{name[:_NAME_COLUMN_MAX]}…")
    row = await DBModelGroup.get_or_none(name=name)
    if row is None or not can_view_group(row, identity):
        raise ModelGroupNotFoundError(name=name)
    return row


async def get_editable_group(name: str, identity: CurrentIdentity) -> DBModelGroup:
    """取出当前身份可修改的模型组"""
    row = await get_visible_group(name, identity)
    if not can_edit_group(row, identity):
        raise PermissionDeniedError
    return row


def validate_group_name(name: str) -> str:
    """校验模型组名

    组名同时是运行时视图的字典键、配置文件里的 YAML 键（`allow_unicode=True` 落盘）与
    `/console/model-groups/{name}` 的路径参数，限制字符集避免出现无法引用的名字。
    上游原版允许中文组名与常见标点，这里同样放行；用 `fullmatch` 而不是 `^...$`：后者的 `$`
    会放过结尾的换行。
    """
    normalized = name.strip()
    if not _NAME_RE.fullmatch(normalized):
        raise ValidationError(
            reason=(
                "模型组名可用中文（及日文假名、韩文）、半角字母数字、空格与 _ . - ( ) + : @ · （ ） 【 】 、 ：，"
                "需以文字、字母、数字或左括号开头，不能有连续空格，最长 64 字符"
            ),
        )
    return normalized


async def create_group(
    identity: CurrentIdentity,
    name: str,
    group: ModelConfigGroup,
    # 未指定即 `DEFAULT_VISIBILITY`（private）。**不要在这里写字面量默认**：
    # 默认值只有一份，在 `models/db_model_group.py`，字段默认与这里引用的是同一个常量
    visibility: Optional[ModelGroupVisibility] = None,
) -> DBModelGroup:
    """新建模型组；租户创建的自动归属该租户"""
    normalized = validate_group_name(name)
    if await DBModelGroup.filter(name=normalized).exists():
        # 组名全局唯一：运行时视图是扁平字典，重名会互相覆盖
        raise ConflictError(resource=f"模型组 '{normalized}'")

    await assert_outbound_urls_allowed(identity, group.BASE_URL, group.CHAT_PROXY)

    # 占位符不是凭据：新建组收到占位串时按"没填 Key"落地（#61）
    if is_model_group_api_key_placeholder(group.API_KEY):
        group.API_KEY = ""

    platform_owned = is_platform_owner(identity)
    row = DBModelGroup(
        name=normalized,
        ownership=(ModelGroupOwnership.PLATFORM if platform_owned else ModelGroupOwnership.TENANT).value,
        owner_tenant_id=None if platform_owned else identity.tenant_id,
        # 租户自建组恒为私有：它承载的是该租户自己的凭据，没有"共享给别的租户"这一说
        visibility=((visibility or DEFAULT_VISIBILITY) if platform_owned else ModelGroupVisibility.PRIVATE).value,
    )
    row.apply_config_group(group)
    await row.save()
    await refresh_runtime_view()
    logger.info(f"新建模型组 {normalized}（归属 {row.ownership}，租户 {row.owner_tenant_id}）")
    return row


async def update_group(
    identity: CurrentIdentity,
    row: DBModelGroup,
    group: ModelConfigGroup,
    visibility: Optional[ModelGroupVisibility] = None,
) -> DBModelGroup:
    """更新模型组

    `group` 中的 `API_KEY` / `BASE_URL` / `CHAT_PROXY` 若等于掩码串，视为"保持不变"：
    前端读接口拿到的是掩码，原样提交回来不能把真实凭据覆盖成 `********`。
    `API_KEY` 等于配置文件占位符（`MODEL_GROUP_API_KEY_PLACEHOLDER`，#61）时同样
    视为"保持不变"：占位符说的是"Key 只在数据库里"，不是一次改 Key 指令。
    """
    if is_credential_mask(group.API_KEY) or is_model_group_api_key_placeholder(group.API_KEY):
        group.API_KEY = row.api_key
    if is_credential_mask(group.BASE_URL):
        group.BASE_URL = row.base_url
    if is_credential_mask(group.CHAT_PROXY):
        group.CHAT_PROXY = row.chat_proxy

    if group.BASE_URL != row.base_url or group.CHAT_PROXY != row.chat_proxy:
        await assert_outbound_urls_allowed(identity, group.BASE_URL, group.CHAT_PROXY)

    row.apply_config_group(group)
    if visibility is not None and is_platform_owner(identity) and row.is_platform_owned:
        row.visibility = visibility.value
    await row.save()
    await refresh_runtime_view()
    logger.info(f"更新模型组 {row.name}")
    return row


async def delete_group(row: DBModelGroup) -> None:
    """删除模型组"""
    if row.name == DEFAULT_GROUP_NAME:
        raise ValidationError(reason="默认模型组不可删除")
    # 按影响行数判定，与批量删除同一口径：并发里已被别人删掉的组报不存在，而不是报"已删除"
    if not await DBModelGroup.filter(id=row.id).delete():
        raise ModelGroupNotFoundError(name=row.name)
    await refresh_runtime_view()
    logger.info(f"删除模型组 {row.name!r}")


class DeleteRefusal(str, Enum):
    """批量删除里某个组没删成的原因

    与单个删除端点的结论一一对应，不多分一档：不存在与"存在但不可见"同为 `not_found`
    （两者一旦可区分，差异本身就是组名存在性探针，见 `can_view_group`）；
    看得见但改不了（租户眼里的平台 shared 组、只读成员）是 `forbidden`。
    `error` 是意料之外的异常（详情只进日志）：单个组出错不能中断整批，也不能让已删的那些组
    在响应里"消失"——那正是"删了一半再 500、调用方不知道删了哪些"。
    """

    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    DEFAULT_GROUP = "default_group"
    ERROR = "error"


class BatchDeleteOutcome(NamedTuple):
    """批量删除的结算结果"""

    deleted: List[str]
    refused: List[Tuple[str, DeleteRefusal]]


# 批量删除收尾（重建运行时视图）的后台任务。被 `asyncio.shield` 保护的内层任务在外层被取消后
# 仍要跑完，这里持有强引用，免得它在跑到一半时被回收
_SETTLING_TASKS: Set["asyncio.Task[None]"] = set()


def _shown_name(name: str) -> str:
    """回显 / 写日志用的组名：超过列宽的截断（那种名字不可能存在，原样回显只是放大请求）"""
    return name if len(name) <= _NAME_COLUMN_MAX else f"{name[:_NAME_COLUMN_MAX]}…"


async def _settle_batch_delete(deleted: List[str], identity: CurrentIdentity) -> None:
    try:
        await refresh_runtime_view()
    except Exception:
        # 行已经删了、视图没跟上：审计信息不能随这次失败一起丢掉。视图会在下一次任意模型组写入或重启时重建
        logger.exception(f"批量删除模型组 {deleted!r} 已落库，但重建运行时视图失败")
        raise
    logger.info(f"批量删除模型组 {deleted!r}（操作者 {identity.username!r}，租户 {identity.tenant_id}）")


async def delete_groups(names: Sequence[str], identity: CurrentIdentity) -> BatchDeleteOutcome:
    """批量删除模型组

    每个组名走与单个删除**同一套**判定（`get_editable_group` + 默认组保护），不另写权限逻辑。
    逐个结算而不是全有全无（同 `routers/console/quota.py` 的批量配额）：列表是旧的、某个组刚被
    别人删掉时，其余组照常删除，结果里如实列出哪些没删成、为什么。重复的组名只处理一次。

    单个组上意料之外的异常记为 `error` 并接着处理下一个（同批量配额的"单个失败不中断整批"）。
    删除按影响行数判定：两个管理员同时删同一个组时，后到的那一边删了 0 行，记 `not_found`，
    不会两边都报"已删除"。

    运行时视图只在最后重建一次（单个删除每次都重建并落盘配置文件，批量时没必要重复 N 次）。
    重建放在 `finally` 里，并用 `asyncio.shield` 包住：请求被取消时，已经删掉的行也必须从运行时
    视图里消失，否则对话仍会拿着已删除组的凭据去调上游，配置文件镜像里也还留着它们（下次启动会被
    当作存量组重新导入）。只靠 `finally` 不够：anyio 的取消作用域（Starlette 中间件、uvicorn 关停时
    取消在途请求）会在作用域内的**每一次** await 上重复投递取消，`finally` 里的重建同样会被打断。

    回显与日志里的组名都经 `_shown_name` 截断、日志用 `!r` 转义，换行之类不会伪造出新的日志行。
    """
    deleted: List[str] = []
    refused: List[Tuple[str, DeleteRefusal]] = []
    try:
        for name in dict.fromkeys(names):
            shown = _shown_name(name)
            try:
                row = await get_editable_group(name, identity)
                # 先判权限再判默认组：租户看不见的 default 必须报 not_found，而不是泄露"它是默认组"
                if row.name == DEFAULT_GROUP_NAME:
                    refused.append((shown, DeleteRefusal.DEFAULT_GROUP))
                    continue
                if not await DBModelGroup.filter(id=row.id).delete():
                    refused.append((shown, DeleteRefusal.NOT_FOUND))
                    continue
            except ModelGroupNotFoundError:
                refused.append((shown, DeleteRefusal.NOT_FOUND))
                continue
            except PermissionDeniedError:
                refused.append((shown, DeleteRefusal.FORBIDDEN))
                continue
            except Exception:  # noqa: BLE001 - 单个组失败不能中断整批
                logger.exception(f"批量删除模型组时出错，跳过该组: {shown!r}")
                refused.append((shown, DeleteRefusal.ERROR))
                continue
            deleted.append(row.name)
    finally:
        if deleted:
            settling = asyncio.ensure_future(_settle_batch_delete(list(deleted), identity))
            _SETTLING_TASKS.add(settling)
            settling.add_done_callback(_SETTLING_TASKS.discard)
            await asyncio.shield(settling)
    return BatchDeleteOutcome(deleted=deleted, refused=refused)


async def upsert_platform_group(name: str, group: ModelConfigGroup) -> DBModelGroup:
    """平台方视角的"写入模型组"（新建或整体覆盖）

    供旧版 `/config/model-groups/{name}` 端点调用：那条路径已限定平台方专属，
    但仍必须落到数据库，否则重启后运行时视图按数据库重建，这次修改会被静默回滚。

    新建时可见性取 `DEFAULT_VISIBILITY`（private，与 `create_group` / 字段默认同一个常量）；命中已有行时
    **不动它的可见性** —— 这条路径的语义是"覆盖模型字段"，把别人显式设过的
    shared 顺手改掉属于越权修改。

    `API_KEY` 等于掩码串或配置文件占位符时视为"保持不变"（理由同 `update_group`）：
    这条路径按组名**整体覆盖**，`/config/model-groups/{name}` 的调用方只对
    `CREDENTIAL_MASK` 做了还原，占位符会原样穿透到这——没有这层防御，
    一次回显提交就会把库里的密文覆盖成一串占位文本的"密文"。
    """
    normalized = validate_group_name(name)
    row = await DBModelGroup.get_or_none(name=normalized)
    if row is None:
        row = DBModelGroup(
            name=normalized,
            ownership=ModelGroupOwnership.PLATFORM.value,
            owner_tenant_id=None,
            visibility=DEFAULT_VISIBILITY.value,
        )
        # 新建分支：占位符按"没填 Key"落地
        if is_model_group_api_key_placeholder(group.API_KEY):
            group.API_KEY = ""
    elif is_credential_mask(group.API_KEY) or is_model_group_api_key_placeholder(group.API_KEY):
        # 覆盖分支：掩码/占位不是一次改 Key 指令，保留库里的现值
        group.API_KEY = row.api_key
    row.apply_config_group(group)
    await row.save()
    await refresh_runtime_view()
    return row


async def delete_platform_group(name: str) -> None:
    """平台方视角的"删除模型组"，理由同 `upsert_platform_group`"""
    await DBModelGroup.filter(name=name).delete()
    await refresh_runtime_view()


# ============================================================
# 引用模型组的配置项：写入闸门与运行时兜底
# ============================================================

# 「这个配置项引用了一个模型组」的权威标记，定义见 `core/core_utils.ExtraField.ref_model_groups`。
# 判定一律由这个标记驱动，**任何地方都不要另维护一份字段名单**：
# 今天是 `USE_MODEL_GROUP` / `DEBUG_MIGRATION_MODEL_GROUP` / `FALLBACK_MODEL_GROUP` /
# `AFFECTION_MODEL_GROUP` 四个可覆盖项，加第五个时名单必然漏一处，而漏一处就是一条越权路径。
MODEL_GROUP_REF_MARK = "ref_model_groups"
# 同一份 `ExtraField` 里声明该引用要的是哪类模型；缺省与 `ExtraField.model_type` 一致
MODEL_GROUP_TYPE_MARK = "model_type"
DEFAULT_MODEL_GROUP_TYPE = "chat"


class ModelGroupRefField(NamedTuple):
    """一个引用了模型组的配置字段"""

    name: str
    model_type: str
    # 字段自己声明的默认值。它由源码给出、租户改不到，因此是"越权值该回落到哪儿"的第一候选
    declared_default: str


def is_model_group_ref_item(config_item: Optional[Dict[str, Any]]) -> bool:
    """某个配置项是否引用模型组

    入参是 `UnifiedConfigService.get_config_item()` 返回的那种字典（它已经把
    `ref_model_groups` 从 `json_schema_extra` 里提取出来了），因此适配器配置、插件配置、
    以及由 `create_overridable_config_model` 动态派生的覆盖层配置全部适用——
    派生时 `json_schema_extra` 是深拷贝过去的，标记不会丢。
    """
    return bool(config_item and config_item.get(MODEL_GROUP_REF_MARK))


def iter_model_group_ref_fields(config_cls: Type[BaseModel]) -> List[ModelGroupRefField]:
    """列出某个配置类里全部引用了模型组的字段

    这是"把引用模型组这件事做成可枚举的"的落点：调用方遍历它，而不是写死字段名。
    """
    refs: List[ModelGroupRefField] = []
    for name, field in config_cls.model_fields.items():
        extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
        if not extra.get(MODEL_GROUP_REF_MARK):
            continue
        declared_default = field.get_default(call_default_factory=True)
        refs.append(
            ModelGroupRefField(
                name=name,
                model_type=str(extra.get(MODEL_GROUP_TYPE_MARK) or DEFAULT_MODEL_GROUP_TYPE),
                declared_default=declared_default if isinstance(declared_default, str) else "",
            ),
        )
    return refs


async def assert_model_group_selectable(name: str, identity: CurrentIdentity) -> None:
    """校验某个身份能否把某个模型组名写进配置项

    这是与配额天花板同一条边界上的另一扇门。只做读侧过滤是不够的：租户对自己实例
    （或自己频道）写一次 `USE_MODEL_GROUP=<平台 private 组名>`，此后每一轮对话都走那把 Key ——
    Key 不会进响应体，但授权与计费边界已经被绕过，`visibility=private` 从此没有意义。

    判定复用 `get_visible_group()`（内部即 `can_view_group`），与 `/console/model-groups`、
    `/config/model-groups` 同源，不另写一套归属判定。

    **不可见与不存在都抛 404**，不是 403。这里的分档理由与项目惯例（"能力边界用 403"）
    恰好相反，因为这一条判据的输入是**租户自己提交的组名**：不存在的组名今天是被放行的
    （配置项就是个字符串），所以一旦"存在但不可见"回 403，403 与 200 的差异本身就是一个
    组名存在性探针，别的租户配了哪些组可以被逐个试出来。两者对外必须不可区分。

    副作用是租户写一个拼错的组名会立刻收到 404 而不是把 Bot 写坏 —— 这是好事：
    改造前这种值要到下一轮对话才在 `config.MODEL_GROUPS[...]` 上抛裸 KeyError。

    平台方与未启用多租户的部署直接返回，行为逐字不变。
    """
    if is_platform_owner(identity):
        return
    normalized = name.strip()
    if not normalized:
        # 空串是多个字段的合法"未设置"取值（回落到主模型组），不是一次引用
        return
    await get_visible_group(normalized, identity)


def runtime_identity(tenant_id: Optional[int]) -> CurrentIdentity:
    """把「某个租户」包装成归属判定认得的身份

    运行时链路上没有登录用户，但 `can_view_group()` 只用得到 `tenant_id` 一个字段
    （`is_platform_owner()` 判的就是它是否为 None）。这样运行时兜底与接口层用的是
    **同一个函数**，不会出现两套判定各自漂移。
    """
    return CurrentIdentity(
        user_id=0,
        username="<runtime>",
        role=TenantRole.PLATFORM_OWNER if tenant_id is None else TenantRole.TENANT_ADMIN,
        tenant_id=tenant_id,
    )


def _pick_legal_group_name(
    rows: Dict[str, DBModelGroup],
    identity: CurrentIdentity,
    ref: ModelGroupRefField,
) -> str:
    """越权引用该回落到哪个组

    候选顺序是确定的，不做任何随机或"就近"选择：

    1. **字段自己声明的默认值** —— 由源码给出、租户改不到，是最保守的一档；
       默认值为空串时直接采用空串：那正是这个字段设计好的"未设置"语义
       （`FALLBACK_MODEL_GROUP` / `AFFECTION_MODEL_GROUP` 空串即回落到主模型组），
       比替运维挑一个组更忠实。
    2. 默认值本身也不可见时，取运行时视图里**第一个**可见且模型类型匹配的组。
       字典顺序即 `refresh_runtime_view()` 的 `order_by("id")`，跨进程稳定。
    3. 一个都没有 —— 返回空串。此时 `USE_MODEL_GROUP` 那一支会在既有的
       `config.MODEL_GROUPS[...]` 上抛 KeyError，即"这个租户压根没有可用模型组"，
       调用方会为此额外打一条 ERROR，不会静默。
    """
    if not ref.declared_default:
        return ""
    default_row = rows.get(ref.declared_default)
    if default_row is not None and can_view_group(default_row, identity):
        return ref.declared_default
    for name, row in rows.items():
        if row.model_type == ref.model_type and can_view_group(row, identity):
            return name
    return ""


async def enforce_model_group_scope(
    config_obj: CoreConfig,
    *,
    tenant_id: Optional[int],
    scope_label: str = "",
) -> List[Tuple[str, str, str]]:
    """就地把有效配置里越界的模型组引用改回合法值

    接口层的 `assert_model_group_selectable()` 只管住"从今往后"，**存量覆盖文件里可能已经
    写着越权的值**（本轮修复之前写入的，或手改磁盘留下的），只补接口层挡不住已落盘的。
    因此运行时再校验一次。

    降级策略是"回落 + 可辨识日志"，不是抛异常：让 Bot 因为一条历史配置直接哑掉，
    对租户是不可诊断的故障，而对攻击者反而只是少花了平台方的钱。

    **只处理"该组存在但不可见"**。组名根本不存在时原样留下：那不是越权（不存在的组花不了
    任何人的 Key），而既有的 `resolve_primary_model_group` / `resolve_fallback_model_group`
    对这种情况已经各有一套刻意设计过的诊断行为，覆盖它等于把那两条教训抹掉。

    Args:
        config_obj: 频道有效配置，**会被就地修改**（幂等：再跑一次没有可改的）
        tenant_id: 该配置生效范围所属的租户；None 表示平台方，直接返回
        scope_label: 日志里用来定位现场的标签（如 `频道 xxx`）

    Returns:
        本次纠正过的 `(字段名, 越权值, 回落值)` 列表，空列表表示没有越界引用
    """
    if tenant_id is None:
        return []

    refs = iter_model_group_ref_fields(type(config_obj))
    if not refs:
        return []

    identity = runtime_identity(tenant_id)
    rows: Dict[str, DBModelGroup] = {row.name: row for row in await DBModelGroup.all().order_by("id")}

    corrections: List[Tuple[str, str, str]] = []
    for ref in refs:
        current = getattr(config_obj, ref.name, "")
        if not isinstance(current, str) or not current.strip():
            continue
        row = rows.get(current)
        if row is None or can_view_group(row, identity):
            continue
        replacement = _pick_legal_group_name(rows, identity, ref)
        setattr(config_obj, ref.name, replacement)
        corrections.append((ref.name, current, replacement))
        logger.warning(
            f"模型组归属越界：{scope_label or '当前作用域'}（租户 {tenant_id}）的 {ref.name} 引用了 "
            f"不属于该租户的模型组 {current}，本次运行已回落到 {replacement or '(未设置)'}。"
            "该值多半是历史越权覆盖留下的，请在后台改为自己的模型组。",
        )
        if not replacement and ref.declared_default:
            logger.error(
                f"模型组归属越界：租户 {tenant_id} 名下没有任何可用的 {ref.model_type} 模型组，"
                f"{ref.name} 已被置空，本轮对话会因此失败。请为该租户配置模型组，"
                "或把平台方模型组的可见性改为 shared。",
            )
    return corrections


async def enforce_channel_model_group_scope(
    config_obj: CoreConfig,
    *,
    instance_key: str,
    adapter_key: str = "",
    chat_key: str = "",
) -> List[Tuple[str, str, str]]:
    """`enforce_model_group_scope()` 的频道入口：由实例反查租户后再判定

    实例未绑定租户（平台方自有实例）、实例不存在、以及未启用多租户的部署都在这里直接返回，
    一次数据库查询都不会多做归属判定，平台方行为逐字不变。

    调用方手上有 `adapter_key` 时必须一起传：另一个适配器下可以有同名实例归平台方，
    只按标识反查会把租户的频道判成平台方，越权的模型组就此放行。
    """
    if not instance_key:
        return []

    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    query = DBAdapterInstance.filter(instance_key=instance_key)
    if adapter_key:
        query = query.filter(adapter_key=adapter_key)
    instance = await query.order_by("id").first()
    if instance is None or instance.tenant_id is None:
        return []
    return await enforce_model_group_scope(
        config_obj,
        tenant_id=instance.tenant_id,
        scope_label=f"频道 {chat_key}" if chat_key else f"实例 {instance_key}",
    )
