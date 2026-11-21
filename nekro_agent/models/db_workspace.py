import asyncio
from typing import Any, Dict, Iterable, Mapping, Optional

from tortoise import fields, timezone
from tortoise.models import Model

# 只能经 `patch_workspace_metadata` 改的 metadata 键：容器的鉴权凭据与它们的标记。
# 别处的代码常常拿着请求开头读出来的对象、改一个无关的键再整份写回 metadata；没有这道
# 护栏，这期间新建容器换掉的 token 会被写回旧值（新容器认不得 NA，残留的旧容器反倒又能用）。
# 键名与 `services/workspace` 里的常量一致，`tests/test_ws_host_hardening.py` 钉着。
GUARDED_METADATA_KEYS = frozenset(
    {
        "sandbox_api_token",  # SandboxContainerManager._rotate_internal_api_token
        "cc_relay_token",  # cc_credential.METADATA_TOKEN_KEY
        "ssh_gateway_client_key",  # 建容器时随容器生成
        "cc_platform_secret_delivered",  # credential_policy.METADATA_SECRET_DELIVERED_KEY
    },
)

_metadata_locks: Dict[int, asyncio.Lock] = {}


class DBWorkspace(Model):
    """工作区数据模型"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    name = fields.CharField(max_length=64, unique=True, index=True, description="工作区名称")
    description = fields.TextField(default="", description="工作区描述")
    status = fields.CharField(max_length=16, default="stopped", description="状态: active|stopped|failed|deleting")

    # 镜像配置
    sandbox_image = fields.CharField(max_length=128, default="", description="cc-sandbox 镜像名")
    sandbox_version = fields.CharField(max_length=64, default="latest", description="镜像版本/Build Tag")

    # 容器标识（container_name 同时作为 Docker 内网 DNS 主机名）
    container_name = fields.CharField(
        max_length=64,
        unique=True,
        null=True,
        description="容器名，格式: nekro-cc-{hex8}，同时用作 Docker 内网主机名",
    )
    container_id = fields.CharField(max_length=128, null=True, description="容器 ID（运行时填充）")

    # 宿主机端口（从高位端口段随机分配，inspect 验证后写入）
    host_port = fields.IntField(null=True, description="宿主机映射端口")

    # 运行策略
    runtime_policy = fields.CharField(max_length=16, default="agent", description="运行策略: agent|relaxed|strict")

    # 运行时状态
    last_heartbeat = fields.DatetimeField(null=True, description="最近心跳时间")
    last_error = fields.TextField(null=True, description="最近错误信息")

    # 元数据（skills 列表、mcp 配置等）
    metadata = fields.JSONField(default=dict, description="元数据")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "workspace"

    async def save(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        """整份写回 metadata 时，`GUARDED_METADATA_KEYS` 一律以库里当前的值为准

        这些键只经 `patch_workspace_metadata` 改（它不走这里）；别的写入方手里的对象可能是旧的，
        不能把它们写回旧值，也不能把已经删掉的写回来。新建的行（还没有主键）不受影响。
        """
        update_fields = kwargs.get("update_fields")
        if update_fields is None and len(args) >= 2:
            update_fields = args[1]
        if self.pk is not None and (update_fields is None or "metadata" in update_fields):
            stored = await DBWorkspace.filter(id=self.pk).values_list("metadata", flat=True)
            if stored:
                current = stored[0] if isinstance(stored[0], dict) else {}
                metadata = dict(self.metadata or {})
                for key in GUARDED_METADATA_KEYS:
                    if key in current:
                        metadata[key] = current[key]
                    else:
                        metadata.pop(key, None)
                self.metadata = metadata
        await super().save(*args, **kwargs)

    @property
    def api_endpoint(self) -> str:
        from nekro_agent.core.config import config
        from nekro_agent.core.os_env import OsEnv

        if OsEnv.RUN_IN_DOCKER and self.container_name:
            return f"http://{self.container_name}:{config.CC_SANDBOX_INTERNAL_PORT}"
        if self.host_port:
            return f"http://127.0.0.1:{self.host_port}"
        raise ValueError(f"Workspace {self.id} 尚无可用的 API 地址")


async def patch_workspace_metadata(
    workspace: DBWorkspace,
    *,
    set_keys: Optional[Mapping[str, Any]] = None,
    drop_keys: Iterable[str] = (),
) -> Dict[str, Any]:
    """按键改 metadata：在这个工作区的锁里读库里当前那一份、改这几个键、写回

    不碰调用方没提到的键（别处同时写进去的不会被这次覆盖）；`GUARDED_METADATA_KEYS`
    只能经这里改。传进来的对象只同步这几个键，其余保持调用方手里的样子。返回写入后的整份。
    """
    set_keys = dict(set_keys or {})
    drop = [key for key in drop_keys if key not in set_keys]
    lock = _metadata_locks.setdefault(workspace.id, asyncio.Lock())
    async with lock:
        stored = await DBWorkspace.filter(id=workspace.id).values_list("metadata", flat=True)
        current: Dict[str, Any] = dict(stored[0]) if stored and isinstance(stored[0], dict) else {}
        changed = False
        for key, value in set_keys.items():
            if key not in current or current[key] != value:
                current[key] = value
                changed = True
        for key in drop:
            if key in current:
                del current[key]
                changed = True
        if changed:
            await DBWorkspace.filter(id=workspace.id).update(metadata=current, update_time=timezone.now())
    local = dict(workspace.metadata or {})
    local.update(set_keys)
    for key in drop:
        local.pop(key, None)
    workspace.metadata = local
    return current
