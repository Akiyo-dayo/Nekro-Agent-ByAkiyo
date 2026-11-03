"""沙盒 RPC **返回方向**（宿主 -> 沙盒）的白名单 pickle

## 只用于宿主自己生成的数据

沙盒 -> 宿主的请求体**不再**经过这里，改成了 JSON + 固定类型标签（`tools/rpc_codec.py`）。
原因：本模块的 `find_class` 交出去的是**真实的类**，pickle 的 `BUILD` 指令会对它做 `setattr` /
`__dict__.update`。`STACK_GLOBAL pathlib.PurePosixPath` + `(None, {'_str': ...})` + `BUILD` 就能改写宿主进程里
所有 `PurePosixPath` 的 `str()`。白名单挡得住“调用任意函数”，挡不住“改写被放行的类”。

所以这里的 :func:`safe_loads` **绝不能**再喂沙盒发来的字节。它现在只做两件事：宿主在发出返回值之前
用它预检一遍（:func:`ensure_rpc_safe`，输入是宿主自己 `pickle.dumps` 出来的字节），以及沙盒侧
`api_caller.py` 用同一份白名单解宿主的返回值（沙盒里本来就是模型的代码，在那里改写类不越过任何边界）。
`tests/test_rpc_codec.py` 钉住：宿主代码里调用 `safe_loads` 的只有 `ensure_rpc_safe`。

## 为什么返回方向不能直接 `pickle.loads`

沙盒侧解宿主的返回值也不该能导入模块、调用任意可调用对象：返回值里可能带着第三方插件从外部拿来的东西。

## 做法

标准库文档给出的办法：重写 `Unpickler.find_class`，只放行白名单里的 `(模块, 名字)`，其余一律拒绝。
白名单只含**基础类型与确实有人传过的值类型**，这些类型的重建不会调用任何有副作用的东西：

- 基础类型（`str` / `int` / `float` / `bool` / `None` / `bytes` / `list` / `tuple` / `dict`）由 pickle 操作码直接构造，
  根本不经过 `find_class`；
- `set` / `frozenset` / `bytearray` / `complex` 用 `builtins` 里的同名构造器重建；
  其中 `bytearray(<整数>)` 会按整数分配并清零内存，几个字节的请求体就能让宿主分配任意大的内存，
  所以宿主侧只接受从 `bytes` / `str` 重建（pickle 自己生成的形态就是这样），见 :func:`_rebuild_bytearray`；
- `datetime` 的五个类型、`decimal.Decimal`、`uuid.UUID` / `SafeUUID`、`collections.OrderedDict`、
  `pathlib.PurePosixPath` / `PosixPath`（宿主跑在 Linux 上时，返回 `pathlib.Path` 的插件序列化出来就是 `PosixPath`）。

没有持久化对象（`persistent_load` 默认拒绝），也不放行任何扩展码之外的全局。

盘点：内置插件的沙盒方法参数只有 `str` / `int` / `float` / `bool` / `None` 与它们组成的
`list` / `dict`；返回值是 `str` / `bool` / `float` / `dict` / `list` 或 `UntrustedToolResult`（后者不回沙盒，
沙盒只拿到一句占位文字）。白名单比内置插件的实际用量宽，宽出来的部分是给第三方插件的常见值类型。

## 两边各一份

NA -> 沙盒的返回值在宿主侧先过一遍白名单（:func:`ensure_rpc_safe`），沙盒里的 `api_caller.py` 带着同一份白名单的拷贝
（`ext_caller_code.py` 里 `SAFE PICKLE` 标记之间；`tests/test_rpc_safe_pickle.py` 钉死两份一致）。
请求方向能解出来的每一种值（`tools/rpc_codec.py`）这里都放行，原样返回参数的插件两个方向都对得上。

## 第三方插件的影响

经 RPC 返回的值必须是上面的类型。返回了别的类型（自定义类、`numpy` 标量、`pandas` 对象……）会得到
可诊断的错误：说明是哪个类型被拒绝、该怎么办（先转成基础类型）。
"""

import io
import pickle
from typing import Any, FrozenSet, Tuple

# 与 `services/sandbox/ext_caller_code.py` 里的拷贝逐项一致（有测试钉住）
ALLOWED_GLOBALS: FrozenSet[Tuple[str, str]] = frozenset(
    {
        ("builtins", "set"),
        ("builtins", "frozenset"),
        ("builtins", "bytearray"),
        ("builtins", "complex"),
        ("collections", "OrderedDict"),
        ("datetime", "datetime"),
        ("datetime", "date"),
        ("datetime", "time"),
        ("datetime", "timedelta"),
        ("datetime", "timezone"),
        ("decimal", "Decimal"),
        ("uuid", "UUID"),
        ("uuid", "SafeUUID"),
        ("pathlib", "PurePosixPath"),
        ("pathlib", "PosixPath"),
    },
)

MAX_REJECTED_NAME_LENGTH = 80


class UnsafePickleError(pickle.UnpicklingError):
    """字节流里出现了白名单之外的类型，或根本不是合法的 pickle"""


def _rebuild_bytearray(*args: Any) -> bytearray:
    """`bytearray` 只能从 `bytes` / `str` 重建，不能 `bytearray(<整数>)`（按整数分配内存）"""
    if args and not isinstance(args[0], (bytes, str)):
        raise UnsafePickleError("RPC 数据里的 bytearray 只能由 bytes 重建，不接受按长度分配")
    return bytearray(*args)


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if (module, name) == ("builtins", "bytearray"):
            return _rebuild_bytearray
        if (module, name) not in ALLOWED_GLOBALS:
            shown = f"{module}.{name}"
            if len(shown) > MAX_REJECTED_NAME_LENGTH:
                shown = shown[:MAX_REJECTED_NAME_LENGTH] + "..."
            raise UnsafePickleError(
                f"RPC 数据里含有不允许的类型 {shown!r}。沙盒方法的返回值只能是基础类型"
                "（str / int / float / bool / None / bytes / list / tuple / dict / set / frozenset）"
                "以及 datetime / Decimal / UUID / OrderedDict / PurePosixPath；"
                "请先把自定义类、numpy / pandas 对象等转换成基础类型再传递。",
            )
        return super().find_class(module, name)


def safe_loads(data: bytes) -> Any:
    """按白名单反序列化；任何白名单之外的类型、坏数据都抛 :class:`UnsafePickleError`

    **只用于宿主自己生成的字节**：它交出的是真实的类，挡不住 `BUILD` 改写类属性（见模块文档）
    """
    try:
        return _RestrictedUnpickler(io.BytesIO(data), fix_imports=False).load()
    except UnsafePickleError:
        raise
    except Exception as e:  # noqa: BLE001 - 坏字节流、递归过深、内存问题……对调用方统一是“数据不合法”
        raise UnsafePickleError(f"RPC 数据无法解析（{type(e).__name__}）") from e


def ensure_rpc_safe(payload: bytes) -> None:
    """NA -> 沙盒的返回值先在宿主侧过一遍同一个白名单，免得沙盒里才报一个没头没尾的错"""
    safe_loads(payload)
