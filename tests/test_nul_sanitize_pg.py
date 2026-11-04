"""NUL 清洗在真 PostgreSQL 上的验收

SQLite 收 NUL，所以 `test_nul_sanitize.py` 只能在 SQLite 上证明"清洗有没有被调用"；
本条要证明的是**原始问题真的存在、修复真的解决它**。因此先做反向对照：往临时表里插带 NUL
的字符串，断言 PG 确实拒绝（否则整套用例就是在证明一个不存在的问题）。

设 `NA_TEST_PG_DSN`（如 `postgres://postgres@127.0.0.1:55499/natest`）开门，未设则 skip。
每个用例建自己的随机 schema，结束时 `DROP SCHEMA ... CASCADE`；`EXECUTOR_CACHE` 进出两端都清
（它是进程级缓存、不含方言），写法与 `test_wechat_session_unique_pg.py` 一致。
"""

import datetime
import json
import os
import uuid
from typing import AsyncIterator, List
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE

from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.chat_message import ChatType

PG_DSN_ENV = "NA_TEST_PG_DSN"

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
CHANNEL_ID = "group_123456"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-{CHANNEL_ID}"

# 只建本文件用到的三张表；这三张表的模型各自 import 了别的模块，但那只是 Python 层的引用，
# 建表只需要它们自己（`generate_schemas` 只处理注册进来的模型）
MODEL_MODULES = [
    "nekro_agent.models.db_chat_message",
    "nekro_agent.models.db_user",
    "nekro_agent.models.db_chat_channel",
]


def _pg_dsn_or_skip() -> str:
    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(
            f"需要真实 PostgreSQL：设 {PG_DSN_ENV}（如 postgres://postgres@127.0.0.1:55499/natest）。"
            "SQLite 侧同名契约由 test_nul_sanitize.py 覆盖。",
        )
    return dsn


@pytest.fixture
async def pg_env() -> AsyncIterator[str]:
    """独占 schema 里按模型生成的三张表；EXECUTOR_CACHE 进出两端都清"""
    dsn = _pg_dsn_or_skip()
    import asyncpg

    schema = f"na_nul_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    EXECUTOR_CACHE.clear()
    await Tortoise.init(
        db_url=f"{dsn}{sep}{urlencode({'schema': schema})}",
        modules={"models": MODEL_MODULES},
    )
    await Tortoise.generate_schemas()
    try:
        yield schema
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


def _find_in_exception_chain(error: BaseException, needle: str) -> bool:
    seen: List[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        if needle in str(current) or needle in type(current).__name__:
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_nul_rejection(error: BaseException) -> bool:
    """PG 拒绝 NUL 的几种形态都算数

    text 列上 asyncpg 抛 `CharacterNotInRepertoireError`（消息含 `0x00`）；
    jsonb 在解析 `\\u0000` 转义时抛的是 `UntranslatableCharacterError`
    （`unsupported Unicode escape sequence`）。以消息内容为准，避免把版本差异写死。
    """
    for needle in ("0x00", "\\u0000", "CharacterNotInRepertoireError", "Unicode escape sequence"):
        if _find_in_exception_chain(error, needle):
            return True
    return False


class TestPostgresReallyRejectsNul:
    """反向对照：这条不绿就说明"问题"不存在，上面的修复与下面的验收都没有意义"""

    async def test_raw_insert_with_nul_is_rejected(self, pg_env: str) -> None:  # noqa: ARG002
        await connections.get("default").execute_script(
            'CREATE TABLE "nul_probe" ("id" serial PRIMARY KEY, "txt" text NOT NULL);',
        )

        with pytest.raises(Exception) as exc_info:
            await connections.get("default").execute_query(
                'INSERT INTO "nul_probe" ("txt") VALUES ($1)',
                ["张\x00三"],
            )

        assert _is_nul_rejection(exc_info.value), f"期望 PG 因 NUL 拒绝写入，实际异常: {exc_info.value!r}"

    async def test_same_string_without_nul_is_accepted(self, pg_env: str) -> None:  # noqa: ARG002
        """正面对照：把 NUL 去掉之后同一个值能写进去，排除"是别的字段/编码问题"的可能"""
        await connections.get("default").execute_script(
            'CREATE TABLE "nul_probe_ok" ("id" serial PRIMARY KEY, "txt" text NOT NULL);',
        )

        await connections.get("default").execute_query(
            'INSERT INTO "nul_probe_ok" ("txt") VALUES ($1)',
            ["张三"],
        )

        rows = await connections.get("default").execute_query_dict('SELECT "txt" FROM "nul_probe_ok"')
        assert [row["txt"] for row in rows] == ["张三"]


class TestChatMessageOnPostgres:
    """验收：带 NUL 的 `DBChatMessage.create` 在 PG 上成功，且除 NUL 外逐字不变

    撤掉 `db_chat_message.py` 末尾的 `pre_save(DBChatMessage)(strip_nul_before_save)` 就红
    （asyncpg 会抛 CharacterNotInRepertoireError，用例在 create 那一行就失败）。
    """

    async def test_create_with_nul_succeeds_and_is_clean(self, pg_env: str) -> None:  # noqa: ARG002
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
            # `content_data` / `ext_data` 是 TextField，存的是 `json.dumps` 的结果：真 NUL 在这里
            # 会被转义成字面的 `\\u0000`，PG 照收。但读回来 `json5.loads` 又变回真 NUL、流进提示词，
            # 所以入口清洗仍要在**结构上**先剔掉（见 test_nul_sanitize.py 的 `_without_nul` 用例）。
            content_data=json.dumps([{"type": "text", "text": "你好"}], ensure_ascii=False),
            raw_cq_code="[CQ:at,qq=4\x002]",
            ext_data=json.dumps({"k": "v"}, ensure_ascii=False),
            send_timestamp=0,
        )

        stored = await DBChatMessage.get(id=created.id)

        assert stored.sender_nickname == "群名片"
        assert stored.sender_name == "张三"
        assert stored.content_text == "ab\tǿĀ"
        # 除 NUL 以外的字段与写入值逐字相同
        assert stored.message_id == "m1"
        assert stored.chat_key == CHAT_KEY
        assert stored.raw_cq_code == "[CQ:at,qq=42]"
        assert json.loads(stored.content_data) == [{"type": "text", "text": "你好"}]
        assert json.loads(stored.ext_data) == {"k": "v"}
        # 原始 JSON 串里也不能留字面的 \u0000
        assert "\\u0000" not in stored.content_data
        assert "\\u0000" not in stored.ext_data


class TestUserJsonbOnPostgres:
    """验收：`ext_data` 是 jsonb，NUL 在键和值上都不收

    撤掉 `db_user.py` 末尾的注册行就红。
    """

    async def test_create_with_nul_in_username_and_jsonb(self, pg_env: str) -> None:  # noqa: ARG002
        created = await DBUser.create(
            username="x\x00y",
            password="",
            adapter_key=ADAPTER,
            platform_userid="10001",
            perm_level=1,
            login_time=datetime.datetime.now(),
            ext_data={"a": ["b\x00c"]},
        )

        stored = await DBUser.get(id=created.id)

        assert stored.username == "xy"
        assert stored.ext_data == {"a": ["bc"]}

    async def test_nul_in_jsonb_key_is_rejected_without_the_guard(self, pg_env: str) -> None:  # noqa: ARG002
        """反向对照：jsonb 的键同样不收 NUL，证明守卫必须处理键而不只是值"""
        await connections.get("default").execute_script(
            'CREATE TABLE "jsonb_probe" ("id" serial PRIMARY KEY, "data" jsonb NOT NULL);',
        )

        with pytest.raises(Exception) as exc_info:
            await connections.get("default").execute_query(
                'INSERT INTO "jsonb_probe" ("data") VALUES ($1::jsonb)',
                ['{"a\\u0000b": 1}'],
            )

        assert _is_nul_rejection(exc_info.value), f"期望 PG 因 NUL 拒绝写入 jsonb，实际异常: {exc_info.value!r}"


class TestChatChannelOnPostgres:
    """验收：`channel_name` 的新建与改名都要能过 PG

    撤掉 `db_chat_channel.py` 末尾的注册行就红。
    """

    async def test_create_with_nul_succeeds(self, pg_env: str) -> None:  # noqa: ARG002
        created = await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id=CHANNEL_ID,
            channel_type=ChatType.GROUP.value,
            channel_name="群\x00",
            chat_key=CHAT_KEY,
            is_active=True,
            observe_mode=False,
            data=json.dumps({}),
        )

        stored = await DBChatChannel.get(id=created.id)
        assert stored.channel_name == "群"

    async def test_rename_existing_row_succeeds(self, pg_env: str) -> None:  # noqa: ARG002
        """`save()` 也要走监听器：后台改名的路径就是它"""
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
