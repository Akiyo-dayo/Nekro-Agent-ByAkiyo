"""docker-guard 判据绕过的回归用例（独立复核发现，PR #8 追加修复）。

## 这份文件和 `test_docker_guard_policy.py` 的分工

policy 的表驱动用例直接喂 `evaluate()` 一个**已经解析好的 dict**。本轮复核发现的几类绕过恰恰发生在
「dict 之外」——判据看到的结构与 Docker 最终解码的结构不是同一个：

1. Docker 用 Go `encoding/json`，**键名大小写不敏感**（含 U+017F `ſ`、U+212A `K` 这类折叠字符），
   重复键后者覆盖前者（嵌套对象合并）；Python 的 `dict.get("HostConfig")` 只认精确键。
2. Docker Engine ≤26 的 `ContainerConfigWrapper` 内嵌 HostConfig：请求里没有 `HostConfig` 键时，
   顶层的 `Binds` / `Privileged` 照样生效。
3. API <1.24 的 start 会把请求体当 HostConfig 解码。

dict 级用例天然看不到这些（喂进去的就是 dict）。所以本文件走**最远的那一层**：`create_app()` 起真正的
guard，用 aiohttp **真实发请求**（原始字节、原始路径），上游换成桩，然后断言两件事——

- 被拒绝的请求，**字节没有到达上游**（桩上看不到任何会改变宿主状态的请求）；
- 放行的请求，**到达上游的字节就是判过的那一份**（不是客户端发来的原始字节）。

每个绕过变体一行，表驱动：新增变体只需要加一行。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Union

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver
from aiohttp.test_utils import TestServer
from yarl import URL

from nekro_agent.core.logger import logger
from nekro_agent.services.docker_guard import proxy as proxy_mod
from nekro_agent.services.docker_guard.policy import MAX_BINDS, PolicyContext
from tests.docker_guard_fakefs import FakeBindFs

ROOT = "/srv/nekro_agent"
OWNED = "owned1"
VICTIM = "victim"
DEEP = "/".join(f"d{i}" for i in range(70))  # 70 层目录：单条 bind 只花 ~71 的预算，32 条一起就会超

_BASE_ENTRIES: Dict[str, Any] = {
    ROOT: "dir",
    f"{ROOT}/sandboxes/x": "dir",
    f"{ROOT}/uploads": "dir",
    f"{ROOT}/{DEEP}": "dir",
}

Raw = Union[str, bytes, Dict[str, Any], None]


# ---------------------------------------------------------------------------
# 桩上游 + 真实 guard
# ---------------------------------------------------------------------------


@dataclass
class _Seen:
    method: str
    path: str  # 含查询串，逐字
    body: bytes


class _StubDocker:
    """桩 Docker：记录收到的每个请求；inspect 按预置内容回答，其余一律 2xx。"""

    def __init__(self) -> None:
        self.requests: List[_Seen] = []
        self.inspect: Dict[str, Dict[str, Any]] = {}
        self.app = web.Application()
        self.app.router.add_route("*", "/{tail:.*}", self._handle)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(_Seen(request.method, request.raw_path, body))
        parts = request.path.strip("/").split("/")
        if request.method == "GET" and len(parts) == 3 and parts[0] == "containers" and parts[2] == "json":
            payload = self.inspect.get(parts[1])
            if payload is None:
                return web.json_response({"message": f"No such container: {parts[1]}"}, status=404)
            return web.json_response(payload)
        if request.method == "POST" and request.path == "/containers/create":
            return web.json_response({"Id": "a" * 64, "Warnings": []}, status=201)
        return web.json_response({}, status=200)

    def mutating(self) -> List[_Seen]:
        """除了只读探测（归属门 / 复核自己会发的按 ID inspect，以及 `_ping`）之外到达上游的全部请求。"""
        return [
            r
            for r in self.requests
            if not (
                r.method == "GET"
                and (r.path == "/_ping" or (r.path.startswith("/containers/") and r.path.split("?")[0].endswith("/json")))
            )
        ]


class _FixedResolver(AbstractResolver):
    """把 guard 往 `http://docker/...` 发的请求落到桩上游的本地端口（`_forward` 本身一个字不改）。"""

    def __init__(self, port: int) -> None:
        self._port = port

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> List[Dict[str, Any]]:  # noqa: ARG002
        return [{"hostname": host, "host": "127.0.0.1", "port": self._port, "family": family, "proto": 0, "flags": 0}]

    async def close(self) -> None:
        return None


@dataclass
class Guard:
    port: int
    client: aiohttp.ClientSession
    stub: _StubDocker
    proxy: proxy_mod.DockerGuardProxy
    extra: Dict[str, Any] = field(default_factory=dict)

    async def send(
        self,
        method: str,
        raw_path: str,
        body: Raw = None,
        *,
        content_type: str = "application/json",
    ) -> Tuple[int, Dict[str, Any]]:
        data: Optional[bytes]
        if body is None:
            data = None
        elif isinstance(body, dict):
            data = json.dumps(body).encode()
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = body
        url = URL(f"http://127.0.0.1:{self.port}{raw_path}", encoded=True)
        async with self.client.request(method, url, data=data, headers={"Content-Type": content_type}) as resp:
            text = await resp.text()
            try:
                parsed = json.loads(text) if text else {}
            except ValueError:
                parsed = {"_text": text}
            return resp.status, parsed if isinstance(parsed, dict) else {"_value": parsed}


def _owned_inspect(*binds: str) -> Dict[str, Any]:
    return {
        "Config": {"Labels": {"nekro.owner.scope": "t"}},
        "HostConfig": {"Binds": [f"{b}:/m" for b in binds]},
        "Mounts": [{"Type": "bind", "Source": b, "Destination": "/m"} for b in binds],
    }


@contextlib.asynccontextmanager
async def _running_guard(monkeypatch: pytest.MonkeyPatch, fs: FakeBindFs) -> AsyncIterator[Guard]:
    monkeypatch.setattr(proxy_mod, "build_context", lambda: PolicyContext(allowed_bind_roots=(ROOT,), fs=fs))
    stub = _StubDocker()
    stub.inspect[OWNED] = _owned_inspect(f"{ROOT}/sandboxes/x")
    stub.inspect[VICTIM] = {"Config": {"Labels": {"maintainer": "postgres"}}, "HostConfig": {}, "Mounts": []}
    stub_server = TestServer(stub.app)
    await stub_server.start_server()

    app, proxy = await proxy_mod.create_app()
    proxy._session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=_FixedResolver(stub_server.port), use_dns_cache=False),
        timeout=aiohttp.ClientTimeout(total=10),
    )
    guard_server = TestServer(app)
    await guard_server.start_server()
    client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    try:
        yield Guard(port=guard_server.port, client=client, stub=stub, proxy=proxy)
    finally:
        await client.close()
        await guard_server.close()
        await stub_server.close()


@pytest.fixture
async def guard(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Guard]:
    async with _running_guard(monkeypatch, FakeBindFs(_BASE_ENTRIES)) as g:
        yield g


def _create(**over: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"Image": "a", "Cmd": ["true"], "HostConfig": {"Memory": 1}}
    body.update(over)
    return body


def _hc(**hc: Any) -> Dict[str, Any]:
    return {"Image": "a", "HostConfig": hc}


# ---------------------------------------------------------------------------
# 1. create：键名大小写 / 重复键 / 扁平写法（P0 #1、#2）
# ---------------------------------------------------------------------------

# (id, 原始请求体, 期望规则)。请求体尽量写成原始 JSON 文本，保证 dict 序列化不会把变体键悄悄吃掉。
CREATE_DENY: List[Tuple[str, Raw, str]] = [
    # ---- 顶层 HostConfig 键的大小写变体 ----
    ("hostconfig_lower", '{"Image":"a","hostconfig":{"Binds":["/:/host"],"Privileged":true}}', "create.key_case_variant"),
    ("hostconfig_upper", '{"Image":"a","HOSTCONFIG":{"Privileged":true}}', "create.key_case_variant"),
    ("hostconfig_camel", '{"Image":"a","hostConfig":{"Privileged":true}}', "create.key_case_variant"),
    ("hostconfig_long_s", '{"Image":"a","Ho\\u017ftConfig":{"Privileged":true}}', "create.key_case_variant"),
    # HostConfig 与 hostConfig 双份并存：Go 解码时重复键合并，后者的 Privileged 叠在前者之上
    (
        "hostconfig_twice",
        '{"Image":"a","HostConfig":{"Memory":1},"hostConfig":{"Privileged":true,"Binds":["/:/host"]}}',
        "create.key_case_variant",
    ),
    ("image_case", '{"image":"a"}', "create.key_case_variant"),
    ("labels_case", '{"Image":"a","labels":{"nekro.netguard":"1"}}', "create.key_case_variant"),
    # ---- HostConfig 内层键的大小写变体 ----
    ("binds_lower", '{"Image":"a","HostConfig":{"binds":["/:/host"]}}', "create.key_case_variant"),
    ("binds_upper", '{"Image":"a","HostConfig":{"BINDS":["/:/host"]}}', "create.key_case_variant"),
    ("binds_long_s", '{"Image":"a","HostConfig":{"Bind\\u017f":["/:/host"]}}', "create.key_case_variant"),
    ("privileged_lower", '{"Image":"a","HostConfig":{"privileged":true}}', "create.key_case_variant"),
    ("mounts_lower", '{"Image":"a","HostConfig":{"mounts":[{"Type":"bind","Source":"/","Target":"/h"}]}}', "create.key_case_variant"),
    ("mounts_long_s", '{"Image":"a","HostConfig":{"Mount\\u017f":[{"Type":"bind","Source":"/","Target":"/h"}]}}', "create.key_case_variant"),
    ("volumesfrom_lower", '{"Image":"a","HostConfig":{"volumesfrom":["sock_holder"]}}', "create.key_case_variant"),
    ("volumesfrom_long_s", '{"Image":"a","HostConfig":{"Volume\\u017fFrom":["sock_holder"]}}', "create.key_case_variant"),
    ("pidmode_lower", '{"Image":"a","HostConfig":{"pidmode":"host"}}', "create.key_case_variant"),
    ("networkmode_lower", '{"Image":"a","HostConfig":{"networkmode":"host"}}', "create.key_case_variant"),
    ("capadd_lower", '{"Image":"a","HostConfig":{"capadd":["SYS_ADMIN"]}}', "create.key_case_variant"),
    ("devices_lower", '{"Image":"a","HostConfig":{"devices":[{"PathOnHost":"/dev/sda"}]}}', "create.key_case_variant"),
    ("securityopt_long_s", '{"Image":"a","HostConfig":{"\\u017fecurityOpt":["apparmor=unconfined"]}}', "create.key_case_variant"),
    ("kernel_kelvin", '{"Image":"a","HostConfig":{"\\u212aernelMemory":1}}', "create.key_case_variant"),
    ("restartpolicy_lower", f'{{"Image":"a","HostConfig":{{"Binds":["{ROOT}/x:/m"],"restartpolicy":{{"Name":"always"}}}}}}', "create.key_case_variant"),
    # RestartPolicy 内层键：`name` 与 `Name` 在 Docker 眼里是同一个
    ("restartpolicy_name_lower", f'{{"Image":"a","HostConfig":{{"Binds":["{ROOT}/x:/m"],"RestartPolicy":{{"name":"always"}}}}}}', "create.key_case_variant"),
    # ---- 未知键：不在白名单里就不放行（fail-closed，也挡将来 Engine 新增的字段）----
    ("unknown_top", '{"Image":"a","Whatever":1}', "create.key_unknown"),
    ("unknown_hostconfig", '{"Image":"a","HostConfig":{"Whatever":1}}', "create.key_unknown"),
    # ---- Engine ≤26：没有 HostConfig 键时，顶层的 HostConfig 字段生效 ----
    ("flat_binds_privileged", '{"Image":"a","Binds":["/:/host"],"Privileged":true}', "create.hostconfig_flat"),
    ("flat_binds_only", '{"Image":"a","Binds":["/:/host"]}', "create.hostconfig_flat"),
    ("flat_privileged_only", '{"Image":"a","Privileged":true}', "create.hostconfig_flat"),
    ("flat_binds_lower", '{"Image":"a","binds":["/:/host"]}', "create.hostconfig_flat"),
    ("flat_networkmode_host", '{"Image":"a","NetworkMode":"host"}', "create.hostconfig_flat"),
    ("flat_pidmode", '{"Image":"a","PidMode":"host"}', "create.hostconfig_flat"),
    ("flat_with_inner_hostconfig", '{"Image":"a","HostConfig":{"Memory":1},"Privileged":true}', "create.hostconfig_flat"),
    # NA 的沙盒配置曾经就带着一个顶层 AutoRemove（写错了位置）：同类形状，同样拒绝
    ("flat_autoremove", '{"Image":"a","AutoRemove":true,"HostConfig":{"Memory":1}}', "create.hostconfig_flat"),
    ("flat_volumesfrom", '{"Image":"a","VolumesFrom":["sock_holder"]}', "create.hostconfig_flat"),
    ("flat_mounts", '{"Image":"a","Mounts":[{"Type":"bind","Source":"/","Target":"/h"}]}', "create.hostconfig_flat"),
    # ---- 精确键的老攻击仍然被拒（对照：判据没有被新逻辑挤掉）----
    ("exact_privileged", _hc(Privileged=True), "create.privileged"),
    ("exact_binds_root", _hc(Binds=["/:/host"]), "create.binds.outside_data_dir"),
    # ---- 非法 JSON 常量 / 过深嵌套：Go 不接受 NaN；嵌套爆栈不能变成 500 或放行 ----
    ("nan_constant", '{"Image":"a","HostConfig":{"Memory":NaN}}', "body.invalid"),
    # `1e999` 能被 Python 解析成 inf，却没法再序列化成 Go 认的 JSON：必须是明确的拒绝，不是 500
    ("float_overflow", '{"Image":"a","HostConfig":{"Memory":1e999}}', "body.invalid"),
    # ---- 复验追加（P0）：MaskedPaths / ReadonlyPaths 是 `systempaths=unconfined` 的 API 形式，只允许 null ----
    ("masked_paths_empty", _hc(MaskedPaths=[]), "create.maskedpaths"),
    ("masked_paths_custom", _hc(MaskedPaths=["/proc/nothing-useful"]), "create.maskedpaths"),
    ("readonly_paths_empty", _hc(ReadonlyPaths=[]), "create.readonlypaths"),
    ("readonly_paths_custom", _hc(ReadonlyPaths=["/proc/bus"]), "create.readonlypaths"),
    ("masked_paths_lower", '{"Image":"a","HostConfig":{"maskedpaths":[]}}', "create.key_case_variant"),
    ("securityopt_label_disable", _hc(SecurityOpt=["label=disable"]), "create.securityopt.forbidden"),
    ("securityopt_label_colon_disable", _hc(SecurityOpt=["label:disable"]), "create.securityopt.forbidden"),
    ("securityopt_label_disable_mixed_case", _hc(SecurityOpt=["no-new-privileges", "LABEL=Disable"]), "create.securityopt.forbidden"),
    ("securityopt_systempaths", _hc(SecurityOpt=["systempaths=unconfined"]), "create.securityopt.forbidden"),
    # ---- 复验追加：白名单内、NA 从不使用、却有提权 / 逃逸能力的字段，只允许空值 ----
    ("device_requests_gpu", _hc(DeviceRequests=[{"Driver": "nvidia", "Count": -1, "Capabilities": [["gpu"]]}]), "create.devicerequests"),
    ("annotations", _hc(Annotations={"io.kubernetes.cri-o.userns-mode": "auto"}), "create.annotations"),
    ("links", _hc(Links=["victim:alias"]), "create.links"),
    ("group_add_root", _hc(GroupAdd=["0"]), "create.groupadd"),
    ("volume_driver_plugin", _hc(VolumeDriver="some-third-party-plugin"), "create.volumedriver"),
    ("cgroup_join_container", _hc(Cgroup="container:victim"), "create.cgroup"),
    ("container_id_file", _hc(ContainerIDFile="/etc/cron.d/x"), "create.containeridfile"),
    ("isolation", _hc(Isolation="hyperv"), "create.isolation"),
    ("oom_score_adj_negative", _hc(OomScoreAdj=-1000), "create.oomscoreadj"),
    ("log_driver_syslog", _hc(LogConfig={"Type": "syslog", "Config": {"syslog-address": "unix:///var/run/docker.sock"}}), "create.logconfig"),
    ("log_config_options", _hc(LogConfig={"Type": "json-file", "Config": {"max-size": "1m"}}), "create.logconfig"),
    ("networking_config_host", {"Image": "a", "NetworkingConfig": {"EndpointsConfig": {"host": {}}}}, "create.networkingconfig"),
    ("networking_config_variant", '{"Image":"a","networkingconfig":{}}', "create.key_case_variant"),
    ("too_deep", '{"Image":"a","Cmd":' + "[" * 20000 + "]" * 20000 + "}", "body.invalid"),
    # 解析能过（< 递归上限）、深拷贝会爆栈：必须是明确的拒绝，不是 500，更不是放行
    ("deep_enough_to_break_deepcopy", '{"Image":"a","Cmd":' + "[" * 700 + "]" * 700 + "}", "body.too_deep"),
]


@pytest.mark.parametrize(("case_id", "body", "rule"), CREATE_DENY, ids=[c[0] for c in CREATE_DENY])
async def test_create_bypass_variants_never_reach_the_upstream(guard: Guard, case_id: str, body: Raw, rule: str):
    status, payload = await guard.send("POST", "/containers/create", body)
    assert guard.stub.mutating() == [], f"{case_id}: 被拒绝的 create 到达了上游: {guard.stub.mutating()}"
    assert status == 403, f"{case_id}: 期望 403，实际 {status} {payload}"
    assert payload.get("rule") == rule, f"{case_id}: {payload}"


async def test_duplicate_hostconfig_keys_are_collapsed_into_the_judged_structure(guard: Guard):
    """重复键：Python 取后者，Go 把两份 HostConfig 合并——两边结构不同。

    放行（后者本身无害），但**转发的是判过的那一份**：前一份里的 `Binds` 不会借着合并溜进 Docker。
    """
    raw = '{"Image":"a","HostConfig":{"Binds":["/:/host"]},"HostConfig":{"Memory":1}}'
    status, _ = await guard.send("POST", "/containers/create", raw)
    assert status == 201
    (sent,) = guard.stub.mutating()
    assert json.loads(sent.body) == {"Image": "a", "HostConfig": {"Memory": 1}}
    assert b"/:/host" not in sent.body


async def test_labels_and_env_maps_are_not_touched_by_key_checks(guard: Guard):
    """`Labels` 等 map 的键是数据，不参与键名校验，也不被改写：只大小写不同的标签键必须原样到达上游。"""
    body = _create(Labels={"Nekro.X": "1", "nekro.x": "2"}, Env=["A=1", "a=2"])
    status, _ = await guard.send("POST", "/containers/create", body)
    assert status == 201
    (sent,) = guard.stub.mutating()
    assert json.loads(sent.body)["Labels"] == {"Nekro.X": "1", "nekro.x": "2"}
    assert json.loads(sent.body)["Env"] == ["A=1", "a=2"]


# 合法形状必须仍然放行，且到达上游的 JSON 与判过的结构等价（NA 真实用到的四类 + 边界）
CREATE_ALLOW: List[Tuple[str, Dict[str, Any]]] = [
    (
        "sandbox",
        {
            "Image": "s",
            "Labels": {"nekro.sandbox": "1"},
            "Cmd": ["bash", "-c", "true"],
            "User": "nobody",
            "HostConfig": {
                "Binds": [f"{ROOT}/sandboxes/x:/app/shared:rw", f"{ROOT}/uploads:/app/uploads:ro"],
                "Memory": 512 * 1024 * 1024,
                "NanoCpus": 1_000_000_000,
                "SecurityOpt": ["no-new-privileges"],
                "NetworkMode": "nekro_sandbox_net",
                "ExtraHosts": ["host.docker.internal:172.20.0.1"],
            },
        },
    ),
    (
        "napcat_restart_no",
        {
            "Image": "n",
            "Labels": {"nekro.napcat.instance": "ob1"},
            "ExposedPorts": {"6099/tcp": {}},
            "HostConfig": {
                "Binds": [f"{ROOT}:/app/nekro_agent_data"],
                "PortBindings": {"6099/tcp": [{"HostIp": "127.0.0.1", "HostPort": "36551"}]},
                "RestartPolicy": {"Name": "no"},
            },
        },
    ),
    ("restart_name_empty_with_binds", _hc(Binds=[f"{ROOT}/x:/m"], RestartPolicy={"Name": "", "MaximumRetryCount": 0})),
    (
        "empty_restricted_fields_are_fine",
        _hc(
            MaskedPaths=None,
            ReadonlyPaths=None,
            DeviceRequests=[],
            Annotations={},
            Links=None,
            GroupAdd=[],
            OomScoreAdj=0,
            VolumeDriver="",
            LogConfig={"Type": "json-file"},
            SecurityOpt=["no-new-privileges"],
        ),
    ),
    ("empty_networking_config_is_fine", {"Image": "a", "NetworkingConfig": {"EndpointsConfig": {}}}),
    ("always_without_binds_is_fine", _hc(RestartPolicy={"Name": "always"})),
    ("unless_stopped_without_binds_is_fine", _hc(RestartPolicy={"Name": "unless-stopped"})),
    (
        "netguard",
        {
            "Image": "i",
            "Cmd": ["iptables", "-L"],
            "Entrypoint": [],
            "Labels": {"nekro.netguard": "1"},
            "HostConfig": {"NetworkMode": "host", "CapAdd": ["NET_ADMIN", "NET_RAW"], "AutoRemove": False},
        },
    ),
]


@pytest.mark.parametrize(("case_id", "body"), CREATE_ALLOW, ids=[c[0] for c in CREATE_ALLOW])
async def test_legit_create_shapes_still_pass_and_forward_the_judged_structure(guard: Guard, case_id: str, body: Dict[str, Any]):
    status, payload = await guard.send("POST", "/containers/create", body)
    assert status == 201, f"{case_id}: {payload}"
    (sent,) = guard.stub.mutating()
    assert json.loads(sent.body) == body, case_id


# ---------------------------------------------------------------------------
# 2. 带挂载的容器不许把重启交给守护进程（P1 #5）
# ---------------------------------------------------------------------------

RESTART_POLICY_DENY = [
    ("always", "always"),
    ("unless_stopped", "unless-stopped"),
    ("on_failure", "on-failure"),
    ("unknown_name", "sometimes"),
    ("upper_no", "NO"),  # 不是 Docker 认的 `no`：宁可拒绝
]


@pytest.mark.parametrize(("case_id", "name"), RESTART_POLICY_DENY, ids=[c[0] for c in RESTART_POLICY_DENY])
async def test_restart_policy_with_binds_is_denied(guard: Guard, case_id: str, name: str):
    body = _hc(Binds=[f"{ROOT}/x:/m"], RestartPolicy={"Name": name})
    status, payload = await guard.send("POST", "/containers/create", body)
    assert guard.stub.mutating() == [], case_id
    assert (status, payload.get("rule")) == (403, "create.restart_policy.with_binds"), (case_id, payload)


async def test_only_binds_matter_for_restart_policy_even_the_readonly_localtime_one(guard: Guard):
    """任何非空 Binds 都算（包括只读的 /etc/localtime）：规则简单，不留「只挂了一个无害文件」的灰区。"""
    body = _hc(Binds=["/etc/localtime:/etc/localtime:ro"], RestartPolicy={"Name": "always"})
    status, payload = await guard.send("POST", "/containers/create", body)
    assert (status, payload.get("rule")) == (403, "create.restart_policy.with_binds")


# ---------------------------------------------------------------------------
# 3. start / restart：不带体，且 API 版本不得低于 1.24（P0 #3）
# ---------------------------------------------------------------------------

START_BODY_DENY = [
    ("start_with_hostconfig_body", "POST", f"/containers/{OWNED}/start", '{"Binds":["/:/host"],"Privileged":true}'),
    ("start_with_empty_object", "POST", f"/containers/{OWNED}/start", "{}"),
    ("start_with_garbage", "POST", f"/containers/{OWNED}/start", "x"),
    ("restart_with_body", "POST", f"/containers/{OWNED}/restart?t=5", '{"Binds":["/:/host"]}'),
    ("start_v1_24_with_body", "POST", f"/v1.24/containers/{OWNED}/start", '{"Privileged":true}'),
]


@pytest.mark.parametrize(("case_id", "method", "path", "body"), START_BODY_DENY, ids=[c[0] for c in START_BODY_DENY])
async def test_start_and_restart_reject_any_request_body(guard: Guard, case_id: str, method: str, path: str, body: str):
    status, payload = await guard.send(method, path, body)
    assert guard.stub.mutating() == [], case_id
    assert (status, payload.get("rule")) == (403, "containers.body_forbidden"), (case_id, payload)


OLD_API_DENY = [
    ("start_v1_23", "POST", f"/v1.23/containers/{OWNED}/start", None),
    ("start_v1_5", "POST", f"/v1.5/containers/{OWNED}/start", None),
    ("start_v1_0", "POST", f"/v1.0/containers/{OWNED}/start", None),
    ("start_v0_99", "POST", f"/v0.99/containers/{OWNED}/start", None),
    ("start_leading_zero", "POST", f"/v1.0023/containers/{OWNED}/start", None),
    ("start_encoded_prefix", "POST", f"/%76%31.20/containers/{OWNED}/start", None),
    ("create_v1_23", "POST", "/v1.23/containers/create", _create()),
    ("version_v1_23", "GET", "/v1.23/version", None),
]


@pytest.mark.parametrize(("case_id", "method", "path", "body"), OLD_API_DENY, ids=[c[0] for c in OLD_API_DENY])
async def test_api_versions_below_1_24_are_rejected(guard: Guard, case_id: str, method: str, path: str, body: Raw):
    status, payload = await guard.send(method, path, body)
    assert guard.stub.requests == [], f"{case_id}: 低版本前缀的请求到达了上游（含 inspect）"
    assert (status, payload.get("rule")) == (403, "endpoint.api_version_too_old"), (case_id, payload)


async def test_start_without_body_and_current_api_versions_still_work(guard: Guard):
    for path in (f"/containers/{OWNED}/start", f"/v1.24/containers/{OWNED}/start", f"/v1.51/containers/{OWNED}/restart?t=3"):
        status, payload = await guard.send("POST", path, None)
        assert status == 200, (path, payload)
    forwarded = guard.stub.mutating()
    assert [r.method for r in forwarded] == ["POST"] * 3
    assert all(r.body == b"" for r in forwarded), "start / restart 转发时不得带体"


# ---------------------------------------------------------------------------
# 4. exec / exec start / network connect：同样的大小写差异（P1 #4）
# ---------------------------------------------------------------------------

EXEC_DENY: List[Tuple[str, Raw, str]] = [
    ("privileged_lower", '{"Cmd":["sh"],"privileged":true}', "exec.key_case_variant"),
    ("privileged_upper", '{"Cmd":["sh"],"PRIVILEGED":true}', "exec.key_case_variant"),
    ("privileged_exact", '{"Cmd":["sh"],"Privileged":true}', "exec.privileged"),
    ("cmd_case_only_privileged_exact", '{"cmd":["sh"],"Privileged":true}', "exec.key_case_variant"),
    ("unknown_key", '{"Cmd":["sh"],"Whatever":1}', "exec.key_unknown"),
    ("body_not_object", "[1,2]", "exec.body.missing"),
    ("detachkeys_both_spellings", '{"Cmd":["sh"],"DetachKeys":"a","detachKeys":"b"}', "exec.key_case_variant"),
    ("detachkeys_other_case", '{"Cmd":["sh"],"DETACHKEYS":"a"}', "exec.key_case_variant"),
    ("container_other_case", f'{{"Cmd":["sh"],"container":"{VICTIM}"}}', "exec.key_unknown"),
    ("container_not_a_string", '{"Cmd":["sh"],"Container":{"x":1}}', "exec.key_unknown"),
]


@pytest.mark.parametrize(("case_id", "body", "rule"), EXEC_DENY, ids=[c[0] for c in EXEC_DENY])
async def test_exec_create_bypass_variants_never_reach_the_upstream(guard: Guard, case_id: str, body: Raw, rule: str):
    status, payload = await guard.send("POST", f"/containers/{OWNED}/exec", body)
    assert guard.stub.mutating() == [], case_id
    assert (status, payload.get("rule")) == (403, rule), (case_id, payload)


async def test_exec_create_forwards_the_judged_structure_not_the_raw_bytes(guard: Guard):
    # 重复键：Python 取最后一个（false），Go 同样取最后一个；转发的是折叠后的那一份
    raw = '{"Cmd":["id"],"Privileged":true,"Privileged":false,"Tty":false}'
    status, _ = await guard.send("POST", f"/containers/{OWNED}/exec", raw)
    assert status == 200
    (sent,) = guard.stub.mutating()
    assert json.loads(sent.body) == {"Cmd": ["id"], "Privileged": False, "Tty": False}
    assert sent.body.count(b"Privileged") == 1


EXEC_START_DENY: List[Tuple[str, Raw, str]] = [
    ("detach_true_then_lower_false", '{"Detach":true,"detach":false}', "exec_start.key_case_variant"),
    ("lower_true_then_detach_false", '{"detach":true,"Detach":false}', "exec_start.key_case_variant"),
    ("unknown_key", '{"Detach":true,"Whatever":1}', "exec_start.key_unknown"),
    ("hijack", '{"Detach":false,"Tty":true}', "exec.start.hijack_unsupported"),
]


@pytest.mark.parametrize(("case_id", "body", "rule"), EXEC_START_DENY, ids=[c[0] for c in EXEC_START_DENY])
async def test_exec_start_bypass_variants_never_reach_the_upstream(guard: Guard, case_id: str, body: Raw, rule: str):
    status, payload = await guard.send("POST", "/exec/deadbeef/start", body)
    assert guard.stub.mutating() == [], case_id
    assert (status, payload.get("rule")) == (403, rule), (case_id, payload)


async def test_detached_exec_start_still_passes(guard: Guard):
    status, _ = await guard.send("POST", "/exec/deadbeef/start", '{"Detach":true}')
    assert status == 200
    (sent,) = guard.stub.mutating()
    assert json.loads(sent.body) == {"Detach": True}


CONNECT_DENY: List[Tuple[str, Raw, str, int]] = [
    # 归属门读到自己的 owned1，Docker 取后写入的 victim
    ("container_then_lower_victim", f'{{"Container":"{OWNED}","container":"{VICTIM}"}}', "network_connect.key_case_variant", 403),
    ("lower_victim_then_container", f'{{"container":"{VICTIM}","Container":"{OWNED}"}}', "network_connect.key_case_variant", 403),
    ("upper_only", f'{{"CONTAINER":"{VICTIM}"}}', "network_connect.key_case_variant", 403),
    ("unknown_key", f'{{"Container":"{OWNED}","Whatever":1}}', "network_connect.key_unknown", 403),
    ("missing_container", '{"EndpointConfig":{}}', "network_connect.container.missing", 403),
    ("no_body", None, "network_connect.body.missing", 403),
    # 精确键但目标不是本部署的：仍然是归属门的 404
    ("foreign_exact", f'{{"Container":"{VICTIM}"}}', "", 404),
]


@pytest.mark.parametrize(("case_id", "body", "rule", "status_expected"), CONNECT_DENY, ids=[c[0] for c in CONNECT_DENY])
async def test_network_connect_bypass_variants_never_reach_the_upstream(
    guard: Guard,
    case_id: str,
    body: Raw,
    rule: str,
    status_expected: int,
):
    status, payload = await guard.send("POST", "/networks/nekro_sandbox_net/connect", body)
    assert guard.stub.mutating() == [], case_id
    assert status == status_expected, (case_id, status, payload)
    if rule:
        assert payload.get("rule") == rule, (case_id, payload)


async def test_network_connect_forwards_the_judged_structure(guard: Guard):
    # 重复键：Python 与 Go 都取最后一个（owned1）；归属门看的与转发的是同一份
    raw = f'{{"Container":"{VICTIM}","Container":"{OWNED}","EndpointConfig":{{}}}}'
    status, _ = await guard.send("POST", "/networks/nekro_sandbox_net/connect", raw)
    assert status == 200
    (sent,) = guard.stub.mutating()
    assert json.loads(sent.body) == {"Container": OWNED, "EndpointConfig": {}}
    assert sent.body.count(b"Container") == 1


# ---------------------------------------------------------------------------
# 5. 工作量上限与超时（P2 #6）
# ---------------------------------------------------------------------------


async def test_too_many_binds_is_rejected_before_any_resolution(guard: Guard):
    binds = [f"{ROOT}/sandboxes/x:/m{i}" for i in range(MAX_BINDS + 1)]
    status, payload = await guard.send("POST", "/containers/create", _hc(Binds=binds))
    assert guard.stub.mutating() == []
    assert (status, payload.get("rule")) == (403, "create.binds.too_many")


async def test_max_binds_still_allowed(guard: Guard):
    binds = [f"{ROOT}/sandboxes/x:/m{i}" for i in range(MAX_BINDS)]
    status, _ = await guard.send("POST", "/containers/create", _hc(Binds=binds))
    assert status == 201


async def test_the_resolution_budget_is_shared_across_all_binds_of_one_request(guard: Guard):
    """单条深路径花得起，同一个请求里 32 条一起就超预算：预算是**请求**级的，不是每条 bind 各算各的。"""
    one = f"{ROOT}/{DEEP}:/m"
    status, _ = await guard.send("POST", "/containers/create", _hc(Binds=[one]))
    assert status == 201

    status, payload = await guard.send("POST", "/containers/create", _hc(Binds=[f"{ROOT}/{DEEP}:/m{i}" for i in range(MAX_BINDS)]))
    assert (status, payload.get("rule")) == (403, "create.binds.too_complex"), payload
    assert len(guard.stub.mutating()) == 1, "超预算的那个请求不该到达上游"


class _BlockingFs(FakeBindFs):
    """lstat 卡住（模拟 NFS / 被挂死的挂载点）。直到 `release` 被置位或 1 秒后才返回。"""

    def __init__(self, entries: Dict[str, Any]) -> None:
        super().__init__(entries)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.block = False

    def lstat_kind(self, path: str) -> str:
        if self.block:
            self.entered.set()
            self.release.wait(1.0)
        return super().lstat_kind(path)


async def test_blocked_filesystem_fails_closed_on_timeout_and_does_not_stall_the_event_loop(monkeypatch: pytest.MonkeyPatch):
    fs = _BlockingFs(_BASE_ENTRIES)
    async with _running_guard(monkeypatch, fs) as g:
        g.proxy._policy_timeout = 0.15
        fs.block = True
        started = time.monotonic()
        create = asyncio.create_task(g.send("POST", "/containers/create", _hc(Binds=[f"{ROOT}/sandboxes/x:/m"])))
        await asyncio.get_running_loop().run_in_executor(None, fs.entered.wait, 2.0)  # 等判据真的卡进 lstat

        # 判据线程卡着的这段时间，事件循环必须还能服务别的请求
        ping_started = time.monotonic()
        ping_status, _ = await g.send("GET", "/_ping")
        ping_elapsed = time.monotonic() - ping_started
        assert ping_status == 200
        assert ping_elapsed < 0.5, f"判据阻塞了事件循环（_ping 花了 {ping_elapsed:.2f}s）"

        status, payload = await create
        assert time.monotonic() - started < 0.9, "超时没有生效，create 一直等到文件系统自己返回"
        assert (status, payload.get("rule")) == (403, "policy.timeout"), payload
        assert g.stub.mutating() == []
        fs.release.set()


class _BrokenFs(FakeBindFs):
    """lstat 抛一个解析器不认识的异常（不是 OSError / ValueError）。"""

    def lstat_kind(self, path: str) -> str:
        raise RuntimeError("文件系统面炸了")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/containers/create", _hc(Binds=[f"{ROOT}/sandboxes/x:/m"])),
        ("POST", f"/containers/{OWNED}/start", None),
    ],
    ids=["create", "start_recheck"],
)
async def test_unexpected_policy_errors_fail_closed_not_open_and_not_500(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: Raw,
):
    async with _running_guard(monkeypatch, _BrokenFs(_BASE_ENTRIES)) as g:
        status, payload = await g.send(method, path, body)
        assert (status, payload.get("rule")) == (403, "policy.error"), payload
        assert g.stub.mutating() == []


async def test_start_recheck_also_runs_off_the_loop_with_a_timeout(monkeypatch: pytest.MonkeyPatch):
    fs = _BlockingFs(_BASE_ENTRIES)
    async with _running_guard(monkeypatch, fs) as g:
        g.proxy._policy_timeout = 0.15
        fs.block = True
        started = time.monotonic()
        status, payload = await g.send("POST", f"/containers/{OWNED}/start")
        assert time.monotonic() - started < 0.9
        assert (status, payload.get("rule")) == (403, "policy.timeout"), payload
        assert g.stub.mutating() == []
        fs.release.set()


# ---------------------------------------------------------------------------
# 6. 启动告警的调用点（原来删掉 create_app 里的调用，所有用例照样全绿）
# ---------------------------------------------------------------------------


class _ErrorSink:
    def __init__(self) -> None:
        self.messages: List[str] = []
        self._id = logger.add(lambda m: self.messages.append(str(m)), level="ERROR", format="{message}")

    def close(self) -> None:
        logger.remove(self._id)


async def test_create_app_warns_at_startup_when_the_data_dir_is_invisible(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_mod, "build_context", lambda: PolicyContext(allowed_bind_roots=(ROOT,), fs=FakeBindFs({})))
    sink = _ErrorSink()
    try:
        await proxy_mod.create_app()
    finally:
        sink.close()
    joined = "\n".join(sink.messages)
    assert "看不到允许的 bind 根目录" in joined and ROOT in joined, sink.messages


async def test_create_app_is_quiet_when_the_data_dir_is_visible(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_mod, "build_context", lambda: PolicyContext(allowed_bind_roots=(ROOT,), fs=FakeBindFs(_BASE_ENTRIES)))
    sink = _ErrorSink()
    try:
        await proxy_mod.create_app()
    finally:
        sink.close()
    assert not any("看不到允许的 bind 根目录" in m for m in sink.messages), sink.messages


# ---------------------------------------------------------------------------
# 7. 复验追加：真实客户端的 exec 请求体、update 端点、孤立代理项
# ---------------------------------------------------------------------------


async def test_real_client_exec_bodies_are_accepted_and_normalised(guard: Guard):
    """aiodocker 0.25 / docker-py 7.1.0 的 exec 请求体都带 `Container`；aiodocker 的 `detachKeys` 是小写开头。

    放行这两个，转发前规范化：`Container` 丢掉（容器只由 URL 决定），`detachKeys` 改写成 `DetachKeys`。
    """
    docker_py = {
        "Container": OWNED,
        "User": "",
        "Privileged": False,
        "Tty": False,
        "AttachStdin": False,
        "AttachStdout": True,
        "AttachStderr": True,
        "Env": None,
        "Cmd": ["ls", "-l"],
        "WorkingDir": None,
        "DetachKeys": "ctrl-p,ctrl-q",
    }
    aiodocker = {"Cmd": ["ls"], "Container": OWNED, "AttachStdin": False, "Tty": False, "detachKeys": "ctrl-p"}
    for body, expected_detach in ((docker_py, "ctrl-p,ctrl-q"), (aiodocker, "ctrl-p")):
        guard.stub.requests.clear()
        status, payload = await guard.send("POST", f"/containers/{OWNED}/exec", body)
        assert status == 200, payload
        (sent,) = guard.stub.mutating()
        forwarded = json.loads(sent.body)
        assert "Container" not in forwarded and "detachKeys" not in forwarded
        assert forwarded["DetachKeys"] == expected_detach
        assert forwarded["Cmd"] == body["Cmd"]


UPDATE_DENY: List[Tuple[str, Raw, str]] = [
    ("always", {"RestartPolicy": {"Name": "always"}}, "update.restart_policy.only_no"),
    ("unless_stopped", {"RestartPolicy": {"Name": "unless-stopped"}}, "update.restart_policy.only_no"),
    ("on_failure", {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 3}}, "update.restart_policy.only_no"),
    ("no_with_retry_count", {"RestartPolicy": {"Name": "no", "MaximumRetryCount": 3}}, "update.restart_policy.only_no"),
    ("empty_name", {"RestartPolicy": {"Name": ""}}, "update.restart_policy.only_no"),
    ("resources_too", {"RestartPolicy": {"Name": "no"}, "Memory": 1}, "update.key_unknown"),
    ("resources_only", {"Memory": 1}, "update.key_unknown"),
    ("variant", '{"restartpolicy":{"Name":"no"}}', "update.key_case_variant"),
    ("inner_variant", '{"RestartPolicy":{"name":"no"}}', "update.key_case_variant"),
    ("not_an_object", {"RestartPolicy": "no"}, "update.restart_policy.required"),
    ("no_body", None, "update.body.missing"),
]


@pytest.mark.parametrize(("case_id", "body", "rule"), UPDATE_DENY, ids=[c[0] for c in UPDATE_DENY])
async def test_update_only_ever_lets_a_restart_policy_become_no(guard: Guard, case_id: str, body: Raw, rule: str):
    status, payload = await guard.send("POST", f"/containers/{OWNED}/update", body)
    assert guard.stub.mutating() == [], case_id
    assert (status, payload.get("rule")) == (403, rule), (case_id, payload)


async def test_update_to_no_is_forwarded_for_owned_containers_only(guard: Guard):
    status, _ = await guard.send("POST", f"/containers/{OWNED}/update", {"RestartPolicy": {"Name": "no"}})
    assert status == 200
    (sent,) = guard.stub.mutating()
    assert (sent.method, sent.path) == ("POST", f"/containers/{OWNED}/update")
    assert json.loads(sent.body) == {"RestartPolicy": {"Name": "no"}}

    guard.stub.requests.clear()
    status, _ = await guard.send("POST", f"/containers/{VICTIM}/update", {"RestartPolicy": {"Name": "no"}})
    assert status == 404, "别人的容器：归属门先拒"
    assert guard.stub.mutating() == []


SURROGATE_DENY: List[Tuple[str, str, str]] = [
    ("create_label_value", '{"Image":"a","Labels":{"k":"\\ud800"}}', "/containers/create"),
    ("create_label_key", '{"Image":"a","Labels":{"\\udc00":"v"}}', "/containers/create"),
    ("create_cmd_item", '{"Image":"a","Cmd":["ok","x\\ud83dy"]}', "/containers/create"),
    ("create_nested_key", '{"Image":"a","HostConfig":{"Dns":[],"Sysctls":{}},"Env":["a\\udfff"]}', "/containers/create"),
    ("exec_cmd", '{"Cmd":["\\ud800"]}', f"/containers/{OWNED}/exec"),
    ("connect_container", '{"Container":"\\ud800"}', "/networks/nekro_sandbox_net/connect"),
]


@pytest.mark.parametrize(("case_id", "raw", "path"), SURROGATE_DENY, ids=[c[0] for c in SURROGATE_DENY])
async def test_lone_surrogates_in_the_body_are_rejected(guard: Guard, case_id: str, raw: str, path: str):
    """Python 照单全收 `\\ud800`，Go 把它换成 U+FFFD：同一个字符串两边看到的不是同一个值。"""
    status, payload = await guard.send("POST", path, raw)
    assert guard.stub.mutating() == [], case_id
    assert (status, payload.get("rule")) == (403, "body.invalid"), (case_id, payload)


async def test_a_valid_surrogate_pair_is_just_an_emoji_and_is_allowed(guard: Guard):
    status, _ = await guard.send("POST", "/containers/create", '{"Image":"a","Labels":{"k":"\\ud83d\\ude00"}}')
    assert status == 201
