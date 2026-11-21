/**
 * 世界书每轮预算的换算，与后端同一条公式
 *
 * 前端只用来做实时预览；真正决定注入多少的是后端。这里的入参与后端
 * `WorldbookBudgetBasis` 一致，改了公式务必两边一起改。
 */

/** 每轮预算的硬上限：上限填 0（不另设上限）时也不超过它，与后端 `HARD_CAP_TOKENS` 相同 */
export const WORLDBOOK_HARD_CAP_TOKENS = 65536

/** 世界书的回复预留：上下文长度填了、但不超过它时，预算只剩 1 token，与后端 `OUTPUT_RESERVE_TOKENS` 相同 */
export const WORLDBOOK_OUTPUT_RESERVE_TOKENS = 4096

export function computeWorldbookBudget(
  percent: number,
  capTokens: number,
  contextLength: number,
  outputReserve: number
): number {
  const base = Math.max(0, contextLength - outputReserve)
  let budget = Math.round((percent / 100) * base)
  if (capTokens > 0) {
    budget = Math.min(budget, capTokens)
  }
  return Math.max(Math.min(budget, WORLDBOOK_HARD_CAP_TOKENS), 1)
}

/** 填了上下文长度、但小到减去回复预留后不剩什么（0 是没填，按默认值算，不算小） */
export function isSmallContextLength(contextLength: number): boolean {
  return contextLength > 0 && contextLength <= WORLDBOOK_OUTPUT_RESERVE_TOKENS
}
