"""沙盒方法里「由模型传入的作用域参数」的归属校验

## 为什么需要

沙盒里跑的是模型生成的代码，而模型的输入（聊天内容、外部资料）由租户完全控制，所以沙盒方法
收到的 `chat_key` 与任何自由填写的参数一样，都是**敌对调用方给的值**。RPC 入口
（`routers/rpc.py`）只保证"这次回调属于哪个会话"（调用许可绑定了 `from_chat_key`），
不会替方法检查它的**参数**。方法自己不校验的话，会话 A 的模型写下 `chat_key="B 的频道"`，
就能读出 B 的聊天记录、改 B 的笔记与定时任务、借 B 的 Bot 发消息。B 属于别的租户时，
这就是一条跨租户通道。

## 两档口径（与 `affection_rank._assert_own_channel` 同源）

（PR #11 复核后定时器从第一档挪到第二档：给同租户的别的频道设提醒是正常用法，单租户部署里旧版本来就允许。）

- **读 / 改会话自己的状态**（聊天记录、笔记、角色状态）：`chat_key` 必须等于当前会话，
  用 :func:`require_own_chat_key`。流进来的是**别人的数据**，没有任何出站闸门能拦。
- **向外发出动作与定时器**（发消息、群头衔、语音、RCON、设定时器、管理周期任务）：同一租户内允许对别的频道操作
  （`AgentCtx.ms` 本来就是为"向其他频道发通知"设计的），跨租户一律拒绝，
  用 :func:`require_same_tenant_chat_key`。租户边界是信任域边界：同一租户的实例互相可见，
  不同租户之间什么都不该流动。

## 单租户部署：归属解析不了时放行

租户边界只有在**确实存在租户**的部署里才有意义。没有任何实例挂在租户名下（平台方独占，默认部署）时，
“解析不了归属”不构成越界：频道行不存在（比如 Telegram 的发送路径本来就不需要频道行）、调用方频道行不存在，
都按旧行为放行；一旦部署里有任何实例属于租户，就回到严格口径——两端都必须解析得出且相等。

## 不泄露"存在但无权"

不满足归属时一律抛 :data:`CHANNEL_NOT_FOUND`，与"这个频道根本不存在"完全同文，
调用方分不出"没有"和"不是你的"，频道也就探测不出来。
"""

from typing import Any, Optional, Tuple

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("plugin_scope_guard")

# 与 `history_travel` 原有的"频道不存在"报错同文：拒绝与不存在对调用方不可区分
CHANNEL_NOT_FOUND = "未找到聊天频道"


def _own_chat_key(ctx: Any) -> str:
    own = getattr(ctx, "from_chat_key", None)
    if not isinstance(own, str) or not own:
        # 上下文不挂在任何会话上（如 Webhook 触发）：没有"当前会话"可比，按拒绝处理
        raise ValueError(CHANNEL_NOT_FOUND)
    return own


def require_own_chat_key(ctx: Any, chat_key: Any) -> str:
    """`chat_key` 必须就是当前会话，否则按"频道不存在"拒绝

    Args:
        ctx: 沙盒方法收到的 `AgentCtx`
        chat_key: 模型传入的值

    Returns:
        当前会话的 `chat_key`（调用方此后一律用它，而不是模型传入的那份）

    Raises:
        ValueError: 不是当前会话，或上下文不属于任何会话
    """
    own = _own_chat_key(ctx)
    if not isinstance(chat_key, str) or chat_key != own:
        raise ValueError(CHANNEL_NOT_FOUND)
    return own


async def _tenant_of_chat_key(chat_key: str) -> Tuple[bool, Optional[int]]:
    """`(是否能解析, 租户 ID)`；租户 ID 为 None 表示归平台方

    解析链与出站总闸一致：频道行 -> `(adapter_key, instance_key)` -> 实例行的 `tenant_id`。
    频道行不存在算解析失败。频道行存在但实例行缺失（悬挂引用）、`instance_key` 为空
    （历史数据）与 `DBAdapterInstance.is_instance_active` 的取舍一致，归平台方。
    """
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance
    from nekro_agent.models.db_chat_channel import DBChatChannel

    channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
    if channel is None:
        return False, None
    if not channel.instance_key:
        return True, None
    instance = (
        await DBAdapterInstance.filter(adapter_key=channel.adapter_key, instance_key=channel.instance_key)
        .order_by("id")
        .first()
    )
    return True, (instance.tenant_id if instance is not None else None)


async def _deployment_has_tenants() -> bool:
    """部署里是否有任何实例挂在租户名下；没有就是单租户（平台方独占）部署"""
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    return await DBAdapterInstance.filter(tenant_id__not_isnull=True).exists()


async def require_same_tenant_chat_key(ctx: Any, chat_key: Any) -> str:
    """目标频道必须在当前会话所属的租户之内，否则按"频道不存在"拒绝

    目标就是当前会话时不查库直接放行（绝大多数调用都是这样）。其余情况：两边都解析得出租户时必须相等，
    平台方（`tenant_id` 为空）自成一域，平台方会话不能借模型传参去操作租户的频道，反之亦然；
    任何一边解析不了时，**单租户部署放行**（没有租户边界可越），**多租户部署拒绝**（fail-closed）。

    Returns:
        通过校验的 `chat_key`

    Raises:
        ValueError: 目标不存在、解析不了或属于别的租户
    """
    own = _own_chat_key(ctx)
    if not isinstance(chat_key, str) or not chat_key:
        raise ValueError(CHANNEL_NOT_FOUND)
    if chat_key == own:
        return chat_key

    own_ok, own_tenant = await _tenant_of_chat_key(own)
    target_ok, target_tenant = await _tenant_of_chat_key(chat_key)
    if own_ok and target_ok:
        if own_tenant == target_tenant:
            return chat_key
    elif not await _deployment_has_tenants():
        return chat_key
    logger.warning(f"拒绝跨租户的沙盒方法调用: from_chat_key={own}, 目标 chat_key={chat_key}")
    raise ValueError(CHANNEL_NOT_FOUND)
