"""协议端出站地址的形状校验：与 NapCat（WHATWG URL）的解析口径对齐

## 这份测试在守什么

NapCat 连接用户填写的 `websocketClients` / `httpClients` 地址时按 WHATWG URL 规则解析，
而 NA 的校验走 Python `urlparse` / httpx。同一个字符串在两套解析器下可能得出不同的主机
（反斜杠、用户名密码、百分号编码主机、数字简写主机、非 ASCII 主机、夹带的制表符等），
于是校验看到的是公网主机、NapCat 实际连的却是内网主机。

这里钉住的是：**校验看到的主机必须等于 NapCat 实际会连的主机，否则拒绝**。形状检查之外
不猜地址指向哪里；通过形状检查后，只用拆出的主机 / 端口重拼探测地址交给公网校验，所以
「校验的主机」与「形状检查的主机」恒为同一个。

## 用例取材

A 表是歧义 / 不受支持的形状，逐条拒绝；B 表是任何解析器都会得出同一主机的形状，逐条放行。
最后一组在有 node 时把 A、B 与补充地址一次性交给 `new URL()`，用 WHATWG 的真身对照：
放行的地址主机与端口必须一致，两套解析器分叉的地址必须全部被拒。
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytest

from nekro_agent.schemas.console import NetworkEntry, NetworkEntryType
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.console import container_manager
from nekro_agent.tools.common_util import UnsafeDownloadUrlError

# ---------------------------------------------------------------------------
# 语料
# ---------------------------------------------------------------------------

REJECTED_URLS: List[str] = [
    # 反斜杠：WHATWG 当 `/`，urlparse / httpx 不当
    "ws://10.0.0.5\\@public.example/",
    "ws://10.0.0.5\\public.example/",
    "ws://public.example\\@10.0.0.5/",
    "ws://example.com\\x/",
    "ws:\\\\10.0.0.5/",
    # 用户名密码：`@` 之前的片段可能是另一种主机的伪装
    "ws://public.example@10.0.0.5/",
    "ws://public.example:80@10.0.0.5/",
    "ws://a@b@10.0.0.5/",
    "ws://user:pw@example.com/",
    # 缺少 `://`、多余斜杠：WHATWG 仍能把 `10.0.0.5` 当主机
    "ws:///10.0.0.5/",
    "ws:////10.0.0.5/",
    "ws:10.0.0.5",
    "ws:/10.0.0.5",
    "ws://",
    "ws:///",
    # 百分号编码：WHATWG 解码，Python 不解码
    "ws://%31%30%2e0%2e0%2e5/",
    "ws://10.0.0.5%2e/",
    "ws://public.example%2f@10.0.0.5/",
    # 数字简写 / 十六进制 / 八进制 / 前导 0：WHATWG 一律按 IPv4 解析
    f"ws://{0x7F000001}/",  # 十进制整数形式的 127.0.0.1
    "ws://127.1/",
    "ws://0x7f.0.0.1/",
    "ws://0177.0.0.1/",
    "ws://010.0.0.1/",
    "ws://127.0.0.1./",
    "ws://256.0.0.1/",
    "ws://1.2.3/",
    "ws://1.2.3.4.5/",
    "ws://203.0.113.04/",
    # 带 zone id / 非法 IPv6：WHATWG 与 urlparse 的结论不看正文
    "ws://[::1%25eth0]/",
    "ws://[::1%eth0]/",
    "ws://[zzzz::1]/",
    "ws://[::1/",
    # 非 ASCII 主机：UTS46、IDNA 2003、IDNA 2008 三方各异
    "ws://ｅｘａｍｐｌｅ．ｃｏｍ/",
    "ws://example。com/",
    "ws://straße.example/",
    # 空白与控制字符：WHATWG 删制表符 / 换行，其他解析器不删
    "ws://exa\tmple.com/",
    "ws://example.com\t/",
    "ws://example.com\n/",
    "ws:// example.com/",
    " ws://example.com/",
    "ws://example.com/ ",
    "ws://ex ample.com/",
    # 端口：空端口、越界、非数字、负数、多个冒号
    "ws://example.com:/",
    "ws://example.com:65536/",
    "ws://example.com:0/",
    "ws://example.com:8a/",
    "ws://example.com:-1/",
    "ws://example.com:80:80/",
    "ws://example.com:123456/",
    # 空域名标签
    "ws://.example.com/",
    "ws://example..com/",
]
"""形状有歧义或不受支持的地址：一律拒绝，不去猜它指向哪里"""

ACCEPTED_URLS: List[Tuple[str, Tuple[str, str, Optional[int]]]] = [
    ("ws://public.example/", ("ws", "public.example", None)),
    ("WS://PUBLIC.EXAMPLE:8080/p?q=1#f", ("ws", "public.example", 8080)),
    ("wss://a_b.example", ("wss", "a_b.example", None)),
    ("ws://xn--zca.example/", ("ws", "xn--zca.example", None)),
    ("http://example.com:8443/path", ("http", "example.com", 8443)),
    ("https://example.com", ("https", "example.com", None)),
    ("ws://203.0.113.8:3001/", ("ws", "203.0.113.8", 3001)),
    ("ws://0.0.0.0/", ("ws", "0.0.0.0", None)),
    ("ws://[2001:db8::1111]:443/", ("ws", "[2001:db8::1111]", 443)),
    ("ws://[::ffff:203.0.113.8]/", ("ws", "[::ffff:203.0.113.8]", None)),
    ("ws://example.com./", ("ws", "example.com.", None)),
    ("ws://example.com?a=@b", ("ws", "example.com", None)),
    ("ws://example.com#@10.0.0.5/", ("ws", "example.com", None)),
    ("ws://example.com/a@b", ("ws", "example.com", None)),
    ("ws://example.com:0080/", ("ws", "example.com", 80)),
    ("ws://host-1.example:1/", ("ws", "host-1.example", 1)),
    ("ws://example.com/路径?名=值", ("ws", "example.com", None)),
]
"""任何解析器都会得出同一主机的形状：放行，协议与主机转小写、末尾点保留"""

SUPPLEMENTARY_URLS: List[str] = [
    # B 表之外、会让 urlparse 与 WHATWG 分叉的地址，用来把差分用例的语料补齐
    "ws://public.example@10.0.0.5/",
    "ws://%31%30%2e0%2e0%2e5/",
    "ws://10.0.0.5%2e/",
    "ws://127.1/",
    "ws://0177.0.0.1/",
    "ws://010.0.0.1/",
    "ws://ｅｘａｍｐｌｅ．ｃｏｍ/",
    "ws://straße.example/",
    "ws://example。com/",
    "ws://exa\tmple.com/",
]

CORPUS: List[str] = list(
    dict.fromkeys([*REJECTED_URLS, *(url for url, _ in ACCEPTED_URLS), *SUPPLEMENTARY_URLS]),
)
"""A、B 两表加补充地址，去重后按加入顺序组成 WHATWG 差分语料"""

_DEFAULT_PORTS: Dict[str, int] = {"ws": 80, "wss": 443, "http": 80, "https": 443}

_NODE_WHATWG_SCRIPT = """
const L = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = L.map(u => { try { const x = new URL(u); return { h: x.hostname, p: x.port }; } catch (e) { return { err: true }; } });
console.log(JSON.stringify(out));
"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _entry(
    url: str,
    entry_type: NetworkEntryType = NetworkEntryType.WS_CLIENT,
    *,
    name: str = "probe",
    managed_by_nekro: bool = False,
) -> NetworkEntry:
    return NetworkEntry(
        type=entry_type,
        name=name,
        url=url,
        managed_by_nekro=managed_by_nekro,
    )


def _strip_ipv6_brackets(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _as_ipv6(host: str) -> Optional[ipaddress.IPv6Address]:
    try:
        return ipaddress.IPv6Address(_strip_ipv6_brackets(host.lower()))
    except ValueError:
        return None


def _same_host(left: Optional[str], right: str) -> bool:
    """两边的主机是否是同一个（IPv6 只差序列化形态时按地址值比较）"""
    if left is None:
        return False
    left_ip = _as_ipv6(left)
    right_ip = _as_ipv6(right)
    if left_ip is not None or right_ip is not None:
        return left_ip is not None and right_ip is not None and left_ip == right_ip
    return left.lower() == right.lower()


@pytest.fixture
def recorded_probes(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """把 `assert_download_url_safe` 换成记录器，返回它收到的探测地址列表"""
    probes: List[str] = []

    async def _recorder(url: str) -> None:
        probes.append(url)

    monkeypatch.setattr(container_manager, "assert_download_url_safe", _recorder)
    return probes


@pytest.fixture(scope="module")
def whatwg_results() -> List[Dict[str, Any]]:
    """一次把整份语料交给 node 的 `new URL()`，返回 WHATWG 解析结果"""
    if shutil.which("node") is None:
        pytest.skip("没有 node，跳过与 WHATWG 的差分")
    completed = subprocess.run(
        ["node", "-e", _NODE_WHATWG_SCRIPT],
        input=json.dumps(CORPUS),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=30,
    )
    results: List[Dict[str, Any]] = json.loads(completed.stdout)
    assert len(results) == len(CORPUS), "node 返回的条数与语料对不上"
    return results


# ---------------------------------------------------------------------------
# A. 形状歧义 / 不受支持的地址逐条拒绝
# ---------------------------------------------------------------------------


class TestSplitRejectsAmbiguousShapes:
    @pytest.mark.parametrize("url", REJECTED_URLS)
    def test_rejected(self, url: str) -> None:
        with pytest.raises(ValueError):
            container_manager._split_outbound_client_url(url)


# ---------------------------------------------------------------------------
# B. 所有解析器结论一致的形状逐条放行
# ---------------------------------------------------------------------------


class TestSplitAcceptsUnambiguousShapes:
    @pytest.mark.parametrize(("url", "expected"), ACCEPTED_URLS)
    def test_accepted(self, url: str, expected: Tuple[str, str, Optional[int]]) -> None:
        assert container_manager._split_outbound_client_url(url) == expected


# ---------------------------------------------------------------------------
# C. 出站校验的行为
# ---------------------------------------------------------------------------


class TestOutboundUrlValidation:
    @pytest.mark.parametrize(
        ("url", "entry_type", "expected_probe"),
        [
            (
                "ws://public.example:3001/onebot/ws?x=1",
                NetworkEntryType.WS_CLIENT,
                "http://public.example:3001/",
            ),
            ("wss://PUBLIC.example/a", NetworkEntryType.WS_CLIENT, "https://public.example/"),
            ("http://example.com/hook", NetworkEntryType.HTTP_CLIENT, "http://example.com/"),
            (
                "ws://[2001:db8::1111]:443/x",
                NetworkEntryType.WS_CLIENT,
                "http://[2001:db8::1111]:443/",
            ),
        ],
    )
    async def test_probe_url_is_rebuilt_from_split_parts(
        self,
        recorded_probes: List[str],
        url: str,
        entry_type: NetworkEntryType,
        expected_probe: str,
    ) -> None:
        """交给公网校验的只能是拆出来的主机和端口，路径 / 查询 / 片段不参与"""
        await container_manager._assert_outbound_url_safe(_entry(url, entry_type), 0)
        assert recorded_probes == [expected_probe]

    @pytest.mark.parametrize("url", REJECTED_URLS)
    async def test_rejected_url_never_reaches_probe(self, recorded_probes: List[str], url: str) -> None:
        with pytest.raises(ValidationError) as excinfo:
            await container_manager._assert_outbound_url_safe(_entry(url), 0)
        assert "不被允许" in str(excinfo.value)
        assert recorded_probes == []

    async def test_backslash_userinfo_gap_is_closed(self, recorded_probes: List[str]) -> None:
        """原始缺口：`urlparse` 看到 public.example、NapCat（WHATWG）实际连 10.0.0.5

        改造前这里会把 `ws://10.0.0.5\\@public.example/` 原样交给下载校验，校验解析出的
        主机是 `public.example`（公网）从而放行，而 NapCat 按 WHATWG 取反斜杠之后的主机
        `10.0.0.5`，于是协议端连进了内网。
        """
        with pytest.raises(ValidationError) as excinfo:
            await container_manager._assert_outbound_url_safe(_entry("ws://10.0.0.5\\@public.example/"), 0)
        assert "不被允许" in str(excinfo.value)
        assert recorded_probes == []

    @pytest.mark.parametrize(
        ("url", "entry_type"),
        [
            ("http://example.com/", NetworkEntryType.WS_CLIENT),
            ("ws://example.com/", NetworkEntryType.HTTP_CLIENT),
            ("ftp://example.com/", NetworkEntryType.WS_CLIENT),
            ("ftp://example.com/", NetworkEntryType.HTTP_CLIENT),
        ],
    )
    async def test_scheme_type_mismatch_is_rejected(
        self,
        recorded_probes: List[str],
        url: str,
        entry_type: NetworkEntryType,
    ) -> None:
        with pytest.raises(ValidationError) as excinfo:
            await container_manager._assert_outbound_url_safe(_entry(url, entry_type), 0)
        assert "不被允许" in str(excinfo.value)
        assert recorded_probes == []

    async def test_unsafe_probe_becomes_validation_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _reject(_url: str) -> None:
            raise UnsafeDownloadUrlError("探测地址命中非公网地址")

        monkeypatch.setattr(container_manager, "assert_download_url_safe", _reject)
        with pytest.raises(ValidationError) as excinfo:
            await container_manager._assert_outbound_url_safe(_entry("ws://public.example/"), 0)
        message = str(excinfo.value)
        assert "不被允许" in message
        assert "探测地址命中非公网地址" in message

    async def test_managed_entry_skips_validation(self, recorded_probes: List[str]) -> None:
        entry = _entry(
            "ws://nekro_agent:8021/onebot/v11/ws",
            name="ws",
            managed_by_nekro=True,
        )
        await container_manager._assert_outbound_url_safe(entry, 0)
        assert recorded_probes == []

    async def test_server_entry_skips_outbound_validation(self, recorded_probes: List[str]) -> None:
        entry = NetworkEntry(
            type=NetworkEntryType.WS_SERVER,
            name="ws-server",
            host="0.0.0.0",
            port=8022,
        )
        await container_manager._assert_outbound_url_safe(entry, 0)
        assert recorded_probes == []

    async def test_table_entry_with_backslash_gap_is_rejected(self, recorded_probes: List[str]) -> None:
        entries = [_entry("ws://10.0.0.5\\@public.example/", name="evil")]
        with pytest.raises(ValidationError) as excinfo:
            await container_manager._validate_entries(entries)
        assert "不被允许" in str(excinfo.value)
        assert recorded_probes == []

    async def test_table_with_clean_url_passes(self, recorded_probes: List[str]) -> None:
        entries = [_entry("ws://public.example/", name="ok")]
        await container_manager._validate_entries(entries)
        assert recorded_probes == ["http://public.example/"]

    def test_entry_shape_bad_ipv6_raises_validation_error(self) -> None:
        """`urlparse('ws://[::1')` 会抛 ValueError，整表入口必须看到 ValidationError"""
        entry = _entry("ws://[::1", name="bad-ipv6")
        with pytest.raises(ValidationError) as excinfo:
            container_manager._validate_entry_shape(entry, 0)
        assert not isinstance(excinfo.value, ValueError)


# ---------------------------------------------------------------------------
# D. 与 WHATWG（node `new URL()`）的差分
# ---------------------------------------------------------------------------


class TestWhatwgDifferential:
    def test_every_accepted_url_agrees_with_whatwg(self, whatwg_results: List[Dict[str, Any]]) -> None:
        """形状检查放行的地址，WHATWG 必须解析成功，且主机 / 端口与我们拆出的一致"""
        whatwg_by_url = dict(zip(CORPUS, whatwg_results))
        accepted = 0
        for url in CORPUS:
            try:
                scheme, host, port = container_manager._split_outbound_client_url(url)
            except ValueError:
                continue
            parsed = whatwg_by_url[url]
            assert "err" not in parsed, f"{url!r} 被形状检查放行，但 WHATWG 解析失败"
            whatwg_host = parsed["h"]
            if host.startswith("["):
                # IPv6 只比地址值：node 的序列化形态（如 `[::ffff:808:808]`）与我们的不同
                assert ipaddress.IPv6Address(host[1:-1]) == ipaddress.IPv6Address(
                    _strip_ipv6_brackets(whatwg_host),
                ), f"{url!r} 的主机分叉: 我们 {host!r} / WHATWG {whatwg_host!r}"
            else:
                assert whatwg_host.lower() == host, f"{url!r} 的主机分叉: 我们 {host!r} / WHATWG {whatwg_host!r}"
            expected_port = port if port is not None else _DEFAULT_PORTS[scheme]
            whatwg_port = int(parsed["p"]) if parsed["p"] else _DEFAULT_PORTS[scheme]
            assert whatwg_port == expected_port, f"{url!r} 的端口分叉: 我们 {expected_port} / WHATWG {whatwg_port}"
            accepted += 1
        assert accepted == len(ACCEPTED_URLS), f"放行的条数变了：{accepted} != {len(ACCEPTED_URLS)}"

    def test_every_whatwg_divergence_is_rejected(self, whatwg_results: List[Dict[str, Any]]) -> None:
        """两套解析器对主机结论不同的地址，形状检查必须全部拒绝"""
        divergent = 0
        for url, parsed in zip(CORPUS, whatwg_results):
            if "err" in parsed:
                continue
            try:
                py_host = urlparse(url).hostname
            except ValueError:
                # urlparse 自己解析不了的地址，视为没有主机
                py_host = None
            if _same_host(py_host, parsed["h"]):
                continue
            divergent += 1
            with pytest.raises(ValueError):
                container_manager._split_outbound_client_url(url)
        assert divergent >= 10, f"语料只覆盖了 {divergent} 条解析分叉，断言可能空转"
