"""群名片 / 昵称 / 正文里的 NUL 不能把整条消息写丢

PG 的 text / varchar / jsonb 都不收 NUL，asyncpg 会抛
`CharacterNotInRepertoireError: invalid byte sequence for encoding "UTF8": 0x00`。
`push_human_message` 的 `DBChatMessage.create` 一旦撞上它，整条消息就从会话里消失了，
而 SQLite 收 NUL，默认跑 SQLite 的测试完全发现不了。

这里钉死四层，每层的用例都写清了"撤掉哪一行修复会红"：

1. `strip_nul` 纯函数的表驱动：只去 NUL，`\\t` / `\\n` / `\\x01` / emoji / `ǿĀ` 逐字不动；
2. `pre_save` 保险层：直接 `create` 也写不进 NUL（撤掉模型文件末尾的注册行就红）；
3. 入口清洗：`push_human_message` 落库与广播看到的是同一份干净数据；
4. 绕过口子：`register_platform_user` 的保留名检查、`DBChatChannel.get_or_create` 的改名比较，
   都必须用清洗后的值——否则 NUL 会在保险层被剔掉之后变出一个非预期的结果。

`tests/test_nul_sanitize_pg.py` 在同一组契约上跑真 PG（含"PG 确实拒绝 NUL"的反向对照）。
"""

import datetime
import json
from typing import Any, List

import pytest
from tortoise import Tortoise
from tortoise.models import Model
from tortoise.signals import Signals

from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.nul_guard import strip_nul_before_save
from nekro_agent.schemas.chat_message import ChatMessage, ChatMessageSegmentAt, ChatMessageSegmentType, ChatType
from nekro_agent.services import message_broadcaster as message_broadcaster_module
from nekro_agent.services.message_service import message_service
from nekro_agent.services.user.util import register_platform_user
from nekro_agent.tools.text_sanitize import strip_nul

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
CHANNEL_ID = "group_123456"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-{CHANNEL_ID}"

# 除 NUL 外什么都别动：制表 / 换行 / 控制字符 / emoji / 看起来像乱码的字符都要逐字保留
MIXED_TEXT = "张\x00三\tǿĀ\n😀\x01"

# 广播器挂在模块属性上，monkeypatch 打的是实例方法
message_service_broadcaster = message_broadcaster_module.message_broadcaster


async def _noop_async(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def assert_no_nul(value: Any) -> None:
    """递归断言结构里没有任何 NUL（字符串、键、嵌套容器都查）"""
    if isinstance(value, str):
        assert "\x00" not in value
    elif isinstance(value, dict):
        for key, item in value.items():
            assert_no_nul(key)
            assert_no_nul(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_no_nul(item)


class TestStripNulTable:
    """纯函数的表：只有 NUL 被去掉，其余逐字不变"""

    def test_plain_string(self) -> None:
        assert strip_nul("a\x00b") == "ab"

    def test_mixed_text_only_loses_nul(self) -> None:
        """撤掉 `replace("\\x00", "")` 之外任何一处"顺手清洗"都会在这里红"""
        assert strip_nul(MIXED_TEXT) == "张三\tǿĀ\n😀\x01"

    def test_nested_dict_keys_and_values(self) -> None:
        original = {"k\x00ey": {"inner\x00": ["v\x00al", 1]}, "n": None}
        cleaned = strip_nul(original)
        assert cleaned == {"key": {"inner": ["val", 1]}, "n": None}
        # 返回新容器：原对象必须原样，否则同一份数据在其他路径上被悄悄改掉
        assert original == {"k\x00ey": {"inner\x00": ["v\x00al", 1]}, "n": None}
        assert strip_nul(original) is not original
        assert strip_nul(original["k\x00ey"]) is not original["k\x00ey"]

    def test_list_of_dict_of_list(self) -> None:
        original: List[Any] = [{"text": "a\x00b", "at": ["x\x00", {"y": "\x00z"}]}]
        assert strip_nul(original) == [{"text": "ab", "at": ["x", {"y": "z"}]}]

    def test_tuple_keeps_tuple_type(self) -> None:
        cleaned = strip_nul(("a\x00", {"b\x00": "c\x00"}))
        assert cleaned == ("a", {"b": "c"})
        assert isinstance(cleaned, tuple)

    def test_non_strings_are_untouched(self) -> None:
        """int / None / bytes 原样返回：NUL 在字节串里是合法数据，不是文本列的内容"""
        assert strip_nul(1) == 1
        assert strip_nul(None) is None
        raw = b"a\x00b"
        assert strip_nul(raw) is raw

    def test_empty_string_stays(self) -> None:
        assert strip_nul("") == ""


class TestPreSaveGuard:
    """保险层：即使调用方漏了清洗，落库前也会剔掉（SQLite 收 NUL，所以能证明是这一层在起作用）"""

    async def test_chat_message_is_cleaned(self, db) -> None:  # noqa: ARG002
        """撤掉 `db_chat_message.py` 末尾的 `pre_save(DBChatMessage)(strip_nul_before_save)` 就红"""
        created = await DBChatMessage.create(
            message_id="m\x001",
            sender_id="10001",
            sender_name="张\x00三",
            sender_nickname="\x00群名片\x00",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            platform_userid="10001",
            is_tome=0,
            is_recalled=False,
            chat_key=CHAT_KEY,
            chat_type=ChatType.GROUP.value,
            content_text="a\x00b\tǿĀ",
            content_data=json.dumps([{"type": "text", "text": "a\x00b"}]),
            raw_cq_code="[CQ:at,qq=4\x002]",
            ext_data=json.dumps({"k": "v\x00"}),
            send_timestamp=0,
        )

        stored = await DBChatMessage.get(id=created.id)

        assert stored.message_id == "m1"
        assert stored.sender_name == "张三"
        assert stored.sender_nickname == "群名片"
        # NUL 以外逐字不变
        assert stored.content_text == "ab\tǿĀ"
        assert stored.raw_cq_code == "[CQ:at,qq=42]"
        assert_no_nul(stored.content_data)
        assert_no_nul(stored.ext_data)
        # jsonb 之外的列同样不能留 NUL
        assert stored.chat_key == CHAT_KEY
        assert stored.adapter_key == ADAPTER

    async def test_user_jsonfield_is_cleaned(self, db) -> None:  # noqa: ARG002
        """`ext_data` 是 JSONField（PG 上是 jsonb）；撤掉 `db_user.py` 的注册行就红"""
        created = await DBUser.create(
            username="a\x00b",
            password="",
            adapter_key=ADAPTER,
            platform_userid="10001",
            perm_level=1,
            login_time=datetime.datetime.now(),
            ext_data={"k\x00": "v\x00", "nested": [{"x": "y\x00"}]},
        )

        stored = await DBUser.get(id=created.id)

        assert stored.username == "ab"
        assert stored.ext_data == {"k": "v", "nested": [{"x": "y"}]}

    async def test_chat_channel_is_cleaned(self, db) -> None:  # noqa: ARG002
        """撤掉 `db_chat_channel.py` 的注册行就红"""
        created = await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id=CHANNEL_ID,
            channel_type=ChatType.GROUP.value,
            channel_name="群\x00名",
            chat_key=CHAT_KEY,
            is_active=True,
            observe_mode=False,
            data=json.dumps({"x": "y\x00"}),
        )

        stored = await DBChatChannel.get(id=created.id)

        assert stored.channel_name == "群名"
        assert_no_nul(stored.data)

    async def test_save_path_is_cleaned_too(self, db) -> None:  # noqa: ARG002
        """不只 create：`save()` 也要走监听器（后台改名的路径就是它）"""
        channel = await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id=CHANNEL_ID,
            channel_type=ChatType.GROUP.value,
            channel_name="旧名",
            chat_key=CHAT_KEY,
            is_active=True,
            observe_mode=False,
            data=json.dumps({}),
        )
        channel.channel_name = "新\x00名"
        await channel.save()

        stored = await DBChatChannel.get(id=channel.id)
        assert stored.channel_name == "新名"


class TestListenerRegistration:
    """注册行本身也钉住：删掉一行注册，上面的保险层用例才会红，这里让它更直接地红"""

    @pytest.mark.parametrize("model", [DBChatMessage, DBUser, DBChatChannel])
    def test_model_has_guard(self, model: type[Model]) -> None:
        assert strip_nul_before_save in Model._listeners[Signals.pre_save].get(model, [])  # noqa: SLF001


class TestPushHumanMessageEndToEnd:
    """入口清洗：落库那行、广播那份、（若有）触发判断看到的都是无 NUL 的同一份"""

    async def test_message_with_nul_lands_clean(self, db, monkeypatch) -> None:  # noqa: ARG002
        """撤掉 `push_human_message` 开头的 `message = _without_nul(message)` 就红"""
        from nekro_agent.models.db_adapter_instance import DBAdapterInstance

        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            enabled=True,
            provider="remote_onebot",
            provider_account_id="1000000012",
        )
        channel = await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id=CHANNEL_ID,
            channel_type=ChatType.GROUP.value,
            channel_name="频道",
            chat_key=CHAT_KEY,
            is_active=True,
            observe_mode=False,
            data=json.dumps({}),
        )
        message = ChatMessage(
            message_id="m1",
            sender_id="10001",
            sender_name="张\x00三",
            sender_nickname="群\x00名片",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            platform_userid="10001",
            is_tome=0,
            is_recalled=False,
            chat_key=CHAT_KEY,
            chat_type=ChatType.GROUP,
            content_text="你好\x00呀",
            content_data=[
                {"type": ChatMessageSegmentType.TEXT.value, "text": "你好\x00呀"},  # type: ignore[list-item]
            ],
            raw_cq_code="",
            ext_data={"ref": "r\x001"},
            send_timestamp=0,
        )
        # 再造一个 At 段：At 段的昵称同样是协议端填的，一样会带 NUL
        message.content_data.append(
            ChatMessageSegmentAt(
                type=ChatMessageSegmentType.AT,
                text="@某人",
                target_platform_userid="42",
                target_nickname="某\x00人",
            ),
        )
        published: List[ChatMessage] = []

        async def fake_publish(_chat_key: str, payload: ChatMessage) -> None:
            published.append(payload)

        # 广播是旁路：这里只借它断言"广播出去的那份与库里的一致"，别让用例真去碰订阅者
        monkeypatch.setattr(message_service_broadcaster, "publish", fake_publish)
        # 触发判断之后会进 Agent 调度，与本条契约无关
        monkeypatch.setattr(type(message_service), "_debounce_task", _noop_async)

        await message_service.push_human_message(message=message, db_chat_channel=channel)

        stored = await DBChatMessage.get(chat_key=CHAT_KEY)
        assert stored.sender_name == "张三"
        assert stored.sender_nickname == "群名片"
        assert stored.content_text == "你好呀"
        assert_no_nul(stored.content_data)
        assert_no_nul(stored.ext_data)
        # 原始 JSON 串里不能留字面的 \u0000（json.dumps 会把真 NUL 转义成它）
        assert "\\u0000" not in stored.content_data
        assert "\\u0000" not in stored.ext_data
        # NUL 以外的内容逐字不变
        parsed = json.loads(stored.content_data)
        assert parsed[0]["text"] == "你好呀"
        assert parsed[1]["target_nickname"] == "某人"
        # 广播出去的那份与库里一致
        assert published and published[0].sender_name == "张三"
        assert published[0].sender_nickname == "群名片"
        assert published[0].content_text == "你好呀"
        assert_no_nul([segment.model_dump() for segment in published[0].content_data])

    async def test_recorded_command_input_lands_clean(self, db, monkeypatch) -> None:  # noqa: ARG002
        """命令原文入库（`record_human_message`，网页适配器的命令回显）走同一道清洗"""
        from nekro_agent.models.db_adapter_instance import DBAdapterInstance

        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            enabled=True,
            provider="remote_onebot",
            provider_account_id="1000000012",
        )
        channel = await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id=CHANNEL_ID,
            channel_type=ChatType.GROUP.value,
            channel_name="频道",
            chat_key=CHAT_KEY,
            is_active=True,
            observe_mode=False,
            data=json.dumps({}),
        )
        message = ChatMessage(
            message_id="m2",
            sender_id="10001",
            sender_name="张\x00三",
            sender_nickname="群\x00名片",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            platform_userid="10001",
            is_tome=1,
            is_recalled=False,
            chat_key=CHAT_KEY,
            chat_type=ChatType.GROUP,
            content_text="/na_info\x00",
            content_data=[
                {"type": ChatMessageSegmentType.TEXT.value, "text": "/na_info\x00"},  # type: ignore[list-item]
            ],
            raw_cq_code="",
            ext_data={},
            send_timestamp=0,
        )
        published: List[ChatMessage] = []

        async def fake_publish(_chat_key: str, payload: ChatMessage) -> None:
            published.append(payload)

        monkeypatch.setattr(message_service_broadcaster, "publish", fake_publish)

        await message_service.record_human_message(message, db_chat_channel=channel)

        stored = await DBChatMessage.get(chat_key=CHAT_KEY)
        assert stored.sender_name == "张三"
        assert stored.content_text == "/na_info"
        assert "\\u0000" not in stored.content_data
        assert published and published[0].content_text == "/na_info"
        assert published[0].sender_nickname == "群名片"

    def test_without_nul_returns_same_object(self) -> None:
        """没有 NUL 的消息不该被白白复制：下游按对象身份做的记录会错位"""
        from nekro_agent.services.message_service import _without_nul

        message = ChatMessage(
            message_id="m1",
            sender_id="1",
            sender_name="张三",
            sender_nickname="张三",
            adapter_key=ADAPTER,
            platform_userid="1",
            chat_key=CHAT_KEY,
            chat_type=ChatType.GROUP,
            content_text="hi",
            content_data=[],
            raw_cq_code="",
            ext_data={},
            send_timestamp=0,
        )

        assert _without_nul(message) is message


class TestRegisterPlatformUser:
    """保留名检查必须用清洗后的值比：否则 NUL 一被剔掉就建出一个叫 admin 的平台用户"""

    async def test_nul_hidden_admin_is_rejected(self, db) -> None:  # noqa: ARG002
        """撤掉 `username = strip_nul(username)` 就红（会建出 admin 行）"""
        user, created = await register_platform_user(ADAPTER, "20001", "adm\x00in")

        assert (user, created) == (None, False)
        assert await DBUser.filter(username="admin").count() == 0
        assert await DBUser.all().count() == 0

    async def test_plain_admin_is_still_rejected(self, db) -> None:  # noqa: ARG002
        assert await register_platform_user(ADAPTER, "20002", "admin") == (None, False)

    async def test_nul_is_stripped_from_ordinary_name(self, db) -> None:  # noqa: ARG002
        user, created = await register_platform_user(ADAPTER, "20003", "张\x00三")

        assert created is True
        assert user is not None
        assert user.username == "张三"


class TestGetOrCreateChannelName:
    """`get_or_create` 里的比较与写入都要用清洗后的名字"""

    async def test_new_channel_name_is_clean(self, db) -> None:  # noqa: ARG002
        channel = await DBChatChannel.get_or_create(
            adapter_key=ADAPTER,
            channel_id=CHANNEL_ID,
            channel_type=ChatType.GROUP,
            channel_name="群\x00名",
            instance_key=INSTANCE,
        )

        stored = await DBChatChannel.get(id=channel.id)
        assert stored.channel_name == "群名"

    async def test_existing_channel_rename_is_clean(self, db) -> None:  # noqa: ARG002
        """已有行改名：比较用清洗后的值，才不会每次同步都判成"变了"再写一遍"""
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id=CHANNEL_ID,
            channel_type=ChatType.GROUP.value,
            channel_name="旧名",
            chat_key=CHAT_KEY,
            is_active=True,
            observe_mode=False,
            data=json.dumps({}),
        )

        channel = await DBChatChannel.get_or_create(
            adapter_key=ADAPTER,
            channel_id=CHANNEL_ID,
            channel_type=ChatType.GROUP,
            channel_name="群里\x00名",
            instance_key=INSTANCE,
        )

        stored = await DBChatChannel.get(id=channel.id)
        assert stored.channel_name == "群里名"

    async def test_same_clean_name_is_not_rewritten(self, db, monkeypatch) -> None:  # noqa: ARG002
        """名字前后只差一个 NUL 时不算改名，不该再 `save()`

        `pre_save` 会把写进去的名字剔干净，所以光看库里的值分不出「写了」还是「没写」；
        这里盯 `save` 的调用次数。撤掉 `get_or_create` 开头的 `strip_nul(channel_name)` 就红：
        带 NUL 的名字与库里的干净名字比较会判成「变了」，白写一遍。
        """
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id=CHANNEL_ID,
            channel_type=ChatType.GROUP.value,
            channel_name="群名",
            chat_key=CHAT_KEY,
            is_active=True,
            observe_mode=False,
            data=json.dumps({}),
        )
        saves: List[str] = []
        real_save = DBChatChannel.save

        async def counting_save(self, *args: Any, **kwargs: Any) -> None:
            saves.append(self.channel_name)
            await real_save(self, *args, **kwargs)

        monkeypatch.setattr(DBChatChannel, "save", counting_save)

        channel = await DBChatChannel.get_or_create(
            adapter_key=ADAPTER,
            channel_id=CHANNEL_ID,
            channel_type=ChatType.GROUP,
            channel_name="群\x00名",
            instance_key=INSTANCE,
        )

        assert channel.channel_name == "群名"
        assert saves == []


class TestStructuredDataNotJsonString:
    """清洗发生在结构上，不是在 `json.dumps` 之后的字符串上

    `json.dumps` 会把 NUL 转义成字面的 `\\u0000`（不是真 NUL），在字符串上做替换根本碰不到它；
    而这份 JSON 读回来又会变回真 NUL，流进提示词和其他表。
    """

    def test_json_dumps_escapes_nul_so_string_replace_would_miss(self) -> None:
        dumped = json.dumps({"k": "a\x00b"}, ensure_ascii=False)
        assert "\\u0000" in dumped
        assert "\x00" not in dumped
        assert strip_nul(json.loads(dumped)) == {"k": "ab"}
