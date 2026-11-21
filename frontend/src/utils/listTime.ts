/** 列表里的短时间：今天内只给时分，今年内带月日，跨年再带年份 */

const EMPTY = '—'

function toDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

const pad = (value: number) => String(value).padStart(2, '0')

/** 今天内「HH:mm」，今年内「MM-DD HH:mm」，更早「YYYY-MM-DD」 */
export function formatListTime(value: string | null | undefined): string {
  const date = toDate(value)
  if (!date) return EMPTY
  const now = new Date()
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  if (sameDay) return time
  const monthDay = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  if (date.getFullYear() === now.getFullYear()) return `${monthDay} ${time}`
  return `${date.getFullYear()}-${monthDay}`
}

/** 悬停提示里的完整时间「YYYY-MM-DD HH:mm:ss」；空值与非法值同样给破折号 */
export function formatFullTime(value: string | null | undefined): string {
  const date = toDate(value)
  if (!date) return EMPTY
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}
