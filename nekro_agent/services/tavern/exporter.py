"""把人设导出成酒馆卡（JSON / PNG）

## 以原文为底、只覆盖改过的

`preset_card.card_raw` 是导入时的整张卡（去掉知识书与正则脚本）。导出时先把它按导入
规则规范化一遍，得到「如果没改过，NA 的列现在应该是什么」；每个映射字段逐个比较：
相同就保留原文的值，不同才写 NA 的现值。这样截断、夹值、丢掉的非字符串标签，只要
管理员没动过，导出就能逐字还原——与 L4 世界书往返用的是同一条规则。

## 正文的两半

`assembled_sha256` 是上次拼装结果的摘要：相等说明管理员没手改正文，五个来源字段照
`source_fields` 写回；不等说明正文被改过，这时 `description` 写人设正文，其余四段写空
（正文已经包含它们）。再导入一次时拼出来的正文与 NA 里的一致。

## 清理规则与知识书

清理规则以每条自己的 `original` 为底，只覆盖改过的 `findRegex` / `replaceString` /
`trimStrings` / `disabled`；知识书不读 `card_raw`（它本来就不存），而是从导出人看得到
的人设书重新生成。
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import unquote_to_bytes

from PIL import Image

from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.services.tavern import cleanup
from nekro_agent.services.tavern.assemble import content_sha256
from nekro_agent.services.tavern.card_book import build_character_book
from nekro_agent.services.tavern.card_format import _convert_v1_to_v2, normalize_data
from nekro_agent.services.tavern.card_png import write_png_card

SOURCE_FIELD_DATA_KEYS: tuple[tuple[str, str], ...] = (
    ("system", "system_prompt"),
    ("description", "description"),
    ("personality", "personality"),
    ("scenario", "scenario"),
    ("mes_example", "mes_example"),
)
_SOURCE_DATA_KEYS = tuple(data_key for _field, data_key in SOURCE_FIELD_DATA_KEYS)

# 顶层 V1 字段：老读取器只认它们。PNG 的 `chara` 里总是写，JSON 只在原文有时写。
_V1_TOP_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "name"),
    ("description", "description"),
    ("personality", "personality"),
    ("scenario", "scenario"),
    ("first_mes", "first_mes"),
    ("mes_example", "mes_example"),
    ("creatorcomment", "creator_notes"),
    ("tags", "tags"),
)

# 文件名里不能出现的字符 + 控制字符。Windows 的保留字符比这更多，但卡片名多来自
# Linux 上的酒馆，按一组够用的字符过滤即可。
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_MAX_FILENAME_CHARS = 64
_PLACEHOLDER_AVATAR_COLOR = (64, 64, 64)


def _original_data(card: DBPresetCard | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 `(原文对象, 原文里的 data)`；NA 原生人设两者都是空对象

    V1 的扁平原文没有 `data`，先用导入时同一套 `convertToV2` 转换；导出侧不再写一份，
    两份转换一旦分叉，往返就会在最不起眼的深度字段上出现差异。
    """
    if card is None or not isinstance(card.card_raw, dict) or not card.card_raw:
        return {}, {}
    raw = card.card_raw
    raw_data = raw.get("data")
    if isinstance(raw_data, dict):
        return raw, copy.deepcopy(raw_data)
    return raw, _convert_v1_to_v2(raw)


def _na_tags(preset: DBPreset) -> list[str]:
    """人设的标签列按逗号切成数组"""
    return [item.strip() for item in (preset.tags or "").split(",") if item.strip()]


def _extensions_of(data: dict[str, Any]) -> dict[str, Any]:
    """取 `data.extensions`，不是对象就换成空对象"""
    value = data.get("extensions")
    if not isinstance(value, dict):
        value = {}
        data["extensions"] = value
    return value


def _apply_field(result: dict[str, Any], base: Mapping[str, Any], key: str, current: Any) -> None:
    """NA 现值与原文映射值不同才写；相同就保留原文（可能根本没有这个键）"""
    if current != base.get(key, ""):
        result[key] = copy.deepcopy(current)


def _apply_source_fields(
    result: dict[str, Any],
    base: Mapping[str, Any],
    preset: DBPreset,
    card: DBPresetCard | None,
) -> None:
    """五个来源字段：正文没改过就按 `source_fields` 写回，改过就写正文、其余清空"""
    if card is None:
        return
    source_fields = card.source_fields if isinstance(card.source_fields, dict) else {}
    if card.assembled_sha256 and content_sha256(preset.content) == card.assembled_sha256:
        for field, data_key in SOURCE_FIELD_DATA_KEYS:
            current = source_fields.get(field, "")
            if not isinstance(current, str):
                current = ""
            _apply_field(result, base, data_key, current)
        return

    # 正文被手改过：它就是人设实际在用的那一份，其余四段再写回去会重复注入。
    result["description"] = preset.content
    for data_key in _SOURCE_DATA_KEYS:
        if data_key != "description":
            result[data_key] = ""


def _apply_creator_notes(
    result: dict[str, Any],
    base: Mapping[str, Any],
    preset: DBPreset,
) -> None:
    """作者备注优先写回多语言的 `zh` 那一项，否则写 `creator_notes`"""
    multilingual = base.get("creator_notes_multilingual")
    result_multilingual = result.get("creator_notes_multilingual")
    if (
        isinstance(multilingual, dict)
        and isinstance(multilingual.get("zh"), str)
        and isinstance(result_multilingual, dict)
    ):
        if preset.description != multilingual["zh"]:
            result_multilingual["zh"] = preset.description
        return
    _apply_field(result, base, "creator_notes", preset.description)


def _apply_greetings(
    result: dict[str, Any],
    base: Mapping[str, Any],
    card: DBPresetCard,
) -> None:
    """开场白与群聊专用开场白按列写回；没改过时保留原文的缺键形状"""
    greetings = card.greetings if isinstance(card.greetings, list) else []
    first = greetings[0] if greetings else ""
    _apply_field(result, base, "first_mes", first)
    _apply_field(result, base, "alternate_greetings", list(greetings[1:]))
    group_greetings = card.group_greetings if isinstance(card.group_greetings, list) else []
    _apply_field(result, base, "group_only_greetings", list(group_greetings))


def _apply_depth_prompt(
    result: dict[str, Any],
    base: Mapping[str, Any],
    card: DBPresetCard,
) -> None:
    """深度注入三列；原文没有这个键且 NA 里都是默认值时不写"""
    base_extensions = base.get("extensions")
    base_depth = base_extensions.get("depth_prompt") if isinstance(base_extensions, dict) else None
    current = {
        "prompt": card.depth_prompt or "",
        "depth": card.depth_prompt_depth,
        "role": card.depth_prompt_role or "system",
    }
    if isinstance(base_depth, dict):
        if all(base_depth.get(key) == value for key, value in current.items()):
            return
        extensions = _extensions_of(result)
        depth = copy.deepcopy(base_depth)
        depth.update(current)
        extensions["depth_prompt"] = depth
        return
    if not current["prompt"] and current["depth"] == 4 and current["role"] == "system":
        return
    _extensions_of(result)["depth_prompt"] = current


def _new_regex_script(rule: Mapping[str, Any]) -> dict[str, Any]:
    """NA 里新加的规则按酒馆脚本的默认值生成"""
    return {
        "id": str(uuid.uuid4()),
        "scriptName": rule.get("name") or "",
        "findRegex": rule.get("find") or "",
        "replaceString": rule.get("replace") or "",
        "trimStrings": list(rule.get("trim") or []),
        "placement": [2],
        "disabled": not bool(rule.get("enabled")),
        "markdownOnly": False,
        "promptOnly": False,
        "runOnEdit": True,
        "substituteRegex": 0,
        "minDepth": None,
        "maxDepth": None,
    }


def _regex_script_from_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    """一条清理规则写回酒馆脚本：以 `original` 为底，只覆盖改过的四个字段"""
    original = rule.get("original")
    if not isinstance(original, dict):
        return _new_regex_script(rule)

    script = copy.deepcopy(original)
    find = rule.get("find")
    if isinstance(find, str) and find != original.get("findRegex"):
        script["findRegex"] = find
    replace = rule.get("replace")
    if isinstance(replace, str) and replace != original.get("replaceString"):
        script["replaceString"] = replace
    trim = rule.get("trim")
    trim = [item for item in trim if isinstance(item, str)] if isinstance(trim, list) else []
    if trim != (original.get("trimStrings") or []):
        script["trimStrings"] = trim
    # 「按原文判出的默认启用」而不是原文的 `disabled`：导入时因安全检查被停用的规则，
    # 只要管理员没有明确改过启用状态，导出就保持停用，不把它的停用状态当成一次编辑。
    if _is_html_display_rule(original):
        # 输出 HTML 的规则：NA 里的「停用 / 只留正文 / 整段删掉」只决定 QQ 上怎么发，酒馆里这条规则照样
        # 渲染网页。不管管理员在 NA 里怎么选、卡里原本是开是关，导出都保留原文的启用状态（早先导入时这类
        # 规则存成停用，也不能因此在导出时把它关掉）。重新导入时 NA 里的选择回到默认，扩展页写明了这一点。
        return script
    default = cleanup.rule_from_st_script(original)
    enabled = bool(rule.get("enabled"))
    if enabled != default["enabled"]:
        script["disabled"] = not enabled
    return script


def _is_html_display_rule(original: Mapping[str, Any]) -> bool:
    """酒馆原文的替换文字带 HTML：这条规则在 NA 里按「输出 HTML 的规则」处理"""
    replace = original.get("replaceString")
    return isinstance(replace, str) and cleanup.has_html_output(replace)


def _apply_regex_scripts(result: dict[str, Any], card: DBPresetCard) -> None:
    """清理规则写回 `extensions.regex_scripts`；一条都没有时不写这个键"""
    rules = card.cleanup_rules if isinstance(card.cleanup_rules, list) else []
    scripts = [_regex_script_from_rule(rule) for rule in rules if isinstance(rule, dict)]
    if not scripts:
        return
    _extensions_of(result)["regex_scripts"] = scripts


def _apply_character_book(
    result: dict[str, Any],
    books: Sequence[tuple[DBWbBook, list[DBWbEntry]]],
) -> None:
    """知识书从可见的人设书重新生成；一本都没有时删掉这个键而不是写 `null`"""
    book = build_character_book(books)
    if book is None:
        result.pop("character_book", None)
    else:
        result["character_book"] = book


def _native_data(preset: DBPreset, books: Sequence[tuple[DBWbBook, list[DBWbEntry]]]) -> dict[str, Any]:
    """NA 原生人设的 V2 骨架"""
    data: dict[str, Any] = {
        "name": preset.name,
        "description": preset.content,
        "personality": "",
        "scenario": "",
        "first_mes": "",
        "mes_example": "",
        "creator_notes": preset.description,
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [],
        "tags": _na_tags(preset),
        "creator": preset.author,
        "character_version": "",
        "extensions": {},
    }
    book = build_character_book(books)
    if book is not None:
        data["character_book"] = book
    return data


def _assemble_v2(raw: Mapping[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """拼出 V2 对象；原文顶层其它键与已有的 V1 字段照原样保留/写回"""
    result = copy.deepcopy(dict(raw)) if raw else {}
    result["spec"] = "chara_card_v2"
    result["spec_version"] = "2.0"
    result["data"] = data
    for top_key, data_key in _V1_TOP_FIELDS:
        if top_key in raw:
            result[top_key] = copy.deepcopy(data.get(data_key, ""))
    return result


def _assemble_v3(raw: Mapping[str, Any], data: dict[str, Any], now: datetime) -> dict[str, Any]:
    """拼出 V3 对象并补齐 V3 必填字段"""
    # V1 的扁平原文不是 V3 的顶层形状，只有原本就是 V2/V3 的对象才保留顶层其它键。
    result = copy.deepcopy(dict(raw)) if raw and isinstance(raw.get("data"), dict) else {}
    result["spec"] = "chara_card_v3"
    result["spec_version"] = "3.0"
    result["data"] = data

    data.setdefault("group_only_greetings", [])
    book = data.get("character_book")
    if isinstance(book, dict):
        entries = book.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    entry.setdefault("use_regex", False)
    creation_date = data.get("creation_date")
    data["creation_date"] = creation_date if isinstance(creation_date, int) and not isinstance(creation_date, bool) else 0
    data["modification_date"] = int(now.timestamp())
    return result


def build_card_object(
    preset: DBPreset,
    card: DBPresetCard | None,
    books: Sequence[tuple[DBWbBook, list[DBWbEntry]]],
    *,
    spec: Literal["v2", "v3"],
    now: datetime,
) -> dict[str, Any]:
    """按 `spec` 生成一张卡；以原文为底，只覆盖改过的字段"""
    raw, raw_data = _original_data(card)
    if not raw_data:
        data = _native_data(preset, books)
        if card is not None:
            # 有扩展行但没有原文（NA 原生人设第一次保存扩展）：骨架之上再套用列值，
            # 否则手动填的开场白、后置指令与清理规则会凭空消失。
            _apply_field(data, {}, "nickname", card.nickname or "")
            _apply_source_fields(data, {}, preset, card)
            _apply_greetings(data, {}, card)
            _apply_field(data, {}, "post_history_instructions", card.post_history or "")
            _apply_depth_prompt(data, {}, card)
            _apply_regex_scripts(data, card)
        if spec == "v3":
            return _assemble_v3(raw, data, now)
        return _assemble_v2(raw, data)

    base_data, _notes = normalize_data(raw_data)
    result = copy.deepcopy(raw_data)

    _apply_field(result, base_data, "name", preset.name)
    if card is not None:
        if spec == "v3" or "nickname" in raw_data:
            _apply_field(result, base_data, "nickname", card.nickname or "")
        _apply_creator_notes(result, base_data, preset)
        _apply_field(result, base_data, "tags", _na_tags(preset))
        _apply_field(result, base_data, "creator", preset.author)
        _apply_field(result, base_data, "character_version", card.character_version or "")
        _apply_source_fields(result, base_data, preset, card)
        _apply_greetings(result, base_data, card)
        _apply_field(result, base_data, "post_history_instructions", card.post_history or "")
        _apply_depth_prompt(result, base_data, card)
        _apply_regex_scripts(result, card)
    else:
        # 没有扩展行的原文卡只可能来自旧数据：列上的值当作没改过，不覆盖原文。
        _apply_field(result, base_data, "creator_notes", preset.description)

    _apply_character_book(result, books)

    if spec == "v3":
        return _assemble_v3(raw, result, now)
    return _assemble_v2(raw, result)


def safe_filename(name: str, suffix: str) -> str:
    """人设名清洗成文件名：去掉路径保留字符与控制字符，截到 64 字"""
    cleaned = _UNSAFE_FILENAME.sub("", name or "").strip()
    cleaned = cleaned[:_MAX_FILENAME_CHARS] or "character"
    return cleaned + suffix


def export_json(
    preset: DBPreset,
    card: DBPresetCard | None,
    books: Sequence[tuple[DBWbBook, list[DBWbEntry]]],
    *,
    now: datetime | None = None,
) -> tuple[bytes, str]:
    """导出 JSON：原卡是 V3 导 V3，其余导 V2"""
    moment = now or datetime.now(timezone.utc)
    spec = "v3" if card is not None and card.card_spec == "v3" else "v2"
    obj = build_card_object(preset, card, books, spec=spec, now=moment)
    payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    return payload, safe_filename(preset.name, ".json")


def _png_from_avatar(avatar: str) -> bytes:
    """把头像 data URL 转成 PNG；为空或解不开时生成一张 256×256 的纯色图"""
    if isinstance(avatar, str) and avatar.startswith("data:"):
        header, separator, payload = avatar.partition(",")
        if separator:
            try:
                if ";base64" in header.lower():
                    binary = base64.b64decode(payload, validate=True)
                else:
                    binary = unquote_to_bytes(payload)
                with Image.open(BytesIO(binary)) as image:
                    output = BytesIO()
                    image.save(output, format="PNG")
                    return output.getvalue()
            except (binascii.Error, OSError, ValueError, Image.DecompressionBombError):
                pass

    output = BytesIO()
    Image.new("RGB", (256, 256), _PLACEHOLDER_AVATAR_COLOR).save(output, format="PNG")
    return output.getvalue()


def _with_v1_top_level(obj: dict[str, Any]) -> dict[str, Any]:
    """PNG 的 `chara` 总是带一份顶层 V1 字段，老读取器只认它们"""
    data = obj.get("data")
    data = data if isinstance(data, dict) else {}
    for top_key, data_key in _V1_TOP_FIELDS:
        obj[top_key] = copy.deepcopy(data.get(data_key, ""))
    return obj


def export_png(
    preset: DBPreset,
    card: DBPresetCard | None,
    books: Sequence[tuple[DBWbBook, list[DBWbEntry]]],
    *,
    now: datetime | None = None,
) -> tuple[bytes, str]:
    """导出 PNG：`chara` 是 V2 对象，`ccv3` 是补齐后的 V3 对象"""
    moment = now or datetime.now(timezone.utc)
    v2 = _with_v1_top_level(build_card_object(preset, card, books, spec="v2", now=moment))
    v3 = build_card_object(preset, card, books, spec="v3", now=moment)
    png = _png_from_avatar(preset.avatar)
    payload = write_png_card(
        png,
        json.dumps(v2, ensure_ascii=False),
        json.dumps(v3, ensure_ascii=False),
    )
    return payload, safe_filename(preset.name, ".png")


__all__ = [
    "build_card_object",
    "export_json",
    "export_png",
    "safe_filename",
]
