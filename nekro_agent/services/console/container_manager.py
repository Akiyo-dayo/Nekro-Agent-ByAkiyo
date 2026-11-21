"""NapCat 协议端容器的直接管理

## 为什么这一层必须比普通业务代码更保守

NA 容器挂载了 `/var/run/docker.sock`，对 Docker 守护进程的调用等价于宿主机 root 权限。
本模块把「创建 / 删除容器」这类能力接到了 HTTP API 上，因此任何一处让用户输入流入
Docker 调用的缝隙，都会把一次越权访问放大成宿主机沦陷。

模块内因此建立了五道硬边界，全部在服务层完成、不依赖路由层的自觉：

1. **镜像白名单**：仓库名恒为 `mlikiowa/napcat-docker`，只有 tag 可配且需通过正则校验。
2. **容器名模板**：容器名由 `instance_key` 按固定模板生成（带部署前缀，见
   `build_container_name`），永不接受调用方传入的容器名。
3. **归属标签**：创建时写入 `nekro.napcat.instance` / `nekro.napcat.owner`；
   所有破坏性操作（start/stop/restart/delete）前先 inspect 校验标签，不匹配即拒绝。
   归属标签的取值口径见 `get_owner_tag()` 与下面的「过渡期双识别」。
4. **端口只绑回环**：WebUI 端口绑定 `127.0.0.1`，不经反代不会暴露到公网。
5. **挂载路径后端生成**：全部派生自 `{DATA_DIR}/napcat_data/{instance_key}/`，不接受用户输入。

## 二维码获取的主备方案

主方案走 NapCat WebUI 的 HTTP API（读容器配置目录下的 `webui.json` 拿 token 换取凭证）；
WebUI 尚未就绪或接口形态变化时，退化为扫描容器日志提取登录链接。两者都失败时返回
`available=False` 并附带人类可读的 `message`，而不是抛异常——扫码页面需要的是状态，不是 500。

## 登录成功后的账号回写

扫码登录成功后，NapCat 会带着这个 QQ 号反向连回 NA。此前**没有任何一处代码**把号码写回
发起这次登录的那个实例，于是 `resolve_instance_key()` 在入站第一步查不到绑定，把协议端
自动登记成另一行 `ob{QQ}` 未认领实例 —— 用内置容器一键扫码，照样裂成两行、照样要手工认领。

本模块提供缺失的那一环：**「这个容器现在登录的是哪个账号」**（`read_container_login_account`）
与「把账号写回该实例」（`bind_login_account`）。归属推导不猜、不靠日志文本，直接问容器自己的
WebUI；容器与实例的对应关系是 NA 亲手建立的（容器名模板 + 归属标签），所以这条链上没有歧义。

## 归属标签与容器名的过渡期双识别（**有明确的删除条件**）

`nekro.napcat.owner` 的取值从 `OsEnv.INSTANCE_NAME or "default"` 换成了
`core/os_env.py:deployment_scope()`（与 iptables 标记 / 沙盒容器归属标签同一套派生）；
容器名同样从裸模板名 `nekro_napcat_{instance_key}` 换成了
`{deployment_scope()}nekro_napcat_{instance_key}`——两套部署上出现同名 `instance_key`
时，裸模板名会撞 Docker 的全局容器名（后建的一方 `create_container` 抛 409），
前缀让两套部署的名字结构上不可能相同（与沙盒网络名 `{作用域}nekro_sandbox_net` 同一风格）。

直接切过去会让**升级前建的存量容器立刻找不回来 / 变成「不归本部署所有」**——
`_assert_owned` 是 fail-closed 的，控制台对它们的启 / 停 / 重启 / 删全部 403；
按名取容器的路径只认新名的话，它们还会在升级当天集体 404。

所以这里两件事都**读认新旧两套口径，写只写新的**：

- 归属标签：见 `_assert_owned` / `create_container`；
- 容器名：按名取容器一律走 `container_name_candidates()`（新名在前、裸模板名在后），
  `_assert_owned` 的名字维度对新旧两个模板名都放行。

存量容器的收口出口不变：控制台把该实例的容器「删除」再「创建」一次，即换成新名与新标签。

## 容器名长度：超过 63 改用稳定短名（#46 复核补上）

容器名在 compose 网络里就是主机名（`_webui_base_url` 在容器内拼 `http://{容器名}:6099`），
DNS 单段最长 63。派生前缀最长 22 + 模板 13 + 实例标识最长 32 = 67 会超，显式的
`NEKRO_INSTANCE_NAME` 更长。展开后超过 63 的名字改用 `{部署前缀}nekro_napcat_{哈希}`
（`build_container_name`，哈希只依赖前缀与实例标识，稳定可复算）；不超长的一律原样不变。
按展开形态建过的 64~67 字符长名仍在读认范围内（`build_unshortened_container_name`，候选表第二位）。
"""

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse

import aiodocker
import httpx
from aiodocker.containers import DockerContainer
from aiodocker.exceptions import DockerError
from pydantic import BaseModel, ConfigDict, Field
from tortoise.transactions import in_transaction

from nekro_agent.adapters.onebot_v11.core.instance import clear_instance_cache
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import (
    OsEnv,
    deployment_scope,
    resolve_deployment_onebot_access_token,
    resolve_self_hostname,
)
from nekro_agent.models.db_adapter_instance import NON_DISPATCHABLE_STATUSES, DBAdapterInstance
from nekro_agent.schemas.console import (
    ContainerInfo,
    ContainerState,
    InstanceProvider,
    LoginQrCodeResponse,
    NetworkConfig,
    NetworkEntry,
    NetworkEntryType,
)
from nekro_agent.schemas.errors import (
    ConflictError,
    NotFoundError,
    OperationFailedError,
    PermissionDeniedError,
    ValidationError,
)
from nekro_agent.tools.common_util import UnsafeDownloadUrlError, assert_download_url_safe

logger = get_sub_logger("console.container")

# ---------------------------------------------------------------------------
# 安全边界常量
# ---------------------------------------------------------------------------

NAPCAT_IMAGE_REPOSITORY = "mlikiowa/napcat-docker"
"""唯一允许创建的镜像仓库。调用方永远只能影响 tag，不能影响仓库名。"""

DEFAULT_IMAGE_TAG = "latest"
CONTAINER_NAME_TEMPLATE = "nekro_napcat_{instance_key}"
"""容器名的模板段。

新建容器的名字是 `{deployment_scope()}` 前缀 + 这段模板（见
`build_container_name`）；升级前建的存量容器用的则是裸模板名（见
`build_legacy_container_name`），读认路径要两个都认（过渡期双识别的容器名那一半）。
"""

CONTAINER_NAME_MAX_LENGTH = 63
"""容器名总长上限：DNS 单段（label）最长 63 个字符。

容器在 compose 网络里靠容器名当主机名被访问（`_webui_base_url`），超过这个长度的名字
Docker 照样接受、但主机名解析不了——建出来一个控制台管得到、NA 却连不上的容器。
"""

CONTAINER_NAME_HASH_LENGTH = 10
"""名字超长时，实例段换成的哈希位数（十六进制）。40 bit，同一部署里实例标识碰撞的概率可忽略。"""

_DOCKER_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
"""Docker 容器名的合法字符集（首字符必须是字母或数字）。"""
LABEL_INSTANCE = "nekro.napcat.instance"
LABEL_OWNER = "nekro.napcat.owner"

LEGACY_OWNER_TAG_FALLBACK = "default"
"""`NEKRO_INSTANCE_NAME` 未设置时，**升级前**那套口径给出的归属标签。

两种标准部署下这个变量默认都是空串，所以同宿主上的每一套部署都把标签写成了逐字相同的
`default` —— 这正是双识别必须加限定的原因，见 `_container_belongs_to_this_deployment()`。
"""

CONTAINER_DATA_DIR = "/app/nekro_agent_data"
CONTAINER_UPLOADS_DIR = f"{CONTAINER_DATA_DIR}/uploads"
CONTAINER_QQ_STATE_DIR = "/app/.config/QQ"
CONTAINER_NAPCAT_CONFIG_DIR = "/app/napcat/config"
"""容器内的挂载点。

`CONTAINER_DATA_DIR` 是协议端眼里 NA 数据目录的位置（配置项 `SANDBOX_ONEBOT_SERVER_MOUNT_DIR`
的默认值）。NA 交给协议端发送的文件都先放进数据目录下的 `uploads`，所以只把它只读挂到
`CONTAINER_UPLOADS_DIR`；升级前建的容器读写挂着整个数据目录（包括 CC 工作区），重建一次就换成
新的挂法。

抽成常量而不是在 `create_container` 里写字面量：接受旧标签时要按**目标路径**把宿主机侧的
源路径找回来（见 `_bind_source`），两边一旦分叉，判据会静默退化成「永远找不到挂载」，
表现是存量容器全部 403 —— 而那恰好是双识别要消灭的症状。
"""

NAPCAT_WEBUI_PORT = 6099
# NapCat WebUI 登录哈希的固定盐值，取自其前端实现：SHA256(token + ".napcat")
NAPCAT_AUTH_SALT = ".napcat"
"""NapCat 容器内 WebUI 端口，见 docker/docker-compose-x-napcat.yml"""

WEBUI_HOST_PORT_RANGE_START = 36000
WEBUI_HOST_PORT_RANGE_END = 36999

INSTANCE_KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")
"""与 OneBot V11 会话标识约束保持一致：排除 `-` 与路径分隔符，同时可安全用于容器名与挂载路径"""

IMAGE_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")

_CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_DOCKER_STATE_MAP: Dict[str, ContainerState] = {
    "running": ContainerState.RUNNING,
    "created": ContainerState.CREATED,
    "restarting": ContainerState.RESTARTING,
    "paused": ContainerState.PAUSED,
    "exited": ContainerState.EXITED,
    "dead": ContainerState.EXITED,
    "removing": ContainerState.EXITED,
}

# ---------------------------------------------------------------------------
# 走不走 docker-guard：决定 NapCat 容器的保活方式
# ---------------------------------------------------------------------------

DOCKER_GUARD_ACTIVE_ENV = "NEKRO_DOCKER_GUARD_ACTIVE"
"""显式声明「NA 的 Docker 调用经过 docker-guard」：`1/true/yes/on` 是，`0/false/no/off` 否。

不设时按 `DOCKER_HOST` 推断（见 `docker_guard_in_path`）。硬化 compose 两样都给了。
"""

DESIRED_RUNNING = "running"
DESIRED_STOPPED = "stopped"
"""实例元数据 `desired_state` 的取值：运维（或 NA）希望这个容器处于什么状态。只在走 guard 时才记录。"""

RECONCILE_MIN_DELAY_SECONDS = 30.0
RECONCILE_MAX_DELAY_SECONDS = 600.0
RECONCILE_STABLE_SECONDS = 120.0
RECONCILE_INSTANCE_TIMEOUT_SECONDS = 30.0
"""拉起退避区间（每次拉起无论成败都翻倍）；拉起后跑满 `RECONCILE_STABLE_SECONDS` 才清零；单个实例一轮的时限。"""


@dataclass
class _ReconcileState:
    next_try: float  # 下次允许拉起的 monotonic 时刻
    delay: float  # 当前退避秒数
    started_at: Optional[float]  # 最近一次成功拉起的时刻（None = 最近一次是失败 / 超时）


_reconcile_state: Dict[str, _ReconcileState] = {}
"""实例标识 -> 保活状态。进程内状态，重启即清零。"""


def docker_guard_in_path() -> bool:
    """NA 的 Docker 调用是否经过 docker-guard（`docs/docker-guard.md`）。

    先看显式的 `NEKRO_DOCKER_GUARD_ACTIVE`；没设再看 `DOCKER_HOST`：硬化 compose 把它指向
    `unix:///run/nekro-guard/guard.sock`，路径里含 `nekro-guard` 或以 `guard.sock` 结尾就算走 guard。
    标准部署（直挂 docker.sock、不设 `DOCKER_HOST`）恒为 False，下面所有因它分支的行为都等于没有。
    """
    explicit = os.environ.get(DOCKER_GUARD_ACTIVE_ENV, "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    host = os.environ.get("DOCKER_HOST", "").strip().lower()
    return "nekro-guard" in host or host.endswith("guard.sock")


def napcat_restart_policy() -> str:
    """新建 NapCat 容器的 `RestartPolicy.Name`。

    标准部署（不走 guard）逐字保持 `unless-stopped`。走 guard 时必须是 `no`：带 Binds 的容器若交给 Docker
    守护进程自动重启，重启时的挂载源解析不经过 guard（容器退出期间把挂载源换成链接即可逃出数据目录，
    见 `docker_guard.policy._check_restart_policy`），guard 会拒绝这种 create。保活改由 NA 经 guard 重新拉起，
    见 `reconcile_container_liveness`。
    """
    return "no" if docker_guard_in_path() else "unless-stopped"


IMAGE_PULL_TIMEOUT_SECONDS = 900
NAPCAT_HTTP_TIMEOUT_SECONDS = 5.0
QRCODE_LOG_SCAN_LINES = 400

# 「这个容器登录的是谁」这一问的超时，比通用值短得多。
#
# 它跑在**入站链路**上（`resolve_instance_key` 冷查未命中时），而候选实例可能有好几个：
# 沿用 5 秒会把一次协议端首连的解析拖成十几秒。目标地址不是回环就是同网桥内的容器名，
# 容器没起来时是秒回的 ECONNREFUSED / DNS 失败，2.5 秒足够覆盖正常往返。
NAPCAT_LOGIN_PROBE_TIMEOUT_SECONDS = 2.5

# 归属推导失败后的重试节奏（秒）。
#
# 存在的理由是 NapCat 内部的一条固定顺序，取自镜像内 `napcat.mjs` 的登录收尾语句：
# `await networkManager.openAllAdapters(), …, ve.setQQLoginInfo(e), ve.setQQLoginStatus(!0)`
# —— **反向 WS 先开，登录账号后公布**。也就是说 NA 的 `on_bot_connect` 理论上可能早于
# WebUI 能回答「我登录的是谁」。中间隔着的只是几条同步语句，而 NA 这边要走完
# 「握手 → 事件分发 → 查库 → 回连 WebUI」，实测量级差着好几个数量级；但这条竞态确实存在，
# 且撞上一次的代价就是这次修复要消灭的那一行 `ob{QQ}`，所以宁可多等这两拍。
#
# 只在「有候选容器还答不上账号」时才重试：候选都已明确答出别的账号时再等也不会变。
LOGIN_ATTRIBUTION_RETRY_DELAYS: Tuple[float, ...] = (0.3, 0.7)

_QRCODE_URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+")
# NapCat 当前输出的登录链接域名是 txz.qq.com，历史版本用过 qm/id/qr.qq.com，全部保留
_QRCODE_URL_HINTS = ("qrcode", "qr.qq.com", "qm.qq.com", "id.qq.com", "txz.qq.com", "ssl.ptlogin2")

_resolved_nekro_network: str = ""
_resolve_network_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# 标识与路径（全部由后端派生，不接受调用方输入）
# ---------------------------------------------------------------------------


def validate_instance_key(instance_key: str) -> str:
    """校验实例标识是否落在安全字符集内。

    该值会进入容器名与宿主机挂载路径，必须在任何 Docker 调用之前收敛，
    否则 `../` 之类的输入可以把挂载点指到 DATA_DIR 之外。
    """
    if not INSTANCE_KEY_PATTERN.match(instance_key):
        raise ValidationError(reason=f"实例标识 '{instance_key}' 非法，仅允许小写字母、数字与下划线，最长 32 位")
    return instance_key


def build_unshortened_container_name(instance_key: str) -> str:
    """`deployment_scope()` 前缀 + 模板段的**原样展开形态**，不看长度。

    这是引入稳定短名之前 `build_container_name` 的全部口径。名字总长超过 `CONTAINER_NAME_MAX_LENGTH`
    （63）时，当前口径改用稳定短名（见 `build_container_name`），而按这个口径建出来的容器
    （名字 64~67 字符，Docker 本身接受）仍在宿主上——所以它只用于**读认**，永不用于新建，
    与 `build_legacy_container_name` 同一类过渡期措施。名字不超长时它与 `build_container_name` 逐字相同。
    """
    key = validate_instance_key(instance_key)
    name = f"{deployment_scope()}{CONTAINER_NAME_TEMPLATE.format(instance_key=key)}"
    if not _DOCKER_NAME_PATTERN.match(name):
        raise ValidationError(reason=f"生成的容器名 {name!r} 不符合 Docker 命名字符集，请检查 NEKRO_INSTANCE_NAME 的取值")
    return name


def _shorten_container_name(scope: str, instance_key: str) -> str:
    """把超长的容器名收成不超过 `CONTAINER_NAME_MAX_LENGTH` 的稳定短名：保留部署前缀、实例段换成哈希。

    形如 `{scope}nekro_napcat_{哈希}`。哈希取 `sha256(scope + NUL + instance_key)` 的前
    `CONTAINER_NAME_HASH_LENGTH` 位十六进制：**只依赖这两个输入**（不掺 pid / 时间 / 随机数），
    同一套部署重启前后算出同一个名字——否则每次重启换一个名字，旧容器全变成孤儿。
    实例标识仍完整写在容器标签 `nekro.napcat.instance` 上，`_assert_owned` 认的是标签，
    孤儿盘点也是读标签取实例标识，所以名字里不必再带它。

    哈希里掺了 `scope`：当 `scope` 本身长到必须截断才放得下（显式 `NEKRO_INSTANCE_NAME` 过长）时，
    只共享开头几十个字符的两套部署仍然得到不同的名字。
    """
    digest = hashlib.sha256(f"{scope}\0{instance_key}".encode()).hexdigest()[:CONTAINER_NAME_HASH_LENGTH]
    stem = CONTAINER_NAME_TEMPLATE.format(instance_key=digest)
    room = max(CONTAINER_NAME_MAX_LENGTH - len(stem), 0)
    return f"{scope[:room]}{stem}"


def build_container_name(instance_key: str) -> str:
    """按**当前口径**生成容器名：`deployment_scope()` 前缀 + 模板段。新建容器的唯一名字来源。

    前缀与 iptables 标记 / 沙盒网络名 / 归属标签同一套派生（`core/os_env.py:deployment_scope()`）。
    不带前缀时两套部署上同名 `instance_key` 的容器名逐字相同：先建的一方占着宿主全局名，
    后建的一方 `create_container` 抛 409（#46 容器名这一半）。

    **长度上限 63（DNS 单段）**：容器在 compose 网络里是靠容器名当主机名被访问的
    （`_webui_base_url` 在容器内拼的就是 `http://{容器名}:6099`），主机名单段超过 63 个字符
    解析不了。派生前缀最长 22 字符 + 模板 13 + 实例标识最长 32 = 67，会超；显式的
    `NEKRO_INSTANCE_NAME` 更长。展开后超过 63 时改用稳定短名（`_shorten_container_name`：
    保留部署前缀、实例段换成哈希）；**不超长的一律原样**，已部署的容器名不因此变动。

    调用时算前缀而不在 import 期定死：测试要能 monkeypatch `OsEnv.INSTANCE_NAME` / `DATA_DIR`
    （§2.9）。`NEKRO_INSTANCE_NAME` 显式设了非法字符时这里先于 Docker 调用拒绝——
    否则错误会是守护进程里一句难定位的 "invalid container name"。
    """
    name = build_unshortened_container_name(instance_key)
    if len(name) <= CONTAINER_NAME_MAX_LENGTH:
        return name
    short = _shorten_container_name(deployment_scope(), validate_instance_key(instance_key))
    if not _DOCKER_NAME_PATTERN.match(short):
        raise ValidationError(reason=f"生成的容器名 {short!r} 不符合 Docker 命名字符集，请检查 NEKRO_INSTANCE_NAME 的取值")
    return short


def build_legacy_container_name(instance_key: str) -> str:
    """按**升级前口径**生成容器名（裸模板名，不带部署前缀）。只用于读认，永不用于新建。

    过渡期措施，见模块 docstring 的「归属标签与容器名的过渡期双识别」。
    即便 `NEKRO_INSTANCE_NAME` 未设置（此时 `deployment_scope()` 由数据目录派生、恒非空），
    升级前的容器也叫这个名字——名字这一维的旧值与标签不同，它**从来与部署身份无关**。
    """
    return CONTAINER_NAME_TEMPLATE.format(instance_key=validate_instance_key(instance_key))


def container_name_candidates(instance_key: str) -> List[str]:
    """按名取容器的候选表：当前口径在前，其后是读认用的旧口径名（重复的只留一个）。

    顺序：当前口径名 → 展开形态的长名（仅当展开后超过 63、当前口径已改用短名时才与前者不同）→
    升级前的裸模板名。后两个都是过渡期读认，永不用于新建。

    模块内所有按名取容器的路径（`_owned_container` / `get_container_info` / `create_container`
    的撞名检查 / `_webui_base_url` 的容器网络域名）以及 adapter 侧的 `get_container` 都走
    这一份表，`_assert_owned` 的名字维度放行的也是这一份。漏掉任何一条按名取容器的路径，
    「升级后存量容器 404」就是那条路径上的线上事故。
    """
    names = [
        build_container_name(instance_key),
        build_unshortened_container_name(instance_key),
        build_legacy_container_name(instance_key),
    ]
    return list(dict.fromkeys(names))


def build_image_reference(image_tag: str) -> str:
    """把调用方给出的 tag 拼成受控的完整镜像引用。"""
    tag = image_tag.strip() or DEFAULT_IMAGE_TAG
    if not IMAGE_TAG_PATTERN.match(tag):
        raise ValidationError(reason=f"镜像标签 '{tag}' 非法")
    return f"{NAPCAT_IMAGE_REPOSITORY}:{tag}"


def get_owner_tag() -> str:
    """本部署的归属标识：**新建容器时写入**的当前口径。

    取值与 iptables 标记 / 沙盒与工作区网络名 / 沙盒容器归属标签同源
    （`core/os_env.py:deployment_scope()`）：显式 `NEKRO_INSTANCE_NAME` 优先，
    没设时由 `NEKRO_DATA_DIR` 的绝对路径派生出一个稳定后缀。

    **不要退回 `OsEnv.INSTANCE_NAME or "default"`**：两种标准部署下那个变量默认都是空串，
    于是同宿主上的两套部署会把标签写成逐字相同的 `default`，控制台可以互相停 / 删 / 重启
    对方的协议端容器。

    调用时读、不在 import 期定死：测试要能 monkeypatch `OsEnv.INSTANCE_NAME` / `DATA_DIR`
    （§2.9：fake 里写死由环境变量派生的值，换台机器就在测另一件事）。
    """
    return deployment_scope()


def get_legacy_owner_tag() -> str:
    """升级前写入的归属标识。**只用于读认，永不写入。**

    过渡期措施，见模块 docstring 的「归属标签的过渡期双识别」。
    `NEKRO_INSTANCE_NAME` 有值时它与 `get_owner_tag()` 逐字相同（`deployment_scope()`
    在那种情形下原样返回该值），双识别对这类部署等于没启用。

    ⚠️ **它读的是「现在」的 `NEKRO_INSTANCE_NAME`，不是建容器那天的值。** 覆盖得到的只有
    「升级前后这个变量没变过」的部署——那是绝大多数。同一次升级里**顺手清掉/改掉**
    `NEKRO_INSTANCE_NAME` 的（`multi-deploy-same-host.md` §2.1 的规避① 就在建议清空它），
    存量容器身上的旧标签是改之前那个值，两档都对不上，仍然会落回 403。
    出口是现成的：控制台把该实例的容器删掉重建一次即可（数据目录保留，不用重新扫码，
    见 `delete_container`）。**不要**为此改成「标签随便什么值、挂载对就放行」——
    那等于把归属判定整条降级成只剩挂载路径一道。
    """
    return OsEnv.INSTANCE_NAME or LEGACY_OWNER_TAG_FALLBACK


def get_instance_data_dir(instance_key: str) -> Path:
    """实例的 NapCat 数据根目录"""
    return Path(OsEnv.DATA_DIR) / "napcat_data" / validate_instance_key(instance_key)


def get_qq_state_dir(instance_key: str) -> Path:
    """QQ 登录态目录，挂载后扫码结果可跨容器重建保留"""
    return get_instance_data_dir(instance_key) / "QQ"


def get_napcat_config_dir(instance_key: str) -> Path:
    """NapCat 配置目录，存放 webui.json 与 onebot11 适配器配置"""
    return get_instance_data_dir(instance_key) / "napcat"


# ---------------------------------------------------------------------------
# 实例元数据
# ---------------------------------------------------------------------------


class ContainerMetadata(BaseModel):
    """`DBAdapterInstance.metadata_json` 中与容器相关的部分"""

    container_name: str = Field(default="")
    webui_port: Optional[int] = Field(default=None)
    image: str = Field(default="")
    access_token: str = Field(default="")
    desired_state: str = Field(default="", description="走 docker-guard 时记录的期望状态（running / stopped），空 = 没记录")


def _load_metadata(instance: DBAdapterInstance) -> Dict[str, Any]:
    raw = (instance.metadata_json or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"实例 {instance.instance_key} 的 metadata_json 不是合法 JSON，按空元数据处理")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_container_metadata(instance: DBAdapterInstance) -> ContainerMetadata:
    """从实例元数据中读出容器相关字段"""
    return ContainerMetadata.model_validate(_load_metadata(instance))


async def _write_container_metadata(instance: DBAdapterInstance, patch: Dict[str, Any]) -> None:
    """合并写回容器相关元数据，保留其他子模块写入的键

    **在行锁下读改写，且基于数据库里的当前值，而不是调用方手里那份快照**：`instance` 可能是几十秒前读出来的
    （例如 reconcile 一轮里先读了一批行，再逐个处理），用快照整份覆盖 `metadata_json` 会把期间别处写入的
    `webui_port` / `access_token` / `desired_state` 抹掉。写完把调用方那份的 `metadata_json` 刷新成最新值。
    """
    async with in_transaction() as connection:
        row = await DBAdapterInstance.filter(id=instance.id).using_db(connection).select_for_update().first()
        if row is None:
            return
        metadata = _load_metadata(row)
        metadata.update(patch)
        row.metadata_json = json.dumps(metadata, ensure_ascii=False)
        await row.save(update_fields=["metadata_json", "update_time"], using_db=connection)
    instance.metadata_json = row.metadata_json


async def _record_desired_state(instance: DBAdapterInstance, state: str) -> None:
    """走 docker-guard 时记下「这个容器该不该在跑」，供 `reconcile_container_liveness` 区分崩溃与主动停止。

    不走 guard 时什么都不写：标准部署由 Docker 的 `unless-stopped` 自己保活，元数据保持逐字不变。
    """
    if not docker_guard_in_path():
        return
    await _write_container_metadata(instance, {"desired_state": state})


def resolve_access_token(instance: DBAdapterInstance) -> str:
    """取实例的 OneBot 鉴权令牌，实例未单独配置时退回部署级令牌

    这是「下发给协议端」与「握手时校验」共用的**同一个判据**：
    `adapters/onebot_v11/core/access_token.py` 直接调它来算期望值，
    两侧各写一份的后果是迟早分叉，而分叉的表现就是协议端连不上。
    """
    return read_container_metadata(instance).access_token or resolve_deployment_onebot_access_token()


async def write_access_token(instance: DBAdapterInstance, token: str) -> None:
    """把实例自己的接入令牌写进元数据（换令牌用）

    走 `_write_container_metadata` 的行锁合并写：只动 `access_token` 这一个键，
    期间别处写入的 `webui_port` / `desired_state` 原样保留。
    """
    # 键名与 `instance_manager.METADATA_ACCESS_TOKEN` 同值。不 import 那个常量：
    # `instance_manager` 会反向延迟导入本模块，这里再反向 import 一次只是把环绕得更长。
    await _write_container_metadata(instance, {"access_token": token})


async def resolve_instance(instance_key: str) -> DBAdapterInstance:
    """按实例标识定位实例记录"""
    validate_instance_key(instance_key)
    instance = await DBAdapterInstance.filter(instance_key=instance_key).exclude(status="deleted").first()
    if instance is None:
        raise NotFoundError(resource=f"实例 '{instance_key}'")
    return instance


def _is_builtin_napcat(instance: DBAdapterInstance) -> bool:
    """判断实例是否为本机 Docker 托管的 NapCat 形态。

    `provider` 在库里是自由字符串（历史数据里还有适配器自行写入的取值），
    这里按与 `instance_manager.map_provider` 一致的方式归一化后再比对。
    """
    return instance.provider.strip().lower() == InstanceProvider.BUILTIN_NAPCAT.value


def _require_builtin_napcat(instance: DBAdapterInstance) -> None:
    """容器管理只对本机托管形态开放；远程协议端没有可供操作的容器

    ## 回收方向的两个豁免（`stop_container` / `delete_container`）

    这条校验只看 `provider` 这个**数据库字段**，而它是会被改写的：实例把协议端搬到外部机器
    之后 provider 变成 `remote_onebot`，宿主上那个容器**并不会随之消失**。于是原先的形态是：
    容器还在跑、还在吃内存，而控制台上停 / 删 / 重启 / 建全部 `ValidationError`
    —— 孤儿制造出来之后，出口是封死的，只能 SSH 上去 `docker rm`
    （测试部署上两个这样的容器空转 19 天，合计 1.2 GB 内存）。

    所以**回收方向的两个操作不看 provider**：

    - `stop_container` / `delete_container` —— 让宿主上的东西**变少**，判定错了的代价是
      「多停了一个本来就没人连的容器」，而容器可以重建、数据目录也仍然保留。
    - 其余全部保持严格（`create` / `start` / `restart` / 网络配置读写 / 扫码 / 快速登录）
      —— 它们让宿主上的东西**变多**，或者会把配置写进一个 NA 已经不认为归自己管的容器。

    **安全边界一分未松**：这两个函数走的仍是 `_owned_container()`，也就是
    `_assert_owned()` 那一整套 fail-closed 判定（容器名模板 + 双标签识别 + 过渡期挂载点确认）。
    provider 从来就不是安全判据——它是**库里的一个自由字符串**，而归属判定问的是宿主上的事实。

    库里那一行整个没了的情形这两个豁免仍然够不到（它们的入参是 `DBAdapterInstance`），
    那一档由 `services/console/napcat_orphans.py` 负责。
    """
    if not _is_builtin_napcat(instance):
        raise ValidationError(reason=f"实例 '{instance.instance_key}' 不是本机托管的 NapCat 容器，无法执行容器操作")


# ---------------------------------------------------------------------------
# Docker 客户端与归属校验
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _docker_client() -> AsyncIterator[aiodocker.Docker]:
    """每次操作新建客户端并确保关闭。

    复用长生命周期客户端曾导致连接池耗尽后请求挂死，这里按操作粒度开闭。
    """
    client = aiodocker.Docker()
    try:
        yield client
    finally:
        await client.close()


async def _resolve_nekro_network(docker: aiodocker.Docker) -> str:
    """探测 NA 自身所在的 nekro_network，使新建的 NapCat 能通过容器名互访。

    结果缓存于模块变量，同一进程内只探测一次。
    """
    global _resolved_nekro_network

    if _resolved_nekro_network:
        return _resolved_nekro_network

    async with _resolve_network_lock:
        if _resolved_nekro_network:
            return _resolved_nekro_network

        try:
            container_id = Path("/etc/hostname").read_text(encoding="utf-8").strip()
            if not _CONTAINER_ID_PATTERN.match(container_id):
                logger.warning(f"[网络探测] /etc/hostname 内容 {container_id!r} 不是有效容器 ID，跳过 inspect")
            else:
                info = await (await docker.containers.get(container_id)).show()
                networks = info.get("NetworkSettings", {}).get("Networks", {})
                for network_name in networks:
                    if network_name.endswith("nekro_network"):
                        _resolved_nekro_network = network_name
                        logger.info(f"[网络探测] 已发现 nekro_network: {network_name!r}")
                        return _resolved_nekro_network
        except (OSError, DockerError) as e:
            logger.warning(f"[网络探测] 探测失败，回退默认网络名: {e}")

        _resolved_nekro_network = "nekro_network"
        logger.warning("[网络探测] 未找到匹配网络，使用默认值: nekro_network")
        return _resolved_nekro_network


async def _inspect_container(docker: aiodocker.Docker, container_name: str) -> Optional[Dict[str, Any]]:
    """inspect 指定容器，不存在返回 None"""
    try:
        container = await docker.containers.get(container_name)
        return await container.show()
    except DockerError as e:
        if e.status == 404:
            return None
        raise


async def _inspect_container_candidates(
    docker: aiodocker.Docker,
    instance_key: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """按 `container_name_candidates` 的顺序找容器，返回 (找到的容器名, inspect 结果)。

    都不存在时返回 (当前口径名, None)——调用方用它拼 404 / 「容器不存在」的提示。
    新名在前：新部署一次命中；升级前建的存量容器顶着裸模板名，第二个候选兜住。
    """
    names = container_name_candidates(instance_key)
    for name in names:
        info = await _inspect_container(docker, name)
        if info is not None:
            return name, info
    return names[0], None


def _bind_source(info: Dict[str, Any], destination: str) -> str:
    """从 inspect 结果里取出挂到容器内 `destination` 的宿主机路径；没有这条挂载返回空串。

    先读 `Mounts`（结构化，Docker 始终返回），读不到再退回 `HostConfig.Binds`。

    **不能按 `:` 切分 Binds**：条目形如 `源:目标[:选项]`，而 Windows 上的源路径自带盘符冒号
    （`C:\\srv\\na:/app/...`），切出来的第一段会是 `C`。这里改成按**已知的目标**定位，
    并要求目标之后要么到头、要么是挂载选项分隔符。
    """
    for mount in info.get("Mounts") or []:
        if isinstance(mount, dict) and str(mount.get("Destination", "")) == destination:
            return str(mount.get("Source", "") or "")

    needle = f":{destination}"
    for raw in info.get("HostConfig", {}).get("Binds") or []:
        text = str(raw)
        start = text.find(needle)
        while start != -1:
            tail = text[start + len(needle) :]
            if not tail or tail.startswith(":"):
                return text[:start]
            start = text.find(needle, start + 1)
    return ""


def _is_same_host_path(candidate: str, expected: Path) -> bool:
    """两条宿主机路径是否指向同一个位置（各自展开后逐字比较）"""
    try:
        return Path(candidate).expanduser().resolve() == expected.expanduser().resolve()
    except OSError:  # pragma: no cover - resolve 在极端权限下才会抛
        return False


def _container_belongs_to_this_deployment(info: Dict[str, Any], instance_key: str) -> bool:
    """这个容器的挂载点是不是落在**本部署**的数据目录下。

    ## 为什么接受旧标签时必须再加这一道

    旧标签在两种标准部署下都是逐字相同的 `default`（`NEKRO_INSTANCE_NAME` 默认空串）。
    只要「认旧标签」不加限定，同宿主上的部署 B 拿着自己库里那行同名 `instance_key`
    去 inspect，就会看见部署 A 建的 `nekro_napcat_{instance_key}`（容器名是**宿主全局**的、
    且不带部署前缀），标签一对就放行 —— 双识别会把 #42 刚关掉的那条串扰路径原样打开。

    ## 判据为什么落在挂载路径上

    容器名不带部署前缀（#46 的另一半，未改），所以名字这一维在这里没有区分度。
    而 `create_container` 的三个 bind 全部派生自 `OsEnv.DATA_DIR`，而数据目录正是
    `deployment_scope()` 的派生来源 —— 两套部署**不可能**共用它（沙盒共享目录、pip 缓存、
    插件目录都在下面，共用一份等于互相踩）。也就是说挂载路径与新标签指向同一件事实，
    只是它写在容器上、不依赖标签，恰好能替升级前那批容器把归属补出来。

    ## 为什么 NA 自己在容器里时这条比较仍然成立

    Docker 报出来的 `Source` 是**宿主机**路径，而这里算出来的是 NA 进程视角的路径。两者相等
    靠的是 compose 里那条恒等挂载 `${NEKRO_DATA_DIR}:${NEKRO_DATA_DIR}:rw`
    —— 同一个不变量也正是 `create_container` 能把 `Path(OsEnv.DATA_DIR).resolve()` 直接当作
    bind 源交给守护进程的前提（守护进程只认宿主机路径）。也就是说：这条比较不成立的部署，
    连建容器都建不对，不存在「只有归属判定单独失灵」的情形。

    ## 判定是 fail-closed 的

    两条实例级挂载有任意一条明确指向别处 → 直接判否；一条都读不到（inspect 结果被裁剪、
    容器是别人用别的方式建的）也判否。宁可让一个存量容器落回 403，也不放行一次跨部署操作。
    """
    matched = False
    for destination, expected in (
        (CONTAINER_NAPCAT_CONFIG_DIR, get_napcat_config_dir(instance_key)),
        (CONTAINER_QQ_STATE_DIR, get_qq_state_dir(instance_key)),
    ):
        source = _bind_source(info, destination)
        if not source:
            continue
        if not _is_same_host_path(source, expected):
            return False
        matched = True
    return matched


def _assert_owned(info: Dict[str, Any], instance_key: str, container_name: str) -> None:
    """破坏性操作前的归属校验。

    只认「容器名匹配模板」且「两个归属标签都对得上」的容器。任何一项不符都拒绝执行——
    否则一个被构造的 instance_key 或一个同名的他人容器，就能借这条 API 停掉/删掉无关容器。

    归属标签这一维走**过渡期双识别**：当前标签（`get_owner_tag()`）直接放行；升级前那套
    标签（`get_legacy_owner_tag()`）额外要求容器的挂载点落在本部署的数据目录下
    （见 `_container_belongs_to_this_deployment`），否则同宿主上的两套部署会借着逐字相同的
    `default` 互相接管容器。

    容器名这一维同样双识别：`container_name`（调用方按当前口径拼出的名字）与候选表
    （`container_name_candidates`：展开形态的长名、升级前的裸模板名）任一匹配即通过。
    存量容器还顶着旧名（#46 容器名这一半的过渡期；名字超过 63 字符后改用短名，
    之前按展开形态建的长名同理），只认新名会让它们在升级当天集体 403。
    """
    actual_name = str(info.get("Name", "")).lstrip("/")
    labels = info.get("Config", {}).get("Labels") or {}
    owner = get_owner_tag()
    actual_owner = labels.get(LABEL_OWNER)

    if actual_name != container_name and actual_name not in container_name_candidates(instance_key):
        logger.warning(f"[归属校验] 容器名不符，拒绝操作: 期望 {container_name!r}，实际 {actual_name!r}")
        raise PermissionDeniedError(detail=f"container name mismatch: {actual_name}")

    if labels.get(LABEL_INSTANCE) == instance_key:
        if actual_owner == owner:
            return
        legacy = get_legacy_owner_tag()
        if actual_owner == legacy and _container_belongs_to_this_deployment(info, instance_key):
            # 过渡期路径：升级前建的容器还带着旧标签。打成 INFO 是为了让「宿主上还剩几个
            # 存量容器」可被观测到——双识别的删除条件就是这条日志不再出现（见模块 docstring）
            logger.info(
                f"[归属校验] 容器 {container_name!r} 带的是升级前的归属标签 {legacy!r}，"
                f"挂载点确认属于本部署，按过渡期规则放行（当前标签应为 {owner!r}；"
                "重建该容器即可收口）",
            )
            return

    logger.warning(
        f"[归属校验] 容器 {container_name!r} 的归属标签不匹配，拒绝操作: "
        f"labels={{{LABEL_INSTANCE}: {labels.get(LABEL_INSTANCE)!r}, "
        f"{LABEL_OWNER}: {actual_owner!r}}}，期望 instance={instance_key!r} owner={owner!r}"
        f"（或升级前的 {get_legacy_owner_tag()!r} 且挂载点属于本部署）",
    )
    raise PermissionDeniedError(detail="container ownership label mismatch")


@contextlib.asynccontextmanager
async def _owned_container(instance_key: str) -> AsyncIterator[Tuple[DockerContainer, Dict[str, Any]]]:
    """取得归属校验通过的容器句柄；容器不存在或非本部署所有时直接失败

    按名取容器走候选表（新名在前、裸模板名在后）：升级前建的存量容器还顶着旧名，
    只按新名找的话，启 / 停 / 重启 / 删 / 日志兜底会在升级当天集体 404。
    """
    primary_name = build_container_name(instance_key)
    async with _docker_client() as docker:
        found_name, info = await _inspect_container_candidates(docker, instance_key)
        if info is None:
            raise NotFoundError(resource=f"容器 '{primary_name}'")
        _assert_owned(info, instance_key, primary_name)
        yield await docker.containers.get(found_name), info


async def assert_container_owned(container: DockerContainer, instance_key: str) -> None:
    """对一个**已经拿到手**的容器句柄做同一套归属校验。

    ## 为什么要有这个公开出口

    模板容器 `nekro_napcat_{instance_key}` 不是只有本模块会去开：
    `adapters/onebot_v11/routers.py` 的 `/container/{status,logs,logs/stream,restart,
    napcat-token}` 也按同一个模板名自己 `containers.get()`（它还要兼容单实例时代那个
    **没有任何标签**的遗留容器名，所以走不了 `_owned_container` 这条通路）。

    那批端点里 `/logs` 与 `/logs/stream` 把 NapCat 的启动日志原样吐出来，**日志里就带着
    WebUI 访问令牌**（前端 `logs.tsx` 的 `extractWebuiToken` 正是从这里抠它），`/restart`
    则是货真价实的生命周期操作。容器名此前是**宿主全局**的裸模板名，同宿主上另一套部署里
    同名的 `instance_key` 会解析到同一个容器——这正是 #46 描述的「互相重启对方的协议端容器」，
    只不过它走的是第二个出口。现在新建容器带部署前缀，但按名取容器仍要认升级前的
    裸模板名，所以这里对**新旧两个名字**都做同一套判定。

    判据必须与 `_assert_owned` **同一份**：两处各写一套的话，同一个容器会出现
    「总后台拒绝、适配器路由放行」的分叉，而分叉里漏出去的那一半没有任何信号。

    Raises:
        PermissionDeniedError: 容器名 / 实例标签 / 归属标签任一不符（判据见 `_assert_owned`）
    """
    _assert_owned(await container.show(), instance_key, build_container_name(instance_key))


# ---------------------------------------------------------------------------
# 容器状态
# ---------------------------------------------------------------------------


def _map_state(raw_state: str) -> ContainerState:
    return _DOCKER_STATE_MAP.get(raw_state.lower(), ContainerState.EXITED)


async def get_container_info(instance: DBAdapterInstance) -> Optional[ContainerInfo]:
    """获取实例的协议端容器信息；非 builtin_napcat 形态返回 None

    容器不存在时返回 `state=absent` 的信息体（而非 None），前端据此渲染「创建容器」入口。

    ## 读侧也要过归属校验

    容器名是**宿主全局**的，同宿主上另一套部署里同名的 `instance_key` 有机会 inspect 到
    同一个候选名下的容器（升级前的裸模板名在两套部署上逐字相同）。写侧（启/停/重启/删）
    早就 fail-closed 了（`_owned_container`），读侧一直没有 —— 于是本部署的后台会照着**邻居的**
    容器显示状态、镜像与 WebUI 端口，而扫码/快速登录正是拿这个判定去决定「容器在不在跑」。
    协议端自助运维降到租户可达之后，这条读路径成了租户能触达的第一段，因此在这里收口。

    不归本部署所有 ⇒ 一律按 `absent` 回答（**本部署没有这个容器**，事实如此：它的每一个
    生命周期操作都会 403）。判据与写侧同一份 `_assert_owned`，两处各写一套必然漂移。
    落一条 WARNING，让「宿主上有个同名但不归我们的容器」这件事可被观测到。

    按名取容器走候选表（新名在前、裸模板名在后）：升级前建的存量容器要照常读到状态，
    否则扫码 / 快速登录在升级当天全部判「容器不存在」。
    """
    if not _is_builtin_napcat(instance):
        return None

    metadata = read_container_metadata(instance)
    primary_name = build_container_name(instance.instance_key)
    absent = ContainerInfo(
        name=primary_name,
        state=ContainerState.ABSENT,
        image=metadata.image,
        webui_port=metadata.webui_port,
    )

    try:
        async with _docker_client() as docker:
            found_name, info = await _inspect_container_candidates(docker, instance.instance_key)
    except (DockerError, OSError) as e:
        logger.warning(f"读取实例 {instance.instance_key} 的容器状态失败: {e}")
        return absent

    if info is None:
        return absent

    try:
        _assert_owned(info, instance.instance_key, primary_name)
    except PermissionDeniedError as e:
        logger.warning(f"容器 {found_name!r} 不归本部署所有，按「容器不存在」回答: {e}")
        return absent

    state_section = info.get("State", {})
    return ContainerInfo(
        name=found_name,
        state=_map_state(str(state_section.get("Status", ""))),
        image=str(info.get("Config", {}).get("Image", "")) or metadata.image,
        webui_port=_read_host_port(info) or metadata.webui_port,
        started_at=str(state_section.get("StartedAt")) or None,
    )


def _read_host_port(info: Dict[str, Any]) -> Optional[int]:
    """从 inspect 结果读出 WebUI 的宿主机端口"""
    bindings = info.get("HostConfig", {}).get("PortBindings") or {}
    entries = bindings.get(f"{NAPCAT_WEBUI_PORT}/tcp") or []
    for entry in entries:
        raw_port = str(entry.get("HostPort", "")).strip()
        if raw_port.isdigit():
            return int(raw_port)
    return None


# ---------------------------------------------------------------------------
# 创建 / 生命周期
# ---------------------------------------------------------------------------


async def _collect_used_webui_ports() -> Set[int]:
    """汇总所有实例元数据中已占用的 WebUI 端口"""
    rows = await DBAdapterInstance.all().values_list("metadata_json", flat=True)
    used: Set[int] = set()
    for raw in rows:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        port = parsed.get("webui_port") if isinstance(parsed, dict) else None
        if isinstance(port, int):
            used.add(port)
    return used


async def _allocate_webui_port() -> int:
    """在受控端口段内随机取一个未被其他实例占用的端口"""
    used = await _collect_used_webui_ports()
    for _ in range(100):
        port = random.randint(WEBUI_HOST_PORT_RANGE_START, WEBUI_HOST_PORT_RANGE_END)
        if port not in used:
            return port
    raise OperationFailedError(operation="分配 NapCat WebUI 端口", detail="端口段内无空闲端口")


async def _ensure_image(docker: aiodocker.Docker, image: str, auto_pull: bool) -> None:
    """确保镜像在本地存在，必要时拉取"""
    try:
        await docker.images.inspect(image)
        return
    except DockerError as e:
        if e.status != 404:
            raise
    if not auto_pull:
        raise OperationFailedError(operation=f"创建容器（本地缺少镜像 {image}）", detail="image not found locally")

    logger.info(f"本地缺少镜像 {image}，开始拉取…")
    try:
        await asyncio.wait_for(
            docker.images.pull(from_image=NAPCAT_IMAGE_REPOSITORY, tag=image.split(":", 1)[1]),
            timeout=IMAGE_PULL_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, DockerError) as e:
        raise OperationFailedError(operation=f"拉取镜像 {image}", detail=str(e)) from e


async def create_container(
    instance: DBAdapterInstance,
    image_tag: str = DEFAULT_IMAGE_TAG,
    auto_pull: bool = True,
) -> ContainerInfo:
    """为实例创建并启动 NapCat 容器

    镜像仓库与容器名均由本模块生成，调用方只能影响 tag。
    """
    _require_builtin_napcat(instance)
    instance_key = validate_instance_key(instance.instance_key)
    container_name = build_container_name(instance_key)
    image = build_image_reference(image_tag)

    qq_state_dir = get_qq_state_dir(instance_key)
    config_dir = get_napcat_config_dir(instance_key)
    qq_state_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    # 容器起来前先落好网络配置，避免首次启动连不上 NA。
    # 数据目录在删容器后仍会保留，重建容器时这里会把用户之前加的连接一并带回来。
    await resync_network_config(instance)

    webui_port = read_container_metadata(instance).webui_port or await _allocate_webui_port()

    async with _docker_client() as docker:
        # 撞名检查覆盖两套口径：本部署名下（归属校验通过）已有容器——不管顶着带前缀的新名，
        # 还是升级前建的裸模板名——都按「已存在」拒绝，否则同一个实例会同时有两个容器
        # （旧容器还占着 WebUI 宿主端口）。名字被**邻居**占着（归属校验不过）不拦新建：
        # 新容器用的是带部署前缀的名字，与邻居的容器结构上不再共享命名空间。
        for existing_name in container_name_candidates(instance_key):
            existing_info = await _inspect_container(docker, existing_name)
            if existing_info is None:
                continue
            try:
                _assert_owned(existing_info, instance_key, existing_name)
            except PermissionDeniedError:
                continue
            raise ConflictError(resource=f"容器 '{existing_name}'")

        await _ensure_image(docker, image, auto_pull)

        # Docker 的 bind source 必须是绝对路径；开发态 DATA_DIR 是相对路径，这里统一展开。
        # 数据目录只挂 `uploads`、只读：协议端只需要读 NA 交给它发送的文件，整个数据目录（CC 工作区、
        # 插件数据、数据库文件）都不该在它手里。目录先建好，免得 Docker 以 root 建一个 NA 写不进去的
        uploads_dir = Path(OsEnv.DATA_DIR).resolve() / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        binds = [
            f"{uploads_dir}:{CONTAINER_UPLOADS_DIR}:ro",
            f"{qq_state_dir.resolve()}:{CONTAINER_QQ_STATE_DIR}",
            f"{config_dir.resolve()}:{CONTAINER_NAPCAT_CONFIG_DIR}",
        ]
        container_config: Dict[str, Any] = {
            "Image": image,
            # 归属标签只写**当前**口径（`deployment_scope()` 派生）。写回旧口径等于让
            # 「读认旧标签」这条过渡期分支永远收不了口，而它比新标签弱：旧标签在两套部署上
            # 逐字相同，全靠挂载路径这一道兜着（见 `_container_belongs_to_this_deployment`）
            "Labels": {
                LABEL_INSTANCE: instance_key,
                LABEL_OWNER: get_owner_tag(),
            },
            "ExposedPorts": {f"{NAPCAT_WEBUI_PORT}/tcp": {}},
            "HostConfig": {
                "Binds": binds,
                # 只绑回环：NapCat WebUI 自带弱口令风险，不应直接暴露到公网
                "PortBindings": {
                    f"{NAPCAT_WEBUI_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(webui_port)}],
                },
                # 标准部署 unless-stopped（逐字不变）；走 docker-guard 时是 no，保活由 NA 经 guard 拉起
                "RestartPolicy": {"Name": napcat_restart_policy()},
            },
        }

        if OsEnv.RUN_IN_DOCKER:
            container_config["HostConfig"]["NetworkMode"] = await _resolve_nekro_network(docker)

        try:
            container = await docker.containers.create(config=container_config, name=container_name)
            await container.start()
        except DockerError as e:
            raise OperationFailedError(operation=f"创建容器 '{container_name}'", detail=str(e)) from e

    metadata_patch: Dict[str, Any] = {"container_name": container_name, "webui_port": webui_port, "image": image}
    if docker_guard_in_path():
        metadata_patch["desired_state"] = DESIRED_RUNNING
    await _write_container_metadata(instance, metadata_patch)
    logger.info(f"已创建 NapCat 容器: {container_name} (image={image}, webui_port={webui_port})")

    info = await get_container_info(instance)
    if info is None:
        raise OperationFailedError(operation=f"创建容器 '{container_name}'")
    return info


def _actual_name(info: Dict[str, Any]) -> str:
    """从 inspect 结果里取出容器的实际名字（日志用）。

    过渡期里它可能与 `build_container_name()` 算出的不一致（存量容器顶着裸模板名），
    日志按实际名字打，「宿主上还剩几个存量容器」才可被观测到（与 §2.4 的口径一致）。
    """
    return str(info.get("Name", "")).lstrip("/")


async def start_container(instance: DBAdapterInstance, *, record_intent: bool = True) -> None:
    """启动实例容器

    `record_intent=False` 只给保活（reconcile）用：它启动的前提就是期望状态已是 running，不需要再写一遍。
    """
    _require_builtin_napcat(instance)
    async with _owned_container(instance.instance_key) as (container, info):
        try:
            await container.start()
        except DockerError as e:
            if e.status != 304:  # 已在运行
                raise OperationFailedError(operation="启动容器", detail=str(e)) from e
    if record_intent:
        await _record_desired_state(instance, DESIRED_RUNNING)
    logger.info(f"已启动 NapCat 容器: {_actual_name(info)}")


async def stop_container(instance: DBAdapterInstance, timeout: int = 15) -> None:
    """停止实例容器。

    **有意不做 `_require_builtin_napcat` 校验**，理由见该函数的 docstring「回收方向的两个豁免」。
    """
    async with _owned_container(instance.instance_key) as (container, info):
        # 主动停止要**先**记下来再停：走 guard 的保活（reconcile）靠它区分「崩溃」与「被停掉」，
        # 先停后记的话，两步之间恰好撞上一轮探活就会把刚停掉的容器又拉起来
        await _record_desired_state(instance, DESIRED_STOPPED)
        try:
            await container.stop(t=timeout)
        except DockerError as e:
            if e.status != 304:  # 已停止
                await _record_desired_state(instance, DESIRED_RUNNING)  # 没停成：容器还在跑，还原期望
                raise OperationFailedError(operation="停止容器", detail=str(e)) from e
    logger.info(f"已停止 NapCat 容器: {_actual_name(info)}")


async def restart_container(instance: DBAdapterInstance, timeout: int = 15) -> None:
    """重启实例容器，重启前同步最新网络配置"""
    _require_builtin_napcat(instance)
    await resync_network_config(instance)
    async with _owned_container(instance.instance_key) as (container, info):
        try:
            # aiodocker 的 `timeout` 是 HTTP 客户端超时，容器停止宽限期是 `t`
            await container.restart(t=timeout)
        except DockerError as e:
            raise OperationFailedError(operation="重启容器", detail=str(e)) from e
    await _record_desired_state(instance, DESIRED_RUNNING)
    logger.info(f"已重启 NapCat 容器: {_actual_name(info)}")


async def delete_container(instance: DBAdapterInstance) -> None:
    """删除实例容器。

    只删容器本身，`{DATA_DIR}/napcat_data/{instance_key}/` 下的 QQ 登录态与配置保留，
    重建容器后无需重新扫码。**要连数据目录一起回收**走总后台的「NapCat 残留回收」
    （`services/console/napcat_orphans.py`），它有完整的判据表与二次确认。

    **有意不做 `_require_builtin_napcat` 校验**，理由见该函数的 docstring「回收方向的两个豁免」。
    """
    async with _owned_container(instance.instance_key) as (container, info):
        try:
            await container.delete(force=True)
        except DockerError as e:
            raise OperationFailedError(operation="删除容器", detail=str(e)) from e

    delete_patch: Dict[str, Any] = {"container_name": ""}
    if docker_guard_in_path():
        delete_patch["desired_state"] = DESIRED_STOPPED
    await _write_container_metadata(instance, delete_patch)
    logger.info(f"已删除 NapCat 容器: {_actual_name(info)}（数据目录保留）")


async def _reconcile_one(instance_id: int, current: float) -> Optional[str]:
    """处理一个实例：该拉起就拉起，返回被拉起的实例标识（没拉起返回 None）。

    **一律重读该行再判**：调用方手里的行是一轮开始时的快照，一轮里控制台可能点了「停止」；
    按快照判断会把刚停掉的容器又拉起来，再用快照里的元数据整份覆盖回 running。
    """
    instance = await DBAdapterInstance.get_or_none(id=instance_id)
    if instance is None or not _is_bindable(instance) or not _is_builtin_napcat(instance):
        return None
    key = instance.instance_key
    state = _reconcile_state.get(key)
    if read_container_metadata(instance).desired_state != DESIRED_RUNNING:
        _reconcile_state.pop(key, None)
        return None
    try:
        info = await get_container_info(instance)
    except Exception as e:  # noqa: BLE001 - 单个实例的探测失败不能中断整轮
        logger.warning(f"[保活] 读取实例 {key} 的容器状态失败，本轮跳过: {e}")
        return None
    if info is None or info.state == ContainerState.ABSENT:
        _reconcile_state.pop(key, None)
        return None
    if info.state == ContainerState.RUNNING:
        # 退避只在「跑满一段时间」之后才清零：刚拉起就又崩的容器（崩溃循环）要接着退避，
        # 否则每个探活周期都拉起一次，等于把 Docker 的 restart 风暴搬到了 NA 里
        if state is not None and state.started_at is not None and current - state.started_at >= RECONCILE_STABLE_SECONDS:
            _reconcile_state.pop(key, None)
        return None
    if info.state in (ContainerState.RESTARTING, ContainerState.PAUSED):
        return None
    if state is not None and current < state.next_try:
        return None

    previous_delay = state.delay if state is not None else 0.0
    delay = min(RECONCILE_MAX_DELAY_SECONDS, previous_delay * 2 if previous_delay else RECONCILE_MIN_DELAY_SECONDS)
    try:
        # record_intent=False：期望状态本来就是 running，不用再写一遍（写会基于旧快照，见 `_write_container_metadata`）
        await start_container(instance, record_intent=False)
    except Exception as e:  # noqa: BLE001 - 拉起失败只影响这一个实例，记下并退避
        _reconcile_state[key] = _ReconcileState(current + delay, delay, None)
        logger.warning(f"[保活] 经 docker-guard 拉起实例 {key} 的 NapCat 容器失败（{delay:.0f}s 后重试）: {e}")
        return None
    # 成功也要退避：下一次崩溃必须等过这段时间才会再拉起；跑满 RECONCILE_STABLE_SECONDS 才清零
    _reconcile_state[key] = _ReconcileState(current + delay, delay, current)
    logger.warning(f"[保活] 实例 {key} 的 NapCat 容器已退出，已经 docker-guard 重新拉起")

    # 拉起期间控制台可能点了「停止」：再读一次期望，已经是 stopped 就把刚拉起的容器停回去
    latest = await DBAdapterInstance.get_or_none(id=instance_id)
    if latest is not None and read_container_metadata(latest).desired_state == DESIRED_STOPPED:
        logger.info(f"[保活] 实例 {key} 在拉起期间被要求停止，停回去")
        try:
            await stop_container(latest)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[保活] 停回实例 {key} 的容器失败: {e}")
        _reconcile_state.pop(key, None)
        return None
    return key


async def reconcile_container_liveness(now: Optional[float] = None) -> List[str]:
    """走 docker-guard 的部署：把「应当在跑却已退出」的内置 NapCat 容器经 guard 重新拉起。

    ## 为什么需要它

    带 Binds 的容器在 guard 下只能是 `RestartPolicy=no`（见 `napcat_restart_policy`）——守护进程自己触发的
    重启不经过 guard 的挂载源复核。可 NapCat 是长命的协议端，崩溃 / 被 OOM 杀 / 宿主重启后需要回来。
    这个函数把「回来」这个动作收回到 NA 手里：每一次拉起都是一个普通的 `POST /containers/{id}/start`，
    要过 guard 的归属门与挂载源复核。

    ## 复用而不新造

    - 周期：挂在实例健康监测的探活循环里（`instance_health`，同一个周期；但在**独立任务**里跑，不挡探活）；
    - 拉起：`start_container()`，与控制台「启动」按钮是同一条路径（含归属校验）；
    - 状态：`get_container_info()`，与控制台展示是同一份判据。

    ## 判据（保守：宁可不拉起，也不要把运维主动停掉的容器拉回来）

    只处理同时满足下列条件的实例：启用且不在 `NON_DISPATCHABLE_STATUSES`、是内置 NapCat、
    元数据 `desired_state == running`（走 guard 时 create / start / restart 记下，stop / delete 记 stopped；
    **没记录的存量容器不碰**）、容器存在且不在运行。每个实例并发处理、各有 `RECONCILE_INSTANCE_TIMEOUT_SECONDS`
    的时限（一个实例的 Docker 调用挂住，只让它自己超时退避，不影响别的实例）。
    退避：每次拉起（无论成败）都把「下次允许拉起」推后（30s 起翻倍、封顶 10 分钟），**跑满
    `RECONCILE_STABLE_SECONDS` 才清零**，崩溃循环因此持续退避。标准部署（不走 guard）直接返回空列表。

    Returns:
        本轮成功拉起的实例标识
    """
    if not docker_guard_in_path():
        return []

    current = time.monotonic() if now is None else now
    rows = await DBAdapterInstance.filter(enabled=True).exclude(status__in=list(NON_DISPATCHABLE_STATUSES))
    candidates = [(row.id, row.instance_key) for row in rows if _is_builtin_napcat(row)]

    async def bounded(instance_id: int, key: str) -> Optional[str]:
        try:
            return await asyncio.wait_for(_reconcile_one(instance_id, current), timeout=RECONCILE_INSTANCE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            state = _reconcile_state.get(key)
            delay = min(RECONCILE_MAX_DELAY_SECONDS, state.delay * 2 if state and state.delay else RECONCILE_MIN_DELAY_SECONDS)
            _reconcile_state[key] = _ReconcileState(current + delay, delay, None)
            logger.warning(
                f"[保活] 实例 {key} 的容器检查 / 拉起超过 {RECONCILE_INSTANCE_TIMEOUT_SECONDS:.0f}s 未返回，放弃本轮（{delay:.0f}s 后重试）",
            )
            return None
        except Exception as e:  # noqa: BLE001 - 单个实例的意外不能带倒整轮
            logger.warning(f"[保活] 处理实例 {key} 时出错，本轮跳过: {e}")
            return None

    results = await asyncio.gather(*(bounded(instance_id, key) for instance_id, key in candidates))
    return [key for key in results if key is not None]


# ---------------------------------------------------------------------------
# 启动阶段：存量 NapCat 容器的 RestartPolicy 迁移 / 回滚告警
# ---------------------------------------------------------------------------


async def audit_napcat_restart_policies() -> None:
    """NA 启动时扫一遍本部署的内置 NapCat 容器，处理「RestartPolicy 与当前部署形态不匹配」的存量。

    **走 guard**：升级前（标准部署）建的 NapCat 是 `unless-stopped` 且带 Binds，守护进程会自己重启它，
    重启时的挂载源解析不经过 guard（见 `docker_guard.policy._check_restart_policy`）。这里用 `update`
    把它改成 `no`（guard 只放行这一种 update：`{"RestartPolicy":{"Name":"no"}}`），并补记 `desired_state`
    （正在跑 = running，已退出 = stopped；已有记录的不覆盖），之后由 reconcile 保活。**失败打 ERROR 并点名容器**，
    运维按日志去控制台删除重建。只动归属校验通过的 NapCat 容器（NA 自己的容器带 `nekro.owner.*` 标签但不是 NapCat，不在扫描范围）。

    **不走 guard**（含从硬化部署回滚到标准 compose）：不改任何东西；若发现 `RestartPolicy=no` 的带挂载 NapCat，
    打 WARNING——回滚后没有 reconcile 保活，这些容器崩溃后不会回来，需要在控制台删除重建（建成 `unless-stopped`）。

    整个函数不抛异常：它是启动期的辅助动作，不能拖垮启动。
    """
    guarded = docker_guard_in_path()
    try:
        async with _docker_client() as docker:
            listed = await docker.containers.list(all=True, filters=json.dumps({"label": [LABEL_INSTANCE]}))
            for item in listed:
                try:
                    await _audit_one_container(docker, item, guarded)
                except Exception as e:  # noqa: BLE001 - 单个容器的失败不影响其余
                    logger.error(f"[RestartPolicy] 检查 NapCat 容器时出错: {e}")
    except Exception as e:  # noqa: BLE001
        # 没有 Docker（开发机 / 未挂 socket）是正常情况，不是错误
        logger.debug(f"[RestartPolicy] 无法列出容器，跳过扫描: {e}")


async def _audit_one_container(docker: aiodocker.Docker, item: DockerContainer, guarded: bool) -> None:
    info = await item.show()
    labels = (info.get("Config") or {}).get("Labels") or {}
    instance_key = str(labels.get(LABEL_INSTANCE) or "")
    if not INSTANCE_KEY_PATTERN.match(instance_key):
        return
    name = str(info.get("Name", "")).lstrip("/")
    try:
        _assert_owned(info, instance_key, build_container_name(instance_key))
    except PermissionDeniedError:
        return  # 邻居部署的容器，不归我们管
    host_config = info.get("HostConfig") or {}
    policy_name = str((host_config.get("RestartPolicy") or {}).get("Name") or "")
    has_binds = bool(host_config.get("Binds")) or any(
        isinstance(m, dict) and m.get("Type") == "bind" for m in (info.get("Mounts") or [])
    )
    if not has_binds:
        return

    if not guarded:
        if policy_name in ("", "no"):
            logger.warning(
                f"[RestartPolicy] NapCat 容器 {name!r} 的 RestartPolicy 是 {policy_name or '空'}：它是在 docker-guard 下建的，"
                "当前部署不走 guard（回滚到标准 compose？），不会有人保活——崩溃或宿主重启后不会自己回来。"
                "请在控制台把该实例的容器「删除」再「创建」一次（数据目录保留），新容器会是 unless-stopped",
            )
        return

    if policy_name in ("", "no"):
        return
    try:
        await docker._query_json(
            f"containers/{item.id}/update",
            method="POST",
            data={"RestartPolicy": {"Name": "no"}},
        )
    except Exception as e:  # noqa: BLE001
        logger.error(
            f"[RestartPolicy] 无法把 NapCat 容器 {name!r}（实例 {instance_key}）的 RestartPolicy 从 {policy_name} 改成 no：{e}。"
            "它仍会被 Docker 守护进程自动重启，重启时的挂载源解析不经过 docker-guard。"
            "请在控制台把该实例的容器「删除」再「创建」一次（数据目录保留）",
        )
        return
    logger.warning(f"[RestartPolicy] 已把 NapCat 容器 {name!r}（实例 {instance_key}）的 RestartPolicy 从 {policy_name} 改成 no，改由 NA 经 docker-guard 保活")

    instance = await DBAdapterInstance.filter(instance_key=instance_key).exclude(status="deleted").first()
    if instance is not None and not read_container_metadata(instance).desired_state:
        running = str((info.get("State") or {}).get("Status", "")).lower() in ("running", "restarting")
        await _write_container_metadata(instance, {"desired_state": DESIRED_RUNNING if running else DESIRED_STOPPED})


# ---------------------------------------------------------------------------
# 网络配置（NapCat 的 onebot11 适配器配置文件）
# ---------------------------------------------------------------------------


MANAGED_ENTRY_NAME = "nekro_agent"
"""NekroAgent 托管连接在 NapCat 配置里的固定名称。

取一个不易与用户自建连接撞名的值；托管条目的识别、重建与保护全部围绕它进行。
"""

LEGACY_MANAGED_ENTRY_NAMES = frozenset({"ws"})
"""历史版本写入的托管连接名。

早期实现（以及 `adapters/onebot_v11` 里的 `ADAPTER_TEMPLATE`）把托管连接固定命名为 `ws`。
读取旧配置时必须把它一并识别成托管条目，否则它会被当成用户条目原样保留，
再叠加一条新的托管连接，导致协议端同时连两次。
"""

MANAGED_ENTRY_TYPE = NetworkEntryType.WS_CLIENT
"""托管连接恒为反向 WS：由协议端主动连回 NA，NA 不需要能反向访问协议端。"""

_SERVER_ENTRY_TYPES = frozenset(
    {
        NetworkEntryType.WS_SERVER,
        NetworkEntryType.HTTP_SERVER,
        NetworkEntryType.HTTP_SSE_SERVER,
    },
)
"""在协议端内部监听端口的类型，用 host + port 而非 url"""

_CLIENT_URL_SCHEMES: Dict[NetworkEntryType, Tuple[str, ...]] = {
    NetworkEntryType.WS_CLIENT: ("ws", "wss"),
    NetworkEntryType.HTTP_CLIENT: ("http", "https"),
}
"""由协议端主动向外连接的类型及其允许的协议"""

_ENTRY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_MIN_PORT = 1
_MAX_PORT = 65535

_UNSAFE_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
"""空白、控制字符、DEL 与反斜杠：WHATWG 会删掉制表符 / 换行、把 `\\` 当成 `/`，其他解析器不会"""

_OUTBOUND_URL_PATTERN = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*\.?)"
    r"(?::(?P<port>[0-9]{1,5}))?"
    r"(?=[/?#]|$)",
)
"""协议 `://` 主机 [:端口] 之后只能紧跟 `/`、`?`、`#` 或结尾。主机只能是 ASCII 域名（标签只含字母数字下划线短横线，
至多一个末尾点）或方括号包起来的 IPv6；用户名密码（`@`）、多余斜杠、百分号、非 ASCII、空端口都匹配不上"""

_NUMERIC_TAIL_PATTERN = re.compile(r"(?:^|\.)(?:0[xX][0-9A-Fa-f]*|[0-9]+)\.?$")
"""WHATWG 把「最后一段是数字」的主机一律按 IPv4 解析（十进制整数、十六进制、八进制、简写都认）"""

_CANONICAL_IPV4_PATTERN = re.compile(r"^(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}$")

_NAPCAT_FIELDS_BY_TYPE: Dict[NetworkEntryType, Tuple[str, ...]] = {
    NetworkEntryType.WS_CLIENT: (
        "enable",
        "name",
        "url",
        "reportSelfMessage",
        "messagePostFormat",
        "token",
        "debug",
        "heartInterval",
        "reconnectInterval",
    ),
    NetworkEntryType.WS_SERVER: (
        "enable",
        "name",
        "host",
        "port",
        "reportSelfMessage",
        "messagePostFormat",
        "token",
        "debug",
        "enableForcePushEvent",
        "heartInterval",
    ),
    NetworkEntryType.HTTP_SERVER: (
        "enable",
        "name",
        "host",
        "port",
        "messagePostFormat",
        "token",
        "debug",
    ),
    NetworkEntryType.HTTP_SSE_SERVER: (
        "enable",
        "name",
        "host",
        "port",
        "reportSelfMessage",
        "messagePostFormat",
        "token",
        "debug",
    ),
    NetworkEntryType.HTTP_CLIENT: (
        "enable",
        "name",
        "url",
        "reportSelfMessage",
        "messagePostFormat",
        "token",
        "debug",
    ),
}
"""每种类型写回 NapCat 时实际输出的字段（camelCase），按类型裁剪掉用不上的字段"""

_NAPCAT_FIELD_TO_ENTRY_ATTR: Dict[str, str] = {
    "enable": "enable",
    "name": "name",
    "url": "url",
    "host": "host",
    "port": "port",
    "token": "token",
    "debug": "debug",
    "messagePostFormat": "message_post_format",
    "reportSelfMessage": "report_self_message",
    "heartInterval": "heart_interval_ms",
    "reconnectInterval": "reconnect_interval_ms",
    "enableForcePushEvent": "enable_force_push_event",
}
"""NapCat 的 camelCase 字段名 ↔ 契约里的 snake_case 属性名"""


class _NapCatEntry(BaseModel):
    """NapCat `network.*` 数组里的单条连接配置

    五种类型共用这一个宽松模型：各数组的字段集互有出入，且 NapCat 版本间会增删字段，
    按类型写五个严格模型只会在协议端升级时把解析炸掉。`extra="allow"` 让未知字段留在
    `model_extra` 里，写回时可原样回填到用户自建条目上，不因为经过一次 NA 就被抹掉。
    """

    model_config = ConfigDict(extra="allow")

    enable: bool = True
    name: str = ""
    url: str = ""
    host: str = "0.0.0.0"
    port: int = 0
    token: str = ""
    debug: bool = False
    messagePostFormat: str = "array"
    reportSelfMessage: bool = False
    heartInterval: int = 30000
    reconnectInterval: int = 3000
    enableForcePushEvent: bool = True


class _NapCatNetworkSection(BaseModel):
    model_config = ConfigDict(extra="allow")

    httpServers: List[_NapCatEntry] = Field(default_factory=list)
    httpSseServers: List[_NapCatEntry] = Field(default_factory=list)
    httpClients: List[_NapCatEntry] = Field(default_factory=list)
    websocketServers: List[_NapCatEntry] = Field(default_factory=list)
    websocketClients: List[_NapCatEntry] = Field(default_factory=list)
    plugins: List[Any] = Field(default_factory=list)


class _NapCatOnebotConfig(BaseModel):
    """NapCat `onebot11_*.json` 的整体结构"""

    model_config = ConfigDict(extra="allow")

    network: _NapCatNetworkSection = Field(default_factory=_NapCatNetworkSection)
    musicSignUrl: str = ""
    enableLocalFile2Url: bool = False
    parseMultMsg: bool = False


def _section_arrays(section: _NapCatNetworkSection) -> Dict[NetworkEntryType, List[_NapCatEntry]]:
    """把 network 段的五个数组映射成「类型 → 条目列表」

    显式列举而不是 `getattr(section, entry_type.value)`：类型枚举的取值虽然就是数组名，
    但显式映射能让类型检查器看住两边同时改动。
    """
    return {
        NetworkEntryType.WS_CLIENT: section.websocketClients,
        NetworkEntryType.WS_SERVER: section.websocketServers,
        NetworkEntryType.HTTP_SERVER: section.httpServers,
        NetworkEntryType.HTTP_SSE_SERVER: section.httpSseServers,
        NetworkEntryType.HTTP_CLIENT: section.httpClients,
    }


def is_managed_entry(entry: NetworkEntry) -> bool:
    """判断一条配置是否属于 NekroAgent 托管连接

    只认反向 WS 类型，且满足下列任一：
    1. 带 `managed_by_nekro` 标记（前端回传的读取结果）；
    2. 名称为当前托管名 `nekro_agent`；
    3. 名称为历史托管名（`ws`），用于兼容旧版本落盘的配置。

    刻意限死类型：否则调用方在一条 httpClient 上打个托管标记，就能让它被当作托管条目
    重建，既丢了用户配置，也绕过了下面的出站地址校验。
    """
    if entry.type != MANAGED_ENTRY_TYPE:
        return False
    name = entry.name.strip()
    return entry.managed_by_nekro or name == MANAGED_ENTRY_NAME or name in LEGACY_MANAGED_ENTRY_NAMES


def resolve_self_listen_port() -> int:
    """NA 进程实际监听的端口 —— 内置 NapCat 反连地址的端口部分

    内置 NapCat 与 NA 在同一张容器网络里，反连走网桥内部直连，端口必须是 NA 进程 bind 的那个，
    **不是** `OsEnv.EXPOSE_PORT`：标准 compose 是 `"${NEKRO_EXPOSE_PORT:-8021}:8021"`，容器内恒为
    8021，`EXPOSE_PORT` 只改宿主侧映射。同机第二套部署把它设成 8022 时，连 `nekro_agent:8022`
    会被拒，实例一直离线。默认部署两者都是 8021，行为不变。

    与 CC 中继、qbob 端点同一个判据（`cc_credential.relay_listen_port`：问 nonebot 驱动，
    拿不到再退回 `EXPOSE_PORT`）。这是反连 URL 两个写入点（`build_managed_entry` 与
    `adapters/onebot_v11/routers.py` 的 `ADAPTER_TEMPLATE` 分支）取端口的唯一口径。
    """
    from nekro_agent.services.workspace import cc_credential

    return cc_credential.relay_listen_port()


def build_managed_entry(instance: DBAdapterInstance) -> NetworkEntry:
    """按当前部署推导 NekroAgent 托管的那条反向 WS 连接

    地址指向 NA 自身（容器网络内的服务名 + 进程监听端口），令牌取实例令牌，都不接受调用方传入。

    主机名走 `resolve_self_hostname()`（#47 的显式可配项）：未配置 `NEKRO_SELF_HOSTNAME`
    时的回落顺序与改造前逐字相同（`INSTANCE_NAME or "nekro_agent"`），行为不变；
    标准 compose 多部署无需再配：`docker/*.yml` 里 NA 服务已默认传
    `NEKRO_SELF_HOSTNAME=${NEKRO_SELF_HOSTNAME:-${INSTANCE_NAME:-}nekro_agent}`（逐字等于容器名），
    「部署前缀」与「反连地址可解析」同时成立（`tests/test_compose_self_hostname.py` 钉住）。
    端口走 `resolve_self_listen_port()`。
    """
    host = resolve_self_hostname()
    return NetworkEntry(
        type=MANAGED_ENTRY_TYPE,
        name=MANAGED_ENTRY_NAME,
        enable=True,
        url=f"ws://{host}:{resolve_self_listen_port()}/onebot/v11/ws",
        token=resolve_access_token(instance),
        # 要看见同账号其他程序发的消息（`message_sent`），协议端得上报自身消息。NA 自己发的那部分
        # 回传由适配器去重，见 `adapters/onebot_v11/core/self_sent.py`
        report_self_message=True,
        managed_by_nekro=True,
    )


def build_default_network_config(instance: DBAdapterInstance) -> NetworkConfig:
    """全新实例的默认网络配置：只有 NekroAgent 托管的那条反向 WS"""
    return NetworkConfig(entries=[build_managed_entry(instance)])


def _network_config_paths(instance: DBAdapterInstance) -> List[Path]:
    """配置文件写入目标。

    NapCat 以 `onebot11_{uin}.json` 按账号加载配置，未登录时用 `onebot11.json` 作为模板。
    账号已知时两份都写，保证「先配置后登录」与「换绑账号」两种顺序都能生效。
    """
    config_dir = get_napcat_config_dir(instance.instance_key)
    paths = [config_dir / "onebot11.json"]
    account = instance.provider_account_id.strip()
    if account.isdigit():
        paths.append(config_dir / f"onebot11_{account}.json")
    return paths


def _read_config_file(instance: DBAdapterInstance) -> Optional[_NapCatOnebotConfig]:
    """读出当前生效的 onebot11 配置；文件缺失或损坏时返回 None

    倒序遍历：账号专属文件（`onebot11_{uin}.json`）优先于模板文件。
    """
    for path in reversed(_network_config_paths(instance)):
        if not path.is_file():
            continue
        try:
            return _NapCatOnebotConfig.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"解析 {path} 失败，跳过该文件: {e}")
    return None


def _to_network_entry(entry_type: NetworkEntryType, raw: _NapCatEntry) -> NetworkEntry:
    """把 NapCat 的一条 camelCase 配置转成契约里的 snake_case 条目"""
    entry = NetworkEntry(
        type=entry_type,
        name=raw.name,
        enable=raw.enable,
        url=raw.url,
        host=raw.host,
        port=raw.port,
        token=raw.token,
        message_post_format=raw.messagePostFormat,
        report_self_message=raw.reportSelfMessage,
        debug=raw.debug,
        heart_interval_ms=raw.heartInterval,
        reconnect_interval_ms=raw.reconnectInterval,
        enable_force_push_event=raw.enableForcePushEvent,
    )
    entry.managed_by_nekro = is_managed_entry(entry)
    return entry


async def read_network_config(instance: DBAdapterInstance) -> NetworkConfig:
    """读取实例的协议端网络配置

    展平 NapCat 的五个数组并按类型标注，同时把托管条目识别出来。文件缺失、损坏或里面
    没有托管条目时，都补上一条按当前部署推导的托管连接——前端据此始终能展示「机器人
    该怎么连上来」，也保证下一次写回会把托管连接重新落盘。

    远程协议端（`remote_onebot`）没有本机配置文件，返回推导出的默认值供用户抄写。
    """
    if not _is_builtin_napcat(instance):
        if instance.provider.strip().lower() != InstanceProvider.REMOTE_ONEBOT.value:
            raise ValidationError(reason=f"实例 '{instance.instance_key}' 不使用 OneBot 协议端，没有网络配置")
        return build_default_network_config(instance)

    parsed = _read_config_file(instance)
    if parsed is None:
        return build_default_network_config(instance)

    entries: List[NetworkEntry] = []
    managed_seen = False
    for entry_type, raw_list in _section_arrays(parsed.network).items():
        for raw in raw_list:
            entry = _to_network_entry(entry_type, raw)
            if entry.managed_by_nekro:
                # 历史配置里可能有多条名为 ws 的反向连接，只把第一条认成托管条目，
                # 其余按用户条目原样展示，避免静默吞掉配置
                if managed_seen:
                    entry.managed_by_nekro = False
                else:
                    managed_seen = True
            entries.append(entry)

    if not managed_seen:
        entries.insert(0, build_managed_entry(instance))

    return NetworkConfig(
        entries=entries,
        music_sign_url=parsed.musicSignUrl,
        enable_local_file_to_url=parsed.enableLocalFile2Url,
        parse_multi_msg=parsed.parseMultMsg,
    )


def _normalize_entries(instance: DBAdapterInstance, entries: List[NetworkEntry]) -> List[NetworkEntry]:
    """归一化待写入的条目列表

    托管条目按后端推导重建：地址、令牌、名称、启用状态全部由服务端决定，调用方改不动
    （改错了机器人就连不上）；只有心跳/重连/调试开关这类不影响连通性的字段沿用提交值。
    其余条目一律清掉托管标记后原样保留——它们是用户自己加的连接，NA 无权改写。
    """
    managed = build_managed_entry(instance)
    user_entries: List[NetworkEntry] = []
    managed_seen = False

    for entry in entries:
        if not managed_seen and is_managed_entry(entry):
            managed = managed.model_copy(
                update={
                    "heart_interval_ms": entry.heart_interval_ms,
                    "reconnect_interval_ms": entry.reconnect_interval_ms,
                    "debug": entry.debug,
                },
            )
            managed_seen = True
            continue
        user_entries.append(entry.model_copy(update={"managed_by_nekro": False}))

    return [managed, *user_entries]


def _validate_entry_shape(entry: NetworkEntry, index: int) -> None:
    """单条配置的结构校验：名称合法、client 有合法 url、server 有合法端口"""
    label = f"第 {index + 1} 条连接"
    if not _ENTRY_NAME_PATTERN.match(entry.name.strip()):
        raise ValidationError(reason=f"{label}的名称 '{entry.name}' 非法，仅允许字母、数字、下划线、点与短横线，最长 64 位")
    if entry.heart_interval_ms <= 0 or entry.reconnect_interval_ms <= 0:
        raise ValidationError(reason=f"{label}的心跳间隔与重连间隔必须为正整数")

    if entry.type in _SERVER_ENTRY_TYPES:
        if not entry.host.strip():
            raise ValidationError(reason=f"{label}缺少监听地址")
        if not _MIN_PORT <= entry.port <= _MAX_PORT:
            raise ValidationError(reason=f"{label}的监听端口 {entry.port} 超出范围（{_MIN_PORT}-{_MAX_PORT}）")
        if entry.port == NAPCAT_WEBUI_PORT:
            raise ValidationError(reason=f"{label}的监听端口 {entry.port} 已被 NapCat WebUI 占用")
        return

    schemes = _CLIENT_URL_SCHEMES[entry.type]
    try:
        parsed = urlparse(entry.url)
    except ValueError as e:
        raise ValidationError(
            reason=f"{label}的地址 '{entry.url}' 非法，必须是完整的 {' / '.join(f'{s}://' for s in schemes)} 地址"
        ) from e
    if parsed.scheme.lower() not in schemes or not parsed.hostname:
        raise ValidationError(reason=f"{label}的地址 '{entry.url}' 非法，必须是完整的 {' / '.join(f'{s}://' for s in schemes)} 地址")


def _split_outbound_client_url(url: str) -> Tuple[str, str, Optional[int]]:
    """按严格形状拆出 (协议, 主机, 端口)，协议与主机已转小写

    NapCat 连接时按 WHATWG URL 规则解析地址，而校验用的是 Python / httpx 的解析，两边对反斜杠、
    用户名密码、百分号编码主机、数字简写主机（十进制整数、`127.1`、`0x7f.0.0.1`）、
    非 ASCII 主机、夹带制表符的地址会得出不同的主机，校验通过的地址 NapCat 可能连去别处。
    所以只放行所有解析器都得出同一个主机的形状，其余一律拒绝。

    Raises:
        ValueError: 形状有歧义或不受支持，文案可直接展示给用户
    """
    if _UNSAFE_URL_CHARS.search(url):
        raise ValueError("地址里不能有空白、控制字符或反斜杠")
    match = _OUTBOUND_URL_PATTERN.match(url)
    if match is None:
        raise ValueError(
            "主机部分只能是纯 ASCII 域名（中文域名请写成 xn-- 形式）、标准 x.x.x.x 或方括号包起来的 IPv6，"
            "不能带用户名密码（@）、多余斜杠、百分号或空端口",
        )
    scheme = match["scheme"].lower()
    host = match["host"].lower()
    port_text = match["port"]
    port = int(port_text) if port_text is not None else None
    if port is not None and not _MIN_PORT <= port <= _MAX_PORT:
        raise ValueError(f"端口 {port_text} 超出范围（{_MIN_PORT}-{_MAX_PORT}）")
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError as e:
            raise ValueError("方括号里不是合法的 IPv6 地址") from e
    elif _NUMERIC_TAIL_PATTERN.search(host):
        if _CANONICAL_IPV4_PATTERN.match(host) is None or any(int(part) > 255 for part in host.split(".")):
            raise ValueError(
                "数字形式的主机必须写成标准的 x.x.x.x（每段 0-255、不带前导 0），不接受十六进制、八进制、简写与末尾点"
            )
    return scheme, host, port


async def _assert_outbound_url_safe(entry: NetworkEntry, index: int) -> None:
    """校验用户填写的出站地址

    `websocketClients` 与 `httpClients` 由协议端主动向该地址发起连接 / POST 事件，
    地址完全由用户填写，等价于一个 SSRF 入口（内网探测、云元数据窃取）。这里复用下载
    链路的白名单校验：只允许 http/https（ws/wss 按同源的 http/https 校验），且解析出的
    每个 IP 都必须是公网地址。

    托管条目不走这里——它的地址指向 NA 自身（容器网络内的服务名，必然是私网地址），
    调用方传什么都会在 `_normalize_entries` 里被后端生成值覆盖，所以既没有校验的必要，
    也不存在「打个托管标记就绕过校验」的缝隙：绕过的代价是地址被丢弃。

    NapCat 按 WHATWG 规则解析地址，而校验用的是 Python / httpx 的解析，同一个字符串可以
    得出不同的主机。因此先按严格形状拆出主机与端口，再用它们重拼一个干净的探测地址交给
    公网校验：路径、查询、片段不参与判定，校验的主机就恒等于形状检查拆出来的那个。
    """
    if entry.managed_by_nekro or entry.type not in _CLIENT_URL_SCHEMES:
        return

    prefix = f"第 {index + 1} 条连接的地址不被允许: "
    try:
        scheme, host, port = _split_outbound_client_url(entry.url)
    except ValueError as e:
        raise ValidationError(reason=f"{prefix}{e}") from e
    if scheme not in _CLIENT_URL_SCHEMES[entry.type]:
        raise ValidationError(reason=f"{prefix}协议 {scheme}:// 不适用于该类型的连接")

    # 只把拆出来的主机和端口交给校验：路径、查询、片段不参与判定，也就没有「校验看到的主机」与「实际连接的主机」不一致的缝隙
    probe_scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    probe_url = f"{probe_scheme}://{host}{f':{port}' if port is not None else ''}/"
    try:
        await assert_download_url_safe(probe_url)
    except UnsafeDownloadUrlError as e:
        raise ValidationError(reason=f"{prefix}{e}") from e


async def _validate_entries(entries: List[NetworkEntry]) -> None:
    """整表校验：同类型内名称唯一、端口不冲突、地址合法且安全"""
    seen_names: Set[Tuple[NetworkEntryType, str]] = set()
    seen_ports: Set[int] = set()

    for index, entry in enumerate(entries):
        _validate_entry_shape(entry, index)

        name = entry.name.strip()
        if not entry.managed_by_nekro and entry.type == MANAGED_ENTRY_TYPE and (
            name == MANAGED_ENTRY_NAME or name in LEGACY_MANAGED_ENTRY_NAMES
        ):
            raise ValidationError(reason=f"名称 '{name}' 由 NekroAgent 托管连接保留，请为自建连接换一个名称")

        key = (entry.type, name)
        if key in seen_names:
            raise ValidationError(reason=f"同类型下存在重名连接: '{name}'")
        seen_names.add(key)

        if entry.type in _SERVER_ENTRY_TYPES:
            if entry.port in seen_ports:
                raise ValidationError(reason=f"监听端口 {entry.port} 被多条连接占用")
            seen_ports.add(entry.port)

        await _assert_outbound_url_safe(entry, index)


def _dump_entry(entry: NetworkEntry, extra_fields: Dict[str, Any]) -> Dict[str, Any]:
    """把一条配置序列化成 NapCat 的 camelCase 形态

    `extra_fields` 是同名条目在原文件里携带、但契约没有覆盖的字段（如 `enableCors`），
    先铺底再用契约字段覆盖，保证用户在 NapCat WebUI 里调过的选项不会被 NA 抹平。
    """
    payload: Dict[str, Any] = dict(extra_fields)
    for field in _NAPCAT_FIELDS_BY_TYPE[entry.type]:
        payload[field] = getattr(entry, _NAPCAT_FIELD_TO_ENTRY_ATTR[field])
    return payload


def _collect_extra_fields(parsed: Optional[_NapCatOnebotConfig]) -> Dict[Tuple[NetworkEntryType, str], Dict[str, Any]]:
    """按「类型 + 名称」索引原文件里的未知字段，供写回时回填"""
    if parsed is None:
        return {}
    collected: Dict[Tuple[NetworkEntryType, str], Dict[str, Any]] = {}
    for entry_type, raw_list in _section_arrays(parsed.network).items():
        for raw in raw_list:
            extra = raw.model_extra
            if extra:
                collected[(entry_type, raw.name.strip())] = dict(extra)
    return collected


def _persist_network_config(instance: DBAdapterInstance, network: NetworkConfig) -> None:
    """把归一化后的配置落盘

    与 `write_network_config` 分开：容器创建 / 重启时需要重新落盘一份已经校验过的配置，
    这条路径不该再跑一遍带 DNS 解析的出站地址校验（临时解析失败不该拦住重启）。
    """
    parsed = _read_config_file(instance)
    extras = _collect_extra_fields(parsed)

    arrays: Dict[str, List[Dict[str, Any]]] = {entry_type.value: [] for entry_type in NetworkEntryType}
    for entry in network.entries:
        arrays[entry.type.value].append(_dump_entry(entry, extras.get((entry.type, entry.name.strip()), {})))

    section: Dict[str, Any] = dict(parsed.network.model_extra or {}) if parsed is not None else {}
    section.update(arrays)
    section["plugins"] = parsed.network.plugins if parsed is not None else []

    payload: Dict[str, Any] = dict(parsed.model_extra or {}) if parsed is not None else {}
    payload["network"] = section
    payload["musicSignUrl"] = network.music_sign_url
    payload["enableLocalFile2Url"] = network.enable_local_file_to_url
    payload["parseMultMsg"] = network.parse_multi_msg

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    config_dir = get_napcat_config_dir(instance.instance_key)
    config_dir.mkdir(parents=True, exist_ok=True)
    for path in _network_config_paths(instance):
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            raise OperationFailedError(operation=f"写入配置文件 {path.name}", detail=str(e)) from e


async def write_network_config(instance: DBAdapterInstance, network: NetworkConfig) -> None:
    """写入实例的协议端网络配置

    托管条目按后端推导重建，用户自建条目校验后原样写回；两类条目按 `type` 分别归位到
    NapCat 的五个数组中。
    """
    _require_builtin_napcat(instance)
    entries = _normalize_entries(instance, network.entries)
    await _validate_entries(entries)
    _persist_network_config(instance, network.model_copy(update={"entries": entries}))
    logger.info(f"已更新实例 {instance.instance_key} 的协议端网络配置（共 {len(entries)} 条连接）")


async def resync_network_config(instance: DBAdapterInstance) -> None:
    """把当前配置原样重新落盘

    用于创建 / 重启容器：托管条目按最新部署参数（服务名、端口、令牌）重建，用户自建的
    连接一条不动地带过去。不重跑出站地址校验——那是写入时的准入检查，重启时再做一次
    只会让一次临时 DNS 故障变成重启失败。
    """
    _require_builtin_napcat(instance)
    current = await read_network_config(instance)
    entries = _normalize_entries(instance, current.entries)
    _persist_network_config(instance, current.model_copy(update={"entries": entries}))


# ---------------------------------------------------------------------------
# NapCat WebUI 客户端（扫码 / 快速登录的主方案）
# ---------------------------------------------------------------------------


class _NapCatEnvelope(BaseModel):
    """NapCat WebUI 的统一响应信封"""

    code: int = -1
    message: str = ""
    data: Any = None


class _QrCodePayload(BaseModel):
    qrcode: str = ""
    qrcodeurl: str = ""


class _LoginStatusPayload(BaseModel):
    isLogin: bool = False
    qrcodeurl: str = ""
    # NapCat 会把"二维码已过期，请刷新"之类的提示放这里，透给用户比干等有用
    loginError: str = ""


class _QuickLoginEntry(BaseModel):
    uin: str = ""
    isQuickLogin: bool = False


class _QQLoginInfoPayload(BaseModel):
    """`POST /api/QQLogin/GetQQLoginInfo` 的响应体

    NapCat 侧的取值来自 `ve.getQQLoginInfo()`，即登录收尾时 `ve.setQQLoginInfo(selfInfo)`
    写入的那份 `{uid, uin, nick}`（外加 WebUI 现拼上的 `online` / `avatarUrl`）。
    **未登录时三个字段都是空串**，实测（测试部署的 `nekro_napcat_inst1`）返回
    `{"code":0,"data":{"uid":"","uin":"","nick":""},"message":"success"}`。

    `uin` 声明成 `Union[int, str]`：NapCat 内部写的是 `t.uin.toString()`，说明上游那份
    `selfInfo.uin` 可以是数字，序列化出来就是 JSON number，按 `str` 严格校验会解析失败。
    """

    uid: str = ""
    uin: Union[int, str] = ""
    nick: str = ""
    online: Optional[bool] = None


def _read_webui_token(instance_key: str) -> str:
    """从容器配置目录读取 NapCat WebUI 令牌"""
    token_file = get_napcat_config_dir(instance_key) / "webui.json"
    if not token_file.is_file():
        return ""
    try:
        parsed = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"读取 {token_file} 失败: {e}")
        return ""
    token = parsed.get("token") if isinstance(parsed, dict) else None
    return str(token) if token else ""


async def _resolve_container_dns_name(instance_key: str) -> str:
    """容器网络里这个实例的容器**此刻**该用哪个名字当主机名。

    容器名现在带部署前缀，但升级前建的存量容器还顶着裸模板名——按新名拼 URL
    会 DNS 解析失败，扫码 / 快速登录 / 登录账号推导（入站链路）在升级当天全部失效。
    所以按候选表找**归属校验通过**的那个：邻居同名裸模板容器即便解析得到，也不该被 probing。

    Docker 不可用 / 候选都不存在时，按当前口径回一个名字让连接自己失败——
    与改造前的行为形态一致（那时也只是无检查地拼模板名）。
    """
    candidates = container_name_candidates(instance_key)
    try:
        async with _docker_client() as docker:
            for name in candidates:
                info = await _inspect_container(docker, name)
                if info is None:
                    continue
                try:
                    _assert_owned(info, instance_key, name)
                except PermissionDeniedError:
                    continue
                return name
    except (DockerError, OSError) as e:
        logger.warning(f"解析实例 {instance_key} 的容器网络主机名失败，按当前口径回答: {e}")
    return candidates[0]


async def _webui_base_url(instance: DBAdapterInstance) -> str:
    """WebUI 基地址。

    NA 在 Docker 内时走容器网络直连容器名（名字按候选表解析，见 `_resolve_container_dns_name`），
    因为端口只绑在宿主机回环上，NA 容器的 127.0.0.1 并不是宿主机的回环。
    """
    if OsEnv.RUN_IN_DOCKER:
        return f"http://{await _resolve_container_dns_name(instance.instance_key)}:{NAPCAT_WEBUI_PORT}"
    port = read_container_metadata(instance).webui_port or NAPCAT_WEBUI_PORT
    return f"http://127.0.0.1:{port}"


def _napcat_auth_hash(token: str) -> str:
    """计算 NapCat WebUI 的登录哈希

    算法取自 NapCat WebUI 前端实现：`SHA256(token + ".napcat")`。
    这个加盐是必须的——只传 md5(token) 或明文都会被服务端以 "token is invalid" 拒绝。
    """
    return hashlib.sha256(f"{token}{NAPCAT_AUTH_SALT}".encode()).hexdigest()


async def _webui_login(client: httpx.AsyncClient, base_url: str, token: str) -> str:
    """换取 WebUI 访问凭证，失败返回空串。

    主形态是加盐 SHA256；旧版本曾直接收明文 token 的 md5，一并保留作兼容尝试。
    """
    bodies = [
        {"hash": _napcat_auth_hash(token)},
        {"hash": hashlib.md5(token.encode("utf-8")).hexdigest()},
    ]
    for body in bodies:
        try:
            resp = await client.post(f"{base_url}/api/auth/login", json=body)
        except httpx.HTTPError as e:
            logger.debug(f"WebUI 登录请求失败: {e}")
            return ""
        if resp.status_code != 200:
            continue
        try:
            envelope = _NapCatEnvelope.model_validate(resp.json())
        except ValueError:
            continue
        if envelope.code != 0 or not isinstance(envelope.data, dict):
            continue
        credential = envelope.data.get("Credential") or envelope.data.get("credential")
        if credential:
            return str(credential)
    return ""


async def _webui_call(
    client: httpx.AsyncClient,
    base_url: str,
    method: str,
    path: str,
    credential: str,
    json_body: Optional[Dict[str, Any]] = None,
) -> Optional[_NapCatEnvelope]:
    """调用一个 WebUI 接口，网络或协议异常统一收敛为 None"""
    try:
        resp = await client.request(
            method,
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {credential}"},
            json=json_body,
        )
    except httpx.HTTPError as e:
        logger.debug(f"WebUI 调用 {path} 失败: {e}")
        return None
    if resp.status_code != 200:
        return None
    try:
        return _NapCatEnvelope.model_validate(resp.json())
    except ValueError:
        return None


@contextlib.asynccontextmanager
async def _webui_session(
    instance: DBAdapterInstance,
    timeout: float = NAPCAT_HTTP_TIMEOUT_SECONDS,
) -> AsyncIterator[Optional[Tuple[httpx.AsyncClient, str, str]]]:
    """建立一次 WebUI 会话，产出 (client, base_url, credential)；不可用时产出 None

    `timeout` 可收紧：跑在入站链路上的归属推导不能沿用面向人的 5 秒（见
    `NAPCAT_LOGIN_PROBE_TIMEOUT_SECONDS`）。
    """
    token = _read_webui_token(instance.instance_key)
    if not token:
        yield None
        return
    base_url = await _webui_base_url(instance)
    async with httpx.AsyncClient(timeout=timeout) as client:
        credential = await _webui_login(client, base_url, token)
        if not credential:
            yield None
            return
        yield client, base_url, credential


# ---------------------------------------------------------------------------
# 登录账号的推导与回写
# ---------------------------------------------------------------------------


def _normalize_account_id(raw: Union[int, str]) -> str:
    """把协议端给出的账号号码归一化成可入库的字符串；不是纯数字一律判空

    宁可判空也不要写进去：这个值随后会成为入站路由的唯一依据，写错一次的代价是
    该账号的消息全部投给另一个实例。
    """
    text = str(raw).strip()
    return text if text.isdigit() else ""


async def _read_login_uin(client: httpx.AsyncClient, base_url: str, credential: str) -> str:
    """在一次已建立的 WebUI 会话里问出「当前登录的是哪个账号」，未登录返回空串"""
    envelope = await _webui_call(client, base_url, "POST", "/api/QQLogin/GetQQLoginInfo", credential)
    if envelope is None or envelope.code != 0 or not isinstance(envelope.data, dict):
        return ""
    try:
        payload = _QQLoginInfoPayload.model_validate(envelope.data)
    except ValueError as e:
        logger.warning(f"解析 NapCat 登录信息失败: {e}")
        return ""
    return _normalize_account_id(payload.uin)


async def read_container_login_account(instance: DBAdapterInstance) -> str:
    """读出该实例的 NapCat 容器此刻登录的账号号码；未登录 / 不可达返回空串

    这是「登录成功」这一事实在 NA 侧唯一可靠的读法：不解析日志文案（版本一变就失效），
    不猜文件名，直接问容器自己的 WebUI。非本机托管形态没有可问的容器，直接返回空串
    而不是抛异常——调用方是在**遍历候选**，一个候选不合格不该打断整轮推导。
    """
    if not _is_builtin_napcat(instance):
        return ""
    async with _webui_session(instance, timeout=NAPCAT_LOGIN_PROBE_TIMEOUT_SECONDS) as session:
        if session is None:
            return ""
        client, base_url, credential = session
        return await _read_login_uin(client, base_url, credential)


def _is_bindable(instance: DBAdapterInstance) -> bool:
    """该实例能不能接收一次账号回写

    停用 / 归档 / 墓碑三种态一律不接：把一个正在收消息的协议端绑到收发闸门关着的实例上，
    这条 Bot 会当场彻底静默，而界面上不会有任何一处说它坏了。判据与
    `adopt_unclaimed_instance` 的拦截同源（`NON_DISPATCHABLE_STATUSES` + `enabled`），
    两处一旦分叉就会出现「认领接口拦得住、自动回写拦不住」的洞。
    """
    return instance.enabled and instance.status.strip().lower() not in NON_DISPATCHABLE_STATUSES


async def bind_login_account(instance: DBAdapterInstance, account_id: str) -> bool:
    """把登录成功的账号回写到该实例，并让状态从 `unbound` 落回 `offline`

    幂等：账号已经是这一行的就只补一次状态同步。返回值表示「回写后这一行是否持有该账号」。

    ## 为什么写之前要重新查一遍库

    调用方手上的 `instance` 可能是几百毫秒前读出来的（归属推导要遍历候选、还可能重试），
    这期间管理员完全可能在总后台把账号手工填上、或者把实例停用。拿旧快照直接 save 会把
    这些改动覆盖掉，而 `provider_account_id` 恰恰是入站路由的唯一依据。

    ## 账号唯一性

    写之前过一遍 `find_account_binding_conflict`，判据与创建/改绑侧完全相同。冲突时**不写**
    并留一条 WARNING：此时正确的出路是认领流程（人来决定历史归谁），不是让自动回写
    再造一行同账号的绑定。
    """
    account = _normalize_account_id(account_id)
    if not account:
        return False

    # 延迟导入：`instance_manager` 会反向 import 本模块（`build_instance_info` 取容器状态），
    # 模块级导入会成环
    from nekro_agent.services.console.instance_manager import sync_account_binding_status

    fresh = await DBAdapterInstance.get_or_none(id=instance.id)
    if fresh is None:
        logger.warning(f"回写登录账号失败，实例已不存在: instance_key={instance.instance_key}")
        return False

    bound = fresh.provider_account_id.strip()
    if bound == account:
        await sync_account_binding_status(fresh)
        return True
    if bound:
        logger.warning(
            f"实例 {fresh.instance_key} 已绑定账号 {bound}，不接受登录账号 {account} 的回写；"
            "一个实例只能绑一个账号，请先在总后台为它解绑",
        )
        return False
    if not _is_bindable(fresh):
        logger.warning(
            f"实例 {fresh.instance_key} 当前不可绑定（enabled={fresh.enabled}, status={fresh.status}），"
            f"跳过账号 {account} 的回写",
        )
        return False

    holder = await DBAdapterInstance.find_account_binding_conflict(
        fresh.adapter_key,
        account,
        exclude_id=fresh.id,
    )
    if holder is not None:
        logger.warning(
            f"账号 {account} 已绑定到实例 '{holder.instance_key}'，跳过对 '{fresh.instance_key}' 的回写；"
            "两行绑同一账号会让入站路由失去唯一答案，请走认领流程",
        )
        return False

    # 登录回写到租户实例上：这个账号此前出示过的令牌在租户手里（与 `update_instance` 改绑同一条规矩）
    from nekro_agent.services.user.token_exposure import record_account_landing_on_tenant

    await record_account_landing_on_tenant(fresh, account, reason=f"登录账号回写到租户实例 {fresh.instance_key}")
    fresh.provider_account_id = account
    await fresh.save(update_fields=["provider_account_id", "update_time"])
    # 账号绑定关系变了就必须让入站路由缓存失效，与 `update_instance` 同一条规矩
    clear_instance_cache()
    await sync_account_binding_status(fresh)
    logger.info(f"内置 NapCat 登录成功，已把账号 {account} 回写到实例 {fresh.instance_key}")
    return True


async def list_instances_awaiting_account(adapter_key: str) -> List[DBAdapterInstance]:
    """列出「已经建好、还等着一个账号落到自己名下」的实例

    两个条件：尚未绑定账号（已绑定的不该被改写），以及可绑定（见 `_is_bindable`，
    它把停用/归档/墓碑三种态挡在外面）。

    ## 两个调用方，一条判据

    1. `_list_login_attribution_candidates`：内置 NapCat 扫码登录后推导「这次登录是谁发起的」；
    2. `adapters/onebot_v11/core/access_token.py`：握手时账号还没绑给任何实例，
       此刻能证明「我是总后台建出来的那个协议端」的只有实例自己那枚令牌。

    两处对「等着绑账号的实例」如果各写一套判据，就会出现「A 认它、B 不认它」的窗口，
    而窗口里的表现是协议端连不上或账号绑错行。

    `provider` 在库里是自由字符串，所以只把「未绑账号」交给数据库过滤，形态判定留给调用方
    （`_is_builtin_napcat` 那一份归一化实现）——未绑定账号的实例本来就是个位数，
    多读几行远比在两处各写一套 provider 判据安全。
    """
    rows = await DBAdapterInstance.filter(adapter_key=adapter_key, provider_account_id="").order_by("id")
    return [row for row in rows if _is_bindable(row)]


async def _list_login_attribution_candidates(adapter_key: str) -> List[DBAdapterInstance]:
    """列出「可能是这次登录发起方」的实例

    在 `list_instances_awaiting_account` 之上再加一条：本机托管的 NapCat（有容器可问）。

    候选数决定了一次推导要并发发出多少个 WebUI 请求。它的上界是「已建好但还没扫码的内置实例」，
    也就是**批量装机那几天**才会大；装完之后恒为 0，整条推导连一次容器往返都不会发生。
    没有容器（`webui.json` 还不存在）的候选在 `_webui_session` 里读文件就返回了，不产生 HTTP。
    """
    return [row for row in await list_instances_awaiting_account(adapter_key) if _is_builtin_napcat(row)]


async def claim_account_from_builtin_login(adapter_key: str, account_id: str) -> Optional[DBAdapterInstance]:
    """协议端带着一个陌生账号连上来时，找出发起这次登录的内置实例并把账号回写给它

    ## 为什么归属能被唯一确定

    内置 NapCat 的容器是 NA 亲手创建的（容器名模板 + 归属标签 + 后端生成的挂载路径），
    「容器 ↔ 实例」这条边没有歧义；缺的只是「容器 ↔ 账号」，而那正好可以问容器自己的 WebUI。
    两条边接起来就是「账号 ↔ 实例」，不需要日志文本匹配，也不需要新起一条轮询。

    ## 匹配不上时一律返回 None

    返回 None 的含义是「按改造前的老路走」——登记成 `ob{QQ}` 未认领实例，由人来认领。
    所有说不准的情形都归到这里：没有候选、候选都答不上、恰好两个容器报同一个账号
    （物理上不该发生，真发生了就是脏状态，交给人比替他猜好）。**外部协议端天然落在这里**，
    它本来就没有「哪个实例发起的」这一信息，行为逐字不变。
    """
    account = _normalize_account_id(account_id)
    if not account:
        return None

    for index, delay in enumerate((0.0, *LOGIN_ATTRIBUTION_RETRY_DELAYS)):
        if delay:
            await asyncio.sleep(delay)

        candidates = await _list_login_attribution_candidates(adapter_key)
        if not candidates:
            return None

        probed = await asyncio.gather(
            *(read_container_login_account(candidate) for candidate in candidates),
            return_exceptions=True,
        )

        matched: List[DBAdapterInstance] = []
        pending = False
        for candidate, result in zip(candidates, probed, strict=True):
            if isinstance(result, BaseException):
                logger.warning(f"读取实例 {candidate.instance_key} 的登录账号失败: {result}")
                pending = True
                continue
            if result == account:
                matched.append(candidate)
            elif not result:
                # 容器不可达、或登录收尾还没公布账号 —— 两者都值得再等一拍
                pending = True

        if len(matched) == 1:
            owner = matched[0]
            if not await bind_login_account(owner, account):
                return None
            # 手上这份候选快照是遍历开始时读的，账号列还是空的；回读一行交出去，
            # 免得调用方拿着一个"看起来没绑账号"的对象继续往下走
            return await DBAdapterInstance.get_or_none(id=owner.id)
        if len(matched) > 1:
            logger.warning(
                f"账号 {account} 同时被多个内置容器报告为已登录，无法确定归属，按未认领协议端处理: "
                f"{[item.instance_key for item in matched]}",
            )
            return None
        if not pending:
            # 候选全都明确答出了别的账号，再等也不会变
            logger.debug(f"账号 {account} 未匹配到任何内置 NapCat 容器（第 {index + 1} 轮，无待定候选）")
            return None

    logger.debug(f"账号 {account} 在全部重试轮次内都未匹配到内置 NapCat 容器")
    return None


# ---------------------------------------------------------------------------
# 扫码登录与快速登录
# ---------------------------------------------------------------------------


def _extract_qrcode_url_from_logs(log_lines: List[str]) -> str:
    """从容器日志里捞出登录二维码链接（兜底方案）

    NapCat 启动未登录时会把二维码内容以 URL 形式打印出来；取最后一次出现的、
    形态上属于 QQ 登录域的链接。
    """
    for line in reversed(log_lines):
        lowered = line.lower()
        if not any(hint in lowered for hint in _QRCODE_URL_HINTS):
            continue
        matches = _QRCODE_URL_PATTERN.findall(line)
        if matches:
            return str(matches[-1]).rstrip(".,;)\"'")
    return ""


async def get_login_qrcode(instance: DBAdapterInstance) -> LoginQrCodeResponse:
    """获取扫码登录信息

    主方案走 WebUI HTTP API，兜底从容器日志提取链接。任何一步失败都返回
    `available=False` + 说明文本，而不是抛异常——扫码页需要的是状态而非错误页。

    ## 为什么一个 GET 会写库

    扫码弹窗本来就在轮询这个端点，`isLogin` 变真就是「登录成功」这件事最早被 NA 观测到的
    时刻，而此刻**发起登录的实例是谁毫无歧义**（就是路径上那个 `instance_key`）。在这里顺手
    把账号回写，覆盖的是「登录成功了、但协议端还没连回来」那段窗口——反向 WS 配错时它可以是
    永远。回写是幂等的，且与入站链路那一侧共用同一个 `bind_login_account`，两条路径抢到
    一起时只有一条会真正落库。
    """
    _require_builtin_napcat(instance)

    info = await get_container_info(instance)
    if info is None or info.state == ContainerState.ABSENT:
        return LoginQrCodeResponse(message="容器不存在，请先创建容器")
    if info.state != ContainerState.RUNNING:
        return LoginQrCodeResponse(message="容器未运行，请先启动容器")

    async with _webui_session(instance) as session:
        if session is not None:
            client, base_url, credential = session

            # CheckLoginStatus 同时给出登录态与二维码链接，NapCat 并没有独立的取二维码接口
            status = await _webui_call(client, base_url, "POST", "/api/QQLogin/CheckLoginStatus", credential)
            if status is not None and status.code == 0 and isinstance(status.data, dict):
                login_status = _LoginStatusPayload.model_validate(status.data)
                if login_status.isLogin:
                    account = await _read_login_uin(client, base_url, credential)
                    bound = await bind_login_account(instance, account) if account else False
                    return LoginQrCodeResponse(
                        message=f"账号 {account} 已登录并绑定到本实例" if bound else "账号已登录，无需扫码",
                        bound_account_id=account if bound else "",
                    )
                if login_status.qrcodeurl:
                    # loginError 常见取值是"二维码已过期，请刷新"，此时 NapCat 会自行重新出码，
                    # 原样透出让用户知道该点刷新，而不是干等
                    message = login_status.loginError or "请使用手机 QQ 扫码登录"
                    return LoginQrCodeResponse(
                        available=True,
                        qrcode_url=login_status.qrcodeurl,
                        message=message,
                    )

    # 兜底：WebUI 不可用（令牌未生成 / 接口形态变化）时从日志提取
    log_lines = await _read_container_logs(instance, tail=QRCODE_LOG_SCAN_LINES)
    qrcode_url = _extract_qrcode_url_from_logs(log_lines)
    if qrcode_url:
        return LoginQrCodeResponse(
            available=True,
            qrcode_url=qrcode_url,
            message="已从容器日志提取二维码链接",
        )

    return LoginQrCodeResponse(message="暂时无法获取二维码，请稍后重试或检查 NapCat WebUI 是否就绪")


async def _read_container_logs(instance: DBAdapterInstance, tail: int) -> List[str]:
    """读取容器日志；失败返回空列表，不打断调用方"""
    try:
        async with _owned_container(instance.instance_key) as (container, _info):
            return list(await container.log(stdout=True, stderr=True, tail=tail))
    except (DockerError, OSError, NotFoundError, PermissionDeniedError) as e:
        logger.warning(f"读取实例 {instance.instance_key} 容器日志失败: {e}")
        return []


async def quick_login(instance: DBAdapterInstance) -> str:
    """使用已缓存的登录态快速登录，返回结果说明文本

    ## 这里也要回写账号

    快速登录与扫码是同一件事——「这个容器登录上了某个账号」——的两种触发方式，而这一侧
    **发起方同样毫无歧义**（就是路径上那个实例）。删掉容器再重建时登录态数据是保留的
    （`delete_container` 明说"登录态数据已保留"），所以「实例还没绑账号、容器里却有可用的
    历史登录态」是一个真实形态：此时 `uin` 从 `GetQuickLoginListNew` 里读出来，NA 手上
    「容器 ↔ 实例」与「容器 ↔ 账号」两条边都齐了，不回写就是把 `get_login_qrcode` 那半边
    的修复漏掉一半。

    ## 判据仍然只认容器自己答出来的账号

    `SetQuickLogin` 返回 code 0 只表示**请求被受理**，不表示登录已经落地；照它就绑，
    一次失败的快速登录会把实例从 `unbound` 推到 `offline`，于是掉线监测开始为一个从未上线过的
    实例告警——那正是 `unbound` 这个状态当初被分出来要消灭的误导。所以这里再问一次
    `GetQQLoginInfo`（与扫码轮询、入站推导共用的同一条判据），答不上就什么都不做，
    逐字回到改造前：入站链路与扫码轮询两条路照样会补上这一笔。
    """
    _require_builtin_napcat(instance)

    info = await get_container_info(instance)
    if info is None or info.state != ContainerState.RUNNING:
        raise OperationFailedError(operation="快速登录", detail="容器未运行")

    async with _webui_session(instance) as session:
        if session is None:
            raise OperationFailedError(operation="快速登录", detail="NapCat WebUI 不可用")
        client, base_url, credential = session

        uin = instance.provider_account_id.strip()
        if not uin:
            listing = await _webui_call(client, base_url, "GET", "/api/QQLogin/GetQuickLoginListNew", credential)
            if listing is not None and listing.code == 0 and isinstance(listing.data, list):
                for raw in listing.data:
                    if not isinstance(raw, dict):
                        continue
                    entry = _QuickLoginEntry.model_validate(raw)
                    if entry.isQuickLogin and entry.uin:
                        uin = entry.uin
                        break
        if not uin:
            raise OperationFailedError(operation="快速登录", detail="没有可用的历史登录账号，请先扫码登录")

        result = await _webui_call(
            client,
            base_url,
            "POST",
            "/api/QQLogin/SetQuickLogin",
            credential,
            json_body={"uin": uin},
        )
        if result is None or result.code != 0:
            detail = result.message if result is not None else "NapCat 无响应"
            raise OperationFailedError(operation="快速登录", detail=detail)

        # 只认容器自己答出来的账号；答不上（登录还在途）就不写，见 docstring
        confirmed = await _read_login_uin(client, base_url, credential)

    bound = await bind_login_account(instance, confirmed) if confirmed else False

    logger.info(f"实例 {instance.instance_key} 已通过快速登录恢复账号 {uin}")
    if bound:
        return f"已触发账号 {confirmed} 的快速登录，并已绑定到本实例"
    return f"已触发账号 {uin} 的快速登录"
