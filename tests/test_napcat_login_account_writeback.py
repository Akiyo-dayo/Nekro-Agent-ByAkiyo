"""内置 NapCat 扫码登录成功后的账号回写

## 修的是什么

上一轮把认领流程补齐之后仍留着一个根因：**没有任何一处代码把登录成功的 QQ 号写回
发起这次登录的那个实例**。于是即便用总后台一键起容器 + 扫码，协议端连回来时
`resolve_instance_key()` 照样查不到绑定，照样自动登记一行 `ob{QQ}` 未认领实例——
一个实例裂成两行，每建一个 Bot 就要手工认领一次。

缺的信息不在数据库里，在容器里：内置 NapCat 的容器是 NA 亲手建的（容器名模板 + 归属标签），
「容器 ↔ 实例」没有歧义，问一句容器自己的 WebUI（`POST /api/QQLogin/GetQQLoginInfo`）
就能补上「容器 ↔ 账号」。

> 该端点的形态取自镜像内 `napcat.mjs`（`ve.getQQLoginInfo()` 返回登录收尾时
> `ve.setQQLoginInfo(selfInfo)` 写入的 `{uid, uin, nick}`），并在 测试部署的
> `nekro_napcat_inst1` 上实测过未登录时的响应：`{"code":0,"data":{"uid":"","uin":"","nick":""}}`。

## 本文件钉住的四件事

**推导正确**：只认「本机托管 + 未绑账号 + 可绑定」的候选，且必须唯一命中；
说不准的一律回落到未认领登记，而不是替用户猜。

**回写安全**：不覆盖已绑定的行、不写进停用/归档的实例、不与既有绑定冲突，
并把状态从 `unbound` 同步回 `offline`（否则掉线监测把它当"不该有连接"永远跳过，
连接事件也会被 `UNMANAGED_STATUSES` 挡掉，实例永远停在"未绑定"）。

**竞态不产生第二行**：回写与自动登记是 `resolve_instance_key()` 里同一个决定的两面，
结构上互斥；推导失败/超时/异常时逐字回到改造前的行为。

**调用点真的接上了**（§2.7 的教训）：零件测得再漂亮，也不能证明它在生产路径上跑得到。
`TestResolveInstanceKeyIsWiredIn` 直接调 `resolve_instance_key()`，只看最终库里有几行实例。
"""

import contextlib
import sys
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11.core import instance as instance_module
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.schemas.console import ContainerInfo, ContainerState
from nekro_agent.services.console import container_manager

ADAPTER = "onebot_v11"
ACCOUNT = "3000000001"
OTHER_ACCOUNT = "1000000008"
# 协议端自行连入时由 `resolve_instance_key()` 生成的标识
AUTO = f"ob{ACCOUNT}"
# 总后台建的内置 NapCat 实例（现场就叫 inst1 / inst2）
BUILTIN = "inst1"
OTHER_BUILTIN = "inst2"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    instance_module.clear_instance_cache()
    try:
        yield
    finally:
        instance_module.clear_instance_cache()
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch: pytest.MonkeyPatch):
    """把重试间隔归零：轮次由列表长度决定，不靠真实时间流逝

    `claim_account_from_builtin_login` 只在 `if delay:` 为真时才 sleep，因此置 0 等于
    「保留全部重试轮次、不产生任何等待」，用例仍然确定性地覆盖到重试逻辑本身。
    """
    monkeypatch.setattr(container_manager, "LOGIN_ATTRIBUTION_RETRY_DELAYS", (0.0, 0.0))


async def seed_instance(
    instance_key: str,
    *,
    provider: str = "builtin_napcat",
    account: str = "",
    status: str = "unbound",
    enabled: bool = True,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=instance_key,
        provider=provider,
        provider_account_id=account,
        status=status,
        enabled=enabled,
    )


def install_prober(
    monkeypatch: pytest.MonkeyPatch,
    answers: Dict[str, Any],
    calls: Optional[List[str]] = None,
) -> None:
    """替换「问容器登录的是谁」这一步

    `answers` 按 `instance_key` 给出答案：字符串表示容器答出的账号（空串 = 答不上），
    异常实例表示这次探测失败。`calls` 用于观察探测轮次。
    """

    async def fake_read(instance: DBAdapterInstance) -> str:
        if calls is not None:
            calls.append(instance.instance_key)
        answer = answers.get(instance.instance_key, "")
        if isinstance(answer, BaseException):
            raise answer
        return str(answer)

    monkeypatch.setattr(container_manager, "read_container_login_account", fake_read)


class TestReadLoginUin:
    """WebUI 响应 → 账号号码的解析

    `uin` 声明成 `Union[int, str]` 不是保守：NapCat 内部写的是 `t.uin.toString()`，
    说明上游那份 `selfInfo.uin` 可以是数字，按 `str` 严格校验会整条解析失败、
    于是「登录成功」永远推导不出来，而现象只是"还是裂成两行"，没有任何报错。
    """

    @pytest.mark.parametrize(
        ("envelope", "expected"),
        [
            pytest.param(None, "", id="WebUI 无响应"),
            pytest.param({"code": -1, "data": {"uin": ACCOUNT}}, "", id="业务码非 0"),
            pytest.param({"code": 0, "data": None}, "", id="data 不是对象"),
            pytest.param({"code": 0, "data": {"uid": "", "uin": "", "nick": ""}}, "", id="未登录（实测响应）"),
            pytest.param({"code": 0, "data": {"uid": "u_x", "uin": ACCOUNT, "nick": "n"}}, ACCOUNT, id="已登录字符串"),
            pytest.param({"code": 0, "data": {"uin": int(ACCOUNT)}}, ACCOUNT, id="已登录数字"),
            pytest.param({"code": 0, "data": {"uin": f"  {ACCOUNT} "}}, ACCOUNT, id="两侧空白"),
            pytest.param({"code": 0, "data": {"uin": "not-a-number"}}, "", id="非数字一律判空"),
            pytest.param({"code": 0, "data": {"uin": None}}, "", id="uin 为 null"),
        ],
    )
    async def test_extract(self, monkeypatch: pytest.MonkeyPatch, envelope, expected: str) -> None:
        async def fake_call(*_args, **_kwargs):
            if envelope is None:
                return None
            return container_manager._NapCatEnvelope.model_validate(envelope)  # noqa: SLF001

        monkeypatch.setattr(container_manager, "_webui_call", fake_call)
        got = await container_manager._read_login_uin(None, "", "")  # type: ignore[arg-type]  # noqa: SLF001
        assert got == expected

    async def test_non_builtin_instance_is_never_probed(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """远程协议端没有可问的容器，必须在建会话之前就返回，而不是去连一个不存在的 WebUI"""
        opened = False

        @contextlib.asynccontextmanager
        async def fake_session(*_args, **_kwargs) -> AsyncIterator[Optional[Tuple[Any, str, str]]]:
            nonlocal opened
            opened = True
            yield None

        monkeypatch.setattr(container_manager, "_webui_session", fake_session)
        remote = await seed_instance("remote1", provider="remote_onebot")

        assert await container_manager.read_container_login_account(remote) == ""
        assert opened is False


class TestBindLoginAccount:
    """回写本身的边界"""

    async def test_binds_and_syncs_status_back_to_offline(self, db) -> None:
        """回写必须同时把 `unbound` 落回 `offline`

        不同步的话这一行仍在 `UNMANAGED_STATUSES` 里：掉线监测跳过它，
        `on_bot_connect` 的 `report_connect` 也被挡掉，实例永远显示"未绑定"。
        """
        instance = await seed_instance(BUILTIN)

        assert await container_manager.bind_login_account(instance, ACCOUNT) is True

        await instance.refresh_from_db()
        assert instance.provider_account_id == ACCOUNT
        assert instance.status == "offline"

    async def test_is_idempotent_for_the_same_account(self, db) -> None:
        instance = await seed_instance(BUILTIN, account=ACCOUNT, status="offline")

        assert await container_manager.bind_login_account(instance, ACCOUNT) is True
        assert await DBAdapterInstance.filter(provider_account_id=ACCOUNT).count() == 1

    async def test_idempotent_path_still_repairs_a_stale_unbound_status(self, db) -> None:
        """账号已经在这一行、状态却还停在 `unbound` —— 幂等分支必须把它修回来

        这行数据自相矛盾但真实可达：适配器自己的绑定流程会直接写账号列而不动状态
        （`resolve_instance_status` 的注释点名了这种行），两条回写路径（扫码轮询与入站推导）
        并发时也可能一条写完账号还没来得及同步状态。停在 `unbound` 的代价是这一行落在
        `UNMANAGED_STATUSES` 里：掉线监测跳过它，连接事件也改不动它，实例永远显示"未绑定"。

        钉住这条的原因是变异测试：把幂等分支里的 `sync_account_binding_status` 删掉，
        原有 42 个用例**全绿**。
        """
        instance = await seed_instance(BUILTIN, account=ACCOUNT, status="unbound")

        assert await container_manager.bind_login_account(instance, ACCOUNT) is True

        await instance.refresh_from_db()
        assert instance.status == "offline"

    async def test_writeback_invalidates_the_inbound_routing_cache(self, db) -> None:
        """绑定关系一变就必须清 `账号 -> 实例` 缓存，与 `update_instance` / 认领同一条规矩

        这个缓存是入站路由的第一跳且**没有 TTL**：留着一条旧映射，该账号的消息会一直投给
        旧实例直到进程重启，而且一条告警都没有。扫码轮询端点是在 HTTP 请求里写库的第三个
        写入方，它同样得守这条规矩。

        同样是变异测试逼出来的：删掉 `clear_instance_cache()` 后原有 42 个用例全绿。
        """
        instance = await seed_instance(BUILTIN)
        # 手工种一条陈旧映射，模拟"这个账号此前被解析到别的实例上过"
        instance_module._account_to_instance["9999"] = "stale_instance"  # noqa: SLF001
        instance_module._instance_to_account["stale_instance"] = "9999"  # noqa: SLF001

        assert await container_manager.bind_login_account(instance, ACCOUNT) is True

        assert instance_module.lookup_cached_account_id("stale_instance") is None
        assert instance_module._account_to_instance == {}  # noqa: SLF001

    async def test_never_overwrites_another_account_on_the_same_row(self, db) -> None:
        """一个实例只能绑一个账号；改绑是管理员的动作，不是自动回写该替他做的事"""
        instance = await seed_instance(BUILTIN, account=OTHER_ACCOUNT, status="offline")

        assert await container_manager.bind_login_account(instance, ACCOUNT) is False

        await instance.refresh_from_db()
        assert instance.provider_account_id == OTHER_ACCOUNT

    @pytest.mark.parametrize(
        ("status", "enabled"),
        [
            pytest.param("disabled", False, id="已停用"),
            pytest.param("archived", False, id="已归档"),
            pytest.param("deleted", False, id="软删墓碑"),
            pytest.param("offline", False, id="仅 enabled=False 的脏行"),
        ],
    )
    async def test_refuses_instances_whose_gate_is_closed(self, db, status: str, enabled: bool) -> None:
        """把正在收消息的协议端绑到收发闸门关着的实例上 = 这条 Bot 当场彻底静默，且界面无提示"""
        instance = await seed_instance(BUILTIN, status=status, enabled=enabled)

        assert await container_manager.bind_login_account(instance, ACCOUNT) is False

        await instance.refresh_from_db()
        assert instance.provider_account_id == ""

    async def test_refuses_when_the_account_is_held_by_another_instance(self, db) -> None:
        """判据与创建/改绑侧同源：两行绑同一账号会让入站路由失去唯一答案"""
        await seed_instance(AUTO, provider="onebot_v11", account=ACCOUNT, status="unclaimed")
        instance = await seed_instance(BUILTIN)

        assert await container_manager.bind_login_account(instance, ACCOUNT) is False

        await instance.refresh_from_db()
        assert instance.provider_account_id == ""
        assert await DBAdapterInstance.filter(provider_account_id=ACCOUNT).count() == 1

    async def test_reads_the_row_again_before_writing(self, db) -> None:
        """调用方手上的快照可能是几百毫秒前的（推导要遍历候选、还会重试）

        这期间管理员完全可能在总后台把账号手工填上。拿旧快照直接 save 会把它覆盖掉，
        而 `provider_account_id` 恰恰是入站路由的唯一依据。
        """
        stale = await seed_instance(BUILTIN)
        # 模拟并发写入：库里已经绑上了别的账号，而调用方手上还是那份空快照
        await DBAdapterInstance.filter(id=stale.id).update(provider_account_id=OTHER_ACCOUNT, status="offline")

        assert await container_manager.bind_login_account(stale, ACCOUNT) is False

        fresh = await DBAdapterInstance.get(id=stale.id)
        assert fresh.provider_account_id == OTHER_ACCOUNT

    @pytest.mark.parametrize("account", ["", "   ", "abc", "10a"])
    async def test_rejects_unusable_account_ids(self, db, account: str) -> None:
        instance = await seed_instance(BUILTIN)
        assert await container_manager.bind_login_account(instance, account) is False


class TestClaimAccountFromBuiltinLogin:
    """从「哪个容器登录了这个号」推导实例归属"""

    async def test_unique_match_is_bound(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        await seed_instance(BUILTIN)
        await seed_instance(OTHER_BUILTIN)
        install_prober(monkeypatch, {BUILTIN: ACCOUNT, OTHER_BUILTIN: ""})

        owner = await container_manager.claim_account_from_builtin_login(ADAPTER, ACCOUNT)

        assert owner is not None
        assert owner.instance_key == BUILTIN
        assert (await DBAdapterInstance.get(instance_key=BUILTIN)).provider_account_id == ACCOUNT
        assert (await DBAdapterInstance.get(instance_key=OTHER_BUILTIN)).provider_account_id == ""

    async def test_no_candidate_returns_none_without_probing(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """全部实例都已绑好账号时（大量 Bot 装完之后的稳态）连一次探测都不该发生"""
        await seed_instance(BUILTIN, account=OTHER_ACCOUNT, status="offline")
        calls: List[str] = []
        install_prober(monkeypatch, {}, calls)

        assert await container_manager.claim_account_from_builtin_login(ADAPTER, ACCOUNT) is None
        assert calls == []

    async def test_remote_and_native_instances_are_not_candidates(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """外部协议端没有「哪个实例发起的」这一信息，它必须继续走未认领 + 认领流程"""
        await seed_instance("inst3", provider="remote_onebot")
        await seed_instance("mail1", provider="native")
        calls: List[str] = []
        install_prober(monkeypatch, {"inst3": ACCOUNT, "mail1": ACCOUNT}, calls)

        assert await container_manager.claim_account_from_builtin_login(ADAPTER, ACCOUNT) is None
        assert calls == []

    async def test_disabled_candidate_is_skipped(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        await seed_instance(BUILTIN, status="disabled", enabled=False)
        calls: List[str] = []
        install_prober(monkeypatch, {BUILTIN: ACCOUNT}, calls)

        assert await container_manager.claim_account_from_builtin_login(ADAPTER, ACCOUNT) is None
        assert calls == []

    async def test_other_adapter_candidates_are_not_considered(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """账号号码按适配器命名空间，别的适配器下的同号实例与这次登录毫无关系"""
        await DBAdapterInstance.create(
            adapter_key="wechat_ilink_multi",
            instance_key="wx1",
            display_name="wx1",
            provider="builtin_napcat",
            provider_account_id="",
            status="unbound",
        )
        install_prober(monkeypatch, {"wx1": ACCOUNT})

        assert await container_manager.claim_account_from_builtin_login(ADAPTER, ACCOUNT) is None

    async def test_ambiguous_match_falls_back_to_unclaimed(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """两个容器同时报同一个号是脏状态，交给人比替他猜好"""
        await seed_instance(BUILTIN)
        await seed_instance(OTHER_BUILTIN)
        install_prober(monkeypatch, {BUILTIN: ACCOUNT, OTHER_BUILTIN: ACCOUNT})

        assert await container_manager.claim_account_from_builtin_login(ADAPTER, ACCOUNT) is None
        assert await DBAdapterInstance.filter(provider_account_id=ACCOUNT).count() == 0

    async def test_candidate_logged_in_as_someone_else_is_not_a_match(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """容器明确答出了别的账号 = 这次登录不是它发起的，且再等也不会变 —— 只探一轮"""
        await seed_instance(BUILTIN)
        calls: List[str] = []
        install_prober(monkeypatch, {BUILTIN: OTHER_ACCOUNT}, calls)

        assert await container_manager.claim_account_from_builtin_login(ADAPTER, ACCOUNT) is None
        assert calls == [BUILTIN]

    async def test_pending_candidate_is_retried(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """NapCat 是**先开反向 WS、后公布登录账号**的（`napcat.mjs` 登录收尾语句的固定顺序）

        因此 NA 第一次问过去可能还答不上。答不上就再问两拍——撞上一次的代价正是这次修复
        要消灭的那一行 `ob{QQ}`。
        """
        await seed_instance(BUILTIN)
        calls: List[str] = []
        answers: Dict[str, Any] = {BUILTIN: ""}

        async def fake_read(instance: DBAdapterInstance) -> str:
            calls.append(instance.instance_key)
            # 第三轮才公布登录账号
            return ACCOUNT if len(calls) >= 3 else str(answers[instance.instance_key])

        monkeypatch.setattr(container_manager, "read_container_login_account", fake_read)

        owner = await container_manager.claim_account_from_builtin_login(ADAPTER, ACCOUNT)

        assert owner is not None
        assert owner.instance_key == BUILTIN
        assert len(calls) == 3

    async def test_probe_failure_is_isolated_to_that_candidate(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """一个容器不可达不该让整轮推导作废——旁边那个照样能被认出来"""
        await seed_instance(BUILTIN)
        await seed_instance(OTHER_BUILTIN)
        install_prober(monkeypatch, {BUILTIN: OSError("connection refused"), OTHER_BUILTIN: ACCOUNT})

        owner = await container_manager.claim_account_from_builtin_login(ADAPTER, ACCOUNT)

        assert owner is not None
        assert owner.instance_key == OTHER_BUILTIN

    async def test_match_that_cannot_be_bound_returns_none(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """推导命中但回写被拒（账号已被别人占着）时不能返回一个没绑上账号的实例

        返回它等于把入站消息投给一个 `provider_account_id` 为空的实例，
        下一次冷查还会重来一遍，而库里那一行始终没有绑定。
        """
        await seed_instance(AUTO, provider="onebot_v11", account=ACCOUNT, status="unclaimed")
        await seed_instance(BUILTIN)
        install_prober(monkeypatch, {BUILTIN: ACCOUNT})

        assert await container_manager.claim_account_from_builtin_login(ADAPTER, ACCOUNT) is None


class TestResolveInstanceKeyIsWiredIn:
    """调用点：只看 `resolve_instance_key()` 跑完之后库里有几行实例

    §2.7 的教训——零件全对但调用点没了，测试会全绿。这一组不碰任何内部函数，
    直接走入站链路的第一步。
    """

    async def test_builtin_login_does_not_create_a_second_row(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """现场时序：某实例发起登录 → 账号连上来。结果必须只有一行实例。"""
        await seed_instance(BUILTIN)
        install_prober(monkeypatch, {BUILTIN: ACCOUNT})

        resolved = await instance_module.resolve_instance_key(ACCOUNT)

        assert resolved == BUILTIN
        assert await DBAdapterInstance.filter(instance_key=AUTO).count() == 0
        assert await DBAdapterInstance.all().count() == 1
        bound = await DBAdapterInstance.get(instance_key=BUILTIN)
        assert bound.provider_account_id == ACCOUNT
        assert bound.status == "offline"

    async def test_second_resolution_hits_the_cache_without_reprobing(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """回写之后这条链路就是普通的已绑定账号，不该再为它付探测代价"""
        await seed_instance(BUILTIN)
        calls: List[str] = []
        install_prober(monkeypatch, {BUILTIN: ACCOUNT}, calls)

        assert await instance_module.resolve_instance_key(ACCOUNT) == BUILTIN
        probes_after_first = len(calls)
        assert await instance_module.resolve_instance_key(ACCOUNT) == BUILTIN

        assert len(calls) == probes_after_first

    async def test_external_protocol_end_still_registers_as_unclaimed(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """用户当前的形态（外部协议端）行为逐字不变：登记成 `ob{QQ}` 等人来认领"""
        await seed_instance(BUILTIN)
        install_prober(monkeypatch, {BUILTIN: ""})

        resolved = await instance_module.resolve_instance_key(ACCOUNT)

        assert resolved == AUTO
        registered = await DBAdapterInstance.get(instance_key=AUTO)
        assert registered.status == "unclaimed"
        assert registered.provider_account_id == ACCOUNT
        assert (await DBAdapterInstance.get(instance_key=BUILTIN)).provider_account_id == ""

    async def test_attribution_failure_degrades_to_the_old_behaviour(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """推导整体炸掉（Docker 不可用之类）也只能是"没改进"，不能是"收不到消息" """
        await seed_instance(BUILTIN)

        async def boom(*_args, **_kwargs):
            raise RuntimeError("docker daemon unreachable")

        monkeypatch.setattr(container_manager, "claim_account_from_builtin_login", boom)

        assert await instance_module.resolve_instance_key(ACCOUNT) == AUTO
        assert (await DBAdapterInstance.get(instance_key=AUTO)).status == "unclaimed"

    async def test_attribution_timeout_degrades_to_the_old_behaviour(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """卡住的容器 WebUI 不能把入站第一步变成不确定的挂起（§2.6 的教训）"""
        await seed_instance(BUILTIN)

        async def hang(*_args, **_kwargs):
            import asyncio

            await asyncio.sleep(3600)

        monkeypatch.setattr(container_manager, "claim_account_from_builtin_login", hang)
        monkeypatch.setattr(instance_module, "LOGIN_ATTRIBUTION_BUDGET_SECONDS", 0.05)

        assert await instance_module.resolve_instance_key(ACCOUNT) == AUTO

    async def test_unimportable_container_manager_degrades_instead_of_breaking_the_bot(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """归属推导那句延迟导入自己失败时，也只能是"没改进"，不能是"收不到消息"

        `container_manager` 会拖进 aiodocker / httpx / console schemas，其中任何一个在某台
        部署上导不进来（缺可选依赖、或本模块被卷进一次循环导入）就是这个形态。
        `matchers/message.py` 调 `resolve_instance_key` 时**没有任何 try/except**，
        所以让 `ImportError` 冲出去 = 这个账号的消息一条都进不来，而且现场只有一条
        与账号毫无关系的 traceback。

        `sys.modules[name] = None` 是这件事的标准复现法：它让 `import name` 抛 ImportError。
        """
        await seed_instance(BUILTIN)
        monkeypatch.setitem(sys.modules, "nekro_agent.services.console.container_manager", None)

        assert await instance_module.resolve_instance_key(ACCOUNT) == AUTO
        assert (await DBAdapterInstance.get(instance_key=AUTO)).status == "unclaimed"

    async def test_already_bound_account_never_triggers_attribution(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """已绑定账号的实例走的是原来那条单行查询，不该多出任何一次容器往返"""
        await seed_instance("inst9", account=ACCOUNT, status="offline")
        calls: List[str] = []
        install_prober(monkeypatch, {}, calls)

        assert await instance_module.resolve_instance_key(ACCOUNT) == "inst9"
        assert calls == []


class TestQrCodePollingWritesBack:
    """扫码弹窗的轮询是「登录成功」最早被观测到的时刻，且发起方毫无歧义

    它覆盖的是「登录成功了、协议端却没连回来」那段窗口——反向 WS 配错时它可以是永远。
    """

    @staticmethod
    def install_webui(
        monkeypatch: pytest.MonkeyPatch,
        *,
        is_login: bool,
        uin: str,
        state: ContainerState = ContainerState.RUNNING,
    ) -> None:
        async def fake_info(_instance: DBAdapterInstance) -> Optional[ContainerInfo]:
            return ContainerInfo(name="nekro_napcat_inst1", state=state)

        @contextlib.asynccontextmanager
        async def fake_session(*_args, **_kwargs) -> AsyncIterator[Optional[Tuple[Any, str, str]]]:
            yield (None, "http://napcat", "cred")

        async def fake_call(_client, _base_url, _method, path: str, _credential, json_body=None):  # noqa: ARG001
            if path.endswith("/CheckLoginStatus"):
                data = {"isLogin": is_login, "qrcodeurl": "" if is_login else "https://txz.qq.com/p?k=x"}
            elif path.endswith("/GetQQLoginInfo"):
                data = {"uid": "u_x", "uin": uin, "nick": "n"}
            else:
                return None
            return container_manager._NapCatEnvelope(code=0, data=data)  # noqa: SLF001

        monkeypatch.setattr(container_manager, "get_container_info", fake_info)
        monkeypatch.setattr(container_manager, "_webui_session", fake_session)
        monkeypatch.setattr(container_manager, "_webui_call", fake_call)

    async def test_logged_in_container_binds_its_account(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = await seed_instance(BUILTIN)
        self.install_webui(monkeypatch, is_login=True, uin=ACCOUNT)

        result = await container_manager.get_login_qrcode(instance)

        assert result.available is False
        assert result.bound_account_id == ACCOUNT
        await instance.refresh_from_db()
        assert instance.provider_account_id == ACCOUNT
        assert instance.status == "offline"

    async def test_not_logged_in_yet_changes_nothing(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = await seed_instance(BUILTIN)
        self.install_webui(monkeypatch, is_login=False, uin="")

        result = await container_manager.get_login_qrcode(instance)

        assert result.available is True
        assert result.bound_account_id == ""
        await instance.refresh_from_db()
        assert instance.provider_account_id == ""
        assert instance.status == "unbound"

    async def test_logged_in_but_account_taken_reports_no_binding(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """账号已经挂在另一行上时不能再造一行绑定，前端也不该报出"已绑定" """
        await seed_instance(AUTO, provider="onebot_v11", account=ACCOUNT, status="unclaimed")
        instance = await seed_instance(BUILTIN)
        self.install_webui(monkeypatch, is_login=True, uin=ACCOUNT)

        result = await container_manager.get_login_qrcode(instance)

        assert result.bound_account_id == ""
        await instance.refresh_from_db()
        assert instance.provider_account_id == ""


class TestQuickLoginWritesBack:
    """快速登录是「登录成功」的第二个触发点，发起方同样毫无歧义

    删容器保留登录态是既定行为（`delete_container` 明说"登录态数据已保留"），所以
    「实例没绑账号、容器里却有可用历史登录态」是真实形态：此时 NA 手上两条边都齐了，
    不回写就是把扫码那半边的修复漏掉一半。
    """

    @staticmethod
    def install_webui(monkeypatch: pytest.MonkeyPatch, *, quick_uin: str, confirmed_uin: str) -> None:
        """`quick_uin` 是历史登录列表里的号，`confirmed_uin` 是登录收尾后 WebUI 答出的号"""

        async def fake_info(_instance: DBAdapterInstance) -> Optional[ContainerInfo]:
            return ContainerInfo(name="nekro_napcat_inst1", state=ContainerState.RUNNING)

        @contextlib.asynccontextmanager
        async def fake_session(*_args, **_kwargs) -> AsyncIterator[Optional[Tuple[Any, str, str]]]:
            yield (None, "http://napcat", "cred")

        async def fake_call(_client, _base_url, _method, path: str, _credential, json_body=None):  # noqa: ARG001
            if path.endswith("/GetQuickLoginListNew"):
                data: Any = [{"uin": quick_uin, "isQuickLogin": True}]
            elif path.endswith("/SetQuickLogin"):
                data = {}
            elif path.endswith("/GetQQLoginInfo"):
                data = {"uid": "u_x", "uin": confirmed_uin, "nick": "n"}
            else:
                return None
            return container_manager._NapCatEnvelope(code=0, data=data)  # noqa: SLF001

        monkeypatch.setattr(container_manager, "get_container_info", fake_info)
        monkeypatch.setattr(container_manager, "_webui_session", fake_session)
        monkeypatch.setattr(container_manager, "_webui_call", fake_call)

    async def test_confirmed_login_is_written_back(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = await seed_instance(BUILTIN)
        self.install_webui(monkeypatch, quick_uin=ACCOUNT, confirmed_uin=ACCOUNT)

        message = await container_manager.quick_login(instance)

        assert ACCOUNT in message
        await instance.refresh_from_db()
        assert instance.provider_account_id == ACCOUNT
        assert instance.status == "offline"

    async def test_login_still_in_flight_writes_nothing(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """`SetQuickLogin` 回 code 0 只表示请求被受理

        照它就绑，一次失败的快速登录会把实例从 `unbound` 推到 `offline`，掉线监测随即开始
        为一个从未上线过的实例告警 —— 那正是 `unbound` 当初被分出来要消灭的误导。
        """
        instance = await seed_instance(BUILTIN)
        self.install_webui(monkeypatch, quick_uin=ACCOUNT, confirmed_uin="")

        await container_manager.quick_login(instance)

        await instance.refresh_from_db()
        assert instance.provider_account_id == ""
        assert instance.status == "unbound"

    async def test_account_held_by_another_instance_is_not_stolen(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """回写共用 `bind_login_account`，账号唯一性判据在这条路径上同样生效"""
        await seed_instance(AUTO, provider="onebot_v11", account=ACCOUNT, status="unclaimed")
        instance = await seed_instance(BUILTIN)
        self.install_webui(monkeypatch, quick_uin=ACCOUNT, confirmed_uin=ACCOUNT)

        await container_manager.quick_login(instance)

        await instance.refresh_from_db()
        assert instance.provider_account_id == ""
        assert await DBAdapterInstance.filter(provider_account_id=ACCOUNT).count() == 1
