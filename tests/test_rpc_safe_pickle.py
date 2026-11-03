"""`/ext/rpc_exec` 不能把沙盒发来的字节交给 pickle；返回方向的 pickle 是严格白名单

## 威胁模型

沙盒里跑的是模型生成的代码，它读得到本容器的回调令牌（`ext_caller_code.RPC_SECRET_KEY`），
所以能带着合法令牌 POST 任意字节。最早的实现对请求体直接 `pickle.loads`：带 `__reduce__` 的请求体
会在 NA 进程里执行任意代码。换成白名单 `Unpickler` 也不够：它挡不住 `BUILD` 改写放行的类，
于是请求方向改成 JSON（`tools/rpc_codec.py`，用例见 `tests/test_rpc_codec.py`）；本文件钉的是：

1. 任何 pickle 形态的请求体（`__reduce__`、经典 gadget、任意协议）经真实入口都被拒绝、什么都不执行；
2. 返回方向（宿主生成的数据）仍按白名单 pickle，沙盒侧那份拷贝与宿主一致；
3. 检查顺序：先验身份再读请求体，方法白名单之前只有 JSON 解码。

## 探针

这里的“恶意负载”是良性的：`__reduce__` 返回一个只会往模块级列表里记一笔的函数。
控制组先证明裸 `pickle.loads` 会执行它（探针有效），再证明解码器拒绝且探针没被调用。
真正危险的形态（`os.system`、`builtins.eval`、`subprocess.Popen`……）只构造字节流喂给解码器，
绝不喂给裸 `pickle.loads`。
"""

import ast
import datetime
import decimal
import os
import pathlib
import pickle
import uuid
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import Any, List

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.routers.rpc import rpc_exec
from nekro_agent.schemas.errors import UnauthorizedError
from nekro_agent.services.message_service import message_service
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.schema import SandboxMethodType
from nekro_agent.services.rpc_service import decode_rpc_request
from nekro_agent.services.sandbox.credential_guard import issue_sandbox_call_grant, revoke_sandbox_call_grant
from nekro_agent.tools.rpc_codec import RpcCodecError
from nekro_agent.tools.safe_pickle import ALLOWED_GLOBALS, UnsafePickleError, safe_loads
from tests.rpc_sandbox_client import FakeRpcRequest, load_stub_block
from tests.rpc_sandbox_client import request_body as sandbox_request_body

REPO = Path(__file__).resolve().parent.parent
CHAT_KEY = "onebot_v11-inst1-group_10001"
CONTAINER = "probe-container"

FIRED: List[str] = []


def _probe(marker: str) -> str:
    """良性探针：被反序列化执行时只记一笔"""
    FIRED.append(marker)
    return marker


class Evil:
    def __reduce__(self) -> Any:
        return (_probe, ("fired",))


class Custom:
    """一个普通的自定义类：第三方插件最可能误传的东西"""

    def __init__(self) -> None:
        self.value = 1


@pytest.fixture(autouse=True)
def _clear_probe() -> None:
    FIRED.clear()


def request_body(*args: Any, method: str = "probe_method") -> bytes:
    """旧协议（pickle）的请求体：只用来证明它已经进不来了"""
    return pickle.dumps({"method": method, "args": args, "kwargs": {}})


# ---------------------------------------------------------------------------
# 1. 探针：修前可执行，修后被拒
# ---------------------------------------------------------------------------


class TestTheProbe:
    def test_control_group_plain_pickle_executes_the_payload(self):
        """证明探针有效：这正是最早的实现对请求体做的事"""
        pickle.loads(request_body(Evil()))
        assert FIRED == ["fired"]

    def test_decoder_refuses_and_never_calls_it(self):
        with pytest.raises(RpcCodecError) as caught:
            decode_rpc_request(request_body(Evil()))
        assert FIRED == []
        assert "JSON" in str(caught.value), "拒绝的原因要说清楚：请求方向已经是 JSON"

    def test_safe_loads_refuses_it_too(self):
        """返回方向的白名单同样不执行它（宿主插件返回了带 `__reduce__` 的对象时）"""
        with pytest.raises(UnsafePickleError):
            safe_loads(pickle.dumps(Evil()))
        assert FIRED == []

    @pytest.mark.parametrize("protocol", range(0, 6))
    def test_every_pickle_protocol_is_covered(self, protocol: int):
        with pytest.raises(RpcCodecError):
            decode_rpc_request(pickle.dumps({"method": "m", "args": [Evil()], "kwargs": {}}, protocol=protocol))
        assert FIRED == []

    @pytest.mark.parametrize(
        "stream",
        [
            b"cos\nsystem\n(S'echo hacked'\ntR.",  # os.system
            b"cposix\nsystem\n(S'echo hacked'\ntR.",
            b"cbuiltins\neval\n(S'1+1'\ntR.",
            b"cbuiltins\nexec\n(S'import os'\ntR.",
            b"cbuiltins\n__import__\n(S'os'\ntR.",
            b"cbuiltins\ngetattr\n(cbuiltins\nstr\nS'join'\ntR.",
            b"csubprocess\nPopen\n(S'id'\ntR.",
            b"c__builtin__\neval\n(S'1'\ntR.",  # py2 别名
            b"cnumpy.core.multiarray\nscalar\n(tR.",
            b"ccopyreg\n_reconstructor\n(tR.",
            b"P0\n.",  # 持久化对象
        ],
    )
    def test_classic_gadgets_are_rejected_by_name(self, stream: bytes):
        """这些字节流只喂给解码器，绝不喂给裸 `pickle.loads`：请求方向不认 pickle，返回方向的白名单按名字拒绝"""
        with pytest.raises(RpcCodecError):
            decode_rpc_request(stream)
        with pytest.raises(UnsafePickleError):
            safe_loads(stream)

    @pytest.mark.parametrize("stream", [b"", b"not a pickle", b"\x80\x04\x95", b"(" * 5000, b"\x80\x04N"[:2]])
    def test_garbage_is_a_clean_refusal_not_a_crash(self, stream: bytes):
        with pytest.raises(UnsafePickleError):
            safe_loads(stream)

    @pytest.mark.parametrize(
        "stream",
        [
            b"cbuiltins\nbytearray\n(I1000000000000000\ntR.",
            b"\x80\x04cbuiltins\nbytearray\n\x8a\x07\x00\x80\xc6\xa4~\x8d\x03\x85R.",  # 协议 4 + LONG1，同样 10**15
        ],
    )
    def test_a_whitelisted_constructor_cannot_be_turned_into_a_memory_bomb(self, stream: bytes):
        """`bytearray(10**15)` 是白名单内的类型，但它按整数分配内存：几十个字节的请求体就能让宿主分配任意大的内存。
        断言的是“因为 bytearray 被拒”，而不只是“抛了错”——没有这道闸时它会以 MemoryError 失败，同样是 UnsafePickleError"""
        with pytest.raises(UnsafePickleError) as caught:
            safe_loads(stream)
        assert "bytearray" in str(caught.value)


# ---------------------------------------------------------------------------
# 2. 合法流量不受影响
# ---------------------------------------------------------------------------

VALUES: List[Any] = [
    None,
    True,
    7,
    -3,
    2.5,
    "中文 text",
    b"bytes\x00\xff",
    [1, "a", [2, [3]]],
    (1, 2, (3,)),
    {"a": 1, "b": {"c": [1, 2]}},
    {1, 2, 3},
    frozenset({"x", "y"}),
    bytearray(b"ba"),
    1 + 2j,
    OrderedDict([("a", 1), ("b", 2)]),
    datetime.datetime(2026, 11, 1, 12, 30, 5),
    datetime.datetime(2026, 11, 1, 12, 30, 5, tzinfo=datetime.timezone.utc),
    datetime.datetime(2026, 11, 1, 12, 30, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=8))),
    datetime.date(2026, 11, 1),
    datetime.time(8, 9, 10),
    datetime.timedelta(days=1, seconds=5),
    decimal.Decimal("1.50"),
    uuid.UUID("12345678-1234-5678-1234-567812345678"),
    PurePosixPath("/app/shared/a.png"),
]


class TestLegitimateTrafficStillWorks:
    @pytest.mark.parametrize("protocol", range(3, 6))  # 沙盒的 Python 默认 4/5；协议 0~2 的 bytes 要经 `_codecs.encode`、UUID 要经 copyreg，本来就不在范围内
    @pytest.mark.parametrize("value", VALUES, ids=lambda v: type(v).__name__)
    def test_round_trip(self, value: Any, protocol: int):
        assert safe_loads(pickle.dumps(value, protocol=protocol)) == value

    def test_a_full_request_decodes(self):
        """沙盒侧真实的编码器发出来的请求（JSON）"""
        request = decode_rpc_request(
            sandbox_request_body("send_msg_text", "chat", "hello", ref_msg_id=None, when=datetime.datetime(2026, 1, 1)),
        )
        assert request.method == "send_msg_text"
        assert request.args == ["chat", "hello"]
        assert request.kwargs == {"ref_msg_id": None, "when": datetime.datetime(2026, 1, 1)}

    def test_what_the_whitelist_lets_through_is_only_values(self):
        """行为断言（替换只查模块名的旧判据）：返回方向白名单里每一项，按 pickle 自己的形态构造出来的都是值，
        不是类、函数或模块；而请求方向已经不经过这份白名单（BUILD 用例见 `tests/test_rpc_codec.py`）"""
        samples = {
            ("builtins", "set"): {1},
            ("builtins", "frozenset"): frozenset({1}),
            ("builtins", "bytearray"): bytearray(b"x"),
            ("builtins", "complex"): 1j,
            ("collections", "OrderedDict"): OrderedDict(a=1),
            ("datetime", "datetime"): datetime.datetime(2026, 1, 1),
            ("datetime", "date"): datetime.date(2026, 1, 1),
            ("datetime", "time"): datetime.time(1),
            ("datetime", "timedelta"): datetime.timedelta(1),
            ("datetime", "timezone"): datetime.timezone.utc,
            ("decimal", "Decimal"): decimal.Decimal("1"),
            ("uuid", "UUID"): uuid.uuid4(),
            ("uuid", "SafeUUID"): uuid.SafeUUID.safe,
            ("pathlib", "PurePosixPath"): PurePosixPath("/a"),
            # 只在 POSIX 上能实例化（宿主跑在 Linux 上）；Windows 开发机上跳过这一个样本
            ("pathlib", "PosixPath"): pathlib.PosixPath("/a") if os.name == "posix" else None,
        }
        assert set(samples) == set(ALLOWED_GLOBALS), "白名单变了：给新条目补一个样本，确认它构造出来的是值"
        for protocol in range(3, 6):
            for value in (v for v in samples.values() if v is not None):
                decoded = safe_loads(pickle.dumps(value, protocol=protocol))
                assert decoded == value
                assert not isinstance(decoded, type) and not callable(decoded)


class TestRejectedTypesAreDiagnosable:
    @pytest.mark.parametrize(("value", "named"), [(Custom(), "Custom"), (Evil(), "_probe")], ids=["Custom", "Evil"])
    def test_the_message_names_the_type_and_the_way_out(self, value: Any, named: str):
        with pytest.raises(UnsafePickleError) as caught:
            safe_loads(pickle.dumps(value))
        message = str(caught.value)
        assert named in message
        assert "基础类型" in message and "转换" in message

    def test_a_very_long_type_name_is_cut(self):
        with pytest.raises(UnsafePickleError) as caught:
            safe_loads(b"c" + b"m" * 5000 + b"\n" + b"n" * 5000 + b"\n.")
        assert len(str(caught.value)) < 600


# ---------------------------------------------------------------------------
# 3. 经真实的 RPC 入口
# ---------------------------------------------------------------------------


@pytest.fixture
async def rpc(monkeypatch: pytest.MonkeyPatch):
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

    plugin = NekroPlugin(name="探针", module_name="rpc_pickle_probe", description="", version="0.1.0", author="t", url="")
    returned: List[Any] = []

    @plugin.mount_sandbox_method(SandboxMethodType.TOOL, "探针方法")
    async def probe_method(_ctx: Any, *args: Any) -> Any:
        return returned[0] if returned else list(args)

    @plugin.mount_sandbox_method(SandboxMethodType.AGENT, "探针 Agent 方法")
    async def probe_agent(_ctx: Any, *args: Any) -> Any:
        return returned[0] if returned else list(args)

    monkeypatch.setitem(plugin_collector.loaded_plugins, plugin.key, plugin)
    token = issue_sandbox_call_grant(
        container_key=CONTAINER,
        from_chat_key=CHAT_KEY,
        method_names=["probe_method", "probe_agent"],
    )

    system_messages: List[str] = []

    async def spy_system_message(chat_key: str, agent_messages: str, **_kwargs: Any) -> None:
        system_messages.append(agent_messages)

    monkeypatch.setattr(message_service, "push_system_message", spy_system_message)

    async def call(payload: bytes) -> Any:
        return await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(payload), x_rpc_token=token)  # type: ignore[arg-type]

    call.returned = returned  # type: ignore[attr-defined]
    call.system_messages = system_messages  # type: ignore[attr-defined]
    call.token = token  # type: ignore[attr-defined]
    try:
        yield call
    finally:
        revoke_sandbox_call_grant(CONTAINER)
        await Tortoise.close_connections()


class TestThroughTheRealRpcEntry:
    async def test_a_hostile_body_with_a_valid_token_is_refused_and_not_executed(self, rpc):
        response = await rpc(request_body(Evil()))
        assert FIRED == []
        assert response.headers["Run-Error"] == "True"
        assert "JSON" in response.body.decode()

    async def test_the_sandbox_sees_an_unsupported_type_before_sending(self, rpc):
        """不支持的类型在沙盒里就报错（点名类型与位置），根本不发出去"""
        with pytest.raises(TypeError) as caught:
            sandbox_request_body("probe_method", Custom())
        assert "Custom" in str(caught.value) and "args[0]" in str(caught.value)

    async def test_a_legitimate_call_still_round_trips(self, rpc):
        response = await rpc(sandbox_request_body("probe_method", "a", 1, datetime.datetime(2026, 1, 1)))
        assert response.headers["Run-Error"] == "False"
        assert safe_loads(response.body) == ["a", 1, datetime.datetime(2026, 1, 1)]

    async def test_a_return_value_the_sandbox_cannot_accept_is_a_diagnosable_error(self, rpc):
        rpc.returned.append(Custom())
        response = await rpc(sandbox_request_body("probe_method", "x"))
        assert response.headers["Run-Error"] == "True"
        text = response.body.decode()
        assert "probe_method" in text and "Custom" in text

    async def test_a_return_value_that_cannot_be_pickled_is_a_diagnosable_error(self, rpc):
        rpc.returned.append(lambda: 1)
        response = await rpc(sandbox_request_body("probe_method", "x"))
        assert response.headers["Run-Error"] == "True"
        assert "无法序列化" in response.body.decode()

    async def test_an_agent_result_that_cannot_go_back_leaves_no_system_message(self, rpc):
        """回不去沙盒的返回值在沙盒里就是一次报错：不该先落一条“方法返回了 xx”的 SYSTEM 消息"""
        rpc.returned.append(Custom())
        response = await rpc(sandbox_request_body("probe_agent", "x"))
        assert response.headers["Run-Error"] == "True"
        assert rpc.system_messages == []

    async def test_an_agent_result_that_goes_back_is_still_a_system_message(self, rpc):
        response = await rpc(sandbox_request_body("probe_agent", "x"))
        assert response.headers["Run-Error"] == "False"
        assert safe_loads(response.body) == ["x"]
        assert rpc.system_messages == ["['x']"]


class TestTheOrderOfChecks:
    """危险内容在认证之前一个字节都不读；认证之后、方法白名单之前只有 JSON 解码"""

    async def test_a_bad_token_is_refused_before_the_body_is_read(self, rpc):
        class _Untouchable:
            @property
            def headers(self) -> Any:
                raise AssertionError("身份不对时不该碰请求体")

            async def stream(self) -> Any:
                raise AssertionError("身份不对时不该读请求体")
                yield b""  # pragma: no cover

            async def body(self) -> bytes:
                raise AssertionError("身份不对时不该读请求体")

        with pytest.raises(UnauthorizedError):
            await rpc_exec(CONTAINER, CHAT_KEY, _Untouchable(), x_rpc_token="not-the-token")  # type: ignore[arg-type]
        with pytest.raises(UnauthorizedError):
            await rpc_exec(CONTAINER, "onebot_v11-inst1-group_other", _Untouchable(), x_rpc_token=rpc.token)  # type: ignore[arg-type]

    async def test_a_hostile_body_naming_an_ungranted_method_never_executes(self, rpc):
        """方法名就在请求体里，方法白名单只能在解码之后查：所以解码这一步本身必须不可能执行代码"""
        response = await rpc(request_body(Evil(), method="not_granted"))
        assert FIRED == []
        assert response.headers["Run-Error"] == "True"

    async def test_a_clean_body_naming_an_ungranted_method_is_unauthorized(self, rpc):
        with pytest.raises(UnauthorizedError):
            await rpc(sandbox_request_body("not_granted", "x"))


# ---------------------------------------------------------------------------
# 4. 沙盒侧那份拷贝与全仓不再有危险的反序列化入口
# ---------------------------------------------------------------------------

STUB = REPO / "nekro_agent" / "services" / "sandbox" / "ext_caller_code.py"


def load_stub_helpers() -> dict:
    return load_stub_block("# --- BEGIN SAFE PICKLE", "# --- END SAFE PICKLE")


class TestTheSandboxSideCopy:
    def test_the_two_whitelists_are_identical(self):
        assert load_stub_helpers()["_SAFE_PICKLE_GLOBALS"] == ALLOWED_GLOBALS

    def test_the_sandbox_refuses_a_hostile_return_value_too(self):
        safe_loads_in_sandbox = load_stub_helpers()["_safe_loads"]
        with pytest.raises(pickle.UnpicklingError):
            safe_loads_in_sandbox(pickle.dumps(Evil()))
        assert FIRED == []
        value = {"when": datetime.datetime(2026, 1, 1), "n": {1, 2}}
        assert safe_loads_in_sandbox(pickle.dumps(value)) == value

    def test_the_stub_no_longer_calls_the_raw_loader(self):
        text = STUB.read_text(encoding="utf-8")
        assert "_pickle.loads(" not in text
        assert "_safe_loads(response.content)" in text

    def test_the_stub_sends_json_not_pickle(self):
        text = STUB.read_text(encoding="utf-8")
        assert "_pickle.dumps(" not in text
        assert "data: bytes = _rpc_encode_request(method.__name__, args, kwargs)" in text


class TestNoDangerousDeserializerIsLeftInTheRepo:
    def _calls(self) -> List[str]:
        offenders: List[str] = []
        for root in ("nekro_agent", "plugins"):
            for path in (REPO / root).rglob("*.py"):
                if path.name == "safe_pickle.py":
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    dotted = ast.unparse(func)
                    if dotted in {"pickle.loads", "pickle.load", "_pickle.loads", "_pickle.load", "marshal.loads", "marshal.load", "shelve.open", "dill.loads", "cloudpickle.loads"}:
                        offenders.append(f"{path.relative_to(REPO)}:{node.lineno} {dotted}")
                    if dotted == "yaml.load" and not any(kw.arg == "Loader" and "Safe" in ast.unparse(kw.value) for kw in node.keywords):
                        offenders.append(f"{path.relative_to(REPO)}:{node.lineno} yaml.load without SafeLoader")
                    if dotted in {"eval"}:
                        offenders.append(f"{path.relative_to(REPO)}:{node.lineno} eval")
        return offenders

    def test_nothing_deserializes_with_a_dangerous_loader(self):
        assert self._calls() == []

    def test_the_criterion_can_see_the_old_pattern(self, tmp_path: Path):
        sample = ast.parse("import pickle\npayload = pickle.loads(raw)\n")
        dotted = [ast.unparse(n.func) for n in ast.walk(sample) if isinstance(n, ast.Call)]
        assert "pickle.loads" in dotted
