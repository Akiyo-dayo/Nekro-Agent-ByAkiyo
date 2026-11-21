"""世界书：匹配引擎、正则安全、定时效果、预算与酒馆格式

除 `service.py`、`runtime.py` 外全部是纯逻辑或进程内状态：不查库、不 import 路由、不读配置。
取数与授权在 `services/memory/instance_scope.py`、`service.py` 与世界书路由里，渲染在
`render.py` 与提示词模板层。这样分层是为了让「一条条目为什么命中 / 为什么没进提示词」
能被穷举用例钉住——它一旦掺进查询，就只能靠集成测试碰运气。

各模块职责：

- `types`：引擎的输入类型、上限常量、ORM 行到引擎输入的转换；
- `matcher`：扫描文本、归一化、关键词与次关键词逻辑、`person` 书的在场判定；
- `safe_regex`：正则关键词的解析、校验与带时限 / 熔断的执行；
- `timed_state`：sticky / cooldown / delay 的判定与按轮次提交的状态存储；
- `selection`：入选排序、预算截断、按注入位置分组；
- `render`：把入选条目渲染成提示词里的文字块（宿主组与不可信组、深度注入的落点）；
- `st_format`：酒馆世界书文件的导入解析与导出还原；
- `service`：查库的模块之一（书与条目的读写、导入导出、隔离区、被拦轮次计数）；
- `runtime`：查库的另一个模块（每轮求值、窗口、定时锚点、按轮次缓存）。`service` 与
  `runtime` 刻意都**不**在这里 re-export：纯函数用例 import 本包时不该顺带把 ORM 查询层
  拉起来。
"""

from .matcher import (
    HitReason,
    book_active_for_person,
    entry_hit,
    key_hit,
    normalize_for_match,
    present_person_accounts,
    scan_text,
)
from .safe_regex import RegexRunner, RegexSpec, disabled_regex_keys, forget_entry, parse_regex_key, validate_regex_key
from .selection import Hit, RenderGroups, Selection, group_for_render, select_hits
from .st_format import (
    ST_ENTRY_DEFAULTS,
    ImportNote,
    ParsedWorld,
    WorldFileRejected,
    columns_to_st_entry,
    export_world,
    parse_world_file,
    st_entry_to_columns,
    summarize_notes,
    validate_entry_regexes,
)
from .timed_state import (
    TimedGate,
    TimedStateStore,
    apply_timed,
    delay_satisfied,
    entry_gate,
    timed_gate,
    timed_state_store,
)
from .types import (
    DEFAULT_BOOK_SCAN_DEPTH,
    DEFAULT_PERSON_BOOK_SCAN_DEPTH,
    MAX_COMMENT_CHARS,
    MAX_CONTENT_CHARS,
    MAX_KEY_LENGTH,
    MAX_KEYS,
    MAX_NAME_CHARS,
    EntryStatus,
    MatchBook,
    MatchEntry,
    Position,
    QuarantineReason,
    ScopeKind,
    SelectiveLogic,
    WindowMessage,
    book_to_match,
    entry_to_match,
)

__all__ = [
    "DEFAULT_BOOK_SCAN_DEPTH",
    "DEFAULT_PERSON_BOOK_SCAN_DEPTH",
    "MAX_COMMENT_CHARS",
    "MAX_CONTENT_CHARS",
    "MAX_KEYS",
    "MAX_KEY_LENGTH",
    "MAX_NAME_CHARS",
    "ST_ENTRY_DEFAULTS",
    "EntryStatus",
    "Hit",
    "HitReason",
    "ImportNote",
    "MatchBook",
    "MatchEntry",
    "ParsedWorld",
    "Position",
    "QuarantineReason",
    "RegexRunner",
    "RegexSpec",
    "RenderGroups",
    "ScopeKind",
    "Selection",
    "SelectiveLogic",
    "TimedGate",
    "TimedStateStore",
    "WindowMessage",
    "WorldFileRejected",
    "apply_timed",
    "book_active_for_person",
    "book_to_match",
    "columns_to_st_entry",
    "delay_satisfied",
    "disabled_regex_keys",
    "entry_gate",
    "entry_hit",
    "entry_to_match",
    "export_world",
    "forget_entry",
    "group_for_render",
    "key_hit",
    "normalize_for_match",
    "parse_regex_key",
    "parse_world_file",
    "present_person_accounts",
    "scan_text",
    "select_hits",
    "st_entry_to_columns",
    "summarize_notes",
    "timed_gate",
    "timed_state_store",
    "validate_entry_regexes",
    "validate_regex_key",
]
