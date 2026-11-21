"""工作区资源敏感载荷的落库形态

## 这里钉的是什么

`DBWorkspaceResource.secret_payload_encrypted` 里装着 SSH 私钥、root 口令、数据库密码
等值。改造前 `encrypt_secret_payload` 就是一句 `json.dumps`，字段名写着 encrypted、
库里却是明文 JSON。现在默认写成 `tools/credential_cipher` 的 `nkenc1:` 信封，
读侧同时兼容存量明文与更早的 AES 载荷，启动时把存量整批重写；`plain` 是回滚开关。

## 每条断言都要有能分辨真假的对照

- "库里看不到秘密"一律配一条"解密后拿得到原文"或"库里确实以信封开头"的对照；
- 健康检查的每个计数都单独造一行；
- 明文回滚用例里另写一个只做 `json.loads` + 判 dict 的小函数，模拟"旧版本的读法"。

## 测试环境

密钥来源是 `OsEnv.CREDENTIAL_SECRET_KEY` 与 `OsEnv.DATA_DIR`，一律 monkeypatch 到
tmp 目录，并在改完之后调用 `reset_credential_cipher_cache()`。数据库用内存 SQLite，
不依赖真 PG / Docker / 网络。
"""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Dict, List

import pytest
from Crypto.Cipher import AES
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_workspace_resource import DBWorkspaceResource
from nekro_agent.models.db_workspace_resource_binding import DBWorkspaceResourceBinding
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.tenant import CREDENTIAL_MASK
from nekro_agent.schemas.workspace_resource import ResourceField, WorkspaceResourceCreate, WorkspaceResourceUpdate
from nekro_agent.services.resources import workspace_resource_service
from nekro_agent.services.resources.crypto import (
    SecretPayloadDecodeError,
    SecretPayloadEncryptError,
    SecretPayloadFormat,
    decode_secret_payload,
    decrypt_secret_payload,
    encrypt_secret_payload,
)
from nekro_agent.services.resources.service import resources_upgrade_at_rest
from nekro_agent.tools.credential_cipher import (
    ENVELOPE_PREFIX,
    KEY_FILE_NAME,
    credential_cipher_status,
    decrypt_credential_with_source,
    is_encrypted_envelope,
    reset_credential_cipher_cache,
)

# 哨兵都是自己编的，不是任何真实凭据
SECRET_VALUE = "pw-" + "sentinel-7Q4b91"
SECOND_SECRET = "token-" + "sentinel-X2m8"
LEGACY_JWT_SECRET = "legacy-jwt-" + "sentinel-31f"
HOST_VALUE = "jump.internal"
KEY_A = "at-rest-key-" + "A-9c1"
KEY_B = "at-rest-key-" + "B-4e7"
KEY_C = "at-rest-key-" + "C-8d2"


def _fields(*, secret: str = SECRET_VALUE, include_secret: bool = True) -> List[ResourceField]:
    fields = [
        ResourceField(field_key="host", label="主机", value_kind="host", order=10, value=HOST_VALUE),
    ]
    if include_secret:
        fields.append(
            ResourceField(
                field_key="private_key",
                label="私钥",
                secret=True,
                value_kind="private_key",
                order=20,
                value=secret,
            )
        )
    return fields


def _create_body(name: str = "prod jump", *, secret: str = SECRET_VALUE, include_secret: bool = True):
    return WorkspaceResourceCreate(
        name=name,
        template_key=None,
        resource_note="",
        resource_tags=[],
        resource_prompt="",
        fields=_fields(secret=secret, include_secret=include_secret),
        enabled=True,
    )


async def _insert_row(name: str, stored: str) -> DBWorkspaceResource:
    """绕开服务层直接写一行，用来造存量明文 / 历史密文 / 坏载荷"""
    return await DBWorkspaceResource.create(
        resource_key=name.lower().replace(" ", "-"),
        name=name,
        template_key=None,
        resource_note="",
        resource_tags_json=[],
        resource_prompt="",
        schema_json=[],
        public_payload={},
        secret_payload_encrypted=stored,
        enabled=True,
    )


async def _stored(resource_id: int) -> str:
    return (await DBWorkspaceResource.get(id=resource_id)).secret_payload_encrypted


async def _bind_to_new_workspace(resource_id: int) -> DBWorkspace:
    workspace = await DBWorkspace.create(name=f"ws-{resource_id}", description="")
    await DBWorkspaceResourceBinding.create(workspace_id=workspace.id, resource_id=resource_id, enabled=True, sort_order=10)
    return workspace


def _legacy_aes_payload(payload: Dict[str, str], secret: str) -> str:
    """按改造前的规则生成一份旧 AES 载荷（nonce / ciphertext / tag）"""
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    nonce = b"nonce-abcdef"
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return json.dumps(
        {
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "tag": base64.b64encode(tag).decode("ascii"),
        }
    )


def _old_version_read(value: str) -> Dict[str, str]:
    """模拟改造前那个只会 `json.loads` 并确认是 dict 的读法"""
    data = json.loads(value)
    assert isinstance(data, dict)
    return data


@pytest.fixture
async def cipher_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """与本机环境无关的密钥环境 + 一张空的内存 SQLite"""
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_A)
    monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", "unrelated-jwt-" + "sentinel")
    monkeypatch.setattr(config, "RESOURCE_PAYLOAD_AT_REST", "encrypted")
    reset_credential_cipher_cache()

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield tmp_path
    finally:
        await Tortoise.close_connections()
        reset_credential_cipher_cache()


@pytest.fixture
def log_messages():
    """抓产品代码真正打出去的日志（loguru 不进 stdlib logging，caplog 抓不到）"""
    from loguru import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="DEBUG")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


class TestCreatedRowsAreEnvelopes:
    async def test_create_encrypts_at_rest_and_still_resolves_plaintext(self, cipher_env: Path) -> None:
        created = await workspace_resource_service.create_resource(_create_body())

        stored = await _stored(created.id)
        assert stored.startswith(ENVELOPE_PREFIX)
        assert SECRET_VALUE not in stored
        # 正向对照：解密后逐字拿得到原文，证明上面不是"写坏了所以搜不到"
        assert decrypt_secret_payload(stored) == {"private_key": SECRET_VALUE}

        detail = await workspace_resource_service.get_resource_detail(created.id)
        values = {field.field_key: field.value for field in detail.fields}
        assert values["private_key"] == CREDENTIAL_MASK
        assert values["host"] == HOST_VALUE

        workspace = await _bind_to_new_workspace(created.id)
        env = await workspace_resource_service.resolve_workspace_resources_to_env(
            workspace.id, allow_platform_secrets=True
        )
        assert env["NEKRO_RESOURCE_PROD_JUMP_PRIVATE_KEY"] == SECRET_VALUE
        assert env["NEKRO_RESOURCE_PROD_JUMP_HOST"] == HOST_VALUE
        assert CREDENTIAL_MASK not in env.values()


class TestLegacyPlaintextUpgrade:
    async def test_plaintext_is_readable_then_upgraded_without_touching_update_time(
        self,
        cipher_env: Path,
    ) -> None:
        payload = {"private_key": SECRET_VALUE, "root_password": SECOND_SECRET}
        stored = json.dumps(payload, ensure_ascii=False)
        row = await _insert_row("legacy plain", stored)
        update_time_before = row.update_time

        decoded = decode_secret_payload(stored)
        assert decoded.format == SecretPayloadFormat.PLAIN
        assert decoded.payload == payload
        assert decoded.stale is True

        await asyncio.sleep(0.01)
        report = await resources_upgrade_at_rest()

        assert report.mode == "encrypted"
        assert report.rewritten == ["legacy plain"]
        after = await _stored(row.id)
        assert after.startswith(ENVELOPE_PREFIX)
        assert SECRET_VALUE not in after
        assert decrypt_secret_payload(after) == payload
        # 重加密不是一次用户编辑：`filter.update` 不该碰 auto_now 的 update_time
        assert (await DBWorkspaceResource.get(id=row.id)).update_time == update_time_before

    async def test_second_upgrade_rewrites_nothing(self, cipher_env: Path) -> None:
        row = await _insert_row("legacy plain", json.dumps({"private_key": SECRET_VALUE}))

        assert (await resources_upgrade_at_rest()).rewritten == ["legacy plain"]
        assert await _stored(row.id) != ""
        assert (await resources_upgrade_at_rest()).rewritten == []


class TestHistoricalKeyEnvelope:
    async def test_fallback_envelope_is_stale_and_reencrypted_with_primary(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 先用 A 加密（此时 A 是环境变量主密钥）
        stored = encrypt_secret_payload({"private_key": SECRET_VALUE})
        assert stored.startswith(ENVELOPE_PREFIX)

        # 把 A 放进数据目录的密钥文件，再把环境变量主密钥换成 B：
        # 按 `_load_key_ring` 的规则，文件里那把 A 自动成为历史密钥
        key_path = Path(OsEnv.DATA_DIR) / KEY_FILE_NAME
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(KEY_A, encoding="utf-8")
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_B)
        reset_credential_cipher_cache()
        assert credential_cipher_status().fallback_count == 1

        row = await _insert_row("rotated key", stored)
        decoded = decode_secret_payload(stored)
        assert decoded.format == SecretPayloadFormat.ENVELOPE
        assert decoded.payload == {"private_key": SECRET_VALUE}
        assert decoded.stale is True

        report = await resources_upgrade_at_rest()

        assert report.rewritten == ["rotated key"]
        after = await _stored(row.id)
        assert after != stored
        plain, source = decrypt_credential_with_source(after)
        assert source == "primary"
        assert json.loads(plain) == {"private_key": SECRET_VALUE}


class TestUnreadableEnvelope:
    async def test_unreadable_envelope_is_named_and_left_alone(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        log_messages: List[str],
    ) -> None:
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_A)
        reset_credential_cipher_cache()
        stored = encrypt_secret_payload({"private_key": SECRET_VALUE})

        # 换一把毫不相干、也没有历史密钥的主密钥
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_C)
        reset_credential_cipher_cache()

        with pytest.raises(SecretPayloadDecodeError) as exc_info:
            decode_secret_payload(stored)
        message = str(exc_info.value)
        assert "NEKRO_CREDENTIAL_SECRET_KEY" in message
        assert "credential_secret.key" in message

        row = await _insert_row("locked out", stored)
        health = await workspace_resource_service.check_secret_payload_health()
        assert health.ok is False
        assert health.invalid_count == 1
        assert health.issues[0].resource_name == "locked out"
        assert health.issues[0].reason == message

        workspace = await _bind_to_new_workspace(row.id)
        with pytest.raises(ValidationError):
            await workspace_resource_service.resolve_workspace_resources_to_env(
                workspace.id, allow_platform_secrets=True
            )

        update_time_before = row.update_time
        report = await resources_upgrade_at_rest()

        assert report.rewritten == []
        assert report.skipped == ["locked out"]
        assert await _stored(row.id) == stored
        assert (await DBWorkspaceResource.get(id=row.id)).update_time == update_time_before
        assert any("locked out" in item for item in log_messages if "不会重写" in item)


def _two_secret_body(name: str, *, key_value: str, passphrase_value: str) -> WorkspaceResourceUpdate:
    return WorkspaceResourceUpdate(
        name=name,
        template_key=None,
        resource_note="",
        resource_tags=[],
        resource_prompt="",
        fields=[
            ResourceField(field_key="host", label="主机", value_kind="host", order=10, value=HOST_VALUE),
            ResourceField(field_key="private_key", label="私钥", secret=True, value_kind="private_key", order=20, value=key_value),
            ResourceField(field_key="passphrase", label="口令短语", secret=True, value_kind="password", order=30, value=passphrase_value),
        ],
        enabled=True,
    )


class TestSaveKeepsUnreadableEnvelope:
    """密钥换掉以后，一次普通保存不能把还可能救回来的密文抹掉"""

    async def _locked_out_row(self, monkeypatch: pytest.MonkeyPatch) -> int:
        created = await workspace_resource_service.create_resource(
            WorkspaceResourceCreate(**_two_secret_body("locked", key_value=SECRET_VALUE, passphrase_value=SECOND_SECRET).model_dump())
        )
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_C)
        reset_credential_cipher_cache()
        return created.id

    async def test_all_masked_save_keeps_ciphertext_and_updates_other_fields(
        self, cipher_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resource_id = await self._locked_out_row(monkeypatch)
        stored_before = await _stored(resource_id)

        await workspace_resource_service.update_resource(
            resource_id,
            _two_secret_body("locked renamed", key_value=CREDENTIAL_MASK, passphrase_value=CREDENTIAL_MASK),
        )

        row = await DBWorkspaceResource.get(id=resource_id)
        assert row.name == "locked renamed"
        assert row.secret_payload_encrypted == stored_before
        # 正向对照：把原密钥放回去，原值逐字还在
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_A)
        reset_credential_cipher_cache()
        assert decrypt_secret_payload(row.secret_payload_encrypted) == {
            "private_key": SECRET_VALUE,
            "passphrase": SECOND_SECRET,
        }

    async def test_partial_refill_is_refused(self, cipher_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        resource_id = await self._locked_out_row(monkeypatch)
        stored_before = await _stored(resource_id)

        with pytest.raises(ValidationError) as exc_info:
            await workspace_resource_service.update_resource(
                resource_id,
                _two_secret_body("locked", key_value="new-" + "key-material", passphrase_value=CREDENTIAL_MASK),
            )
        assert "NEKRO_CREDENTIAL_SECRET_KEY" in str(exc_info.value)
        assert await _stored(resource_id) == stored_before

    async def test_full_refill_overwrites_with_current_key(
        self, cipher_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resource_id = await self._locked_out_row(monkeypatch)
        new_key = "new-" + "key-material"
        new_phrase = "new-" + "phrase"

        await workspace_resource_service.update_resource(
            resource_id,
            _two_secret_body("locked", key_value=new_key, passphrase_value=new_phrase),
        )

        assert decrypt_secret_payload(await _stored(resource_id)) == {"private_key": new_key, "passphrase": new_phrase}


class TestLegacyAesPayload:
    async def test_recovery_requires_old_key_and_writes_an_envelope(self, cipher_env: Path) -> None:
        payload = {"private_key": SECRET_VALUE}
        legacy_payload = _legacy_aes_payload(payload, LEGACY_JWT_SECRET)
        row = await _insert_row("old aes", legacy_payload)

        # 当前 JWT 密钥不是创建时那把：直接解会失败，必须显式给旧密钥
        with pytest.raises(SecretPayloadDecodeError):
            decode_secret_payload(legacy_payload)

        recovered = decode_secret_payload(legacy_payload, legacy_secret=LEGACY_JWT_SECRET)
        assert recovered.format == SecretPayloadFormat.LEGACY_ENCRYPTED
        assert recovered.payload == payload
        assert recovered.stale is False

        response = await workspace_resource_service.recover_legacy_secret_payloads(LEGACY_JWT_SECRET)

        assert response.recovered_count == 1
        assert response.invalid_count == 0
        after = await _stored(row.id)
        assert after.startswith(ENVELOPE_PREFIX)
        assert decrypt_secret_payload(after) == payload


class TestNoAvailableKeyRefusesWrites:
    async def test_secret_write_fails_closed_but_secretless_write_still_works(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        blocker = cipher_env / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(blocker / "nested"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        reset_credential_cipher_cache()
        assert credential_cipher_status().available is False
        with pytest.raises(SecretPayloadEncryptError):
            encrypt_secret_payload({"private_key": SECRET_VALUE})

        with pytest.raises(ValidationError) as exc_info:
            await workspace_resource_service.create_resource(_create_body())
        assert "凭据静态加密不可用" in str(exc_info.value)
        assert "拒绝以明文保存敏感字段" in str(exc_info.value)
        assert await DBWorkspaceResource.all().count() == 0

        # 不带敏感字段：没有东西要加密，照常成功
        created = await workspace_resource_service.create_resource(_create_body(name="plain host", include_secret=False))
        assert await _stored(created.id) == ""

    async def test_upgrade_does_not_rewrite_when_cipher_is_unavailable(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        blocker = cipher_env / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(blocker / "nested"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        reset_credential_cipher_cache()

        stored = json.dumps({"private_key": SECRET_VALUE})
        row = await _insert_row("legacy plain", stored)
        report = await resources_upgrade_at_rest()

        assert report.rewritten == []
        assert await _stored(row.id) == stored


class TestPlainRollbackMode:
    async def test_plain_mode_writes_json_and_upgrade_rolls_envelopes_back(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        log_messages: List[str],
    ) -> None:
        payload = {"private_key": SECRET_VALUE, "root_password": SECOND_SECRET}

        monkeypatch.setattr(config, "RESOURCE_PAYLOAD_AT_REST", "plain")
        plain = encrypt_secret_payload(payload)
        assert not plain.startswith(ENVELOPE_PREFIX)
        assert _old_version_read(plain) == payload

        # 先按默认模式造一行信封，再把配置切到回滚模式
        monkeypatch.setattr(config, "RESOURCE_PAYLOAD_AT_REST", "encrypted")
        envelope = encrypt_secret_payload(payload)
        assert is_encrypted_envelope(envelope)
        row = await _insert_row("encrypted row", envelope)

        monkeypatch.setattr(config, "RESOURCE_PAYLOAD_AT_REST", "plain")
        report = await resources_upgrade_at_rest()

        assert report.mode == "plain"
        assert report.rewritten == ["encrypted row"]
        after = await _stored(row.id)
        assert not after.startswith(ENVELOPE_PREFIX)
        # 旧版本只做 json.loads + 判 dict，读出来的内容必须与原文一致
        assert _old_version_read(after) == payload
        assert any("写回明文" in item for item in log_messages)


class TestHealthCountsEveryFormat:
    async def test_counts_and_stale_rows(
        self,
        cipher_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 历史密钥信封：用 A 加密，文件里留 A、主密钥换成 B
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_A)
        reset_credential_cipher_cache()
        fallback_envelope = encrypt_secret_payload({"private_key": SECOND_SECRET})
        key_path = Path(OsEnv.DATA_DIR) / KEY_FILE_NAME
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(KEY_A, encoding="utf-8")
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_B)
        reset_credential_cipher_cache()

        primary_envelope = encrypt_secret_payload({"private_key": SECRET_VALUE})
        plain = json.dumps({"private_key": SECOND_SECRET})
        # 健康检查不传旧密钥，所以这一行必须能用当前 `NEKRO_JWT_SECRET_KEY` 解开，
        # 才会落进 `legacy_encrypted_count` 而不是 `invalid_count`
        legacy = _legacy_aes_payload({"private_key": SECRET_VALUE}, OsEnv.JWT_SECRET_KEY)

        await _insert_row("envelope primary", primary_envelope)
        await _insert_row("envelope fallback", fallback_envelope)
        await _insert_row("plain row", plain)
        await _insert_row("empty row", "")
        await _insert_row("legacy row", legacy)
        await _insert_row("invalid row", "这不是 JSON")

        health = await workspace_resource_service.check_secret_payload_health()

        assert health.total == 6
        assert health.encrypted_count == 2
        assert health.plaintext_count == 1
        assert health.empty_count == 1
        # `EMPTY` 不再被算成明文
        assert health.plaintext_count + health.empty_count == 2
        assert health.legacy_encrypted_count == 1
        assert health.invalid_count == 1
        assert health.stale_count == 2  # 存量明文 + 历史密钥信封
        assert health.ok is False
        assert [issue.resource_name for issue in health.issues] == ["invalid row"]


class TestEnvelopeDetectionIsStructural:
    async def test_json_value_that_only_looks_like_a_prefix_stays_plain(self, cipher_env: Path) -> None:
        inner = f"{ENVELOPE_PREFIX}not-really-a-ciphertext"
        payload = {"private_key": inner}
        stored = json.dumps(payload)

        assert is_encrypted_envelope(inner) is False
        result = decode_secret_payload(stored)

        assert result.format == SecretPayloadFormat.PLAIN
        assert result.payload == payload
        assert result.stale is True
