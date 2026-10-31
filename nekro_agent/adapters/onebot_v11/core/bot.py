"""OneBot V11 Bot 连接定位

一个实例对应一个 Bot 账号。出站消息必须由**该会话所属账号**的连接发出：
此前这里是一个无参函数，直接返回 `get_bots()` 里遇到的第一个实例，
多协议端接入时必然串号 —— 账号 A 收到的消息会由账号 B 回复。

## 三种定位方式

- `get_bot_by_chat_key(chat_key)` —— 出站首选，按会话精确定位
- `get_bot(account_id)` —— 已知账号号码时直接取
- `get_bot()` —— 无参形态，按当前处理链路的实例上下文解析

保留无参形态是为了兼容第三方插件：市场上的旧插件普遍这么写。有实例上下文时
它能解析到正确账号；上下文缺失且多账号在线时会显式报错，而不是静默串号。
"""

from typing import List

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

from .channel import parse_instance_key
from .instance import get_current_instance, lookup_cached_account_id, resolve_account_id


class BotInstanceOfflineError(RuntimeError):
    """目标账号的协议端未连接

    出站时绝不允许回落到其他账号：宁可显式失败，也不能让消息从错误的账号发出去。
    """

    def __init__(self, target: str):
        self.target = target
        super().__init__(f"OneBot V11 连接不可用: {target or '(未指定实例，且无法无歧义推定)'}")


def list_connected_account_ids() -> List[str]:
    """列出当前已连接的所有 OneBot V11 账号号码"""
    return [key for key, bot in get_bots().items() if isinstance(bot, OneBotV11Bot)]


def get_bot(account_id: str = "") -> OneBotV11Bot:
    """按账号号码获取 OneBot V11 连接

    Args:
        account_id: 协议端账号号码（`self_id`）。留空时依次尝试：
            当前处理链路的实例上下文 → 唯一在线账号。都无法确定则报错。

    Returns:
        对应账号的 Bot 连接

    Raises:
        BotInstanceOfflineError: 该账号未连接，或无法无歧义地推定目标账号
    """
    if not account_id:
        context = get_current_instance()
        if context:
            account_id = context.account_id
        else:
            # 无上下文（如后台任务）时，只有唯一账号在线才能安全推定
            connected = list_connected_account_ids()
            if len(connected) != 1:
                raise BotInstanceOfflineError("")
            account_id = connected[0]

    bot_instance = get_bots().get(account_id)
    if isinstance(bot_instance, OneBotV11Bot):
        return bot_instance
    raise BotInstanceOfflineError(account_id)


async def get_bot_by_chat_key(chat_key: str) -> OneBotV11Bot:
    """按会话标识获取 OneBot V11 连接

    Args:
        chat_key: 会话标识，形如 `onebot_v11-inst1-group_123456`

    Returns:
        该会话所属账号的 Bot 连接

    Raises:
        BotInstanceOfflineError: 该账号未连接，或历史会话无法无歧义归属
    """
    instance_key = parse_instance_key(chat_key)
    if not instance_key:
        # 升级前的历史会话没有实例段，走无参推定路径
        return get_bot()

    account_id = lookup_cached_account_id(instance_key) or await resolve_account_id(instance_key)
    if not account_id:
        raise BotInstanceOfflineError(instance_key)
    return get_bot(account_id)
