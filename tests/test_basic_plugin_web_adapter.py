"""基础插件对网页适配器的支持面

网页会话只有"发一条文本"和"发一个文件"两种落库形态（见 `adapters/web/adapter.py`
的 `_build_agent_messages_from_platform_segments`），图文混排与头像工具都没有意义：
下发用不了的方法，模型会照着提示词去调，然后在运行期拿到一个空结果。
"""

from typing import Any, List

import pytest


class _Ctx:
    def __init__(self, adapter_key: str) -> None:
        self.adapter_key = adapter_key
        self.chat_key = f"{adapter_key}-session_test"
        self.container_key = "container_test"


def _basic_plugin() -> Any:
    import plugins.builtin.basic as basic

    return basic


def test_web_is_in_the_declared_support_list() -> None:
    """插件元数据里没写 `web` 时，插件根本不会在该适配器下加载"""
    basic = _basic_plugin()

    assert "web" in basic.plugin.support_adapter


@pytest.mark.asyncio
async def test_web_gets_text_and_file_only() -> None:
    basic = _basic_plugin()

    methods = await basic.collect_available_methods(_Ctx("web"))

    names = [method.__name__ for method in methods]
    assert "send_msg_text" in names
    assert "send_msg_file" in names
    assert "send_msg_mixed" not in names
    assert "get_user_avatar" not in names


@pytest.mark.asyncio
async def test_onebot_still_gets_everything() -> None:
    """对照组：加这个分支不能顺手收窄别的适配器"""
    basic = _basic_plugin()

    methods = await basic.collect_available_methods(_Ctx("onebot_v11"))

    names: List[str] = [method.__name__ for method in methods]
    assert {"send_msg_text", "send_msg_file", "send_msg_mixed", "get_user_avatar"} <= set(names)
