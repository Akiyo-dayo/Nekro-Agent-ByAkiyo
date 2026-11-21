"""聊天登记之后的原文清理与本轮待清理表

## 这条测试钉的是什么

- 密码、口令短语、私钥整段与私钥正文的每一行都能命中；短于 6 个字符的片段不替换；
- 聊天记录四列（含 Bot 自己的回复）、执行记录三列、提示词日志、记忆段落与频道摘要都被
  清理；窗口外与别的频道不碰；`is_recalled` 不动；
- 每次清理留一条不含原文的 `scrub` 审计；
- 待清理表在登记之后继续生效，过期后自动失效；没有待清理项时正文逐字不变。

环境是内存 SQLite；提示词日志目录与向量删除都打桩，不碰真磁盘布局与真 Qdrant。
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path

import pytest
from tortoise import Tortoise

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_exec_code import DBExecCode
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.services.memory import admin_service
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import scrub as scrub_module
from nekro_agent.services.ssh_gateway.scrub import (
    PLACEHOLDER,
    apply_pending,
    pending_fragments,
    register_pending,
    reset_pending,
    scrub_after_registration,
    secret_fragments,
)
from nekro_agent.tools import credential_cipher

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OWNER = "10001"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_7"
OTHER_CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_8"
PASSWORD = "pw-" + "scrub-8f3a"
PASSPHRASE = "phrase-" + "scrub-1b7c"
PRIVATE_KEY = (
    "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n"
    + "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQyNTUxOQ"
    + "\n-----END OPENSSH " + "PRIVATE KEY-----"
)
KEY_BODY_LINE = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQyNTUxOQ"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def cipher_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "ssh-scrub-" + "key-1")
    credential_cipher.reset_credential_cipher_cache()
    try:
        yield
    finally:
        credential_cipher.reset_credential_cipher_cache()


@pytest.fixture(autouse=True)
def clean_state():
    reset_pending()
    scrub_module.reset_prompt_log_registry()
    try:
        yield
    finally:
        reset_pending()
        scrub_module.reset_prompt_log_registry()


@pytest.fixture(autouse=True)
def prompt_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    prompts = tmp_path / "prompts"
    errors = tmp_path / "prompts_error"
    prompts.mkdir()
    errors.mkdir()
    monkeypatch.setattr(scrub_module.os_env, "PROMPT_LOG_DIR", str(prompts))
    monkeypatch.setattr(scrub_module.os_env, "PROMPT_ERROR_LOG_DIR", str(errors))
    return prompts, errors


@pytest.fixture(autouse=True)
def no_vectors(monkeypatch: pytest.MonkeyPatch):
    """软删段落时删向量的调用换成记录器，不连真 Qdrant"""
    calls: list[int] = []

    async def _fake_delete(ids):
        calls.extend(int(value) for value in ids)
        return True

    monkeypatch.setattr(admin_service, "_delete_vectors", _fake_delete)
    return calls


async def make_workspace_and_channel() -> tuple[DBWorkspace, DBChatChannel]:
    workspace = await DBWorkspace.create(name="ws-scrub", description="ws-scrub", status="active")
    channel = await DBChatChannel.create(
        chat_key=CHAT_KEY,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_7",
        channel_type="group",
        data="{}",
        workspace_id=workspace.id,
    )
    return workspace, channel


async def make_message(
    *,
    chat_key: str = CHAT_KEY,
    sender_id: str = OWNER,
    text: str,
    recalled: bool = False,
    message_id: str = "m1",
) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=sender_id,
        sender_name="sender",
        sender_nickname="sender",
        is_tome=0,
        is_recalled=recalled,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        message_id=message_id,
        chat_key=chat_key,
        chat_type="group",
        platform_userid=sender_id,
        content_text=text,
        content_data=text,
        raw_cq_code=text,
        ext_data=text,
        send_timestamp=int(time.time()),
    )


# ---------------------------------------------------------------------------
# 片段
# ---------------------------------------------------------------------------


def test_secret_fragments_include_key_lines_and_skip_short_values() -> None:
    fragments = secret_fragments(PASSWORD, PRIVATE_KEY, PASSPHRASE)

    assert PASSWORD in fragments
    assert PASSPHRASE in fragments
    assert PRIVATE_KEY.strip() in fragments
    assert KEY_BODY_LINE in fragments
    assert "short" not in fragments
    assert all(len(fragment) >= 6 for fragment in fragments)
    assert fragments == sorted(fragments, key=len, reverse=True)


def test_secret_fragments_drop_values_shorter_than_six() -> None:
    assert secret_fragments("abcde", "", "") == []
    assert secret_fragments("", "", "abc") == []


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


async def test_scrub_replaces_all_places_and_audits_without_plaintext(db, prompt_dirs, no_vectors) -> None:
    prompts, errors = prompt_dirs
    workspace, _channel = await make_workspace_and_channel()
    fragments = secret_fragments(PASSWORD, PRIVATE_KEY, PASSPHRASE)

    own = await make_message(text=f"用户说密码是 {PASSWORD}", message_id="m1")
    bot = await make_message(text=f"Bot 复述 {PASSWORD}", sender_id="-1", message_id="m2")
    other_channel = await make_message(text=f"别的频道 {PASSWORD}", chat_key=OTHER_CHAT_KEY, message_id="m3")
    old = await make_message(text=f"很久以前 {PASSWORD}", message_id="m4")
    await DBChatMessage.filter(id=old.id).update(
        create_time=datetime.datetime.now() - datetime.timedelta(hours=2),
    )

    code = await DBExecCode.create(
        chat_key=CHAT_KEY,
        code_text=f"password = {PASSWORD}",
        outputs=f"echo {PASSWORD}",
        thought_chain=f"用 {PASSPHRASE} 解密私钥 {PRIVATE_KEY}",
    )
    old_code = await DBExecCode.create(
        chat_key=CHAT_KEY,
        code_text=f"old {PASSWORD}",
        outputs="",
        thought_chain="",
    )
    await DBExecCode.filter(id=old_code.id).update(
        create_time=datetime.datetime.now() - datetime.timedelta(hours=2),
    )

    prompt_file = prompts / "chat_log_1.json"
    prompt_file.write_text(f'{{"prompt": "{PASSWORD}"}}', encoding="utf-8")
    scrub_module.note_prompt_log(CHAT_KEY, prompt_file)
    error_file = errors / "chat_err_1.json"
    error_file.write_text(f'{{"error": "{PASSWORD}"}}', encoding="utf-8")
    scrub_module.note_prompt_log(CHAT_KEY, error_file)
    untouched_file = prompts / "untouched.json"
    untouched_file.write_text("nothing here", encoding="utf-8")
    scrub_module.note_prompt_log(CHAT_KEY, untouched_file)
    # 别的频道写的日志：哪怕里面有同一串字也不碰（只清本频道自己写过的文件）
    other_channel_log = prompts / "chat_log_other.json"
    other_channel_log.write_text(f'{{"prompt": "{PASSWORD}"}}', encoding="utf-8")
    scrub_module.note_prompt_log(OTHER_CHAT_KEY, other_channel_log)
    # 本频道、但两小时前写的
    old_prompt = prompts / "old.json"
    old_prompt.write_text(PASSWORD, encoding="utf-8")
    scrub_module._prompt_logs[CHAT_KEY].appendleft((time.time() - 7200, str(old_prompt)))

    paragraph = await DBMemParagraph.create(
        workspace_id=workspace.id,
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.CONVERSATION,
        content=f"用户提供了 {PASSWORD}",
        origin_kind=OriginKind.MESSAGE,
        origin_chat_key=CHAT_KEY,
    )
    other_paragraph = await DBMemParagraph.create(
        workspace_id=workspace.id,
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.CONVERSATION,
        content=f"别的频道 {PASSWORD}",
        origin_kind=OriginKind.MESSAGE,
        origin_chat_key=OTHER_CHAT_KEY,
    )
    summary = await DBMemChannelSummary.create(chat_key=CHAT_KEY, content=f"摘要 {PASSWORD}")
    other_summary = await DBMemChannelSummary.create(chat_key=OTHER_CHAT_KEY, content=f"别的 {PASSWORD}")

    counts = await scrub_after_registration(chat_key=CHAT_KEY, fragments=fragments)

    for row_id in (own.id, bot.id):
        row = await DBChatMessage.get(id=row_id)
        assert row.content_text == f"用户说密码是 {PLACEHOLDER}" or row.content_text == f"Bot 复述 {PLACEHOLDER}"
        assert row.content_data == row.content_text
        assert row.raw_cq_code == row.content_text
        assert row.ext_data == row.content_text
    assert (await DBChatMessage.get(id=other_channel.id)).content_text == f"别的频道 {PASSWORD}"
    assert (await DBChatMessage.get(id=old.id)).content_text == f"很久以前 {PASSWORD}"
    assert (await DBChatMessage.get(id=own.id)).is_recalled is False

    code_row = await DBExecCode.get(id=code.id)
    assert PASSWORD not in code_row.code_text
    assert PASSWORD not in code_row.outputs
    assert "BEGIN OPENSSH" not in (code_row.thought_chain or "")
    assert PASSWORD in (await DBExecCode.get(id=old_code.id)).code_text

    assert PASSWORD not in prompt_file.read_text(encoding="utf-8")
    assert PASSWORD not in error_file.read_text(encoding="utf-8")
    assert untouched_file.read_text(encoding="utf-8") == "nothing here"
    assert old_prompt.read_text(encoding="utf-8") == PASSWORD
    assert PASSWORD in other_channel_log.read_text(encoding="utf-8")

    assert (await DBMemParagraph.get(id=paragraph.id)).is_inactive is True
    assert (await DBMemParagraph.get(id=other_paragraph.id)).is_inactive is False
    assert no_vectors == [paragraph.id]
    assert PASSWORD not in (await DBMemChannelSummary.get(id=summary.id)).content
    assert PASSWORD in (await DBMemChannelSummary.get(id=other_summary.id)).content

    assert counts["chat_messages"] >= 2
    assert counts["exec_code"] >= 2
    assert counts["prompt_logs"] >= 2
    assert counts["mem_paragraphs"] == 1
    assert counts["mem_summaries"] >= 1

    audit = await DBWsSshAudit.filter(event=C.EVENT_SCRUB).first()
    assert audit is not None
    assert PASSWORD not in audit.detail
    assert PASSPHRASE not in audit.detail


async def test_private_key_lines_are_replaced_even_when_wrapping_changed(db) -> None:
    fragments = secret_fragments("", PRIVATE_KEY, "")
    message = await make_message(text=f"粘贴了一半：{KEY_BODY_LINE}", message_id="key-1")

    await scrub_after_registration(chat_key=CHAT_KEY, fragments=fragments)

    assert KEY_BODY_LINE not in (await DBChatMessage.get(id=message.id)).content_text
    assert PLACEHOLDER in (await DBChatMessage.get(id=message.id)).content_text


async def test_json_escaped_secret_in_content_data_is_replaced_without_breaking_json(db) -> None:
    """`content_data` 是 `json.dumps` 存的：带引号、反斜杠的密码在里面是转义后的样子"""
    import json as _json

    tricky = 'Pa"ss\\w0rd' + '","text":"'
    text = f"我的密码是 {tricky}"
    segments = [{"type": "text", "text": text}]
    message = await DBChatMessage.create(
        sender_id=OWNER,
        sender_name="sender",
        sender_nickname="sender",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        message_id="json-1",
        chat_key=CHAT_KEY,
        chat_type="group",
        platform_userid=OWNER,
        content_text=text,
        content_data=_json.dumps(segments, ensure_ascii=False),
        raw_cq_code=text.replace(",", "&#44;").replace("[", "&#91;").replace("]", "&#93;"),
        ext_data=_json.dumps({"ref_msg_id": ""}, ensure_ascii=False),
        send_timestamp=int(time.time()),
    )

    await scrub_after_registration(chat_key=CHAT_KEY, fragments=[tricky])

    stored = await DBChatMessage.get(id=message.id)
    assert tricky not in stored.content_text
    decoded = _json.loads(stored.content_data)
    assert decoded == [{"type": "text", "text": f"我的密码是 {PLACEHOLDER}"}]
    assert "Pa" not in stored.raw_cq_code
    assert stored.is_recalled is False


async def test_paragraph_text_is_replaced_before_soft_delete_and_episode_is_cleaned(db, no_vectors) -> None:
    from nekro_agent.models.db_mem_episode import DBMemEpisode

    workspace, _channel = await make_workspace_and_channel()
    paragraph = await DBMemParagraph.create(
        workspace_id=workspace.id,
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.CONVERSATION,
        content=f"用户提供了服务器密码 {PASSWORD}，之后又聊了别的",
        summary=f"密码 {PASSWORD}",
        origin_kind=OriginKind.MESSAGE,
        origin_chat_key=CHAT_KEY,
    )
    # Episode 的叙事摘要按 80 字截取段落原文：凭据可能只剩开头一截留在末尾
    narrative = ("x" * 70) + PASSWORD[:7]
    episode = await DBMemEpisode.create(
        workspace_id=workspace.id,
        origin_chat_key=CHAT_KEY,
        title=f"服务器 {PASSWORD}",
        narrative_summary=narrative,
        paragraph_ids=[paragraph.id],
    )

    counts = await scrub_after_registration(chat_key=CHAT_KEY, fragments=[PASSWORD])

    stored = await DBMemParagraph.get(id=paragraph.id)
    assert stored.is_inactive is True
    assert PASSWORD not in stored.content and PASSWORD not in (stored.summary or "")
    cleaned = await DBMemEpisode.get(id=episode.id)
    assert PASSWORD not in cleaned.title
    assert PASSWORD[:7] not in cleaned.narrative_summary
    assert counts["mem_episodes"] >= 2


async def test_short_fragments_are_not_replaced(db) -> None:
    message = await make_message(text="普通短词 abcde", message_id="short-1")

    counts = await scrub_after_registration(chat_key=CHAT_KEY, fragments=["abcde"])

    assert (await DBChatMessage.get(id=message.id)).content_text == "普通短词 abcde"
    assert all(value == 0 for value in counts.values())


# ---------------------------------------------------------------------------
# 待清理表
# ---------------------------------------------------------------------------


def test_pending_table_replaces_until_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scrub_module, "_clock", lambda: 1000.0)
    register_pending(CHAT_KEY, [PASSWORD], ttl_seconds=10)

    assert apply_pending(CHAT_KEY, f"下一段执行记录 {PASSWORD}") == f"下一段执行记录 {PLACEHOLDER}"
    assert pending_fragments(CHAT_KEY) == [PASSWORD]

    monkeypatch.setattr(scrub_module, "_clock", lambda: 1011.0)
    assert apply_pending(CHAT_KEY, f"过期之后 {PASSWORD}") == f"过期之后 {PASSWORD}"
    assert pending_fragments(CHAT_KEY) == []


def test_pending_table_without_entries_returns_text_unchanged() -> None:
    text = "没有待清理项时逐字不变"
    assert apply_pending(CHAT_KEY, text) == text
    assert apply_pending("", text) == text


def test_pending_table_merges_multiple_registrations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scrub_module, "_clock", lambda: 2000.0)
    register_pending(CHAT_KEY, [PASSWORD], ttl_seconds=10)
    register_pending(CHAT_KEY, [PASSPHRASE], ttl_seconds=10)

    assert set(pending_fragments(CHAT_KEY)) == {PASSWORD, PASSPHRASE}


# ---------------------------------------------------------------------------
# 提示词日志落盘
# ---------------------------------------------------------------------------


async def test_finish_round_scrubs_messages_saved_after_registration(db) -> None:
    """登记之后本轮才落库的消息（例如 Bot 复述了凭据的回复）在回合收尾时补扫掉"""
    from nekro_agent.services.ssh_gateway.scrub import finish_round

    assert await finish_round(CHAT_KEY) == 0, "没有待清理项时什么都不做"

    register_pending(CHAT_KEY, [PASSWORD])
    reply = await make_message(sender_id="-1", text=f"好的，已登记，密码是 {PASSWORD}", message_id="bot-1")
    other = await make_message(chat_key="other-chat", text=f"别的频道的 {PASSWORD}", message_id="o-1")

    # 计数按「替换了几处」算（一条消息的四列各算一处）
    assert await finish_round(CHAT_KEY) > 0
    stored = await DBChatMessage.get(id=reply.id)
    assert PASSWORD not in stored.content_text
    assert PASSWORD not in stored.raw_cq_code
    assert (await DBChatMessage.get(id=other.id)).content_text == f"别的频道的 {PASSWORD}", "别的频道不碰"


async def test_prompt_log_write_uses_pending_fragments(tmp_path: Path) -> None:
    from nekro_agent.services.agent.openai import OpenAIResponse, log_pending_chat_key

    register_pending(CHAT_KEY, [PASSWORD])
    response = OpenAIResponse(
        response_content=f"回答里也有 {PASSWORD}",
        thought_chain="",
        messages=[],
        message_cnt=1,
        token_consumption=0,
        token_input=0,
        token_output=0,
        use_model="test-model",
        speed_tokens_per_second=0.0,
        first_token_cost_ms=0,
        generation_time_ms=0,
        stream_mode=False,
    )
    log_path = tmp_path / "chat_log.json"

    with log_pending_chat_key(CHAT_KEY):
        await response.save_log(
            log_path,
            log_style="json",
            messages=[{"role": "user", "content": f"请求里有 {PASSWORD}"}],
        )

    text = log_path.read_text(encoding="utf-8")
    assert PASSWORD not in text
    assert PLACEHOLDER in text


async def test_prompt_log_write_is_unchanged_after_context_reset(tmp_path: Path) -> None:
    from nekro_agent.services.agent.openai import OpenAIResponse, log_pending_chat_key

    register_pending(CHAT_KEY, [PASSWORD])
    response = OpenAIResponse(
        response_content="",
        thought_chain="",
        messages=[],
        message_cnt=1,
        token_consumption=0,
        token_input=0,
        token_output=0,
        use_model="test-model",
        speed_tokens_per_second=0.0,
        first_token_cost_ms=0,
        generation_time_ms=0,
        stream_mode=False,
    )
    log_path = tmp_path / "other_log.json"

    with log_pending_chat_key("other-chat"):
        await response.save_log(
            log_path,
            log_style="json",
            messages=[{"role": "user", "content": PASSWORD}],
        )

    assert PASSWORD in log_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 记忆实体：规范名撞车与彻底失败
# ---------------------------------------------------------------------------


async def make_entity(
    workspace: DBWorkspace,
    *,
    name: str,
    canonical_name: str,
    aliases: list[str] | None = None,
) -> DBMemEntity:
    return await DBMemEntity.create(
        workspace_id=workspace.id,
        instance_key=INSTANCE,
        entity_type=EntityType.CONCEPT,
        name=name,
        canonical_name=canonical_name,
        aliases=list(aliases or []),
    )


async def link_entity_to_paragraph(
    workspace: DBWorkspace,
    *,
    subject: DBMemEntity,
    obj: DBMemEntity,
    paragraph_id: int = 4242,
) -> DBMemRelation:
    return await DBMemRelation.create(
        workspace_id=workspace.id,
        instance_key=INSTANCE,
        subject_entity_id=subject.id,
        predicate="uses",
        object_entity_id=obj.id,
        paragraph_id=paragraph_id,
        memory_source="na",
        cognitive_type="episodic",
    )


async def test_entity_scrub_retries_with_id_when_canonical_name_collides(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两条实体替换后规范名相同：第二次带 `#id` 再试一次并成功"""
    from tortoise.queryset import QuerySet

    workspace, _channel = await make_workspace_and_channel()
    replaced = f"user-{PLACEHOLDER}"
    clean = await make_entity(
        workspace,
        name="alpha",
        canonical_name="alpha",
        aliases=[f"aka-{PASSWORD}"],
    )
    collided = await make_entity(
        workspace,
        name=f"user-{PASSWORD}",
        canonical_name=f"user-{PASSWORD}",
    )
    # 已经存在、替换后会与 collided 撞名的实体（模拟唯一约束的另一端）
    await make_entity(workspace, name="existing", canonical_name=replaced)
    await link_entity_to_paragraph(workspace, subject=collided, obj=clean)

    real_update = QuerySet.update
    attempts: list[str] = []

    def fake_update(self, **kwargs):
        if self.model is DBMemEntity and kwargs.get("canonical_name") == replaced:
            attempts.append(replaced)
            if len(attempts) == 1:
                raise RuntimeError("UNIQUE constraint failed: mem_entity.canonical_name")
        return real_update(self, **kwargs)

    monkeypatch.setattr(QuerySet, "update", fake_update)

    total = await scrub_module._scrub_entities([PASSWORD], [4242])

    assert total >= 1
    stored_clean = await DBMemEntity.get(id=clean.id)
    assert all(PASSWORD not in alias for alias in stored_clean.aliases)
    assert any(PLACEHOLDER in alias for alias in stored_clean.aliases)
    stored_collided = await DBMemEntity.get(id=collided.id)
    assert stored_collided.canonical_name == f"{replaced}#{collided.id}"
    assert len(attempts) == 1


async def test_entity_scrub_warns_and_keeps_cleaning_when_retry_also_fails(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连 `#id` 也失败：记 WARNING、不抛出，其余实体照常清理"""
    from tortoise.queryset import QuerySet

    workspace, _channel = await make_workspace_and_channel()
    clean = await make_entity(
        workspace,
        name="alpha",
        canonical_name="alpha",
        aliases=[f"aka-{PASSWORD}"],
    )
    broken = await make_entity(
        workspace,
        name=f"user-{PASSWORD}",
        canonical_name=f"user-{PASSWORD}",
    )
    await link_entity_to_paragraph(workspace, subject=broken, obj=clean)

    real_update = QuerySet.update

    def fake_update(self, **kwargs):
        if self.model is DBMemEntity and "canonical_name" in kwargs:
            raise RuntimeError("UNIQUE constraint failed: mem_entity.canonical_name")
        return real_update(self, **kwargs)

    monkeypatch.setattr(QuerySet, "update", fake_update)

    warnings: list[str] = []

    class _Recorder:
        def warning(self, message: str) -> None:
            warnings.append(message)

    monkeypatch.setattr(scrub_module, "logger", _Recorder())

    total = await scrub_module._scrub_entities([PASSWORD], [4242])

    assert total >= 1
    assert any(
        f"entity_id={broken.id}" in message and "RuntimeError" in message
        for message in warnings
    ), warnings
    assert all(PASSWORD not in message for message in warnings)
    stored_clean = await DBMemEntity.get(id=clean.id)
    assert all(PASSWORD not in alias for alias in stored_clean.aliases)
    assert any(PLACEHOLDER in alias for alias in stored_clean.aliases)
    stored_broken = await DBMemEntity.get(id=broken.id)
    assert PASSWORD in stored_broken.canonical_name, "改不了的那条保持原样，流程不抛"

