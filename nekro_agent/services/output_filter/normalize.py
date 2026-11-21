"""归一化：全半角、大小写、空白与零宽字符

匹配前先把原文与规则都收成同一种形态，否则「BAD / ＢＡＤ / b​a​d」这类写法只要换一种
就能绕过词表。归一化串与原文之间必须保留逐字符的映射，命中位置才能换回原文区间——
替换要覆盖掉插在字之间的空白与零宽字符，而不是只换掉字母本身。
"""

import unicodedata

# 不在 `Cf` 类别里、但同样不可见 / 不承载语义的字符。
# 这些是常见「拆字」写法里会被插进来的角色，单独列出来比逐个猜类别更明确。
_DROP_CHARS = frozenset({"\u034f", "\u115f", "\u1160", "\u3164", "\uffa0"})
_DROP_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))


def is_invisible_char(ch: str) -> bool:
    """该字符是否不可见（零宽、方向控制、变体选择符等），**不含空白**

    公开出来是因为世界书的扫描文本要先按同一套判据去掉不可见字符：两份判据迟早只改
    一份，而漏掉的那一份就是「在字之间插一个零宽空格就能绕过关键词」。
    空白刻意不在这里判：调用方对空白的处理各不相同（这里要删、世界书要压成一个空格），
    把它们混在一起说「不可见」会让调用方误删本该保留的分隔。
    """
    if unicodedata.category(ch) == "Cf":
        return True
    if ch in _DROP_CHARS:
        return True
    codepoint = ord(ch)
    return any(start <= codepoint <= end for start, end in _DROP_RANGES)


def _is_dropped(ch: str) -> bool:
    """该字符是否在匹配前被丢弃（空白与不可见字符都丢）"""
    return ch.isspace() or is_invisible_char(ch)


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """归一化文字，并给出每个归一化字符对应的原文下标

    Returns:
        (归一化串, 下标列表)。`len(下标列表) == len(归一化串)`。
    """
    normalized: list[str] = []
    indexes: list[int] = []
    for index, char in enumerate(text):
        if _is_dropped(char):
            continue
        # 逐个原文字符做 NFKC：一个全角字符可能展开成多个半角字符，
        # 它们都记在同一个原文下标上，替换时整段一起换掉。
        for folded in unicodedata.normalize("NFKC", char).casefold():
            if _is_dropped(folded):
                continue
            normalized.append(folded)
            indexes.append(index)
    return "".join(normalized), indexes


def normalize_rule_text(text: str) -> str:
    """把词表里的一条规则归一化成与正文相同的形态"""
    return normalize_with_map(text)[0]
