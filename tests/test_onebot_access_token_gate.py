"""OneBot V11 协议端接入令牌：按实例校验

盘点发现的死局：总后台每建一个实例都生成一枚 access token 并下发进
NapCat 配置，而 NA 这一侧的校验只有部署级的一个值。于是「留空 = 谁都能冒充协议端连进来」，
「填上 = 总后台建的实例全部握手 403」。

这份用例钉住修复之后的五组契约：

1. **上游前提**：OneBot V11 的鉴权发生在 WS 握手阶段，且 `X-Self-ID` 在令牌校验**之前**
   就能读到 —— 整个「按实例校验」的可行性建立在这一条上，上游改了必须当场红；
2. **判据本身**：`check_token` 的四档（不校验 / 缺令牌 / 令牌错 / 令牌对）；
3. **期望令牌的解析**：有自己令牌的实例按自己的算，其余一律退回部署级值
   （老部署、陌生协议端、查库失败三条都在这一档）；
4. **中间件行为**：WS 与 HTTP 上报两条路径的放行/拒绝形态，放行时向下游背书，
   以及"不该管的 scope 一律不碰"；
5. **下发即校验**：握手期望的令牌必须与 `container_manager` 下发给协议端的是**同一个值**，
   这是本次修复的核心不变量，两侧任何一侧被改动都要红。

另有一组盯着部署级令牌的解析口径：裸机部署上 `os.environ` 里根本没有 `ONEBOT_ACCESS_TOKEN`
（`run_bot.py` 只导 `NEKRO_` 前缀的键），只读环境变量会让 NA 把**空令牌**下发进协议端配置，
而 NoneBot 那边照着 env 文件在校验 —— 同样是"填上就断连"，只是成因在另一头。
"""

from __future__ import annotations

import datetime
import inspect
import json
import re
from pathlib import Path
from typing import Any, Dict, List, MutableMapping, Optional, Tuple

import pytest
from nonebot.adapters.onebot.utils import get_auth_bearer
from nonebot.adapters.onebot.v11 import Adapter as UpstreamOneBotV11Adapter

# 模块级导入是必须的：本文件开着 `from __future__ import annotations`，
# FastAPI 解析 websocket 端点的类型注解时按**模块全局**查名字，
# 放进函数里 import 会让它认不出 `WebSocket`、把参数当成查询参数处理，
# 结果是端点一被调用就报错、连接被关掉 —— 一个"测试写法"造出来的假红。
from starlette.websockets import WebSocket
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11 import routers as onebot_routers
from nekro_agent.adapters.onebot_v11.core import access_token as gate
from nekro_agent.adapters.onebot_v11.core.access_token import (
    MSG_TOKEN_INVALID,
    MSG_TOKEN_MISSING,
    ONEBOT_PATH_PREFIX,
    WS_CLOSE_CODE_POLICY_VIOLATION,
    OneBotAccessTokenMiddleware,
    TokenSource,
    check_token,
    resolve_expected_token,
)
from nekro_agent.core import os_env
from nekro_agent.core.os_env import resolve_deployment_onebot_access_token
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.services.console import container_manager, instance_manager
from nekro_agent.services.user.perm import Role

REPO_ROOT = Path(__file__).resolve().parent.parent

ADAPTER_KEY = "onebot_v11"

DEPLOYMENT_TOKEN = "deployment-token-aaaaaaaaaaaa"  # noqa: S105 - 测试常量
INSTANCE_TOKEN = "instance-token-bbbbbbbbbbbbbb"  # noqa: S105 - 测试常量

# 有自己那枚令牌的实例（总后台建出来的形态）
SCOPED_INSTANCE = "inst_scoped"
SCOPED_ACCOUNT = "10001"
# 只靠部署级令牌的实例（单实例时代升级上来 / 自动登记的待认领协议端）
LEGACY_INSTANCE = "inst_legacy"
LEGACY_ACCOUNT = "10002"
# 软删墓碑：不是可路由的归属方
TOMBSTONE_INSTANCE = "inst_dead"
TOMBSTONE_ACCOUNT = "10003"
# 库里根本没有的账号
UNKNOWN_ACCOUNT = "19999"
# 总后台刚建好、令牌已经下发、账号还没绑上的实例（首次接入那一刻的形态）
AWAITING_INSTANCE = "inst_awaiting"
AWAITING_TOKEN = "awaiting-token-cccccccccccc"  # noqa: S105 - 测试常量
AWAITING_ACCOUNT = "10777"  # 它扫码登录后会上报的账号，此刻库里还没有这一行绑定

# 上游注册的端点路径，**写死字面量**而不是从 `ONEBOT_PATH_PREFIX` 派生。
# 派生会让「判据」与「被判值」一起变：把常量改窄成 `/onebot/v11/ws` 时，
# 用例里那批路径跟着变窄，于是「少认了两个别名」这个变异全绿存活（实测过）。
# 字面量与上游的一致性由 `test_literals_match_upstream_registration` 单独盯。
UPSTREAM_WS_PATHS = ("/onebot/v11/", "/onebot/v11/ws", "/onebot/v11/ws/")
UPSTREAM_HTTP_PATHS = ("/onebot/v11/", "/onebot/v11/http", "/onebot/v11/http/")


@pytest.fixture
def deployment_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """把部署级令牌固定成一个已知值

    测试进程里没有注册 NoneBot 适配器，`resolve_deployment_onebot_access_token()`
    会退回 `os.environ` 那一支，因此这里改环境变量即可。
    """
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", DEPLOYMENT_TOKEN)
    return DEPLOYMENT_TOKEN


@pytest.fixture
def no_deployment_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """部署级令牌为空 —— `.env.prod` 今天的形态"""
    monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)
    return ""


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER_KEY,
            instance_key=SCOPED_INSTANCE,
            display_name="有自己令牌的实例",
            provider="builtin_napcat",
            provider_account_id=SCOPED_ACCOUNT,
            status="online",
            metadata_json=f'{{"access_token": "{INSTANCE_TOKEN}"}}',
        )
        await DBAdapterInstance.create(
            adapter_key=ADAPTER_KEY,
            instance_key=LEGACY_INSTANCE,
            display_name="没有自己令牌的实例",
            provider="remote_onebot",
            provider_account_id=LEGACY_ACCOUNT,
            status="online",
            metadata_json="{}",
        )
        await DBAdapterInstance.create(
            adapter_key=ADAPTER_KEY,
            instance_key=TOMBSTONE_INSTANCE,
            display_name="墓碑",
            provider="builtin_napcat",
            provider_account_id=TOMBSTONE_ACCOUNT,
            status="deleted",
            metadata_json=f'{{"access_token": "{INSTANCE_TOKEN}"}}',
        )
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
async def awaiting_instance(db) -> DBAdapterInstance:  # noqa: ARG001 - 依赖 db 建好表
    """总后台刚建好、还没绑上账号的实例

    这是**最常见的新建形态**，不是边角：内置 NapCat 的 `provider_account_id` 要等扫码登录
    成功才回填（`InstanceCreateRequest` 上写着"可留空待登录后回填"），远程协议端也允许
    先建实例、后填 QQ 号。而它建好的那一刻令牌就已经生成并下发进协议端配置了。
    """
    return await DBAdapterInstance.create(
        adapter_key=ADAPTER_KEY,
        instance_key=AWAITING_INSTANCE,
        display_name="等着扫码登录",
        provider="builtin_napcat",
        provider_account_id="",
        status="unbound",
        enabled=True,
        metadata_json=f'{{"access_token": "{AWAITING_TOKEN}"}}',
    )


# ---------------------------------------------------------------------------
# 1. 上游前提：握手阶段拿得到 self_id，而上游只有一个全局令牌
# ---------------------------------------------------------------------------


class TestUpstreamHandshakeContract:
    """整个方案建立在这几条上游事实上，上游一改必须当场红而不是静默失效"""

    def test_self_id_is_readable_before_token_check(self) -> None:
        """`X-Self-ID` 在 `_check_access_token` **之前**被读出来

        这是"能不能按实例校验"的唯一前提。若上游把顺序调过来（先校验令牌再读账号），
        我们仍能从同一份请求头里读到它——真正会破坏方案的是上游不再要求这个头。
        """
        source = inspect.getsource(UpstreamOneBotV11Adapter._handle_ws)  # noqa: SLF001 - 有意钉住上游实现
        assert 'headers.get("x-self-id")' in source
        assert source.index('headers.get("x-self-id")') < source.index("_check_access_token")

    def test_upstream_checks_exactly_one_global_token(self) -> None:
        """上游拿的是**唯一一个**配置项，没有任何按连接区分的余地 —— 这正是断链的成因"""
        source = inspect.getsource(UpstreamOneBotV11Adapter._check_access_token)  # noqa: SLF001
        assert "self.onebot_config.onebot_access_token" in source
        # 上游的两句拒绝文案我们逐字复用，排障经验因此可以平移
        assert MSG_TOKEN_INVALID in source
        assert MSG_TOKEN_MISSING in source

    def test_ws_close_code_matches_upstream(self) -> None:
        source = inspect.getsource(UpstreamOneBotV11Adapter._handle_ws)  # noqa: SLF001
        assert f"close({WS_CLOSE_CODE_POLICY_VIOLATION}" in source

    def test_bearer_parser_is_upstream_function(self) -> None:
        """令牌的解析口径直接复用上游函数，不另写一套（`Bearer` 与 `Token` 两种 scheme）"""
        assert gate.get_auth_bearer is get_auth_bearer
        assert get_auth_bearer("Bearer abc") == "abc"
        assert get_auth_bearer("Token abc") == "abc"
        assert get_auth_bearer("Basic abc") is None
        assert get_auth_bearer(None) is None

    def test_literals_match_upstream_registration(self) -> None:
        """用例里那批路径字面量必须就是上游 `_setup()` 注册的那几条

        上游多注册一条别名而我们没跟上 = 多一个不设防的入口，必须在这里当场红。
        """
        source = inspect.getsource(UpstreamOneBotV11Adapter._setup)  # noqa: SLF001
        registered = set(re.findall(r'URL\("(/onebot/v11[^"]*)"\)', source))
        assert registered == set(UPSTREAM_WS_PATHS) | set(UPSTREAM_HTTP_PATHS)
        # 而我们的前缀必须覆盖它们全部
        assert all(path.startswith(ONEBOT_PATH_PREFIX) for path in registered)

    def test_upstream_http_report_path_has_no_token_check(self) -> None:
        """上游的 HTTP 上报路径**完全不校验令牌** —— 这是我们要一并堵上的那个洞

        它一旦被上游补上，本模块 `_should_guard` 里的 http 那一支就成了重复校验，
        可以（也应该）重新评估。
        """
        source = inspect.getsource(UpstreamOneBotV11Adapter._handle_http)  # noqa: SLF001
        assert "_check_access_token" not in source
        assert "_check_signature" in source


# ---------------------------------------------------------------------------
# 2. 判据本身
# ---------------------------------------------------------------------------


class TestCheckToken:
    """表驱动：这是整条链路上唯一的判据"""

    @pytest.mark.parametrize(
        ("expected", "presented", "allowed", "message"),
        [
            # 不配令牌 = 不校验。这一档是老部署与"陌生协议端首次连入"的行为，不能收紧
            ("", None, True, ""),
            ("", "whatever", True, ""),
            # 配了令牌
            (DEPLOYMENT_TOKEN, DEPLOYMENT_TOKEN, True, ""),
            (DEPLOYMENT_TOKEN, None, False, MSG_TOKEN_MISSING),
            (DEPLOYMENT_TOKEN, "", False, MSG_TOKEN_MISSING),
            (DEPLOYMENT_TOKEN, "wrong", False, MSG_TOKEN_INVALID),
            # 前缀 / 大小写 / 首尾空白都不算匹配
            (DEPLOYMENT_TOKEN, DEPLOYMENT_TOKEN[:-1], False, MSG_TOKEN_INVALID),
            (DEPLOYMENT_TOKEN, DEPLOYMENT_TOKEN + "x", False, MSG_TOKEN_INVALID),
            (DEPLOYMENT_TOKEN, DEPLOYMENT_TOKEN.upper(), False, MSG_TOKEN_INVALID),
            (DEPLOYMENT_TOKEN, f" {DEPLOYMENT_TOKEN} ", False, MSG_TOKEN_INVALID),
            # 实例令牌与部署级令牌是两把不同的钥匙
            (INSTANCE_TOKEN, DEPLOYMENT_TOKEN, False, MSG_TOKEN_INVALID),
        ],
    )
    def test_table(self, expected: str, presented: Optional[str], allowed: bool, message: str) -> None:
        verdict = check_token(expected, presented)
        assert verdict.allowed is allowed
        assert verdict.message == message

    def test_non_ascii_token_does_not_explode(self) -> None:
        """`secrets.compare_digest` 对非 ASCII 的 str 会抛 TypeError，必须先编码

        令牌是调用方可以自定义的（`InstanceCreateRequest.access_token`），
        抛出去的后果是握手 500 而不是拒绝。
        """
        assert check_token("令牌中文", "令牌中文").allowed is True
        assert check_token("令牌中文", "别的").allowed is False


# ---------------------------------------------------------------------------
# 3. 期望令牌的解析
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestResolveExpectedToken:
    async def test_instance_with_own_token(self, deployment_token: str) -> None:
        """有自己令牌的实例：按它自己那枚算，部署级值不参与"""
        expected = await resolve_expected_token(SCOPED_ACCOUNT)
        assert expected.token == INSTANCE_TOKEN
        assert expected.token != deployment_token
        assert expected.source is TokenSource.INSTANCE
        assert expected.instance_key == SCOPED_INSTANCE

    async def test_instance_without_own_token_falls_back(self, deployment_token: str) -> None:
        """没有自己令牌的实例：退回部署级值，与改造前逐字相同"""
        expected = await resolve_expected_token(LEGACY_ACCOUNT)
        assert expected.token == deployment_token
        assert expected.source is TokenSource.DEPLOYMENT
        assert expected.instance_key == LEGACY_INSTANCE

    async def test_unknown_account_falls_back(self, deployment_token: str) -> None:
        """库里没有的账号（陌生协议端首次连入）：部署级值

        这一档必须成立，否则"连上来 → 被自动登记为待认领实例"这条链路在第一步就断了。
        """
        expected = await resolve_expected_token(UNKNOWN_ACCOUNT)
        assert expected.token == deployment_token
        assert expected.source is TokenSource.DEPLOYMENT
        assert expected.instance_key == ""

    async def test_tombstone_account_falls_back(self, deployment_token: str) -> None:
        """软删墓碑不是可路由的归属方，判据与入站路由同一条（`get_by_bound_account`）"""
        expected = await resolve_expected_token(TOMBSTONE_ACCOUNT)
        assert expected.token == deployment_token
        assert expected.source is TokenSource.DEPLOYMENT

    async def test_missing_self_id_falls_back(self, deployment_token: str) -> None:
        """没带 `X-Self-ID`：按部署级值判，随后上游会因为缺这个头自行拒掉"""
        expected = await resolve_expected_token("")
        assert expected.token == deployment_token
        assert expected.source is TokenSource.DEPLOYMENT

    async def test_no_deployment_token_and_no_instance_token_means_no_check(
        self,
        no_deployment_token: str,
    ) -> None:
        """两头都没有令牌 = 不校验，与改造前逐字相同（今天 `.env.prod` 的形态）"""
        expected = await resolve_expected_token(LEGACY_ACCOUNT)
        assert expected.token == ""
        assert check_token(expected.token, None).allowed is True

    async def test_instance_token_still_enforced_when_deployment_token_empty(
        self,
        no_deployment_token: str,
    ) -> None:
        """部署级令牌为空**不**意味着实例令牌也失效

        这条是"填不填部署级令牌"这个死局的出口：不填也不再是完全敞开的。
        """
        expected = await resolve_expected_token(SCOPED_ACCOUNT)
        assert expected.token == INSTANCE_TOKEN
        assert check_token(expected.token, None).allowed is False

    async def test_db_failure_degrades_to_deployment_rule(
        self,
        deployment_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """查库炸了不能让所有协议端掉线：退回部署级规则（不比改造前更松）"""

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("db is down")

        monkeypatch.setattr(DBAdapterInstance, "get_by_bound_account", boom)
        expected = await resolve_expected_token(SCOPED_ACCOUNT)
        assert expected.token == deployment_token
        assert expected.source is TokenSource.DEPLOYMENT_FALLBACK

    async def test_metadata_failure_degrades_to_deployment_rule(
        self,
        deployment_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """元数据读不出来同理"""

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("metadata is broken")

        monkeypatch.setattr(container_manager, "resolve_access_token", boom)
        expected = await resolve_expected_token(SCOPED_ACCOUNT)
        assert expected.token == deployment_token
        assert expected.source is TokenSource.DEPLOYMENT_FALLBACK
        assert expected.instance_key == SCOPED_INSTANCE


# ---------------------------------------------------------------------------
# 4. 「下发即校验」—— 本次修复的核心不变量
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestProvisionedTokenEqualsExpectedToken:
    """握手期望的令牌必须与下发给协议端的是同一个值

    改造前正是这条不成立：`create_instance` 生成一枚、`container_manager` 下发一枚、
    校验侧却只认部署级那一枚。任一侧被改动（比如有人给校验侧另写一份元数据解析）
    都会让这组用例红。
    """

    @pytest.mark.parametrize("account", [SCOPED_ACCOUNT, LEGACY_ACCOUNT])
    async def test_expected_matches_managed_entry(self, account: str, deployment_token: str) -> None:
        instance = await DBAdapterInstance.get_by_bound_account(ADAPTER_KEY, account)
        assert instance is not None
        managed_entry = container_manager.build_managed_entry(instance)
        expected = await resolve_expected_token(account)
        assert managed_entry.token == expected.token
        # 下发进 NapCat 配置的就是这条托管连接，握手时用它出示的正是这个值
        assert check_token(expected.token, managed_entry.token).allowed is True

    async def test_newly_created_instance_can_connect_with_provisioned_token(
        self,
        deployment_token: str,
    ) -> None:
        """走真的建实例路径：生成的令牌能连上，部署级令牌连不上

        这一条同时钉住"生成侧"：`create_instance` 若不再写这个键，第一句断言就红。
        """
        from nekro_agent.schemas.console import InstanceCreateRequest, InstanceProvider

        instance = await instance_manager.create_instance(
            InstanceCreateRequest(
                instance_key="fresh1",
                adapter_key=ADAPTER_KEY,
                provider=InstanceProvider.REMOTE_ONEBOT,
                provider_account_id="10009",
            ),
        )
        provisioned = container_manager.resolve_access_token(instance)
        assert provisioned and provisioned != deployment_token

        expected = await resolve_expected_token("10009")
        assert check_token(expected.token, provisioned).allowed is True
        assert check_token(expected.token, deployment_token).allowed is False

    async def test_explicit_token_is_stripped_before_storing(self, deployment_token: str) -> None:
        """抄粘带进来的首尾空白会让"抄过去的"与"存下来的"不等长，必须在写入时归一"""
        from nekro_agent.schemas.console import InstanceCreateRequest, InstanceProvider

        instance = await instance_manager.create_instance(
            InstanceCreateRequest(
                instance_key="fresh2",
                adapter_key=ADAPTER_KEY,
                provider=InstanceProvider.REMOTE_ONEBOT,
                provider_account_id="10010",
                access_token=f"  {INSTANCE_TOKEN}  ",
            ),
        )
        assert container_manager.resolve_access_token(instance) == INSTANCE_TOKEN

    async def test_auto_registered_instance_gets_no_own_token(self, deployment_token: str) -> None:
        """自动登记的待认领实例**不**分配自己的令牌

        NA 碰不到用户自建协议端的配置，凭空生成一枚等于下次重连就把它拒了。
        """
        from nekro_agent.adapters.onebot_v11.core.instance import (
            _register_unclaimed_instance,  # noqa: PLC2701 - 有意直接打这条内部路径
            clear_instance_cache,
        )

        clear_instance_cache()
        instance = await _register_unclaimed_instance("10011")
        assert container_manager.read_container_metadata(instance).access_token == ""
        expected = await resolve_expected_token("10011")
        assert expected.token == deployment_token
        clear_instance_cache()


# ---------------------------------------------------------------------------
# 5. 中间件行为
# ---------------------------------------------------------------------------


class _RecordingApp:
    """记录下游拿到的 scope；被调到就说明中间件放行了"""

    def __init__(self) -> None:
        self.calls: List[MutableMapping[str, Any]] = []

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        self.calls.append(scope)

    @property
    def called(self) -> bool:
        return bool(self.calls)


def _ws_scope(self_id: str, token: Optional[str], path: str = "/onebot/v11/ws") -> Dict[str, Any]:
    headers: List[Tuple[bytes, bytes]] = [(b"host", b"nekro_agent:8021")]
    if self_id:
        headers.append((b"X-Self-ID", self_id.encode()))
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return {"type": "websocket", "path": path, "headers": headers}


def _http_scope(self_id: str, token: Optional[str], method: str = "POST") -> Dict[str, Any]:
    scope = _ws_scope(self_id, token, path="/onebot/v11/")
    scope["type"] = "http"
    scope["method"] = method
    return scope


async def _run(scope: MutableMapping[str, Any]) -> Tuple[_RecordingApp, List[Dict[str, Any]]]:
    downstream = _RecordingApp()
    sent: List[Dict[str, Any]] = []

    async def receive() -> Dict[str, Any]:
        return {"type": "websocket.connect"} if scope["type"] == "websocket" else {"type": "http.request"}

    async def send(message: Dict[str, Any]) -> None:
        sent.append(message)

    await OneBotAccessTokenMiddleware(downstream)(scope, receive, send)
    return downstream, sent


def _header(scope: MutableMapping[str, Any], name: str) -> Optional[str]:
    for raw_name, raw_value in scope["headers"]:
        if bytes(raw_name).decode().lower() == name:
            return bytes(raw_value).decode()
    return None


@pytest.mark.usefixtures("db")
class TestWebSocketHandshake:
    async def test_correct_instance_token_passes(self, deployment_token: str) -> None:
        downstream, sent = await _run(_ws_scope(SCOPED_ACCOUNT, INSTANCE_TOKEN))
        assert downstream.called
        assert sent == []

    async def test_wrong_token_is_rejected(self, deployment_token: str) -> None:
        downstream, sent = await _run(_ws_scope(SCOPED_ACCOUNT, "not-the-token"))
        assert not downstream.called
        assert sent == [
            {"type": "websocket.close", "code": WS_CLOSE_CODE_POLICY_VIOLATION, "reason": MSG_TOKEN_INVALID},
        ]

    async def test_missing_token_is_rejected(self, deployment_token: str) -> None:
        downstream, sent = await _run(_ws_scope(SCOPED_ACCOUNT, None))
        assert not downstream.called
        assert sent[0]["reason"] == MSG_TOKEN_MISSING

    async def test_deployment_token_does_not_open_a_scoped_instance(self, deployment_token: str) -> None:
        """平台方那枚不是万能钥匙 —— 否则"实例隔离"只是个说法"""
        downstream, _ = await _run(_ws_scope(SCOPED_ACCOUNT, deployment_token))
        assert not downstream.called

    async def test_legacy_instance_uses_deployment_token(self, deployment_token: str) -> None:
        """老部署形态：实例没有自己的令牌，部署级令牌照旧管用"""
        downstream, _ = await _run(_ws_scope(LEGACY_ACCOUNT, deployment_token))
        assert downstream.called

    async def test_legacy_instance_rejects_other_instances_token(self, deployment_token: str) -> None:
        downstream, _ = await _run(_ws_scope(LEGACY_ACCOUNT, INSTANCE_TOKEN))
        assert not downstream.called

    async def test_no_token_configured_anywhere_passes(self, no_deployment_token: str) -> None:
        """两头都没配 = 不校验（今天的行为），一个字都不能变"""
        downstream, _ = await _run(_ws_scope(LEGACY_ACCOUNT, None))
        assert downstream.called

    async def test_unknown_account_uses_deployment_token(self, deployment_token: str) -> None:
        downstream, _ = await _run(_ws_scope(UNKNOWN_ACCOUNT, deployment_token))
        assert downstream.called
        downstream, _ = await _run(_ws_scope(UNKNOWN_ACCOUNT, "nope"))
        assert not downstream.called

    @pytest.mark.parametrize("path", UPSTREAM_WS_PATHS)
    async def test_every_upstream_ws_path_is_guarded(self, path: str, deployment_token: str) -> None:
        """上游把 WS 端点注册在四个路径别名上，漏掉任何一个都是一个绕过口"""
        downstream, _ = await _run(_ws_scope(SCOPED_ACCOUNT, "nope", path=path))
        assert not downstream.called

    async def test_disconnect_before_handshake_is_not_answered(self, deployment_token: str) -> None:
        """收到的不是 `websocket.connect` 时什么都不发，避免对着已断开的连接写"""
        scope = _ws_scope(SCOPED_ACCOUNT, "nope")
        sent: List[Dict[str, Any]] = []

        async def receive() -> Dict[str, Any]:
            return {"type": "websocket.disconnect", "code": 1006}

        async def send(message: Dict[str, Any]) -> None:
            sent.append(message)

        await OneBotAccessTokenMiddleware(_RecordingApp())(scope, receive, send)
        assert sent == []


@pytest.mark.usefixtures("db")
class TestEndorsement:
    """放行之后要让下游 NoneBot 那道部署级校验也通过"""

    async def test_authorization_is_rewritten_to_deployment_token(self, deployment_token: str) -> None:
        """实例令牌 != 部署级令牌时，不背书就会被下游再拒一次 —— 正是死局那一半"""
        downstream, _ = await _run(_ws_scope(SCOPED_ACCOUNT, INSTANCE_TOKEN))
        scope = downstream.calls[0]
        assert _header(scope, "authorization") == f"Bearer {deployment_token}"
        # 下游还要靠这个头解析账号，不能在改写时被顺手丢掉
        assert _header(scope, "x-self-id") == SCOPED_ACCOUNT

    async def test_only_one_authorization_header_remains(self, deployment_token: str) -> None:
        downstream, _ = await _run(_ws_scope(SCOPED_ACCOUNT, INSTANCE_TOKEN))
        names = [bytes(name).decode().lower() for name, _ in downstream.calls[0]["headers"]]
        assert names.count("authorization") == 1

    async def test_no_rewrite_when_deployment_token_is_empty(self, no_deployment_token: str) -> None:
        """部署级令牌为空时下游本来就不校验，少改一个头就少一处出错可能"""
        downstream, _ = await _run(_ws_scope(SCOPED_ACCOUNT, INSTANCE_TOKEN))
        assert _header(downstream.calls[0], "authorization") == f"Bearer {INSTANCE_TOKEN}"

    async def test_endorsement_would_satisfy_upstream_check(self, deployment_token: str) -> None:
        """把改写后的头喂给上游那套解析，结论必须是"通过"

        直接调上游的 `get_auth_bearer` + 上游的比较规则，而不是复述我们自己的判据。
        """
        downstream, _ = await _run(_ws_scope(SCOPED_ACCOUNT, INSTANCE_TOKEN))
        presented_to_upstream = get_auth_bearer(_header(downstream.calls[0], "authorization"))
        assert presented_to_upstream == deployment_token


@pytest.mark.usefixtures("db")
class TestHttpReportPath:
    """上游的 HTTP 上报路径不校验令牌，只堵 WS 等于没堵"""

    async def test_post_with_wrong_token_is_rejected(self, deployment_token: str) -> None:
        downstream, sent = await _run(_http_scope(SCOPED_ACCOUNT, "nope"))
        assert not downstream.called
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 403
        assert sent[1]["body"] == MSG_TOKEN_INVALID.encode()

    async def test_post_with_correct_token_passes(self, deployment_token: str) -> None:
        downstream, _ = await _run(_http_scope(SCOPED_ACCOUNT, INSTANCE_TOKEN))
        assert downstream.called

    async def test_post_passes_when_nothing_is_configured(self, no_deployment_token: str) -> None:
        downstream, _ = await _run(_http_scope(LEGACY_ACCOUNT, None))
        assert downstream.called

    async def test_get_is_not_guarded(self, deployment_token: str) -> None:
        """只有上报是 POST；对 GET 动手会波及上游将来可能加的探活类端点"""
        downstream, _ = await _run(_http_scope(SCOPED_ACCOUNT, "nope", method="GET"))
        assert downstream.called


@pytest.mark.usefixtures("db")
class TestHeaderParityWithDownstream:
    """同名头取哪一条，必须与下游逐字一致 —— 不一致就有走私空间"""

    def test_first_occurrence_wins_on_both_sides(self) -> None:
        """下游是 `CIMultiDict(starlette.Headers.items()).get(...)`，取第一条

        若哪天下游改成取最后一条，攻击者就能用「第一个 X-Self-ID 过校验、
        第二个被下游拿去绑 Bot」冒充别的账号。用真的两个类打靶，不复述结论。
        """
        from multidict import CIMultiDict
        from starlette.datastructures import Headers

        raw = [
            (b"x-self-id", SCOPED_ACCOUNT.encode()),
            (b"x-self-id", LEGACY_ACCOUNT.encode()),
            (b"authorization", b"Bearer one"),
            (b"authorization", b"Bearer two"),
        ]
        downstream = CIMultiDict(Headers(raw=raw).items())
        ours = gate._read_headers({"headers": raw})  # noqa: SLF001 - 就是要打这个内部函数

        assert downstream.get("x-self-id") == ours["x-self-id"] == SCOPED_ACCOUNT
        assert downstream.get("authorization") == ours["authorization"] == "Bearer one"

    async def test_duplicate_self_id_cannot_smuggle_another_account(self, deployment_token: str) -> None:
        """拿 legacy 那一档的令牌 + 伪造的第二个 X-Self-ID，不能连成 scoped 那个实例"""
        scope: Dict[str, Any] = {
            "type": "websocket",
            "path": "/onebot/v11/ws",
            "headers": [
                (b"x-self-id", SCOPED_ACCOUNT.encode()),
                (b"x-self-id", LEGACY_ACCOUNT.encode()),
                (b"authorization", f"Bearer {deployment_token}".encode()),
            ],
        }
        downstream, _ = await _run(scope)
        assert not downstream.called


@pytest.mark.usefixtures("db")
class TestRootPathMounting:
    """NA 被挂在子路径下时这道门不能整个失效"""

    async def test_guarded_behind_root_path(self, deployment_token: str) -> None:
        scope: Dict[str, Any] = {
            "type": "websocket",
            "path": "/nekro/onebot/v11/ws",
            "root_path": "/nekro",
            "headers": [(b"x-self-id", SCOPED_ACCOUNT.encode()), (b"authorization", b"Bearer nope")],
        }
        downstream, _ = await _run(scope)
        assert not downstream.called

    async def test_root_path_does_not_swallow_unrelated_paths(self, deployment_token: str) -> None:
        scope: Dict[str, Any] = {
            "type": "websocket",
            "path": "/nekro/api/workspaces/1/sandbox/terminal",
            "root_path": "/nekro",
            "headers": [],
        }
        downstream, sent = await _run(scope)
        assert downstream.called
        assert sent == []


@pytest.mark.usefixtures("db")
class TestScopesLeftAlone:
    """不该管的一律不碰"""

    @pytest.mark.parametrize(
        ("scope_type", "path"),
        [
            ("http", "/api/health"),
            ("http", "/api/console/instances"),
            ("websocket", "/api/workspaces/1/sandbox/terminal"),
            ("websocket", "/onebot/v12/ws"),
            ("lifespan", ""),
        ],
    )
    async def test_untouched(self, scope_type: str, path: str, deployment_token: str) -> None:
        scope: Dict[str, Any] = {"type": scope_type, "path": path, "method": "POST", "headers": []}
        downstream, sent = await _run(scope) if scope_type != "lifespan" else (await _run_lifespan(scope))
        assert downstream.called
        assert sent == []


async def _run_lifespan(scope: MutableMapping[str, Any]) -> Tuple[_RecordingApp, List[Dict[str, Any]]]:
    downstream = _RecordingApp()
    sent: List[Dict[str, Any]] = []

    async def receive() -> Dict[str, Any]:
        return {"type": "lifespan.startup"}

    async def send(message: Dict[str, Any]) -> None:
        sent.append(message)

    await OneBotAccessTokenMiddleware(downstream)(scope, receive, send)
    return downstream, sent


# ---------------------------------------------------------------------------
# 6. 接线：中间件真的挂上去了
# ---------------------------------------------------------------------------


class TestWiring:
    """只有判据没有接线等于没做 —— 这一组盯住"它确实被挂进了应用" """

    def test_mount_middlewares_registers_the_gate(self) -> None:
        from fastapi import FastAPI

        from nekro_agent.routers import mount_middlewares

        app = FastAPI()
        mount_middlewares(app)
        assert OneBotAccessTokenMiddleware in [entry.cls for entry in app.user_middleware]

    def test_gate_is_not_a_base_http_middleware(self) -> None:
        """`@app.middleware("http")` 那种写法看不见 websocket scope，握手就不会经过它"""
        source = inspect.getsource(gate.OneBotAccessTokenMiddleware)
        assert "async def __call__(self, scope" in source


# ---------------------------------------------------------------------------
# 7. 部署级令牌的解析口径（裸机那颗雷）
# ---------------------------------------------------------------------------


class TestDeploymentTokenResolution:
    def test_falls_back_to_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "  from-env  ")
        assert resolve_deployment_onebot_access_token() == "from-env"

    def test_empty_when_nothing_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)
        assert resolve_deployment_onebot_access_token() == ""

    def test_registered_adapter_wins_over_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """裸机部署上环境变量里没有它、env 文件里有 —— 权威只能是适配器那份配置"""
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "stale-env-value")
        monkeypatch.setattr(os_env, "_driver_onebot_access_token", lambda: "from-adapter")
        assert resolve_deployment_onebot_access_token() == "from-adapter"

    def test_adapter_configured_empty_beats_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """适配器在场就以它为准，"配成空串"与"读不到"必须是两回事

        混为一谈的话，环境里残留的一个旧值会让 NA 下发一枚下游根本不认的令牌。
        """
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "stale-env-value")
        monkeypatch.setattr(os_env, "_driver_onebot_access_token", lambda: "")
        assert resolve_deployment_onebot_access_token() == ""

    def test_secret_str_shielded_value_is_unwrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`nekro_log_hygiene` 会把凭据类键临时换成 SecretStr，还原顺序一变不能静默读到掩码"""
        from pydantic import SecretStr

        class _FakeConfig:
            onebot_access_token = SecretStr("shielded-value")

        class _FakeAdapter:
            onebot_config = _FakeConfig()

        import nonebot

        monkeypatch.setattr(nonebot, "get_adapter", lambda _cls: _FakeAdapter())
        assert resolve_deployment_onebot_access_token() == "shielded-value"

    def test_run_bot_only_exports_nekro_prefixed_keys(self) -> None:
        """这条上游事实正是"不能只读 os.environ"的理由，它一变本模块的取舍就该重估

        `run_bot.py` 把 env 文件搬进 `os.environ` 时只搬 `NEKRO_` 前缀的键，
        而 `ONEBOT_ACCESS_TOKEN` 没有这个前缀 —— 裸机部署上它永远读不到。
        """
        source = (REPO_ROOT / "run_bot.py").read_text(encoding="utf-8")
        assert re.search(r'if\s+key\.startswith\("NEKRO_"\):\s*\n\s*os\.environ\[key\]\s*=\s*value', source)
        assert "ONEBOT_ACCESS_TOKEN" not in source

    def test_env_files_still_declare_the_key(self) -> None:
        """`.env.prod` 是镜像里那份、键少得可数，它必须仍然登记着这一项"""
        for name in (".env.prod", ".env.example"):
            content = (REPO_ROOT / name).read_text(encoding="utf-8")
            assert re.search(r"^ONEBOT_ACCESS_TOKEN=", content, re.MULTILINE), name

    def test_compose_files_still_pass_the_key_through(self) -> None:
        """env 文件与 compose 是两份清单，只改一份的后果是容器里根本没有这个变量"""
        for name in (
            "docker/docker-compose.yml",
            "docker/docker-compose-x-napcat.yml",
            "docker/docker-compose-x-hardened.yml",
        ):
            content = (REPO_ROOT / name).read_text(encoding="utf-8")
            assert "ONEBOT_ACCESS_TOKEN=${ONEBOT_ACCESS_TOKEN:-}" in content, name


class TestNoStaleModuleConstant:
    """改造前那个 `os_env.ONEBOT_ACCESS_TOKEN` 常量必须消失

    留着它就是留一个陷阱：它在裸机上恒为空串，而名字看起来完全像"部署级令牌"。
    """

    def test_constant_is_gone(self) -> None:
        assert not hasattr(os_env, "ONEBOT_ACCESS_TOKEN")

    def test_nobody_imports_it_anymore(self) -> None:
        """扫描的覆盖面本身也要立得住（以前有一次"0 命中"就是扫窄了扫出来的）

        - **括号跨行的 import 必须能命中**：改造前 `adapters/onebot_v11/routers.py`
          写的正是 `from ... import (\\n    ONEBOT_ACCESS_TOKEN,\\n ...)`，
          一条 `^from .*import .*TOKEN` 的单行正则**看不见它**；
        - 扫描根目录不止 `nekro_agent/`：`run_bot.py`、`nekro_log_hygiene.py`、
          `plugins/` 都能 import 它（`tests/` 不扫：用例里有意逐字写着这个名字）。
        """
        pattern = re.compile(r"from\s+[\w.]*os_env\s+import\s+(?:\(([^)]*)\)|([^\n(]*))")
        offenders: List[str] = []
        roots = [REPO_ROOT / name for name in ("nekro_agent", "plugins")]
        files = [path for root in roots for path in root.rglob("*.py") if "__pycache__" not in path.parts]
        files += [REPO_ROOT / "run_bot.py", REPO_ROOT / "nekro_log_hygiene.py"]
        for path in files:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for parenthesised, inline in pattern.findall(text):
                if re.search(r"\bONEBOT_ACCESS_TOKEN\b", parenthesised or inline):
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == []

    def test_the_scanner_above_would_actually_see_a_multiline_import(self) -> None:
        """对照组：把改造前那段**跨行** import 原样喂给同一条正则，必须命中

        没有这一条，上面那个 `assert offenders == []` 只是"我没扫到"而不是"确实没有"。
        """
        pattern = re.compile(r"from\s+[\w.]*os_env\s+import\s+(?:\(([^)]*)\)|([^\n(]*))")
        pre_fix_snippet = (
            "from nekro_agent.core.os_env import (\n"
            "\n"
            "    NAPCAT_ONEBOT_ADAPTER_DIR,\n"
            "    ONEBOT_ACCESS_TOKEN,\n"
            "    OsEnv,\n"
            ")\n"
        )
        hits = [
            group
            for group in pattern.findall(pre_fix_snippet)
            if re.search(r"\bONEBOT_ACCESS_TOKEN\b", group[0] or group[1])
        ]
        assert len(hits) == 1
        # 单行形态同样要命中
        assert pattern.findall("from nekro_agent.core.os_env import ONEBOT_ACCESS_TOKEN\n")


# ---------------------------------------------------------------------------
# 8. 真实 ASGI 栈：中间件不只是"在列表里"，它必须真的拦得住
# ---------------------------------------------------------------------------


class TestRealAsgiStack:
    """接线的强断言：把 `mount_middlewares` 挂到一个真的 FastAPI 应用上打

    `test_mount_middlewares_registers_the_gate` 只证明"类在 `user_middleware` 里"，
    那是一次名单核对：中间件被别的中间件挡在外面、websocket scope 根本流不到它、
    或 Starlette 换了构建顺序，名单核对全都看不见。这一组走 `TestClient`，
    经过 CORS + 计时中间件 + 异常处理器构成的**真实栈**，再落到真实路径上。

    刻意**不初始化数据库**：`resolve_expected_token` 的查库会失败并按既定规则退回
    部署级令牌（`DEPLOYMENT_FALLBACK`），于是这一组打的是"部署级那一档"的行为，
    与上面那批按实例的用例互补，且不引入跨事件循环的 Tortoise 连接。
    """

    @staticmethod
    def _app():
        from fastapi import FastAPI

        from nekro_agent.routers import mount_middlewares

        app = FastAPI()
        mount_middlewares(app)

        @app.websocket("/onebot/v11/ws")
        async def _ws(websocket: WebSocket) -> None:  # pragma: no cover - 被拒时不会执行
            await websocket.accept()
            await websocket.send_text("upstream-reached")
            await websocket.close()

        @app.post("/onebot/v11/")
        async def _http() -> Dict[str, str]:
            return {"ok": "upstream-reached"}

        return app

    def test_ws_handshake_without_token_is_rejected_through_the_real_stack(
        self,
        deployment_token: str,
    ) -> None:
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        with TestClient(self._app()) as client, pytest.raises(WebSocketDisconnect) as excinfo:  # noqa: PT012
            with client.websocket_connect("/onebot/v11/ws", headers={"X-Self-ID": UNKNOWN_ACCOUNT}) as ws:
                ws.receive_text()
        assert excinfo.value.code == WS_CLOSE_CODE_POLICY_VIOLATION
        assert excinfo.value.reason == MSG_TOKEN_MISSING

    def test_ws_handshake_with_token_reaches_upstream_through_the_real_stack(
        self,
        deployment_token: str,
    ) -> None:
        from starlette.testclient import TestClient

        with TestClient(self._app()) as client, client.websocket_connect(
            "/onebot/v11/ws",
            headers={"X-Self-ID": UNKNOWN_ACCOUNT, "Authorization": f"Bearer {deployment_token}"},
        ) as ws:
            assert ws.receive_text() == "upstream-reached"

    def test_http_report_without_token_is_rejected_through_the_real_stack(
        self,
        deployment_token: str,
    ) -> None:
        from starlette.testclient import TestClient

        with TestClient(self._app()) as client:
            response = client.post("/onebot/v11/", headers={"X-Self-ID": UNKNOWN_ACCOUNT})
        assert response.status_code == 403
        assert response.text == MSG_TOKEN_MISSING

    def test_http_report_with_token_reaches_upstream_through_the_real_stack(
        self,
        deployment_token: str,
    ) -> None:
        from starlette.testclient import TestClient

        with TestClient(self._app()) as client:
            response = client.post(
                "/onebot/v11/",
                headers={"X-Self-ID": UNKNOWN_ACCOUNT, "Authorization": f"Bearer {deployment_token}"},
            )
        assert response.status_code == 200
        assert response.json() == {"ok": "upstream-reached"}

    def test_unrelated_api_path_is_untouched_through_the_real_stack(self, deployment_token: str) -> None:
        """这道门不能顺手把 NA 自己的接口也要求带 OneBot 令牌"""
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        from nekro_agent.routers import mount_middlewares

        app = FastAPI()
        mount_middlewares(app)

        @app.post("/api/whatever")
        async def _api() -> Dict[str, str]:
            return {"ok": "reached"}

        with TestClient(app) as client:
            response = client.post("/api/whatever")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# 9. 「该抄进协议端配置的那枚令牌」的第二个出口：总后台那个接口
# ---------------------------------------------------------------------------


def _platform_owner() -> DBUser:
    """平台方身份。这一族端点全是 `Role.Super`，构造一个够格的用户直接打函数"""
    return DBUser(
        username="platform-owner",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
    )


@pytest.mark.usefixtures("db")
class TestConsoleTokenEndpointParity:
    """`GET /adapters/onebot_v11/container/onebot-token` 必须回答"这个实例要出示哪枚令牌"

    这是判据的**第二个出口**：握手侧按实例校验，而前端 NapCat 页面照着这个接口显示
    "OneBot 接入密钥"并提供一键复制。它要是继续只回答部署级那一枚，用户抄过去的就是
    一枚**会被自己拒掉**的令牌 —— 正是本轮要消灭的那类"填上就断连"，只是把它挪到了人身上。
    """

    async def test_matches_the_gate_for_every_instance_shape(self, deployment_token: str) -> None:
        for instance_key, account in ((SCOPED_INSTANCE, SCOPED_ACCOUNT), (LEGACY_INSTANCE, LEGACY_ACCOUNT)):
            response = await onebot_routers.get_onebot_token(
                instance_key=instance_key,
                _current_user=_platform_owner(),
            )
            expected = await resolve_expected_token(account)
            assert response.token == (expected.token or None), instance_key
            assert check_token(expected.token, response.token).allowed is True, instance_key

    async def test_scoped_instance_does_not_get_the_deployment_token(self, deployment_token: str) -> None:
        """有自己令牌的实例，这里必须**不是**平台方那一枚"""
        response = await onebot_routers.get_onebot_token(
            instance_key=SCOPED_INSTANCE,
            _current_user=_platform_owner(),
        )
        assert response.token == INSTANCE_TOKEN
        assert response.token != deployment_token

    async def test_matches_what_gets_written_into_the_protocol_config(self, deployment_token: str) -> None:
        """页面上显示的那枚，必须与下发进 NapCat 配置的那枚逐字相同"""
        instance = await DBAdapterInstance.get_by_bound_account(ADAPTER_KEY, SCOPED_ACCOUNT)
        assert instance is not None
        response = await onebot_routers.get_onebot_token(
            instance_key=SCOPED_INSTANCE,
            _current_user=_platform_owner(),
        )
        assert response.token == container_manager.build_managed_entry(instance).token

    async def test_without_instance_key_returns_the_deployment_token(self, deployment_token: str) -> None:
        """单实例老部署的前端不带这个参数，行为逐字不变"""
        response = await onebot_routers.get_onebot_token(_current_user=_platform_owner())
        assert response.token == deployment_token

    async def test_without_instance_key_and_without_deployment_token_is_none(
        self,
        no_deployment_token: str,
    ) -> None:
        response = await onebot_routers.get_onebot_token(_current_user=_platform_owner())
        assert response.token is None

    async def test_unknown_instance_is_not_found(self, deployment_token: str) -> None:
        """越权/不存在一律 404，与实例族其余端点同一口径"""
        from nekro_agent.schemas.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await onebot_routers.get_onebot_token(
                instance_key="nope_not_here",
                _current_user=_platform_owner(),
            )

    def test_every_container_endpoint_carries_a_role_gate(self) -> None:
        """整族端点都得有身份门 —— #46 那次正是"总后台修好了、适配器这族一道校验都没有"

        判据不看装饰器长什么样，看**源码里这个函数上方确实写了 `@require_role`**，
        并且被包函数的参数名是 `_current_user`（名字不对 `require_role` 不生效，见 §2）。
        """
        source = inspect.getsource(onebot_routers)
        endpoints = re.findall(
            r"@router\.(?:get|post|put|patch|delete)\([^)]*\)\s*\n(?P<deco>(?:@[^\n]*\n)*)"
            r"async def (?P<name>\w+)\((?P<sig>[^)]*)\)",
            source,
        )
        assert endpoints, "没解析出任何端点，说明这条正则跟不上路由写法了"
        ungated = [name for deco, name, _sig in endpoints if "@require_role(" not in deco]
        assert ungated == []
        wrong_param = [name for deco, name, sig in endpoints if "@require_role(" in deco and "_current_user" not in sig]
        assert wrong_param == []


# ---------------------------------------------------------------------------
# 10. 协议端配置的第二个写入点：单实例时代那份 `onebot11_{QQ}.json`
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestLegacyAdapterFileWritePoint:
    """`restart()` 在适配器文件缺失时会补一份，那份里的 `token` 也要与校验侧同源

    `container_manager.build_managed_entry` 是第一个写入点（已有用例钉住），
    这里是第二个。两个写入点写的令牌与校验侧不是同一条判据时，表现就是协议端连不上。
    """

    @staticmethod
    def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bot_qq: str) -> Path:
        import contextlib as _contextlib

        class _FakeAdapterConfig:
            BOT_QQ = bot_qq

        class _FakeAdapter:
            config = _FakeAdapterConfig()

        @_contextlib.asynccontextmanager
        async def _fake_container(_key=None):
            class _C:
                async def restart(self, timeout: int = 30) -> None:
                    return None

            yield _C()

        monkeypatch.setattr(onebot_routers.adapter_utils, "get_typed_adapter", lambda *_a, **_k: _FakeAdapter())
        monkeypatch.setattr(onebot_routers, "get_container", _fake_container)
        monkeypatch.setattr(onebot_routers, "NAPCAT_ONEBOT_ADAPTER_DIR", str(tmp_path))
        return tmp_path / f"onebot11_{bot_qq}.json"

    @staticmethod
    def _written_token(target: Path) -> str:
        return json.loads(target.read_text(encoding="utf-8"))["network"]["websocketClients"][0]["token"]

    async def test_written_token_is_the_one_the_gate_expects_for_a_scoped_instance(
        self,
        deployment_token: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """账号绑在一个有自己令牌的实例上时，写部署级令牌 = 写一枚会被自己拒掉的令牌"""
        target = self._prepare(monkeypatch, tmp_path, SCOPED_ACCOUNT)
        await onebot_routers.restart(instance_key=None, _current_user=_platform_owner())
        written = self._written_token(target)
        expected = await resolve_expected_token(SCOPED_ACCOUNT)
        assert written == expected.token == INSTANCE_TOKEN
        assert written != deployment_token
        assert check_token(expected.token, written).allowed is True

    async def test_legacy_deployment_still_gets_the_deployment_token(
        self,
        deployment_token: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """单实例老部署（账号没绑给任何实例）逐字不变"""
        target = self._prepare(monkeypatch, tmp_path, UNKNOWN_ACCOUNT)
        await onebot_routers.restart(instance_key=None, _current_user=_platform_owner())
        assert self._written_token(target) == deployment_token

    async def test_existing_file_is_never_overwritten(
        self,
        deployment_token: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """已有文件一个字节都不动 —— 用户手改过的配置不该被一次重启抹掉"""
        target = self._prepare(monkeypatch, tmp_path, SCOPED_ACCOUNT)
        target.write_text("user-hand-written", encoding="utf-8")
        await onebot_routers.restart(instance_key=None, _current_user=_platform_owner())
        assert target.read_text(encoding="utf-8") == "user-hand-written"


# ---------------------------------------------------------------------------
# 11. 部署级令牌的其余两个出口
# ---------------------------------------------------------------------------


class TestDeploymentTokenSecondaryOutlets:
    def test_adapter_with_unset_token_is_empty_not_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """适配器在场但**没配**这一项（上游默认值就是 `None`）→ 空串，不许回落到环境变量

        回落的后果：NoneBot 那边压根不校验，而 NA 这边把环境里那个残值当成部署级令牌
        下发进协议端配置、并在网关上要求它 —— 一枚"下游根本不认识"的令牌。
        这条走的是真的 `_driver_onebot_access_token`（打 `nonebot.get_adapter`），
        而不是把那个函数整个换掉。
        """
        import nonebot

        class _FakeConfig:
            onebot_access_token = None

        class _FakeAdapter:
            onebot_config = _FakeConfig()

        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "stale-host-environ-value")
        monkeypatch.setattr(nonebot, "get_adapter", lambda _cls: _FakeAdapter())
        assert os_env._driver_onebot_access_token() == ""  # noqa: SLF001 - 就是要打这个内部函数
        assert resolve_deployment_onebot_access_token() == ""

    def test_deployment_token_is_redacted_out_of_sandbox_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """部署级令牌必须在沙盒脱敏名单里

        改造前这里读的是 `os_env.ONEBOT_ACCESS_TOKEN` 常量 —— 裸机上恒为空串，
        于是这枚凭据**从来没被脱敏过**。改回常量 / 删掉这一行都要在这里当场红。
        """
        from nekro_agent.services.sandbox.credential_guard import collect_platform_secrets

        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", DEPLOYMENT_TOKEN)
        assert DEPLOYMENT_TOKEN in collect_platform_secrets()

        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)
        # 反方向：没配的时候不能把空串塞进脱敏名单（那会让脱敏对着空串乱替换）
        assert "" not in collect_platform_secrets()


# ---------------------------------------------------------------------------
# 12. `X-Self-ID` 的取值口径与下游的差异
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestSelfIdNormalisation:
    """网关对 `X-Self-ID` 做了 `strip()`，下游（`Bot(self, self_id)`）没有

    差异只能朝**更严**的方向存在：加空白只会让网关解析到同一个账号、于是要求**那个**
    账号的令牌。要是哪天改成"网关不 strip"，`" 10001"` 就会解析不出实例、退回部署级令牌，
    等于用平台方那枚敲开一个有自己令牌的实例的门。
    """

    @pytest.mark.parametrize("raw", [f" {SCOPED_ACCOUNT}", f"{SCOPED_ACCOUNT} ", f"\t{SCOPED_ACCOUNT}\r\n"])
    async def test_whitespace_padding_cannot_downgrade_to_the_deployment_token(
        self,
        raw: str,
        deployment_token: str,
    ) -> None:
        scope: Dict[str, Any] = {
            "type": "websocket",
            "path": "/onebot/v11/ws",
            "headers": [
                (b"x-self-id", raw.encode()),
                (b"authorization", f"Bearer {deployment_token}".encode()),
            ],
        }
        downstream, _ = await _run(scope)
        assert not downstream.called

    async def test_whitespace_padded_self_id_still_matches_its_own_instance_token(
        self,
        deployment_token: str,
    ) -> None:
        scope: Dict[str, Any] = {
            "type": "websocket",
            "path": "/onebot/v11/ws",
            "headers": [
                (b"x-self-id", f" {SCOPED_ACCOUNT} ".encode()),
                (b"authorization", f"Bearer {INSTANCE_TOKEN}".encode()),
            ],
        }
        downstream, _ = await _run(scope)
        assert downstream.called


# ---------------------------------------------------------------------------
# 13. 「有令牌、还没有账号」：总后台新建实例的**首次**接入
#
# 期望令牌按账号解析，而实例是先建好、后绑账号的。所以从"建好"到"绑上账号"之间，
# 协议端出示的是实例自己那枚、而账号查不出任何实例 —— 只按部署级令牌判就是 403。
# 部署级令牌留空时这一格被"不配令牌=不校验"盖住，因此它只在**按上线清单填上之后**显形：
# 那正是本模块存在的理由（填上就断连），只是换了个入口。
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("awaiting_instance")
class TestInstanceAwaitingItsAccount:
    async def test_first_connect_with_its_own_token_is_allowed(self, deployment_token: str) -> None:
        """建好、令牌抄进协议端、还没登录 —— 第一次连上来必须放行

        这一条红 = "总后台建的实例在绑上账号之前一律连不上"，
        而绑账号恰恰要靠这条连接（或扫码弹窗那一侧）才会发生。
        """
        downstream, sent = await _run(_ws_scope(AWAITING_ACCOUNT, AWAITING_TOKEN))
        assert downstream.called
        assert sent == []

    async def test_first_connect_over_http_report_too(self, deployment_token: str) -> None:
        downstream, _ = await _run(_http_scope(AWAITING_ACCOUNT, AWAITING_TOKEN))
        assert downstream.called

    async def test_endorsement_still_happens_on_this_branch(self, deployment_token: str) -> None:
        """这一支同样要向下游背书，否则 NoneBot 那道部署级校验会把它再拒一次"""
        downstream, _ = await _run(_ws_scope(AWAITING_ACCOUNT, AWAITING_TOKEN))
        assert _header(downstream.calls[0], "authorization") == f"Bearer {deployment_token}"

    async def test_wrong_token_is_still_rejected(self, deployment_token: str) -> None:
        downstream, sent = await _run(_ws_scope(AWAITING_ACCOUNT, "not-any-token"))
        assert not downstream.called
        assert sent[0]["reason"] == MSG_TOKEN_INVALID

    async def test_deployment_token_still_works_for_an_unknown_account(self, deployment_token: str) -> None:
        """老路一个字不变：陌生协议端拿部署级令牌照样连得上"""
        downstream, _ = await _run(_ws_scope(AWAITING_ACCOUNT, deployment_token))
        assert downstream.called

    async def test_a_bound_instances_token_does_not_open_the_unknown_account_door(
        self,
        deployment_token: str,
    ) -> None:
        """**放宽只到"还等着绑账号"为止**：已经绑好账号的实例那枚开不了这扇门

        否则一个定制版客户手里那枚运行中的令牌，就成了"给任意 QQ 号登记待认领实例"的通行证。
        """
        downstream, _ = await _run(_ws_scope(AWAITING_ACCOUNT, INSTANCE_TOKEN))
        assert not downstream.called

    async def test_bound_account_is_untouched_by_the_widening(self, deployment_token: str) -> None:
        """已绑账号的那一格半个字都不能松：备选表必须是空的

        断言的是"这条支路对它根本不存在"，而不是"这次恰好没走到"。
        """
        expected = await resolve_expected_token(SCOPED_ACCOUNT)
        assert expected.awaiting == ()
        # 反向对照：换成还没绑账号的账号，同一次调用会给出非空备选表
        assert (await resolve_expected_token(AWAITING_ACCOUNT)).awaiting == (
            (AWAITING_INSTANCE, AWAITING_TOKEN),
        )
        # 而备选表存在也不能让部署级令牌敲开一个已绑账号的实例
        downstream, _ = await _run(_ws_scope(SCOPED_ACCOUNT, deployment_token))
        assert not downstream.called

    async def test_an_awaiting_instances_token_cannot_open_a_bound_account(self, deployment_token: str) -> None:
        """备选表**只属于"账号查不出实例"那一格**，不能顺着 `authorize_handshake` 漏到别处

        漏过去的后果是：任何一个还没绑账号的实例那枚令牌，就成了一把敲开**所有已绑账号实例**的
        万能钥匙——比部署级令牌那把还宽，因为它握在租户手里。
        （变异测试 M31：把备选循环改成"备选表为空时现查一份"，这条必须红。）
        """
        downstream, sent = await _run(_ws_scope(SCOPED_ACCOUNT, AWAITING_TOKEN))
        assert not downstream.called
        assert sent[0]["reason"] == MSG_TOKEN_INVALID
        # 没有自己令牌的那种实例同样不行（它走的是部署级那一档）
        downstream, _ = await _run(_ws_scope(LEGACY_ACCOUNT, AWAITING_TOKEN))
        assert not downstream.called

    async def test_source_reports_the_branch_that_allowed_it(self, deployment_token: str) -> None:
        """日志得说清是哪一支放的行，否则排障时看不出"它为什么连上了" """
        verdict, expected = await gate.authorize_handshake(AWAITING_ACCOUNT, AWAITING_TOKEN)
        assert verdict.allowed is True
        assert expected.source is TokenSource.AWAITING_ACCOUNT
        assert expected.instance_key == AWAITING_INSTANCE

    async def test_empty_own_token_never_enters_the_alternatives(self, deployment_token: str) -> None:
        """没有自己令牌的"等待绑账号"实例不能往备选表里塞一个空串

        空串喂给 `check_token` 是"谁都放行" —— 那会把这一格变成一个不设防的入口。
        """
        await DBAdapterInstance.create(
            adapter_key=ADAPTER_KEY,
            instance_key="inst_await_notoken",
            display_name="等着绑账号、但没有自己的令牌",
            provider="remote_onebot",
            provider_account_id="",
            status="unbound",
            enabled=True,
            metadata_json="{}",
        )
        expected = await resolve_expected_token(AWAITING_ACCOUNT)
        assert "" not in [token for _key, token in expected.awaiting]
        downstream, _ = await _run(_ws_scope(AWAITING_ACCOUNT, "still-wrong"))
        assert not downstream.called

    @pytest.mark.parametrize(
        ("field", "value"),
        [("enabled", False), ("status", "archived"), ("status", "deleted")],
    )
    async def test_non_bindable_instances_are_excluded(
        self,
        field: str,
        value: Any,
        deployment_token: str,
        awaiting_instance: DBAdapterInstance,
    ) -> None:
        """停用/归档/墓碑的实例不该再接受连接 —— 判据与账号回写那一侧同源（`_is_bindable`）"""
        setattr(awaiting_instance, field, value)
        await awaiting_instance.save()
        downstream, _ = await _run(_ws_scope(AWAITING_ACCOUNT, AWAITING_TOKEN))
        assert not downstream.called

    async def test_no_deployment_token_means_no_extra_lookup(self, no_deployment_token: str) -> None:
        """部署级令牌为空时主判据恒放行，备选表在证明上不可达，于是也不该去查它

        （每一个待认领协议端每隔几秒重连一次，白查一次库是白查的。）
        """
        expected = await resolve_expected_token(AWAITING_ACCOUNT)
        assert expected.token == ""
        assert expected.awaiting == ()
        downstream, _ = await _run(_ws_scope(AWAITING_ACCOUNT, None))
        assert downstream.called

    async def test_lookup_failure_degrades_to_deployment_only(
        self,
        deployment_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """查这张表炸了不能把握手一起炸掉：退回"只认部署级令牌"（= 本节修复之前的行为）"""

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("db is down")

        monkeypatch.setattr(container_manager, "list_instances_awaiting_account", boom)
        expected = await resolve_expected_token(AWAITING_ACCOUNT)
        assert expected.token == deployment_token
        assert expected.awaiting == ()

    async def test_criterion_is_shared_with_login_attribution(self, deployment_token: str) -> None:
        """「等着绑账号的实例」只有一条判据：登录归属推导就长在它上面

        两处各写一套的表现是"握手放行了、归属推导却不认这个候选"——
        协议端连上来了，账号却永远绑不到那个实例上。
        """
        awaiting = await container_manager.list_instances_awaiting_account(ADAPTER_KEY)
        candidates = await container_manager._list_login_attribution_candidates(ADAPTER_KEY)  # noqa: SLF001
        assert {row.instance_key for row in candidates} <= {row.instance_key for row in awaiting}
        assert AWAITING_INSTANCE in {row.instance_key for row in candidates}

    def test_login_attribution_is_built_on_the_shared_criterion_in_source(self) -> None:
        """行为断言杀不掉"把同一条查询再抄一份"——那是个等价变异，只能盯源码

        两份实现今天等价，明天就不一定：改了一处忘了另一处，表现是"握手放行了、
        归属推导却不认这个候选"。所以这里要求归属推导**长在**共享判据上。
        （变异测试 M32 正是这条：抄回一份自己的查询，行为用例全绿。）
        """
        source = inspect.getsource(container_manager._list_login_attribution_candidates)  # noqa: SLF001
        assert "list_instances_awaiting_account" in source
        assert "DBAdapterInstance.filter" not in source


@pytest.mark.usefixtures("db")
class TestNewlyCreatedInstanceCanConnectBeforeItsAccountIsKnown:
    """走真的建实例路径，端到端复现"新建实例的第一次接入"

    上面那组用的是手工造的行；这一组证明 `create_instance` 建出来的实例（账号留空、
    令牌自动生成、下发进协议端配置）拿着**下发给它的那枚**能连上。
    """

    async def test_provisioned_token_connects_before_account_binding(self, deployment_token: str) -> None:
        from nekro_agent.schemas.console import InstanceCreateRequest, InstanceProvider

        instance = await instance_manager.create_instance(
            InstanceCreateRequest(
                instance_key="fresh3",
                adapter_key=ADAPTER_KEY,
                provider=InstanceProvider.BUILTIN_NAPCAT,
                # 建实例时不知道 QQ 号 —— 内置 NapCat 要扫码登录成功才回填
                provider_account_id="",
            ),
        )
        assert instance.provider_account_id == ""
        provisioned = container_manager.build_managed_entry(instance).token
        assert provisioned and provisioned != deployment_token

        downstream, _ = await _run(_ws_scope("10778", provisioned))
        assert downstream.called


# ---------------------------------------------------------------------------
# 14. 认领会换掉「这个协议端该出示哪枚令牌」
#
# 未认领实例按定义没有自己的令牌，一直靠部署级令牌（或不带令牌）连着。把它并进一个
# **有自己令牌**的实例，账号一过户，同一条连接下一次重连出示的就是一枚不再被接受的令牌。
# 改造前令牌不校验，所以这一步以前没有代价；现在有了，至少得在日志里可见。
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestAdoptionChangesTheExpectedToken:
    @staticmethod
    async def _adopt(target_key: str, target_provider: str, target_metadata: str) -> List[str]:
        """把一个未认领协议端并进目标实例，返回过程中打出的 WARNING 文本"""
        source = await DBAdapterInstance.create(
            adapter_key=ADAPTER_KEY,
            instance_key="ob70123",
            display_name="未认领实例 70123",
            provider="onebot_v11",
            provider_account_id="70123",
            status="unclaimed",
            enabled=True,
        )
        target = await DBAdapterInstance.create(
            adapter_key=ADAPTER_KEY,
            instance_key=target_key,
            display_name="目标",
            provider=target_provider,
            provider_account_id="",
            status="unbound",
            enabled=True,
            metadata_json=target_metadata,
        )
        warnings: List[str] = []

        class _Recorder:
            def __getattr__(self, _name: str) -> Any:
                return lambda *_a, **_k: None

            def warning(self, message: str, *_a: Any, **_k: Any) -> None:
                warnings.append(message)

        original = instance_manager.logger
        instance_manager.logger = _Recorder()  # type: ignore[assignment]
        try:
            await instance_manager.adopt_unclaimed_instance(source, target)
        finally:
            instance_manager.logger = original
        return warnings

    async def test_remote_target_with_its_own_token_is_flagged(self, deployment_token: str) -> None:
        """表现是"认领成功之后 Bot 就掉线了"，而界面上这一步显示的是成功 —— 日志是唯一现场"""
        warnings = await self._adopt("adopt_remote", "remote_onebot", f'{{"access_token": "{INSTANCE_TOKEN}"}}')
        hits = [line for line in warnings if "该协议端要出示的令牌变了" in line]
        assert len(hits) == 1
        assert "adopt_remote" in hits[0]
        # 令牌本身绝不进日志
        assert INSTANCE_TOKEN not in hits[0]

    async def test_target_without_its_own_token_is_not_flagged(self, deployment_token: str) -> None:
        """目标实例没有自己的令牌 = 期望值一个字没变，不能拿一条假告警去吓运维"""
        warnings = await self._adopt("adopt_plain", "remote_onebot", "{}")
        assert [line for line in warnings if "该协议端要出示的令牌变了" in line] == []

    async def test_builtin_napcat_target_is_not_flagged(self, deployment_token: str) -> None:
        """内置 NapCat 的那份配置由 `resync_network_config` 下发，不需要人收尾"""
        warnings = await self._adopt("adopt_builtin", "builtin_napcat", f'{{"access_token": "{INSTANCE_TOKEN}"}}')
        assert [line for line in warnings if "该协议端要出示的令牌变了" in line] == []


# ---------------------------------------------------------------------------
# 15. 令牌里出现非 latin-1 字符：判据说"支持"，而 HTTP 头层面根本装不下
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db")
class TestNonLatin1DeploymentToken:
    """`check_token` 明确支持非 ASCII 令牌（有用例），但 HTTP 头只能装 latin-1

    于是存在一个只在**放行**分支上炸的组合：部署级令牌是中文、而实例有自己的 ASCII 令牌。
    协议端出示对的实例令牌 → 判定放行 → 背书时 `"Bearer 令牌".encode("latin-1")` 抛
    `UnicodeEncodeError`，一路穿出中间件。表现不是"令牌不对"而是每一次**本该成功**的握手
    都变成一个看不懂的编码错误。
    """

    async def test_allow_path_does_not_explode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "令牌中文")
        downstream, sent = await _run(_ws_scope(SCOPED_ACCOUNT, INSTANCE_TOKEN))
        # 放行本身仍然成立（判据没变），只是这一次不背书
        assert downstream.called
        assert sent == []

    async def test_no_endorsement_when_token_cannot_be_a_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不背书 = 把连接交给下游那道严格校验，失败方向仍然是拒绝

        这里逐字断言"原样透传"：若哪天改成"编不动就塞个空 Bearer"，
        下游拿到的就是一个**合法形态但值为空**的头，判据会变。
        """
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "令牌中文")
        downstream, _ = await _run(_ws_scope(SCOPED_ACCOUNT, INSTANCE_TOKEN))
        assert _header(downstream.calls[0], "authorization") == f"Bearer {INSTANCE_TOKEN}"

    async def test_http_path_does_not_explode_either(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "令牌中文")
        downstream, sent = await _run(_http_scope(SCOPED_ACCOUNT, INSTANCE_TOKEN))
        assert downstream.called
        assert sent == []

    async def test_reject_path_is_unaffected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """拒绝分支本来就不背书，行为一个字不变"""
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "令牌中文")
        downstream, sent = await _run(_ws_scope(SCOPED_ACCOUNT, "nope"))
        assert not downstream.called
        assert sent[0]["reason"] == MSG_TOKEN_INVALID
