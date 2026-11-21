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
  type MemoryOrder,
  type MemoryPartition,
  type MemorySort,
  type MemoryStatusFilter,
  type OriginKind,
  type ParagraphFilterSelection,
  type ParagraphListParams,
  type QuarantineListParams,
} from '../../services/api/memory'

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
  /** 来源类型；空串 = 全部来源 */
  originKind: OriginKind | ''
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
  originKind: '',
  timeFrom: '',
  timeTo: '',
  sort: 'create_time',
  order: 'desc',
  page: 1,
}

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/

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
    originKind: includes(ORIGIN_KINDS, origin) ? origin : '',
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
  put('origin', filters.originKind)
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
    origin_kind: filters.originKind || undefined,
    time_from: localDayStart(filters.timeFrom),
    time_to: localDayEnd(filters.timeTo),
    q: filters.search.trim().slice(0, MEMORY_SEARCH_MAX_LENGTH) || undefined,
    sort: filters.sort,
    order: filters.order,
    page: filters.page,
    page_size: pageSize,
  }
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
    time_from: localDayStart(filters.timeFrom),
    time_to: localDayEnd(filters.timeTo),
    q: filters.search.trim().slice(0, MEMORY_SEARCH_MAX_LENGTH) || undefined,
    // 批量接口也认来源类型：「按筛选全选」选中的必须正是列表里看到的那一批
    origin_kind: filters.originKind || undefined,
  }
}
