import type { RpPresetSegmentBrief } from '../../services/api/rpPresets'

/**
 * 预设段落的排布口径
 *
 * 后端给的 `position` 只分「相对」与「按深度插入」；「在聊天记录之前还是之后」要看它在
 * 顺序表里与 `chatHistory` 占位段的相对位置。这个判断在两个地方都要用（导入报告的摘要表、
 * 详情抽屉的段落列表），放成纯函数免得两边各写一份再慢慢漂移。
 */

export type RpSegmentPlacement =
  | { kind: 'history' }
  | { kind: 'depth'; depth: number }
  | { kind: 'before' }
  | { kind: 'after' }

/** `chatHistory` 在顺序表里的下标；没有这个占位段时返回 -1 */
export function chatHistoryIndex(segments: readonly RpPresetSegmentBrief[]): number {
  return segments.findIndex(segment => segment.identifier === 'chatHistory')
}

export function isChatHistory(segment: RpPresetSegmentBrief): boolean {
  return segment.identifier === 'chatHistory'
}

/** 一段落在哪：聊天记录分隔带、深度注入，还是聊天记录之前 / 之后 */
export function placementOf(
  segment: RpPresetSegmentBrief,
  index: number,
  historyIndex: number
): RpSegmentPlacement {
  if (isChatHistory(segment)) return { kind: 'history' }
  if (segment.position === 'in_chat') {
    return { kind: 'depth', depth: Math.max(0, segment.depth) }
  }
  if (historyIndex >= 0 && index > historyIndex) return { kind: 'after' }
  return { kind: 'before' }
}

/**
 * 占位段由 NA 填什么
 *
 * 键是详情节里「由 NA 填入」后面的说明文案。认不出的占位段只标「由 NA 填入」，不猜。
 */
export const MARKER_FILL_KEYS: Record<string, string> = {
  charDescription: 'lab.presets.detail.marker.fills.charDescription',
  charPersonality: 'lab.presets.detail.marker.fills.charPersonality',
  scenario: 'lab.presets.detail.marker.fills.scenario',
  dialogueExamples: 'lab.presets.detail.marker.fills.dialogueExamples',
  worldInfoBefore: 'lab.presets.detail.marker.fills.worldInfo',
  worldInfoAfter: 'lab.presets.detail.marker.fills.worldInfo',
  chatHistory: 'lab.presets.detail.marker.fills.chatHistory',
}

export function markerFillKey(identifier: string): string | null {
  return MARKER_FILL_KEYS[identifier] ?? null
}

/** `meta.rp_preset.used[].where` 的文案键；深度项带一个数字 */
export function rpWhereLabelKey(
  where: string
): { key: string; depth: number | null } {
  if (where === 'system') return { key: 'lab.preview.rpWhere.system', depth: null }
  if (where === 'tail') return { key: 'lab.preview.rpWhere.tail', depth: null }
  if (where.startsWith('depth:')) {
    const depth = Number(where.slice('depth:'.length))
    return {
      key: 'lab.preview.rpWhere.depth',
      depth: Number.isFinite(depth) ? depth : 0,
    }
  }
  return { key: '', depth: null }
}

/** 位置文案的键与插值；导入报告与详情抽屉共用 */
export interface RpPlacementLabel {
  key: string
  options?: { depth: number }
}

export function placementLabel(placement: RpSegmentPlacement): RpPlacementLabel {
  switch (placement.kind) {
    case 'history':
      return { key: 'lab.presets.detail.marker.chatHistory' }
    case 'depth':
      return {
        key: 'lab.presets.detail.position.depth',
        options: { depth: placement.depth },
      }
    case 'after':
      return { key: 'lab.presets.detail.position.after' }
    default:
      return { key: 'lab.presets.detail.position.before' }
  }
}
