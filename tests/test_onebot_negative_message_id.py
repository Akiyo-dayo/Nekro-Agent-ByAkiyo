"""OneBot 的负数消息编号：WebUI 预览、视频分析、引用视频与前端占位都要认

NapCat 与 SnowLuma 的编号是有符号 int32 哈希，约一半是负数。这里分别钉住解析函数、
两条视频接口、插件的 `message:` 来源、引用视频的占位，以及前端占位正则。

后四项复用既有用例的替身与 fixture：预览接口用真 HTTP 上游，插件与引用视频走真实
`find_video_record` / 匹配器，断言落在协议端实际收到的 `message_id` 类型与值上。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from nekro_agent.adapters.onebot_v11.core.message_id import parse_message_id
from nekro_agent.adapters.onebot_v11.tools.video import build_video_segment
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessageSegmentVideo
from plugins.builtin.video_analysis import source
from tests.test_message_video_preview import (  # noqa: F401 - fixtures are used by name
    BODY,
    CHAT_KEY,
    FakeBot,
    Web,
    media,
    seed,
    web,
)
from tests.test_onebot_video_visibility import (  # noqa: F401 - fixtures are used by name
    BOT_ID,
    GROUP_ID,
    USER_ID,
    at_segment,
    napcat_event,
    pipeline,
    prompt_of,
    reply_segment,
    rows_of,
    text_segment,
    video_segment,
)
from tests.test_video_analysis_plugin import _ctx, db, harness  # noqa: F401 - fixtures are used by name

REPO_ROOT = Path(__file__).parent.parent
FRONTEND = REPO_ROOT / "frontend" / "src"
PLAYER = FRONTEND / "pages" / "chat-channel" / "components" / "detail-tabs" / "MessageVideoPlayer.tsx"

NEGATIVE_MESSAGE_ID = -12345678
NEGATIVE_MESSAGE_ID_TEXT = str(NEGATIVE_MESSAGE_ID)
VIDEO_NAME = "0a1b2c3d4e5f.mp4"
VIDEO_URL = "https://video.example.com/clip.mp4"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123", 123),
        ("-12345678", -12345678),
        # -1 是合法编号，不是「没有编号」的哨兵值；int32 的两端都要收
        ("-1", -1),
        (-1, -1),
        (str(-(2**31)), -(2**31)),
        (-(2**31), -(2**31)),
        (str(2**31 - 1), 2**31 - 1),
        ("0", 0),
        (-5, -5),
        (str(-(2**63)), -(2**63)),
        (True, None),
        (False, None),
        ("-0", None),
        ("007", None),
        ("+5", None),
        (" 5", None),
        ("5 ", None),
        ("5\n", None),
        ("１２", None),
        ("²", None),
        ("--1", None),
        ("-", None),
        ("", None),
        (str(2**63), None),
        (2**63, None),
        (None, None),
        (1.5, None),
        (b"12", None),
    ],
)
def test_parse_message_id(raw: object, expected: Any) -> None:
    assert parse_message_id(raw) == expected


class RecordingBot(FakeBot):
    """在既有替身上多记一份调用参数：要看协议端收到的是 int 而不是字符串"""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.api_calls: List[Tuple[str, Dict[str, Any]]] = []

    async def call_api(self, api: str, **data: Any) -> Any:
        self.api_calls.append((api, data))
        return await super().call_api(api, **data)


async def test_video_preview_and_stream_accept_a_negative_message_id(web: Web, media: Any) -> None:  # noqa: F811
    await seed(message_id=NEGATIVE_MESSAGE_ID)
    bot = RecordingBot(f"{media.base_url}/ranged.mp4")
    web.bot(bot)

    response = await web.access(message_id=NEGATIVE_MESSAGE_ID_TEXT)
    access = response.json()
    assert access["status"] == "ok", access
    assert bot.api_calls[0] == ("get_msg", {"message_id": NEGATIVE_MESSAGE_ID})
    assert isinstance(bot.api_calls[0][1]["message_id"], int)

    stream = await web.stream(access["url"])
    assert stream.status_code == 200
    assert stream.content == BODY


async def test_video_analysis_accepts_a_negative_message_id(db: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    segment = build_video_segment(NEGATIVE_MESSAGE_ID_TEXT, {"file": VIDEO_NAME, "file_size": "1024"})
    await DBChatMessage.create(
        message_id=NEGATIVE_MESSAGE_ID_TEXT, sender_id=USER_ID, sender_name="t", sender_nickname="t", adapter_key="onebot_v11",
        instance_key="ob_acme", platform_userid=USER_ID, is_tome=0, is_recalled=False, chat_key=CHAT_KEY, chat_type="group",
        content_text="x", content_data=json.dumps([segment.model_dump()]), raw_cq_code="", ext_data="{}", send_timestamp=int(time.time()),
    )  # fmt: skip
    calls: List[Tuple[str, Dict[str, Any]]] = []

    class _Bot:
        async def call_api(self, api: str, **data: Any) -> Any:
            calls.append((api, data))
            return {
                "message_type": "group",
                "group_id": GROUP_ID,
                "user_id": int(USER_ID),
                "message": [{"type": "video", "data": {"file": VIDEO_NAME, "file_size": "1024", "url": VIDEO_URL}}],
            }

    async def get_bot(_self: AgentCtx) -> Any:
        return _Bot()

    monkeypatch.setattr(AgentCtx, "get_onebot_v11_bot", get_bot)

    assert await source.resolve_message_video(await _ctx(), f"message:{NEGATIVE_MESSAGE_ID_TEXT}") == VIDEO_URL
    assert calls == [("get_msg", {"message_id": NEGATIVE_MESSAGE_ID})]


async def test_a_reply_to_a_negative_id_video_gets_a_quoted_placeholder(db: None, pipeline: Any) -> None:  # noqa: F811
    await pipeline.feed(napcat_event([video_segment()], message_id=NEGATIVE_MESSAGE_ID))
    pipeline.protocol.replies["get_msg"] = {
        "time": int(time.time()),
        "message_type": "group",
        "group_id": GROUP_ID,
        "message_id": NEGATIVE_MESSAGE_ID,
        "real_id": NEGATIVE_MESSAGE_ID,
        "user_id": int(USER_ID),
        "sender": {"user_id": int(USER_ID), "nickname": "tester"},
        "message": [video_segment()],
        "raw_message": "",
        "font": 14,
    }

    await pipeline.feed(
        napcat_event([reply_segment(NEGATIVE_MESSAGE_ID), at_segment(BOT_ID), text_segment(" 这个视频说了啥")], message_id=778),
    )

    row = (await rows_of())[1]
    quoted = row.parse_content_data()[-1]
    assert isinstance(quoted, ChatMessageSegmentVideo) and quoted.quoted
    assert f"[Video: message:{NEGATIVE_MESSAGE_ID_TEXT}]" in prompt_of(row)


def test_frontend_placeholder_accepts_a_negative_message_id() -> None:
    text = PLAYER.read_text(encoding="utf-8")
    found = re.search(r"VIDEO_PLACEHOLDER\s*=\s*/(.+)/", text)
    assert found is not None, "找不到 VIDEO_PLACEHOLDER 的字面量"
    pattern = re.compile(found.group(1))

    negative = pattern.fullmatch("[Video: message:-12345678]")
    assert negative is not None and negative.group(1) == "-12345678"
    positive = pattern.fullmatch("[Video: message:123]")
    assert positive is not None and positive.group(1) == "123"
    assert pattern.fullmatch("[Video: message:--1]") is None
    assert pattern.fullmatch("[Video: message:]") is None
