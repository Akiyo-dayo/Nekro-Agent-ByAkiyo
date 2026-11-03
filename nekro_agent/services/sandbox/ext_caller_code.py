"""沙盒环境下的扩展方法调用代理"""

import base64 as _base64
import datetime as _datetime
import importlib
import io as _io
import json as _json
import os as _os
import pickle as _pickle
import random as _random
import subprocess
import sys
import threading as _threading
import time as _time
import urllib.parse
import uuid as _uuid
from importlib.metadata import PackageNotFoundError, distributions
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import matplotlib.pyplot as plt
import requests as _requests
from packaging.specifiers import SpecifierSet
from packaging.version import parse

# 设置中文字体
plt.rcParams["font.sans-serif"] = ["SimHei", "DejaVu Sans", "Arial Unicode MS", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

CHAT_API = "{CHAT_API}"
CONTAINER_KEY = "{CONTAINER_KEY}"
FROM_CHAT_KEY = "{FROM_CHAT_KEY}"
# 本容器专属的一次性回调令牌，不是全局密钥。
# 沙盒里的代码由模型生成，必须假设它会读到这个值：因此服务端把令牌绑定到
# (container_key, from_chat_key, 本轮下发的方法白名单) 三元组上并设了有效期，
# 拿到它既换不到别的会话身份，也调不动没下发给本轮的方法。
RPC_SECRET_KEY = "{RPC_SECRET_KEY}"

# 回调的连接超时。宿主与容器之间是同一台机器上的一次 TCP 握手，正常情况下是毫秒级，
# 10 秒留了三个数量级的余量，不会误伤任何真实排队。
#
# **这个值是整条链路上唯一一条无论宿主防火墙怎么坏都成立的兜底。**
# 沙盒回连 NA 的包一旦落进包过滤规则的兜底 DROP（规则被外部改写、配置写错、
# 别的部署共用了同一个标记……），DROP 不回 RST，没有超时的 `requests.post` 会一直等到
# 内核耗尽 SYN 重传（约 130 秒）——而沙盒容器早在那之前就被 NA 按运行超时杀掉了。
# 于是「回调不通」这个真因被整个吞掉，外面只看得到一次没有任何线索的沙盒超时。
RPC_CONNECT_TIMEOUT_SECONDS = 10
# 读超时由宿主注入（取沙盒运行超时，容器本来就活不过它）：纯粹是防「连上了但对端不回」的兜底，
# 不会让任何一次本来能成功的调用失败
RPC_READ_TIMEOUT_SECONDS = int("{RPC_READ_TIMEOUT_SECONDS}")

# 回调不通时打印的标记。它会随 [Sandbox Output] 一起进入模型上下文与执行记录，
# 让「沙盒挂了」在日志和聊天里都能一眼归因，而不是又一次无线索的超时
RPC_UNREACHABLE_TAG = "[NA-RPC-UNREACHABLE]"


# --- BEGIN SAFE PICKLE（与 nekro_agent/tools/safe_pickle.py 的白名单逐项一致，tests/test_rpc_safe_pickle.py 钉住）---
# 宿主回给沙盒的数据同样按白名单反序列化：沙盒里读到的任何字节流都不该能导入模块、调用任意可调用对象
_SAFE_PICKLE_GLOBALS = frozenset(
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


class _SafeUnpickler(_pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) not in _SAFE_PICKLE_GLOBALS:
            raise _pickle.UnpicklingError(f"RPC 返回数据里含有不允许的类型 {module}.{name}")
        return super().find_class(module, name)


def _safe_loads(data: bytes):
    return _SafeUnpickler(_io.BytesIO(data), fix_imports=False).load()


# --- END SAFE PICKLE ---


# --- BEGIN RPC CODEC（沙盒 -> 宿主的请求编码；宿主侧解码在 nekro_agent/tools/rpc_codec.py，tests/test_rpc_codec.py 两边对测）---
# 请求体是 JSON。JSON 原生没有的类型写成 {"$rpc": "<标签>", "v": <载荷>}，宿主按标签用写死的构造函数还原，
# 不会按请求里的名字去找任何类。宿主对请求体、嵌套、元素数、容器数、字符串长度、整数位数、同哈希元素数都有上限；
# 同一组数字由宿主写进这里（_RPC_LIMITS），发出之前在沙盒里就按宿主的计数口径预检，超限抛 RpcArgumentError，
# 模型代码可以捕获后改小参数再调用，而不是发到宿主被拒、整段脚本退出。
# （用到的 _base64 / _datetime / _json / _os / _threading / _uuid 在文件顶部导入，带下划线是为了不被
# `from api_caller import *` 带进模型的命名空间；两个异常类故意不带下划线，模型代码可以直接 except）
_RPC_LIMITS = _json.loads('{RPC_LIMITS_JSON}')
# 整数位数的界只算一次：每个整数都现算一遍 10 的幂，十万个整数的参数就要多花不少时间
_RPC_INT_BOUND = 10 ** _RPC_LIMITS["max_int_digits"]
_RPC_TYPE_TAG = "$rpc"
_RPC_SUPPORTED_TYPES = (
    "None / bool / int / float / str / list / tuple / dict / set / frozenset / bytes（bytearray、memoryview 按 bytes 发）/ "
    "datetime / date / time / timedelta / uuid.UUID；os.PathLike（如 pathlib.Path）按 str 发"
)
# 宿主按方法签名补上：{方法名: [(参数名, 类型说明), ...]}，只用来把超限提示写对（这个参数接受什么、该怎么改小）
_RPC_METHOD_PARAMS = {}
# 同时在途的调用（含方法执行）不超过宿主每容器“执行中”的上限；读与解码那道更窄的闸（2 个）靠“忙”拒绝加自动退避重试，
# 所以并发调用慢方法的多线程代码不会被串行（只在把请求体送上去的那一小段可能要重试）
_RPC_IN_FLIGHT = _threading.BoundedSemaphore(_RPC_LIMITS["max_in_flight"])
# 宿主回“忙”时最多重试这么久（沙盒默认运行 120 秒）
_RPC_BUSY_RETRY_SECONDS = 30


class RpcArgumentError(TypeError, ValueError):
    """参数不能经 RPC 传给宿主：类型不支持，或超出宿主的上限。在沙盒里就抛出、没有发出去；可以捕获后改小参数再调用"""


class RpcRejectedError(RuntimeError):
    """宿主在执行之前拒绝了这次调用（同时在途的调用过多、宿主正忙，或请求不合法）。方法没有执行；可以捕获，稍后重试"""


class _RpcEncodeError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
        self.where = []


def _rpc_type_name(value):
    kind = type(value)
    return kind.__qualname__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__qualname__}"


class _RpcEncoder:
    """把参数编码成 JSON 可写的结构，计数口径与宿主 `_Decoder` 逐项一致：
    每个值记一个元素；每个 JSON list / dict（含标签对象、标签的载荷列表、键值对）记一个容器；子元素比父层深一层"""

    def __init__(self):
        self.items = 0
        self.containers = 0
        self.active = set()

    def container(self):
        self.containers += 1
        if self.containers > _RPC_LIMITS["max_containers"]:
            raise _RpcEncodeError(f"容器（list / dict / tuple / set 等）总数超过 {_RPC_LIMITS['max_containers']}")

    def tagged(self, tag, payload):
        self.container()
        return {_RPC_TYPE_TAG: tag, "v": payload}

    def string(self, text, what="字符串"):
        if len(text) > _RPC_LIMITS["max_str_chars"]:
            raise _RpcEncodeError(f"{what}超过 {_RPC_LIMITS['max_str_chars']} 字符（这个有 {len(text)} 字符）")
        return text

    def sequence(self, values, depth):
        encoded = []
        for index, item in enumerate(values):
            try:
                encoded.append(self.encode(item, depth + 1))
            except _RpcEncodeError as e:
                e.where.append(f"[{index}]")
                raise
        return encoded

    def spread(self, members):
        try:
            hashes = sorted(hash(member) for member in members)
        except TypeError:
            raise _RpcEncodeError("集合元素与非字符串的字典键必须是可哈希的值") from None
        run = 1
        for index in range(1, len(hashes)):
            run = run + 1 if hashes[index] == hashes[index - 1] else 1
            if run > _RPC_LIMITS["max_same_hash"]:
                raise _RpcEncodeError(f"有超过 {_RPC_LIMITS['max_same_hash']} 个元素的哈希值相同，宿主会拒绝")

    def encode(self, value, depth):
        if depth > _RPC_LIMITS["max_depth"]:
            raise _RpcEncodeError(f"嵌套超过 {_RPC_LIMITS['max_depth']} 层")
        self.items += 1
        if self.items > _RPC_LIMITS["max_items"]:
            raise _RpcEncodeError(f"元素总数超过 {_RPC_LIMITS['max_items']}")
        if value is None:
            return None
        if isinstance(value, bool):
            return bool(value)
        if isinstance(value, int):
            number = int(value)
            if abs(number) >= _RPC_INT_BOUND:
                raise _RpcEncodeError(f"整数超过 {_RPC_LIMITS['max_int_digits']} 位，请改传 str")
            return number
        if isinstance(value, float):
            return float(value)
        if isinstance(value, str):
            # str 的子类（比如 `class X(str, Enum)`）取底层的字符串，不走它自己的 __str__（那会得到 "X.A"）
            return self.string(str.__str__(value))
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            if len(raw) > _RPC_LIMITS["max_bytes_len"]:
                raise _RpcEncodeError(f"字节串超过 {_RPC_LIMITS['max_bytes_len']} 字节（这个有 {len(raw)} 字节）")
            return self.tagged("bytes", _base64.b64encode(raw).decode("ascii"))
        if isinstance(value, _datetime.datetime):
            return self.tagged("datetime", value.isoformat())
        if isinstance(value, _datetime.date):
            return self.tagged("date", value.isoformat())
        if isinstance(value, _datetime.time):
            return self.tagged("time", value.isoformat())
        if isinstance(value, _datetime.timedelta):
            encoded = self.tagged("timedelta", [value.days, value.seconds, value.microseconds])
            self.container()
            return encoded
        if isinstance(value, _uuid.UUID):
            return self.tagged("uuid", str(value))
        if isinstance(value, _os.PathLike):
            self.items -= 1  # 换成字符串再算一次，不重复计数
            return self.encode(_os.fspath(value), depth)
        if isinstance(value, (list, tuple, set, frozenset, dict)):
            if id(value) in self.active:
                raise _RpcEncodeError("含有循环引用")
            self.active.add(id(value))
            try:
                return self.container_value(value, depth)
            finally:
                self.active.discard(id(value))
        raise _RpcEncodeError(
            f"类型 {_rpc_type_name(value)} 不能经 RPC 传给宿主。能传的类型：{_RPC_SUPPORTED_TYPES}。"
            "请先转换成这些类型（numpy 标量用 .item()，Decimal 用 str() 或 float()，自定义对象转成 dict）",
        )

    def container_value(self, value, depth):
        if isinstance(value, dict):
            if all(isinstance(key, str) for key in value) and _RPC_TYPE_TAG not in value:
                self.container()
                encoded = {}
                for key, item in value.items():
                    try:
                        name = self.string(str.__str__(key), "字典键")
                        encoded[name] = self.encode(item, depth + 1)
                    except _RpcEncodeError as e:
                        e.where.append(f"[{key[:32]!r}]")
                        raise
                return encoded
            self.spread(value)
            self.container()  # 标签对象
            self.container()  # 键值对列表
            pairs = []
            for index, (key, item) in enumerate(value.items()):
                try:
                    self.container()
                    pairs.append([self.encode(key, depth + 1), self.encode(item, depth + 1)])
                except _RpcEncodeError as e:
                    e.where.append(f"[{index}]")
                    raise
            return {_RPC_TYPE_TAG: "dict", "v": pairs}
        if isinstance(value, list):
            self.container()
            return self.sequence(value, depth)
        if isinstance(value, (set, frozenset)):
            self.spread(value)
        tag = "tuple" if isinstance(value, tuple) else "frozenset" if isinstance(value, frozenset) else "set"
        self.container()  # 标签对象
        self.container()  # 载荷列表
        return {_RPC_TYPE_TAG: tag, "v": self.sequence(value, depth)}


def _rpc_hint(method_name, root):
    """按方法的实际参数给出改法：只接受字符串的参数让它截短或分段，路径参数才建议写文件"""
    params = _RPC_METHOD_PARAMS.get(method_name) or []
    name, annotation = None, ""
    if root.startswith("args["):
        index = int(root[5 : root.index("]")])
        if index < len(params):
            name, annotation = params[index]
    elif root.startswith("kwargs["):
        wanted = root[8 : root.index("]") - 1]
        name, annotation = next(((n, a) for n, a in params if n == wanted), (wanted, ""))
    if name is None:
        return "先把这个参数缩小，或分批多次调用"
    shown = f"参数 {name}" + (f"（{annotation}）" if annotation else "")
    lowered = name.lower()
    if "path" in lowered or "file" in lowered:
        return f"{shown} 接受路径：把内容写进 /app/shared 下的文件，传文件路径"
    if annotation in ("str", "Optional[str]", "str | None"):
        return f"{shown} 只接受字符串：先截短，或分段多次调用"
    return f"{shown}：先缩小数据，或分批多次调用"


def _rpc_dump(document):
    try:
        return _json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        # 字符串里有落单的代理字符（比如按 surrogateescape 解出来的文件名）：转义成 \uXXXX 照样能发
        return _json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _rpc_encode_request(method_name, args, kwargs):
    """把一次调用编码成请求体；不支持的类型、超出宿主任一上限的请求在沙盒里就抛 RpcArgumentError，不发出去"""
    encoder = _RpcEncoder()
    document = {"method": method_name, "args": [], "kwargs": {}}
    root = "args"
    try:
        for index, item in enumerate(args):
            root = f"args[{index}]"
            document["args"].append(encoder.encode(item, 1))
        for key, item in kwargs.items():
            root = f"kwargs[{key!r}]"
            if not key.isidentifier() or len(key) > _RPC_LIMITS["max_identifier_chars"]:
                raise _RpcEncodeError(f"关键字参数名必须是 {_RPC_LIMITS['max_identifier_chars']} 字符以内的标识符")
            document["kwargs"][key] = encoder.encode(item, 1)
    except _RpcEncodeError as e:
        where = (root + "".join(reversed(e.where)))[:200]
        raise RpcArgumentError(f"方法 `{method_name}` 的参数 {where}：{e.reason}。{_rpc_hint(method_name, root)}") from None
    data = _rpc_dump(document)
    if len(data) > _RPC_LIMITS["max_body_bytes"]:
        sizes = [(len(_rpc_dump(item)), f"args[{index}]") for index, item in enumerate(document["args"])]
        sizes += [(len(_rpc_dump(item)), f"kwargs[{key!r}]") for key, item in document["kwargs"].items()]
        largest = max(sizes)[1] if sizes else "args"
        raise RpcArgumentError(
            f"方法 `{method_name}` 的参数编码后有 {len(data)} 字节，超过宿主接受的上限 {_RPC_LIMITS['max_body_bytes']} 字节"
            f"（最大的是 {largest}）。{_rpc_hint(method_name, largest)}",
        )
    return data


# --- END RPC CODEC ---


def _rpc_post_with_busy_retry(data):
    """发一次调用；宿主回“忙”（并发闸门、解码预算、排队已满，都发生在读请求体之前，方法没有执行）时按建议的间隔退避重试

    间隔取宿主建议值与指数退避中较大的一个，加随机抖动，单次最多 2 秒，总共最多等 _RPC_BUSY_RETRY_SECONDS 秒；
    重试期间不占在途名额。最后一次仍是“忙”就把那次回应交回，由调用方抛 RpcRejectedError
    """
    deadline = _time.monotonic() + _RPC_BUSY_RETRY_SECONDS
    attempt = 0
    while True:
        with _RPC_IN_FLIGHT:
            response = _requests.post(
                f"{CHAT_API}/ext/rpc_exec?container_key={CONTAINER_KEY}&from_chat_key={FROM_CHAT_KEY}",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "X-RPC-Token": RPC_SECRET_KEY,
                },
                timeout=(RPC_CONNECT_TIMEOUT_SECONDS, RPC_READ_TIMEOUT_SECONDS),
            )
        if response.status_code != 200 or response.headers.get("Rpc-Busy", "").lower() != "true":
            return response
        try:
            hint = int(response.headers.get("Rpc-Retry-After-Ms", "50")) / 1000
        except ValueError:
            hint = 0.05
        wait = min(max(hint, 0.05 * (2**attempt)), 2.0)
        wait += _random.uniform(0, wait / 2)
        if _time.monotonic() + wait > deadline:
            return response
        _time.sleep(wait)
        attempt += 1


def __extension_method_proxy(method: Callable):
    """扩展方法代理执行器"""

    def acutely_call_method(*args: Tuple[Any], **kwargs: Dict[str, Any]):
        """Agent 执行沙盒扩展方法时实际调用的方法"""

        data: bytes = _rpc_encode_request(method.__name__, args, kwargs)
        try:
            response = _rpc_post_with_busy_retry(data)
        except (_requests.exceptions.ConnectionError, _requests.exceptions.Timeout) as e:
            print(
                f"{RPC_UNREACHABLE_TAG} Cannot reach the NekroAgent callback endpoint {CHAT_API} "
                f"({type(e).__name__}: {e}). "
                "沙盒 -> NA 的回调链路不通，本次执行无法调用任何插件方法。"
                "请检查宿主的沙盒网络隔离规则（是否被同宿主的其他部署/脚本按同一标记改写）"
                "与 SANDBOX_CHAT_API_URL 是否指向宿主可达的地址。",
                # 非 tty 下 stdout 是块缓冲的；这句话是整条故障链上唯一的线索，
                # 不能押在「解释器退出时会帮我们 flush」上
                flush=True,
            )
            raise
        if response.status_code == 200:
            if response.headers.get("Rpc-Rejected", "").lower() == "true":
                # 宿主在执行之前就拒绝了（请求不合法，或“忙”且自动重试也没等到）：方法没跑，交给模型代码决定是否重试
                raise RpcRejectedError(f"方法 `{method.__name__}` 的调用被宿主拒绝，方法没有执行：{response.text}")
            if response.headers.get("Run-Error") and response.headers["Run-Error"].lower() == "true":
                print(
                    f"The method `{method.__name__}` returned an error:\n{response.text}",
                )
                exit(1)
            ret_data = _safe_loads(response.content)
            if response.headers.get("Method-Type") == "agent":
                print(
                    f"The agent method `{method.__name__}` returned:\n{ret_data}\n[result end]\nPlease continue to generate an appropriate response based on the above information.",
                )
                exit(8)
            if response.headers.get("Method-Type") == "multimodal_agent":
                print(
                    f"The multimodal agent method `{method.__name__}` returned:\n{ret_data}\n[result end]",
                )
                exit(11)
            return ret_data
        if response.status_code in (401, 403):
            # 回调到达了 NA 但被身份/白名单挡下。与「网络不通」是完全不同的故障，
            # 必须分得开：前者查 credential_guard 的许可，后者查防火墙
            raise Exception(
                f"Plugin RPC method `{method.__name__}` was rejected by NekroAgent "
                f"({response.status_code}): the call grant for this container is missing, expired, "
                "or does not cover this method.",
            )
        raise Exception(f"Plugin RPC method `{method.__name__}` call failed: {response.status_code}")

    return acutely_call_method


def dynamic_importer(
    package_spec: str,
    import_name: Optional[str] = None,
    mirror: Optional[str] = "https://mirrors.aliyun.com/pypi/simple",
    trusted_host: bool = True,
    timeout: int = 300,
    repo_dir: Optional[str] = "/app/packages",
) -> Any:
    """动态安装并导入Python包

    Args:
        package_spec: 包名称和版本规范 (如 "requests" 或 "numpy==1.21.0")
        import_name: 导入名称（如果与包名不同）
        mirror: PyPI镜像源URL
        trusted_host: 是否信任镜像源主机
        timeout: 安装超时时间（秒）
        repo_dir: 持久化存储目录 (默认使用系统路径)

    Returns:
        导入的模块对象

    Raises:
        RuntimeError: 安装失败时抛出
        ImportError: 导入失败时抛出
    """
    # 提取基础包名和版本约束
    package_name = package_spec.strip()
    version_spec = ""
    for sep in ["==", ">=", "<=", "!=", ">", "<", "~="]:
        if sep in package_name:
            package_name, version_spec = package_name.split(sep, 1)
            package_name = package_name.strip()
            version_spec = f"{sep}{version_spec.strip()}"
            break

    # 配置持久化仓库路径
    if repo_dir:
        repo_path = Path(repo_dir)
        repo_path.mkdir(parents=True, exist_ok=True)
        repo_site_packages = repo_path / "site-packages"
        repo_site_packages.mkdir(parents=True, exist_ok=True)

        # 添加路径到sys.path
        for path in [str(repo_path), str(repo_site_packages)]:
            if path not in sys.path:
                sys.path.insert(0, path)

    # 检查是否已安装符合条件的版本
    need_install = True
    try:
        # 处理distributions函数的path参数
        if repo_dir:
            path_list = [repo_dir]
            dists = distributions(path=path_list)
        else:
            dists = distributions()

        for dist in dists:
            metadata = dist.metadata
            dist_name = getattr(metadata, "Name", None) or dist.name
            if dist_name.lower() == package_name.lower() and (
                not version_spec or parse(dist.version) in SpecifierSet(version_spec)
            ):
                need_install = False
                break
    except PackageNotFoundError:
        pass

    # 构建安装命令
    if need_install:
        install_cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            "--no-input",
            "--no-warn-script-location",
        ]

        # 添加持久化目录参数
        if repo_dir:
            install_cmd += ["--target", repo_dir]

        # 添加镜像源配置
        if mirror:
            install_cmd += ["--index-url", mirror]
            if trusted_host:
                host = urllib.parse.urlparse(mirror).hostname
                if host:
                    install_cmd += ["--trusted-host", host]

        install_cmd.append(package_spec)

        # 执行安装
        try:
            subprocess.run(install_cmd, capture_output=True, text=True, check=True, timeout=timeout)
        except subprocess.CalledProcessError as e:
            error_msg = _parse_pip_error(e.stderr or e.stdout)
            print(f"安装 {package_spec} 失败: {error_msg}")
            exit(1)
        except subprocess.TimeoutExpired:
            print(f"安装 {package_spec} 超时（{timeout}秒），请检查网络连接")
            exit(1)

    # 确定导入模块名称
    module_name = import_name if import_name is not None else package_name

    # 动态导入模块
    try:
        module = importlib.import_module(module_name)
        module = importlib.reload(module)  # 确保加载最新版本
    except ImportError:
        # 尝试刷新导入路径
        if repo_dir:
            import site

            site.addsitedir(repo_dir)
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                print(f"安装成功但无法导入 {module_name}，请确认模块名正确")
                exit(1)
        else:
            print(f"安装成功但无法导入 {module_name}，请确认模块名正确")
            exit(1)

    return module


def _parse_pip_error(output: str) -> str:
    """解析pip错误信息"""
    patterns = {
        "No matching distribution": "包不存在或版本不可用",
        "Could not find a version": "找不到指定版本",
        "SSL: CERTIFICATE_VERIFY_FAILED": "SSL验证失败，尝试使用 trusted_host=True",
        "403 Client Error": "访问被拒绝（可能镜像源需要认证）",
        "404 Client Error": "资源不存在",
        "Connection refused": "连接被拒绝，检查镜像源地址",
        "Network is unreachable": "网络不可达",
    }
    for pattern, message in patterns.items():
        if pattern in output:
            return message
    return f"未知错误：{output[:200]}..." if len(output) > 200 else output
