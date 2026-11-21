"""角色状态的命令与状态图

要钉住的事：

1. **群里永远不出现私密字段与别人的档案**：四种身份（普通群友、高级用户、超级用户、WebUI 执行）在群里拿到的
   文字与图都一样，只有频道状态里不私密的部分，加上发命令的人自己档案里不私密的部分；私聊里对方的档案完整；
   隐藏字段哪里都不出现；
2. 渲染器只吃过滤好的 `StateCardData`（只有字符串），拿不到状态树；没有字体时退回文字；
3. 文字里的 `@` 与 `[CQ:` 换成全角；文字有上限；
4. 重置与恢复只给高级用户、写审计；查看时就地初始化（没有模板时说明怎么加）；冷却按频道、按种类；
5. 画图全进程最多两张同时画；自动发图的档位、间隔下限、被拦不发、私聊等更新跑完、不进聊天记录、
   群里不带任何人的档案；
6. 卡片整理：页头字段、好感映射、列表与深层只数看得见的、行数上限按优先级丢组。
"""

# ruff: noqa: F811 - 固件从同目录的用例模块导入，用例参数与它同名

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from test_state_table_plugin import (  # type: ignore[import-not-found]
    GROUP_KEY,
    PRIVATE_KEY,
    _config,
    _load,
    _mark_person_root,
    db,  # noqa: F401 - 固件
    model,  # noqa: F401 - 固件
)

from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_state_channel import DBStateChannel
from nekro_agent.models.db_state_person import DBStatePerson
from nekro_agent.models.db_state_snapshot import DBStateSnapshot
from nekro_agent.models.db_state_template import DBStateTemplate
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.services.command.schemas import CommandExecutionContext, CommandResponseStatus
from nekro_agent.services.imagecard import fonts_available
from nekro_agent.services.imagecard.state_card import (
    MAX_ROWS,
    StateCardData,
    StateCardRow,
    StateCardSection,
    card_text,
    plain,
    render_state_card,
)
from plugins.builtin.state_table import card, commands, session, updater, view
from plugins.builtin.state_table import main as state_main
from plugins.builtin.state_table.schema import FieldDef, Template

OUTFITS = {"u1": "礼服", "u2": "雨衣", "u3": "围裙"}
SECRETS = {"u1": "一号的私事", "u2": "二号的私事", "u3": "三号的私事"}
CHANNEL_SECRET = "频道里的秘密"


@pytest.fixture
def scene(db: SimpleNamespace, model: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """群聊与私聊各有状态；三个人各有一份档案（穿着不私密，隐私私密）；冷却关掉（连下限一起）"""
    card.reset_for_tests()
    model.configure(COMMAND_TEXT_COOLDOWN_SECONDS=0, COMMAND_IMAGE_COOLDOWN_SECONDS=0)
    monkeypatch.setattr(card, "cooldown_seconds", lambda _instance_key, _kind: 0.0)
    state = SimpleNamespace(affection=False, image_mode="command", every=10, min_n=1)

    async def effective(_self: Any) -> Any:
        return _config(
            MODEL_GROUPS={},
            AFFECTION_ENABLE_SYSTEM=state.affection,
            STATE_IMAGE_SEND_MODE=state.image_mode,
            STATE_IMAGE_EVERY_N=state.every,
            STATE_IMAGE_MIN_N=state.min_n,
        )

    monkeypatch.setattr(DBChatChannel, "get_effective_config", effective)

    async def setup() -> None:
        await _load(db.group, code="g")
        await _load(db.private, code="p")
        await _mark_person_root(db.preset.id)
        for chat_key in (GROUP_KEY, PRIVATE_KEY):
            row = await DBStateChannel.get(chat_key=chat_key)
            await DBStateChannel.filter(id=row.id).update(data={**row.data, "秘密": CHANNEL_SECRET})
        for uid in OUTFITS:
            scopes = await session.resolve_identity_scopes(db.group, [uid], preset_id=db.preset.id)
            columns = dict(session.AffectionKey.of_scope(scopes[uid]).as_columns())
            await DBStatePerson.create(**columns, data={"主角信息": {"穿着": OUTFITS[uid], "隐私": SECRETS[uid]}})

    state.setup = setup
    return state


IDENTITIES = {
    "member": {},
    "advanced": {"is_advanced_user": True},
    "super": {"is_super_user": True, "is_advanced_user": True},
    "webui": {"user_id": "42", "adapter_key": "webui", "is_super_user": True, "is_advanced_user": True},
}


def _context(chat_key: str, identity: str = "member", **overrides: Any) -> CommandExecutionContext:
    base: dict[str, Any] = {"user_id": "u1", "chat_key": chat_key, "username": "阿青", "adapter_key": "onebot_v11"}
    base.update(IDENTITIES[identity])
    base.update(overrides)
    return CommandExecutionContext(**base)


def _strings(value: Any) -> list[str]:
    """把卡片数据里的全部字符串取出来；顺便断言里面只有字符串、整数与这几个数据类（没有字典、列表）"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, int):
        return []
    if isinstance(value, tuple):
        return [text for item in value for text in _strings(item)]
    assert isinstance(value, (StateCardData, StateCardSection, StateCardRow)), type(value)
    return [text for item in dataclasses.fields(value) for text in _strings(getattr(value, item.name))]


@pytest.fixture
def drawn(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """把真正的渲染换成记录参数（有没有字体都能跑）"""
    seen: list[Any] = []

    def fake_render(data: Any) -> bytes:
        seen.append(data)
        return b"\x89PNG fake"

    monkeypatch.setattr(card, "render_state_card", fake_render)
    monkeypatch.setattr(card, "fonts_available", lambda: True)
    return seen


class TestPrivacy:
    @pytest.mark.parametrize("identity", list(IDENTITIES))
    async def test_group_text_and_image_hold_only_what_everyone_may_see(
        self, db: SimpleNamespace, scene: Any, drawn: list[Any], identity: str
    ) -> None:
        await scene.setup()

        text = (await commands.state_command(_context(GROUP_KEY, identity), "")).message
        image = await commands.state_command(_context(GROUP_KEY, identity), "图")
        assert image.output_segments and image.output_segments[0].file_path
        shown = "\n".join([text, *_strings(drawn[-1])])

        assert "早上" in shown and "码头" in shown
        assert CHANNEL_SECRET not in shown and "不给模型看" not in shown
        assert "便服" not in shown  # 频道行里留着的个人范围那一棵（标个人范围之前建的）不当频道状态画
        assert not any(secret in shown for secret in SECRETS.values())
        assert OUTFITS["u2"] not in shown and OUTFITS["u3"] not in shown
        assert (OUTFITS["u1"] in shown) is (identity != "webui")  # WebUI 执行没有群里的账号，也就没有「自己的档案」

    async def test_private_chat_shows_the_counterparts_whole_profile(
        self, db: SimpleNamespace, scene: Any, drawn: list[Any]
    ) -> None:
        await scene.setup()

        text = (await commands.state_command(_context(PRIVATE_KEY), "")).message
        await commands.state_command(_context(PRIVATE_KEY), "图")
        shown = "\n".join([text, *_strings(drawn[-1])])

        assert CHANNEL_SECRET in shown and SECRETS["u1"] in shown and OUTFITS["u1"] in shown
        assert "不给模型看" not in shown
        assert OUTFITS["u2"] not in shown and SECRETS["u2"] not in shown

    async def test_renderer_only_gets_card_data(self, db: SimpleNamespace, scene: Any, drawn: list[Any]) -> None:
        await scene.setup()

        await commands.state_command(_context(GROUP_KEY), "图")

        assert len(drawn) == 1 and isinstance(drawn[0], StateCardData)
        _strings(drawn[0])  # 里面只有字符串与卡片数据类，没有任何字典或列表
        with pytest.raises(TypeError):
            render_state_card({"当前时间": "早上"})  # type: ignore[arg-type]

    async def test_others_affection_stage_never_shows(self, db: SimpleNamespace, scene: Any) -> None:
        await scene.setup()
        scene.affection = True
        for uid, score in (("u1", 10), ("u2", 180)):
            scopes = await session.resolve_identity_scopes(db.group, [uid], preset_id=db.preset.id)
            columns = dict(session.AffectionKey.of_scope(scopes[uid]).as_columns())
            await DBAffection.create(**columns, score=score)

        mine = (await commands.state_command(_context(GROUP_KEY), "")).message
        theirs_view = await view.build_card(db.group, requester_uid="u2", include_requester=True)

        assert "关系" in mine
        assert theirs_view is not None
        mine_rows = {
            row.value
            for section in (await view.build_card(db.group, requester_uid="u1", include_requester=True)).sections
            for row in section.rows
        }  # type: ignore[union-attr]
        their_rows = {row.value for section in theirs_view.sections for row in section.rows}
        assert mine_rows != their_rows  # 各看各的：一个人的命令里只有他自己的关系
        auto = await view.build_card(db.group, requester_uid="u1", include_requester=False)
        assert auto is not None and all(row.label != "关系" for section in auto.sections for row in section.rows)
        assert OUTFITS["u1"] not in _strings(auto)  # 不带发命令的人时，连自己的档案也不带


class TestTextOutput:
    def test_mentions_and_protocol_codes_are_defanged(self) -> None:
        assert (
            commands.safe_text("找 [@id:123@] 和 [CQ:at,qq=1] 还有 [ cq :face]")
            == "找 [＠id:123＠] 和 ［CQ:at,qq=1] 还有 ［ cq :face]"
        )

    def test_text_is_capped_and_plain(self) -> None:
        data = StateCardData(
            title="标题\n第二行",
            sections=(StateCardSection("组", tuple(StateCardRow(f"键{index}", "值" * 100) for index in range(60))),),
        )

        text = card_text(data, 1500)

        assert len(text) <= 1500 and text.endswith("后面没写）")
        assert plain("a​b\nc\x07", 10) == "ab c"

    @pytest.mark.parametrize("mark", ["⁦", "⁧", "⁨", "⁩", "‮", "‏"])
    def test_direction_marks_are_stripped_from_image_and_text(self, mark: str) -> None:
        data = StateCardData(
            title=f"标{mark}题",
            headline=(StateCardRow(f"时{mark}间", f"傍{mark}晚"),),
            sections=(StateCardSection(f"组{mark}", (StateCardRow(f"键{mark}", f"值{mark}"),)),),
            footer=f"脚{mark}注",
        )

        assert plain(f"左{mark}右", 10) == "左右"
        assert mark not in card_text(data, 1500)

    async def test_text_command_points_to_the_image_when_cut(
        self, db: SimpleNamespace, scene: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scene.setup()
        monkeypatch.setattr(commands, "TEXT_LIMIT", 30)

        text = (await commands.state_command(_context(GROUP_KEY), "")).message

        assert "「/状态 图」" in text

    async def test_no_fonts_falls_back_to_text(
        self, db: SimpleNamespace, scene: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scene.setup()
        monkeypatch.setattr(card, "fonts_available", lambda: False)

        response = await commands.state_command(_context(GROUP_KEY), "图")

        assert not response.output_segments and "早上" in response.message


class TestCommandRules:
    def test_command_is_mounted(self) -> None:
        names = {command.name: command for command in state_main.plugin._commands}  # noqa: SLF001
        assert "state" in names and "状态" in names["state"].aliases

    async def test_reset_and_resume_need_an_advanced_user(self, db: SimpleNamespace, scene: Any) -> None:
        await scene.setup()
        await DBStateChannel.filter(chat_key=GROUP_KEY).update(data={"当前时间": "深夜"}, paused=True)

        denied = await commands.state_command(_context(GROUP_KEY), "重置")
        assert denied.status == CommandResponseStatus.ERROR
        assert (await DBStateChannel.get(chat_key=GROUP_KEY)).data == {"当前时间": "深夜"}

        await commands.state_command(_context(GROUP_KEY, "advanced"), "重置")
        await commands.state_command(_context(GROUP_KEY, "advanced"), "恢复")

        row = await DBStateChannel.get(chat_key=GROUP_KEY)
        assert row.data["当前时间"] == "早上" and row.paused is False
        audit = (
            await DBStateSnapshot.filter(target_kind="channel", target_id=row.id)
            .order_by("id")
            .values_list("source", "editor")
        )
        assert ("reset", "onebot_v11:u1") in audit and ("admin", "onebot_v11:u1") in audit
        assert await DBStatePerson.filter(platform_userid="u1").first() is not None  # 个人档案不动

    async def test_command_explains_how_to_add_a_template(self, db: SimpleNamespace, scene: Any) -> None:
        await DBWbEntry.filter(comment="[initvar]初始变量").update(enabled=False)

        response = await commands.state_command(_context(GROUP_KEY), "")
        reset = await commands.state_command(_context(GROUP_KEY, "advanced"), "重置")

        assert response.message == commands.NO_TEMPLATE_HINT
        assert reset.message == commands.NO_TEMPLATE_HINT
        assert await DBStateChannel.all().count() == 0
        assert await DBStateTemplate.all().count() == 0

    async def test_unknown_action(self, db: SimpleNamespace, scene: Any) -> None:
        response = await commands.state_command(_context(GROUP_KEY), "删库")

        assert response.status == CommandResponseStatus.ERROR and "用法" in response.message

    async def test_cooldown_is_per_channel_and_kind(
        self, db: SimpleNamespace, scene: Any, model: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scene.setup()
        monkeypatch.setattr(card, "cooldown_seconds", lambda _instance_key, kind: 30.0 if kind == "image" else 10.0)

        first = await commands.state_command(_context(GROUP_KEY), "")
        second = await commands.state_command(_context(GROUP_KEY), "")
        other_channel = await commands.state_command(_context(PRIVATE_KEY), "")

        assert "早上" in first.message and "秒后再试" in second.message and "早上" in other_channel.message
        assert card.cooldown_left("x", "image", 30) == 0 and card.cooldown_left("x", "text", 10) == 0
        assert card.cooldown_left("x", "image", 30) > 0
        assert card.cooldown_left("y", "image", 0) == 0 and card.cooldown_left("y", "image", 0) == 0

    @pytest.mark.parametrize(
        ("platform", "instance", "expected"),
        [
            ((0, 0), (0, 0), (3, 10)),  # 都设成 0 也有代码里的下限
            ((20, 60), (5, 15), (20, 60)),  # 实例调低不管用：平台设的是下限
            ((20, 60), (40, 90), (40, 90)),  # 实例只能往上调
        ],
    )
    def test_cooldown_has_a_floor_that_instances_can_only_raise(
        self,
        model: Any,
        monkeypatch: pytest.MonkeyPatch,
        platform: tuple[int, int],
        instance: tuple[int, int],
        expected: tuple[int, int],
    ) -> None:
        model.configure(COMMAND_TEXT_COOLDOWN_SECONDS=instance[0], COMMAND_IMAGE_COOLDOWN_SECONDS=instance[1])
        monkeypatch.setattr(
            card.plugin,
            "get_global_config",
            lambda _cls: SimpleNamespace(
                COMMAND_TEXT_COOLDOWN_SECONDS=platform[0], COMMAND_IMAGE_COOLDOWN_SECONDS=platform[1]
            ),
        )

        assert (card.cooldown_seconds("st1", "text"), card.cooldown_seconds("st1", "image")) == expected

    async def test_command_uses_the_floored_cooldown(
        self, db: SimpleNamespace, scene: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scene.setup()
        seen: list[tuple[str, str, float]] = []
        real = card.cooldown_left
        monkeypatch.setattr(card, "cooldown_seconds", lambda instance_key, kind: 12.0 if kind == "image" else 4.0)

        def spy(chat_key: str, kind: str, seconds: float) -> int:
            seen.append((chat_key, kind, seconds))
            return real(chat_key, kind, seconds)

        monkeypatch.setattr(card, "cooldown_left", spy)
        await commands.state_command(_context(GROUP_KEY), "")
        await commands.state_command(_context(GROUP_KEY), "图")

        assert seen == [(GROUP_KEY, "text", 4.0), (GROUP_KEY, "image", 12.0)]


class TestCommandInitialisation:
    async def test_a_template_without_a_row_is_created_on_the_first_view(
        self, db: SimpleNamespace, scene: Any
    ) -> None:
        await _load(db.group, code="seed")
        await DBStateChannel.all().delete()

        first = await commands.state_command(_context(GROUP_KEY), "")
        second = await commands.state_command(_context(GROUP_KEY), "")

        assert "早上" in first.message and "还没有角色状态" not in first.message
        assert "早上" in second.message
        assert await DBStateChannel.filter(chat_key=GROUP_KEY).count() == 1

    async def test_a_book_initvar_is_saved_and_used(self, db: SimpleNamespace, scene: Any) -> None:
        response = await commands.state_command(_context(GROUP_KEY), "")

        template_row = await DBStateTemplate.get(preset_id=db.preset.id)
        assert template_row.source == "initvar"
        assert "早上" in response.message
        assert await DBStateChannel.filter(chat_key=GROUP_KEY).count() == 1

    async def test_reset_creates_the_missing_row_first(self, db: SimpleNamespace, scene: Any) -> None:
        await _load(db.group, code="seed")
        await DBStateChannel.all().delete()

        response = await commands.state_command(_context(GROUP_KEY, "advanced"), "重置")

        assert response.status == CommandResponseStatus.SUCCESS
        row = await DBStateChannel.get(chat_key=GROUP_KEY)
        assert row.data == (await DBStateTemplate.get(preset_id=db.preset.id)).initial


class TestRenderSlots:
    async def test_at_most_two_draw_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(card, "fonts_available", lambda: True)
        running = 0
        peak = 0
        lock = threading.Lock()
        release = threading.Event()

        def slow(_data: Any) -> bytes:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            release.wait(2)
            with lock:
                running -= 1
            return b"png"

        monkeypatch.setattr(card, "render_state_card", slow)
        tasks = [asyncio.create_task(card.render(StateCardData(title="t"))) for _ in range(4)]
        await asyncio.sleep(0.3)
        release.set()
        results = await asyncio.gather(*tasks)

        # 两张在画，另外两个不排队、直接放弃
        assert peak == card.RENDER_SLOTS and results == [b"png", b"png", None, None]

    async def test_gives_up_at_once_when_no_slot_is_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(card, "fonts_available", lambda: True)
        monkeypatch.setattr(card, "render_state_card", lambda _data: b"png")
        slot = card._slot()  # noqa: SLF001
        for _ in range(card.RENDER_SLOTS):
            await slot.acquire()
        try:
            started = time.perf_counter()
            assert await card.render(StateCardData(title="t")) is None
            assert time.perf_counter() - started < 0.05
        finally:
            for _ in range(card.RENDER_SLOTS):
                slot.release()
        assert await card.render(StateCardData(title="t")) == b"png"

    async def test_busy_slots_fall_back_to_text(
        self, db: SimpleNamespace, scene: Any, drawn: list[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scene.setup()
        slot = card._slot()  # noqa: SLF001
        for _ in range(card.RENDER_SLOTS):
            await slot.acquire()
        try:
            response = await commands.state_command(_context(GROUP_KEY), "图")
        finally:
            for _ in range(card.RENDER_SLOTS):
                slot.release()

        assert not response.output_segments and "早上" in response.message and drawn == []

    @pytest.mark.skipif(not fonts_available(), reason="环境里没有中文字体")
    def test_real_render_produces_a_png(self) -> None:
        data = StateCardData(
            title="阿青",
            subtitle="角色状态",
            headline=(StateCardRow("当前时间", "傍晚"), StateCardRow("所在地点", "码头")),
            sections=(StateCardSection("事件", (StateCardRow("", "下雨了"), StateCardRow("心情", "好", 1))),),
            footer="还有 1 组放不下，没画",
            theme="night",
        )

        image = render_state_card(data, scale=1)

        assert image.startswith(b"\x89PNG")


class _Fs:
    async def mixed_forward_file(self, data: bytes, file_name: str) -> str:
        assert data == b"\x89PNG fake"
        return f"/app/uploads/{file_name}"


def _round_ctx(channel: DBChatChannel, code: str = "r1") -> Any:
    return SimpleNamespace(
        one_time_code=code,
        db_chat_channel=channel,
        from_platform_userid="u1",
        from_chat_key=channel.chat_key,
        chat_key=channel.chat_key,
        fs=_Fs(),
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, bool]]:
    seen: list[tuple[str, str, bool]] = []

    async def fake_send(chat_key: str, path: str, _ctx: Any, *, record: bool = True, **_kwargs: Any) -> bool:
        seen.append((chat_key, path, record))
        return True

    monkeypatch.setattr(card.plugin, "send_image", fake_send)
    return seen


class TestAutoSend:
    def test_interval_rules(self) -> None:
        card.reset_for_tests()

        def rounds(mode: str, every: int, floor: int, count: int) -> list[bool]:
            config = SimpleNamespace(STATE_IMAGE_SEND_MODE=mode, STATE_IMAGE_EVERY_N=every, STATE_IMAGE_MIN_N=floor)
            return [card._due(f"{mode}-{every}-{floor}", config) for _ in range(count)]  # noqa: SLF001

        assert rounds("command", 1, 1, 3) == [False, False, False]
        assert rounds("every_reply", 10, 1, 3) == [True, True, True]
        assert rounds("every_n", 3, 1, 6) == [False, False, True, False, False, True]
        assert rounds("every_reply", 10, 3, 3) == [False, False, True]  # 下限也管「每次回复后」
        assert rounds("every_n", 2, 4, 4) == [False, False, False, True]

    async def test_group_images_carry_no_profiles_and_skip_the_history(
        self, db: SimpleNamespace, scene: Any, drawn: list[Any], sent: list[Any]
    ) -> None:
        await scene.setup()
        scene.image_mode = "every_reply"

        await state_main.on_round_finished(_round_ctx(db.group), "error")
        await card.drain()
        assert sent == []

        await state_main.on_round_finished(_round_ctx(db.group), "ok")
        await card.drain()

        assert [(chat_key, record) for chat_key, _path, record in sent] == [(GROUP_KEY, False)]
        shown = "\n".join(_strings(drawn[-1]))
        assert "早上" in shown and not any(outfit in shown for outfit in OUTFITS.values())
        assert CHANNEL_SECRET not in shown

    async def test_private_images_wait_for_the_update_and_carry_the_counterpart(
        self, db: SimpleNamespace, scene: Any, drawn: list[Any], sent: list[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await scene.setup()
        scene.image_mode = "every_reply"
        monkeypatch.setattr(updater, "schedule", lambda chat_key: updater._inflight.add(chat_key))  # noqa: SLF001

        await state_main.on_round_finished(_round_ctx(db.private), "ok")
        await asyncio.sleep(0.8)
        assert sent == []  # 分步更新还在跑
        updater._inflight.discard(PRIVATE_KEY)  # noqa: SLF001
        await card.drain()

        assert [chat_key for chat_key, _path, _record in sent] == [PRIVATE_KEY]
        assert SECRETS["u1"] in "\n".join(_strings(drawn[-1]))

    async def test_failed_rounds_never_send(
        self, db: SimpleNamespace, scene: Any, drawn: list[Any], sent: list[Any]
    ) -> None:
        await scene.setup()
        scene.image_mode = "every_reply"

        for outcome in ("error", "blocked", "cancelled"):
            await card.after_round(_round_ctx(db.group), db.group, outcome)
        await card.drain()

        assert sent == []

    async def test_command_mode_sends_nothing(self, db: SimpleNamespace, scene: Any, sent: list[Any]) -> None:
        await scene.setup()

        for _ in range(3):
            await state_main.on_round_finished(_round_ctx(db.group), "ok")
        await card.drain()

        assert sent == []


class TestCardShape:
    def _template(self, *fields: FieldDef) -> Template:
        return Template(fields=fields)

    def test_headline_heuristic_and_explicit_mark(self) -> None:
        tree = {"当前时间": "傍晚", "所在地点": "码头", "心情": "好", "天气": "雨"}
        guessed = view.card_from_tree(
            self._template(), tree, title="t", subtitle="", is_private=False, affection_enabled=False, private_words=()
        )
        marked = view.card_from_tree(
            self._template(FieldDef(path=("心情",), type="string", headline=True)),
            tree,
            title="t",
            subtitle="",
            is_private=False,
            affection_enabled=False,
            private_words=(),
        )

        assert [row.label for row in guessed.headline] == ["当前时间", "所在地点", "天气"]
        assert [row.label for row in marked.headline] == ["心情"]

    def test_affection_mapped_fields_follow_the_switch(self) -> None:
        template = self._template(FieldDef(path=("好感度",), type="number", affection="score"))
        on = view.card_from_tree(
            template, {"好感度": 50}, title="t", subtitle="", is_private=True, affection_enabled=True, private_words=()
        )
        off = view.card_from_tree(
            template, {"好感度": 50}, title="t", subtitle="", is_private=True, affection_enabled=False, private_words=()
        )

        assert "50" not in str(_strings(on)) and "50" in str(_strings(off))

    def test_lists_and_deep_values_count_only_what_may_be_seen(self) -> None:
        template = self._template(
            FieldDef(path=("日志",), type="array"),
            FieldDef(path=("日志", "*"), type="string"),
            FieldDef(path=("日志", "10"), type="string", private=True),
        )
        tree = {
            "日志": [f"第{index}条" for index in range(12)],
            "档案": {"一": {"二": {"三": {"四": "x", "秘密四": "y", "_藏": "z"}}}},
        }

        data = view.card_from_tree(
            template, tree, title="t", subtitle="", is_private=False, affection_enabled=False, private_words=("秘密",)
        )
        shown = _strings(data)

        assert "…还有 3 条" in shown  # 后四条里第 10 条是私密的，不算
        assert "（1 项）" in shown  # 三个里只有一个看得见

    def test_row_cap_drops_low_priority_groups(self) -> None:
        template = self._template(
            FieldDef(path=("主线",), type="object", priority=5),
            FieldDef(path=("杂项",), type="object", priority=0),
        )
        tree = {
            "主线": {f"k{index}": "v" for index in range(30)},
            "杂项": {f"k{index}": "v" for index in range(30)},
        }

        data = view.card_from_tree(
            template, tree, title="t", subtitle="", is_private=False, affection_enabled=False, private_words=()
        )

        assert [section.title for section in data.sections] == ["主线"]
        assert sum(len(section.rows) for section in data.sections) <= MAX_ROWS
        assert "1 组" in data.footer

    def test_theme_round_trip_and_unknown_theme(self) -> None:
        template = Template.from_json({"fields": [], "theme": "sakura"})
        assert template.theme == "sakura" and Template.from_json(template.to_json()).theme == "sakura"
        assert Template.from_json({"fields": [], "theme": "#ff0000"}).theme == ""
        field = FieldDef.from_json({"path": ["a", "b"], "type": "string", "headline": True})
        assert field.headline is False  # 页头只认顶层字段

    async def test_time_budget_of_the_text_view(self) -> None:
        template = self._template()
        tree = {f"组{group}": {f"键{index}": "值" * 50 for index in range(40)} for group in range(40)}

        started = time.perf_counter()
        data = view.card_from_tree(
            template, tree, title="t", subtitle="", is_private=False, affection_enabled=False, private_words=()
        )
        card_text(data, 1500)

        assert time.perf_counter() - started < 0.5
