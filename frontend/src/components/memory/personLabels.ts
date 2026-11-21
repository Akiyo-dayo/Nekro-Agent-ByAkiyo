import type { PersonLabel } from '../../services/api/memory'

/**
 * 记忆里「涉及的人」的名字怎么拼
 *
 * 记忆页的列表、分组视图、按人筛选的候选，以及以后图谱里的「人」节点都从这里取名字，
 * 口径只有一份：有昵称用昵称，没有就退到账号；称呼单独放，账号始终拿得到。
 */

/** 一个人的主名字：有昵称用昵称，没有就退到账号 */
export function personPrimaryName(person: PersonLabel): string {
  return person.display_name.trim() || person.account
}

/**
 * 悬停提示里的一行：昵称 · 称呼 · 账号（缺哪样省哪样）
 *
 * 昵称来自好感记录时在后面注明（`fromAffectionLabel`）：好感记录是整个好感域共用的，那个昵称
 * 可能是在别的群或私聊里看到的，不一定是这段记忆所在频道里的叫法。
 */
export function personTooltipLine(
  person: PersonLabel,
  callNameLabel: string,
  fromAffectionLabel?: string
): string {
  const parts: string[] = []
  const displayName = person.display_name.trim()
  if (displayName) {
    parts.push(
      person.name_source === 'affection' && fromAffectionLabel
        ? `${displayName}（${fromAffectionLabel}）`
        : displayName
    )
  }
  if (person.call_name.trim()) parts.push(`${callNameLabel}：${person.call_name.trim()}`)
  parts.push(person.account)
  return parts.join(' · ')
}

/** 老后端没有 `persons` 时，用账号拼出一份空名字的标签 */
export function labelsOrAccounts(persons: PersonLabel[] | undefined, accounts: string[]): PersonLabel[] {
  if (persons && persons.length > 0) return persons
  return accounts.map(account => ({ account, display_name: '', call_name: '' }))
}

/** 按人筛选候选在本地按名字再筛一遍：昵称、称呼、账号任一包含关键词就算 */
export function personMatches(person: PersonLabel, keyword: string): boolean {
  const needle = keyword.trim().toLowerCase()
  if (!needle) return true
  return [person.display_name, person.call_name, person.account].some(value =>
    value.toLowerCase().includes(needle)
  )
}
