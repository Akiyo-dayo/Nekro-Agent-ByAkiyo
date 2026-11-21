import {
  WORLDBOOK_SCOPE_KINDS,
  type WorldbookBookListParams,
  type WorldbookScopeKind,
} from '../../services/api/worldbook'

/**
 * 世界书「书」标签的筛选状态与 URL 的互转
 *
 * 规则与全站一致：筛选属于「刷新后应当还在」的状态，所以进 query；路径只表达当前在
 * 哪个标签。空值与默认值不写进 URL，非法值在解析时回落默认。
 */

export const WORLDBOOK_SEARCH_MAX_LENGTH = 128
export const DEFAULT_WORLDBOOK_PAGE_SIZE = 20

export const WORLDBOOK_ENABLED_FILTERS = ['all', 'enabled', 'disabled'] as const
export type WorldbookEnabledFilter = (typeof WORLDBOOK_ENABLED_FILTERS)[number]

export interface WorldbookBooksFilterState {
  scopeKind: WorldbookScopeKind | ''
  presetId: number | null
  chatKey: string
  person: string
  enabled: WorldbookEnabledFilter
  /** 发给后端时叫 `q` */
  search: string
  page: number
}

export const DEFAULT_WORLDBOOK_BOOKS_FILTERS: WorldbookBooksFilterState = {
  scopeKind: '',
  presetId: null,
  chatKey: '',
  person: '',
  enabled: 'all',
  search: '',
  page: 1,
}

const includes = <T extends string>(values: readonly T[], value: string | null): value is T =>
  value !== null && (values as readonly string[]).includes(value)

function parsePresetId(value: string | null): number | null {
  if (value === null || value.trim() === '') return null
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null
}

export function parseWorldbookPage(value: string | null): number {
  if (value === null || value.trim() === '') return 1
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : 1
}

/** 从 URL 读筛选状态；任何一项不合法都回落到默认值 */
export function worldbookFiltersFromSearchParams(
  searchParams: URLSearchParams
): WorldbookBooksFilterState {
  const scopeKind = searchParams.get('scopeKind')
  const enabled = searchParams.get('enabled')

  return {
    scopeKind: includes(WORLDBOOK_SCOPE_KINDS, scopeKind) ? scopeKind : '',
    presetId: parsePresetId(searchParams.get('preset')),
    chatKey: searchParams.get('chat') ?? '',
    person: searchParams.get('person') ?? '',
    enabled: includes(WORLDBOOK_ENABLED_FILTERS, enabled) ? enabled : 'all',
    search: (searchParams.get('search') ?? '').slice(0, WORLDBOOK_SEARCH_MAX_LENGTH),
    page: parseWorldbookPage(searchParams.get('page')),
  }
}

/** 写回 URL；空值与默认值不写 */
export function worldbookFiltersToSearchParams(
  filters: WorldbookBooksFilterState
): URLSearchParams {
  const params = new URLSearchParams()
  const put = (key: string, value: string) => {
    if (value) params.set(key, value)
  }

  put('scopeKind', filters.scopeKind)
  if (filters.presetId !== null) put('preset', String(filters.presetId))
  put('chat', filters.chatKey)
  put('person', filters.person.trim())
  if (filters.enabled !== DEFAULT_WORLDBOOK_BOOKS_FILTERS.enabled) {
    put('enabled', filters.enabled)
  }
  put('search', filters.search.trim().slice(0, WORLDBOOK_SEARCH_MAX_LENGTH))
  if (filters.page !== DEFAULT_WORLDBOOK_BOOKS_FILTERS.page) put('page', String(filters.page))

  return params
}

/** 列表接口的查询参数（不含作用域，作用域由调用方拼上） */
export function worldbookFiltersToQuery(
  filters: WorldbookBooksFilterState
): WorldbookBookListParams {
  return {
    scope_kind: filters.scopeKind || undefined,
    preset_id: filters.presetId ?? undefined,
    chat_key: filters.chatKey || undefined,
    person: filters.person.trim() || undefined,
    enabled:
      filters.enabled === 'all' ? undefined : filters.enabled === 'enabled',
    q: filters.search.trim().slice(0, WORLDBOOK_SEARCH_MAX_LENGTH) || undefined,
    page: filters.page,
    page_size: DEFAULT_WORLDBOOK_PAGE_SIZE,
  }
}
