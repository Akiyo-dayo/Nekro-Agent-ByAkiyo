"""合并转发消息渲染进提示词时的可信边界回归测试

保护的契约（对应两类攻击）：

1. **身份伪造** —— 转发节点的昵称与正文完全由发送方构造，与真实群聊消息格式同构。
   渲染时必须包裹在带一次性会话令牌的边界内并标注 `trust:untrusted`，
   否则模型无法区分「别人转发给我的第三方内容」和「本群刚刚发生的对话」。
2. **越权提权** —— 转发内容此前是唯一落到裸拼分支的消息段（Image/File/At/JsonCard 都有令牌包裹），
   该不一致必须保持修复状态。

参见 `nekro_agent/models/db_chat_message.py` 的 `convert_raw_msg_data_json_to_msg_prompt`。
"""

import json

import pytest

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_message import convert_raw_msg_data_json_to_msg_prompt

ONE_TIME_CODE = "0123456789abcdef"


def _forward_segment_json(text: str, node_count: int = 1) -> str:
    """构造一条只含合并转发段的 content_data JSON"""
    return json.dumps(
        [
            {
                "type": "forward",
                "text": text,
                "forward_content": [{"sender": f"用户{i}", "content": "x", "images": []} for i in range(node_count)],
            },
        ],
        ensure_ascii=False,
    )


class TestForwardPromptBoundary:
    def test_forward_is_wrapped_with_session_token(self) -> None:
        prompt = convert_raw_msg_data_json_to_msg_prompt(
            _forward_segment_json("[合并转发消息]\n小明: 你好", node_count=1),
            ONE_TIME_CODE,
            config,
        )
        assert prompt.startswith(f"<{ONE_TIME_CODE} | Forward nodes:1 trust:untrusted>")
        assert prompt.endswith(f"<{ONE_TIME_CODE} | /Forward>")

    def test_node_count_is_reported(self) -> None:
        """节点数进入标签，便于模型判断这是一大坨外部内容而非一句话"""
        prompt = convert_raw_msg_data_json_to_msg_prompt(
            _forward_segment_json("[合并转发消息]\na: 1\nb: 2\nc: 3", node_count=3),
            ONE_TIME_CODE,
            config,
        )
        assert f"<{ONE_TIME_CODE} | Forward nodes:3 trust:untrusted>" in prompt

    def test_forward_is_never_rendered_bare(self) -> None:
        """回归 R18-3：转发段曾落到裸拼分支，与 JSON 卡片的处理不一致"""
        body = "[合并转发消息]\n小明: 你好"
        prompt = convert_raw_msg_data_json_to_msg_prompt(_forward_segment_json(body), ONE_TIME_CODE, config)
        assert prompt != body
        assert ONE_TIME_CODE in prompt

    def test_control_characters_are_stripped_at_render(self) -> None:
        """历史遗留数据可能未在解析期清洗，渲染期必须兜底剥离控制字符"""
        prompt = convert_raw_msg_data_json_to_msg_prompt(
            _forward_segment_json("[合并转发消息]\n小明: 前\x00中​后"),
            ONE_TIME_CODE,
            config,
        )
        assert "\x00" not in prompt
        assert "​" not in prompt

    def test_long_forward_is_truncated(self) -> None:
        """转发体积由发送方决定，必须有渲染期上限，避免挤占上下文"""
        prompt = convert_raw_msg_data_json_to_msg_prompt(
            _forward_segment_json("[合并转发消息]\n" + "长" * 5000),
            ONE_TIME_CODE,
            config,
        )
        assert "…" in prompt
        assert len(prompt) < 3000

    def test_travel_mode_omits_token_but_still_sanitizes(self) -> None:
        """漫游模式与其他消息段一致地不带令牌，但清洗不能少"""
        prompt = convert_raw_msg_data_json_to_msg_prompt(
            _forward_segment_json("[合并转发消息]\n小明: a\x00b"),
            ONE_TIME_CODE,
            config,
            travel_mode=True,
        )
        assert ONE_TIME_CODE not in prompt
        assert "\x00" not in prompt


class TestOtherSegmentsUnaffected:
    """确认为转发新增的分支没有改变其他消息段的既有渲染"""

    @pytest.mark.parametrize(
        ("segment", "expected_fragment"),
        [
            ({"type": "text", "text": "普通文本"}, "普通文本"),
            (
                {"type": "at", "text": "", "target_platform_userid": "123", "target_nickname": "小明"},
                f"<{ONE_TIME_CODE} | At:[@id:123;nickname:小明@]>",
            ),
        ],
    )
    def test_segment_rendering_is_unchanged(self, segment: dict, expected_fragment: str) -> None:
        prompt = convert_raw_msg_data_json_to_msg_prompt(
            json.dumps([segment], ensure_ascii=False),
            ONE_TIME_CODE,
            config,
        )
        assert expected_fragment in prompt
