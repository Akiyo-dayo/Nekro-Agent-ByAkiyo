"""聊天上传文件的签名取件

`GET /api/common/uploads/{chat_key}/{filename}` 拿不到 `Authorization` 头
（它要被 `<img src>` 直接加载），上游因此把它做成完全公开的。路径穿越是堵住的，
但 `chat_key` 形如 `onebot_v11-{instance_key}-group_{群号}` 是**可枚举**的，
文件名又在消息里出现过 —— 于是"知道两个名字"就等于拿到任意租户的聊天文件。

这里钉死四条：

1. **没有签名就取不到**：即使文件真实存在、路径完全正确，无签名请求也是 404；
2. **签名不可跨频道搬运**：A 频道的令牌拿去取 B 频道的文件必须失败 ——
   这一条正是"跨租户读文件"的直接对应物；
3. **拒绝一律 404**：无签名 / 过期 / 篡改 / 越权 / 文件不存在给出同一个状态码，
   否则 `chat_key` 的可枚举性会退化成"频道存在性可探测"；
4. **令牌只能从带归属校验的接口拿到**：`/common/uploads-access-token` 走
   `assert_channel_accessible`，租户拿别人的 `chat_key` 换令牌是 404。

时间一律注入，不依赖真实时间流逝。
"""

import datetime
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tortoise import Tortoise

from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import common as common_router
from nekro_agent.schemas.errors import AppFileNotFoundError, NotFoundError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.command.output import (
    build_command_output_messages,
    build_command_output_platform_segments,
    materialize_command_output_segment,
    materialize_command_response,
)
from nekro_agent.services.command.schemas import (
    CommandOutputSegment,
    CommandOutputSegmentType,
    CommandResponse,
    CommandResponseStatus,
)
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role
from nekro_agent.tools.upload_access import (
    TOKEN_QUERY_PARAM,
    UPLOAD_ACCESS_WINDOW_SECONDS,
    build_signed_upload_url,
    issue_upload_access_token,
    verify_upload_access_token,
)

ACME_TENANT_ID = 1
OTHER_TENANT_ID = 2

OWN_CHAT_KEY = "onebot_v11-inst1-group_10001"
FOREIGN_CHAT_KEY = "onebot_v11-rival1-group_20001"
IMAGE_NAME = "cat.png"
IMAGE_BYTES = b"\x89PNG\r\n\x1a\nfake-image-body"

NOW = 1_800_000_000


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份（作用域判定只读这三个字段）"""
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def acme_member() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        layout: List[Tuple[str, Optional[int], str]] = [
            ("inst1", ACME_TENANT_ID, OWN_CHAT_KEY),
            ("rival1", OTHER_TENANT_ID, FOREIGN_CHAT_KEY),
        ]
        for instance_key, tenant_id, chat_key in layout:
            await DBAdapterInstance.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
            await DBChatChannel.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
                chat_key=chat_key,
                channel_type="group",
                data="{}",
            )
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把上传根目录指到临时目录，并在两个频道下各放一张图"""
    root = tmp_path / "uploads"
    for chat_key in (OWN_CHAT_KEY, FOREIGN_CHAT_KEY):
        directory = root / chat_key
        directory.mkdir(parents=True, exist_ok=True)
        (directory / IMAGE_NAME).write_bytes(IMAGE_BYTES)
    monkeypatch.setattr(common_router, "UPLOAD_PATH", root)
    return root


@pytest.fixture
def client(upload_root: Path):
    """真实路由 + 真实异常处理器，身份用依赖覆盖注入"""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(common_router.router, prefix="/api")
    test_client = TestClient(app, raise_server_exceptions=False)

    def login_as(user: Optional[DBUser]) -> None:
        if user is None:
            app.dependency_overrides.pop(get_current_active_user, None)
            return
        app.dependency_overrides[get_current_active_user] = lambda: user

    test_client.login_as = login_as  # type: ignore[attr-defined]
    return test_client


def fetch(client: TestClient, chat_key: str, *, token: Optional[str], file_name: str = IMAGE_NAME):
    params = {TOKEN_QUERY_PARAM: token} if token is not None else None
    return client.get(f"/api/common/uploads/{chat_key}/{file_name}", params=params)


class TestTokenPrimitive:
    """签名本身：覆盖什么、不覆盖什么"""

    @pytest.mark.parametrize(
        ("description", "token_factory", "chat_key", "at", "expected"),
        [
            (
                "本频道、窗口内的令牌通过",
                lambda: issue_upload_access_token(OWN_CHAT_KEY, now=NOW),
                OWN_CHAT_KEY,
                NOW,
                True,
            ),
            (
                "换一个频道就对不上（跨租户读文件的直接对应物）",
                lambda: issue_upload_access_token(OWN_CHAT_KEY, now=NOW),
                FOREIGN_CHAT_KEY,
                NOW,
                False,
            ),
            (
                "过了两个时间窗必定失效",
                lambda: issue_upload_access_token(OWN_CHAT_KEY, now=NOW),
                OWN_CHAT_KEY,
                NOW + 2 * UPLOAD_ACCESS_WINDOW_SECONDS,
                False,
            ),
            (
                "空串不是签名",
                lambda: "",
                OWN_CHAT_KEY,
                NOW,
                False,
            ),
            (
                "只有版本段与过期时间、没有摘要的令牌不通过",
                lambda: f"v1.{NOW + 3600}.",
                OWN_CHAT_KEY,
                NOW,
                False,
            ),
            (
                "把过期时间改大（摘要不变）不通过",
                lambda: _tamper_expiry(issue_upload_access_token(OWN_CHAT_KEY, now=NOW), NOW + 900),
                OWN_CHAT_KEY,
                NOW,
                False,
            ),
            (
                "翻转摘要末位不通过",
                lambda: _tamper_digest(issue_upload_access_token(OWN_CHAT_KEY, now=NOW)),
                OWN_CHAT_KEY,
                NOW,
                False,
            ),
            (
                "超长串直接拒绝，不进解析",
                lambda: "v1.9999999999." + "a" * 512,
                OWN_CHAT_KEY,
                NOW,
                False,
            ),
            (
                "剩余有效期超过两个时间窗的令牌不通过（签发侧只会签出 [1,2) 个窗）",
                lambda: issue_upload_access_token(OWN_CHAT_KEY, now=NOW),
                OWN_CHAT_KEY,
                NOW - 10 * UPLOAD_ACCESS_WINDOW_SECONDS,
                False,
            ),
            # 下面三条各自只差一步就会抛异常而不是返回 False，所以摘要段都取足 64 位，
            # 好让长度这一关先放行、把判据真正压在字符集上
            (
                "过期时间段是 `²`：str.isdigit() 为真但 int() 不收，必须判假而不是抛 ValueError",
                lambda: f"v1.².{'a' * 64}",
                OWN_CHAT_KEY,
                NOW,
                False,
            ),
            (
                "过期时间段是圈号 `①`：同上",
                lambda: f"v1.①.{'a' * 64}",
                OWN_CHAT_KEY,
                NOW,
                False,
            ),
            (
                "摘要段含非 ASCII：hmac.compare_digest 会抛 TypeError，必须在那之前判假",
                lambda: f"v1.{NOW + 900}.{'é' * 64}",
                OWN_CHAT_KEY,
                NOW,
                False,
            ),
        ],
    )
    def test_verification_table(
        self,
        description: str,
        token_factory,
        chat_key: str,
        at: int,
        expected: bool,
    ) -> None:
        assert verify_upload_access_token(token_factory(), chat_key, now=at) is expected, description

    def test_token_is_stable_within_a_window_and_rolls_after_it(self) -> None:
        """窗口内逐字节相同 —— URL 不变，浏览器缓存才留得住；跨窗口必须换新

        这不是性能优化：URL 一变，同一张图会被重新下载一遍。
        """
        window_start = NOW - NOW % UPLOAD_ACCESS_WINDOW_SECONDS
        first = issue_upload_access_token(OWN_CHAT_KEY, now=window_start)
        same_window = issue_upload_access_token(OWN_CHAT_KEY, now=window_start + UPLOAD_ACCESS_WINDOW_SECONDS - 1)
        next_window = issue_upload_access_token(OWN_CHAT_KEY, now=window_start + UPLOAD_ACCESS_WINDOW_SECONDS)

        assert first == same_window
        assert first != next_window

    def test_token_issued_at_window_end_is_still_usable(self) -> None:
        """窗口末尾签出来的令牌不能一签发就过期

        `_bucket_expiry` 加两个窗而不是一个，就是为了这条。
        """
        window_end = NOW - NOW % UPLOAD_ACCESS_WINDOW_SECONDS + UPLOAD_ACCESS_WINDOW_SECONDS - 1
        token = issue_upload_access_token(OWN_CHAT_KEY, now=window_end)

        assert verify_upload_access_token(token, OWN_CHAT_KEY, now=window_end) is True
        assert verify_upload_access_token(token, OWN_CHAT_KEY, now=window_end + UPLOAD_ACCESS_WINDOW_SECONDS) is True

    def test_signature_is_bound_to_the_sanitized_directory(self) -> None:
        """签名覆盖的是落盘目录名，与写入侧同一个规整函数

        `chat_key` 里的非法字符在落盘时被换成 `_`，签名若按原串算就会与目录对不上。
        """
        raw = "wechat:room:42"
        sanitized = "wechat_room_42"
        token = issue_upload_access_token(raw, now=NOW)

        assert verify_upload_access_token(token, sanitized, now=NOW) is True


class TestFetchEndpoint:
    """取件路由：签名是唯一的门"""

    def test_unsigned_request_is_rejected_even_though_the_file_exists(self, client: TestClient) -> None:
        response = fetch(client, OWN_CHAT_KEY, token=None)

        assert response.status_code == 404
        assert IMAGE_BYTES not in response.content

    def test_valid_signature_serves_the_file(self, client: TestClient) -> None:
        response = fetch(client, OWN_CHAT_KEY, token=issue_upload_access_token(OWN_CHAT_KEY))

        assert response.status_code == 200
        assert response.content == IMAGE_BYTES
        assert response.headers["content-type"].startswith("image/png")

    def test_response_is_not_shared_cacheable(self, client: TestClient) -> None:
        """取件响应是某个租户的聊天文件，不该落进共享代理缓存"""
        response = fetch(client, OWN_CHAT_KEY, token=issue_upload_access_token(OWN_CHAT_KEY))

        assert response.headers["cache-control"].startswith("private")

    def test_token_of_another_channel_cannot_read_this_one(self, client: TestClient) -> None:
        """这条就是原漏洞：知道别人的 chat_key 与文件名"""
        response = fetch(client, FOREIGN_CHAT_KEY, token=issue_upload_access_token(OWN_CHAT_KEY))

        assert response.status_code == 404
        assert IMAGE_BYTES not in response.content

    def test_expired_token_is_rejected(self, client: TestClient) -> None:
        """端点读的是真实时钟，所以令牌要签在**真实过去**

        `NOW` 是 2027 年的固定戳，用 `NOW - n` 签出来的令牌相对真实时钟仍在未来，
        撞上的是"剩余有效期超上限"那一条而不是过期那一条 —— 那条另有用例覆盖。
        从纪元附近签，才落在过期分支上。
        """
        stale = issue_upload_access_token(OWN_CHAT_KEY, now=0)
        response = fetch(client, OWN_CHAT_KEY, token=stale)

        assert response.status_code == 404

    def test_tampered_token_is_rejected(self, client: TestClient) -> None:
        response = fetch(client, OWN_CHAT_KEY, token=_tamper_digest(issue_upload_access_token(OWN_CHAT_KEY)))

        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("description", "expiry_part", "digest_part"),
        [
            ("过期时间段是 `²`", "²", "a" * 64),
            ("过期时间段是圈号 `①`", "①", "a" * 64),
            ("摘要段是重音字母", None, "é" * 64),
            ("摘要段是中文", None, "中" * 64),
            ("摘要段是全角数字", None, "０" * 64),
        ],
    )
    def test_malformed_signature_is_a_404_not_a_500(
        self,
        client: TestClient,
        description: str,
        expiry_part: Optional[str],
        digest_part: str,
    ) -> None:
        """畸形签名必须落在同一个 404 上

        这条路由**不带登录态**，`sig` 完全由请求方给：任何能穿过验签函数的畸形输入
        都是一条匿名可触发的 500 —— 既破坏"拒绝一律同形"这条判据，
        又让任何人都能往日志里灌栈。踩过的两块：`str.isdigit()` 对 `²`/`①` 为真而
        `int()` 不收（ValueError），`hmac.compare_digest()` 见到非 ASCII 直接 TypeError。

        过期时间取**真实时间**的下一个窗口内：端点读的是真实时钟，取一个遥远的未来值
        会先被"剩余有效期超上限"挡掉，那样断言的就不是字符集这一关了。
        """
        expiry = expiry_part if expiry_part is not None else str(int(time.time()) + UPLOAD_ACCESS_WINDOW_SECONDS)
        response = fetch(client, OWN_CHAT_KEY, token=f"v1.{expiry}.{digest_part}")

        assert response.status_code == 404, f"{description}: {response.status_code} {response.text[:200]}"
        assert IMAGE_BYTES not in response.content

    def test_missing_file_with_valid_token_is_the_same_404(self, client: TestClient) -> None:
        """有签名但文件不存在，与无签名给出同一个状态码"""
        signed = fetch(client, OWN_CHAT_KEY, token=issue_upload_access_token(OWN_CHAT_KEY), file_name="nope.png")
        unsigned = fetch(client, OWN_CHAT_KEY, token=None)

        assert signed.status_code == unsigned.status_code == 404

    @pytest.mark.parametrize("hostile_name", ["../../secret.txt", "..\\..\\secret.txt", "..", "."])
    async def test_traversal_in_the_file_name_stays_inside_the_upload_root(
        self,
        client: TestClient,
        upload_root: Path,
        hostile_name: str,
    ) -> None:
        """签名给了这个频道的目录，不等于给了目录之外

        直接调端点函数而不是发 HTTP 请求：Starlette 会先把 `..` 规范化掉，
        经 HTTP 打过去只会撞上"路由没匹配上"的 404，那样断言的是路由器不是这个守卫。
        """
        (upload_root.parent / "secret.txt").write_bytes(b"platform-secret")

        with pytest.raises(AppFileNotFoundError):
            await common_router.get_upload_file(
                chat_key=OWN_CHAT_KEY,
                filename=hostile_name,
                sig=issue_upload_access_token(OWN_CHAT_KEY),
            )


class TestAccessTokenEndpoint:
    """令牌签发点：整条链路上唯一的授权判据"""

    async def test_owner_gets_a_usable_token(self, db, client: TestClient) -> None:
        client.login_as(platform_owner())  # type: ignore[attr-defined]

        response = client.get("/api/common/uploads-access-token", params={"chat_key": OWN_CHAT_KEY})

        assert response.status_code == 200
        body = response.json()
        assert body["query_param"] == TOKEN_QUERY_PARAM
        assert verify_upload_access_token(body["token"], OWN_CHAT_KEY) is True
        assert fetch(client, OWN_CHAT_KEY, token=body["token"]).status_code == 200

    async def test_tenant_admin_gets_a_token_for_its_own_channel(self, db, client: TestClient) -> None:
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.get("/api/common/uploads-access-token", params={"chat_key": OWN_CHAT_KEY})

        assert response.status_code == 200

    async def test_tenant_admin_cannot_get_a_token_for_another_tenant(self, db, client: TestClient) -> None:
        """越权是 404 而不是 403：403 会把别人的频道变成可枚举的"""
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.get("/api/common/uploads-access-token", params={"chat_key": FOREIGN_CHAT_KEY})

        assert response.status_code == NotFoundError.http_status

    async def test_read_only_member_cannot_get_a_token(self, db, client: TestClient) -> None:
        """门槛与消息历史一致取 Role.Admin，只读成员本来也看不到消息历史"""
        client.login_as(acme_member())  # type: ignore[attr-defined]

        response = client.get("/api/common/uploads-access-token", params={"chat_key": OWN_CHAT_KEY})

        assert response.status_code == 403


class TestServerSideIssuers:
    """服务端主动把 URL 交给前端的签发点"""

    async def test_command_output_web_url_is_signed_and_immediately_usable(
        self,
        client: TestClient,
        upload_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """命令输出的富媒体附件：物化后给出的 `web_url` 必须自带签名

        它不经过前端的令牌接口，前端拿到就直接塞进 `<img src>`。
        """
        source = tmp_path / "chart.png"
        source.write_bytes(IMAGE_BYTES)

        async def _fake_copy(file_path: str, file_name: str, from_chat_key: str):
            target = upload_root / from_chat_key / file_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(Path(file_path).read_bytes())
            return str(target), file_name

        monkeypatch.setattr("nekro_agent.services.command.output.copy_to_upload_dir", _fake_copy)

        segment = await materialize_command_output_segment(
            OWN_CHAT_KEY,
            CommandOutputSegment(
                type=CommandOutputSegmentType.IMAGE,
                file_path=str(source),
                file_name="chart.png",
            ),
        )

        assert segment.web_url is not None
        assert f"{TOKEN_QUERY_PARAM}=" in segment.web_url
        assert client.get(segment.web_url).status_code == 200

    async def test_signature_never_reaches_the_model_or_the_platform(
        self,
        db,
        upload_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """签名只走 WebUI，不进模型上下文、也不进发往聊天平台的消息段

        `web_url` 与 `file_path` 是同一个段上的两个字段，很容易在后续改动里被互相替换。
        真换了的话：签名串会占提示词 token、可能被模型原样复述出去，
        而聊天平台侧拿到一条 15 分钟后就失效的链接。
        """
        source = tmp_path / "chart.png"
        source.write_bytes(IMAGE_BYTES)

        async def _fake_copy(file_path: str, file_name: str, from_chat_key: str):
            target = upload_root / from_chat_key / file_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(Path(file_path).read_bytes())
            return str(target), file_name

        monkeypatch.setattr("nekro_agent.services.command.output.copy_to_upload_dir", _fake_copy)

        response = await materialize_command_response(
            OWN_CHAT_KEY,
            CommandResponse(
                status=CommandResponseStatus.SUCCESS,
                message="图表已生成",
                output_segments=[
                    CommandOutputSegment(
                        type=CommandOutputSegmentType.IMAGE,
                        file_path=str(source),
                        file_name="chart.png",
                    ),
                ],
            ),
        )
        assert response.output_segments is not None
        assert f"{TOKEN_QUERY_PARAM}=" in (response.output_segments[0].web_url or "")

        agent_messages = await build_command_output_messages(response, OWN_CHAT_KEY)
        platform_segments = await build_command_output_platform_segments(response, OWN_CHAT_KEY)

        model_facing = [segment.content for segment in agent_messages]
        platform_facing = [segment.file_path or segment.content or "" for segment in platform_segments]

        assert all(f"{TOKEN_QUERY_PARAM}=" not in value for value in model_facing), model_facing
        assert all(f"{TOKEN_QUERY_PARAM}=" not in value for value in platform_facing), platform_facing
        # 模型侧拿到的仍然是宿主机路径，与改动前逐字相同
        assert str(upload_root / OWN_CHAT_KEY / "chart.png") in model_facing

    def test_signed_url_matches_what_the_frontend_composes(self) -> None:
        """服务端签的 URL 与前端用令牌拼出来的必须逐字相同

        不然同一张图会因为两条路径给出两个 URL 而被下载两遍。
        前端的拼法在 `frontend/src/services/api/common.ts:buildSignedUploadUrl`。
        """
        token = issue_upload_access_token(OWN_CHAT_KEY, now=NOW)
        composed = f"/api/common/uploads/{OWN_CHAT_KEY}/{IMAGE_NAME}?{TOKEN_QUERY_PARAM}={token}"

        assert build_signed_upload_url(OWN_CHAT_KEY, IMAGE_NAME, now=NOW) == composed


def _tamper_expiry(token: str, new_expiry: int) -> str:
    version, _expiry, digest = token.split(".")
    return f"{version}.{new_expiry}.{digest}"


def _tamper_digest(token: str) -> str:
    version, expiry, digest = token.split(".")
    flipped = "0" if digest[-1] != "0" else "1"
    return f"{version}.{expiry}.{digest[:-1]}{flipped}"
