"""OneBot V11：频道标识里的目标 ID 不是数字时，发送与改群名片的报错

`channel_id` 约定是 `group_<群号>` / `private_<QQ 号>`。正常入站造不出别的形态（NoneBot 的事件模型把
`group_id` / `user_id` 校验成整数），但库里确实可能躺着 `group_<非数字>` 的 OneBot 频道行：
测试脚本、手工写库、早期探针都留下过，而且它可以一直是启用状态。节日祝福这类「对所有启用频道推一条
系统消息并触发 Agent」的广播会照常落到它头上，Agent 一回话就走到下面两处：

- 适配器 `_send_to_chat` 用 `int(channel_id.split("_")[1])` 取目标 ID，`group_tenant_gate_test`
  切出来是 `tenant`，抛 `invalid literal for int() with base 10: 'tenant'`；
- 内置 `status` 插件改群名片时 `partition("_")` 之后直接 `int(chat_id)`，抛 `... 'tenant_gate_test'`。

报错里没有频道标识，日志上看不出是哪个频道出的事，只能靠前后行去猜。适配器里同一种
`split("_")[1]` / `replace("group_", "")` 的写法还有好几处（文件发送、合并转发、取频道信息、戳一戳），
`judgement` 插件也是 `int(chat_id)`，修的时候应当收进同一个取目标 ID 的函数。

这两条用例以前是 strict xfail，标记着「已确认但尚未修复」。现在修复已经落地，它们是回归用例：
任何一处再退回 `int(channel_id.split("_")[1])` 这类写法，报错都会重新丢掉频道标识。
"""

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest
from nonebot.adapters.onebot.v11 import Message

from nekro_agent.adapters.interface.schemas.platform import PlatformUser
from nekro_agent.adapters.onebot_v11.core.channel import (
    NonNumericChannelIdError,
    is_numeric_channel_id,
    parse_numeric_target_id,
)
from nekro_agent.models.db_chat_channel import DBChatChannel

BAD_CHANNEL_ID = "group_tenant_gate_test"
BAD_CHAT_KEY = f"onebot_v11-inst1-{BAD_CHANNEL_ID}"


class _RecordingBot:
    """只记录调用、不连协议端的假 Bot"""

    self_id = "10001"

    def __init__(self) -> None:
        self.calls: List[Tuple[str, dict]] = []

    async def send_group_msg(self, **kwargs: Any) -> dict:
        self.calls.append(("send_group_msg", kwargs))
        return {"message_id": 1}

    async def send_private_msg(self, **kwargs: Any) -> dict:
        self.calls.append(("send_private_msg", kwargs))
        return {"message_id": 1}

    async def set_group_card(self, **kwargs: Any) -> None:
        self.calls.append(("set_group_card", kwargs))

    async def set_group_ban(self, **kwargs: Any) -> None:
        self.calls.append(("set_group_ban", kwargs))


class _RecordingLogger:
    """替换插件模块里的 `logger`，把每一级日志的文字收下来"""

    def __init__(self) -> None:
        self.records: List[Tuple[str, str]] = []

    def __getattr__(self, level: str):
        def _log(message: Any, *args: Any, **kwargs: Any) -> None:
            self.records.append((level, str(message)))

        return _log


@pytest.fixture
def bad_channel(monkeypatch: pytest.MonkeyPatch) -> DBChatChannel:
    channel = DBChatChannel(
        adapter_key="onebot_v11",
        instance_key="inst1",
        chat_key=BAD_CHAT_KEY,
        channel_id=BAD_CHANNEL_ID,
        channel_type="group",
    )

    async def fake_get_channel(*args: Any, **kwargs: Any) -> DBChatChannel:
        return channel

    monkeypatch.setattr(DBChatChannel, "get_channel", fake_get_channel)
    return channel


async def test_send_to_non_numeric_channel_reports_which_channel(
    monkeypatch: pytest.MonkeyPatch,
    bad_channel: DBChatChannel,
) -> None:
    from nekro_agent.adapters.onebot_v11 import adapter as adapter_module
    from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

    bot = _RecordingBot()

    async def fake_get_bot(chat_key: str) -> _RecordingBot:
        return bot

    monkeypatch.setattr(adapter_module, "get_bot_by_chat_key", fake_get_bot)

    with pytest.raises(Exception) as exc_info:
        await OnebotV11Adapter()._send_to_chat(BAD_CHAT_KEY, Message("hi"))

    message = str(exc_info.value)
    assert bot.calls == []
    assert "invalid literal for int()" not in message, message
    assert BAD_CHANNEL_ID in message, message


async def test_set_group_card_on_non_numeric_channel_does_not_crash_on_int(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.builtin import status as status_plugin

    bot = _RecordingBot()
    recorder = _RecordingLogger()

    async def fake_get_bot(chat_key: str) -> _RecordingBot:
        return bot

    async def fake_get_self_info(chat_key: str) -> PlatformUser:
        return PlatformUser(platform_name="onebot_v11", user_id="10001", user_name="bot", user_avatar="")

    monkeypatch.setattr(status_plugin, "get_bot_by_chat_key", fake_get_bot)
    monkeypatch.setattr(status_plugin, "logger", recorder)
    monkeypatch.setattr(status_plugin.config, "ENABLE_CHANGE_NICKNAME", True)

    ctx = SimpleNamespace(
        chat_key=BAD_CHAT_KEY,
        channel_id=BAD_CHANNEL_ID,
        adapter=SimpleNamespace(get_self_info=fake_get_self_info),
    )
    await status_plugin.set_bot_group_card(ctx, "立冬快乐")  # type: ignore[arg-type]

    assert not [call for call in bot.calls if call[0] == "set_group_card"]
    texts = [text for _level, text in recorder.records]
    assert not [text for text in texts if "invalid literal for int()" in text], texts
    # 改群名片只是锦上添花：脏频道行应当安静跳过，不该每轮都刷一条 error / 堆栈
    loud = [(level, text) for level, text in recorder.records if level in {"warning", "error", "exception"}]
    assert not loud, loud


# ---------------------------------------------------------------------------
# 取目标 ID 的判据本身
# ---------------------------------------------------------------------------

VALID_NUMERIC_CHANNEL_IDS = [
    ("group_123456", 123456),
    ("private_42", 42),
]

INVALID_NUMERIC_CHANNEL_IDS = [
    pytest.param("group_tenant_gate_test", id="非数字"),
    pytest.param("group_12a", id="数字后混字母"),
    pytest.param("group_", id="没有号码"),
    pytest.param("group_１２", id="全角数字"),
    pytest.param("group_123\n", id="结尾换行"),
    pytest.param("123456", id="没有前缀"),
    pytest.param("", id="空串"),
    pytest.param("channel_1", id="其它前缀"),
]


@pytest.mark.parametrize(("channel_id", "expected"), VALID_NUMERIC_CHANNEL_IDS)
def test_parse_numeric_target_id_accepts_numeric_ids(channel_id: str, expected: int) -> None:
    assert parse_numeric_target_id(channel_id) == expected
    assert is_numeric_channel_id(channel_id) is True


@pytest.mark.parametrize("channel_id", INVALID_NUMERIC_CHANNEL_IDS)
def test_parse_numeric_target_id_rejects_non_numeric_ids(channel_id: str) -> None:
    with pytest.raises(NonNumericChannelIdError) as exc_info:
        parse_numeric_target_id(channel_id)

    # 继承 ValueError 是既有 `except ValueError` 兜底仍能接住它的前提
    assert isinstance(exc_info.value, ValueError)
    # 异常文案里用 `!r` 呈现标识，换行等控制字符会被转义，所以两种形态都认
    message = str(exc_info.value)
    assert channel_id in message or repr(channel_id) in message, message
    assert is_numeric_channel_id(channel_id) is False


# ---------------------------------------------------------------------------
# 禁言插件：脏频道标识的失败说明里要带频道标识
# ---------------------------------------------------------------------------


async def test_mute_on_non_numeric_channel_reports_which_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugins.builtin import judgement as judgement_plugin

    bot = _RecordingBot()

    async def fake_get_bot(chat_key: str) -> _RecordingBot:
        return bot

    monkeypatch.setattr(judgement_plugin, "get_bot_by_chat_key", fake_get_bot)
    monkeypatch.setattr(judgement_plugin.config_judgement, "ENABLE_ADMIN_REPORT", False)

    ctx = SimpleNamespace(
        from_chat_key=BAD_CHAT_KEY,
        chat_key=BAD_CHAT_KEY,
        channel_id=BAD_CHANNEL_ID,
    )
    result = await judgement_plugin.mute_user(ctx, BAD_CHAT_KEY, "20002", 60, "刷屏")  # type: ignore[arg-type]

    assert BAD_CHANNEL_ID in result
    assert "invalid literal for int()" not in result
    assert not [call for call in bot.calls if call[0] == "set_group_ban"]


# ---------------------------------------------------------------------------
# AST 契约：不能用 int() 去解析频道标识里的目标 ID
# ---------------------------------------------------------------------------


def test_no_int_call_parses_a_channel_identifier() -> None:
    """所有 `int(...)` 的实参里都不该出现频道标识相关的变量

    行为用例只覆盖了 `_send_to_chat` / `set_group_card` / 禁言三处；这个契约盯住整个文件，
    以后新加的发送路径照抄旧写法时会被直接抓出来。
    """
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "nekro_agent" / "adapters" / "onebot_v11" / "adapter.py",
        root / "plugins" / "builtin" / "status.py",
        root / "plugins" / "builtin" / "judgement.py",
        root / "plugins" / "builtin" / "ai_voice.py",
        root / "plugins" / "builtin" / "group_honor.py",
    ]

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "int":
                dumped = ast.dump(node)
                assert "channel_id" not in dumped, f"{path.name} 仍在用 int() 解析频道标识: {dumped}"
                assert "chat_id" not in dumped, f"{path.name} 仍在用 int() 解析频道标识: {dumped}"
