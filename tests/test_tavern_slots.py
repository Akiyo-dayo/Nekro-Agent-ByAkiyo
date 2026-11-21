"""高级提示词槽位：逐层 `{{original}}`、按层取值与三个槽位的解析

`chain_original` 只做一件事：从低到高，把每层的 `{{original}}` 换成下面那层的结果；
没写占位就整段替换。`layered_values` 必须与 `get_effective_config` 用同一条优先级，
否则预览里的链与真正发出去的值会对不上。

`resolve_slots` 的人设层是最上一层；开关关着时文字照算，由调用方决定发不发。
"""

from __future__ import annotations

import pytest

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.services import config_resolver as config_resolver_module
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.config_service import UnifiedConfigService
from nekro_agent.services.tavern.slots import chain_original, resolve_slots

ADAPTER = "onebot_v11"
INSTANCE = "i1"
CHAT_KEY = "onebot_v11-group_1"
ADAPTER_KEY = f"adapter_override_{ADAPTER}"
INSTANCE_KEY = f"instance_override_{ADAPTER}__{INSTANCE}"
CHANNEL_KEY = f"channel_config_{CHAT_KEY}"


def _override(**values: object) -> OverridableConfig:
    layer = OverridableConfig()
    for name, value in values.items():
        setattr(layer, name, value)
    return layer


@pytest.fixture
def layers(monkeypatch: pytest.MonkeyPatch):
    """三层覆盖的内存替身；`_has_instance_layer` 直接当「有实例层」处理"""
    registry: dict[str, OverridableConfig] = {}

    def _get(config_key: str):
        return registry.get(config_key)

    monkeypatch.setattr(UnifiedConfigService, "_get_config_instance", staticmethod(_get))
    monkeypatch.setattr(config_resolver, "_has_instance_layer", lambda _adapter_key: True)
    return registry


def _channel() -> DBChatChannel:
    return DBChatChannel(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="1",
        channel_type="group",
        chat_key=CHAT_KEY,
        data="{}",
    )


def _global_value(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    monkeypatch.setattr(config, name, value)
    # 解析器模块里另有一份 `system_config` 引用，指向同一个对象；这里显式再设一次是为了
    # 让「谁读全局值」这件事在用例里没有歧义（两个名字现在指向同一个实例）。
    monkeypatch.setattr(config_resolver_module.system_config, name, value)


class TestChainOriginal:
    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            ([], ""),
            (["只有全局"], "只有全局"),
            (["全局", "频道整段替换"], "频道整段替换"),
            (["全局", "频道 {{original}}"], "频道 全局"),
            (["全局", "{{ original }}"], "全局"),
            (["全局", "{{ORIGINAL}}"], "全局"),
            (["全局", "前 {{ original }} 中 {{ original }} 后"], "前 全局 中 全局 后"),
            (["全局", ""], ""),
        ],
    )
    def test_chain_combinations(self, values: list[str], expected: str) -> None:
        assert chain_original(values) == expected


class TestLayeredValues:
    async def test_order_matches_effective_value(self, layers: dict[str, OverridableConfig], monkeypatch) -> None:
        _global_value(monkeypatch, "PROMPT_PREFILL", "全局")
        layers[ADAPTER_KEY] = _override(enable_PROMPT_PREFILL=True, PROMPT_PREFILL="适配器")
        layers[INSTANCE_KEY] = _override(enable_PROMPT_PREFILL=True, PROMPT_PREFILL="实例 {{original}}")
        layers[CHANNEL_KEY] = _override(enable_PROMPT_PREFILL=True, PROMPT_PREFILL="频道 {{original}}")

        values = await config_resolver.layered_values(CHAT_KEY, "PROMPT_PREFILL", INSTANCE)
        assert [layer for layer, _value in values] == ["system", "adapter", "instance", "channel"]
        assert [value for _layer, value in values] == [
            "全局",
            "适配器",
            "实例 {{original}}",
            "频道 {{original}}",
        ]

        effective = await config_resolver.get_effective_config(CHAT_KEY, INSTANCE)
        assert effective.PROMPT_PREFILL == values[-1][1]
        # 适配器层没写占位 → 它把全局值整段替换掉，所以全局文字不留在最终结果里
        assert chain_original([value for _layer, value in values]) == "频道 实例 适配器"

    async def test_without_any_override_only_the_global_value(
        self,
        layers: dict[str, OverridableConfig],
        monkeypatch,
    ) -> None:
        _global_value(monkeypatch, "PROMPT_PREFILL", "只有全局")

        values = await config_resolver.layered_values(CHAT_KEY, "PROMPT_PREFILL", INSTANCE)
        assert values == [("system", "只有全局")]

    async def test_disabled_override_layer_is_skipped(
        self,
        layers: dict[str, OverridableConfig],
        monkeypatch,
    ) -> None:
        _global_value(monkeypatch, "PROMPT_PREFILL", "全局")
        layers[INSTANCE_KEY] = _override(enable_PROMPT_PREFILL=False, PROMPT_PREFILL="不该出现")
        layers[CHANNEL_KEY] = _override(enable_PROMPT_PREFILL=True, PROMPT_PREFILL="频道")

        values = await config_resolver.layered_values(CHAT_KEY, "PROMPT_PREFILL", INSTANCE)
        assert values == [("system", "全局"), ("channel", "频道")]

    async def test_instance_view_excludes_channel(
        self,
        layers: dict[str, OverridableConfig],
        monkeypatch,
    ) -> None:
        _global_value(monkeypatch, "PROMPT_AUXILIARY", "全局")
        layers[ADAPTER_KEY] = _override(enable_PROMPT_AUXILIARY=True, PROMPT_AUXILIARY="适配器")
        layers[INSTANCE_KEY] = _override(enable_PROMPT_AUXILIARY=True, PROMPT_AUXILIARY="实例")
        layers[CHANNEL_KEY] = _override(enable_PROMPT_AUXILIARY=True, PROMPT_AUXILIARY="频道")

        values = config_resolver.instance_layered_values(ADAPTER, INSTANCE, "PROMPT_AUXILIARY")
        assert values == [("system", "全局"), ("adapter", "适配器"), ("instance", "实例")]

        effective = config_resolver.get_instance_effective_config(ADAPTER, INSTANCE)
        assert effective.PROMPT_AUXILIARY == values[-1][1]

    @pytest.mark.parametrize(
        "active",
        [
            frozenset(),
            frozenset({"adapter"}),
            frozenset({"instance"}),
            frozenset({"channel"}),
            frozenset({"adapter", "instance"}),
            frozenset({"adapter", "channel"}),
            frozenset({"instance", "channel"}),
            frozenset({"adapter", "instance", "channel"}),
        ],
    )
    async def test_any_layer_subset_matches_the_effective_value(
        self,
        layers: dict[str, OverridableConfig],
        monkeypatch,
        active: frozenset[str],
    ) -> None:
        """任意几层同时覆盖，逐层列表的最高一项都必须等于解析出的生效值

        顺序错一次，`{{original}}` 就会把频道文字串到实例下面；按子集遍历是为了让
        「只开了中间一层」这种最容易写错的形态也有覆盖。
        """
        _global_value(monkeypatch, "PROMPT_PREFILL", "全局")
        names = {"adapter": ADAPTER_KEY, "instance": INSTANCE_KEY, "channel": CHANNEL_KEY}
        display = {"adapter": "适配器", "instance": "实例", "channel": "频道"}
        for name in active:
            layers[names[name]] = _override(enable_PROMPT_PREFILL=True, PROMPT_PREFILL=display[name])

        values = await config_resolver.layered_values(CHAT_KEY, "PROMPT_PREFILL", INSTANCE)
        expected_layers = ["system"] + [name for name in ("adapter", "instance", "channel") if name in active]
        assert [layer for layer, _value in values] == expected_layers

        effective = await config_resolver.get_effective_config(CHAT_KEY, INSTANCE)
        assert effective.PROMPT_PREFILL == values[-1][1]


class TestResolveSlots:
    async def test_preset_layer_and_original_across_layers(
        self,
        layers: dict[str, OverridableConfig],
        monkeypatch,
    ) -> None:
        _global_value(monkeypatch, "PROMPT_POST_HISTORY", "全局后置")
        _global_value(monkeypatch, "PROMPT_AUXILIARY", "全局辅助")
        _global_value(monkeypatch, "PROMPT_PREFILL", "")
        _global_value(monkeypatch, "PROMPT_ADVANCED_SLOTS_ENABLED", True)
        layers[CHANNEL_KEY] = _override(
            enable_PROMPT_POST_HISTORY=True,
            PROMPT_POST_HISTORY="频道后置 {{original}}",
        )
        layers[INSTANCE_KEY] = _override(
            enable_PROMPT_AUXILIARY=True,
            PROMPT_AUXILIARY="实例辅助",
            enable_PROMPT_PREFILL=True,
            PROMPT_PREFILL="实例预填",
        )
        card = DBPresetCard(post_history="人设后置 {{original}}", prefill="人设预填")

        effective = await config_resolver.get_effective_config(CHAT_KEY, INSTANCE)
        slots = await resolve_slots(_channel(), effective, card)

        assert slots.enabled is True
        assert slots.post_history.text == "人设后置 频道后置 全局后置"
        assert slots.post_history.layers == ["system", "channel", "preset"]
        # 辅助提示词没有人设层，实例层没有写占位 → 整段替换
        assert slots.auxiliary.text == "实例辅助"
        assert slots.auxiliary.layers == ["instance"]
        # 预填的人设层没有写占位 → 整段替换掉配置层
        assert slots.prefill.text == "人设预填"
        assert slots.prefill.layers == ["preset"]

    async def test_disabled_gate_still_computes_text(
        self,
        layers: dict[str, OverridableConfig],
        monkeypatch,
    ) -> None:
        _global_value(monkeypatch, "PROMPT_POST_HISTORY", "关着也算")
        _global_value(monkeypatch, "PROMPT_ADVANCED_SLOTS_ENABLED", False)

        effective = await config_resolver.get_effective_config(CHAT_KEY, INSTANCE)
        slots = await resolve_slots(_channel(), effective, None)

        assert slots.enabled is False
        assert slots.post_history.text == "关着也算"
        assert slots.post_history.layers == ["system"]

    async def test_enabled_comes_from_channel_effective_config(
        self,
        layers: dict[str, OverridableConfig],
        monkeypatch,
    ) -> None:
        _global_value(monkeypatch, "PROMPT_ADVANCED_SLOTS_ENABLED", False)
        layers[CHANNEL_KEY] = _override(
            enable_PROMPT_ADVANCED_SLOTS_ENABLED=True,
            PROMPT_ADVANCED_SLOTS_ENABLED=True,
        )

        effective = await config_resolver.get_effective_config(CHAT_KEY, INSTANCE)
        slots = await resolve_slots(_channel(), effective, None)
        assert effective.PROMPT_ADVANCED_SLOTS_ENABLED is True
        assert slots.enabled is True

    async def test_empty_preset_layer_does_not_clear_config_layers(
        self,
        layers: dict[str, OverridableConfig],
        monkeypatch,
    ) -> None:
        _global_value(monkeypatch, "PROMPT_POST_HISTORY", "全局后置")
        _global_value(monkeypatch, "PROMPT_ADVANCED_SLOTS_ENABLED", True)
        card = DBPresetCard(post_history="", prefill="")

        effective = await config_resolver.get_effective_config(CHAT_KEY, INSTANCE)
        slots = await resolve_slots(_channel(), effective, card)

        assert slots.post_history.text == "全局后置"
        assert slots.post_history.layers == ["system"]
        assert slots.prefill.text == ""
        assert slots.prefill.layers == []


def test_core_config_has_slot_fields() -> None:
    """槽位字段确实在 `CoreConfig` 上，且默认关 / 空"""
    cfg = CoreConfig()
    assert cfg.PROMPT_ADVANCED_SLOTS_ENABLED is False
    assert cfg.PROMPT_POST_HISTORY == ""
    assert cfg.PROMPT_AUXILIARY == ""
    assert cfg.PROMPT_PREFILL == ""
