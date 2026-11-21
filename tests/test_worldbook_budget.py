"""世界书每轮预算：按 token 计，照 SillyTavern 的「占上下文的百分比」与「token 上限」

要钉住的事：

1. 公式与 ST 一致：`base = max(0, 上下文 − 4096)`、`round(百分比 × base)`、上限 > 0 时取小、至少 1；
   另有硬上限 65536（上限 0 也不超过它）；上下文 1 ~ 4096 时预算只有 1，保存与启动时记 WARNING；
2. 上下文取这一轮主模型组上填的值，没填按 32768；系数取组上的、否则按模型名；
3. 备用模型组上下文更小时记下它（明细据此提示）；
4. 配置页的换算接口与运行时同一个函数，覆盖层按「上层值 + 本层启用的覆盖」算；迁移提示只给平台方
   （专用接口、系统配置列表与单项读三条路径都是）；token 上限不被按名兜底判成凭据；
5. 模型组的两个新字段：读库夹回合法区间、请求体「清空」就是回到 0、越界当场拒绝；
6. 配置项列表带上滑块需要的 `minimum` / `maximum` 与两个新标记；导入期校验两个标记配对。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, Iterator, List

import pytest
from loguru import logger
from pydantic import Field
from pydantic import ValidationError as PydanticValidationError
from tortoise import Tortoise

from nekro_agent.core.config import (
    CoreConfig,
    ModelConfigGroup,
    check_tenant_limit_markers,
    config,
)
from nekro_agent.core.core_utils import ConfigBase, ExtraField
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.routers.console import model_groups as console_model_groups
from nekro_agent.schemas.errors import ConfigNotFoundError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import config_resolver as config_resolver_module
from nekro_agent.services import model_group_service
from nekro_agent.services.config_service import ConfigService, UnifiedConfigService
from nekro_agent.services.user.role import Role
from nekro_agent.services.worldbook import tokens
from nekro_agent.services.worldbook.budget import (
    DEFAULT_CONTEXT_LENGTH,
    HARD_CAP_TOKENS,
    OUTPUT_RESERVE_TOKENS,
    compute_budget,
    is_small_context,
    resolve_budget,
)

CHANNEL_KEY = "channel_config_onebot_v11-budget-group_1"
MIGRATION_KEYS = ("WORLDBOOK_BUDGET_MIGRATION_NOTICE", "WORLDBOOK_BUDGET_MIGRATED")
NOTICE_LINE = "频道 onebot_v11-group_1：原 2000 字 → 上限 1600 token"
# `nekro_agent.core` 包上的 `config` 属性是配置对象而不是模块，要拿模块得走 sys.modules
core_config_module = importlib.import_module("nekro_agent.core.config")


def platform_owner() -> DBUser:
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


def tenant_admin() -> DBUser:
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


@pytest.fixture
def groups(monkeypatch: pytest.MonkeyPatch) -> Dict[str, ModelConfigGroup]:
    """运行时视图里放几个模型组：填了上下文的、没填的、填了系数的"""
    table = {
        "big": ModelConfigGroup(CHAT_MODEL="claude-3-5-sonnet", CONTEXT_LENGTH=128_000),
        "plain": ModelConfigGroup(CHAT_MODEL="deepseek-chat"),
        "tuned": ModelConfigGroup(CHAT_MODEL="deepseek-chat", CONTEXT_LENGTH=64_000, TOKEN_COUNT_FACTOR=0.75),
        "small": ModelConfigGroup(CHAT_MODEL="qwen-plus", CONTEXT_LENGTH=16_000),
    }
    monkeypatch.setattr(config, "MODEL_GROUPS", table)
    return table


@pytest.fixture
def warnings_logged() -> Iterator[List[str]]:
    messages: List[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="WARNING")
    yield messages
    logger.remove(sink_id)


def effective(**values: Any) -> SimpleNamespace:
    base = {
        "USE_MODEL_GROUP": "plain",
        "FALLBACK_MODEL_GROUP": "plain",
        "WORLDBOOK_BUDGET_PERCENT": 25,
        "WORLDBOOK_BUDGET_CAP_TOKENS": 0,
    }
    base.update(values)
    return SimpleNamespace(**base)


class TestFormula:
    @pytest.mark.parametrize(
        ("context", "percent", "cap", "expected"),
        [
            (32768, 25, 0, (28672, 7168)),  # NA 默认：未填上下文、25%、不设上限
            (128_000, 25, 0, (123_904, 30976)),
            (128_000, 25, 4800, (123_904, 4800)),  # 上限更小就取上限
            (32768, 100, 2400, (28672, 2400)),  # 迁移后的形态：百分比不起限制作用
            (4096, 25, 0, (0, 1)),  # base 为 0 时至少 1（与 ST 的 `|| 1` 一致）
            (1000, 25, 0, (0, 1)),
            (32768, 0, 0, (28672, 287)),  # 百分比夹到 1
            (32768, 500, 0, (28672, 28672)),  # 百分比夹到 100
            (32768, 25, 10**9, (28672, 7168)),  # 上限夹到 65536
            (300_000, 25, 0, (295_904, 65536)),  # 上限 0 按百分比算，但不超过硬上限
            (300_000, 25, 70_000, (295_904, 65536)),  # 上限本身也夹到 65536
            (300_000, 25, 50_000, (295_904, 50_000)),
        ],
    )
    def test_matches_sillytavern(self, context: int, percent: int, cap: int, expected: tuple[int, int]) -> None:
        assert compute_budget(context, percent, cap) == expected

    @pytest.mark.parametrize("percent", [25, 100])
    def test_cap_zero_with_a_ten_million_context_stops_at_the_hard_cap(self, percent: int) -> None:
        """上限 0 不是没有边：模型组上下文能填到一千万，25% 就是每轮两百多万 token"""
        assert compute_budget(10_000_000, percent, 0) == (10_000_000 - 4096, HARD_CAP_TOKENS)
        assert HARD_CAP_TOKENS == 65536

    def test_the_hard_cap_reaches_resolve_budget(self, groups: Dict[str, Any]) -> None:
        groups["huge"] = ModelConfigGroup(CHAT_MODEL="m", CONTEXT_LENGTH=10_000_000)
        budget = resolve_budget(effective(USE_MODEL_GROUP="huge", WORLDBOOK_BUDGET_CAP_TOKENS=0))
        assert (budget.cap_tokens, budget.budget) == (0, 65536)

    @pytest.mark.parametrize(("context", "small"), [(0, False), (1, True), (4096, True), (4097, False), (-3, False)])
    def test_small_context(self, context: int, small: bool) -> None:
        """填了、但不超过回复预留：预算只有 1 token（0 是没填，按 32768 算，不算小）"""
        assert is_small_context(context) is small
        if small:
            assert compute_budget(context, 100, 0)[1] == 1

    def test_constants(self) -> None:
        assert OUTPUT_RESERVE_TOKENS == 4096
        assert DEFAULT_CONTEXT_LENGTH == 32768


class TestSmallContextWarning:
    """上下文长度 1 ~ 4096 不拦，但保存时与启动时各记一条 WARNING（模型组页面上另有提示）"""

    def test_the_helper(self, warnings_logged: List[str]) -> None:
        assert model_group_service.warn_if_context_too_small("tiny", 4096) is True
        assert model_group_service.warn_if_context_too_small("unset", 0) is False
        assert model_group_service.warn_if_context_too_small("normal", 8192) is False
        assert len(warnings_logged) == 1
        assert "'tiny'" in warnings_logged[0] and "1 token" in warnings_logged[0]

    async def test_create_and_update_warn(
        self,
        model_group_db: None,
        monkeypatch: pytest.MonkeyPatch,
        warnings_logged: List[str],
    ) -> None:
        async def no_refresh(persist: bool = True) -> Dict[str, Any]:  # 不碰运行时视图与配置文件
            return {}

        monkeypatch.setattr(model_group_service, "refresh_runtime_view", no_refresh)
        identity = model_group_service.resolve_console_identity(platform_owner())

        row = await model_group_service.create_group(
            identity, "ctx_tiny", ModelConfigGroup(CHAT_MODEL="m", CONTEXT_LENGTH=2048)
        )
        assert [message for message in warnings_logged if "ctx_tiny" in message]

        warnings_logged.clear()
        await model_group_service.update_group(identity, row, ModelConfigGroup(CHAT_MODEL="m", CONTEXT_LENGTH=8192))
        assert not [message for message in warnings_logged if "ctx_tiny" in message]

        await model_group_service.update_group(identity, row, ModelConfigGroup(CHAT_MODEL="m", CONTEXT_LENGTH=100))
        assert [message for message in warnings_logged if "ctx_tiny" in message and "100" in message]


class TestResolve:
    def test_the_group_context_drives_the_budget(self, groups: Dict[str, Any]) -> None:
        budget = resolve_budget(effective(USE_MODEL_GROUP="big"))
        assert budget.context_length == 128_000 and budget.context_length_set is True
        assert budget.budget == 30976
        assert budget.factor == 1.29 and budget.factor_source == "model_name" and budget.factor_family == "claude"

    def test_unset_context_is_32768(self, groups: Dict[str, Any]) -> None:
        budget = resolve_budget(effective(USE_MODEL_GROUP="plain"))
        assert budget.context_length == DEFAULT_CONTEXT_LENGTH and budget.context_length_set is False
        assert budget.budget == 7168
        assert budget.factor == 0.80

    def test_a_missing_group_still_gives_a_budget(self, groups: Dict[str, Any]) -> None:
        budget = resolve_budget(effective(USE_MODEL_GROUP="gone"))
        assert budget.model_group == "gone"
        assert budget.budget == 7168 and budget.factor == 1.0 and budget.factor_source == "default"

    def test_the_group_factor_wins(self, groups: Dict[str, Any]) -> None:
        budget = resolve_budget(effective(USE_MODEL_GROUP="tuned"))
        assert budget.factor == 0.75 and budget.factor_source == "group"
        assert budget.count("测试正文") == tokens.count_tokens("测试正文", 0.75)

    def test_a_smaller_fallback_group_is_recorded(self, groups: Dict[str, Any]) -> None:
        budget = resolve_budget(effective(USE_MODEL_GROUP="big", FALLBACK_MODEL_GROUP="small"))
        assert (budget.fallback_model_group, budget.fallback_context_length) == ("small", 16_000)

        larger = resolve_budget(effective(USE_MODEL_GROUP="small", FALLBACK_MODEL_GROUP="big"))
        assert (larger.fallback_model_group, larger.fallback_context_length) == ("", 0)

    def test_as_dict_carries_the_approximate_chars(self, groups: Dict[str, Any]) -> None:
        data = resolve_budget(effective()).as_dict()
        assert data["budget"] == 7168 and data["approx_chars"] > 0
        assert {"model_group", "percent", "cap_tokens", "base", "factor", "estimator"} <= set(data)


class TestBudgetBasisEndpoint:
    async def test_system_scope(self, groups: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "USE_MODEL_GROUP", "big")
        monkeypatch.setattr(config, "WORLDBOOK_BUDGET_PERCENT", 25)
        monkeypatch.setattr(config, "WORLDBOOK_BUDGET_CAP_TOKENS", 0)

        basis = await config_router.get_worldbook_budget_basis("system", _current_user=platform_owner())

        assert basis.model_group == "big" and basis.context_length == 128_000
        assert basis.budget == 30976 and basis.output_reserve == 4096
        assert basis.inherited_percent is None and basis.inherited_cap_tokens is None
        assert basis.tokens_per_char == round(0.78 * 1.29, 4)

    async def test_override_layer_uses_upper_values_plus_its_own_overrides(
        self,
        groups: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        layer = OverridableConfig()
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 3000
        layer.WORLDBOOK_BUDGET_PERCENT = 90  # 覆盖开关关着：不参与

        async def accessible(_user: DBUser, _key: str, *, for_write: bool = True) -> None:
            return None

        async def inherited(_key: str) -> Dict[str, Any]:
            return {
                "USE_MODEL_GROUP": "big",
                "FALLBACK_MODEL_GROUP": "big",
                "WORLDBOOK_BUDGET_PERCENT": 25,
                "WORLDBOOK_BUDGET_CAP_TOKENS": 4800,
            }

        monkeypatch.setattr(config_router, "_assert_config_key_accessible", accessible)
        monkeypatch.setattr(config_resolver_module.config_resolver, "inherited_values", inherited)
        monkeypatch.setattr(UnifiedConfigService, "_get_config_instance", staticmethod(lambda _key: layer))

        basis = await config_router.get_worldbook_budget_basis(CHANNEL_KEY, _current_user=tenant_admin())

        assert basis.percent == 25 and basis.cap_tokens == 3000 and basis.budget == 3000
        assert basis.inherited_percent == 25 and basis.inherited_cap_tokens == 4800

    async def test_tenant_cannot_read_another_tenants_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读权限与 `/list/{config_key}` 同一个判据：判据拒了，接口就拒"""

        async def refuse(_user: DBUser, _key: str, *, for_write: bool = True) -> None:
            raise config_router.NotFoundError(resource="频道")

        monkeypatch.setattr(config_router, "_assert_config_key_accessible", refuse)
        with pytest.raises(config_router.NotFoundError):
            await config_router.get_worldbook_budget_basis(CHANNEL_KEY, _current_user=tenant_admin())


class TestMigrationNotice:
    async def test_only_the_platform_sees_and_acks_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "WORLDBOOK_BUDGET_MIGRATION_NOTICE", ["全局：原 6000 字 → 上限 4800 token"])
        saved: list[bool] = []
        monkeypatch.setattr(core_config_module, "save_config", lambda: saved.append(True))

        assert (await config_router.get_worldbook_budget_notice(_current_user=tenant_admin())).lines == []
        assert (await config_router.get_worldbook_budget_notice(_current_user=platform_owner())).lines == [
            "全局：原 6000 字 → 上限 4800 token",
        ]

        await config_router.ack_worldbook_budget_notice(_current_user=platform_owner(), _owner=platform_owner())

        assert config.WORLDBOOK_BUDGET_MIGRATION_NOTICE == []
        assert saved == [True]

    @pytest.mark.parametrize("name", MIGRATION_KEYS)
    def test_the_migration_keys_are_platform_only(self, name: str) -> None:
        assert config_router._is_platform_only_config("system", name), name  # noqa: SLF001

    async def test_tenant_reads_of_system_config_leave_them_out(
        self,
        model_group_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """迁移提示里是各实例、频道的会话键（带 QQ 号）：租户的系统配置列表里没有这两项，单项读按「不存在」"""
        await DBTenant.create(id=1, name="acme", display_name="acme")
        monkeypatch.setattr(config, "WORLDBOOK_BUDGET_MIGRATION_NOTICE", [NOTICE_LINE])
        monkeypatch.setattr(config, "WORLDBOOK_BUDGET_MIGRATED", True)

        items = await config_router.get_config_list(config_key="system", _current_user=tenant_admin())
        keys = {item.key for item in items}
        assert not keys & set(MIGRATION_KEYS)
        assert "WORLDBOOK_BUDGET_CAP_TOKENS" in keys, "预算的两个旋钮租户照样看得到"
        assert NOTICE_LINE not in str([item.value for item in items])
        for name in MIGRATION_KEYS:
            with pytest.raises(ConfigNotFoundError):
                await config_router.get_config_item(config_key="system", item_key=name, _current_user=tenant_admin())

    async def test_the_platform_still_reads_them(self, model_group_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "WORLDBOOK_BUDGET_MIGRATION_NOTICE", [NOTICE_LINE])

        items = await config_router.get_config_list(config_key="system", _current_user=platform_owner())
        assert set(MIGRATION_KEYS) <= {item.key for item in items}
        item = await config_router.get_config_item(
            config_key="system",
            item_key="WORLDBOOK_BUDGET_MIGRATION_NOTICE",
            _current_user=platform_owner(),
        )
        assert item.value == [NOTICE_LINE]


class TestConfigItems:
    def test_sliders_carry_their_bounds_and_markers(self) -> None:
        items = {item["key"]: item for item in ConfigService.get_config_list(CoreConfig())}

        percent = items["WORLDBOOK_BUDGET_PERCENT"]
        assert (percent["minimum"], percent["maximum"], percent["slider"]) == (1, 100, True)
        cap = items["WORLDBOOK_BUDGET_CAP_TOKENS"]
        assert (cap["minimum"], cap["maximum"], cap["slider"], cap["zero_means_unlimited"]) == (0, 65536, True, True)
        assert items["WORLDBOOK_MAX_CHARS"]["is_hidden"] is True
        assert items["WORLDBOOK_BUDGET_MIGRATED"]["is_hidden"] is True

    @pytest.mark.parametrize(
        ("name", "credential"),
        [
            ("WORLDBOOK_BUDGET_CAP_TOKENS", False),
            ("MAX_TOKENS", False),
            ("MIN_TOKENS", False),
            ("CAP_TOKENS", False),
            ("BOT_TOKEN", True),
            ("BOT_TOKENS", True),
            ("ACCESS_TOKEN", True),
            ("EXTRA_TOKENS", True),
            ("TOKEN", True),
        ],
    )
    def test_the_token_cap_is_not_taken_for_a_credential(self, name: str, credential: bool) -> None:
        """按名兜底的凭据判据只排除「多少个 token」的计数前缀，复数的凭据名照样算凭据"""
        assert config_router._is_credential_field_name(name) is credential  # noqa: SLF001

    def test_tenant_sees_the_token_cap_as_a_plain_number(self) -> None:
        """租户读系统配置走的那两步（列表 + 脱敏）：上限项不标 `is_secret`、值原样"""
        cfg = CoreConfig()
        cfg.WORLDBOOK_BUDGET_CAP_TOKENS = 4800
        items = config_router._mask_config_items(ConfigService.get_config_list(cfg))  # noqa: SLF001
        cap = next(item for item in items if item["key"] == "WORLDBOOK_BUDGET_CAP_TOKENS")
        assert not cap.get("is_secret")
        assert cap["value"] == 4800

    def test_marker_pairing_is_checked_at_import(self) -> None:
        class Unpaired(ConfigBase):
            CAP: int = Field(default=0, json_schema_extra=ExtraField(zero_means_unlimited=True).model_dump())

        class WrongType(ConfigBase):
            CAP: str = Field(
                default="0",
                json_schema_extra=ExtraField(zero_means_unlimited=True, tenant_may_only_lower=True).model_dump(),
            )

        with pytest.raises(TypeError, match="tenant_may_only_lower"):
            check_tenant_limit_markers(Unpaired)
        with pytest.raises(TypeError, match="不是 int"):
            check_tenant_limit_markers(WrongType)
        check_tenant_limit_markers(CoreConfig)


@pytest.fixture
async def model_group_db() -> AsyncIterator[None]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


class TestModelGroupFields:
    async def test_round_trip(self, model_group_db: None) -> None:
        row = await DBModelGroup.create(
            name="ctx_roundtrip", chat_model="m", context_length=200_000, token_count_factor=1.2
        )
        group = row.to_config_group()
        assert (group.CONTEXT_LENGTH, group.TOKEN_COUNT_FACTOR) == (200_000, 1.2)

        group = ModelConfigGroup(CHAT_MODEL="m", CONTEXT_LENGTH=8000, TOKEN_COUNT_FACTOR=0.9)
        row.apply_config_group(group)
        await row.save()
        loaded = await DBModelGroup.get(name="ctx_roundtrip")
        assert (loaded.context_length, loaded.token_count_factor) == (8000, 0.9)

    async def test_bad_stored_values_are_clamped_not_fatal(self, model_group_db: None) -> None:
        """手改库写出越界值：这一组照样进运行时视图，只是夹回合法区间"""
        row = await DBModelGroup.create(name="ctx_bad", chat_model="m", context_length=-5, token_count_factor=-1.0)
        assert (row.to_config_group().CONTEXT_LENGTH, row.to_config_group().TOKEN_COUNT_FACTOR) == (0, 0.0)
        row.context_length = 10**12
        row.token_count_factor = 1e9
        assert (row.to_config_group().CONTEXT_LENGTH, row.to_config_group().TOKEN_COUNT_FACTOR) == (10_000_000, 10.0)

    def test_payload_clearing_means_zero(self) -> None:
        base = ModelConfigGroup(CHAT_MODEL="m", CONTEXT_LENGTH=128_000, TOKEN_COUNT_FACTOR=1.1)
        payload = console_model_groups.ModelGroupPayload.model_validate(
            {"context_length": None, "token_count_factor": None}
        )

        merged = console_model_groups._merge_payload(base, payload)  # noqa: SLF001

        assert (merged.CONTEXT_LENGTH, merged.TOKEN_COUNT_FACTOR) == (0, 0.0)

    @pytest.mark.parametrize(
        "body",
        [{"context_length": -1}, {"context_length": 10**8}, {"token_count_factor": -0.1}, {"token_count_factor": 11}],
    )
    def test_out_of_range_payload_is_rejected(self, body: Dict[str, Any]) -> None:
        with pytest.raises(PydanticValidationError):
            console_model_groups.ModelGroupPayload.model_validate(body)

    def test_omitting_the_fields_keeps_them(self) -> None:
        base = ModelConfigGroup(CHAT_MODEL="m", CONTEXT_LENGTH=128_000, TOKEN_COUNT_FACTOR=1.1)
        merged = console_model_groups._merge_payload(  # noqa: SLF001
            base,
            console_model_groups.ModelGroupPayload.model_validate({"chat_model": "n"}),
        )
        assert (merged.CHAT_MODEL, merged.CONTEXT_LENGTH, merged.TOKEN_COUNT_FACTOR) == ("n", 128_000, 1.1)
