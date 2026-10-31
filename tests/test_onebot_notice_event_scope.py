"""不指向任何会话的通知事件，不该炸成一条 ERROR + traceback

真实现场（测试部署上真号 3000000001 的日志）：私聊里对方一开始打字，协议端就上报
`notice.notify.input_status`——它带着 user_id，`group_id` 是 0。而兜底的通知匹配器
`on_notice(priority=99999, block=False)` 会收下**每一种**通知，于是这条事件被解析成
`group_0`、拼成 `onebot_v11-ob3000000001-group_0`，在 `DBChatChannel.get_channel` 抛
`ValueError: 聊天频道不存在`，被 nonebot 翻译成整条 `Running Matcher(...) failed`。
实测两分钟内刷了 44 条 ERROR。

`get_chat_info_old` 里本来**有**一条「频道 ID 为 0 就拒绝」的守卫，但它写的是
`str(channel_body) == "0"`；多实例改造把 `channel_body` 换成了带前缀的 `group_0`，
判据从那天起永不成立——这是一条静默失效的守卫，没有任何测试盯着它。

这份测试因此分两层：
1. 判据本身（`is_addressable_channel_id` / `parse_channel_target_id`）表驱动；
2. **调用点**：真的驱动 `notice_matcher` 注册的那个处理函数，断言它对这条事件安静返回、
   且根本没走到查库那一步（§2.7 的教训：零件测得再全，接线断了测试照样全绿）。
"""

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import NotifyEvent

from nekro_agent.adapters.onebot_v11.core.channel import (
    is_addressable_channel_id,
    parse_channel_target_id,
)

# ---------------------------------------------------------------------------
# 1. 判据本身
# ---------------------------------------------------------------------------

TARGET_ID_CASES = [
    ("group_123456", "123456"),
    ("private_654321", "654321"),
    ("group_0", "0"),
    ("private_0", "0"),
    ("group_", ""),
    # 没有前缀：不是本适配器的频道标识形态
    ("123456", ""),
    ("", ""),
]


@pytest.mark.parametrize(("channel_id", "expected"), TARGET_ID_CASES)
def test_parse_channel_target_id(channel_id: str, expected: str) -> None:
    assert parse_channel_target_id(channel_id) == expected


ADDRESSABLE_CASES = [
    ("group_123456", True),
    ("private_654321", True),
    # 事故形态：协议端把「不属于任何会话」报成 group_id=0
    ("group_0", False),
    ("private_0", False),
    ("group_", False),
    # 旧判据只挡得住这一个，而改造之后它再也不会出现
    ("0", False),
    ("", False),
]


@pytest.mark.parametrize(("channel_id", "expected"), ADDRESSABLE_CASES)
def test_is_addressable_channel_id(channel_id: str, expected: bool) -> None:
    assert is_addressable_channel_id(channel_id) is expected


def test_zero_id_guard_is_not_written_against_the_bare_id() -> None:
    """守卫必须看前缀后面那一段，不能退回 `channel_body == "0"`

    这条是这次事故的直接教训：判据与被判值的形态一起变了，而只有一侧被改。
    """
    from nekro_agent.adapters.onebot_v11.tools import onebot_util

    # 按 AST 判，不按源码文本 —— 否则本文件的注释里提一句旧写法就能让断言反转
    func = ast.parse(inspect.getsource(onebot_util.get_chat_info_old)).body[0]
    called = {
        node.func.attr
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "is_addressable_channel_id" in called

    compared_to_zero = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Compare)
        and any(isinstance(c, ast.Constant) and c.value == "0" for c in node.comparators)
    ]
    assert not compared_to_zero, "旧判据 `channel_body == \"0\"` 在带前缀的取值上永不成立"


# ---------------------------------------------------------------------------
# 2. 调用点：真的驱动通知匹配器
# ---------------------------------------------------------------------------


def _input_status_event() -> NotifyEvent:
    """逐字复刻现场那条事件（见模块 docstring）"""
    return NotifyEvent(
        time=1786457675,
        self_id=3000000001,
        post_type="notice",
        notice_type="notify",
        sub_type="input_status",
        user_id=2000000003,
        group_id=0,
    )


def _group_poke_event() -> NotifyEvent:
    return NotifyEvent(
        time=1786457675,
        self_id=3000000001,
        post_type="notice",
        notice_type="notify",
        sub_type="poke",
        user_id=2000000003,
        group_id=500000001,
        target_id=3000000001,
    )


def _notice_handler():
    """取出 `notice_matcher` 上注册的那个处理函数"""
    from nekro_agent.adapters.onebot_v11.matchers import notice as notice_module

    handlers = notice_module.notice_matcher.handlers
    assert len(handlers) == 1, "通知匹配器上应当只有一个处理函数"
    return notice_module, handlers[0].call


@pytest.mark.asyncio
async def test_input_status_notice_is_skipped_without_touching_the_db(monkeypatch) -> None:
    notice_module, handler = _notice_handler()

    async def _resolve_instance_key(_self_id):
        return "ob3000000001"

    looked_up: list = []

    async def _get_channel(chat_key: str):
        looked_up.append(chat_key)
        raise AssertionError("不该为不指向任何会话的通知去查库")

    handled: list = []

    async def _handle(*args, **kwargs):
        handled.append((args, kwargs))
        return None

    monkeypatch.setattr(notice_module, "resolve_instance_key", _resolve_instance_key)
    monkeypatch.setattr(notice_module, "set_current_instance", lambda *a, **k: None)
    monkeypatch.setattr(notice_module.DBChatChannel, "get_channel", classmethod(lambda cls, chat_key: _get_channel(chat_key)))
    monkeypatch.setattr(notice_module.notice_manager, "handle", _handle)

    bot = SimpleNamespace(self_id="3000000001")

    # 安静返回：既不抛异常，也不查库，更不进处理器分发
    result = await handler(_=None, event=_input_status_event(), bot=bot)

    assert result is None
    assert looked_up == []
    assert handled == []


@pytest.mark.asyncio
async def test_real_group_notice_still_reaches_the_handlers(monkeypatch) -> None:
    """对照组：正常的群通知必须照旧走到查库与分发

    没有这一条的话，把整个函数体换成 `return` 也能让上面那条通过。
    """
    notice_module, handler = _notice_handler()

    async def _resolve_instance_key(_self_id):
        return "ob3000000001"

    looked_up: list = []
    sentinel_channel = SimpleNamespace(adapter=SimpleNamespace(cast=lambda _t: None))

    async def _get_channel(chat_key: str):
        looked_up.append(chat_key)
        return sentinel_channel

    handled: list = []

    async def _handle(event, bot, db_chat_channel):
        handled.append(db_chat_channel)
        return None  # 没有处理器匹配上，函数在此安静返回

    from nekro_agent.adapters.onebot_v11.tools import onebot_util

    monkeypatch.setattr(notice_module, "resolve_instance_key", _resolve_instance_key)
    monkeypatch.setattr(onebot_util, "resolve_instance_key", _resolve_instance_key)
    monkeypatch.setattr(notice_module, "set_current_instance", lambda *a, **k: None)
    monkeypatch.setattr(notice_module.DBChatChannel, "get_channel", classmethod(lambda cls, chat_key: _get_channel(chat_key)))
    monkeypatch.setattr(notice_module.notice_manager, "handle", _handle)

    bot = SimpleNamespace(self_id="3000000001")

    await handler(_=None, event=_group_poke_event(), bot=bot)

    assert looked_up == ["onebot_v11-ob3000000001-group_500000001"]
    assert handled == [sentinel_channel]


def test_the_guard_is_wired_into_the_matcher() -> None:
    """AST 契约：查库那两行必须包在 try 里，异常分支只 return 不再往上抛

    行为化那条已经覆盖了「安静返回」，这条额外钉住形状，防止有人把 try 挪走之后
    改用一个恰好也返回 None 的写法。
    """
    path = Path(
        inspect.getsourcefile(
            __import__("nekro_agent.adapters.onebot_v11.matchers.notice", fromlist=["notice"]),
        )
        or "",
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))

    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_"
        and any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "handle"
            for d in node.decorator_list
        )
    ]
    assert handlers, "没找到通知匹配器的处理函数"

    tries = [n for n in ast.walk(handlers[0]) if isinstance(n, ast.Try)]
    guarded = [
        t
        for t in tries
        if "get_chat_info_old" in ast.dump(ast.Module(body=t.body, type_ignores=[]))
        and "get_channel" in ast.dump(ast.Module(body=t.body, type_ignores=[]))
    ]
    assert guarded, "get_chat_info_old / get_channel 必须在同一个 try 里"

    handler_names = {
        h.type.id if isinstance(h.type, ast.Name) else None for h in guarded[0].handlers
    }
    assert "ValueError" in handler_names
    assert any(
        isinstance(stmt, ast.Return) for h in guarded[0].handlers for stmt in h.body
    ), "异常分支必须 return，不能继续往下走"
