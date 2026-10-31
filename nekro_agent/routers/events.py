"""全局系统事件 SSE 端点

连接建立时自动推送一条 type=snapshot 事件（包含所有 domain 的当前完整状态），
之后持续推送增量 delta 事件。断线重连时同样会先收到 snapshot。

## 租户作用域：过滤发生在订阅者自己的出口

`services/system_broadcast.py` 的订阅位是**全局稀缺资源**（`_MAX_SUBSCRIBERS=50`，
每个打开后台的浏览器标签页占一位），所以这里**不**按租户各开一路订阅、也不动广播器本身：
每个连接照旧只占一个订阅位，过滤放在从自己队列里取出 payload 之后 —— 建连时的全量快照
按 domain 逐条裁剪，之后每个 delta 事件逐条判定该不该发给这个身份。

判据来自事件自带的归属字段：`workspace_id` 过 `accessible_workspace_ids`，
`chat_key` 过本身份实例名下的频道集合（与 `routers/chat_channel.py:assert_channel_accessible`
逐字同一套口径），`instance_key` 过 `accessible_instance_keys`。

**频道归属不要改成 `{adapter_key}-{instance_key}-` 前缀匹配**：那只是
`BaseAdapter.build_chat_key` 的默认实现，`wechat_ilink_multi` 与 `qqbot_openclaw` 都不遵守
（见 `adapters/__init__.py:CHAT_KEY_PREFIX_ALIASES`），前缀匹配对它们恒不命中，
表现为租户看不到自己频道的 Agent 活动。

**未登记的事件类型对租户一律不可见**（见 `_allows` 的兜底分支）：新增一种广播时忘了在
本模块登记，后果应当是"租户看不到新功能"，而不是"租户看到全域数据"。
`tests/test_dashboard_events_tenant_scope.py` 会因为漏登记而变红。

平台方（含未启用多租户的部署，`get_tenant_scope()` 恒为 None）拿到的是
`visibility is None`，一条判定都不进，行为与改造前逐字一致。
"""

import asyncio
import json
import time
from typing import Any, AsyncGenerator, Dict, FrozenSet, Optional, Set

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.services.runtime_state import is_shutting_down
from nekro_agent.services.system_broadcast import (
    subscribe_system_events,
    unsubscribe_system_events,
)
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import accessible_instance_keys, get_tenant_scope
from nekro_agent.services.workspace.tenant_scope import accessible_workspace_ids

logger = get_sub_logger("events")

router = APIRouter(prefix="/events", tags=["Events"])

# 事件类型 → 归属判据。取值与 `system_broadcast._update_state` 里的 domain 名一一对应，
# 因此同一张表既用于 delta 事件（`type` 字段）也用于快照裁剪（domain 键）。
WORKSPACE_SCOPED_EVENTS: Set[str] = {
    "workspace_status",
    "workspace_cc_active",
    "workspace_cc_runtime_status",
    "kb_index_progress",
}
CHAT_SCOPED_EVENTS: Set[str] = {
    "agent_active",
    "agent_runtime_status",
}
# 同时带工作区与频道归属：两条都得过。工作区归属不唯一（`SHARED_GROUP`）时对租户本就不可见，
# 这里跟着拒绝，不会把另一侧的记忆检索内容漏出去。
WORKSPACE_AND_CHAT_SCOPED_EVENTS: Set[str] = {"memory_recall_activity"}
INSTANCE_SCOPED_EVENTS: Set[str] = {"adapter_instance_status"}
# 全局知识库是全体共用的一份，它的只读面（列表/全文/原文）本来就对租户开放，
# 索引进度不构成额外泄露。见 `tests/test_route_permission_contract.py` 里 kb_library 的登记。
SHARED_LIBRARY_EVENTS: Set[str] = {"kb_library_index_progress"}


class TenantEventVisibility:
    """某个租户订阅者的可见范围

    可见集在第一条 payload 到达时解析，之后按 `REFRESH_INTERVAL_SECONDS` 刷新：SSE 连接可以挂几个小时，
    而实例归属、频道绑定与工作区绑定都是会变的，只在建连时算一次会留下一个
    "归属已经改了、这条连接还在收"的窗口。刷新由事件驱动，空闲连接不产生查询。
    反过来，**新建的频道也要等一次刷新**（≤30 秒）才会出现在可见集里，
    表现为一个刚拉起来的群第一次触发 Agent 时活动卡片可能晚一点亮。

    初始值是**空集**，所以解析失败时什么都不发（fail-closed）；后续刷新失败则沿用上一次的结果并很快重试，
    不会因为一次数据库抖动把连接打断。
    """

    REFRESH_INTERVAL_SECONDS = 30.0
    RETRY_INTERVAL_SECONDS = 3.0

    def __init__(self, user: DBUser) -> None:
        self._user = user
        self._expires_at = 0.0
        self._instance_keys: FrozenSet[str] = frozenset()
        self._chat_keys: FrozenSet[str] = frozenset()
        self._workspace_ids: FrozenSet[int] = frozenset()

    async def visible_payload(self, payload: str) -> Optional[str]:
        """把一条广播 payload 裁剪成这个身份该看到的样子

        Returns:
            原样转发的 payload、裁剪后的快照，或 None（整条不该发给它）。
        """
        try:
            event = json.loads(payload)
        except ValueError:
            logger.warning("系统事件 payload 不是合法 JSON，对租户按不可见处理")
            return None
        if not isinstance(event, dict):
            return None

        await self._refresh_if_stale()

        kind = event.get("type")
        if kind == "snapshot":
            return json.dumps(
                {"type": "snapshot", "data": self._pruned_snapshot(event.get("data"))},
                ensure_ascii=False,
            )
        if not isinstance(kind, str) or not self._allows(kind, event):
            return None
        return payload

    def _pruned_snapshot(self, data: object) -> Dict[str, Dict[str, Any]]:
        """按 domain 逐条裁剪全量快照

        快照必须照常发出（前端靠它初始化各张状态表），裁剪后为空就是一份空快照。
        """
        if not isinstance(data, dict):
            return {}
        pruned: Dict[str, Dict[str, Any]] = {}
        for domain, entries in data.items():
            if not isinstance(entries, dict):
                continue
            visible = {
                key: value
                for key, value in entries.items()
                if isinstance(value, dict) and self._allows(str(domain), value)
            }
            if visible:
                pruned[str(domain)] = visible
        return pruned

    def _allows(self, kind: str, entry: Dict[str, Any]) -> bool:
        if kind in SHARED_LIBRARY_EVENTS:
            return True
        if kind in INSTANCE_SCOPED_EVENTS:
            return entry.get("instance_key") in self._instance_keys
        if kind in CHAT_SCOPED_EVENTS:
            return self._owns_chat_key(entry.get("chat_key"))
        if kind in WORKSPACE_SCOPED_EVENTS:
            return entry.get("workspace_id") in self._workspace_ids
        if kind in WORKSPACE_AND_CHAT_SCOPED_EVENTS:
            return entry.get("workspace_id") in self._workspace_ids and self._owns_chat_key(entry.get("chat_key"))
        logger.warning(f"系统事件类型 {kind!r} 未登记租户可见性判据，对租户按不可见处理")
        return False

    def _owns_chat_key(self, chat_key: object) -> bool:
        """频道归属按 `chat_key -> chat_channel.instance_key` 判定

        与 `routers/chat_channel.py:assert_channel_accessible` 同一套口径。集合在刷新时一次取出，
        逐事件判定不再产生查询；没有频道行的 `chat_key`（历史残留、已删频道）一律不可见。
        """
        return isinstance(chat_key, str) and chat_key in self._chat_keys

    async def _refresh_if_stale(self) -> None:
        if time.monotonic() < self._expires_at:
            return
        try:
            instance_keys = await accessible_instance_keys(self._user)
            chat_keys = (
                await DBChatChannel.filter(instance_key__in=instance_keys).values_list("chat_key", flat=True)
                if instance_keys
                else []
            )
            workspace_ids = await accessible_workspace_ids(self._user)
        except Exception as e:
            logger.warning(f"刷新租户事件可见范围失败，沿用上一次的结果: {e}")
            self._expires_at = time.monotonic() + self.RETRY_INTERVAL_SECONDS
            return

        self._instance_keys = frozenset(instance_keys)
        self._chat_keys = frozenset(chat_keys)
        self._workspace_ids = frozenset(workspace_ids or ())
        self._expires_at = time.monotonic() + self.REFRESH_INTERVAL_SECONDS


async def resolve_event_visibility(user: DBUser) -> Optional[TenantEventVisibility]:
    """解析这条订阅的可见范围，平台方返回 None（不过滤）"""
    if get_tenant_scope(user) is None:
        return None
    return TenantEventVisibility(user)


@router.get("/stream", summary="全局系统事件实时推送（SSE）")
@require_role(Role.Admin)
async def stream_system_events(
    request: Request,
    _current_user: DBUser = Depends(get_current_active_user),
) -> EventSourceResponse:
    """订阅全局系统事件流。

    连接建立后立即推送 ``type=snapshot`` 全量状态快照，随后持续推送增量事件：

    - type=snapshot: ``{type, data: {domain: {key: value, ...}, ...}}``
    - type=workspace_status: ``{type, workspace_id, status, name, ...}``
    - type=workspace_cc_active: ``{type, workspace_id, active, max_duration_ms}``

    租户身份下每条事件都按归属过滤，见模块 docstring。
    """

    # 身份判定必须在建流之前：`EventSourceResponse` 一旦返回状态码就已经是 200，
    # 生成器里再抛异常只表现为一条建立后立刻断开的连接。
    visibility = await resolve_event_visibility(_current_user)

    q = subscribe_system_events()

    def cleanup_subscription() -> None:
        if q is not None:
            unsubscribe_system_events(q)

    async def handle_client_disconnect(_: object) -> None:
        cleanup_subscription()

    async def event_generator() -> AsyncGenerator[str, None]:
        if q is None:
            yield '{"type":"error","message":"连接数已达上限，请稍后重试"}'
            return
        try:
            while not is_shutting_down():
                if await request.is_disconnected():
                    return
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield {"comment": "ping"}  # SSE keep-alive
                    continue
                if visibility is not None:
                    visible = await visibility.visible_payload(payload)
                    if visible is None:
                        continue
                    payload = visible
                yield {"data": payload}
        finally:
            cleanup_subscription()

    return EventSourceResponse(
        event_generator(),
        client_close_handler_callable=handle_client_disconnect,
    )
