"""多实例 chat_key 里的 @ 解析必须取到正确的群号

`at_parser` 曾用 `db_chat_channel.chat_key.replace("group_", "")` 取群号。单实例格式
（`onebot_v11-group_123456`）下碰巧对；多实例格式（`onebot_v11-inst1-group_123456`）下
得到的是 `onebot_v11-inst1-123456`，`get_user_group_card_name` 里 `int(group_id)` 直接抛
`ValueError`。触发条件是 `SESSION_ENABLE_AT` 为 False 且 `[@id:xxx@]` 不带昵称。

两个模块（`nekro_agent/tools/at_parser.py` 与适配器里的同名模块）逻辑相同，这里对两者各跑一遍：
撤掉任一处改用 `parse_channel_target_id(parse_channel_id(...))` 都会红。
"""

from types import SimpleNamespace
from typing import Any, List

import pytest

MODULES = [
    "nekro_agent.tools.at_parser",
    "nekro_agent.adapters.onebot_v11.tools.at_parser",
]


def make_channel(chat_key: str) -> SimpleNamespace:
    """只实现解析链路用到的成员：`adapter.config.SESSION_ENABLE_AT` 与私聊分支的 chat_key"""
    return SimpleNamespace(
        chat_key=chat_key,
        adapter=SimpleNamespace(config=SimpleNamespace(SESSION_ENABLE_AT=False)),
    )


class _FakeBot:
    """记录 `get_group_member_info` 收到的参数，证明 int() 那一步真的被走过"""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def get_group_member_info(self, *, group_id: Any, user_id: Any, no_cache: bool) -> dict:
        self.calls.append({"group_id": group_id, "user_id": user_id, "no_cache": no_cache})
        return {"card": "名片"}


@pytest.fixture
def recorded(monkeypatch):
    """把模块内的 `get_user_group_card_name` 换成记账替身

    直接替模块属性而不是替 `onebot_util` 里的原名：解析器导入的是名字本身，
    替原名对已经 import 过的解析器无效。
    """
    calls: List[str] = []

    async def fake(group_id: str, user_id: str, db_chat_channel: Any) -> str:  # noqa: ARG001
        calls.append(group_id)
        return "昵称"

    return calls, fake


@pytest.mark.parametrize("module_path", MODULES)
class TestGroupIdExtraction:
    @pytest.mark.parametrize(
        ("chat_key", "expected"),
        [
            # 多实例格式：修复前得到 `onebot_v11-inst1-123456`
            ("onebot_v11-inst1-group_123456", "123456"),
            # 旧单实例格式：修复前后都应当对，别把兼容性改坏
            ("onebot_v11-group_123456", "123456"),
            # 实例标识带下划线和数字：分段解析不能被它们干扰
            ("onebot_v11-my_inst2-group_9", "9"),
        ],
    )
    async def test_multi_instance_chat_key(
        self,
        module_path: str,
        chat_key: str,
        expected: str,
        recorded,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls, fake = recorded
        module = __import__(module_path, fromlist=["parse_at_from_text"])
        monkeypatch.setattr(module, "get_user_group_card_name", fake)

        result = await module.parse_at_from_text("你好 [@id:42@]", make_channel(chat_key))

        assert calls == [expected]
        assert isinstance(calls[0], str)
        assert "昵称" in result

    async def test_non_numeric_group_id_falls_back_to_uid(
        self,
        module_path: str,
        recorded,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """解析不出数字群号时不硬 int()：退回 QQ 号，与「查不到群名片」同一行为"""
        calls, fake = recorded
        module = __import__(module_path, fromlist=["parse_at_from_text"])
        monkeypatch.setattr(module, "get_user_group_card_name", fake)

        result = await module.parse_at_from_text("你好 [@id:42@]", make_channel("onebot_v11-inst1-group_abc"))

        assert calls == []
        assert "42" in result

    async def test_private_chat_is_unchanged(
        self,
        module_path: str,
        recorded,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """私聊无法 @：原行为是空串，修复不该碰它"""
        calls, fake = recorded
        module = __import__(module_path, fromlist=["parse_at_from_text"])
        monkeypatch.setattr(module, "get_user_group_card_name", fake)

        result = await module.parse_at_from_text("你好 [@id:42@]", make_channel("onebot_v11-inst1-private_5"))

        assert calls == []
        # 原行为：私聊的 @ 段渲染成空串（尾部还会带一个原文本留下的空串，一并保留）
        assert result == ["你好 ", "", ""]


class TestRealCallChain:
    """真调用链：不替 `get_user_group_card_name`，只替它内部的 `get_bot_by_chat_key`

    这样 `get_user_group_card_name` 里的 `int(group_id)` 会被真的执行——多实例 chat_key 在
    修复前正是在这一步抛 `ValueError`。
    """

    @pytest.mark.parametrize("module_path", MODULES)
    async def test_group_id_reaches_bot_as_int(
        self,
        module_path: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.adapters.onebot_v11.tools import onebot_util

        bot = _FakeBot()

        async def fake_get_bot(_chat_key: str) -> _FakeBot:
            return bot

        async def fake_self_info(_chat_key: str) -> SimpleNamespace:
            # 与 @ 的目标不是同一个号，避免走「@自己」的分支
            return SimpleNamespace(user_id="999")

        monkeypatch.setattr(onebot_util, "get_bot_by_chat_key", fake_get_bot)
        module = __import__(module_path, fromlist=["parse_at_from_text"])
        channel = make_channel("onebot_v11-inst1-group_123456")
        channel.adapter.get_self_info = fake_self_info

        result = await module.parse_at_from_text("你好 [@id:42@]", channel)

        assert [call["group_id"] for call in bot.calls] == [123456]
        assert isinstance(bot.calls[0]["group_id"], int)
        assert "名片" in result
