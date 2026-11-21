import {
  MEMORY_TYPES,
  type MemoryItemAction,
  type MemoryItemDetail,
  type MemoryType,
} from '../../services/api/memory'

/**
 * 图谱节点的小工具
 *
 * 图谱节点的 `metadata` 是后端按类型塞的一个字典，取值时逐项核对类型，不直接断言。
 */

/** 四种有单条详情的记忆；「人」节点不是 */
export const isItemType = (value: string): value is MemoryType =>
  (MEMORY_TYPES as readonly string[]).includes(value)

export function metaString(metadata: Record<string, unknown>, key: string): string {
  const value = metadata[key]
  return typeof value === 'string' ? value : ''
}

export function metaNumber(metadata: Record<string, unknown>, key: string): number | null {
  const value = metadata[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function metaStrings(metadata: Record<string, unknown>, key: string): string[] {
  const value = metadata[key]
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

export const isMilestoneNode = (node: { memory_type: string; metadata: Record<string, unknown> }) =>
  node.memory_type === 'paragraph' && node.metadata.is_milestone === true

/**
 * 详情面板里能直接点的干预动作
 *
 * 与后端白名单一致：段落全套、事件只有隔离与放回、关系只能调权、实体没有。已删除 / 已清理的
 * 不给动作（与详情对话框一样只读）；隔离着的段落只给「放回」。
 */
export function graphNodeActions(memoryType: MemoryType, detail: MemoryItemDetail): MemoryItemAction[] {
  if (detail.status !== 'active' && detail.status !== 'quarantined') return []
  if (detail.status === 'quarantined') {
    return memoryType === 'paragraph' || memoryType === 'episode' ? ['restore'] : []
  }
  if (memoryType === 'paragraph') {
    return [
      'reinforce',
      'demote',
      detail.is_frozen ? 'unfreeze' : 'freeze',
      detail.is_protected ? 'unprotect' : 'protect',
      'quarantine',
    ]
  }
  if (memoryType === 'episode') return ['quarantine']
  if (memoryType === 'relation') return ['reinforce', 'demote']
  return []
}
