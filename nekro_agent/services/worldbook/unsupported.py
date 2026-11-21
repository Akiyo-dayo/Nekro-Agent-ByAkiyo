"""导入报告的「不生效清单」与网页代码的分类

人设卡导入与世界书文件导入共用这里的判据，两边报告里同一类东西说的是同一句话：

- **酒馆助手脚本**（只有人设卡带）：名字、字数、写死的联网地址、属于哪一类；NA 不执行。
- **EJS 模板块**（`<% … %>`）：只数个数与所在处数。世界书条目里的模板在运行时按安全子集
  求值（`template_lite`），这里不重复那套判断。
- **世界书条目里 NA 不支持的酒馆功能**：按功能数条目，并写明 NA 实际怎么处理这些条目
  （照样导入、照样匹配，只是那个功能不起作用）。「最少激活数」「最大递归层数」是酒馆的全局
  设置，不在书文件里，数不到；书里能看到的递归设置只有「只在递归时激活」。

网页代码分成四类，卡片的 HTML 清理规则与酒馆助手脚本共用：

- `display` 纯展示：只有 HTML / CSS，不跑脚本、不取外部资源；
- `external` 依赖外部资源：没有脚本，但要从网上取图片、样式或字体；
- `interactive` 交互程序：带脚本、内联事件或 `javascript:` 链接；
- `remote` 远端代码或改世界书：从网上加载代码来执行（`import` 网址、`<script src>`、拼一个
  `<script>` 元素），或调用酒馆助手的接口改写世界书。

所有判断都是对文字的有界扫描，不执行任何东西；正则里的重复都封了顶，几十万字的脚本也是线性的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

# ---------------------------------------------------------------------------
# 网页代码分类
# ---------------------------------------------------------------------------

CODE_DISPLAY = "display"
CODE_EXTERNAL = "external"
CODE_INTERACTIVE = "interactive"
CODE_REMOTE = "remote"
CODE_KINDS = (CODE_DISPLAY, CODE_EXTERNAL, CODE_INTERACTIVE, CODE_REMOTE)
CODE_KIND_LABELS = {
    CODE_DISPLAY: "纯展示",
    CODE_EXTERNAL: "依赖外部资源",
    CODE_INTERACTIVE: "交互程序",
    CODE_REMOTE: "远端代码或改世界书",
}

_SCRIPT_TAG = re.compile(r"<\s{0,8}script\b", re.IGNORECASE)
_INLINE_EVENT = re.compile(r"\son[a-z]{3,24}\s{0,8}=", re.IGNORECASE)
_JS_URL = re.compile(r"javascript\s{0,8}:", re.IGNORECASE)
# `import x from "https://…"`、`import "https://…"`、`import("https://…")`
_REMOTE_IMPORT = re.compile(
    r"""\bimport\s{0,8}(?:\(\s{0,8})?(?:[\w{}\s,*$]{1,200}?\sfrom\s{0,8})?["'`](?:https?:)?//""",
    re.IGNORECASE,
)
_SCRIPT_SRC = re.compile(
    r"""<\s{0,8}script\b[^<>]{0,500}?\bsrc\s{0,8}=\s{0,8}["']?\s{0,8}(?:https?:)?//""", re.IGNORECASE
)
_CREATE_SCRIPT = re.compile(r"""createElement\(\s{0,8}["'`]script["'`]\s{0,8}\)""", re.IGNORECASE)
# 酒馆助手（及旧版 Lorebook 接口）里改写世界书的函数
_WORLDBOOK_WRITE = re.compile(
    r"\b(?:createOrReplaceWorldbook|replaceWorldbook|deleteWorldbook|createWorldbook|createWorldbookEntries|"
    r"deleteWorldbookEntries|updateWorldbookWith|rebindCharWorldbooks|rebindChatWorldbook|rebindGlobalWorldbooks|"
    r"createLorebook|deleteLorebook|createLorebookEntries|deleteLorebookEntries|setLorebookEntries|"
    r"setLorebookSettings|setChatLorebook)\s{0,8}\(",
)
# 从网上取的资源：属性里的网址、CSS 的 `url(//…)`、`@import`
_EXTERNAL = re.compile(
    r"""\b(?:src|href|poster|srcset|data)\s{0,8}=\s{0,8}["']?\s{0,8}(?:https?:)?//"""
    r"""|url\(\s{0,8}["']?\s{0,8}(?:https?:)?//"""
    r"""|@import\b""",
    re.IGNORECASE,
)


def writes_worldbook(text: str) -> bool:
    """这段脚本会不会调酒馆助手（或旧版 Lorebook 接口）的函数改写世界书"""
    return _WORLDBOOK_WRITE.search(text) is not None


def classify_code(text: str, *, script: bool = False) -> str:
    """一段网页代码属于哪一类，见模块说明；`script` 为真表示它本身就是脚本（至少是交互程序）"""
    if (
        _REMOTE_IMPORT.search(text)
        or _SCRIPT_SRC.search(text)
        or _CREATE_SCRIPT.search(text)
        or _WORLDBOOK_WRITE.search(text)
    ):
        return CODE_REMOTE
    if script or _SCRIPT_TAG.search(text) or _INLINE_EVENT.search(text) or _JS_URL.search(text):
        return CODE_INTERACTIVE
    if _EXTERNAL.search(text):
        return CODE_EXTERNAL
    return CODE_DISPLAY


# ---------------------------------------------------------------------------
# 酒馆助手脚本
# ---------------------------------------------------------------------------

# 写死的联网地址，只取主机名。
_URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)", re.IGNORECASE)
# 不是网络请求的主机：`www.w3.org` 出现在 SVG / XML 的命名空间里；`.invalid` 是保留顶级域
# （RFC 2606），永远解析不到，脚本作者常拿它当占位。
_NON_NETWORK_HOSTS = frozenset({"www.w3.org"})
_RESERVED_SUFFIX = ".invalid"
# 报告里最多列多少个脚本、每个脚本最多列多少个主机；超出的写「另有 N 个」。
MAX_LISTED_SCRIPTS = 20
MAX_LISTED_HOSTS = 8
# 脚本名字在说明里最多显示多少字。
SCRIPT_NAME_CHARS = 40
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def tavern_helper_scripts(extensions: Mapping[str, Any]) -> list[tuple[str, str]]:
    """卡里的酒馆助手脚本 `(名字, 正文)`，按出现顺序

    新结构是 `extensions.tavern_helper.scripts`，里面可以有文件夹（带自己的 `scripts`）；旧版本写在
    `extensions.TavernHelper_scripts`。两处都收。JSON 嵌套层数在解析时已经封顶，这里递归不会失控。
    """
    found: list[tuple[str, str]] = []

    def walk(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                name = item.get("name")
                found.append((name if isinstance(name, str) else "", content))
            elif isinstance(item.get("scripts"), list):
                walk(item["scripts"])

    helper = extensions.get("tavern_helper")
    if isinstance(helper, dict):
        walk(helper.get("scripts"))
    walk(extensions.get("TavernHelper_scripts"))
    return found


def script_hosts(content: str) -> list[str]:
    """脚本里写死的联网主机，去重保序，去掉不是网络请求的那几种"""
    hosts: list[str] = []
    for match in _URL_HOST.finditer(content):
        host = match.group(1).lower().rstrip(".")
        if not host or host in _NON_NETWORK_HOSTS or host.endswith(_RESERVED_SUFFIX) or host in hosts:
            continue
        hosts.append(host)
    return hosts


def _label(name: str) -> str:
    return _CONTROL_CHARS.sub("", name).strip()[:SCRIPT_NAME_CHARS] or "未命名"


def scripts_detail(scripts: list[tuple[str, str]]) -> str:
    """酒馆助手脚本清单的说明：名字、字数、类别、联网主机；写明 NA 不执行"""
    kinds = [classify_code(content, script=True) for _name, content in scripts]
    parts: list[str] = []
    for (name, content), kind in list(zip(scripts, kinds))[:MAX_LISTED_SCRIPTS]:
        label = _label(name)
        hosts = script_hosts(content)
        if hosts:
            listed = "、".join(hosts[:MAX_LISTED_HOSTS])
            if len(hosts) > MAX_LISTED_HOSTS:
                listed += f" 等 {len(hosts)} 个"
            parts.append(f"「{label}」{len(content)} 字，{CODE_KIND_LABELS[kind]}，联网 {listed}")
        else:
            parts.append(f"「{label}」{len(content)} 字，{CODE_KIND_LABELS[kind]}，没有写死的联网地址")
    more = f"；另有 {len(scripts) - MAX_LISTED_SCRIPTS} 个未列出" if len(scripts) > MAX_LISTED_SCRIPTS else ""
    tally = "、".join(
        f"{CODE_KIND_LABELS[kind]} {kinds.count(kind)} 个" for kind in (CODE_INTERACTIVE, CODE_REMOTE) if kind in kinds
    )
    return (
        f"卡里有 {len(scripts)} 个酒馆助手脚本（{tally}），NA 不执行（原样保存在卡片原文里，导出时还原）："
        + "；".join(parts)
        + more
    )


# ---------------------------------------------------------------------------
# EJS 模板块
# ---------------------------------------------------------------------------


def count_ejs_blocks(text: str) -> int:
    """数 `<% … %>` 块；手写的线性扫描

    `<%[\\s\\S]*?%>` 这类懒惰正则在大量未闭合的 `<%` 上对每个起点都扫到末尾，是二次方的，
    而卡片文字最长有几 MB。
    """
    count = 0
    position = text.find("<%")
    while position >= 0:
        end = text.find("%>", position + 2)
        if end < 0:
            break
        count += 1
        position = text.find("<%", end + 2)
    return count


def ejs_usage(texts: Iterable[str]) -> tuple[int, int]:
    """一批文字里共有多少个模板块、分布在几段文字里"""
    blocks = 0
    places = 0
    for text in texts:
        found = count_ejs_blocks(text)
        if found:
            blocks += found
            places += 1
    return blocks, places


# ---------------------------------------------------------------------------
# 世界书条目里不支持的功能
# ---------------------------------------------------------------------------


def _value(entry: Mapping[str, Any], st_key: str, card_key: str) -> Any:
    """取一个字段：世界书文件的写法在顶层（`useProbability`），卡内书条目在 `extensions` 里（`probability`、
    `delay_until_recursion` 这样的蛇形名）。两种形状都认，顶层优先。"""
    if st_key in entry:
        return entry.get(st_key)
    extensions = entry.get("extensions")
    if isinstance(extensions, dict):
        return extensions.get(card_key)
    return None


def _truthy_number(value: Any) -> bool:
    """布尔真或大于 0 的数；`delayUntilRecursion` 在新版里是层数，旧版是布尔"""
    if isinstance(value, bool):
        return value
    return isinstance(value, (int, float)) and value > 0


def _non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and any(item not in (None, "") for item in value)


def _uses_group(entry: Mapping[str, Any]) -> bool:
    group = _value(entry, "group", "group")
    return isinstance(group, str) and group.strip() != ""


def _uses_probability(entry: Mapping[str, Any]) -> bool:
    use = _value(entry, "useProbability", "useProbability")
    if use is False:
        return False
    probability = _value(entry, "probability", "probability")
    return isinstance(probability, (int, float)) and not isinstance(probability, bool) and probability < 100


def _uses_recursion_delay(entry: Mapping[str, Any]) -> bool:
    return _truthy_number(_value(entry, "delayUntilRecursion", "delay_until_recursion"))


def _uses_character_filter(entry: Mapping[str, Any]) -> bool:
    if _non_empty_list(entry.get("characterFilterNames")) or _non_empty_list(entry.get("characterFilterTags")):
        return True
    extensions = entry.get("extensions")
    card_filter = extensions.get("character_filter") if isinstance(extensions, dict) else None
    for raw in (entry.get("characterFilter"), card_filter):
        if isinstance(raw, dict) and (_non_empty_list(raw.get("names")) or _non_empty_list(raw.get("tags"))):
            return True
    return False


def _uses_triggers(entry: Mapping[str, Any]) -> bool:
    return _non_empty_list(_value(entry, "triggers", "triggers"))


_EXTRA_SOURCES = (
    ("matchPersonaDescription", "match_persona_description"),
    ("matchCharacterDescription", "match_character_description"),
    ("matchCharacterPersonality", "match_character_personality"),
    ("matchCharacterDepthPrompt", "match_character_depth_prompt"),
    ("matchScenario", "match_scenario"),
    ("matchCreatorNotes", "match_creator_notes"),
)


def _uses_extra_sources(entry: Mapping[str, Any]) -> bool:
    return any(_value(entry, st_key, card_key) is True for st_key, card_key in _EXTRA_SOURCES)


def _uses_automation(entry: Mapping[str, Any]) -> bool:
    automation = _value(entry, "automationId", "automation_id")
    return isinstance(automation, str) and automation.strip() != ""


def _uses_decorator(entry: Mapping[str, Any]) -> bool:
    content = entry.get("content")
    return isinstance(content, str) and content.startswith("@@")


@dataclass(frozen=True)
class Feature:
    """一种 NA 不支持的条目功能：名字与 NA 实际怎么处理用了它的条目"""

    key: str
    label: str
    handling: str
    check: Callable[[Mapping[str, Any]], bool]


FEATURES: tuple[Feature, ...] = (
    Feature("decorator", "装饰器（正文以 @@ 开头）", "不解析，开头那几行当正文注入", _uses_decorator),
    Feature("recursion", "只在递归时激活", "NA 不做递归扫描，这些条目按普通条目匹配", _uses_recursion_delay),
    Feature("group", "包含组 / 组评分", "同组条目不互斥，命中的都会注入", _uses_group),
    Feature("probability", "概率", "命中就注入，不掷骰", _uses_probability),
    Feature("character_filter", "角色 / 标签过滤", "不过滤，哪个人设下都会命中", _uses_character_filter),
    Feature("triggers", "生成类型过滤", "不区分生成类型", _uses_triggers),
    Feature("extra_sources", "额外扫描来源（人设描述、角色描述等）", "只扫描聊天记录", _uses_extra_sources),
    Feature("automation", "自动化（automationId）", "不执行", _uses_automation),
)


@dataclass(frozen=True)
class FeatureUse:
    """一本书里用了某种不支持功能的条目数"""

    feature: Feature
    count: int


def feature_uses(entries: Iterable[Mapping[str, Any]]) -> list[FeatureUse]:
    """按 :data:`FEATURES` 的顺序数条目；没人用的功能不出现。条目可以是世界书文件或卡内书的形状"""
    counts = [0] * len(FEATURES)
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        for index, feature in enumerate(FEATURES):
            if feature.check(entry):
                counts[index] += 1
    return [FeatureUse(feature, count) for feature, count in zip(FEATURES, counts) if count]


def features_detail(uses: list[FeatureUse], *, book_label: str) -> str:
    """不支持功能的说明：一本书一句话，逐个功能写条数与 NA 的处理"""
    parts = [f"{use.feature.label} {use.count} 条（{use.feature.handling}）" for use in uses]
    return f"{book_label}有 NA 不支持的酒馆功能，这些条目照样导入：" + "；".join(parts)


# ---------------------------------------------------------------------------
# 合起来：一份「不生效清单」
# ---------------------------------------------------------------------------

EJS_DETAIL_CARD = (
    "这张卡有 {blocks} 个 EJS 模板块（<% … %>，在 {places} 处文字里）。卡内知识书条目里的"
    "模板按安全子集求值（只认按变量选段落、把变量打出来这类写法，不执行代码；状态功能上线前"
    "变量一律取不到），认不出的条目整条不注入；人设正文等其它字段里的模板 NA 不执行，会原样留在文字里"
)
EJS_DETAIL_BOOK = (
    "这本书有 {blocks} 个 EJS 模板块（<% … %>，在 {places} 条条目里）。模板按安全子集求值（只认按变量选段落、"
    "把变量打出来这类写法，不执行代码；状态功能上线前变量一律取不到），认不出的条目整条不注入"
)


@dataclass(frozen=True)
class UnsupportedItem:
    """清单里的一项：`field` 指认是哪一块（`extensions.tavern_helper` / `ejs` / `character_book` / `entries`）"""

    field: str
    detail: str


def unsupported_items(
    *,
    entries: Iterable[Mapping[str, Any]] = (),
    extensions: Optional[Mapping[str, Any]] = None,
    texts: Optional[Iterable[str]] = None,
    card: bool = False,
) -> list[UnsupportedItem]:
    """人设卡与世界书文件共用的「不生效清单」

    Args:
        entries: 世界书条目（世界书文件或卡内书的形状都行），数不支持的功能。
        extensions: 卡片的 `extensions`，列酒馆助手脚本；世界书文件没有，传 `None`。
        texts: 要数 EJS 模板块的文字；不给就数条目正文。
        card: 用人设卡的口径写说明（卡内知识书 / 其它字段），否则用世界书文件的口径。
    """
    entry_list = [entry for entry in entries if isinstance(entry, Mapping)]
    items: list[UnsupportedItem] = []

    if texts is None:
        texts = [entry["content"] for entry in entry_list if isinstance(entry.get("content"), str)]
    blocks, places = ejs_usage(texts)
    if blocks:
        template = EJS_DETAIL_CARD if card else EJS_DETAIL_BOOK
        items.append(UnsupportedItem("ejs", template.format(blocks=blocks, places=places)))

    uses = feature_uses(entry_list)
    if uses:
        if card:
            items.append(UnsupportedItem("character_book", features_detail(uses, book_label="卡内知识书里")))
        else:
            items.append(UnsupportedItem("entries", features_detail(uses, book_label="这本书里")))

    if extensions is not None:
        scripts = tavern_helper_scripts(extensions)
        if scripts:
            items.append(UnsupportedItem("extensions.tavern_helper", scripts_detail(scripts)))
    return items


__all__ = [
    "CODE_DISPLAY",
    "CODE_EXTERNAL",
    "CODE_INTERACTIVE",
    "CODE_KINDS",
    "CODE_KIND_LABELS",
    "CODE_REMOTE",
    "FEATURES",
    "MAX_LISTED_HOSTS",
    "MAX_LISTED_SCRIPTS",
    "Feature",
    "FeatureUse",
    "UnsupportedItem",
    "classify_code",
    "count_ejs_blocks",
    "ejs_usage",
    "feature_uses",
    "features_detail",
    "script_hosts",
    "scripts_detail",
    "tavern_helper_scripts",
    "unsupported_items",
    "writes_worldbook",
]
