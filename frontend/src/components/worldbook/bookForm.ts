import {
  WORLDBOOK_BUDGET_MAX,
  WORLDBOOK_DESCRIPTION_MAX_LENGTH,
  WORLDBOOK_NAME_MAX_LENGTH,
  WORLDBOOK_SCAN_DEPTH_MAX,
  WORLDBOOK_SCAN_DEPTH_MIN,
  WORLDBOOK_SCOPE_KINDS,
  type WorldbookBookCreateRequest,
  type WorldbookBookDetail,
  type WorldbookBookUpdateRequest,
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
  budgetChars: string
  caseSensitive: boolean
  matchWholeWords: boolean
}

export interface WorldbookBookFormErrors {
  name?: string
  description?: string
  scope?: string
  scanDepth?: string
  budget?: string
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
  budgetChars: '0',
  caseSensitive: false,
  matchWholeWords: false,
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
    budgetChars: String(book.budget_chars),
    caseSensitive: book.case_sensitive,
    matchWholeWords: book.match_whole_words,
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

  const budget = parseBudget(state.budgetChars)
  if (budget === null || budget < 0 || budget > WORLDBOOK_BUDGET_MAX) {
    errors.budget = 'bookForm.budgetInvalid'
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
    budget_chars: parseBudget(state.budgetChars) ?? 0,
    case_sensitive: state.caseSensitive,
    match_whole_words: state.matchWholeWords,
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

  const budget = parseBudget(state.budgetChars)
  if (budget !== null && budget !== book.budget_chars) body.budget_chars = budget

  if (state.caseSensitive !== book.case_sensitive) body.case_sensitive = state.caseSensitive
  if (state.matchWholeWords !== book.match_whole_words) {
    body.match_whole_words = state.matchWholeWords
  }

  return body
}
