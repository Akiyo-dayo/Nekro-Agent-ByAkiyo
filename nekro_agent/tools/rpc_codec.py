"""沙盒 -> 宿主的 RPC 请求体：JSON + 显式类型标签

## 为什么请求方向不再用 pickle

`/ext/rpc_exec` 的请求体来自沙盒，沙盒里跑的是模型生成的代码，它读得到本容器的回调令牌，能带合法令牌
POST 任意字节；方法名就在请求体里，所以解码发生在方法白名单之前。

把 `pickle.loads` 换成白名单 `Unpickler` 并不够：`find_class` 交出去的是
**真实的类**，pickle 的 `BUILD` 指令会对它做 `setattr` / `__dict__.update`。`STACK_GLOBAL pathlib.PurePosixPath`
+ `(None, {'_str': <宿主路径>})` + `BUILD` 一个 50 字节的请求体，就能让宿主进程里所有 `PurePosixPath` 的 `str()`
变成攻击者给的路径（路径校验通过之后读写任意宿主文件）。pickle 本来就是“在宿主进程里构造对象图”的指令集
（`BUILD` / `NEWOBJ` / `INST` / memo 引用……），逐条封堵靠不住。

JSON 只能表达纯数据：没有类、没有可调用对象、没有引用。JSON 原生没有的类型由沙盒侧写成
`{"$rpc": "<标签>", "v": <载荷>}`，宿主按标签调用**写死的**构造函数还原。整个解码过程不按请求里的任何名字
查找类或函数，不对任何对象做 `setattr` / `__setstate__`，产出的只有下面这些值。

## 能传的类型（逐个说明理由）

- `None` / `bool` / `int` / `float` / `str` / `list` / 字符串键的 `dict`：JSON 原生，内置插件的全部参数都在这里面；
- `bytes`（标签 `bytes`，base64）：二进制内容是第三方插件最常见的非文本参数；沙盒侧把 `bytearray` / `memoryview` 也按它发；
- `tuple`（`tuple`）：Python 调用里很常见（坐标、尺寸），不加标签 JSON 会悄悄把它变成 `list`；
- `set` / `frozenset`（`set` / `frozenset`）：一组 ID 之类的集合参数；`frozenset` 可哈希，能当集合元素与字典键；
- 非字符串键的 `dict`（`dict`，键值对列表）：`{1: "a"}`、`{(x, y): v}`；不加标签 JSON 会把 `1` 悄悄变成 `"1"`；
- `datetime` / `date` / `time`（ISO 8601 字符串）与 `timedelta`（`[天, 秒, 微秒]`）：定时、日程类插件的常见参数；
  带时区的时间还原成固定偏移（`datetime.timezone`）；
- `uuid.UUID`（`uuid`，字符串）：标识符常以 `uuid.uuid4()` 的对象形态出现。

不再接受（与白名单 pickle 的做法相比）：`Decimal`（`int(Decimal("1e2000000"))` 一次就占满 CPU 一分多钟；
请传 `str` 或 `float`）、`pathlib` 的路径（改写类属性那条攻击的载体；沙盒侧把 `os.PathLike` 自动转成 `str`，宿主只收到字符串）、
`uuid.SafeUUID`（`UUID` 的 pickle 内部状态，不是值）、`OrderedDict`（沙盒侧按普通 `dict` 发，`dict` 本来就保序）、
`complex` 与单独的 `timezone`（没有使用场景；时区已经写在 ISO 字符串里）。

## 上限（拒绝服务）

宿主是单进程，事件循环全租户共用。解码之前、解码之中逐项设上限，超限一律拒绝（:class:`RpcCodecError`，
由路由以 `Run-Error` 回给沙盒），并且发生在把参数拼进任何字符串、调用任何方法之前：

- 请求体 :data:`MAX_BODY_BYTES`（1 MiB）：内置插件里最大的参数是交给 `view_str_content` 分页查看的整份数据，
  其余是消息文本、关键词、任务描述这类几 KB 的东西；RPC 出口给任何结果正文兜的底是 200000 字符，入口留到
  约 35 万汉字 / 100 万 ASCII 字符。由路由在读请求体时检查 `Content-Length` 并边读边截断。
  **解析的代价主要不在 JSON 本身，而在循环垃圾回收**（实测，导入完整应用之后约 38 万个对象）：1 MiB 的
  `[[],[],…]` 会新建 35 万个容器，触发整堆回收，`json.loads` 是 C 代码、全程持 GIL，GC 开着要 280 ms，关掉只要 15 ms；
  每项 16 层嵌套的形态 336 ms 对 32 ms；纯字符串 8 ms、纯整数 23 ms。所以解码放在专用的单线程里串行进行，
  解码期间暂停循环 GC，拒绝时异常不带出解码线程、半成品在暂停期间就被释放（`services/rpc_service.py`）；单次解码持 GIL 约 35 ms 以内，
  1 MiB 不必再收紧。**光放进线程不够**：`json.loads` 不让出 GIL，“放进线程就不卡循环”并不成立；
- 嵌套 :data:`MAX_DEPTH`（32 层）：正常参数不超过 10 层；同时让插件里对参数的递归处理（`str()`、`json.dumps`）
  离解释器的递归上限很远；
- 元素总数 :data:`MAX_ITEMS`（100000）：比内置插件最大的结构化参数（几十个元素的消息段、概率表）高三个数量级；
- 容器总数 :data:`MAX_CONTAINERS`（10000 个 list / dict，含类型标签）：被接受的参数会活到方法返回，期间被
  GC 提升进老年代；每个请求最多带进 1 万个容器，攻击者靠高频调用逼出整堆回收的速度就被压住了。仍比内置插件的
  实际用量高两个数量级；
- 单个字符串 :data:`MAX_STR_CHARS`（1000000 字符）与单个字节串 :data:`MAX_BYTES_LEN`（768 KiB，base64 后正好是
  请求体上限）：单个值可以占满请求体（整份数据交给 `view_str_content`），单独钉住是为了以后放宽请求体上限时
  不会顺带放大单个值；
- 整数位数 :data:`MAX_INT_DIGITS`（100 位十进制）：覆盖 64 位雪花 ID（20 位）、128 位整数（39 位），远低于
  CPython 自己的 4300 位安全阈值，任何 `str()` / 运算都是微秒级；在 JSON 解析时、调用 `int()` 之前检查；
- 哈希碰撞：`set` / `frozenset` / 非字符串键 `dict` 在**构造之前**先把元素的哈希值排序（O(n log n)，不经过哈希表），
  同一个哈希值出现超过 :data:`MAX_SAME_HASH`（8）次就拒绝。整数的哈希是模 2^61-1，攻击者能造出任意多个哈希相同的整数，
  插进哈希表是二次方耗时（修复前 575 KB 请求体解码 9 秒）；元组、浮点数同理。合法的集合 / 字典里元素互不相等，
  完全相同的哈希值极少（`-1` 与 `-2` 这类），8 次足够宽；
- 共享引用放大：JSON 没有引用，解出来的是一棵树，大小与请求体成正比；pickle 的 memo 让 257 字节解出 GB 级结构的形态不存在；
- 解析之前的结构预估：`json.loads` 一旦开始就要把整个结构建完，1 万容器的上限要等解析完才查得到。所以先用
  `bytes.count` 数 `[` / `{`、`,` 与 `"`：64 KiB 一片、片间让出 GIL（`bytes.count` 全程持 GIL，1 MiB 一口气数几遍就是 3 ms 以上，
  事件循环每次 I/O 唤醒都得等）。数出来超过上限时，减掉字符串字面量里的括号与逗号再判：字面量不超过 :data:`_PRESCAN_FEW_STRINGS`
  个就用 `find` 逐个找，不超过 :data:`_PRESCAN_MAX_STRINGS` 个就用正则剥掉再数；确实超限就不解析、直接拒绝。容器密集的 1 MiB
  请求体由此不再解析（解析要持 GIL 15–35 ms），持 GIL 的单段约 0.1 ms。字符串字面量很多时不剥（正则逐个匹配几十万个短字符串
  反而要 50 ms），交给解析与下面的解码预算；
- 并发与速率：读请求体之前，每个容器同时在读 / 解码的请求最多 :data:`MAX_IN_FLIGHT_PER_CONTAINER`（2）个、正在执行的
  方法最多 :data:`MAX_EXECUTING_PER_CONTAINER`（8）个，超出立即拒绝、不排队；每个容器有一份解码时间预算（令牌桶：
  每秒补 :data:`DECODE_BUDGET_PER_SECOND`（50 ms）、最多攒 :data:`DECODE_BUDGET_BURST`（100 ms），按实测解码耗时扣），
  欠账时同样在读请求体之前就拒绝。只限并发不够：单个容器背靠背地发、每个都被拒，解码线程照样满负荷，`json.loads` 每次持 GIL
  几十毫秒，事件循环每次 I/O 唤醒都要等。正常调用的参数是 KB 级，解码不到 1 ms，碰不到预算；全进程等待解码的请求最多
  `max(`:data:`MAX_PENDING_DECODES`（32）`, 2 × SANDBOX_MAX_CONCURRENT)` 个。这几种拒绝都标成“忙”，带建议的重试间隔，
  `api_caller.py` 自动退避重试。

沙盒侧编码器（`ext_caller_code.py`）用 :func:`sandbox_limits` 下发的同一组数字在发出之前预检全部上限，
超限抛 `RpcArgumentError`（模型代码可以捕获），不会发到宿主再被拒、整段脚本退出。

## 返回方向

宿主 -> 沙盒的返回值仍是 pickle（数据由宿主生成），见 `tools/safe_pickle.py`。这里能解出来的每一种值，
返回方向的白名单都放行（`tests/test_rpc_codec.py` 钉住），所以“原样返回参数”的插件在两个方向上都对得上。
"""

import base64
import binascii
import datetime
import json
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Tuple

TYPE_TAG = "$rpc"
VALUE_KEY = "v"

MAX_BODY_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_ITEMS = 100_000
MAX_STR_CHARS = 1_000_000
MAX_BYTES_LEN = MAX_BODY_BYTES * 3 // 4
MAX_INT_DIGITS = 100
MAX_SAME_HASH = 8
MAX_CONTAINERS = 10_000
MAX_IN_FLIGHT_PER_CONTAINER = 2
MAX_EXECUTING_PER_CONTAINER = 8
MAX_PENDING_DECODES = 32
DECODE_BUDGET_PER_SECOND = 0.05
DECODE_BUDGET_BURST = 0.1

# 解析之前剥字符串字面量的前提：字面量不超过这么多个（正则逐个匹配，几十万个短字符串反而比解析还慢）
_PRESCAN_MAX_STRINGS = 20_000
# 字面量不超过这么多个时逐个减掉里面的括号与逗号，不整体复制请求体
_PRESCAN_FEW_STRINGS = 64
# 分片数结构字符：每片 64 KiB（约 25 µs），片间让出 GIL 这么久
_PRESCAN_SLICE = 64 * 1024
_PRESCAN_PAUSE = 0.0002
# 请求信封自己的容器（根对象、args 列表、kwargs 对象）与分隔符（method / args / kwargs 之间的两个逗号）不算在上限里
_ENVELOPE_CONTAINERS = 3
_ENVELOPE_COMMAS = 2
_JSON_STRING_LITERAL = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"')

# ISO 8601 时间最长形如 `2026-11-02T12:34:56.123456+08:00`（32 字符），64 留足余量
_MAX_ISO_CHARS = 64
# UUID 的字符串形态最长是 `urn:uuid:` 前缀加 36 位
_MAX_UUID_CHARS = 45
_MAX_BASE64_CHARS = (MAX_BYTES_LEN + 2) // 3 * 4
# 方法名与关键字参数名只能是 Python 标识符（不含换行、空白与控制符）：它们会进日志、进方法白名单比较
_MAX_IDENTIFIER_CHARS = 128
# 报错里引用的键 / 标签最多保留这么多字符，错误消息绝不整段回显请求内容
_SHOWN_CHARS = 32

SUPPORTED_TYPES_TEXT = (
    "None / bool / int / float / str / list / tuple / dict / set / frozenset / bytes / "
    "datetime / date / time / timedelta / uuid.UUID"
)


def sandbox_limits() -> Dict[str, int]:
    """写进沙盒 `api_caller.py` 的同一组上限：沙盒侧在发出之前就按它们预检（唯一来源，两边不会各写一份）"""
    return {
        "max_body_bytes": MAX_BODY_BYTES,
        "max_depth": MAX_DEPTH,
        "max_items": MAX_ITEMS,
        "max_containers": MAX_CONTAINERS,
        "max_str_chars": MAX_STR_CHARS,
        "max_bytes_len": MAX_BYTES_LEN,
        "max_int_digits": MAX_INT_DIGITS,
        "max_same_hash": MAX_SAME_HASH,
        "max_identifier_chars": _MAX_IDENTIFIER_CHARS,
        # 沙盒侧同时在途的调用（含方法执行）与宿主每容器执行中的上限相同；读与解码那道更窄的闸靠“忙”拒绝加自动重试
        "max_in_flight": MAX_EXECUTING_PER_CONTAINER,
    }


def prescan_structure(raw: bytes) -> None:
    """解析之前的结构预估：容器数或元素数确定超限时不解析、直接拒绝

    只在“一定会被拒”时拒绝（下界估计）：信封自己的 3 个容器与 2 个逗号不算；每个元素至少占一个逗号，
    标签对象内部多出的一个逗号最多 :data:`MAX_CONTAINERS` 个，所以元素的判据再留出这么多余量。

    Raises:
        RpcCodecError: 容器或元素确定超限
    """
    brackets, commas, quotes = _count_structure(raw)
    too_many_containers = brackets - _ENVELOPE_CONTAINERS > MAX_CONTAINERS
    too_many_items = commas - _ENVELOPE_COMMAS > MAX_ITEMS + MAX_CONTAINERS
    if not (too_many_containers or too_many_items):
        return
    if quotes > 2 * _PRESCAN_MAX_STRINGS:
        return  # 字符串字面量太多，剥起来比解析还贵；交给解析与解码预算
    if quotes <= 2 * _PRESCAN_FEW_STRINGS:
        # 字面量很少（容器密集的请求体通常只有信封里那几个）：用 `find` 逐个找到开头的引号，减掉字面量里面的括号与逗号，
        # 不复制、不用正则扫整个请求体
        pos = raw.find(b'"')
        while pos != -1:
            literal = _JSON_STRING_LITERAL.match(raw, pos)
            if literal is None:
                raise RpcCodecError("RPC 请求被拒绝：请求体不是合法的 JSON（字符串没有闭合）")
            inside_brackets, inside_commas, _ = _count_structure(literal.group())
            brackets -= inside_brackets
            commas -= inside_commas
            pos = raw.find(b'"', literal.end())
    else:
        brackets, commas, _ = _count_structure(_JSON_STRING_LITERAL.sub(b"", raw))
    if brackets - _ENVELOPE_CONTAINERS > MAX_CONTAINERS:
        raise RpcCodecError(f"RPC 请求被拒绝：容器（list / dict）总数超过 {MAX_CONTAINERS}（解析之前的预估）")
    if commas - _ENVELOPE_COMMAS > MAX_ITEMS + MAX_CONTAINERS:
        raise RpcCodecError(f"RPC 请求被拒绝：元素总数超过 {MAX_ITEMS}（解析之前的预估）")


def _count_structure(raw: bytes) -> Tuple[int, int, int]:
    """数 `[` / `{`、`,` 与 `"`：分片数，片与片之间短暂让出 GIL

    `bytes.count` 是 C 代码、全程持 GIL，1 MiB 数一遍约 0.4 ms；预估要数好几遍，一口气数完事件循环就要等 3 ms 以上。
    解码线程与事件循环抢 GIL 时，循环每次 I/O 唤醒都得等解码线程当前这段做完，所以分片数、片间让出
    """
    brackets = commas = quotes = 0
    for start in range(0, len(raw), _PRESCAN_SLICE):
        if start:
            time.sleep(_PRESCAN_PAUSE)
        chunk = raw[start : start + _PRESCAN_SLICE]
        brackets += chunk.count(b"[") + chunk.count(b"{")
        commas += chunk.count(b",")
        quotes += chunk.count(b'"')
    return brackets, commas, quotes


class RpcCodecError(ValueError):
    """请求体不合法或超出上限

    消息会原样回给沙盒（`Run-Error`），只含位置、类型名、标签名、长度这类诊断信息，不回显请求内容。
    """


class _Rejected(Exception):
    """解码途中的拒绝：逐层往外抛时补上位置，最外层换成 :class:`RpcCodecError`

    位置只在出错时才拼：如果对每个元素都预先拼好位置字符串，一个 1 MB 长的键下面挂十万个元素就是平方级的字符串拷贝
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.where: List[str] = []


def _shown(text: str) -> str:
    return repr(text[:_SHOWN_CHARS] + ("…" if len(text) > _SHOWN_CHARS else ""))


def _parse_int(text: str) -> int:
    """JSON 整数的解析钩子：先数位数再 `int()`，超长的数字串一位都不转换"""
    digits = len(text) - 1 if text.startswith("-") else len(text)
    if digits > MAX_INT_DIGITS:
        raise _Rejected(f"整数超过 {MAX_INT_DIGITS} 位")
    return int(text)


def _require_spread_hashes(keys: List[Any]) -> None:
    """构造哈希表之前先查哈希值的分布：排序是 O(n log n)，且不经过任何哈希表"""
    try:
        hashes = sorted([hash(key) for key in keys])
    except TypeError as e:
        raise _Rejected("集合元素与非字符串的字典键必须是可哈希的值（不能是 list / dict / set）") from e
    run = 1
    for index in range(1, len(hashes)):
        if hashes[index] == hashes[index - 1]:
            run += 1
            if run > MAX_SAME_HASH:
                raise _Rejected(f"有超过 {MAX_SAME_HASH} 个元素的哈希值相同（重复元素或刻意构造的哈希碰撞）")
        else:
            run = 1


class _Decoder:
    """把 `json.loads` 的结果逐个检查并换成目标值；只认 JSON 的几种原生类型和固定的标签"""

    def __init__(self) -> None:
        self.items = 0
        self.containers = 0

    def container(self) -> None:
        """每遇到一个 JSON 容器（list / dict，含标签对象、标签的载荷列表与键值对）记一笔"""
        self.containers += 1
        if self.containers > MAX_CONTAINERS:
            raise _Rejected(f"容器（list / dict）总数超过 {MAX_CONTAINERS}")

    def value(self, node: Any, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise _Rejected(f"嵌套超过 {MAX_DEPTH} 层")
        self.items += 1
        if self.items > MAX_ITEMS:
            raise _Rejected(f"元素总数超过 {MAX_ITEMS}")
        kind = type(node)
        if kind is str:
            if len(node) > MAX_STR_CHARS:
                raise _Rejected(f"字符串超过 {MAX_STR_CHARS} 字符")
            return node
        if node is None or kind is bool or kind is int or kind is float:
            return node
        if kind is list:
            self.container()
            return self.sequence(node, depth)
        if kind is dict:
            self.container()
            if TYPE_TAG in node:
                return self.tagged(node, depth)
            decoded: Dict[str, Any] = {}
            for key, item in node.items():
                if len(key) > MAX_STR_CHARS:
                    raise _Rejected(f"字典键超过 {MAX_STR_CHARS} 字符")
                try:
                    decoded[key] = self.value(item, depth + 1)
                except _Rejected as e:
                    e.where.append(f"[{_shown(key)}]")
                    raise
            return decoded
        raise _Rejected(f"不支持的 JSON 值 {kind.__name__}")  # json.loads 不会产出别的类型；兜底

    def sequence(self, node: List[Any], depth: int) -> List[Any]:
        decoded = []
        for index, item in enumerate(node):
            try:
                decoded.append(self.value(item, depth + 1))
            except _Rejected as e:
                e.where.append(f"[{index}]")
                raise
        return decoded

    def tagged(self, node: Dict[str, Any], depth: int) -> Any:
        if len(node) != 2 or VALUE_KEY not in node:
            raise _Rejected(f'类型标签对象只能有 "{TYPE_TAG}" 与 "{VALUE_KEY}" 两个键')
        tag = node[TYPE_TAG]
        if type(tag) is not str:
            raise _Rejected("类型标签必须是字符串")
        decode = _TAG_DECODERS.get(tag)
        if decode is None:
            raise _Rejected(f"不认识的类型标签 {_shown(tag)}；能传的类型：{SUPPORTED_TYPES_TEXT}")
        try:
            return decode(self, node[VALUE_KEY], depth)
        except _Rejected as e:
            e.where.append(f"<{tag}>")
            raise

    # ---- 各标签：载荷形状写死，构造函数写死 ----

    def list_payload(self, payload: Any, depth: int, tag: str) -> List[Any]:
        if type(payload) is not list:
            raise _Rejected(f"{tag} 的载荷必须是列表")
        self.container()
        return self.sequence(payload, depth)

    def as_bytes(self, payload: Any, _depth: int) -> bytes:
        if type(payload) is not str:
            raise _Rejected("bytes 的载荷必须是 base64 字符串")
        if len(payload) > _MAX_BASE64_CHARS:
            raise _Rejected(f"字节串超过 {MAX_BYTES_LEN} 字节")
        try:
            return base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as e:
            raise _Rejected("bytes 的载荷不是合法的 base64") from e

    def as_tuple(self, payload: Any, depth: int) -> Tuple[Any, ...]:
        return tuple(self.list_payload(payload, depth, "tuple"))

    def as_set(self, payload: Any, depth: int) -> set:
        items = self.list_payload(payload, depth, "set")
        _require_spread_hashes(items)
        return set(items)

    def as_frozenset(self, payload: Any, depth: int) -> frozenset:
        items = self.list_payload(payload, depth, "frozenset")
        _require_spread_hashes(items)
        return frozenset(items)

    def as_dict(self, payload: Any, depth: int) -> Dict[Any, Any]:
        if type(payload) is not list:
            raise _Rejected("dict 的载荷必须是 [键, 值] 对的列表")
        self.container()
        keys: List[Any] = []
        values: List[Any] = []
        for index, pair in enumerate(payload):
            try:
                if type(pair) is not list or len(pair) != 2:
                    raise _Rejected("dict 的载荷必须是 [键, 值] 对的列表")
                self.container()
                keys.append(self.value(pair[0], depth + 1))
                values.append(self.value(pair[1], depth + 1))
            except _Rejected as e:
                e.where.append(f"[{index}]")
                raise
        _require_spread_hashes(keys)
        return dict(zip(keys, values))

    def as_timedelta(self, payload: Any, _depth: int) -> datetime.timedelta:
        if type(payload) is not list or len(payload) != 3 or any(type(part) is not int for part in payload):
            raise _Rejected("timedelta 的载荷必须是 [天, 秒, 微秒] 三个整数")
        self.container()
        try:
            return datetime.timedelta(days=payload[0], seconds=payload[1], microseconds=payload[2])
        except OverflowError as e:
            raise _Rejected("timedelta 超出范围") from e

    def as_uuid(self, payload: Any, _depth: int) -> uuid.UUID:
        if type(payload) is not str or len(payload) > _MAX_UUID_CHARS:
            raise _Rejected("uuid 的载荷必须是 UUID 字符串")
        try:
            return uuid.UUID(payload)
        except ValueError as e:
            raise _Rejected("uuid 的载荷不是合法的 UUID") from e


def _iso_decoder(cls: Any) -> Callable[[_Decoder, Any, int], Any]:
    def decode(_self: _Decoder, payload: Any, _depth: int) -> Any:
        if type(payload) is not str or len(payload) > _MAX_ISO_CHARS:
            raise _Rejected(f"{cls.__name__} 的载荷必须是 ISO 8601 字符串")
        try:
            return cls.fromisoformat(payload)
        except ValueError as e:
            raise _Rejected(f"{cls.__name__} 的载荷不是合法的 ISO 8601 字符串") from e

    return decode


_TAG_DECODERS: Dict[str, Callable[[_Decoder, Any, int], Any]] = {
    "bytes": _Decoder.as_bytes,
    "tuple": _Decoder.as_tuple,
    "set": _Decoder.as_set,
    "frozenset": _Decoder.as_frozenset,
    "dict": _Decoder.as_dict,
    "datetime": _iso_decoder(datetime.datetime),
    "date": _iso_decoder(datetime.date),
    "time": _iso_decoder(datetime.time),
    "timedelta": _Decoder.as_timedelta,
    "uuid": _Decoder.as_uuid,
}

# 解码可能产出的全部类型（封闭清单，测试拿它对解码结果做逐节点断言）
DECODED_TYPES = frozenset(
    {
        type(None),
        bool,
        int,
        float,
        str,
        bytes,
        list,
        tuple,
        dict,
        set,
        frozenset,
        datetime.datetime,
        datetime.date,
        datetime.time,
        datetime.timedelta,
        datetime.timezone,
        uuid.UUID,
    },
)


def _is_identifier(text: Any) -> bool:
    return type(text) is str and len(text) <= _MAX_IDENTIFIER_CHARS and text.isidentifier()


def _rejected(e: _Rejected, root: str) -> RpcCodecError:
    where = root + "".join(reversed(e.where))
    if len(where) > 200:
        where = where[:200] + "…"
    return RpcCodecError(f"RPC 请求被拒绝：{e.reason}（位置：{where}）")


def decode_rpc_request_body(raw: bytes) -> Tuple[str, List[Any], Dict[str, Any]]:
    """解码沙盒发来的请求体，返回 `(方法名, 位置参数, 关键字参数)`

    Raises:
        RpcCodecError: 不是合法的请求、含不支持的类型或超出任何一项上限；消息可以直接回给沙盒
    """
    if len(raw) > MAX_BODY_BYTES:
        raise RpcCodecError(f"RPC 请求被拒绝：请求体超过 {MAX_BODY_BYTES} 字节")
    prescan_structure(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RpcCodecError("RPC 请求被拒绝：请求体不是 UTF-8 编码的 JSON") from e
    try:
        document = json.loads(text, parse_int=_parse_int)
    except _Rejected as e:
        raise RpcCodecError(f"RPC 请求被拒绝：{e.reason}") from e
    except RecursionError as e:
        raise RpcCodecError(f"RPC 请求被拒绝：嵌套超过 {MAX_DEPTH} 层") from e
    except ValueError as e:
        raise RpcCodecError(
            "RPC 请求被拒绝：请求体不是合法的 JSON。沙盒 -> NekroAgent 的调用从这一版起是 JSON；"
            "如果沙盒代码没有自己拼请求，说明容器里的 api_caller.py 不是本版宿主生成的",
        ) from e

    if type(document) is not dict or not {"method"} <= set(document) <= {"method", "args", "kwargs"}:
        raise RpcCodecError('RPC 请求被拒绝：请求体必须是 {"method": ..., "args": [...], "kwargs": {...}}')
    method = document["method"]
    if not _is_identifier(method):
        raise RpcCodecError(f"RPC 请求被拒绝：方法名必须是 {_MAX_IDENTIFIER_CHARS} 字符以内的标识符")
    raw_args = document.get("args", [])
    raw_kwargs = document.get("kwargs", {})
    if type(raw_args) is not list or type(raw_kwargs) is not dict:
        raise RpcCodecError("RPC 请求被拒绝：args 必须是列表，kwargs 必须是对象")

    decoder = _Decoder()
    args: List[Any] = []
    for index, item in enumerate(raw_args):
        try:
            args.append(decoder.value(item, 1))
        except _Rejected as e:
            raise _rejected(e, f"args[{index}]") from None
    kwargs: Dict[str, Any] = {}
    for key, item in raw_kwargs.items():
        if not _is_identifier(key):
            raise RpcCodecError(f"RPC 请求被拒绝：关键字参数名必须是 {_MAX_IDENTIFIER_CHARS} 字符以内的标识符")
        try:
            kwargs[key] = decoder.value(item, 1)
        except _Rejected as e:
            raise _rejected(e, f"kwargs[{key!r}]") from None
    return method, args, kwargs
