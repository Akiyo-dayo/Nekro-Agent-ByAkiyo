"""CC 工作区的模型凭据下发

## 问题：容器里的那把 Key 就是平台方的 Key

改造前 `settings.json` 与 `.claude/settings.json` 里写的是 CC 模型预设的 `auth_token` 原文，
也就是平台方向上游（默认 `https://api.nekro.ai/claude`）付费的那把 Key。于是租户对**自己的**
工作区调 `POST /workspaces/{id}/comm/send` 让 CC「读一下 /workspace/settings.json」就拿到了它——
API 层的租户隔离对这条路无效，因为那确实是他自己的工作区。

## 先查清 CC 到底怎么用这把 Key（结论决定了哪些修法是假的）

翻 `kromiose/nekro-cc-sandbox` 镜像里的实现（`src/nekro_cc_sandbox/`）：

- `settings.py:Settings.get_env_vars()` 把 `providers[active].base_url / auth_token / model`
  原样翻译成 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL`；
- `claude/runtime.py:_get_env_overrides()` 每次 spawn 前**重新读盘**，
  `send_message_in_workspace()` 把它 `env.update` 进 `claude` CLI 子进程的环境。

也就是说这把 Key 只有一个用途：**给 `claude` CLI 当 Anthropic 兼容 API 的 Bearer**。
`base_url` 是完全可重定向的——预设默认值本来就不是 `api.anthropic.com` 而是一个中转站。

由此排掉两条「看起来能修」的路：

- **「用完就删 settings.json」不成立**：Key 必须在整个 LLM 调用期间待在 `claude` 进程的环境里，
  而 CC 的 Bash 工具是它的子进程，`echo $ANTHROPIC_AUTH_TOKEN` / 读 `/proc/self/environ`
  一样拿得到。文件只是暴露面之一，删文件只是把门关上、窗户还开着。
- **「上游签子 Key」不成立**：上游是第三方中转站，没有可编程的子 Key 接口，
  也不能要求所有部署方都换成支持子 Key 的上游。

## 所以走「代理转发」：容器里放一把只对本工作区有效的短令牌

`base_url` 指向 NA 自己的 `/api/cc-relay`，`auth_token` 换成**每工作区一把**的中继令牌。
`routers/cc_relay.py` 校验令牌 → 定位工作区 → 取它生效的预设 → 用**真实** Key 转发到真实上游。
真 Key 只存在于 NA 进程与磁盘上的 `configs/cc-model-presets.json`（那本来就是平台方的地盘），
一个字节都不进容器。

令牌形如 `nkcc-{workspace_id}-{secret}`：工作区 ID 直接编在令牌里，中继端点按 ID 取行、
再对 `secret` 做常量时间比较，不需要全表扫描，也不需要一份会失效的内存索引。

### 代价（如实记，不要装作没有）

1. **CC 的 LLM 调用从此依赖 NA 进程存活。** 改前 NA 重启时容器里在跑的任务能继续跑完，
   结果缓存在容器里等 NA 回来 `GET /pending-results`（`services/workspace/client.py`）；
   改后 NA 重启窗口内 CC 发出的 API 请求会连接失败，CLI 自身重试撑不过去就是这一轮失败。
   换来的是「平台方 Key 不在容器里」。这条必须写进文档，不能假装不存在。
2. **多一跳网络。** 同宿主同网桥的一跳，相对一次 LLM 请求的秒级耗时可以忽略，
   但要求转发**真正流式**：中继端点用 `aiter_raw()` 逐块透传，不缓冲整个响应体，
   否则 CC 的打字机效果会退化成「憋完再吐」。
3. **容器 -> NA 多了一条网络路径。** 工作区守卫此前刻意「一条容器 -> NA 的放行都没有」
   （见 `network_guard` 模块 docstring）。现在必须开一个口子，收窄成
   「目的地 = NA 在本网桥上的地址 + NA 的监听端口」——按**目的地**匹配而不是按源地址，
   所以容器伪造源地址换不到额外权限。

### 令牌泄漏了会怎样

CC 仍然能读到中继令牌（它就在 `settings.json` 与子进程环境里），泄漏后攻击者能做的是
「以这个工作区的名义烧平台方的模型额度」——而 CC 本来就在烧同一份额度。
拿不到的是：真实 Key 本身、其他工作区、以及上游中转站上除 `/v1/messages` 之外的任何接口
（中继端点是路径白名单）。令牌在**每次容器创建/重启**时轮换，所以泄漏的有效期最长到下次重建。

## 配置

两个开关刻意放在环境变量而不是 WebUI 配置里，与 `OsEnv.ALLOW_PRIVATE_NETWORK_DOWNLOAD`
同一个理由：这是平台方与租户之间的安全边界，不该出现在租户能改的那一层
（四层配置里承载凭据的项一律不标 `overridable`，见 PROJECT_STATE §1.2）。

- `NEKRO_CC_CREDENTIAL_MODE`：`relay`（默认）或 `direct`（改造前的行为，真 Key 进容器）。
  部署确实无法让工作区容器回连 NA 时才用 `direct`。
- `NEKRO_CC_RELAY_BASE_URL`：容器视角下的中继地址，留空则自动推导（见 `resolve_relay_base_url`）。
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from nekro_agent.core.core_utils import OsEnvTypes
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_workspace import DBWorkspace

if TYPE_CHECKING:
    from nekro_agent.core.cc_model_presets import CCModelPresetItem

logger = get_sub_logger("cc_credential")

# 中继端点挂载路径（`routers/__init__.py` 把所有路由挂在 /api 下）
RELAY_ROUTE_PREFIX = "/cc-relay"
RELAY_PUBLIC_PREFIX = f"/api{RELAY_ROUTE_PREFIX}"

# 令牌与 metadata 键
RELAY_TOKEN_PREFIX = "nkcc"
# ID 段限长：`\d+` 在 Python 里是任意精度整数，一把 `nkcc-999…999-xxxx` 的伪造令牌会被原样
# 送进主键查询——SQLite 只是查不到，PostgreSQL 则因为整数越界抛 DataError，把一次本该 401
# 的伪造请求变成 500 加一条堆栈。工作区 ID 是 32 位整数，9 位足够且必然落在范围内。
_RELAY_TOKEN_RE = re.compile(rf"^{RELAY_TOKEN_PREFIX}-(\d{{1,9}})-([A-Za-z0-9_-]{{16,}})$")
METADATA_TOKEN_KEY = "cc_relay_token"
METADATA_BASE_URL_KEY = "cc_relay_base_url"

# 容器内 `claude` CLI 会拼在 base_url 后面的路径，中继端点只放行这些
ALLOWED_UPSTREAM_PATHS: Tuple[str, ...] = (
    "v1/messages",
    "v1/messages/count_tokens",
)

_HOST_GATEWAY_ALIAS = "host.docker.internal"

DEFAULT_TIMEOUT_MS = 300000


def credential_mode() -> str:
    """当前的凭据下发模式：`relay`（默认）或 `direct`"""
    mode = OsEnvTypes.Str("CC_CREDENTIAL_MODE", default="relay").strip().lower()
    return mode if mode in ("relay", "direct") else "relay"


def relay_listen_port() -> int:
    """NA 进程实际 bind 的端口

    **不等于** `OsEnv.EXPOSE_PORT`：标准 compose 是 `ports: "${NEKRO_EXPOSE_PORT}:8021"`，
    容器内固定监听 8021，`EXPOSE_PORT` 是发布到宿主的那个。走网桥内部直连 NA 容器时要用前者，
    所以问 nonebot 驱动自己绑的端口（来自 `.env.prod` 的 `PORT`），拿不到再退回 `EXPOSE_PORT`。
    """
    try:
        import nonebot

        port = int(nonebot.get_driver().config.port)
    except Exception:
        port = 0
    return port if port > 0 else OsEnv.EXPOSE_PORT


def relay_host_port() -> int:
    """容器经**宿主地址**回连 NA 时该用的端口

    NA 在容器里时，容器要连的是 compose 发布到宿主的那个端口（`EXPOSE_PORT`）；
    NA 跑在宿主上时，宿主上监听的就是进程自己 bind 的那个。
    """
    return OsEnv.EXPOSE_PORT if OsEnv.RUN_IN_DOCKER else relay_listen_port()


def relay_base_url_override() -> str:
    """`NEKRO_CC_RELAY_BASE_URL` 的显式声明，未设置时为空串"""
    return OsEnvTypes.Str("CC_RELAY_BASE_URL", default="").strip().rstrip("/")


def resolve_relay_base_url(controller_address: str = "", host_address: str = "") -> str:
    """推导「容器视角下」的中继地址

    优先级：

    1. `NEKRO_CC_RELAY_BASE_URL` 显式声明——奇怪拓扑（反向代理、独立网段）的唯一出路；
    2. NA 在工作区专用网络上的 IPv4 地址（由 `prepare_workspace_network` 给出），
       端口取 NA **进程 bind 的端口**。用地址而不是容器名：不依赖 Docker 内嵌 DNS，
       也不用去猜 `${INSTANCE_NAME}nekro_agent` 这个命名约定
       （`services/console/container_manager.py` 里那份猜法在 `INSTANCE_NAME` 非空时其实是错的）。
       **这条路径完全在网桥内部，不经过宿主**，所以工作区守卫「发往宿主一律拒绝」的形态不用动；
    3. 该网络的网关地址——NA 跑在宿主上时它就是宿主，这时守卫必须额外放行这一个端口
       （见 `network_guard.WorkspaceNetworkSpec.host_relay_addresses`）；
    4. 兜底 `host.docker.internal`（容器 ExtraHosts 里有这个别名），
       给「连网关地址都读不出来」的场景留一条路。

    Args:
        controller_address: NA 在该工作区网络上的 IPv4 地址，未接入时为空串
        host_address: 该网络的网关地址（= 宿主在这张网桥上的地址），读不到时为空串

    Returns:
        形如 `http://x.x.x.x:8021/api/cc-relay` 的基地址
    """
    override = relay_base_url_override()
    if override:
        return override
    if controller_address.strip():
        return f"http://{controller_address.strip()}:{relay_listen_port()}{RELAY_PUBLIC_PREFIX}"
    host = host_address.strip() or _HOST_GATEWAY_ALIAS
    return f"http://{host}:{relay_host_port()}{RELAY_PUBLIC_PREFIX}"


def build_relay_token(workspace_id: int) -> str:
    """签发一把新的中继令牌"""
    return f"{RELAY_TOKEN_PREFIX}-{int(workspace_id)}-{secrets.token_urlsafe(32)}"


def parse_relay_token(token: str) -> Optional[Tuple[int, str]]:
    """把令牌拆成 (工作区 ID, 秘密段)，格式不符返回 None"""
    matched = _RELAY_TOKEN_RE.match(token.strip())
    if not matched:
        return None
    return int(matched.group(1)), matched.group(2)


@dataclass(frozen=True)
class WorkspaceCredential:
    """一个工作区当前该写进容器的凭据

    Attributes:
        base_url: 写进 `settings.json` 的 `providers.anthropic.base_url`
        auth_token: 写进 `settings.json` 的 `providers.anthropic.auth_token`
        relayed: True 表示 `auth_token` 是中继令牌而非上游真实 Key
    """

    base_url: str
    auth_token: str
    relayed: bool


async def resolve_workspace_credential(
    workspace: DBWorkspace,
    cc_preset: Optional["CCModelPresetItem"],
    *,
    rotate: bool = False,
    controller_address: str = "",
    host_address: str = "",
) -> WorkspaceCredential:
    """算出该写进这个工作区的凭据，必要时落库

    `direct` 模式下逐字返回改造前的取值（预设的 `base_url` + 真实 `auth_token`），
    这样关掉开关的部署行为与改造前完全一致。

    「还没有中继地址、这次也推不出来」的热更新同样返回改造前的取值：那种工作区的容器是
    改造前建的、此刻还在跑，把一个它连不到的地址写进去只会让它当场失联（判据见下方注释）。

    Args:
        workspace: 工作区记录
        cc_preset: 当前生效的 CC 模型预设，缺失时为 None（沿用改造前的空串行为）
        rotate: 是否重新签发令牌。**只在容器创建/重启时为 True**——热更新
            `settings.json` 时轮换会让容器里正在跑的那一轮 LLM 调用当场 401
        controller_address: NA 在该工作区网络上的地址。只有问过 Docker 的调用方给得出——
            创建容器时按新推导的网络给，重启时按容器**实际所在**的网络反查后给；
            给了就按它重算中继地址。热更新不给，沿用 metadata 里存着的那个
        host_address: 该网络的网关地址，与 `controller_address` 同一个来源与同一条时机规则

    Returns:
        该写进 `settings.json` 的凭据
    """
    direct = WorkspaceCredential(
        base_url=cc_preset.base_url if cc_preset else "",
        auth_token=cc_preset.auth_token if cc_preset else "",
        relayed=False,
    )
    if credential_mode() != "relay":
        return direct

    metadata = dict(workspace.metadata or {})
    token = metadata.get(METADATA_TOKEN_KEY)
    base_url = metadata.get(METADATA_BASE_URL_KEY)
    # 判据是**地址**而不是令牌：令牌随时可以现签一把，热更新推不出来的是「容器该往哪连」。
    # （停容器时令牌会被吊销、地址留着，那种工作区仍然属于「已经切到中继」。）
    address_known = isinstance(base_url, str) and bool(base_url)
    # 「知道容器视角下的中继落点」：要么部署方显式声明了，要么调用方刚推导过网络
    informed = bool(relay_base_url_override() or controller_address.strip() or host_address.strip())

    if not rotate and not address_known and not informed:
        # 走到这里只有一种情形：改造前建的容器还在跑，磁盘上那份 settings.json 里是真实 Key。
        # 热更新不重启容器，而这条路径推不出中继地址，只能写 `host.docker.internal` ——
        # 隔离生效的部署里发往宿主的包被守卫一律挡掉，等于把一个正在跑的容器就地写死，
        # 而且这个地址会落进 metadata、连重启都修不回来。
        # 保持它改造前的形态，等下一次创建/重启（那时才拿得到真实地址）再切到中继。
        logger.debug(f"工作区 {workspace.id} 尚无中继落点，热更新沿用改造前的直连凭据")
        return direct

    changed = False
    if rotate or not isinstance(token, str) or parse_relay_token(token) is None:
        token = build_relay_token(workspace.id)
        metadata[METADATA_TOKEN_KEY] = token
        changed = True
    if controller_address or host_address or not isinstance(base_url, str) or not base_url:
        resolved = resolve_relay_base_url(controller_address, host_address)
        if resolved != base_url:
            base_url = resolved
            metadata[METADATA_BASE_URL_KEY] = base_url
            changed = True

    if changed:
        workspace.metadata = metadata
        await workspace.save(update_fields=["metadata", "update_time"])

    return WorkspaceCredential(base_url=str(base_url), auth_token=str(token), relayed=True)


def build_settings_payload(credential: WorkspaceCredential, model: str, timeout_ms: int) -> Dict[str, Any]:
    """生成 cc-sandbox 读取的 `settings.json` 内容

    形状与改造前逐字相同，只是 `base_url`/`auth_token` 换成了中继的那一对。
    """
    return {
        "provider": "anthropic",
        "providers": {
            "anthropic": {
                "name": "Anthropic",
                "base_url": credential.base_url,
                "auth_token": credential.auth_token,
                "model": model,
            },
        },
        "active_provider": "anthropic",
        "timeout_ms": timeout_ms,
    }


def build_claude_settings_payload(
    cc_preset: "CCModelPresetItem",
    credential: WorkspaceCredential,
) -> Dict[str, Any]:
    """生成 `.claude/settings.json` 内容，并把其中的凭据换成中继的那一对

    这份文件当前其实是死的：`claude` CLI 的 cwd 是 `/workspace/default`，
    项目级设置只会从那里的 `.claude/` 读，而这份写在 `/workspace/.claude/`。
    但它照样是一个躺在容器可读目录里的凭据副本，所以一并换掉。

    **未覆盖**：预设的 `extra_env` 原样透传。运维若把别的凭据塞进 `extra_env`，
    那一份仍然会进容器——它不在本模块的判据范围内。
    """
    payload = dict(cc_preset.to_config_json())
    env = dict(payload.get("env") or {})
    env["ANTHROPIC_BASE_URL"] = credential.base_url
    env["ANTHROPIC_AUTH_TOKEN"] = credential.auth_token
    payload["env"] = env
    return payload


@dataclass(frozen=True)
class RelayGrant:
    """一次中继调用的授权结果

    Attributes:
        workspace_id: 令牌对应的工作区
        upstream_base_url: 真实上游基地址
        upstream_token: 真实上游凭据
        timeout_seconds: 转发超时
    """

    workspace_id: int
    upstream_base_url: str
    upstream_token: str
    timeout_seconds: float


async def resolve_relay_grant(token: str) -> Optional[RelayGrant]:
    """校验中继令牌并解析出该用哪把真实凭据转发

    令牌里编着工作区 ID，所以是一次主键查询而不是全表扫描；秘密段用常量时间比较。
    任何一步不成立都返回 None，由调用方统一回 401——不区分「工作区不存在」与
    「令牌不对」，避免把工作区 ID 变成可枚举的。
    """
    from nekro_agent.services.workspace.manager import WorkspaceService

    if credential_mode() != "relay":
        # 切回 `direct` 之后中继不该再授权任何东西。少了这一条，切换之前签发的令牌会在一个
        # 公网可达的端点上永久有效——开关名义上关了，门还开着，而且此时守卫已经不再放行
        # 容器那条路，唯一还能用它的只剩「令牌泄漏出去的人」。
        return None

    parsed = parse_relay_token(token)
    if parsed is None:
        return None
    workspace_id, _ = parsed

    workspace = await DBWorkspace.get_or_none(id=workspace_id)
    if workspace is None:
        return None
    stored = (workspace.metadata or {}).get(METADATA_TOKEN_KEY)
    if not isinstance(stored, str) or not secrets.compare_digest(stored, token):
        return None

    from nekro_agent.core.cc_model_presets import cc_presets_store

    preset_id = WorkspaceService.get_effective_cc_preset_id(workspace)
    cc_preset = cc_presets_store.get_by_id(preset_id) if preset_id is not None else None
    if cc_preset is None:
        # 与 `SandboxContainerManager._resolve_preset` 用同一套回落。两边判据不一致的表现是：
        # 绑定的预设被换掉/删掉之后 settings.json 照样按默认预设写好，容器却在每一次 LLM 调用上
        # 吃 401——一个从日志和配置里都看不出原因的故障。
        cc_preset = cc_presets_store.get_default()
    if cc_preset is None:
        logger.warning(f"工作区 {workspace_id} 的中继请求找不到生效的 CC 模型预设")
        return None

    try:
        timeout_ms = int(cc_preset.api_timeout_ms) if cc_preset.api_timeout_ms else DEFAULT_TIMEOUT_MS
    except (TypeError, ValueError):
        timeout_ms = DEFAULT_TIMEOUT_MS

    return RelayGrant(
        workspace_id=workspace_id,
        # `strip()` 与 `auth_token` 那一侧是同一条判据：两个字段来自后台同一张表单，
        # 粘贴带上的首尾空白同样常见，而 URL 对空白的反应比首部更难看——实测三种形态：
        #   `...claude\n` / `\t...` -> `httpx.InvalidURL` 从 `client.build_request()` 抛出，
        #       那一行在 `relay_to_upstream` 的 `try` **之外**，于是中继端点直接 500；
        #   `  ...claude`           -> 502 `unknown url type`，指着上游喊冤；
        #   `...claude `            -> 不报错，路径被编码成 `/claude%20/v1/messages`，
        #       每一次 LLM 调用静默 404，而控制台连通性测试照样显示通过。
        # 最后这条最毒：`anthropic._normalize_messages_endpoint` 一直是 `.strip().rstrip("/")`，
        # 所以「测试通过、真跑就废」是两处实现不一致直接造出来的。这里对齐它。
        upstream_base_url=cc_preset.base_url.strip().rstrip("/"),
        upstream_token=cc_preset.auth_token,
        timeout_seconds=max(timeout_ms, 1000) / 1000,
    )
