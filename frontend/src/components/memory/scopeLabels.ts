import type { ScopeOptionsResponse } from '../../services/api/memory'

/**
 * 作用域选项里的显示名
 *
 * 摘要列表与摘要对话框都要把 `chat_key` / `preset_id` 翻成人能读的名字。选项只来自
 * `GET /memory/scope-options`：查不到时保留原始标识，而不是凭列表里的行自己拼一份 ——
 * 同一屏两处各拼一份，迟早会出现两处叫法不一样。
 */

// 控制台直发 CC 的结论专用的人设档（后端 `instance_scope.CONSOLE_ONLY_PRESET_ID`）。
// 它是负数，绝不是一个"被删掉的人设"：显示成 `#-1` 会让管理员以为数据坏了。
export const CONSOLE_ONLY_PRESET_ID = -1

export function channelNameOf(
  options: ScopeOptionsResponse | undefined,
  chatKey: string
): string {
  return (
    options?.channels.find(channel => channel.chat_key === chatKey)?.channel_name || chatKey
  )
}

export function presetNameOf(
  options: ScopeOptionsResponse | undefined,
  presetId: number,
  noneLabel: string,
  consoleOnlyLabel?: string
): string {
  // 0 是「无人设档」，它没有对应的选项行
  if (presetId === 0) return noneLabel
  if (presetId === CONSOLE_ONLY_PRESET_ID && consoleOnlyLabel) return consoleOnlyLabel
  return options?.presets.find(preset => preset.preset_id === presetId)?.name || `#${presetId}`
}
