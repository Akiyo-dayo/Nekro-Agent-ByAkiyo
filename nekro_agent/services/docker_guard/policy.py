"""Docker API 请求策略（纯函数，无 I/O）。

这是 `docker-guard` 反向代理的判据核心，独立成模块的唯一目的是**可被表驱动单测钉住**：
代理转发前对每个请求调用 `evaluate()`，deny 的直接回 403，不落到宿主 docker.sock。

## 为什么不用现成的 tecnativa/docker-socket-proxy

它的粒度是「整类 API」（`CONTAINERS=1`），只按 URL 路径前缀放行，**不看请求体**。而 NA
必须能 `POST /containers/create`（沙盒、CC 工作区、NapCat 都要建容器），一旦放行这个端点，
攻击者就能在请求体里塞 `Privileged: true` + `Binds: ["/:/host"]` 起一个挂载宿主根目录的
特权容器——实测（见 docs/docker-guard.md）能直接读走宿主 `/etc/shadow`、写宿主 `/root`。
所以对 NA 这个威胁模型，路径级代理**等于没有**。

本模块的判据补上了请求体检查：`create` 必须逐字段满足白名单，任何越界字段直接 deny。
它不是「整类放行」而是「按 NA 实际用到的动作 + 安全形状放行」。

## 判据看到的结构必须就是 Docker 最终解码的结构

判据对请求体读的是 Python `dict` 的**精确键**，而 Docker 用 Go `encoding/json` 解码：键名大小写不敏感、
重复键合并、Engine ≤26 还认顶层摊平的 HostConfig 字段、API <1.24 的 start 会把请求体当 HostConfig。
这些差异每一处都是一条绕过（见 `docs/docker-guard.md` 与 `tests/test_docker_guard_bypass.py`）。所以：
请求体按**精确键白名单**校验（大小写变体、未知键、摊平的 HostConfig 字段一律拒绝，`_check_keys`）；
放行后转发的是**判过的结构**（`Decision.rewritten_body`），不是客户端原始字节；低于 v1.24 的版本前缀一律拒绝，
start / restart 不许带体。带 Binds 的容器只许 `RestartPolicy=no`——守护进程自己触发的重启不经过 guard。

## NA 实际用到的 Docker API 面（本白名单据此推导，多一个都不放）

- 容器：list / create / inspect / logs / start / stop / restart / kill / wait / delete /
  exec + exec-start（MCP stdio 校验、CC 终端）
- 镜像：inspect / pull
- 网络：inspect / create / connect
- 系统：version / info / _ping

其余（commit、update、build、events、volumes、swarm、archive、镜像删除…）NA 从不调用，一律 deny。
"""

from __future__ import annotations

import copy
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote

from nekro_agent.services.docker_guard.bindpath import (
    RULE_OUTSIDE,
    BindFs,
    RealBindFs,
    ResolveBudget,
    resolve_bind_source,
)

# 单个 create 请求最多允许的 bind 条数。NA 自己最多 4 条；留足余量，但不无界——
# 条数不设限的话，一个 1MB 的请求体就能塞进两万条 bind 让 guard 逐条解析。
MAX_BINDS = 32
# start / restart 复核时 inspect 出来的挂载源条数上限（Mounts 与 Binds 并集，所以是 create 上限的两倍）
MAX_RECHECK_SOURCES = MAX_BINDS * 2

# Docker Engine 从 1.12 起（API 1.24）才在 start 上拒绝非空请求体；更低的 API 版本会把 start 的请求体当
# HostConfig 解码（旧接口），绕过 create 时的全部判据。低于它的版本前缀一律拒绝。
MIN_API_VERSION = (1, 24)

# 容器创建时 CapAdd 只允许这两个，且仅限「宿主网络命名空间的 iptables 临时容器」使用
# （services/sandbox/network_guard.py 的 HelperContainerIptablesExecutor）。
# 沙盒 / CC / NapCat 容器的 CapAdd 恒为空。
_NETGUARD_CAPS = frozenset({"NET_ADMIN", "NET_RAW"})

# 允许的 iptables 二进制名（netguard 临时容器的 Cmd[0]）
_IPTABLES_BINARIES = frozenset({"iptables", "ip6tables", "iptables-legacy", "ip6tables-legacy"})

# SecurityOpt 白名单：NA 在容器内运行时只用 no-new-privileges；
# 任何 *=unconfined（apparmor/seccomp）都是放宽约束，deny；`label=disable`（关掉 SELinux 隔离）同样 deny。
# `systempaths=unconfined` 是 CLI 写法，API 里的等价形式是 HostConfig.MaskedPaths / ReadonlyPaths
# 取非 null 值（`[]` = 不再遮蔽 /proc、/sys 的敏感路径），由 `_check_restricted_fields` 判。
_ALLOWED_SECURITY_OPT_PREFIXES = ("no-new-privileges", "label=", "seccomp=/", "apparmor=docker")

# 归属标签前缀：判定「这个容器是不是 NA（本部署）建的」。
# 路径级放行只保证「NA 能对 Docker 做哪些动作」，管不到「对哪个容器做」——
# 而 exec / logs / inspect / start / stop / kill / restart / delete 这些**按 ID 指向某个具体容器**
# 的端点若不加归属判据，攻击者在 NA 进程内拿到代码执行后就能：
#   - `exec` 进宿主上任意一个别人的容器（比如挂了 docker.sock 的第三方容器 → 宿主 root）；
#   - `inspect` 兄弟容器 postgres 读走 `POSTGRES_PASSWORD`（平台方凭据）；
#   - `stop`/`kill` 无关容器做 DoS。
# 这些容器都**不是** NA 建的，也就不带任何 `nekro.` 标签。NA 自己建的沙盒 / CC 工作区 /
# NapCat / netguard 容器全部带 `nekro.*` 标签（工作区那条见 services/workspace/container.py）。
# 代理据此在转发前先确认目标容器归属本部署，非本部署的一律 403。
OWNERSHIP_LABEL_KEY_PREFIX = "nekro."


@dataclass(frozen=True)
class Decision:
    """一次请求的放行判据结果。"""

    allow: bool
    reason: str
    # 命中的规则名，便于日志与测试断言
    rule: str = ""
    # create 放行时：**代理必须转发这份**而不是客户端发来的原始字节。
    # 判据看到的结构与 Docker 最终解析的结构只要有一处不同，白名单就失效；bind source 在这里被换成
    # 解析后的规范路径（见 `bindpath`），转发的就是判过的那一份。
    rewritten_body: Optional[Dict[str, Any]] = field(default=None, compare=False, hash=False, repr=False)


@dataclass(frozen=True)
class PolicyContext:
    """判据所需的部署上下文（由代理注入，测试里显式构造）。"""

    # 允许作为 bind source 的宿主目录前缀（已 normpath），通常是 NEKRO_DATA_DIR。
    allowed_bind_roots: Sequence[str] = field(default_factory=tuple)
    # 额外允许的只读单文件 bind（如 /etc/localtime）。
    allowed_ro_files: Sequence[str] = field(default_factory=lambda: ("/etc/localtime",))
    # bind source 的真实路径解析要用的文件系统面。默认就是真实文件系统：看不到允许根目录时
    # 一律拒绝（fail-closed），**不存在「没有文件系统就退回字符串判断」这条路**。测试里注入内存实现。
    fs: BindFs = field(default_factory=RealBindFs, compare=False, hash=False, repr=False)


# ---------------------------------------------------------------------------
# 路径归一化与端点匹配
# ---------------------------------------------------------------------------

# 去掉可选的 API 版本前缀：aiodocker 不带前缀，docker CLI 带 `/v1.51`。
# 只认 ASCII 数字：Docker 路由的版本段是 `[0-9.]+`，Unicode 数字它不认（会 404），这里也不该认。
_VERSION_PREFIX = re.compile(r"^/v([0-9]+)\.([0-9]+)(?=/)")

_CID = r"[a-zA-Z0-9][a-zA-Z0-9_.-]*"  # 容器 / 网络 / exec ID 或名称


_IMAGE_REF = r"[A-Za-z0-9][A-Za-z0-9_.:/@-]*"  # 镜像引用：可含 `/`（library/python）、`:`（tag）、`@`（digest）


@dataclass(frozen=True)
class _Endpoint:
    method: str
    pattern: re.Pattern[str]
    rule: str
    guard_create_body: bool = False
    # 端点按 ID 指向某个具体容器（exec / logs / inspect / start / stop / …）时置 True：
    # 转发前必须额外确认该容器归属本部署，否则 NA 一旦被攻破就能操作宿主上任意容器。
    # 正则里以 `(?P<cid>…)` 捕获容器引用，供 `container_ownership_target` 取出。
    container_scoped: bool = False
    # 该端点的请求体需要额外判据（exec 创建、exec 启动）；对应 `evaluate_body_for_rule`。
    guard_body_rule: str = ""
    # start / restart 时 Docker 才真正解析 bind source，所以 create 时判过的路径到这一刻要**再判一遍**
    # （检查与挂载之间的间隔从「create 到 start 的任意长」收窄成一次 inspect 的时间）。
    recheck_binds: bool = False
    # start / restart 不带请求体：API <1.24 会把 start 的请求体解码成 HostConfig（见 MIN_API_VERSION）。
    # 即便版本前缀已拒绝低版本，非空体在这里也不放行——NA 与 docker CLI 都从不给这两个端点发体。
    forbid_body: bool = False


# 放行端点表：未命中任何一条即 deny。顺序不敏感（互斥）。
_ENDPOINTS: List[_Endpoint] = [
    # —— 系统探测（只读）——
    _Endpoint("GET", re.compile(r"^/_ping$"), "system.ping"),
    _Endpoint("HEAD", re.compile(r"^/_ping$"), "system.ping"),
    _Endpoint("GET", re.compile(r"^/version$"), "system.version"),
    _Endpoint("GET", re.compile(r"^/info$"), "system.info"),
    # —— 容器 ——
    _Endpoint("GET", re.compile(r"^/containers/json$"), "containers.list"),
    _Endpoint("POST", re.compile(r"^/containers/create$"), "containers.create", guard_create_body=True),
    _Endpoint("GET", re.compile(rf"^/containers/(?P<cid>{_CID})/json$"), "containers.inspect", container_scoped=True),
    _Endpoint("GET", re.compile(rf"^/containers/(?P<cid>{_CID})/logs$"), "containers.logs", container_scoped=True),
    _Endpoint(
        "POST",
        re.compile(rf"^/containers/(?P<cid>{_CID})/start$"),
        "containers.start",
        container_scoped=True,
        recheck_binds=True,
        forbid_body=True,
    ),
    _Endpoint("POST", re.compile(rf"^/containers/(?P<cid>{_CID})/stop$"), "containers.stop", container_scoped=True),
    _Endpoint(
        "POST",
        re.compile(rf"^/containers/(?P<cid>{_CID})/restart$"),
        "containers.restart",
        container_scoped=True,
        recheck_binds=True,
        forbid_body=True,
    ),
    _Endpoint("POST", re.compile(rf"^/containers/(?P<cid>{_CID})/kill$"), "containers.kill", container_scoped=True),
    # update 只为一件事开：把存量容器的 RestartPolicy 改成 no（NA 启动时迁移升级前建的 `unless-stopped` NapCat，
    # 见 `container_manager.migrate_legacy_restart_policies`）。请求体判据 `evaluate_update_body` 只放行
    # `{"RestartPolicy":{"Name":"no"}}`，资源配额（Memory / CpuShares …）等 update 的其它字段一律拒绝。
    _Endpoint(
        "POST",
        re.compile(rf"^/containers/(?P<cid>{_CID})/update$"),
        "containers.update",
        container_scoped=True,
        guard_body_rule="update_restart_policy",
    ),
    _Endpoint("POST", re.compile(rf"^/containers/(?P<cid>{_CID})/wait$"), "containers.wait", container_scoped=True),
    _Endpoint("DELETE", re.compile(rf"^/containers/(?P<cid>{_CID})$"), "containers.remove", container_scoped=True),
    _Endpoint(
        "POST",
        re.compile(rf"^/containers/(?P<cid>{_CID})/exec$"),
        "containers.exec_create",
        container_scoped=True,
        guard_body_rule="exec_create",
    ),
    # —— exec ——
    # exec/{id} 里的 id 是 docker 在 exec-create 时新签发的 exec 句柄，不是容器 ID；
    # 攻击者拿不到「别人容器」的 exec 句柄（exec-create 已按容器归属挡下），故此处无需再判归属。
    _Endpoint("GET", re.compile(rf"^/exec/{_CID}/json$"), "exec.inspect"),
    _Endpoint("POST", re.compile(rf"^/exec/{_CID}/start$"), "exec.start", guard_body_rule="exec_start"),
    _Endpoint("POST", re.compile(rf"^/exec/{_CID}/resize$"), "exec.resize"),
    # —— 镜像 ——
    _Endpoint("GET", re.compile(r"^/images/create$"), "images.pull"),  # 部分客户端用 GET 探测
    _Endpoint("POST", re.compile(r"^/images/create$"), "images.pull"),
    # 镜像引用允许 `/`，所以这条是白名单里唯一「一个通配段能吃掉多级路径」的规则——
    # 字符集必须收紧到镜像引用真正用得到的那些字符，别让它顺带匹配到别的端点。
    # （`..` 之类的穿越段由 `evaluate()` 里的 `path_has_dot_segments` 统一挡，见那里的注释。）
    _Endpoint("GET", re.compile(rf"^/images/{_IMAGE_REF}/json$"), "images.inspect"),
    # —— 网络 ——
    _Endpoint("POST", re.compile(r"^/networks/create$"), "networks.create"),
    _Endpoint("GET", re.compile(rf"^/networks/{_CID}$"), "networks.inspect"),
    # connect 的目标容器写在**请求体**里（`Container` 字段），所以它也要读 body——
    # 不是为了多一条判据，而是归属门要拿到那个引用（见 `network_connect_target`）。
    _Endpoint("POST", re.compile(rf"^/networks/{_CID}/connect$"), "networks.connect", guard_body_rule="network_connect"),
]


def normalize_path(raw_path: str) -> str:
    """去掉版本前缀与查询串，返回用于匹配的裸路径。

    **必须先做一次百分号解码**：判据看的路径与 Docker 守护进程最终看的路径必须是同一条。
    Go 的 `net/url` 会把 `%2e%2e` 解成 `..`，判据若只看编码形态就会与它对不上（见
    `path_has_dot_segments` 的注释）。只解一次，与 Go 一致；双重编码 `%252e` 解成 `%2e`
    后不是穿越段，Docker 那侧同样也不是。
    """
    path = raw_path.split("?", 1)[0]
    path = unquote(path)
    path = _VERSION_PREFIX.sub("", path, count=1)
    # 折叠重复斜杠，防止 `//containers//create` 之类绕过端点正则
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def api_version_of(raw_path: str) -> Optional[Tuple[int, int]]:
    """请求路径里的 API 版本前缀（`/v1.24/...` -> (1, 24)）；没有前缀返回 None（走守护进程当前版本）。

    与 `normalize_path` 同一个解码口径（先百分号解码一次），判据看到的版本就是 Docker 路由看到的版本。
    """
    path = unquote(raw_path.split("?", 1)[0])
    matched = _VERSION_PREFIX.match(path)
    if matched is None:
        return None
    return int(matched.group(1)), int(matched.group(2))


def path_has_dot_segments(path: str) -> bool:
    """路径里是否含 `.` / `..` 这类相对段。

    **这是一条实测打穿过的绕过**：代理转发时用 `aiohttp`/`yarl` 拼 URL，而 yarl 在构造
    URL 时会**自己消解 `..`**——
        URL("http://docker/images/x/../../containers/victim/json").path
        == "/containers/victim/json"
    于是 `GET /images/x/../../containers/<兄弟容器>/json` 会被判据当成 `images.inspect`
    放行（镜像引用本来就允许 `/`，且这条端点不需要归属校验），转发时却变成对**任意容器**的
    inspect —— 归属门被整条绕过，`Config.Env` 里的 `POSTGRES_PASSWORD` /
    `ONEBOT_ACCESS_TOKEN` / `NEKRO_ADMIN_PASSWORD` 直接读走。这正是归属门要挡的那件事，
    只是从另一个端点进来的。

    判据与转发之间只要存在**任何**路径改写，白名单就形同虚设。所以两侧同时收口：
    这里对含相对段的路径一律 deny，`proxy._forward` 那侧改成按原样字节转发
    （`URL(..., encoded=True)`）且不跟随上游重定向。NA 真实用到的路径里没有任何一条
    含 `.` / `..` 段，deny 不会误伤。
    """
    return any(seg in {".", ".."} for seg in path.split("/"))


# ---------------------------------------------------------------------------
# create 请求体判据
# ---------------------------------------------------------------------------


def _norm_source(source: str) -> str:
    """归一化 bind source 的宿主路径，消解 `..` 与重复斜杠。"""
    return posixpath.normpath(source)


def _bind_source_and_mode(bind: str) -> tuple[str, str]:
    """从 `src:dst[:mode]` 里拆出 source 与 mode。

    Windows 盘符路径含冒号，但 NA 的 bind source 恒为容器内看到的宿主 POSIX 路径，不涉及盘符。
    """
    parts = bind.split(":")
    if len(parts) < 2:
        return bind, ""
    source = parts[0]
    mode = parts[2] if len(parts) >= 3 else "rw"
    return source, mode


def _vet_binds(host_config: Dict[str, Any], ctx: PolicyContext) -> Tuple[Optional[Decision], Optional[List[str]]]:
    """逐条判 `HostConfig.Binds`；通过时同时给出**应当转发的** Binds（source 已换成解析后的规范路径）。

    判据看的和 Docker 挂的必须是同一条路径：字符串前缀判据看不穿宿主上的符号链接，
    `<数据目录>/link/../y` 词法上在数据目录内、物理上却可能在链接目标的上一级。所以这里先在宿主路径
    语义下把 source 解析成规范路径（`bindpath.resolve_bind_source`），解析不了就拒绝，
    通过后把**解析后的路径**写回去转发，Docker 拿到的就是判过的那一条。
    """
    binds = host_config.get("Binds")
    if not binds:
        return None, None
    if not isinstance(binds, list):
        return Decision(False, "HostConfig.Binds 不是列表", "create.binds.malformed"), None
    if len(binds) > MAX_BINDS:
        return Decision(False, f"HostConfig.Binds 条数过多（{len(binds)} > {MAX_BINDS}）", "create.binds.too_many"), None
    # 整个请求共享一份解析预算：多条 bind 各自重新计算的话，条数 × 单条上限仍然是个大数
    budget = ResolveBudget()
    forwarded: List[str] = []
    for bind in binds:
        if not isinstance(bind, str):
            return Decision(False, "HostConfig.Binds 元素非字符串", "create.binds.malformed"), None
        source, mode = _bind_source_and_mode(bind)
        norm = _norm_source(source)
        rest = bind[len(source) :]  # `:目标[:选项]`，原样保留
        # 只读单文件豁免（/etc/localtime）
        if norm in {_norm_source(f) for f in ctx.allowed_ro_files}:
            if "ro" not in mode.split(","):
                return Decision(False, f"只读豁免文件必须以 ro 挂载: {bind}", "create.binds.ro_required"), None
            forwarded.append(norm + rest)
            continue
        resolution = resolve_bind_source(source, ctx.allowed_bind_roots, ctx.fs, budget)
        if not resolution.ok:
            return Decision(False, resolution.reason, f"create.binds.{resolution.rule}"), None
        forwarded.append(resolution.resolved + rest)
    return None, forwarded


def _check_mounts(host_config: Dict[str, Any]) -> Optional[Decision]:
    # NA 从不使用 HostConfig.Mounts；出现即视为绕过 Binds 校验的尝试。
    mounts = host_config.get("Mounts")
    if mounts:
        return Decision(False, "HostConfig.Mounts 不被允许（NA 只用 Binds）", "create.mounts.forbidden")
    # `VolumesFrom` 是 Binds 判据的**第三个入口**，且是最狠的一个：它整份继承另一个容器的挂载表，
    # 不写任何路径就能拿到对方挂进来的东西。宿主上只要存在一个挂了 `/` 或 `docker.sock` 的容器
    # （guard 的威胁模型里这个前提是**成立的**——归属门存在的理由正是「宿主上恰有挂了 docker.sock
    # 的兄弟容器」），`VolumesFrom:["那个容器"]` 就等价于把它的 docker.sock 挂进新容器 → 宿主 root，
    # 而 Binds/Mounts 两条判据一个字都没被触发。NA 自己从不用 VolumesFrom。
    if host_config.get("VolumesFrom"):
        return Decision(
            False,
            "HostConfig.VolumesFrom 不被允许（会整份继承别的容器的挂载，绕过 Binds 判据）",
            "create.volumes_from.forbidden",
        )
    return None


# 命名空间共享类字段：`host` 是宿主命名空间，`container:<ref>` 是**别的容器**的命名空间。
# 只挡 `host` 是不够的：`PidMode=container:<某容器>` 让新容器进到对方的 PID 命名空间，
# 容器内 root 即可通过 `/proc/<pid>/root/` 读写对方的整个文件系统（对方若挂了宿主目录就是宿主文件）；
# `NetworkMode=container:<某容器>` 直接借用对方的网络栈（含它 bind 在 127.0.0.1 上的管理口）。
# NA 只用具名网络 / 默认网桥 / netguard 的 host 网络，从不共享别的容器的命名空间。
_NAMESPACE_FIELDS = ("PidMode", "IpcMode", "UsernsMode", "CgroupnsMode", "UTSMode", "NetworkMode")


def _check_namespace_sharing(host_config: Dict[str, Any]) -> Optional[Decision]:
    for key in _NAMESPACE_FIELDS:
        val = str(host_config.get(key) or "").strip().lower()
        # NetworkMode=host 是 netguard 那条通道，交给 `_check_caps_and_network` 逐字段判形状
        if val == "host" and key != "NetworkMode":
            return Decision(False, f"禁止 {key}=host", f"create.{key.lower()}_host")
        if val.startswith("container:"):
            return Decision(
                False,
                f"禁止共享其它容器的命名空间: {key}={val}",
                f"create.{key.lower()}_container",
            )
    return None


def _check_privilege_flags(host_config: Dict[str, Any]) -> Optional[Decision]:
    if host_config.get("Privileged"):
        return Decision(False, "禁止 Privileged 容器", "create.privileged")
    ns = _check_namespace_sharing(host_config)
    if ns is not None:
        return ns
    if host_config.get("CgroupParent"):
        return Decision(False, "禁止指定 CgroupParent", "create.cgroup_parent")
    if host_config.get("Devices"):
        return Decision(False, "禁止映射宿主设备（Devices）", "create.devices")
    if host_config.get("DeviceCgroupRules"):
        return Decision(False, "禁止 DeviceCgroupRules", "create.device_cgroup_rules")
    if host_config.get("Sysctls"):
        return Decision(False, "禁止自定义 Sysctls", "create.sysctls")
    # NA 从不指定 Runtime（走宿主默认）。指定即意味着挑一个可能更弱的运行时（如某些 shim）。
    if str(host_config.get("Runtime") or "").strip():
        return Decision(False, "禁止指定 Runtime", "create.runtime")
    return None


def _check_security_opt(host_config: Dict[str, Any]) -> Optional[Decision]:
    sec = host_config.get("SecurityOpt")
    if not sec:
        return None
    if not isinstance(sec, list):
        return Decision(False, "SecurityOpt 不是列表", "create.securityopt.malformed")
    for opt in sec:
        text = str(opt).strip()
        # `label=disable` / `label:disable`：关掉 SELinux 对容器的约束（Docker 两种分隔符都认）。
        # 注意 `label=` 在下面的允许前缀里，所以必须在前缀放行之前显式拒绝；`label:disable` / 大小写变体不在前缀里，
        # 本来也会被下面的兜底拒绝，这里一并写明是为了将来放宽前缀表时不会悄悄重新放行
        # （变异验证里这两处是等价变异体，不是漏测）。
        if re.match(r"^label[=:]\s*disable\s*$", text, re.IGNORECASE):
            return Decision(False, f"禁止的 SecurityOpt: {text}（关闭 SELinux 隔离）", "create.securityopt.forbidden")
        if text == "no-new-privileges" or text.startswith(_ALLOWED_SECURITY_OPT_PREFIXES):
            continue
        return Decision(False, f"禁止的 SecurityOpt: {text}", "create.securityopt.forbidden")
    return None


# HostConfig 白名单里「NA 的 create 请求从不使用、却有提权 / 逃逸能力」的字段：只允许空值。
# 白名单（`_HOST_CONFIG_FIELDS`）回答「这个键是不是 Docker 的字段」（防大小写变体 / 摊平），不回答「这个值安不安全」；
# 后一个问题要逐字段有判据。逐个审过：
#   ContainerIDFile  守护进程不读，但它是宿主路径，留着只有害处
#   VolumeDriver     指定卷插件（任意第三方驱动），NA 只用 Binds
#   Cgroup           与 CgroupnsMode 同类的 cgroup 共享写法（`container:<ref>`）
#   DeviceRequests   GPU / 设备请求：`--gpus` 的 API 形式，会把宿主设备节点放进容器
#   Annotations      传给 OCI 运行时 / hook 的注解，部分运行时据此改行为
#   Links            旧式容器链接：把别的容器的环境变量 / 地址注入进来
#   GroupAdd         给容器进程追加附加组（如 root 组），读写挂载进来的组权限文件
#   Isolation        运行时隔离模式（Windows）
#   OomScoreAdj      负值让容器进程更不易被 OOM 杀掉（拖垮宿主）
# 其余没列出的放行字段（Memory / NanoCpus / PortBindings / ExtraHosts / Dns* / Tmpfs / Ulimits 等）只改资源或网络参数，
# 不带来宿主文件系统或特权层面的逃逸能力，保持放行。
_MUST_BE_EMPTY_HOST_CONFIG_FIELDS = (
    "ContainerIDFile",
    "VolumeDriver",
    "Cgroup",
    "DeviceRequests",
    "Annotations",
    "Links",
    "GroupAdd",
    "Isolation",
    "OomScoreAdj",
)
# MaskedPaths / ReadonlyPaths 是 `--security-opt systempaths=unconfined` 的 API 形式：null（缺省）= 用 Docker 默认的
# 遮蔽 / 只读路径集合；`[]` 或自定义列表 = 把 /proc、/sys 里的敏感路径露出来。所以只允许 null，连 `[]` 也不行。
_MUST_BE_NULL_HOST_CONFIG_FIELDS = ("MaskedPaths", "ReadonlyPaths")
_EMPTY_VALUES = (None, "", [], {}, 0)
_SAFE_LOG_DRIVERS = ("", "json-file", "local", "none")


def _check_restricted_fields(host_config: Dict[str, Any]) -> Optional[Decision]:
    for field_name in _MUST_BE_NULL_HOST_CONFIG_FIELDS:
        if host_config.get(field_name) is not None:
            return Decision(
                False,
                f"HostConfig.{field_name} 只允许 null：非 null 等价于 systempaths=unconfined，会露出 /proc、/sys 的敏感路径",
                f"create.{field_name.lower()}",
            )
    for field_name in _MUST_BE_EMPTY_HOST_CONFIG_FIELDS:
        value = host_config.get(field_name)
        if isinstance(value, bool) or value not in _EMPTY_VALUES:
            return Decision(False, f"HostConfig.{field_name} 不被允许（NA 不使用，且有提权 / 逃逸能力）", f"create.{field_name.lower()}")
    log_config = host_config.get("LogConfig")
    if log_config is not None:
        if not isinstance(log_config, dict) or set(log_config) - {"Type", "Config"}:
            return Decision(False, "HostConfig.LogConfig 格式非法", "create.logconfig")
        if log_config.get("Type") not in _SAFE_LOG_DRIVERS or log_config.get("Config") not in (None, {}):
            # 日志驱动可以让守护进程去连任意地址（syslog / fluentd / gelf）或写任意路径
            return Decision(False, "HostConfig.LogConfig 只允许 json-file / local / none 且不带选项", "create.logconfig")
    return None


def _check_networking_config(body: Dict[str, Any]) -> Optional[Decision]:
    """NA 从不传 NetworkingConfig（网络靠 HostConfig.NetworkMode）。

    非空的 `EndpointsConfig` 在 NetworkMode 缺省时会决定容器入哪个网络，是 `NetworkMode=host` 判据之外的另一条路，
    所以只允许缺省 / 空对象。
    """
    config = body.get("NetworkingConfig")
    if config is None:
        return None
    if not isinstance(config, dict):
        return Decision(False, "NetworkingConfig 格式非法", "create.networkingconfig")
    keys_denial = _check_keys(config, frozenset({"EndpointsConfig"}), "NetworkingConfig", "create")
    if keys_denial is not None:
        return keys_denial
    if config.get("EndpointsConfig") not in (None, {}):
        return Decision(False, "NetworkingConfig.EndpointsConfig 不被允许（NA 不使用）", "create.networkingconfig")
    return None


def _check_caps_and_network(config: Dict[str, Any], host_config: Dict[str, Any]) -> Optional[Decision]:
    """CapAdd 与 host 网络的联合判据。

    唯一允许 `NetworkMode=host` 的形状是 network_guard 的 iptables 临时容器：
    host 网络 + 只加 NET_ADMIN/NET_RAW + 打了 `nekro.netguard=1` 标签 + Cmd[0] 是 iptables 二进制。
    这样 host 网络命名空间的能力被收敛到「只能跑 iptables」——攻击者最多篡改宿主防火墙，
    读不到宿主文件、也 exec 不到宿主（残余风险已在文档说明）。
    """
    cap_add_raw = host_config.get("CapAdd") or []
    cap_add = {str(c).upper() for c in cap_add_raw} if isinstance(cap_add_raw, list) else {"__malformed__"}
    network_mode = str(host_config.get("NetworkMode") or "").strip().lower()

    is_host_net = network_mode == "host"
    extra_caps = cap_add - _NETGUARD_CAPS

    if extra_caps:
        return Decision(False, f"禁止的 Linux capability: {sorted(extra_caps)}", "create.capadd.forbidden")

    if not is_host_net:
        # 非 host 网络时不允许任何附加能力（沙盒/CC/NapCat 都不需要）
        if cap_add:
            return Decision(False, f"非 host 网络容器不得附加能力: {sorted(cap_add)}", "create.capadd.non_host")
        return None

    # 到这里：请求 host 网络。必须严格符合 netguard 形状。
    labels = config.get("Labels")
    if not isinstance(labels, dict) or labels.get("nekro.netguard") != "1":
        return Decision(False, "host 网络仅允许 netguard 临时容器（缺 nekro.netguard 标签）", "create.hostnet.not_netguard")
    if not cap_add.issubset(_NETGUARD_CAPS):
        return Decision(False, "netguard 容器能力越界", "create.hostnet.caps")

    # 实际执行的程序由 Entrypoint 决定（非空时覆盖 Cmd），Cmd 只在 Entrypoint 缺省时生效。
    # 只查 Cmd[0] 是不够的：`Cmd=[iptables]` 配上 `Entrypoint=[/bin/sh,-c,evil]` 时 Docker 跑的是
    # entrypoint 的 shell（实测能 201 建出来），于是 host 网络命名空间 + NET_ADMIN/NET_RAW 落到
    # 任意 shell 手里（可抓包、可绑宿主端口），远不止「只能跑 iptables」。
    entrypoint = config.get("Entrypoint")
    if isinstance(entrypoint, str) and entrypoint.strip():
        # 字符串形式的 Entrypoint 会被 Docker 当 `/bin/sh -c <字符串>` 跑 —— 直接是 shell。
        return Decision(False, "netguard 容器 Entrypoint 不得为 shell 字符串形式", "create.hostnet.entrypoint_shell")
    if isinstance(entrypoint, list) and entrypoint:
        argv0, argv0_from = entrypoint[0], "Entrypoint"
    else:
        cmd = config.get("Cmd")
        if not isinstance(cmd, list) or not cmd:
            return Decision(False, "netguard 容器必须显式指定 iptables Cmd", "create.hostnet.no_cmd")
        argv0, argv0_from = cmd[0], "Cmd"
    binary = posixpath.basename(str(argv0))
    if binary not in _IPTABLES_BINARIES:
        return Decision(
            False,
            f"netguard 容器实际执行的程序必须是 iptables，实际 {binary!r}（来自 {argv0_from}）",
            "create.hostnet.cmd_not_iptables",
        )
    if host_config.get("Binds"):
        return Decision(False, "netguard 容器不得挂载任何宿主目录", "create.hostnet.binds")
    return None


# ---------------------------------------------------------------------------
# 请求体键名白名单（判据与 Docker 对「同一个键」的理解必须一致）
# ---------------------------------------------------------------------------
#
# Docker 用 Go 的 `encoding/json` 解码请求体：**键名大小写不敏感**（`hostconfig`、`HOSTCONFIG`、
# `Hostconfig` 都解到 `HostConfig` 字段；还认 Unicode 折叠，如 `ſ`(U+017F) 当 `s`、`K`(U+212A) 当 `k`），
# 重复键后者覆盖前者（嵌套对象则合并）。而 Python 的 `dict.get("HostConfig")` 只认精确键。
# 于是 `{"Image":"a","hostconfig":{"Binds":["/:/host"],"Privileged":true}}` 在判据眼里是「没有 HostConfig」
# （放行），在 Docker 眼里却是一份特权 HostConfig。内层 `binds` / `privileged` / `mounts` / `volumesfrom`
# 同理，`HostConfig` 与 `hostConfig` 双份并存也一样（重复键合并）。
#
# 做法：**不就地归一化**（`Labels` / `Env` 这类 map 的键是大小写敏感的数据，不能动），而是对判据读取的
# 每一层结构（create 顶层、HostConfig、RestartPolicy；exec 创建 / 启动；网络 connect）按精确键白名单校验：
#   - 键在白名单里 -> 过；
#   - 键不在白名单、但 `casefold` 后等于某个白名单键 -> 大小写变体，一律拒绝（不尝试猜它「想写」什么）；
#   - 其余未知键 -> 拒绝（fail-closed，也挡住将来 Engine 新增的、本判据不认识的字段）。
# 判据通过后转发的是判过的结构（`Decision.rewritten_body`），不是客户端原始字节，重复键由此消除。

# create 请求体顶层：`container.Config` 的字段 + `HostConfig` + `NetworkingConfig`
_CONTAINER_CONFIG_FIELDS = frozenset(
    {
        "Hostname", "Domainname", "User", "AttachStdin", "AttachStdout", "AttachStderr", "ExposedPorts",
        "Tty", "OpenStdin", "StdinOnce", "Env", "Cmd", "Healthcheck", "ArgsEscaped", "Image", "Volumes",
        "WorkingDir", "Entrypoint", "NetworkDisabled", "MacAddress", "OnBuild", "Labels", "StopSignal",
        "StopTimeout", "Shell",
    },
)  # fmt: skip
_CREATE_TOP_FIELDS = _CONTAINER_CONFIG_FIELDS | {"HostConfig", "NetworkingConfig"}

# `container.HostConfig`（含内嵌的 `Resources`）的全部字段。除了校验 HostConfig 自己的键，
# 它还回答另一个问题：顶层出现这里面的名字（且不是 Config 字段）= Engine ≤26 的扁平写法，见 `_check_keys`。
_HOST_CONFIG_FIELDS = frozenset(
    {
        "Binds", "ContainerIDFile", "LogConfig", "NetworkMode", "PortBindings", "RestartPolicy", "AutoRemove",
        "VolumeDriver", "VolumesFrom", "Mounts", "ConsoleSize", "Annotations", "CapAdd", "CapDrop", "CgroupnsMode",
        "Dns", "DnsOptions", "DnsSearch", "ExtraHosts", "GroupAdd", "IpcMode", "Cgroup", "Links", "OomScoreAdj",
        "PidMode", "Privileged", "PublishAllPorts", "ReadonlyRootfs", "SecurityOpt", "StorageOpt", "Tmpfs",
        "UTSMode", "UsernsMode", "ShmSize", "Sysctls", "Runtime", "Isolation", "MaskedPaths", "ReadonlyPaths",
        "Init",
        # Resources
        "CpuShares", "Memory", "NanoCpus", "CgroupParent", "BlkioWeight", "BlkioWeightDevice",
        "BlkioDeviceReadBps", "BlkioDeviceWriteBps", "BlkioDeviceReadIOps", "BlkioDeviceWriteIOps", "CpuPeriod",
        "CpuQuota", "CpuRealtimePeriod", "CpuRealtimeRuntime", "CpusetCpus", "CpusetMems", "Devices",
        "DeviceCgroupRules", "DeviceRequests", "KernelMemory", "KernelMemoryTCP", "MemoryReservation",
        "MemorySwap", "MemorySwappiness", "OomKillDisable", "PidsLimit", "Ulimits", "CpuCount", "CpuPercent",
        "IOMaximumIOps", "IOMaximumBandwidth",
    },
)  # fmt: skip

_RESTART_POLICY_FIELDS = frozenset({"Name", "MaximumRetryCount"})
_EXEC_CREATE_FIELDS = frozenset(
    {
        "User", "Privileged", "Tty", "ConsoleSize", "AttachStdin", "AttachStderr", "AttachStdout", "Detach",
        "DetachKeys", "Env", "WorkingDir", "Cmd",
    },
)  # fmt: skip
_EXEC_START_FIELDS = frozenset({"Detach", "Tty", "ConsoleSize"})
_NETWORK_CONNECT_FIELDS = frozenset({"Container", "EndpointConfig"})


def _fold(key: str) -> str:
    """键名的折叠形态。`casefold` 覆盖 Go 的简单折叠（含 U+017F -> s、U+212A -> k）；折得更狠只会多拒，不会漏。"""
    return key.casefold()


def _check_keys(
    obj: Dict[str, Any],
    allowed: frozenset[str],
    where: str,
    rule_ns: str,
    flat_fields: frozenset[str] = frozenset(),
) -> Optional[Decision]:
    """按精确键白名单校验一层结构，见上面的说明。

    Args:
        obj: 被校验的那一层（JSON 对象）
        allowed: 这一层允许的精确键名
        where: 人话位置（进拒绝原因）
        rule_ns: 规则名前缀（`create` / `exec` / `exec_start` / `network_connect`）
        flat_fields: 「属于另一层」的字段名；本层出现它们（精确或仅大小写不同）= 把下一层的字段摊平写在了
            这一层。create 顶层用它抓 Engine ≤26 的扁平 HostConfig 写法。
    """
    folded_allowed = {_fold(name): name for name in allowed}
    folded_flat = {_fold(name): name for name in flat_fields if name not in allowed}
    for key in obj:
        if key in allowed:
            continue
        folded = _fold(key)
        if folded in folded_allowed:
            return Decision(
                False,
                f"{where} 的键 {key!r} 只是 {folded_allowed[folded]!r} 的大小写变体："
                "Docker 按大小写不敏感解码，而判据只认精确键，两边会对不上，一律拒绝",
                f"{rule_ns}.key_case_variant",
            )
        if key in flat_fields or folded in folded_flat:
            return Decision(
                False,
                f"{where} 出现了 HostConfig 的字段 {key!r}：Engine ≤26 在没有 HostConfig 键时会让顶层的这些字段生效，拒绝",
                f"{rule_ns}.hostconfig_flat",
            )
        return Decision(False, f"{where} 出现未知键 {key!r}，拒绝", f"{rule_ns}.key_unknown")
    return None


def _check_restart_policy(host_config: Dict[str, Any]) -> Optional[Decision]:
    """带 Binds 的容器只允许不自动重启（`RestartPolicy.Name` 为空或 `no`）。

    **restart policy 触发的重启由 Docker 守护进程自己做，不经过 guard**，所以 start / restart 前的挂载源复核
    管不到它：创建一个 `always` 且绑 `<数据目录>/x` 的容器，之后把 `x` 换成指向 `/` 的链接，再等容器退出
    （或自己让它退出），守护进程重启它时会重新解析挂载源、挂上宿主根目录，guard 一无所知。
    所以带挂载的容器不许把「重启」这个动作交给守护进程；需要保活的由 NA 经 guard 重新拉起
    （见 `container_manager` 的 reconcile）。没有 Binds 的容器没有挂载源可换，不受限。
    """
    policy = host_config.get("RestartPolicy")
    if policy is None:
        return None
    if not isinstance(policy, dict):
        return Decision(False, "HostConfig.RestartPolicy 格式非法", "create.restart_policy.malformed")
    keys_denial = _check_keys(policy, _RESTART_POLICY_FIELDS, "HostConfig.RestartPolicy", "create")
    if keys_denial is not None:
        return keys_denial
    name = policy.get("Name", "")
    if host_config.get("Binds") and name not in ("", "no"):
        return Decision(
            False,
            f"带 Binds 的容器不允许 RestartPolicy={name!r}：守护进程自己触发的重启不经过 guard，"
            "挂载源在容器退出期间被换成链接就会逃出数据目录；请用 RestartPolicy=no，保活交给 NA 经 guard 拉起",
            "create.restart_policy.with_binds",
        )
    return None


def evaluate_create_body(body: Dict[str, Any], ctx: PolicyContext) -> Decision:
    """对 `POST /containers/create` 的请求体做逐字段判据。"""
    # 键名先于一切：下面所有 `.get("Xxx")` 只认精确键，必须先确认没有「Docker 会当作同一个键」的变体
    top_denial = _check_keys(body, _CREATE_TOP_FIELDS, "create 请求体顶层", "create", flat_fields=_HOST_CONFIG_FIELDS)
    if top_denial is not None:
        return top_denial

    host_config = body.get("HostConfig")
    if host_config is None:
        host_config = {}
    if not isinstance(host_config, dict):
        return Decision(False, "HostConfig 格式非法", "create.hostconfig.malformed")
    hc_denial = _check_keys(host_config, _HOST_CONFIG_FIELDS, "HostConfig", "create")
    if hc_denial is not None:
        return hc_denial

    for check in (
        _check_privilege_flags(host_config),
        _check_security_opt(host_config),
        _check_mounts(host_config),
        _check_restricted_fields(host_config),
        _check_networking_config(body),
        _check_restart_policy(host_config),
    ):
        if check is not None:
            return check
    bind_denial, forwarded_binds = _vet_binds(host_config, ctx)
    if bind_denial is not None:
        return bind_denial
    caps_denial = _check_caps_and_network(body, host_config)
    if caps_denial is not None:
        return caps_denial

    # 转发的是判过的这一份（Binds 已换成规范路径），不是客户端发来的原始字节
    rewritten = copy.deepcopy(body)
    if forwarded_binds is not None:
        rewritten["HostConfig"]["Binds"] = forwarded_binds
    return Decision(True, "create 请求体通过全部安全判据", "containers.create", rewritten_body=rewritten)


def evaluate_container_bind_sources(sources: Sequence[str], ctx: PolicyContext) -> Decision:
    """start / restart 前对**已存在容器**的 bind source 复核（`proxy` 用 inspect 取出来后传入）。

    Docker 在 start 时才解析 bind source。create 时判过的目录，到 start 时可能已被换成指向别处的链接
    ——持有数据目录写权限的一方 create 之后、start 之前换一下就够了，创建时校验堵不住。
    所以 start / restart 再判一遍，并且**要求规范路径与 create 时记下的完全一致**：
    变了就是被换过。

    只复核落在允许根之内的 source：根之外的挂载（NA 自己容器的运维挂载、`/etc/localtime`）要么是运维
    给的、要么 create 时就已被拒绝，不归这条判据管，否则 `restart_self` 会被运维自己的挂载误伤。
    """
    if len(sources) > MAX_RECHECK_SOURCES:
        return Decision(False, f"挂载源条数过多（{len(sources)} > {MAX_RECHECK_SOURCES}），无法在预算内复核", "start.binds.too_many")
    budget = ResolveBudget()
    for source in sources:
        resolution = resolve_bind_source(source, ctx.allowed_bind_roots, ctx.fs, budget)
        if not resolution.ok:
            if resolution.rule == RULE_OUTSIDE:
                continue
            return Decision(False, resolution.reason, f"start.binds.{resolution.rule}")
        if resolution.resolved != _norm_source(source):
            return Decision(
                False,
                f"挂载源 {source} 现在解析到 {resolution.resolved}（创建之后被换成了链接？），拒绝启动",
                "start.binds.changed",
            )
    return Decision(True, "挂载源复核通过", "containers.start")


def invisible_bind_roots(ctx: PolicyContext) -> List[str]:
    """guard 视角里看不到（不存在 / 不是目录）的允许根——这些根之下的 bind 会被 fail-closed 拒绝。

    启动时调用，让「边车没把数据目录挂进来」在日志里一眼可见，而不是等沙盒第一次起不来才发现。
    """
    return [root for root in ctx.allowed_bind_roots if not ctx.fs.stat_is_dir(_norm_source(root))]


def evaluate_exec_create_body(body: Optional[Dict[str, Any]]) -> Decision:
    """对 `POST /containers/{id}/exec` 的请求体做判据。

    exec 的请求体里有一个和 create 同名同义的 `Privileged` 字段——**create 那侧逐字段判到死、
    这侧却一个字段都不看**，是同一类判据漏掉的第二个入口。`exec --privileged` 会给这次执行
    补齐全部 capability 并**摘掉 seccomp / AppArmor 限制**，把一个已经被收敛的沙盒容器
    重新变成半个特权容器。NA 自己的两处 exec（`services/mcp/validator.py` 的 stdio 校验、
    CC 终端）都不设这个字段。
    """
    if body is None:
        # 没有请求体的 exec 创建请求本来就不合法（Cmd 是必填），按判不了处理
        return Decision(False, "exec 创建请求缺少可解析的 JSON 请求体", "exec.body.missing")
    # 真实客户端的两处“不规范”写法（实测 aiodocker 0.25 / docker-py 7.1.0 的 exec 请求体）：
    #   - 两者都带 `Container`（旧版 ExecConfig 的字段，容器由 URL 决定，请求体里的值守护进程不采信）；
    #   - aiodocker 把 `DetachKeys` 写成小写开头的 `detachKeys`（靠 Go 的大小写不敏感才生效）。
    # 只放行这两个，且转发前规范化：`Container` 丢掉（URL 才是容器的唯一来源，请求体里多一个只会制造歧义），
    # `detachKeys` 改写成 `DetachKeys`。`DetachKeys` 与 `detachKeys` 同时出现 = 同键两写，按变体拒绝。
    candidate = dict(body)
    if "detachKeys" in candidate:
        if "DetachKeys" in candidate:
            return Decision(False, "exec 请求体同时带 DetachKeys 与 detachKeys", "exec.key_case_variant")
        candidate["DetachKeys"] = candidate.pop("detachKeys")
    container_ref = candidate.pop("Container", None)
    if container_ref is not None and not isinstance(container_ref, str):
        return Decision(False, "exec 请求体的 Container 不是字符串", "exec.key_unknown")
    # `{"Cmd":["sh"],"privileged":true}`：Docker 的 Go 解码大小写不敏感，判据只读精确键 `Privileged`
    keys_denial = _check_keys(candidate, _EXEC_CREATE_FIELDS, "exec 请求体", "exec")
    if keys_denial is not None:
        return keys_denial
    if candidate.get("Privileged"):
        return Decision(False, "禁止 Privileged exec", "exec.privileged")
    # 转发判过的结构（消除重复键，已规范化）
    return Decision(True, "exec 创建请求体通过判据", "containers.exec_create", rewritten_body=copy.deepcopy(candidate))


def evaluate_exec_start_body(body: Optional[Dict[str, Any]]) -> Decision:
    """对 `POST /exec/{id}/start` 的请求体做判据。

    `Detach=false` 的 exec 走的是 **HTTP 劫持（`Connection: Upgrade` + 裸双向流）**，
    而 guard 是一层普通的 HTTP 反向代理：它剥掉逐跳头、把上游响应重新按分块编码发回，
    既没有把客户端后续写入的 stdin 送上去的通道，也会把 docker 的 raw-stream 帧和分块
    帧混在一起。结果不是「能用但不安全」，而是**挂住或输出乱码**——§2.6 教训②说得很清楚，
    不可诊断的挂起比明确失败糟得多。所以这里明确拒绝并说清原因。

    影响的两条产品路径（**硬化部署下不可用**，已写进 docs/docker-guard.md）：
    `services/mcp/validator.py` 的 stdio 校验、`routers/workspaces.py` 的 CC 终端。
    `Detach=true`（沙盒不用、NA 侧目前没有调用方）是普通请求-响应，照常放行。
    """
    if body is not None:
        # `{"Detach":true,"detach":false}`：判据读到 true 放行，Docker 取后者 false 走劫持流——大小写变体一律拒绝
        keys_denial = _check_keys(body, _EXEC_START_FIELDS, "exec start 请求体", "exec_start")
        if keys_denial is not None:
            return keys_denial
    if body is not None and body.get("Detach") is True:
        return Decision(True, "分离式 exec 启动", "exec.start", rewritten_body=copy.deepcopy(body))
    return Decision(
        False,
        "guard 不支持 HTTP 劫持流（Detach=false 的 exec）：硬化部署下 CC 终端与 MCP stdio 校验不可用，"
        "详见 docs/docker-guard.md",
        "exec.start.hijack_unsupported",
    )


def evaluate_update_body(body: Optional[Dict[str, Any]]) -> Decision:
    """对 `POST /containers/{id}/update` 的请求体做判据：只允许把 RestartPolicy 改成 `no`。

    Docker 的 update 请求体是「资源字段（摊平）+ RestartPolicy」。放开它的其它字段没有任何 NA 用途，
    还会引入新的变更面（改 cgroup 配额、改重启策略为 always——那正是 `_check_restart_policy` 关掉的通道）。
    """
    if body is None:
        return Decision(False, "update 请求缺少可解析的 JSON 请求体", "update.body.missing")
    keys_denial = _check_keys(body, frozenset({"RestartPolicy"}), "update 请求体", "update")
    if keys_denial is not None:
        return keys_denial
    policy = body.get("RestartPolicy")
    if not isinstance(policy, dict):
        return Decision(False, "update 只允许修改 RestartPolicy", "update.restart_policy.required")
    inner_denial = _check_keys(policy, _RESTART_POLICY_FIELDS, "update.RestartPolicy", "update")
    if inner_denial is not None:
        return inner_denial
    if policy.get("Name") != "no" or policy.get("MaximumRetryCount") not in (None, 0):
        return Decision(False, "update 只允许把 RestartPolicy 改成 no", "update.restart_policy.only_no")
    return Decision(True, "update 请求体通过判据", "containers.update", rewritten_body=copy.deepcopy(body))


def evaluate_network_connect_body(body: Optional[Dict[str, Any]]) -> Decision:
    """对 `POST /networks/{id}/connect` 的请求体做判据。

    归属门读的是 `Container` 字段，而 Docker 大小写不敏感地解码：`{"Container":"owned1","container":"victim"}`
    让归属门看到自己的 `owned1`，Docker 连的却是 `victim`（后者覆盖前者）。所以先按精确键校验，
    变体一律拒绝；转发的也是判过的这一份。
    """
    if body is None:
        return Decision(False, "network connect 请求缺少可解析的 JSON 请求体", "network_connect.body.missing")
    keys_denial = _check_keys(body, _NETWORK_CONNECT_FIELDS, "network connect 请求体", "network_connect")
    if keys_denial is not None:
        return keys_denial
    container = body.get("Container")
    if not isinstance(container, str) or not container:
        return Decision(False, "network connect 缺少 Container 引用", "network_connect.container.missing")
    return Decision(True, "network connect 请求体通过判据", "networks.connect", rewritten_body=copy.deepcopy(body))


def network_connect_target(method: str, raw_path: str, body: Optional[Dict[str, Any]]) -> Optional[str]:
    """`POST /networks/{id}/connect` 请求体里指向的容器引用（需要归属校验）。

    归属门是按 **URL 里的容器 ID** 装的，而这个端点把容器引用放在**请求体**里——同一类判据的
    第二个出口。不校验的话，攻击者可以把宿主上**别人的**容器接进 NA 的沙盒/工作区网络里
    （或反过来），拿到一条本不存在的横向通路。NA 自己只会连自己的容器。
    """
    if method.upper() != "POST":
        return None
    path = normalize_path(raw_path)
    if not re.match(rf"^/networks/{_CID}/connect$", path):
        return None
    if not isinstance(body, dict):
        return None
    container = body.get("Container")
    return container if isinstance(container, str) and container else None


def is_safe_container_ref(ref: str) -> bool:
    """容器引用是否只含 Docker 容器名/ID 的合法字符。

    URL 路径里的引用由端点正则保证；**请求体里的引用（`/networks/{id}/connect` 的 `Container`）
    不受任何约束**——代理要拿它去拼一条 inspect URL 判归属，放行 `/` 或 `..` 就等于让攻击者
    操纵那条 URL 去 inspect **另一个**（自己名下的）容器，从而骗过归属判定。
    不合法的引用一律当作「归属无法确证」处理。
    """
    return bool(re.fullmatch(_CID, ref))


def body_guard_rule(method: str, raw_path: str) -> str:
    """该请求是否需要解析请求体做判据；返回判据名（空串表示不需要）。

    代理据此决定要不要读+解析请求体——判据侧与代理侧共用同一张端点表，
    避免「表里加了一条、代理忘了给它读 body」这种接线漏。
    """
    method = method.upper()
    path = normalize_path(raw_path)
    for ep in _ENDPOINTS:
        if ep.method == method and ep.pattern.match(path):
            if ep.guard_create_body:
                return "create"
            return ep.guard_body_rule
    return ""


def body_forbidden(method: str, raw_path: str) -> bool:
    """该请求是否**不许带请求体**（start / restart）。代理据此在转发前拒绝非空体。"""
    method = method.upper()
    path = normalize_path(raw_path)
    for ep in _ENDPOINTS:
        if ep.method == method and ep.pattern.match(path):
            return ep.forbid_body
    return False


def evaluate(
    method: str,
    raw_path: str,
    ctx: PolicyContext,
    body: Optional[Dict[str, Any]] = None,
) -> Decision:
    """判定单个 Docker API 请求是否放行。

    Args:
        method: HTTP 方法（大小写不敏感）
        raw_path: 原始请求路径（可带版本前缀与查询串）
        ctx: 部署上下文
        body: 已解析的 JSON 请求体（仅 create 需要；其余端点忽略）

    Returns:
        Decision：allow=False 时代理回 403，不接触宿主 docker.sock
    """
    method = method.upper()
    path = normalize_path(raw_path)

    version = api_version_of(raw_path)
    if version is not None and version < MIN_API_VERSION:
        return Decision(
            False,
            f"API 版本 v{version[0]}.{version[1]} 低于 v{MIN_API_VERSION[0]}.{MIN_API_VERSION[1]}：旧版本的 start 会把请求体当 "
            "HostConfig 解码，绕过 create 时的全部判据，拒绝",
            "endpoint.api_version_too_old",
        )

    # 相对段必须在端点匹配**之前**挡掉：判据看的路径与最终发给 Docker 的路径一旦能不一致，
    # 整张白名单就失效（实测绕过详见 `path_has_dot_segments`）。
    if path_has_dot_segments(path):
        return Decision(False, f"路径含相对段（`.`/`..`），拒绝: {method} {path}", "endpoint.path_traversal")

    matched: Optional[_Endpoint] = None
    for ep in _ENDPOINTS:
        if ep.method == method and ep.pattern.match(path):
            matched = ep
            break

    if matched is None:
        return Decision(False, f"端点不在白名单内: {method} {path}", "endpoint.denied")

    if matched.guard_create_body:
        if body is None:
            return Decision(False, "create 请求缺少可解析的 JSON 请求体", "create.body.missing")
        return evaluate_create_body(body, ctx)

    if matched.guard_body_rule == "exec_create":
        return evaluate_exec_create_body(body)
    if matched.guard_body_rule == "exec_start":
        return evaluate_exec_start_body(body)
    if matched.guard_body_rule == "network_connect":
        return evaluate_network_connect_body(body)
    if matched.guard_body_rule == "update_restart_policy":
        return evaluate_update_body(body)

    return Decision(True, "端点在白名单内", matched.rule)


# ---------------------------------------------------------------------------
# 容器归属判据（挡「exec 进别人的容器」这类越权）
# ---------------------------------------------------------------------------


def container_ownership_target(method: str, raw_path: str) -> Optional[str]:
    """若该请求按 ID 指向某个具体容器且需要归属校验，返回容器引用；否则返回 None。

    仅对 `container_scoped=True` 的端点（exec/logs/inspect/start/stop/restart/kill/wait/delete）
    生效。`/containers/json`（列举）、`/containers/create`（尚无 ID）、以及 exec 句柄 / 镜像 /
    网络端点都返回 None——它们要么不指向单个容器，要么已被别的判据覆盖。

    返回的引用可能是容器 ID（完整或短）或名称，由代理拿去 inspect 出 Labels 判归属。
    """
    method = method.upper()
    path = normalize_path(raw_path)
    if path_has_dot_segments(path):
        # 这类路径在 `evaluate()` 里已被 deny；这里同样不给出目标，避免本函数被单独复用时
        # 对一条会被改写的路径算出「安全」的归属目标。
        return None
    for ep in _ENDPOINTS:
        if ep.method == method and ep.container_scoped:
            m = ep.pattern.match(path)
            if m:
                return m.group("cid")
    return None


def bind_recheck_target(method: str, raw_path: str) -> Optional[str]:
    """该请求是否会让 Docker 重新解析某个已存在容器的 bind source（start / restart）；是则返回容器引用。

    与 `container_ownership_target` 同源同判（共用端点表），只是多一个 `recheck_binds` 标记。
    含相对段的路径同样不给目标（那类请求在 `evaluate()` 里已被 deny）。
    """
    method = method.upper()
    path = normalize_path(raw_path)
    if path_has_dot_segments(path):
        return None
    for ep in _ENDPOINTS:
        if ep.method == method and ep.recheck_binds:
            m = ep.pattern.match(path)
            if m:
                return m.group("cid")
    return None


def labels_indicate_ownership(labels: Any) -> bool:
    """容器的 Labels 是否表明它由 NA（本部署）创建。

    判据：存在任意一个以 `nekro.` 开头的标签键。NA 建的四类容器都带这类键
    （沙盒 `nekro.sandbox.instance`、NapCat `nekro.napcat.instance`、netguard `nekro.netguard`、
    CC 工作区 `nekro.owner.scope`）；宿主上任何第三方 / 兄弟容器（postgres、别的项目、
    挂了 docker.sock 的容器）都不带，于是被判为「非本部署」并在代理层拒绝。

    ⚠️ **第五类容器：NA 自己**。它不是 NA 建的，而是 compose / 运维建的，因此没有任何东西
    会自动给它打标签——而 NA 有三条路径要 inspect/restart **自己这个容器**：
    `tools/docker_util.py:restart_self`（后台「重启系统」）、
    `services/sandbox/network_guard.py:resolve_self_container`（取自身镜像去起 iptables 临时
    容器；认不出就退化成告警，网络隔离静默失效）、
    `services/console/container_manager.py:_resolve_nekro_network`（探测自身所在网络，
    认不出就回落到硬编码的 `nekro_network`，NapCat 会被接到错的网络上）。
    所以**硬化部署的 compose 必须给 NA 服务本身打上 `nekro.owner.scope` 标签**
    （见 docker/docker-compose-x-hardened.yml 与 docs/docker-guard.md），
    否则归属门会把 NA 对自己的这三次调用一并挡掉。

    只认前缀、不认具体值：值随实例 / 部署而变，用前缀即可把「NA 的容器」与「别人的容器」分开。
    跨两套硬化部署之间的更细归属（防 A 的 NA 被攻破后 exec 进 B 的容器）是登记在案的残余项，
    见 docs/docker-guard.md 与 compose 注释。
    """
    if not isinstance(labels, dict):
        return False
    return any(isinstance(k, str) and k.startswith(OWNERSHIP_LABEL_KEY_PREFIX) for k in labels)
