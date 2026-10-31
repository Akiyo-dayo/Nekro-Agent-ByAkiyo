import type { TFunction } from 'i18next'
import type { ModelGroupTestItem } from '../../services/api/unified-config'

/**
 * 连通性测试失败时的展示文案
 *
 * 优先按 `error_code` 取译文：租户测平台方的模型组时，后端只下发结论码与有限集合内的文案，
 * `error_message` 里不会有上游原文。译文缺失时才回落到后端文案，前端从不解析上游文本。
 */
export function describeTestFailure(
  result: ModelGroupTestItem,
  t: TFunction,
  fallback: string
): string {
  const code = result.error_code
  if (code && code !== 'none') {
    const localized = t(`modelGroup.health.errorCodes.${code}`, { defaultValue: '' })
    if (localized) return localized
  }
  return result.error_message || result.message || fallback
}
