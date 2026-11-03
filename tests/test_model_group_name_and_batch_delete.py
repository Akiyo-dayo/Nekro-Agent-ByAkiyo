"""模型组：中文组名 + 批量删除

## 中文组名

上游原版允许中文组名，二改后 `validate_group_name` 收成了 ASCII 正则：存量 YAML 里的中文组照常
导入（导入不走这条校验），但在后台**新建**中文组一律 400。前端表单只排除 URL 特殊字符，于是
表单校验通过、提交时才被后端拒掉。这里钉住三件事：

1. **后端规则**（`TestGroupNameRule`）：中日韩文字、半角字母数字，以及上游常见命名里的空格与标点
   （`GPT-4o（备用）`、`DeepSeek V3`、`qwen2.5:7b`、`通义千问·Max`、`【主】模型`）放行；URL 结构字符、
   控制/零宽字符、连续空格、超长一律拒绝；同形字母、全角字母、不可见的"字母"（韩文填充符等）也拒绝。
   正反两侧都有用例——只测"中文能过"的话，什么都放行的实现同样能通过。
   字符集挡不住所有形近字，平台方配置下拉框标出租户自建的组（`TestConfigDropdownShowsOwnership`）。
2. **前后端是同一条规则**（`TestFrontendRuleMatchesBackend`）：读 `ModelGroupFormDialog.tsx` 里的字符集，
   换写成 Python 正则后与后端**逐码点**比对；编辑时不再按新规则校验存量组名。
3. **真落库**（`TestChineseNameEndToEnd`）：从控制台接口与旧版平台方接口各建一个中文组，进运行时视图。

## 批量删除

`delete_groups` 对每个组名走与单个删除**同一套**判定（`get_editable_group` + 默认组保护）。
钉的是权限而不是功能：

- 租户只删得掉自己租户的组；平台 shared 组是 `forbidden`，平台 private 组与别的租户的组
  和根本不存在的组**同为** `not_found`——两者可区分时，差异本身就是组名存在性探针；
- 只读成员在装饰器那一层就被挡下，服务层判定同样拒绝（两道门各自有红）；
- 默认组不可删；没删成的组一行都没动；
- 单个组出意外记 `error` 接着删下一个；超长组名、并发里已被别人删掉的组记 `not_found`；
- 运行时视图只重建一次，且请求被取消时已删的行也要从视图里消失。

## 删除前的引用提醒

`find_model_group_references` 回答"这些组被哪些配置引用"（`TestReferenceLookup`）。钉的同样是作用域：

- 平台方看得到六层（系统、适配器、实例、频道、插件全局、插件实例）；
- 租户只看得到**自己实例**下的三层，别的租户实例 / 频道里的引用、平台层的引用一条都不出现；
  实例归属按 `(adapter_key, instance_key)` 判，别的适配器下的同名实例不算自己的；
- 只对可编辑的组作答，看不见的、改不了的、不存在的组名不出现在结果里；
- 覆盖层开关关着的值不算引用；扫描只读盘，不新建、不改写任何文件。
"""

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import anyio
import pytest
import yaml
from fastapi import HTTPException
from pydantic import Field
from pydantic import ValidationError as PydanticValidationError
from tortoise import Tortoise

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.core.core_utils import ConfigBase, ExtraField
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.model_groups import (
    MAX_BATCH_DELETE,
    ModelGroupBatchDeleteRequest,
    ModelGroupCreateRequest,
    ModelGroupPayload,
    ModelGroupReferencesRequest,
    batch_delete_console_model_groups,
    create_console_model_group,
    get_console_model_group_references,
    update_console_model_group,
)
from nekro_agent.schemas.errors import ModelGroupNotFoundError, ValidationError
from nekro_agent.schemas.tenant import CurrentIdentity, TenantRole
from nekro_agent.services import model_group_refs, model_group_service
from nekro_agent.services.model_group_refs import ModelGroupRefScope, find_model_group_references
from nekro_agent.services.model_group_service import (
    DeleteRefusal,
    delete_groups,
    resolve_console_identity,
    upsert_platform_group,
    validate_group_name,
)
from nekro_agent.services.user.role import Role

BS = chr(92)  # 反斜杠
ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

FRONTEND_FORM = (
    Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "model-group" / "ModelGroupFormDialog.tsx"
)

# ---------------------------------------------------------------------------
# 组名样例表：前后端两侧都对这张表跑
# ---------------------------------------------------------------------------

ACCEPTED_NAMES: List[str] = [
    "default",
    "gpt-4o",
    "v1.5",
    "主力模型",
    "通义千问-max",
    "GPT-4o_备用",
    "1号模型",
    "クロード",
    "모델",
    "名" * 64,
    chr(0x20000) + "模型",  # 扩展 B 区汉字（代理对）
    # 上游常见命名：空格与常见标点
    "主力 模型",
    "DeepSeek V3",
    "Claude 3.5 Sonnet",
    "GPT-4o（备用）",
    "模型(测试)",
    "qwen2.5:7b",
    "通义千问" + chr(0x00B7) + "Max",
    "【主】模型",
    "（测试）模型",
    "模型、备用",
    "模型：备用",
    "kimi+",
    "gpt@openai",
]

REJECTED_NAMES: List[str] = [
    "",
    "a  b",  # 连续空格：与单个空格肉眼几乎分不出
    "a" + chr(0x3000) + "b",  # 全角空格，同理
    "a" + chr(0x00A0) + "b",  # 不换行空格
    chr(0x00B7) + "x",  # 标点打头（左括号以外）
    ":x",
    "+x",
    "@x",
    "日本語モデル" + chr(0x30FB) + "高速",  # 片假名中点，与 · 形近
    "模型" + chr(0x2460),  # ①
    "模型" + chr(0x1F600),  # emoji
    "a/b",
    "a\\b",
    "a?b",
    "a#b",
    "a%2Fb",
    "a&b",
    "a=b",
    "_lead",  # 首字符不是字母或数字
    ".hidden",
    "-x",
    "..",
    "a\tb",
    "a" + chr(0x200B) + "b",  # 零宽空格：肉眼看不见，造得出两个"看起来同名"的组
    "名" * 65,
    # 以下都被 Unicode `\w` 算作字母，能造出与平台组像素级一致的组名
    "gpt" + chr(0x3164),  # 韩文填充符（Lo，不可见）
    chr(0xFFA0) + "gpt",  # 半角韩文填充符
    chr(0x115F) + "gpt",  # 韩文初声填充符
    "gpt" + chr(0x0640),  # 阿拉伯延长符
    "d" + chr(0x0435) + "fault",  # 西里尔 е
    chr(0x03A9) + "mega",  # 希腊字母
    "".join(chr(ord(c) + 0xFEE0) for c in "default"),  # 全角 ｄｅｆａｕｌｔ
    chr(0xFF11) + "号",  # 全角数字
    chr(0x3007) + "号",  # 〇（不在汉字区段，形同字母 O）
    chr(0xF900),  # 兼容汉字，与基本区的同一个字长得一样
]


def _make_user(*, user_id: int, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    user = DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )
    user.id = user_id
    return user


def platform_owner() -> DBUser:
    return _make_user(user_id=1, tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def acme_admin() -> DBUser:
    return _make_user(user_id=11, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def acme_member() -> DBUser:
    return _make_user(user_id=12, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)


# ---------------------------------------------------------------------------
# 中文组名
# ---------------------------------------------------------------------------


class TestGroupNameRule:
    @pytest.mark.parametrize("name", ACCEPTED_NAMES)
    def test_accepted(self, name: str) -> None:
        assert validate_group_name(name) == name

    @pytest.mark.parametrize("name", REJECTED_NAMES)
    def test_rejected(self, name: str) -> None:
        with pytest.raises(ValidationError):
            validate_group_name(name)

    def test_surrounding_whitespace_is_stripped_not_rejected(self) -> None:
        """首尾空白按"顺手多敲了空格"处理：去掉后落库，而不是报错或原样存下"""
        assert validate_group_name("  主力模型\n") == "主力模型"

    def test_trailing_newline_inside_is_not_smuggled_through(self) -> None:
        """`^...$` 的 `$` 会放过结尾换行；规则必须整串匹配"""
        assert model_group_service._NAME_RE.fullmatch("abc\n") is None  # noqa: SLF001

    def test_error_message_mentions_chinese(self) -> None:
        """报错文案要说清楚中文是允许的，否则用户会以为是中文本身的问题"""
        with pytest.raises(ValidationError) as excinfo:
            validate_group_name("主力/模型")
        assert "中文" in str(excinfo.value.params.get("reason", ""))


# 前端规则的写法：三段字符集常量（若干个单引号字符串相加）+ 套用它们的 RegExp 模板
FRONTEND_PATTERN_TEMPLATE = (
    "const GROUP_NAME_PATTERN = new RegExp(\n"
    "  `^(?!.*  )[${GROUP_NAME_LETTERS}${GROUP_NAME_OPENERS}][${GROUP_NAME_LETTERS}${GROUP_NAME_PUNCT}]{0,63}$`,\n"
    "  'u'\n"
    ")"
)


def _frontend_const(source: str, name: str) -> str:
    """取出一个由单引号字符串相加而成的 TS 常量，按 TS 字符串字面量解释其中的反斜杠"""
    head = f"const {name} ="
    start = source.index(head) + len(head)
    end = re.compile(r"\n(?:const |/\*\*)").search(source, start)
    assert end is not None
    body = re.sub(r"//[^\n]*", "", source[start : end.start()])
    return "".join(re.findall(r"'([^']*)'", body)).replace(BS + BS, BS)


def _frontend_rule_as_python() -> "re.Pattern[str]":
    """把前端规则换写成 Python 正则

    TS 源码里的字符串带一个反斜杠转义（如 `\\u3400`），交给 `new RegExp(..., 'u')` 解释；
    Python `re` 同样认 `\\uXXXX`，只有 JS 的 `\\u{XXXXX}` 要换成 `\\UXXXXXXXX`。
    """
    source = FRONTEND_FORM.read_text(encoding="utf-8")
    assert FRONTEND_PATTERN_TEMPLATE in source, (
        "GROUP_NAME_PATTERN 不再是约定的模板（不许连续空格、首字符、63 个字符、'u' 标志）"
    )
    letters, punct, openers = (
        _frontend_const(source, f"GROUP_NAME_{part}") for part in ("LETTERS", "PUNCT", "OPENERS")
    )
    letters = re.sub(r"\\u\{([0-9A-Fa-f]+)\}", lambda m: BS + "U" + m.group(1).zfill(8), letters)
    return re.compile(f"(?!.*  )[{letters}{openers}][{letters}{punct}]{{0,63}}")


class TestFrontendRuleMatchesBackend:
    @pytest.mark.parametrize("name", ACCEPTED_NAMES + REJECTED_NAMES)
    def test_same_verdict_on_samples(self, name: str) -> None:
        frontend_ok = _frontend_rule_as_python().fullmatch(name.strip()) is not None
        try:
            validate_group_name(name)
            backend_ok = True
        except ValidationError:
            backend_ok = False
        assert frontend_ok == backend_ok

    def test_same_verdict_on_every_code_point(self) -> None:
        """逐码点比对首字符与后续字符两个位置：样例表只覆盖想得到的字符，这里覆盖全部"""
        frontend = _frontend_rule_as_python()
        backend = model_group_service._NAME_RE  # noqa: SLF001
        diverged = []
        for code_point in range(0x110000):
            if 0xD800 <= code_point <= 0xDFFF:
                continue
            ch = chr(code_point)
            for candidate in (ch, "a" + ch):
                if (frontend.fullmatch(candidate) is None) != (backend.fullmatch(candidate) is None):
                    diverged.append(hex(code_point))
                    break
        assert diverged == []

    def test_validator_uses_the_pattern(self) -> None:
        """规则声明了却没被用上同样不算数：表单校验必须真的调用它"""
        source = FRONTEND_FORM.read_text(encoding="utf-8")
        assert re.search(
            r"isValidGroupName\s*=\s*\(name: string\)\s*=>\s*GROUP_NAME_PATTERN\.test\(name\.trim\(\)\)", source
        )

    def test_edit_mode_does_not_revalidate_the_name(self) -> None:
        """编辑时组名不可改：存量组名可能来自上游更宽松的规则，按新规则挡住就再也保存不了"""
        source = FRONTEND_FORM.read_text(encoding="utf-8")
        submit = source[source.index("const handleSubmit = async") :]
        checks = re.findall(r"if \(([^)]*?)isValidGroupName\(trimmedName\)\)", submit)
        assert checks == ["isCreate && !"], checks


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    """空的 `model_group` 表 + 不落盘的配置（`refresh_runtime_view` 会 `save_config()`）"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    monkeypatch.setattr(model_group_service, "save_config", lambda: None)
    monkeypatch.setattr(model_group_service.config, "MODEL_GROUPS", {})
    try:
        yield
    finally:
        await Tortoise.close_connections()


class TestChineseNameEndToEnd:
    async def test_console_create(self, db: None) -> None:
        body = ModelGroupCreateRequest(name="主力模型", chat_model="gpt-4o")
        info = await create_console_model_group(body=body, _current_user=platform_owner())
        assert info.name == "主力模型"
        assert await DBModelGroup.filter(name="主力模型").exists()
        assert "主力模型" in model_group_service.config.MODEL_GROUPS

    async def test_legacy_platform_upsert(self, db: None) -> None:
        """旧版 `/config/model-groups/{name}`（OOBE 用它）同样走 `validate_group_name`"""
        await upsert_platform_group("备用模型", ModelConfigGroup(CHAT_MODEL="gpt-4o"))
        assert "备用模型" in model_group_service.config.MODEL_GROUPS

    async def test_invalid_name_creates_nothing(self, db: None) -> None:
        body = ModelGroupCreateRequest(name="主力/模型", chat_model="gpt-4o")
        with pytest.raises(ValidationError):
            await create_console_model_group(body=body, _current_user=platform_owner())
        assert await DBModelGroup.all().count() == 0


# ---------------------------------------------------------------------------
# 批量删除
# ---------------------------------------------------------------------------

DEFAULT_GROUP = "default"
PLATFORM_SHARED = "platform_shared"
PLATFORM_PRIVATE = "platform_private"
ACME_OWN = "acme_own"
ACME_OWN_CN = "租户自建"
RIVAL_OWN = "rival_own"
MISSING = "no_such_group"


@pytest.fixture
async def seeded(db: None, monkeypatch: pytest.MonkeyPatch) -> List[int]:
    """四档归属 + 默认组；返回 `refresh_runtime_view` 的调用记录（每次调用追加一个 1）"""
    for name, ownership, owner_tenant_id, visibility in (
        (DEFAULT_GROUP, "platform", None, "shared"),
        (PLATFORM_SHARED, "platform", None, "shared"),
        (PLATFORM_PRIVATE, "platform", None, "private"),
        (ACME_OWN, "tenant", ACME_TENANT_ID, "private"),
        (ACME_OWN_CN, "tenant", ACME_TENANT_ID, "private"),
        (RIVAL_OWN, "tenant", RIVAL_TENANT_ID, "private"),
    ):
        await DBModelGroup.create(
            name=name,
            ownership=ownership,
            owner_tenant_id=owner_tenant_id,
            visibility=visibility,
            group_name=name,
            chat_model="gpt-4o",
            base_url="https://api.example/v1",
            api_key=f"sk-{name}",
        )
    await model_group_service.refresh_runtime_view()

    calls: List[int] = []
    original = model_group_service.refresh_runtime_view

    async def _counting(persist: bool = True):
        calls.append(1)
        return await original(persist)

    monkeypatch.setattr(model_group_service, "refresh_runtime_view", _counting)
    return calls


async def _remaining() -> List[str]:
    return sorted(await DBModelGroup.all().values_list("name", flat=True))


class TestBatchDeleteAsPlatformOwner:
    async def test_deletes_any_ownership_but_not_default(self, seeded: List[int]) -> None:
        identity = resolve_console_identity(platform_owner())
        outcome = await delete_groups([PLATFORM_PRIVATE, ACME_OWN, RIVAL_OWN, DEFAULT_GROUP, MISSING], identity)

        assert outcome.deleted == [PLATFORM_PRIVATE, ACME_OWN, RIVAL_OWN]
        assert outcome.refused == [
            (DEFAULT_GROUP, DeleteRefusal.DEFAULT_GROUP),
            (MISSING, DeleteRefusal.NOT_FOUND),
        ]
        assert await _remaining() == sorted([DEFAULT_GROUP, PLATFORM_SHARED, ACME_OWN_CN])

    async def test_runtime_view_rebuilt_exactly_once(self, seeded: List[int]) -> None:
        identity = resolve_console_identity(platform_owner())
        await delete_groups([PLATFORM_PRIVATE, ACME_OWN, ACME_OWN_CN], identity)
        assert len(seeded) == 1
        view = model_group_service.config.MODEL_GROUPS
        assert PLATFORM_PRIVATE not in view and ACME_OWN not in view and ACME_OWN_CN not in view
        assert PLATFORM_SHARED in view

    async def test_nothing_deleted_means_no_rebuild(self, seeded: List[int]) -> None:
        identity = resolve_console_identity(platform_owner())
        outcome = await delete_groups([DEFAULT_GROUP, MISSING], identity)
        assert outcome.deleted == []
        assert seeded == []

    async def test_duplicates_are_processed_once(self, seeded: List[int]) -> None:
        identity = resolve_console_identity(platform_owner())
        outcome = await delete_groups([ACME_OWN, ACME_OWN], identity)
        assert outcome.deleted == [ACME_OWN]
        assert outcome.refused == []

    async def test_unexpected_error_on_one_group_does_not_stop_the_batch(
        self,
        seeded: List[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """单个组抛出意料之外的异常：记为 error 接着删下一个，响应里如实列出已删的（否则会删一半再 500，调用方不知道删了哪些）"""
        original = model_group_service.get_editable_group

        async def _explode_on_second(name: str, identity: CurrentIdentity) -> DBModelGroup:
            if name == ACME_OWN_CN:
                raise RuntimeError("boom")
            return await original(name, identity)

        monkeypatch.setattr(model_group_service, "get_editable_group", _explode_on_second)
        identity = resolve_console_identity(platform_owner())
        outcome = await delete_groups([ACME_OWN, ACME_OWN_CN, PLATFORM_PRIVATE], identity)

        assert outcome.deleted == [ACME_OWN, PLATFORM_PRIVATE]
        assert outcome.refused == [(ACME_OWN_CN, DeleteRefusal.ERROR)]
        assert len(seeded) == 1
        assert ACME_OWN_CN in await _remaining()

    async def test_view_is_rebuilt_even_when_cancelled(
        self,
        seeded: List[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """请求被取消（不归 `except Exception` 管）：已删的组也得从视图里消失"""
        original = model_group_service.get_editable_group

        async def _cancel_on_second(name: str, identity: CurrentIdentity) -> DBModelGroup:
            if name == ACME_OWN_CN:
                raise asyncio.CancelledError
            return await original(name, identity)

        monkeypatch.setattr(model_group_service, "get_editable_group", _cancel_on_second)
        identity = resolve_console_identity(platform_owner())
        with pytest.raises(asyncio.CancelledError):
            await delete_groups([ACME_OWN, ACME_OWN_CN], identity)

        assert len(seeded) == 1
        assert ACME_OWN not in model_group_service.config.MODEL_GROUPS
        assert ACME_OWN not in await _remaining()

    async def test_view_is_rebuilt_even_under_anyio_cancellation(
        self,
        seeded: List[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """anyio 的取消作用域会在作用域内每一次 await 上重复投递取消（Starlette 中间件、uvicorn 关停）：
        `finally` 里的重建不屏蔽就会被打断，已删的组留在运行时视图里、也留在配置文件镜像里
        """
        original = model_group_service.get_editable_group

        with anyio.CancelScope() as scope:

            async def _cancel_scope_on_second(name: str, identity: CurrentIdentity) -> DBModelGroup:
                if name == ACME_OWN_CN:
                    scope.cancel()
                    await anyio.sleep(0)
                return await original(name, identity)

            monkeypatch.setattr(model_group_service, "get_editable_group", _cancel_scope_on_second)
            await delete_groups([ACME_OWN, ACME_OWN_CN], resolve_console_identity(platform_owner()))

        assert scope.cancelled_caught
        # 重建在被屏蔽的后台任务里跑完；外层的 await 已经因取消返回，这里等它落定
        for _ in range(100):
            if ACME_OWN not in model_group_service.config.MODEL_GROUPS:
                break
            await asyncio.sleep(0.01)
        assert ACME_OWN not in model_group_service.config.MODEL_GROUPS
        assert ACME_OWN not in await _remaining()
        assert len(seeded) == 1

    async def test_concurrent_delete_is_not_reported_twice(
        self,
        seeded: List[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """取出行之后、删除之前被别人删掉：删了 0 行，记 not_found，不报「已删除」"""
        original = model_group_service.get_editable_group

        async def _deleted_meanwhile(name: str, identity: CurrentIdentity) -> DBModelGroup:
            row = await original(name, identity)
            await DBModelGroup.filter(id=row.id).delete()
            return row

        monkeypatch.setattr(model_group_service, "get_editable_group", _deleted_meanwhile)
        identity = resolve_console_identity(platform_owner())
        outcome = await delete_groups([ACME_OWN], identity)
        assert outcome.deleted == []
        assert outcome.refused == [(ACME_OWN, DeleteRefusal.NOT_FOUND)]

    async def test_overlong_name_is_just_not_found(self, seeded: List[int]) -> None:
        """超过列宽的组名不可能存在：按不存在结算，不让 Tortoise 的长度校验冒成 500"""
        overlong = "y" * 129
        identity = resolve_console_identity(platform_owner())
        outcome = await delete_groups([ACME_OWN, overlong, ACME_OWN_CN], identity)
        assert outcome.deleted == [ACME_OWN, ACME_OWN_CN]
        # 回显截到列宽：那种名字不可能存在，原样回显只是放大请求
        assert outcome.refused == [("y" * 128 + "…", DeleteRefusal.NOT_FOUND)]
        with pytest.raises(ModelGroupNotFoundError):
            await model_group_service.get_visible_group(overlong, identity)

    def test_column_width_constant_matches_the_model(self) -> None:
        """列宽常量是手写的：列宽一改它就漂移，这里钉在一起"""
        field = DBModelGroup._meta.fields_map["name"]  # noqa: SLF001
        assert model_group_service._NAME_COLUMN_MAX == field.max_length  # noqa: SLF001

    async def test_single_delete_of_a_row_deleted_meanwhile_is_not_found(self, seeded: List[int]) -> None:
        """单个删除与批量删除同一口径：并发里已被别人删掉的组报不存在"""
        row = await DBModelGroup.get(name=ACME_OWN)
        await DBModelGroup.filter(id=row.id).delete()
        with pytest.raises(ModelGroupNotFoundError):
            await model_group_service.delete_group(row)
        assert seeded == []

    async def test_legacy_name_can_still_be_updated(self, seeded: List[int]) -> None:
        """不合新规则的存量组名（上游导入）照样能编辑保存：更新接口只拿组名当查找键，不校验"""
        await DBModelGroup.create(name="DeepSeek V3", ownership="platform", visibility="private", chat_model="old")
        info = await update_console_model_group(
            name="DeepSeek V3",
            body=ModelGroupPayload(chat_model="deepseek-chat"),
            _current_user=platform_owner(),
        )
        assert info.chat_model == "deepseek-chat"


class TestBatchDeleteAsTenantAdmin:
    async def test_only_own_groups_are_deleted(self, seeded: List[int]) -> None:
        identity = resolve_console_identity(acme_admin())
        outcome = await delete_groups(
            [ACME_OWN, ACME_OWN_CN, PLATFORM_SHARED, PLATFORM_PRIVATE, RIVAL_OWN, MISSING, DEFAULT_GROUP],
            identity,
        )

        assert outcome.deleted == [ACME_OWN, ACME_OWN_CN]
        assert dict(outcome.refused) == {
            PLATFORM_SHARED: DeleteRefusal.FORBIDDEN,  # 看得见、改不了
            PLATFORM_PRIVATE: DeleteRefusal.NOT_FOUND,  # 看不见
            RIVAL_OWN: DeleteRefusal.NOT_FOUND,  # 别的租户的
            MISSING: DeleteRefusal.NOT_FOUND,
            DEFAULT_GROUP: DeleteRefusal.FORBIDDEN,  # shared 的平台组：先判权限，不泄露"它是默认组"
        }
        assert await _remaining() == sorted([DEFAULT_GROUP, PLATFORM_SHARED, PLATFORM_PRIVATE, RIVAL_OWN])

    async def test_invisible_and_missing_are_indistinguishable(self, seeded: List[int]) -> None:
        """存在性探针：别的租户的组与不存在的组，结论必须逐字相同"""
        identity = resolve_console_identity(acme_admin())
        outcome = await delete_groups([RIVAL_OWN, MISSING, PLATFORM_PRIVATE], identity)
        assert {reason for _, reason in outcome.refused} == {DeleteRefusal.NOT_FOUND}

    async def test_console_endpoint_reports_per_item(self, seeded: List[int]) -> None:
        body = ModelGroupBatchDeleteRequest(names=[ACME_OWN, PLATFORM_SHARED])
        result = await batch_delete_console_model_groups(body=body, _current_user=acme_admin())
        assert result.ok is False
        assert result.succeeded == [ACME_OWN]
        assert [(item.name, item.reason) for item in result.failed] == [(PLATFORM_SHARED, DeleteRefusal.FORBIDDEN)]

    async def test_console_endpoint_all_ok(self, seeded: List[int]) -> None:
        body = ModelGroupBatchDeleteRequest(names=[ACME_OWN, ACME_OWN_CN])
        result = await batch_delete_console_model_groups(body=body, _current_user=acme_admin())
        assert result.ok is True
        assert result.failed == []


class TestBatchDeleteAsReadOnlyMember:
    async def test_endpoint_rejects_at_the_role_gate(self, seeded: List[int]) -> None:
        body = ModelGroupBatchDeleteRequest(names=[ACME_OWN])
        with pytest.raises(HTTPException) as excinfo:
            await batch_delete_console_model_groups(body=body, _current_user=acme_member())
        assert excinfo.value.status_code == 403
        assert ACME_OWN in await _remaining()

    async def test_service_also_refuses(self, seeded: List[int]) -> None:
        """第二道门：即便绕过装饰器，只读成员对自己租户的组也是 `forbidden`"""
        identity = resolve_console_identity(acme_member())
        outcome = await delete_groups([ACME_OWN], identity)
        assert outcome.deleted == []
        assert outcome.refused == [(ACME_OWN, DeleteRefusal.FORBIDDEN)]
        assert ACME_OWN in await _remaining()


class TestBatchDeleteRequestBounds:
    def test_empty_list_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ModelGroupBatchDeleteRequest(names=[])

    def test_over_limit_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ModelGroupBatchDeleteRequest(names=[f"g{i}" for i in range(MAX_BATCH_DELETE + 1)])

    def test_refusal_vocabulary_is_pinned(self) -> None:
        """前端按这几个字符串取 i18n 文案（`modelGroup.batchDelete.reasons.*`）"""
        assert {member.value for member in DeleteRefusal} == {"not_found", "forbidden", "default_group", "error"}

    def test_every_refusal_has_copy_in_both_locales(self) -> None:
        """少一条文案，界面上显示的就是 i18n 键名"""
        expected = {member.value for member in DeleteRefusal}
        for locale in ("zh-CN", "en-US"):
            data = json.loads((FRONTEND_LOCALES / locale / "settings.json").read_text(encoding="utf-8"))
            assert set(data["modelGroup"]["batchDelete"]["reasons"]) == expected, locale


# ---------------------------------------------------------------------------
# 删除前的引用提醒
# ---------------------------------------------------------------------------

ADAPTER = "onebot_v11"
ACME_INSTANCE = "inst1"
RIVAL_INSTANCE = "rival1"
PLATFORM_INSTANCE = "plat1"
ACME_CHAT = "onebot_v11-inst1-group_10001"
RIVAL_CHAT = "onebot_v11-rival1-group_20001"
PROBE_PLUGIN = "probe_plugin"

FRONTEND_MANAGER = FRONTEND_FORM.parent / "ModelGroupManager.tsx"
FRONTEND_NOTICE = FRONTEND_FORM.parent / "ModelGroupReferenceNotice.tsx"
FRONTEND_LOCALES = FRONTEND_FORM.parents[2] / "locales"


class ProbePluginConfig(ConfigBase):
    """一个带模型组引用字段的插件配置（字段由标记识别，不靠名字）"""

    PROBE_DRAW_GROUP: str = Field(
        default="",
        title="探针绘图模型组",
        json_schema_extra=ExtraField(ref_model_groups=True, model_type="draw").model_dump(),
    )
    PROBE_PLAIN_TEXT: str = Field(default="", title="不是引用的普通字段")


def _write_yaml(path: Path, data: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.fixture
async def refs_env(seeded: List[int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """六层配置里各放一处引用；acme 的组被平台层、acme 自己、rival 的实例同时引用

    rival 的实例引用 acme 的组走不通 API（租户只能选自己看得见的组），但平台方手改文件写得出来——
    正好用来钉"租户看不到别的租户那边的引用"。
    """
    for instance_key, tenant_id in (
        (ACME_INSTANCE, ACME_TENANT_ID),
        (RIVAL_INSTANCE, RIVAL_TENANT_ID),
        (PLATFORM_INSTANCE, None),
    ):
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )
    for instance_key, chat_key in ((ACME_INSTANCE, ACME_CHAT), (RIVAL_INSTANCE, RIVAL_CHAT)):
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
            chat_key=chat_key,
            channel_type="group",
            channel_name=chat_key,
            data="{}",
        )

    config_dir = tmp_path / "configs"
    channel_dir = config_dir / "channels"
    plugin_dir = tmp_path / "plugin_data"
    monkeypatch.setattr(model_group_refs, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(model_group_refs, "CHANNEL_CONFIG_DIR", channel_dir)
    monkeypatch.setattr(model_group_refs, "PLUGIN_DATA_DIR", plugin_dir)
    # 插件实例覆盖经 `scope.load_instance_config_overrides` 读，它用自己模块里的目录常量
    from nekro_agent.services.plugin import scope as plugin_scope

    monkeypatch.setattr(plugin_scope, "PLUGIN_DATA_DIR", plugin_dir)

    # 平台层：系统配置、适配器覆盖、插件全局配置
    monkeypatch.setattr(model_group_refs.config, "FALLBACK_MODEL_GROUP", ACME_OWN)
    _write_yaml(config_dir / ADAPTER / "overrides.yaml", {"enable_USE_MODEL_GROUP": True, "USE_MODEL_GROUP": ACME_OWN})
    probe = SimpleNamespace(
        key=PROBE_PLUGIN,
        config_class=ProbePluginConfig,
        get_global_config=lambda: ProbePluginConfig(PROBE_DRAW_GROUP=ACME_OWN, PROBE_PLAIN_TEXT=ACME_OWN),
    )
    monkeypatch.setattr(model_group_refs.plugin_collector, "get_all_plugins", lambda: [probe])

    # acme 自己实例下的三层
    _write_yaml(
        config_dir / ADAPTER / "instances" / ACME_INSTANCE / "overrides.yaml",
        {"enable_FALLBACK_MODEL_GROUP": True, "FALLBACK_MODEL_GROUP": ACME_OWN},
    )
    _write_yaml(
        channel_dir / ADAPTER / f"{ACME_CHAT}.yaml",
        {
            "enable_USE_MODEL_GROUP": True,
            "USE_MODEL_GROUP": ACME_OWN_CN,
            # 开关关着的值不参与解析，不算引用
            "enable_AFFECTION_MODEL_GROUP": False,
            "AFFECTION_MODEL_GROUP": ACME_OWN,
        },
    )
    _write_yaml(plugin_dir / PROBE_PLUGIN / "instances" / ACME_INSTANCE / "config.yaml", {"PROBE_DRAW_GROUP": ACME_OWN})

    # rival 实例下的三层（同样引用 acme 的组）
    _write_yaml(
        config_dir / ADAPTER / "instances" / RIVAL_INSTANCE / "overrides.yaml",
        {"enable_USE_MODEL_GROUP": True, "USE_MODEL_GROUP": ACME_OWN},
    )
    _write_yaml(
        channel_dir / ADAPTER / f"{RIVAL_CHAT}.yaml", {"enable_USE_MODEL_GROUP": True, "USE_MODEL_GROUP": ACME_OWN}
    )
    _write_yaml(
        plugin_dir / PROBE_PLUGIN / "instances" / RIVAL_INSTANCE / "config.yaml", {"PROBE_DRAW_GROUP": ACME_OWN}
    )
    return tmp_path


def _scopes(reports, name: str) -> List[tuple]:
    report = next(item for item in reports if item.name == name)
    return sorted((ref.scope.value, ref.target, ref.instance_key, ref.field) for ref in report.references)


class TestReferenceLookup:
    async def test_platform_owner_sees_all_six_layers(self, refs_env: Path) -> None:
        reports = await find_model_group_references(
            [ACME_OWN, ACME_OWN_CN, MISSING],
            resolve_console_identity(platform_owner()),
        )
        assert [report.name for report in reports] == [ACME_OWN, ACME_OWN_CN]
        assert _scopes(reports, ACME_OWN) == sorted(
            [
                ("system", "", "", "FALLBACK_MODEL_GROUP"),
                ("adapter", ADAPTER, "", "USE_MODEL_GROUP"),
                ("instance", f"{ADAPTER}/{ACME_INSTANCE}", "", "FALLBACK_MODEL_GROUP"),
                ("instance", f"{ADAPTER}/{RIVAL_INSTANCE}", "", "USE_MODEL_GROUP"),
                ("channel", RIVAL_CHAT, "", "USE_MODEL_GROUP"),
                ("plugin", PROBE_PLUGIN, "", "PROBE_DRAW_GROUP"),
                ("plugin_instance", PROBE_PLUGIN, ACME_INSTANCE, "PROBE_DRAW_GROUP"),
                ("plugin_instance", PROBE_PLUGIN, RIVAL_INSTANCE, "PROBE_DRAW_GROUP"),
            ],
        )
        assert _scopes(reports, ACME_OWN_CN) == [("channel", ACME_CHAT, "", "USE_MODEL_GROUP")]

    async def test_tenant_sees_only_its_own_instances(self, refs_env: Path) -> None:
        reports = await find_model_group_references(
            [ACME_OWN, ACME_OWN_CN],
            resolve_console_identity(acme_admin()),
        )
        assert _scopes(reports, ACME_OWN) == sorted(
            [
                ("instance", f"{ADAPTER}/{ACME_INSTANCE}", "", "FALLBACK_MODEL_GROUP"),
                ("plugin_instance", PROBE_PLUGIN, ACME_INSTANCE, "PROBE_DRAW_GROUP"),
            ],
        )
        assert _scopes(reports, ACME_OWN_CN) == [("channel", ACME_CHAT, "", "USE_MODEL_GROUP")]

    async def test_same_instance_name_under_another_adapter_is_not_mine(self, refs_env: Path) -> None:
        """实例名只在适配器内唯一：rival 在另一个适配器下也有个 inst1，它的实例覆盖与频道不能报给 acme"""
        other_adapter = "wechat_probe"
        secret_chat = f"{other_adapter}-{ACME_INSTANCE}-secretroom"
        await DBAdapterInstance.create(
            adapter_key=other_adapter,
            instance_key=ACME_INSTANCE,
            display_name="rival twin",
            status="online",
            provider="remote_onebot",
            tenant_id=RIVAL_TENANT_ID,
            metadata_json="",
        )
        await DBChatChannel.create(
            adapter_key=other_adapter,
            instance_key=ACME_INSTANCE,
            channel_id="secretroom",
            chat_key=secret_chat,
            channel_type="group",
            channel_name=secret_chat,
            data="{}",
        )
        _write_yaml(
            refs_env / "configs" / other_adapter / "instances" / ACME_INSTANCE / "overrides.yaml",
            {"enable_USE_MODEL_GROUP": True, "USE_MODEL_GROUP": ACME_OWN},
        )
        _write_yaml(
            refs_env / "configs" / "channels" / other_adapter / f"{secret_chat}.yaml",
            {"enable_USE_MODEL_GROUP": True, "USE_MODEL_GROUP": ACME_OWN},
        )

        acme = await find_model_group_references([ACME_OWN], resolve_console_identity(acme_admin()))
        assert _scopes(acme, ACME_OWN) == sorted(
            [
                ("instance", f"{ADAPTER}/{ACME_INSTANCE}", "", "FALLBACK_MODEL_GROUP"),
                ("plugin_instance", PROBE_PLUGIN, ACME_INSTANCE, "PROBE_DRAW_GROUP"),
            ],
        )
        # 正向对照：平台方看得到这两处，说明不是文件没写对
        platform = await find_model_group_references([ACME_OWN], resolve_console_identity(platform_owner()))
        assert ("instance", f"{other_adapter}/{ACME_INSTANCE}", "", "USE_MODEL_GROUP") in _scopes(platform, ACME_OWN)
        assert ("channel", secret_chat, "", "USE_MODEL_GROUP") in _scopes(platform, ACME_OWN)

    async def test_toggle_is_read_like_pydantic(self, refs_env: Path) -> None:
        """手改文件把开关写成 "true" / 1 时运行时照样生效，扫描也得算；"false" / 0 不算"""
        second_chat = f"{ADAPTER}-{ACME_INSTANCE}-group_10002"
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            channel_id="group_10002",
            chat_key=second_chat,
            channel_type="group",
            channel_name=second_chat,
            data="{}",
        )
        _write_yaml(
            refs_env / "configs" / "channels" / ADAPTER / f"{second_chat}.yaml",
            {
                "enable_FALLBACK_MODEL_GROUP": "true",
                "FALLBACK_MODEL_GROUP": ACME_OWN_CN,
                "enable_DEBUG_MIGRATION_MODEL_GROUP": 1,
                "DEBUG_MIGRATION_MODEL_GROUP": ACME_OWN_CN,
                "enable_AFFECTION_MODEL_GROUP": "false",
                "AFFECTION_MODEL_GROUP": ACME_OWN_CN,
                "enable_USE_MODEL_GROUP": 0,
                "USE_MODEL_GROUP": ACME_OWN_CN,
            },
        )
        reports = await find_model_group_references([ACME_OWN_CN], resolve_console_identity(acme_admin()))
        assert _scopes(reports, ACME_OWN_CN) == sorted(
            [
                ("channel", ACME_CHAT, "", "USE_MODEL_GROUP"),
                ("channel", second_chat, "", "DEBUG_MIGRATION_MODEL_GROUP"),
                ("channel", second_chat, "", "FALLBACK_MODEL_GROUP"),
            ],
        )

    async def test_tenant_gets_no_answer_for_groups_it_cannot_delete(self, refs_env: Path) -> None:
        """平台组（看得见改不了 / 看不见）、别的租户的组、不存在的组：一律不出现，不当存在性探针"""
        reports = await find_model_group_references(
            [PLATFORM_SHARED, PLATFORM_PRIVATE, RIVAL_OWN, MISSING, DEFAULT_GROUP],
            resolve_console_identity(acme_admin()),
        )
        assert reports == []

    async def test_read_only_member_gets_nothing(self, refs_env: Path) -> None:
        assert await find_model_group_references([ACME_OWN], resolve_console_identity(acme_member())) == []
        with pytest.raises(HTTPException) as excinfo:
            await get_console_model_group_references(
                body=ModelGroupReferencesRequest(names=[ACME_OWN]),
                _current_user=acme_member(),
            )
        assert excinfo.value.status_code == 403

    async def test_scan_never_touches_the_disk(self, refs_env: Path) -> None:
        before = _snapshot(refs_env)
        await find_model_group_references([ACME_OWN, ACME_OWN_CN], resolve_console_identity(platform_owner()))
        assert _snapshot(refs_env) == before

    async def test_unreadable_file_is_skipped(self, refs_env: Path) -> None:
        (refs_env / "configs" / "channels" / ADAPTER / f"{ACME_CHAT}.yaml").write_text(
            "::: [not yaml", encoding="utf-8"
        )
        reports = await find_model_group_references([ACME_OWN_CN, ACME_OWN], resolve_console_identity(acme_admin()))
        assert _scopes(reports, ACME_OWN_CN) == []
        assert len(_scopes(reports, ACME_OWN)) == 2

    async def test_long_lists_are_truncated_but_counted(self, refs_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(model_group_refs, "MAX_REFERENCES_PER_GROUP", 3)
        reports = await find_model_group_references([ACME_OWN], resolve_console_identity(platform_owner()))
        assert reports[0].total == 8
        assert len(reports[0].references) == 3

    async def test_console_endpoint(self, refs_env: Path) -> None:
        response = await get_console_model_group_references(
            body=ModelGroupReferencesRequest(names=[ACME_OWN_CN]),
            _current_user=acme_admin(),
        )
        assert [item.name for item in response.items] == [ACME_OWN_CN]
        reference = response.items[0].references[0]
        assert (reference.scope, reference.target, reference.field) == (
            ModelGroupRefScope.CHANNEL,
            ACME_CHAT,
            "USE_MODEL_GROUP",
        )
        assert reference.title, "界面要展示字段标题，不能只给字段名"
        assert reference.i18n_title and reference.i18n_title.get("en-US") == "Primary Model Group", (
            "英文界面要拿得到英文标题"
        )


def _batch_mutation_block(source: str) -> str:
    start = source.index("const batchDeleteMutation = useMutation(")
    return source[start : source.index("const handleSubmit = async", start)]


class TestBatchDeleteFrontendDetails:
    """前端没有组件测试基座，这几条只能钉源码。每一条都对应一个"代码改回去、测试照样全绿"的变异"""

    def test_confirm_list_is_frozen_when_the_dialog_opens(self) -> None:
        """名单只在打开确认框时写入一次；渲染、提醒、删除用的都是这一份，不跟着实时勾选走"""
        source = FRONTEND_MANAGER.read_text(encoding="utf-8")
        assert len(re.findall(r"\bsetBatchDeleteTargets\(", source)) == 1
        assert re.search(
            r"const openBatchDelete = \(\) => \{\s*setBatchDeleteTargets\(groups\.filter\(group => activeSelection\.includes\(group\.name\)\)\)",
            source,
        )
        assert "() => batchDeleteTargets.map(group => group.name)," in source
        assert "{batchDeleteTargets.map(group => (" in source

    def test_failed_request_still_refreshes_the_list(self) -> None:
        """请求失败时服务端可能已删掉一部分，列表不能还显示着它们"""
        block = _batch_mutation_block(FRONTEND_MANAGER.read_text(encoding="utf-8"))
        assert re.search(
            r"onError: \(error: Error\) => \{\s*notification\.error\(error\.message\)\s*invalidate\(\)\s*\}", block
        )

    def test_partial_failure_notice_names_at_most_a_few_groups(self) -> None:
        source = FRONTEND_MANAGER.read_text(encoding="utf-8")
        assert re.search(r"const MAX_FAILURES_IN_NOTICE = 5\b", source)
        assert "result.failed.slice(0, MAX_FAILURES_IN_NOTICE)" in _batch_mutation_block(source)

    def test_platform_owner_sees_which_groups_belong_to_tenants(self) -> None:
        """平台方「全选」会连带租户自建的组，确认名单里要标出来"""
        source = FRONTEND_MANAGER.read_text(encoding="utf-8")
        assert re.search(
            r"\{isPlatformOwner && group\.ownership === 'tenant' && \(\s*<Typography[^>]*>\s*"
            r"\{t\('modelGroup\.batchDelete\.tenantOwned'",
            source,
        )

    def test_reference_check_does_not_rescan_on_window_focus(self) -> None:
        """每次都是一遍全量扫盘，确认框开着时切回窗口不该再扫"""
        assert "refetchOnWindowFocus: false," in FRONTEND_NOTICE.read_text(encoding="utf-8")

    def test_english_counts_are_pluralized(self) -> None:
        data = json.loads((FRONTEND_LOCALES / "en-US" / "settings.json").read_text(encoding="utf-8"))["modelGroup"]
        for keys in (data["notifications"], data["batchDelete"]):
            base = "batchDeleteSuccess" if "batchDeleteSuccess_one" in keys else "content"
            assert {f"{base}_one", f"{base}_other"} <= set(keys), base
            assert base not in keys, f"{base} 留着无后缀的键会让英文永远走复数"


class TestReferenceNoticeIsWiredIntoBothDialogs:
    """提醒组件得真的出现在单个删除与批量删除两个确认框里，且检查进行中禁用确认"""

    def test_both_dialogs_render_the_notice(self) -> None:
        """只数组件出现次数是不够的：`{false && (<ModelGroupReferenceNotice ...` 也数得出两次（变异实测存活）。
        这里钉的是"挂在哪个条件下、传的是哪批组名"——单个删除挂在 `deleteTarget` 下、查的是它的组名；
        批量删除挂在 `batchDeleteOpen` 下、查的是打开确认框时定格的那批组名
        """
        source = FRONTEND_MANAGER.read_text(encoding="utf-8")
        assert len(re.findall(r"<ModelGroupReferenceNotice\b", source)) == 2
        # 单个删除那一段里夹着 `sx={{ ... }}` 这类嵌套花括号，用"紧随其后的一小段"而不是配平括号
        assert re.search(
            r"\{deleteTarget && \(\s*<>(?:(?!\{\w+ && \().){0,600}?<ModelGroupReferenceNotice\s+names=\{\[deleteTarget\.name\]\}",
            source,
            re.DOTALL,
        )
        assert re.search(r"\{batchDeleteOpen && \(\s*<ModelGroupReferenceNotice\s+names=\{batchDeleteNames\}", source)
        # 确认按钮删的就是提醒查过的那一批
        assert "onClick={() => batchDeleteMutation.mutate(batchDeleteNames)}" in source
        assert len(re.findall(r"onCheckingChange=\{setCheckingReferences\}", source)) == 2
        assert len(re.findall(r"disabled=\{[^}]*checkingReferences", source)) == 2

    def test_tenant_is_not_told_there_are_no_references_at_all(self) -> None:
        """租户看不到平台层的引用，"没有引用"这句绝对化的话只能对平台方说"""
        source = FRONTEND_NOTICE.read_text(encoding="utf-8")
        assert re.search(
            r"isPlatformOwner\s*\?\s*t\('modelGroup\.references\.none'\)\s*:\s*t\('modelGroup\.references\.noneInScope'\)",
            source,
        )

    def test_edit_submits_the_stored_name_unchanged(self) -> None:
        """编辑时用源组名作路径：存量组名可能带首尾空白，trim 之后就找不到那一行"""
        source = FRONTEND_FORM.read_text(encoding="utf-8")
        assert "await onSubmit(isCreate ? trimmedName : (source?.name ?? trimmedName), payload, isCreate)" in source

    def test_scope_labels_cover_every_scope(self) -> None:
        """每个 scope 都要有中英文文案，否则界面上显示的是 i18n 键名"""
        expected = {member.value for member in ModelGroupRefScope}
        for locale in ("zh-CN", "en-US"):
            data = json.loads((FRONTEND_LOCALES / locale / "settings.json").read_text(encoding="utf-8"))
            assert set(data["modelGroup"]["references"]["scopes"]) == expected, locale


FRONTEND_CONFIG_TABLE = FRONTEND_FORM.parents[1] / "common" / "ConfigTable.tsx"


class TestConfigDropdownShowsOwnership:
    """字符集挡不住所有形近字（片假名 カ 与汉字 力、英文 I 与 l）：平台方在配置项的模型组下拉框里
    必须看得出哪些是租户自建的组，否则选错一次，平台的对话就发到了租户配置的端点上
    """

    def test_platform_owner_loads_ownership(self) -> None:
        source = FRONTEND_CONFIG_TABLE.read_text(encoding="utf-8")
        assert "queryKey: TENANT_QUERY_KEYS.modelGroups," in source
        assert "enabled: isPlatformOwner && Boolean(configService.getModelGroups)," in source
        assert ".filter(group => group.ownership === 'tenant')" in source

    def test_each_tenant_group_option_carries_a_label(self) -> None:
        source = FRONTEND_CONFIG_TABLE.read_text(encoding="utf-8")
        assert re.search(
            r"<MenuItem key=\{name\} value=\{name\}>\s*\{name\}\s*\{tenantOwnedGroups\.has\(name\) && \(\s*<Chip"
            r"[^>]*?label=\{t\('configTable\.tenantOwnedModelGroup'",
            source,
            re.DOTALL,
        )

    def test_label_has_copy_in_both_locales(self) -> None:
        for locale in ("zh-CN", "en-US"):
            data = json.loads((FRONTEND_LOCALES / locale / "common.json").read_text(encoding="utf-8"))
            assert "{{id}}" in data["configTable"]["tenantOwnedModelGroup"], locale
