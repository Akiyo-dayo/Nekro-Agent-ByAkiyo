"""附带 qbob（QQ 官方机器人 → OneBot v11）的托管：服务账号、接入、运维、权限与部署文件

设计见 `docs/qbob-integration.md`。qbob 管理 API 用一个**按源码核实过的假实现**代替
（`httpx.MockTransport`，路径、字段、会话 cookie、401 / 409 语义与 qbob 0.1.5 一致），不需要 Docker。

| 契约 | 把它改回去 | 该红的用例 |
|---|---|---|
| 服务账号由 admin 初始口令引导，口令加密落盘，admin 口令不动 | 直接用 admin、明文落盘 | `TestServiceAccount` |
| 401 重登一次、重试一次，不循环 | 去掉重试 / 无限重试 | `TestSession` |
| 先建 bot（无端点）→ 绑账号 → 再接线 | 建 bot 时就带端点 | `test_bot_is_created_without_endpoint_and_wired_after_binding` |
| 凭据错（qbob 只在 last_error 里说）就回滚刚建的 bot | 只看 self_id / 去掉回滚 | `test_a_wrong_secret_is_rolled_back_with_the_platform_reason` |
| 锁内读库里的当前行 | 用路由传进来的旧快照 | `test_a_stale_snapshot_cannot_attach_a_second_bot` |
| 端点端口是 NA 实际监听的端口 | 用 `EXPOSE_PORT` | `test_the_endpoint_uses_the_port_na_listens_on_not_the_published_one` |
| 登录失败后退避 | 每次轮询都登录 | `test_a_failed_login_backs_off_instead_of_hammering_qbob` |
| 只接管 / 删除带本实例标记的 bot | 不查 `notes` | `TestOwnershipMarker` |
| AppSecret / 令牌 / 口令不出现在响应与日志里 | 把凭据带进模型或日志 | `TestCredentialsNeverLeak` |
| 扫码任务只能被发起它的实例轮询 | 不校验任务归属 | `test_a_task_cannot_be_polled_from_another_instance` |
| 接入归平台方，运维给租户管理员 + 归属校验 | 降权 / 去掉归属校验 | `TestPermissionTiers` |
| 部署文件：管理面只绑回环、数据目录同路径挂载、环境变量接线 | 改坏 compose / 入口脚本 | `TestDeploymentFiles` |
"""

from __future__ import annotations

import asyncio
import datetime
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest
import yaml
from fastapi import HTTPException
from loguru import logger as loguru_logger
from tortoise import Tortoise

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console import qbob as qbob_router
from nekro_agent.schemas.console import InstanceCreateRequest, InstanceProvider, InstanceUpdateRequest
from nekro_agent.schemas.console_qbob import (
    QbobAttachRequest,
    QbobBotInfo,
    QbobBotOptionsRequest,
    QbobOperationError,
)
from nekro_agent.schemas.errors import ConflictError, DeploymentCapabilityError, NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.console import instance_manager, qbob_manager
from nekro_agent.services.console.container_manager import resolve_access_token
from nekro_agent.services.user.role import Role
from nekro_agent.tools.credential_cipher import decrypt_credential

REPO_ROOT = Path(__file__).parent.parent
DOCKER_DIR = REPO_ROOT / "docker"
QBOB_DIR = DOCKER_DIR / "qbob"

ADMIN_URL = "http://qbob.test:17810"
ADMIN_INITIAL_PASSWORD = "PW-admin-initial-must-never-leak"
SECRET = "SECRET-appsecret-do-not-leak"
# 位数刻意避开 9~11 位（公开版泄漏扫描器按 QQ 号形态命中），模糊匹配那条用例会再拼一位
APPID = "1020304"
OTHER_APPID = "1020305"
SELF_ID_BASE = 100000000000000

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2


# ---------------------------------------------------------------------------
# 假 qbob
# ---------------------------------------------------------------------------


BAD_SECRET = "SECRET-bad-do-not-leak"
# qbob `core/bot.py:_on_credential_error` 写进 last_error 的形态
CREDENTIAL_ERROR = "100016 invalid appid or secret（凭据错误 ×1）"


class FakeQbob:
    """qbob 管理 API 的最小实现（只实现 NA 用到的端点），按 qbob 9c36d57 源码的行为写：

    - `self_id` 只由 AppID 决定，bot 一启动就有，**与凭据对错无关**；
    - 凭据错不会让建 bot 失败，只在 `last_error` 里留下「凭据错误」，几轮之后被自动停用；
    - `GET /api/bots` 里停用的 bot `running=False`、`self_id=0`；`GET /api/status` 只列在运行的；
    - PUT 改了凭据 / 启用状态就重载：停用的 bot 重载后仍不运行。
    """

    def __init__(self) -> None:
        self.users: Dict[str, Tuple[str, str]] = {"admin": (ADMIN_INITIAL_PASSWORD, "admin")}
        self.sessions: Dict[str, str] = {}
        self.bots: Dict[str, Dict[str, Any]] = {}
        self.qr_results: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Tuple[str, str, Any]] = []
        self.login_attempts: List[str] = []
        # 每次接线（PUT onebot_endpoints）时回调，用来在那一刻检查 NA 的状态
        self.on_wire = None
        self.always_401: set = set()
        self.unreachable = False

    @staticmethod
    def self_id_of(appid: str) -> int:
        return SELF_ID_BASE + int(appid)

    def _restart(self, bot: Dict[str, Any]) -> None:
        bot["running"] = bool(bot["enabled"])
        bot["last_error"] = CREDENTIAL_ERROR if "bad" in bot["secret"] else ""

    def add_bot(self, appid: str, notes: str, **extra: Any) -> Dict[str, Any]:
        bot = {
            "appid": appid,
            "secret": "SECRET-existing-do-not-leak",
            "name": f"bot{appid}",
            "notes": notes,
            "onebot_endpoints": [],
            "enabled": True,
            "markdown_enabled": True,
            "report_self_message": True,
            "group_list_mode": "white",
        }
        bot.update(extra)
        self._restart(bot)
        self.bots[appid] = bot
        return bot

    def auto_disable(self, appid: str) -> None:
        """凭据连错几轮之后 qbob 自己做的事：置 enabled=0 并停掉"""
        self.bots[appid].update(enabled=False, running=False)

    def _public(self, bot: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in bot.items() if k != "last_error"}
        out["self_id"] = self.self_id_of(bot["appid"]) if bot["running"] else 0
        return out

    def _session_user(self, request: httpx.Request) -> Optional[str]:
        cookie = request.headers.get("cookie", "")
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == qbob_manager.SESSION_COOKIE:
                return self.sessions.get(value)
        return None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))

        if path == "/api/login":
            self.login_attempts.append(body["username"])
            user = self.users.get(body["username"])
            if user is None or user[0] != body["password"]:
                return httpx.Response(401, json={"detail": "用户名或密码错误或已锁定"})
            token = secrets.token_hex(8)
            self.sessions[token] = body["username"]
            return httpx.Response(
                200,
                json={"username": body["username"], "role": user[1]},
                headers={"set-cookie": f"{qbob_manager.SESSION_COOKIE}={token}; HttpOnly; Path=/"},
            )

        username = self._session_user(request)
        if username is None or path in self.always_401:
            return httpx.Response(401, json={"detail": "未登录"})

        if path == "/api/logout":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/users" and method == "POST":
            if body["username"] in self.users:
                return httpx.Response(409, json={"detail": "用户已存在"})
            self.users[body["username"]] = (body["password"], body["role"])
            return httpx.Response(200, json={"ok": True})
        if path.startswith("/api/users/") and method == "PUT":
            name = path.rsplit("/", 1)[1]
            self.users[name] = (body["password"], body.get("role") or self.users[name][1])
            self.sessions = {t: u for t, u in self.sessions.items() if u != name}
            return httpx.Response(200, json={"ok": True})
        if path == "/api/status":
            wanted = request.url.params.get("appids", "")
            snaps = [
                {
                    "appid": bot["appid"],
                    "self_id": self.self_id_of(bot["appid"]),
                    "qq_connected": not bot["last_error"],
                    "last_error": bot["last_error"],
                    "links": [{"url": ep["url"], "connected": True} for ep in bot["onebot_endpoints"]],
                }
                for bot in self.bots.values()
                # qbob 的 /api/status 只列在运行的 bot
                if bot["running"] and (not wanted or bot["appid"] in wanted.split(","))
            ]
            return httpx.Response(200, json={"version": "0.1.5", "bots": snaps})
        if path == "/api/bots" and method == "GET":
            q = request.url.params.get("q", "")
            # 与 qbob 一样是 LIKE 模糊匹配
            return httpx.Response(200, json=[self._public(b) for b in self.bots.values() if q in b["appid"] or q in b["notes"]])
        if path == "/api/bots" and method == "POST":
            if body["appid"] in self.bots:
                return httpx.Response(409, json={"detail": "appid 已存在"})
            extra = {k: body[k] for k in ("name", "onebot_endpoints", "group_list_mode", "report_self_message", "enabled") if k in body}
            self.add_bot(body["appid"], body.get("notes", ""), secret=body["secret"], **extra)
            return httpx.Response(200, json={"ok": True, "appid": body["appid"]})
        if path.startswith("/api/bots/"):
            parts = path.split("/")
            appid = parts[3]
            bot = self.bots.get(appid)
            if bot is None:
                return httpx.Response(404, json={"detail": "bot 不存在"})
            if method == "PUT":
                if "onebot_endpoints" in body and self.on_wire is not None:
                    await self.on_wire(appid, body["onebot_endpoints"])
                bot.update(body)
                # qbob 的 PUT 一律 reload_bot：停用的 bot 不会被拉起
                self._restart(bot)
                return httpx.Response(200, json={"ok": True, "changed": True})
            if method == "DELETE":
                del self.bots[appid]
                return httpx.Response(200, json={"ok": True})
            if method == "POST" and parts[-1] == "reload":
                return httpx.Response(200, json={"ok": True})
        if path == "/api/qrconnect/start":
            task_id = f"task{len(self.qr_results) + 1}"
            self.qr_results[task_id] = {"status": "pending"}
            return httpx.Response(200, json={"task_id": task_id, "connect_url": f"https://example.com/c?t={task_id}", "qr_svg": "<svg/>"})
        if path.startswith("/api/qrconnect/poll/"):
            task_id = path.rsplit("/", 1)[1]
            result = self.qr_results.get(task_id)
            if result is None:
                return httpx.Response(404, json={"detail": "任务不存在或已清理"})
            return httpx.Response(200, json=result)
        return httpx.Response(404, json={"detail": f"unexpected {method} {path}"})

    def calls_to(self, method: str, path_prefix: str) -> List[Tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] == method and c[1].startswith(path_prefix)]


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


def make_user(*, user_id: int, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    user = DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )
    user.id = user_id
    return user


def platform_owner() -> DBUser:
    return make_user(user_id=1, tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def acme_admin() -> DBUser:
    return make_user(user_id=2, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def acme_member() -> DBUser:
    return make_user(user_id=3, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "data" / "nekro_agent"
    (root / "qbob_data").mkdir(parents=True)
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(root))
    monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
    monkeypatch.setattr(OsEnv, "SELF_HOSTNAME", "na-test-host")
    monkeypatch.setattr(OsEnv, "EXPOSE_PORT", 8021)
    monkeypatch.setattr(OsEnv, "QBOB_DATA_DIR", "")
    return root


def write_initial_password(data_dir: Path, password: str = ADMIN_INITIAL_PASSWORD) -> None:
    (data_dir / "qbob_data" / qbob_manager.INITIAL_PASSWORD_FILE).write_text(f"admin / {password}\n", encoding="utf-8")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> FakeQbob:
    fake = FakeQbob()
    write_initial_password(data_dir)
    monkeypatch.setattr(OsEnv, "QBOB_ADMIN_URL", ADMIN_URL)
    monkeypatch.setattr(qbob_manager, "_client", qbob_manager.QbobClient(ADMIN_URL, transport=httpx.MockTransport(fake.handle)))
    monkeypatch.setattr(qbob_manager, "SELF_ID_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(qbob_manager, "SELF_ID_POLL_ATTEMPTS", 2)
    qbob_manager._qr_tasks.clear()
    qbob_manager._instance_locks.clear()
    return fake


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def make_qbob_instance(instance_key: str, tenant_id: Optional[int] = None) -> DBAdapterInstance:
    instance = await instance_manager.create_instance(
        InstanceCreateRequest(instance_key=instance_key, adapter_key="onebot_v11", provider=InstanceProvider.BUILTIN_QBOB),
    )
    if tenant_id is not None:
        await DBAdapterInstance.filter(id=instance.id).update(tenant_id=tenant_id)
    return await DBAdapterInstance.get(id=instance.id)


async def reload(instance: DBAdapterInstance) -> DBAdapterInstance:
    return await DBAdapterInstance.get(id=instance.id)


@pytest.fixture
def log_lines():
    lines: List[str] = []
    sink = loguru_logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        yield lines
    finally:
        loguru_logger.remove(sink)


# ---------------------------------------------------------------------------
# 服务账号与会话
# ---------------------------------------------------------------------------


class TestServiceAccount:
    async def test_status_reports_the_commit_the_image_was_built_from(
        self, fake: FakeQbob, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        ref = "0123456789abcdef0123456789abcdef01234567"
        monkeypatch.setattr(OsEnv, "QBOB_REF", ref)

        status = await qbob_manager.service_status()

        assert status.qbob_ref == ref
    async def test_first_use_creates_a_dedicated_admin_and_stores_it_encrypted(self, fake: FakeQbob, data_dir: Path) -> None:
        status = await qbob_manager.service_status()

        assert status.configured and status.reachable and status.service_account_ready
        password, role = fake.users[qbob_manager.SERVICE_USERNAME]
        assert role == "admin"
        # admin 的口令没被动过，留给运维
        assert fake.users["admin"][0] == ADMIN_INITIAL_PASSWORD
        stored = json.loads(qbob_manager.service_account_path().read_text(encoding="utf-8"))
        assert stored["username"] == qbob_manager.SERVICE_USERNAME
        assert password not in qbob_manager.service_account_path().read_text(encoding="utf-8")
        assert decrypt_credential(stored["password"]) == password

    async def test_a_fresh_client_reuses_the_stored_account_without_touching_admin(self, fake: FakeQbob) -> None:
        await qbob_manager.service_status()
        fake.login_attempts.clear()
        qbob_manager._client = qbob_manager.QbobClient(ADMIN_URL, transport=httpx.MockTransport(fake.handle))

        await qbob_manager.service_status()

        assert fake.login_attempts == [qbob_manager.SERVICE_USERNAME]

    async def test_an_existing_service_user_gets_its_password_reset(self, fake: FakeQbob) -> None:
        fake.users[qbob_manager.SERVICE_USERNAME] = ("stale-password-must-never-leak", "admin")

        status = await qbob_manager.service_status()

        assert status.service_account_ready
        assert fake.calls_to("PUT", f"/api/users/{qbob_manager.SERVICE_USERNAME}")
        assert fake.users[qbob_manager.SERVICE_USERNAME][0] != "stale-password-must-never-leak"

    async def test_without_initial_password_it_asks_for_a_manual_account(self, fake: FakeQbob, data_dir: Path) -> None:
        (data_dir / "qbob_data" / qbob_manager.INITIAL_PASSWORD_FILE).unlink()

        status = await qbob_manager.service_status()

        assert status.reachable and not status.service_account_ready
        assert "手工" in status.message
        assert fake.login_attempts == []

        fake.users["ops"] = ("PW-ops-must-never-leak", "admin")
        await qbob_manager.set_service_credential("ops", "PW-ops-must-never-leak")
        assert (await qbob_manager.service_status()).service_account_ready

    async def test_a_wrong_manual_account_is_refused_and_not_stored(self, fake: FakeQbob, data_dir: Path) -> None:
        (data_dir / "qbob_data" / qbob_manager.INITIAL_PASSWORD_FILE).unlink()

        with pytest.raises(QbobOperationError):
            await qbob_manager.set_service_credential("ops", "PW-wrong-must-never-leak")
        assert not qbob_manager.service_account_path().exists()

    async def test_a_manual_account_must_be_admin(self, fake: FakeQbob, data_dir: Path) -> None:
        """删 bot 要 admin：advanced 账号存下来，到解绑那一步才 403"""
        (data_dir / "qbob_data" / qbob_manager.INITIAL_PASSWORD_FILE).unlink()
        fake.users["ops"] = ("PW-ops-must-never-leak", "advanced")

        with pytest.raises(QbobOperationError, match="admin"):
            await qbob_manager.set_service_credential("ops", "PW-ops-must-never-leak")
        assert not qbob_manager.service_account_path().exists()

    async def test_state_lives_outside_the_install_files_directory(self, fake: FakeQbob, data_dir: Path) -> None:
        """`{DATA_DIR}/qbob/` 是 install.sh 拷进来的 compose 文件与镜像构建上下文"""
        await qbob_manager.service_status()

        assert qbob_manager.service_account_path().parent == data_dir / qbob_manager.STATE_DIR_NAME
        assert not (data_dir / "qbob").exists()

    async def test_a_failed_login_backs_off_instead_of_hammering_qbob(self, fake: FakeQbob, data_dir: Path) -> None:
        """qbob 按 IP 锁定失败登录；面板每 10 秒轮询一次，不退避就会把 NA 自己锁在外面"""
        await qbob_manager.service_status()
        (data_dir / "qbob_data" / qbob_manager.INITIAL_PASSWORD_FILE).unlink()
        fake.users[qbob_manager.SERVICE_USERNAME] = ("changed-by-ops-must-never-leak", "admin")
        fake.sessions.clear()
        fake.login_attempts.clear()

        for _ in range(5):
            status = await qbob_manager.service_status()
            assert not status.service_account_ready

        assert fake.login_attempts == [qbob_manager.SERVICE_USERNAME]
        # 平台方手工提供账号不受退避限制，成功后恢复
        fake.users["ops"] = ("PW-ops-must-never-leak", "admin")
        await qbob_manager.set_service_credential("ops", "PW-ops-must-never-leak")
        assert (await qbob_manager.service_status()).service_account_ready

    async def test_the_ownership_marker_survives_a_compose_switch(
        self, fake: FakeQbob, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        """标准 compose 与 x-napcat compose 的 NEKRO_INSTANCE_NAME 不同；标记不能跟着变"""
        instance = await make_qbob_instance("qb1")
        before = qbob_manager.managed_note(instance)

        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "nekro_agent")

        assert qbob_manager.managed_note(instance) == before

    async def test_unconfigured_deployment(self, monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
        monkeypatch.setattr(OsEnv, "QBOB_ADMIN_URL", "")

        status = await qbob_manager.service_status()

        assert not status.configured
        with pytest.raises(DeploymentCapabilityError):
            qbob_manager.get_client()


class TestSession:
    async def test_a_restarted_qbob_costs_exactly_one_relogin(self, fake: FakeQbob) -> None:
        await qbob_manager.service_status()
        fake.login_attempts.clear()
        fake.sessions.clear()  # qbob 重启：会话在进程内存里，全部失效

        status = await qbob_manager.service_status()

        assert status.service_account_ready
        assert fake.login_attempts == [qbob_manager.SERVICE_USERNAME]

    async def test_a_persistent_401_does_not_loop(self, fake: FakeQbob) -> None:
        await qbob_manager.service_status()
        fake.login_attempts.clear()
        fake.always_401.add("/api/status")

        with pytest.raises(qbob_manager.QbobApiError):
            await qbob_manager.get_client().request("GET", "/api/status")

        assert fake.login_attempts == [qbob_manager.SERVICE_USERNAME]

    async def test_a_deleted_service_user_is_rebuilt_once(self, fake: FakeQbob) -> None:
        await qbob_manager.service_status()
        del fake.users[qbob_manager.SERVICE_USERNAME]
        fake.sessions.clear()
        fake.login_attempts.clear()

        assert (await qbob_manager.service_status()).service_account_ready

        assert fake.login_attempts == [qbob_manager.SERVICE_USERNAME, "admin", qbob_manager.SERVICE_USERNAME]


# ---------------------------------------------------------------------------
# 接入
# ---------------------------------------------------------------------------


class TestAttach:
    async def test_bot_is_created_without_endpoint_and_wired_after_binding(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        seen_at_wire: Dict[str, Any] = {}

        async def on_wire(appid: str, endpoints: List[Dict[str, str]]) -> None:
            row = await DBAdapterInstance.get(id=instance.id)
            seen_at_wire.update(account=row.provider_account_id, endpoints=endpoints)

        fake.on_wire = on_wire

        info = await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        created = fake.calls_to("POST", "/api/bots")[0][2]
        assert created["onebot_endpoints"] == []
        assert created["notes"] == qbob_manager.managed_note(instance)
        assert created["group_list_mode"] == "black"
        self_id = str(fake.self_id_of(APPID))
        # 接线那一刻账号已经绑在实例上：bot 第一次握手就命中本实例
        assert seen_at_wire["account"] == self_id
        expected = {
            "url": "ws://na-test-host:8021/onebot/v11/ws",
            "access_token": resolve_access_token(await reload(instance)),
        }
        assert seen_at_wire["endpoints"] == [expected]
        assert expected["access_token"]
        row = await reload(instance)
        assert row.provider_account_id == self_id
        assert qbob_manager.attached_appid(row) == APPID
        assert info.attached and info.found and info.endpoint_ok and info.onebot_connected
        assert info.self_id == self_id

    async def test_a_wrong_secret_is_rolled_back_with_the_platform_reason(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """真实 qbob 上凭据错也读得到 self_id；能用的信号只有 last_error"""
        instance = await make_qbob_instance("qb1")

        with pytest.raises(QbobOperationError) as exc:
            await qbob_manager.attach_with_credentials(instance, APPID, BAD_SECRET)

        assert "凭据错误" in str(exc.value)
        assert APPID not in fake.bots
        row = await reload(instance)
        assert row.provider_account_id == ""
        assert qbob_manager.attached_appid(row) == ""
        assert not [c for c in fake.calls_to("PUT", f"/api/bots/{APPID}") if "onebot_endpoints" in c[2]]

    async def test_an_auto_disabled_bot_of_ours_is_revived_by_new_credentials(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """凭据无效被 qbob 自动停用后，qbob 的重载不会拉起它：重新提交时要连 enabled 一起写"""
        instance = await make_qbob_instance("qb1")
        fake.add_bot(APPID, notes=qbob_manager.managed_note(instance), secret=BAD_SECRET)
        fake.auto_disable(APPID)

        info = await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        put = fake.calls_to("PUT", f"/api/bots/{APPID}")[0][2]
        assert put == {"enabled": True, "secret": SECRET}
        assert info.attached and info.running and info.enabled
        assert fake.bots[APPID]["secret"] == SECRET

    async def test_resuming_a_still_broken_bot_of_ours_reports_why_and_keeps_it(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """不是本次建的 bot 不回滚删除（可能是之前就接好的），只报原因"""
        instance = await make_qbob_instance("qb1")
        fake.add_bot(APPID, notes=qbob_manager.managed_note(instance), secret=BAD_SECRET)

        with pytest.raises(qbob_manager.QbobError, match="凭据错误"):
            await qbob_manager._attach(instance, APPID, None)

        assert APPID in fake.bots
        assert (await reload(instance)).provider_account_id == ""

    async def test_the_endpoint_uses_the_port_na_listens_on_not_the_published_one(
        self, fake: FakeQbob, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        """compose 把宿主的 NEKRO_EXPOSE_PORT 映射到容器内固定的 8021；容器网络里只能连后者"""
        from nekro_agent.services.workspace import cc_credential

        monkeypatch.setattr(OsEnv, "EXPOSE_PORT", 8022)
        monkeypatch.setattr(cc_credential, "relay_listen_port", lambda: 8021)
        instance = await make_qbob_instance("qb1")

        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        assert fake.bots[APPID]["onebot_endpoints"][0]["url"] == "ws://na-test-host:8021/onebot/v11/ws"

    async def test_a_stale_snapshot_cannot_attach_a_second_bot(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """路由在进锁前读出的实例对象可能已经旧了：锁内必须按库里的当前行判断"""
        instance = await make_qbob_instance("qb1")
        stale = await reload(instance)
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        with pytest.raises(ConflictError):
            await qbob_manager.attach_with_credentials(stale, OTHER_APPID, SECRET)

        assert OTHER_APPID not in fake.bots
        assert (await reload(instance)).provider_account_id == str(fake.self_id_of(APPID))

    async def test_concurrent_attaches_leave_exactly_one_bot(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        first, second = await reload(instance), await reload(instance)

        results = await asyncio.gather(
            qbob_manager.attach_with_credentials(first, APPID, SECRET),
            qbob_manager.attach_with_credentials(second, OTHER_APPID, SECRET),
            return_exceptions=True,
        )

        assert sum(isinstance(r, ConflictError) for r in results) == 1
        assert len(fake.bots) == 1
        winner = next(iter(fake.bots))
        assert qbob_manager.attached_appid(await reload(instance)) == winner

    async def test_a_unicode_digit_appid_is_refused(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        with pytest.raises(Exception, match="AppID"):
            await qbob_manager.attach_with_credentials(instance, "１２３", SECRET)
        assert fake.calls_to("POST", "/api/bots") == []

    async def test_fuzzy_match_in_qbob_does_not_pick_the_wrong_bot(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """qbob 的 q 是 LIKE；`1020304` 的查询会把 `10203041` 一并带回来"""
        fake.add_bot(APPID + "1", notes="someone else")
        instance = await make_qbob_instance("qb1")

        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        assert APPID in fake.bots
        assert fake.bots[APPID + "1"]["onebot_endpoints"] == []

    async def test_an_instance_can_only_hold_one_bot(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        with pytest.raises(ConflictError):
            await qbob_manager.attach_with_credentials(await reload(instance), OTHER_APPID, SECRET)

    async def test_resubmitting_credentials_updates_the_secret(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        await qbob_manager.attach_with_credentials(await reload(instance), APPID, "SECRET-rotated-do-not-leak")

        assert fake.bots[APPID]["secret"] == "SECRET-rotated-do-not-leak"
        assert len(fake.calls_to("POST", "/api/bots")) == 1

    async def test_non_qbob_instances_are_refused(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        napcat = await instance_manager.create_instance(
            InstanceCreateRequest(instance_key="nc1", adapter_key="onebot_v11", provider=InstanceProvider.BUILTIN_NAPCAT),
        )
        with pytest.raises(Exception, match="不是内置 qbob 实例"):
            await qbob_manager.attach_with_credentials(napcat, APPID, SECRET)
        assert fake.calls_to("POST", "/api/bots") == []

    async def test_generic_account_editing_is_refused_once_attached(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """概览页的通用改绑 / 解绑绕过 qbob 侧的解绑，bot 重连就会落成未认领实例"""
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        row = await reload(instance)

        with pytest.raises(ConflictError, match="解绑 qbob"):
            await instance_manager.update_instance(row, InstanceUpdateRequest(provider_account_id=""))
        assert (await reload(instance)).provider_account_id == str(fake.self_id_of(APPID))

        # 其他字段照常可改
        updated = await instance_manager.update_instance(row, InstanceUpdateRequest(display_name="改名了"))
        assert updated.display_name == "改名了"

    async def test_an_account_mismatch_blocks_operations_but_status_still_opens(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        # 模拟修复前被通用入口清空的账号：直接改库，绕过新的 update_instance 守卫
        await DBAdapterInstance.filter(id=instance.id).update(provider_account_id="")
        row = await reload(instance)

        with pytest.raises(ConflictError, match="不一致"):
            await qbob_manager.reload_bot(row)
        with pytest.raises(ConflictError, match="不一致"):
            await qbob_manager.update_bot_options(row, QbobBotOptionsRequest(enabled=False))
        with pytest.raises(ConflictError, match="不一致"):
            await qbob_manager.rewire(row)
        info = await qbob_manager.get_bot_info(row)
        assert "不一致" in info.message and info.attached



class TestQrConnect:
    async def test_scan_then_attach_without_returning_the_secret(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        started = await qbob_manager.start_qrconnect(instance)
        assert (await qbob_manager.poll_qrconnect(instance, started.task_id)).status == "pending"

        fake.qr_results[started.task_id] = {"status": "ready", "appid": APPID, "secret": SECRET, "user_openid": "o"}
        result = await qbob_manager.poll_qrconnect(instance, started.task_id)

        assert result.status == "attached"
        assert result.bot is not None and result.bot.appid == APPID
        assert SECRET not in result.model_dump_json()
        assert fake.bots[APPID]["secret"] == SECRET

    async def test_a_task_cannot_be_polled_from_another_instance(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        mine = await make_qbob_instance("qb1")
        other = await make_qbob_instance("qb2")
        started = await qbob_manager.start_qrconnect(mine)
        fake.qr_results[started.task_id] = {"status": "ready", "appid": APPID, "secret": SECRET}

        result = await qbob_manager.poll_qrconnect(other, started.task_id)

        assert result.status == "expired"
        assert fake.calls_to("GET", "/api/qrconnect/poll/") == []
        assert APPID not in fake.bots

    async def test_exists_resumes_our_own_bot_and_refuses_a_foreign_one(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        fake.add_bot(APPID, notes=qbob_manager.managed_note(instance))
        fake.add_bot(OTHER_APPID, notes="hand-made in qbob console")

        first = await qbob_manager.start_qrconnect(instance)
        fake.qr_results[first.task_id] = {"status": "exists", "appid": APPID}
        assert (await qbob_manager.poll_qrconnect(instance, first.task_id)).status == "attached"
        assert fake.calls_to("POST", "/api/bots") == []

        await qbob_manager.detach(await reload(instance))
        fake.add_bot(APPID, notes=qbob_manager.managed_note(instance))
        second = await qbob_manager.start_qrconnect(instance)
        fake.qr_results[second.task_id] = {"status": "exists", "appid": OTHER_APPID}
        result = await qbob_manager.poll_qrconnect(await reload(instance), second.task_id)
        assert result.status == "conflict"
        assert fake.bots[OTHER_APPID]["onebot_endpoints"] == []

    async def test_a_failure_after_the_scan_stays_visible(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """qbob 在 ready 时就清掉了它那边的任务：接入失败的原因必须留在本地，下一次轮询不能变成「已过期」"""
        instance = await make_qbob_instance("qb1")
        started = await qbob_manager.start_qrconnect(instance)
        fake.qr_results[started.task_id] = {"status": "ready", "appid": APPID, "secret": BAD_SECRET}

        first = await qbob_manager.poll_qrconnect(instance, started.task_id)
        del fake.qr_results[started.task_id]  # qbob 侧已不存在
        polls_before = len(fake.calls_to("GET", "/api/qrconnect/poll/"))
        second = await qbob_manager.poll_qrconnect(instance, started.task_id)

        assert first.status == second.status == "failed"
        assert "凭据错误" in first.message and second.message == first.message
        assert len(fake.calls_to("GET", "/api/qrconnect/poll/")) == polls_before
        assert BAD_SECRET not in first.model_dump_json()

    async def test_exists_revives_an_auto_disabled_bot_without_a_secret(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """扫码回 exists 时不带 Secret；bot 若已被自动停用，仍要单独发 enabled=True 才能拉起来"""
        instance = await make_qbob_instance("qb1")
        fake.add_bot(APPID, notes=qbob_manager.managed_note(instance))
        fake.auto_disable(APPID)

        started = await qbob_manager.start_qrconnect(instance)
        fake.qr_results[started.task_id] = {"status": "exists", "appid": APPID}
        result = await qbob_manager.poll_qrconnect(instance, started.task_id)

        assert result.status == "attached"
        first_put = fake.calls_to("PUT", f"/api/bots/{APPID}")[0][2]
        assert first_put == {"enabled": True}
        assert fake.bots[APPID]["enabled"] is True and fake.bots[APPID]["running"] is True

    async def test_a_conflict_is_kept_for_the_next_poll(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        fake.add_bot(OTHER_APPID, notes="hand-made in qbob console")
        started = await qbob_manager.start_qrconnect(await reload(instance))
        fake.qr_results[started.task_id] = {"status": "exists", "appid": OTHER_APPID}

        first = await qbob_manager.poll_qrconnect(await reload(instance), started.task_id)
        del fake.qr_results[started.task_id]  # qbob 侧在 ready / exists 时已删任务
        polls_before = len(fake.calls_to("GET", "/api/qrconnect/poll/"))
        second = await qbob_manager.poll_qrconnect(await reload(instance), started.task_id)

        assert first.status == second.status == "conflict"
        assert first.message and second.message == first.message
        assert len(fake.calls_to("GET", "/api/qrconnect/poll/")) == polls_before
        # 终态被下一次轮询取走后任务才清掉
        assert (await qbob_manager.poll_qrconnect(await reload(instance), started.task_id)).status == "expired"

    async def test_polling_stays_pending_while_attaching_and_keeps_the_result(
        self, fake: FakeQbob, db, monkeypatch: pytest.MonkeyPatch  # noqa: ARG002
    ) -> None:
        """qbob 在 ready 时已删任务：接入期间再问只会 404，不能把「接入中」报成「已过期」"""
        instance = await make_qbob_instance("qb1")
        started = await qbob_manager.start_qrconnect(instance)
        fake.qr_results[started.task_id] = {"status": "ready", "appid": APPID, "secret": SECRET}
        attaching_at_attach: List[bool] = []

        async def fake_attach(*_args: Any, **_kwargs: Any) -> QbobBotInfo:
            # 真正接入之前标记必须已经置上
            attaching_at_attach.append(qbob_manager._qr_tasks[started.task_id].attaching)
            return QbobBotInfo(configured=True, attached=True, found=True, appid=APPID)

        monkeypatch.setattr(qbob_manager, "_attach", fake_attach)
        first = await qbob_manager.poll_qrconnect(instance, started.task_id)

        assert first.status == "attached"
        assert attaching_at_attach == [True]
        # 终态留到下一次轮询：第一次取到 attached，第二次再取一次后才删
        assert started.task_id in qbob_manager._qr_tasks
        assert (await qbob_manager.poll_qrconnect(instance, started.task_id)).status == "attached"
        assert started.task_id not in qbob_manager._qr_tasks
        assert (await qbob_manager.poll_qrconnect(instance, started.task_id)).status == "expired"

        # 接入中：不再向 qbob 发轮询请求，直接回 pending
        second = await qbob_manager.start_qrconnect(instance)
        fake.qr_results[second.task_id] = {"status": "ready", "appid": APPID, "secret": SECRET}
        qbob_manager._qr_tasks[second.task_id].attaching = True
        polls_before = len(fake.calls_to("GET", "/api/qrconnect/poll/"))
        during = await qbob_manager.poll_qrconnect(instance, second.task_id)
        assert during.status == "pending"
        assert len(fake.calls_to("GET", "/api/qrconnect/poll/")) == polls_before



class TestDeploymentId:
    async def test_a_lost_id_with_attached_bots_is_refused_not_regenerated(self, fake: FakeQbob, db, data_dir: Path) -> None:  # noqa: ARG002
        """文件丢了就换新 ID 的话，qbob 里所有 bot 的归属标记都会对不上"""
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        path = data_dir / qbob_manager.STATE_DIR_NAME / qbob_manager.DEPLOYMENT_ID_FILE
        before = path.read_text(encoding="utf-8")
        path.unlink()

        with pytest.raises(qbob_manager.QbobSetupRequiredError, match="deployment_id"):
            await qbob_manager.ensure_deployment_id()

        info = await qbob_manager.get_bot_info(instance)
        assert "deployment_id" in info.message
        assert not path.exists()
        assert before

    async def test_a_lost_id_without_attachments_is_generated_as_before(self, fake: FakeQbob, db, data_dir: Path) -> None:  # noqa: ARG002
        await make_qbob_instance("qb1")  # 已建实例但还没接入任何 bot
        path = data_dir / qbob_manager.STATE_DIR_NAME / qbob_manager.DEPLOYMENT_ID_FILE
        assert not path.exists()

        generated = await qbob_manager.ensure_deployment_id()

        assert generated and path.read_text(encoding="utf-8") == generated


# ---------------------------------------------------------------------------
# 运维与归属
# ---------------------------------------------------------------------------


class TestOwnershipMarker:
    async def test_a_foreign_bot_is_never_taken_over(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        fake.add_bot(APPID, notes="hand-made in qbob console")
        instance = await make_qbob_instance("qb1")

        with pytest.raises(ConflictError):
            await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        assert fake.calls_to("PUT", "/api/bots/") == []
        assert fake.calls_to("DELETE", "/api/bots/") == []

    async def test_detach_deletes_our_bot_and_unbinds_the_account(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        await qbob_manager.detach(await reload(instance))

        assert APPID not in fake.bots
        row = await reload(instance)
        assert row.provider_account_id == ""
        assert qbob_manager.attached_appid(row) == ""

    async def test_detach_refuses_a_bot_whose_marker_changed(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """只清 NA 侧会让实例回到「等待绑账号」，那个仍带本实例令牌的 bot 重连时就被登记成未认领实例"""
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        fake.bots[APPID]["notes"] = "taken over by ops"

        with pytest.raises(ConflictError):
            await qbob_manager.detach(await reload(instance))

        assert APPID in fake.bots
        row = await reload(instance)
        assert qbob_manager.attached_appid(row) == APPID
        assert row.provider_account_id == str(fake.self_id_of(APPID))

    async def test_detach_with_a_stale_snapshot_still_detaches(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        stale = await reload(instance)
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        await qbob_manager.detach(stale)

        assert APPID not in fake.bots
        row = await reload(instance)
        assert row.provider_account_id == "" and qbob_manager.attached_appid(row) == ""

    async def test_detach_of_a_bot_already_gone_from_qbob_clears_na_side(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        del fake.bots[APPID]

        await qbob_manager.detach(await reload(instance))

        row = await reload(instance)
        assert row.provider_account_id == "" and qbob_manager.attached_appid(row) == ""

    async def test_status_reports_a_tampered_endpoint_and_rewire_fixes_it(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        fake.bots[APPID]["onebot_endpoints"] = [{"url": "ws://example.com/ws", "access_token": ""}]

        info = await qbob_manager.get_bot_info(await reload(instance))
        assert not info.endpoint_ok and not info.onebot_connected and "重新接线" in info.message

        fixed = await qbob_manager.rewire(await reload(instance))
        assert fixed.endpoint_ok and fixed.onebot_connected

    async def test_options_only_send_the_given_fields(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("qb1")
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        fake.calls.clear()

        info = await qbob_manager.update_bot_options(await reload(instance), QbobBotOptionsRequest(markdown_enabled=False))

        assert fake.calls_to("PUT", f"/api/bots/{APPID}")[0][2] == {"markdown_enabled": False}
        assert not info.markdown_enabled and info.enabled


class TestCredentialsNeverLeak:
    async def test_responses_and_logs_carry_no_secret_token_or_password(
        self, fake: FakeQbob, db, log_lines: List[str]  # noqa: ARG002
    ) -> None:
        instance = await make_qbob_instance("qb1")
        attached = await qbob_router.attach(
            instance_key="qb1", body=QbobAttachRequest(appid=APPID, secret=SECRET), _current_user=platform_owner()
        )
        status = await qbob_router.get_bot_info(instance_key="qb1", _current_user=platform_owner())
        service = await qbob_router.get_service_status(_current_user=platform_owner())

        token = resolve_access_token(await reload(instance))
        service_password = fake.users[qbob_manager.SERVICE_USERNAME][0]
        dumped = attached.model_dump_json() + status.model_dump_json() + service.model_dump_json()
        logged = "\n".join(log_lines)
        # 非空对照：确实抓到了接入日志
        assert any("已接入 qbob bot" in line for line in log_lines)
        for value in (SECRET, token, service_password, ADMIN_INITIAL_PASSWORD):
            assert value not in dumped
            assert value not in logged


# ---------------------------------------------------------------------------
# 权限分档（行为侧；分档登记钉在 test_route_permission_contract.py）
# ---------------------------------------------------------------------------


class TestPermissionTiers:
    async def test_tenant_admin_operates_its_own_bot(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("acme1", tenant_id=ACME_TENANT_ID)
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)

        info = await qbob_router.get_bot_info(instance_key="acme1", _current_user=acme_admin())
        assert info.appid == APPID
        await qbob_router.reload_bot(instance_key="acme1", _current_user=acme_admin())
        await qbob_router.update_bot_options(
            instance_key="acme1", body=QbobBotOptionsRequest(enabled=False), _current_user=acme_admin()
        )
        assert fake.bots[APPID]["enabled"] is False

    async def test_tenant_admin_cannot_reach_a_foreign_instance(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        await make_qbob_instance("rival1", tenant_id=RIVAL_TENANT_ID)

        with pytest.raises(NotFoundError):
            await qbob_router.get_bot_info(instance_key="rival1", _current_user=acme_admin())
        with pytest.raises(NotFoundError):
            await qbob_router.reload_bot(instance_key="rival1", _current_user=acme_admin())
        with pytest.raises(NotFoundError):
            await qbob_router.update_bot_options(
                instance_key="rival1", body=QbobBotOptionsRequest(enabled=False), _current_user=acme_admin()
            )
        assert fake.calls == []

    async def test_a_changed_marker_blocks_every_write(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """qbob 里的备注被改过：租户与平台方都不能再动这个 bot，且一次写都不该发出"""
        instance = await make_qbob_instance("acme1", tenant_id=ACME_TENANT_ID)
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        fake.bots[APPID]["notes"] = "taken over by ops"
        writes_before = [c for c in fake.calls if c[0] in {"POST", "PUT", "DELETE"} and c[1].startswith("/api/bots")]

        with pytest.raises(QbobOperationError):
            await qbob_router.reload_bot(instance_key="acme1", _current_user=acme_admin())
        with pytest.raises(QbobOperationError):
            await qbob_router.update_bot_options(
                instance_key="acme1", body=QbobBotOptionsRequest(enabled=False), _current_user=acme_admin()
            )
        with pytest.raises(ConflictError):
            await qbob_router.rewire(instance_key="acme1", _current_user=platform_owner())

        writes_after = [c for c in fake.calls if c[0] in {"POST", "PUT", "DELETE"} and c[1].startswith("/api/bots")]
        assert writes_after == writes_before

    async def test_tenant_sees_no_internal_service_detail(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """qbob 连不上时，平台方看得到原因，租户管理员只拿到通用说明（不带内部地址与平台方设置入口）"""
        instance = await make_qbob_instance("acme1", tenant_id=ACME_TENANT_ID)
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        fake.unreachable = True

        tenant_view = await qbob_router.get_bot_info(instance_key="acme1", _current_user=acme_admin())
        platform_view = await qbob_router.get_bot_info(instance_key="acme1", _current_user=platform_owner())

        assert tenant_view.message == qbob_manager.TENANT_SERVICE_MESSAGE
        assert platform_view.message != tenant_view.message and "ConnectError" in platform_view.message
        for message in (tenant_view.message, platform_view.message):
            assert ADMIN_URL not in message and "qbob.test" not in message
        with pytest.raises(QbobOperationError) as exc:
            await qbob_router.reload_bot(instance_key="acme1", _current_user=acme_admin())
        assert qbob_manager.TENANT_SERVICE_MESSAGE in str(exc.value)

    async def test_tenant_errors_do_not_point_at_platform_operations(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        instance = await make_qbob_instance("acme1", tenant_id=ACME_TENANT_ID)
        await qbob_manager.attach_with_credentials(instance, APPID, SECRET)
        del fake.bots[APPID]  # 平台方在 qbob 管理台删掉了这个 bot

        with pytest.raises(QbobOperationError) as tenant_exc:
            await qbob_router.reload_bot(instance_key="acme1", _current_user=acme_admin())
        with pytest.raises(ValidationError, match="找不到 bot") as platform_exc:
            await qbob_router.reload_bot(instance_key="acme1", _current_user=platform_owner())

        assert qbob_manager.TENANT_OPERATION_MESSAGE in str(tenant_exc.value)
        assert "找不到 bot" not in str(tenant_exc.value)
        assert "找不到 bot" in str(platform_exc.value)

    async def test_same_named_tombstone_in_another_adapter_grants_nothing(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        """instance_key 只在适配器内唯一：另一适配器下的同名墓碑不能被当成 qbob 实例"""
        await DBAdapterInstance.create(
            adapter_key="minecraft",
            instance_key="dup",
            display_name="dup",
            provider=InstanceProvider.NATIVE.value,
            status=TOMBSTONE_STATUS,
            tenant_id=ACME_TENANT_ID,
            metadata_json="{}",
        )
        platform_instance = await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key="dup",
            display_name="dup",
            provider=InstanceProvider.BUILTIN_QBOB.value,
            status="unbound",
            provider_account_id=str(SELF_ID_BASE + int(APPID)),
            metadata_json=json.dumps({qbob_manager.METADATA_APPID: APPID}),
        )
        fake.add_bot(APPID, notes=qbob_manager.managed_note(platform_instance))

        with pytest.raises(NotFoundError):
            await qbob_router.get_bot_info(instance_key="dup", _current_user=acme_admin())
        with pytest.raises(NotFoundError):
            await qbob_router.reload_bot(instance_key="dup", _current_user=acme_admin())
        with pytest.raises(NotFoundError):
            await qbob_router.update_bot_options(
                instance_key="dup", body=QbobBotOptionsRequest(enabled=False), _current_user=acme_admin()
            )
        assert fake.calls == []

    async def test_an_older_same_named_row_in_another_adapter_does_not_hide_the_qbob_instance(
        self,
        fake: FakeQbob,
        db,  # noqa: ARG002
    ) -> None:
        """存量的跨适配器同名行排在前面时，按标识取第一行会落到它上面：租户管不了自己的 qbob 实例"""
        await DBAdapterInstance.create(
            adapter_key="minecraft",
            instance_key="dup",
            display_name="dup",
            provider=InstanceProvider.NATIVE.value,
            status="online",
            tenant_id=RIVAL_TENANT_ID,
            metadata_json="{}",
        )
        own = await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key="dup",
            display_name="dup",
            provider=InstanceProvider.BUILTIN_QBOB.value,
            status="online",
            tenant_id=ACME_TENANT_ID,
            provider_account_id=str(SELF_ID_BASE + int(APPID)),
            metadata_json=json.dumps({qbob_manager.METADATA_APPID: APPID}),
        )
        fake.add_bot(APPID, notes=qbob_manager.managed_note(own))

        for user in (acme_admin(), platform_owner()):
            info = await qbob_router.get_bot_info(instance_key="dup", _current_user=user)
            assert info.attached and info.appid == APPID
        await qbob_router.reload_bot(instance_key="dup", _current_user=acme_admin())
        assert any("reload" in str(call) for call in fake.calls)

    async def test_attach_and_detach_stay_platform_only(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        await make_qbob_instance("acme1", tenant_id=ACME_TENANT_ID)

        for call in (
            qbob_router.attach(instance_key="acme1", body=QbobAttachRequest(appid=APPID, secret=SECRET), _current_user=acme_admin()),
            qbob_router.start_qrconnect(instance_key="acme1", _current_user=acme_admin()),
            qbob_router.detach(instance_key="acme1", _current_user=acme_admin()),
            qbob_router.get_service_status(_current_user=acme_admin()),
        ):
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == 403
        assert fake.calls == []

    async def test_read_only_member_is_refused(self, fake: FakeQbob, db) -> None:  # noqa: ARG002
        await make_qbob_instance("acme1", tenant_id=ACME_TENANT_ID)

        for call in (
            qbob_router.get_bot_info(instance_key="acme1", _current_user=acme_member()),
            qbob_router.reload_bot(instance_key="acme1", _current_user=acme_member()),
            qbob_router.update_bot_options(
                instance_key="acme1", body=QbobBotOptionsRequest(enabled=False), _current_user=acme_member()
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == 403
        assert fake.calls == []


def test_provider_maps_and_needs_an_account() -> None:
    assert instance_manager.map_provider("builtin_qbob") is InstanceProvider.BUILTIN_QBOB
    assert instance_manager.account_binding_required("builtin_qbob")


# ---------------------------------------------------------------------------
# 部署文件
# ---------------------------------------------------------------------------


def _load_entry_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qbob_entry_under_test", QBOB_DIR / "qbob_entry.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDeploymentFiles:
    def _compose(self) -> Dict[str, Any]:
        return yaml.safe_load((QBOB_DIR / "docker-compose.qbob.yml").read_text(encoding="utf-8"))

    def test_override_adds_the_admin_url_and_the_built_commit_to_na(self) -> None:
        na = self._compose()["services"]["nekro_agent"]
        assert set(na) == {"environment"}
        env = na["environment"]
        assert [item.partition("=")[0] for item in env] == ["NEKRO_QBOB_ADMIN_URL", "NEKRO_QBOB_REF"]
        assert env[0] == "NEKRO_QBOB_ADMIN_URL=http://${INSTANCE_NAME:-}nekro_qbob:17810"
        # 许可说明里的「所用提交」必须是实际构建镜像的那一个默认值
        assert env[1].startswith("NEKRO_QBOB_REF=${QBOB_REF:-")
        assert len(env[1].split(":-", 1)[1].rstrip("}")) == 40

    def test_env_var_name_is_the_one_os_env_reads(self) -> None:
        """compose 传的名字与 `OsEnv` 读的名字是同一根线（monkeypatch OsEnv 的用例覆盖不到这一点）"""
        source = (REPO_ROOT / "nekro_agent" / "core" / "os_env.py").read_text(encoding="utf-8")
        assert 'OsEnvTypes.Str("QBOB_ADMIN_URL"' in source
        assert "NEKRO_QBOB_ADMIN_URL=" in (QBOB_DIR / "docker-compose.qbob.yml").read_text(encoding="utf-8")

    def test_qbob_service_is_loopback_only_and_shares_the_data_path(self) -> None:
        service = self._compose()["services"]["nekro_qbob"]
        assert service["ports"] and all(str(port).startswith("127.0.0.1:") for port in service["ports"])
        # 镜像名带上游提交：缺失才构建；`pull_policy: build` 会让每次 up 都重新拉源码与依赖
        assert service["image"].startswith("nekro-qbob:${QBOB_REF:-")
        assert service.get("pull_policy") != "build"
        assert service["build"]["context"] == "./qbob"
        assert "nekro_network" in service["networks"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        mounts = [volume.split(":") for volume in service["volumes"]]
        assert all(parts[0] == parts[1] for parts in mounts)
        data_env = [item for item in service["environment"] if item.startswith("QBOB_DATA_DIR=")]
        assert data_env == [f"QBOB_DATA_DIR={mounts[0][0]}"]

    def test_entry_config_points_everything_into_the_data_dir(self, tmp_path: Path) -> None:
        entry = _load_entry_module()
        data = str(tmp_path / "qbob_data")

        config = entry.build_config(data)

        assert config["admin_on_public"] is False
        assert config["update_check_hours"] == 0
        assert config["http_api_host"] == "127.0.0.1"
        # qbob 把首启口令文件写在 db_path 的上级目录：必须正好是数据目录，NA 才找得到
        assert str(Path(config["db_path"]).parent) == data
        for key in ("db_path", "media_dir", "log_file"):
            assert Path(config[key]).is_absolute() and config[key].startswith(data)

    def test_entry_never_overwrites_an_existing_config(self, tmp_path: Path) -> None:
        entry = _load_entry_module()
        path = tmp_path / "config.json"
        path.write_text('{"admin_port": 1}', encoding="utf-8")

        assert entry.ensure_config(path, str(tmp_path)) is False
        assert json.loads(path.read_text(encoding="utf-8")) == {"admin_port": 1}
        fresh = tmp_path / "new" / "config.json"
        assert entry.ensure_config(fresh, str(tmp_path)) is True
        assert json.loads(fresh.read_text(encoding="utf-8"))["admin_on_public"] is False

    def test_public_base_url_set_later_still_takes_effect(self, tmp_path: Path) -> None:
        entry = _load_entry_module()
        path = tmp_path / "config.json"
        path.write_text('{"public_base_url": "", "admin_port": 1}', encoding="utf-8")

        assert entry.sync_public_base_url(path, "") is False
        assert entry.sync_public_base_url(path, "https://bot.example.com") is True
        assert json.loads(path.read_text(encoding="utf-8")) == {"public_base_url": "https://bot.example.com", "admin_port": 1}
        assert entry.sync_public_base_url(path, "https://bot.example.com") is False

    def test_entry_locks_the_data_dir_down_before_handing_it_to_qbob(self) -> None:
        """qbob 的库里是明文 AppSecret：数据目录 0700、umask 077；root 写的配置文件单独交给 qbob 用户"""
        source = (QBOB_DIR / "qbob_entry.py").read_text(encoding="utf-8")
        main = source[source.index("def main()") :]
        assert "os.umask(0o077)" in main
        assert "os.chmod(root, 0o700)" in main
        assert "os.chown(config_path, QBOB_USER_UID, QBOB_USER_UID, follow_symlinks=False)" in main
        assert main.index("os.chown(config_path") < main.index("drop_privileges(")

    def test_entry_refuses_a_symlinked_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """数据目录对 qbob 用户可写：config.json 被换成符号链接后，root 的写入 / chown 会作用到链接目标"""
        entry = _load_entry_module()
        data = tmp_path / "data"
        data.mkdir()
        target = tmp_path / "outside.json"
        target.write_text("{}", encoding="utf-8")
        try:
            (data / "config.json").symlink_to(target)
        except OSError:
            pytest.skip("当前环境不允许创建符号链接")
        monkeypatch.setenv("QBOB_DATA_DIR", str(data))

        with pytest.raises(SystemExit) as exc:
            entry.main()

        assert exc.value.code not in (0, None)
        assert target.read_text(encoding="utf-8") == "{}"

    def test_entry_does_not_import_qbob(self) -> None:
        source = (QBOB_DIR / "qbob_entry.py").read_text(encoding="utf-8")
        assert "import qqbot_onebot" not in source and "from qqbot_onebot" not in source

    def test_dockerfile_pins_a_full_commit_and_drops_git(self) -> None:
        dockerfile = (QBOB_DIR / "Dockerfile").read_text(encoding="utf-8")
        compose = (QBOB_DIR / "docker-compose.qbob.yml").read_text(encoding="utf-8")
        example = (DOCKER_DIR / ".env.example").read_text(encoding="utf-8")
        default_ref = dockerfile.split("ARG QBOB_REF=", 1)[1].split()[0]
        assert len(default_ref) == 40
        # 三处默认提交一致
        assert f"QBOB_REF:-{default_ref}" in compose
        assert f"QBOB_REF={default_ref}" in example
        assert "rm -rf .git" in dockerfile
        assert "--require-hashes" in dockerfile

    def test_dockerfile_can_compile_sdist_only_dependencies(self) -> None:
        """锁文件里的 pilk 在 PyPI 上没有 Linux 轮子，必须现场编译；slim 镜像不带编译器"""
        dockerfile = (QBOB_DIR / "Dockerfile").read_text(encoding="utf-8")
        build_stage, _, runtime_stage = dockerfile.partition("\nFROM python:3.12-slim\n")
        assert runtime_stage, "应有独立的运行阶段"
        assert " AS build" in build_stage and "gcc" in build_stage and "libc6-dev" in build_stage
        assert "--prefix=/install" in build_stage
        assert "COPY --from=build /install /usr/local" in runtime_stage
        # 编译器不进运行镜像
        assert "gcc" not in runtime_stage


def _bash() -> str:
    bash = shutil.which("bash")
    git = shutil.which("git")
    if bash is None and git is not None:
        candidate = Path(git).resolve().parent.parent / "usr" / "bin" / "bash.exe"
        bash = str(candidate) if candidate.is_file() else None
    if bash is None:
        pytest.skip("没有 bash")
    return bash


def _install_function(name: str) -> str:
    source = (DOCKER_DIR / "install.sh").read_text(encoding="utf-8")
    start = source.index(f"{name}() {{")
    return source[start : source.index("\n}\n", start) + 3]


class TestInstallScript:
    def _run(self, tmp_path: Path, body: str) -> subprocess.CompletedProcess:
        prelude = (
            "QBOB_FILES=(Dockerfile qbob_entry.py docker-compose.qbob.yml)\n"
            "QBOB_COMPOSE_FILE=qbob/docker-compose.qbob.yml\n"
            + _install_function("obtain_qbob_files")
            + _install_function("env_has_qbob")
            + _install_function("append_qbob_compose_file")
        )
        script = tmp_path / "run.sh"
        script.write_text(prelude + body, encoding="utf-8", newline="\n")
        # 与 test_install_compose_source 同一做法：bash 所在目录里就有 mkdir / cp / grep / cmp
        env = {"PATH": str(Path(_bash()).parent)}
        return subprocess.run([_bash(), script.name], cwd=tmp_path, capture_output=True, text=True, env=env)

    def test_files_are_copied_from_the_script_dir(self, tmp_path: Path) -> None:
        script_dir = tmp_path / "repo-docker"
        shutil.copytree(QBOB_DIR, script_dir / "qbob")
        data = tmp_path / "data"
        data.mkdir()
        body = (
            f'NEKRO_SCRIPT_DIR="{script_dir.as_posix()}"; NEKRO_START_DIR=""\n'
            f'cd "{data.as_posix()}" && obtain_qbob_files && echo OK\n'
        )
        result = self._run(tmp_path, body)
        assert "OK" in result.stdout, result.stderr
        for name in ("Dockerfile", "qbob_entry.py", "docker-compose.qbob.yml"):
            assert (data / "qbob" / name).read_bytes() == (QBOB_DIR / name).read_bytes()

    def test_missing_files_fail_instead_of_downloading(self, tmp_path: Path) -> None:
        body = 'NEKRO_SCRIPT_DIR="/nonexistent"; NEKRO_START_DIR=""\nobtain_qbob_files || echo MISSING\n'
        assert "MISSING" in self._run(tmp_path, body).stdout

    def test_compose_file_is_remembered_only_after_a_successful_up(self) -> None:
        """先写 COMPOSE_FILE 再 up 的话，qbob 构建失败后每次手敲 up 都会再构建、再失败"""
        source = (DOCKER_DIR / "install.sh").read_text(encoding="utf-8")
        up = source.index('$COMPOSE_FILE_ARGS up -d"')
        writes = [i for i in range(len(source)) if source.startswith('COMPOSE_FILE=docker-compose.yml:', i)]
        assert writes and all(i > up for i in writes)

    def test_reruns_never_open_up_qbob_data(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        for rel in ("configs/nekro-agent.yaml", "qbob_data/qqbot_onebot.db", "qbob_data/media/a.png", "qbob_state/service_account.json"):
            (data / rel).parent.mkdir(parents=True, exist_ok=True)
            (data / rel).write_text("x", encoding="utf-8")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "chmod.log"
        (bindir / "sudo").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8", newline="\n")
        (bindir / "chmod").write_text(f'#!/bin/sh\necho "$*" >> "{log.as_posix()}"\n', encoding="utf-8", newline="\n")
        for fake_bin in (bindir / "sudo", bindir / "chmod"):
            fake_bin.chmod(0o755)
        script = tmp_path / "perm.sh"
        script.write_text(
            _install_function("open_data_dir_permissions") + f'open_data_dir_permissions "{data.as_posix()}"\n',
            encoding="utf-8",
            newline="\n",
        )
        # 与 test_secret_file_permissions 同一做法：假 sudo / chmod 只记参数，find 是真的
        env = {"PATH": f"{bindir}{os.pathsep}{Path(_bash()).parent}"}
        result = subprocess.run([_bash(), script.name], cwd=tmp_path, capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stderr
        calls = log.read_text(encoding="utf-8").splitlines()

        opening = [c for c in calls if c.startswith("777 ")]
        assert any("nekro-agent.yaml" in c for c in opening), calls
        assert not any("qbob_data" in c or "qbob_state" in c for c in opening), opening
        tightening = [c for c in calls if c.startswith("-R go-rwx ")]
        assert any("qbob_data" in c for c in tightening) and any("qbob_state" in c for c in tightening), calls

    def test_reruns_never_open_up_qbob_build_context(self, tmp_path: Path) -> None:
        """`qbob/` 是镜像构建上下文：Dockerfile 与入口脚本会以 root 执行，不能放开成 777"""
        data = tmp_path / "data"
        for rel in ("configs/nekro-agent.yaml", "qbob/Dockerfile", "qbob/qbob_entry.py", "qbob/docker-compose.qbob.yml"):
            (data / rel).parent.mkdir(parents=True, exist_ok=True)
            (data / rel).write_text("x", encoding="utf-8")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "chmod.log"
        (bindir / "sudo").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8", newline="\n")
        (bindir / "chmod").write_text(f'#!/bin/sh\necho "$*" >> "{log.as_posix()}"\n', encoding="utf-8", newline="\n")
        for fake_bin in (bindir / "sudo", bindir / "chmod"):
            fake_bin.chmod(0o755)
        script = tmp_path / "perm.sh"
        script.write_text(
            _install_function("open_data_dir_permissions") + f'open_data_dir_permissions "{data.as_posix()}"\n',
            encoding="utf-8",
            newline="\n",
        )
        env = {"PATH": f"{bindir}{os.pathsep}{Path(_bash()).parent}"}
        result = subprocess.run([_bash(), script.name], cwd=tmp_path, capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stderr
        calls = log.read_text(encoding="utf-8").splitlines()

        opening = [c for c in calls if c.startswith("777 ")]
        assert any("nekro-agent.yaml" in c for c in opening), calls
        assert not any("/qbob/" in c or "\\qbob\\" in c for c in opening), opening

    def test_compose_file_keeps_existing_extras_and_adds_qbob_once(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("COMPOSE_FILE=docker-compose.yml:extra.yml\n", encoding="utf-8")

        first = self._run(tmp_path, "append_qbob_compose_file && cat .env\n")

        assert first.stdout.strip() == "COMPOSE_FILE=docker-compose.yml:extra.yml:qbob/docker-compose.qbob.yml"
        second = self._run(tmp_path, "append_qbob_compose_file && cat .env\n")
        assert second.stdout.strip() == first.stdout.strip()

    def test_previous_install_is_detected_from_compose_file(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("COMPOSE_FILE=docker-compose.yml:qbob/docker-compose.qbob.yml\n", encoding="utf-8")
        assert "YES" in self._run(tmp_path, "env_has_qbob && echo YES\n").stdout
        (tmp_path / ".env").write_text("INSTANCE_NAME=\n", encoding="utf-8")
        assert "NO" in self._run(tmp_path, "env_has_qbob || echo NO\n").stdout
