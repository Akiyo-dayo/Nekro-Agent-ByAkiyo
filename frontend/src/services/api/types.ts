/**
 * 统一 API 类型定义
 *
 * 本模块提供与后端 API 响应格式对应的类型定义和工具函数
 */

// i18n 字典类型，与后端 SupportedLang 对应
export type I18nDict = {
  'zh-CN'?: string
  'en-US'?: string
}

/**
 * 后端错误响应格式
 *
 * 与后端 AppError.to_response() 输出格式对应
 */
export interface ApiErrorResponse {
  /** 错误类名，如 "ConfigNotFoundError" */
  error: string
  /** 本地化错误消息 */
  message: string
  /** 技术细节（可选，仅开发环境） */
  detail?: string | null
  /** 附加数据（可选） */
  data?: unknown
}

export interface ProtocolApiErrorResponse {
  error: {
    code: string
    message: string
    details?: unknown
  }
}

/**
 * 获取本地化文本
 *
 * @param i18nDict - 国际化字典
 * @param defaultText - 默认文本
 * @param lang - 目标语言，默认 zh-CN
 * @returns 本地化文本
 *
 * @example
 * ```ts
 * // i18n 字段以 i18n_ 前缀命名，便于字母排序聚合
 * const name = getLocalizedText(item.i18n_display_name, item.display_name, i18n.language)
 * const desc = getLocalizedText(item.i18n_description, item.description, i18n.language)
 * ```
 */
export function getLocalizedText(
  i18nDict: I18nDict | undefined | null,
  defaultText: string,
  lang: string = 'zh-CN'
): string {
  if (!i18nDict) return defaultText
  return i18nDict[lang as keyof I18nDict] || defaultText
}

/**
 * 检查响应是否为错误响应格式
 */
export function isApiErrorResponse(data: unknown): data is ApiErrorResponse {
  return (
    typeof data === 'object' &&
    data !== null &&
    'error' in data &&
    'message' in data &&
    typeof (data as ApiErrorResponse).error === 'string' &&
    typeof (data as ApiErrorResponse).message === 'string'
  )
}

export function isProtocolApiErrorResponse(data: unknown): data is ProtocolApiErrorResponse {
  if (typeof data !== 'object' || data === null || !('error' in data)) {
    return false
  }

  const error = (data as { error?: unknown }).error
  return (
    typeof error === 'object' &&
    error !== null &&
    'code' in error &&
    'message' in error &&
    typeof (error as { code?: unknown }).code === 'string' &&
    typeof (error as { message?: unknown }).message === 'string'
  )
}

/**
 * 换人设的影响面
 *
 * 两个入口共用一份契约：频道人设（`POST /chat-channel/{chat_key}/preset`）与
 * 实例默认人设（`PUT /presets/instance/{key}/default`）。摆在这里而不是各写一份，
 * 是因为**同一件事在两个弹窗里说成两种措辞，运营会以为是两种不同的后果**。
 *
 * 后端对应 `routers/chat_channel.py:PresetSwitchImpactResponse` 与
 * `routers/presets.py:InstanceDefaultPresetImpact`（字段逐一对齐）。
 */
export interface PresetSwitchImpact {
  /** 影响范围的名字：频道名，或实例显示名 */
  scope_label: string
  domain_id: string
  current_preset_id: number
  current_preset_name: string
  target_preset_id: number
  target_preset_name: string
  /** 生效人设其实没变。这种情况**不该**弹确认框，拦它只会让人学会一路点确认 */
  is_noop: boolean
  /** 当前人设下本域里有好感记录的人数 —— 换过去之后这些人看起来会归零 */
  affected_people: number
  /** 目标人设下已经有记录的人数 —— 「换回来就回来」的证据 */
  returning_people: number
  /** 受影响的频道数；频道入口恒为 1，实例默认入口是「未绑人设的频道」数 */
  affected_channels: number
  /** 本域的记忆条数（记忆不按人设分行，见 memory_scope_note） */
  domain_memory_paragraphs: number
  memory_scope_note: string
  /** 「旧数据保留、换回来就回来」，措辞由后端给，前端不各写一份 */
  reversible_note: string
}
