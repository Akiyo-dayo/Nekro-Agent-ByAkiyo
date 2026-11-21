"""世界书配置项：租户能开自己的，预算（百分比、token 上限）只能往下调。

`WORLDBOOK_ENABLED` 是「本频道是否注入世界书」的闸：关着时运行时不查世界书、提示词与
改造前逐字相同。它不是一次能力升级，因此**不标** `tenant_may_only_lower`——平台方与
租户管理员都能为自己的实例 / 频道打开或关闭。

`WORLDBOOK_BUDGET_PERCENT` / `WORLDBOOK_BUDGET_CAP_TOKENS` 是每轮 token 预算的两个旋钮，标了 `tenant_may_only_lower`：上限由平台方定，
租户只能往下调。整数项的「低」是数值小，判据因此不能照搬 bool 那条「不许新写一条打开的
覆盖」，它比的是**写入前后这一层的生效值**不得超过「写之前的生效值」与「上层值」两者中
较大的那个。

写路径逐条覆盖：`/set`、`/batch`、`PUT /config/instance/...`；不带取值的 `/save`（内存
写盘）与 `/reload`（磁盘读回内存）按搬运前后这一层的生效值比较。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from nekro_agent.core.config import CoreConfig
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.schemas.errors import PermissionDeniedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import config_resolver as config_resolver_module
from nekro_agent.services.config_service import UnifiedConfigService
from nekro_agent.services.user.role import Role

INSTANCE_KEY = "instance_override_onebot_v11__writer"
CHANNEL_KEY = "channel_config_onebot_v11-writer-group_1"

# 平台方定的每轮 token 上限。用例里逐条与它比较，比的是「能不能超过这个数」。
UPSTREAM_CAP_TOKENS = 3000


def tenant_admin() -> DBUser:
    """租户管理员：能写自己实例 / 频道的覆盖层"""
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


def platform_owner() -> DBUser:
    """平台方：不受「只能调低」约束"""
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


class ConfigHarness:
    """给真实路由装一份内存里的覆盖层配置，并把上层值固定成用例给的那份

    路由的价值在于它调了 `_assert_tenant_may_not_raise_only_field`；配置服务是不是真的落盘
    与这条判据无关，所以这里只保留读写语义，把磁盘换成内存对象。上层值走
    `config_resolver.inherited_values()`，与生产同一条入口。
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        layer: OverridableConfig,
        inherited: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.layer = layer
        self.inherited: Dict[str, Any] = inherited or {}
        self.saved: list[str] = []

        monkeypatch.setattr(
            UnifiedConfigService,
            "_get_config_instance",
            staticmethod(lambda config_key: layer if config_key in (INSTANCE_KEY, CHANNEL_KEY) else None),
        )
        monkeypatch.setattr(UnifiedConfigService, "save_config", staticmethod(self._save))

        async def fake_accessible(_user: DBUser, _config_key: str) -> None:
            return None

        async def fake_inherited(_config_key: str) -> Dict[str, Any]:
            return dict(self.inherited)

        monkeypatch.setattr(config_router, "_assert_config_key_accessible", fake_accessible)
        monkeypatch.setattr(config_resolver_module.config_resolver, "inherited_values", fake_inherited)

    def _save(self, config_key: str, _file_path: Any = None) -> tuple[bool, None]:
        self.saved.append(config_key)
        return True, None


def worldbook_inherited(*, cap_tokens: int = UPSTREAM_CAP_TOKENS, enabled: bool = False) -> Dict[str, Any]:
    """上层（平台方）此刻的取值"""
    return {"WORLDBOOK_BUDGET_CAP_TOKENS": cap_tokens, "WORLDBOOK_ENABLED": enabled}


@pytest.fixture
def layer() -> OverridableConfig:
    return OverridableConfig()


class TestTenantCanTurnOn:
    """闸门不是能力升级：租户对自己的实例 / 频道开与关都放行"""

    async def test_tenant_may_turn_it_on_for_its_own_instance(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, worldbook_inherited())

        await config_router.batch_update_config(
            INSTANCE_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_WORLDBOOK_ENABLED": "true", "WORLDBOOK_ENABLED": "true"},
            ),
            _current_user=tenant_admin(),
        )

        assert layer.enable_WORLDBOOK_ENABLED is True
        assert layer.WORLDBOOK_ENABLED is True

    async def test_tenant_may_turn_it_on_for_its_own_channel(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, worldbook_inherited())

        await config_router.batch_update_config(
            CHANNEL_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_WORLDBOOK_ENABLED": "true", "WORLDBOOK_ENABLED": "true"},
            ),
            _current_user=tenant_admin(),
        )

        assert layer.enable_WORLDBOOK_ENABLED is True
        assert layer.WORLDBOOK_ENABLED is True

    async def test_tenant_may_turn_it_off_again(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 平台方已经在这个实例上打开了闸，租户把它关掉同样是一次合法写入。
        ConfigHarness(monkeypatch, layer, worldbook_inherited(enabled=True))
        layer.enable_WORLDBOOK_ENABLED = True
        layer.WORLDBOOK_ENABLED = True

        await config_router.batch_update_config(
            INSTANCE_KEY,
            config_router.BatchUpdateConfig(configs={"WORLDBOOK_ENABLED": "false"}),
            _current_user=tenant_admin(),
        )

        assert layer.WORLDBOOK_ENABLED is False
        assert layer.enable_WORLDBOOK_ENABLED is True

    def test_the_gate_is_not_in_the_only_lower_list_but_the_budget_is(self) -> None:
        names = config_router._tenant_only_lower_field_names()  # noqa: SLF001

        assert "WORLDBOOK_ENABLED" not in names
        assert "WORLDBOOK_BUDGET_CAP_TOKENS" in names


class TestCapTokensOnlyLower:
    """整数项：写完以后的生效值不得超过「写之前」与「上层值」中较大的那个"""

    async def test_lowering_is_allowed(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, worldbook_inherited())

        await config_router.batch_update_config(
            CHANNEL_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "true", "WORLDBOOK_BUDGET_CAP_TOKENS": "2000"},
            ),
            _current_user=tenant_admin(),
        )

        assert layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS is True
        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == 2000

    async def test_raising_above_the_upstream_value_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, worldbook_inherited())

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                CHANNEL_KEY,
                config_router.BatchUpdateConfig(
                    configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "true", "WORLDBOOK_BUDGET_CAP_TOKENS": "3001"},
                ),
                _current_user=tenant_admin(),
            )

        assert layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS is False

    async def test_going_back_up_to_the_upstream_value_is_allowed(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 已有覆盖 1000：调回上层值（3000）不让任何人受损，放行。
        ConfigHarness(monkeypatch, layer, worldbook_inherited())
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 1000

        await config_router.batch_update_config(
            CHANNEL_KEY,
            config_router.BatchUpdateConfig(configs={"WORLDBOOK_BUDGET_CAP_TOKENS": "3000"}),
            _current_user=tenant_admin(),
        )

        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == UPSTREAM_CAP_TOKENS

    async def test_going_above_the_upstream_value_from_a_low_override_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, worldbook_inherited())
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 1000

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                CHANNEL_KEY,
                config_router.BatchUpdateConfig(configs={"WORLDBOOK_BUDGET_CAP_TOKENS": "3500"}),
                _current_user=tenant_admin(),
            )

        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == 1000

    async def test_turning_the_override_off_falls_back_to_the_upstream_value(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 覆盖 1000、上层 3000：关掉覆盖等于回到 3000，不是抬高。
        ConfigHarness(monkeypatch, layer, worldbook_inherited())
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 1000

        await config_router.batch_update_config(
            INSTANCE_KEY,
            config_router.BatchUpdateConfig(configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "false"}),
            _current_user=tenant_admin(),
        )

        assert layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS is False

    async def test_a_high_value_under_a_closed_override_is_not_a_raise(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 关掉覆盖的同时带一个高值：覆盖关着时这个值不生效，生效的是上层 3000，不算调高。
        # 之后想让它生效就得再打开覆盖，那一次会按 9000 判、被拒。
        ConfigHarness(monkeypatch, layer, worldbook_inherited())
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 1000

        await config_router.batch_update_config(
            INSTANCE_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "false", "WORLDBOOK_BUDGET_CAP_TOKENS": "9000"},
            ),
            _current_user=tenant_admin(),
        )

        assert layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS is False
        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                INSTANCE_KEY,
                config_router.BatchUpdateConfig(configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "true"}),
                _current_user=tenant_admin(),
            )

    async def test_a_stale_value_under_a_closed_override_is_not_the_starting_point(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 覆盖关着、本层残留一个 9000（不生效，生效的是上层 3000）。打开覆盖写 8000：
        # 写之前的生效值是 3000 而不是残留的 9000，所以 8000 是调高，要拒。
        ConfigHarness(monkeypatch, layer, worldbook_inherited())
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = False
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 9000

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                INSTANCE_KEY,
                config_router.BatchUpdateConfig(
                    configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "true", "WORLDBOOK_BUDGET_CAP_TOKENS": "8000"},
                ),
                _current_user=tenant_admin(),
            )

        assert layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS is False

    async def test_tenant_may_lower_a_platform_raised_value(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 平台方给这个实例单独调高到 6000；租户往下调到 5000 放行，调更高的 7000 拒绝。
        ConfigHarness(monkeypatch, layer, worldbook_inherited())
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 6000

        await config_router.batch_update_config(
            INSTANCE_KEY,
            config_router.BatchUpdateConfig(configs={"WORLDBOOK_BUDGET_CAP_TOKENS": "5000"}),
            _current_user=tenant_admin(),
        )
        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == 5000

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                INSTANCE_KEY,
                config_router.BatchUpdateConfig(configs={"WORLDBOOK_BUDGET_CAP_TOKENS": "7000"}),
                _current_user=tenant_admin(),
            )

        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == 5000

    async def test_an_unparsable_submission_keeps_the_current_value(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 判据本身不因为一个格式错误的提交而放行一次调高，也不该把它判成一次降低。
        ConfigHarness(monkeypatch, layer, worldbook_inherited())
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 1000

        await config_router._assert_tenant_may_not_raise_only_field(  # noqa: SLF001
            tenant_admin(),
            CHANNEL_KEY,
            {"WORLDBOOK_BUDGET_CAP_TOKENS": "abc"},
        )

        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == 1000

    async def test_platform_owner_may_raise_it(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, worldbook_inherited())

        await config_router.batch_update_config(
            INSTANCE_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "true", "WORLDBOOK_BUDGET_CAP_TOKENS": "9000"},
            ),
            _current_user=platform_owner(),
        )

        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == 9000


class TestSaveAndReload:
    """`/save`、`/reload` 不带取值，在内存与磁盘之间整层搬运；按搬运前后这一层的生效值判"""

    @staticmethod
    def write_disk_copy(path: Path, *, cap_tokens: int) -> None:
        disk = OverridableConfig()
        disk.set_instance_config_file_path(path)
        disk.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        disk.WORLDBOOK_BUDGET_CAP_TOKENS = cap_tokens
        disk.dump_config()

    async def test_save_that_would_raise_the_effective_value_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 内存 5000（覆盖开）、磁盘 1000（覆盖开）、上层 3000：落盘会让重启后的生效值变高。
        harness = ConfigHarness(monkeypatch, layer, worldbook_inherited())
        path = tmp_path / "channel.yaml"
        self.write_disk_copy(path, cap_tokens=1000)
        layer.set_instance_config_file_path(path)
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 5000

        with pytest.raises(PermissionDeniedError):
            await config_router.save_config_api(CHANNEL_KEY, _current_user=tenant_admin())

        assert harness.saved == []

    async def test_reload_that_would_raise_the_effective_value_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 反过来：内存 1000、磁盘 5000，读回磁盘那份就是抬高生效值。
        ConfigHarness(monkeypatch, layer, worldbook_inherited())
        reloaded: list[str] = []
        monkeypatch.setattr(
            UnifiedConfigService,
            "reload_config",
            staticmethod(lambda config_key, _file_path=None: (reloaded.append(config_key), (True, None))[1]),
        )
        path = tmp_path / "channel.yaml"
        self.write_disk_copy(path, cap_tokens=5000)
        layer.set_instance_config_file_path(path)
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 1000

        with pytest.raises(PermissionDeniedError):
            await config_router.reload_config_api(CHANNEL_KEY, _current_user=tenant_admin())

        assert reloaded == []

    async def test_both_sides_below_the_upstream_value_are_allowed(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        harness = ConfigHarness(monkeypatch, layer, worldbook_inherited())
        path = tmp_path / "channel.yaml"
        self.write_disk_copy(path, cap_tokens=1000)
        layer.set_instance_config_file_path(path)
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 2000

        await config_router.save_config_api(CHANNEL_KEY, _current_user=tenant_admin())

        assert harness.saved == [CHANNEL_KEY]
        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == 2000


class TestZeroMeansUnlimited:
    """token 上限里 0 是「不设上限」：判据把 0 当作无穷大，否则租户写 0 就绕过了平台方的上限"""

    async def test_tenant_cannot_write_zero_under_a_platform_cap(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, worldbook_inherited(cap_tokens=4800))

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                CHANNEL_KEY,
                config_router.BatchUpdateConfig(
                    configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "true", "WORLDBOOK_BUDGET_CAP_TOKENS": "0"},
                ),
                _current_user=tenant_admin(),
            )

        assert layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS is False

    async def test_tenant_may_set_a_cap_when_the_platform_has_none(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, worldbook_inherited(cap_tokens=0))

        await config_router.batch_update_config(
            CHANNEL_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "true", "WORLDBOOK_BUDGET_CAP_TOKENS": "4800"},
            ),
            _current_user=tenant_admin(),
        )

        assert layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS is True
        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == 4800

    async def test_tenant_may_lower_a_platform_cap(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, worldbook_inherited(cap_tokens=4800))

        await config_router.batch_update_config(
            CHANNEL_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "true", "WORLDBOOK_BUDGET_CAP_TOKENS": "3000"},
            ),
            _current_user=tenant_admin(),
        )

        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == 3000

    async def test_tenant_may_drop_its_own_cap_when_the_platform_has_none(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """上层不设上限、这一层写过 4800：改回 0 等于把这一层的上限去掉，按「回到上层值」放行"""
        ConfigHarness(monkeypatch, layer, worldbook_inherited(cap_tokens=0))
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 4800

        await config_router.batch_update_config(
            CHANNEL_KEY,
            config_router.BatchUpdateConfig(configs={"WORLDBOOK_BUDGET_CAP_TOKENS": "0"}),
            _current_user=tenant_admin(),
        )

        assert layer.WORLDBOOK_BUDGET_CAP_TOKENS == 0

    async def test_reload_of_an_uncapped_disk_copy_is_a_raise(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """内存 3000、磁盘 0（不设上限）、上层 4800：读回磁盘那份就是放开上限"""
        ConfigHarness(monkeypatch, layer, worldbook_inherited(cap_tokens=4800))
        reloaded: list[str] = []
        monkeypatch.setattr(
            UnifiedConfigService,
            "reload_config",
            staticmethod(lambda config_key, _file_path=None: (reloaded.append(config_key), (True, None))[1]),
        )
        path = tmp_path / "channel.yaml"
        TestSaveAndReload.write_disk_copy(path, cap_tokens=0)
        layer.set_instance_config_file_path(path)
        layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS = True
        layer.WORLDBOOK_BUDGET_CAP_TOKENS = 3000

        with pytest.raises(PermissionDeniedError):
            await config_router.reload_config_api(CHANNEL_KEY, _current_user=tenant_admin())

        assert reloaded == []

    @pytest.mark.parametrize("value", ["0", "2", "65536"])
    async def test_a_migrated_one_token_cap_cannot_be_raised(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        """旧字数上限 0（不注入）迁移成上一层的上限 1：租户在下层写 0（不另设上限）或更大的数都被拒"""
        ConfigHarness(monkeypatch, layer, worldbook_inherited(cap_tokens=1))

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                CHANNEL_KEY,
                config_router.BatchUpdateConfig(
                    configs={"enable_WORLDBOOK_BUDGET_CAP_TOKENS": "true", "WORLDBOOK_BUDGET_CAP_TOKENS": value},
                ),
                _current_user=tenant_admin(),
            )

        assert layer.enable_WORLDBOOK_BUDGET_CAP_TOKENS is False

    async def test_percent_is_only_lower_too(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, {**worldbook_inherited(), "WORLDBOOK_BUDGET_PERCENT": 25})

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                CHANNEL_KEY,
                config_router.BatchUpdateConfig(
                    configs={"enable_WORLDBOOK_BUDGET_PERCENT": "true", "WORLDBOOK_BUDGET_PERCENT": "60"},
                ),
                _current_user=tenant_admin(),
            )
        await config_router.batch_update_config(
            CHANNEL_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_WORLDBOOK_BUDGET_PERCENT": "true", "WORLDBOOK_BUDGET_PERCENT": "10"},
            ),
            _current_user=tenant_admin(),
        )
        assert layer.WORLDBOOK_BUDGET_PERCENT == 10

    async def test_lookup_caps_are_only_lower_too(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """查询的 token 与每轮次数都只能往下调：租户写更大的值被拒，写更小的值放行"""
        ConfigHarness(
            monkeypatch,
            layer,
            {
                **worldbook_inherited(),
                "WORLDBOOK_LOOKUP_MAX_TOKENS": 3000,
                "WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND": 4,
            },
        )

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                CHANNEL_KEY,
                config_router.BatchUpdateConfig(
                    configs={
                        "enable_WORLDBOOK_LOOKUP_MAX_TOKENS": "true",
                        "WORLDBOOK_LOOKUP_MAX_TOKENS": "8000",
                    },
                ),
                _current_user=tenant_admin(),
            )
        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                CHANNEL_KEY,
                config_router.BatchUpdateConfig(
                    configs={
                        "enable_WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND": "true",
                        "WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND": "12",
                    },
                ),
                _current_user=tenant_admin(),
            )

        await config_router.batch_update_config(
            CHANNEL_KEY,
            config_router.BatchUpdateConfig(
                configs={
                    "enable_WORLDBOOK_LOOKUP_MAX_TOKENS": "true",
                    "WORLDBOOK_LOOKUP_MAX_TOKENS": "600",
                    "enable_WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND": "true",
                    "WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND": "1",
                },
            ),
            _current_user=tenant_admin(),
        )
        assert layer.WORLDBOOK_LOOKUP_MAX_TOKENS == 600
        assert layer.WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND == 1

    def test_the_marker_is_paired_with_only_lower(self) -> None:
        """导入期校验：标了「0 表示不设上限」的整数项必须同时标「租户只能调低」"""
        extra = CoreConfig.model_fields["WORLDBOOK_BUDGET_CAP_TOKENS"].json_schema_extra
        assert isinstance(extra, dict)
        assert extra.get("zero_means_unlimited") is True and extra.get("tenant_may_only_lower") is True
        assert config_router._int_rank("WORLDBOOK_BUDGET_CAP_TOKENS", 0) == float("inf")  # noqa: SLF001
        assert config_router._int_rank("WORLDBOOK_BUDGET_PERCENT", 0) == 0  # noqa: SLF001


class TestDeclaredDefaults:
    """世界书各项的默认值与约束：改默认值要同时改这里，免得文档与代码两边漂移"""

    def test_defaults_and_bounds(self) -> None:
        fields = CoreConfig.model_fields

        assert fields["WORLDBOOK_ENABLED"].default is False
        assert fields["WORLDBOOK_BUDGET_PERCENT"].default == 25
        assert fields["WORLDBOOK_BUDGET_CAP_TOKENS"].default == 0
        assert fields["WORLDBOOK_MAX_ENTRIES_PER_ROUND"].default == 32
        assert fields["WORLDBOOK_AUTO_QUARANTINE_BLOCKED_ROUNDS"].default == 3
        assert fields["WORLDBOOK_LOOKUP_MAX_TOKENS"].default == 3000
        assert fields["WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND"].default == 4
        # 已废弃的旧项：保留字段只为让旧配置文件照常加载，界面上隐藏
        legacy = fields["WORLDBOOK_MAX_CHARS"].json_schema_extra
        assert isinstance(legacy, dict) and legacy.get("is_hidden") is True

    def test_only_the_intended_items_are_overridable(self) -> None:
        fields = CoreConfig.model_fields

        def overridable(name: str) -> Any:
            extra = fields[name].json_schema_extra
            return isinstance(extra, dict) and extra.get("overridable")

        assert overridable("WORLDBOOK_ENABLED") is True
        assert overridable("WORLDBOOK_BUDGET_PERCENT") is True
        assert overridable("WORLDBOOK_BUDGET_CAP_TOKENS") is True
        assert overridable("WORLDBOOK_LOOKUP_MAX_TOKENS") is True
        assert overridable("WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND") is True
        assert overridable("WORLDBOOK_MAX_ENTRIES_PER_ROUND") is False
        assert overridable("WORLDBOOK_AUTO_QUARANTINE_BLOCKED_ROUNDS") is False
        assert overridable("WORLDBOOK_BUDGET_MIGRATED") is False

    def test_every_item_carries_both_languages(self) -> None:
        """配置页按 i18n 文案显示，缺一种语言就会在另一种语言下露出中文标题"""
        titles = {
            "WORLDBOOK_ENABLED": ("启用世界书", "Enable Worldbook"),
            "WORLDBOOK_BUDGET_PERCENT": ("百分比", "Context %"),
            "WORLDBOOK_BUDGET_CAP_TOKENS": ("token 上限", "Token Cap"),
            "WORLDBOOK_MAX_ENTRIES_PER_ROUND": ("条数上限", "Max Entries"),
            "WORLDBOOK_AUTO_QUARANTINE_BLOCKED_ROUNDS": ("自动隔离", "Auto Quarantine"),
            "WORLDBOOK_LOOKUP_MAX_TOKENS": ("token 上限", "Tokens per Call"),
            "WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND": ("次数上限", "Calls per Round"),
        }
        for name, (zh_hint, en_hint) in titles.items():
            extra = CoreConfig.model_fields[name].json_schema_extra
            assert isinstance(extra, dict)
            assert extra["i18n_category"] == {"zh-CN": "世界书", "en-US": "Worldbook"}
            i18n_title = extra["i18n_title"]
            i18n_description = extra["i18n_description"]
            assert zh_hint in i18n_title["zh-CN"], name
            assert en_hint in i18n_title["en-US"], name
            assert i18n_description["zh-CN"], name
            assert i18n_description["en-US"], name
