"""运维回合的上下文里有外部内容时，写操作一律拒绝

## 为什么单独测

「读过外部内容之后不写」原来只看本轮调用的方法返回了什么。回合开始时渲染进来的历史——
运维群里名单外的人的发言、定时器与外部推送带进来的正文——同样是模型的输入，却不经过
任何一次方法调用。这里测的是渲染历史时那一道判断：哪些消息算、哪些不算，以及它和
「本轮读过工具结果」两种来源的区别（前者再说一句也没用）。
"""

from types import SimpleNamespace
from typing import Any, List

import pytest

from nekro_agent.core.config import config
from nekro_agent.schemas.chat_message import SELF_SENT_SENDER_ID, TOOL_RESULT_SENDER_ID
from nekro_agent.services.ops_agent import context_guard as guard_module
from nekro_agent.services.ops_agent import round_grant as grant_module
from nekro_agent.services.ops_agent.context_guard import (
    REASON_NON_OPERATOR,
    REASON_PUSHED,
    REASON_SELF_SENT,
    note_rendered_history,
    outside_content_reason,
)
from nekro_agent.services.ops_agent.policy import OpsAgentPolicy
from nekro_agent.services.ops_agent.round_grant import (
    TAINT_CONTEXT,
    TAINT_TOOL,
    OpsRoundGrant,
    authorize_call,
    close_round,
    inside_ops_round,
    mark_tainted,
    reset_round_grants,
)
from nekro_agent.services.sandbox.runner import build_container_key

ADAPTER = "onebot_v11"
INSTANCE = "opsinst"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_10001"
OPERATOR = "10001"
OTHER_OPERATOR = "10002"
OUTSIDER = "20001"
CONTAINER = build_container_key(CHAT_KEY)
NOW = 1_800_000.0


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch):
    reset_round_grants()
    monkeypatch.setattr(grant_module, "_wall_clock", lambda: NOW)
    yield
    reset_round_grants()


@pytest.fixture
def ops_on(monkeypatch: pytest.MonkeyPatch):
    for name, value in {
        "OPS_AGENT_TOOLS_ENABLED": True,
        "OPS_AGENT_OPERATORS": [OPERATOR, OTHER_OPERATOR],
        "OPS_AGENT_GROUPS": [CHAT_KEY],
        "OPS_AGENT_PRIVATE_ENABLED": True,
        "OPS_AGENT_WRITE_LIMIT_PER_ROUND": 10,
        "OPS_AGENT_WRITE_LIMIT_PER_HOUR": 10,
    }.items():
        monkeypatch.setattr(config, name, value)


def install_grant() -> OpsRoundGrant:
    grant = OpsRoundGrant(
        round_id="r1",
        container_key=CONTAINER,
        chat_key=CHAT_KEY,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        operators=frozenset({OPERATOR}),
        is_group=True,
    )
    grant_module._grants[CONTAINER] = grant  # noqa: SLF001
    return grant


def message(sender_id: str, *, user_id: str = "", timestamp: float = NOW - 30) -> Any:
    return SimpleNamespace(sender_id=sender_id, platform_userid=user_id, send_timestamp=int(timestamp))


def human(user_id: str, **kwargs: Any) -> Any:
    return message(user_id, user_id=user_id, **kwargs)


def bot(**kwargs: Any) -> Any:
    return message("-1", user_id="10000012", **kwargs)


def reason_of(messages: List[Any]) -> str:
    return outside_content_reason(
        chat_key=CHAT_KEY,
        adapter_key=ADAPTER,
        opened_wall=NOW,
        messages=messages,
        policy=OpsAgentPolicy.from_config(config),
    )


class TestWhatCountsAsOutsideContent:
    def test_operators_and_the_bot_alone_are_clean(self, ops_on) -> None:  # noqa: ARG002
        assert reason_of([human(OPERATOR), bot(), human(OTHER_OPERATOR), bot()]) == ""

    def test_a_non_operator_in_the_window_counts(self, ops_on) -> None:  # noqa: ARG002
        assert reason_of([human(OPERATOR), human(OUTSIDER), bot()]) == REASON_NON_OPERATOR

    def test_a_message_from_another_program_on_the_bot_account_counts(self, ops_on) -> None:  # noqa: ARG002
        assert reason_of([message(SELF_SENT_SENDER_ID, user_id="10000012")]) == REASON_SELF_SENT

    def test_a_pushed_untrusted_body_counts(self, ops_on) -> None:  # noqa: ARG002
        assert reason_of([message(TOOL_RESULT_SENDER_ID, timestamp=NOW - 300)]) == REASON_PUSHED

    def test_a_tool_result_read_in_an_earlier_ops_round_does_not_count(self, ops_on) -> None:  # noqa: ARG002
        # 上一轮运维人员让 Bot 读了错误日志：那一轮结束时记下了起止时刻
        earlier = install_grant()
        earlier.opened_wall = NOW - 400
        close_round(CONTAINER)
        assert inside_ops_round(CHAT_KEY, NOW - 350)

        assert reason_of([message(TOOL_RESULT_SENDER_ID, timestamp=NOW - 350)]) == ""
        # 落在那一轮之外的仍是推送
        assert reason_of([message(TOOL_RESULT_SENDER_ID, timestamp=NOW - 500)]) == REASON_PUSHED

    def test_this_rounds_own_tool_result_is_left_to_the_rpc_path(self, ops_on) -> None:  # noqa: ARG002
        assert reason_of([message(TOOL_RESULT_SENDER_ID, timestamp=NOW + 5)]) == ""


class TestNoteRenderedHistory:
    async def test_a_non_operator_in_history_refuses_writes_with_its_own_reason(
        self,
        ops_on,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        grant = install_grant()

        async def owned(*_args: Any) -> bool:
            return True

        monkeypatch.setattr(grant_module, "_instance_platform_owned", owned)

        note_rendered_history(CHAT_KEY, [human(OUTSIDER), human(OPERATOR)])

        assert (grant.tainted, grant.taint_source) == (True, TAINT_CONTEXT)
        allowed, reason = await authorize_call(CONTAINER, CHAT_KEY, "ops_set_channel_active", is_write=True)
        assert (allowed, reason) == (False, "tainted_context")
        # 读工具照常
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (True, "")
        # 关掉工具只会缩小权限，照样放行
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_tools_off", is_write=True) == (True, "")

    async def test_a_clean_history_leaves_writes_alone(self, ops_on, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        grant = install_grant()

        async def owned(*_args: Any) -> bool:
            return True

        monkeypatch.setattr(grant_module, "_instance_platform_owned", owned)

        note_rendered_history(CHAT_KEY, [human(OPERATOR), bot()])

        assert grant.tainted is False
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_channel_active", is_write=True) == (True, "")

    def test_switch_off_touches_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "OPS_AGENT_TOOLS_ENABLED", False)
        grant = install_grant()

        def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("开关关着时不该做任何判断")

        monkeypatch.setattr(guard_module, "current_grant_for_chat", explode)

        note_rendered_history(CHAT_KEY, [human(OUTSIDER)])

        assert grant.tainted is False

    def test_a_check_that_fails_counts_as_outside_content(self, ops_on, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        grant = install_grant()

        def broken(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("名单读不出来")

        monkeypatch.setattr(guard_module, "is_operator", broken)

        note_rendered_history(CHAT_KEY, [human(OPERATOR)])

        assert (grant.tainted, grant.taint_source) == (True, TAINT_CONTEXT)

    def test_no_grant_no_effect(self, ops_on) -> None:  # noqa: ARG002
        note_rendered_history(CHAT_KEY, [human(OUTSIDER)])
        assert grant_module.get(CONTAINER) is None

    def test_context_wins_over_tool_either_way(self, ops_on) -> None:  # noqa: ARG002
        grant = install_grant()
        mark_tainted(CONTAINER)
        assert grant.taint_source == TAINT_TOOL

        note_rendered_history(CHAT_KEY, [human(OUTSIDER)])
        assert grant.taint_source == TAINT_CONTEXT

        mark_tainted(CONTAINER)
        assert grant.taint_source == TAINT_CONTEXT


class TestRoundSpans:
    def test_closing_a_round_remembers_its_span_and_old_spans_expire(self, ops_on, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        grant = install_grant()
        grant.opened_wall = NOW - 100
        close_round(CONTAINER)
        assert inside_ops_round(CHAT_KEY, NOW - 50)
        assert not inside_ops_round(CHAT_KEY, NOW - 200)
        assert not inside_ops_round("other-chat", NOW - 50)

        # 很久以后再收一轮：前一轮的起止时刻过了保留期，被丢掉
        later = NOW + grant_module.ROUND_SPAN_KEEP_SECONDS + 1000
        monkeypatch.setattr(grant_module, "_wall_clock", lambda: later)
        second = install_grant()
        second.opened_wall = later - 10
        close_round(CONTAINER)
        assert not inside_ops_round(CHAT_KEY, NOW - 50)
        assert inside_ops_round(CHAT_KEY, later - 5)


class TestHistoryRendererCallsTheGuard:
    async def test_render_history_data_hands_over_what_it_rendered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tests.test_history_image_local_path_scope import _render_with_segments

        seen: List[Any] = []

        def record(chat_key: str, messages: Any) -> None:
            seen.append((chat_key, list(messages)))

        monkeypatch.setattr(guard_module, "note_rendered_history", record)

        await _render_with_segments(monkeypatch, [])

        assert len(seen) == 1
        chat_key, messages = seen[0]
        assert chat_key
        assert len(messages) == 1
