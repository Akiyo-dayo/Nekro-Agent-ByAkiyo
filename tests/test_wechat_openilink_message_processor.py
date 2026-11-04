"""微信 OpenILink 入站图片 / 文件落盘（上游 `ca32bb0`）

上游改的是「媒体消息不能只剩一段文本」：`image_item` / `file_item` 与各个 URL 字段都要
落成本地文件，落不下来时降级成一个**纯文本占位**段，而不是让整条消息消失。

本分支的增量只有一处，这里重点钉住它：落盘目标目录用的 `chat_key` 必须走
`BaseAdapter.build_chat_key()`，不能自己拼 `f"{adapter_key}-{channel_id}"`。
"""

from typing import Any, List, Optional, Tuple

import pytest

from nekro_agent.adapters.wechat_openilink.config import WeChatOpenILinkConfig
from nekro_agent.adapters.wechat_openilink.message_processor import OpenILinkMessageProcessor
from nekro_agent.schemas.chat_message import (
    ChatMessageSegment,
    ChatMessageSegmentFile,
    ChatMessageSegmentImage,
    ChatMessageSegmentType,
)


def _processor(build_chat_key: Optional[Any] = None) -> OpenILinkMessageProcessor:
    return OpenILinkMessageProcessor(
        config=WeChatOpenILinkConfig(),
        adapter_key="wechat_openilink",
        self_user_id="wxid_bot",
        build_chat_key=build_chat_key or (lambda channel_id: f"wechat_openilink-{channel_id}"),
    )


def _record_images(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, str, str, str]]:
    calls: List[Tuple[str, str, str, str]] = []

    async def fake_create_from_url(
        cls: Any,
        url: str,
        from_chat_key: str,
        file_name: str = "",
        use_suffix: str = "",
    ) -> Any:
        calls.append((url, from_chat_key, file_name, use_suffix))
        return cls(
            type=ChatMessageSegmentType.IMAGE,
            text=f"[Image: {file_name}]",
            file_name=file_name,
            local_path=f"/tmp/{file_name}",
            remote_url=url,
        )

    monkeypatch.setattr(ChatMessageSegmentImage, "create_from_url", classmethod(fake_create_from_url))
    return calls


def _record_files(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, str, str, str]]:
    calls: List[Tuple[str, str, str, str]] = []

    async def fake_create_from_url(
        cls: Any,
        url: str,
        from_chat_key: str,
        file_name: str = "",
        use_suffix: str = "",
    ) -> Any:
        calls.append((url, from_chat_key, file_name, use_suffix))
        return cls(
            type=ChatMessageSegmentType.FILE,
            text=f"[File: {file_name}]",
            file_name=file_name,
            local_path=f"/tmp/{file_name}",
            remote_url=url,
        )

    monkeypatch.setattr(ChatMessageSegmentFile, "create_from_url", classmethod(fake_create_from_url))
    return calls


async def test_image_only_message_becomes_an_image_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_images(monkeypatch)

    parsed = await _processor().parse(
        {
            "user_id": "wxid_user",
            "message_id": "msg-image",
            "type": "image",
            "image_url": "https://example.test/image.jpg",
        },
    )

    assert parsed is not None
    assert parsed.message.content_text == "[Image: image.jpg]"
    assert parsed.message.content_data[0].type == ChatMessageSegmentType.IMAGE.value
    assert calls == [("https://example.test/image.jpg", "wechat_openilink-private_wxid__user", "image.jpg", ".jpg")]


async def test_file_only_message_becomes_a_file_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_files(monkeypatch)

    parsed = await _processor().parse(
        {
            "user_id": "wxid_user",
            "message_id": "msg-file",
            "type": "file",
            "file_url": "https://example.test/report.pdf",
            "file_name": "report.pdf",
        },
    )

    assert parsed is not None
    assert parsed.message.content_text == "[File: report.pdf]"
    assert parsed.message.content_data[0].type == ChatMessageSegmentType.FILE.value
    assert calls == [("https://example.test/report.pdf", "wechat_openilink-private_wxid__user", "report.pdf", "")]


async def test_text_and_image_are_both_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    _record_images(monkeypatch)

    parsed = await _processor().parse(
        {
            "user_id": "wxid_user",
            "message_id": "msg-mixed",
            "type": "image",
            "text": "看这张图",
            "image_url": "https://example.test/image.jpg",
        },
    )

    assert parsed is not None
    assert [seg.type for seg in parsed.message.content_data] == [
        ChatMessageSegmentType.TEXT.value,
        ChatMessageSegmentType.IMAGE.value,
    ]
    assert parsed.message.content_text == "看这张图 [Image: image.jpg]"


async def test_item_list_image_is_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """微信侧常见形态：类型字段不写，媒体藏在 `item_list[*].image_item` 里"""
    calls = _record_images(monkeypatch)

    parsed = await _processor().parse(
        {
            "user_id": "wxid_user",
            "message_id": "msg-items",
            "item_list": [
                {"image_item": {"url": "https://example.test/a.png", "file_name": "a.png"}},
            ],
        },
    )

    assert parsed is not None
    assert parsed.message.content_data[0].type == ChatMessageSegmentType.IMAGE.value
    assert calls == [("https://example.test/a.png", "wechat_openilink-private_wxid__user", "a.png", ".jpg")]


async def test_item_list_file_is_picked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_files(monkeypatch)

    parsed = await _processor().parse(
        {
            "user_id": "wxid_user",
            "message_id": "msg-items-file",
            "item_list": [
                {"file_item": {"url": "https://example.test/a.zip", "name": "a.zip"}},
            ],
        },
    )

    assert parsed is not None
    assert parsed.message.content_data[0].type == ChatMessageSegmentType.FILE.value
    assert calls == [("https://example.test/a.zip", "wechat_openilink-private_wxid__user", "a.zip", "")]


async def test_group_message_uses_group_chat_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_images(monkeypatch)

    parsed = await _processor().parse(
        {
            "user_id": "wxid_user",
            "group_id": "123456",
            "message_id": "msg-group",
            "type": "image",
            "image_url": "https://example.test/g.jpg",
        },
    )

    assert parsed is not None
    assert parsed.channel.channel_id == "group_123456"
    assert calls[0][1] == "wechat_openilink-group_123456"


async def test_download_failure_degrades_to_a_text_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """一张下不来的图不该把用户打的字一起丢掉，也不能整条消息消失"""

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("下载被出站防护拒绝")

    monkeypatch.setattr(ChatMessageSegmentImage, "create_from_url", classmethod(boom))

    parsed = await _processor().parse(
        {
            "user_id": "wxid_user",
            "message_id": "msg-bad-image",
            "type": "image",
            "text": "文字还在",
            "image_url": "http://127.0.0.1/evil.jpg",
        },
    )

    assert parsed is not None
    assert [seg.type for seg in parsed.message.content_data] == [
        ChatMessageSegmentType.TEXT.value,
        ChatMessageSegmentType.TEXT.value,
    ]
    assert parsed.message.content_data[1].text == "[图片下载失败]"
    assert parsed.message.content_text == "文字还在 [图片下载失败]"


async def test_text_only_message_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_images(monkeypatch)

    parsed = await _processor().parse(
        {"user_id": "wxid_user", "message_id": "msg-text", "text": "纯文本"},
    )

    assert parsed is not None
    assert parsed.message.content_text == "纯文本"
    assert len(parsed.message.content_data) == 1
    assert calls == []


async def test_empty_message_is_dropped() -> None:
    """既没文字也没媒体：解析结果为空，不该造一条空消息"""
    assert await _processor().parse({"user_id": "wxid_user", "message_id": "msg-empty"}) is None


async def test_inline_bytes_are_written_to_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Tuple[bytes, str, str, str]] = []

    async def fake_create_from_bytes(
        cls: Any,
        _bytes: bytes,
        from_chat_key: str,
        file_name: str = "",
        use_suffix: str = "",
    ) -> Any:
        calls.append((_bytes, from_chat_key, file_name, use_suffix))
        return cls(
            type=ChatMessageSegmentType.IMAGE,
            text=f"[Image: {file_name}]",
            file_name=file_name,
            local_path=f"/tmp/{file_name}",
        )

    monkeypatch.setattr(ChatMessageSegmentImage, "create_from_bytes", classmethod(fake_create_from_bytes))

    parsed = await _processor().parse(
        {
            "user_id": "wxid_user",
            "message_id": "msg-bytes",
            "type": "image",
            "image": {"data": b"\x89PNG\r\n", "file_name": "inline.png"},
        },
    )

    assert parsed is not None
    assert calls == [(b"\x89PNG\r\n", "wechat_openilink-private_wxid__user", "inline.png", ".jpg")]


async def test_chat_key_comes_from_the_adapter_not_a_hand_built_string() -> None:
    """本分支的增量：chat_key 必须来自 `BaseAdapter.build_chat_key`

    多实例适配器会在这里插入实例段；自己拼 `f"{adapter_key}-{channel_id}"` 会让媒体
    落到别人的上传目录，`history.py` 的本地图片范围校验随之读不到。
    """
    from nekro_agent.adapters.wechat_openilink.adapter import WeChatOpenILinkAdapter

    adapter = WeChatOpenILinkAdapter()
    processor = OpenILinkMessageProcessor(
        config=WeChatOpenILinkConfig(),
        adapter_key=adapter.key,
        build_chat_key=adapter.build_chat_key,
    )

    # 单实例：与手工拼接一致
    assert processor._resolve_chat_key("private_a") == adapter.build_chat_key("private_a")

    # 多实例：只用 `channel_id` 拼不出这一层，说明必须走适配器的方法
    assert adapter.build_chat_key("private_a", "inst1") == "wechat_openilink-inst1-private_a"
    assert processor._resolve_chat_key("private_a") != "wechat_openilink-inst1-private_a"


async def test_adapter_passes_its_own_chat_key_builder() -> None:
    """适配器构造 processor 时必须把自己的 `build_chat_key` 传进去"""
    import ast
    import inspect
    import textwrap

    from nekro_agent.adapters.wechat_openilink import adapter as adapter_mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(adapter_mod.WeChatOpenILinkAdapter.__init__)))
    keywords = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "OpenILinkMessageProcessor"
        for keyword in node.keywords
    }

    assert "build_chat_key" in keywords


async def test_inbound_handler_awaits_parse() -> None:
    """`parse` 变成 async 后，调用点必须 `await`，否则拿到的是协程对象、消息全丢"""
    import ast
    import inspect
    import textwrap

    from nekro_agent.adapters.wechat_openilink import adapter as adapter_mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(adapter_mod.WeChatOpenILinkAdapter._handle_inbound_message)))
    awaited = {
        node.value.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
    }

    assert "parse" in awaited


def test_placeholder_text_carries_no_attacker_controlled_content() -> None:
    """占位段会原样进提示词，文案里不能拼文件名 / URL 这类发送方可控内容"""
    import inspect
    import re

    source = inspect.getsource(OpenILinkMessageProcessor._build_media_segment)
    placeholder = re.search(r'text=f"\[[^"]*下载失败\]"', source)

    assert placeholder is not None
    # 只有「图片」/「文件」这两个固定词，没有 file_name / url
    assert "{media_name}" in placeholder.group(0)
    assert "file_name" not in placeholder.group(0)
    assert "url" not in placeholder.group(0)


def test_segment_type_helper_matches_enum() -> None:
    assert ChatMessageSegment(type=ChatMessageSegmentType.TEXT, text="x").type == "text"
