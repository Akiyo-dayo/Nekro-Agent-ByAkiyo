import {
  COGNITIVE_TYPES,
  KNOWLEDGE_TYPES,
  MEMORY_STATUS_FILTERS,
  ORDERS,
  ORIGIN_KINDS,
  PARTITIONS,
  SORTS,
  type CognitiveType,
  type EpisodeListParams,
  type KnowledgeType,
  type MemoryGraphParams,
  type MemoryOrder,
  type MemoryPartition,
  type MemorySort,
  type MemoryStatusFilter,
  type OriginKind,
  type ParagraphFilterSelection,
  type ParagraphListParams,
  type QuarantineListParams,
} from '../../services/api/memory'
import type { MemoryPageTab } from '../../router/routes'

/**
 * 记忆页的筛选状态与 URL 的互转
 *
 * 规则与全站一致：筛选属于「刷新后应当还在」的状态，所以进 query；路径只表达
 * 当前在哪个标签。空值与默认值不写进 URL，非法值在解析时回落默认。
 */

export const MEMORY_SEARCH_MAX_LENGTH = 64

export const MEMORY_PAGE_SIZE_OPTIONS = [20, 50, 100] as const
export const DEFAULT_MEMORY_PAGE_SIZE = 20

export const MEMORY_DOMAINS = ['self', 'group'] as const
export type MemoryDomain = (typeof MEMORY_DOMAINS)[number]

export const MEMORY_KINDS = ['paragraph', 'episode'] as const
export type MemoryKind = (typeof MEMORY_KINDS)[number]

/** 敏感筛选：空串看全部，`only` 只看敏感，`exclude` 不含敏感 */
export const MEMORY_SENSITIVE_FILTERS = ['only', 'exclude'] as const
export type MemorySensitiveFilter = '' | (typeof MEMORY_SENSITIVE_FILTERS)[number]

export interface MemoryFilterState {
  /** 发给后端时叫 `q` */
  search: string
  status: MemoryStatusFilter
  chatKey: string
  presetId: number | null
  partition: MemoryPartition
  domain: MemoryDomain
  person: string
  cognitiveType: CognitiveType | ''
  knowledgeType: KnowledgeType | ''
  sensitive: MemorySensitiveFilter
  /** 来源类型；空串 = 全部来源 */
  originKind: OriginKind | ''
  /**
   * 所属事件：空串不筛，`none` = 没归入任何事件，数字串 = 事件 ID
   *
   * 「按事件分组」视图里点「在列表里看这一组」时带上；筛选栏里不单独画控件，由列表上方的
   * 一枚可删除的标签表示。
   */
  episode: string
  timeFrom: string
  timeTo: string
  sort: MemorySort
  order: MemoryOrder
  page: number
}

export const DEFAULT_MEMORY_FILTERS: MemoryFilterState = {
  search: '',
  status: 'active',
  chatKey: '',
  presetId: null,
  partition: 'all',
  domain: 'self',
  person: '',
  cognitiveType: '',
  knowledgeType: '',
  sensitive: '',
  originKind: '',
  episode: '',
  timeFrom: '',
  timeTo: '',
  sort: 'create_time',
  order: 'desc',
  page: 1,
}

/**
 * 每个标签页认哪些筛选项
 *
 * 切标签时只把这些字段带过去，其余回落默认：隔离区不认「知识类型」「认知类型」，
 * 带过去只会让列表一直是空的。三张表与各标签页筛选栏里的 `fields` 必须一致。
 */
export const MEMORY_TAB_FILTER_FIELDS: Record<
  MemoryPageTab,
  readonly (keyof MemoryFilterState)[]
> = {
  graph: ['chatKey', 'presetId', 'partition', 'domain', 'person', 'timeFrom', 'timeTo'],
  items: [
    'search',
    'status',
    'chatKey',
    'presetId',
    'partition',
    'domain',
    'person',
    'cognitiveType',
    'knowledgeType',
    'sensitive',
    'originKind',
    'timeFrom',
    'timeTo',
    'sort',
    'order',
  ],
  quarantine: ['chatKey', 'presetId', 'partition'],
}

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/

/** 事件筛选只认 `none` 与正整数；别的回落成不筛，不把非法值交给后端换一个 400 */
export function parseEpisodeFilter(value: string | null): string {
  if (value === null) return ''
  const text = value.trim().toLowerCase()
  if (text === 'none') return 'none'
  return /^[1-9][0-9]{0,17}$/.test(text) ? text : ''
}

const includes = <T extends string>(values: readonly T[], value: string | null): value is T =>
  value !== null && (values as readonly string[]).includes(value)

/** 日期输入框只接受真实存在的 `YYYY-MM-DD`：`2024-02-31` 会被回退成空串 */
function parseDateInput(value: string | null): string {
  if (!value || !DATE_PATTERN.test(value)) return ''
  const date = new Date(`${value}T00:00:00`)
  if (Number.isNaN(date.getTime())) return ''
  const roundTrip = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(
    date.getDate()
  ).padStart(2, '0')}`
  return roundTrip === value ? value : ''
}

function parsePresetId(value: string | null): number | null {
  if (value === null || value.trim() === '') return null
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null
}

function parsePage(value: string | null): number {
  if (value === null || value.trim() === '') return 1
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : 1
}

export const isMemoryKind = (value: string | null | undefined): value is MemoryKind =>
  includes(MEMORY_KINDS, value ?? null)

export function parsePageSize(value: string | null): number {
  const parsed = Number(value)
  return (MEMORY_PAGE_SIZE_OPTIONS as readonly number[]).includes(parsed)
    ? parsed
    : DEFAULT_MEMORY_PAGE_SIZE
}

/**
 * 从 URL 读筛选状态
 *
 * 所有枚举都逐项核对，数字逐项校验；任何一项不合法就回落到默认值，
 * 而不是把非法值原样交给后端换一个 400。
 */
export function filtersFromSearchParams(searchParams: URLSearchParams): MemoryFilterState {
  const status = searchParams.get('status')
  const partition = searchParams.get('partition')
  const domain = searchParams.get('domain')
  const cognitive = searchParams.get('cognitive')
  const knowledge = searchParams.get('knowledge')
  const sensitive = searchParams.get('sensitive')
  const origin = searchParams.get('origin')
  const sort = searchParams.get('sort')
  const order = searchParams.get('order')

  return {
    search: (searchParams.get('search') ?? '').slice(0, MEMORY_SEARCH_MAX_LENGTH),
    status: includes(MEMORY_STATUS_FILTERS, status) ? status : 'active',
    chatKey: searchParams.get('chat') ?? '',
    presetId: parsePresetId(searchParams.get('preset')),
    partition: includes(PARTITIONS, partition) ? partition : 'all',
    domain: includes(MEMORY_DOMAINS, domain) ? domain : 'self',
    person: searchParams.get('person') ?? '',
    cognitiveType: includes(COGNITIVE_TYPES, cognitive) ? cognitive : '',
    knowledgeType: includes(KNOWLEDGE_TYPES, knowledge) ? knowledge : '',
    sensitive: includes(MEMORY_SENSITIVE_FILTERS, sensitive) ? sensitive : '',
    originKind: includes(ORIGIN_KINDS, origin) ? origin : '',
    episode: parseEpisodeFilter(searchParams.get('episode')),
    timeFrom: parseDateInput(searchParams.get('from')),
    timeTo: parseDateInput(searchParams.get('to')),
    sort: includes(SORTS, sort) ? sort : 'create_time',
    order: includes(ORDERS, order) ? order : 'desc',
    page: parsePage(searchParams.get('page')),
  }
}

/** 写回 URL；空值与默认值不写 */
export function filtersToSearchParams(filters: MemoryFilterState): URLSearchParams {
  const params = new URLSearchParams()
  const put = (key: string, value: string) => {
    if (value) params.set(key, value)
  }

  put('search', filters.search.trim().slice(0, MEMORY_SEARCH_MAX_LENGTH))
  if (filters.status !== DEFAULT_MEMORY_FILTERS.status) put('status', filters.status)
  put('chat', filters.chatKey)
  if (filters.presetId !== null) put('preset', String(filters.presetId))
  if (filters.partition !== DEFAULT_MEMORY_FILTERS.partition) put('partition', filters.partition)
  if (filters.domain !== DEFAULT_MEMORY_FILTERS.domain) put('domain', filters.domain)
  put('person', filters.person.trim())
  put('cognitive', filters.cognitiveType)
  put('knowledge', filters.knowledgeType)
  put('sensitive', filters.sensitive)
  put('origin', filters.originKind)
  put('episode', parseEpisodeFilter(filters.episode))
  put('from', filters.timeFrom)
  put('to', filters.timeTo)
  if (filters.sort !== DEFAULT_MEMORY_FILTERS.sort) put('sort', filters.sort)
  if (filters.order !== DEFAULT_MEMORY_FILTERS.order) put('order', filters.order)
  if (filters.page !== DEFAULT_MEMORY_FILTERS.page) put('page', String(filters.page))

  return params
}

/**
 * 日期框的值是「本地某一天」，后端要的是时间戳
 *
 * 起始取当天本地 0 点，结束取次日本地 0 点（左闭右开），这样「今天」不会因为
 * 时区问题漏掉当天后半段的记忆。
 */
function localDayStart(value: string): string | undefined {
  const normalized = parseDateInput(value)
  if (!normalized) return undefined
  const date = new Date(`${normalized}T00:00:00`)
  return date.toISOString()
}

function localDayEnd(value: string): string | undefined {
  const normalized = parseDateInput(value)
  if (!normalized) return undefined
  const date = new Date(`${normalized}T00:00:00`)
  date.setDate(date.getDate() + 1)
  return date.toISOString()
}

/** 敏感筛选转成后端的布尔参数：不筛时不传，避免 `false` 被当成「只看非敏感」 */
function sensitiveParam(value: MemorySensitiveFilter): boolean | undefined {
  if (value === 'only') return true
  if (value === 'exclude') return false
  return undefined
}

/** 段落列表的查询参数（不含作用域，作用域由调用方拼上） */
export function filtersToParagraphQuery(
  filters: MemoryFilterState,
  pageSize: number
): ParagraphListParams {
  return {
    partition: filters.partition,
    chat_key: filters.chatKey || undefined,
    preset_id: filters.presetId ?? undefined,
    person: filters.person.trim() || undefined,
    status: filters.status,
    cognitive_type: filters.cognitiveType || undefined,
    knowledge_type: filters.knowledgeType || undefined,
    sensitive: sensitiveParam(filters.sensitive),
    origin_kind: filters.originKind || undefined,
    episode: parseEpisodeFilter(filters.episode) || undefined,
    time_from: localDayStart(filters.timeFrom),
    time_to: localDayEnd(filters.timeTo),
    q: filters.search.trim().slice(0, MEMORY_SEARCH_MAX_LENGTH) || undefined,
    sort: filters.sort,
    order: filters.order,
    page: filters.page,
    page_size: pageSize,
  }
}

/**
 * 分组视图（按人 / 按事件）的查询参数：与段落列表同一套筛选，不带排序与分页
 *
 * 分组本身就是「按哪一维看」，按人分组时人这一项、按事件分组时事件这一项都不该再当筛选
 * 带上——否则分出来永远只有一组。
 */
export function filtersToGroupQuery(
  filters: MemoryFilterState,
  by: 'person' | 'topic'
): Omit<ParagraphListParams, 'sort' | 'order' | 'page' | 'page_size'> {
  const {
    sort: _sort,
    order: _order,
    page: _page,
    page_size: _pageSize,
    ...rest
  } = filtersToParagraphQuery(filters, DEFAULT_MEMORY_PAGE_SIZE)
  return by === 'person' ? { ...rest, person: undefined } : { ...rest, episode: undefined }
}

/** episode 列表不接受按人 / 认知类型 / 知识类型筛选 */
export function filtersToEpisodeQuery(
  filters: MemoryFilterState,
  pageSize: number
): EpisodeListParams {
  return {
    partition: filters.partition,
    chat_key: filters.chatKey || undefined,
    preset_id: filters.presetId ?? undefined,
    status: filters.status,
    time_from: localDayStart(filters.timeFrom),
    time_to: localDayEnd(filters.timeTo),
    q: filters.search.trim().slice(0, MEMORY_SEARCH_MAX_LENGTH) || undefined,
    sort: filters.sort,
    order: filters.order,
    page: filters.page,
    page_size: pageSize,
  }
}

export function filtersToQuarantineQuery(
  filters: MemoryFilterState,
  pageSize: number
): QuarantineListParams {
  return {
    partition: filters.partition,
    chat_key: filters.chatKey || undefined,
    preset_id: filters.presetId ?? undefined,
    page: filters.page,
    page_size: pageSize,
  }
}

/** 批量操作里「当前筛选的全部」那一档；键名与后端一致 */
export function filtersToParagraphSelection(
  filters: MemoryFilterState
): ParagraphFilterSelection {
  return {
    partition: filters.partition,
    chat_key: filters.chatKey || undefined,
    preset_id: filters.presetId ?? undefined,
    person: filters.person.trim() || undefined,
    status: filters.status,
    cognitive_type: filters.cognitiveType || undefined,
    knowledge_type: filters.knowledgeType || undefined,
    sensitive: sensitiveParam(filters.sensitive),
    time_from: localDayStart(filters.timeFrom),
    time_to: localDayEnd(filters.timeTo),
    q: filters.search.trim().slice(0, MEMORY_SEARCH_MAX_LENGTH) || undefined,
    // 批量接口也认来源类型：「按筛选全选」选中的必须正是列表里看到的那一批
    origin_kind: filters.originKind || undefined,
    // 同上：从「按事件分组」点进来的列表只看那一组，全选也只能选那一组
    episode: parseEpisodeFilter(filters.episode) || undefined,
  }
}

/** 图谱的查询参数：分区、频道、人设、人、时间与列表同义；失活与否由图谱自己的开关决定 */
export function filtersToGraphQuery(
  filters: MemoryFilterState,
  includeInactive: boolean
): MemoryGraphParams {
  return {
    partition: filters.partition,
    chat_key: filters.chatKey || undefined,
    preset_id: filters.presetId ?? undefined,
    person: filters.person.trim() || undefined,
    include_inactive: includeInactive || undefined,
    time_from: localDayStart(filters.timeFrom),
    time_to: localDayEnd(filters.timeTo),
  }
}
