"""模型组引用的写入闸门与运行时兜底（P0：租户不得花平台方的 Key）

## 这里钉的是什么

`can_view_group()` 此前**只在读路径用**（`/config/model-groups`、`/console/model-groups`）。
写路径完全不看它：`_assert_tenant_may_write` 只管三件事——平台方边界前缀 `OPS_BOT_`、
承载凭据的字段、配额天花板，对 `ref_model_groups=True` 的字段**只当普通字符串**。

于是租户对自己实例（或自己频道）写一次 `USE_MODEL_GROUP=<平台 private 组名>`，
此后每一轮对话都走那把 Key。这不是密钥外泄（Key 不进响应体），而是**授权与计费边界被绕过**，
`visibility=private` 这个字段从此没有意义——而它正是 R19「平台模型组 shared/private」的执行点。

这与已修的配额天花板是同一条边界上的另一扇门（`routers/console/quota.py` 的注释亲口写过
「通用配置接口是同一条边界上的另一扇门」），模型组这一侧那扇门此前还开着。

## 三层断言

1. **写入闸门**（`TestTenantMayNotSelectForeignGroup`）：四档归属 × 三条写路径，
   每档都有平台方正向对照。不可见与不存在同样 404 —— 若"存在但不可见"回 403，
   403 与 200 的差异本身就是一个组名存在性探针。
2. **运行时兜底**（`TestRuntimeScopeGuard`）：接口层只管住"从今往后"，存量覆盖文件里
   可能早就写着越权的组名。越界引用就地回落 + WARNING，不抛异常。
3. **接线**（`TestTheGuardsAreActuallyWiredIn`，见 PROJECT_STATE §2.7）：
   零件测得再漂亮也不构成"这段逻辑在生产路径上跑得到"的证据。把
   `_assert_tenant_may_write` 里那次调用删掉、或把 `run_agent` 里那次调用删掉，
   必须各自有用例变红。写路径有**三个**调用点，每个都单独有红（§2.7 round-14 的教训）。

## 判定不许有第二份实现

归属判定一律复用 `model_group_service.can_view_group`（经 `get_visible_group` /
`assert_model_group_selectable`）。"引用了模型组"这件事一律由 `ref_model_groups` 标记驱动
（`iter_model_group_ref_fields`），不硬编码字段名——`TestRefMarkerDrivesEverything`
把这条钉住，且在有人给别的"受归属约束的资源"加可覆盖引用时变红。
"""

import ast
import datetime
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

import pytest
from pydantic import Field
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.core.core_utils import ConfigBase, ExtraField
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.config import (
    BatchUpdateConfig,
    batch_update_config,
    set_config_value,
    update_instance_config,
)
from nekro_agent.routers.plugins import (
    SavePluginConfigRequest,
    save_plugin_instance_config_handler,
)
from nekro_agent.schemas.errors import ModelGroupNotFoundError
from nekro_agent.schemas.tenant import CREDENTIAL_MASK, TenantRole
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.config_service import ConfigService, UnifiedConfigService
from nekro_agent.services.model_group_service import (
    ModelGroupRefField,
    assert_model_group_selectable,
    enforce_channel_model_group_scope,
    enforce_model_group_scope,
    is_model_group_ref_item,
    iter_model_group_ref_fields,
    resolve_console_identity,
    runtime_identity,
)
from nekro_agent.services.plugin import scope as plugin_scope
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
OTHER_TENANT_ID = 2

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"
PLATFORM_INSTANCE = "plat1"

OWN_CHAT_KEY = "onebot_v11-inst1-group_10001"
PLATFORM_CHAT_KEY = "onebot_v11-plat1-group_30001"

OWN_INSTANCE_CONFIG_KEY = "instance_override_onebot_v11__inst1"
OWN_CHANNEL_CONFIG_KEY = f"channel_config_{OWN_CHAT_KEY}"

# 模型组的四档归属。名字直接写成结论，用例失败时不用回头翻固件。
PLATFORM_PRIVATE = "platform_private"
PLATFORM_SHARED = "platform_shared"
ACME_OWN = "acme_own"
RIVAL_OWN = "rival_own"
MISSING = "no_such_group"
# `USE_MODEL_GROUP` 等字段的源码声明默认值，同时是回落阶梯的第一候选
DEFAULT_GROUP = "default"
EMBEDDING_GROUP = "text-embedding"

# (归属档位, 租户身份下的期望结果)。None 表示放行。
# 不存在的组名与"存在但不可见"必须给同一个结论，否则 404/200 的差异就是存在性探针。
SELECT_CASES: List[Tuple[str, Optional[Type[Exception]]]] = [
    (PLATFORM_PRIVATE, ModelGroupNotFoundError),
    (PLATFORM_SHARED, None),
    (ACME_OWN, None),
    (RIVAL_OWN, ModelGroupNotFoundError),
    (MISSING, ModelGroupNotFoundError),
    ("", None),
]

# 四个 `overridable=True` 且引用模型组的字段。这份名单**只用于断言枚举结果**，
# 判定本身一个字段名都不写死（见 `TestRefMarkerDrivesEverything`）。
OVERRIDABLE_REF_FIELDS = (
    "USE_MODEL_GROUP",
    "DEBUG_MIGRATION_MODEL_GROUP",
    "FALLBACK_MODEL_GROUP",
    "AFFECTION_MODEL_GROUP",
)


def make_user(*, user_id: int, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份（判定只读 tenant_id / tenant_role / perm_level）"""
    user = DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )
    user.id = user_id
    return user


def acme_admin() -> DBUser:
    return make_user(user_id=11, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(user_id=1, tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


class ConfigServiceSpy:
    """一份不落盘的配置服务

    `UnifiedConfigService._get_config_instance()` 对覆盖类配置键会 `dump_config()` 落盘，
    因此整份服务在这里换成 spy。配置项**元数据仍取自真实的 `OverridableConfig`**——
    `ref_model_groups` 这个标记是被测判据的输入，伪造它等于在测另一件事（§2.9）。
    """

    def __init__(self) -> None:
        self.written: List[Tuple[str, Dict[str, str]]] = []
        self._probe = OverridableConfig()

    def get_config_item(self, config_key: str, item_key: str) -> Optional[Dict[str, Any]]:
        return ConfigService.get_config_item(self._probe, item_key)

    def set_config_value(self, config_key: str, item_key: str, value: str) -> Tuple[bool, str]:
        self.written.append((config_key, {item_key: value}))
        return True, ""

    def batch_update_config(self, config_key: str, configs: Dict[str, str]) -> Tuple[bool, Optional[str]]:
        self.written.append((config_key, dict(configs)))
        return True, None

    def save_config(self, config_key: str) -> Tuple[bool, Optional[str]]:
        return True, None


@pytest.fixture
async def spy(monkeypatch: pytest.MonkeyPatch):
    """两个租户 + 一个平台方实例，四档归属的模型组，外加不落盘的配置服务"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()

    for instance_key, tenant_id, chat_key in (
        (OWN_INSTANCE, ACME_TENANT_ID, OWN_CHAT_KEY),
        (FOREIGN_INSTANCE, OTHER_TENANT_ID, "onebot_v11-rival1-group_20001"),
        (PLATFORM_INSTANCE, None, PLATFORM_CHAT_KEY),
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
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
            chat_key=chat_key,
            channel_type="group",
            channel_name=chat_key,
            data="{}",
        )

    # 建表顺序即 `refresh_runtime_view()` 的 `order_by("id")`，回落阶梯第二档依赖这个顺序
    for name, ownership, owner_tenant_id, visibility, model_type in (
        (DEFAULT_GROUP, "platform", None, "shared", "chat"),
        (EMBEDDING_GROUP, "platform", None, "shared", "embedding"),
        (PLATFORM_SHARED, "platform", None, "shared", "chat"),
        (PLATFORM_PRIVATE, "platform", None, "private", "chat"),
        (ACME_OWN, "tenant", ACME_TENANT_ID, "private", "chat"),
        (RIVAL_OWN, "tenant", OTHER_TENANT_ID, "private", "chat"),
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
            model_type=model_type,
        )

    service_spy = ConfigServiceSpy()
    monkeypatch.setattr(UnifiedConfigService, "get_config_item", service_spy.get_config_item)
    monkeypatch.setattr(UnifiedConfigService, "set_config_value", service_spy.set_config_value)
    monkeypatch.setattr(UnifiedConfigService, "batch_update_config", service_spy.batch_update_config)
    monkeypatch.setattr(UnifiedConfigService, "save_config", service_spy.save_config)

    previous_groups = config.MODEL_GROUPS
    config.MODEL_GROUPS = {row.name: row.to_config_group() for row in await DBModelGroup.all().order_by("id")}
    try:
        yield service_spy
    finally:
        config.MODEL_GROUPS = previous_groups
        await Tortoise.close_connections()


def make_effective_config(**overrides: Any) -> CoreConfig:
    """构造一份频道有效配置，形态与 `ConfigResolver.get_effective_config()` 一致"""
    data = config.model_dump()
    data.update(overrides)
    return CoreConfig(**data)


async def _write_via_set(user: DBUser, item_key: str, value: str) -> None:
    await set_config_value(
        config_key=OWN_INSTANCE_CONFIG_KEY,
        item_key=item_key,
        value=value,
        _current_user=user,
    )


async def _write_via_batch(user: DBUser, item_key: str, value: str) -> None:
    await batch_update_config(
        config_key=OWN_CHANNEL_CONFIG_KEY,
        body=BatchUpdateConfig(configs={item_key: value}),
        _current_user=user,
    )


async def _write_via_instance_put(user: DBUser, item_key: str, value: str) -> None:
    await update_instance_config(
        adapter_key=ADAPTER,
        instance_key=OWN_INSTANCE,
        body=BatchUpdateConfig(configs={item_key: value}),
        _current_user=user,
    )


# 三条写路径，全部汇到 `_assert_tenant_may_write`。§2.7 的教训：接线用例要按**调用点**数，
# 一个调用点一条红；只测其中一条，另外两条被删掉时测试全绿。
WRITE_PATHS = [
    pytest.param(_write_via_set, id="POST /config/set"),
    pytest.param(_write_via_batch, id="POST /config/batch"),
    pytest.param(_write_via_instance_put, id="PUT /config/instance"),
]


class TestTenantMayNotSelectForeignGroup:
    """四档归属 × 三条写路径，每档都有平台方正向对照"""

    @pytest.mark.parametrize("write", WRITE_PATHS)
    @pytest.mark.parametrize(("group_name", "expected"), SELECT_CASES)
    @pytest.mark.parametrize("item_key", OVERRIDABLE_REF_FIELDS)
    async def test_tenant_write(
        self,
        spy: ConfigServiceSpy,
        write: Any,
        item_key: str,
        group_name: str,
        expected: Optional[Type[Exception]],
    ) -> None:
        user = acme_admin()
        spy.written.clear()
        if expected is None:
            await write(user, item_key, group_name)
            assert spy.written, f"{item_key}={group_name!r} 应当放行，却一个字都没写下去"
        else:
            with pytest.raises(expected):
                await write(user, item_key, group_name)
            # 闸门必须挡在服务层之前：判定跑在写入之后等于越权值已经落盘
            assert spy.written == []

    @pytest.mark.parametrize("write", WRITE_PATHS)
    @pytest.mark.parametrize("group_name", [name for name, _ in SELECT_CASES])
    @pytest.mark.parametrize("item_key", OVERRIDABLE_REF_FIELDS)
    async def test_platform_owner_is_never_filtered(
        self,
        spy: ConfigServiceSpy,
        write: Any,
        item_key: str,
        group_name: str,
    ) -> None:
        """未启用多租户的部署里全部账号都走这条分支，行为必须与改造前逐字一致"""
        spy.written.clear()
        await write(platform_owner(), item_key, group_name)
        assert spy.written

    @pytest.mark.parametrize("write", WRITE_PATHS)
    async def test_existence_is_not_probeable(self, spy: ConfigServiceSpy, write: Any) -> None:
        """「存在但不可见」与「根本不存在」对租户必须完全不可区分

        若前者回 403、后者放行，租户就能靠状态码差异把平台方与别的租户的组名逐个试出来。
        """
        user = acme_admin()
        results: List[Tuple[Type[Exception], int]] = []
        for name in (PLATFORM_PRIVATE, RIVAL_OWN, MISSING):
            with pytest.raises(Exception) as exc_info:  # noqa: PT011
                await write(user, "USE_MODEL_GROUP", name)
            results.append((type(exc_info.value), exc_info.value.http_status))
        assert results == [(ModelGroupNotFoundError, 404)] * 3

    @pytest.mark.parametrize("write", WRITE_PATHS)
    async def test_mask_sentinel_is_not_a_reference(self, spy: ConfigServiceSpy, write: Any) -> None:
        """掩码哨兵表示"保持不变"，会被整个剔除，不该被当成一次模型组引用去校验"""
        spy.written.clear()
        await write(acme_admin(), "USE_MODEL_GROUP", CREDENTIAL_MASK)
        for _config_key, configs in spy.written:
            assert "USE_MODEL_GROUP" not in configs

    async def test_non_ref_fields_are_untouched(self, spy: ConfigServiceSpy) -> None:
        """反向钉住：不带 `ref_model_groups` 的字段不受本次改动影响

        判定误伤的话租户就再也改不了自己实例的普通配置了。
        """
        spy.written.clear()
        await _write_via_set(acme_admin(), "AI_CHAT_CONTEXT_MAX_LENGTH", PLATFORM_PRIVATE)
        assert spy.written

    @pytest.mark.parametrize("item_key", [f"enable_{name}" for name in OVERRIDABLE_REF_FIELDS])
    async def test_the_override_toggles_are_not_references(self, spy: ConfigServiceSpy, item_key: str) -> None:
        """覆盖层的 `enable_X` 开关不是一次模型组引用

        `create_overridable_config_model` 给每个可覆盖字段派生一个 `enable_X: bool`，
        取值是 `"true"` / `"false"`。标记若顺手传到开关上，这道闸门会把 `"true"` 当组名去查、
        一律 404 —— 租户从此连"启用这项覆盖"都点不动，而且报的是「模型组不存在」，
        没有任何人能从这条报错联想到开关。这是**误伤**方向的钉子，与越权那侧同等重要。
        """
        spy.written.clear()
        await _write_via_set(acme_admin(), item_key, "true")
        assert spy.written == [(OWN_INSTANCE_CONFIG_KEY, {item_key: "true"})]


class TestAssertModelGroupSelectableItself:
    """闸门零件本身

    路由那一侧永远只在 `_is_tenant_view` 为真时才调到本函数，所以"平台方豁免"这条分支
    在接口层是够不着的 —— 但它是本函数对外承诺的契约（未启用多租户的部署、以及将来任何
    复用它的调用点都依赖它），够不着不等于不用钉。
    """

    @pytest.mark.parametrize(("group_name", "expected"), SELECT_CASES)
    async def test_tenant_identity(
        self,
        spy: ConfigServiceSpy,
        group_name: str,
        expected: Optional[Type[Exception]],
    ) -> None:
        identity = resolve_console_identity(acme_admin())
        if expected is None:
            await assert_model_group_selectable(group_name, identity)
        else:
            with pytest.raises(expected):
                await assert_model_group_selectable(group_name, identity)

    @pytest.mark.parametrize("group_name", [name for name, _ in SELECT_CASES])
    async def test_platform_identity_is_exempt(self, spy: ConfigServiceSpy, group_name: str) -> None:
        """平台方连"这个组存不存在"都不校验：改造前它就能写任意字符串，行为必须逐字不变"""
        await assert_model_group_selectable(group_name, resolve_console_identity(platform_owner()))


class TestRuntimeScopeGuard:
    """运行时兜底：存量覆盖文件里已经写着的越权值，接口层挡不住"""

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            # 越界的两档：就地回落到字段声明默认值（它归平台方且 shared，租户可见）
            (PLATFORM_PRIVATE, DEFAULT_GROUP),
            (RIVAL_OWN, DEFAULT_GROUP),
            # 合法的两档：一个字都不许动
            (PLATFORM_SHARED, PLATFORM_SHARED),
            (ACME_OWN, ACME_OWN),
            # 组名根本不存在：不是越权（不存在的组花不了任何人的 Key），
            # 原样留下，交给 run_agent 里既有的诊断行为
            (MISSING, MISSING),
            ("", ""),
        ],
    )
    async def test_tenant_channel(self, spy: ConfigServiceSpy, stored: str, expected: str) -> None:
        cfg = make_effective_config(USE_MODEL_GROUP=stored)
        await enforce_model_group_scope(cfg, tenant_id=ACME_TENANT_ID, scope_label="测试")
        assert cfg.USE_MODEL_GROUP == expected

    @pytest.mark.parametrize("stored", [PLATFORM_PRIVATE, RIVAL_OWN, PLATFORM_SHARED, ACME_OWN, MISSING])
    async def test_platform_owner_is_never_rewritten(self, spy: ConfigServiceSpy, stored: str) -> None:
        """平台方（含未启用多租户）一律原样返回，且不做任何归属判定"""
        cfg = make_effective_config(USE_MODEL_GROUP=stored)
        corrections = await enforce_model_group_scope(cfg, tenant_id=None)
        assert corrections == []
        assert cfg.USE_MODEL_GROUP == stored

    async def test_every_ref_field_is_covered(self, spy: ConfigServiceSpy) -> None:
        """四个字段一起越界时四个一起被纠正——判定按标记遍历，不是只盯着主模型组"""
        cfg = make_effective_config(**dict.fromkeys(OVERRIDABLE_REF_FIELDS, PLATFORM_PRIVATE))
        corrections = await enforce_model_group_scope(cfg, tenant_id=ACME_TENANT_ID)
        assert {name for name, _, _ in corrections} >= set(OVERRIDABLE_REF_FIELDS)
        for field_name in OVERRIDABLE_REF_FIELDS:
            assert getattr(cfg, field_name) != PLATFORM_PRIVATE

    async def test_empty_default_falls_back_to_unset(self, spy: ConfigServiceSpy) -> None:
        """声明默认值为空串的字段回落到空串——那正是它设计好的"未设置=回落到主模型组"语义"""
        cfg = make_effective_config(AFFECTION_MODEL_GROUP=PLATFORM_PRIVATE)
        await enforce_model_group_scope(cfg, tenant_id=ACME_TENANT_ID)
        assert cfg.AFFECTION_MODEL_GROUP == ""

    async def test_falls_back_to_first_visible_group_of_matching_type(self, spy: ConfigServiceSpy) -> None:
        """默认组本身也不可见时，取运行时视图里第一个可见且模型类型匹配的组"""
        await DBModelGroup.filter(name=DEFAULT_GROUP).update(visibility="private")
        cfg = make_effective_config(USE_MODEL_GROUP=PLATFORM_PRIVATE)
        await enforce_model_group_scope(cfg, tenant_id=ACME_TENANT_ID)
        # `text-embedding` 按 id 排在更前面，但它是 embedding 组，不该被挑给 chat 引用
        assert cfg.USE_MODEL_GROUP == PLATFORM_SHARED

    async def test_no_legal_group_leaves_it_unset(self, spy: ConfigServiceSpy) -> None:
        """一个可见组都没有时置空并保持可诊断，而不是把越权值留在那儿继续花别人的 Key"""
        await DBModelGroup.filter(ownership="platform").update(visibility="private")
        await DBModelGroup.filter(name=ACME_OWN).delete()
        cfg = make_effective_config(USE_MODEL_GROUP=PLATFORM_PRIVATE)
        corrections = await enforce_model_group_scope(cfg, tenant_id=ACME_TENANT_ID)
        assert cfg.USE_MODEL_GROUP == ""
        assert ("USE_MODEL_GROUP", PLATFORM_PRIVATE, "") in corrections

    async def test_is_idempotent(self, spy: ConfigServiceSpy) -> None:
        """有效配置是带缓存的对象，第二次跑不能再改一次"""
        cfg = make_effective_config(USE_MODEL_GROUP=PLATFORM_PRIVATE)
        await enforce_model_group_scope(cfg, tenant_id=ACME_TENANT_ID)
        assert await enforce_model_group_scope(cfg, tenant_id=ACME_TENANT_ID) == []
        assert cfg.USE_MODEL_GROUP == DEFAULT_GROUP

    @pytest.mark.parametrize(
        ("instance_key", "stored", "expected"),
        [
            (OWN_INSTANCE, PLATFORM_PRIVATE, DEFAULT_GROUP),
            # 平台方自有实例：不进任何归属判定
            (PLATFORM_INSTANCE, PLATFORM_PRIVATE, PLATFORM_PRIVATE),
            # 实例不存在 / 单实例部署（instance_key 为空）：同样原样返回
            ("nosuch", PLATFORM_PRIVATE, PLATFORM_PRIVATE),
            ("", PLATFORM_PRIVATE, PLATFORM_PRIVATE),
        ],
    )
    async def test_channel_entry_resolves_tenant_from_instance(
        self,
        spy: ConfigServiceSpy,
        instance_key: str,
        stored: str,
        expected: str,
    ) -> None:
        cfg = make_effective_config(USE_MODEL_GROUP=stored)
        await enforce_channel_model_group_scope(cfg, instance_key=instance_key, chat_key=OWN_CHAT_KEY)
        assert cfg.USE_MODEL_GROUP == expected

    async def test_corrections_report_field_and_both_values(self, spy: ConfigServiceSpy) -> None:
        """降级必须可辨识：调用方拿得到"哪个字段、原值是什么、回落到了哪儿"三元组

        WARNING 日志与这个返回值同源（同一个循环里一起产出），断言在返回值上是因为
        loguru 不走 stdlib logging，caplog 抓不到它——对着抓不到的东西断言等于没断言（§2.11）。
        """
        cfg = make_effective_config(USE_MODEL_GROUP=PLATFORM_PRIVATE)
        corrections = await enforce_model_group_scope(cfg, tenant_id=ACME_TENANT_ID, scope_label="频道 X")
        assert corrections == [("USE_MODEL_GROUP", PLATFORM_PRIVATE, DEFAULT_GROUP)]


class TestRefMarkerDrivesEverything:
    """"引用了模型组"这件事是可枚举的，不是四个字段名硬编码在判定里"""

    def test_enumeration_covers_the_overridable_fields(self) -> None:
        names = {ref.name for ref in iter_model_group_ref_fields(CoreConfig)}
        assert names >= set(OVERRIDABLE_REF_FIELDS)

    def test_overridable_layer_keeps_the_marker(self) -> None:
        """覆盖层模型是动态派生的，标记必须跟着一起传下去——它才是租户真正写得到的那一层"""
        names = {ref.name for ref in iter_model_group_ref_fields(OverridableConfig)}
        assert names == set(OVERRIDABLE_REF_FIELDS)

    def test_declared_defaults_and_model_types(self) -> None:
        refs = {ref.name: ref for ref in iter_model_group_ref_fields(CoreConfig)}
        assert refs["USE_MODEL_GROUP"] == ModelGroupRefField("USE_MODEL_GROUP", "chat", DEFAULT_GROUP)
        assert refs["AFFECTION_MODEL_GROUP"] == ModelGroupRefField("AFFECTION_MODEL_GROUP", "chat", "")
        assert refs["MEMORY_EMBEDDING_MODEL_GROUP"].model_type == "embedding"

    def test_config_item_marker_is_read_from_the_real_metadata(self) -> None:
        probe = OverridableConfig()
        assert is_model_group_ref_item(ConfigService.get_config_item(probe, "USE_MODEL_GROUP"))
        assert not is_model_group_ref_item(ConfigService.get_config_item(probe, "AI_CHAT_CONTEXT_MAX_LENGTH"))
        assert not is_model_group_ref_item(None)

    def test_no_other_ownership_constrained_reference_is_overridable(self) -> None:
        """绊线：别的"受归属约束的资源"（人设 / 邮箱账户 / ……）一旦变成可覆盖字段，这里变红

        目前 `AI_CHAT_DEFAULT_PRESET_ID`（`ref_presets`）与邮箱账户引用都**不可覆盖**，
        租户写不到它们（`system` / `adapter_*` / `plugin_*` 对租户一律 403），
        因此不需要第二道闸门。哪天有人给它们标上 `overridable=True`，这条用例会先红——
        那时要做的是给对应资源接一道同形的判定，而不是把这条用例改掉。
        """
        offenders = []
        for name, field in CoreConfig.model_fields.items():
            extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
            if not extra.get("overridable"):
                continue
            refs = sorted(key for key, value in extra.items() if key.startswith("ref_") and value)
            if refs and refs != ["ref_model_groups"]:
                offenders.append((name, refs))
        assert offenders == []


def _run_agent_body() -> ast.AsyncFunctionDef:
    source = Path(inspect.getsourcefile(run_agent_module) or "").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_agent":
            return node
    raise AssertionError("run_agent 不见了")


class TestTheGuardsAreActuallyWiredIn:
    """零件全对、调用点没了 —— 测试会全绿（PROJECT_STATE §2.7）"""

    async def test_run_agent_guards_before_touching_the_model_group(
        self,
        spy: ConfigServiceSpy,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """行为化的接线用例：真的调 `run_agent`，断言它拿到的有效配置已被纠正

        把 `run_agent` 里那一行删掉，这条立刻红。人设那一步换成哨兵异常，
        用它把这一轮停在紧接着守卫的位置上——再往后就要连适配器与沙盒了，
        而那些与本条契约无关。
        """
        effective = make_effective_config(USE_MODEL_GROUP=PLATFORM_PRIVATE)

        class _StopRightAfterTheGuard(RuntimeError):
            pass

        async def _fake_effective_config(self: DBChatChannel) -> CoreConfig:
            return effective

        async def _stop(self: DBChatChannel) -> Any:
            raise _StopRightAfterTheGuard

        monkeypatch.setattr(DBChatChannel, "get_effective_config", _fake_effective_config)
        monkeypatch.setattr(DBChatChannel, "get_preset", _stop)

        with pytest.raises(_StopRightAfterTheGuard):
            await run_agent_module.run_agent(chat_key=OWN_CHAT_KEY)

        assert effective.USE_MODEL_GROUP == DEFAULT_GROUP

    async def test_run_agent_leaves_platform_channels_alone(
        self,
        spy: ConfigServiceSpy,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """同一条路径上的负向对照：平台方实例的频道逐字不变"""
        effective = make_effective_config(USE_MODEL_GROUP=PLATFORM_PRIVATE)

        class _Stop(RuntimeError):
            pass

        async def _fake_effective_config(self: DBChatChannel) -> CoreConfig:
            return effective

        async def _stop(self: DBChatChannel) -> Any:
            raise _Stop

        monkeypatch.setattr(DBChatChannel, "get_effective_config", _fake_effective_config)
        monkeypatch.setattr(DBChatChannel, "get_preset", _stop)

        with pytest.raises(_Stop):
            await run_agent_module.run_agent(chat_key=PLATFORM_CHAT_KEY)

        assert effective.USE_MODEL_GROUP == PLATFORM_PRIVATE

    def test_the_guard_runs_before_the_first_model_group_lookup(self) -> None:
        """守卫必须在 `config.MODEL_GROUPS[...]` **之前**

        上一条行为用例杀得掉"调用被删掉"，杀不掉"调用被挪到取模型组之后"——
        那时守卫仍然跑了，但本轮已经拿着越权的组去发请求了。
        """
        body = _run_agent_body()
        guard_lines = [
            node.lineno
            for node in ast.walk(body)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "enforce_channel_model_group_scope"
        ]
        lookup_lines = [
            node.lineno
            for node in ast.walk(body)
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "MODEL_GROUPS"
        ]
        assert guard_lines, "run_agent 里没有模型组归属守卫"
        assert lookup_lines, "run_agent 里没有模型组取用点，判据的另一半失效了（见 §2.10）"
        assert max(guard_lines) < min(lookup_lines)

    def test_the_guard_is_given_the_real_instance_key(self) -> None:
        """传空的 `instance_key` 会让守卫在入口直接返回，等价于把它删掉"""
        body = _run_agent_body()
        for node in ast.walk(body):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "enforce_channel_model_group_scope"
            ):
                passed = {
                    kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg
                }
                assert passed.get("instance_key") == "db_chat_channel.instance_key"
                return
        raise AssertionError("run_agent 里没有模型组归属守卫")

    @pytest.mark.parametrize("write", WRITE_PATHS)
    async def test_every_write_path_is_gated(self, spy: ConfigServiceSpy, write: Any) -> None:
        """三条写路径各有一个 `_assert_tenant_may_write` 调用点，每个都要有自己的红

        §2.7（round-14）：写接线用例前先数一遍这个 API 有几个调用点。
        只测其中一条的话，另外两条的守卫被删掉时测试全绿。
        """
        with pytest.raises(ModelGroupNotFoundError):
            await write(acme_admin(), "USE_MODEL_GROUP", PLATFORM_PRIVATE)

    def test_runtime_identity_reuses_can_view_group(self) -> None:
        """运行时身份只是 `can_view_group` 的入参适配，不是第二套归属判定"""
        assert runtime_identity(None).tenant_id is None
        assert runtime_identity(ACME_TENANT_ID).tenant_id == ACME_TENANT_ID
        source = inspect.getsource(enforce_model_group_scope)
        assert "can_view_group" in source


# ============================================================
# 第二个出口：插件的实例级配置覆盖
# ============================================================
#
# 通用配置那条路由（`instance_override_*` / `channel_config_*`）被 `_assert_tenant_may_write`
# 把住了，但**插件配置根本不走它**：`PUT /plugins/config/{plugin_key}/{instance_key}` 直接落
# `save_instance_config_overrides()`。而插件里的模型组引用与 `USE_MODEL_GROUP` 是同一个形状，
# 运行时同样是 `core_config.MODEL_GROUPS[<名字>]` 取真实 Key
# （`view_image.VISION_MODEL` / `draw.USE_DRAW_MODEL_GROUP` / `emotion.EMBEDDING_MODEL` /
# `email_utils.DEFAULT_SUMMARY_MODEL_GROUP`，且 `plugin.get_config()` 返回的是**按实例解析**的
# 代理，实例级覆盖在运行时确实生效）。作用域守卫在这条路径上是对的——写的确实是他自己的实例——
# 被绕过的是 `visibility=private` 这条授权与计费边界。

PLUGIN_REF_FIELDS = ("VISION_MODEL", "USE_DRAW_MODEL_GROUP")


class ProbeModelGroupPluginConfig(ConfigBase):
    """形态取自 `plugins/builtin/view_image.py` 与 `plugins/builtin/draw/plugin.py`

    两个字段的**名字与 `CoreConfig` 那四个完全不同**，是有意的：判定若退回成一份字段名单，
    这里立刻挡不住。`MODE` 是反向对照，不带标记的字段不该被这道闸门碰到。
    """

    VISION_MODEL: str = Field(
        default=DEFAULT_GROUP,
        title="视觉模型组",
        json_schema_extra=ExtraField(ref_model_groups=True, required=True, model_type="chat").model_dump(),
    )
    USE_DRAW_MODEL_GROUP: str = Field(
        default=DEFAULT_GROUP,
        title="绘图模型组",
        # `email_utils` 用的是等价的裸 dict 而不是 `ExtraField`，两种声明都必须认
        json_schema_extra={"ref_model_groups": True, "model_type": "draw"},
    )
    MODE: str = Field(default="fast", title="模式")


@pytest.fixture
def probe_plugin(tmp_path: Path):
    """一个只存在于本次测试里的插件，全局配置与实例级覆盖都落在 tmp_path

    落盘目录必须整体改道：实例级覆盖是真文件，写进真实 DATA_DIR 会污染开发机上的部署。
    """
    data_dir = tmp_path / "plugin_data"
    original = (plugin_scope.PLUGIN_DATA_DIR, plugin_scope.SCOPE_OVERRIDE_DIR)
    plugin_scope.PLUGIN_DATA_DIR = data_dir
    plugin_scope.SCOPE_OVERRIDE_DIR = data_dir / "_overrides"
    plugin_scope._scope_override_cache.clear()  # noqa: SLF001
    plugin_scope._scoped_config_cache.clear()  # noqa: SLF001

    probe = NekroPlugin(
        name="模型组引用探针",
        module_name="model_group_probe",
        description="仅用于模型组写入闸门测试",
        version="0.1.0",
        author="Probe",
        url="",
    )
    # 路径要在挂载配置类之前改掉：`mount_config()` 当场就会把默认值落盘一次
    probe._plugin_path = data_dir / probe.key  # noqa: SLF001
    probe._plugin_config_path = probe._plugin_path / "config.yaml"  # noqa: SLF001
    probe.mount_config()(ProbeModelGroupPluginConfig)

    plugin_collector.loaded_plugins[probe.key] = probe
    try:
        yield probe
    finally:
        plugin_collector.loaded_plugins.pop(probe.key, None)
        plugin_scope.PLUGIN_DATA_DIR, plugin_scope.SCOPE_OVERRIDE_DIR = original
        plugin_scope._scope_override_cache.clear()  # noqa: SLF001
        plugin_scope._scoped_config_cache.clear()  # noqa: SLF001


async def _write_plugin_config(user: DBUser, plugin_key: str, item_key: str, value: str) -> None:
    await save_plugin_instance_config_handler(
        plugin_key=plugin_key,
        instance_key=OWN_INSTANCE,
        body=SavePluginConfigRequest(configs={item_key: value}),
        _current_user=user,
    )


def _landed_overrides(plugin_key: str) -> Dict[str, Any]:
    """磁盘上真的落下了什么——断言在真文件上，不在 spy 上"""
    return plugin_scope.load_instance_config_overrides(plugin_key, OWN_INSTANCE)


class TestPluginInstanceConfigIsGatedToo:
    """插件实例级配置：同一条边界、同一个判定，第二个出口"""

    @pytest.mark.parametrize(("group_name", "expected"), SELECT_CASES)
    @pytest.mark.parametrize("item_key", PLUGIN_REF_FIELDS)
    async def test_tenant_write(
        self,
        spy: ConfigServiceSpy,
        probe_plugin: NekroPlugin,
        item_key: str,
        group_name: str,
        expected: Optional[Type[Exception]],
    ) -> None:
        user = acme_admin()
        if expected is None:
            await _write_plugin_config(user, probe_plugin.key, item_key, group_name)
            # 声明默认值本就"与全局一致"，那时不该落覆盖；其余（含空串）必须真的写下去
            if group_name != DEFAULT_GROUP:
                assert _landed_overrides(probe_plugin.key).get(item_key) == group_name
        else:
            with pytest.raises(expected):
                await _write_plugin_config(user, probe_plugin.key, item_key, group_name)
            # 闸门必须挡在落盘之前
            assert _landed_overrides(probe_plugin.key) == {}

    @pytest.mark.parametrize("group_name", [name for name, _ in SELECT_CASES])
    @pytest.mark.parametrize("item_key", PLUGIN_REF_FIELDS)
    async def test_platform_owner_is_never_filtered(
        self,
        spy: ConfigServiceSpy,
        probe_plugin: NekroPlugin,
        item_key: str,
        group_name: str,
    ) -> None:
        """未启用多租户的部署走的就是这条分支，行为必须与改造前逐字一致"""
        await _write_plugin_config(platform_owner(), probe_plugin.key, item_key, group_name)
        if group_name != DEFAULT_GROUP:
            assert _landed_overrides(probe_plugin.key).get(item_key) == group_name

    async def test_existence_is_not_probeable(self, spy: ConfigServiceSpy, probe_plugin: NekroPlugin) -> None:
        """与通用配置那侧同一个结论：不可见与不存在必须不可区分"""
        results: List[Tuple[Type[Exception], int]] = []
        for name in (PLATFORM_PRIVATE, RIVAL_OWN, MISSING):
            with pytest.raises(Exception) as exc_info:  # noqa: PT011
                await _write_plugin_config(acme_admin(), probe_plugin.key, "VISION_MODEL", name)
            results.append((type(exc_info.value), exc_info.value.http_status))
        assert results == [(ModelGroupNotFoundError, 404)] * 3

    async def test_plain_fields_are_untouched(self, spy: ConfigServiceSpy, probe_plugin: NekroPlugin) -> None:
        """反向对照：不带标记的字段不受这道闸门影响，租户照旧改得动自己实例的插件配置"""
        await _write_plugin_config(acme_admin(), probe_plugin.key, "MODE", PLATFORM_PRIVATE)
        assert _landed_overrides(probe_plugin.key) == {"MODE": PLATFORM_PRIVATE}

    async def test_mask_sentinel_is_not_a_reference(self, spy: ConfigServiceSpy, probe_plugin: NekroPlugin) -> None:
        """掩码哨兵在还原那一步就被整项剔除，不该被当成一次模型组引用去校验"""
        await _write_plugin_config(acme_admin(), probe_plugin.key, "VISION_MODEL", CREDENTIAL_MASK)
        assert _landed_overrides(probe_plugin.key) == {}

    def test_the_gate_is_marker_driven_not_a_field_name_list(self) -> None:
        """探针的两个字段名与 `CoreConfig` 那四个毫无交集，判定仍然拦得住

        这一条钉的是"判定按 `ref_model_groups` 标记走"：换成任何一份字段名单，
        插件这一侧的字段名必然不在那份名单里，闸门就会在这里漏掉。
        """
        assert {ref.name for ref in iter_model_group_ref_fields(ProbeModelGroupPluginConfig)} == set(PLUGIN_REF_FIELDS)
        assert not set(PLUGIN_REF_FIELDS) & set(OVERRIDABLE_REF_FIELDS)


def _scan_plugin_ref_markers() -> Dict[str, List[str]]:
    """扫出 `plugins/` 下每个源文件在**配置字段元数据里**声明过的 `ref_*` 标记

    两种写法都要认：`ExtraField(ref_model_groups=True)` 与等价的裸 dict
    `{"ref_model_groups": True}`（`email_utils` 就是后者）。

    判定按 AST 走而不是正则：`ref_` 开头的**普通函数关键字参数**在插件里是存在的
    （`history_travel.py` 的 `parse_chat_history_prompt(..., ref_mode=True)`），
    正则版第一次跑就把它误报成了一处配置标记 —— 词表放宽一格就全是噪声，
    而人对着噪声只会把断言改松（§2.11 / #38 的同一个坑）。

    扫描器自己的覆盖面由下面那条用例的**自检断言**兜住："0 命中"最常见的原因是扫描器坏了。
    """
    found: Dict[str, List[str]] = {}
    plugins_root = Path(__file__).resolve().parent.parent / "plugins"
    for path in sorted(plugins_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        marks: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "ExtraField":
                    marks |= {
                        kw.arg
                        for kw in node.keywords
                        if kw.arg
                        and kw.arg.startswith("ref_")
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                    }
            elif isinstance(node, ast.keyword) and node.arg == "json_schema_extra" and isinstance(node.value, ast.Dict):
                marks |= {
                    key.value
                    for key, value in zip(node.value.keys, node.value.values)
                    if isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value.startswith("ref_")
                    and isinstance(value, ast.Constant)
                    and value.value is True
                }
        if marks:
            found[path.relative_to(plugins_root).as_posix()] = sorted(marks)
    return found


def test_plugin_configs_declare_no_other_ownership_constrained_reference() -> None:
    """绊线：插件配置里出现别的「受归属约束的资源」引用时，这条立刻红

    插件配置是租户**写得到**的一层（`PUT /plugins/config/{plugin_key}/{instance_key}`），
    因此这里的每一个 `ref_*` 都需要一道对应的归属判定。今天只有 `ref_model_groups`，
    它已经接上了 `assert_tenant_may_select_model_groups`；哪天有人在插件里引用人设
    （`ref_presets`）或邮箱账户，要做的是给那类资源接一道同形的判定，而不是改掉这条用例。
    """
    found = _scan_plugin_ref_markers()
    # 自检：扫描器必须先找得到今天已知的四处声明，否则下面那条断言的"通过"没有任何意义
    assert set(found) >= {
        "builtin/draw/plugin.py",
        "builtin/email_utils.py",
        "builtin/emotion.py",
        "builtin/view_image.py",
    }, f"扫描器失效：只扫到 {sorted(found)}"
    offenders = {path: marks for path, marks in found.items() if marks != ["ref_model_groups"]}
    assert offenders == {}
