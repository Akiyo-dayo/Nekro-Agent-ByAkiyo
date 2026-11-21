import type { TFunction } from 'i18next'
import type { WorldbookBookItem } from '../../services/api/worldbook'

/** 作用域类型绑定对象的展示文字；域书没有绑定对象 */
export function worldbookBindingText(
  book: Pick<
    WorldbookBookItem,
    'scope_kind' | 'preset_id' | 'preset_name' | 'chat_key' | 'channel_name' | 'person_account'
  >
): string {
  switch (book.scope_kind) {
    case 'preset':
      return book.preset_name || `#${book.preset_id}`
    case 'channel':
      return book.channel_name || book.chat_key
    case 'person':
      return book.person_account
    default:
      return ''
  }
}

/** 绑定对象已经不在了时的说明；域书没有绑定对象 */
export function worldbookBindingMissingText(scopeKind: string, t: TFunction): string {
  switch (scopeKind) {
    case 'preset':
      return t('bindingMissing.preset')
    case 'channel':
      return t('bindingMissing.channel')
    case 'person':
      return t('bindingMissing.person')
    default:
      return ''
  }
}

/**
 * 命中了但没有注入的原因
 *
 * 条数上限之后的条目不分析；缓存里已经知道要分流的，后端把两个原因用 `|` 连在一起
 * （如 `max_entries|template_unsupported`），这里逐个翻译再连起来。
 */
export function worldbookDropReasonText(reason: string, prefix: string, t: TFunction): string {
  return reason
    .split('|')
    .filter(Boolean)
    .map(part => t(`${prefix}.${part}`, { defaultValue: part }))
    .join(t('common.reasonSeparator'))
}

/** 位置的名字；深度位置带上深度 */
export function worldbookPositionText(
  position: string,
  depth: number,
  t: TFunction
): string {
  if (position === 'at_depth') return t('positions.atDepth', { depth })
  if (position === 'after_persona') return t('positions.afterPersona')
  if (position === 'before_history') return t('positions.beforeHistory')
  return position
}
