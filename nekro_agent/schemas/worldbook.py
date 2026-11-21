"""世界书管理接口的请求与响应模型

字段约束尽量引用 `services/worldbook/types.py` 里的常量，而不是在这里另写一遍数字：
保存、导入、导出三条路径都要按同一组上限夹值，写三份就会出现「保存时允许 300 字、
导入时允许 200 字」这种只有一条路径能复现的不一致。只在类型层做得到的约束（字符串去
空白后非空、关键词去重）也放在这里，让坏输入在构造请求对象时就被挡住，而不是等到写库
前一刻才报错。

有两处上限留在本模块：`description` 的 2000 字与 `budget_chars` 的 100000 只影响界面
与预算算术，引擎侧一个字节都不读它们，放进 `types.py` 会让那份常量表看起来像引擎判据。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from nekro_agent.services.worldbook.types import (
    DEPTH_MAX,
    DEPTH_MIN,
    MAX_COMMENT_CHARS,
    MAX_CONTENT_CHARS,
    MAX_KEY_LENGTH,
    MAX_KEYS,
    MAX_NAME_CHARS,
    MAX_TRIGGER_TEXT_CHARS,
    ORDER_MAX,
    ORDER_MIN,
    SCAN_DEPTH_MAX,
    SCAN_DEPTH_MIN,
    TIMED_MAX,
    Position,
    ScopeKind,
    SelectiveLogic,
)

# 只给人看的两处上限，理由见模块文档。
MAX_DESCRIPTION_CHARS = 2000
MAX_BUDGET_CHARS = 100_000

# 「记一笔」的内容上限。比条目正文小得多，理由见 `PersonNoteCreateRequest`。
PERSON_NOTE_MAX_CHARS = 500


def _clean_keys(value: list[str]) -> list[str]:
    """关键词数组：逐项去首尾空白、丢空串、去重保序，再按上限拒绝

    去重保序而不是排序：关键词的先后决定「因为哪个词命中」这条明细读起来是否顺，排序会
    把它变成与管理员书写顺序无关的东西。超限抛 `ValueError`（而不是截断）：手动保存时
    悄悄丢掉几个关键词，管理员只会看到「我明明写了它却不生效」。
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        key = item.strip()
        if not key or key in seen:
            continue
        if len(key) > MAX_KEY_LENGTH:
            raise ValueError(f"单个关键词不能超过 {MAX_KEY_LENGTH} 字")
        seen.add(key)
        cleaned.append(key)
    if len(cleaned) > MAX_KEYS:
        raise ValueError(f"关键词不能超过 {MAX_KEYS} 个")
    return cleaned


class BookCreateRequest(BaseModel):
    """建书请求

    `scope_kind` 决定哪几个绑定字段有意义：`preset` 用 `preset_id`、`channel` 用
    `chat_key`、`person` 用 `person_account`、`domain` 谁也不用。这里不按类型清空无关
    字段——那是服务层的事，它才知道最终归属（平台方可以不给实例）。
    """

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS)
    scope_kind: ScopeKind
    preset_id: int = Field(default=0, ge=0)
    chat_key: str = Field(default="", max_length=256)
    person_account: str = Field(default="", max_length=128)
    enabled: bool = True
    # 可空：空表示「按作用域类型取默认」，`person` 书 10、其它 4。默认值不在模型层定死，
    # 是因为它取决于 `scope_kind`，而那要等请求构造完才知道。
    scan_depth: Optional[int] = Field(default=None, ge=SCAN_DEPTH_MIN, le=SCAN_DEPTH_MAX)
    budget_chars: int = Field(default=0, ge=0, le=MAX_BUDGET_CHARS)
    case_sensitive: bool = False
    match_whole_words: bool = False

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("书名不能为空")
        return text

    @field_validator("person_account")
    @classmethod
    def _person_account_stripped(cls, value: str) -> str:
        return value.strip()


class BookUpdateRequest(BaseModel):
    """改书设置请求；`None` 表示不改

    作用域类型与绑定对象刻意不在里面：换作用域等于换一本书（条目会跟着跑到另一个
    频道 / 人设上），删了重建才是可解释的操作。
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=MAX_NAME_CHARS)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)
    enabled: Optional[bool] = None
    scan_depth: Optional[int] = Field(default=None, ge=SCAN_DEPTH_MIN, le=SCAN_DEPTH_MAX)
    budget_chars: Optional[int] = Field(default=None, ge=0, le=MAX_BUDGET_CHARS)
    case_sensitive: Optional[bool] = None
    match_whole_words: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("书名不能为空")
        return text


class EntryFields(BaseModel):
    """条目创建与编辑共用的字段集合

    `scan_depth` / `case_sensitive` / `match_whole_words` 三列可空，空表示「跟书」：
    这是三态覆盖，不能折成布尔的默认值，否则「没设」与「设成假」会变成同一件事。
    """

    comment: str = Field(default="", max_length=MAX_COMMENT_CHARS)
    keys: list[str] = Field(default_factory=list)
    secondary_keys: list[str] = Field(default_factory=list)
    selective_logic: SelectiveLogic = SelectiveLogic.AND_ANY
    use_regex: bool = False
    constant: bool = False
    content: str = Field(default="", max_length=MAX_CONTENT_CHARS)
    order: int = Field(default=100, ge=ORDER_MIN, le=ORDER_MAX)
    position: Position = Position.BEFORE_HISTORY
    depth: int = Field(default=4, ge=DEPTH_MIN, le=DEPTH_MAX)
    scan_depth: Optional[int] = Field(default=None, ge=SCAN_DEPTH_MIN, le=SCAN_DEPTH_MAX)
    case_sensitive: Optional[bool] = None
    match_whole_words: Optional[bool] = None
    sticky: Optional[int] = Field(default=None, ge=0, le=TIMED_MAX)
    cooldown: Optional[int] = Field(default=None, ge=0, le=TIMED_MAX)
    delay: Optional[int] = Field(default=None, ge=0, le=TIMED_MAX)
    enabled: bool = True
    trusted: Optional[bool] = None

    @field_validator("keys", "secondary_keys")
    @classmethod
    def _keys_cleaned(cls, value: list[str]) -> list[str]:
        return _clean_keys(value)

    @field_validator("comment")
    @classmethod
    def _comment_stripped(cls, value: str) -> str:
        return value.strip()


class EntryCreateRequest(EntryFields):
    """建条目请求：正文必填，其余取模型默认值

    正文必须去空白后非空：一条没有正文的条目注入进提示词只会占预算，管理员多半是漏填了。
    """

    content: str = Field(max_length=MAX_CONTENT_CHARS)

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("正文不能为空")
        return text


class EntryUpdateRequest(BaseModel):
    """改条目请求；没传表示不改

    不继承 `EntryFields`：那边每个字段都有默认值，直接继承会让「没传」变成「改成默认
    值」，一次只改标题的请求会把正文清空。

    可空的六列（扫描深度、大小写、整词、sticky / cooldown / delay）显式传 `null` 表示
    清空——回到跟着书走或不用；其余字段传 `null` 与没传相同。判据是 `model_fields_set`，
    见 `services/worldbook/service.py::update_entry`。
    """

    comment: Optional[str] = Field(default=None, max_length=MAX_COMMENT_CHARS)
    keys: Optional[list[str]] = None
    secondary_keys: Optional[list[str]] = None
    selective_logic: Optional[SelectiveLogic] = None
    use_regex: Optional[bool] = None
    constant: Optional[bool] = None
    content: Optional[str] = Field(default=None, max_length=MAX_CONTENT_CHARS)
    order: Optional[int] = Field(default=None, ge=ORDER_MIN, le=ORDER_MAX)
    position: Optional[Position] = None
    depth: Optional[int] = Field(default=None, ge=DEPTH_MIN, le=DEPTH_MAX)
    scan_depth: Optional[int] = Field(default=None, ge=SCAN_DEPTH_MIN, le=SCAN_DEPTH_MAX)
    case_sensitive: Optional[bool] = None
    match_whole_words: Optional[bool] = None
    sticky: Optional[int] = Field(default=None, ge=0, le=TIMED_MAX)
    cooldown: Optional[int] = Field(default=None, ge=0, le=TIMED_MAX)
    delay: Optional[int] = Field(default=None, ge=0, le=TIMED_MAX)
    enabled: Optional[bool] = None
    trusted: Optional[bool] = None

    @field_validator("keys", "secondary_keys")
    @classmethod
    def _keys_cleaned(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        return _clean_keys(value)

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("正文不能为空")
        return text

    @field_validator("comment")
    @classmethod
    def _comment_stripped(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else value.strip()


class EntryActionRequest(BaseModel):
    """隔离 / 放回 / 启用 / 停用

    四种动作走同一个端点而不是四个：它们的判据（当前身份能否改这条条目）与返回体完全
    相同，拆开只会让前端多写三个请求函数。
    """

    action: Literal["quarantine", "restore", "enable", "disable"]


class BookItem(BaseModel):
    """书列表的一行

    `binding_missing` 表示绑定对象已经不在了（人设被删、频道行被删）。管理页据此标
    「人设已删除」：书本身还在，但它在运行时永远不会命中，不标出来管理员只会以为
    「这本书怎么不生效」。
    """

    id: int
    name: str
    description: str = ""
    scope_kind: str
    adapter_key: str = ""
    instance_key: str = ""
    preset_id: int = 0
    preset_name: str = ""
    chat_key: str = ""
    channel_name: str = ""
    person_account: str = ""
    enabled: bool
    scan_depth: int
    budget_chars: int
    case_sensitive: bool
    match_whole_words: bool
    entry_count: int = 0
    quarantined_count: int = 0
    update_time: Optional[datetime] = None
    binding_missing: bool = False
    # 平台方给全局人设建、对当前租户只读开放的书：能看书名、条目与正文，改、删、导出、
    # 追加导入都拒绝。平台方看任何书都是 False。
    read_only: bool = False


class BookListResponse(BaseModel):
    total: int
    items: list[BookItem]


class BookDetail(BookItem):
    """书详情：列表行加上书级 `ext` 的键名

    只给键名不整份返回：`ext` 是导入时原样保留的酒馆字段，可能有几十 KB，而管理页对它的
    全部需求就是「这本书带进来了哪些额外字段」。
    """

    ext_keys: list[str] = Field(default_factory=list)


class InvalidKeyItem(BaseModel):
    """导入时被判为无效的关键词

    `reason` 是中文原因，直接显示给管理员：他需要知道是语法错、太长、还是复杂度被拒。
    """

    key: str
    reason: str


class EntryItem(BaseModel):
    """条目的一行

    `ext` 不整份返回：它装着原始酒馆条目（几十个字段），而管理页要的只是「有没有坏
    关键词」与「有没有被熔断」这两件事，因此拆成两个有名字的字段。
    """

    id: int
    book_id: int
    uid: int
    comment: str = ""
    keys: list[str] = Field(default_factory=list)
    secondary_keys: list[str] = Field(default_factory=list)
    selective_logic: str
    use_regex: bool
    constant: bool
    content: str
    order: int
    position: str
    depth: int
    scan_depth: Optional[int] = None
    case_sensitive: Optional[bool] = None
    match_whole_words: Optional[bool] = None
    sticky: Optional[int] = None
    cooldown: Optional[int] = None
    delay: Optional[int] = None
    enabled: bool
    trusted: bool
    status: str
    quarantined_at: Optional[datetime] = None
    quarantine_reason: str = ""
    blocked_round_hits: int = 0
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    invalid_keys: list[InvalidKeyItem] = Field(default_factory=list)
    slow_disabled_keys: list[str] = Field(default_factory=list)


class EntryListResponse(BaseModel):
    items: list[EntryItem]


class QuarantineItem(BaseModel):
    """隔离区的一行：带上书 ID 与书名，管理员不用再点进书里才知道它属于哪本"""

    entry_id: int
    book_id: int
    book_name: str
    comment: str = ""
    quarantine_reason: str = ""
    blocked_round_hits: int = 0
    quarantined_at: Optional[datetime] = None


class QuarantineListResponse(BaseModel):
    total: int
    items: list[QuarantineItem]


class DeleteResponse(BaseModel):
    """删书的返回：连带删掉多少条目

    报条数而不是只回 204：世界书是真删，管理员需要一个「确实删掉了这些」的凭据。
    """

    deleted_entries: int


class ImportDetailItem(BaseModel):
    """导入报告的一条明细"""

    entry_key: str = ""
    field: str = ""
    kind: str = ""
    detail: str = ""


class ImportReport(BaseModel):
    """导入结果

    `notes` 每个类别都有键（0 也在），前端可以无条件读；`details` 截断到 200 条——一份
    两千条的文件可能产生上万条明细，全带回去只会把响应体撑爆，而管理员实际只会看前几条。
    """

    book_id: int
    created: int
    notes: dict[str, int] = Field(default_factory=dict)
    details: list[ImportDetailItem] = Field(default_factory=list)


class PersonNoteCreateRequest(BaseModel):
    """记一笔请求：内容去首尾空白后必须 1 到 500 字

    上限 500 比条目正文的 8000 小得多：这是「一句话备注」，不是设定；放宽到条目上限
    只会让好感详情里的一个文本框塞进整屏设定，而那些内容该在世界书页里写。

    两个 `before` 校验器而不是 `after`：长度约束在 `after` 之前就已经生效了，用 `after`
    的话「500 字后面跟两个空格」会被判成超长——而它去空白之后正好 500 字，是合法的。
    """

    adapter_key: str = Field(default="", max_length=64)
    instance_key: str = Field(max_length=64)
    person_account: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=PERSON_NOTE_MAX_CHARS)

    @field_validator("person_account", "content", mode="before")
    @classmethod
    def _strip_text(cls, value: object) -> object:
        # 非字符串交给 pydantic 自己的类型报错，这里只负责去掉首尾空白。
        return value.strip() if isinstance(value, str) else value


class PersonNoteCreateResponse(BaseModel):
    """记一笔的结果

    `created_book` 让界面能说清「顺手建了一本个人书」：管理员随后在世界书页看到一本新书时
    不必回头猜它是哪里来的。
    """

    book_id: int
    entry_id: int
    created_book: bool = False


class PersonNoteItem(BaseModel):
    """好感详情里的一条已有备注

    `status` / `enabled` 都带上：这一栏是只读列表，管理员需要看出哪一条已经被隔离或停用，
    否则「写了却没生效」会变成一次排查。
    """

    book_id: int
    book_name: str
    entry_id: int
    comment: str = ""
    content: str
    enabled: bool
    status: str
    trusted: bool
    update_time: Optional[datetime] = None


class PersonNoteListResponse(BaseModel):
    items: list[PersonNoteItem] = Field(default_factory=list)


class TriggerTestRequest(BaseModel):
    """触发测试请求

    `preset_id` 不给时用频道当前生效的人设：管理员多半就是想知道「现在这个频道会注入
    什么」，让他再选一次人设只会选错。
    """

    chat_key: str = Field(max_length=256)
    # 测试文字要逐字归一化、再被每条关键词扫一遍，不设上限时一次请求就能让这个端点占满一个
    # 工作线程。它与条目正文的安全上限不是一回事（那个宽到真实世界书碰不到），单独设一个小的。
    text: str = Field(default="", max_length=MAX_TRIGGER_TEXT_CHARS)
    use_recent: bool = True
    preset_id: Optional[int] = None


class TriggerBookItem(BaseModel):
    """这一轮生效的书；`trusted_entries` 与 `untrusted_entries` 分开数

    按宿主内容注入的条目与普通条目在提示词里待遇不同，管理员需要一眼看出这本书里有多
    少条会被当成可信内容。
    """

    id: int
    name: str
    scope_kind: str
    trusted_entries: int = 0
    untrusted_entries: int = 0


class TriggerHitItem(BaseModel):
    """一条命中；逐条只给 ID 与标题，正文只在 `rendered` 里出现一次

    管理员本来就有这本书的读权限，逐条再带一份正文没有意义，而一次测试可能命中几十条、
    每条几千字。正文的完整形状由 `rendered` 给出（那才是真正会进提示词的东西），逐条
    重复一遍只会让响应翻倍。
    """

    entry_id: int
    book_id: int
    comment: str = ""
    reason_kind: str
    matched_key: str = ""
    position: str
    order: int
    chars: int
    trusted: bool


class TriggerDroppedItem(BaseModel):
    """被预算跳过的条目与原因 `global_chars` / `book_chars` / `max_entries`"""

    entry_id: int
    reason: str


class TriggerGroups(BaseModel):
    """按注入位置分好的组；`at_depth` 的键是深度（字符串，JSON 对象的键只能是字符串）"""

    after_persona: list[int] = Field(default_factory=list)
    before_history: list[int] = Field(default_factory=list)
    at_depth: dict[str, list[int]] = Field(default_factory=dict)


class TriggerRendered(BaseModel):
    """三个注入位置渲染出的文字

    三个字段恒有值（没有条目时是空串），前端可以无条件渲染三段。`at_depth` 的键是深度
    的字符串形式，与 `TriggerGroups.at_depth` 同一套键：两处键对不上时前端会把一段文字
    配到另一个位置上。
    """

    after_persona: str = ""
    before_history: str = ""
    at_depth: dict[str, str] = Field(default_factory=dict)


class TriggerTestResponse(BaseModel):
    """触发测试结果

    `timed_ignored` 恒为真：本端点不读定时状态、也不推进它，sticky 保持期与 cooldown
    在测试里一律当作「从没生效过」。这个字段存在的意义是把这条限制写在响应里，而不是
    只写在文档里。

    `gate_enabled` 报这个频道**此刻**的闸状态。闸关时结果照算（内部一致、只是不会真的
    注入），页面据此提示「现在不会注入」——直接把结果清空会让管理员以为书配错了。

    `rendered` 是三个位置的渲染文字，令牌用固定占位串；租户看不到的书里的条目，正文是
    固定占位文字（书名与标题照常给）。
    """

    books: list[TriggerBookItem] = Field(default_factory=list)
    hits: list[TriggerHitItem] = Field(default_factory=list)
    selected: list[int] = Field(default_factory=list)
    dropped: list[TriggerDroppedItem] = Field(default_factory=list)
    groups: TriggerGroups = Field(default_factory=TriggerGroups)
    window_size: int = 0
    timed_ignored: bool = True
    gate_enabled: bool = True
    rendered: TriggerRendered = Field(default_factory=TriggerRendered)


__all__ = [
    "MAX_BUDGET_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "PERSON_NOTE_MAX_CHARS",
    "BookCreateRequest",
    "BookDetail",
    "BookItem",
    "BookListResponse",
    "BookUpdateRequest",
    "DeleteResponse",
    "EntryActionRequest",
    "EntryCreateRequest",
    "EntryFields",
    "EntryItem",
    "EntryListResponse",
    "EntryUpdateRequest",
    "ImportDetailItem",
    "ImportReport",
    "InvalidKeyItem",
    "PersonNoteCreateRequest",
    "PersonNoteCreateResponse",
    "PersonNoteItem",
    "PersonNoteListResponse",
    "QuarantineItem",
    "QuarantineListResponse",
    "TriggerBookItem",
    "TriggerDroppedItem",
    "TriggerGroups",
    "TriggerHitItem",
    "TriggerRendered",
    "TriggerTestRequest",
    "TriggerTestResponse",
]
