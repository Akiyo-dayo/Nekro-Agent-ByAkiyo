"""世界书的 token 计数：本地 `o200k_base` 数一遍，乘模型家族系数

## 为什么用 `o200k_base` 加系数

每轮世界书预算按 token 计（照 SillyTavern 的口径），而 NA 接的模型五花八门，没有办法为每家
都装一份分词器。做法是用 tiktoken 的 `o200k_base` 数一遍，再乘一个「这家分词器相对
`o200k_base` 多数还是少数」的系数。系数是在同一段 4,849 字的中文设定正文上，用 ST 1.19.0
自带的各家分词器实测的（`o200k_base` 数出 3,772 token），见 `MODEL_FAMILY_FACTORS`。

模型组上填了 `TOKEN_COUNT_FACTOR`（非 0）时优先用它；没填就按模型名里含的字样取第一个
命中的家族，大小写不敏感；都不中按 1.0。英文、代码与中文的比例不同，误差在一两成以内——
预算本来就是软上限。

## 离线可用

tiktoken 第一次用某个编码时会联网下载 BPE 文件，国内网络、内网部署都可能失败，而且第一次
下载会卡住调用它的那一轮。所以编码文件随代码一起发（`o200k_base.tiktoken.gz`，gzip 压缩，
解压后的 sha256 与 tiktoken 官方给的一致才用），从本地构造编码，**不联网**。加载失败时退回
字符估算：中日韩字符 × 0.78、其它字符 ÷ 4，再乘系数；只打一次 WARNING，
:func:`estimator` 报 `chars`，明细与测试页据此标「估算方式：字符」。

## 缓存

数过的正文按内容摘要缓存「`o200k_base` 下的原始 token 数」（不含系数：换模型组不必重数），
满了丢最早一半（与 L4 其它进程内缓存同一个做法）。键里带估算方式：编码晚一步加载成功后，
先前按字符估的数不会被沿用。
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from nekro_agent.core.config import MODEL_GROUP_TOKEN_FACTOR_MAX
from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("worldbook.tokens")

# 随代码发布的编码文件与它解压后的 sha256（tiktoken 0.12 `o200k_base()` 里的 expected_hash）
ENCODING_FILE = Path(__file__).with_name("o200k_base.tiktoken.gz")
ENCODING_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
ENCODING_NAME = "o200k_base"

# `o200k_base` 的切分正则与特殊 token，照抄 tiktoken（`tiktoken_ext/openai_public.py`，MIT 许可）。
# 有用例拿 tiktoken 自己的定义逐项比对，库升级改了定义会当场红。
O200K_PAT_STR = "|".join(
    [
        r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
        r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
        r"""\p{N}{1,3}""",
        r""" ?[^\s\p{L}\p{N}]+[\r\n/]*""",
        r"""\s*[\r\n]+""",
        r"""\s+(?!\S)""",
        r"""\s+""",
    ],
)
O200K_SPECIAL_TOKENS = {"<|endoftext|>": 199999, "<|endofprompt|>": 200018}

# 模型名里含这些字样 → 系数（相对 `o200k_base`）。按顺序取第一个命中的：`gpt-4o` 必须排在
# `gpt-4` 前面，`nemo` 必须排在 `mistral` 前面（`mistral-nemo` 用的是 Nemo 的分词器）。
# `o1` / `o3` / `o4` 太短，按独立的词匹配（见 `_O_SERIES`），其余按子串。
MODEL_FAMILY_FACTORS: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("deepseek", ("deepseek",), 0.80),
    ("gemma", ("gemini", "gemma"), 0.89),
    ("qwen", ("qwen",), 0.90),
    ("yi", ("yi-",), 0.92),
    ("command-r", ("command-r",), 0.91),
    ("o200k", ("gpt-4o", "gpt-4.1", "gpt-4.5", "gpt-5"), 1.00),
    ("llama", ("llama",), 1.02),
    ("nemo", ("nemo",), 1.16),
    ("claude", ("claude",), 1.29),
    ("mistral", ("mistral", "mixtral"), 1.35),
    ("cl100k", ("gpt-3.5", "gpt-4"), 1.38),
)
_O_SERIES = re.compile(r"(?<![a-z0-9])o[134](?![a-z0-9])")
DEFAULT_FACTOR = 1.0
# 模型组上 `TOKEN_COUNT_FACTOR` 的合法区间（0 表示按模型名自动取）
MAX_FACTOR = MODEL_GROUP_TOKEN_FACTOR_MAX

# 字符估算：中日韩字符每字 0.78 token（`o200k_base` 在中文设定正文上的实测值），其它字符 4 个一 token
ESTIMATE_CJK_TOKENS_PER_CHAR = 0.78
ESTIMATE_OTHER_CHARS_PER_TOKEN = 4.0
# 按码位区间拼字符类，源码里不出现这些字符本身：兼容汉字区（U+F900 起）的字在 NFC 之后会变成别的码位
_CJK_RANGES = (
    (0x2E80, 0x9FFF),  # 部首、假名、注音、中日韩统一汉字
    (0xAC00, 0xD7AF),  # 谚文音节
    (0xF900, 0xFAFF),  # 兼容汉字
    (0xFF00, 0xFFEF),  # 全角与半角形式
    (0x20000, 0x2FFFF),  # 扩展汉字
)
_CJK = re.compile("[" + "".join(f"{chr(low)}-{chr(high)}" for low, high in _CJK_RANGES) + "]")

# 原始 token 数缓存的条数上限；满了丢最早的一半
MAX_CACHE_ENTRIES = 50_000
# 分段计数：每段至多这么多字，切口往回最多找这么多字去对齐空白 / 标点（见 `_count_with_encoding`）
CHUNK_CHARS = 1024
CHUNK_BACKTRACK = 64
# `base_counts` / `prefetch` 一批正文合计不超过这么多字就直接在事件循环上做（摘要加计数），超过才放到线程里
PREFETCH_INLINE_CHARS = 50_000


@dataclass(frozen=True)
class TokenFactor:
    """这一轮用的系数，以及它从哪来

    Attributes:
        value: 系数。
        source: `group`（模型组上填的）/ `model_name`（按模型名匹配的家族）/ `default`（都没有，按 1.0）。
        family: 按模型名匹配到的家族标签；`source` 不是 `model_name` 时为空。
    """

    value: float
    source: str
    family: str = ""


def factor_for_model_name(model_name: str) -> TokenFactor:
    """按模型名取家族系数；都不中按 1.0"""
    name = (model_name or "").lower()
    if name:
        for family, needles, factor in MODEL_FAMILY_FACTORS:
            if family == "o200k" and _O_SERIES.search(name):
                return TokenFactor(value=factor, source="model_name", family=family)
            if any(needle in name for needle in needles):
                return TokenFactor(value=factor, source="model_name", family=family)
    return TokenFactor(value=DEFAULT_FACTOR, source="default")


def factor_for_group(group: Optional[Any]) -> TokenFactor:
    """模型组的系数：组上填了（> 0）就用它，否则按 `CHAT_MODEL` 取家族系数"""
    if group is None:
        return TokenFactor(value=DEFAULT_FACTOR, source="default")
    try:
        explicit = float(getattr(group, "TOKEN_COUNT_FACTOR", 0) or 0)
    except (TypeError, ValueError):
        explicit = 0.0
    if math.isfinite(explicit) and 0 < explicit <= MAX_FACTOR:
        return TokenFactor(value=explicit, source="group")
    return factor_for_model_name(str(getattr(group, "CHAT_MODEL", "") or ""))


class _EncodingHolder:
    """编码只加载一次；加载失败记一次 WARNING，之后一直用字符估算"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._encoding: Any = None
        self._failed = False

    @property
    def attempted(self) -> bool:
        return self._encoding is not None or self._failed

    def get(self) -> Any:
        if self._encoding is not None or self._failed:
            return self._encoding
        with self._lock:
            if self._encoding is None and not self._failed:
                try:
                    self._encoding = _load_encoding(ENCODING_FILE)
                except Exception as error:  # noqa: BLE001 - 加载失败退回字符估算，不能让世界书整轮失败
                    self._failed = True
                    logger.warning(
                        f"世界书 token 计数器加载 {ENCODING_NAME} 失败，改用字符估算（中日韩字符 × "
                        f"{ESTIMATE_CJK_TOKENS_PER_CHAR}、其它字符 ÷ {ESTIMATE_OTHER_CHARS_PER_TOKEN:g}，再乘系数）: {error!r}",
                    )
        return self._encoding

    def reset(self) -> None:
        """只给用例用：回到「还没加载过」"""
        with self._lock:
            self._encoding = None
            self._failed = False


def _load_encoding(path: Path) -> Any:
    """从随代码发布的文件构造 `o200k_base`；内容摘要对不上就拒绝（宁可估算也不用被改过的表）"""
    import tiktoken

    data = gzip.decompress(path.read_bytes())
    digest = hashlib.sha256(data).hexdigest()
    if digest != ENCODING_SHA256:
        raise ValueError(f"{path.name} 的内容摘要不对（{digest[:16]}…），文件可能被改过或损坏")
    ranks: dict[bytes, int] = {}
    for line in data.splitlines():
        if not line:
            continue
        token, rank = line.split()
        ranks[base64.b64decode(token)] = int(rank)
    return tiktoken.Encoding(
        name=ENCODING_NAME,
        pat_str=O200K_PAT_STR,
        mergeable_ranks=ranks,
        special_tokens=dict(O200K_SPECIAL_TOKENS),
    )


_holder = _EncodingHolder()
_cache: dict[tuple[str, bytes], int] = {}
_cache_lock = threading.Lock()


def _count_with_encoding(encoding: Any, text: str) -> int:
    """分段数：每段至多 `CHUNK_CHARS` 字，尽量切在空白或标点上

    BPE 合并在一个不被正则切开的长片段上是平方级的：中日韩正文里没有空格，一大段不带标点的
    文字就是一个片段——实测 1.6 万字要 0.6 秒，百万字（单条正文的安全上限）要几十分钟，
    任何能写条目的人都能借此把计数卡死。分段以后每段毫秒级。切口优先落在不是字母数字的字符
    之前（空白归下一段，英文的「空格 + 词」仍是一个 token）或标点之后（那里本来就是 token 边界），
    找不到才硬切；硬切最多让每段多出一个 token，相对预算可以忽略。
    """
    length = len(text)
    if length <= CHUNK_CHARS:
        return len(encoding.encode_ordinary(text))
    total = 0
    start = 0
    while start < length:
        end = min(length, start + CHUNK_CHARS)
        if end < length:
            floor = max(start + CHUNK_CHARS // 2, end - CHUNK_BACKTRACK)
            for index in range(end, floor, -1):
                if text[index].isspace() or not (text[index - 1].isalnum() or text[index - 1].isspace()):
                    end = index
                    break
        total += len(encoding.encode_ordinary(text[start:end]))
        start = end
    return total


def ensure_loaded() -> bool:
    """加载编码（只在第一次真正读文件），返回能否按 `o200k_base` 数"""
    return _holder.get() is not None


async def ensure_loaded_async() -> bool:
    """同上，放到线程里做：第一次加载要一两秒，不能卡住事件循环"""
    if _holder.attempted:
        return _holder.get() is not None
    return await asyncio.to_thread(ensure_loaded)


def estimator() -> str:
    """现在用的计数方式：`tiktoken` / `chars`（还没尝试加载时按会去加载算）"""
    return "tiktoken" if _holder.get() is not None else "chars"


def estimate_by_chars(text: str) -> int:
    """字符估算（不含系数）：中日韩字符 × 0.78、其它字符 ÷ 4，向上取整"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return math.ceil(cjk * ESTIMATE_CJK_TOKENS_PER_CHAR + other / ESTIMATE_OTHER_CHARS_PER_TOKEN)


def base_count(text: str) -> int:
    """`o200k_base` 下的 token 数（不含系数）；编码不可用时是字符估算。按内容摘要缓存"""
    if not text:
        return 0
    encoding = _holder.get()
    method = "tiktoken" if encoding is not None else "chars"
    key = (method, hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest())
    cached = _cache.get(key)
    if cached is not None:
        return cached
    value = _count_with_encoding(encoding, text) if encoding is not None else estimate_by_chars(text)
    with _cache_lock:
        if len(_cache) >= MAX_CACHE_ENTRIES:
            for stale in list(_cache)[: max(1, len(_cache) // 2)]:
                _cache.pop(stale, None)
        _cache[key] = value
    return value


def _base_counts_sync(texts: list[str]) -> list[int]:
    return [base_count(text) for text in texts]


async def base_counts(texts: Iterable[str]) -> list[int]:
    """一批正文的原始 token 数（不含系数），顺序与传入的一致

    合计不超过 :data:`PREFETCH_INLINE_CHARS` 字时直接在当前线程做；超过就把**摘要和计数一起**
    放到线程里——缓存键是整段正文的摘要，百万字的条目光摘要就要几毫秒，命中缓存也一样要算，
    每轮命中几条长条目时不能都压在事件循环上。第一次数一条百万字的条目要两三秒，更不能。
    """
    await ensure_loaded_async()
    items = [text or "" for text in texts]
    if sum(len(text) for text in items) <= PREFETCH_INLINE_CHARS:
        return _base_counts_sync(items)
    return await asyncio.to_thread(_base_counts_sync, items)


async def prefetch(texts: Iterable[str]) -> None:
    """先把一批正文数进缓存（规则同 :func:`base_counts`），之后同步的 :func:`count_tokens` 只查缓存"""
    await base_counts(texts)


def prefetch_sync(texts: Iterable[str]) -> None:
    """在当前线程把一批正文数进缓存

    给已经跑在自己执行器里的任务用（世界书导入、卡片检查的分析）：它们不该再把计数交给
    `asyncio.to_thread` 的默认线程池。
    """
    ensure_loaded()
    _base_counts_sync([text or "" for text in texts])


def apply_factor(raw: int, factor: float = DEFAULT_FACTOR) -> int:
    """原始数按系数折算：`round(原始数 × 系数)`，非空正文至少 1"""
    if raw <= 0:
        return 0
    return max(1, round(raw * factor))


def count_tokens(text: str, factor: float = DEFAULT_FACTOR) -> int:
    """按系数折算后的 token 数：`round(原始数 × 系数)`，非空正文至少 1"""
    return apply_factor(base_count(text), factor)


def approx_chars(tokens: int, factor: float = DEFAULT_FACTOR) -> int:
    """「约合多少中文字」：给界面上「约 M 字」用，按中文正文的 0.78 token / 字与系数反算"""
    per_char = ESTIMATE_CJK_TOKENS_PER_CHAR * (factor if factor > 0 else DEFAULT_FACTOR)
    return max(0, round(tokens / per_char))


def clear_cache() -> None:
    """只给用例用：清空原始 token 数缓存"""
    with _cache_lock:
        _cache.clear()


def reset_for_tests() -> None:
    """只给用例用：编码回到「还没加载过」并清空缓存"""
    _holder.reset()
    clear_cache()


__all__ = [
    "DEFAULT_FACTOR",
    "ENCODING_FILE",
    "ENCODING_SHA256",
    "MAX_FACTOR",
    "MODEL_FAMILY_FACTORS",
    "TokenFactor",
    "apply_factor",
    "approx_chars",
    "base_count",
    "base_counts",
    "count_tokens",
    "ensure_loaded",
    "ensure_loaded_async",
    "estimate_by_chars",
    "estimator",
    "factor_for_group",
    "factor_for_model_name",
    "prefetch",
    "prefetch_sync",
]
