"""世界书条目里的 EJS 模板：安全子集，只解析、不执行

## 为什么要有这一层

酒馆的「提示词模板」扩展（ST-Prompt-Template）会在世界书条目里执行 EJS：`<% if (...) { %>`
按变量挑段落、`<%= getvar('…') %>` 把变量打出来。它执行的是卡片作者写的 JavaScript，
能碰到整个浏览器页面（`localStorage`、`window`、网络）。NA 不执行第三方代码；不执行又原样
注入的话，模型会读到大段代码，有的卡还专门针对「不执行模板的客户端」埋了诱饵——把几千字
不相干甚至辱骂的文字包在一个永远不成立的判断里，执行模板的客户端看不到，原样注入的客户端
全吃进去。

这里写的是一个**只解析、不执行**的求值器：认一小撮写法，按 JS 的语义算出该输出哪些段落；
认不出的写法一律判「含不支持的模板」，整条不注入。失败方向是安全的——宁可少注入一条，
也不让代码或诱饵进提示词。

## 认的写法

标签（与 ejs 3 一致）：`<% %>` 代码、`<%= %>` / `<%- %>` 输出（一律当纯文本，不做 HTML
转义）、`<%# %>` 注释、`<%_` / `_%>` 吞掉标签外侧同一行的空格与制表符、`-%>` / `_%>` 吞掉
紧跟的一个换行、`<%%` 输出字面的 `<%`。

代码里只认：

- 语句：`if (…) { … }`、`else if (…) { … }`、`else { … }`（花括号必须写）、空语句 `;`、
  `//` 与 `/* */` 注释；
- 取值：`getvar('a.b')`、`getLocalVar('…')`（参数只能是一个字符串字面量）、`String(…)`；
- 字面量：字符串（单 / 双引号）、数字、`true` / `false` / `null` / `undefined`；
- 运算：`. 属性`（属性名可以是中文）、`.includes(…)`、`.toLowerCase()`、`.length`、
  `=== !== == != > >= < <=`、`&& || !`、括号。

其它任何写法——赋值、`var` / `let` / `const`、函数、箭头函数、循环、`try`、模板字符串、
`localStorage` / `window` 之类的标识符、`getwi` / `execute` 等别的函数——都判为不支持。

## 求值语义

按 JS：`undefined` 与 `null` 是两个值；`&&` / `||` 返回操作数本身；`==` 走宽松相等；比较
运算两边都是字符串时按字典序，否则转成数字（转不成就是 NaN，任何比较都不成立）；真假按 JS
（`0`、`""`、`NaN`、`null`、`undefined`、`false` 为假，对象与数组一律为真）。对 `undefined` /
`null` 取属性或调方法在 JS 里会抛 `TypeError`，这里抛 :class:`TemplateRuntimeError`，调用方
按「这一条不注入」处理——酒馆那边同样会报模板错误。

变量来自 :class:`VariableProvider`。状态功能还没有，运行时用 :data:`EMPTY_VARIABLES`：所有
变量都取不到（`undefined`），按变量挑的段落就都不出现，与「没有变量」时的酒馆一致。

## 上限（条目正文与以后的变量值都按恶意输入看）

- **正文**：带模板的正文超过 :data:`MAX_TEMPLATE_CHARS` 字直接判不支持，不切词、不建树。
- **结构**：语句与括号、`!` 的嵌套超过 :data:`MAX_NESTING` 层判不支持（这是解析器自己的
  递归深度）；表达式树的**实际深度**——取属性、比较、相等的连写都会让树变深——超过
  :data:`MAX_EXPR_DEPTH` 也判不支持（这是求值时的递归深度）。`&&` / `||` 的连写拍平成一个
  节点，不加深度。所以求值、静态检查都不会撞 Python 的递归上限。
- **输出**：一条渲染出来超过 :data:`MAX_OUTPUT_CHARS` 字按求值出错处理。变量为空时输出只由
  原文里的字面文字拼成，不会比原文长；有了变量提供者以后，`<%= getvar('x') %>` 写一千遍就会把
  一个长变量放大一千倍，所以这里一直设着上限。变量值嵌套过深（列表套列表）同样按出错处理。
- **原因文字**：不支持的原因只带一小段原文，最长 :data:`MAX_REASON_CHARS` 字。

切词、切段与宽松扫描都是手写的单遍扫描或以字面量开头的正则，在构造的输入上也是线性的。
"""

from __future__ import annotations

import decimal
import hashlib
import hmac
import math
import random
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, Union

# 语句与括号、`!` 的嵌套上限：解析器自己的递归深度。真实卡片里最深不过三四层。
MAX_NESTING = 40
# 表达式树的实际深度上限：求值、静态检查的递归深度。
MAX_EXPR_DEPTH = 64
# 带模板的正文上限（字）。真实卡片里能处理的模板条目最长一万多字；陷阱条目两三万字，
# 它们本来就在子集之外。
MAX_TEMPLATE_CHARS = 64 * 1024
# 一条渲染出来的上限（字）。
MAX_OUTPUT_CHARS = 64 * 1024
# 不支持的原因最长多少字（会进缓存、条目列表与导入报告）。
MAX_REASON_CHARS = 80
# 变量值转字符串时允许的嵌套层数。
MAX_VALUE_DEPTH = 32
# 列表下标最多几位数字：`int()` 遇到四千多位的数字串会直接抛 `ValueError`（Python 的整数转换上限）。
MAX_INDEX_DIGITS = 9
# 宽松扫描（只给疑似防盗判断用）最多看正文的前多少字：陷阱的特征在开头就看得出来，
# 不必为一条一百万字的条目把整段都扫一遍。
MAX_SCAN_CHARS = 256 * 1024

# 判「疑似防盗」用的门槛：受条件控制的正文（或代码里的字符串）合计有这么多字，才算「按
# 判断结果输出大段正文」。真实的陷阱条目动辄几千到几万字；几百字的条件段落是正常写法。
TRAP_MIN_CHARS = 1000

# 模板里出现这些标识符就说明它在探测运行环境（浏览器存储、页面对象、宿主程序）。
# 这类写法本来就在子集之外，列出来只为在报告里说清「为什么疑似防盗」。
ENV_IDENTIFIERS = frozenset(
    {
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "window",
        "document",
        "navigator",
        "location",
        "globalThis",
        "self",
        "top",
        "parent",
        "frames",
        "process",
        "require",
        "SillyTavern",
        "TavernHelper",
        "chrome",
    },
)


class _Undefined:
    """JS 的 `undefined`；单例，与 Python 的 `None`（对应 JS 的 `null`）分开"""

    _instance: "_Undefined | None" = None

    def __new__(cls) -> "_Undefined":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "undefined"

    def __bool__(self) -> bool:
        return False


UNDEFINED = _Undefined()


class TemplateUnsupported(ValueError):
    """模板里有子集之外的写法；消息是给管理员看的中文原因（不长于 :data:`MAX_REASON_CHARS`）"""


class TemplateRuntimeError(ValueError):
    """求值时出了 JS 里会抛 `TypeError` 的情况（对 `undefined` 取属性之类），或者输出超限"""


def _snippet(value: Any, limit: int = 16) -> str:
    """原因文字里引用的一小段原文"""
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_reason(text: str) -> str:
    """把原因文字截到 :data:`MAX_REASON_CHARS` 字以内"""
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 1] + "…"


class VariableProvider(Protocol):
    """模板读变量的来源

    `scope` 是 `"cache"`（`getvar`）或 `"local"`（`getLocalVar`）；`path` 是点号路径原文。
    取不到返回 :data:`UNDEFINED`。返回值只应是 JSON 形状（字典、列表、字符串、数字、布尔、
    `None`），求值器不会对它调任何方法。

    给以后的状态功能（变量值会受群友影响）留的两条约束：

    - **每轮一个实例**，值只活在这一轮的求值里，绝不进 `purpose` 模块的分析缓存——那份缓存
      按条目共享，混进某一轮、某个频道的变量值就成了跨轮、跨频道的泄漏；
    - 渲染结果之后还会做宏展开（`round_prompt` 的 `render_block(expand=…)`），变量值里的
      `{{…}}` 会被当成宏。接入真实变量时，要么先把变量值里的 `{{` 转义，要么先展开宏再求模板。
    """

    def get(self, scope: str, path: str) -> Any: ...


class _EmptyVariables:
    """什么变量都取不到：状态功能上线之前的唯一提供者"""

    def get(self, scope: str, path: str) -> Any:  # noqa: ARG002 - 协议签名
        return UNDEFINED


EMPTY_VARIABLES: VariableProvider = _EmptyVariables()


class _SoftEmptyVariables:
    """诱饵判断的备用基线：什么都读不到，并且对 `undefined` 取属性、调方法得到 `undefined`（不抛错）

    空变量下按 JS 会出错的模板（MVU 卡里常见 `getvar('stat_data').a.b`）在空提供者下不注入，可是有了
    真实变量就算得出字来；它们照样要判诱饵，基线就用这个软的世界（语义同 :class:`_PartialVariables`
    的 `soft`）。
    """

    soft = True

    def get(self, scope: str, path: str) -> Any:  # noqa: ARG002 - 协议签名
        return UNDEFINED


_SOFT_EMPTY_VARIABLES = _SoftEmptyVariables()


class MappingVariables:
    """从一棵普通字典按点号路径取值；给用例与以后的状态功能用

    路径按 `.` 切开逐层取（与 lodash `_.get` 的点号写法一致，不认 `[0]` 这类下标写法）；
    取列表元素的那一段只认 ASCII 数字（`'²'.isdigit()` 为真，`int('²')` 却会抛错）。
    """

    def __init__(self, cache: Mapping[str, Any] | None = None, local: Mapping[str, Any] | None = None) -> None:
        self._scopes = {"cache": cache or {}, "local": local or {}}

    def get(self, scope: str, path: str) -> Any:
        node: Any = self._scopes.get(scope, {})
        for part in path.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            elif (
                isinstance(node, list)
                and part.isascii()
                and part.isdigit()
                and len(part) <= MAX_INDEX_DIGITS
                and int(part) < len(node)
            ):
                node = node[int(part)]
            else:
                return UNDEFINED
        return node


# 诱饵判断（见 :func:`compare_renders`）用四组哨兵模拟「变量都有」：
#
# - 「短而靠前」：8 个字，以 4 个 U+0001 开头（按 UTF-16 码元排，比任何可见的字都靠前）；
# - 「长而靠后」：24 个字，以 20 个 U+FFFF 开头（比任何字都靠后；同一组里序号的先后与上一组一样）；
# - 「随机小写」：十六进制的小写字母与数字（第一位是字母），一组一个长度（8 ~ 24），字符随机；
# - 「随机混合」：开头一个字从西里尔字母、汉字、谚文、全角字母几段互不相交的区间里取（只有一个字时
#   是西里尔或全角的大写字母），夹着大写字母，每个变量各取一个长度（1 ~ 24，前 24 个变量互不相同）。
#
# 随机的两组用种子喂的伪随机数取，种子 = 密钥对正文的 HMAC：同一条正文得到同一组哨兵，导入报告、
# 书详情与运行时的结论一致。密钥由调用方给（服务端一把持久的密钥派生出来的，重启前后不变）；不给就用
# 每个进程启动时随机取的一把。条目作者事先算不出具体的哨兵，但哨兵的**形状**是公开的（长度范围、开头
# 落在哪几段、大小写），所以：
#
# - 只看一个条件的按形状认哨兵的写法，总有一组走另一边：按字典序跟一个常量比（`v < '{'`、
#   `v < '\uff00'`、`v > 'Z'`）由两组固定的哨兵在两头兜住；转小写（`v.toLowerCase() === v`）由混合
#   那组兜住；按长度（`v.length >= 8`）由「只有一部分变量」那几次兜住（那里 `.length` 读不到）。
# - 几个条件或起来、每个条件对上一组的形状（`v < '\u0002' || v >= '\uffff' || 以 a ~ f 开头 ||
#   v.toLowerCase() != v`），就能把四组都认出来：这种写法判不出。哨兵只有 24 个字长，按长度上限或
#   贴着字典序两头写的条件（`!(v.length > 24)`、`v < '\uffff'` 连写二十几个）同样判不出。
_SENTINEL_MIN_LENGTH = 8
_SENTINEL_MAX_LENGTH = 24
_SENTINEL_INDEX_WIDTH = 4
_SENTINEL_LOW = chr(0x0001)
_SENTINEL_HIGH = chr(0xFFFF)
# 没给密钥时用的那把：每个进程启动时随机取。
_SENTINEL_SEED_KEY = secrets.token_bytes(32)
# 「随机混合」那组：最短一个字；开头的字从这几段里取（都不是空白、不是数字）。
_MIXED_SHORTEST = 1
_MIXED_ASCII = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_MIXED_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_MIXED_HEAD_RANGES = ((0x0410, 0x044F), (0x4E00, 0x9FA5), (0xAC00, 0xD7A3), (0xFF21, 0xFF5A))
_CJK_FIRST = 0x4E00
_CJK_LAST = 0x9FA5
_CYRILLIC_UPPER_A = 0x0410
_FULLWIDTH_UPPER_A = 0xFF21


class _Sentinel(str):
    """「变量都有」时一个变量的值：非空、为真、各不相同的字符串；取它的属性得到另一个哨兵

    `getvar('s').a.b` 在真有变量的客户端里能取到，这里也不能因为字符串没有 `.a` 就抛错。取属性
    按点号路径归一：`getvar('s').a` 与 `getvar('s.a')` 是同一个哨兵（与 lodash `_.get` 一致）。
    `.length` 照字符串算（一个正数）。取属性实际由求值时的那一组变量决定（见
    :meth:`_SentinelVariables.prop`、:class:`_PartialVariables`）。
    """

    owner: "_SentinelVariables"
    scope: str
    path: str
    # 「只有一部分变量」那几次用：取过的属性（属性名 → 哨兵），省得每次都拼一遍路径、查一遍字典；
    # 它在路径树上的位置（见 :class:`_PathIndex`）。都是用到时才填。
    children: "dict[str, _Sentinel] | None" = None
    chain: "tuple[int, ...] | None" = None

    def prop(self, name: str) -> Any:
        return self.owner.prop(self, name)

    def lowered(self) -> "_Sentinel":
        """JS 的 `toLowerCase()`：没有大写字母就是它自己，否则是同一个变量的小写版本"""
        text = str.lower(self)
        if text == self:
            return self
        value = _Sentinel(text)
        value.owner = self.owner
        value.scope = self.scope
        value.path = self.path
        return value


# 两位小写字母的全部组合（`aa` … `zz`），按 26 进制的大小排好：拼序号时一次查两位。
_LETTER_PAIRS = tuple(chr(ord("a") + high) + chr(ord("a") + low) for high in range(26) for low in range(26))


def _index_letters(index: int) -> str:
    """序号写成至少 4 位的小写字母（26 进制）：同一组哨兵里各不相同"""
    high, low = divmod(index, len(_LETTER_PAIRS))
    if high < len(_LETTER_PAIRS):
        return _LETTER_PAIRS[high] + _LETTER_PAIRS[low]
    letters: list[str] = []
    while True:
        index, digit = divmod(index, 26)
        letters.append(chr(ord("a") + digit))
        if index == 0 and len(letters) >= _SENTINEL_INDEX_WIDTH:
            break
    return "".join(reversed(letters))


def _short_low(index: int) -> str:
    return _SENTINEL_LOW * (_SENTINEL_MIN_LENGTH - _SENTINEL_INDEX_WIDTH) + _index_letters(index)


def _long_high(index: int) -> str:
    # 序号的先后与「短而靠前」那组一样：两个变量互相比大小不靠这几组定（见 :func:`_compare`）
    return _SENTINEL_HIGH * (_SENTINEL_MAX_LENGTH - _SENTINEL_INDEX_WIDTH) + _index_letters(index)


def _seeded_rng(label: str, seed: str, key: bytes | None) -> random.Random:
    message = f"{label}\0{seed}".encode("utf-8", "surrogatepass")
    material = hmac.new(key if key is not None else _SENTINEL_SEED_KEY, message, hashlib.sha256).digest()
    return random.Random(int.from_bytes(material, "big"))


# 随机的两组每个哨兵只调一次 `getrandbits`，按十六进制展开再换成这一组的字（几万个哨兵的模板也不慢）。
_HEX_DIGITS = "0123456789abcdef"


def _seeded_maker(seed: str, key: bytes | None = None) -> Callable[[int], str]:
    """「随机小写」那组：十六进制的小写字母与数字，一组一个长度

    第一位总是字母：全是数字（或者 `1e5` 这样）的哨兵会被当成数，`getvar(x) > 0` 这种按值比的条件
    就会随抽到的哨兵变。
    """
    rng = _seeded_rng("lower", seed, key)
    length = rng.randint(_SENTINEL_MIN_LENGTH, _SENTINEL_MAX_LENGTH)
    spec = f"0{length - 1}x"

    def make(_index: int) -> str:
        bits = rng.getrandbits(4 * (length - 1) + 8)
        return _HEX_DIGITS[10 + (bits & 0xFF) % 6] + format(bits >> 8, spec)

    return make


def _mixed_maker(seed: str, key: bytes | None = None) -> Callable[[int], str]:
    """「随机混合」那组：每个变量各取一个长度，前 24 个变量的长度互不相同

    开头一个字从 :data:`_MIXED_HEAD_RANGES` 几段里各随机挑 4 个、共 16 个里取；中间从这一组随机挑的
    8 个 ASCII 字母数字与 8 个汉字里取，再在开头之后的某处插一个大写字母；只有一个字的是西里尔或全角的
    大写字母。
    """
    rng = _seeded_rng("mixed", seed, key)
    lengths = list(range(_MIXED_SHORTEST, _SENTINEL_MAX_LENGTH + 1))
    rng.shuffle(lengths)
    heads = [chr(rng.randint(low, high)) for low, high in _MIXED_HEAD_RANGES for _ in range(4)]
    cjk = [chr(rng.randint(_CJK_FIRST, _CJK_LAST)) for _ in range(8)]
    table = str.maketrans(_HEX_DIGITS, "".join(rng.sample(_MIXED_ASCII, 8) + cjk))

    def make(index: int) -> str:
        length = lengths[index] if index < len(lengths) else rng.randint(_MIXED_SHORTEST, _SENTINEL_MAX_LENGTH)
        if length == 1:
            pick = rng.randrange(52)
            return chr(_CYRILLIC_UPPER_A + pick) if pick < 26 else chr(_FULLWIDTH_UPPER_A + pick - 26)
        bits = rng.getrandbits(4 * (length - 2) + 16)
        middle = format(bits >> 16, f"0{length - 2}x").translate(table) if length > 2 else ""
        pick, first = divmod(bits & 0xFFFF, 16)
        pick, upper = divmod(pick, 26)
        at = pick % (length - 1)
        return heads[first] + middle[:at] + _MIXED_UPPER[upper] + middle[at:]

    return make


class _SentinelVariables:
    """每条变量路径都返回一个唯一的真值哨兵：模拟「变量都有」的客户端，只给诱饵判断用

    `maker(序号)` 决定这一组哨兵长什么样；同一组里撞了（按码位相同）就再取一个（随机的两组才
    可能撞）。
    """

    def __init__(self, maker: Callable[[int], str] | None = None) -> None:
        self._make = maker if maker is not None else _seeded_maker(secrets.token_hex(16))
        self._values: dict[tuple[str, str], _Sentinel] = {}
        self._texts: set[str] = set()

    def get(self, scope: str, path: str) -> Any:
        key = (scope, path)
        value = self._values.get(key)
        if value is None:
            text = self._make(len(self._values))
            while text in self._texts:
                text = self._make(len(self._texts) + len(self._values))
            value = _Sentinel(text)
            value.owner = self
            value.scope = scope
            value.path = path
            self._values[key] = value
            self._texts.add(text)
        return value

    def prop(self, value: _Sentinel, name: str) -> Any:
        if name == "length":
            return len(value)
        return self.get(value.scope, f"{value.path}.{name}")


class _PathIndex:
    """按路径拿掉变量用的索引：模板里静态可知的变量路径按前缀闭合成的一棵树（见 :func:`_unit_tree`）

    树上每个节点（`(作用域, 点号路径)`，最深 :data:`MAX_UNIT_DEPTH` 层）有一个编号；:meth:`chain` 给出
    一条路径从根往下经过的那些节点的编号。更深的路径跟着它在这一层的祖先走，不在树上的路径只认它
    在树上的那几层祖先。
    """

    def __init__(self, units: dict[tuple[str, str], int]) -> None:
        self._units = units
        self._chains: dict[tuple[str, str], tuple[int, ...]] = {}

    def chain(self, scope: str, path: str) -> tuple[int, ...]:
        key = (scope, path)
        found = self._chains.get(key)
        if found is None:
            nodes: list[int] = []
            prefix = ""
            for depth, part in enumerate(path.split(".", MAX_UNIT_DEPTH)[:MAX_UNIT_DEPTH]):
                prefix = part if depth == 0 else f"{prefix}.{part}"
                unit = self._units.get((scope, prefix))
                if unit is None:
                    break
                nodes.append(unit)
            found = self._chains[key] = tuple(nodes)
        return found


class _PartialVariables:
    """在「变量都有」那一组哨兵上改出来的世界：拿掉一部分变量路径，或者只有根变量

    - 拿掉路径：第 `depth` 层里编号在 `missing` 里的那些节点，连同它们下面的路径都读不到
      （`getvar('s.a')` 拿掉了，`getvar('s.a.b')`、`getvar('s').a` 也读不到；`s` 与 `s.b` 照旧）；
    - `shallow`：根变量都有，往下一层都读不到——`getvar('s.a')`、`getvar('s').a`、
      `getvar('s').length` 都是 `undefined`（像一个没有这些键的对象）。

    真变量的客户端里一个变量、一个字段没设，或者设成了别的形状，是很平常的事，诱饵也能借它藏起来
    （`!getvar('a') || getvar('从来不设')`、`!getvar('s.a') || getvar('s.从来不设')`、`!v || v.从来不设`）。
    这些世界里对 `undefined` 取属性、调方法不抛错，得到 `undefined`：只为了比出哪些段落会不见。JS 里
    这样会抛错，可要是这里也照抛，条目作者只要放一句只在这种世界里才走得到的出错代码（作者自己的
    客户端走不到），就能让这次比较作废。
    """

    soft = True

    def __init__(
        self,
        base: _SentinelVariables,
        *,
        index: _PathIndex | None = None,
        depth: int = 0,
        missing: frozenset[int] = frozenset(),
        shallow: bool = False,
    ) -> None:
        self._base = base
        self._index = index
        self._depth = depth
        self._missing = missing
        self._shallow = shallow

    def get(self, scope: str, path: str) -> Any:
        if self._shallow:
            if "." in path:
                return UNDEFINED
        elif self._index is not None:
            chain = self._index.chain(scope, path)
            if len(chain) >= self._depth and chain[self._depth - 1] in self._missing:
                return UNDEFINED
            value = self._base.get(scope, path)
            if value.chain is None:
                value.chain = chain
            return value
        return self._base.get(scope, path)

    def prop(self, value: _Sentinel, name: str) -> Any:
        if self._shallow:
            return UNDEFINED
        # 这几次渲染反复走同一批哨兵：取过的属性记在哨兵上（「变量都有」那几次每个只走一遍，不记）
        children = value.children
        child = children.get(name) if children is not None else None
        if child is None:
            child = self._base.prop(value, name)
            # `.length` 是字符串自己的：变量在，它就在
            if name == "length":
                return child
            if children is None:
                children = value.children = {}
            children[name] = child
        if self._depth <= 1 or self._index is None:
            # 拿掉的是根变量：取得到它，就取得到它下面的
            return child
        parent = value.chain
        if parent is not None and len(parent) >= self._depth:
            # 它在这一层的祖先已经核过（不然取不到它），下面的路径跟着同一个祖先
            return child
        chain = child.chain
        if chain is None:
            # 同一次比较里各个世界共用这棵路径树，算一次记在哨兵上
            chain = child.chain = self._index.chain(child.scope, child.path)
        if len(chain) >= self._depth and chain[self._depth - 1] in self._missing:
            return UNDEFINED
        return child


# ---------------------------------------------------------------------------
# 第一层：按 ejs 的标签规则把原文切成文字段与代码段
# ---------------------------------------------------------------------------

_TAG = re.compile(r"(<%%|%%>|<%=|<%-|<%_|<%#|<%|%>|-%>|_%>)")
_SLURP_CLOSE = re.compile(r"_%>[ \t]*")
_LEADING_NEWLINE = re.compile(r"^(?:\r\n|\r|\n)")


def _slurp_open(source: str) -> str:
    """`[ \\t]*<%_` → `<%_`：去掉 `<%_` 前面同一行的空格与制表符

    不用正则：`[ \\t]*<%_` 在一长串空格上会对每个起点回溯一遍，是二次方的。这里每个字符
    最多看两遍。
    """
    if "<%_" not in source:
        return source
    out: list[str] = []
    start = 0
    index = source.find("<%_")
    while index >= 0:
        cut = index
        while cut > start and source[cut - 1] in " \t":
            cut -= 1
        out.append(source[start:cut])
        out.append("<%_")
        start = index + 3
        index = source.find("<%_", start)
    out.append(source[start:])
    return "".join(out)


@dataclass(frozen=True)
class _Segment:
    """切出来的一段：`text` 文字、`eval` 代码、`escaped` / `raw` 输出（`comment` 已丢弃）"""

    kind: str
    body: str


def has_template(text: str) -> bool:
    """正文里有没有模板标签；没有的条目不经过本模块，注入的文字与原文逐字相同"""
    return "<%" in text


def _segments(source: str) -> list[_Segment]:
    """按 ejs 3 的扫描规则切段；找不到配对的结束标签就判不支持

    几个与直觉不同、但与 ejs 一致的地方：

    - `<%%` 进入「字面模式」：直到下一个结束标签为止都原样当文字输出，结束标签本身也输出；
    - 文字里落单的 `%>` 会被吞掉（ejs 在文字模式下遇到结束标签只是复位模式）；
    - `-%>` 与 `_%>` 之后紧跟的一个换行被吞掉。
    """
    prepared = _SLURP_CLOSE.sub("_%>", _slurp_open(source))
    parts = _TAG.split(prepared)
    segments: list[_Segment] = []
    mode: str | None = None
    buffer: list[str] = []
    truncate = False

    def add_text(text: str) -> None:
        if text:
            segments.append(_Segment("text", text))

    for index, part in enumerate(parts):
        if index % 2 == 0:
            text = part
            if truncate:
                text = _LEADING_NEWLINE.sub("", text, count=1)
                truncate = False
            if mode is None or mode == "literal":
                add_text(text)
            else:
                buffer.append(text)
            continue

        tag = part
        if tag in ("%>", "-%>", "_%>"):
            if mode == "literal":
                add_text(tag)
            elif mode in ("eval", "escaped", "raw"):
                segments.append(_Segment(mode, "".join(buffer)))
            # 注释与文字模式下的落单结束标签：什么也不输出（与 ejs 一致）
            buffer = []
            mode = None
            truncate = tag[0] in "-_"
            continue
        if tag == "<%%":
            if mode is not None:
                raise TemplateUnsupported("模板标签里又出现了开始标签")
            add_text("<%")
            mode = "literal"
            continue
        if tag == "%%>":
            if mode not in (None, "literal"):
                raise TemplateUnsupported("代码里出现了 %%>")
            add_text("%>")
            mode = "literal"
            continue
        if mode is not None and mode != "literal":
            raise TemplateUnsupported("模板标签没有闭合就开始了下一个标签")
        if mode == "literal":
            add_text(tag)
            continue
        mode = {"<%=": "escaped", "<%-": "raw", "<%#": "comment"}.get(tag, "eval")
        buffer = []

    if mode is not None and mode != "literal":
        raise TemplateUnsupported("模板标签没有闭合")
    return segments


# ---------------------------------------------------------------------------
# 第二层：代码段的词法
# ---------------------------------------------------------------------------

_PUNCTUATORS = ("===", "!==", "==", "!=", ">=", "<=", "&&", "||", "(", ")", "{", "}", ".", ",", ";", "!", ">", "<")
# JS 标识符可以是任何 Unicode 字母（卡片里常见 `getvar('主角档案').模式` 这种中文属性名）。
_IDENT = re.compile(r"(?:[^\W\d]|\$)(?:\w|\$)*")
# 数字字面量只认 ASCII 数字（`\d` 还认全角、阿拉伯-印度等别的文字的数字，JS 不认）。
_NUMBER = re.compile(r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
# JS 源码里的换行（`//` 注释到这里为止）。
_LINE_TERMINATOR = re.compile("[\n\r" + chr(0x2028) + chr(0x2029) + "]")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
# JS 里任何对象都从原型上继承的名字：`({}).constructor`、`'x'.constructor` 是函数（为真），经 lodash
# `_.get` 取的变量路径（`getvar('s.toString')`）也一样；这里的映射与变量路径都没有，照搬会得到
# undefined。取属性与变量路径里出现这些名字一律判不支持。
_PROTOTYPE_NAMES = frozenset(
    {
        "constructor",
        "__proto__",
        "toString",
        "toLocaleString",
        "valueOf",
        "hasOwnProperty",
        "isPrototypeOf",
        "propertyIsEnumerable",
        "__defineGetter__",
        "__defineSetter__",
        "__lookupGetter__",
        "__lookupSetter__",
    }
)


@dataclass(frozen=True)
class _Token:
    """词法单元：`punct` / `ident` / `string` / `number`，以及拼进来的 `text` / `output`"""

    kind: str
    value: Any


def _lex(code: str) -> list[_Token]:
    """把一段代码切成词；碰到子集之外的字符就判不支持"""
    tokens: list[_Token] = []
    pos = 0
    length = len(code)
    while pos < length:
        char = code[pos]
        if char in _JS_WHITESPACE:
            pos += 1
            continue
        if code.startswith("//", pos):
            terminator = _LINE_TERMINATOR.search(code, pos)
            pos = length if terminator is None else terminator.end()
            continue
        if code.startswith("/*", pos):
            end = code.find("*/", pos + 2)
            if end < 0:
                raise TemplateUnsupported("注释没有闭合")
            pos = end + 2
            continue
        if char in "'\"":
            value, pos = _lex_string(code, pos)
            tokens.append(_Token("string", value))
            continue
        if "0" <= char <= "9" or (char == "." and pos + 1 < length and "0" <= code[pos + 1] <= "9"):
            number = _NUMBER.match(code, pos)
            if number is not None:
                literal = number.group(0)
                if len(literal) > 1 and literal[0] == "0" and "0" <= literal[1] <= "9":
                    # `010` 在非严格模式的 JS 里是八进制（8），`09` 又是十进制：照搬容易算错，判不支持
                    raise TemplateUnsupported(f"以 0 开头的数字 {_snippet(literal)}")
                tokens.append(_Token("number", float(literal)))
                pos = number.end()
                continue
        ident = _IDENT.match(code, pos)
        if ident:
            tokens.append(_Token("ident", ident.group(0)))
            pos = ident.end()
            continue
        for punct in _PUNCTUATORS:
            if code.startswith(punct, pos):
                tokens.append(_Token("punct", punct))
                pos += len(punct)
                break
        else:
            raise TemplateUnsupported(f"不支持的写法 {_snippet(code[pos : pos + 12])}")
    return tokens


def _join_string(parts: list[str]) -> str:
    """把字符串的各段拼起来；`\\uD83D\\uDE00` 这种成对的代理转义合成一个字符，落单的判不支持

    落单的代理字符进了提示词或接口的 JSON，编码 UTF-8 时会抛错；租户条目的清洗会把它删掉，
    表情就静默没了。
    """
    text = "".join(parts)
    if not any("\ud800" <= char <= "\udfff" for char in text):
        return text
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeDecodeError:
        raise TemplateUnsupported("字符串里有落单的代理字符") from None


def _lex_string(code: str, start: int) -> tuple[str, int]:
    """单 / 双引号字符串；转义只认常见几种，`\\u` 必须是四位十六进制

    与 JS 对不上的转义判不支持：`\\x41`（JS 里是 `A`）、`\\1` ~ `\\9` 与 `\\0` 后面接数字（宽松模式下的
    八进制，严格模式下是语法错误）、反斜杠接换行（JS 里是续行）。
    """
    quote = code[start]
    pos = start + 1
    out: list[str] = []
    while pos < len(code):
        char = code[pos]
        if char == quote:
            return _join_string(out), pos + 1
        if char in "\r\n":
            raise TemplateUnsupported("字符串里有换行")
        if char == "\\":
            pos += 1
            if pos >= len(code):
                break
            escaped = code[pos]
            if escaped == "u":
                digits = code[pos + 1 : pos + 5]
                if len(digits) != 4 or not all(c in "0123456789abcdefABCDEF" for c in digits):
                    raise TemplateUnsupported("字符串里有不支持的转义")
                out.append(chr(int(digits, 16)))
                pos += 5
                continue
            octal = escaped == "0" and pos + 1 < len(code) and "0" <= code[pos + 1] <= "9"
            if escaped == "x" or "1" <= escaped <= "9" or octal or _LINE_TERMINATOR.match(escaped):
                raise TemplateUnsupported("字符串里有不支持的转义")
            out.append(_ESCAPES.get(escaped, escaped))
            pos += 1
            continue
        out.append(char)
        pos += 1
    raise TemplateUnsupported("字符串没有闭合")


# ---------------------------------------------------------------------------
# 语法树：表达式节点都带着自己的深度（1 + 子节点里最深的），建节点时就检查上限
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False, repr=False)
class _Literal:
    value: Any
    depth: int = 1


@dataclass(frozen=True, eq=False, repr=False)
class _Var:
    scope: str
    path: str
    depth: int = 1


@dataclass(frozen=True, eq=False, repr=False)
class _ToString:
    operand: "Expr"
    depth: int = 1


@dataclass(frozen=True, eq=False, repr=False)
class _Prop:
    target: "Expr"
    name: str
    depth: int = 1


@dataclass(frozen=True, eq=False, repr=False)
class _Method:
    target: "Expr"
    name: str
    args: tuple["Expr", ...]
    depth: int = 1


@dataclass(frozen=True, eq=False, repr=False)
class _Not:
    operand: "Expr"
    depth: int = 1


@dataclass(frozen=True, eq=False, repr=False)
class _Binary:
    op: str
    left: "Expr"
    right: "Expr"
    depth: int = 1


@dataclass(frozen=True, eq=False, repr=False)
class _Logical:
    """`a && b && c` / `a || b || c` 拍平成一个节点

    这两个运算满足结合律，拍平之后求值是一个循环；几千个连写也只有一层深。
    """

    op: str
    operands: tuple["Expr", ...]
    depth: int = 1


# 语法树节点都是不可变的，但比较与哈希按对象身份、`repr` 不展开子树：`else if` 链能有两千多层，
# 默认生成的 `__eq__` / `__hash__` / `__repr__` 会顺着子树递归，撞上 Python 的递归上限。
Expr = Union[_Literal, _Var, _ToString, _Prop, _Method, _Not, _Binary, _Logical]


@dataclass(frozen=True, eq=False, repr=False)
class _Text:
    text: str


@dataclass(frozen=True, eq=False, repr=False)
class _Output:
    expr: Expr


@dataclass(frozen=True, eq=False, repr=False)
class _If:
    condition: Expr
    then: tuple["Node", ...]
    otherwise: tuple["Node", ...] = ()


Node = Union[_Text, _Output, _If]


@dataclass(frozen=True, eq=False, repr=False)
class Template:
    """编译好的模板

    Attributes:
        nodes: 语法树。
        source_chars: 原文字数。
        code_chars: 标签里的代码字数（含标签本身），用来判「模板代码占比」。
        node_count: 语法树的节点数，用来估缓存占多少内存。
        roots: 读到的根变量（`(作用域, 路径里第一个点号前那一段)`），不重复（先输出标签里的，再代码
            里的，各按出现的先后）。
    """

    nodes: tuple[Node, ...]
    source_chars: int
    code_chars: int
    node_count: int = 0
    roots: tuple[tuple[str, str], ...] = ()

    def __repr__(self) -> str:
        return f"Template(source_chars={self.source_chars}, node_count={self.node_count})"


# ---------------------------------------------------------------------------
# 第三层：把文字、输出与代码词拼成一条流，再递归下降
# ---------------------------------------------------------------------------

_METHODS = {"includes": 1, "toLowerCase": 0}
_LITERAL_IDENTS = {"true": True, "false": False, "null": None, "undefined": UNDEFINED}


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.nesting = 0
        self.node_count = 0
        self.roots: dict[tuple[str, str], None] = {}

    # 小工具 ---------------------------------------------------------------

    def _peek(self) -> _Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _is(self, kind: str, value: Any = None) -> bool:
        token = self._peek()
        return token is not None and token.kind == kind and (value is None or token.value == value)

    def _expect(self, kind: str, value: Any = None) -> _Token:
        token = self._peek()
        if token is None or token.kind != kind or (value is not None and token.value != value):
            got = "结尾" if token is None else _snippet(token.value)
            want = value if value is not None else kind
            raise TemplateUnsupported(f"需要 {want}，实际是 {got}")
        self.pos += 1
        return token

    def _enter(self) -> None:
        self.nesting += 1
        if self.nesting > MAX_NESTING:
            raise TemplateUnsupported(f"嵌套超过 {MAX_NESTING} 层")

    def _leave(self) -> None:
        self.nesting -= 1

    def _node(self, cls: type, *fields: Any, children: tuple[Expr, ...]) -> Any:
        """建一个表达式节点，深度 = 1 + 子节点里最深的；超过上限判不支持"""
        depth = 1 + max((child.depth for child in children), default=0)
        if depth > MAX_EXPR_DEPTH:
            raise TemplateUnsupported(f"表达式超过 {MAX_EXPR_DEPTH} 层")
        self.node_count += 1
        return cls(*fields, depth=depth)

    # 语句 -----------------------------------------------------------------

    def parse_block(self, *, top: bool) -> tuple[Node, ...]:
        nodes: list[Node] = []
        while True:
            token = self._peek()
            if token is None:
                if not top:
                    raise TemplateUnsupported("if 的花括号没有闭合")
                return tuple(nodes)
            if token.kind == "punct" and token.value == "}":
                if top:
                    raise TemplateUnsupported("多出来的 }")
                return tuple(nodes)
            if token.kind == "text":
                nodes.append(_Text(token.value))
                self.node_count += 1
                self.pos += 1
            elif token.kind == "output":
                nodes.append(_Output(token.value))
                self.node_count += 1
                self.pos += 1
            elif token.kind == "punct" and token.value == ";":
                self.pos += 1
            elif token.kind == "ident" and token.value == "if":
                nodes.append(self.parse_if())
            else:
                raise TemplateUnsupported(f"不支持的语句 {_snippet(token.value)}")

    def parse_if(self) -> _If:
        """`if … else if … else if … else …` 整条链在一个循环里读完

        `else if` 不算嵌套（它们在同一层），不计入 :data:`MAX_NESTING`；链再长也不递归。链在树里
        仍表示成层层套在 `otherwise` 里的 `_If`，所以用到它的地方（渲染、字数统计）都是迭代的。
        """
        self._enter()
        branches: list[tuple[Expr, tuple[Node, ...]]] = []
        otherwise: tuple[Node, ...] = ()
        while True:
            self._expect("ident", "if")
            self._expect("punct", "(")
            condition = self.parse_expr()
            self._expect("punct", ")")
            self._expect("punct", "{")
            then = self.parse_block(top=False)
            self._expect("punct", "}")
            branches.append((condition, then))
            if not self._is("ident", "else"):
                break
            self.pos += 1
            if self._is("ident", "if"):
                continue
            self._expect("punct", "{")
            otherwise = self.parse_block(top=False)
            self._expect("punct", "}")
            break
        self._leave()
        node: _If | None = None
        for condition, then in reversed(branches):
            node = _If(condition, then, otherwise)
            otherwise = (node,)
            self.node_count += 1
        assert node is not None
        return node

    # 表达式（JS 优先级：|| < && < 相等 < 比较 < 一元 < 后缀）---------------------

    def parse_expr(self) -> Expr:
        self._enter()
        operands = [self._parse_and()]
        while self._is("punct", "||"):
            self.pos += 1
            operands.append(self._parse_and())
        self._leave()
        if len(operands) == 1:
            return operands[0]
        return self._node(_Logical, "||", tuple(operands), children=tuple(operands))

    def _parse_and(self) -> Expr:
        operands = [self._parse_equality()]
        while self._is("punct", "&&"):
            self.pos += 1
            operands.append(self._parse_equality())
        if len(operands) == 1:
            return operands[0]
        return self._node(_Logical, "&&", tuple(operands), children=tuple(operands))

    def _parse_equality(self) -> Expr:
        left = self._parse_relational()
        while True:
            token = self._peek()
            if token is not None and token.kind == "punct" and token.value in ("===", "!==", "==", "!="):
                self.pos += 1
                right = self._parse_relational()
                left = self._node(_Binary, token.value, left, right, children=(left, right))
            else:
                return left

    def _parse_relational(self) -> Expr:
        left = self._parse_unary()
        while True:
            token = self._peek()
            if token is not None and token.kind == "punct" and token.value in (">", ">=", "<", "<="):
                self.pos += 1
                right = self._parse_unary()
                left = self._node(_Binary, token.value, left, right, children=(left, right))
            else:
                return left

    def _parse_unary(self) -> Expr:
        if self._is("punct", "!"):
            self.pos += 1
            self._enter()
            operand = self._parse_unary()
            self._leave()
            return self._node(_Not, operand, children=(operand,))
        return self._parse_postfix()

    def _parse_postfix(self) -> Expr:
        expr = self._parse_primary()
        while self._is("punct", "."):
            self.pos += 1
            name = self._expect("ident").value
            if self._is("punct", "("):
                if name not in _METHODS:
                    raise TemplateUnsupported(f"不支持的方法 .{_snippet(name)}()")
                self.pos += 1
                args: list[Expr] = []
                if not self._is("punct", ")"):
                    args.append(self.parse_expr())
                    while self._is("punct", ","):
                        self.pos += 1
                        args.append(self.parse_expr())
                self._expect("punct", ")")
                if len(args) != _METHODS[name]:
                    raise TemplateUnsupported(f".{name}() 的参数个数不对")
                expr = self._node(_Method, expr, name, tuple(args), children=(expr, *args))
            else:
                if name in _METHODS or name in _PROTOTYPE_NAMES:
                    # 不带括号的方法引用（`v.includes`）与原型上的名字在 JS 里是函数，这里取不到
                    raise TemplateUnsupported(f"不支持的属性 .{_snippet(name)}")
                expr = self._node(_Prop, expr, name, children=(expr,))
        return expr

    def _parse_primary(self) -> Expr:
        token = self._peek()
        if token is None:
            raise TemplateUnsupported("表达式不完整")
        if token.kind == "string" or token.kind == "number":
            self.pos += 1
            return self._node(_Literal, token.value, children=())
        if token.kind == "punct" and token.value == "(":
            self.pos += 1
            inner = self.parse_expr()
            self._expect("punct", ")")
            return inner
        if token.kind == "ident":
            name = token.value
            if name in _LITERAL_IDENTS:
                self.pos += 1
                return self._node(_Literal, _LITERAL_IDENTS[name], children=())
            if name in ("getvar", "getLocalVar"):
                self.pos += 1
                self._expect("punct", "(")
                path = self._expect("string").value
                self._expect("punct", ")")
                if not _PROTOTYPE_NAMES.isdisjoint(path.split(".")):
                    raise TemplateUnsupported(f"变量路径里有不支持的名字 {_snippet(path)}")
                scope = "cache" if name == "getvar" else "local"
                self.roots.setdefault((scope, path.partition(".")[0]), None)
                return self._node(_Var, scope, path, children=())
            if name == "String":
                self.pos += 1
                self._expect("punct", "(")
                operand = self.parse_expr()
                self._expect("punct", ")")
                return self._node(_ToString, operand, children=(operand,))
            raise TemplateUnsupported(f"不支持的标识符 {_snippet(name)}")
        raise TemplateUnsupported(f"不支持的写法 {_snippet(token.value)}")


def _parse_output(code: str, parser: _Parser) -> Expr:
    """`<%= … %>` / `<%- … %>` 里只能是一个表达式，末尾可以带一个分号"""
    tokens = _lex(code)
    if tokens and tokens[-1].kind == "punct" and tokens[-1].value == ";":
        tokens = tokens[:-1]
    if not tokens:
        raise TemplateUnsupported("输出标签是空的")
    sub = _Parser(tokens)
    expr = sub.parse_expr()
    if sub.pos != len(tokens):
        raise TemplateUnsupported("输出标签里不止一个表达式")
    parser.node_count += sub.node_count
    parser.roots.update(sub.roots)
    return expr


def compile_template(source: str) -> Template:
    """把条目正文编译成语法树；子集之外的写法、超长的正文抛 :class:`TemplateUnsupported`"""
    if len(source) > MAX_TEMPLATE_CHARS:
        raise TemplateUnsupported(f"带模板的正文超过 {MAX_TEMPLATE_CHARS} 字")
    stream: list[_Token] = []
    code_chars = 0
    outputs = _Parser([])
    for segment in _segments(source):
        if segment.kind == "text":
            stream.append(_Token("text", segment.body))
        elif segment.kind == "eval":
            code_chars += len(segment.body) + 4
            stream.extend(_lex(segment.body))
        else:
            code_chars += len(segment.body) + 5
            stream.append(_Token("output", _parse_output(segment.body, outputs)))
    parser = _Parser(stream)
    nodes = parser.parse_block(top=True)
    return Template(
        nodes=nodes,
        source_chars=len(source),
        code_chars=code_chars,
        node_count=parser.node_count + outputs.node_count,
        roots=tuple({**outputs.roots, **parser.roots}),
    )


# ---------------------------------------------------------------------------
# 求值（JS 语义）
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_float(value: int | float) -> float:
    """数转成浮点；超出浮点范围的整数（变量值里可能有）按 JS 记成正负无穷"""
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _js_length(text: str) -> int:
    """JS 字符串的 `.length`：UTF-16 码元数（基本平面以外的字算两个）"""
    if text.isascii():
        return len(text)
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _utf16(text: str) -> bytes:
    """按 UTF-16 码元排序用的键：大端字节的先后就是码元的先后"""
    return text.encode("utf-16-be", "surrogatepass")


# JS 的空白与换行（`Number(' 1 ')` 两头去掉的那些）：WhiteSpace（含 BOM 与所有 Zs 类空格）加 LineTerminator。
# Python 的 `str.strip()` 去掉的是另一套（含 \x1c ~ \x1f、不含 BOM）。
_JS_WHITESPACE_CODES = (*range(0x09, 0x0E), 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F)
_JS_WHITESPACE = "".join(map(chr, (*_JS_WHITESPACE_CODES, 0x205F, 0x3000, 0xFEFF)))
# JS 的 StrNumericLiteral：ASCII 十进制（可带正负号、小数点、指数、`Infinity`），或者不带符号的 0x / 0o / 0b。
# 不认下划线分隔、全角或别的文字的数字（Python 的 `float()` 都认）。
_JS_DECIMAL = re.compile(r"[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")
_JS_NON_DECIMAL = re.compile(r"0(?:[xX]([0-9a-fA-F]+)|[oO]([0-7]+)|[bB]([01]+))")


def to_js_string(value: Any, _depth: int = 0) -> str:
    """JS 的 `String(value)`；列表嵌套超过 :data:`MAX_VALUE_DEPTH` 层抛 :class:`TemplateRuntimeError`"""
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if _is_number(value):
        return _number_to_string(_as_float(value))
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if _depth >= MAX_VALUE_DEPTH:
            raise TemplateRuntimeError("变量值嵌套太深")
        return ",".join("" if item is None or item is UNDEFINED else to_js_string(item, _depth + 1) for item in value)
    return "[object Object]"


def _number_to_string(number: float) -> str:
    """JS 的 `Number::toString`：能还原这个数的最短那几位数字（与 Python 的 `repr` 相同）；
    1e-7 < |x| < 1e21 写成小数，其余写成指数（`1e-7`、`1.5e+21`）"""
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    if number < 0:
        return "-" + _number_to_string(-number)
    parts = decimal.Decimal(repr(number)).as_tuple()
    all_digits = "".join(map(str, parts.digits))
    digits = all_digits.rstrip("0")
    # 数值 = 0.<digits> × 10^point
    point = int(parts.exponent) + len(all_digits)
    count = len(digits)
    if count <= point <= 21:
        return digits + "0" * (point - count)
    if 0 < point <= 21:
        return digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return "0." + "0" * -point + digits
    exponent = point - 1
    sign = "+" if exponent >= 0 else "-"
    mantissa = digits if count == 1 else digits[0] + "." + digits[1:]
    return f"{mantissa}e{sign}{abs(exponent)}"


def _string_to_number(text: str) -> float:
    """JS 的 StringToNumber（见 :data:`_JS_WHITESPACE`、:data:`_JS_DECIMAL`）；认不出的是 NaN"""
    text = text.strip(_JS_WHITESPACE)
    if not text:
        return 0.0
    match = _JS_NON_DECIMAL.fullmatch(text)
    if match is not None:
        hex_digits, octal_digits, binary_digits = match.groups()
        if hex_digits is not None:
            return _as_float(int(hex_digits, 16))
        if octal_digits is not None:
            return _as_float(int(octal_digits, 8))
        return _as_float(int(binary_digits, 2))
    if _JS_DECIMAL.fullmatch(text) is None:
        return math.nan
    if text.lstrip("+-") == "Infinity":
        return -math.inf if text.startswith("-") else math.inf
    return float(text)


def _to_number(value: Any) -> float:
    """JS 的 `Number(value)`"""
    if value is UNDEFINED:
        return math.nan
    if value is None or value is False:
        return 0.0
    if value is True:
        return 1.0
    if _is_number(value):
        return _as_float(value)
    if isinstance(value, str):
        return _string_to_number(value)
    return _to_number(to_js_string(value))


def truthy(value: Any) -> bool:
    """JS 的真假"""
    if value is UNDEFINED or value is None or value is False:
        return False
    if value is True:
        return True
    if _is_number(value):
        return not (value == 0 or math.isnan(_as_float(value)))
    if isinstance(value, str):
        return value != ""
    return True


def _strict_equal(left: Any, right: Any) -> bool:
    if _is_number(left) and _is_number(right):
        return _as_float(left) == _as_float(right)
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    # null / undefined / 对象与数组：按引用比较
    return left is right


def _loose_equal(left: Any, right: Any) -> bool:
    nullish = (None, UNDEFINED)
    if left in nullish or right in nullish:
        return (left in nullish) and (right in nullish)
    # 字符串按值比：`str` 的子类（诱饵判断用的哨兵）与普通字符串也是同一种类型，与 JS 一致。
    if (
        type(left) is type(right)
        or (_is_number(left) and _is_number(right))
        or (isinstance(left, str) and isinstance(right, str))
    ):
        return _strict_equal(left, right)
    if isinstance(left, bool):
        return _loose_equal(_to_number(left), right)
    if isinstance(right, bool):
        return _loose_equal(left, _to_number(right))
    if _is_number(left) and isinstance(right, str):
        return _as_float(left) == _to_number(right)
    if isinstance(left, str) and _is_number(right):
        return _to_number(left) == _as_float(right)
    if isinstance(left, (dict, list)) and not isinstance(right, (dict, list)):
        return _loose_equal(to_js_string(left), right)
    if isinstance(right, (dict, list)) and not isinstance(left, (dict, list)):
        return _loose_equal(left, to_js_string(right))
    return left is right


def _compare(op: str, left: Any, right: Any) -> bool:
    if isinstance(left, _Sentinel) and isinstance(right, _Sentinel) and left != right:
        # 两个变量互相比大小（`hp < maxhp`）按数值比：哨兵不是数，像变量读不到时一样不成立。
        # 否则哨兵的先后决定走哪边，`if (hp < maxhp) {…} else {一大段}` 这种正常写法就成了诱饵。
        return False
    if isinstance(left, (dict, list)):
        left = to_js_string(left)
    if isinstance(right, (dict, list)):
        right = to_js_string(right)
    if isinstance(left, str) and isinstance(right, str):
        # 按 UTF-16 码元比（与 JS 一样）：基本平面以外的字在 JS 里是两个代理码元，排在 U+E000 ~ U+FFFF 前面
        a: Any = _utf16(left)
        b: Any = _utf16(right)
    else:
        a = _to_number(left)
        b = _to_number(right)
        if math.isnan(a) or math.isnan(b):
            return False
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    return a <= b


def _describe(value: Any) -> str:
    return "undefined" if value is UNDEFINED else "null"


def _soft(variables: VariableProvider) -> bool:
    """诱饵判断里「部分变量」的那几组（:class:`_PartialVariables`）：JS 里会抛错的取属性、调方法得到 `undefined`"""
    return getattr(variables, "soft", False) is True


def _same_value_zero(left: Any, right: Any) -> bool:
    if _is_number(left) and _is_number(right):
        a, b = _as_float(left), _as_float(right)
        return a == b or (math.isnan(a) and math.isnan(b))
    return _strict_equal(left, right)


def _evaluate(expr: Expr, variables: VariableProvider) -> Any:
    """递归深度不超过表达式树的深度（建树时已限制在 :data:`MAX_EXPR_DEPTH` 以内）"""
    if isinstance(expr, _Literal):
        return expr.value
    if isinstance(expr, _Var):
        return variables.get(expr.scope, expr.path)
    if isinstance(expr, _ToString):
        return to_js_string(_evaluate(expr.operand, variables))
    if isinstance(expr, _Not):
        return not truthy(_evaluate(expr.operand, variables))
    if isinstance(expr, _Prop):
        target = _evaluate(expr.target, variables)
        if target is UNDEFINED or target is None:
            if _soft(variables):
                return UNDEFINED
            raise TemplateRuntimeError(f"读 {_describe(target)} 的属性")
        if isinstance(target, _Sentinel):
            # 哨兵只出现在诱饵判断的那几组变量里；取属性由这一组决定（见 `_PartialVariables`）
            return variables.prop(target, expr.name)  # type: ignore[attr-defined]
        if isinstance(target, Mapping):
            return target.get(expr.name, UNDEFINED)
        if expr.name == "length":
            if isinstance(target, str):
                return _js_length(target)
            if isinstance(target, list):
                return len(target)
            return UNDEFINED
        # 字符串、数、布尔、数组上别的名字在 JS 里多半取到原型上的方法（`'x'.trim`、`(1).toFixed`、
        # `[].map`，都为真），这里一个也没有，照搬会得到 undefined：按出错算，这一条不注入。
        raise TemplateRuntimeError(f"读字符串、数、布尔或数组的 .{_snippet(expr.name)}")
    if isinstance(expr, _Method):
        target = _evaluate(expr.target, variables)
        if target is UNDEFINED or target is None:
            if _soft(variables):
                return UNDEFINED
            raise TemplateRuntimeError(f"对 {_describe(target)} 调用 .{expr.name}()")
        if isinstance(target, _Sentinel):
            # 哨兵的字符是随机取的：`includes('a')` 照字面算，结果就随哨兵变。按「真变量里没有这一段」
            # 算，结果稳定；但空串与它自己在 JS 里对任何字符串都包含，这两种照实算真（不然
            # `!v || !v.includes('')` 几次渲染都输出诱饵）。转小写照实算（混合那组会变）。
            if expr.name == "includes":
                needle = to_js_string(_evaluate(expr.args[0], variables))
                return needle == "" or needle == str(target)
            return target.lowered()
        if expr.name == "toLowerCase":
            if not isinstance(target, str):
                if _soft(variables):
                    return UNDEFINED
                raise TemplateRuntimeError("对非字符串调用 .toLowerCase()")
            return target.lower()
        needle = _evaluate(expr.args[0], variables)
        if isinstance(target, str):
            return to_js_string(needle) in target
        if isinstance(target, list):
            return any(_same_value_zero(item, needle) for item in target)
        if _soft(variables):
            return UNDEFINED
        raise TemplateRuntimeError("对非字符串、非数组调用 .includes()")
    if isinstance(expr, _Logical):
        # JS 的短路：`&&` 返回第一个假值（都真就返回最后一个），`||` 返回第一个真值。
        value: Any = UNDEFINED
        for operand in expr.operands:
            value = _evaluate(operand, variables)
            if truthy(value) == (expr.op == "||"):
                return value
        return value
    left = _evaluate(expr.left, variables)
    right = _evaluate(expr.right, variables)
    if expr.op == "===":
        return _strict_equal(left, right)
    if expr.op == "!==":
        return not _strict_equal(left, right)
    if expr.op == "==":
        return _loose_equal(left, right)
    if expr.op == "!=":
        return not _loose_equal(left, right)
    return _compare(expr.op, left, right)


def _output_text(value: Any) -> str:
    """输出标签：`undefined` / `null` 输出空串（ejs 的 `__append` 与转义函数都这样做）"""
    if value is UNDEFINED or value is None:
        return ""
    return to_js_string(value)


def _walk(template: Template, variables: VariableProvider) -> Iterator[tuple[Node, str]]:
    """按变量逐个产出会输出的文字节点与输出节点（及其文字）

    语句迭代地走（`if` 的嵌套已经被解析器限了层数，`else if` 链也不递归），表达式的递归深度受
    :data:`MAX_EXPR_DEPTH` 限制。
    """
    pending: list[list[Node]] = [list(reversed(template.nodes))]
    while pending:
        frame = pending[-1]
        if not frame:
            pending.pop()
            continue
        node = frame.pop()
        if isinstance(node, _Text):
            yield node, node.text
        elif isinstance(node, _Output):
            yield node, _output_text(_evaluate(node.expr, variables))
        else:
            branch = node.then if truthy(_evaluate(node.condition, variables)) else node.otherwise
            if branch:
                pending.append(list(reversed(branch)))


def render(template: Template, variables: VariableProvider = EMPTY_VARIABLES) -> str:
    """按变量把模板算成文字

    JS 里会抛错的情况、输出超过 :data:`MAX_OUTPUT_CHARS` 字，都抛 :class:`TemplateRuntimeError`。
    """
    out: list[str] = []
    total = 0
    for _node, piece in _walk(template, variables):
        total += len(piece)
        if total > MAX_OUTPUT_CHARS:
            raise TemplateRuntimeError(f"渲染结果超过 {MAX_OUTPUT_CHARS} 字")
        out.append(piece)
    return "".join(out)


def _render_trace(template: Template, variables: VariableProvider, cap: int) -> tuple[str, dict[int, str]]:
    """渲染一遍，同时记下每个输出了文字的节点输出了什么；总字数超过 `cap` 抛错"""
    out: list[str] = []
    emitted: dict[int, str] = {}
    total = 0
    for node, piece in _walk(template, variables):
        total += len(piece)
        if total > cap:
            raise TemplateRuntimeError(f"渲染结果超过 {MAX_OUTPUT_CHARS} 字")
        out.append(piece)
        emitted[id(node)] = piece
    return "".join(out), emitted


@dataclass(frozen=True)
class RenderComparison:
    """「变量读不到」与「变量都有 / 只有一部分」几次渲染的对比

    Attributes:
        empty_text: 变量全读不到时渲染出的文字（就是空提供者下会注入的文字）；求值出错时为 `None`，
            这时诱饵字数按「取属性得到 undefined」的软世界当基线照常算（有了真实变量，这类模板算得出字）。
        bait_chars: 变量读不到时才会注入的字数：只要有一次带哨兵的渲染没输出、或者输出的文字不一样的
            节点，把「读不到」那次的文字整段算上。够 :data:`TRAP_MIN_CHARS` 就是针对「没有变量的
            客户端」的诱饵。「只有一部分变量」的那几次只在结论还没定时才渲染、判出来就停，所以不到
            门槛时这个数可能偏小；够不够门槛总是准的。
        partial_renders: 「只有一部分变量」的渲染实际做了几次（记账用：调用方事后按次数补记）。
    """

    empty_text: str | None
    bait_chars: int
    partial_renders: int = 0


# 「变量都有」那几组哨兵（见模块里哨兵的说明）。
SENTINEL_SETS = 4
# 变量路径树（见 :func:`_unit_tree`）的节点不超过这么多个时，每个节点各渲染一次「只有它没设」；更多时
# 每一层按同一个父节点下的序号的二进制位分组。
MAX_SINGLE_UNSET_RENDERS = 8
# 拿掉变量时路径最深看几层：更深的路径跟着它在这一层的祖先走，不单独拿掉。MVU 卡的变量路径一般三四层
# （`stat_data.角色.好感度`）；每多一层，又深又窄的模板就要多渲染一次。
MAX_UNIT_DEPTH = 4
# 「只有一部分变量」的渲染最多几次（含「只有根变量」那一次）；路径树又宽又深时，靠后的层就不再拿掉。
MAX_PARTIAL_RENDERS = 32
# `(a || b || …).c` 往下推时，一个 `&&` / `||` 最多带出几条路径（多出来的不进路径树，免得几千个变量的
# 或写法后面接几十层取属性，路径数成了两者的乘积）。
MAX_LOGICAL_PATHS = 8


def reads_variables(source: str) -> bool:
    """编译之前判断模板会不会读变量（记账用）：不会读的，诱饵判断只渲染一次

    读变量只有 `getvar` / `getLocalVar` 两种写法，原文里没有这两个词就一定不读；有的不一定读
    （可能在正文或注释里），按会读记，只多不少。
    """
    return "getvar" in source or "getLocalVar" in source


def _variable_paths(template: Template) -> list[tuple[str, str]]:
    """模板里静态可知的变量路径（`(作用域, 点号路径)`），大致按出现的先后，不重复

    `getvar('a.b')`，以及接在变量上的取属性（`getvar('a').b.c`；经过 `String()` 与 `.toLowerCase()`
    也算，`.length` 不算）。经过 `&&` / `||` 的按各操作数路径的并集往下推（`(getvar('s.a') || 0).c`
    带出 `s.a.c`），一个并集最多 :data:`MAX_LOGICAL_PATHS` 条；路径到 :data:`MAX_UNIT_DEPTH` 段就不再往下接
    （路径树只看这么深）。
    """
    found: dict[tuple[str, str], None] = {}

    def visit(expr: Expr) -> tuple[tuple[str, str], ...]:
        # 递归深度不超过表达式树的深度（建树时已限制在 MAX_EXPR_DEPTH 以内）
        result: tuple[tuple[str, str], ...] = ()
        if isinstance(expr, _Var):
            result = ((expr.scope, expr.path),)
        elif isinstance(expr, _Prop):
            base = visit(expr.target)
            if expr.name != "length":
                result = tuple(
                    (scope, path if path.count(".") + 1 >= MAX_UNIT_DEPTH else f"{path}.{expr.name}")
                    for scope, path in base
                )
        elif isinstance(expr, _ToString):
            result = visit(expr.operand)
        elif isinstance(expr, _Method):
            base = visit(expr.target)
            for arg in expr.args:
                visit(arg)
            if expr.name == "toLowerCase":
                result = base
        elif isinstance(expr, _Not):
            visit(expr.operand)
        elif isinstance(expr, _Logical):
            merged: dict[tuple[str, str], None] = {}
            for operand in expr.operands:
                for item in visit(operand):
                    if len(merged) < MAX_LOGICAL_PATHS:
                        merged.setdefault(item, None)
            result = tuple(merged)
        elif isinstance(expr, _Binary):
            visit(expr.left)
            visit(expr.right)
        for item in result:
            found.setdefault(item, None)
        return result

    pending: list[Node] = list(reversed(template.nodes))
    while pending:
        node = pending.pop()
        if isinstance(node, _Output):
            visit(node.expr)
        elif isinstance(node, _If):
            visit(node.condition)
            pending.extend(reversed(node.otherwise))
            pending.extend(reversed(node.then))
    return list(found)


def _reads_variable(expr: Expr) -> bool:
    """表达式里有没有读变量（`getvar` / `getLocalVar`）"""
    pending: list[Expr] = [expr]
    while pending:
        item = pending.pop()
        if isinstance(item, _Var):
            return True
        if isinstance(item, (_ToString, _Not)):
            pending.append(item.operand)
        elif isinstance(item, _Prop):
            pending.append(item.target)
        elif isinstance(item, _Method):
            pending.append(item.target)
            pending.extend(item.args)
        elif isinstance(item, _Logical):
            pending.extend(item.operands)
        elif isinstance(item, _Binary):
            pending.append(item.left)
            pending.append(item.right)
    return False


def _variable_dependent_nodes(template: Template) -> set[int]:
    """输出可能随变量变的节点（`id`）：读变量的输出标签，以及条件里读了变量的 `if` 下面的一切

    别的节点（顶层的文字、不读变量的输出、条件不读变量的分支里的）在哪个世界里输出都一样。
    """
    dependent: set[int] = set()
    pending: list[tuple[Node, bool]] = [(node, False) for node in template.nodes]
    while pending:
        node, inherited = pending.pop()
        if isinstance(node, _Text):
            if inherited:
                dependent.add(id(node))
        elif isinstance(node, _Output):
            if inherited or _reads_variable(node.expr):
                dependent.add(id(node))
        else:
            reads = inherited or _reads_variable(node.condition)
            pending.extend((child, reads) for child in node.then)
            pending.extend((child, reads) for child in node.otherwise)
    return dependent


def _unit_tree(template: Template) -> tuple[dict[tuple[str, str], int], list[tuple[int, int]]]:
    """变量路径按前缀闭合成一棵树：`s.a.b` 带出 `s`、`s.a`、`s.a.b` 三个节点（最深 :data:`MAX_UNIT_DEPTH` 层）

    返回 `(节点 → 编号, 每个编号的 (层数, 在同一个父节点下的序号))`；两种作用域的根变量算同一组兄弟。
    """
    units: dict[tuple[str, str], int] = {}
    info: list[tuple[int, int]] = []
    children: dict[int, int] = {}
    for scope, path in _variable_paths(template):
        parent = -1
        prefix = ""
        for depth, part in enumerate(path.split(".", MAX_UNIT_DEPTH)[:MAX_UNIT_DEPTH], start=1):
            prefix = part if depth == 1 else f"{prefix}.{part}"
            unit = units.get((scope, prefix))
            if unit is None:
                unit = units[(scope, prefix)] = len(info)
                sibling = children.get(parent, 0)
                children[parent] = sibling + 1
                info.append((depth, sibling))
            parent = unit
    return units, info


def _partial_worlds(base: _SentinelVariables, template: Template) -> Iterator[_PartialVariables]:
    """「只有一部分变量」的几次：只有根变量一次；再按变量路径树（见 :func:`_unit_tree`）拿掉一部分路径

    - 节点不超过 :data:`MAX_SINGLE_UNSET_RENDERS` 个：每个节点各拿掉一次（只有一个根变量时，拿掉它
      就是变量全读不到那一次，不必再渲染）。
    - 更多时一层一层来：第 d 层按「同一个父节点下的序号」的每一个二进制位分两次（这一位是 0 的都
      拿掉、是 1 的都拿掉），上面的层都不动；这一层每个父节点下都只有一个孩子时，整层拿掉一次（第 2 层
      整层拿掉与「只有根变量」那次几乎一样，省掉）。任意两条路径，只要一条不在另一条下面，它们在分叉
      那一层的节点序号不同，总有一次是「这条拿掉、那条还在」：`!getvar('s.a') || getvar('s.从来不设')`
      这种两条路径配合的写法照样判得出。次数是各层序号位数之和的两倍，最多 :data:`MAX_PARTIAL_RENDERS`
      次（含只有根变量那一次）。
    """
    yield _PartialVariables(base, shallow=True)
    units, info = _unit_tree(template)
    index = _PathIndex(units)
    by_depth: dict[int, list[int]] = {}
    for unit, (depth, _sibling) in enumerate(info):
        by_depth.setdefault(depth, []).append(unit)
    if len(info) <= MAX_SINGLE_UNSET_RENDERS:
        for unit, (depth, _sibling) in enumerate(info):
            if depth == 1 and len(by_depth[1]) == 1:
                continue
            yield _PartialVariables(base, index=index, depth=depth, missing=frozenset((unit,)))
        return
    made = 1
    for depth in sorted(by_depth):
        members = by_depth[depth]
        widest = 1 + max(info[unit][1] for unit in members)
        if widest == 1:
            splits = [] if depth <= 2 else [None]
        else:
            splits = [(bit, side) for bit in range((widest - 1).bit_length()) for side in (0, 1)]
        for split in splits:
            if made >= MAX_PARTIAL_RENDERS:
                return
            made += 1
            if split is None:
                missing = frozenset(members)
            else:
                bit, side = split
                missing = frozenset(unit for unit in members if (info[unit][1] >> bit) & 1 == side)
            yield _PartialVariables(base, index=index, depth=depth, missing=missing)


def compare_renders(template: Template, *, seed: str | None = None, key: bytes | None = None) -> RenderComparison:
    """一次变量全读不到，再在几组「变量都有」「只有一部分变量」的世界里各渲染一次，按节点比

    诱饵的本质是「变量读不到时才出现的大段文字」，写法可以千变万化（`!getvar(x)`、
    `getvar(x) === getvar('不存在')`、`(getvar(x) || 'n') == 'n'`、`<%= getvar(x) || '诱饵' %>`……），
    逐种写法去认永远认不全，所以直接比渲染结果。一个节点在「读不到」那次输出了文字，而在任何
    一次别的渲染里没输出、或者输出的不是同一段文字，就把「读不到」那次的文字整段算进来
    （只算多出来的长度的话，`(!getvar(x) && '诱饵') || '等长的正常文字'` 这种同一个输出节点里
    二选一的写法就漏了）。比的是节点，不是总长：`if (!getvar(x)) {诱饵} else {同样长的正常文字}`
    也判得出。

    几个世界：

    - 四组哨兵（变量都是各不相同的真值字符串，形状见模块里哨兵的说明）。随机的两组由 `key` 与
      `seed` 定（调用方传服务端持久密钥派生的一把与正文：同一条正文重启前后结论不变；`key` 不传就用
      每进程随机的那把，`seed` 不传就每次随机）。
    - 只有根变量（`getvar('s.a')`、`getvar('s').a`、`.length` 都读不到）：抓 `!v || v.从来不设`。
    - 按路径拿掉一部分变量（见 :func:`_partial_worlds`）：抓 `!getvar('a') || getvar('从来不设')`、
      `!getvar('s.a') || getvar('s.从来不设')`。

    后两种只在结论还没定时才渲染：四组哨兵已经判出来了，或者「读不到」那次输出的字里受变量影响、
    还没判出来的那部分（见 :func:`_variable_dependent_nodes`）加上已经判出来的不到门槛，就不必再
    渲染；一旦判出来就停。

    任何一次带哨兵的渲染求值出错（哨兵取属性也得到哨兵，「只有一部分变量」的世界里对 `undefined`
    取属性得到 `undefined`，正常写法到不了；能走到的多半是故意的，比如输出超限）时按保守处理：
    「读不到」那次输出的全部文字都算可疑——否则先放一句让哨兵出错的代码，就能把判断拉回容易绕过
    的老办法。比值的条件（`getvar(x) >= 3`、`!== 'ready'`、两个变量比大小）在几次里结果相同，不算
    诱饵：有变量的客户端里同样可能走到那一边。反过来，按长度或字典序挑段落的正常写法（变量短时
    显示一大段默认文字）、「一个有、另一个没有」时才不显示的正常写法（`a && !b ? 短 : 一大段`）都会被
    当成诱饵——这是有意的取舍。

    还绕得过去的写法（这是启发式，更彻底的办法是污点追踪）：两个以上从来不设的变量一起用
    （`!a || n1 || n2`）；路径多到要分组时三条路径配合；路径多到要分组、第 2 层每个父节点都只有一个
    孩子时靠第 2 层整层拿掉才分得开的写法（那一次与「只有根变量」相近，省掉了）；按值比较后取反
    （`!(v < 50)`、两个变量比大小取反、两个变量相等、`!(v < v.toLowerCase())`——两个哨兵比大小一律
    不成立，同一个变量与它转小写的结果也算两个）；几个条件或起来分别对上每组哨兵的形状；长度或字典序
    贴着哨兵范围的两头；按类型区分（真变量是对象、没有 `.length`）；一个 `&&` / `||` 里超过
    :data:`MAX_LOGICAL_PATHS` 条的路径；路径深过 :data:`MAX_UNIT_DEPTH` 层或拿掉的次数超过
    :data:`MAX_PARTIAL_RENDERS` 时更深、更靠后的那部分。
    """
    rendered: str | None
    try:
        empty_text, empty = _render_trace(template, EMPTY_VARIABLES, MAX_OUTPUT_CHARS)
        rendered = empty_text
    except TemplateRuntimeError:
        # 空变量下按 JS 会出错（对 undefined 取属性，MVU 卡的 `getvar('stat_data').a.b` 就是这样）：
        # 空提供者下这一条不注入（`empty_text` 记 None），但有了真实变量就算得出字，所以诱饵照常判，
        # 基线换成「取属性得到 undefined」的软世界。不读变量的模板在哪个世界里都出错，不必再比。
        rendered = None
        if not template.roots:
            return RenderComparison(None, 0)
        try:
            empty_text, empty = _render_trace(template, _SOFT_EMPTY_VARIABLES, MAX_OUTPUT_CHARS)
        except TemplateRuntimeError:
            # 连软世界也出错（多半是输出超限这类故意的写法）：比不出基线，按下面「带哨兵的渲染出错」
            # 同一个保守口径，整条算可疑。
            return RenderComparison(None, TRAP_MIN_CHARS)
    if not template.roots:
        # 不读变量：几次渲染都一样
        return RenderComparison(empty_text, 0)
    if seed is None:
        seed = secrets.token_hex(16)
    base = _SentinelVariables(_short_low)
    # 别的三组用完就丢（一组能有几万个哨兵）；「短而靠前」那组留给后面「只有一部分变量」的几次复用。
    makers = (_long_high, _seeded_maker(seed, key), _mixed_maker(seed, key))
    flagged: set[int] = set()
    bait = 0

    def differs(world: VariableProvider) -> bool:
        """在这个世界里渲染一次，记下与「读不到」那次不同的节点；出错返回 False"""
        nonlocal bait
        try:
            # 哨兵比一般的取值长，连同哨兵在内的总字数按四倍封顶（输出节点的个数受正文长度限制）
            _text, present = _render_trace(template, world, 4 * MAX_OUTPUT_CHARS)
        except TemplateRuntimeError:
            return False
        for node_id, text in empty.items():
            if node_id not in flagged and present.get(node_id) != text:
                flagged.add(node_id)
                bait += len(text)
        return True

    if not differs(base) or not all(differs(_SentinelVariables(maker)) for maker in makers):
        return RenderComparison(rendered, len(empty_text))
    if bait >= TRAP_MIN_CHARS:
        return RenderComparison(rendered, bait)
    # 还没判出来、又受变量影响的字（见 `_variable_dependent_nodes`）加上已经判出来的不到门槛，后面几次
    # 渲染怎么拿掉变量也凑不够：不必再渲染（正文大段不受变量影响的模板就这样省掉了）。
    dependent = _variable_dependent_nodes(template)
    open_chars = sum(len(text) for node_id, text in empty.items() if node_id in dependent and node_id not in flagged)
    if bait + open_chars < TRAP_MIN_CHARS:
        return RenderComparison(rendered, bait)
    partial = 0
    for world in _partial_worlds(base, template):
        partial += 1
        if not differs(world):
            return RenderComparison(rendered, len(empty_text), partial)
        if bait >= TRAP_MIN_CHARS:
            break
    return RenderComparison(rendered, bait, partial)


# ---------------------------------------------------------------------------
# 防盗条目的静态特征
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateScan:
    """不求值、只看字面的扫描结果，给「疑似防盗条目」判断用

    Attributes:
        env_identifiers: 代码里出现的探测运行环境的标识符。
        conditional_chars: 落在代码花括号里（受判断控制）的文字字数。
        code_string_chars: 代码里字符串字面量的字数（诱饵也可能写成字符串再输出）。
        dead_branch_chars: 判断条件是常量假（`if (false)` 之类）的分支里的文字字数；
            只在能按子集解析时才算得出来。
        missing_variable_chars: 只因为变量读不到才会走到的分支里的文字字数（见
            :func:`missing_variable_branch_chars`）；只在能按子集解析时才算得出来。
    """

    env_identifiers: frozenset[str] = frozenset()
    conditional_chars: int = 0
    code_string_chars: int = 0
    dead_branch_chars: int = 0
    missing_variable_chars: int = 0


def _scan_code(code: str, env: set[str]) -> tuple[int, int]:
    """宽松地扫一段代码：返回（字符串字面量的字数, 花括号净增数），顺手收集探测环境的标识符

    手写的单遍扫描：注释与字符串的正则在大量未闭合的 `/*`、反引号上会对每个起点扫到末尾，
    是二次方的；这里每个字符只看一遍。
    """
    strings = 0
    braces = 0
    index = 0
    length = len(code)
    while index < length:
        char = code[index]
        if char == "/" and index + 1 < length and code[index + 1] in "/*":
            if code[index + 1] == "/":
                end = code.find("\n", index + 2)
                index = length if end < 0 else end + 1
            else:
                end = code.find("*/", index + 2)
                index = length if end < 0 else end + 2
            continue
        if char in "'\"`":
            end = index + 1
            while end < length:
                current = code[end]
                if current == "\\":
                    end += 2
                    continue
                if current == char or (char != "`" and current == "\n"):
                    break
                end += 1
            strings += max(0, min(end, length) - index - 1)
            index = end + 1
            continue
        if char == "_" or char == "$" or char.isalpha():
            match = _IDENT.match(code, index)
            if match is None:
                index += 1
                continue
            name = match.group(0)
            if name in ENV_IDENTIFIERS:
                env.add(name)
            index = match.end()
            continue
        if char == "{":
            braces += 1
        elif char == "}":
            braces -= 1
        index += 1
    return strings, braces


def scan_template(source: str, template: Template | None = None) -> TemplateScan:
    """宽松扫描一段模板：子集之外的写法也能扫（只数字面，不解析语法）

    切段失败（标签不闭合之类）时按「整段都是代码」处理：这种条目本来就不会注入，扫描只
    为在报告里说明原因。整段是线性的，且最多只看前 :data:`MAX_SCAN_CHARS` 字。
    """
    source = source[:MAX_SCAN_CHARS]
    try:
        segments = _segments(source)
    except TemplateUnsupported:
        segments = [_Segment("eval", source)]

    env: set[str] = set()
    conditional = 0
    strings = 0
    depth = 0
    for segment in segments:
        if segment.kind == "text":
            if depth > 0:
                conditional += len(segment.body)
            continue
        segment_strings, braces = _scan_code(segment.body, env)
        strings += segment_strings
        depth = max(0, depth + braces)

    dead = _dead_branch_chars(template.nodes) if template is not None else 0
    missing = missing_variable_branch_chars(template) if template is not None else 0
    return TemplateScan(
        env_identifiers=frozenset(env),
        conditional_chars=conditional,
        code_string_chars=strings,
        dead_branch_chars=dead,
        missing_variable_chars=missing,
    )


def _reads_variables(expr: Expr) -> bool:
    if isinstance(expr, _Var):
        return True
    if isinstance(expr, _Literal):
        return False
    if isinstance(expr, (_ToString, _Not)):
        return _reads_variables(expr.operand)
    if isinstance(expr, _Prop):
        return _reads_variables(expr.target)
    if isinstance(expr, _Method):
        return _reads_variables(expr.target) or any(_reads_variables(arg) for arg in expr.args)
    if isinstance(expr, _Logical):
        return any(_reads_variables(operand) for operand in expr.operands)
    return _reads_variables(expr.left) or _reads_variables(expr.right)


def _literal_chars(expr: Expr) -> int:
    """输出标签里写死的文字有多少字：`<%= '…' %>` 与 `'…' || '…'`、`String('…')` 里的字符串字面量"""
    if isinstance(expr, _Literal):
        return len(expr.value) if isinstance(expr.value, str) else 0
    if isinstance(expr, _Logical):
        return sum(_literal_chars(operand) for operand in expr.operands)
    if isinstance(expr, _ToString):
        return _literal_chars(expr.operand)
    return 0


def _text_chars(nodes: tuple[Node, ...]) -> int:
    """一段语句里能输出的字数：文字节点，加上输出标签里写死的字符串（诱饵也可能写成 `<%= '…' %>`）

    迭代地走：`else if` 链在树里是层层套的，链长不受嵌套上限约束。
    """
    total = 0
    pending: list[tuple[Node, ...]] = [nodes]
    while pending:
        for node in pending.pop():
            if isinstance(node, _Text):
                total += len(node.text)
            elif isinstance(node, _Output):
                total += _literal_chars(node.expr)
            else:
                pending.append(node.then)
                pending.append(node.otherwise)
    return total


def _constant_truth(condition: Expr) -> bool | None:
    """条件不读任何变量时算出它的真假；读变量或算出错时为 `None`"""
    if _reads_variables(condition):
        return None
    try:
        return truthy(_evaluate(condition, EMPTY_VARIABLES))
    except TemplateRuntimeError:
        return None


def _dead_branch_chars(nodes: tuple[Node, ...]) -> int:
    """条件不读任何变量、算出来恒假（或恒真时的 else）的分支里有多少字"""
    total = 0
    pending: list[tuple[Node, ...]] = [nodes]
    while pending:
        for node in pending.pop():
            if not isinstance(node, _If):
                continue
            taken = _constant_truth(node.condition)
            if taken is False:
                total += _text_chars(node.then)
                pending.append(node.otherwise)
            elif taken is True:
                total += _text_chars(node.otherwise)
                pending.append(node.then)
            else:
                pending.append(node.then)
                pending.append(node.otherwise)
    return total


# 「变量读不到」时与之相等的字面量：`null`、`undefined`，空串，以及 `String(undefined)` /
# `String(null)` 得到的 `'undefined'` / `'null'`。
_ABSENT_LITERALS = (None, UNDEFINED)
_ABSENT_STRINGS = ("", "undefined", "null")


def _is_constant(expr: Expr) -> bool:
    """字面量（或者取反的字面量）"""
    if isinstance(expr, _Literal):
        return True
    return isinstance(expr, _Not) and _is_constant(expr.operand)


def _constant_is_neutral(op: str, expr: Expr) -> bool:
    """`&&` 里的真值、`||` 里的假值不影响结果；反过来的（`&& false`、`|| true`）让整个条件恒定"""
    return truthy(_evaluate(expr, EMPTY_VARIABLES)) is (op == "&&")


def _absent_literal(expr: Expr) -> bool:
    if not isinstance(expr, _Literal):
        return False
    value = expr.value
    if value is None or value is UNDEFINED:
        return True
    return isinstance(value, str) and value in _ABSENT_STRINGS


def _variable_side(expr: Expr) -> bool:
    """变量本身，或者 `String(变量)`"""
    return isinstance(expr, _Var) or (isinstance(expr, _ToString) and isinstance(expr.operand, _Var))


def _existence_only(expr: Expr) -> bool:
    """条件是不是纯粹在问「这个变量有没有」（启发式）

    认：变量本身的真假、`!`、`&&` / `||` 组合（不影响结果的字面量操作数不算数，如 `!getvar('s') && true`、
    `!getvar('s') || false`；`|| true` / `&& false` 让条件恒定，就不是在问变量了）、
    变量或 `String(变量)` 与「读不到时会相等的字面量」（`null`、`undefined`、`''`、`'undefined'`、
    `'null'`）的宽松或严格相等比较。`getvar('剧情权重') >= 3` 这种比的是值，不算：有变量的客户端
    里它同样可能不成立。这终究是对常见写法的枚举，换一种绕法仍然可能漏过去。
    """
    if isinstance(expr, _Var):
        return True
    if isinstance(expr, _Not):
        return _existence_only(expr.operand)
    if isinstance(expr, _Logical):
        constants = [operand for operand in expr.operands if _is_constant(operand)]
        if not all(_constant_is_neutral(expr.op, operand) for operand in constants):
            return False
        relevant = [operand for operand in expr.operands if not _is_constant(operand)]
        return bool(relevant) and all(_existence_only(operand) for operand in relevant)
    if isinstance(expr, _Binary) and expr.op in ("==", "===", "!=", "!=="):
        left, right = expr.left, expr.right
        return (_variable_side(left) and _absent_literal(right)) or (_variable_side(right) and _absent_literal(left))
    return False


def missing_variable_branch_chars(template: Template) -> int:
    """变量全都读不到时，只因为「变量不存在」才会走到的分支里有多少字

    只看会被渲染的那条路径：条件纯粹在问变量有没有（:func:`_existence_only`）、又读了变量、
    在变量全空时成立的分支，把它里面的字数算进来。这正是针对「没有变量的客户端」的诱饵：
    `<% if (!getvar('stat_data')) { %>几千字<% } %>` 在有 MVU 的客户端里永远看不到。

    这是早先逐种写法的静态判断，**不再参与诱饵判定**（判定看 :func:`compare_renders`）；留着给
    用例与「两种判法结论是否一致」的对照（复核真实卡片的误报时用）。
    """
    total = 0
    pending: list[tuple[Node, ...]] = [template.nodes]
    while pending:
        for node in pending.pop():
            if not isinstance(node, _If):
                continue
            try:
                taken = truthy(_evaluate(node.condition, EMPTY_VARIABLES))
            except TemplateRuntimeError:
                continue
            branch = node.then if taken else node.otherwise
            if _reads_variables(node.condition) and _existence_only(node.condition):
                total += _text_chars(branch)
                continue
            pending.append(branch)
    return total


def outputs_variables(template: Template) -> bool:
    """有没有输出标签把读到的变量值打出来（`<%= getvar('x') %>`、`<%= getvar('s').length %>` 一类）

    只看语法树的结构，与变量值无关，可以随分析缓存。只用变量来**挑段落**的模板（`if` 的条件里读
    变量、输出的都是原文）不算。语句迭代地走，表达式的递归深度受 :data:`MAX_EXPR_DEPTH` 限制。
    """
    pending: list[tuple[Node, ...]] = [template.nodes]
    while pending:
        for node in pending.pop():
            if isinstance(node, _Output):
                if _reads_variables(node.expr):
                    return True
            elif isinstance(node, _If):
                pending.append(node.then)
                pending.append(node.otherwise)
    return False


__all__ = [
    "EMPTY_VARIABLES",
    "ENV_IDENTIFIERS",
    "MAX_EXPR_DEPTH",
    "MAX_INDEX_DIGITS",
    "MAX_NESTING",
    "MAX_SCAN_CHARS",
    "MAX_OUTPUT_CHARS",
    "MAX_REASON_CHARS",
    "MAX_TEMPLATE_CHARS",
    "MAX_SINGLE_UNSET_RENDERS",
    "MAX_VALUE_DEPTH",
    "SENTINEL_SETS",
    "TRAP_MIN_CHARS",
    "UNDEFINED",
    "MappingVariables",
    "RenderComparison",
    "Template",
    "TemplateRuntimeError",
    "TemplateScan",
    "TemplateUnsupported",
    "VariableProvider",
    "compare_renders",
    "compile_template",
    "has_template",
    "missing_variable_branch_chars",
    "outputs_variables",
    "reads_variables",
    "render",
    "scan_template",
    "short_reason",
    "to_js_string",
    "truthy",
]
