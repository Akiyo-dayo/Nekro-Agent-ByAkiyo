import axios from './axios'

/**
 * 记忆管理接口
 *
 * 路径都相对 `/memory`，权限一律 `Role.Admin`。可见范围由服务端按当前身份算出：
 * 请求里的实例、频道、人设、人都只当**筛选条件**，不是授权依据，所以这里照常原样传。
 *
 * 查询参数统一做一次空值清理：空串与 `undefined` 不写进 URL，让后端的默认值生效。
 * `0` 与 `'all'` 是有效取值（`preset_id=0` 指默认人设），不能被当成空值丢掉。
 */

/** 实例作用域：平台方全局视图不带 `instance_key`；租户必须带上 */
export interface MemoryScopeParams {
  instance_key?: string
  /** 选了实例时一起带：同名实例可以出现在两个适配器下 */
  adapter_key?: string
  domain?: 'self' | 'group'
}

export const MEMORY_TYPES = ['paragraph', 'episode', 'relation', 'entity'] as const
export type MemoryType = (typeof MEMORY_TYPES)[number]

export const MEMORY_STATUSES = ['active', 'quarantined', 'deleted', 'pruned'] as const
export type MemoryStatus = (typeof MEMORY_STATUSES)[number]

/** 列表筛选多一档「全部」 */
export const MEMORY_STATUS_FILTERS = [...MEMORY_STATUSES, 'all'] as const
export type MemoryStatusFilter = (typeof MEMORY_STATUS_FILTERS)[number]

/** 明细里还会出现这两种：真删之后的残留引用、以及无法归类 */
export const MEMORY_DETAIL_STATUSES = [...MEMORY_STATUSES, 'hard_deleted', 'unknown'] as const
export type MemoryDetailStatus = (typeof MEMORY_DETAIL_STATUSES)[number]

export const PARTITIONS = ['all', 'domain', 'workspace'] as const
export type MemoryPartition = (typeof PARTITIONS)[number]

export const COGNITIVE_TYPES = ['episodic', 'semantic'] as const
export type CognitiveType = (typeof COGNITIVE_TYPES)[number]

export const KNOWLEDGE_TYPES = [
  'conversation',
  'preference',
  'fact',
  'experience',
  'decision',
  'emotion',
] as const
export type KnowledgeType = (typeof KNOWLEDGE_TYPES)[number]

export const SORTS = ['create_time', 'update_time', 'blocked_round_hits'] as const
export type MemorySort = (typeof SORTS)[number]

export const ORDERS = ['desc', 'asc'] as const
export type MemoryOrder = (typeof ORDERS)[number]

/**
 * 段落的来源类型（对应后端 `OriginKind`）
 *
 * 空串是「全部」这一档，所以列表与筛选都把它单独拿出来，不混进数组里。
 */
export const ORIGIN_KINDS = [
  'message',
  'consolidation',
  'task',
  'manual',
  'plugin',
  'affection',
] as const
export type OriginKind = (typeof ORIGIN_KINDS)[number]

export const PARAGRAPH_ACTIONS = [
  'freeze',
  'unfreeze',
  'protect',
  'unprotect',
  'reinforce',
  'demote',
  'quarantine',
  'restore',
] as const
export type ParagraphAction = (typeof PARAGRAPH_ACTIONS)[number]

/** episode 没有冻结 / 保护 / 调权这些列，只有隔离与放回 */
export const EPISODE_ACTIONS = ['quarantine', 'restore'] as const
export type EpisodeAction = (typeof EPISODE_ACTIONS)[number]

export type MemoryItemAction = ParagraphAction | EpisodeAction

export const BULK_ACTIONS = [
  'delete',
  'hard_delete',
  'quarantine',
  'restore',
  'freeze',
  'unfreeze',
] as const
export type BulkAction = (typeof BULK_ACTIONS)[number]

export interface MemoryInstanceOption {
  adapter_key: string
  instance_key: string
  display_name: string
}

export interface MemoryChannelOption {
  chat_key: string
  channel_name: string
  channel_type: string
}

export interface MemoryPresetOption {
  preset_id: number
  name: string
}

export interface ScopeOptionsResponse {
  instances: MemoryInstanceOption[]
  channels: MemoryChannelOption[]
  presets: MemoryPresetOption[]
}

export interface ParagraphListItem {
  id: number
  workspace_id: number
  adapter_key: string
  instance_key: string
  preset_id: number
  origin_chat_key: string | null
  origin_chat_type: string
  /** 来源类型；老行上可能为空，调用方按「未知来源」处理 */
  origin_kind: string | null
  /** 来源引用（里程碑那类会指回产生它的对象）；空 = 没有 */
  origin_ref: string | null
  summary: string
  content: string
  cognitive_type: string
  knowledge_type: string
  status: string
  is_frozen: boolean
  is_protected: boolean
  blocked_round_hits: number
  quarantined_at: string | null
  quarantine_reason: string
  create_time: string | null
  update_time: string | null
  person_accounts: string[]
}

export interface ParagraphListResponse {
  total: number
  items: ParagraphListItem[]
}

export interface EpisodeListItem {
  id: number
  workspace_id: number
  adapter_key: string
  instance_key: string
  preset_id: number
  origin_chat_key: string | null
  origin_chat_type: string
  title: string
  narrative_summary: string
  status: string
  blocked_round_hits: number
  quarantined_at: string | null
  quarantine_reason: string
  time_start: string | null
  time_end: string | null
  create_time: string | null
  update_time: string | null
}

export interface EpisodeListResponse {
  total: number
  items: EpisodeListItem[]
}

export interface QuarantineListItem {
  memory_type: string
  id: number
  title_or_summary: string
  origin_chat_key: string | null
  quarantined_at: string | null
  quarantine_reason: string
  blocked_round_hits: number
}

export interface QuarantineListResponse {
  total: number
  items: QuarantineListItem[]
}

export interface MemoryPerson {
  account: string
  display_name: string
}

/**
 * 单条详情
 *
 * 四种记忆类型的列不一样，服务端只固定三列共有的信息，其余原样透传；
 * 这里把段落与 episode 会用到的列都写成可选项，读取时按 `memory_type` 收窄。
 */
export interface MemoryItemDetail {
  id: number
  memory_type: MemoryType
  status: MemoryDetailStatus
  /** 这条记忆怎么来的，见 `OriginKind`；老数据可能为空 */
  origin_kind?: string | null
  /** 与 `origin_kind` 配套的来源标识（里程碑存的是事件编号） */
  origin_ref?: string | null
  content?: string
  summary?: string
  cognitive_type?: string
  knowledge_type?: string
  origin_chat_key?: string | null
  origin_channel_name?: string
  is_frozen?: boolean
  is_protected?: boolean
  base_weight?: number
  blocked_round_hits?: number
  quarantined_at?: string | null
  quarantine_reason?: string
  persons?: MemoryPerson[]
  episode?: { id: number; title: string } | null
  reinforcement_count?: number
  workspace_id?: number
  instance_key?: string
  adapter_key?: string
  preset_id?: number
  title?: string
  narrative_summary?: string
  time_start?: string | null
  time_end?: string | null
}

export interface MemoryItemUpdateBody {
  content?: string
  summary?: string
  person_accounts?: string[]
  title?: string
  narrative_summary?: string
}

export interface MemoryTraceMessage {
  db_id: number
  message_id: string
  sender_name: string
  send_timestamp: number
  content_preview: string
}

export interface MemoryTraceResponse {
  workspace_id: number
  memory_type: string
  memory_id: number
  origin_kind: string | null
  origin_ref: string | null
  origin_chat_key: string | null
  anchor_msg_id: string | null
  anchor_msg_id_start: string | null
  anchor_msg_id_end: string | null
  anchor_timestamp_start: number | null
  anchor_timestamp_end: number | null
  cross_channel: boolean
  messages: MemoryTraceMessage[]
}

export interface HardDeleteReport {
  paragraphs: number
  persons: number
  relations: number
  episodes: number
  entities: number
  reinforcement_logs: number
  vectors_failed: boolean
}

export interface BulkResult {
  matched: number
  affected: number
  failed_ids: number[]
  cascade: HardDeleteReport | null
}

/** 按筛选批量执行时的条件，键名与列表端点的查询参数一致 */
export interface ParagraphFilterSelection {
  partition?: MemoryPartition
  chat_key?: string | null
  preset_id?: number | null
  person?: string
  status?: MemoryStatusFilter
  frozen?: boolean | null
  protected?: boolean | null
  cognitive_type?: string | null
  knowledge_type?: string | null
  time_from?: string | null
  time_to?: string | null
  q?: string | null
  origin_kind?: OriginKind | null
  sort?: MemorySort
  order?: MemoryOrder
  page?: number
  page_size?: number
}

export interface BulkSelection {
  ids?: number[]
  filter?: ParagraphFilterSelection
}

export interface BulkParagraphsBody {
  selection: BulkSelection
  action: BulkAction
  dry_run?: boolean
  expected_count?: number
}

export interface SummaryListEntry {
  chat_key: string
  preset_id: number
  adapter_key: string
  instance_key: string
  id: number
  version: number
  content: string
  source: string
  create_time: string | null
  version_count: number
  paused: boolean
}

export interface SummaryListResponse {
  total: number
  items: SummaryListEntry[]
}

export interface SummaryVersionItem {
  id: number
  version: number
  content: string
  source: string
  source_version: number | null
  edited_by: string
  create_time: string | null
}

export interface SummaryVersionsResponse {
  chat_key: string
  preset_id: number
  paused: boolean
  paused_by: string
  paused_at: string | null
  versions: SummaryVersionItem[]
}

export interface SummaryPauseResponse {
  paused: boolean
  paused_by: string
  paused_at: string | null
}

export interface PromptRoundListItem {
  id: number
  chat_key: string
  preset_id: number
  outcome: string
  render_count: number
  memory_state: string
  memory_items_count: number
  memory_block_chars: number
  summary_state: string
  summary_version: number | null
  summary_chars: number
  affection_shown: boolean
  affection_chars: number
  affection_people: number
  /** 这一轮注入的关系里程碑段落；空数组 = 没有 */
  affection_milestone_ids?: number[]
  /** 世界书一栏：注入状态与真正注入的条目数、字数 */
  worldbook_state: string
  worldbook_count: number
  worldbook_chars: number
  /**
   * 酒馆卡与高级槽位的本轮状态（门控结论、后置指令与预填的字数等）
   *
   * 后端是 `dict[str, Any]`，键随卡片版本与槽位开关而变，这里不猜形状；要用某个键的地方
   * 自己收窄。老行上是空对象。
   */
  tavern_items?: Record<string, unknown>
  started_at: string | null
  finished_at: string | null
}

export interface PromptRoundListResponse {
  total: number
  items: PromptRoundListItem[]
}

export interface PromptRoundMemoryItem {
  type: string
  id: number
  status: string
  preview: string
  chars: number
  renders: number
}

/** 明细里的世界书条目；`type = 'worldbook'` 是注入的，`'worldbook_dropped'` 是被预算跳过的 */
export interface PromptRoundWorldbookItem {
  type: string
  id: number
  /** 租户看不到的书（`status = 'hidden'`）没有这个键 */
  book_id?: number
  chars: number
  position?: string
  trusted?: boolean
  renders?: number
  /** 只有 `worldbook_dropped` 有：`global_chars` / `book_chars` / `max_entries` */
  reason?: string
  /** active / disabled / quarantined / hard_deleted / hidden */
  status: string
  book_name: string
  comment: string
}

export interface PromptRoundDetail {
  id: number
  round_id: string
  chat_key: string
  adapter_key: string
  instance_key: string
  preset_id: number
  workspace_id: number
  outcome: string
  render_count: number
  memory_state: string
  memory_items: PromptRoundMemoryItem[]
  memory_block_chars: number
  cooldown_skipped: number
  summary_state: string
  summary_id: number | null
  summary_version: number | null
  summary_chars: number
  affection_shown: boolean
  affection_chars: number
  affection_people: number
  /** 这一轮好感块里注入的关系里程碑段落，同样能点开记忆条目详情 */
  affection_milestone_ids: number[]
  worldbook_state: string
  worldbook_items: PromptRoundWorldbookItem[]
  worldbook_chars: number
  /** 酒馆卡与高级槽位的本轮状态；形状同列表行，见上面的说明 */
  tavern_items?: Record<string, unknown>
  started_at: string | null
  finished_at: string | null
  summary: Record<string, unknown>
}

export interface ParagraphListParams {
  partition?: MemoryPartition
  chat_key?: string
  preset_id?: number | null
  person?: string
  status?: MemoryStatusFilter
  frozen?: boolean | null
  protected?: boolean | null
  cognitive_type?: string
  knowledge_type?: string
  /** 来源类型；不传 = 全部 */
  origin_kind?: OriginKind
  time_from?: string
  time_to?: string
  q?: string
  sort?: MemorySort
  order?: MemoryOrder
  page?: number
  page_size?: number
}

/** episode 不接受按人 / 认知类型 / 知识类型筛选，查询类型里索性不放这三项 */
export interface EpisodeListParams {
  partition?: MemoryPartition
  chat_key?: string
  preset_id?: number | null
  status?: MemoryStatusFilter
  time_from?: string
  time_to?: string
  q?: string
  sort?: MemorySort
  order?: MemoryOrder
  page?: number
  page_size?: number
}

export interface QuarantineListParams {
  partition?: MemoryPartition
  chat_key?: string
  preset_id?: number | null
  page?: number
  page_size?: number
}

export interface SummaryListParams {
  chat_key?: string
  preset_id?: number | null
  page?: number
  page_size?: number
}

export interface PromptRoundListParams {
  chat_key: string
  page?: number
  page_size?: number
}

/** 去掉空值；`0`、`false`、`'all'` 都保留 */
function compact<T extends object>(params: T): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '')
  )
}

export const memoryApi = {
  getScopeOptions: async (scope: MemoryScopeParams): Promise<ScopeOptionsResponse> => {
    const response = await axios.get<ScopeOptionsResponse>('/memory/scope-options', {
      params: compact(scope),
    })
    return response.data
  },

  listParagraphs: async (
    params: ParagraphListParams & MemoryScopeParams
  ): Promise<ParagraphListResponse> => {
    const response = await axios.get<ParagraphListResponse>('/memory/paragraphs', {
      params: compact(params),
    })
    return response.data
  },

  listEpisodes: async (
    params: EpisodeListParams & MemoryScopeParams
  ): Promise<EpisodeListResponse> => {
    const response = await axios.get<EpisodeListResponse>('/memory/episodes', {
      params: compact(params),
    })
    return response.data
  },

  listQuarantine: async (
    params: QuarantineListParams & MemoryScopeParams
  ): Promise<QuarantineListResponse> => {
    const response = await axios.get<QuarantineListResponse>('/memory/quarantine', {
      params: compact(params),
    })
    return response.data
  },

  getItem: async (memoryType: MemoryType, id: number): Promise<MemoryItemDetail> => {
    const response = await axios.get<MemoryItemDetail>(
      `/memory/items/${encodeURIComponent(memoryType)}/${id}`
    )
    return response.data
  },

  traceParagraph: async (id: number): Promise<MemoryTraceResponse> => {
    const response = await axios.get<MemoryTraceResponse>(`/memory/items/paragraph/${id}/trace`)
    return response.data
  },

  updateItem: async (
    memoryType: MemoryType,
    id: number,
    body: MemoryItemUpdateBody
  ): Promise<MemoryItemDetail> => {
    const response = await axios.put<MemoryItemDetail>(
      `/memory/items/${encodeURIComponent(memoryType)}/${id}`,
      body
    )
    return response.data
  },

  actOnItem: async (
    memoryType: MemoryType,
    id: number,
    action: MemoryItemAction
  ): Promise<{ ok: boolean }> => {
    const response = await axios.post<{ ok: boolean }>(
      `/memory/items/${encodeURIComponent(memoryType)}/${id}/actions`,
      { action }
    )
    return response.data
  },

  deleteItem: async (
    memoryType: MemoryType,
    id: number,
    hard: boolean
  ): Promise<HardDeleteReport> => {
    const response = await axios.delete<HardDeleteReport>(
      `/memory/items/${encodeURIComponent(memoryType)}/${id}`,
      { params: { hard } }
    )
    return response.data
  },

  bulkParagraphs: async (
    body: BulkParagraphsBody,
    scope: MemoryScopeParams
  ): Promise<BulkResult> => {
    const response = await axios.post<BulkResult>('/memory/paragraphs/bulk', body, {
      params: compact(scope),
    })
    return response.data
  },

  listSummaries: async (
    params: SummaryListParams & MemoryScopeParams
  ): Promise<SummaryListResponse> => {
    const response = await axios.get<SummaryListResponse>('/memory/summaries', {
      params: compact(params),
    })
    return response.data
  },

  getSummaryVersions: async (
    chatKey: string,
    presetId: number
  ): Promise<SummaryVersionsResponse> => {
    const response = await axios.get<SummaryVersionsResponse>(
      `/memory/summaries/${encodeURIComponent(chatKey)}/${presetId}`
    )
    return response.data
  },

  editSummary: async (
    chatKey: string,
    presetId: number,
    body: { content: string; base_version: number }
  ): Promise<SummaryVersionItem> => {
    const response = await axios.put<SummaryVersionItem>(
      `/memory/summaries/${encodeURIComponent(chatKey)}/${presetId}`,
      body
    )
    return response.data
  },

  rollbackSummary: async (
    chatKey: string,
    presetId: number,
    toVersion?: number
  ): Promise<SummaryVersionItem> => {
    const response = await axios.post<SummaryVersionItem>(
      `/memory/summaries/${encodeURIComponent(chatKey)}/${presetId}/rollback`,
      toVersion === undefined ? {} : { to_version: toVersion }
    )
    return response.data
  },

  pauseSummary: async (
    chatKey: string,
    presetId: number,
    paused: boolean
  ): Promise<SummaryPauseResponse> => {
    const response = await axios.post<SummaryPauseResponse>(
      `/memory/summaries/${encodeURIComponent(chatKey)}/${presetId}/pause`,
      { paused }
    )
    return response.data
  },

  listPromptRounds: async (
    params: PromptRoundListParams & MemoryScopeParams
  ): Promise<PromptRoundListResponse> => {
    const response = await axios.get<PromptRoundListResponse>('/memory/prompt-rounds', {
      params: compact(params),
    })
    return response.data
  },

  getPromptRound: async (id: number): Promise<PromptRoundDetail> => {
    const response = await axios.get<PromptRoundDetail>(`/memory/prompt-rounds/${id}`)
    return response.data
  },
}
