"""酒馆人设卡接口：检查、导入、扩展行与导出

## 作用域

每个端点都先经过本模块的作用域函数，再按 ID 取行——与 `routers/worldbook.py` 同一
写法：作用域只由当前身份决定，请求里的 `instance_key` 只用来选实例。租户不带实例是
400，带别人的实例是 404；全局人设对租户的写操作是 403（与人设页一致）。

## 导入与检查分两段

检查只解析、不落库，因此不需要事务；导入把「建人设 → 建扩展行 → 建人设书」放进一个
事务，任何一步失败都不留半个人设。两者共用同一份解析器，报告里的字段就是导入会写到
列上的值。
"""

from __future__ import annotations

import datetime
import json
import secrets
import time
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from pydantic import ValidationError as PydanticValidationError
from tortoise.expressions import Q

from nekro_agent.core.config import CHANNEL_CONFIG_DIR, CoreConfig, ModelConfigGroup, config
from nekro_agent.core.core_utils import ConfigBase, ConfigManager
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_config_audit import DBConfigChangeAudit
from nekro_agent.models.db_llm_usage import DBLLMUsageRecord
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.routers.chat_channel import assert_channel_accessible
from nekro_agent.routers.presets import (
    _require_instance,
    get_visible_preset,
    get_writable_preset,
    resolve_instance_scope,
)
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.errors import AppError, NotFoundError, ValidationError
from nekro_agent.schemas.tavern import (
    CardImportOptions,
    CardImportResult,
    CardInspectReport,
    CardReassembleResponse,
    PresetCardResponse,
    PresetCardUpdateRequest,
    SlotsOffChannel,
    TargetGates,
    TavernAuditItem,
    TavernAuditResponse,
    TavernPreviewRequest,
    TavernPreviewResponse,
    TavernTrialRequest,
    TavernTrialResponse,
)
from nekro_agent.schemas.usage import LLMUsageScene
from nekro_agent.services.agent.round_prompt import build_round_prompt
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.config_service import (
    CHANNEL_OVERRIDE_KEY_PREFIX,
    UnifiedConfigService,
    instance_override_config_key,
)
from nekro_agent.services.memory.instance_scope import admin_row_visible
from nekro_agent.services.model_group_service import (
    can_edit_group,
    get_visible_group,
    list_visible_groups,
    resolve_console_identity,
)
from nekro_agent.services.tavern import audit, cleanup
from nekro_agent.services.tavern import cleanup_runtime as cleanup_runtime_service
from nekro_agent.services.tavern import exporter as exporter_service
from nekro_agent.services.tavern import importer as importer_service
from nekro_agent.services.tavern import preview as preview_service
from nekro_agent.services.tavern import trial as trial_service
from nekro_agent.services.tavern.assemble import assemble_content, content_sha256
from nekro_agent.services.tavern.card_format import CardFileRejected
from nekro_agent.services.tavern.limits import (
    MAX_CLEANUP_RULES,
    MAX_GREETING_CHARS,
    MAX_GREETINGS,
    MAX_TEXT_FIELD_CHARS,
    MAX_UPLOAD_BYTES,
)
from nekro_agent.services.tavern.visibility import visible_preset_books
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import (
    accessible_instance_refs,
    assert_instance_accessible,
    get_tenant_scope,
    instance_refs_filter,
)
from nekro_agent.services.worldbook import service as worldbook_service

router = APIRouter(prefix="/tavern", tags=["Tavern"])
logger = get_sub_logger("tavern.admin")

# 扩展行里可由 PUT 修改的来源字段；别的键一律拒绝，免得前端把整份 data 塞进来。
SOURCE_FIELDS = frozenset({"system", "description", "personality", "scenario", "mes_example"})
# 深度注入深度的范围，与卡片导入时夹值用的是同一组数字。
DEPTH_MIN = 0
DEPTH_MAX = 100
# 人设层两个槽位的审计字段名。
_AUDIT_FIELDS = {"post_history": "preset.post_history", "prefill": "preset.prefill"}


def _configured_upload_bytes() -> int:
    """平台配置 `MAX_UPLOAD_SIZE_MB` 折成字节；至少 1 MiB"""
    return max(1, int(config.MAX_UPLOAD_SIZE_MB)) * 1024 * 1024


def _max_upload_bytes() -> int:
    """上传上限：卡片自身的上限与平台配置取小者

    `MAX_UPLOAD_SIZE_MB` 是给其它上传用的总量限制（默认 10），卡片这边还有自己的
    `MAX_UPLOAD_BYTES`（20 MiB）；两者取小，避免「平台设了 50 MiB 就能传一张
    30 MiB 的卡」。所以默认部署实际仍是 10 MiB，平台方调大 `MAX_UPLOAD_SIZE_MB` 才放开到 20。
    """
    return min(MAX_UPLOAD_BYTES, _configured_upload_bytes())


def _upload_too_large_reason(limit: int) -> str:
    """超限时的说明：写明是哪一道上限，并给出「转成 JSON」这条出路

    PNG 卡常把同一份卡片数据存两遍、外加整张图片，导出成 JSON 往往只有一半大小。
    """
    size = f"{limit // (1024 * 1024)} MiB"
    if _configured_upload_bytes() < MAX_UPLOAD_BYTES:
        bound = (
            f"文件超过 {size}（平台的上传文件大小限制 MAX_UPLOAD_SIZE_MB 当前是 {int(config.MAX_UPLOAD_SIZE_MB)} MB，"
            f"卡片本身最大 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB）"
        )
    else:
        bound = f"文件超过 {size}"
    return f"{bound}。可以在酒馆里把卡导出成 JSON 再导入"


async def _read_upload(file: UploadFile) -> bytes:
    """按上限多读一个字节判断超限，不把超大文件整个读进内存"""
    limit = _max_upload_bytes()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationError(reason=_upload_too_large_reason(limit))
    return data


async def _scope_with_instance(
    user: DBUser,
    instance_key: str,
    adapter_key: str,
) -> tuple[str, Optional[DBAdapterInstance]]:
    """解析作用域并按需取实例行；租户不带实例 400，别人的实例 404"""
    scope = await resolve_instance_scope(user, instance_key)
    if not scope:
        return scope, None
    instance = await assert_instance_accessible(user, scope, adapter_key=adapter_key)
    return scope, instance


async def _preset_scope(user: DBUser, instance_key: str) -> str:
    """人设类端点的作用域：与现有建人设接口同一口径"""
    return await resolve_instance_scope(user, instance_key)


async def _load_visible_channel(user: DBUser, chat_key: str) -> DBChatChannel:
    """按 `chat_key` 取频道并判归属；不存在与不可见抛同一个 404

    与 `routers/worldbook.py` 的 `_load_visible_channel` 同形：`chat_key` 在频道表上只有
    索引、没有唯一约束，同一 `chat_key` 可能有多行。租户必须对**每一行**都有权限才放行
    （fail-closed），取第一行时按 ID 排序，结果不随数据库返回顺序变化。

    判完多行再走一次 `assert_channel_accessible`：它是全仓频道授权的统一出口，权限契约
    扫描认这个名字；这里不另写一套「实例对在不在我的列表里」。
    """
    channels = await DBChatChannel.filter(chat_key=chat_key).order_by("id").all()
    if not channels:
        raise NotFoundError(resource="聊天频道")
    if get_tenant_scope(user) is not None:
        for channel in channels:
            if not await admin_row_visible(user, channel.adapter_key, channel.instance_key):
                raise NotFoundError(resource="聊天频道")
    await assert_channel_accessible(user, chat_key)
    return channels[0]


# 试发防刷：每个用户每分钟 5 次。进程内计数只为挡住误操作与脚本刷，重启清零；
# 真正要跨重启保留的是每实例每天的上限（按账本行数算，见下面的函数）。
_TRIAL_PER_MINUTE = 5
_TRIAL_WINDOW_SECONDS = 60.0
_trial_timestamps: dict[int, list[float]] = {}


class TrialRateLimitedError(ValidationError):
    """试发太频繁；继承 `ValidationError` 以便调用方按 400 处理"""

    i18n_message = ValidationError.i18n_message


def _assert_trial_per_minute(user: DBUser) -> None:
    """同一管理员一分钟内最多 5 次；计数只留窗口内的戳"""
    now = time.monotonic()
    recent = [stamp for stamp in _trial_timestamps.get(user.id, []) if now - stamp < _TRIAL_WINDOW_SECONDS]
    if len(recent) >= _TRIAL_PER_MINUTE:
        raise TrialRateLimitedError(reason=f"试发太频繁了，每分钟最多 {_TRIAL_PER_MINUTE} 次，请稍后再试")
    recent.append(now)
    _trial_timestamps[user.id] = recent


async def _assert_trial_daily(adapter_key: str, instance_key: str) -> None:
    """每个实例每天最多 `TAVERN_TRIAL_DAILY_LIMIT` 次；0 表示不限

    按账本里这个实例当天 `prompt_trial` 的行数算，所以重启不清零。行数是**实例级**的：
    同一实例上多个管理员共用这个额度。`instance_key` 只在适配器内唯一，过滤必须成对。
    """
    limit = int(getattr(config, "TAVERN_TRIAL_DAILY_LIMIT", 0) or 0)
    if limit <= 0:
        return
    midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    used = await DBLLMUsageRecord.filter(
        adapter_key=adapter_key,
        instance_key=instance_key,
        scene=LLMUsageScene.PROMPT_TRIAL.value,
        create_time__gte=midnight,
    ).count()
    if used >= limit:
        raise TrialRateLimitedError(reason=f"这个实例今天最多试发 {limit} 次，请明天再试")


def _gates_for(instance: Optional[DBAdapterInstance]) -> TargetGates:
    """导入目标的两个闸；没有实例时用全局配置"""
    effective = config
    if instance is not None:
        effective = config_resolver.get_instance_effective_config(instance.adapter_key, instance.instance_key)
    return TargetGates(
        worldbook_enabled=bool(getattr(effective, "WORLDBOOK_ENABLED", False)),
        advanced_slots_enabled=bool(getattr(effective, "PROMPT_ADVANCED_SLOTS_ENABLED", False)),
    )


# ---- 导入时顺带打开高级提示词槽位 -------------------------------------------------
#
# 卡里有后置指令、导入目标实例的槽位是关的，导入确认里问一句「要不要一起打开」。打开写的是
# 这个实例的实例级覆盖，走通用配置的批量写入口（与配置表单打开这一项是同一个函数），所以
# 权限（租户只能开自己的实例）、落盘与 `config_change_audit` 都与现有开关一致。配置在文件里、
# 人设在库里，放不进一个事务：先导入、再打开，打开失败时人设照样留着，结果里写明没打开。

_SLOTS_FIELD = "PROMPT_ADVANCED_SLOTS_ENABLED"


def _slots_enable_confirm() -> dict[str, str]:
    """配置表单打开这一项时弹的确认文案（键是 zh-CN / en-US），导入确认里原样给出"""
    extra = CoreConfig.model_fields[_SLOTS_FIELD].json_schema_extra
    confirm = extra.get("enable_confirm") if isinstance(extra, dict) else None
    return {str(key): str(value) for key, value in confirm.items()} if isinstance(confirm, dict) else {}


def _channel_override_layer(adapter_key: str, chat_key: str) -> Optional[ConfigBase]:
    """取频道级覆盖层；没有覆盖文件的频道返回 None

    不走 `UnifiedConfigService._get_config_instance` 的「加载不到就建」：检查一次导入不该给
    这个实例的每个频道都建一份空覆盖文件。已加载过的用内存里那份（它比文件新），否则只在
    文件存在时才加载。
    """
    config_key = f"{CHANNEL_OVERRIDE_KEY_PREFIX}{chat_key}"
    cached = ConfigManager.get_config(config_key)
    if cached is not None:
        return cached
    if not (CHANNEL_CONFIG_DIR / adapter_key / f"{chat_key}.yaml").exists():
        return None
    return UnifiedConfigService._get_config_instance(config_key)  # noqa: SLF001


async def _slots_off_channels(instance: DBAdapterInstance) -> list[SlotsOffChannel]:
    """这个实例下用频道级覆盖把槽位单独关着的频道"""
    rows = (
        await DBChatChannel.filter(adapter_key=instance.adapter_key, instance_key=instance.instance_key)
        .order_by("id")
        .values_list("chat_key", "channel_name")
    )
    found: list[SlotsOffChannel] = []
    seen: set[str] = set()
    for chat_key, channel_name in rows:
        if chat_key in seen:
            continue
        seen.add(chat_key)
        layer = _channel_override_layer(instance.adapter_key, chat_key)
        if (
            layer is not None
            and getattr(layer, f"enable_{_SLOTS_FIELD}", False)
            and not getattr(layer, _SLOTS_FIELD, False)
        ):
            found.append(SlotsOffChannel(chat_key=chat_key, channel_name=channel_name or ""))
    return found


async def _import_gates(instance: Optional[DBAdapterInstance]) -> TargetGates:
    """导入检查用的闸状态：在两个生效值之外，说明能不能顺带打开槽位

    没有目标实例（平台方在全局视图导入全局人设）时不提供顺带打开：那要改的是全局开关，
    影响所有实例，不在导入确认里顺手做。
    """
    gates = _gates_for(instance)
    gates.advanced_slots_global_enabled = bool(getattr(config, _SLOTS_FIELD, False))
    if instance is not None:
        gates.advanced_slots_off_channels = await _slots_off_channels(instance)
        if not gates.advanced_slots_enabled:
            gates.can_enable_advanced_slots = True
            gates.advanced_slots_enable_confirm = _slots_enable_confirm()
    return gates


async def _enable_slots_after_import(
    user: DBUser,
    instance: Optional[DBAdapterInstance],
    result: CardImportResult,
) -> None:
    """「打开并导入」的后一半：人设已经导入，按需打开这个实例的高级槽位，结果写回 `result`"""
    card = await DBPresetCard.get_or_none(preset_id=result.preset_id)
    if instance is None or card is None or not card.post_history:
        # 没导入后置指令（卡里没有，或导入时取消了勾选），或没有目标实例：没什么要打开的
        result.advanced_slots = "not_needed"
        return
    result.advanced_slots_off_channels = await _slots_off_channels(instance)
    effective = config_resolver.get_instance_effective_config(instance.adapter_key, instance.instance_key)
    if bool(getattr(effective, _SLOTS_FIELD, False)):
        result.advanced_slots = "already_enabled"
        return

    # 与配置表单同一个写入口：权限、落盘、审计都不另写一套（延迟导入，两个路由模块互不依赖加载顺序）
    from nekro_agent.routers import config as config_router

    config_key = instance_override_config_key(instance.adapter_key, instance.instance_key)
    try:
        await config_router.batch_update_config(
            config_key,
            config_router.BatchUpdateConfig(configs={f"enable_{_SLOTS_FIELD}": "true", _SLOTS_FIELD: "true"}),
            _current_user=user,
        )
    except AppError as exc:
        result.advanced_slots = "failed"
        result.advanced_slots_error = str(exc)
    except Exception as exc:
        logger.exception(f"导入后打开高级提示词槽位失败: {instance.adapter_key}/{instance.instance_key}")
        result.advanced_slots = "failed"
        result.advanced_slots_error = f"保存配置失败: {exc}"
    else:
        result.advanced_slots = "enabled"
        result.notes.append(f"已为实例 {instance.instance_key} 打开高级提示词槽位（实例级覆盖）")
        return
    result.notes.append(f"人设已导入，但高级提示词槽位没打开：{result.advanced_slots_error}")


def _strip_original(rules: Any) -> list[dict[str, Any]]:
    """返回给前端的规则不带 `original`：那是导出还原用的底稿，可能很大"""
    if not isinstance(rules, list):
        return []
    result: list[dict[str, Any]] = []
    for rule in rules:
        if isinstance(rule, dict):
            result.append({key: value for key, value in rule.items() if key != "original"})
    return result


def _slow_disabled_rules(preset_id: int) -> list[int]:
    """本进程里属于这个人设的停用规则序号，升序

    扩展页要标「这条规则太慢、已经被停用」，否则管理员看到的 `enabled: true` 与
    实际行为对不上。
    """
    return sorted(index for (owner, index) in cleanup_runtime_service.disabled_cleanup_rules() if owner == preset_id)


def _card_response(
    preset: DBPreset,
    card: Optional[DBPresetCard],
    gates: TargetGates,
) -> PresetCardResponse:
    """把扩展行（或默认值）拼成响应

    `gates` 由调用方按作用域算好再传进来：两个端点的作用域来源不同（GET 只要可见，
    PUT 要可写），但「这次请求的目标实例是谁」是同一个东西。
    """
    slow_disabled = _slow_disabled_rules(preset.id)
    if card is None:
        return PresetCardResponse(
            exists=False,
            preset_id=preset.id,
            source_fields={},
            content_assembled=False,
            # 没有扩展行时宏必须是关的：模型默认值是「导入的卡默认开」，而 PUT 新建
            # 行时写的是 `macros_enabled=False`。前端拿 GET 默认值当表单初值，若这里
            # 回模型默认值，NA 原生人设第一次保存扩展就会把宏打开，正文随之被展开。
            macros_enabled=False,
            gates=gates,
            slow_disabled_rules=slow_disabled,
        )
    return PresetCardResponse(
        exists=True,
        preset_id=preset.id,
        card_spec=card.card_spec,
        nickname=card.nickname,
        source_fields=card.source_fields if isinstance(card.source_fields, dict) else {},
        assembled_sha256=card.assembled_sha256,
        content_assembled=bool(card.assembled_sha256) and content_sha256(preset.content) == card.assembled_sha256,
        macros_enabled=card.macros_enabled,
        greetings=list(card.greetings or []),
        group_greetings=list(card.group_greetings or []),
        greeting_pick=card.greeting_pick,
        post_history=card.post_history,
        prefill=card.prefill,
        depth_prompt=card.depth_prompt,
        depth_prompt_depth=card.depth_prompt_depth,
        depth_prompt_role=card.depth_prompt_role,
        cleanup_rules=_strip_original(card.cleanup_rules),
        character_version=card.character_version,
        imported_by=card.imported_by,
        imported_at=card.imported_at,
        gates=gates,
        slow_disabled_rules=slow_disabled,
    )


def _too_long(field: str, value: str, limit: int) -> None:
    if len(value) > limit:
        raise ValidationError(reason=f"{field}最多 {limit} 字")


def _validate_greetings(field: str, greetings: list[str]) -> None:
    """开场白两类共用一套上限：最多 50 条、每条最多 16000 字"""
    if len(greetings) > MAX_GREETINGS:
        raise ValidationError(reason=f"{field}最多 {MAX_GREETINGS} 条")
    for index, greeting in enumerate(greetings):
        if len(greeting) > MAX_GREETING_CHARS:
            raise ValidationError(reason=f"{field}第 {index + 1} 条超过 {MAX_GREETING_CHARS} 字")


def _restore_rule_originals(
    incoming: list[dict[str, Any]],
    old_rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按「同一序号、`name` 相同」从旧值补回 `original`

    前端读到的规则不带 `original`，保存时整条传回；对不上序号的当成新加的规则。
    这样「打开扩展页、改一个字、保存」不会把导出还原用的底稿弄丢。
    """
    result: list[dict[str, Any]] = []
    for index, rule in enumerate(incoming):
        original = rule.get("original")
        if not isinstance(original, dict):
            old = old_rules[index] if index < len(old_rules) else None
            if isinstance(old, dict) and old.get("name") == rule.get("name"):
                original = old.get("original")
        trim = rule.get("trim")
        result.append(
            {
                "name": rule.get("name") if isinstance(rule.get("name"), str) else "",
                "find": rule.get("find") if isinstance(rule.get("find"), str) else "",
                "replace": rule.get("replace") if isinstance(rule.get("replace"), str) else "",
                "trim": list(trim) if isinstance(trim, list) else [],
                "enabled": bool(rule.get("enabled")),
                "disabled_reason": rule.get("disabled_reason") if isinstance(rule.get("disabled_reason"), str) else "",
                "original": original if isinstance(original, dict) else None,
            },
        )
    return result


def _outgoing_cleanup_rules(req: PresetCardUpdateRequest, old_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验并整理请求里的清理规则；不过时一次性列出所有序号与原因"""
    rules = req.cleanup_rules or []
    if len(rules) > MAX_CLEANUP_RULES:
        raise ValidationError(reason=f"清理规则最多 {MAX_CLEANUP_RULES} 条")
    errors: list[str] = []
    for index, rule in enumerate(rules):
        # 停用的规则不参与运行（`compile_rules` 只编译启用的），不能拿启用规则的标准
        # 去卡正则：导入时因 `invalid_regex` / `unsafe_regex` 停用的规则本来就过不了
        # `validate_rule`，把整张表传回来保存会连带把表锁死。停用规则只查存储形状。
        reason = cleanup.validate_rule(rule) if rule.get("enabled") else cleanup.validate_disabled_rule_shape(rule)
        if reason is not None:
            errors.append(f"第 {index + 1} 条：{reason}")
    if errors:
        raise ValidationError(reason="清理规则不合法：" + "；".join(errors))
    return _restore_rule_originals(rules, old_rules)


def _validated_updates(req: PresetCardUpdateRequest) -> dict[str, Any]:
    """逐字段校验请求；只返回真的出现在请求里的字段"""
    updates: dict[str, Any] = {}
    present = req.model_fields_set
    scalar_limits = {
        "post_history": ("后置指令", MAX_TEXT_FIELD_CHARS),
        "prefill": ("预填文字", MAX_TEXT_FIELD_CHARS),
        "depth_prompt": ("深度注入", MAX_TEXT_FIELD_CHARS),
        "character_version": ("character_version", 64),
    }
    for field, (label, limit) in scalar_limits.items():
        if field in present and req.__dict__.get(field) is not None:
            value = getattr(req, field)
            _too_long(label, value, limit)
            updates[field] = value
    if "nickname" in present and req.nickname is not None:
        _too_long("昵称", req.nickname, 128)
        updates["nickname"] = req.nickname
    if "depth_prompt_role" in present and req.depth_prompt_role is not None:
        _too_long("深度注入角色", req.depth_prompt_role, 16)
        updates["depth_prompt_role"] = req.depth_prompt_role
    if "depth_prompt_depth" in present and req.depth_prompt_depth is not None:
        if not DEPTH_MIN <= req.depth_prompt_depth <= DEPTH_MAX:
            raise ValidationError(reason=f"深度注入位置必须在 {DEPTH_MIN} 到 {DEPTH_MAX} 之间")
        updates["depth_prompt_depth"] = req.depth_prompt_depth
    if "greeting_pick" in present and req.greeting_pick is not None:
        if req.greeting_pick not in ("first", "random"):
            raise ValidationError(reason="开场白挑选方式只能是 first 或 random")
        updates["greeting_pick"] = req.greeting_pick
    if "greetings" in present and req.greetings is not None:
        _validate_greetings("开场白", req.greetings)
        updates["greetings"] = req.greetings
    if "group_greetings" in present and req.group_greetings is not None:
        _validate_greetings("群聊专用开场白", req.group_greetings)
        updates["group_greetings"] = req.group_greetings
    if "source_fields" in present and req.source_fields is not None:
        unknown = sorted(set(req.source_fields) - SOURCE_FIELDS)
        if unknown:
            raise ValidationError(reason="来源字段只能包含 " + "、".join(sorted(SOURCE_FIELDS)))
        for field, value in req.source_fields.items():
            _too_long(f"来源字段 {field}", value, MAX_TEXT_FIELD_CHARS)
        updates["source_fields"] = req.source_fields
    if "macros_enabled" in present and req.macros_enabled is not None:
        updates["macros_enabled"] = req.macros_enabled
    return updates


async def _audit_slot_change(
    user: DBUser,
    preset: DBPreset,
    field: str,
    old_value: str,
    new_value: str,
) -> None:
    """人设层槽位改了才写一行审计；摘要规则与通用配置写入口共用同一份"""
    await audit.record_change(
        user=user,
        layer="preset",
        field=_AUDIT_FIELDS[field],
        old=old_value,
        new=new_value,
        instance_key=preset.owner_instance_key,
        preset_id=preset.id,
    )


@router.post("/cards/inspect", summary="检查一张酒馆人设卡（不落库）")
@require_role(Role.Admin)
async def inspect_card_endpoint(
    file: UploadFile = File(...),
    instance_key: str = Form(""),
    adapter_key: str = Form(""),
    _current_user: DBUser = Depends(get_current_active_user),
) -> CardInspectReport:
    """只解析上传的卡片，返回导入报告与拼装预览"""
    data = await _read_upload(file)
    _scope, instance = await _scope_with_instance(_current_user, instance_key, adapter_key)
    budget_chars = (
        worldbook_service.import_budget_chars(instance.adapter_key, instance.instance_key)
        if instance is not None
        else worldbook_service.import_budget_chars("", "")
    )
    try:
        return await importer_service.inspect_card(
            data,
            template=config.TAVERN_CARD_TEMPLATE,
            target=await _import_gates(instance),
            budget_chars=budget_chars,
        )
    except CardFileRejected as exc:
        raise ValidationError(reason=str(exc)) from exc


@router.post("/cards/import", summary="导入一张酒馆人设卡")
@require_role(Role.Admin)
async def import_card_endpoint(
    file: UploadFile = File(...),
    options: str = Form("{}"),
    instance_key: str = Form(""),
    adapter_key: str = Form(""),
    _current_user: DBUser = Depends(get_current_active_user),
) -> CardImportResult:
    """上传文件 + 选项，建人设、扩展行与人设书；作用域同现有建人设"""
    try:
        raw_options = json.loads(options or "{}")
        if not isinstance(raw_options, dict):
            raise ValueError("选项必须是对象")
        import_options = CardImportOptions.model_validate(raw_options)
    except (ValueError, PydanticValidationError) as exc:
        raise ValidationError(reason="导入选项格式不正确") from exc

    scope = await _preset_scope(_current_user, instance_key)
    instance: Optional[DBAdapterInstance] = None
    if scope:
        instance = await _require_instance(_current_user, scope)
        if adapter_key and instance.adapter_key != adapter_key:
            # 同一个 instance_key 可能在别的适配器下也有实例；导入要写归属，必须落对行。
            instance = await assert_instance_accessible(_current_user, scope, adapter_key=adapter_key)
    data = await _read_upload(file)
    book_owner = (instance.adapter_key, instance.instance_key) if instance is not None else ("", "")
    try:
        result = await importer_service.import_card(
            data,
            import_options,
            owner_instance_key=scope,
            book_owner=book_owner,
            username=_current_user.username,
            template=config.TAVERN_CARD_TEMPLATE,
        )
    except CardFileRejected as exc:
        raise ValidationError(reason=str(exc)) from exc
    if import_options.enable_advanced_slots:
        await _enable_slots_after_import(_current_user, instance, result)
    return result


@router.get("/presets/{preset_id}/card", summary="读人设的酒馆扩展")
@require_role(Role.Admin)
async def get_preset_card(
    preset_id: int,
    instance_key: str = "",
    adapter_key: str = "",
    _current_user: DBUser = Depends(get_current_active_user),
) -> PresetCardResponse:
    """返回扩展行；没有行时返回默认值与 `exists: false`

    `instance_key` / `adapter_key` 都是查询参数：前者是作用域，后者只用来选实例拿闸状态，
    可见性判据仍是 `get_visible_preset`。默认值写成裸空串而不是 `Query("")`：这个函数
    有用例直接调用，`Query` 对象会当成查询值传下去，把「没传」变成一个查不到的适配器。
    """
    scope, instance = await _scope_with_instance(_current_user, instance_key, adapter_key)
    preset = await get_visible_preset(preset_id, scope)
    card = await DBPresetCard.get_or_none(preset_id=preset.id)
    return _card_response(preset, card, _gates_for(instance))


@router.put("/presets/{preset_id}/card", summary="改人设的酒馆扩展")
@require_role(Role.Admin)
async def update_preset_card(
    preset_id: int,
    req: PresetCardUpdateRequest,
    instance_key: str = "",
    adapter_key: str = "",
    _current_user: DBUser = Depends(get_current_active_user),
) -> PresetCardResponse:
    """只改请求里出现的字段；没有扩展行时新建一行

    可写性判据仍是 `get_writable_preset`，`adapter_key` 只影响返回的闸状态；两个查询参数
    的默认值写法与上面的读接口同一理由。
    """
    scope, instance = await _scope_with_instance(_current_user, instance_key, adapter_key)
    preset = await get_writable_preset(preset_id, scope)
    card = await DBPresetCard.get_or_none(preset_id=preset.id)
    old_rules = card.cleanup_rules if card is not None and isinstance(card.cleanup_rules, list) else []

    updates = _validated_updates(req)
    if "cleanup_rules" in req.model_fields_set and req.cleanup_rules is not None:
        updates["cleanup_rules"] = _outgoing_cleanup_rules(req, old_rules)

    if card is None:
        card = await DBPresetCard.create(preset=preset, card_spec="", macros_enabled=False)

    # 审计比较的是写入前的值，因此先取旧值再落字段。
    old_values = {field: getattr(card, field, "") for field in ("post_history", "prefill")}
    for field, value in updates.items():
        setattr(card, field, value)
    await card.save()

    # 规则改过之后重新给这条人设一次超时额度：旧规则太慢留下的本进程停用登记不该
    # 跟着新规则继续生效。放在保存成功之后，保存失败时登记保持原样。
    if "cleanup_rules" in updates:
        cleanup_runtime_service.forget_preset(preset.id)

    for field in ("post_history", "prefill"):
        if field in updates:
            await _audit_slot_change(_current_user, preset, field, old_values[field], updates[field])
    return _card_response(preset, card, _gates_for(instance))


@router.post("/presets/{preset_id}/card/reassemble", summary="按当前模板重新拼装正文")
@require_role(Role.Admin)
async def reassemble_preset_card(
    preset_id: int,
    instance_key: str = Query(""),
    _current_user: DBUser = Depends(get_current_active_user),
) -> CardReassembleResponse:
    """用当前模板与 `source_fields` 重拼正文，覆盖手改"""
    scope = await _preset_scope(_current_user, instance_key)
    preset = await get_writable_preset(preset_id, scope)
    card = await DBPresetCard.get_or_none(preset_id=preset.id)
    if card is None:
        raise ValidationError(reason="这个人设还没有酒馆扩展行，先保存一次扩展再重新拼装")
    source_fields = card.source_fields if isinstance(card.source_fields, dict) else {}
    assembled = assemble_content(config.TAVERN_CARD_TEMPLATE, source_fields)
    digest = content_sha256(assembled)
    preset.content = assembled
    card.assembled_sha256 = digest
    await preset.save()
    await card.save()
    return CardReassembleResponse(content=assembled, assembled_sha256=digest)


@router.get("/presets/{preset_id}/export", summary="导出酒馆人设卡")
@require_role(Role.Admin)
async def export_preset_card(
    preset_id: int,
    format: str = Query("json"),
    instance_key: str = Query(""),
    _current_user: DBUser = Depends(get_current_active_user),
) -> Response:
    """导出 PNG 或 JSON；知识书只含导出人看得到的书"""
    if format not in ("png", "json"):
        raise ValidationError(reason="导出格式只能是 png 或 json")
    scope = await _preset_scope(_current_user, instance_key)
    preset = await get_visible_preset(preset_id, scope)
    card = await DBPresetCard.get_or_none(preset_id=preset.id)
    books = await visible_preset_books(_current_user, preset.id)

    if format == "png":
        payload, filename = exporter_service.export_png(preset, card, books)
        media_type = "image/png"
        fallback = f"preset-{preset.id}.png"
    else:
        payload, filename = exporter_service.export_json(preset, card, books)
        media_type = "application/json"
        fallback = f"preset-{preset.id}.json"

    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content=payload, media_type=media_type, headers={"Content-Disposition": disposition})


async def _select_trial_group(
    user: DBUser,
    channel: DBChatChannel,
    effective_config: Any,
    requested: Optional[str],
) -> tuple[DBModelGroup, ModelConfigGroup, str]:
    """挑这次试发真正用的模型组；返回（库行、运行时组、组名）

    平台方不受限：显式给的组按 `get_visible_group` 取（不可见即 404），不传就用频道生效
    的主组。租户只能用自己名下的组：显式给平台组或其他租户的组一律按不存在处理（404），
    不传时若频道生效的组不是自己的，就按「名下有没有组」给两句不同的 400。
    """
    identity = resolve_console_identity(user)
    effective_name = str(getattr(effective_config, "USE_MODEL_GROUP", "") or "")
    is_platform = get_tenant_scope(user) is None

    if requested:
        row = await get_visible_group(requested, identity)
        if not is_platform and not can_edit_group(row, identity):
            # 平台方的 shared 组与别的租户的组都走这一支：对外与「不存在」不可区分。
            raise NotFoundError(resource="模型组")
        if row.model_type != "chat":
            raise ValidationError(reason="试发只能用聊天模型组，这个组不是 chat 类型")
        name = row.name
    else:
        row = await DBModelGroup.get_or_none(name=effective_name) if effective_name else None
        if not is_platform and (row is None or not can_edit_group(row, identity)):
            own_chat = [
                info for info in await list_visible_groups(identity) if info.editable and info.model_type == "chat"
            ]
            if not own_chat:
                raise ValidationError(reason="你名下还没有模型组，不能试发；先在模型组页添加一个")
            raise ValidationError(reason="频道当前用的是平台方的模型组，请选一个你自己名下的模型组试发")
        if row is None:
            raise ValidationError(reason="频道当前没有可用的主模型组，请先选一个模型组试发")
        if row.model_type != "chat":
            raise ValidationError(reason="频道当前的主模型组不是聊天模型组，请选一个 chat 类型的组试发")
        name = effective_name

    runtime_group = config.MODEL_GROUPS.get(name)
    if runtime_group is None:
        # 组在库里、但运行时视图还没有它（刚建完还没同步）。试发不该拿一份凭空构造的
        # 配置去发请求：直接让管理员换一个可用的组。
        raise ValidationError(reason="这个模型组还没有可用的运行时配置，请换一个组试发")
    return row, runtime_group, name


@router.post("/preview", summary="预览提示词")
@require_role(Role.Admin)
async def preview_prompt(
    req: TavernPreviewRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TavernPreviewResponse:
    """按频道当前配置渲染这一轮会发给模型的完整消息；不调模型、不写库、不留状态"""
    channel = await _load_visible_channel(_current_user, req.chat_key)
    effective_config = await channel.get_effective_config()
    sample_text = req.sample_text
    if sample_text is not None and len(sample_text) > preview_service.PREVIEW_SAMPLE_MAX_CHARS:
        raise ValidationError(reason=f"示例消息不能超过 {preview_service.PREVIEW_SAMPLE_MAX_CHARS} 字")

    preset_override: Optional[DBPreset] = None
    if req.preset_id is not None:
        # 换人设只看，不切换：可见性按频道所属实例判，与设置人设的入口同一口径。
        preset_override = await get_visible_preset(req.preset_id, channel.instance_key)

    tenant_scope = get_tenant_scope(_current_user) is not None
    hidden_book_ids: set[int] = set()
    if tenant_scope:
        hidden_book_ids = await worldbook_service.books_invisible_to(_current_user, await DBWbBook.all())

    extra_history_lines: Optional[list[str]] = None
    token = secrets.token_hex(8)
    if sample_text:
        extra_history_lines = [
            preview_service.format_synthetic_history_line(
                token,
                preview_service.PREVIEW_NICKNAME,
                preview_service.sanitize_sample_text(sample_text),
                effective_config,
            ),
        ]

    ctx = await AgentCtx.create_by_chat_key(req.chat_key)
    prompt = await build_round_prompt(
        chat_key=req.chat_key,
        db_chat_channel=channel,
        ctx=ctx,
        one_time_code=token,
        trigger_message=None,
        dry_run=True,
        preset_override=preset_override,
        extra_history_lines=extra_history_lines,
        hidden_book_ids=hidden_book_ids,
        mask_platform_slots=tenant_scope,
    )
    return preview_service.build_preview_response(prompt, token)


@router.post("/trial", summary="试发一次提示词")
@require_role(Role.Admin)
async def trial_prompt(
    req: TavernTrialRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TavernTrialResponse:
    """用管理员自己的模型组发一次；不发进任何频道、不进记忆、不执行代码"""
    text = req.text.strip()
    if not text:
        raise ValidationError(reason="试发内容不能为空")
    if len(req.text) > preview_service.PREVIEW_SAMPLE_MAX_CHARS:
        raise ValidationError(reason=f"试发内容不能超过 {preview_service.PREVIEW_SAMPLE_MAX_CHARS} 字")

    channel = await _load_visible_channel(_current_user, req.chat_key)
    effective_config = await channel.get_effective_config()
    _row, runtime_group, group_name = await _select_trial_group(
        _current_user,
        channel,
        effective_config,
        req.model_group,
    )
    _assert_trial_per_minute(_current_user)
    await _assert_trial_daily(channel.adapter_key, channel.instance_key)

    token = secrets.token_hex(8)
    line = preview_service.format_synthetic_history_line(
        token,
        "管理员（试发）",
        preview_service.sanitize_sample_text(req.text),
        effective_config,
    )
    ctx = await AgentCtx.create_by_chat_key(req.chat_key)
    prompt = await build_round_prompt(
        chat_key=req.chat_key,
        db_chat_channel=channel,
        ctx=ctx,
        one_time_code=token,
        trigger_message=None,
        dry_run=True,
        model_group_override=runtime_group,
        extra_history_lines=[line],
    )
    return await trial_service.run_trial(
        prompt=prompt,
        channel=channel,
        group=runtime_group,
        group_name=group_name,
        config=effective_config,
        operator=_current_user.username,
    )


def _tenant_audit_filter(refs: set[tuple[str, str]]) -> Q:
    """租户能看的审计行：自己实例对上的配置层行，加上自己实例的人设层行

    人设只按实例标识归属（`presets.owner_instance_key`，没有适配器一列），所以人设层的
    审计行只有 `instance_key`、`adapter_key` 是空串；按实例对成对匹配会把它们全漏掉，
    租户就看不到自己人设上后置指令 / 预填的改动。人设层单独按实例标识匹配，与人设页
    的归属判据一致。
    """
    preset_keys = sorted({instance for _adapter, instance in refs if instance})
    preset_rows = Q(layer="preset", adapter_key="", instance_key__in=preset_keys) if preset_keys else Q(id__in=[])
    return instance_refs_filter(refs) | preset_rows


@router.get("/audit", summary="配置变更记录")
@require_role(Role.Admin)
async def list_tavern_audit(
    instance_key: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _current_user: DBUser = Depends(get_current_active_user),
) -> TavernAuditResponse:
    """按时间倒序列出配置变更；租户只看自己实例的行，空实例的系统 / 适配器行不可见"""
    query = DBConfigChangeAudit.all()
    if get_tenant_scope(_current_user) is not None:
        # `instance_key` 不唯一：另一个适配器下可以有同名实例归别的租户。按
        # `(adapter_key, instance_key)` 成对过滤，而不是拿字符串列表做 `__in`。
        refs = await accessible_instance_refs(_current_user)
        # 空 `instance_key` 是系统 / 适配器层的行；即使某个实例恰好用了空标识，租户也不该
        # 看到这些行——任务书把「平台方写的配置」挡在租户外面的判据就是这一列。
        query = query.exclude(instance_key="")
        if instance_key:
            selected = {ref for ref in refs if ref[1] == instance_key}
            if not selected:
                # 越权与不存在同一口径：403 会让租户枚举出别人的实例标识。
                raise NotFoundError(resource="实例")
            query = query.filter(_tenant_audit_filter(selected))
        else:
            query = query.filter(_tenant_audit_filter(refs))
    elif instance_key:
        query = query.filter(instance_key=instance_key)

    total = await query.count()
    rows = await query.order_by("-created_at", "-id").offset((page - 1) * page_size).limit(page_size)
    return TavernAuditResponse(
        total=total,
        items=[
            TavernAuditItem(
                id=row.id,
                created_at=row.created_at,
                operator=row.operator,
                operator_is_platform=row.operator_is_platform,
                layer=row.layer,
                adapter_key=row.adapter_key,
                instance_key=row.instance_key,
                chat_key=row.chat_key,
                preset_id=row.preset_id,
                field=row.field,
                old_value=row.old_value,
                new_value=row.new_value,
            )
            for row in rows
        ],
    )


__all__ = [
    "export_preset_card",
    "get_preset_card",
    "import_card_endpoint",
    "inspect_card_endpoint",
    "list_tavern_audit",
    "preview_prompt",
    "reassemble_preset_card",
    "router",
    "trial_prompt",
    "update_preset_card",
]
