"""OneBot V11 协议端接入令牌：按**实例**校验

## 改造前是什么样（这是一个「既生成又不校验」的死局）

- `services/console/instance_manager.py:create_instance` 每建一个实例都
  `secrets.token_urlsafe(24)` 生成一枚令牌写进实例元数据；
- `services/console/container_manager.py:resolve_access_token` 把它下发进 NapCat 的
  `onebot11.json`（托管的那条反向 WS 连接的 `token` 字段）；
- 而 **NA 这一侧的校验只有部署级的一个值**：NoneBot 的
  `Adapter._check_access_token` 拿 `onebot_access_token` 这**一个**配置项去比对所有连接。

于是二选一：部署级令牌**留空** → NoneBot 直接跳过校验，任何人知道 ws 地址就能冒充协议端
连进来（上线清单 §1.5 的 🔴 拦截项）；部署级令牌**填上** → 总后台建的每个实例带的都是
自己那枚令牌，与部署级值不等，**全部握手 403**。也就是说「按清单填上」的那一刻，
总后台建出来的所有实例集体断连。

## 握手时能不能知道是哪个实例：能

OneBot V11 的反向 WS 鉴权发生在 **HTTP 握手阶段**，`Adapter._handle_ws` 的第一行就是
`websocket.request.headers.get("x-self-id")` —— 协议端在握手请求头里带着自己的账号号码，
**先于**令牌校验就能读到（`nonebot/adapters/onebot/v11/adapter.py`：先查 `X-Self-ID`、
再查重复连接、最后才 `_check_access_token`）。

拿到的是**账号号码**不是实例标识，中间差一次查库：
`DBAdapterInstance.get_by_bound_account(ADAPTER_KEY, self_id)`
——沿用入站路由那条既有判据（排除软删墓碑、重复绑定取最早一行），不另写一套。

握手期查库是可以接受的：连接是低频事件（NapCat 默认 3s 重连一次），
而入站消息走的是 `core/instance.py` 里的内存映射，不受影响。

## 为什么是 ASGI 中间件，而不是替换/继承 NoneBot 的适配器

`Adapter.__init__` 里的 `_setup()` 就把 `self._handle_ws` **绑定**进了
`WebSocketServerSetup`，之后再改类属性或实例属性都不会改变已注册的那个路由端点；
而 `register_adapter` 发生在 `run_bot.py` 里、**早于** `nonebot.load_from_toml`，
也就是早于本包被导入。想换成子类就得在 `run_bot.py` 里导入它，
而 `import nekro_agent.<任何子模块>` 会提前执行 `nekro_agent/__init__.py`
（后果不是报错而是插件静默不加载）。

ASGI 中间件挂在 `routers/__init__.py:mount_middlewares()`，那里在应用启动**之前**执行、
拿得到 `app`，且天然能看见 `scope["type"] == "websocket"` 的握手。

## 放行时为什么要改写 Authorization 头（"背书"）

中间件按实例判完之后，请求还会继续流到 NoneBot 自己的 `_check_access_token`，
那一关仍然拿部署级令牌比对。实例令牌与部署级令牌不等时它会把我们刚放行的连接再拒一次——
正是上面那个死局。所以放行时把 Authorization 头改写成部署级令牌，向下游"背书"。

这个方向是**故意选的**：中间件万一没挂上（导入失败、有人删了那行），
连接会落回 NoneBot 那道严格的部署级校验，**失败方向是拒绝而不是放行**。
反过来做（把 NoneBot 的 `onebot_config.onebot_access_token` 清成 None）同样能跑通，
但中间件一旦缺席整个部署就是敞开的。

## 兼容矩阵（每一格都有用例）

| 部署形态 | 期望令牌 | 结论 |
|---|---|---|
| 老部署，只配了部署级 `ONEBOT_ACCESS_TOKEN`，实例没有自己的令牌 | 部署级值 | 与改造前**逐字相同** |
| 老部署，部署级令牌为空 | 空 | 不校验，与改造前**逐字相同**（含"陌生协议端首次连入→自动登记待认领"这条链路） |
| 总后台建的实例，元数据里有自己的令牌 | 该实例的令牌 | 按实例校验；部署级令牌**不**被接受 |
| 账号还没绑定到任何实例的协议端 | 部署级值，**外加**「等着绑账号的实例」各自那枚 | 见下一节 |
| 查库失败 / 元数据读不出来 | 部署级值 + 一条 WARNING | 退回改造前那条规则，不比改造前更松 |

「实例有自己的令牌时部署级令牌**不**被接受」是有意的严格：定制版客户手里那枚是他自己的，
平台方那枚不该成为一把万能钥匙；两把都收等于实例隔离形同虚设。

## 「有令牌、还没有账号」这一格：首次接入

期望令牌是按**账号**解析的，而实例是先建好、后绑账号的：

- 内置 NapCat：`create_instance` 当场生成令牌并写进 `onebot11.json`，而
  `provider_account_id` 要等扫码登录成功才回填（`可留空待登录后回填`）；
- 远程协议端：管理员可以先建实例、把令牌抄给客户，QQ 号后填。

于是首次接入那一刻，协议端出示的是**实例自己那枚**、而账号查不出任何实例——只按部署级令牌
判就是 403。部署级令牌留空时这一格恰好被"不配令牌=不校验"盖住，所以它只在**按上线清单
填上部署级令牌之后**才显形：总后台新建的每个实例，从建好到绑上账号之间连不上。
那正是本模块要消灭的那类「填上就断连」，只是换了个入口。

因此账号解析不出实例时，除了部署级令牌，还接受
`container_manager.list_instances_awaiting_account()` 里那批实例各自的令牌
（判据与内置 NapCat 登录归属推导同源，见那个函数）。放宽的边界是清楚的：

- 只在**账号还没绑给任何实例**时才多看一眼——已绑账号的实例只认自己那枚，一个字没松；
- 只认「已经建好、还等着绑账号、且可绑定」的实例自己那枚令牌，空令牌不入列
  （否则 `check_token("", ...)` 会把这一格变成"谁都放行"）；
- 拿着它能做的事，与拿着部署级令牌能做的事完全相同：连上来，然后被登记成一个待认领实例
  或被认到发起登录的那个内置实例名下。而在**部署级令牌留空**的今天，这件事根本不需要任何令牌。
  也就是说这一格比现状严，比"只认部署级"松，而后者根本走不通。

## HTTP 上报路径（对上游的一处加严）

NoneBot 的 `_handle_http` **完全不校验 access token**（只在配了 `onebot_secret` 时验签名），
于是 `POST /onebot/v11/` 带一个 `X-Self-ID` 就能凭空注入事件——伪造任意用户的消息、
让 AI 据此执行动作。只堵 WS 不堵它等于没堵。因此这里对两条路径用**同一条规则**。

⚠️ 这是本模块唯一一处「单实例老部署行为不是逐字不变」的地方：
配了部署级令牌、且协议端走 HTTP 上报又不带 Authorization 头的部署，从此会收到 403。
要退回上游行为，把 `_should_guard()` 里的 `http` 那一支去掉即可（有用例钉着这个取舍）。
"""

import secrets
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, List, MutableMapping, Optional, Set, Tuple

from nonebot.adapters.onebot.utils import get_auth_bearer

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import resolve_deployment_onebot_access_token
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.services.user.token_exposure import token_fingerprint

from .channel import ADAPTER_KEY

logger = get_sub_logger("adapter.onebot_v11")

# NoneBot 的 OneBot V11 适配器把 HTTP 与 WS 端点都注册在这个前缀下
# （`/onebot/v11/`、`/onebot/v11/http[/]`、`/onebot/v11/ws[/]`）。
# 用前缀而不是逐个枚举路径：多一个尾斜杠、上游多注册一条别名，都不该变成一个绕过口。
ONEBOT_PATH_PREFIX = "/onebot/v11"

SELF_ID_HEADER = "x-self-id"
AUTHORIZATION_HEADER = "authorization"

# 与 `nonebot/adapters/onebot/v11/adapter.py:_check_access_token` 逐字一致的拒绝文案。
# 分成两句是上游的行为（带了但不对 / 根本没带），照抄是为了让排障经验可以平移。
MSG_TOKEN_INVALID = "Authorization Header is invalid"
MSG_TOKEN_MISSING = "Missing Authorization Header"

# 关闭 WS 握手用的状态码，同样取自上游 `_handle_ws`
WS_CLOSE_CODE_POLICY_VIOLATION = 1008


class TokenSource(str, Enum):
    """期望令牌是从哪儿解析出来的

    握手放不放行不看它（只进日志）；放行之后它跟着握手上下文走（`HandshakeRecord`），
    超级用户的来源判定只认 `INSTANCE` 与 `DEPLOYMENT` 两档（`services/user/super_users.py`）。
    """

    INSTANCE = "instance"
    """该账号所属实例元数据里的令牌"""

    DEPLOYMENT = "deployment"
    """部署级 `ONEBOT_ACCESS_TOKEN`"""

    DEPLOYMENT_FALLBACK = "deployment_fallback"
    """本该按实例解析，但查库/读元数据失败，退回部署级值"""

    AWAITING_ACCOUNT = "awaiting_account"
    """账号还没绑给任何实例，出示的是某个「等着绑账号的实例」自己那枚令牌"""


@dataclass(frozen=True)
class ExpectedToken:
    """本次握手应当匹配的令牌"""

    token: str
    """空串表示「本部署对这个协议端不做令牌校验」"""

    source: TokenSource
    instance_key: str = ""

    awaiting: Tuple[Tuple[str, str], ...] = ()
    """`(实例标识, 该实例自己那枚令牌)`，额外可接受的备选

    只在「账号还没绑给任何实例」这一格非空，见模块文档「有令牌、还没有账号」那一节。
    里面**只放非空令牌**：空串喂给 `check_token` 会变成"谁都放行"。
    """


@dataclass(frozen=True)
class HandshakeRecord:
    """一次放行的握手凭的是什么

    中间件放行时把它放进 `_current_handshake` 再交给下游。NoneBot 在这条请求的任务里用
    `asyncio.create_task(bot.handle_event(event))` 派发事件（反向 WS 与 HTTP 上报都是），
    新任务复制的是当时的上下文，于是这条连接上派发的每一个事件都能回查到它是怎么握手的；
    两条连接、两次 HTTP 上报各在各的上下文里，互相读不到。

    事件归属只看握手请求头里的账号（`bot.self_id`），而握手时这个账号可能还没绑定、
    靠的是备选令牌，或者事后被并进了别的实例——光看事件归属的实例判不出这条连接背后是谁。
    """

    self_id: str
    source: TokenSource
    instance_key: str
    """握手时账号解析到的实例；账号还没绑定时为空"""

    token_fingerprint: str
    """放行时匹配上的那枚期望令牌的指纹；期望令牌为空（不校验）时为空串"""


_current_handshake: ContextVar[Optional[HandshakeRecord]] = ContextVar("onebot_handshake", default=None)


def current_handshake() -> Optional[HandshakeRecord]:
    """当前事件所在连接的握手记录；没经过本中间件的路径（含 NA 主动连出去的正向 WS）为 None"""
    return _current_handshake.get()


# 账号还没绑定实例时握手出示过的令牌指纹。账号之后被并进 / 改绑到租户实例上时，这些令牌就在那个租户手里了
# （`token_exposure.record_account_landing_on_tenant`）。已绑定账号出示的就是所属实例的令牌，按库里的行就能算出来，不必记。
# 只在内存里、重启清空；条目有上限，按最近使用淘汰：备选令牌对任意未绑定账号都有效，不设上限就能被刷账号号码撑大
_PRESENTED_ACCOUNT_LIMIT = 65536
_presented_by_unbound_account: "OrderedDict[str, Set[str]]" = OrderedDict()


def _remember_unbound_presentation(record: HandshakeRecord) -> None:
    # 备选令牌那一格的 `instance_key` 是匹配上的那个待绑实例，账号本身同样还没绑定
    unbound = not record.instance_key or record.source is TokenSource.AWAITING_ACCOUNT
    if not unbound or not record.self_id or not record.token_fingerprint:
        return
    fingerprints = _presented_by_unbound_account.pop(record.self_id, set())
    fingerprints.add(record.token_fingerprint)
    _presented_by_unbound_account[record.self_id] = fingerprints
    while len(_presented_by_unbound_account) > _PRESENTED_ACCOUNT_LIMIT:
        _presented_by_unbound_account.popitem(last=False)


def fingerprints_presented_by(account: str) -> FrozenSet[str]:
    """这个账号在还没绑定实例时、本进程里握手出示过的令牌指纹"""
    return frozenset(_presented_by_unbound_account.get(account, ()))


@dataclass(frozen=True)
class TokenVerdict:
    """一次令牌比对的结论"""

    allowed: bool
    message: str = ""


def check_token(expected: str, presented: Optional[str]) -> TokenVerdict:
    """把「期望令牌 + 协议端出示的令牌」判成放行/拒绝

    刻意做成纯函数：这是整条链路上唯一的判据，表驱动用例直接打它。

    - `expected` 为空 → 放行。**这一档必须保留**：它是「部署级令牌留空」的老部署
      与「陌生协议端首次连入」两条既有链路的行为，收紧它等于按清单填令牌之前
      所有部署一起断连。
    - `presented` 为 `None` 表示请求根本没带（或带了个不认识的 scheme），
      解析口径直接复用 NoneBot 的 `get_auth_bearer`（同时认 `Bearer` 与 `Token`）。
    """
    if not expected:
        return TokenVerdict(allowed=True)
    if not presented:
        return TokenVerdict(allowed=False, message=MSG_TOKEN_MISSING)
    if not _constant_time_equals(presented, expected):
        return TokenVerdict(allowed=False, message=MSG_TOKEN_INVALID)
    return TokenVerdict(allowed=True)


def _constant_time_equals(left: str, right: str) -> bool:
    """定长时间比较，避免按字节逐位试探令牌

    `secrets.compare_digest` 对非 ASCII 的 str 会抛 TypeError，先编码成字节。
    """
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


async def resolve_expected_token(self_id: str) -> ExpectedToken:
    """解析出这个协议端账号应当出示的令牌

    账号解析不出实例（没带 `X-Self-ID`、账号还没绑定给任何实例、账号所在的行是墓碑）
    一律落到部署级令牌——那正是改造前的行为，也是「陌生协议端首次连入后被自动登记为
    待认领实例」这条链路能继续走通的前提。

    其中「账号还没绑定给任何实例」这一格**额外**带一张备选表 `awaiting`：实例是先建好、
    后绑账号的，新建实例的第一次接入出示的是它自己那枚令牌，而那时账号还查不出任何实例。
    只按部署级令牌判就是"填上部署级令牌那一刻，新建的实例全部连不上"。
    详见模块文档「有令牌、还没有账号」那一节。

    **任何异常都不向上抛**：这是握手的第一步，一个查库抖动没有资格让所有协议端掉线。
    退回部署级令牌不会比改造前更松（改造前本来就只有这一个值）。
    """
    deployment = resolve_deployment_onebot_access_token()
    if not self_id:
        return ExpectedToken(token=deployment, source=TokenSource.DEPLOYMENT)

    try:
        instance = await DBAdapterInstance.get_by_bound_account(ADAPTER_KEY, self_id)
    except Exception as e:  # noqa: BLE001 - 见 docstring
        logger.warning(f"解析协议端 {self_id} 的归属实例失败，本次按部署级令牌校验: {e}")
        return ExpectedToken(token=deployment, source=TokenSource.DEPLOYMENT_FALLBACK)

    if instance is None:
        # 部署级令牌为空时主判据 `check_token("", ...)` 恒放行，备选表在证明上不可达，
        # 于是不查它——否则每一个待认领协议端的每一次重连都白白多一次查库。
        return ExpectedToken(
            token=deployment,
            source=TokenSource.DEPLOYMENT,
            awaiting=await _awaiting_account_tokens() if deployment else (),
        )

    try:
        # 延迟导入：`container_manager` 会拖进 aiodocker / httpx，而它自己在模块级导入
        # 本包的 `core.instance`，模块级反向导入会成环（与 `core/instance.py` 同一条理由）。
        # 用它而不是自己解一遍元数据：`resolve_access_token` 正是**下发给协议端**的那个值，
        # 校验侧另写一份判据 = 两边迟早分叉，而分叉的表现是协议端连不上。
        from nekro_agent.services.console.container_manager import (
            read_container_metadata,
            resolve_access_token,
        )

        token = resolve_access_token(instance)
        has_own_token = bool(read_container_metadata(instance).access_token)
    except Exception as e:  # noqa: BLE001 - 见 docstring
        logger.warning(f"读取实例 {instance.instance_key} 的接入令牌失败，本次按部署级令牌校验: {e}")
        return ExpectedToken(
            token=deployment,
            source=TokenSource.DEPLOYMENT_FALLBACK,
            instance_key=instance.instance_key,
        )

    return ExpectedToken(
        token=token,
        source=TokenSource.INSTANCE if has_own_token else TokenSource.DEPLOYMENT,
        instance_key=instance.instance_key,
    )


async def _awaiting_account_tokens() -> Tuple[Tuple[str, str], ...]:
    """「已经建好、还等着绑账号」的实例各自那枚令牌

    判据不在这里另写：直接用 `container_manager.list_instances_awaiting_account`——
    内置 NapCat 的登录归属推导用的是同一条，两边分叉就会出现
    「握手放行了、归属推导却不认这个候选」这类只在首次接入那一刻显形的窗口。

    取的是 `read_container_metadata(...).access_token`（**实例自己那枚**）而不是
    `resolve_access_token`：后者在实例没有自己的令牌时会退回部署级值，
    而部署级值已经是主判据，再进备选表只是把同一个值判两遍；更要紧的是实例两头都没令牌时
    它返回空串，空串进了备选表就等于「这一格谁都放行」。

    任何异常都咽掉：这是握手路径，退化成"只认部署级令牌"即可，那正是本节修复之前的行为。
    """
    try:
        from nekro_agent.services.console.container_manager import (
            list_instances_awaiting_account,
            read_container_metadata,
        )

        rows = await list_instances_awaiting_account(ADAPTER_KEY)
    except Exception as e:  # noqa: BLE001 - 见 docstring
        logger.warning(f"读取「等待绑定账号」的实例清单失败，本次只按部署级令牌校验: {e}")
        return ()

    pairs: List[Tuple[str, str]] = []
    for row in rows:
        try:
            token = read_container_metadata(row).access_token.strip()
        except Exception as e:  # noqa: BLE001 - 一行元数据坏掉不该拖垮整张表
            logger.warning(f"读取实例 {row.instance_key} 的接入令牌失败，本次不把它计入备选: {e}")
            continue
        if token:
            pairs.append((row.instance_key, token))
    return tuple(pairs)


async def authorize_handshake(self_id: str, presented: Optional[str]) -> Tuple[TokenVerdict, ExpectedToken]:
    """握手鉴权的完整链路：解析期望令牌 → 比对

    主判据是 `expected.token`。只有它不通过时才轮到 `expected.awaiting`——那张表只在
    「账号还没绑给任何实例」这一格非空（见模块文档），别的格子里它恒为空元组，
    因此这个循环对已绑账号的协议端**一次都不会执行**。
    """
    expected = await resolve_expected_token(self_id)
    verdict = check_token(expected.token, presented)
    if verdict.allowed:
        return verdict, expected

    matched = [(instance_key, token) for instance_key, token in expected.awaiting if check_token(token, presented).allowed]
    if matched:
        # 放不放行只看有没有匹配上；记哪个实例要求**只匹配上一个**：同一枚令牌对应多个待绑实例时，
        # 判不出这个协议端是谁建的，实例留空，超级用户的来源判定因此对不上任何实例
        return TokenVerdict(allowed=True), ExpectedToken(
            token=matched[0][1],
            source=TokenSource.AWAITING_ACCOUNT,
            instance_key=matched[0][0] if len(matched) == 1 else "",
        )
    return verdict, expected


# ---------------------------------------------------------------------------
# ASGI 中间件
# ---------------------------------------------------------------------------


def _guarded_paths(scope: MutableMapping[str, Any]) -> List[str]:
    """本次请求要拿去比对前缀的路径形态

    多给一个「剥掉 `root_path` 之后」的形态：把 NA 挂在子路径下时
    （`root_path=/nekro`），`scope["path"]` 是 `/nekro/onebot/v11/ws`，而路由匹配用的是
    剥掉之后的那一段。只比对原始 path 的话这道门会**整个失效且悄无声息**——
    多比一个形态的代价只是偶尔多判一次，漏比一个形态的代价是敞开。
    """
    path = str(scope.get("path") or "")
    paths = [path]
    root_path = str(scope.get("root_path") or "")
    if root_path and path.startswith(root_path):
        paths.append(path[len(root_path) :])
    return paths


def _should_guard(scope: MutableMapping[str, Any]) -> bool:
    """判断这个 ASGI scope 是否是需要鉴权的 OneBot 协议端接入

    WS 是协议端的正常接入方式；HTTP POST 是 NoneBot 同样开着、却完全不校验令牌的那条
    上报路径（见模块文档最后一节）。其余（NA 自己的 API、静态资源、工作区终端 WS）一概不碰。
    """
    if not any(candidate.startswith(ONEBOT_PATH_PREFIX) for candidate in _guarded_paths(scope)):
        return False
    scope_type = scope.get("type")
    if scope_type == "websocket":
        return True
    return scope_type == "http" and str(scope.get("method") or "").upper() == "POST"


def _read_headers(scope: MutableMapping[str, Any]) -> Dict[str, str]:
    """把 ASGI 的字节头列表读成小写键的字典

    ASGI 规定头名已经是小写字节串，这里仍显式 `lower()`：判据不该依赖上游服务器的规范程度。

    **同名头取第一条**（`setdefault`），这一点必须与下游一致：下游是
    `CIMultiDict(starlette.Headers.items()).get(...)`，同样取第一条。
    两边取的不是同一条就有走私空间——第一个 `X-Self-ID` 拿去过令牌校验、
    第二个被下游拿去绑定 Bot，等于用 A 的令牌冒充 B。有用例逐字钉住这条一致性。
    """
    result: Dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers") or []:
        try:
            name = bytes(raw_name).decode("latin-1").lower()
            value = bytes(raw_value).decode("latin-1")
        except (UnicodeDecodeError, ValueError):
            continue
        result.setdefault(name, value)
    return result


def _endorse(scope: MutableMapping[str, Any], deployment_token: str) -> None:
    """放行后把 Authorization 头改写成部署级令牌，向下游 NoneBot 背书

    见模块文档「放行时为什么要改写 Authorization 头」。部署级令牌为空时下游本来就不校验，
    什么都不做——少改一个头，就少一个"改写逻辑本身出错"的可能。

    **部署级令牌必须能编成 latin-1**：HTTP 头就是 latin-1 字节
    （ASGI 规定 `scope["headers"]` 是字节对，starlette 也按 latin-1 解）。
    配了一个含非 latin-1 字符的部署级令牌时，`str.encode("latin-1")` 会抛 —— 而这里
    是**放行分支**，抛出去等于每一次本该成功的握手都变成中间件里一个看不懂的编码错误。
    那种令牌本来就不可能通过任何一侧的校验（协议端根本发不出这样的头，
    下游 NoneBot 的比对也必然不等），所以这里记一条能直接定位到配置项的 WARNING、
    **不背书**，把连接交给下游那道严格校验去拒——失败方向仍然是拒绝。
    """
    if not deployment_token:
        return
    try:
        authorization = f"Bearer {deployment_token}".encode("latin-1")
    except UnicodeEncodeError:
        logger.warning(
            "部署级 ONEBOT_ACCESS_TOKEN 含非 latin-1 字符，无法作为 HTTP 头下发；"
            "该令牌对任何协议端都不可用，请改成 ASCII 随机串（本次连接交由下游校验，结果必然是拒绝）",
        )
        return
    headers: List[Tuple[bytes, bytes]] = [
        (name, value)
        for name, value in (scope.get("headers") or [])
        if bytes(name).decode("latin-1").lower() != AUTHORIZATION_HEADER
    ]
    headers.append((AUTHORIZATION_HEADER.encode("latin-1"), authorization))
    scope["headers"] = headers


class OneBotAccessTokenMiddleware:
    """在 NoneBot 的 OneBot V11 端点之前按**实例**校验协议端令牌

    纯 ASGI 中间件（不是 `BaseHTTPMiddleware`）：`BaseHTTPMiddleware` 只看得见
    `http` 类型的 scope，而协议端的鉴权发生在 WebSocket 握手上。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if not _should_guard(scope):
            await self.app(scope, receive, send)
            return

        headers = _read_headers(scope)
        self_id = headers.get(SELF_ID_HEADER, "").strip()
        presented = get_auth_bearer(headers.get(AUTHORIZATION_HEADER))

        verdict, expected = await authorize_handshake(self_id, presented)
        if not verdict.allowed:
            # 只记账号与来源，绝不记令牌本身（明文或掩码都不记）——这条日志会进 app.log
            logger.warning(
                f"拒绝 OneBot 协议端接入: self_id={self_id or '(未上报)'}, "
                f"instance={expected.instance_key or '(未绑定)'}, "
                f"期望令牌来源={expected.source.value}, 原因={verdict.message}",
            )
            await _reject(scope, receive, send, verdict.message)
            return

        _endorse(scope, resolve_deployment_onebot_access_token())
        record = HandshakeRecord(
            self_id=self_id,
            source=expected.source,
            instance_key=expected.instance_key,
            token_fingerprint=token_fingerprint(expected.token),
        )
        _remember_unbound_presentation(record)
        # 用完复位：同一个任务里后面的请求（HTTP keep-alive）不该继承这一次的握手
        reset = _current_handshake.set(record)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_handshake.reset(reset)


async def _reject(scope: MutableMapping[str, Any], receive: Any, send: Any, message: str) -> None:
    """拒绝一次接入

    - WebSocket：先收掉 `websocket.connect` 再发 `websocket.close(1008)`，
      与上游 `_handle_ws` 的拒绝形态一致（握手阶段的 close 由 ASGI 服务器翻成 HTTP 403）；
    - HTTP：直接回 403 与同一句文案。
    """
    if scope.get("type") == "websocket":
        event = await receive()
        if event.get("type") != "websocket.connect":
            return
        await send({"type": "websocket.close", "code": WS_CLOSE_CODE_POLICY_VIOLATION, "reason": message})
        return

    body = message.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        },
    )
    await send({"type": "http.response.body", "body": body})
