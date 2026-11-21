"""酒馆人设卡的识别、V1 转换与字段规范化

卡片可能是 V1 的扁平对象、V2/V3 的外壳加 `data`，也可能来自 PNG 里的文本块。
这里只做纯函数：识别版本、把 V1 补成 V2 形状、逐字段检查类型与长度，并把不放
进字段的原文整份留在 `raw` 里供导出还原。

规范化时“能读就尽量读”与“读不了就记报告”是分开的：字段类型不对、超长、被截
断都进 `notes`，只有整张卡真的无法识别、JSON 过深、条目超限才拒绝。这样管理员
在导入页能看到“哪些字段没按原样进入 NA”，而不是只得到一个 400。
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from nekro_agent.services.worldbook import unsupported as wb_unsupported

from .card_png import PNG_SIGNATURE, PngCardInfo, card_text_from_png, read_png
from .errors import CardFileRejected
from .limits import (
    MAX_BOOK_ENTRIES,
    MAX_CARD_JSON_BYTES,
    MAX_CLEANUP_RULES,
    MAX_DATA_URI_IMAGE_BYTES,
    MAX_GREETING_CHARS,
    MAX_GREETINGS,
    MAX_JSON_DEPTH,
    MAX_TAGS,
    MAX_TEXT_FIELD_CHARS,
    MAX_UPLOAD_BYTES,
)

# 规范里的文字字段。缺省时一律补空串，读的人不用再判 `None`。
TEXT_FIELDS = (
    "name",
    "nickname",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "creator_notes",
    "system_prompt",
    "post_history_instructions",
    "creator",
    "character_version",
)
# 开场白两类共用同一套条数与单条字数上限。
_GREETING_FIELDS = ("alternate_greetings", "group_only_greetings")
# 变量宏与条件宏的名字；酒馆的写法可能带空格，大小写也不固定。
_VARIABLE_MACRO = re.compile(
    r"\{\{\s*(?:setvar|getvar|addvar|incvar|decvar|hasvar|deletevar|setglobalvar|getglobalvar|if|else|/if)\b",
    re.IGNORECASE,
)
# EJS 模板块、酒馆助手脚本与卡内书里不支持的功能：判据与说明在 `worldbook.unsupported`，
# 与世界书文件导入共用同一套。这里留几个名字给已有的调用方。
_count_ejs_blocks = wb_unsupported.count_ejs_blocks
MAX_LISTED_SCRIPTS = wb_unsupported.MAX_LISTED_SCRIPTS
MAX_LISTED_HOSTS = wb_unsupported.MAX_LISTED_HOSTS


@dataclass(frozen=True)
class CardNote:
    """导入报告里的一条说明

    `kind` 是机读的分类：`truncated` / `dropped` / `bad_value` / `unsupported` /
    `newer_spec` / `warning`。`detail` 是给管理员看的中文说明，不含卡片原文。
    """

    field: str
    kind: str
    detail: str


@dataclass
class ParsedCard:
    """一张解析后的卡片

    Attributes:
        spec: `v1` / `v2` / `v3`。
        raw: 原始卡片对象的深拷贝；V1 就是那个扁平对象。导出时以它为底还原。
        data: 规范化后的 V2 形状 `data`；V3 多出来的字段也保留在里面。
        notes: 规范化过程中的截断、丢弃与不支持项。
        source_format: `png` / `json`。
    """

    spec: str
    raw: dict[str, Any]
    data: dict[str, Any]
    notes: list[CardNote] = field(default_factory=list)
    source_format: str = "json"


def _strip_controls(text: str) -> str:
    """去掉 NUL 与除换行、制表、回车以外的 C0 控制字符"""
    return "".join(char for char in text if char in "\n\t\r" or ord(char) >= 0x20)


def _clamp_depth(raw: Any) -> tuple[int, CardNote | None]:
    """读深度并夹进 0 到 100；读不出来用缺省 4"""
    value = _as_int(raw)
    if value is None:
        return 4, CardNote("extensions.depth_prompt.depth", "bad_value", "深度不是整数，已按缺省 4 处理")
    if value < 0 or value > 100:
        return max(0, min(100, value)), CardNote(
            "extensions.depth_prompt.depth",
            "bad_value",
            "深度超出 0 到 100，已夹到区间内",
        )
    return value, None


def _as_int(value: Any) -> int | None:
    """只认整数与整数值的浮点；布尔不算数字"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_number(value: Any) -> Any:
    """V1 的深度字段在酒馆里常以字符串保存，这里按 Number() 的习惯转一次"""
    if isinstance(value, bool) or not isinstance(value, str):
        return value
    stripped = value.strip()
    try:
        return int(stripped)
    except ValueError:
        try:
            return float(stripped)
        except ValueError:
            return value


def _ensure_depth_within_limit(data: Any) -> None:
    """迭代遍历算 JSON 深度，超过上限就拒绝

    迭代而不是递归：恶意文件可以让递归版先栈溢出，异常发生在 `json.loads` 之外，
    表现为服务端错误而不是「文件不合法」。写法与世界书解析同源，但抛卡片自己的
    拒绝异常，消息也不带原文。
    """
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise CardFileRejected(f"JSON 嵌套超过 {MAX_JSON_DEPTH} 层")
        if isinstance(node, dict):
            for value in node.values():
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                stack.append((value, depth + 1))


def _normalize_text(result: dict[str, Any], name: str, notes: list[CardNote]) -> None:
    """把一个文字字段收成字符串：缺省空串，非字符串置空并报告，超长截断"""
    if name not in result:
        result[name] = ""
        return
    value = result[name]
    if not isinstance(value, str):
        result[name] = ""
        notes.append(CardNote(name, "bad_value", "不是字符串，已置为空串"))
        return
    cleaned = _strip_controls(value)
    if len(cleaned) > MAX_TEXT_FIELD_CHARS:
        cleaned = cleaned[:MAX_TEXT_FIELD_CHARS]
        notes.append(CardNote(name, "truncated", f"超过 {MAX_TEXT_FIELD_CHARS} 字，已截断"))
    result[name] = cleaned


def _normalize_string_array(
    result: dict[str, Any],
    name: str,
    notes: list[CardNote],
    *,
    limit: int | None = None,
    item_limit: int | None = None,
) -> None:
    """把一个字符串数组收成列表：非数组置空，非字符串元素丢掉，超限截断"""
    if name not in result:
        result[name] = []
        return
    value = result[name]
    if not isinstance(value, list):
        result[name] = []
        notes.append(CardNote(name, "bad_value", "不是数组，已置为空列表"))
        return

    items: list[str] = []
    dropped = 0
    truncated = False
    for item in value:
        if not isinstance(item, str):
            dropped += 1
            continue
        if item_limit is not None and len(item) > item_limit:
            item = item[:item_limit]
            truncated = True
        items.append(item)
    if limit is not None and len(items) > limit:
        items = items[:limit]
        truncated = True
    result[name] = items
    if dropped:
        notes.append(CardNote(name, "dropped", f"丢掉 {dropped} 个不是字符串的元素"))
    if truncated:
        notes.append(CardNote(name, "truncated", "超过条数或单条字数上限，已截断"))


def _normalize_depth_prompt(extensions: dict[str, Any], notes: list[CardNote]) -> None:
    """规范化 `extensions.depth_prompt`，保留里面其它未知键"""
    if "depth_prompt" not in extensions:
        return
    raw = extensions["depth_prompt"]
    if not isinstance(raw, dict):
        extensions["depth_prompt"] = {}
        notes.append(CardNote("extensions.depth_prompt", "bad_value", "不是对象，已置为空对象"))
        return

    prompt = raw.get("prompt", "")
    if not isinstance(prompt, str):
        prompt = ""
        notes.append(CardNote("extensions.depth_prompt.prompt", "bad_value", "不是字符串，已置为空串"))
    else:
        prompt = _strip_controls(prompt)
        if len(prompt) > MAX_TEXT_FIELD_CHARS:
            prompt = prompt[:MAX_TEXT_FIELD_CHARS]
            notes.append(
                CardNote("extensions.depth_prompt.prompt", "truncated", f"超过 {MAX_TEXT_FIELD_CHARS} 字，已截断"),
            )
    raw["prompt"] = prompt

    depth, depth_note = _clamp_depth(raw.get("depth", 4))
    raw["depth"] = depth
    if depth_note is not None:
        notes.append(depth_note)

    role = raw.get("role", "system")
    if not isinstance(role, str):
        role = "system"
        notes.append(CardNote("extensions.depth_prompt.role", "bad_value", "不是字符串，已按 system 处理"))
    raw["role"] = role[:16]


def _normalize_extensions(result: dict[str, Any], notes: list[CardNote]) -> None:
    """规范化 `extensions` 对象本身与其中会用到的那几项"""
    if "extensions" not in result:
        result["extensions"] = {}
        return
    raw = result["extensions"]
    if not isinstance(raw, dict):
        result["extensions"] = {}
        notes.append(CardNote("extensions", "bad_value", "不是对象，已置为空对象"))
        return

    _normalize_depth_prompt(raw, notes)

    if "regex_scripts" in raw:
        scripts = raw["regex_scripts"]
        if not isinstance(scripts, list):
            del raw["regex_scripts"]
            notes.append(CardNote("extensions.regex_scripts", "bad_value", "不是数组，已丢弃"))
        elif len(scripts) > MAX_CLEANUP_RULES:
            raw["regex_scripts"] = scripts[:MAX_CLEANUP_RULES]
            notes.append(
                CardNote(
                    "extensions.regex_scripts",
                    "truncated",
                    f"超过 {MAX_CLEANUP_RULES} 条，多出的已丢弃",
                ),
            )


def _normalize_character_book(result: dict[str, Any], notes: list[CardNote]) -> None:
    """规范化卡内知识书的形状；条目本身留给知识书转换那一步"""
    if "character_book" not in result:
        return
    book = result["character_book"]
    if not isinstance(book, dict) or not isinstance(book.get("entries"), list):
        del result["character_book"]
        notes.append(CardNote("character_book", "bad_value", "不是对象或 entries 不是数组，已丢弃"))
        return
    if len(book["entries"]) > MAX_BOOK_ENTRIES:
        raise CardFileRejected(f"卡内知识书超过 {MAX_BOOK_ENTRIES} 条")


def normalize_data(data: Mapping[str, Any]) -> tuple[dict[str, Any], list[CardNote]]:
    """把 V2 形状的 `data` 规范化成 NA 能直接用的字段

    返回 `(规范化后的对象, 报告)`。未知键原样保留，因为导出时要按原文还原；只有
    明确不支持的形状（如 `character_book` 不是对象）会被删掉并记报告。
    """
    result = copy.deepcopy(dict(data))
    notes: list[CardNote] = []

    for name in TEXT_FIELDS:
        _normalize_text(result, name, notes)

    _normalize_string_array(result, "alternate_greetings", notes, limit=MAX_GREETINGS, item_limit=MAX_GREETING_CHARS)
    _normalize_string_array(result, "group_only_greetings", notes, limit=MAX_GREETINGS, item_limit=MAX_GREETING_CHARS)
    _normalize_string_array(result, "tags", notes, limit=MAX_TAGS)
    _normalize_string_array(result, "source", notes)

    if "creator_notes_multilingual" in result:
        multilingual = result["creator_notes_multilingual"]
        if isinstance(multilingual, dict):
            result["creator_notes_multilingual"] = {
                key: value for key, value in multilingual.items() if isinstance(key, str) and isinstance(value, str)
            }
        else:
            result["creator_notes_multilingual"] = {}
            notes.append(CardNote("creator_notes_multilingual", "bad_value", "不是对象，已置为空对象"))

    _normalize_extensions(result, notes)
    _normalize_character_book(result, notes)
    return result, notes


def _v1_tags(value: Any) -> Any:
    """V1 的标签常是一段逗号分隔的字符串；照酒馆拆成数组，其它形态交给规范化"""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _convert_v1_to_v2(card: Mapping[str, Any]) -> dict[str, Any]:
    """照酒馆 `convertToV2` 把 V1 扁平对象补成 V2 的 `data` 形状"""
    data: dict[str, Any] = {}
    for key in ("name", "description", "personality", "scenario", "first_mes", "mes_example", "creator"):
        if key in card:
            data[key] = card[key]
    if "creatorcomment" in card:
        data["creator_notes"] = card["creatorcomment"]
    if "tags" in card:
        data["tags"] = _v1_tags(card["tags"])
    depth_keys = ("depth_prompt_prompt", "depth_prompt_depth", "depth_prompt_role")
    if any(key in card for key in depth_keys):
        data["extensions"] = {
            "depth_prompt": {
                "prompt": card.get("depth_prompt_prompt", ""),
                "depth": _as_number(card.get("depth_prompt_depth", 4)),
                "role": card.get("depth_prompt_role", "system"),
            },
        }
    return data


def _spec_version(card: Mapping[str, Any]) -> float | None:
    """读 `spec_version`；字符串与数字都试，读不出来当没有"""
    raw = card.get("spec_version")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _identify(card: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """识别版本并取出 V2 形状的 `data`；不认识时抛拒绝"""
    spec = card.get("spec")
    if spec == "chara_card_v3":
        data = card.get("data")
        if not isinstance(data, dict):
            raise CardFileRejected("卡片缺少 data 对象")
        return "v3", data
    if spec == "chara_card_v2":
        data = card.get("data")
        if not isinstance(data, dict):
            raise CardFileRejected("卡片缺少 data 对象")
        return "v2", data
    if "spec" not in card and "name" in card and ("description" in card or "first_mes" in card):
        return "v1", _convert_v1_to_v2(card)
    raise CardFileRejected("不是酒馆人设卡")


def _load_json(text: str) -> dict[str, Any]:
    """解析一段 JSON 文本：大小、语法、顶层形状与嵌套深度都在这里守"""
    if len(text.encode("utf-8")) > MAX_CARD_JSON_BYTES:
        raise CardFileRejected(f"卡片 JSON 超过 {MAX_CARD_JSON_BYTES // (1024 * 1024)} MiB")
    try:
        parsed = json.loads(text)
    except RecursionError as exc:
        # 解析器自己是递归的：极深的括号会在深度检查之前撑爆它。
        raise CardFileRejected(f"JSON 嵌套超过 {MAX_JSON_DEPTH} 层") from exc
    except ValueError as exc:
        raise CardFileRejected("不是合法的 JSON") from exc
    if not isinstance(parsed, dict):
        raise CardFileRejected("卡片 JSON 的顶层必须是一个对象")
    _ensure_depth_within_limit(parsed)
    return parsed


def parse_card_object(obj: Any, *, source_format: str) -> ParsedCard:
    """解析一个已经反序列化的卡片对象

    与 :func:`parse_card_file` 共用同一套识别、规范化、上限与深度检查：云端信封里的
    `card` 与上传的文件内容本质相同，不能因为少了「读文件」这一步就走另一条宽松的
    路径。`source_format` 只用于报告来源，上传是 `png` / `json`，云端是 `cloud`。
    拒绝时只给中文原因，不带卡片原文。
    """
    if not isinstance(obj, dict):
        raise CardFileRejected("卡片 JSON 的顶层必须是一个对象")
    _ensure_depth_within_limit(obj)
    card: dict[str, Any] = obj
    spec, raw_data = _identify(card)
    normalized, notes = normalize_data(raw_data)
    notes.extend(detect_unsupported(normalized))
    version = _spec_version(card)
    if version is not None and version > 3.0:
        notes.append(
            CardNote(
                "spec_version",
                "newer_spec",
                "卡片版本比 NA 认识的新，已按 V3 读取",
            ),
        )
    return ParsedCard(
        spec=spec,
        raw=copy.deepcopy(card),
        data=normalized,
        notes=notes,
        source_format=source_format,
    )


def parse_card_file(data: bytes) -> tuple[ParsedCard, PngCardInfo | None]:
    """解析上传的 PNG 或 JSON 卡片

    返回解析结果与 PNG 信息（JSON 卡为 `None`）。PNG 的宽高不在这里判定是否可做
    头像，调用方按 :data:`nekro_agent.services.tavern.limits.MAX_IMAGE_SIDE` 决定。
    所有拒绝都只给中文原因，不带文件原文。
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise CardFileRejected(
            f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB。可以在酒馆里把卡导出成 JSON 再导入",
        )
    info: PngCardInfo | None = None
    if data.startswith(PNG_SIGNATURE):
        info = read_png(data)
        text = card_text_from_png(info)
        source_format = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        raise CardFileRejected(
            "这是 JPG 图片，存不下人设卡数据；多半是网站缩略图或转存时被转码了，请找原始 PNG 或 JSON",
        )
    elif data.startswith((b"GIF87a", b"GIF89a")):
        raise CardFileRejected(
            "这是 GIF 图片，存不下人设卡数据；多半是网站缩略图或转存时被转码了，请找原始 PNG 或 JSON",
        )
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        raise CardFileRejected(
            "这是 WebP 图片，存不下人设卡数据；多半是网站缩略图或转存时被转码了，请找原始 PNG 或 JSON",
        )
    elif data.startswith(b"PK\x03\x04"):
        raise CardFileRejected("暂不支持 CHARX（ZIP）格式的卡")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CardFileRejected("文件不是 UTF-8 文本") from exc
        source_format = "json"

    card = _load_json(text)
    return parse_card_object(card, source_format=source_format), info


def _iter_text_values(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """列出所有要检查变量宏的文字：文字字段、开场白、知识书条目正文"""
    values: list[tuple[str, str]] = []
    for name in TEXT_FIELDS:
        value = data.get(name)
        if isinstance(value, str):
            values.append((name, value))
    for name in _GREETING_FIELDS:
        raw = data.get(name)
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if isinstance(item, str):
                    values.append((f"{name}[{index}]", item))
    book = data.get("character_book")
    if isinstance(book, dict):
        entries = book.get("entries")
        if isinstance(entries, list):
            for index, entry in enumerate(entries):
                if isinstance(entry, dict) and isinstance(entry.get("content"), str):
                    values.append((f"character_book.entries[{index}]", entry["content"]))
    return values


def detect_unsupported(data: Mapping[str, Any]) -> list[CardNote]:
    """找出这张卡里 NA 不支持的写法，每类最多一条带计数的说明

    只报告，不改变数据：变量宏与条件宏原样留在 `raw` 里，导出时不会丢；NA 不执行
    它们，依赖状态系统的卡效果会打折扣，管理员需要在导入页看到这一点。
    """
    notes: list[CardNote] = []

    macro_count = sum(len(_VARIABLE_MACRO.findall(value)) for _, value in _iter_text_values(data))
    if macro_count:
        notes.append(
            CardNote(
                "content",
                "unsupported",
                f"这张卡用了变量宏或条件宏，NA 不支持，依赖它的状态系统不会工作（共 {macro_count} 处）",
            ),
        )

    assets = data.get("assets")
    if isinstance(assets, list):
        other_assets = 0
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("type") != "icon" or asset.get("name") != "main":
                other_assets += 1
        if other_assets:
            notes.append(
                CardNote(
                    "assets",
                    "unsupported",
                    f"卡里还有 {other_assets} 个非主图标的资产，NA 不会使用",
                ),
            )

    extensions = data.get("extensions")
    if isinstance(extensions, dict):
        world = extensions.get("world")
        if isinstance(world, str) and world.strip() and world.strip() != _book_name(data):
            # 酒馆导出带卡内书的卡时，会把 `extensions.world` 写成卡内书自己的名字；那本书会随卡
            # 导入，不是「外部」的。只有名字对不上（或根本没有卡内书）才真是链接了别的书。
            notes.append(
                CardNote(
                    "extensions.world",
                    "unsupported",
                    "卡片链接了外部世界书，它不会随卡导入（共 1 处）",
                ),
            )

    # EJS 模板块、卡内书里不支持的功能（装饰器、包含组、概率……）、酒馆助手脚本：与世界书文件
    # 导入共用 `worldbook.unsupported.unsupported_items`，同一类东西两边说同一句话。
    book = data.get("character_book")
    entries = book.get("entries") if isinstance(book, dict) else None
    for item in wb_unsupported.unsupported_items(
        entries=entries if isinstance(entries, list) else (),
        extensions=extensions if isinstance(extensions, dict) else None,
        texts=[value for _, value in _iter_text_values(data)],
        card=True,
    ):
        notes.append(CardNote(item.field, "unsupported", item.detail))

    return notes


def _book_name(data: Mapping[str, Any]) -> str:
    """卡内书的名字（去掉首尾空白）；没有卡内书或没有名字时为空串"""
    book = data.get("character_book")
    name = book.get("name") if isinstance(book, dict) else None
    return name.strip() if isinstance(name, str) else ""


def pick_creator_notes(data: Mapping[str, Any]) -> str:
    """作者备注优先取多语言里的 `zh`，没有再取 `creator_notes`"""
    multilingual = data.get("creator_notes_multilingual")
    if isinstance(multilingual, dict):
        zh = multilingual.get("zh")
        if isinstance(zh, str):
            return zh
    creator_notes = data.get("creator_notes")
    return creator_notes if isinstance(creator_notes, str) else ""


def main_icon_data_uri(data: Mapping[str, Any]) -> str | None:
    """取 V3 的主图标数据 URI；外部地址、内嵌路径与默认图标都不取

    只接受 `data:image/` 且 base64 解码后不超过上限的那一个。这里只解码看大小，
    不解码成图片；宽高与格式由调用方在重新编码前再挡。
    """
    assets = data.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        if asset.get("type") != "icon" or asset.get("name") != "main":
            continue
        uri = asset.get("uri")
        if not isinstance(uri, str) or not uri.startswith("data:image/"):
            return None
        header, separator, payload = uri.partition(",")
        if not separator or ";base64" not in header.lower():
            return None
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return None
        if len(decoded) > MAX_DATA_URI_IMAGE_BYTES:
            return None
        return uri
    return None
