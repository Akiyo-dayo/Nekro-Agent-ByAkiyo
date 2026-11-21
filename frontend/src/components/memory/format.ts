/** 记忆页共用的展示格式化：时间、截断、秒级时间戳 */

const EMPTY = '—'

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return EMPTY
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return EMPTY
  return new Date(timestamp).toLocaleString()
}

/**
 * 短时间：同一年写「11-11 23:10」，跨年再带上年份
 *
 * 摘要的版本用生成时间称呼（「11-11 23:10 生成」），比「第 N 版」说得清：版号只在保留的
 * 那几版里有意义，时间对着聊天记录就能找到。
 */
export function formatShortDateTime(value: string | null | undefined): string {
  if (!value) return EMPTY
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return EMPTY
  const pad = (n: number) => String(n).padStart(2, '0')
  const monthDay = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  return date.getFullYear() === new Date().getFullYear()
    ? `${monthDay} ${time}`
    : `${date.getFullYear()}-${monthDay} ${time}`
}

/** 秒级时间戳（追溯消息里的 `send_timestamp`） */
export function formatUnixSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return EMPTY
  const timestamp = new Date(value * 1000).getTime()
  if (Number.isNaN(timestamp)) return EMPTY
  return new Date(timestamp).toLocaleString()
}

const pad2 = (value: number) => String(value).padStart(2, '0')

/**
 * 列表时间与全站列表同一口径（`utils/listTime.ts`）
 *
 * 两份实现逐例对比过输出完全相同，合并成一份；这里重新导出，调用点不用改。
 */
export { formatListTime } from '../../utils/listTime'

/** tooltip 与详情用的完整时间：`YYYY-MM-DD HH:mm:ss` */
export function formatFullDateTime(value: string | null | undefined): string {
  if (!value) return EMPTY
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return EMPTY
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(
    date.getHours()
  )}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`
}

/** 列表里的长文本只留一个能认出是哪条的片段 */
export function truncateText(value: string, max: number): string {
  const text = value.trim()
  return text.length > max ? `${text.slice(0, max)}…` : text
}
