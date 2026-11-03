"""未认领实例的闸门挡在 OneBot 各个入口的最前面：不建频道行、不落库、不向协议端发 API

真实事件走真实匹配器（测试替身沿用 `test_onebot_video_visibility.py` 那一套），判据与 collector 的用例见
`test_unclaimed_dispatch.py`。
"""

import json
import time
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional

import pytest
from nonebot.adapters.onebot.v11 import Adapter, GroupUploadNoticeEvent

from nekro_agent.adapters.onebot_v11.core import access_token as gate
from nekro_agent.adapters.onebot_v11.core.access_token import HandshakeRecord, TokenSource
from nekro_agent.adapters.onebot_v11.matchers import notice as notice_module
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.services import unclaimed_dispatch
from nekro_agent.services.message_service import message_service
from nekro_agent.services.user import token_exposure
from nekro_agent.services.user.token_exposure import token_fingerprint
from tests.test_onebot_video_visibility import (  # noqa: F401 - fixtures are used by name
    BOT_ID,
    GROUP_ID,
    USER_ID,
    Pipeline,
    napcat_event,
    pipeline,
    self_event,
    text_segment,
)
from tests.test_video_analysis_plugin import (  # noqa: F401 - fixtures are used by name
    ADAPTER,
    CHAT_KEY,
    INSTANCE,
    db,
    harness,
    upstream,
)

DEPLOYMENT_TOKEN = "deployment-token"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from nekro_agent.core.os_env import OsEnv

    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    token_exposure._write([], seeded=True)  # noqa: SLF001
    monkeypatch.setattr(token_exposure, "_rebuild_pending", False)
    monkeypatch.setattr(token_exposure, "_rebuild_lock", None)
    monkeypatch.setattr(token_exposure, "_unwritten_fingerprints", set())
    monkeypatch.setattr(unclaimed_dispatch, "_last_warned", {})


def set_deployment_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    if token:
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", token)
    else:
        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)


@contextmanager
def connection(token: Optional[str]) -> Iterator[None]:
    """`token` 为 None：没有握手记录；否则是凭这枚令牌握手的连接"""
    record = (
        None
        if token is None
        else HandshakeRecord(
            self_id=BOT_ID, source=TokenSource.DEPLOYMENT, instance_key="", token_fingerprint=token_fingerprint(token),
        )
    )
    reset = gate._current_handshake.set(record)  # noqa: SLF001
    try:
        yield
    finally:
        gate._current_handshake.reset(reset)  # noqa: SLF001


async def make_unclaimed() -> None:
    """测试替身把账号固定解析到 `INSTANCE`：把它改成刚自动登记的样子，频道行也还没有"""
    await DBAdapterInstance.filter(instance_key=INSTANCE).update(status="unclaimed", tenant_id=None)
    await DBChatChannel.filter(chat_key=CHAT_KEY).delete()


async def nothing_recorded(pipeline: Pipeline) -> bool:  # noqa: F811
    return (
        await DBChatChannel.filter(chat_key=CHAT_KEY).count() == 0
        and await DBChatMessage.all().count() == 0
        and pipeline.protocol.calls == []
    )


class TestMessageMatchers:
    async def test_anonymous_group_message_leaves_no_trace(self, db, pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
        set_deployment_token(monkeypatch, "")
        await make_unclaimed()

        with connection(""):
            await pipeline.feed(napcat_event([text_segment("白用一下")], message_id=801))

        assert await nothing_recorded(pipeline)

    async def test_control_on_the_deployment_token_it_is_recorded(self, db, pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        await make_unclaimed()
        # 夹具里的租户实例没有自己的令牌，判定时会先把部署级令牌记成曝光（那正是规则）；对照里给它们配上
        await DBAdapterInstance.filter(tenant_id__not_isnull=True).update(metadata_json=json.dumps({"access_token": "own"}))

        with connection(DEPLOYMENT_TOKEN):
            await pipeline.feed(napcat_event([text_segment("正常消息")], message_id=802))

        assert await DBChatMessage.filter(chat_key=CHAT_KEY).count() == 1

    async def test_no_handshake_leaves_no_trace(self, db, pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
        set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
        await make_unclaimed()

        with connection(None):
            await pipeline.feed(napcat_event([text_segment("没握手")], message_id=803))

        assert await nothing_recorded(pipeline)

    async def test_self_sent_message_is_not_recorded(self, db, pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
        set_deployment_token(monkeypatch, "")
        await make_unclaimed()

        with connection(""):
            await pipeline.feed(self_event([text_segment("自己发的")], message_id=804))

        assert await nothing_recorded(pipeline)

    async def test_group_upload_notice_is_dropped(self, db, pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
        set_deployment_token(monkeypatch, "")
        await make_unclaimed()
        event = GroupUploadNoticeEvent(
            time=int(time.time()), self_id=int(BOT_ID), post_type="notice", notice_type="group_upload", user_id=int(USER_ID),
            group_id=GROUP_ID, file={"id": "a", "name": "a.zip", "size": 1, "busid": 102},
        )  # fmt: skip

        with connection(""):
            await pipeline.notice(None, event, pipeline.bot())  # type: ignore[attr-defined]

        assert await nothing_recorded(pipeline)


class TestNoticeMatcher:
    """`matchers/notice.py` 的通用通知匹配器直接调 `push_system_message` / `push_human_message`，不经过 collector"""

    @pytest.fixture
    def pushed(self, monkeypatch: pytest.MonkeyPatch) -> List[str]:
        """记下闸门之后的第一步（解析会话）与两个落库口有没有被走到

        解析会话那一步记下后就按「不指向已知会话」收尾：后面的处理器要真实适配器，与闸门无关。
        """
        calls: List[str] = []

        async def record_system(*_args: Any, **_kwargs: Any) -> None:
            calls.append("system")

        async def record_human(*_args: Any, **_kwargs: Any) -> None:
            calls.append("human")

        async def reached_chat_info(*_args: Any, **_kwargs: Any) -> Any:
            calls.append("chat_info")
            raise ValueError("用例到此为止")

        async def resolve(_self_id: str) -> str:
            return INSTANCE

        monkeypatch.setattr(message_service, "push_system_message", record_system)
        monkeypatch.setattr(message_service, "push_human_message", record_human)
        monkeypatch.setattr(notice_module, "get_chat_info_old", reached_chat_info)
        monkeypatch.setattr(notice_module, "resolve_instance_key", resolve)
        monkeypatch.setattr(notice_module, "set_current_instance", lambda *_a, **_k: None)
        return calls

    @staticmethod
    async def feed_poke(pipeline: Pipeline) -> None:  # noqa: F811
        raw = {
            "time": int(time.time()), "self_id": int(BOT_ID), "post_type": "notice", "notice_type": "notify",
            "sub_type": "poke", "group_id": GROUP_ID, "user_id": int(USER_ID), "target_id": int(BOT_ID),
        }  # fmt: skip
        event = Adapter.json_to_event(json.loads(json.dumps(raw)))
        assert event is not None
        handler = notice_module.notice_matcher.handlers[0].call
        await handler(None, event, pipeline.bot())

    async def test_anonymous_poke_is_dropped(self, db, pipeline: Pipeline, pushed: List[str], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
        set_deployment_token(monkeypatch, "")
        await make_unclaimed()

        with connection(""):
            await self.feed_poke(pipeline)

        assert pushed == []

    async def test_control_poke_on_a_claimed_instance_gets_past_the_gate(
        self, db, pipeline: Pipeline, pushed: List[str], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    ) -> None:
        set_deployment_token(monkeypatch, "")
        await DBAdapterInstance.filter(instance_key=INSTANCE).update(tenant_id=None)

        with connection(""):
            await self.feed_poke(pipeline)

        assert pushed == ["chat_info"]

    async def test_it_asks_the_gate_with_the_resolved_instance(
        self, db, pipeline: Pipeline, pushed: List[str], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    ) -> None:
        asked: List[tuple] = []

        async def refuse(adapter_key: str, instance_key: str) -> bool:
            asked.append((adapter_key, instance_key))
            return False

        monkeypatch.setattr(notice_module, "is_inbound_dispatch_allowed", refuse)
        await self.feed_poke(pipeline)

        assert asked == [(ADAPTER, INSTANCE)]
        assert pushed == []
