"""输出过滤的后台权限、配置分层与两个只读接口

权限沿用现成规则：租户只能碰自己实例（越权 404），平台方不指定实例时看全部。
配置用真实的 `config_resolver`，实例层用现成的 `OverridableConfig` + `ConfigManager` 搭出来。
"""

import datetime
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.core_utils import ConfigManager
from nekro_agent.core.os_env import OsEnv
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.config import (
    _assert_config_key_accessible,
    _assert_tenant_may_write,
    get_output_filter_hits,
    get_output_filter_starter_words,
)
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.config_service import (
    instance_override_config_key,
    instance_override_root_dir,
)
from nekro_agent.services.output_filter import stats
from nekro_agent.services.output_filter.service import STARTER_WORDS_PATH, settings_for_chat
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
OTHER_TENANT_ID = 2

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
OWN_SECOND_INSTANCE = "inst2"
FOREIGN_INSTANCE = "rival1"

INSTANCE_LAYOUT: List[tuple[str, Optional[int]]] = [
    (OWN_INSTANCE, ACME_TENANT_ID),
    (OWN_SECOND_INSTANCE, ACME_TENANT_ID),
    (FOREIGN_INSTANCE, OTHER_TENANT_ID),
]

CHAT_KEYS = {
    OWN_INSTANCE: f"{ADAPTER}-{OWN_INSTANCE}-group_12345",
    OWN_SECOND_INSTANCE: f"{ADAPTER}-{OWN_SECOND_INSTANCE}-group_12345",
    FOREIGN_INSTANCE: f"{ADAPTER}-{FOREIGN_INSTANCE}-group_12345",
}


def make_user(*, user_id: int, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
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
    return make_user(user_id=2, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(user_id=1, tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, tenant_id in INSTANCE_LAYOUT:
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
                channel_id="group_12345",
                channel_name="test",
                channel_type="group",
                chat_key=CHAT_KEYS[instance_key],
                data="{}",
            )
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """数据目录与覆盖配置完全隔离，用例之间不共享缓存"""
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path))
    for name in (
        "OUTPUT_FILTER_ENABLE",
        "OUTPUT_FILTER_MODE",
        "OUTPUT_FILTER_REPLACEMENT",
        "OUTPUT_FILTER_WORDS",
        "OUTPUT_FILTER_REGEX",
        "OUTPUT_FILTER_BEFORE_MEMORY",
    ):
        default = type(config).model_fields[name].get_default(call_default_factory=True)
        monkeypatch.setattr(config, name, default)

    instance_override_root_dir(ADAPTER).mkdir(parents=True, exist_ok=True)
    registered: List[str] = []

    def install(instance_key: str, **values: Any) -> OverridableConfig:
        overrides = OverridableConfig()
        for name, value in values.items():
            setattr(overrides, f"enable_{name}", True)
            setattr(overrides, name, value)
        config_key = instance_override_config_key(ADAPTER, instance_key)
        ConfigManager.register_config(config_key, overrides)
        registered.append(config_key)
        config_resolver.bump_version()
        return overrides

    yield install

    for config_key in registered:
        ConfigManager.unregister_config(config_key)
    config_resolver.bump_version()


class TestConfigLayering:
    async def test_instance_override_enables_filter_with_its_own_words(self, db, isolated_config) -> None:
        config.OUTPUT_FILTER_ENABLE = False
        isolated_config(OWN_INSTANCE, OUTPUT_FILTER_ENABLE=True, OUTPUT_FILTER_WORDS="instword")

        settings = await settings_for_chat(CHAT_KEYS[OWN_INSTANCE])
        other = await settings_for_chat(CHAT_KEYS[OWN_SECOND_INSTANCE])

        assert settings is not None
        assert [word for word, _rule in settings.rules.words] == ["instword"]
        assert other is None

    async def test_instance_override_can_turn_filter_off(self, db, isolated_config) -> None:
        config.OUTPUT_FILTER_ENABLE = True
        config.OUTPUT_FILTER_WORDS = "globalword"
        isolated_config(OWN_INSTANCE, OUTPUT_FILTER_ENABLE=False, OUTPUT_FILTER_WORDS="instword")

        settings = await settings_for_chat(CHAT_KEYS[OWN_INSTANCE])
        other = await settings_for_chat(CHAT_KEYS[OWN_SECOND_INSTANCE])

        assert settings is None
        assert other is not None
        assert [word for word, _rule in other.rules.words] == ["globalword"]


class TestWriteScope:
    async def test_tenant_cannot_write_foreign_instance(self, db) -> None:
        with pytest.raises(NotFoundError):
            await _assert_config_key_accessible(
                acme_admin(),
                instance_override_config_key(ADAPTER, FOREIGN_INSTANCE),
                for_write=True,
            )

    async def test_tenant_can_write_own_instance(self, db) -> None:
        await _assert_config_key_accessible(
            acme_admin(),
            instance_override_config_key(ADAPTER, OWN_INSTANCE),
            for_write=True,
        )

    async def test_filter_fields_are_not_blocked_as_credentials_or_platform_boundary(self, db, isolated_config) -> None:
        isolated_config(OWN_INSTANCE)

        await _assert_tenant_may_write(
            acme_admin(),
            instance_override_config_key(ADAPTER, OWN_INSTANCE),
            {
                "OUTPUT_FILTER_ENABLE": "true",
                "OUTPUT_FILTER_MODE": "fragment",
                "OUTPUT_FILTER_REPLACEMENT": "[X]",
                "OUTPUT_FILTER_WORDS": "bad",
                "OUTPUT_FILTER_REGEX": "ba+d",
                "OUTPUT_FILTER_BEFORE_MEMORY": "true",
            },
        )


class TestHitsEndpoint:
    async def _seed_hits(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(stats, "HITS_PATH", tmp_path / "output_filter" / "hits.json")
        await stats.hit_store.record("onebot_v11", OWN_INSTANCE, Counter({"word:bad": 2}))
        await stats.hit_store.record("onebot_v11", FOREIGN_INSTANCE, Counter({"regex:ba+d": 1}))

    async def test_tenant_must_pass_instance_key(self, db) -> None:
        with pytest.raises(ValidationError):
            await get_output_filter_hits(_current_user=acme_admin(), instance_key=None)

    async def test_tenant_cannot_read_foreign_instance(self, db) -> None:
        with pytest.raises(NotFoundError):
            await get_output_filter_hits(_current_user=acme_admin(), adapter_key=ADAPTER, instance_key=FOREIGN_INSTANCE)

    async def test_tenant_only_sees_own_instance(self, db, isolated_config, monkeypatch, tmp_path) -> None:
        await self._seed_hits(monkeypatch, tmp_path)
        isolated_config(OWN_INSTANCE, OUTPUT_FILTER_REGEX="(a+)+")

        response = await get_output_filter_hits(
            _current_user=acme_admin(),
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
        )

        assert [item["instance_key"] for item in response["instances"]] == [OWN_INSTANCE]
        rules = response["instances"][0]["rules"]
        assert rules[0]["rule"] == "bad"
        assert rules[0]["kind"] == "word"
        assert rules[0]["count"] == 2
        assert any(item["rule"] == "(a+)+" and item["reason"] for item in response["rejected_rules"])

    async def test_platform_without_instance_returns_all(self, db, monkeypatch, tmp_path) -> None:
        await self._seed_hits(monkeypatch, tmp_path)
        # 没有指定实例时被拒规则取自全局配置，这里放一条故意写坏的正则
        config.OUTPUT_FILTER_REGEX = "(a+)+"

        response = await get_output_filter_hits(_current_user=platform_owner(), adapter_key=None, instance_key=None)

        instance_keys = sorted(item["instance_key"] for item in response["instances"])
        assert instance_keys == [OWN_INSTANCE, FOREIGN_INSTANCE]
        assert any(item["rule"] == "(a+)+" for item in response["rejected_rules"])

    async def test_platform_with_instance_filters_to_it(self, db, isolated_config, monkeypatch, tmp_path) -> None:
        await self._seed_hits(monkeypatch, tmp_path)

        response = await get_output_filter_hits(
            _current_user=platform_owner(),
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
        )

        assert [item["instance_key"] for item in response["instances"]] == [OWN_INSTANCE]


class TestStarterWordsEndpoint:
    async def test_returns_file_text(self, db) -> None:
        response = await get_output_filter_starter_words(_current_user=platform_owner())

        assert response["text"] == STARTER_WORDS_PATH.read_text(encoding="utf-8")
