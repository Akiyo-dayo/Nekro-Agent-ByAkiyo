/**
 * 两版摘要的逐句对比
 *
 * 摘要是几百到一两千字的中文叙述，改动通常是增删或改写几句话。按句子切开再做最长公共子序列，
 * 结果读起来是「这几句新加的、这几句删掉的」，比逐字对比清楚得多（逐字会把改写了几个字的句子
 * 切成满屏碎片），也不用为它引一个依赖。
 *
 * 句子切分：在中英文句末标点（。！？；!?;）和换行之后断开，标点留在前一句里。
 */

export type DiffKind = 'same' | 'added' | 'removed'

export interface DiffPart {
  kind: DiffKind
  text: string
}

/** 一段文字最多切多少句；超过就把剩下的并成最后一句，LCS 表不会大到卡住页面 */
const MAX_SENTENCES = 600

export function splitSentences(text: string): string[] {
  const sentences: string[] = []
  let current = ''
  for (const char of text) {
    current += char
    if ('。！？；!?;\n'.includes(char)) {
      sentences.push(current)
      current = ''
    }
  }
  if (current) sentences.push(current)
  if (sentences.length > MAX_SENTENCES) {
    const head = sentences.slice(0, MAX_SENTENCES - 1)
    head.push(sentences.slice(MAX_SENTENCES - 1).join(''))
    return head
  }
  return sentences
}

/**
 * `before` → `after` 的逐句差异，按 `after` 的顺序排；删掉的句子插在它原来的位置上
 *
 * 比较时忽略句子首尾空白，输出保留原文。
 */
export function diffSentences(before: string, after: string): DiffPart[] {
  const a = splitSentences(before)
  const b = splitSentences(after)
  const key = (value: string) => value.trim()
  const rows = a.length + 1
  const cols = b.length + 1
  // lcs[i][j] = a[i:] 与 b[j:] 的最长公共子序列长度
  const lcs: Uint16Array[] = Array.from({ length: rows }, () => new Uint16Array(cols))
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      lcs[i][j] =
        key(a[i]) === key(b[j]) ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
    }
  }

  const parts: DiffPart[] = []
  const push = (kind: DiffKind, text: string) => {
    const last = parts[parts.length - 1]
    if (last && last.kind === kind) last.text += text
    else parts.push({ kind, text })
  }
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (key(a[i]) === key(b[j])) {
      push('same', b[j])
      i += 1
      j += 1
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      push('removed', a[i])
      i += 1
    } else {
      push('added', b[j])
      j += 1
    }
  }
  while (i < a.length) {
    push('removed', a[i])
    i += 1
  }
  while (j < b.length) {
    push('added', b[j])
    j += 1
  }
  return parts
}

/** 差异里有没有任何改动（两版只差空白时没有） */
export function hasChanges(parts: DiffPart[]): boolean {
  return parts.some(part => part.kind !== 'same' && part.text.trim() !== '')
}
