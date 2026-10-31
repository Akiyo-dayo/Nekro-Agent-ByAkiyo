"""运维写操作确认深链的回归测试

深链是"IM 里不执行写操作"这条设计的落点。它同时承担四件事，
少任何一件这条链接就会退化成一张免登录的通行证：

1. **签名**：内容改一个字节就失效，攻击者无法凭空造出一条链接
2. **短过期**：泄露到聊天记录/截图里也很快失效
3. **一次性**：用过即作废，防止翻出旧链接重放
4. **落地校验身份**：本文件测不到（在路由的 `require_platform_owner()` 依赖上），
   但前三条的价值都建立在第四条之上，故在此备注

另外覆盖 TTL 夹紧：配置项由人手填，填 0 或填一天都可能发生，
夹紧的行为必须是确定的，否则"短时效"这个前提就不成立。
"""

import base64

import pytest

from nekro_agent.services.ops_bot.deeplink import (
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    OpsDeepLinkError,
    OpsDeepLinkReason,
    clear_pending_links,
    consume_ops_deeplink,
    issue_ops_deeplink,
    normalize_ttl_seconds,
    peek_ops_deeplink,
)

NOW = 1_800_000_000
ISSUER = "10001"
ISSUER_CHAT_KEY = "onebot_v11-opsbot-group_900001"


@pytest.fixture(autouse=True)
def _isolate_pending_links():
    """待用表是进程内单例，用例之间必须隔离"""
    clear_pending_links()
    yield
    clear_pending_links()


def issue(action: str = "disable-instance", target: str = "inst1", ttl: int = 300) -> str:
    token, _payload = issue_ops_deeplink(
        action=action,
        target=target,
        issuer_user_id=ISSUER,
        issuer_chat_key=ISSUER_CHAT_KEY,
        ttl_seconds=ttl,
        now=NOW,
    )
    return token


class TestRoundTrip:
    def test_payload_survives_the_round_trip(self) -> None:
        token = issue()
        payload = peek_ops_deeplink(token, now=NOW + 1)
        assert payload.action == "disable-instance"
        assert payload.target == "inst1"
        assert payload.issuer_user_id == ISSUER
        assert payload.issuer_chat_key == ISSUER_CHAT_KEY
        assert payload.exp == NOW + 300

    def test_peek_does_not_consume(self) -> None:
        """预览不消费：否则浏览器预取或聊天软件的链接预览就能白白吃掉令牌"""
        token = issue()
        peek_ops_deeplink(token, now=NOW + 1)
        peek_ops_deeplink(token, now=NOW + 2)
        assert consume_ops_deeplink(token, now=NOW + 3).action == "disable-instance"

    def test_two_issues_produce_distinct_tokens(self) -> None:
        """每条链接的 jti 独立，作废一条不能连带作废另一条"""
        first, second = issue(target="inst1"), issue(target="inst2")
        assert first != second
        consume_ops_deeplink(first, now=NOW + 1)
        assert consume_ops_deeplink(second, now=NOW + 1).target == "inst2"


class TestReplayProtection:
    def test_second_use_is_rejected(self) -> None:
        token = issue()
        consume_ops_deeplink(token, now=NOW + 1)
        with pytest.raises(OpsDeepLinkError) as excinfo:
            consume_ops_deeplink(token, now=NOW + 2)
        assert excinfo.value.reason is OpsDeepLinkReason.ALREADY_USED

    def test_preview_after_use_is_rejected(self) -> None:
        """用过之后连预览都不该再成功，否则确认页会显示一条其实已失效的操作"""
        token = issue()
        consume_ops_deeplink(token, now=NOW + 1)
        with pytest.raises(OpsDeepLinkError) as excinfo:
            peek_ops_deeplink(token, now=NOW + 2)
        assert excinfo.value.reason is OpsDeepLinkReason.ALREADY_USED

    def test_token_from_a_previous_process_is_rejected(self) -> None:
        """待用表清空（等价于重启）后旧链接失效，是 fail-closed 的正确方向"""
        token = issue()
        clear_pending_links()
        with pytest.raises(OpsDeepLinkError) as excinfo:
            consume_ops_deeplink(token, now=NOW + 1)
        assert excinfo.value.reason is OpsDeepLinkReason.ALREADY_USED


class TestExpiry:
    def test_expired_token_is_rejected(self) -> None:
        token = issue(ttl=60)
        with pytest.raises(OpsDeepLinkError) as excinfo:
            consume_ops_deeplink(token, now=NOW + 61)
        assert excinfo.value.reason is OpsDeepLinkReason.EXPIRED

    def test_token_at_exact_expiry_is_rejected(self) -> None:
        """边界取闭区间的哪一侧必须确定：过期时刻本身即失效"""
        token = issue(ttl=60)
        with pytest.raises(OpsDeepLinkError) as excinfo:
            consume_ops_deeplink(token, now=NOW + 60)
        assert excinfo.value.reason is OpsDeepLinkReason.EXPIRED

    def test_token_just_before_expiry_still_works(self) -> None:
        token = issue(ttl=60)
        assert consume_ops_deeplink(token, now=NOW + 59).target == "inst1"

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            (0, MIN_TTL_SECONDS),
            (-1, MIN_TTL_SECONDS),
            (MIN_TTL_SECONDS, MIN_TTL_SECONDS),
            (300, 300),
            (MAX_TTL_SECONDS, MAX_TTL_SECONDS),
            (86400, MAX_TTL_SECONDS),
        ],
    )
    def test_ttl_is_clamped(self, configured: int, expected: int) -> None:
        assert normalize_ttl_seconds(configured) == expected

    def test_issued_expiry_uses_the_clamped_ttl(self) -> None:
        """夹紧必须发生在签发时而不只是展示时，否则实际有效期仍是配置里的坏值"""
        _token, payload = issue_ops_deeplink(
            action="disable-instance",
            target="inst1",
            issuer_user_id=ISSUER,
            issuer_chat_key=ISSUER_CHAT_KEY,
            ttl_seconds=86400,
            now=NOW,
        )
        assert payload.exp == NOW + MAX_TTL_SECONDS


class TestSignature:
    def test_tampered_payload_is_rejected(self) -> None:
        """把 disable 改成别的目标必须失效——这正是签名要挡住的攻击"""
        token = issue(target="inst1")
        payload_segment, _dot, signature = token.partition(".")
        raw = base64.urlsafe_b64decode(payload_segment + "=" * (-len(payload_segment) % 4))
        forged_raw = raw.replace(b'"inst1"', b'"inst9"')
        assert forged_raw != raw
        forged_segment = base64.urlsafe_b64encode(forged_raw).decode("ascii").rstrip("=")

        with pytest.raises(OpsDeepLinkError) as excinfo:
            consume_ops_deeplink(f"{forged_segment}.{signature}", now=NOW + 1)
        assert excinfo.value.reason is OpsDeepLinkReason.BAD_SIGNATURE

    def test_tampered_signature_is_rejected(self) -> None:
        token = issue()
        payload_segment, _dot, signature = token.partition(".")
        forged = signature[:-1] + ("0" if signature[-1] != "0" else "1")
        with pytest.raises(OpsDeepLinkError) as excinfo:
            consume_ops_deeplink(f"{payload_segment}.{forged}", now=NOW + 1)
        assert excinfo.value.reason is OpsDeepLinkReason.BAD_SIGNATURE

    @pytest.mark.parametrize(
        "token",
        ["", "no-separator", ".onlysignature", "onlypayload.", "a" * 5000],
    )
    def test_malformed_tokens_are_rejected(self, token: str) -> None:
        with pytest.raises(OpsDeepLinkError) as excinfo:
            consume_ops_deeplink(token, now=NOW)
        assert excinfo.value.reason in (OpsDeepLinkReason.MALFORMED, OpsDeepLinkReason.BAD_SIGNATURE)

    def test_unsigned_payload_is_rejected(self) -> None:
        """伪造者能构造合法 JSON，但构造不出签名"""
        forged_payload = (
            b'{"v":1,"action":"suspend-tenant","target":"1","issuer_user_id":"9",'
            b'"issuer_chat_key":"x","jti":"forged","exp":9999999999}'
        )
        segment = base64.urlsafe_b64encode(forged_payload).decode("ascii").rstrip("=")
        with pytest.raises(OpsDeepLinkError) as excinfo:
            consume_ops_deeplink(f"{segment}.v1={'0' * 64}", now=NOW)
        assert excinfo.value.reason is OpsDeepLinkReason.BAD_SIGNATURE


class TestTokenShape:
    def test_token_is_url_safe(self) -> None:
        """令牌要塞进 URL 查询串，出现需要转义的字符会在各种链接解析里出问题"""
        token = issue()
        assert all(char.isalnum() or char in "-_.=" for char in token), token
