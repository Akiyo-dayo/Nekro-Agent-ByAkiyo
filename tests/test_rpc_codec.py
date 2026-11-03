"""沙盒 -> 宿主的 RPC 请求解码：只产出纯数据，逐项设上限，并发与事件循环都有闸

## 为什么白名单 pickle 不够

白名单 `Unpickler` 的 `find_class` 交出去的是**真实的类**，pickle 的 `BUILD` 指令会对它 `setattr`。
`STACK_GLOBAL pathlib.PurePosixPath` + `(None, {'_str': <宿主路径>})` + `BUILD`，50 字节就让宿主进程里所有
`PurePosixPath` 的 `str()` 变成攻击者给的路径。另有一组拒绝服务：整数哈希碰撞（二次方）、memo 共享引用放大、
`Decimal` 的 `int()`、请求体无上限、容器密集的请求体在 `json.loads` 里持着 GIL 触发整堆回收、冻住事件循环。

## 探针

本文件的“恶意负载”都是良性的：`BUILD` 写的是一个不存在的属性名（`_nr_build_probe`），从不写 `_str` 这类会让
全进程路径错乱的真实属性。控制组用一份**照抄白名单 pickle 实现**的 `Unpickler`（不是生产代码），先证明探针确实能改写类，
再证明解码器拒绝、类状态不变。原样的 `_str` 载荷只喂给解码器与真实入口，绝不喂给控制组；被改写的类无论如何都会被恢复。
"""

import ast
import asyncio
import base64
import datetime
import decimal
import enum
import gc
import io
import json
import pathlib
import pickle
import time
import uuid
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.routers import rpc as rpc_router
from nekro_agent.routers.rpc import rpc_exec
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services import rpc_service
from nekro_agent.services.message_service import message_service
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.schema import SandboxMethodType
from nekro_agent.services.sandbox.credential_guard import issue_sandbox_call_grant, revoke_sandbox_call_grant
from nekro_agent.tools import rpc_codec
from nekro_agent.tools.rpc_codec import (
    DECODED_TYPES,
    MAX_BODY_BYTES,
    MAX_BYTES_LEN,
    MAX_CONTAINERS,
    MAX_DEPTH,
    MAX_INT_DIGITS,
    MAX_ITEMS,
    MAX_SAME_HASH,
    MAX_STR_CHARS,
    RpcCodecError,
    decode_rpc_request_body,
    sandbox_limits,
)
from nekro_agent.tools.safe_pickle import ALLOWED_GLOBALS, ensure_rpc_safe, safe_loads
from tests.rpc_sandbox_client import SANDBOX_CODEC, FakeRpcRequest, load_stub_block, load_stub_module, request_body

REPO = Path(__file__).resolve().parent.parent
CHAT_KEY = "onebot_v11-inst1-group_10001"
CONTAINER = "codec-probe-container"
P = (1 << 61) - 1  # CPython 整数哈希的模数：`n` 与 `n + k * P` 哈希相同

# 旧的白名单 pickle 解码器在请求方向放行的全部全局，逐个做 BUILD 尝试
OLD_PICKLE_WHITELIST = sorted(
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
PROBE_ATTR = "_nr_build_probe"


def raw_json(document: Any) -> bytes:
    return json.dumps(document, separators=(",", ":")).encode()


def envelope(*args: Any, **kwargs: Any) -> bytes:
    """手写请求体（绕过沙盒侧编码器，模拟不走 `api_caller.py` 的敌意调用方）"""
    return raw_json({"method": "probe_method", "args": list(args), "kwargs": kwargs})


def tagged(tag: str, value: Any) -> Dict[str, Any]:
    return {"$rpc": tag, "v": value}


def _short_unicode(text: str) -> bytes:
    raw = text.encode()
    return pickle.SHORT_BINUNICODE + bytes([len(raw)]) + raw


def build_payload(module: str, name: str, attr: str, value: str) -> bytes:
    """改写类属性那条攻击的真实载荷形态：`STACK_GLOBAL <模块> <名字>` + `(None, {attr: value})` + `BUILD`"""
    return (
        pickle.PROTO
        + b"\x04"
        + _short_unicode(module)
        + _short_unicode(name)
        + pickle.STACK_GLOBAL
        + pickle.NONE
        + pickle.EMPTY_DICT
        + _short_unicode(attr)
        + _short_unicode(value)
        + pickle.SETITEM
        + pickle.TUPLE2
        + pickle.BUILD
        + pickle.STOP
    )


def resolve(module: str, name: str) -> Any:
    return getattr(__import__(module, fromlist=[name]), name)


def class_state(target: Any) -> Dict[str, Any]:
    return dict(vars(target))


def restore_class(target: Any, before: Dict[str, Any]) -> None:
    """把类的 `__dict__` 恢复成 `before`：解码器坏掉（先红后绿、变异验证）时，被改写的类不能拖垮本进程里其余的用例"""
    for key in set(vars(target)) - set(before):
        try:
            delattr(target, key)
        except (AttributeError, TypeError):
            pass
    for key, value in before.items():
        if vars(target).get(key) is not value:
            try:
                setattr(target, key, value)
            except (AttributeError, TypeError):
                pass


class _ClassesUntouched:
    """`with` 块里断言旧白名单放行的类全都没被改写，退出时无论如何都恢复原状"""

    def __enter__(self) -> "_ClassesUntouched":
        self.before = {key: class_state(resolve(*key)) for key in OLD_PICKLE_WHITELIST}
        return self

    def __exit__(self, *_exc: Any) -> None:
        changed = [key for key, state in self.before.items() if class_state(resolve(*key)) != state]
        for key, state in self.before.items():
            restore_class(resolve(*key), state)
        # 即使块里已经在抛别的异常，也要先报“类被改写了”：那才是要紧的事
        assert changed == [], f"这些类被改写了: {changed}"


class OldWhitelistUnpickler(pickle.Unpickler):
    """照抄旧的白名单解码器（控制组专用，不是生产代码）：只查名字，交出真实的类"""

    def find_class(self, module: str, name: str) -> Any:
        if (module, name) not in OLD_PICKLE_WHITELIST:
            raise pickle.UnpicklingError(f"{module}.{name}")
        return super().find_class(module, name)


# ---------------------------------------------------------------------------
# 1. BUILD 改写宿主进程里的类
# ---------------------------------------------------------------------------


class TestBuildCannotReachAHostClass:
    def test_control_group_the_old_whitelist_decoder_lets_build_rewrite_a_class(self):
        """证明探针有效：旧的白名单解码器对 `PurePosixPath` 做 BUILD，类属性真的被改写了"""
        payload = build_payload("pathlib", "PurePosixPath", PROBE_ATTR, "rewritten")
        try:
            OldWhitelistUnpickler(io.BytesIO(payload)).load()
            assert getattr(PurePosixPath, PROBE_ATTR) == "rewritten"
        finally:
            if hasattr(PurePosixPath, PROBE_ATTR):
                delattr(PurePosixPath, PROBE_ATTR)

    @pytest.mark.parametrize(("module", "name"), OLD_PICKLE_WHITELIST, ids=lambda v: str(v))
    @pytest.mark.parametrize("attr", [PROBE_ATTR, "_str", "__init__"])
    def test_every_formerly_allowed_global_is_refused_and_untouched(self, module: str, name: str, attr: str):
        with _ClassesUntouched():
            with pytest.raises(RpcCodecError):
                rpc_service.decode_rpc_request(build_payload(module, name, attr, "/etc/passwd"))

    def test_the_p0_payload_leaves_paths_rendering_as_themselves(self):
        with _ClassesUntouched():
            with pytest.raises(RpcCodecError):
                rpc_service.decode_rpc_request(build_payload("pathlib", "PurePosixPath", "_str", "/etc/passwd"))
            with pytest.raises(RpcCodecError):
                rpc_service.decode_rpc_request(build_payload("pathlib", "PosixPath", "_str", "/etc/passwd"))
            assert str(PurePosixPath("/app/data/x.txt")) == "/app/data/x.txt"
            assert str(pathlib.PurePosixPath("/app/data/x.txt").parent) == "/app/data"

    @pytest.mark.parametrize(
        "document",
        [
            tagged("pathlib.PurePosixPath", "/etc/passwd"),
            tagged("PurePosixPath", "/etc/passwd"),
            tagged("decimal", "1e2000000"),
            tagged("Decimal", "1"),
            tagged("complex", [1, 2]),
            tagged("timezone", 3600),
            tagged("OrderedDict", []),
            tagged("os.system", "id"),
            tagged("__import__", "os"),
            {"$rpc": "set", "v": [], "__class__": "pathlib.PurePosixPath"},
            {"$rpc": "set", "v": [], "__setstate__": {"_str": "/etc/passwd"}},
            {"$rpc": ["set"], "v": []},
            {"$rpc": "set"},
        ],
        ids=lambda d: json.dumps(d)[:60],
    )
    def test_json_cannot_name_a_class_either(self, document: Any):
        """JSON 里能“点名”的只有写死的标签：类名、模块路径、多余的键一律拒绝"""
        with pytest.raises(RpcCodecError):
            rpc_service.decode_rpc_request(envelope(document))

    def test_decoded_values_are_pure_data_whatever_the_tags(self):
        """行为断言（不是只查模块名）：任何能解出来的东西，逐节点都是封闭清单里的纯数据，
        没有类、函数、模块；时区只可能是 `datetime.timezone`"""
        every_tag = [
            tagged("bytes", base64.b64encode(b"\x00\xff").decode()),
            tagged("tuple", [1, tagged("tuple", [])]),
            tagged("set", [1, "a", tagged("tuple", [1, 2])]),
            tagged("frozenset", [tagged("frozenset", [1])]),
            tagged("dict", [[1, "a"], [tagged("tuple", [1, 2]), [3]], ["$rpc", None]]),
            tagged("datetime", "2026-11-02T12:00:00+08:00"),
            tagged("date", "2026-11-02"),
            tagged("time", "08:09:10.000123+01:00"),
            tagged("timedelta", [1, 2, 3]),
            tagged("uuid", "12345678-1234-5678-1234-567812345678"),
            {"plain": [None, True, 1, 1.5, "s", {"nested": {}}]},
        ]
        _method, args, kwargs = decode_rpc_request_body(raw_json({"method": "m", "args": every_tag, "kwargs": {"k": every_tag}}))
        seen = set()

        def walk(value: Any) -> None:
            assert type(value) in DECODED_TYPES, type(value)
            assert not isinstance(value, type) and not callable(value)
            seen.add(type(value))
            if isinstance(value, dict):
                for key, item in value.items():
                    walk(key)
                    walk(item)
            elif isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    walk(item)
            elif isinstance(value, (datetime.datetime, datetime.time)) and value.tzinfo is not None:
                assert type(value.tzinfo) is datetime.timezone

        walk(args)
        walk(kwargs)
        assert seen >= DECODED_TYPES - {datetime.timezone}

    def test_the_request_path_never_touches_pickle(self, monkeypatch: pytest.MonkeyPatch):
        """请求方向一个 pickle 字节都不解：把 pickle 的两个解码入口换成炸弹，合法与敌意的请求都不碰它们"""

        def forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("请求方向不该再经过 pickle")

        monkeypatch.setattr(pickle, "loads", forbidden)
        monkeypatch.setattr(pickle, "Unpickler", forbidden)
        rpc_service.decode_rpc_request(request_body("m", "a", (1, 2), {3}, b"x"))
        with pytest.raises(RpcCodecError):
            rpc_service.decode_rpc_request(build_payload("pathlib", "PurePosixPath", PROBE_ATTR, "x"))

    def test_safe_loads_is_only_used_on_bytes_the_host_produced(self):
        """返回方向的 `safe_loads` 交出的是真实的类，挡不住 BUILD：宿主代码里调用它的只能是 `ensure_rpc_safe`
        （输入是宿主自己 `pickle.dumps` 出来的字节）"""
        callers = []
        for root in ("nekro_agent", "plugins"):
            for path in (REPO / root).rglob("*.py"):
                if path.name == "safe_pickle.py":
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] in {"safe_loads", "_RestrictedUnpickler"}:
                        callers.append(f"{path.relative_to(REPO).as_posix()}:{node.lineno}")
        assert callers == []
        router = ast.parse((REPO / "nekro_agent" / "routers" / "rpc.py").read_text(encoding="utf-8"))
        checked = [ast.unparse(n.args[0]) for n in ast.walk(router) if isinstance(n, ast.Call) and ast.unparse(n.func) == "ensure_rpc_safe"]
        assert checked == ["payload"]
        assigned = [ast.unparse(n.value) for n in ast.walk(router) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "payload"]
        assert assigned == ["pickle.dumps(sandbox_value)"]


# ---------------------------------------------------------------------------
# 2. 两边对得上：沙盒侧编码器 <-> 宿主解码器 <-> 返回方向白名单
# ---------------------------------------------------------------------------


class Color(enum.IntEnum):
    RED = 1


class Label(str, enum.Enum):
    A = "a"


ROUND_TRIP: List[Any] = [
    None,
    True,
    False,
    0,
    -(10**99),
    2**63,
    2.5,
    float("inf"),
    "中文 text\n多行",
    "",
    b"bytes\x00\xff",
    [1, "a", [2, [3]]],
    (1, 2, (3,)),
    (),
    {"a": 1, "b": {"c": [1, 2]}},
    {1: "int key", (1, 2): "tuple key", None: 0, b"k": 1},
    {"$rpc": "a user dict that happens to use the tag key", "v": 1},
    {1, 2, 3},
    {(1, 2), "x"},
    frozenset({"x", frozenset({1})}),
    datetime.datetime(2026, 11, 1, 12, 30, 5),
    datetime.datetime(2026, 11, 1, 12, 30, 5, 123456, tzinfo=datetime.timezone.utc),
    datetime.datetime(2026, 11, 1, 12, 30, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=8))),
    datetime.date(2026, 11, 1),
    datetime.time(8, 9, 10, 11),
    datetime.timedelta(days=-1, seconds=5, microseconds=7),
    uuid.UUID("12345678-1234-5678-1234-567812345678"),
]


def same(a: Any, b: Any) -> bool:
    """值相等且类型一样（`(1,)` 与 `[1]`、`1` 与 `True` 都算不同）"""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return len(a) == len(b) and all(k in b and same(v, b[k]) for k, v in a.items())
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


class TestBothSidesAgree:
    @pytest.mark.parametrize("value", ROUND_TRIP, ids=lambda v: type(v).__name__)
    def test_sandbox_encoder_to_host_decoder(self, value: Any):
        method, args, kwargs = decode_rpc_request_body(request_body("m", value, key=value))
        assert method == "m"
        assert same(args[0], value) and same(kwargs["key"], value)

    def test_conversions_the_sandbox_makes_on_purpose(self):
        _method, args, _kwargs = decode_rpc_request_body(
            request_body(
                "m",
                bytearray(b"ba"),
                memoryview(b"mv"),
                Path("/app/shared/a.png"),
                PurePosixPath("/app/shared/b.png"),
                OrderedDict([("b", 1), ("a", 2)]),
                Color.RED,
                Label.A,
                float("nan"),
            ),
        )
        assert args[:3] == [b"ba", b"mv", str(Path("/app/shared/a.png"))]
        assert args[3] == "/app/shared/b.png" and type(args[3]) is str, "宿主只收到字符串，永远不构造路径对象"
        assert type(args[4]) is dict and list(args[4]) == ["b", "a"], "OrderedDict 按普通 dict 发，顺序保留"
        assert type(args[5]) is int and args[5] == 1
        assert type(args[6]) is str and args[6] == "a"
        assert args[7] != args[7]

    @pytest.mark.parametrize("value", ROUND_TRIP, ids=lambda v: type(v).__name__)
    def test_whatever_the_host_decodes_can_be_returned_to_the_sandbox(self, value: Any):
        """返回方向白名单覆盖请求方向能解出来的每一种值：原样返回参数的插件两个方向都对得上"""
        _method, args, _kwargs = decode_rpc_request_body(request_body("m", value))
        payload = pickle.dumps(args[0])
        ensure_rpc_safe(payload)
        sandbox_loads = load_stub_block("# --- BEGIN SAFE PICKLE", "# --- END SAFE PICKLE")["_safe_loads"]
        assert same(sandbox_loads(payload), value)

    def test_the_stub_template_carries_the_host_limits(self):
        text = (REPO / "nekro_agent" / "services" / "sandbox" / "ext_caller_code.py").read_text(encoding="utf-8")
        assert "_RPC_LIMITS = _json.loads('{RPC_LIMITS_JSON}')" in text
        assert "_pickle.dumps(" not in text, "沙盒不再用 pickle 发请求"
        assert '"Content-Type": "application/json"' in text
        caller = (REPO / "nekro_agent" / "services" / "sandbox" / "ext_caller.py").read_text(encoding="utf-8")
        assert '.replace("{RPC_LIMITS_JSON}", json.dumps(sandbox_limits()))' in caller
        assert SANDBOX_CODEC["_RPC_LIMITS"] == sandbox_limits()
        assert sandbox_limits()["max_body_bytes"] == MAX_BODY_BYTES

    async def test_the_rendered_api_caller_has_the_limits_and_the_parameter_table(self, monkeypatch: pytest.MonkeyPatch):
        from nekro_agent.services.sandbox import ext_caller
        from plugins.builtin import basic

        async def one_method(_ctx: Any = None) -> List[Any]:
            return [SimpleNamespace(func=basic.view_str_content)]

        monkeypatch.setattr(ext_caller.plugin_collector, "get_all_sandbox_methods", one_method)
        code = await ext_caller.get_api_caller_code(container_key="render-probe", from_chat_key=CHAT_KEY)
        revoke_sandbox_call_grant("render-probe")
        assert f"_RPC_LIMITS = _json.loads('{json.dumps(sandbox_limits())}')" in code
        assert "{RPC_LIMITS_JSON}" not in code
        assert "_RPC_METHOD_PARAMS['view_str_content'] = [('data_str', 'str'), ('start_line', 'int')" in code
        compile(code, "api_caller.py", "exec")


class TestUnsupportedTypesAreDiagnosable:
    class Custom:
        pass

    @pytest.mark.parametrize(
        ("value", "named"),
        [
            (decimal.Decimal("1.5"), "decimal.Decimal"),
            (Custom(), "Custom"),
            (1 + 2j, "complex"),
            (datetime.timezone.utc, "datetime.timezone"),
            (object(), "object"),
            (len, "builtin_function_or_method"),
        ],
        ids=["Decimal", "Custom", "complex", "timezone", "object", "function"],
    )
    def test_the_sandbox_names_the_type_the_position_and_the_way_out(self, value: Any, named: str):
        with pytest.raises(TypeError) as caught:
            request_body("send_it", 1, {"x": [0, 0, value]})
        message = str(caught.value)
        assert named in message
        assert "send_it" in message and "args[1]['x'][2]" in message
        assert "能传的类型" in message and "转换" in message

    def test_a_cycle_is_a_clear_error_not_a_recursion_crash(self):
        loop: List[Any] = [1]
        loop.append(loop)
        with pytest.raises(TypeError, match="循环引用"):
            request_body("m", loop)

    def test_a_request_above_the_host_limit_fails_in_the_sandbox_before_sending(self):
        half = "x" * (MAX_BODY_BYTES // 2 + 10)
        with pytest.raises(ValueError) as caught:
            request_body("view_str_content", half, half)
        assert str(MAX_BODY_BYTES) in str(caught.value) and "view_str_content" in str(caught.value)

    def test_the_size_hint_follows_the_methods_real_parameters(self):
        """超限提示按方法的实际参数写：只接受字符串的参数（`view_str_content(data_str: str)`）不能建议“写成文件传路径”"""
        stub = load_stub_module(post=None, method_params={
            "view_str_content": [("data_str", "str"), ("start_line", "int")],
            "send_msg_file": [("chat_key", "str"), ("file_path", "str")],
        })
        error = stub["RpcArgumentError"]
        with pytest.raises(error) as caught:
            stub["_rpc_encode_request"]("view_str_content", ("x" * (MAX_BODY_BYTES + 1),), {})
        assert "data_str" in str(caught.value) and "只接受字符串" in str(caught.value)
        assert "/app/shared" not in str(caught.value)
        with pytest.raises(error) as caught:
            stub["_rpc_encode_request"]("send_msg_file", ("c",), {"file_path": "y" * (MAX_STR_CHARS + 1)})
        assert "file_path" in str(caught.value) and "/app/shared" in str(caught.value)
        with pytest.raises(error) as caught:
            stub["_rpc_encode_request"]("unknown_method", ([0] * MAX_ITEMS,), {})
        assert "分批" in str(caught.value) and "/app/shared" not in str(caught.value)

    def test_a_lone_surrogate_is_still_sent(self):
        _method, args, _kwargs = decode_rpc_request_body(request_body("m", "file-\udcff.txt"))
        assert args == ["file-\udcff.txt"]

    @pytest.mark.parametrize(
        "body",
        [
            envelope(tagged("t" * 100_000, 1)),
            envelope({"k" * 100_000: [tagged("decimal", 1)]}),
            envelope({"k" * 900: {"k" * 900: {"k" * 900: tagged("decimal", 1)}}}),
            raw_json({"method": "m", "args": [], "kwargs": {"x" * 100_000: 1}}),
        ],
        ids=["long-tag", "long-key", "deep-long-keys", "long-kwarg"],
    )
    def test_a_refusal_never_echoes_long_request_content(self, body: bytes):
        """拒绝消息会进日志、回给沙盒：标签、键名都只引用开头一小段，整条消息有上限"""
        with pytest.raises(RpcCodecError) as caught:
            rpc_service.decode_rpc_request(body)
        assert len(str(caught.value)) < 400

    def test_the_host_names_an_unknown_tag_and_lists_what_is_supported(self):
        with pytest.raises(RpcCodecError) as caught:
            rpc_service.decode_rpc_request(envelope({"k": [tagged("decimal", "1e2000000")]}))
        message = str(caught.value)
        assert "'decimal'" in message and "args[0]['k'][0]" in message
        assert "datetime" in message and "bytes" in message
        assert "1e2000000" not in message, "错误消息不回显请求内容"

    @pytest.mark.parametrize(
        "body",
        [
            b"",
            b"not json",
            b"\x80\x04N.",
            b"\xff\xfe",
            raw_json([]),
            raw_json({"args": []}),
            raw_json({"method": "m", "args": {}, "kwargs": {}}),
            raw_json({"method": "m", "args": [], "kwargs": []}),
            raw_json({"method": "m\n[SYSTEM]", "args": []}),
            raw_json({"method": "m", "args": [], "kwargs": {"not an identifier": 1}}),
            raw_json({"method": "m", "args": [], "extra": 1}),
            raw_json({"method": "m" * 200, "args": []}),
        ],
    )
    def test_malformed_requests_are_a_clean_refusal(self, body: bytes):
        with pytest.raises(RpcCodecError):
            rpc_service.decode_rpc_request(body)


# ---------------------------------------------------------------------------
# 3. 拒绝服务的上限
# ---------------------------------------------------------------------------


def nested(depth: int) -> Any:
    value: Any = 0
    for _ in range(depth - 1):
        value = [value]
    return value


class TestLimits:
    def test_depth(self):
        decode_rpc_request_body(envelope(nested(MAX_DEPTH)))
        with pytest.raises(RpcCodecError, match="嵌套超过"):
            decode_rpc_request_body(envelope(nested(MAX_DEPTH + 1)))

    def test_tags_count_as_one_level(self):
        value: Any = 0
        for _ in range(MAX_DEPTH - 1):
            value = tagged("tuple", [value])
        decode_rpc_request_body(envelope(value))
        with pytest.raises(RpcCodecError, match="嵌套超过"):
            decode_rpc_request_body(envelope(tagged("tuple", [value])))

    @pytest.mark.parametrize("tag", ["dict", "set", "frozenset", "tuple"])
    def test_every_container_tag_counts_depth_for_its_elements(self, tag: str):
        """dict 的键与值、set / frozenset / tuple 的元素都比标签深一层（漏掉任何一处，嵌套检查就能被绕过去）"""

        def wrap(value: Any) -> Any:
            return tagged(tag, [[value, 1]] if tag == "dict" else [value])

        value: Any = 0
        for _ in range(MAX_DEPTH):
            value = wrap(value)
        with pytest.raises(RpcCodecError, match="嵌套超过"):
            decode_rpc_request_body(envelope(value))

    def test_dict_keys_count_depth_too(self):
        """一路嵌在键里的 dict：深度检查必须先于可哈希检查生效，否则错误会变成“不可哈希”、深度从没被数过"""
        key: Any = 0
        for _ in range(MAX_DEPTH):
            key = tagged("dict", [[key, 1]])
        with pytest.raises(RpcCodecError, match="嵌套超过"):
            decode_rpc_request_body(envelope(key))

    def test_containers(self):
        decode_rpc_request_body(envelope([[] for _ in range(MAX_CONTAINERS - 1)]))
        with pytest.raises(RpcCodecError, match="容器"):
            decode_rpc_request_body(envelope([[] for _ in range(MAX_CONTAINERS)]))
        with pytest.raises(RpcCodecError, match="容器"):
            decode_rpc_request_body(envelope([{} for _ in range(MAX_CONTAINERS)]))

    def test_json_nesting_far_beyond_the_limit_is_refused_quickly(self):
        start = time.perf_counter()
        # 括号数在容器上限之内：解析之前的预估放行，由解析器的递归保护拒绝
        with pytest.raises(RpcCodecError, match="嵌套超过"):
            decode_rpc_request_body(b'{"method":"m","args":[' + b"[" * MAX_CONTAINERS + b"]")
        # 占满请求体的括号：解析之前就被拒
        with pytest.raises(RpcCodecError, match="容器.*解析之前的预估"):
            decode_rpc_request_body(b'{"method":"m","args":[' + b"[" * (MAX_BODY_BYTES - 100) + b"]")
        assert time.perf_counter() - start < 2

    def test_items(self):
        decode_rpc_request_body(envelope(list(range(MAX_ITEMS - 1))))
        with pytest.raises(RpcCodecError, match="元素总数超过"):
            decode_rpc_request_body(envelope(list(range(MAX_ITEMS))))

    def test_items_are_counted_across_arguments(self):
        half = MAX_ITEMS // 2
        with pytest.raises(RpcCodecError, match="元素总数超过"):
            decode_rpc_request_body(envelope([0] * half, [0] * half))

    def test_string(self):
        decode_rpc_request_body(envelope("x" * MAX_STR_CHARS))
        with pytest.raises(RpcCodecError, match="字符串超过"):
            decode_rpc_request_body(envelope("x" * (MAX_STR_CHARS + 1)))
        with pytest.raises(RpcCodecError, match="字典键超过"):
            decode_rpc_request_body(envelope({"k" * (MAX_STR_CHARS + 1): 1}))

    def test_bytes(self, monkeypatch: pytest.MonkeyPatch):
        """单个字节串的上限单独钉住：把请求体上限临时放大，确认字节串上限自己也生效"""
        monkeypatch.setattr(rpc_codec, "MAX_BODY_BYTES", MAX_BODY_BYTES * 4)
        decode_rpc_request_body(envelope(tagged("bytes", base64.b64encode(b"\x01" * MAX_BYTES_LEN).decode())))
        with pytest.raises(RpcCodecError, match="字节串超过"):
            decode_rpc_request_body(envelope(tagged("bytes", base64.b64encode(b"\x01" * (MAX_BYTES_LEN + 3)).decode())))
        with pytest.raises(RpcCodecError, match="base64"):
            decode_rpc_request_body(envelope(tagged("bytes", "not base64!")))

    def test_integer_digits(self):
        decode_rpc_request_body(envelope(10**MAX_INT_DIGITS - 1, -(10**MAX_INT_DIGITS - 1)))
        for value in (10**MAX_INT_DIGITS, -(10**MAX_INT_DIGITS)):
            with pytest.raises(RpcCodecError, match="整数超过"):
                decode_rpc_request_body(envelope(value))

    def test_a_huge_integer_is_refused_before_it_is_converted(self, monkeypatch: pytest.MonkeyPatch):
        """位数在 `int()` 之前就数：一个 50 万位的数字串一位都不转换（CPython 的 str -> int 是平方级的，
        而且不能指望解释器自己的 4300 位上限——它可以被 `PYTHONINTMAXSTRDIGITS=0` 关掉）"""
        converted: List[int] = []

        def spy_int(text: Any) -> int:
            converted.append(len(text))
            return int(text)

        monkeypatch.setattr(rpc_codec, "int", spy_int, raising=False)
        with pytest.raises(RpcCodecError, match="整数超过"):
            decode_rpc_request_body(b'{"method":"m","args":[1,' + b"9" * 500_000 + b"]}")
        assert converted == [1]

    def test_timedelta_out_of_range_is_a_refusal(self):
        with pytest.raises(RpcCodecError, match="timedelta"):
            decode_rpc_request_body(envelope(tagged("timedelta", [10**12, 0, 0])))

    @pytest.mark.parametrize("tag", ["set", "frozenset"])
    def test_colliding_set_members(self, tag: str):
        decode_rpc_request_body(envelope(tagged(tag, [1 + k * P for k in range(MAX_SAME_HASH)])))
        with pytest.raises(RpcCodecError, match="哈希值相同"):
            decode_rpc_request_body(envelope(tagged(tag, [1 + k * P for k in range(MAX_SAME_HASH + 1)])))

    def test_colliding_dict_keys(self):
        with pytest.raises(RpcCodecError, match="哈希值相同"):
            decode_rpc_request_body(envelope(tagged("dict", [[1 + k * P, k] for k in range(MAX_SAME_HASH + 1)])))

    def test_colliding_tuples(self):
        """元组的哈希由元素哈希算出：元素同哈希，元组就同哈希。判据看的是哈希值本身，不只认整数"""
        members = [tagged("tuple", [1 + k * P, "x"]) for k in range(MAX_SAME_HASH + 1)]
        with pytest.raises(RpcCodecError, match="哈希值相同"):
            decode_rpc_request_body(envelope(tagged("set", members)))

    def test_repeated_members_count_too(self):
        with pytest.raises(RpcCodecError, match="哈希值相同"):
            decode_rpc_request_body(envelope(tagged("set", ["same"] * (MAX_SAME_HASH + 1))))

    def test_unhashable_members_are_a_refusal(self):
        with pytest.raises(RpcCodecError, match="可哈希"):
            decode_rpc_request_body(envelope(tagged("set", [[1]])))

    def test_a_full_body_of_colliding_integers_is_refused_fast(self):
        """修复前：575 KB 的同哈希整数集合解码 9 秒（二次方）。现在整个请求体塞满也在排序之后直接拒绝"""
        members: List[int] = []
        body = b""
        for k in range(1, 60_000):
            members.append(1 + k * P)
        while True:
            body = envelope(tagged("set", members))
            if len(body) <= MAX_BODY_BYTES:
                break
            members = members[: len(members) * 19 // 20]
        start = time.perf_counter()
        with pytest.raises(RpcCodecError, match="哈希值相同"):
            decode_rpc_request_body(body)
        assert time.perf_counter() - start < 2

    def test_the_worst_set_that_passes_the_check_is_still_linear(self):
        """能通过检查的最坏形态：每组 8 个完全同哈希、组与组只在高位不同（低位相同，探测序列前段重合）"""
        members = [(group << 45) + m * P for group in range(12_000) for m in range(MAX_SAME_HASH)]
        while True:
            body = envelope(tagged("set", members))
            if len(body) <= MAX_BODY_BYTES:
                break
            members = members[: len(members) * 19 // 20]
        start = time.perf_counter()
        _method, args, _kwargs = decode_rpc_request_body(body)
        assert len(args[0]) == len(members)
        assert time.perf_counter() - start < 2

    def test_the_pickle_memo_bomb_is_refused(self):
        """memo 放大的形态：每层是“上一层引用两次”的列表，30 层两百来字节，展开后是 2^30 个叶子。
        控制组（旧的白名单解码器）几微秒就解出来；JSON 没有引用，新解码器根本不认这种字节流"""
        bomb = pickle.PROTO + b"\x04" + pickle.EMPTY_LIST + pickle.MEMOIZE
        for level in range(1, 31):
            bomb += pickle.MARK + pickle.BINGET + bytes([level - 1]) + pickle.BINGET + bytes([level - 1]) + pickle.LIST + pickle.MEMOIZE
        bomb += pickle.STOP
        assert len(bomb) < 260
        decoded = OldWhitelistUnpickler(io.BytesIO(bomb)).load()
        assert decoded[0] is decoded[1], "控制组：两个元素是同一个对象，展开量是请求体的指数倍"
        # 直接喂解码器本身，不经 `rpc_service`：先红后绿 / 变异验证时那里可能是旧的 pickle 解码，
        # 旧路径会把它交给 pydantic 校验，报错信息要 repr 整个展开结构，几秒就吃掉几个 GB
        with pytest.raises(RpcCodecError):
            decode_rpc_request_body(bomb)

    def test_decoded_trees_share_nothing(self):
        """解出来的是一棵树：同样的子结构出现多次，就是多个独立对象，大小与请求体成正比"""
        _method, args, _kwargs = decode_rpc_request_body(envelope([[1, 2]] * 3, {"a": [0], "b": [0]}))
        containers = [args[0][0], args[0][1], args[0][2], args[1]["a"], args[1]["b"]]
        assert len({id(c) for c in containers}) == len(containers)

    def test_the_body_limit_itself(self):
        room = MAX_BODY_BYTES - len(envelope("", ""))
        exactly = envelope("a" * (room // 2), "b" * (room - room // 2))
        assert len(exactly) == MAX_BODY_BYTES
        decode_rpc_request_body(exactly)
        with pytest.raises(RpcCodecError, match="请求体超过"):
            decode_rpc_request_body(envelope("a" * (room // 2), "b" * (room - room // 2 + 1)))


class TestStructureEstimateBeforeParsing:
    """`json.loads` 一旦开始就要把整个结构建完；容器 / 元素确定超限的请求体在解析之前就被拒"""

    @pytest.fixture
    def no_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("确定超限的请求体不该走到 json.loads")

        monkeypatch.setattr(rpc_codec, "json", SimpleNamespace(loads=refuse))

    @pytest.mark.parametrize("item", ["[]", "{}", "[" * 16 + "]" * 16, "[[],{}]"])
    def test_container_dense_bodies_are_refused_without_parsing(self, no_parsing: None, item: str):
        with pytest.raises(RpcCodecError, match="容器.*解析之前的预估"):
            decode_rpc_request_body(dense(item))

    @pytest.mark.parametrize("item", ["0", "true", "null"])
    def test_item_dense_bodies_are_refused_without_parsing(self, no_parsing: None, item: str):
        with pytest.raises(RpcCodecError, match="元素总数.*解析之前的预估"):
            decode_rpc_request_body(dense(item))

    def test_brackets_and_commas_inside_strings_do_not_count(self):
        text = "[{," * 150_000
        assert decode_rpc_request_body(request_body("echo_args", text, key="]}" * 1000)) == ("echo_args", [text], {"key": "]}" * 1000})

    def test_escaped_quotes_do_not_end_a_string_early(self):
        text = 'a\\"' + "[" * (MAX_CONTAINERS * 2) + '\\\\"b'
        assert decode_rpc_request_body(request_body("echo_args", text))[1] == [text]

    def test_an_unclosed_string_is_refused_before_parsing(self, no_parsing: None):
        with pytest.raises(RpcCodecError, match="不是合法的 JSON"):
            decode_rpc_request_body(b'{"method":"m","args":[' + b"[]," * 20_000 + b'"open')

    def test_counting_a_large_body_yields_between_slices(self, monkeypatch: pytest.MonkeyPatch):
        """`bytes.count` 全程持 GIL：大请求体分片数，片间让出，事件循环不必等整段数完；小请求体不让出、不多花时间"""
        pauses: List[float] = []
        monkeypatch.setattr(rpc_codec, "time", SimpleNamespace(sleep=pauses.append))
        rpc_codec.prescan_structure(request_body("echo_args", "x" * 60_000, [[]] * 1000))
        assert pauses == []
        with pytest.raises(RpcCodecError):
            rpc_codec.prescan_structure(dense("[]"))
        slices = -(-MAX_BODY_BYTES // rpc_codec._PRESCAN_SLICE)  # noqa: SLF001
        assert len(pauses) >= slices - 1 and all(p > 0 for p in pauses)

    def test_the_largest_legitimate_structure_still_parses(self):
        value = [[] for _ in range(MAX_CONTAINERS - 1)]
        assert len(decode_rpc_request_body(envelope(value))[1][0]) == MAX_CONTAINERS - 1

    def test_with_very_many_string_literals_the_parser_decides(self):
        """字符串字面量很多时不剥（正则逐个匹配几十万个短字符串比解析还慢）：交给解析之后的计数，结论一样是拒绝"""
        with pytest.raises(RpcCodecError) as caught:
            decode_rpc_request_body(dense('["a"]'))
        assert "容器" in str(caught.value) and "预估" not in str(caught.value)


def chain(depth: int, wrap: Any) -> Any:
    value: Any = 0
    for _ in range(depth - 1):
        value = wrap(value)
    return value


RELAXED = {key: value * 1000 for key, value in sandbox_limits().items()}
RELAXED_CODEC = load_stub_block("# --- BEGIN RPC CODEC", "# --- END RPC CODEC", limits=RELAXED)

# (名字, 刚好在上限内的值, 刚好超限的值)：沙盒在超限的那一个上就抛错；放宽沙盒的上限硬发出去，宿主在同一个边界上拒绝
BOUNDARIES = [
    ("depth-list", nested(MAX_DEPTH), nested(MAX_DEPTH + 1)),
    ("depth-tuple", chain(MAX_DEPTH, lambda v: (v,)), chain(MAX_DEPTH + 1, lambda v: (v,))),
    ("depth-dict-value", chain(MAX_DEPTH, lambda v: {1: v}), chain(MAX_DEPTH + 1, lambda v: {1: v})),
    ("depth-frozenset", chain(MAX_DEPTH, lambda v: frozenset([v])), chain(MAX_DEPTH + 1, lambda v: frozenset([v]))),
    ("items", list(range(MAX_ITEMS - 1)), list(range(MAX_ITEMS))),
    ("containers-lists", [[] for _ in range(MAX_CONTAINERS - 1)], [[] for _ in range(MAX_CONTAINERS)]),
    ("containers-tuples", [() for _ in range((MAX_CONTAINERS - 1) // 2)], [() for _ in range((MAX_CONTAINERS - 1) // 2 + 1)]),
    ("containers-dict-pairs", {i: 0 for i in range(MAX_CONTAINERS - 2)}, {i: 0 for i in range(MAX_CONTAINERS - 1)}),
    ("containers-timedelta", [datetime.timedelta(1)] * ((MAX_CONTAINERS - 1) // 2), [datetime.timedelta(1)] * ((MAX_CONTAINERS - 1) // 2 + 1)),
    ("string", "x" * MAX_STR_CHARS, "x" * (MAX_STR_CHARS + 1)),
    ("integer", 10**MAX_INT_DIGITS - 1, 10**MAX_INT_DIGITS),
    ("negative-integer", -(10**MAX_INT_DIGITS - 1), -(10**MAX_INT_DIGITS)),
    ("same-hash-set", {1 + k * P for k in range(MAX_SAME_HASH)}, {1 + k * P for k in range(MAX_SAME_HASH + 1)}),
    ("same-hash-dict", {1 + k * P: k for k in range(MAX_SAME_HASH)}, {1 + k * P: k for k in range(MAX_SAME_HASH + 1)}),
]


class TestTheSandboxPrechecksEveryLimit:
    """沙盒侧编码器按宿主的计数口径预检全部上限：超限抛 `RpcArgumentError`（模型代码能捕获），不发出去、不让整段脚本退出"""

    @pytest.mark.parametrize(("name", "inside", "outside"), BOUNDARIES, ids=[b[0] for b in BOUNDARIES])
    def test_both_sides_draw_the_line_in_the_same_place(self, name: str, inside: Any, outside: Any):
        decode_rpc_request_body(request_body("m", inside))
        with pytest.raises(SANDBOX_CODEC["RpcArgumentError"]):
            request_body("m", outside)
        with pytest.raises(RpcCodecError):
            decode_rpc_request_body(RELAXED_CODEC["_rpc_encode_request"]("m", (outside,), {}))

    def test_bytes_and_keyword_names_are_checked_too(self):
        error = SANDBOX_CODEC["RpcArgumentError"]
        with pytest.raises(error, match="字节串超过"):
            request_body("m", b"\x01" * (MAX_BYTES_LEN + 1))
        with pytest.raises(error, match="标识符"):
            request_body("m", **{"not an identifier": 1})
        with pytest.raises(error, match="字典键超过"):
            request_body("m", {"k" * (MAX_STR_CHARS + 1): 1})

    def test_the_argument_error_is_catchable_as_type_or_value_error(self):
        error = SANDBOX_CODEC["RpcArgumentError"]
        assert issubclass(error, TypeError) and issubclass(error, ValueError)
        assert not issubclass(SANDBOX_CODEC["RpcRejectedError"], (TypeError, ValueError, SystemExit))


class TestTheSandboxProxy:
    """`api_caller.py` 里真正发请求的那段代理：宿主拒绝时抛模型代码能捕获的异常；同时在途的调用数与宿主的闸门一致"""

    def response(self, *, rejected: bool = False, error: bool = False, body: bytes = b"") -> Any:
        headers = {"Run-Error": "True" if (rejected or error) else "False", "Method-Type": "tool"}
        if rejected:
            headers["Rpc-Rejected"] = "True"
        return SimpleNamespace(status_code=200, headers=headers, text=body.decode(errors="replace"), content=body)

    def busy(self, retry_after_ms: str = "20") -> Any:
        reply = self.response(rejected=True, body="RPC 请求被拒绝：本容器已有 2 个调用正在传输或解码".encode())
        reply.headers.update({"Rpc-Busy": "True", "Rpc-Retry-After-Ms": retry_after_ms})
        return reply

    def test_a_host_rejection_raises_instead_of_exiting(self):
        sent: List[Any] = []
        stub = load_stub_module(post=lambda *a, **_k: sent.append(a) or self.response(rejected=True, body="RPC 请求被拒绝：容器总数超过 10000".encode()))
        proxied = stub["__extension_method_proxy"](lambda *a, **k: None)
        with pytest.raises(stub["RpcRejectedError"]) as caught:
            proxied("x")
        assert isinstance(caught.value, RuntimeError) and "容器总数" in str(caught.value)
        assert len(sent) == 1, "不是“忙”的拒绝（超限、格式不对）重试也没用，不重试"

    def test_a_busy_host_is_retried_with_backoff(self):
        replies = [self.busy(), self.busy("not-a-number"), self.response(body=pickle.dumps("ok"))]
        sent: List[float] = []

        def post(*_a: Any, **_k: Any) -> Any:
            sent.append(time.perf_counter())
            return replies[len(sent) - 1]

        stub = load_stub_module(post=post)
        assert stub["__extension_method_proxy"](lambda *a, **k: None)("x") == "ok"
        assert len(sent) == 3
        # 间隔取宿主建议（20 ms）与指数退避（50 ms、100 ms）中较大的一个
        assert sent[1] - sent[0] >= 0.05 and sent[2] - sent[1] >= 0.1

    def test_busy_retries_give_up_and_raise_a_catchable_error(self):
        sent: List[Any] = []
        stub = load_stub_module(post=lambda *a, **_k: sent.append(a) or self.busy())
        stub["_RPC_BUSY_RETRY_SECONDS"] = 0.3
        with pytest.raises(stub["RpcRejectedError"]) as caught:
            stub["__extension_method_proxy"](lambda *a, **k: None)("x")
        assert "本容器已有" in str(caught.value)
        assert 2 <= len(sent) <= 5

    def test_a_busy_wait_does_not_hold_an_in_flight_slot(self):
        """退避等待期间不占沙盒侧的在途名额：别的线程的调用照常发出"""
        import threading

        first_busy = threading.Event()
        order: List[str] = []

        def post(_url: str, **kwargs: Any) -> Any:
            if b'"busy"' in kwargs["data"] and not first_busy.is_set():
                first_busy.set()
                return self.busy("300")
            order.append("busy" if b'"busy"' in kwargs["data"] else "other")
            return self.response(body=pickle.dumps("ok"))

        stub = load_stub_module(post=post)
        stub["_RPC_IN_FLIGHT"] = stub["_threading"].BoundedSemaphore(1)
        proxied = stub["__extension_method_proxy"](lambda *a, **k: None)
        waiting = threading.Thread(target=proxied, args=("busy",))
        waiting.start()
        first_busy.wait(5)
        started = time.perf_counter()
        proxied("other")
        assert time.perf_counter() - started < 0.2
        waiting.join()
        assert order == ["other", "busy"]

    def test_a_method_error_still_ends_the_script(self):
        stub = load_stub_module(post=lambda *_a, **_k: self.response(error=True, body=b"boom"))
        with pytest.raises(SystemExit):
            stub["__extension_method_proxy"](lambda *a, **k: None)("x")

    def test_a_local_limit_never_reaches_the_host(self):
        sent: List[Any] = []
        stub = load_stub_module(post=lambda *a, **k: sent.append(a) or self.response())
        with pytest.raises(stub["RpcArgumentError"]):
            stub["__extension_method_proxy"](lambda *a, **k: None)(nested(MAX_DEPTH + 1))
        assert sent == []

    def test_concurrent_calls_queue_in_the_sandbox(self):
        """多线程的模型代码在沙盒里排队：同时在途的调用不超过宿主每容器“执行中”的上限，碰不到那道“超出立即拒绝”的闸"""
        import threading

        lock = threading.Lock()
        active = [0, 0]

        def post(*_a: Any, **_k: Any) -> Any:
            with lock:
                active[0] += 1
                active[1] = max(active[1], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return self.response(body=pickle.dumps("ok"))

        stub = load_stub_module(post=post)
        proxied = stub["__extension_method_proxy"](lambda *a, **k: None)
        threads = [threading.Thread(target=proxied, args=(i,)) for i in range(rpc_codec.MAX_EXECUTING_PER_CONTAINER + 4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert active[1] == rpc_codec.MAX_EXECUTING_PER_CONTAINER
        assert stub["_RPC_LIMITS"]["max_in_flight"] == rpc_codec.MAX_EXECUTING_PER_CONTAINER

    def test_concurrent_slow_methods_are_not_serialized(self):
        """8 个线程同时调一个要 0.3 秒的方法（等外部 I/O 的插件方法）：一起跑完约 0.3 秒；在途上限是 2 的话要排成 4 轮、1.2 秒"""
        import threading

        def post(*_a: Any, **_k: Any) -> Any:
            time.sleep(0.3)
            return self.response(body=pickle.dumps("ok"))

        stub = load_stub_module(post=post)
        proxied = stub["__extension_method_proxy"](lambda *a, **k: None)
        results: List[Any] = []
        threads = [threading.Thread(target=lambda i=i: results.append(proxied(i))) for i in range(8)]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - started
        assert results == ["ok"] * 8
        assert elapsed < 0.6, f"8 个并发调用用了 {elapsed:.2f} 秒，被沙盒侧串行了"


class TestReadingTheBody:
    async def test_a_declared_oversize_body_is_not_read_at_all(self):
        request = FakeRpcRequest(b"x" * 10, content_length=str(MAX_BODY_BYTES + 1))
        with pytest.raises(RpcCodecError, match="Content-Length"):
            await rpc_service.read_rpc_body(request)  # type: ignore[arg-type]
        assert request.consumed == 0

    @pytest.mark.parametrize("declared", ["abc", "-1", "1e9", "²", " 10", "9" * 40])
    async def test_a_malformed_content_length_is_refused(self, declared: str):
        request = FakeRpcRequest(b"{}", content_length=declared)
        with pytest.raises(RpcCodecError):
            await rpc_service.read_rpc_body(request)  # type: ignore[arg-type]
        assert request.consumed == 0

    async def test_an_undeclared_endless_stream_is_cut_while_reading(self):
        """分块传输、不带 `Content-Length`：边读边数，过了上限立刻停，不会先把整个请求体读进内存"""
        request = FakeRpcRequest(b"x" * 65536, content_length=None, endless=True)
        with pytest.raises(RpcCodecError, match="请求体超过"):
            await rpc_service.read_rpc_body(request)  # type: ignore[arg-type]
        assert MAX_BODY_BYTES < request.consumed <= MAX_BODY_BYTES + 65536

    async def test_a_lying_content_length_is_cut_too(self):
        request = FakeRpcRequest(b"x" * 65536, content_length="10", endless=True)
        with pytest.raises(RpcCodecError):
            await rpc_service.read_rpc_body(request)  # type: ignore[arg-type]
        assert request.consumed <= MAX_BODY_BYTES + 65536

    async def test_a_body_at_the_limit_is_read_whole(self):
        payload = b"y" * MAX_BODY_BYTES
        assert await rpc_service.read_rpc_body(FakeRpcRequest(payload, chunk_size=1000)) == payload  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. 经真实入口：超限 / 不合法一律 Run-Error，且发生在拼字符串、调方法之前
# ---------------------------------------------------------------------------


def dense(item: str) -> bytes:
    head = '{"method":"echo_args","args":['
    tail = '],"kwargs":{}}'
    count = (MAX_BODY_BYTES - len(head) - len(tail)) // (len(item) + 1)
    return (head + ",".join([item] * count) + tail).encode()


# 1 MiB 的容器密集形态：`[[],[],…]`、每项 16 层嵌套、`["",…]`
DENSE_BODIES = {
    "lists": dense("[]"),
    "nested16": dense("[" * 16 + "]" * 16),
    "strings": dense('""'),
    # 带大量字符串字面量：解析之前的结构预估不剥它（剥起来比解析还贵），真的会解析、建出十几万个容器
    "lists_with_strings": dense('["a"]'),
}


@pytest.fixture
async def entry(monkeypatch: pytest.MonkeyPatch):
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await DBAdapterInstance.create(
        adapter_key="onebot_v11",
        instance_key="inst1",
        display_name="inst1",
        status="online",
        provider="remote_onebot",
        metadata_json="",
    )
    await DBChatChannel.create(
        adapter_key="onebot_v11",
        instance_key="inst1",
        channel_id="group_10001",
        chat_key=CHAT_KEY,
        channel_type="group",
        data="{}",
    )
    plugin = NekroPlugin(name="编解码探针", module_name="rpc_codec_probe", description="", version="0.1.0", author="t", url="")
    calls: List[Any] = []
    returned: List[Any] = []
    hold: List[asyncio.Event] = []

    @plugin.mount_sandbox_method(SandboxMethodType.TOOL, "原样返回")
    async def echo_args(_ctx: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        for event in list(hold):
            await event.wait()
        return returned[0] if returned else [list(args), kwargs]

    @plugin.mount_sandbox_method(SandboxMethodType.MULTIMODAL_AGENT, "多模态探针")
    async def multimodal_probe(_ctx: Any) -> Any:
        calls.append("multimodal")
        return returned[0] if returned else {"ok": 1}

    monkeypatch.setitem(plugin_collector.loaded_plugins, plugin.key, plugin)
    token = issue_sandbox_call_grant(container_key=CONTAINER, from_chat_key=CHAT_KEY, method_names=["echo_args", "multimodal_probe"])

    async def spy_system_message(chat_key: str, agent_messages: str, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(message_service, "push_system_message", spy_system_message)
    logged: List[str] = []

    class _SpyLogger:
        def __getattr__(self, _level: str) -> Any:
            return lambda message, *_a, **_k: logged.append(str(message))

    monkeypatch.setattr(rpc_router, "logger", _SpyLogger())
    # 日志节流的状态是模块级的：每条用例从空表开始，前一条用例记过的键不会让这条的日志被略过
    monkeypatch.setattr(rpc_router, "_rejection_log", rpc_router._ThrottledLog())  # noqa: SLF001
    monkeypatch.setattr(rpc_router, "_identity_log", rpc_router._ThrottledLog())  # noqa: SLF001

    async def call(request: FakeRpcRequest) -> Any:
        return await rpc_exec(CONTAINER, CHAT_KEY, request, x_rpc_token=token)  # type: ignore[arg-type]

    call.calls = calls  # type: ignore[attr-defined]
    call.returned = returned  # type: ignore[attr-defined]
    call.logged = logged  # type: ignore[attr-defined]
    call.hold = hold  # type: ignore[attr-defined]
    try:
        yield call
    finally:
        revoke_sandbox_call_grant(CONTAINER)
        await Tortoise.close_connections()


def oversized_requests() -> Dict[str, FakeRpcRequest]:
    def body(value: Any) -> bytes:
        return raw_json({"method": "echo_args", "args": [value], "kwargs": {}})

    return {
        "content-length": FakeRpcRequest(b"{}", content_length=str(MAX_BODY_BYTES + 1)),
        "stream": FakeRpcRequest(b"[" * 65536, content_length=None, endless=True),
        "depth": FakeRpcRequest(body(nested(MAX_DEPTH + 5))),
        "items": FakeRpcRequest(body([0] * MAX_ITEMS)),
        "string": FakeRpcRequest(body("s" * (MAX_STR_CHARS + 1))),
        "integer": FakeRpcRequest(b'{"method":"echo_args","args":[' + b"7" * 5000 + b"]}"),
        "collision": FakeRpcRequest(body(tagged("set", [1 + k * P for k in range(50)]))),
        "decimal": FakeRpcRequest(body(tagged("decimal", "1e2000000"))),
        "build": FakeRpcRequest(build_payload("pathlib", "PurePosixPath", "_str", "/etc/passwd")),
        "not-json": FakeRpcRequest(pickle.dumps({"method": "echo_args", "args": [], "kwargs": {}})),
    }


class TestThroughTheRealEntry:
    @pytest.mark.parametrize("case", list(oversized_requests()))
    async def test_every_limit_is_a_run_error_before_anything_runs(self, entry, case: str):
        with _ClassesUntouched():
            response = await entry(oversized_requests()[case])
            assert str(PurePosixPath("/app/data/x.txt")) == "/app/data/x.txt"
        assert response.headers["Run-Error"] == "True"
        assert response.body.decode().startswith("RPC 请求被拒绝")
        assert len(response.body) < 600, "错误消息不回显请求内容"
        assert entry.calls == [], "超限的请求不能调用任何方法"
        assert not any("收到 RPC 执行请求" in line for line in entry.logged), "超限的请求不能走到拼日志那一步"
        assert all(len(line) < 800 for line in entry.logged)

    async def test_the_route_itself_stops_reading_at_the_limit(self, entry):
        """路由层的限长：声明超限的一个字节都不读；不声明的边读边截断。
        不能指望解码器那道请求体检查兜底——那时整个请求体已经在内存里了"""
        declared = FakeRpcRequest(b"{}", content_length=str(MAX_BODY_BYTES + 1))
        response = await entry(declared)
        assert response.headers["Run-Error"] == "True" and "Content-Length" in response.body.decode()
        assert declared.consumed == 0
        endless = FakeRpcRequest(b"[" * 65536, content_length=None, endless=True)
        response = await entry(endless)
        assert response.headers["Run-Error"] == "True"
        assert endless.consumed <= MAX_BODY_BYTES + 65536
        assert entry.calls == []

    async def test_a_legitimate_call_round_trips_every_type(self, entry):
        sandbox_loads = load_stub_block("# --- BEGIN SAFE PICKLE", "# --- END SAFE PICKLE")["_safe_loads"]
        values = [v for v in ROUND_TRIP if v == v]
        response = await entry(FakeRpcRequest(request_body("echo_args", *values, when=datetime.date(2026, 1, 1))))
        assert response.headers["Run-Error"] == "False"
        args, kwargs = sandbox_loads(response.body)
        assert all(same(a, b) for a, b in zip(args, values)) and len(args) == len(values)
        assert kwargs == {"when": datetime.date(2026, 1, 1)}

    async def test_a_validation_error_is_a_run_error_not_an_http_error(self, entry, monkeypatch: pytest.MonkeyPatch):
        """`decode_rpc_request` 抛的 `ValidationError` 以前没被接住，沙盒看到的是 HTTP 错误"""
        monkeypatch.setattr(rpc_service, "decode_rpc_request_body", lambda _raw: ("echo_args", "not a list", {}))
        response = await entry(FakeRpcRequest(request_body("echo_args")))
        assert response.headers["Run-Error"] == "True"
        assert "请求参数无效" in response.body.decode()
        assert entry.calls == []

    def test_the_validation_error_is_the_one_the_router_catches(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rpc_service, "decode_rpc_request_body", lambda _raw: ("m", "not a list", {}))
        with pytest.raises(ValidationError):
            rpc_service.decode_rpc_request(b"{}")

    async def test_a_multimodal_result_that_is_not_json_is_a_run_error_not_a_500(self, entry):
        """MULTIMODAL 分支的 `json.dumps` 以前在 try 之外，返回 set 之类的值直接 500"""
        entry.returned.append({1, 2})
        response = await entry(FakeRpcRequest(request_body("multimodal_probe")))
        assert response.headers["Run-Error"] == "True"
        assert "无法序列化" in response.body.decode() and "multimodal_probe" in response.body.decode()

    async def test_a_multimodal_result_still_goes_back(self, entry):
        response = await entry(FakeRpcRequest(request_body("multimodal_probe")))
        assert response.headers["Run-Error"] == "False"
        assert safe_loads(response.body) == '<AGENT_RESULT>{"ok": 1}</AGENT_RESULT>'

    @pytest.mark.parametrize("containers", [1, 4])
    async def test_a_sustained_stream_from_attacking_containers_does_not_starve_other_coroutines(self, entry, containers: int):
        """真实 uvicorn、请求体经事件循环读：1 个 / 4 个容器各开 2 条连接，持续 2 秒背靠背 POST 1 MiB 的 16 层嵌套请求体
        （每个都会被拒）。判据是旁路协程做本机 TCP 往返的中位延迟——只限并发时，解码线程满负荷、`json.loads` 每次持
        GIL 几十毫秒，旁路往返中位几十毫秒起步；有了每容器解码预算与解析前的结构预估，应回到个位数毫秒以内"""
        import http.client
        import socket
        import statistics
        import threading

        import uvicorn
        from fastapi import FastAPI

        tokens = [issue_sandbox_call_grant(container_key=f"stream-{i}", from_chat_key=CHAT_KEY, method_names=["echo_args"]) for i in range(containers)]
        app = FastAPI()
        app.include_router(rpc_router.router)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            await asyncio.sleep(0.01)

        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while data := await reader.read(1):
                writer.write(data)
                await writer.drain()

        echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", echo_server.sockets[0].getsockname()[1])
        deadline = time.perf_counter() + 2.0
        outcomes: Dict[str, int] = {}

        def attack(container: int) -> None:
            conn = None
            while time.perf_counter() < deadline:
                try:
                    conn = conn or http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    conn.request(
                        "POST",
                        f"/ext/rpc_exec?container_key=stream-{container}&from_chat_key={CHAT_KEY}",
                        body=DENSE_BODIES["nested16"],
                        headers={"Content-Type": "application/json", "X-RPC-Token": tokens[container]},
                    )
                    response = conn.getresponse()
                    response.read()
                    key = "busy" if response.headers.get("Rpc-Busy") == "True" else "rejected"
                    outcomes[key] = outcomes.get(key, 0) + 1
                except Exception:  # noqa: BLE001 - 没读完请求体就回应时服务端会断开，重连
                    if conn is not None:
                        conn.close()
                    conn = None

        threads = [threading.Thread(target=attack, args=(c,)) for c in range(containers) for _ in range(2)]
        samples: List[float] = []
        try:
            for thread in threads:
                thread.start()
            while time.perf_counter() < deadline:
                start = time.perf_counter()
                writer.write(b"x")
                await writer.drain()
                await reader.readexactly(1)
                samples.append(time.perf_counter() - start)
                await asyncio.sleep(0.005)
        finally:
            await asyncio.to_thread(lambda: [thread.join() for thread in threads])
            writer.close()
            echo_server.close()
            server.should_exit = True
            await serving
            for i in range(containers):
                revoke_sandbox_call_grant(f"stream-{i}")
        median = statistics.median(samples)
        assert median < 0.010, f"旁路往返中位 {median * 1000:.1f} ms（{len(samples)} 次），攻击方请求 {outcomes}"
        assert outcomes.get("busy", 0) > 0, outcomes
        assert entry.calls == []

    async def test_a_container_over_its_decode_budget_is_refused_before_the_body_is_read(self, entry):
        rpc_service.decode_budget.charge(CONTAINER, rpc_codec.DECODE_BUDGET_BURST + 0.2)
        request = FakeRpcRequest(request_body("echo_args", 1))
        response = await entry(request)
        assert request.consumed == 0
        assert response.headers["Rpc-Busy"] == "True" and response.headers["Rpc-Rejected"] == "True"
        assert 3000 <= int(response.headers["Rpc-Retry-After-Ms"]) <= 5000
        assert "解码时间预算" in response.body.decode()
        assert entry.calls == []

    async def test_ordinary_calls_stay_far_inside_the_budget(self, entry):
        for index in range(100):
            response = await entry(FakeRpcRequest(request_body("echo_args", "x" * 2000, index, {"k": [1, 2, 3]})))
            assert response.headers["Run-Error"] == "False", response.body
        assert rpc_service.decode_budget.available(CONTAINER) > 0

    async def test_decoding_is_charged_to_the_container_that_sent_it(self, entry):
        before = rpc_service.decode_budget.available(CONTAINER)
        await entry(FakeRpcRequest(DENSE_BODIES["lists_with_strings"]))
        assert rpc_service.decode_budget.available(CONTAINER) < before
        assert rpc_service.decode_budget.available("someone-else") == rpc_codec.DECODE_BUDGET_BURST

    def test_the_budget_refills_over_time(self):
        clock = [1000.0]
        budget = rpc_service._DecodeBudget(clock=lambda: clock[0])  # noqa: SLF001
        budget.charge("c", 0.3)
        assert budget.available("c") == pytest.approx(rpc_codec.DECODE_BUDGET_BURST - 0.3)
        clock[0] += 2.0
        assert budget.available("c") == pytest.approx(rpc_codec.DECODE_BUDGET_BURST - 0.3 + 2 * rpc_codec.DECODE_BUDGET_PER_SECOND)
        clock[0] += 100.0
        assert budget.available("c") == pytest.approx(rpc_codec.DECODE_BUDGET_BURST)

    async def test_executing_methods_per_container_are_bounded(self, entry):
        """每个执行中的调用都持有解码出的参数：执行中达到上限后，后来的调用在读请求体之前就被拒（“忙”），放行之后恢复"""
        release = asyncio.Event()
        entry.hold.append(release)
        tasks = []
        for index in range(rpc_codec.MAX_EXECUTING_PER_CONTAINER):
            tasks.append(asyncio.create_task(entry(FakeRpcRequest(request_body("echo_args", index)))))
            while len(entry.calls) <= index:
                await asyncio.sleep(0.005)
        refused = FakeRpcRequest(request_body("echo_args", "late"))
        try:
            # 上限失效时第 9 个调用会进到方法里等同一个事件：限时，失败得快
            response = await asyncio.wait_for(entry(refused), 5)
        finally:
            release.set()
        assert response.headers.get("Rpc-Busy") == "True" and "正在执行" in response.body.decode()
        assert refused.consumed == 0
        results = await asyncio.gather(*tasks)
        assert all(r.headers["Run-Error"] == "False" for r in results)
        assert rpc_service._executing == {}  # noqa: SLF001
        entry.hold.clear()
        assert (await entry(FakeRpcRequest(request_body("echo_args", "again")))).headers["Run-Error"] == "False"

    async def test_a_call_that_finds_execution_full_after_decoding_is_busy_not_run(self, entry, monkeypatch: pytest.MonkeyPatch):
        """读请求体之前查过一次；解码期间别的调用可能已经占满执行名额，开始执行之前再查一次"""
        from contextlib import nullcontext

        monkeypatch.setattr(rpc_router, "rpc_admission", lambda _key: nullcontext())
        monkeypatch.setitem(rpc_service._executing, CONTAINER, rpc_codec.MAX_EXECUTING_PER_CONTAINER)  # noqa: SLF001
        response = await entry(FakeRpcRequest(request_body("echo_args", 1)))
        assert response.headers.get("Rpc-Busy") == "True" and "正在执行" in response.body.decode()
        assert entry.calls == []

    async def test_the_per_container_read_gate_rejects_the_third_concurrent_request(self, entry):
        responses = await asyncio.gather(*(entry(FakeRpcRequest(DENSE_BODIES["lists_with_strings"])) for _ in range(3)))
        texts = [r.body.decode() for r in responses]
        assert sum("本容器已有" in text for text in texts) == 1, texts
        assert all(r.headers.get("Rpc-Rejected") == "True" for r in responses)
        assert rpc_service._in_flight == {}  # noqa: SLF001

    async def test_the_global_decode_queue_is_bounded(self, entry, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rpc_service, "max_pending_decodes", lambda: 2)
        tokens = [issue_sandbox_call_grant(container_key=f"queue-{i}", from_chat_key=CHAT_KEY, method_names=["echo_args"]) for i in range(5)]
        try:
            responses = await asyncio.gather(
                *(rpc_exec(f"queue-{i}", CHAT_KEY, FakeRpcRequest(DENSE_BODIES["lists_with_strings"]), x_rpc_token=tokens[i]) for i in range(5)),  # type: ignore[arg-type]
            )
        finally:
            for i in range(5):
                revoke_sandbox_call_grant(f"queue-{i}")
        busy = [r for r in responses if "宿主正在解码的请求已满" in r.body.decode()]
        assert len(busy) == 3 and all(r.headers.get("Rpc-Busy") == "True" for r in busy)
        assert rpc_service._pending_decodes == 0  # noqa: SLF001

    def test_the_global_queue_grows_with_the_sandbox_concurrency(self, monkeypatch: pytest.MonkeyPatch):
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "SANDBOX_MAX_CONCURRENT", 4)
        assert rpc_service.max_pending_decodes() == rpc_codec.MAX_PENDING_DECODES
        monkeypatch.setattr(config, "SANDBOX_MAX_CONCURRENT", 30)
        assert rpc_service.max_pending_decodes() == 60

    async def test_decoding_runs_on_the_dedicated_thread_with_gc_paused(self, entry, monkeypatch: pytest.MonkeyPatch):
        seen: List[Any] = []
        original = rpc_service.decode_rpc_request

        def spy(raw: bytes) -> Any:
            import threading

            seen.append((threading.current_thread().name, gc.isenabled()))
            return original(raw)

        monkeypatch.setattr(rpc_service, "decode_rpc_request", spy)
        await entry(FakeRpcRequest(request_body("echo_args", 1)))
        await entry(FakeRpcRequest(DENSE_BODIES["lists_with_strings"]))
        assert [name.startswith("rpc-decode") for name, _gc in seen] == [True, True]
        assert [enabled for _name, enabled in seen] == [False, False]
        assert gc.isenabled()

    async def test_decodes_from_different_containers_run_one_at_a_time(self, monkeypatch: pytest.MonkeyPatch):
        """单线程串行：同一时刻只有一个解码在持 GIL；暂停 GC 是全进程的开关，两个解码并行时先结束的那个会把 GC 提前打开"""
        import threading

        lock = threading.Lock()
        active = [0, 0]
        original = rpc_service.decode_rpc_request

        def spy(raw: bytes) -> Any:
            with lock:
                active[0] += 1
                active[1] = max(active[1], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return original(raw)

        monkeypatch.setattr(rpc_service, "decode_rpc_request", spy)
        decoded = await asyncio.gather(*(rpc_service.decode_rpc_request_off_loop(request_body("echo_args", i), f"serial-{i}") for i in range(4)))
        assert [request.args for request in decoded] == [[0], [1], [2], [3]]
        assert active[1] == 1

    def test_a_rejected_structure_is_freed_before_gc_comes_back(self):
        """拒绝时半成品结构挂在异常的回溯帧上：异常要是被带出解码线程（或被留住），结构会在 GC 恢复之后被整批提升进老年代。
        解码线程只交回原因字符串。这里先整堆回收、清空年轻代，再让 GC 一直关着，数调用之后年轻代里还活着的对象：
        结构还活着的话是几十万个（数绝对值而不是差值：被留住的旧结构换成新结构时，差值会是零）。
        用的请求体带大量字符串字面量，解析之前的结构预估不剥它，真的会建出十几万个容器"""
        gc.collect()
        gc.disable()
        try:
            outcome = rpc_service._decode_with_gc_paused(DENSE_BODIES["lists_with_strings"])  # noqa: SLF001
            alive = len(gc.get_objects(generation=0))
        finally:
            gc.enable()
        assert isinstance(outcome, str) and "容器" in outcome
        assert alive < 10_000, f"拒绝之后年轻代里还有 {alive} 个对象，半成品结构没被释放"

    async def test_a_client_that_disconnects_mid_body_is_refused_quietly(self, entry):
        from starlette.requests import ClientDisconnect

        class _Gone(FakeRpcRequest):
            async def stream(self) -> Any:
                yield b'{"method":'
                raise ClientDisconnect

        for _ in range(20):
            response = await entry(_Gone(b"x" * 100))
            assert response.headers.get("Rpc-Rejected") == "True"
        assert not any("断开" in line for line in entry.logged), "对端断开能被反复制造，不能逐条记日志"

    async def test_bad_identities_are_logged_with_a_bound(self, entry):
        from nekro_agent.schemas.errors import UnauthorizedError

        for index in range(60):
            with pytest.raises(UnauthorizedError):
                await rpc_exec(f"forged-{index % 3}\n[SYSTEM]", CHAT_KEY, FakeRpcRequest(b"{}"), x_rpc_token="wrong")  # type: ignore[arg-type]
        identity_lines = [line for line in entry.logged if "非法的沙盒 RPC 回调身份" in line]
        assert len(identity_lines) == 3, "同一个键每 10 秒一行"
        assert all("\n" not in line for line in identity_lines)
        for index in range(100):
            with pytest.raises(UnauthorizedError):
                await rpc_exec(f"many-{index}", CHAT_KEY, FakeRpcRequest(b"{}"), x_rpc_token="wrong")  # type: ignore[arg-type]
        identity_lines = [line for line in entry.logged if "非法的沙盒 RPC 回调身份" in line]
        assert len(identity_lines) <= rpc_router._identity_log.budget, "键可以随便编，全局每 10 秒另有总量"  # noqa: SLF001

    def test_rejection_logs_are_throttled_per_container(self, monkeypatch: pytest.MonkeyPatch):
        lines: List[str] = []
        monkeypatch.setattr(rpc_router, "logger", SimpleNamespace(warning=lines.append, info=lines.append))
        throttle = rpc_router._ThrottledLog()  # noqa: SLF001
        monkeypatch.setattr(rpc_router, "_rejection_log", throttle)
        for _ in range(50):
            rpc_router._log_rejection("flood-a", "RPC 请求被拒绝：x")  # noqa: SLF001
        rpc_router._log_rejection("flood-b", "RPC 请求被拒绝：y")  # noqa: SLF001
        assert len(lines) == 2
        throttle.interval = 0.0
        rpc_router._log_rejection("flood-a", "RPC 请求被拒绝：x")  # noqa: SLF001
        assert "此前 49 次" in lines[-1]


class TestThroughTheRealHttpStack:
    """经 FastAPI / Starlette 真实的请求对象：`Content-Length` 与分块传输都在读的时候截断"""

    @pytest.fixture
    def client(self, entry):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(rpc_router.router)
        token = issue_sandbox_call_grant(container_key=CONTAINER, from_chat_key=CHAT_KEY, method_names=["echo_args"])

        def post(content: Any) -> Any:
            return TestClient(app).post(
                f"/ext/rpc_exec?container_key={CONTAINER}&from_chat_key={CHAT_KEY}",
                content=content,
                headers={"Content-Type": "application/json", "X-RPC-Token": token},
            )

        post.calls = entry.calls  # type: ignore[attr-defined]
        return post

    def test_a_normal_call(self, client):
        response = client(request_body("echo_args", (1, 2), b"x"))
        assert response.headers["Run-Error"] == "False"
        assert safe_loads(response.content) == [[(1, 2), b"x"], {}]

    def test_an_oversized_body_is_a_run_error(self, client):
        response = client(b"x" * (MAX_BODY_BYTES + 1))
        assert response.status_code == 200 and response.headers["Run-Error"] == "True"
        assert "请求体超过" in response.text
        assert client.calls == []

    def test_an_oversized_chunked_body_is_a_run_error(self, client):
        """不带 `Content-Length` 的分块上传（截断时读了多少由 `TestReadingTheBody` 用替身钉住）"""

        def chunks() -> Any:
            for _ in range(64):
                yield b"[" * 65536

        response = client(chunks())
        assert response.headers["Run-Error"] == "True"
        assert "请求体超过" in response.text
        assert client.calls == []


class TestTheResponseWhitelistIsUnchanged:
    def test_the_response_direction_still_accepts_what_it_accepted(self):
        """返回方向（宿主生成的数据）保持现状：旧白名单放行的类型照样能回传"""
        assert set(OLD_PICKLE_WHITELIST) == set(ALLOWED_GLOBALS)
