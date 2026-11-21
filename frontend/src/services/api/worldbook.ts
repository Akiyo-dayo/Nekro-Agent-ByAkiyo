import axios from './axios'

/**
 * 世界书管理接口
 *
 * 路径都相对 `/worldbook`，权限一律 `Role.Admin`。可见范围由服务端按当前身份算出：
 * 请求里的实例、人设、频道、人都只当筛选条件，不是授权依据。
 *
 * 查询参数统一做一次空值清理：空串与 `undefined` 不写进 URL，让后端的默认值生效。
 * `0` 与 `false` 是有效取值，不能被当成空值丢掉。
 */

/** 实例作用域：平台方全局视图不带 `instance_key`；租户必须带上 */
export interface WorldbookScopeParams {
  instance_key?: string
  /** 选了实例时一起带：同名实例可以出现在两个适配器下 */
  adapter_key?: string
}

export const WORLDBOOK_SCOPE_KINDS = ['domain', 'preset', 'channel', 'person'] as const
export type WorldbookScopeKind = (typeof WORLDBOOK_SCOPE_KINDS)[number]

/** 书的注入方式：关键词命中就推送、留给模型自己查，或两样都行 */
export const WORLDBOOK_INJECT_MODES = ['push', 'lookup', 'both'] as const
export type WorldbookInjectMode = (typeof WORLDBOOK_INJECT_MODES)[number]

/** 书的生效场景：私聊和群聊、只在私聊，或只在群聊 */
export const WORLDBOOK_ACTIVE_SCENES = ['both', 'private', 'group'] as const
export type WorldbookActiveScene = (typeof WORLDBOOK_ACTIVE_SCENES)[number]

export const WORLDBOOK_POSITIONS = ['after_persona', 'before_history', 'at_depth'] as const
export type WorldbookPosition = (typeof WORLDBOOK_POSITIONS)[number]

export const WORLDBOOK_SELECTIVE_LOGICS = ['and_any', 'and_all', 'not_any', 'not_all'] as const
export type WorldbookSelectiveLogic = (typeof WORLDBOOK_SELECTIVE_LOGICS)[number]

export const WORLDBOOK_ENTRY_STATUSES = ['active', 'quarantined'] as const
export type WorldbookEntryStatus = (typeof WORLDBOOK_ENTRY_STATUSES)[number]

export const WORLDBOOK_ENTRY_ACTIONS = [
  'quarantine',
  'restore',
  'enable',
  'disable',
] as const
export type WorldbookEntryAction = (typeof WORLDBOOK_ENTRY_ACTIONS)[number]

export const WORLDBOOK_QUARANTINE_REASONS = ['manual', 'auto_blocked'] as const
export type WorldbookQuarantineReason = (typeof WORLDBOOK_QUARANTINE_REASONS)[number]

/**
 * 导入报告的类别，来自 `services/worldbook/st_format.py::NOTE_KINDS`
 *
 * 后端每个类别都会给键（0 也在），所以界面上可以无条件读；文案取不到时显示原键。
 */
export const WORLDBOOK_IMPORT_NOTE_KINDS = [
  'skipped',
  'truncated',
  'clamped',
  'invalid_key',
  'position_downgraded',
  'renumbered',
  'bad_value',
  'over_budget',
  'name_var',
  'state_only',
  'unsupported_template',
  'suspected_trap',
  'template_not_analyzed',
  'budget_check',
  'unsupported',
] as const
export type WorldbookImportNoteKind = (typeof WORLDBOOK_IMPORT_NOTE_KINDS)[number]

/**
 * 条目的用途（`services/worldbook/purpose.py`，由标题与正文现算、不存库）
 *
 * `normal` / `mvu_plot` 照常注入；`mvu_update` / `initvar` / `state_render` 仅在开启角色状态时
 * 使用，不进主提示词；`unsupported_template` 含不支持的模板，不注入。
 */
export type WorldbookEntryPurpose =
  | 'normal'
  | 'mvu_plot'
  | 'mvu_update'
  | 'initvar'
  | 'state_render'
  | 'unsupported_template'
export const WORLDBOOK_STATE_ONLY_PURPOSES: readonly string[] = ['mvu_update', 'initvar', 'state_render']

/** 与后端 `services/worldbook/types.py` 同值：表单校验与展示都用这一份 */
export const WORLDBOOK_NAME_MAX_LENGTH = 128
export const WORLDBOOK_DESCRIPTION_MAX_LENGTH = 2000
export const WORLDBOOK_COMMENT_MAX_LENGTH = 10_000
export const WORLDBOOK_CONTENT_MAX_LENGTH = 1_000_000
export const WORLDBOOK_MAX_KEYS = 1000
export const WORLDBOOK_KEY_MAX_LENGTH = 2000
/** 触发测试手填文字的上限（后端 `MAX_TRIGGER_TEXT_CHARS`），与条目正文上限不是一回事 */
export const WORLDBOOK_TRIGGER_TEXT_MAX_LENGTH = 8000
export const WORLDBOOK_SCAN_DEPTH_MIN = 1
export const WORLDBOOK_SCAN_DEPTH_MAX = 50
export const WORLDBOOK_DEPTH_MIN = 0
export const WORLDBOOK_DEPTH_MAX = 100
export const WORLDBOOK_TIMED_MAX = 200
export const WORLDBOOK_BUDGET_MAX = 65_536
export const WORLDBOOK_ORDER_MIN = -100_000
export const WORLDBOOK_ORDER_MAX = 100_000
export const WORLDBOOK_IMPORT_MAX_BYTES = 32 * 1024 * 1024
export const WORLDBOOK_PERSON_NOTE_MAX_LENGTH = 500

/** 触发测试命中的原因：常驻，或某个关键词 */
export type WorldbookTriggerReasonKind = 'constant' | 'key'
/** 命中了但没注入的原因：前三个是预算，后面是用途与模板（含每轮模板预算） */
export type WorldbookTriggerDropReason =
  | 'global_tokens'
  | 'book_tokens'
  | 'max_entries'
  | 'state_only'
  | 'template_unsupported'
  | 'template_trap'
  | 'template_error'
  | 'template_empty'
  | 'template_budget'

/** 触发测试与提示词明细共用的预算换算结果 */
export interface WorldbookBudgetInfo {
  model_group: string
  context_length: number
  context_length_set: boolean
  output_reserve: number
  percent: number
  cap_tokens: number
  base: number
  budget: number
  factor: number
  factor_source: string
  factor_family: string
  estimator: string
  fallback_model_group: string
  fallback_context_length: number
  approx_chars: number
}

/**
 * 预算体检里一条在「只有常驻条目命中」时被跳过的常驻条目；`reason` 是 `global_tokens` /
 * `book_tokens` / `max_entries`。只有 `alone_over` 为真的才是永远进不来，其余的在关键词条目
 * 命中后可能反而放得下
 */
export interface BudgetDroppedItem {
  entry_id: number
  uid: number
  title: string
  tokens: number
  reason: string
  /** 这一条单独就超过预算：再怎么腾也进不来 */
  alone_over: boolean
}

/**
 * 书的预算体检：把书里启用的常驻条目当作这一轮全都命中，按每轮预算装一遍
 *
 * `budget` 为 null 表示没有体检结果（预算读不到，或这本书对当前租户只读）；
 * `truncated` 为真表示常驻条目太多，只看了编号最小的一批。
 */
export interface BookBudgetCheck {
  budget: WorldbookBudgetInfo | null
  book_budget: number
  max_entries: number
  constant_count: number
  constant_tokens: number
  fit_count: number
  fit_tokens: number
  routed_out: number
  dropped: BudgetDroppedItem[]
  truncated: boolean
  /** 这个账号下已经有一个体检在算，这次没算；`summary` 写着稍后再看 */
  busy: boolean
  /** 一句中文结论，可以直接显示 */
  summary: string
}

export interface WorldbookInstanceOption {
  adapter_key: string
  instance_key: string
  display_name: string
}

export interface WorldbookChannelOption {
  chat_key: string
  channel_name: string
  channel_type: string
}

export interface WorldbookPresetOption {
  preset_id: number
  name: string
}

export interface WorldbookScopeOptions {
  instances: WorldbookInstanceOption[]
  channels: WorldbookChannelOption[]
  presets: WorldbookPresetOption[]
}

export interface WorldbookBookItem {
  id: number
  name: string
  description: string
  scope_kind: string
  adapter_key: string
  instance_key: string
  preset_id: number
  preset_name: string
  chat_key: string
  channel_name: string
  person_account: string
  enabled: boolean
  scan_depth: number
  budget_tokens: number
  inject_mode: WorldbookInjectMode
  active_scene: WorldbookActiveScene
  case_sensitive: boolean
  match_whole_words: boolean
  /** 扫描时每条消息前带上说话人名字（Bot 用人设名、群友用昵称） */
  scan_names: boolean
  /** 本书全部非常驻条目都参与语义触发 */
  vector_all: boolean
  /** 语义查询取最近几条非空消息 */
  vector_query_messages: number
  /** 本书每轮最多语义命中几条 */
  vector_max_entries: number
  /** 语义相似度下限 */
  vector_threshold: number
  entry_count: number
  quarantined_count: number
  update_time: string | null
  binding_missing: boolean
  /** 平台方给全局人设建、对当前租户只读开放的书：能看，不能改、删、导出、追加导入 */
  read_only?: boolean
}

export interface WorldbookBookDetail extends WorldbookBookItem {
  ext_keys: string[]
  /** 参与语义触发的条目数 */
  vector_participating: number
  /** 参与语义触发但还没生成向量的条目数 */
  vector_pending: number
}

export interface WorldbookBookListResponse {
  total: number
  items: WorldbookBookItem[]
}

export interface WorldbookInvalidKeyItem {
  key: string
  reason: string
}

export interface WorldbookEntryItem {
  id: number
  book_id: number
  uid: number
  comment: string
  keys: string[]
  secondary_keys: string[]
  selective_logic: string
  use_regex: boolean
  constant: boolean
  content: string
  order: number
  position: string
  depth: number
  scan_depth: number | null
  case_sensitive: boolean | null
  match_whole_words: boolean | null
  sticky: number | null
  cooldown: number | null
  delay: number | null
  enabled: boolean
  trusted: boolean
  /** 这条参与语义触发（向量激活） */
  vectorized: boolean
  /** 命中后不受全局与这本书的预算限制；只对宿主条目生效 */
  ignore_budget: boolean
  status: string
  quarantined_at: string | null
  quarantine_reason: string
  blocked_round_hits: number
  create_time: string | null
  update_time: string | null
  invalid_keys: WorldbookInvalidKeyItem[]
  slow_disabled_keys: string[]
  purpose: WorldbookEntryPurpose
  /** 含不支持的模板时的原因 */
  template_note: string
  suspected_trap: boolean
  trap_reason: string
  /** 这次请求的分析预算用完、还没分析的条目：用途与防盗标记稍后再取 */
  analysis_pending: boolean
}

/** 书详情每页条数（后端默认 50、最多 200） */
export const WORLDBOOK_ENTRY_PAGE_SIZE = 50

export interface WorldbookEntryListParams {
  /** 从 1 开始 */
  page?: number
  page_size?: number
  /** 按标题、关键词、正文搜（不区分大小写） */
  q?: string
}

export interface WorldbookEntryListResponse {
  items: WorldbookEntryItem[]
  total: number
  page: number
  page_size: number
}

export interface WorldbookQuarantineItem {
  entry_id: number
  book_id: number
  book_name: string
  comment: string
  quarantine_reason: string
  blocked_round_hits: number
  quarantined_at: string | null
}

export interface WorldbookQuarantineListResponse {
  total: number
  items: WorldbookQuarantineItem[]
}

export interface WorldbookDeleteResponse {
  deleted_entries: number
}

export interface WorldbookImportDetailItem {
  entry_key: string
  field: string
  kind: string
  detail: string
}

export interface WorldbookImportReport {
  book_id: number
  created: number
  notes: Record<string, number>
  details: WorldbookImportDetailItem[]
}

export interface WorldbookPersonNoteItem {
  book_id: number
  book_name: string
  entry_id: number
  comment: string
  content: string
  enabled: boolean
  status: string
  trusted: boolean
  update_time: string | null
}

export interface WorldbookPersonNoteListResponse {
  items: WorldbookPersonNoteItem[]
}

export interface WorldbookPersonNoteCreateRequest {
  adapter_key?: string
  instance_key: string
  person_account: string
  content: string
}

export interface WorldbookPersonNoteCreateResponse {
  book_id: number
  entry_id: number
  created_book: boolean
}

export interface WorldbookTriggerTestRequest {
  chat_key: string
  text?: string
  use_recent?: boolean
  preset_id?: number | null
}

export interface WorldbookTriggerBookItem {
  id: number
  name: string
  scope_kind: string
  trusted_entries: number
  untrusted_entries: number
}

export interface WorldbookTriggerHitItem {
  entry_id: number
  book_id: number
  comment: string
  reason_kind: string
  matched_key: string
  /** 语义命中的余弦相似度；其余原因为 null */
  score: number | null
  position: string
  order: number
  chars: number
  /** 这一轮分词系数下的 token 数 */
  tokens: number | null
  trusted: boolean
  /** 这一条是靠「忽略预算」进来的 */
  ignored_budget: boolean
}

export interface WorldbookTriggerDroppedItem {
  entry_id: number
  reason: WorldbookTriggerDropReason
  /** 这一轮分词系数下的 token 数 */
  tokens: number | null
  /**
   * 因预算（`global_tokens` / `book_tokens`）被跳过时，在它前面入选、占掉预算的条目 ID
   * （按入选顺序）；其它原因为空数组
   */
  ahead: number[]
}

export interface WorldbookTriggerGroups {
  after_persona: number[]
  before_history: number[]
  at_depth: Record<string, number[]>
}

export interface WorldbookTriggerRendered {
  after_persona: string
  before_history: string
  at_depth: Record<string, string>
}

export interface WorldbookTriggerTestResponse {
  books: WorldbookTriggerBookItem[]
  hits: WorldbookTriggerHitItem[]
  selected: number[]
  dropped: WorldbookTriggerDroppedItem[]
  groups: WorldbookTriggerGroups
  window_size: number
  timed_ignored: boolean
  gate_enabled: boolean
  rendered: WorldbookTriggerRendered
  budget: WorldbookBudgetInfo | null
  /** 入选条目合计 token */
  total_tokens: number
}

export interface WorldbookBookCreateRequest {
  name: string
  description?: string
  scope_kind: WorldbookScopeKind
  preset_id?: number
  chat_key?: string
  person_account?: string
  enabled?: boolean
  scan_depth?: number | null
  budget_tokens?: number
  inject_mode?: WorldbookInjectMode
  active_scene?: WorldbookActiveScene
  case_sensitive?: boolean
  match_whole_words?: boolean
  /** 扫描时每条消息前带上说话人名字；不传时按后端默认 */
  scan_names?: boolean
  vector_all?: boolean
  vector_query_messages?: number
  vector_max_entries?: number
  vector_threshold?: number
}

export interface WorldbookBookUpdateRequest {
  name?: string
  description?: string
  enabled?: boolean
  scan_depth?: number
  budget_tokens?: number
  inject_mode?: WorldbookInjectMode
  active_scene?: WorldbookActiveScene
  case_sensitive?: boolean
  match_whole_words?: boolean
  scan_names?: boolean
  vector_all?: boolean
  vector_query_messages?: number
  vector_max_entries?: number
  vector_threshold?: number
}

export interface WorldbookEntryCreateRequest {
  comment: string
  keys: string[]
  secondary_keys: string[]
  selective_logic: WorldbookSelectiveLogic
  use_regex: boolean
  constant: boolean
  content: string
  order: number
  position: WorldbookPosition
  depth: number
  scan_depth: number | null
  case_sensitive: boolean | null
  match_whole_words: boolean | null
  sticky: number | null
  cooldown: number | null
  delay: number | null
  enabled: boolean
  /** 这条参与语义触发（向量激活） */
  vectorized?: boolean
  trusted?: boolean
  /** 命中后不受全局与这本书的预算限制；只对宿主条目生效 */
  ignore_budget?: boolean
}

export interface WorldbookEntryUpdateRequest {
  comment?: string
  keys?: string[]
  secondary_keys?: string[]
  selective_logic?: WorldbookSelectiveLogic
  use_regex?: boolean
  constant?: boolean
  content?: string
  order?: number
  position?: WorldbookPosition
  depth?: number
  scan_depth?: number | null
  case_sensitive?: boolean | null
  match_whole_words?: boolean | null
  sticky?: number | null
  cooldown?: number | null
  delay?: number | null
  enabled?: boolean
  /** 这条参与语义触发（向量激活） */
  vectorized?: boolean
  trusted?: boolean
  /** 命中后不受全局与这本书的预算限制；只对宿主条目生效 */
  ignore_budget?: boolean
}

/** 图谱节点 / 边上的业务字段。后端按节点类型只填其中一部分，所以全部可选 */
export interface WorldbookGraphMetadata {
  scope_kind?: WorldbookScopeKind
  entry_count?: number
  constant_count?: number
  quarantined_count?: number
  inject_mode?: WorldbookInjectMode
  active_scene?: WorldbookActiveScene
  vector_all?: boolean
  read_only?: boolean
  instance_key?: string
  preset_id?: number
  preset_name?: string
  chat_key?: string
  channel_name?: string
  person_account?: string
  book_ids?: number[]
  book_id?: number
  uid?: number
  keys?: string[]
  constant?: boolean
  vectorized?: boolean
  ignore_budget?: boolean
  trusted?: boolean
  position?: WorldbookPosition
  injected?: number
  dropped?: number
  lookups?: number
  /** 最近只被预算挤掉、一次都没进去过 */
  starved?: boolean
  preview?: string
  /** 关键词引用是推导出来的，不是模型给的 */
  derived?: boolean
  /** 后端可能加字段；面板用字符串键读，不因为多一个字段就报类型错 */
  [key: string]: unknown
}

export interface WorldbookGraphNode {
  id: string
  /** `book` / `entry` / `preset` / `channel` / `person` */
  memory_type: string
  ref_id: number
  label: string
  subtitle: string
  /** `active` / `inactive` / `quarantined` */
  status: string
  cognitive_type: string | null
  weight: number
  size: number
  importance: number
  paragraph_id: number | null
  metadata: WorldbookGraphMetadata
}

export interface WorldbookGraphEdge {
  id: string
  source: string
  target: string
  /** `book_entry` / `book_binding` / `key_reference` */
  edge_type: string
  label: string
  weight: number
  strength: number
  status: string
  cognitive_type: string | null
  metadata: WorldbookGraphMetadata
}

export interface WorldbookGraphResponse {
  generated_at: string
  nodes: WorldbookGraphNode[]
  edges: WorldbookGraphEdge[]
  node_count: number
  edge_count: number
  books_total: number
  books_drawn: number
  entries_total: number
  entries_drawn: number
  /** 最近扫了多少轮提示词明细 */
  rounds_scanned: number
  books_truncated: boolean
  entries_truncated: boolean
  references_truncated: boolean
}

export interface WorldbookGraphParams extends WorldbookScopeParams {
  /** 只看一本书；不传是作用域内全部书 */
  book_id?: number
  /** 条目标题 / 关键词 / 正文包含；与条目列表搜索同义 */
  q?: string
  include_inactive?: boolean
  /** 条目节点上限；后端夹在 50 ~ 600 */
  limit?: number
}

export interface WorldbookBookListParams {
  scope_kind?: WorldbookScopeKind
  preset_id?: number
  chat_key?: string
  person?: string
  enabled?: boolean
  q?: string
  page?: number
  page_size?: number
}

export interface WorldbookQuarantineListParams {
  page?: number
  page_size?: number
}

/** 去掉空值；`0`、`false`、`'all'` 都保留 */
function compact<T extends object>(params: T): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '')
  )
}

/** 从 `Content-Disposition` 取文件名；取不到就用固定名字 */
function exportFilename(contentDisposition: unknown, bookId: number): string {
  if (typeof contentDisposition === 'string') {
    const match = contentDisposition.match(/filename="?([^";]+)"?/)
    if (match?.[1]) return match[1]
  }
  return `worldbook-${bookId}.json`
}

export const worldbookApi = {
  getScopeOptions: async (scope: WorldbookScopeParams): Promise<WorldbookScopeOptions> => {
    const response = await axios.get<WorldbookScopeOptions>('/worldbook/scope-options', {
      params: compact(scope),
    })
    return response.data
  },

  listBooks: async (
    params: WorldbookBookListParams & WorldbookScopeParams
  ): Promise<WorldbookBookListResponse> => {
    const response = await axios.get<WorldbookBookListResponse>('/worldbook/books', {
      params: compact(params),
    })
    return response.data
  },

  /** 世界书图谱：书、条目、绑定对象，以及条目间的关键词引用 */
  getGraph: async (params: WorldbookGraphParams = {}): Promise<WorldbookGraphResponse> => {
    const response = await axios.get<WorldbookGraphResponse>('/worldbook/graph', {
      params: compact(params),
    })
    return response.data
  },

  createBook: async (
    body: WorldbookBookCreateRequest,
    scope: WorldbookScopeParams
  ): Promise<WorldbookBookDetail> => {
    const response = await axios.post<WorldbookBookDetail>('/worldbook/books', body, {
      params: compact(scope),
    })
    return response.data
  },

  getBook: async (bookId: number): Promise<WorldbookBookDetail> => {
    const response = await axios.get<WorldbookBookDetail>(`/worldbook/books/${bookId}`)
    return response.data
  },

  /** 预算体检：常驻条目合计、按每轮预算能进几条、哪几条永远进不来 */
  getBookBudgetCheck: async (bookId: number): Promise<BookBudgetCheck> => {
    const response = await axios.get<BookBudgetCheck>(`/worldbook/books/${bookId}/budget-check`)
    return response.data
  },

  updateBook: async (
    bookId: number,
    body: WorldbookBookUpdateRequest
  ): Promise<WorldbookBookDetail> => {
    const response = await axios.put<WorldbookBookDetail>(`/worldbook/books/${bookId}`, body)
    return response.data
  },

  deleteBook: async (bookId: number): Promise<WorldbookDeleteResponse> => {
    const response = await axios.delete<WorldbookDeleteResponse>(`/worldbook/books/${bookId}`)
    return response.data
  },

  listEntries: async (
    bookId: number,
    params: WorldbookEntryListParams = {}
  ): Promise<WorldbookEntryListResponse> => {
    const response = await axios.get<WorldbookEntryListResponse>(
      `/worldbook/books/${bookId}/entries`,
      { params }
    )
    return response.data
  },

  createEntry: async (
    bookId: number,
    body: WorldbookEntryCreateRequest
  ): Promise<WorldbookEntryItem> => {
    const response = await axios.post<WorldbookEntryItem>(
      `/worldbook/books/${bookId}/entries`,
      body
    )
    return response.data
  },

  updateEntry: async (
    entryId: number,
    body: WorldbookEntryUpdateRequest
  ): Promise<WorldbookEntryItem> => {
    const response = await axios.put<WorldbookEntryItem>(
      `/worldbook/entries/${entryId}`,
      body
    )
    return response.data
  },

  /** 按 ID 取一条；供图谱详情里「在书里打开这一条」与地址参数 `?entry=` 用 */
  getEntry: async (entryId: number): Promise<WorldbookEntryItem> => {
    const response = await axios.get<WorldbookEntryItem>(`/worldbook/entries/${entryId}`)
    return response.data
  },

  deleteEntry: async (entryId: number): Promise<WorldbookDeleteResponse> => {
    const response = await axios.delete<WorldbookDeleteResponse>(
      `/worldbook/entries/${entryId}`
    )
    return response.data
  },

  entryAction: async (
    entryId: number,
    action: WorldbookEntryAction
  ): Promise<WorldbookEntryItem> => {
    const response = await axios.post<WorldbookEntryItem>(
      `/worldbook/entries/${entryId}/action`,
      { action }
    )
    return response.data
  },

  listQuarantine: async (
    params: WorldbookQuarantineListParams & WorldbookScopeParams
  ): Promise<WorldbookQuarantineListResponse> => {
    const response = await axios.get<WorldbookQuarantineListResponse>(
      '/worldbook/quarantine',
      { params: compact(params) }
    )
    return response.data
  },

  importNewBook: async (
    file: File,
    fields: WorldbookBookCreateRequest,
    scope: WorldbookScopeParams,
    rewriteNameVars = true
  ): Promise<WorldbookImportReport> => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('name', fields.name)
    formData.append('description', fields.description ?? '')
    formData.append('scope_kind', fields.scope_kind)
    formData.append('preset_id', String(fields.preset_id ?? 0))
    formData.append('chat_key', fields.chat_key ?? '')
    formData.append('person_account', fields.person_account ?? '')
    formData.append('enabled', String(fields.enabled ?? true))
    if (fields.scan_depth !== undefined && fields.scan_depth !== null) {
      formData.append('scan_depth', String(fields.scan_depth))
    }
    formData.append('budget_tokens', String(fields.budget_tokens ?? 0))
    formData.append('inject_mode', fields.inject_mode ?? 'both')
    formData.append('active_scene', fields.active_scene ?? 'both')
    formData.append('case_sensitive', String(fields.case_sensitive ?? false))
    formData.append('match_whole_words', String(fields.match_whole_words ?? false))
    formData.append('rewrite_name_vars', String(rewriteNameVars))
    formData.append('scan_names', String(fields.scan_names ?? true))
    const response = await axios.post<WorldbookImportReport>(
      '/worldbook/books/import',
      formData,
      {
        headers: { 'Content-Type': 'multipart/form-data' },
        params: compact(scope),
      }
    )
    return response.data
  },

  importIntoBook: async (
    bookId: number,
    file: File,
    rewriteNameVars = true
  ): Promise<WorldbookImportReport> => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('rewrite_name_vars', String(rewriteNameVars))
    const response = await axios.post<WorldbookImportReport>(
      `/worldbook/books/${bookId}/import`,
      formData,
      { headers: { 'Content-Type': 'multipart/form-data' } }
    )
    return response.data
  },

  exportBook: async (bookId: number): Promise<void> => {
    const response = await axios.get<Blob>(`/worldbook/books/${bookId}/export`, {
      responseType: 'blob',
    })
    const blob =
      response.data instanceof Blob
        ? response.data
        : new Blob([response.data], { type: 'application/json' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.setAttribute('download', exportFilename(response.headers['content-disposition'], bookId))
    document.body.appendChild(link)
    link.click()
    window.URL.revokeObjectURL(url)
    document.body.removeChild(link)
  },

  listPersonNotes: async (params: {
    instance_key?: string
    adapter_key?: string
    person_account: string
  }): Promise<WorldbookPersonNoteListResponse> => {
    const response = await axios.get<WorldbookPersonNoteListResponse>(
      '/worldbook/person-notes',
      { params: compact(params) }
    )
    return response.data
  },

  createPersonNote: async (
    body: WorldbookPersonNoteCreateRequest
  ): Promise<WorldbookPersonNoteCreateResponse> => {
    const response = await axios.post<WorldbookPersonNoteCreateResponse>(
      '/worldbook/person-notes',
      body
    )
    return response.data
  },

  testTrigger: async (
    body: WorldbookTriggerTestRequest
  ): Promise<WorldbookTriggerTestResponse> => {
    const response = await axios.post<WorldbookTriggerTestResponse>('/worldbook/test', body)
    return response.data
  },
}
