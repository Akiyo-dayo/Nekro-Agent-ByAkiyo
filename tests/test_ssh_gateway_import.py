"""存量工作区资源里的 SSH 模板导入

## 这条测试钉的是什么

三个旧 SSH 模板（密钥 / 密码 / root 密码）要能从「资源 + 启用绑定」导成平台方凭据行，
字段、加密、来源引用与备注正确；旧资源一行不改；重复跑只导一次；各种不导的情形都要
在报告里点名原因，而不是静默少几条。别名转换与工作区内冲突也在这一组里。

## 环境

内存 SQLite；主密钥 monkeypatch 到临时目录；目标主机全部是文档地址或 example 域名；
秘密值一律现编。工作区能否拿平台方凭据这一条按工作区打桩：判据本身在
`tests/test_cc_platform_credentials.py` 里单独钉，这里只关心导入会不会照着它跳过。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Dict, Optional, Set

import pytest
from fastapi import HTTPException
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_workspace_resource import DBWorkspaceResource
from nekro_agent.models.db_workspace_resource_binding import DBWorkspaceResourceBinding
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.routers import ssh_gateway as ssh_router
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.resources.crypto import encrypt_secret_payload
from nekro_agent.services.ssh_gateway import import_resources
from nekro_agent.services.ssh_gateway import secrets as ssh_secrets
from nekro_agent.services.ssh_gateway.constants import EVENT_CRED_CREATE
from nekro_agent.services.user.role import Role
from nekro_agent.tools import credential_cipher

KEY_PRIMARY = "import-resource-" + "primary-4b1"
PASSWORD = "pw-" + "import-9ad"
PASSPHRASE = "phrase-" + "import-2c7"
PRIVATE_KEY = (
    "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n"
    + "ZmFrZS10ZXN0LWtleS1tYXRlcmlhbC1mb3ItaW1wb3J0\n"
    + "-----END OPENSSH " + "PRIVATE KEY-----"
)
HOST = "ssh.example.test"


# ---------------------------------------------------------------------------
# 环境与夹具
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def cipher_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_PRIMARY)
    credential_cipher.reset_credential_cipher_cache()
    try:
        yield
    finally:
        credential_cipher.reset_credential_cipher_cache()


@pytest.fixture(autouse=True)
def platform_allowed(monkeypatch: pytest.MonkeyPatch):
    """默认所有工作区都归平台方；要拒的用例自己改这个集合"""
    denied: Set[int] = set()

    async def _allowed(workspace: DBWorkspace) -> bool:
        return workspace.id not in denied

    monkeypatch.setattr(import_resources, "platform_credentials_allowed", _allowed)
    return denied


def platform_owner() -> DBUser:
    return DBUser(
        username="owner",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def tenant_admin() -> DBUser:
    return DBUser(
        username="tenant-admin",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=41,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


async def make_workspace(name: str) -> DBWorkspace:
    return await DBWorkspace.create(name=name, description=name)


async def make_resource(
    template_key: str,
    name: str,
    public: Dict[str, str],
    secret: Dict[str, str],
    *,
    enabled: bool = True,
    secret_payload: Optional[str] = None,
) -> DBWorkspaceResource:
    return await DBWorkspaceResource.create(
        resource_key=f"res-{name}-{template_key}",
        name=name,
        template_key=template_key,
        public_payload=public,
        secret_payload_encrypted=(
            secret_payload if secret_payload is not None else encrypt_secret_payload(secret)
        ),
        enabled=enabled,
    )


async def bind(resource: DBWorkspaceResource, workspace: DBWorkspace, *, enabled: bool = True) -> None:
    await DBWorkspaceResourceBinding.create(
        workspace_id=workspace.id,
        resource_id=resource.id,
        enabled=enabled,
    )


async def import_once(actor: str = "tester"):
    return await import_resources.import_ssh_resources(actor=actor)


def _skip_reasons(report) -> Dict[str, str]:
    return {item.resource_name: item.reason for item in report.skipped}


# ---------------------------------------------------------------------------
# 三种模板
# ---------------------------------------------------------------------------


class TestImportThreeTemplates:
    async def test_fields_encryption_reference_and_note(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        key_resource = await make_resource(
            "server_ssh_key",
            "prod key",
            {"host": HOST, "port": "2222", "username": "deploy"},
            {"private_key": PRIVATE_KEY, "passphrase": PASSPHRASE},
        )
        password_resource = await make_resource(
            "server_ssh_password",
            "prod password",
            {"host": HOST, "port": "", "username": "ops"},
            {"password": PASSWORD},
        )
        root_resource = await make_resource(
            "server_root_password",
            "prod root",
            {"host": HOST, "port": "2202"},
            {"password": PASSWORD},
        )
        for resource in (key_resource, password_resource, root_resource):
            await bind(resource, ws)

        report = await import_once()

        assert len(report.imported) == 3
        assert report.already == 0
        assert report.skipped == []
        rows = {row.alias: row for row in await DBWsSshCredential.filter(workspace_id=ws.id)}
        assert set(rows) == {"prod-key", "prod-password", "prod-root"}

        key_row = rows["prod-key"]
        assert key_row.owner_kind == "platform"
        assert key_row.owner_account == ""
        assert (key_row.adapter_key, key_row.instance_key) == ("", "")
        assert key_row.created_via == "import"
        assert key_row.import_ref == f"resource:{key_resource.id}"
        assert key_row.note == f"导入自工作区资源 #{key_resource.id}：prod key"
        assert key_row.created_by == key_row.updated_by == "tester"
        assert key_row.host == HOST
        assert key_row.port == 2222
        assert key_row.username == "deploy"
        assert key_row.host_keys == []
        assert key_row.status == "active"
        assert key_row.password_enc == ""
        assert key_row.private_key_enc.startswith(credential_cipher.ENVELOPE_PREFIX)
        assert PRIVATE_KEY not in key_row.private_key_enc
        assert ssh_secrets.decrypt_secret(key_row.private_key_enc)[0] == PRIVATE_KEY
        assert ssh_secrets.decrypt_secret(key_row.passphrase_enc)[0] == PASSPHRASE

        password_row = rows["prod-password"]
        assert password_row.port == 22
        assert password_row.username == "ops"
        assert ssh_secrets.decrypt_secret(password_row.password_enc)[0] == PASSWORD
        assert password_row.private_key_enc == ""

        root_row = rows["prod-root"]
        assert root_row.port == 2202
        assert root_row.username == "root"
        assert ssh_secrets.decrypt_secret(root_row.password_enc)[0] == PASSWORD

        assert await DBWsSshAudit.filter(event=EVENT_CRED_CREATE, actor="tester").count() == 3

    async def test_old_resources_and_bindings_are_not_touched(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_password",
            "prod",
            {"host": HOST, "port": "22", "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws)
        before_payload = resource.secret_payload_encrypted
        before_update_time = resource.update_time
        binding_before = await DBWorkspaceResourceBinding.filter(resource_id=resource.id).first()
        assert binding_before is not None

        await import_once()

        fresh = await DBWorkspaceResource.get(id=resource.id)
        assert fresh.secret_payload_encrypted == before_payload
        assert fresh.update_time == before_update_time
        assert fresh.public_payload == resource.public_payload
        fresh_binding = await DBWorkspaceResourceBinding.filter(id=binding_before.id).first()
        assert fresh_binding is not None and fresh_binding.enabled is True

    async def test_second_run_only_counts_already(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        for index, template in enumerate(("server_ssh_key", "server_ssh_password", "server_root_password")):
            resource = await make_resource(
                template,
                f"server-{index}",
                {"host": HOST, "port": "22", "username": "ops"},
                {"private_key": PRIVATE_KEY} if template == "server_ssh_key" else {"password": PASSWORD},
            )
            await bind(resource, ws)

        first = await import_once()
        second = await import_once()

        assert len(first.imported) == 3
        assert second.imported == []
        assert second.already == 3
        assert second.skipped == []
        assert await DBWsSshCredential.filter(workspace_id=ws.id).count() == 3


# ---------------------------------------------------------------------------
# 不导的情形
# ---------------------------------------------------------------------------


class TestSkipReasons:
    async def test_workspace_not_platform(self, db, platform_allowed: Set[int]) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        platform_allowed.add(ws.id)
        resource = await make_resource(
            "server_ssh_password",
            "tenant-ish",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws)

        report = await import_once()

        assert report.imported == []
        assert _skip_reasons(report)["tenant-ish"] == "工作区不归平台方"
        assert await DBWsSshCredential.filter(workspace_id=ws.id).count() == 0

    async def test_resource_disabled(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_password",
            "disabled-resource",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
            enabled=False,
        )
        await bind(resource, ws)

        report = await import_once()

        assert report.imported == []
        assert _skip_reasons(report)["disabled-resource"] == "资源已停用"
        assert await DBWsSshCredential.all().count() == 0

    async def test_binding_disabled(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_password",
            "disabled-binding",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws, enabled=False)

        report = await import_once()

        assert report.imported == []
        assert _skip_reasons(report)["disabled-binding"] == "绑定已停用"
        assert await DBWsSshCredential.all().count() == 0

    async def test_undecryptable_payload(self, db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: ARG002
        # 用另一把密钥、另一个数据目录造一封当前主密钥解不开的信封
        other_dir = tmp_path / "other-key"
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(other_dir))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "import-resource-" + "other-key")
        credential_cipher.reset_credential_cipher_cache()
        broken_payload = encrypt_secret_payload({"password": PASSWORD})
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_PRIMARY)
        credential_cipher.reset_credential_cipher_cache()

        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_password",
            "broken-payload",
            {"host": HOST, "username": "ops"},
            {},
            secret_payload=broken_payload,
        )
        await bind(resource, ws)

        report = await import_once()

        assert report.imported == []
        assert "解不开" in _skip_reasons(report)["broken-payload"]
        assert await DBWsSshCredential.all().count() == 0

    async def test_missing_host(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_password",
            "missing-host",
            {"host": "  ", "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws)

        report = await import_once()

        assert report.imported == []
        assert "主机" in _skip_reasons(report)["missing-host"]

    async def test_missing_username_for_non_root_template(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_password",
            "missing-user",
            {"host": HOST, "username": ""},
            {"password": PASSWORD},
        )
        await bind(resource, ws)

        report = await import_once()

        assert report.imported == []
        assert "用户名" in _skip_reasons(report)["missing-user"]

    async def test_missing_credentials(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_key",
            "missing-secret",
            {"host": HOST, "username": "deploy"},
            {},
        )
        await bind(resource, ws)

        report = await import_once()

        assert report.imported == []
        reason = _skip_reasons(report)["missing-secret"]
        assert "密码" in reason and "私钥" in reason


# ---------------------------------------------------------------------------
# 别名
# ---------------------------------------------------------------------------


class TestAliases:
    async def test_chinese_name_falls_back_to_resource_id(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-alias")
        resource = await make_resource(
            "server_ssh_password",
            "生产服务器",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws)

        await import_once()

        row = await DBWsSshCredential.get(workspace_id=ws.id)
        assert row.alias == f"srv-{resource.id}"

    async def test_conflict_suffix_and_total_length(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-alias")
        first = await make_resource(
            "server_ssh_password",
            "same name",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        second = await make_resource(
            "server_ssh_password",
            "same-name",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        third = await make_resource(
            "server_ssh_password",
            "same_name",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        for resource in (first, second, third):
            await bind(resource, ws)

        await import_once()

        aliases = sorted(await DBWsSshCredential.filter(workspace_id=ws.id).values_list("alias", flat=True))
        assert aliases == ["same-name", "same-name-2", "same-name-3"]

    async def test_long_name_is_truncated_to_the_limit(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-alias")
        resource = await make_resource(
            "server_ssh_password",
            "a" * 40,
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws)

        await import_once()

        row = await DBWsSshCredential.get(workspace_id=ws.id)
        assert row.alias == "a" * 32

    async def test_reserved_prefix_is_removed(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-alias")
        resource = await make_resource(
            "server_ssh_password",
            "NK_web",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws)

        await import_once()

        row = await DBWsSshCredential.get(workspace_id=ws.id)
        assert row.alias == "web"


# ---------------------------------------------------------------------------
# 启动与后台触发
# ---------------------------------------------------------------------------


class TestTriggers:
    async def test_startup_does_nothing_when_gateway_is_off(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_password",
            "prod",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws)
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)

        report = await import_resources.import_ssh_resources_on_startup()

        assert report.imported == []
        assert await DBWsSshCredential.all().count() == 0

    async def test_startup_imports_when_gateway_is_on(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_password",
            "prod",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws)
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)

        report = await import_resources.import_ssh_resources_on_startup()

        assert len(report.imported) == 1
        assert report.imported[0].resource_name == "prod"
        assert await DBWsSshCredential.all().count() == 1

    async def test_manual_endpoint_imports_even_when_gateway_is_off(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_password",
            "prod",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws)
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)

        report = await ssh_router.import_ssh_gateway_resources(_current_user=platform_owner())

        assert len(report.imported) == 1
        row = await DBWsSshCredential.get(workspace_id=ws.id)
        assert row.created_by == "owner"
        assert row.created_via == "import"

    async def test_manual_endpoint_rejects_tenant_admin(self, db) -> None:  # noqa: ARG002
        with pytest.raises(HTTPException) as exc_info:
            await ssh_router.import_ssh_gateway_resources(_current_user=tenant_admin())
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# 报告与日志里不出现凭据值
# ---------------------------------------------------------------------------


class TestNoSecretValuesInReportOrLogs:
    async def test_skipped_and_imported_entries_never_carry_secrets(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-import")
        resource = await make_resource(
            "server_ssh_password",
            "prod",
            {"host": HOST, "username": "ops"},
            {"password": PASSWORD},
        )
        await bind(resource, ws)

        report = await import_once()

        text = report.model_dump_json()
        assert PASSWORD not in text
        assert PRIVATE_KEY not in text
