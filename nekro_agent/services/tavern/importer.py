"""酒馆人设卡的检查与导入

检查接口只解析、不落库；导入接口在一个事务里建人设、扩展行与人设书。两件事共用同一
套解析结果，因此「检查页看到的」与「导入后得到的」不会因为走了两条代码路径而分叉。

## 头像为什么在事务之前处理

头像要经过 Pillow 重新编码，可能失败、可能很慢；把它放在事务里会让一张坏图把数据库
事务拖到超时，而它本身又完全不依赖任何一行新数据。因此先算出 data URL，再开事务。
JSON 卡的数据 URI 只读宽高（`Image.open(...).size`），不调 `load()`、不改
`Image.MAX_IMAGE_PIXELS`：后者是进程级设置，会影响视觉、头像上传等别处的图片处理。

## 信任级别

卡内知识书条目一律 `trusted = false`，即便导入人是平台方。卡片来自网上，导入动作只
表示「我把它放进了库」，不表示「这些文字可以作为宿主内容注入」。
"""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Mapping

from PIL import Image
from tortoise.transactions import in_transaction

from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.schemas.tavern import (
    CardBookInspect,
    CardImportOptions,
    CardImportResult,
    CardInspectReport,
    CardNoteItem,
    CleanupRuleInspect,
    DepthPromptInspect,
    TargetGates,
)
from nekro_agent.services.tavern import card_book as card_book_service
from nekro_agent.services.tavern import cleanup
from nekro_agent.services.tavern.assemble import assemble_content, content_sha256
from nekro_agent.services.tavern.card_format import (
    CardNote,
    ParsedCard,
    detect_unsupported,
    main_icon_data_uri,
    parse_card_file,
    pick_creator_notes,
)
from nekro_agent.services.tavern.card_png import PngCardInfo
from nekro_agent.services.tavern.limits import LONG_CONTENT_WARN_CHARS, MAX_IMAGE_SIDE
from nekro_agent.services.worldbook import st_format
from nekro_agent.tools.image_utils import process_image_data_url

# 卡片字段 ↔ 人设正文拼装占位。导入与导出都按这张表，顺序无关，但不能漏。
SOURCE_FIELD_KEYS: tuple[str, ...] = ("system", "description", "personality", "scenario", "mes_example")
_SOURCE_FIELD_TO_DATA = {
    "system": "system_prompt",
    "description": "description",
    "personality": "personality",
    "scenario": "scenario",
    "mes_example": "mes_example",
}

# 前端新建人设时头像初值就是空串；这里复用它，不在后端另造一张默认图。
DEFAULT_PRESET_AVATAR = ""

# 检查页「超预算」说明里最多点名几条、标题显示多少字。
_OVER_BUDGET_LISTED = 10
_OVER_BUDGET_TITLE_CHARS = 20

# 标签列与作者列的上限，取自 `DBPreset` 的字段宽度。
MAX_TAGS_CHARS = 512
MAX_AUTHOR_CHARS = 128
MAX_NAME_CHARS = 256
MAX_TITLE_CHARS = 128


@dataclass(frozen=True)
class CardExtras:
    """信封里 `na` 的三项：卡片规范没有、但 NA 的扩展行需要的值

    云端下载与同步都按信封原样写回这三列；普通导入不传，取扩展行的默认值。
    """

    prefill: str = ""
    macros_enabled: bool = True
    greeting_pick: str = "first"


@dataclass(frozen=True)
class PresetFields:
    """云端下载时要原样使用的 `presets` 列

    信封里只有卡片与正文，名字、标题、描述、标签、作者、云端编号与共享状态仍以云端
    返回的详情为准；`ext_data` 写回共享前的原值，不把信封本身存进本地。
    """

    name: str
    title: str
    description: str
    tags: str
    author: str
    remote_id: str | None = None
    on_shared: bool = False
    ext_data: Any = None


def source_fields_of(data: Mapping[str, Any]) -> dict[str, str]:
    """从规范化后的卡片 `data` 取出五个来源字段"""
    return {key: str(data.get(data_key) or "") for key, data_key in _SOURCE_FIELD_TO_DATA.items()}


def rules_of(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把卡片里的正则脚本转成清理规则；非对象条目直接跳过"""
    extensions = data.get("extensions")
    scripts = extensions.get("regex_scripts") if isinstance(extensions, dict) else None
    if not isinstance(scripts, list):
        return []
    rules: list[dict[str, Any]] = []
    for script in scripts:
        if isinstance(script, dict):
            rules.append(dict(cleanup.rule_from_st_script(script)))
    return rules


def _count_decorated(book: Mapping[str, Any]) -> int:
    """内容以 `@@` 开头的条目数；NA 不解析装饰器，只报告"""
    entries = book.get("entries")
    if not isinstance(entries, list):
        return 0
    return sum(
        1
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("content"), str) and entry["content"].startswith("@@")
    )


def _avatar_source_for_png(info: PngCardInfo) -> tuple[str, str]:
    """PNG 卡的头像来源；宽高超过上限时不拿它当头像"""
    if info.width > MAX_IMAGE_SIDE or info.height > MAX_IMAGE_SIDE:
        return "default", f"图片宽高 {info.width}×{info.height} 超过 {MAX_IMAGE_SIDE} 像素，改用默认头像"
    return "png", "使用 PNG 图片本身作为头像"


def _avatar_source_for_json(data: Mapping[str, Any]) -> tuple[str, str]:
    """JSON 卡的头像来源；只认 V3 主图标的 data URI"""
    uri = main_icon_data_uri(data)
    if uri is None:
        return "default", "卡片没有可用的内嵌主图标，改用默认头像"
    try:
        header, _, payload = uri.partition(",")
        if ";base64" not in header.lower():
            return "default", "主图标不是 base64 数据，改用默认头像"
        decoded = base64.b64decode(payload, validate=True)
        # 只读文件头里的宽高，不解码像素，也不改 Pillow 的全局上限。
        with Image.open(BytesIO(decoded)) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001 - 解码失败就是没有头像，原因不把底层文本带出去
        return "default", "主图标无法解码，改用默认头像"
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        return "default", f"图片宽高 {width}×{height} 超过 {MAX_IMAGE_SIDE} 像素，改用默认头像"
    return "data_uri", "使用卡片主图标作为头像"


def _avatar_source(parsed: ParsedCard, info: PngCardInfo | None) -> tuple[str, str]:
    """检查接口用的头像来源判定：只读宽高，不重新编码"""
    if info is not None:
        return _avatar_source_for_png(info)
    return _avatar_source_for_json(parsed.data)


async def _process_avatar(parsed: ParsedCard, info: PngCardInfo | None, data: bytes) -> tuple[str, str, str]:
    """导入前算出头像 data URL，返回 `(data URL, 来源, 说明)`

    任何失败或超限都用默认头像：头像只是展示，不该让一张卡导不进来。
    """
    if info is not None:
        source, note = _avatar_source_for_png(info)
        if source == "default":
            return DEFAULT_PRESET_AVATAR, source, note
        data_url = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    else:
        source, note = _avatar_source_for_json(parsed.data)
        data_url = main_icon_data_uri(parsed.data) or ""
    if source == "default" or not data_url:
        return DEFAULT_PRESET_AVATAR, "default", note
    try:
        processed = await process_image_data_url(data_url)
    except Exception:  # noqa: BLE001 - 重新编码失败就退回默认头像
        return DEFAULT_PRESET_AVATAR, "default", "头像重新编码失败，改用默认头像"
    if not processed or processed == data_url:
        # 底层函数处理失败时会原样返回输入；原样返回不等于「重新编码成功」，卡片图里的
        # tEXt 块会跟着头像进库。
        return DEFAULT_PRESET_AVATAR, "default", "头像重新编码失败，改用默认头像"
    return processed, source, note


def _over_budget_note(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    notes: list[st_format.ImportNote],
    budget_chars: int,
) -> CardNote | None:
    """把 `budget_notes` 判出的超预算条目汇成检查页上的一条说明，点名最长的几条

    判据只在 `st_format.budget_notes` 一处；这里只是按条目编号回查标题与字数，写成人能读的一句。
    """
    over_keys = {note.entry_key for note in notes if note.kind == "over_budget"}
    if not over_keys:
        return None
    over = [columns for columns, _ext in rows if str(columns.get("uid", "")) in over_keys]
    over.sort(key=lambda columns: len(str(columns.get("content") or "")), reverse=True)
    listed = "、".join(
        f"「{str(columns.get('comment') or '').strip()[:_OVER_BUDGET_TITLE_CHARS] or '未命名'}」"
        f"{len(str(columns.get('content') or ''))} 字"
        for columns in over[:_OVER_BUDGET_LISTED]
    )
    more = f" 等 {len(over)} 条" if len(over) > _OVER_BUDGET_LISTED else ""
    return CardNote(
        "character_book",
        "over_budget",
        f"卡内知识书有 {len(over)} 条正文比这个实例每轮注入上限 WORLDBOOK_MAX_CHARS（{budget_chars} 字）还长，"
        f"按现在的设置注入不进去：{listed}{more}",
    )


async def inspect_card(
    data: bytes,
    *,
    template: str,
    target: TargetGates,
    budget_chars: int = 0,
) -> CardInspectReport:
    """解析一张卡并给出导入前的检查报告；不写任何表

    `budget_chars` 是导入目标这一层的 `WORLDBOOK_MAX_CHARS`（调用方按实例或全局取）；> 0 时，
    卡内书里正文比它长的条目记进知识书说明的 `over_budget`，并在说明里点名。
    """
    parsed, info = parse_card_file(data)
    card_data = parsed.data
    source_fields = source_fields_of(card_data)
    assembled = assemble_content(template, source_fields)

    book = card_data.get("character_book")
    book_present = isinstance(book, dict)
    book_entries = book.get("entries") if book_present else None
    book_notes: dict[str, int] = st_format.summarize_notes([])
    invalid_keys = 0
    over_budget: CardNote | None = None
    if book_present:
        _book_ext, rows, book_import_notes = card_book_service.build_book_rows(book, budget_chars=budget_chars)
        book_notes = st_format.summarize_notes(book_import_notes)
        invalid_keys = sum(1 for note in book_import_notes if note.kind == "invalid_key")
        over_budget = _over_budget_note(rows, book_import_notes, budget_chars)

    extensions = card_data.get("extensions")
    depth_raw = extensions.get("depth_prompt") if isinstance(extensions, dict) else None
    depth = depth_raw if isinstance(depth_raw, dict) else {}
    avatar_source, avatar_note = _avatar_source(parsed, info)
    unsupported = detect_unsupported(card_data)
    notes = [note for note in parsed.notes if note.kind != "unsupported"]
    if over_budget is not None:
        notes.append(over_budget)

    return CardInspectReport(
        spec=parsed.spec,
        source_format=parsed.source_format,
        name=str(card_data.get("name") or ""),
        nickname=str(card_data.get("nickname") or ""),
        creator=str(card_data.get("creator") or ""),
        character_version=str(card_data.get("character_version") or ""),
        tags=list(card_data.get("tags") or []),
        creator_notes=pick_creator_notes(card_data),
        assembled_content=assembled,
        assembled_chars=len(assembled),
        long_content=len(assembled) > LONG_CONTENT_WARN_CHARS,
        system_prompt=str(card_data.get("system_prompt") or ""),
        post_history_instructions=str(card_data.get("post_history_instructions") or ""),
        depth_prompt=DepthPromptInspect(
            text=str(depth.get("prompt") or ""),
            depth=int(depth.get("depth", 4)),
        ),
        greetings_count=(1 if str(card_data.get("first_mes") or "") else 0)
        + len(card_data.get("alternate_greetings") or []),
        group_greetings_count=len(card_data.get("group_only_greetings") or []),
        book=CardBookInspect(
            present=book_present,
            entries=len(book_entries) if isinstance(book_entries, list) else 0,
            invalid_keys=invalid_keys,
            decorated=_count_decorated(book) if book_present else 0,
            notes=book_notes,
        ),
        cleanup_rules=[
            CleanupRuleInspect(
                index=index,
                name=rule.get("name") or "",
                find=rule.get("find") or "",
                replace=rule.get("replace") or "",
                enabled=bool(rule.get("enabled")),
                disabled_reason=rule.get("disabled_reason") or "",
            )
            for index, rule in enumerate(rules_of(card_data))
        ],
        avatar_source=avatar_source,
        avatar_note=avatar_note,
        unsupported=[CardNoteItem(field=note.field, kind=note.kind, detail=note.detail) for note in unsupported],
        notes=[CardNoteItem(field=note.field, kind=note.kind, detail=note.detail) for note in notes],
        target=target,
    )


def _tags_text(raw: Any) -> str:
    """标签列表按逗号连接；标签里的逗号换成顿号，免得一个标签被拆成两个"""
    if not isinstance(raw, list):
        return ""
    tags = [str(item).replace(",", "、").strip() for item in raw if str(item).strip()]
    return ",".join(tags)[:MAX_TAGS_CHARS]


def _apply_rule_options(
    rules: list[dict[str, Any]],
    options: CardImportOptions,
) -> tuple[list[dict[str, Any]], list[str]]:
    """按导入选项调整每条规则的启用状态

    只能关掉默认启用的，或打开 `html_output` / `disabled_in_card` 这两类；安全与
    语义不符的停用原因不能靠一个复选框绕过，忽略请求并在结果里说明。
    """
    notes: list[str] = []
    for index, rule in enumerate(rules):
        requested = options.rule_enabled.get(index)
        if requested is None:
            continue
        if not requested:
            if rule.get("enabled"):
                rule["enabled"] = False
                rule["disabled_reason"] = cleanup.MANUAL
            continue
        if rule.get("enabled"):
            continue
        reason = str(rule.get("disabled_reason") or "")
        if reason in (cleanup.HTML_OUTPUT, cleanup.DISABLED_IN_CARD):
            # 打开前过一遍与扩展页手动保存同一套校验：`disabled_in_card` 的规则导入时
            # 不解析正则（判到「卡里关着」就停了），`html_output` 的替换文字常超过手动
            # 保存的上限。在这里放行的话，规则表里就有一条启用着却过不了校验的规则，
            # 之后管理员改表里任何一条都会因为它 400。
            invalid = cleanup.validate_rule(rule)
            if invalid is not None:
                name = str(rule.get("name") or f"第 {index + 1} 条")
                notes.append(f"规则「{name}」没有打开：{invalid}")
                continue
            rule["enabled"] = True
            rule["disabled_reason"] = ""
        else:
            name = str(rule.get("name") or f"第 {index + 1} 条")
            notes.append(f"规则「{name}」没有打开：停用原因 {reason or '未知'} 不能由导入选项覆盖")
    return rules, notes


def _card_raw(parsed: ParsedCard) -> dict[str, Any]:
    """导入时保存的卡片原文：去掉知识书与正则脚本，V1 就是原扁平对象"""
    raw = copy.deepcopy(parsed.raw)
    if parsed.spec == "v1":
        return raw
    data = raw.get("data")
    if isinstance(data, dict):
        data.pop("character_book", None)
        extensions = data.get("extensions")
        if isinstance(extensions, dict):
            extensions.pop("regex_scripts", None)
    return raw


def _book_name(card_data: Mapping[str, Any], preset_name: str) -> str:
    """卡内书的名字；没有就用「{人设名} 的知识书」"""
    book = card_data.get("character_book")
    if isinstance(book, dict):
        name = book.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return f"{preset_name} 的知识书"


def prepare_card_columns(
    parsed: ParsedCard,
    options: CardImportOptions,
    *,
    template: str,
    extra: CardExtras | None = None,
) -> tuple[dict[str, Any], str, list[str]]:
    """从解析结果算出扩展行的列、拼装正文与导入选项说明

    普通导入、云端下载与云端同步都要把同一张卡落成同样的列；这里只算不写库，调用方
    决定是新建一行还是覆盖已有行。`assembled` 始终按当前模板与来源字段计算，即使
    调用方随后用信封里的完整正文覆盖人设正文，摘要也仍用来判断正文有没有被手改。
    """
    values = extra or CardExtras()
    card_data = parsed.data
    source_fields = source_fields_of(card_data)
    if not options.include_system_prompt:
        source_fields["system"] = ""
    assembled = assemble_content(template, source_fields)
    rules, rule_notes = _apply_rule_options(rules_of(card_data), options)

    extensions = card_data.get("extensions")
    depth = extensions.get("depth_prompt") if isinstance(extensions, dict) else None
    depth = depth if isinstance(depth, dict) else {}
    columns: dict[str, Any] = {
        "card_spec": parsed.spec,
        "card_raw": _card_raw(parsed),
        "nickname": str(card_data.get("nickname") or "")[:128],
        "source_fields": source_fields,
        "assembled_sha256": content_sha256(assembled),
        "macros_enabled": values.macros_enabled,
        "greetings": ([card_data["first_mes"]] if str(card_data.get("first_mes") or "") else [])
        + list(card_data.get("alternate_greetings") or []),
        "group_greetings": list(card_data.get("group_only_greetings") or []),
        "greeting_pick": values.greeting_pick,
        "post_history": str(card_data.get("post_history_instructions") or "") if options.include_post_history else "",
        "prefill": values.prefill,
        "depth_prompt": str(depth.get("prompt") or "") if options.include_depth_prompt else "",
        "depth_prompt_depth": int(depth.get("depth", 4)),
        "depth_prompt_role": str(depth.get("role") or "system")[:16],
        "cleanup_rules": rules,
        "character_version": str(card_data.get("character_version") or "")[:64],
    }
    return columns, assembled, rule_notes


async def import_parsed_card(
    parsed: ParsedCard,
    options: CardImportOptions,
    *,
    owner_instance_key: str,
    book_owner: tuple[str, str],
    username: str,
    template: str,
    avatar: str,
    content_override: str | None = None,
    extra: CardExtras | None = None,
    preset_fields: PresetFields | None = None,
) -> CardImportResult:
    """把一张已解析的卡落库：建人设、扩展行与人设书，整笔一个事务

    头像由调用方在事务之前算好传进来（普通导入要重新编码，云端下载按现有规则原样
    使用云端头像）。`content_override` 不为空时人设正文直接用它，来源字段与摘要仍按
    当前模板计算；`preset_fields` 给定时名字、标题、描述、标签、作者、云端编号与
    共享状态用云端值，而不是卡片里的同名值。
    """
    columns, assembled, notes = prepare_card_columns(parsed, options, template=template, extra=extra)
    card_data = parsed.data

    if preset_fields is None:
        name = (str(card_data.get("name") or "").strip() or "导入的人设")[:MAX_NAME_CHARS]
        title = name[:MAX_TITLE_CHARS]
        description = pick_creator_notes(card_data)
        tags = _tags_text(card_data.get("tags"))
        author = (str(card_data.get("creator") or "").strip() or username)[:MAX_AUTHOR_CHARS]
        remote_id: str | None = None
        on_shared = False
        ext_data: Any = None
    else:
        name = preset_fields.name
        title = preset_fields.title
        description = preset_fields.description
        tags = preset_fields.tags
        author = preset_fields.author
        remote_id = preset_fields.remote_id
        on_shared = preset_fields.on_shared
        ext_data = preset_fields.ext_data

    content = content_override if content_override is not None else assembled
    book = card_data.get("character_book")

    async with in_transaction():
        preset = await DBPreset.create(
            name=name,
            title=title,
            avatar=avatar,
            content=content,
            description=description,
            tags=tags,
            author=author,
            on_shared=on_shared,
            owner_instance_key=owner_instance_key,
            remote_id=remote_id,
            ext_data=ext_data,
        )

        await DBPresetCard.create(
            preset=preset,
            imported_by=username,
            imported_at=datetime.now(timezone.utc),
            **columns,
        )

        book_id: int | None = None
        book_report = None
        if options.include_book and isinstance(book, dict) and isinstance(book.get("entries"), list):
            created_book, book_report = await card_book_service.create_card_book(
                book_owner,
                preset.id,
                _book_name(card_data, name),
                book,
                username=username,
            )
            book_id = created_book.id

    return CardImportResult(preset_id=preset.id, book_id=book_id, book_report=book_report, notes=notes)


async def import_card(
    data: bytes,
    options: CardImportOptions,
    *,
    owner_instance_key: str,
    book_owner: tuple[str, str],
    username: str,
    template: str,
) -> CardImportResult:
    """导入一张卡：处理头像、解析，然后把解析结果交给落库那一段

    头像在事务之前处理；事务里任何一步失败都整体回滚，不会留下半个人设。导入只建这些
    行，不绑频道、不改实例默认人设、不开任何闸。
    """
    parsed, info = parse_card_file(data)
    avatar, _avatar_source_kind, avatar_note = await _process_avatar(parsed, info, data)

    result = await import_parsed_card(
        parsed,
        options,
        owner_instance_key=owner_instance_key,
        book_owner=book_owner,
        username=username,
        template=template,
        avatar=avatar,
    )
    if avatar_note and avatar == DEFAULT_PRESET_AVATAR:
        # 头像说明排在导入选项说明之前：管理员先要知道「头像为什么不是卡里的那张」。
        result.notes.insert(0, avatar_note)
    return result


__all__ = [
    "DEFAULT_PRESET_AVATAR",
    "SOURCE_FIELD_KEYS",
    "CardExtras",
    "PresetFields",
    "import_card",
    "import_parsed_card",
    "inspect_card",
    "prepare_card_columns",
    "rules_of",
    "source_fields_of",
]
