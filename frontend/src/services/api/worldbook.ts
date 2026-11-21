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
] as const
export type WorldbookImportNoteKind = (typeof WORLDBOOK_IMPORT_NOTE_KINDS)[number]

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
export const WORLDBOOK_BUDGET_MAX = 100_000
export const WORLDBOOK_ORDER_MIN = -100_000
export const WORLDBOOK_ORDER_MAX = 100_000
export const WORLDBOOK_IMPORT_MAX_BYTES = 32 * 1024 * 1024
export const WORLDBOOK_PERSON_NOTE_MAX_LENGTH = 500

/** 触发测试命中的原因：常驻，或某个关键词 */
export type WorldbookTriggerReasonKind = 'constant' | 'key'
/** 被预算跳过的原因 */
export type WorldbookTriggerDropReason = 'global_chars' | 'book_chars' | 'max_entries'

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
  budget_chars: number
  case_sensitive: boolean
  match_whole_words: boolean
  entry_count: number
  quarantined_count: number
  update_time: string | null
  binding_missing: boolean
  /** 平台方给全局人设建、对当前租户只读开放的书：能看，不能改、删、导出、追加导入 */
  read_only?: boolean
}

export interface WorldbookBookDetail extends WorldbookBookItem {
  ext_keys: string[]
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
  status: string
  quarantined_at: string | null
  quarantine_reason: string
  blocked_round_hits: number
  create_time: string | null
  update_time: string | null
  invalid_keys: WorldbookInvalidKeyItem[]
  slow_disabled_keys: string[]
}

export interface WorldbookEntryListResponse {
  items: WorldbookEntryItem[]
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
  position: string
  order: number
  chars: number
  trusted: boolean
}

export interface WorldbookTriggerDroppedItem {
  entry_id: number
  reason: string
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
  budget_chars?: number
  case_sensitive?: boolean
  match_whole_words?: boolean
}

export interface WorldbookBookUpdateRequest {
  name?: string
  description?: string
  enabled?: boolean
  scan_depth?: number
  budget_chars?: number
  case_sensitive?: boolean
  match_whole_words?: boolean
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
  trusted?: boolean
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
  trusted?: boolean
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

  listEntries: async (bookId: number): Promise<WorldbookEntryListResponse> => {
    const response = await axios.get<WorldbookEntryListResponse>(
      `/worldbook/books/${bookId}/entries`
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
    scope: WorldbookScopeParams
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
    formData.append('budget_chars', String(fields.budget_chars ?? 0))
    formData.append('case_sensitive', String(fields.case_sensitive ?? false))
    formData.append('match_whole_words', String(fields.match_whole_words ?? false))
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

  importIntoBook: async (bookId: number, file: File): Promise<WorldbookImportReport> => {
    const formData = new FormData()
    formData.append('file', file)
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
