"""NA 反连地址的主机名口径：`NEKRO_SELF_HOSTNAME` 显式可配 + 三级回落

## 这份测试在守什么

内置 NapCat 反连 NA 的 `ws://{主机名}:{端口}/onebot/v11/ws` 有两个写入点：
`container_manager.build_managed_entry`（总后台实例配置）与
`adapters/onebot_v11/routers.py:ADAPTER_TEMPLATE` 分支（单实例时代的 `onebot11_{QQ}.json`）。
从前两处都拿 `OsEnv.INSTANCE_NAME` 当主机名，而 compose 里 NA 的容器名是
`${INSTANCE_NAME}nekro_agent` —— 于是「设 `NEKRO_INSTANCE_NAME` 给宿主全局命名空间加前缀」
与「反连地址在容器网络里解析得到 NA 容器」二选一（#47）。

修复把「NA 的主机名」拆成独立的 `NEKRO_SELF_HOSTNAME`，取值统一走
`resolve_self_hostname()` 三级回落。这份用例钉住：

1. 回落本身的三档：SELF_HOSTNAME → INSTANCE_NAME → `nekro_agent`；
2. 两个写入点写的 URL host 都按解析结果拼；
3. 第二个写入点的必坏 URL 回归：INSTANCE_NAME 为空时 host 不再是空串
   （改造前裸读无回落，写出 `ws://:8021/...` 必然连不上）；
4. 默认什么都没配时，产出与改造前口径（`INSTANCE_NAME or "nekro_agent"`）逐字相同。

## 用例的取材原则

- 环境派生值在**调用时**算、monkeypatch 换 `OsEnv` 类属性（§2.9：fake 里写死
  由环境变量派生的值，换台机器就在测另一件事）；
- 「默认不变」不用字面量断言拼凑，而是与改造前公式**逐格对照**（§2.11 对照组思想：
  断言新口径等于旧口径，比断言新口径等于某个字面量更强）。
"""

from __future__ import annotations

import contextlib
import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nekro_agent.adapters.onebot_v11 import routers as onebot_routers
from nekro_agent.core.os_env import OsEnv, resolve_self_hostname
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.services.console import container_manager
from nekro_agent.services.user.perm import Role

# 三级回落的三档取值：各有区分度，任何一档被错拿成另一档断言都会红
HOST_EXPLICIT = "na_a_nekro_agent"  # 显式 NEKRO_SELF_HOSTNAME（compose 多部署形态）
INSTANCE_PREFIX = "na_a_"  # 只有 NEKRO_INSTANCE_NAME（改造前会写出解析不到的 ws://na_a_:8021/...）
HOST_FALLBACK = "nekro_agent"  # 都不配（compose 里 NA 容器名的默认后缀）

# 反连 URL 里端口之前的那一段（host 部分）的截取口径
_URL_HOST_SLICE = slice(len("ws://"), -len(f":{OsEnv.EXPOSE_PORT}/onebot/v11/ws"))


def _legacy_host_formula() -> str:
    """改造前 `build_managed_entry` 的取值口径：`OsEnv.INSTANCE_NAME or "nekro_agent"`

    对照组专用：断言「默认不配 SELF_HOSTNAME 时行为逐字不变」时，拿它与新口径逐格比。
    """
    return OsEnv.INSTANCE_NAME or HOST_FALLBACK


def _platform_owner() -> DBUser:
    """平台方身份。这族端点是 `Role.Super`，构造一个够格的用户直接打函数（与令牌门用例同一写法）"""
    return DBUser(
        username="platform-owner",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
    )


def _unsaved_instance() -> DBAdapterInstance:
    """不需要库的实例对象：`build_managed_entry` 只读元数据字段拼 URL，令牌路径另有用例钉

    不走建实例的库路径 —— 这里盯的是 URL 的 host，令牌取值与本文件无关。
    """
    return DBAdapterInstance(
        adapter_key="onebot_v11",
        instance_key="inst_hostname",
        display_name="主机名口径用例",
        provider="builtin_napcat",
        provider_account_id="10086",
        status="online",
        metadata_json="{}",
    )


# ---------------------------------------------------------------------------
# 1. 三级回落逐档
# ---------------------------------------------------------------------------


class TestResolveSelfHostnameFallback:
    def test_explicit_self_hostname_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """第一档：显式 SELF_HOSTNAME 优先，INSTANCE_NAME 再有值也不参与"""
        monkeypatch.setattr(OsEnv, "SELF_HOSTNAME", HOST_EXPLICIT)
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", INSTANCE_PREFIX)
        assert resolve_self_hostname() == HOST_EXPLICIT

    def test_instance_name_used_when_self_hostname_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """第二档：SELF_HOSTNAME 未配置时沿用 INSTANCE_NAME（= 改造前行为）"""
        monkeypatch.setattr(OsEnv, "SELF_HOSTNAME", "")
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", INSTANCE_PREFIX)
        assert resolve_self_hostname() == INSTANCE_PREFIX

    def test_final_fallback_is_nekro_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """第三档：都不配时回落到 compose 默认容器名后缀，而不是空串"""
        monkeypatch.setattr(OsEnv, "SELF_HOSTNAME", "")
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        assert resolve_self_hostname() == HOST_FALLBACK

    @pytest.mark.parametrize("instance_name", ["", INSTANCE_PREFIX])
    def test_unset_self_hostname_matches_legacy_formula_exactly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        instance_name: str,
    ) -> None:
        """「默认不配时逐字不变」：SELF_HOSTNAME 为空的两格（INSTANCE_NAME 有/无）里，
        新口径逐字等于改造前公式 —— 显式配置了 SELF_HOSTNAME 的格**刻意不在此列**，
        那正是本功能要改的行为（见 `test_explicit_self_hostname_wins`）。"""
        monkeypatch.setattr(OsEnv, "SELF_HOSTNAME", "")
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        assert resolve_self_hostname() == _legacy_host_formula()
        # 产出必须完全由旧公式解释（SELF_HOSTNAME 不掺和）
        assert (resolve_self_hostname() == HOST_FALLBACK) == (instance_name == "")


# ---------------------------------------------------------------------------
# 2. 第一个写入点：build_managed_entry 的反连 URL host
# ---------------------------------------------------------------------------


class TestManagedEntryUrlHost:
    @pytest.mark.parametrize(
        ("self_hostname", "instance_name", "expected_host"),
        [
            (HOST_EXPLICIT, INSTANCE_PREFIX, HOST_EXPLICIT),
            ("", INSTANCE_PREFIX, INSTANCE_PREFIX),
            ("", "", HOST_FALLBACK),
        ],
    )
    def test_url_host_follows_resolution(
        self,
        monkeypatch: pytest.MonkeyPatch,
        self_hostname: str,
        instance_name: str,
        expected_host: str,
    ) -> None:
        """URL host 按三级回落的结果拼 —— 三档各一条，档与档之间取值互不相同"""
        monkeypatch.setattr(OsEnv, "SELF_HOSTNAME", self_hostname)
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        entry = container_manager.build_managed_entry(_unsaved_instance())
        assert entry.url == f"ws://{expected_host}:{OsEnv.EXPOSE_PORT}/onebot/v11/ws"
        # host 部分单独钉住：URL 里 host 与端口之间不允许出现空档（`ws://:8021/...` 形态）
        assert entry.url[_URL_HOST_SLICE] == expected_host

    def test_default_config_output_matches_legacy_formula(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认不配 SELF_HOSTNAME 时，整条 URL 与改造前口径逐字相同（#47 的兼容承诺）"""
        for instance_name in ("", INSTANCE_PREFIX):
            monkeypatch.setattr(OsEnv, "SELF_HOSTNAME", "")
            monkeypatch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
            legacy = f"ws://{_legacy_host_formula()}:{OsEnv.EXPOSE_PORT}/onebot/v11/ws"
            assert container_manager.build_managed_entry(_unsaved_instance()).url == legacy


# ---------------------------------------------------------------------------
# 3. 第二个写入点：ADAPTER_TEMPLATE 分支（单实例时代的 onebot11_{QQ}.json）
# ---------------------------------------------------------------------------


class TestLegacyAdapterFileUrlHost:
    """`restart()` 在适配器文件缺失时按模板补一份 —— 那份里的 URL host 也要走同一口径

    改造前这里裸读 `OsEnv.INSTANCE_NAME` **无回落**：INSTANCE_NAME 为空时写出
    `ws://:8021/onebot/v11/ws`，host 是空串、必然连不上（#47 顺带修掉的真 bug，
    这组用例就是它的回归钉）。
    """

    @staticmethod
    def _prepare(monkeypatch: pytest.MonkeyPatch, config_dir: Path, bot_qq: str) -> Path:
        class _FakeAdapterConfig:
            BOT_QQ = bot_qq

        class _FakeAdapter:
            config = _FakeAdapterConfig()

        @contextlib.asynccontextmanager
        async def _fake_container(_key=None):
            class _C:
                async def restart(self, timeout: int = 30) -> None:
                    return None

            yield _C()

        monkeypatch.setattr(onebot_routers.adapter_utils, "get_typed_adapter", lambda *_a, **_k: _FakeAdapter())
        monkeypatch.setattr(onebot_routers, "get_container", _fake_container)
        monkeypatch.setattr(onebot_routers, "NAPCAT_ONEBOT_ADAPTER_DIR", str(config_dir))
        # 这里只关心 URL host，令牌值与本文件无关，固定住即可。令牌解析本身要查库：本文件不建库，
        # 之前的用例留下的数据库配置会让它现开一个 sqlite 连接、没人关，所以连解析一起换掉
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "irrelevant-to-hostname")

        async def _fixed_token(_self_id: str) -> SimpleNamespace:
            return SimpleNamespace(token="irrelevant-to-hostname")

        monkeypatch.setattr(onebot_routers, "resolve_expected_token", _fixed_token)
        return config_dir / f"onebot11_{bot_qq}.json"

    @staticmethod
    def _written_url(target: Path) -> str:
        return json.loads(target.read_text(encoding="utf-8"))["network"]["websocketClients"][0]["url"]

    async def _written_url_with(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        self_hostname: str,
        instance_name: str,
    ) -> str:
        """在指定的环境档位下走真的 `restart()` 落盘路径，返回模板写出的反连 URL"""
        monkeypatch.setattr(OsEnv, "SELF_HOSTNAME", self_hostname)
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        config_dir = tmp_path / "napcat_config"
        config_dir.mkdir()
        target = self._prepare(monkeypatch, config_dir, bot_qq="10086")
        await onebot_routers.restart(instance_key=None, _current_user=_platform_owner())
        assert target.is_file(), "适配器文件没写出来，测试口径已脱靶"
        return self._written_url(target)

    async def test_empty_instance_name_no_longer_writes_broken_url(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """回归钉：INSTANCE_NAME 为空时 host 是 nekro_agent，不再是 `ws://:8021/...`"""
        url = await self._written_url_with(monkeypatch, tmp_path, self_hostname="", instance_name="")
        assert url == f"ws://{HOST_FALLBACK}:{OsEnv.EXPOSE_PORT}/onebot/v11/ws"
        # 必坏形态的反向断言：host 不允许是空串
        assert url[_URL_HOST_SLICE] == HOST_FALLBACK != ""

    async def test_explicit_self_hostname_is_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """两个写入点同一口径：显式 SELF_HOSTNAME 写进模板的 URL"""
        url = await self._written_url_with(monkeypatch, tmp_path, self_hostname=HOST_EXPLICIT, instance_name="")
        assert url == f"ws://{HOST_EXPLICIT}:{OsEnv.EXPOSE_PORT}/onebot/v11/ws"

    async def test_both_write_points_share_one_host(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """两个写入点在同样的环境档位下写出**同一个** host —— 口径分叉的表现就是协议端连不上"""
        url_from_template = await self._written_url_with(
            monkeypatch, tmp_path, self_hostname=HOST_EXPLICIT, instance_name=INSTANCE_PREFIX
        )
        url_from_managed = container_manager.build_managed_entry(_unsaved_instance()).url
        assert url_from_template == url_from_managed
