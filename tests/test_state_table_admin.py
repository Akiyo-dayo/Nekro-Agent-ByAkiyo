"""角色状态的管理接口（后台页用）

要钉住的事：

1. 作用域：租户只看得到自己实例的频道状态、自成一域的个人档案；实例组域的档案只有平台方看得到（列表里平台方
   另外看到这个实例所在那个组的）；全局人设的模板租户只读；
2. 改动都要带版本号，对不上 409；都写审计行（谁、哪几个路径），审计列表与各列表都不带值；
3. 频道列表的筛选、搜索、分页、频道名、人设名与最近一次结果；状态图预览画的是频道里会看到的那一份；
4. 按书重推可以指定条目；现成模板（场景、物品栏）与已有模板合并，同名的跳过；
5. 按好感的键找档案。
"""

# ruff: noqa: F811 - 固件从同目录的用例模块导入，用例参数与它同名

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_state_table_plugin import (  # type: ignore[import-not-found]
    ADAPTER,
    GROUP_KEY,
    INSTANCE,
    OTHER_INSTANCE,
    _client,
    _load,
    _mark_person_root,
    _scope,
    db,  # noqa: F401 - 固件
)

from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_state_channel import DBStateChannel
from nekro_agent.models.db_state_person import DBStatePerson
from nekro_agent.models.db_state_snapshot import DBStateSnapshot
from nekro_agent.models.db_state_template import DBStateTemplate
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.services.imagecard.state_card import StateCardData
from plugins.builtin.state_table import builtin_templates, card, session, store
from plugins.builtin.state_table.schema import Template


@pytest.fixture(autouse=True)
def _fresh_cooldowns() -> None:
    card.reset_for_tests()


def _strings(data: StateCardData) -> str:
    parts = [data.title, data.subtitle, data.footer]
    parts += [f"{row.label} {row.value}" for row in data.headline]
    for section in data.sections:
        parts.append(section.title)
        parts += [f"{row.label} {row.value}" for row in section.rows]
    return "\n".join(parts)


class TestChannels:
    async def test_list_has_names_last_result_filters_and_pages(self, db: SimpleNamespace) -> None:
        await _load(db.group, code="g")
        await _load(db.private, code="p")
        row = await DBStateChannel.get(chat_key=GROUP_KEY)
        await store.write_snapshot(
            target_kind="channel",
            target_id=row.id,
            chat_key=GROUP_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            source="compat",
            result="partial",
            keep=20,
        )
        await store.write_snapshot(
            target_kind="channel",
            target_id=row.id,
            chat_key=GROUP_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            source="admin",
            result="applied",
            keep=20,
        )
        await DBStateChannel.filter(id=row.id).update(paused=True)

        async with await _client(_scope()) as client:
            listing = (await client.get("/channels")).json()
            paged = (await client.get("/channels", params={"size": 1, "page": 2})).json()
            paused = (await client.get("/channels", params={"paused": "true"})).json()
            by_key = (await client.get("/channels", params={"q": "group_9"})).json()
            by_name = (await client.get("/channels", params={"q": "测试"})).json()
            other_preset = (await client.get("/channels", params={"preset_id": db.global_preset.id})).json()
        async with await _client(_scope(OTHER_INSTANCE)) as client:
            foreign = (await client.get("/channels")).json()

        assert listing["total"] == 2
        group = next(item for item in listing["items"] if item["chat_key"] == GROUP_KEY)
        assert (group["channel_name"], group["preset_name"]) == ("测试", "阿青")
        assert (group["last_source"], group["last_result"]) == ("compat", "partial")  # 审计行不算更新结果
        assert "data" not in group
        assert paged["total"] == 2 and len(paged["items"]) == 1
        assert [item["chat_key"] for item in paused["items"]] == [GROUP_KEY]
        assert [item["chat_key"] for item in by_key["items"]] == [GROUP_KEY]
        assert by_name["total"] == 2
        assert other_preset["items"] == [] and foreign["items"] == []

    async def test_image_preview_is_what_the_channel_would_see(
        self, db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _load(db.group, code="g")
        await _mark_person_root(db.preset.id)
        scopes = await session.resolve_identity_scopes(db.group, ["u1"], preset_id=db.preset.id)
        columns = dict(session.AffectionKey.of_scope(scopes["u1"]).as_columns())
        await DBStatePerson.create(**columns, data={"主角信息": {"穿着": "礼服"}})
        row = await DBStateChannel.get(chat_key=GROUP_KEY)
        drawn: list[StateCardData] = []

        async def fake_render(data: StateCardData) -> bytes:
            drawn.append(data)
            return b"\x89PNG"

        monkeypatch.setattr(card, "render", fake_render)
        async with await _client(_scope()) as client:
            response = await client.get(f"/channels/{row.id}/image")
        async with await _client(_scope(OTHER_INSTANCE)) as client:
            assert (await client.get(f"/channels/{row.id}/image")).status_code == 404

        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        shown = _strings(drawn[0])
        assert "早上" in shown and "没有" not in shown  # 群聊：秘密不画
        assert "礼服" not in shown  # 预览不带任何人的档案

    async def test_image_preview_without_fonts_is_an_error(
        self, db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _load(db.group, code="g")
        row = await DBStateChannel.get(chat_key=GROUP_KEY)

        async def no_image(_data: StateCardData) -> None:
            return None

        monkeypatch.setattr(card, "render", no_image)
        async with await _client(_scope()) as client:
            response = await client.get(f"/channels/{row.id}/image")

        assert response.status_code in (400, 422)

    async def test_image_preview_shares_the_channels_image_cooldown(
        self, db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _load(db.group, code="g")
        row = await DBStateChannel.get(chat_key=GROUP_KEY)
        drawn: list[StateCardData] = []

        async def fake_render(data: StateCardData) -> bytes:
            drawn.append(data)
            return b"\x89PNG"

        monkeypatch.setattr(card, "render", fake_render)
        async with await _client(_scope()) as client:
            first = await client.get(f"/channels/{row.id}/image")
            second = await client.get(f"/channels/{row.id}/image")

        assert first.status_code == 200 and second.status_code in (400, 422)
        assert len(drawn) == 1  # 第二次在冷却里，连画都没画
        # 频道里的「/状态 图」与后台预览用的是同一个冷却
        assert card.cooldown_left(GROUP_KEY, "image", card.cooldown_seconds(INSTANCE, "image")) > 0


async def _people(db: SimpleNamespace) -> dict[str, DBStatePerson]:
    common = {"preset_id": db.preset.id}
    own = await DBStatePerson.create(
        adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="u1", data={"背包": {"苹果": 1}}, **common
    )
    foreign = await DBStatePerson.create(
        adapter_key=ADAPTER, instance_key=OTHER_INSTANCE, platform_userid="u1", data={}, **common
    )
    grouped = await DBStatePerson.create(domain_group_id=7, platform_userid="u2", data={"背包": {}}, **common)
    elsewhere = await DBStatePerson.create(domain_group_id=8, platform_userid="u3", data={}, **common)
    await DBInstanceDomainBinding.create(adapter_key=ADAPTER, instance_key=INSTANCE, memory_group_id=7)
    await DBAffection.create(
        adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="u1", preset_id=db.preset.id, nickname="小林"
    )
    return {"own": own, "foreign": foreign, "grouped": grouped, "elsewhere": elsewhere}


class TestPeople:
    async def test_list_scope_nicknames_and_filters(self, db: SimpleNamespace) -> None:
        rows = await _people(db)

        async with await _client(_scope()) as client:
            tenant = (await client.get("/people")).json()
            by_uid = (await client.get("/people", params={"uid": "u9"})).json()
        async with await _client(_scope(platform=True)) as client:
            platform = (await client.get("/people")).json()

        assert [item["id"] for item in tenant["items"]] == [rows["own"].id]
        assert tenant["items"][0]["nickname"] == "小林" and "data" not in tenant["items"][0]
        assert by_uid["items"] == []
        assert sorted(item["id"] for item in platform["items"]) == sorted([rows["own"].id, rows["grouped"].id])

    async def test_lookup_by_affection_key(self, db: SimpleNamespace) -> None:
        rows = await _people(db)
        own = {"preset_id": db.preset.id, "uid": "u1"}
        grouped = {"preset_id": db.preset.id, "uid": "u2", "domain_group_id": 7}

        async with await _client(_scope()) as client:
            assert (await client.get("/people/lookup", params=own)).json() == {"id": rows["own"].id}
            assert (await client.get("/people/lookup", params=grouped)).status_code == 404
            assert (await client.get("/people/lookup", params={**own, "uid": "u9"})).status_code == 404
        async with await _client(_scope(platform=True)) as client:
            assert (await client.get("/people/lookup", params=grouped)).json() == {"id": rows["grouped"].id}

    async def test_edit_snapshot_and_rollback(self, db: SimpleNamespace) -> None:
        rows = await _people(db)
        person = rows["own"]

        async with await _client(_scope()) as client:
            stale = await client.put(f"/people/{person.id}", json={"data": {"背包": {}}, "version": person.version + 3})
            first = await client.put(
                f"/people/{person.id}", json={"data": {"背包": {"梨": 2}}, "version": person.version}
            )
            snapshot_id = (await client.get(f"/people/{person.id}")).json()["snapshots"][0]["id"]
            await client.put(f"/people/{person.id}", json={"data": {"背包": {}}, "version": first.json()["version"]})
            current = (await client.get(f"/people/{person.id}")).json()
            snapshot = (await client.get(f"/people/{person.id}/snapshots/{snapshot_id}")).json()
            rolled = await client.post(
                f"/people/{person.id}/rollback/{snapshot_id}", json={"version": current["version"]}
            )
            assert (
                await client.put(f"/people/{rows['grouped'].id}", json={"data": {}, "version": 1})
            ).status_code == 404
            assert (await client.get(f"/people/{rows['foreign'].id}/snapshots/{snapshot_id}")).status_code == 404

        assert stale.status_code == 409 and first.status_code == 200 and rolled.status_code == 200
        assert snapshot["data"] == {"背包": {"梨": 2}}
        assert (await DBStatePerson.get(id=person.id)).data == {"背包": {"梨": 2}}
        sources = await DBStateSnapshot.filter(target_kind="person", target_id=person.id).values_list(
            "source", flat=True
        )
        assert sorted(sources) == ["admin", "admin", "rollback"]
        audit = await DBStateSnapshot.filter(target_kind="person", target_id=person.id, source="admin").first()
        assert (audit.instance_key, audit.editor) == (INSTANCE, "管理员")
        assert audit.ops_applied == [{"op": "admin", "path": "/背包"}]

    async def test_platform_may_edit_group_profiles(self, db: SimpleNamespace) -> None:
        rows = await _people(db)

        async with await _client(_scope(platform=True)) as client:
            response = await client.put(
                f"/people/{rows['grouped'].id}", json={"data": {"背包": {"伞": 1}}, "version": 1}
            )

        assert response.status_code == 200
        audit = await DBStateSnapshot.get(target_kind="person", target_id=rows["grouped"].id)
        assert audit.instance_key == ""  # 记在档案自己的域上


class TestTemplates:
    async def test_list_initvar_entries_and_entry_choice(self, db: SimpleNamespace) -> None:
        other_instance_preset = await DBPreset.create(
            name="别人的",
            title="别人的",
            avatar="",
            content="",
            description="",
            tags="",
            author="t",
            owner_instance_key=OTHER_INSTANCE,
        )
        second = await DBWbEntry.create(
            book=db.book,
            uid=3,
            comment="[initvar]第二份",
            keys=[],
            secondary_keys=[],
            selective_logic="and_any",
            use_regex=False,
            constant=False,
            content="只有这一项: 1\n",
            order=50,
            position="before_history",
            depth=4,
            enabled=True,
            trusted=True,
            status="active",
            ext={},
        )

        async with await _client(_scope()) as client:
            listing = (await client.get("/templates")).json()
            entries = (await client.get(f"/templates/{db.preset.id}/initvar-entries")).json()
            chosen = await client.post(
                f"/templates/{db.preset.id}/from-initvar", json={"version": 0, "entry_id": second.id}
            )
            missing = await client.post(
                f"/templates/{db.preset.id}/from-initvar", json={"version": chosen.json()["version"], "entry_id": 999}
            )

        names = {item["preset_name"]: item for item in listing["items"]}
        assert set(names) == {"阿青", "全局"} and other_instance_preset.name not in names
        assert names["阿青"]["writable"] and not names["全局"]["writable"] and names["全局"]["global"]
        assert [item["title"] for item in entries["items"]] == ["[initvar]初始变量", "[initvar]第二份"]
        assert all("content" not in item for item in entries["items"])
        assert chosen.status_code == 200 and missing.status_code == 404
        row = await DBStateTemplate.get(preset_id=db.preset.id)
        assert row.initial == {"只有这一项": 1} and row.source_entry_id == second.id

    async def test_builtin_templates_create_and_merge(self, db: SimpleNamespace) -> None:
        async with await _client(_scope()) as client:
            stale = await client.post(f"/templates/{db.preset.id}/builtin", json={"kinds": ["scene"], "version": 4})
            created = (
                await client.post(f"/templates/{db.preset.id}/builtin", json={"kinds": ["scene"], "version": 0})
            ).json()
            again = (
                await client.post(
                    f"/templates/{db.preset.id}/builtin", json={"kinds": ["scene"], "version": created["version"]}
                )
            ).json()
            merged = (
                await client.post(
                    f"/templates/{db.preset.id}/builtin", json={"kinds": ["inventory"], "version": created["version"]}
                )
            ).json()
            unknown = await client.post(f"/templates/{db.preset.id}/builtin", json={"kinds": ["map"], "version": 0})
            global_denied = await client.post(
                f"/templates/{db.global_preset.id}/builtin", json={"kinds": ["scene"], "version": 0}
            )

        assert created["added"] == ["scene"] and again == {
            "preset_id": db.preset.id,
            "version": created["version"],
            "added": [],
            "skipped": ["scene"],
        }
        assert merged["added"] == ["inventory"]
        assert stale.status_code == 409 and unknown.status_code in (400, 422) and global_denied.status_code == 403
        row = await DBStateTemplate.get(preset_id=db.preset.id)
        template = Template.from_json(row.field_defs)
        assert row.source == "manual" and template.person_roots() == ("背包",)
        assert template.match(("故事时间",)).headline  # type: ignore[union-attr]
        assert row.initial == {"故事时间": "", "所在地点": "", "环境": "", "背包": {}}
        audits = await DBStateSnapshot.filter(target_kind="template").values_list("ops_applied", flat=True)
        assert [{"op": "template", "path": "builtin:scene"}] in audits

    def test_merge_keeps_existing_fields(self) -> None:
        existing = Template.from_json({"fields": [{"path": ["环境"], "type": "number"}]})

        merged = builtin_templates.merge(existing, {"环境": 3}, ["scene", "inventory"])

        assert merged.added == ("inventory",) and merged.skipped == ("scene",)
        assert merged.template.match(("环境",)).type == "number"  # type: ignore[union-attr]
        assert merged.initial == {"环境": 3, "背包": {}}


class TestAudit:
    async def test_audit_list_has_who_and_paths_but_no_values(self, db: SimpleNamespace) -> None:
        await _load(db.group, code="g")
        row = await DBStateChannel.get(chat_key=GROUP_KEY)
        await store.write_snapshot(
            target_kind="channel",
            target_id=row.id,
            chat_key=GROUP_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            source="update",
            result="applied",
            keep=20,
        )  # 普通的更新记录不是审计

        async with await _client(_scope()) as client:
            await client.put(
                f"/channels/{row.id}", json={"data": {**row.data, "当前时间": "中午"}, "version": row.version}
            )
            await client.post(f"/channels/{row.id}/reset")
            everything = (await client.get("/audit")).json()
            resets = (await client.get("/audit", params={"source": "reset"})).json()
            templates = (await client.get("/audit", params={"kind": "template"})).json()
            bad = await client.get("/audit", params={"kind": "anything"})
        async with await _client(_scope(OTHER_INSTANCE)) as client:
            foreign = (await client.get("/audit")).json()

        assert everything["total"] == 2
        assert {item["source"] for item in everything["items"]} == {"admin", "reset"}
        edit = next(item for item in everything["items"] if item["source"] == "admin")
        assert edit["ops"] == [{"op": "admin", "path": "/当前时间"}] and edit["editor"] == "管理员"
        assert all("data" not in item for item in everything["items"])
        assert "中午" not in str(everything)
        assert [item["source"] for item in resets["items"]] == ["reset"]
        assert templates["items"] == [] and bad.status_code == 422 and foreign["items"] == []

    async def test_platform_sees_audit_rows_of_its_groups_profiles(self, db: SimpleNamespace) -> None:
        rows = await _people(db)

        async with await _client(_scope(platform=True)) as client:
            for key in ("grouped", "elsewhere"):
                response = await client.put(f"/people/{rows[key].id}", json={"data": {"背包": {"伞": 1}}, "version": 1})
                assert response.status_code == 200
            platform = (await client.get("/audit")).json()
            people_only = (await client.get("/audit", params={"kind": "person"})).json()
            channels_only = (await client.get("/audit", params={"kind": "channel"})).json()
        async with await _client(_scope()) as client:
            tenant = (await client.get("/audit")).json()

        # 这个实例所在的组（7）的档案看得到，别的组（8）的看不到；租户都看不到
        assert [item["target_id"] for item in platform["items"]] == [rows["grouped"].id]
        assert [item["target_id"] for item in people_only["items"]] == [rows["grouped"].id]
        assert channels_only["items"] == [] and tenant["items"] == []
        assert "伞" not in str(platform)
