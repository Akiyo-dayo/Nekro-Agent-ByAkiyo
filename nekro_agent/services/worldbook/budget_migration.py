"""启动时把旧的「每轮正文字数上限」迁移到按 token 计的预算（一次，幂等）

0.4.7 起世界书预算照 SillyTavern 按 token 计：`WORLDBOOK_BUDGET_PERCENT`（占上下文的百分比）
与 `WORLDBOOK_BUDGET_CAP_TOKENS`（token 上限，0 表示不另设上限、按百分比算；预算始终不超过 65536）。
旧的 `WORLDBOOK_MAX_CHARS` 不再生效。

## 原则

已经在用世界书的部署，升级后每轮注入多少**基本不变**，不能因为口径换了，费用悄悄翻几倍；
没用过世界书的部署，直接用新的默认值（25%、不另设上限）。

## 做法

1. **判断有没有在用**：四层（全局、适配器、实例、频道）里任何一层显式设过 `WORLDBOOK_MAX_CHARS`
   （全局层看值是不是默认的 3000，覆盖层看 `enable_WORLDBOOK_MAX_CHARS`），或任何一层开过
   `WORLDBOOK_ENABLED`，就算在用。上一次迁移写到一半（覆盖层已经写了、全局还没落盘）时，
   覆盖层里已经有的 token 上限也算在用，重跑结果一样。
2. **在用**：
   - 全局层写 `WORLDBOOK_BUDGET_PERCENT = 100`：百分比不起限制作用，由上限决定预算，与迁移前
     「只有字数上限」的语义一致。只写在全局一处：下层不会留一个高过上层的百分比，免得和
     「租户只能往下调」打架；管理员想换成 ST 的玩法，只改全局两个滑块。
   - 每一层显式设过 `c` 字的，在同一层写 `WORLDBOOK_BUDGET_CAP_TOKENS = round(c × 0.8)`，覆盖开关
     照抄；全局层就是全局的值（默认 3000 → 2400）。
   - `c = 0` 的层（原义「不注入」）：新上限里 0 是「不另设上限」，意思相反。改为在同一层写
     `WORLDBOOK_BUDGET_CAP_TOKENS = 1`（覆盖开关打开）：旧的 0 是「租户只能调低」的天花板，等于禁止
     注入，换成上限 1 仍然是天花板——租户抬不上去（0 在判据里当无穷大，也写不回去），而 1 token 的
     预算实际上什么都放不进（只有正文恰好 1 token 的条目才放得下）。不改这一层的 `WORLDBOOK_ENABLED`：
     改成关掉的话，租户在下层自己打开就能拿到上一层的预算；原来开着、继承 0 的下层会在升级后直接开始注入。
     全局层 `c = 0` 同理，全局上限写 1。
   - 覆盖层里旧的两个键（开关与值）一并删掉；全局层的旧值改回默认 3000，以后再看到别的值就是
     迁移之后有人手改的，启动时打 WARNING。
3. **没在用**：什么都不写，直接用新默认值。
4. 逐层记一行日志，同时存进全局配置 `WORLDBOOK_BUDGET_MIGRATION_NOTICE`，世界书配置页顶部
   据此显示一次性提示，平台方点「知道了」后清空。最后置 `WORLDBOOK_BUDGET_MIGRATED = true`。
5. **每层单独兜底**：一层的文件写不回去或里面的值坏得读不了（例如手填的 `.inf`），只跳过这一层并记
   ERROR，迁移提示里点名「这一层没换算、会继承上一层，请手工设」；别的层照常换算。一层出错就把后面各层
   都停下的话，那些层调低过的上限会悄悄退回继承的更高值。

## 落盘

- **全局层只改内存**，由随后 `bootstrap_model_groups()` 那次常规落盘一起写进配置文件（与
  `services/plugin/default_on.py` 同一个做法：配置落盘牵涉模型组 Key 的掩码时机，这里不另落一次）。
  那次落盘被跳过时，下次启动重跑一遍，结果相同。
- **覆盖层**是各自的文件，按原始 YAML 读写（只动世界书这几个键，其余键原样），原子写入；
  写完从配置缓存里摘掉这一层、作废有效配置版本，之后按新文件重新加载。

书级 `wb_book.budget_chars → budget_tokens` 走数据库迁移，不在这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from nekro_agent.core.config import CHANNEL_CONFIG_DIR, CoreConfig
from nekro_agent.core.config import config as global_config
from nekro_agent.core.core_utils import ConfigManager, atomic_write_text
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

from .budget import CAP_MAX, HARD_CAP_TOKENS

logger = get_sub_logger("worldbook.budget_migration")

# 1 字 → 0.8 token（`o200k_base` 在中文设定正文上约 0.78，取整到 0.8）
TOKENS_PER_LEGACY_CHAR = 0.8
LEGACY_FIELD = "WORLDBOOK_MAX_CHARS"
LEGACY_TOGGLE = f"enable_{LEGACY_FIELD}"
PERCENT_FIELD = "WORLDBOOK_BUDGET_PERCENT"
CAP_FIELD = "WORLDBOOK_BUDGET_CAP_TOKENS"
CAP_TOGGLE = f"enable_{CAP_FIELD}"
ENABLED_FIELD = "WORLDBOOK_ENABLED"
ENABLED_TOGGLE = f"enable_{ENABLED_FIELD}"
# 迁移后全局层写的百分比：不起限制作用，预算由上限决定
MIGRATED_PERCENT = 100
# 旧值 0（「不注入」，租户只能调低的天花板）换算成的 token 上限：仍是天花板，实际上什么都放不进
ZERO_CHARS_CAP_TOKENS = 1


def legacy_default_chars() -> int:
    """旧字数上限的代码默认值（3000）"""
    return int(CoreConfig.model_fields[LEGACY_FIELD].default)


def chars_to_tokens(chars: int) -> int:
    """旧字数上限换算成 token 上限：× 0.8 四舍五入，夹在新上限的合法区间里"""
    return max(1, min(CAP_MAX, round(int(chars) * TOKENS_PER_LEGACY_CHAR)))


@dataclass
class OverrideLayerFile:
    """磁盘上的一份覆盖层文件（原始 YAML）"""

    label: str
    config_key: str
    path: Path
    data: dict[str, Any] = field(default_factory=dict)
    changed: bool = False


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    """读成整数，读不出来用默认值；YAML 里手填的 `.inf` / `.nan` 也算读不出来"""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def discover_override_layers(
    configs_dir: Optional[Path] = None, channels_dir: Optional[Path] = None
) -> list[OverrideLayerFile]:
    """列出磁盘上所有覆盖层文件：适配器 `configs/<适配器>/overrides.yaml`、实例
    `configs/<适配器>/instances/<实例>/overrides.yaml`、频道 `configs/channels/<适配器>/<会话>.yaml`

    读不了或顶层不是映射的文件跳过并记 WARNING：迁移不能因为一份坏文件让启动失败。
    """
    from nekro_agent.services.config_service import (
        ADAPTER_OVERRIDE_KEY_PREFIX,
        CHANNEL_OVERRIDE_KEY_PREFIX,
        INSTANCE_OVERRIDE_KEY_PREFIX,
        INSTANCE_OVERRIDE_KEY_SEP,
    )

    configs_dir = configs_dir or Path(OsEnv.DATA_DIR) / "configs"
    channels_dir = channels_dir or CHANNEL_CONFIG_DIR
    found: list[tuple[str, str, Path]] = []
    if configs_dir.is_dir():
        for adapter_dir in sorted(configs_dir.iterdir()):
            if not adapter_dir.is_dir() or adapter_dir.resolve() == channels_dir.resolve():
                continue
            adapter_key = adapter_dir.name
            adapter_file = adapter_dir / "overrides.yaml"
            if adapter_file.is_file():
                found.append((f"适配器 {adapter_key}", f"{ADAPTER_OVERRIDE_KEY_PREFIX}{adapter_key}", adapter_file))
            instances_dir = adapter_dir / "instances"
            if instances_dir.is_dir():
                for instance_dir in sorted(instances_dir.iterdir()):
                    instance_file = instance_dir / "overrides.yaml"
                    if instance_file.is_file():
                        found.append(
                            (
                                f"实例 {adapter_key}/{instance_dir.name}",
                                f"{INSTANCE_OVERRIDE_KEY_PREFIX}{adapter_key}{INSTANCE_OVERRIDE_KEY_SEP}{instance_dir.name}",
                                instance_file,
                            ),
                        )
    if channels_dir.is_dir():
        for adapter_dir in sorted(channels_dir.iterdir()):
            if not adapter_dir.is_dir():
                continue
            for channel_file in sorted(adapter_dir.glob("*.yaml")):
                found.append(
                    (f"频道 {channel_file.stem}", f"{CHANNEL_OVERRIDE_KEY_PREFIX}{channel_file.stem}", channel_file),
                )

    layers: list[OverrideLayerFile] = []
    for label, config_key, path in found:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - 一份坏文件不能让启动失败
            logger.warning(f"世界书预算迁移：读不了覆盖层文件 {path}，跳过这一层: {error!r}")
            continue
        if data is None:
            data = {}
        if not isinstance(data, dict):
            logger.warning(f"世界书预算迁移：覆盖层文件 {path} 的顶层不是映射，跳过这一层")
            continue
        layers.append(OverrideLayerFile(label=label, config_key=config_key, path=path, data=data))
    return layers


def _layer_in_use(layer: OverrideLayerFile) -> bool:
    data = layer.data
    if _truthy(data.get(LEGACY_TOGGLE)):
        return True
    if _truthy(data.get(ENABLED_TOGGLE)) and _truthy(data.get(ENABLED_FIELD)):
        return True
    # 上一次迁移写到一半：覆盖层已经换成 token 上限，全局还没落盘
    return _truthy(data.get(CAP_TOGGLE))


def _migrate_layer(layer: OverrideLayerFile) -> Optional[str]:
    """改一层覆盖文件里的世界书几个键，返回这一层的迁移记录（没改就是 None）"""
    data = layer.data
    note: Optional[str] = None
    if _truthy(data.get(LEGACY_TOGGLE)):
        chars = _as_int(data.get(LEGACY_FIELD), legacy_default_chars())
        if chars <= 0:
            data[CAP_TOGGLE] = True
            data[CAP_FIELD] = ZERO_CHARS_CAP_TOKENS
            note = (
                f"{layer.label}：原 0 字（不注入）→ 上限 {ZERO_CHARS_CAP_TOKENS} token"
                "（0 在新上限里表示不另设上限，意思相反；1 token 实际上放不进任何条目，租户也抬不上去）"
            )
        else:
            tokens = chars_to_tokens(chars)
            data[CAP_TOGGLE] = True
            data[CAP_FIELD] = tokens
            note = f"{layer.label}：原 {chars} 字 → 上限 {tokens} token"
    if LEGACY_TOGGLE in data or LEGACY_FIELD in data:
        data.pop(LEGACY_TOGGLE, None)
        data.pop(LEGACY_FIELD, None)
        layer.changed = True
    if note is not None:
        layer.changed = True
    return note


def _migrate_layer_safely(layer: OverrideLayerFile, write_layer: Callable[[OverrideLayerFile], None]) -> Optional[str]:
    """换算并写回一层；这一层出任何错都只跳过它自己，返回写进迁移提示的那一行

    一层的错让后面各层都停下的话，那些层调低过的上限会悄悄退回继承的更高值。
    """
    try:
        note = _migrate_layer(layer)
        if layer.changed:
            write_layer(layer)
        return note
    except Exception as error:  # noqa: BLE001 - 一层的错不能拖住别的层，也不能挡启动
        logger.error(f"世界书预算迁移：{layer.label}（{layer.path}）没能迁移，跳过这一层: {error!r}")
        return (
            f"{layer.label}：没能迁移（{type(error).__name__}），这一层原来的字数上限没有换算成 token 上限，"
            "现在继承上一层；请到这一层的配置里手工设 token 上限"
        )


def _write_layer(layer: OverrideLayerFile) -> None:
    from nekro_agent.services.config_resolver import config_resolver

    atomic_write_text(
        layer.path,
        yaml.dump(data=layer.data, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    # 缓存里若已经有这一层（正常启动此刻还没有），摘掉，下次按新文件加载
    ConfigManager.unregister_config(layer.config_key)
    config_resolver.bump_version()


def migrate_worldbook_budget(
    config: Any = None,
    *,
    layers: Optional[list[OverrideLayerFile]] = None,
    write_layer: Callable[[OverrideLayerFile], None] = _write_layer,
) -> list[str]:
    """把旧字数上限迁移到 token 预算；已经迁移过就只检查有没有人又设了旧项。返回迁移记录

    Args:
        config: 全局配置对象（默认是运行中的那一个）；只改内存，落盘交给随后的常规落盘。
        layers: 覆盖层文件（默认扫数据目录）；用例可以直接给。
        write_layer: 写回一层覆盖文件的函数（用例可以换掉）。
    """
    config = global_config if config is None else config
    if bool(getattr(config, "WORLDBOOK_BUDGET_MIGRATED", False)):
        stray = _as_int(getattr(config, LEGACY_FIELD, legacy_default_chars()), legacy_default_chars())
        if stray != legacy_default_chars():
            logger.warning(
                f"{LEGACY_FIELD}（={stray}）已废弃、不再生效：世界书预算 0.4.7 起按 token 计，"
                f"请改用 {PERCENT_FIELD} 与 {CAP_FIELD}（系统配置 → 世界书）",
            )
        return []

    layers = discover_override_layers() if layers is None else layers
    global_chars = _as_int(getattr(config, LEGACY_FIELD, legacy_default_chars()), legacy_default_chars())
    global_set = global_chars != legacy_default_chars()
    in_use = global_set or bool(getattr(config, ENABLED_FIELD, False)) or any(_layer_in_use(layer) for layer in layers)

    notes: list[str] = []
    if in_use:
        setattr(config, PERCENT_FIELD, MIGRATED_PERCENT)
        if global_chars <= 0:
            setattr(config, CAP_FIELD, ZERO_CHARS_CAP_TOKENS)
            notes.append(
                f"全局：原 0 字（不注入）→ 上限 {ZERO_CHARS_CAP_TOKENS} token"
                "（1 token 实际上放不进任何条目，下层也抬不上去）",
            )
        else:
            tokens = chars_to_tokens(global_chars)
            setattr(config, CAP_FIELD, tokens)
            source = "" if global_set else "（默认值）"
            notes.append(f"全局：原 {global_chars} 字{source} → 上限 {tokens} token")
        notes.append(
            f"全局：占上下文的百分比设为 {MIGRATED_PERCENT}%（由上限决定预算，与迁移前「只有字数上限」一致）；"
            f"想换成 SillyTavern 的默认玩法，把全局的百分比改成 25、上限改成 0（不另设上限，按百分比算，"
            f"但每轮最多 {HARD_CAP_TOKENS} token）",
        )
        for layer in layers:
            note = _migrate_layer_safely(layer, write_layer)
            if note is not None:
                notes.append(note)
        for note in notes:
            logger.warning(f"世界书预算已改为按 token 计（启动时迁移）：{note}")
        logger.warning(
            "世界书预算口径从「字」换成了「token」（1 字按 0.8 token 换算）；计数时还会乘模型家族的分词系数，"
            "迁移后能放下的字数随模型略有出入（DeepSeek 多放约两成、Claude 少放约两成）",
        )
    else:
        # 覆盖层里可能还残留着没启用的旧键：顺手删掉，文件里不留一个看不见的废弃项（删不掉也不影响使用）
        for layer in layers:
            try:
                _migrate_layer(layer)
                if layer.changed:
                    write_layer(layer)
            except Exception as error:  # noqa: BLE001
                logger.warning(f"世界书预算迁移：清理覆盖层文件 {layer.path} 里的旧键失败（不影响使用）: {error!r}")

    setattr(config, LEGACY_FIELD, legacy_default_chars())
    setattr(config, "WORLDBOOK_BUDGET_MIGRATION_NOTICE", list(notes))
    setattr(config, "WORLDBOOK_BUDGET_MIGRATED", True)
    return notes


def migrate_worldbook_budget_at_startup() -> None:
    """启动流程里调用：失败只记 ERROR，不挡启动（下次启动重试）"""
    try:
        migrate_worldbook_budget()
    except Exception as error:  # noqa: BLE001 - 迁移失败不能让 NA 起不来
        logger.error(f"世界书预算迁移失败，下次启动重试（这次按配置里现有的值运行）: {error!r}")


__all__ = [
    "TOKENS_PER_LEGACY_CHAR",
    "OverrideLayerFile",
    "chars_to_tokens",
    "discover_override_layers",
    "migrate_worldbook_budget",
    "migrate_worldbook_budget_at_startup",
]
