"""图文混合发送的回归测试

保护三组契约：

**引用消息 ID 的入口校验**
模型想把"图 + 配文"凑成一条消息时，最常见的写法是把配文或图片路径塞进
`send_msg_file(_ck, path, "配文")` 的第三个参数（那是 `ref_msg_id`）。这类值以前会一路
带到 OneBot 适配器的 `int(ref_msg_id)` 才炸开，用户既收不到图也收不到文字，
而模型拿到的错误是"请确保聊天标识正确且内容不为空或过长"，与真实原因毫无关系。

**消息段序列构造**
`send_msg_mixed` 的全部价值就是让文字和图片落在同一条平台消息里，且顺序与模型给出的一致。

**资源段类型按 MIME 判定**
模型经常把 PDF 之类标成 `image`，按声明发出去用户会看到一张损坏的图片。
"""

from pathlib import Path
from typing import List, Optional

import pytest

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendSegment,
    PlatformSendSegmentType,
)
from nekro_agent.schemas.agent_message import AgentMessageSegment, AgentMessageSegmentType
from nekro_agent.services.chat.universal_chat_service import universal_chat_service
from nekro_agent.tools.message_id import is_plausible_message_id, resolve_ref_msg_id

# 1x1 透明 PNG，用于让 libmagic 真的识别出 image/png
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082",
)


class TestRefMsgIdValidation:
    """把配文/路径当成 ref_msg_id 传进来时必须在入口被拒绝，且提示可执行"""

    @pytest.mark.parametrize(
        "raw",
        [
            "这是给你的图",  # 中文配文
            "Here you go",  # 带空格的英文配文
            "./shared/heart.png",  # 沙盒相对路径
            "/app/shared/heart.png",  # 沙盒绝对路径
            "https://example.com/a.png",  # URL
            "a" * 65,  # 超长串
        ],
    )
    def test_rejects_values_that_are_not_message_ids(self, raw: str) -> None:
        assert not is_plausible_message_id(raw)
        with pytest.raises(ValueError, match="send_msg_mixed"):
            resolve_ref_msg_id(raw)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            ("", None),
            ("123456789", "123456789"),  # OneBot / Telegram / Discord 风格
            ("msg_id:123456789", "123456789"),  # 模型常见的元信息误写
            ("ou_ab12cd34ef", "ou_ab12cd34ef"),  # 飞书风格
            ("wxid_8a7b6c5d", "wxid_8a7b6c5d"),  # 微信风格
            ("Zm9vYmFy-_=", "Zm9vYmFy-_="),  # base64url 风格
        ],
    )
    def test_accepts_real_message_ids(self, raw: Optional[str], expected: Optional[str]) -> None:
        assert resolve_ref_msg_id(raw) == expected


class TestPlatformSegmentSequence:
    """文字与图片必须落进同一个发送请求，并保持模型给出的顺序"""

    async def _preprocess(self, segments: List[AgentMessageSegment], monkeypatch, tmp_path: Path):
        monkeypatch.setattr(
            "nekro_agent.services.chat.universal_chat_service.convert_to_host_path",
            lambda sandbox_path, chat_key, container_key=None: tmp_path / Path(sandbox_path).name,
        )
        return await universal_chat_service._preprocess_messages(  # noqa: SLF001
            segments,
            "onebot_v11-inst1-group_123456",
            None,
            file_mode=True,
        )

    async def test_text_and_image_stay_in_one_ordered_sequence(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "heart.png").write_bytes(_PNG_BYTES)

        processed = await self._preprocess(
            [
                AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content="画好啦～"),
                AgentMessageSegment(type=AgentMessageSegmentType.IMAGE, content="./shared/heart.png"),
            ],
            monkeypatch,
            tmp_path,
        )

        assert [seg.type for seg in processed] == [PlatformSendSegmentType.TEXT, PlatformSendSegmentType.IMAGE]
        assert processed[0].content == "画好啦～"
        assert processed[1].file_path == str(tmp_path / "heart.png")

    async def test_image_before_text_preserves_model_order(self, monkeypatch, tmp_path: Path) -> None:
        (tmp_path / "heart.png").write_bytes(_PNG_BYTES)

        processed = await self._preprocess(
            [
                AgentMessageSegment(type=AgentMessageSegmentType.IMAGE, content="./shared/heart.png"),
                AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content="你看看喜欢吗"),
            ],
            monkeypatch,
            tmp_path,
        )

        assert [seg.type for seg in processed] == [PlatformSendSegmentType.IMAGE, PlatformSendSegmentType.TEXT]

    async def test_file_segment_keeps_file_type_in_file_mode(self, monkeypatch, tmp_path: Path) -> None:
        """FILE 段必须保持 FILE 类型，适配器才会走文件上传而不是当图片发"""
        (tmp_path / "report.txt").write_text("hello", encoding="utf-8")

        processed = await self._preprocess(
            [
                AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content="报告给你"),
                AgentMessageSegment(type=AgentMessageSegmentType.FILE, content="./shared/report.txt"),
            ],
            monkeypatch,
            tmp_path,
        )

        assert [seg.type for seg in processed] == [PlatformSendSegmentType.TEXT, PlatformSendSegmentType.FILE]


class TestOneBotForwardMessage:
    """适配器层的兜底：非法引用 ID 不能让整条消息发不出去"""

    @pytest.fixture
    def adapter(self):
        from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

        return OnebotV11Adapter()

    @pytest.fixture
    def sent_messages(self, monkeypatch, adapter):
        from nekro_agent.models.db_chat_channel import DBChatChannel

        captured: List = []

        async def fake_get_channel(*args, **kwargs):
            return DBChatChannel(chat_key="onebot_v11-inst1-group_123456", channel_id="group_123456")

        async def fake_parse_at(text, db_chat_channel):
            return [text]

        async def fake_send_to_chat(chat_key, message):
            captured.append(message)
            return "999"

        monkeypatch.setattr(DBChatChannel, "get_channel", fake_get_channel)
        monkeypatch.setattr("nekro_agent.adapters.onebot_v11.adapter.parse_at_from_text", fake_parse_at)
        monkeypatch.setattr(adapter, "_send_to_chat", fake_send_to_chat)
        return captured

    async def test_non_numeric_ref_msg_id_does_not_drop_the_message(self, adapter, sent_messages, tmp_path: Path) -> None:
        """回归：这里以前抛 ValueError，图片和文字一起消失，用户什么也收不到"""
        from nekro_agent.adapters.interface.schemas.platform import PlatformSendRequest

        image_path = tmp_path / "heart.png"
        image_path.write_bytes(_PNG_BYTES)

        response = await adapter.forward_message(
            PlatformSendRequest(
                chat_key="onebot_v11-inst1-group_123456",
                segments=[
                    PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="画好啦～"),
                    PlatformSendSegment(type=PlatformSendSegmentType.IMAGE, file_path=str(image_path)),
                ],
                ref_msg_id="这是给你的图",
            ),
        )

        assert response.success
        assert len(sent_messages) == 1
        assert [seg.type for seg in sent_messages[0]] == ["text", "image"]

    async def test_numeric_ref_msg_id_still_produces_a_reply_segment(self, adapter, sent_messages) -> None:
        from nekro_agent.adapters.interface.schemas.platform import PlatformSendRequest

        await adapter.forward_message(
            PlatformSendRequest(
                chat_key="onebot_v11-inst1-group_123456",
                segments=[PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="收到")],
                ref_msg_id="123456789",
            ),
        )

        assert [seg.type for seg in sent_messages[0]] == ["reply", "text"]


class TestResolveMixedSegments:
    """模型给出的 segments 列表解析"""

    @pytest.fixture
    def ctx(self):
        class _Ctx:
            chat_key = "onebot_v11-inst1-group_123456"
            container_key = "container_test"
            adapter_key = "onebot_v11"

        return _Ctx()

    @pytest.fixture
    def resolve(self, monkeypatch, tmp_path: Path):
        import plugins.builtin.basic as basic

        monkeypatch.setattr(
            basic,
            "convert_to_host_path",
            lambda sandbox_path, chat_key, container_key=None: tmp_path / Path(sandbox_path).name,
        )
        return basic.resolve_mixed_segments

    async def test_declared_image_that_is_not_an_image_becomes_a_file_segment(self, resolve, ctx, tmp_path: Path) -> None:
        """按 MIME 判定：模型把 PDF 标成 image 时不能真的当图片发"""
        (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

        segments, _ = await resolve(ctx, ctx.chat_key, [{"type": "image", "content": "./shared/report.pdf"}])

        assert [seg.type for seg in segments] == [AgentMessageSegmentType.FILE]

    async def test_declared_file_that_is_an_image_becomes_an_image_segment(self, resolve, ctx, tmp_path: Path) -> None:
        (tmp_path / "heart.png").write_bytes(_PNG_BYTES)

        segments, _ = await resolve(ctx, ctx.chat_key, [{"type": "file", "content": "./shared/heart.png"}])

        assert [seg.type for seg in segments] == [AgentMessageSegmentType.IMAGE]

    async def test_keeps_order_and_accepts_plain_string_as_text(self, resolve, ctx, tmp_path: Path) -> None:
        (tmp_path / "heart.png").write_bytes(_PNG_BYTES)

        segments, _ = await resolve(
            ctx,
            ctx.chat_key,
            ["先看这个", {"type": "image", "content": "./shared/heart.png"}, {"type": "text", "content": "喜欢吗"}],
        )

        assert [seg.type for seg in segments] == [
            AgentMessageSegmentType.TEXT,
            AgentMessageSegmentType.IMAGE,
            AgentMessageSegmentType.TEXT,
        ]
        assert [seg.content for seg in segments][0] == "先看这个"

    async def test_identical_content_yields_identical_fingerprint(self, resolve, ctx, tmp_path: Path) -> None:
        """整条混合消息作为一个整体查重，避免只丢掉文字或图片而发出半条消息"""
        (tmp_path / "heart.png").write_bytes(_PNG_BYTES)
        payload = [{"type": "text", "content": "画好啦"}, {"type": "image", "content": "./shared/heart.png"}]

        _, first = await resolve(ctx, ctx.chat_key, payload)
        _, second = await resolve(ctx, ctx.chat_key, payload)
        _, other = await resolve(ctx, ctx.chat_key, [{"type": "text", "content": "换个说法"}])

        assert first == second
        assert first != other

    @pytest.mark.parametrize(
        ("payload", "expected_error"),
        [
            ([], TypeError),
            ("画好啦", TypeError),
            ([{"type": "text", "content": ""}], ValueError),
            ([{"type": "voice", "content": "./shared/a.wav"}], ValueError),
            ([{"type": "image", "content": "./shared/missing.png"}], FileNotFoundError),
        ],
    )
    async def test_rejects_malformed_payloads(self, resolve, ctx, payload, expected_error) -> None:
        with pytest.raises(expected_error):
            await resolve(ctx, ctx.chat_key, payload)
