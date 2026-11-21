/** 记忆页共用的展示格式化：时间、截断、秒级时间戳 */

const EMPTY = '—'

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return EMPTY
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return EMPTY
  return new Date(timestamp).toLocaleString()
}

/** 秒级时间戳（追溯消息里的 `send_timestamp`） */
export function formatUnixSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return EMPTY
  const timestamp = new Date(value * 1000).getTime()
  if (Number.isNaN(timestamp)) return EMPTY
  return new Date(timestamp).toLocaleString()
}

/** 列表里的长文本只留一个能认出是哪条的片段 */
export function truncateText(value: string, max: number): string {
  const text = value.trim()
  return text.length > max ? `${text.slice(0, max)}…` : text
}
