import type { TFunction } from 'i18next'

/**
 * 提示词明细里 `tavern_items` 的读法
 *
 * 后端把它存成 `dict[str, Any]`，老行是空对象，新行才逐项补齐；键随卡片版本与槽位
 * 开关而变。这里集中收窄形状，列表的短摘要与展开后的明细表用同一套判断，避免两处
 * 对「什么算空」各有一套说法。
 */

export interface TavernSlotDetail {
  chars: number
  layers: string[]
  state: string
}

export interface TavernMacros {
  expanded: number
  guarded: boolean
}

/** 槽位层名到 `tavern` 命名空间的翻译键；不认识的原样显示 */
const LAYER_LABEL_KEYS: Record<string, string> = {
  system: 'rounds.layer.system',
  adapter: 'rounds.layer.adapter',
  instance: 'rounds.layer.instance',
  channel: 'rounds.layer.channel',
  preset: 'rounds.layer.preset',
}

/** 槽位状态到 `tavern` 命名空间的翻译键；不认识的原样显示 */
const STATE_LABEL_KEYS: Record<string, string> = {
  sent: 'rounds.state.sent',
  pending: 'rounds.state.pending',
  gated: 'rounds.state.gated',
  empty: 'rounds.state.empty',
  unsupported: 'rounds.state.unsupported',
  retried_without: 'rounds.state.retriedWithout',
}

export const SLOT_KEYS = ['post_history', 'auxiliary', 'prefill'] as const
export type TavernSlotKey = (typeof SLOT_KEYS)[number]

export const SLOT_LABEL_KEYS: Record<TavernSlotKey, string> = {
  post_history: 'rounds.slot.postHistory',
  auxiliary: 'rounds.slot.auxiliary',
  prefill: 'rounds.slot.prefill',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function hasTavernItems(raw: Record<string, unknown> | null | undefined): boolean {
  return isRecord(raw) && Object.keys(raw).length > 0
}

export function readSlot(
  raw: Record<string, unknown> | null | undefined,
  key: TavernSlotKey
): TavernSlotDetail | null {
  if (!isRecord(raw)) return null
  const value = raw[key]
  if (!isRecord(value)) return null
  return {
    chars: typeof value.chars === 'number' ? value.chars : 0,
    layers: Array.isArray(value.layers)
      ? value.layers.filter((layer): layer is string => typeof layer === 'string')
      : [],
    state: typeof value.state === 'string' ? value.state : '',
  }
}

export function readGate(raw: Record<string, unknown> | null | undefined): boolean | null {
  if (!isRecord(raw)) return null
  return typeof raw.gate === 'boolean' ? raw.gate : null
}

export function readDepthChars(raw: Record<string, unknown> | null | undefined): number {
  if (!isRecord(raw)) return 0
  const depth = raw.depth_prompt
  return isRecord(depth) && typeof depth.chars === 'number' ? depth.chars : 0
}

export function readMacros(raw: Record<string, unknown> | null | undefined): TavernMacros {
  if (!isRecord(raw)) return { expanded: 0, guarded: false }
  const macros = raw.macros
  if (!isRecord(macros)) return { expanded: 0, guarded: false }
  return {
    expanded: typeof macros.expanded === 'number' ? macros.expanded : 0,
    guarded: macros.guarded === true,
  }
}

export function readGreeting(raw: Record<string, unknown> | null | undefined): boolean {
  // 后端只在真的先发了开场白时写这个键；老行没有它就是没发。
  return isRecord(raw) && raw.greeting === true
}

export function stateText(state: string, t: TFunction): string {
  const key = STATE_LABEL_KEYS[state]
  return key ? t(key) : state
}

export function layerText(layer: string, t: TFunction): string {
  const key = LAYER_LABEL_KEYS[layer]
  return key ? t(key) : layer
}

/**
 * 列表里的短摘要
 *
 * 空对象表示这一轮没有记录（回滚闸关着，或这一轮没跑到记录那一步），列表写一个
 * 破折号；有记录时按「槽位开关 → 非空槽位 → 深度注入 → 开场白 → 宏」的顺序拼。
 * 槽位关着但原文非空的状态是 `gated`，仍然要显示，管理员才知道「打开后会发什么」。
 */
export function summarizeTavernItems(
  raw: Record<string, unknown> | null | undefined,
  t: TFunction
): string {
  if (!hasTavernItems(raw)) return t('rounds.summary.none')

  const parts: string[] = []
  const gate = readGate(raw)
  if (gate !== null) {
    parts.push(gate ? t('rounds.summary.gateOn') : t('rounds.summary.gateOff'))
  }

  SLOT_KEYS.forEach(key => {
    const slot = readSlot(raw, key)
    if (!slot || slot.state === 'empty') return
    parts.push(
      t('rounds.summary.slot', {
        slot: t(SLOT_LABEL_KEYS[key]),
        chars: slot.chars,
        state: stateText(slot.state, t),
      })
    )
  })

  const depthChars = readDepthChars(raw)
  if (depthChars > 0) {
    parts.push(t('rounds.summary.depth', { chars: depthChars }))
  }

  if (readGreeting(raw)) {
    parts.push(t('rounds.summary.greeting'))
  }

  const macros = readMacros(raw)
  if (macros.expanded > 0) {
    parts.push(t('rounds.summary.macros', { count: macros.expanded }))
  }
  if (macros.guarded) {
    parts.push(t('rounds.summary.macrosGuarded'))
  }

  return parts.length > 0 ? parts.join(' · ') : t('rounds.summary.none')
}
