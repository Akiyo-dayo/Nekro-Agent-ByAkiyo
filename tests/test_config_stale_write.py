"""配置页保存时的过期写回防护（`services/config_write_guard.py`）

配置页打开之后，后台别处改了某一项（删实例、划转时从值守名单或共享发信名单里摘掉那个实例，或者另一个
管理员先存了一次），旧页面上再点保存，就会把别处的改动盖回去：名单里已经摘掉的实例又被写回来。

页面把每个改动项读取时的值（`base`）一起带上，服务端按调用方读取时的同一个视图取现值比对，任何一项对不上
就整批拒绝（409，`data.keys` 列出是哪几项），一项都不写。不带 `base` 的调用方照旧。

写盘一律拦住，只记下写了什么，不碰开发机上的配置文件。
"""

from __future__ import annotations

import datetime
import json
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest
from pydantic import BaseModel
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.routers import plugins as plugins_router
from nekro_agent.schemas.errors import ConfigStaleWriteError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.config_write_guard import canonical_config_value, find_stale_keys, view_values
from nekro_agent.services.user.role import Role

KEEP = "onebot_v11:keep"
GONE = "onebot_v11:gone"
NEW = "onebot_v11:new"


@pytest.fixture
async def db() -> AsyncIterator[None]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
    calls: List[tuple] = []

    def record_batch(config_key: str, configs: dict) -> tuple:
        calls.append(("batch", config_key, dict(configs)))
        return True, ""

    monkeypatch.setattr(config_router.UnifiedConfigService, "batch_update_config", record_batch)
    monkeypatch.setattr(config_router.UnifiedConfigService, "save_config", lambda config_key: (True, ""))
    return calls


def platform_super() -> DBUser:
    user = DBUser(
        username="owner",
        password="",
        adapter_key="",
        platform_userid="",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )
    user.id = 1
    return user


# ---------------------------------------------------------------------------
# 比对本身
# ---------------------------------------------------------------------------


class TestCanonicalValues:
    def test_key_order_and_whole_number_floats_do_not_count_as_changes(self) -> None:
        assert canonical_config_value({"b": 1, "a": [1.0, {"y": 2, "x": 3}]}) == canonical_config_value(
            {"a": [1, {"x": 3, "y": 2.0}], "b": 1.0},
        )

    def test_real_differences_do(self) -> None:
        assert canonical_config_value([KEEP, GONE]) != canonical_config_value([KEEP])
        assert canonical_config_value([KEEP, GONE]) != canonical_config_value([GONE, KEEP]), "名单的顺序也算"
        assert canonical_config_value(1.5) != canonical_config_value(1)
        assert canonical_config_value(True) != canonical_config_value(1)
        assert canonical_config_value(None) != canonical_config_value("")

    def test_models_compare_by_their_json_form(self) -> None:
        class Item(BaseModel):
            name: str
            weight: float

        assert canonical_config_value(Item(name="x", weight=2.0)) == canonical_config_value({"weight": 2, "name": "x"})

    def test_values_compare_in_the_form_the_page_read_them(self) -> None:
        """服务端是原始值，页面提交回来的是读接口 JSON 经浏览器解析再序列化的结果，两边要能对上"""

        class Level(Enum):
            LOW = 1

        class Mode(str, Enum):
            FAST = "fast"

        when = datetime.datetime(2026, 3, 4, 5, 6, 7)
        assert canonical_config_value(Level.LOW) == canonical_config_value(1)
        assert canonical_config_value(Mode.FAST) == canonical_config_value("fast")
        assert canonical_config_value(when) == canonical_config_value("2026-03-04T05:06:07")
        assert canonical_config_value({"at": [when]}) == canonical_config_value({"at": ["2026-03-04T05:06:07"]})
        # 2**60 + 1 在浏览器里读成 2**60，再序列化回来写成 1152921504606847000
        assert canonical_config_value(2**60 + 1) == canonical_config_value(1152921504606847000)
        assert canonical_config_value(-(2**60) - 1) == canonical_config_value(-1152921504606847000)
        assert canonical_config_value(2**53 - 1) != canonical_config_value(2**53 - 2), "安全范围内照常逐位比"
        assert canonical_config_value(2**60) != canonical_config_value(2**61), "超出范围的大数真变了也算"


class TestFindStaleKeys:
    def test_without_base_nothing_is_compared(self) -> None:
        assert find_stale_keys(["A"], None, {"A": 2}) == []
        assert find_stale_keys(["A"], {}, {"A": 2}) == []

    def test_only_submitted_keys_that_have_a_base_are_compared(self) -> None:
        base = {"A": 1, "B": 1}
        current = {"A": 1, "B": 2, "C": 3}
        assert find_stale_keys(["A", "C"], base, current) == [], "B 变了但这次没提交；C 没带读取时的值"
        assert find_stale_keys(["A", "B"], base, current) == ["B"]

    def test_a_key_that_no_longer_exists_is_stale(self) -> None:
        assert find_stale_keys(["A"], {"A": 1}, {}) == ["A"]


def test_the_error_is_a_409_that_lists_the_keys() -> None:
    error = ConfigStaleWriteError(keys=["SSH_GATEWAY_DUTY_INSTANCES", "COMMAND_ENABLED"])
    assert error.http_status == 409
    assert error.data == {"keys": ["SSH_GATEWAY_DUTY_INSTANCES", "COMMAND_ENABLED"]}
    assert "SSH_GATEWAY_DUTY_INSTANCES" in str(error)


# ---------------------------------------------------------------------------
# 系统配置页（`/config/batch/system`）
# ---------------------------------------------------------------------------


class TestSystemPage:
    @pytest.fixture(autouse=True)
    def duty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 页面打开之后，删实例把 GONE 从名单里摘掉了：现值只剩 KEEP
        monkeypatch.setattr(config, "SSH_GATEWAY_DUTY_INSTANCES", [KEEP])

    async def test_a_name_removed_after_the_page_was_opened_is_not_written_back(
        self,
        db: None,  # noqa: ARG002
        writes: List[tuple],
    ) -> None:
        """旧页面上往名单里加一个实例：提交的名单里还留着已经摘掉的那个，整批拒绝"""
        with pytest.raises(ConfigStaleWriteError) as excinfo:
            await config_router.batch_update_config(
                config_key="system",
                body=config_router.BatchUpdateConfig(
                    configs={"SSH_GATEWAY_DUTY_INSTANCES": json.dumps([KEEP, GONE, NEW])},
                    base={"SSH_GATEWAY_DUTY_INSTANCES": [KEEP, GONE]},
                ),
                _current_user=platform_super(),
            )
        assert excinfo.value.data == {"keys": ["SSH_GATEWAY_DUTY_INSTANCES"]}
        assert writes == []
        assert config.SSH_GATEWAY_DUTY_INSTANCES == [KEEP]

    async def test_the_whole_batch_is_refused(self, db: None, writes: List[tuple]) -> None:  # noqa: ARG002
        """同一批里没过期的项也不写：页面刷新后会把它们带回来再存一次"""
        command_enabled = config.COMMAND_ENABLED
        with pytest.raises(ConfigStaleWriteError) as excinfo:
            await config_router.batch_update_config(
                config_key="system",
                body=config_router.BatchUpdateConfig(
                    configs={
                        "SSH_GATEWAY_DUTY_INSTANCES": json.dumps([KEEP, GONE]),
                        "COMMAND_ENABLED": json.dumps(not command_enabled),
                    },
                    base={"SSH_GATEWAY_DUTY_INSTANCES": [KEEP, GONE], "COMMAND_ENABLED": command_enabled},
                ),
                _current_user=platform_super(),
            )
        assert excinfo.value.data == {"keys": ["SSH_GATEWAY_DUTY_INSTANCES"]}
        assert writes == []

    async def test_saving_on_the_current_values_goes_through(self, db: None, writes: List[tuple]) -> None:  # noqa: ARG002
        await config_router.batch_update_config(
            config_key="system",
            body=config_router.BatchUpdateConfig(
                configs={"SSH_GATEWAY_DUTY_INSTANCES": json.dumps([KEEP, NEW])},
                base={"SSH_GATEWAY_DUTY_INSTANCES": [KEEP]},
            ),
            _current_user=platform_super(),
        )
        assert writes == [("batch", "system", {"SSH_GATEWAY_DUTY_INSTANCES": json.dumps([KEEP, NEW])})]

    async def test_callers_without_base_are_unchanged(self, db: None, writes: List[tuple]) -> None:  # noqa: ARG002
        """切换语言、首次引导这类只提交自己那一项的调用方不带读取时的值，照旧写入"""
        await config_router.batch_update_config(
            config_key="system",
            body=config_router.BatchUpdateConfig(configs={"SSH_GATEWAY_DUTY_INSTANCES": json.dumps([KEEP, GONE])}),
            _current_user=platform_super(),
        )
        assert len(writes) == 1

    async def test_a_changed_key_that_is_not_submitted_does_not_block(self, db: None, writes: List[tuple]) -> None:  # noqa: ARG002
        command_enabled = config.COMMAND_ENABLED
        await config_router.batch_update_config(
            config_key="system",
            body=config_router.BatchUpdateConfig(
                configs={"COMMAND_ENABLED": json.dumps(not command_enabled)},
                base={"COMMAND_ENABLED": command_enabled, "SSH_GATEWAY_DUTY_INSTANCES": [KEEP, GONE]},
            ),
            _current_user=platform_super(),
        )
        assert len(writes) == 1

    async def test_every_value_read_from_the_list_compares_equal_after_passing_through_json(self, db: None) -> None:  # noqa: ARG002
        """页面拿到的是读接口的 JSON：整张系统配置表原样当作「读取时的值」带回来，一项都不该算过期"""
        user = platform_super()
        items = await config_router.get_config_list(config_key="system", _current_user=user)
        base = {item.key: json.loads(json.dumps(item.model_dump(mode="json")["value"])) for item in items}
        current = view_values(await config_router._config_list_view(user, "system"))  # noqa: SLF001
        assert find_stale_keys(list(base), base, current) == []


# ---------------------------------------------------------------------------
# 实例覆盖页（`PUT /config/instance/...`）与插件的实例级配置
# ---------------------------------------------------------------------------


class TestInstanceOverridePage:
    @pytest.fixture(autouse=True)
    def scoped(self, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
        state: Dict[str, Any] = {
            "view": [{"key": "SSH_GATEWAY_DUTY_INSTANCES", "value": [KEEP], "title": "值守实例", "type": "list"}],
            "view_users": [],
        }

        async def exists(_config_key: str) -> None:
            return None

        async def no_audit(_config_key: str) -> Optional[Any]:
            return None

        async def view(user: DBUser, config_key: str) -> List[Dict[str, Any]]:
            state["view_users"].append((user.username, config_key))
            return list(state["view"])

        monkeypatch.setattr(config_router, "_assert_override_target_exists", exists)
        monkeypatch.setattr(config_router, "_audit_target_for", no_audit)
        monkeypatch.setattr(config_router, "_instance_config_view", view)
        return state

    async def test_a_stale_override_is_refused(self, writes: List[tuple], scoped: Dict[str, Any]) -> None:
        with pytest.raises(ConfigStaleWriteError):
            await config_router.update_instance_config(
                adapter_key="onebot_v11",
                instance_key="main",
                body=config_router.BatchUpdateConfig(
                    configs={"SSH_GATEWAY_DUTY_INSTANCES": json.dumps([KEEP, GONE])},
                    base={"SSH_GATEWAY_DUTY_INSTANCES": [KEEP, GONE]},
                ),
                _current_user=platform_super(),
            )
        assert writes == []
        assert scoped["view_users"], "比对用的是读接口给这个身份的那份视图"

    async def test_a_fresh_override_is_written(self, writes: List[tuple]) -> None:
        await config_router.update_instance_config(
            adapter_key="onebot_v11",
            instance_key="main",
            body=config_router.BatchUpdateConfig(
                configs={"SSH_GATEWAY_DUTY_INSTANCES": json.dumps([KEEP, NEW])},
                base={"SSH_GATEWAY_DUTY_INSTANCES": [KEEP]},
            ),
            _current_user=platform_super(),
        )
        assert len(writes) == 1

    async def test_the_read_endpoint_serves_the_same_view(self, scoped: Dict[str, Any]) -> None:
        items = await config_router.get_instance_config(
            adapter_key="onebot_v11", instance_key="main", _current_user=platform_super()
        )
        assert [(item.key, item.value) for item in items] == [("SSH_GATEWAY_DUTY_INSTANCES", [KEEP])]
        assert scoped["view_users"] == [("owner", "instance_override_onebot_v11__main")]


class TestPluginInstanceConfig:
    @pytest.fixture(autouse=True)
    def scoped(self, monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
        saved: List[tuple] = []

        async def accessible(_user: DBUser, _instance_key: str, _chat_key: str = "") -> None:
            return None

        async def view(_user: DBUser, _plugin_key: str, _instance_key: str) -> List[Dict[str, Any]]:
            return [{"key": "SEND_TO", "value": [KEEP]}]

        async def save(plugin_key: str, instance_key: str, configs: Dict[str, str]) -> tuple:
            saved.append((plugin_key, instance_key, dict(configs)))
            return True, ""

        monkeypatch.setattr(plugins_router, "assert_plugin_scope_accessible", accessible)
        monkeypatch.setattr(plugins_router, "_plugin_instance_config_view", view)
        monkeypatch.setattr(plugins_router, "save_plugin_instance_config", save)
        return saved

    async def test_a_stale_value_is_refused(self, scoped: List[tuple]) -> None:
        with pytest.raises(ConfigStaleWriteError):
            await plugins_router.save_plugin_instance_config_handler(
                plugin_key="demo.plugin",
                instance_key="main",
                body=plugins_router.SavePluginConfigRequest(
                    configs={"SEND_TO": json.dumps([KEEP, GONE])}, base={"SEND_TO": [KEEP, GONE]}
                ),
                _current_user=platform_super(),
            )
        assert scoped == []

    async def test_a_fresh_value_is_saved(self, scoped: List[tuple]) -> None:
        await plugins_router.save_plugin_instance_config_handler(
            plugin_key="demo.plugin",
            instance_key="main",
            body=plugins_router.SavePluginConfigRequest(
                configs={"SEND_TO": json.dumps([KEEP, NEW])}, base={"SEND_TO": [KEEP]}
            ),
            _current_user=platform_super(),
        )
        assert scoped == [("demo.plugin", "main", {"SEND_TO": json.dumps([KEEP, NEW])})]
