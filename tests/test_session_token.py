"""Agent 会话令牌的回归测试

保护两条契约：

1. **令牌能跨 RPC 边界还原** —— 插件的沙盒方法经独立 HTTP 请求回调进来，
   新建的 `AgentCtx` 必须能通过 `container_key` 拿回本轮令牌，否则插件返回的
   外部内容只能用无令牌的弱边界，模型无法区分「资料」与「指令」。
2. **令牌绝不下发进沙盒容器** —— 容器内运行的是模型生成的代码，
   一旦读到令牌就能伪造可信标签，比完全没有令牌更危险。
"""

import pytest

from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.agent.session_token import (
    clear_session_token,
    get_session_token,
    register_session_token,
)
from nekro_agent.services.sandbox.runner import build_container_key

CHAT_KEY = "onebot_v11-ob10001-group_123456"
TOKEN = "0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_registry():
    container_key = build_container_key(CHAT_KEY)
    clear_session_token(container_key)
    yield
    clear_session_token(container_key)


class TestSessionTokenRegistry:
    def test_round_trip(self) -> None:
        container_key = build_container_key(CHAT_KEY)
        register_session_token(container_key, TOKEN)
        assert get_session_token(container_key) == TOKEN

    def test_unregistered_returns_none(self) -> None:
        assert get_session_token("never-registered") is None

    def test_empty_inputs_are_ignored(self) -> None:
        """空值不入表，避免用空 container_key 取到别人的令牌"""
        register_session_token("", TOKEN)
        register_session_token("some-key", "")
        assert get_session_token("") is None
        assert get_session_token("some-key") is None

    def test_rerun_overwrites(self) -> None:
        """同一频道的容器键稳定，新一轮运行必须覆盖旧令牌"""
        container_key = build_container_key(CHAT_KEY)
        register_session_token(container_key, TOKEN)
        register_session_token(container_key, "newtoken")
        assert get_session_token(container_key) == "newtoken"


class TestContainerKeyConsistency:
    """容器键的生成规则必须只有一处，否则关联会静默失效"""

    def test_key_is_deterministic(self) -> None:
        assert build_container_key(CHAT_KEY) == build_container_key(CHAT_KEY)

    def test_distinct_chats_get_distinct_keys(self) -> None:
        assert build_container_key(CHAT_KEY) != build_container_key("onebot_v11-ob10002-group_123456")

    def test_key_is_path_safe(self) -> None:
        """容器键会成为容器名与共享目录名的一部分"""
        key = build_container_key("weird/chat:key with spaces")
        assert "/" not in key
        assert ":" not in key
        assert " " not in key


class TestAgentCtxExposure:
    def test_ctx_resolves_registered_token(self) -> None:
        container_key = build_container_key(CHAT_KEY)
        register_session_token(container_key, TOKEN)
        ctx = AgentCtx(container_key=container_key, from_chat_key=CHAT_KEY)
        assert ctx.one_time_code == TOKEN

    def test_ctx_without_container_key_returns_empty(self) -> None:
        """Webhook / 命令等非 Agent 触发路径没有令牌，必须返回空串而不是抛异常"""
        ctx = AgentCtx(from_chat_key=CHAT_KEY)
        assert ctx.one_time_code == ""

    def test_ctx_with_unknown_container_key_returns_empty(self) -> None:
        ctx = AgentCtx(container_key="unknown-container", from_chat_key=CHAT_KEY)
        assert ctx.one_time_code == ""


class TestTokenNeverLeavesServer:
    """令牌不得出现在下发给沙盒容器的任何内容里

    这是本模块存在的根本理由：把令牌交给容器等于把伪造可信标签的能力交给模型生成的代码。
    """

    def test_api_caller_template_has_no_token_placeholder(self) -> None:
        from pathlib import Path

        template = Path("nekro_agent/services/sandbox/ext_caller_code.py").read_text(encoding="utf-8")
        for forbidden in ("one_time_code", "ONE_TIME_CODE", "session_token", "SESSION_TOKEN"):
            assert forbidden not in template, f"沙盒下发模板不得包含 {forbidden}"

    def test_api_caller_builder_does_not_inject_token(self) -> None:
        from pathlib import Path

        builder = Path("nekro_agent/services/sandbox/ext_caller.py").read_text(encoding="utf-8")
        for forbidden in ("one_time_code", "get_session_token"):
            assert forbidden not in builder, f"沙盒代码生成器不得引用 {forbidden}"
