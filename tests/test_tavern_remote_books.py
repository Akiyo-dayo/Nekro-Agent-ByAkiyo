"""卡片脚本要下载的世界书：认出来源、酒馆助手格式的转换、下载与导入

样本全是现写的小脚本、小书，不取自任何真实卡片。网络一律换掉（`common_util.fetch_url_bytes`）；
下载守卫走代理时只 `CONNECT` 校验过的地址，另有用例在 `test_tenant_egress.py` 里走到建连接那一层。
要钉住的事：

- 只认会改写世界书的脚本里写死的完整地址，同一个文件名的几个地址算一个来源（主地址在前）；
- 酒馆助手条目照它自己的 `fromWorldbookEntry` 摊平成酒馆条目，认不出的值记进报告而不是悄悄改掉；
- 导入的条目一律不可信（平台方导入也一样），书绑在人设上、归属跟作用域走；
- 已经导入过要明确替换；替换在一个事务里换掉全部条目，书的设置不动；
- 失败原因逐个地址写主机名，不带路径与查询串，「解析不到」与「指向内网」说同一句话；
- 作用域：租户看不到、动不了别的实例的人设，全局人设对租户只读；`key` 只按服务端重新认出的来源找。
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from typing import Any, AsyncIterator, Optional

import httpx
import pytest
from starlette.datastructures import UploadFile
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.routers import tavern
from nekro_agent.schemas.errors import (
    NotFoundError,
    PermissionDeniedError,
    RemoteWorldbookBusyError,
    RemoteWorldbookDownloadError,
    RemoteWorldbookImportedError,
    ValidationError,
)
from nekro_agent.schemas.tavern import RemoteBookCheckRequest, RemoteBookDownloadRequest
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.tavern import remote_books
from nekro_agent.services.user.role import Role
from nekro_agent.services.worldbook import st_format
from nekro_agent.tools.common_util import UnresolvableDownloadHostError, UnsafeDownloadUrlError

ADAPTER = "onebot_v11"
FILE = "demo-common-worldbook.json"
PRIMARY = f"https://cdn.example.test/gh/someone/repo@v1.2.0/{FILE}"
MIRROR = f"https://gcore.example.test/gh/someone/repo@v1.2.0/{FILE}"
GIST = f"https://gist.example.test/someone/abc/raw/{FILE}?token=secret-ish"
KEY = remote_books.source_key(FILE)


def _script(urls: list[str], *, writes: bool = True, name: str = "设定同步器") -> dict[str, Any]:
    quoted = ",".join(json.dumps(url) for url in urls)
    call = "await createOrReplaceWorldbook(name, entries);" if writes else "console.log(1);"
    return {"name": name, "type": "script", "enabled": True, "content": f"const SOURCES=[{quoted}];{call}"}


def _card(scripts: list[dict[str, Any]], *, name: str = "测试角色") -> dict[str, Any]:
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": name,
            "description": "描述",
            "first_mes": "开场",
            "extensions": {"tavern_helper": {"scripts": scripts}},
        },
    }


def _th_entry(uid: int, content: str = "设定正文", **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "uid": uid,
        "name": f"条目{uid}",
        "enabled": True,
        "strategy": {
            "type": "selective",
            "keys": [f"关键词{uid}"],
            "keys_secondary": {"logic": "and_any", "keys": []},
            "scan_depth": "same_as_global",
        },
        "position": {"type": "before_character_definition", "role": "system", "depth": 4, "order": 100},
        "content": content,
        "probability": 100,
        "recursion": {"prevent_incoming": False, "prevent_outgoing": True, "delay_until": None},
        "effect": {"sticky": None, "cooldown": None, "delay": None},
    }
    entry.update(fields)
    return entry


def _th_book(entries: list[dict[str, Any]], *, version: str = "1.0.0", book_name: str = "通用设定") -> bytes:
    payload = {
        "schema": "tavern-helper-worldbook-sync/v1",
        "id": "demo-common",
        "book_name": book_name,
        "version": version,
        "entries": entries,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# 认出
# ---------------------------------------------------------------------------


class TestDetect:
    def test_mirrors_of_one_file_are_one_source(self) -> None:
        release = "https://cdn.example.test/gh/someone/repo@main/release.json"
        sources = remote_books.detect_remote_books(
            _card([_script([PRIMARY, release, MIRROR, GIST])])["data"]["extensions"]
        )

        assert len(sources) == 1
        source = sources[0]
        assert source.file_name == FILE
        assert source.urls == [PRIMARY, MIRROR, GIST]  # 主地址在前，release.json 不是书
        assert source.version_hint == "v1.2.0"
        assert source.scripts == ["设定同步器"]
        assert source.key == KEY == remote_books.source_key(FILE.upper())

    def test_scripts_that_do_not_write_worldbooks_are_ignored(self) -> None:
        extensions = _card([_script([PRIMARY], writes=False)])["data"]["extensions"]
        assert remote_books.detect_remote_books(extensions) == []

    def test_built_addresses_and_other_files_are_ignored(self) -> None:
        content = (
            "const base='https://cdn.example.test/gh/a/b@main';"
            "fetch(`${base}/x-worldbook.json`);"  # 拼出来的地址：认不出，也不去认
            "fetch('https://cdn.example.test/data.json');"  # 文件名里没有世界书字样
            "fetch('https://cdn.example.test/x-worldbook.js');"
            "fetch('ftp://cdn.example.test/x-worldbook.json');"
            "await replaceWorldbook(n, e);"
        )
        extensions = {"tavern_helper": {"scripts": [{"name": "s", "content": content}]}}
        assert remote_books.detect_remote_books(extensions) == []

    def test_folders_legacy_location_and_several_files(self) -> None:
        other = "https://cdn.example.test/gh/someone/repo@main/second_lorebook.json"
        extensions = {
            "tavern_helper": {"scripts": [{"name": "文件夹", "scripts": [_script([PRIMARY], name="甲")]}]},
            "TavernHelper_scripts": [_script([other, MIRROR], name="乙")],
        }
        sources = remote_books.detect_remote_books(extensions)

        assert [source.file_name for source in sources] == [FILE, "second_lorebook.json"]
        assert sources[0].urls == [PRIMARY, MIRROR]
        assert sources[0].scripts == ["甲", "乙"]
        assert sources[1].version_hint == "main"
        assert sources[0].key != sources[1].key

    def test_caps(self) -> None:
        many_files = [f"https://cdn.example.test/f{index}-worldbook.json" for index in range(20)]
        assert (
            len(remote_books.detect_remote_books({"TavernHelper_scripts": [_script(many_files)]}))
            == remote_books.MAX_SOURCES
        )
        mirrors = [f"https://m{index}.example.test/{FILE}" for index in range(20)]
        [source] = remote_books.detect_remote_books({"TavernHelper_scripts": [_script(mirrors)]})
        assert len(source.urls) == remote_books.MAX_URLS_PER_SOURCE

    def test_card_raw_extensions(self) -> None:
        card = _card([_script([PRIMARY])])
        assert remote_books.card_raw_extensions(card) is card["data"]["extensions"]
        assert remote_books.card_raw_extensions({"name": "V1 卡", "first_mes": "x"}) == {}
        assert remote_books.card_raw_extensions("不是对象") == {}

    def test_long_scripts_stay_linear(self) -> None:
        content = "x" * 400_000 + "https://cdn.example.test/" + "a/" * 100_000 + "createWorldbook(1)"
        started = time.monotonic()
        remote_books.detect_remote_books({"TavernHelper_scripts": [{"name": "长", "content": content}]})
        assert time.monotonic() - started < 2


# ---------------------------------------------------------------------------
# 酒馆助手格式
# ---------------------------------------------------------------------------


class TestTavernHelperFormat:
    def test_entry_flattens_like_tavern_helper(self) -> None:
        entry = _th_entry(
            7,
            strategy={
                "type": "constant",
                "keys": ["甲", "/乙/i"],
                "keys_secondary": {"logic": "not_all", "keys": ["丙"]},
                "scan_depth": 2,
            },
            position={"type": "at_depth", "role": "assistant", "depth": 3, "order": 104},
            probability=60,
            recursion={"prevent_incoming": True, "prevent_outgoing": False, "delay_until": 2},
            effect={"sticky": 3, "cooldown": None, "delay": 1},
            extra={"note": "自带的附加字段"},
            group="组",
            characterFilter={"names": ["某人"]},
            context={"scope": {"kind": "脚本自己的"}},
        )
        raw = st_format.tavern_helper_entry_to_st(entry, 5)

        expected_core = {
            "uid": 7,
            "displayIndex": 5,
            "comment": "条目7",
            "disable": False,
            "constant": True,
            "selective": False,
            "key": ["甲", "/乙/i"],
            "selectiveLogic": 1,
            "keysecondary": ["丙"],
            "scanDepth": 2,
            "vectorized": False,
            "position": 4,
            "role": 2,
            "depth": 3,
            "order": 104,
            "content": "设定正文",
            "useProbability": True,
            "probability": 60,
            "excludeRecursion": True,
            "preventRecursion": False,
            "delayUntilRecursion": 2,
            "sticky": 3,
            "cooldown": None,
            "delay": 1,
            "extra": {"note": "自带的附加字段"},
            "group": "组",
            "characterFilter": {"isExclude": False, "names": ["某人"], "tags": []},
        }
        assert {key: raw[key] for key in expected_core} == expected_core
        assert raw["addMemo"] is True and raw["groupWeight"] == 100  # 酒馆助手的隐含默认值
        assert "context" not in raw  # 只取它认的字段，与酒馆助手自己的转换一致

    def test_defaults_follow_tavern_helper(self) -> None:
        raw = st_format.tavern_helper_entry_to_st({"content": "只有正文"}, 0)
        assert "uid" not in raw  # 交给导入时重新编号
        assert raw["constant"] is True  # 没写类型时按常驻
        assert raw["selective"] is False
        assert (raw["position"], raw["role"], raw["depth"], raw["order"]) == (4, 0, 4, 100)
        assert raw["key"] == [] and raw["keysecondary"] == [] and raw["selectiveLogic"] == 0
        assert raw["scanDepth"] is None and raw["delayUntilRecursion"] is False
        assert raw["disable"] is False and raw["comment"] == ""

        columns, _notes = st_format.st_entry_to_columns(
            st_format.tavern_helper_entry_to_st(_th_entry(1, enabled=False), 0),
        )
        assert columns["enabled"] is False
        assert columns["constant"] is False
        assert columns["position"] == "after_persona"

    def test_unknown_values_are_reported_not_hidden(self) -> None:
        raw = st_format.tavern_helper_entry_to_st(
            _th_entry(
                1,
                position={"type": "somewhere_else"},
                strategy={"type": "selective", "keys_secondary": {"logic": "xor"}},
            ),
            0,
        )
        _columns, notes = st_format.st_entry_to_columns(raw)
        kinds = {(note.field, note.kind) for note in notes}
        assert ("position", "position_downgraded") in kinds
        assert ("selectiveLogic", "bad_value") in kinds

    def test_numeric_keys_become_strings(self) -> None:
        raw = st_format.tavern_helper_entry_to_st(
            _th_entry(1, strategy={"type": "selective", "keys": [12, 3.0, "甲", True, {"x": 1}]}),
            0,
        )
        assert raw["key"][:4] == ["12", "3", "甲", "true"]
        columns, notes = st_format.st_entry_to_columns(raw)
        assert columns["keys"] == ["12", "3", "甲", "true"]
        assert any(note.kind == "skipped" and note.field == "key" for note in notes)

    def test_payload_shapes(self) -> None:
        parsed = st_format.parse_world_payload(_th_book([_th_entry(1), _th_entry(2), "不是对象"]))
        assert [key for key, _raw in parsed.entries] == ["1", "2", "#3"]
        assert parsed.entries[2][1] == "不是对象"  # 导入时整条跳过并记一条说明
        assert parsed.book_ext["version"] == "1.0.0"
        assert parsed.book_ext["book_name"] == "通用设定"
        assert parsed.book_ext[st_format.SOURCE_FORMAT_KEY] == st_format.TAVERN_HELPER_FORMAT

        bare = st_format.parse_world_payload(json.dumps([_th_entry(4)]).encode())
        assert [key for key, _raw in bare.entries] == ["4"]

        st_file = json.dumps({"entries": {"0": {"uid": 0, "key": ["甲"], "content": "x"}}, "name": "书"}).encode()
        assert st_format.parse_world_payload(st_file) == st_format.parse_world_file(st_file)

    @pytest.mark.parametrize(
        "payload",
        [
            {"entries": "不是对象也不是数组"},
            {"entries": []},
            {"entries": [{"key": ["甲"], "content": "酒馆格式却放成了数组"}]},
            "就是一个字符串",
        ],
    )
    def test_unrecognised_payloads_are_rejected(self, payload: Any) -> None:
        with pytest.raises(st_format.WorldFileRejected):
            st_format.parse_world_payload(json.dumps(payload, ensure_ascii=False).encode())

    def test_limits_still_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        too_long = "长" * (st_format.MAX_COMMENT_CHARS + 1)
        with pytest.raises(st_format.WorldFileRejected, match="第 2 条"):
            st_format.parse_world_payload(_th_book([_th_entry(1), _th_entry(2, name=too_long)]))
        monkeypatch.setattr(st_format, "MAX_ENTRIES", 2)
        with pytest.raises(st_format.WorldFileRejected, match="安全上限"):
            st_format.parse_world_payload(_th_book([_th_entry(index) for index in range(3)]))


# ---------------------------------------------------------------------------
# 失败原因
# ---------------------------------------------------------------------------


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", PRIMARY)
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))


class TestFailureReasons:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (UnsafeDownloadUrlError("下载内容超过大小上限: >8388608 bytes"), "超过 8 MiB"),
            (httpx.ConnectTimeout("x"), "超时"),
            (TimeoutError(), "超时"),  # asyncio.timeout 与下载守卫等响应头的时限
            (_status_error(404), "HTTP 404"),
            (httpx.ProxyError("x"), "代理"),
            (httpx.ConnectError("x"), "连不上"),
            (st_format.WorldFileRejected("不是合法的 JSON"), "不是能导入的世界书"),
        ],
    )
    def test_reasons(self, error: Exception, expected: str) -> None:
        assert expected in remote_books._failure_reason(error)  # noqa: SLF001

    def test_unresolvable_and_private_say_the_same_thing(self) -> None:
        unresolvable = remote_books._failure_reason(UnresolvableDownloadHostError("主机名解析失败: x"))  # noqa: SLF001
        private = remote_books._failure_reason(UnsafeDownloadUrlError("下载链接指向内网地址"))  # noqa: SLF001
        assert unresolvable == private


# ---------------------------------------------------------------------------
# 下载与导入（真表）
# ---------------------------------------------------------------------------


def _platform_user() -> DBUser:
    return DBUser(
        id=1,
        username="platform",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def _tenant_user() -> DBUser:
    return DBUser(
        id=21,
        username="tenant-admin",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        tenant_id=1,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


async def _preset_with_card(name: str, owner_instance_key: str, card: dict[str, Any]) -> DBPreset:
    preset = await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content="正文",
        description="",
        tags="",
        author="测试",
        owner_instance_key=owner_instance_key,
    )
    await DBPresetCard.create(preset=preset, card_spec="v2", macros_enabled=False, card_raw=card)
    return preset


class FakeNet:
    """按地址给结果：字节串就是下载到的内容，异常就抛，协程函数就先等它（模拟慢服务器或下载期间别处的改动）；
    记下每次调用的地址与代理，以及是不是按租户口径"""

    def __init__(self) -> None:
        self.answers: dict[str, Any] = {}
        self.calls: list[tuple[str, Optional[str]]] = []
        self.tenant_flags: list[bool] = []

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: Optional[int] = None,
        timeout: Any = None,
        proxy: Optional[str] = None,
        tenant: bool = False,
    ) -> bytes:
        self.calls.append((url, proxy))
        self.tenant_flags.append(tenant)
        assert max_bytes == remote_books.REMOTE_BOOK_MAX_BYTES
        answer = self.answers.get(url, httpx.ConnectError("没配"))
        if callable(answer):
            answer = await answer()
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    net = FakeNet()
    remote_books.clear_source_cache()
    monkeypatch.setattr(remote_books.common_util, "fetch_url_bytes", net.fetch)
    monkeypatch.setattr(config, "DEFAULT_PROXY", "")
    monkeypatch.setattr(config, "DEFAULT_PROXY_USERNAME", "")
    monkeypatch.setattr(config, "DEFAULT_PROXY_PASSWORD", "")
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, tenant_id in (("t1a", 1), ("t2a", 2)):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                tenant_id=tenant_id,
                provider_account_id=f"bot_{instance_key}",
            )
        card = _card([_script([PRIMARY, MIRROR, GIST])])
        yield {
            "net": net,
            "platform": _platform_user(),
            "tenant": _tenant_user(),
            "global": await _preset_with_card("全局人设", "", card),
            "t1a": await _preset_with_card("租户一人设", "t1a", card),
            "t2a": await _preset_with_card("租户二人设", "t2a", card),
        }
    finally:
        await Tortoise.close_connections()


async def _download(world: dict[str, Any], who: str = "platform", preset: str = "global", **kwargs: Any) -> Any:
    instance = {"platform": ("", ""), "tenant": ("t1a", ADAPTER)}[who]
    return await tavern.download_remote_book(
        preset_id=world[preset].id,
        key=kwargs.pop("key", KEY),
        req=RemoteBookDownloadRequest(**kwargs),
        instance_key=instance[0],
        adapter_key=instance[1],
        _current_user=world[who],
    )


class TestDownloadAndImport:
    async def test_platform_import_is_untrusted_and_bound_to_the_preset(self, world: dict[str, Any]) -> None:
        net: FakeNet = world["net"]
        net.answers[PRIMARY] = httpx.ConnectTimeout("慢")
        net.answers[MIRROR] = _th_book([_th_entry(1), _th_entry(2), _th_entry(3, strategy={"type": "constant"})])

        result = await _download(world)

        assert [url for url, _proxy in net.calls] == [PRIMARY, MIRROR]  # 主地址超时，换镜像
        assert (result.host, result.version, result.manual, result.replaced) == (
            "gcore.example.test",
            "1.0.0",
            False,
            False,
        )
        assert result.report.created == 3
        book = await DBWbBook.get(id=result.book_id)
        assert (book.scope_kind, book.preset_id, book.adapter_key, book.instance_key) == (
            "preset",
            world["global"].id,
            "",
            "",
        )
        assert book.name == "通用设定"
        assert "gcore.example.test" in book.description
        assert book.scan_names is True, "新导入的酒馆书默认扫描时带上说话人名字"
        entries = await DBWbEntry.filter(book_id=book.id).all()
        assert len(entries) == 3
        assert all(entry.trusted is False for entry in entries), "来自网上的文件，平台方导入也不可信"
        info = book.ext[remote_books.REMOTE_SOURCE_KEY]
        assert info["key"] == KEY and info["url"] == MIRROR and info["version"] == "1.0.0" and info["manual"] is False
        assert len(info["sha256"]) == 64 and info["size"] > 0
        assert book.ext["id"] == "demo-common"  # 文件顶层原样留着

        listing = await tavern.list_remote_books(
            preset_id=world["global"].id, instance_key="", adapter_key="", _current_user=world["platform"]
        )
        [view] = listing.sources
        assert view.urls == [PRIMARY, MIRROR, GIST]
        assert view.imported is not None
        assert (view.imported.book_id, view.imported.entries, view.imported.host) == (book.id, 3, "gcore.example.test")

    async def test_tenant_import_belongs_to_the_instance(self, world: dict[str, Any]) -> None:
        world["net"].answers[PRIMARY] = _th_book([_th_entry(1)])
        result = await _download(world, "tenant", "t1a")
        book = await DBWbBook.get(id=result.book_id)
        assert (book.adapter_key, book.instance_key, book.preset_id) == (ADAPTER, "t1a", world["t1a"].id)

    async def test_second_import_needs_replace_and_replace_swaps_entries(self, world: dict[str, Any]) -> None:
        net: FakeNet = world["net"]
        net.answers[PRIMARY] = _th_book([_th_entry(1), _th_entry(2)])
        first = await _download(world)
        book = await DBWbBook.get(id=first.book_id)
        book.name = "管理员自己改的名字"
        book.budget_tokens = 321
        await book.save()
        old_ids = set(await DBWbEntry.filter(book_id=book.id).values_list("id", flat=True))

        with pytest.raises(RemoteWorldbookImportedError):
            await _download(world)

        net.answers[PRIMARY] = _th_book([_th_entry(9, content="新版本的正文")], version="2.0.0")
        second = await _download(world, replace=True)

        assert (second.book_id, second.replaced, second.version) == (book.id, True, "2.0.0")
        await book.refresh_from_db()
        assert (book.name, book.budget_tokens) == ("管理员自己改的名字", 321)
        assert book.ext[remote_books.REMOTE_SOURCE_KEY]["version"] == "2.0.0"
        entries = await DBWbEntry.filter(book_id=book.id).all()
        assert [entry.content for entry in entries] == ["新版本的正文"]
        assert not old_ids & {entry.id for entry in entries}
        assert all(entry.trusted is False for entry in entries)
        assert await DBWbBook.filter(preset_id=world["global"].id).count() == 1

    async def test_check_update(self, world: dict[str, Any]) -> None:
        net: FakeNet = world["net"]
        check = RemoteBookCheckRequest()

        async def run() -> Any:
            return await tavern.check_remote_book(
                preset_id=world["global"].id,
                key=KEY,
                req=check,
                instance_key="",
                adapter_key="",
                _current_user=world["platform"],
            )

        net.answers[PRIMARY] = _th_book([_th_entry(1)])
        first = await run()
        assert (first.status, first.remote_version) == ("not_imported", "1.0.0")
        await _download(world)
        assert (await run()).status == "same"
        net.answers[PRIMARY] = _th_book([_th_entry(1, content="改了一个字")])
        assert (await run()).status == "changed"
        net.answers[PRIMARY] = _th_book([_th_entry(1)], version="1.1.0")
        newer = await run()
        assert (newer.status, newer.local_version, newer.remote_version) == ("newer", "1.0.0", "1.1.0")
        assert await DBWbEntry.all().count() == 1, "检查更新不导入"

    async def test_failures_list_each_host_without_paths(self, world: dict[str, Any]) -> None:
        net: FakeNet = world["net"]
        net.answers[PRIMARY] = UnresolvableDownloadHostError("主机名解析失败")
        net.answers[MIRROR] = b"<html>not json</html>"
        net.answers[GIST] = _status_error(403)

        with pytest.raises(RemoteWorldbookDownloadError) as caught:
            await _download(world)

        message = str(caught.value.params["reason"])
        for host in ("cdn.example.test", "gcore.example.test", "gist.example.test"):
            assert host in message
        assert "HTTP 403" in message and "不是能导入的世界书" in message
        assert "/gh/" not in message and "token" not in message
        assert await DBWbBook.filter(preset_id=world["global"].id).count() == 0

    async def test_a_mirror_serving_something_else_falls_through(self, world: dict[str, Any]) -> None:
        net: FakeNet = world["net"]
        net.answers[PRIMARY] = json.dumps({"entries": "x"}).encode()
        net.answers[MIRROR] = _th_book([_th_entry(1)])
        result = await _download(world)
        assert result.host == "gcore.example.test"

    async def test_proxy_is_optional_and_needs_configuration(
        self, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        net: FakeNet = world["net"]
        with pytest.raises(RemoteWorldbookDownloadError, match="DEFAULT_PROXY"):
            await _download(world, use_proxy=True)
        assert net.calls == []

        monkeypatch.setattr(config, "DEFAULT_PROXY", "http://127.0.0.1:7890")
        monkeypatch.setattr(config, "DEFAULT_PROXY_USERNAME", "user")
        monkeypatch.setattr(config, "DEFAULT_PROXY_PASSWORD", "pass")
        net.answers[PRIMARY] = _th_book([_th_entry(1)])
        await _download(world, use_proxy=True)
        assert net.calls == [(PRIMARY, "http://user:pass@127.0.0.1:7890")]

        listing = await tavern.list_remote_books(
            preset_id=world["global"].id, instance_key="", adapter_key="", _current_user=world["platform"]
        )
        assert listing.proxy_available is True

    async def test_one_download_per_tenant(self, world: dict[str, Any]) -> None:
        world["net"].answers[PRIMARY] = _th_book([_th_entry(1)])
        with remote_books._download_slot("platform"):  # noqa: SLF001 - 模拟同一个租户已有一个在下载
            with pytest.raises(RemoteWorldbookBusyError):
                await _download(world)
        assert (await _download(world)).report.created == 1

    async def test_manual_upload(self, world: dict[str, Any]) -> None:
        st_file = json.dumps(
            {"entries": {"0": {"uid": 0, "key": ["甲"], "content": "手动上传的正文"}}}, ensure_ascii=False
        )

        async def upload(data: bytes, replace: bool = False) -> Any:
            return await tavern.upload_remote_book(
                preset_id=world["t1a"].id,
                key=KEY,
                file=UploadFile(file=io.BytesIO(data), filename="book.json"),
                replace=replace,
                instance_key="t1a",
                adapter_key=ADAPTER,
                _current_user=world["tenant"],
            )

        with pytest.raises(RemoteWorldbookDownloadError, match="不是能导入的世界书"):
            await upload(b"not json")
        result = await upload(st_file.encode("utf-8"))
        assert (result.manual, result.host, result.version) == (True, "", "")
        book = await DBWbBook.get(id=result.book_id)
        assert book.ext[remote_books.REMOTE_SOURCE_KEY]["manual"] is True
        assert [entry.trusted for entry in await DBWbEntry.filter(book_id=book.id)] == [False]
        assert world["net"].calls == []

        with pytest.raises(RemoteWorldbookImportedError):
            await upload(st_file.encode("utf-8"))
        assert (await upload(_th_book([_th_entry(1), _th_entry(2)]), replace=True)).report.created == 2


class TestTenantRulesAndLimits:
    """租户口径的出站判据、系统代理只给平台方、时限、替换时重新取书、认出来源的缓存"""

    async def test_tenant_downloads_use_the_tenant_rules(self, world: dict[str, Any]) -> None:
        net: FakeNet = world["net"]
        net.answers[PRIMARY] = _th_book([_th_entry(1)])
        await _download(world, "tenant", "t1a")
        await _download(world, "platform", "global")
        assert net.tenant_flags == [True, False]

    async def test_the_system_proxy_is_for_the_platform_only(
        self, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "DEFAULT_PROXY", "http://127.0.0.1:7890")
        tenant_view = await tavern.list_remote_books(
            preset_id=world["t1a"].id, instance_key="t1a", adapter_key=ADAPTER, _current_user=world["tenant"]
        )
        platform_view = await tavern.list_remote_books(
            preset_id=world["global"].id, instance_key="", adapter_key="", _current_user=world["platform"]
        )
        assert (tenant_view.proxy_available, platform_view.proxy_available) == (False, True)
        with pytest.raises(ValidationError):
            await _download(world, "tenant", "t1a", use_proxy=True)
        with pytest.raises(ValidationError):
            await tavern.check_remote_book(
                preset_id=world["t1a"].id,
                key=KEY,
                req=RemoteBookCheckRequest(use_proxy=True),
                instance_key="t1a",
                adapter_key=ADAPTER,
                _current_user=world["tenant"],
            )
        with pytest.raises(RemoteWorldbookDownloadError, match="平台方"):
            remote_books._proxy_url(True, is_platform=False)  # noqa: SLF001 - 服务层自己也拒
        assert world["net"].calls == []

    @pytest.mark.parametrize(
        ("proxy", "available"),
        [("", False), ("http://p:1", True), ("https://p:1", True), ("127.0.0.1:7890", True), ("socks5://p:1", False)],
    )
    def test_proxy_available_only_for_http_proxies(
        self, monkeypatch: pytest.MonkeyPatch, proxy: str, available: bool
    ) -> None:
        monkeypatch.setattr(config, "DEFAULT_PROXY", proxy)
        assert remote_books.proxy_available() is available

    async def test_a_slow_address_times_out_and_the_next_is_tried(
        self, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        net: FakeNet = world["net"]

        async def trickle() -> bytes:
            await asyncio.sleep(5)  # 每隔一会吐一个字节的服务器：守卫自己只有每次读的超时
            return b""

        monkeypatch.setattr(remote_books, "REMOTE_URL_TIMEOUT_SECONDS", 0.05)
        net.answers[PRIMARY] = trickle
        net.answers[MIRROR] = _th_book([_th_entry(1)])
        result = await _download(world)
        assert result.host == "gcore.example.test"

    async def test_the_whole_download_has_a_deadline(
        self, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        net: FakeNet = world["net"]

        async def trickle() -> bytes:
            await asyncio.sleep(5)
            return b""

        monkeypatch.setattr(remote_books, "REMOTE_TOTAL_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(remote_books, "REMOTE_MIN_URL_SECONDS", 0.2)
        net.answers[PRIMARY] = trickle
        net.answers[MIRROR] = _th_book([_th_entry(1)])
        started = time.monotonic()
        with pytest.raises(RemoteWorldbookDownloadError) as caught:
            await _download(world)
        assert time.monotonic() - started < 2
        reason = str(caught.value.params["reason"])
        assert "cdn.example.test：超时" in reason
        assert "gcore.example.test：没来得及试" in reason
        assert "platform" not in remote_books._running, "名额已经还回来了"  # noqa: SLF001

    async def test_manual_upload_does_not_take_the_download_slot(self, world: dict[str, Any]) -> None:
        with remote_books._download_slot("tenant:1"):  # noqa: SLF001 - 模拟同一个租户的下载卡着
            result = await tavern.upload_remote_book(
                preset_id=world["t1a"].id,
                key=KEY,
                file=UploadFile(file=io.BytesIO(_th_book([_th_entry(1)])), filename="book.json"),
                replace=False,
                instance_key="t1a",
                adapter_key=ADAPTER,
                _current_user=world["tenant"],
            )
        assert result.manual is True

    async def test_replace_rereads_the_book(self, world: dict[str, Any]) -> None:
        net: FakeNet = world["net"]
        net.answers[PRIMARY] = _th_book([_th_entry(1)])
        first = await _download(world)

        async def meanwhile_edited() -> bytes:
            # 下载期间管理员在世界书页改了书名、预算、关掉了书
            await DBWbBook.filter(id=first.book_id).update(name="下载期间改的名", budget_tokens=99, enabled=False)
            return _th_book([_th_entry(2)], version="2.0.0")

        net.answers[PRIMARY] = meanwhile_edited
        second = await _download(world, replace=True)
        book = await DBWbBook.get(id=first.book_id)
        assert (book.name, book.budget_tokens, book.enabled) == ("下载期间改的名", 99, False)
        assert second.book_name == "下载期间改的名"
        assert book.ext[remote_books.REMOTE_SOURCE_KEY]["version"] == "2.0.0"

        async def meanwhile_deleted() -> bytes:
            await DBWbBook.filter(id=first.book_id).delete()
            return _th_book([_th_entry(3)])

        net.answers[PRIMARY] = meanwhile_deleted
        with pytest.raises(NotFoundError):
            await _download(world, replace=True)

    async def test_detected_sources_are_cached_per_card_version(
        self, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0
        real = remote_books.detect_remote_books

        def counting(extensions: Any) -> Any:
            nonlocal calls
            calls += 1
            return real(extensions)

        monkeypatch.setattr(remote_books, "detect_remote_books", counting)
        preset = world["t1a"]
        for _ in range(3):
            assert [source.key for source in await remote_books.preset_sources(preset.id)] == [KEY]
        assert calls == 1
        card = await DBPresetCard.get(preset_id=preset.id)
        card.card_raw = _card([_script([PRIMARY]), _script(["https://cdn.example.test/other-worldbook.json"])])
        await card.save()
        assert len(await remote_books.preset_sources(preset.id)) == 2
        assert calls == 2


class TestImportHygiene:
    """普通导入不认文件里自带的 `na_*` 键；编号超出 32 位范围时重新编号"""

    def test_na_keys_in_a_file_are_dropped(self) -> None:
        forged = {"key": "0" * 16, "url": "https://evil.example.test/x?token=1"}
        st_file = {"name": "书", "na_remote_source": forged, "entries": {"0": {"uid": 0, "content": "x"}}}
        parsed = st_format.parse_world_file(json.dumps(st_file).encode())
        assert parsed.book_ext == {"name": "书"}
        th_file = json.loads(_th_book([_th_entry(1)]))
        th_file["na_remote_source"] = forged
        parsed = st_format.parse_world_payload(json.dumps(th_file).encode())
        assert remote_books.REMOTE_SOURCE_KEY not in parsed.book_ext
        assert st_format.export_world({"name": "书", "na_remote_source": forged}, [])["name"] == "书"
        assert "na_remote_source" not in st_format.export_world({"na_remote_source": forged}, [])

    def test_out_of_range_uids_are_renumbered(self) -> None:
        columns, notes = st_format.st_entry_to_columns({"uid": 2**40, "content": "x"})
        assert columns["uid"] is None
        assert any(note.field == "uid" and "范围" in note.detail for note in notes)
        columns, _notes = st_format.st_entry_to_columns({"uid": 2**31 - 1, "content": "x"})
        assert columns["uid"] == 2**31 - 1

    async def test_huge_remote_uids_import_fine(self, world: dict[str, Any]) -> None:
        world["net"].answers[PRIMARY] = _th_book([_th_entry(2**40), _th_entry(2**31 - 1), _th_entry(5)])
        result = await _download(world)
        uids = sorted(await DBWbEntry.filter(book_id=result.book_id).values_list("uid", flat=True))
        assert len(uids) == 3 and uids[-1] == 2**31 - 1 and all(-(2**31) <= uid < 2**31 for uid in uids)


class TestScope:
    async def test_tenant_cannot_see_another_instances_preset(self, world: dict[str, Any]) -> None:
        with pytest.raises(NotFoundError):
            await tavern.list_remote_books(
                preset_id=world["t2a"].id, instance_key="t1a", adapter_key=ADAPTER, _current_user=world["tenant"]
            )
        with pytest.raises(NotFoundError):
            await _download(world, "tenant", "t2a")

    async def test_global_preset_is_read_only_for_tenants(self, world: dict[str, Any]) -> None:
        world["net"].answers[PRIMARY] = _th_book([_th_entry(1)])
        await _download(world)  # 平台方导进全局（归属为空）
        listing = await tavern.list_remote_books(
            preset_id=world["global"].id, instance_key="t1a", adapter_key=ADAPTER, _current_user=world["tenant"]
        )
        [view] = listing.sources
        assert view.imported is None, "归属不是这个实例的书不算"
        with pytest.raises(PermissionDeniedError):
            await _download(world, "tenant", "global")

    async def test_platform_needs_an_instance_for_an_instance_preset(self, world: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            await _download(world, "platform", "t1a")

    @pytest.mark.parametrize("key", ["0" * 16, "../../etc/passwd", KEY.upper(), ""])
    async def test_unknown_or_malformed_keys_are_404(self, world: dict[str, Any], key: str) -> None:
        with pytest.raises(NotFoundError):
            await _download(world, key=key)
        assert world["net"].calls == []


class TestCardReports:
    async def test_inspect_and_import_list_the_sources(self, world: dict[str, Any]) -> None:
        raw = json.dumps(_card([_script([PRIMARY, MIRROR])]), ensure_ascii=False).encode("utf-8")
        report = await tavern.inspect_card_endpoint(
            file=UploadFile(file=io.BytesIO(raw), filename="card.json"),
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )
        assert [(source.key, source.urls) for source in report.remote_books] == [(KEY, [PRIMARY, MIRROR])]

        result = await tavern.import_card_endpoint(
            file=UploadFile(file=io.BytesIO(raw), filename="card.json"),
            options="{}",
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )
        assert [source.key for source in result.remote_books] == [KEY]
        # 导入后人设页从存着的卡片原文重新认出同一个来源
        listing = await tavern.list_remote_books(
            preset_id=result.preset_id, instance_key="t1a", adapter_key=ADAPTER, _current_user=world["tenant"]
        )
        assert [source.key for source in listing.sources] == [KEY]
        assert world["net"].calls == [], "检查与导入人设都不联网"
