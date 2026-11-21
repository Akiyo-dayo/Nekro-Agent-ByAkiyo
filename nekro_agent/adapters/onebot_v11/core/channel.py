"""OneBot V11 会话标识的实例编解码

## 格式

```
channel_id = {group|private}_{target_id}              例: group_123456
chat_key   = onebot_v11-{instance_key}-{channel_id}   例: onebot_v11-inst1-group_123456
```

## 为什么实例段只放在 chat_key 而不放进 channel_id

`channel_id` 是插件通过 `ctx.channel_id` 直接读到的一等公开字段，大量既有插件会用
`channel_id.split("_")` 取频道类型与目标 ID。把实例段塞进 `channel_id` 会让
`split("_")[0]` 静默得到 `"inst1-group"` 而不是 `"group"` —— 不报错，只是走错分支，
是最难排查的一类破坏。因此 `channel_id` 保持与单账号时代完全一致的干净形态。

会话唯一性改由 `(adapter_key, instance_key, channel_id)` 三元组保证
（见 `DBChatChannel.get_or_create`），`chat_key` 作为对外的单一句柄仍需带上实例段。

## 为什么 instance_key 不允许包含 `-`

`chat_key` 按 `-` 分段解析。约束 slug 为 `[a-z0-9_]` 后，
`onebot_v11-inst1-group_123456` 可无歧义地切成三段，无需查库即可解析出实例。
同时该字符集落在 `sanitize_chat_key_for_path()` 的安全集内，
不同实例的上传目录与沙盒挂载路径不会碰撞。
"""

import re
from typing import Tuple

ADAPTER_KEY = "onebot_v11"
CHANNEL_TYPE_PREFIXES = ("group_", "private_")
# 实例标识的合法字符集：小写字母、数字、下划线。刻意排除 `-`，见模块文档
INSTANCE_KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")

# 频道标识的严格形态：前缀 + 纯 ASCII 数字。刻意不用 `\d`：它在 Python 里也匹配全角、阿拉伯数字等，
# 而那些字符 `int()` 照样能转，转出来的号码却不是任何一个真实的群号 / QQ 号
_NUMERIC_CHANNEL_ID_PATTERN = re.compile(r"(group|private)_([0-9]+)")


class NonNumericChannelIdError(ValueError):
    """频道标识不是 `group_<数字>` / `private_<数字>`，取不出目标 ID

    故意继承 `ValueError`：既有调用点大多把「取不出目标 ID」写成 `except ValueError`，
    换一个无关的异常类型会让那些兜底静默失效。

    为什么需要这个类型：正常入站造不出非数字形态（NoneBot 的事件模型把群号 / QQ 号校验成整数），
    但库里可能躺着测试脚本、手工写库留下的 `group_<非数字>` 频道行。以前的
    `int(channel_id.split("_")[1])` 抛的是 `invalid literal for int() with base 10: 'tenant'`，
    报错里没有频道标识，日志上看不出是哪个频道出的问题。把原频道标识带进异常文案，
    才能一眼定位到那条脏数据。
    """

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id
        super().__init__(
            f"OneBot V11 频道标识 {channel_id!r} 不是 group_<数字> / private_<数字>，取不出目标 ID",
        )


def is_numeric_channel_id(channel_id: str) -> bool:
    """频道标识是否为 `group_<数字>` / `private_<数字>` 的严格形态

    用 `fullmatch` 而不是 `match` + `$`：后者会放过结尾的换行符，
    `group_123\\n` 会被当成合法标识，而 `int()` 也能把它转成 123。
    """
    return _NUMERIC_CHANNEL_ID_PATTERN.fullmatch(channel_id) is not None


def parse_numeric_target_id(channel_id: str) -> int:
    """从频道标识取出数字目标 ID，形如 `group_123456` -> `123456`

    Raises:
        NonNumericChannelIdError: 标识不是严格形态，取不出目标 ID（异常里带原频道标识）
    """
    matched = _NUMERIC_CHANNEL_ID_PATTERN.fullmatch(channel_id)
    if matched is None:
        raise NonNumericChannelIdError(channel_id)
    return int(matched.group(2))


def is_valid_instance_key(instance_key: str) -> bool:
    """校验实例标识是否符合命名约束"""
    return bool(INSTANCE_KEY_PATTERN.match(instance_key))


def build_chat_key(instance_key: str, channel_id: str) -> str:
    """构建带实例段的会话标识

    Args:
        instance_key: 实例标识（稳定 slug）；为空时退化为单实例格式
        channel_id: 频道标识，形如 `group_123456`

    Returns:
        形如 `onebot_v11-inst1-group_123456` 的会话标识
    """
    if not instance_key:
        return f"{ADAPTER_KEY}-{channel_id}"
    return f"{ADAPTER_KEY}-{instance_key}-{channel_id}"


def parse_channel_target_id(channel_id: str) -> str:
    """从频道标识取出目标 ID，形如 `group_123456` -> `123456`

    缺前缀时返回空串。**不做数值校验**，判「这条事件指向真实会话吗」用
    `is_addressable_channel_id`。
    """
    for prefix in CHANNEL_TYPE_PREFIXES:
        if channel_id.startswith(prefix):
            return channel_id[len(prefix) :]
    return ""


def is_addressable_channel_id(channel_id: str) -> bool:
    """频道标识是否指向一个真实会话

    协议端会把不属于任何会话的事件也报上来（OneBot V11 的 `notice.notify.input_status`
    就带着 `group_id=0`），落到这里是 `group_0`。这类标识拼出来的 `chat_key`
    在库里永远查不到，必须在入口就判掉而不是留给下游抛「聊天频道不存在」。
    """
    target_id = parse_channel_target_id(channel_id)
    return bool(target_id) and target_id != "0"


def parse_chat_key(chat_key: str) -> Tuple[str, str]:
    """从会话标识解析出实例标识与频道标识

    兼容升级前的单实例格式（`onebot_v11-group_123456`），此时实例标识为空串。

    Args:
        chat_key: 会话标识

    Returns:
        (instance_key, channel_id)。无法解析时实例标识为空串，频道标识尽力返回。
    """
    _adapter_key, separator, remainder = chat_key.partition("-")
    if not separator:
        return "", chat_key
    if remainder.startswith(CHANNEL_TYPE_PREFIXES):
        # 历史格式：没有实例段
        return "", remainder
    instance_key, separator, channel_id = remainder.partition("-")
    if not separator or not channel_id.startswith(CHANNEL_TYPE_PREFIXES):
        return "", remainder
    # 解析出的实例段必须符合命名约束才认。chat_key 正常由本模块构建、来源可信，
    # 但它也会从数据库、插件参数、AI 生成的沙盒调用等处流入；不校验的话
    # `onebot_v11-..-group_1` 会解析出 `..`，而实例标识会被用作目录名。
    if not is_valid_instance_key(instance_key):
        return "", remainder
    return instance_key, channel_id


def parse_instance_key(chat_key: str) -> str:
    """从会话标识解析实例标识，历史格式返回空串"""
    return parse_chat_key(chat_key)[0]


def parse_channel_id(chat_key: str) -> str:
    """从会话标识解析频道标识，形如 `group_123456`"""
    return parse_chat_key(chat_key)[1]
