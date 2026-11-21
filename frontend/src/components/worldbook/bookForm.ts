import {
  WORLDBOOK_BUDGET_MAX,
  WORLDBOOK_DESCRIPTION_MAX_LENGTH,
  WORLDBOOK_NAME_MAX_LENGTH,
  WORLDBOOK_SCAN_DEPTH_MAX,
  WORLDBOOK_SCAN_DEPTH_MIN,
  WORLDBOOK_SCOPE_KINDS,
  type WorldbookActiveScene,
  type WorldbookBookCreateRequest,
  type WorldbookBookDetail,
  type WorldbookBookUpdateRequest,
  type WorldbookInjectMode,
  type WorldbookScopeKind,
} from '../../services/api/worldbook'

/**
 * 建书 / 书设置共用的表单状态
 *
 * 数字输入在表单里保持字符串：空串表示「留空用后端默认」，折成 `null` 或 0 会丢掉
 * 这个区别。提交时再按字段语义折成接口要的形状。
 */
export interface WorldbookBookFormState {
  name: string
  description: string
  scopeKind: WorldbookScopeKind
  presetId: string
  chatKey: string
  personAccount: string
  enabled: boolean
  scanDepth: string
  budgetTokens: string
  injectMode: WorldbookInjectMode
  activeScene: WorldbookActiveScene
  caseSensitive: boolean
  matchWholeWords: boolean
  scanNames: boolean
  vectorAll: boolean
  vectorQueryMessages: string
  vectorMaxEntries: string
  vectorThreshold: string
}

export interface WorldbookBookFormErrors {
  name?: string
  description?: string
  scope?: string
  scanDepth?: string
  budget?: string
  vectorQueryMessages?: string
  vectorMaxEntries?: string
  vectorThreshold?: string
}

export const DEFAULT_WORLDBOOK_BOOK_FORM: WorldbookBookFormState = {
  name: '',
  description: '',
  scopeKind: 'domain',
  presetId: '',
  chatKey: '',
  personAccount: '',
  enabled: true,
  scanDepth: '',
  budgetTokens: '0',
  injectMode: 'both',
  activeScene: 'both',
  caseSensitive: false,
  matchWholeWords: false,
  scanNames: false,
  vectorAll: false,
  vectorQueryMessages: '2',
  vectorMaxEntries: '5',
  vectorThreshold: '0.25',
}

export function bookFormFromBook(book: WorldbookBookDetail): WorldbookBookFormState {
  const scopeKind = (WORLDBOOK_SCOPE_KINDS as readonly string[]).includes(book.scope_kind)
    ? (book.scope_kind as WorldbookScopeKind)
    : 'domain'

  return {
    name: book.name,
    description: book.description,
    scopeKind,
    presetId: book.preset_id > 0 ? String(book.preset_id) : '',
    chatKey: book.chat_key,
    personAccount: book.person_account,
    enabled: book.enabled,
    scanDepth: String(book.scan_depth),
    budgetTokens: String(book.budget_tokens),
    injectMode: book.inject_mode,
    activeScene: book.active_scene,
    caseSensitive: book.case_sensitive,
    matchWholeWords: book.match_whole_words,
    scanNames: book.scan_names,
    vectorAll: book.vector_all,
    vectorQueryMessages: String(book.vector_query_messages),
    vectorMaxEntries: String(book.vector_max_entries),
    vectorThreshold: String(book.vector_threshold),
  }
}

export function parseOptionalInteger(value: string): number | null {
  const text = value.trim()
  if (!text) return null
  const parsed = Number(text)
  return Number.isInteger(parsed) ? parsed : null
}

/** 空串按 0 处理：预算的 0 本来就是「不单独限制」 */
function parseBudget(value: string): number | null {
  const text = value.trim()
  if (!text) return 0
  const parsed = Number(text)
  return Number.isInteger(parsed) ? parsed : null
}

function effectiveName(state: WorldbookBookFormState, fallbackName?: string): string {
  return state.name.trim() || fallbackName?.trim() || ''
}

/**
 * 前端校验
 *
 * 返回的是文案键而不是句子：两个调用方（建书对话框与导入对话框）共用这套判据，
 * 但各自在组件里翻译。
 */
export function validateBookForm(
  state: WorldbookBookFormState,
  options: { hasInstance: boolean; fallbackName?: string; scopeLocked?: boolean }
): WorldbookBookFormErrors {
  const errors: WorldbookBookFormErrors = {}
  const name = effectiveName(state, options.fallbackName)

  if (!name) {
    errors.name = 'bookForm.nameRequired'
  } else if (name.length > WORLDBOOK_NAME_MAX_LENGTH) {
    errors.name = 'bookForm.nameTooLong'
  }

  if (state.description.length > WORLDBOOK_DESCRIPTION_MAX_LENGTH) {
    errors.description = 'bookForm.descriptionTooLong'
  }

  // 书设置里作用域只读：沿用建书时的绑定，不再按当前实例重新要求一遍
  if (!options.scopeLocked) {
    if ((state.scopeKind === 'domain' || state.scopeKind === 'person') && !options.hasInstance) {
      errors.scope = 'bookForm.needInstance'
    } else if (
      state.scopeKind === 'preset' &&
      (!state.presetId ||
        !Number.isInteger(Number(state.presetId)) ||
        Number(state.presetId) <= 0)
    ) {
      errors.scope = 'bookForm.presetRequired'
    } else if (state.scopeKind === 'channel' && !state.chatKey) {
      errors.scope = 'bookForm.channelRequired'
    } else if (state.scopeKind === 'person' && !state.personAccount.trim()) {
      errors.scope = 'bookForm.personRequired'
    }
  }

  // 建书时留空交给后端默认；书设置里这一列在库里不可空，留空没有「回到默认」的意思，
  // 只会被当成不改，所以编辑时必须填一个值
  const scanDepth = parseOptionalInteger(state.scanDepth)
  if (
    (state.scanDepth.trim() !== '' || options.scopeLocked) &&
    (scanDepth === null ||
      scanDepth < WORLDBOOK_SCAN_DEPTH_MIN ||
      scanDepth > WORLDBOOK_SCAN_DEPTH_MAX)
  ) {
    errors.scanDepth = 'bookForm.scanDepthInvalid'
  }

  const budget = parseBudget(state.budgetTokens)
  if (budget === null || budget < 0 || budget > WORLDBOOK_BUDGET_MAX) {
    errors.budget = 'bookForm.budgetInvalid'
  }

  const queryMessages = Number(state.vectorQueryMessages)
  if (
    !Number.isInteger(queryMessages) ||
    queryMessages < 1 ||
    queryMessages > 10
  ) {
    errors.vectorQueryMessages = 'bookForm.semantic.queryMessagesInvalid'
  }
  const maxEntries = Number(state.vectorMaxEntries)
  if (!Number.isInteger(maxEntries) || maxEntries < 1 || maxEntries > 20) {
    errors.vectorMaxEntries = 'bookForm.semantic.maxEntriesInvalid'
  }
  const threshold = Number(state.vectorThreshold)
  if (!Number.isFinite(threshold) || threshold < 0 || threshold > 1) {
    errors.vectorThreshold = 'bookForm.semantic.thresholdInvalid'
  }

  return errors
}

export function bookFormToCreateRequest(
  state: WorldbookBookFormState,
  fallbackName?: string
): WorldbookBookCreateRequest {
  return {
    name: effectiveName(state, fallbackName),
    description: state.description,
    scope_kind: state.scopeKind,
    preset_id: state.scopeKind === 'preset' ? Number(state.presetId) : 0,
    chat_key: state.scopeKind === 'channel' ? state.chatKey : '',
    person_account: state.scopeKind === 'person' ? state.personAccount.trim() : '',
    enabled: state.enabled,
    scan_depth: parseOptionalInteger(state.scanDepth),
    budget_tokens: parseBudget(state.budgetTokens) ?? 0,
    inject_mode: state.injectMode,
    active_scene: state.activeScene,
    case_sensitive: state.caseSensitive,
    match_whole_words: state.matchWholeWords,
    scan_names: state.scanNames,
    vector_all: state.vectorAll,
    vector_query_messages: Number(state.vectorQueryMessages),
    vector_max_entries: Number(state.vectorMaxEntries),
    vector_threshold: Number(state.vectorThreshold),
  }
}

/** 书设置只发改过的字段：没改的字段不传，后端按「不修改」处理 */
export function bookFormToUpdateRequest(
  state: WorldbookBookFormState,
  book: WorldbookBookDetail
): WorldbookBookUpdateRequest {
  const body: WorldbookBookUpdateRequest = {}
  const name = state.name.trim()
  if (name !== book.name) body.name = name
  if (state.description !== book.description) body.description = state.description
  if (state.enabled !== book.enabled) body.enabled = state.enabled

  const scanDepth = parseOptionalInteger(state.scanDepth)
  if (scanDepth !== null && scanDepth !== book.scan_depth) body.scan_depth = scanDepth

  const budget = parseBudget(state.budgetTokens)
  if (budget !== null && budget !== book.budget_tokens) body.budget_tokens = budget

  if (state.injectMode !== book.inject_mode) body.inject_mode = state.injectMode
  if (state.activeScene !== book.active_scene) body.active_scene = state.activeScene

  if (state.caseSensitive !== book.case_sensitive) body.case_sensitive = state.caseSensitive
  if (state.matchWholeWords !== book.match_whole_words) {
    body.match_whole_words = state.matchWholeWords
  }
  if (state.scanNames !== book.scan_names) body.scan_names = state.scanNames

  if (state.vectorAll !== book.vector_all) body.vector_all = state.vectorAll
  const queryMessages = Number(state.vectorQueryMessages)
  if (queryMessages !== book.vector_query_messages) body.vector_query_messages = queryMessages
  const maxEntries = Number(state.vectorMaxEntries)
  if (maxEntries !== book.vector_max_entries) body.vector_max_entries = maxEntries
  const threshold = Number(state.vectorThreshold)
  if (threshold !== book.vector_threshold) body.vector_threshold = threshold

  return body
}
