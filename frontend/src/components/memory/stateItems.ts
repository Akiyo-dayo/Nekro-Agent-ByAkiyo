import type { TFunction } from 'i18next'

/**
 * 提示词明细里 `state_items` 的读法
 *
 * 后端只记计数和名字，不记任何状态值；键随模板与闸门开关而变，老行上是空对象。
 * 列表的短摘要与展开后的明细共用这里的一套判断，避免两处对「什么算空」各说一套。
 */

export type StateItems = Record<string, unknown>

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 空对象表示这一轮没有记录，整块与摘要都不显示 */
export function hasStateItems(raw: StateItems | null | undefined): boolean {
  return isRecord(raw) && Object.keys(raw).length > 0
}

export function readState(raw: StateItems | null | undefined): string {
  if (!isRecord(raw)) return ''
  return typeof raw.state === 'string' ? raw.state : ''
}

export function readStateNumber(raw: StateItems, key: string): number | null {
  return typeof raw[key] === 'number' ? (raw[key] as number) : null
}

export function readStateBoolean(raw: StateItems, key: string): boolean | null {
  const value = raw[key]
  return typeof value === 'boolean' ? value : null
}

export function readTrimmed(raw: StateItems | null | undefined): string[] {
  if (!isRecord(raw)) return []
  const trimmed = raw.trimmed
  if (!Array.isArray(trimmed)) return []
  return trimmed.filter((item): item is string => typeof item === 'string')
}

export interface StateLastUpdate {
  at: string
  result: string
  applied: number | null
  rejected: number | null
  source: string
  step: string
}

export function readLastUpdate(raw: StateItems | null | undefined): StateLastUpdate | null {
  if (!isRecord(raw)) return null
  const value = raw.last_update
  if (!isRecord(value)) return null
  return {
    at: typeof value.at === 'string' ? value.at : '',
    result: typeof value.result === 'string' ? value.result : '',
    applied: typeof value.applied === 'number' ? value.applied : null,
    rejected: typeof value.rejected === 'number' ? value.rejected : null,
    source: typeof value.source === 'string' ? value.source : '',
    step: typeof value.step === 'string' ? value.step : '',
  }
}

/**
 * 列表里的短摘要
 *
 * 只有 `state = injected` 时才谈 token 与被截掉的组 —— 其余状态（没模板、被暂停）
 * 注入的根本不是这段内容，报一个 0 token 只会让人以为「注入了但是空的」。
 */
export function summarizeStateItems(
  raw: StateItems | null | undefined,
  t: TFunction
): string | null {
  if (!isRecord(raw) || Object.keys(raw).length === 0) return null

  const state = readState(raw)
  const stateText = t(`enums.state.${state}`, { defaultValue: state })
  if (state !== 'injected') return stateText

  const parts: string[] = [t('summary.injected')]
  const tokens = readStateNumber(raw, 'tokens')
  const chars = readStateNumber(raw, 'chars')
  if (tokens !== null) parts.push(t('summary.tokens', { tokens }))
  else if (chars !== null) parts.push(t('summary.chars', { chars }))

  const trimmed = readTrimmed(raw)
  if (trimmed.length > 0) parts.push(t('summary.trimmed', { count: trimmed.length }))

  return parts.join(' · ')
}
