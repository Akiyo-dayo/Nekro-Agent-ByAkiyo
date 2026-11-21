"""会话的归属解析（谁是这次调用的主人）

## 为什么需要单独一处

邮箱与表情这两类资源都是「平台方一份、所有实例共用」的老结构。要在租户实例的会话里把它们
关掉，第一步必须先回答「这次调用属于谁」。这个问题的答案只能从**宿主**拿：沙盒里跑的是模型
生成的代码，模型给的一切参数（`chat_key`、账户名、模型组名）都是敌对调用方给的值。

## 身份只认 `from_chat_key`

`ctx.from_chat_key` 是 RPC 调用许可绑定的那一个（与 `scope_guard._own_chat_key` 同源），
模型改不了；`ctx.chat_key` 是给插件用的派生视图，任何模型传入的频道参数都可能绕到这里，
所以**刻意不读它**。解析链与 `scope_guard._tenant_of_chat_key` 完全一致：
频道行 -> `(adapter_key, instance_key)` -> 实例行的 `tenant_id`。

## 解析不了时的两档口径

- **单租户部署**（没有任何实例挂在租户名下）：没有租户边界可越，"解析不了"按平台方对待，
  行为与改造前逐字一致；
- **多租户部署**：解析不了一律 fail-closed，什么都不给（与 `scope_guard` 的
  「两端都必须解析得出」同一条取舍）。

`instance_key` 为空的频道（email 频道、多账号化之前的历史数据）按平台方对待：它本来就没有
"本实例"这个概念，只能用平台方提供的那一份。
"""

from dataclasses import dataclass
from typing import Any, Optional

from nekro_agent.services.plugin.scope_guard import deployment_has_tenants


@dataclass(frozen=True)
class SessionOwner:
    """一次调用的归属

    字段全部是解析结果，不带任何原始参数：调用方只需要问「是不是平台方」，
    需要租户时取 `tenant_id`，需要"本实例"时取 `(adapter_key, instance_key)`。

    Attributes:
        resolved: 频道行查到了没有；为 False 表示这次调用解析不出归属
        adapter_key: 频道行上的适配器标识；解析不了时为空串
        instance_key: 频道行上的实例标识；解析不了时为空串
        tenant_id: 实例行的租户 ID；None 表示平台方（或解析不了）
        platform: 是否按平台方口径对待；解析成功时看 `tenant_id`，
            解析失败时看部署里有没有租户（单租户按平台方，多租户 fail-closed）
        chat_key: 解析时实际用的那个频道标识（宿主签发的 `from_chat_key`）；
            解析不了时为空串。只给需要按这条频道取别的属性的调用方用（例如表情库要按频道
            解析当前人设），它同样是宿主给的值，不是模型传入的参数
    """

    resolved: bool
    adapter_key: str
    instance_key: str
    tenant_id: Optional[int]
    platform: bool
    chat_key: str = ""


async def resolve_chat_owner(chat_key: Any) -> SessionOwner:
    """按频道标识解析归属

    判据与 `scope_guard._tenant_of_chat_key` 完全一致：频道行不存在算解析失败；
    `instance_key` 为空（历史数据、单账号适配器）归平台方；实例行缺失（悬挂引用）也归平台方，
    与 `DBAdapterInstance.is_instance_active` 的取舍相同。

    Args:
        chat_key: 待解析的频道标识；非字符串或空串一律按解析失败处理

    Returns:
        解析结果；解析失败时 `resolved=False`、键为空串、`tenant_id=None`，
        `platform` 由部署里有没有租户决定（单租户按平台方，多租户 fail-closed）
    """
    # 模型导入放在函数里：模型层会反拖适配器与配置解析器，模块级导入容易成环
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance
    from nekro_agent.models.db_chat_channel import DBChatChannel

    if not isinstance(chat_key, str) or not chat_key:
        return SessionOwner(
            resolved=False,
            adapter_key="",
            instance_key="",
            tenant_id=None,
            platform=not await deployment_has_tenants(),
            chat_key="",
        )

    channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
    if channel is None:
        return SessionOwner(
            resolved=False,
            adapter_key="",
            instance_key="",
            tenant_id=None,
            platform=not await deployment_has_tenants(),
            chat_key="",
        )

    adapter_key = channel.adapter_key or ""
    instance_key = channel.instance_key or ""
    if not instance_key:
        # 没有实例段的频道属于平台方：它没有"本实例"，也就没有租户可归属
        return SessionOwner(
            resolved=True,
            adapter_key=adapter_key,
            instance_key="",
            tenant_id=None,
            platform=True,
            chat_key=channel.chat_key,
        )

    instance = (
        await DBAdapterInstance.filter(adapter_key=channel.adapter_key, instance_key=instance_key).order_by("id").first()
    )
    tenant_id = instance.tenant_id if instance is not None else None
    return SessionOwner(
        resolved=True,
        adapter_key=adapter_key,
        instance_key=instance_key,
        tenant_id=tenant_id,
        platform=tenant_id is None,
        chat_key=channel.chat_key,
    )


async def resolve_session_owner(ctx: Any) -> SessionOwner:
    """按会话上下文解析归属

    **只**取 `ctx.from_chat_key`（宿主签发、绑定在调用许可上的那个）。
    属性缺失、为空或不是字符串时按解析失败处理，交由 :func:`resolve_chat_owner` 的口径兜底。

    Args:
        ctx: 沙盒方法 / 提示词注入 / 命令收到的上下文

    Returns:
        解析结果；含义与 :class:`SessionOwner` 一致
    """
    return await resolve_chat_owner(getattr(ctx, "from_chat_key", None))
