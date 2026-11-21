import type { TFunction } from 'i18next'
import { ApiError } from '../../services/api/axios'
import type { StateChannelItem, StateSnapshotOp } from '../../services/api/stateTable'
import type { StatusTone } from '../../theme/themeConfig'

/**
 * 角色状态页共用的几件小事
 *
 * 状态树的编辑、快照的展示、枚举值的中文说法在这几个标签里反复出现；只在这里收一次，
 * 避免各处对「什么算空」「冲突怎么判」慢慢分叉。
 */

export type StateRecord = Record<string, unknown>

export function isRecord(value: unknown): value is StateRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * 把编辑框里的文本解析成状态对象
 *
 * 状态树只接受对象：数组、`null`、数字都是合法的 JSON，却不是能写回服务端的形状，
 * 提前拦下来比让服务端回一个 422 更直接。
 */
export function parseStateObject(text: string): { ok: true; value: StateRecord } | { ok: false } {
  try {
    const parsed: unknown = JSON.parse(text)
    if (!isRecord(parsed)) return { ok: false }
    return { ok: true, value: parsed }
  } catch {
    return { ok: false }
  }
}

/** 版本冲突：服务端明确要求重新拉一次再改 */
export function isConflictError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

/** 频道名缺省时退回标识，两者都没有才给破折号 */
export function channelName(row: StateChannelItem): string {
  return row.channel_name || row.chat_key
}

/** 枚举值有中文就用中文，不认识的原样显示 */
export function enumText(t: TFunction, group: string, value: string | null | undefined): string {
  if (!value) return '—'
  return t(`${group}.${value}`, { defaultValue: value })
}

/** 快照结果的语气：应用成功绿、部分应用黄、被拒与失败红，其余中性 */
export function resultTone(result: string): StatusTone {
  if (result === 'applied') return 'active'
  if (result === 'partial') return 'paused'
  if (
    result === 'rejected' ||
    result === 'error' ||
    result === 'parse_failed' ||
    result === 'conflict'
  ) {
    return 'failed'
  }
  return 'inactive'
}

/** 路径一律当纯文本；只有 `op` 没有路径时就显示 `op` 本身 */
export function opPathText(op: StateSnapshotOp): string {
  return op.path || op.op
}

/** 审计里的「改了哪几处」：几条路径用顿号连起来 */
export function opsText(ops: StateSnapshotOp[] | null | undefined): string {
  const parts = (ops ?? []).map(opPathText).filter(Boolean)
  return parts.length > 0 ? parts.join('、') : '—'
}
