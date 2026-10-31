"""沙盒故障的**可辨识性**回归测试

由来是一次真实事故：宿主上的包过滤规则被一个共用同一标记的脚本改写，沙盒回连 NA 的包落进
兜底 DROP。DROP 不回 RST，容器里那次 `requests.post` 于是一直等下去，直到 NA 按运行超时把
容器杀掉——九个多小时里，NA 侧的全部线索只有 4 条「容器运行超过 120 秒」。

真因（回调不通）与现象（超时）之间断了链，而**排查成本几乎全部来自这个断链**。所以这里保护的
契约不是「不出故障」，而是「每一类故障各自说得出自己是谁」：

1. 容器侧的回调有连接超时，且超时时打印带标记的诊断——这是唯一一条无论宿主防火墙怎么坏
   都成立的兜底（`ext_caller_code.py`）；
2. 回调被 NA 拒绝（401/403）与回调根本到不了 NA，是两条不同的报错；
3. 镜像缺失、容器创建失败、Docker 守护进程报错，各自翻译成指名道姓的异常，
   而不是一起消失在上层的兜底 except 里（`runner.describe_container_start_failure`）；
4. 「容器超时 + 全程零回调」能被认出来，用来把「回调链路不通」与「模型写了个死循环」分开
   （`credential_guard` 的回调计数）；
5. 隔离没装上时回落到默认网桥，而不是把容器挂到隔离网络上送进可能残留的 DROP
   （`prepare_sandbox_network`）。

模板下发、异常翻译、计数都是纯逻辑，因此全部用确定性断言，不起真容器、不靠时间流逝。
"""

import ast
import re
from pathlib import Path
from typing import Dict, List, Optional

import aiodocker
import pytest

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv, deployment_scope
from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.sandbox import ext_caller, network_guard, runner
from nekro_agent.services.sandbox.credential_guard import (
    issue_sandbox_call_grant,
    note_sandbox_callback,
    resolve_sandbox_call_grant,
    revoke_sandbox_call_grant,
    sandbox_callback_count,
)
from nekro_agent.services.sandbox.network_guard import (
    LEGACY_PLAN,
    SandboxNetworkIsolationError,
    prepare_sandbox_network,
    reset_isolation_state,
    sandbox_bridge_name,
    sandbox_network_name,
)
from nekro_agent.services.sandbox.runner import (
    SandboxImageUnavailableError,
    SandboxStartupError,
    describe_container_start_failure,
)
from nekro_agent.services.workspace import network_guard as workspace_network_guard

TEMPLATE_PATH = Path(ext_caller.__file__).resolve().with_name("ext_caller_code.py")


class TestContainerSideRpcTimeout:
    """容器侧回调的超时与诊断输出

    没有 `timeout=` 的 `requests.post` 在包被 DROP 时会一直等到内核耗尽 SYN 重传（约 130 秒），
    而沙盒容器早在那之前就被杀了——真因就是这样被吞掉的。
    """

    TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")

    def test_template_post_always_carries_a_timeout(self) -> None:
        """按 AST 判而不是按文本判：这一行的 blame 在上游，后续同步上游时最容易被改回去，
        而缩进/换行怎么变都不该让这条契约失效"""
        posts = [
            node
            for node in ast.walk(ast.parse(self.TEMPLATE))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "post"
        ]
        assert posts, "模板里找不到回调用的 requests.post"
        for call in posts:
            keywords = {keyword.arg for keyword in call.keywords}
            assert "timeout" in keywords, "沙盒回调没有超时，被 DROP 时会一直等到容器被杀"

    def test_connect_timeout_is_a_literal_not_an_injected_value(self) -> None:
        """连接超时不能跟着配置走：它要在「配置本身就是错的」时仍然成立"""
        assert re.search(r"RPC_CONNECT_TIMEOUT_SECONDS = \d+", self.TEMPLATE)

    def test_unreachable_endpoint_prints_a_greppable_tag(self) -> None:
        assert 'RPC_UNREACHABLE_TAG = "[NA-RPC-UNREACHABLE]"' in self.TEMPLATE
        assert "RPC_UNREACHABLE_TAG" in self.TEMPLATE.split("except (")[1]

    def test_connection_and_timeout_errors_are_both_caught(self) -> None:
        """ConnectionError 覆盖「被拒绝」，Timeout 覆盖「被 DROP 成黑洞」，缺一条就漏一半"""
        handler = self.TEMPLATE.split("except (")[1].split(")")[0]
        assert "ConnectionError" in handler
        assert "Timeout" in handler

    def test_rejected_callback_is_a_different_error_than_unreachable(self) -> None:
        """401/403 说明包到了 NA 只是没被认；与「根本没到」的排查方向完全相反"""
        assert "response.status_code in (401, 403)" in self.TEMPLATE
        assert "rejected by NekroAgent" in self.TEMPLATE

    @pytest.mark.parametrize(
        ("running_timeout", "expected"),
        [
            (120, 120),
            (300, 300),
            (10, 10),
            # 配得比一次往返还短、或干脆是 0/负数时兜到下限，避免把能成功的调用掐掉
            (3, 10),
            (0, 10),
            (-1, 10),
        ],
    )
    def test_read_timeout_follows_the_sandbox_budget(
        self,
        monkeypatch: pytest.MonkeyPatch,
        running_timeout: int,
        expected: int,
    ) -> None:
        monkeypatch.setattr(config, "SANDBOX_RUNNING_TIMEOUT", running_timeout)
        assert ext_caller.resolve_rpc_read_timeout() == expected

    def test_generated_code_has_no_placeholder_left(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """占位符没被替换掉的话，容器里 `int("{RPC_READ_TIMEOUT_SECONDS}")` 会在导入期就炸，
        表现又会变成一次没有线索的沙盒失败"""
        monkeypatch.setattr(config, "SANDBOX_RUNNING_TIMEOUT", 120)
        generated = (
            self.TEMPLATE.replace("{CHAT_API}", "http://host.docker.internal:8021/api")
            .replace("{CONTAINER_KEY}", "sandbox_x")
            .replace("{FROM_CHAT_KEY}", "onebot_v11-ob1-group_1")
            .replace("{RPC_SECRET_KEY}", "token")
            .replace("{RPC_READ_TIMEOUT_SECONDS}", str(ext_caller.resolve_rpc_read_timeout()))
        )
        assert "{RPC_READ_TIMEOUT_SECONDS}" not in generated
        assert "RPC_READ_TIMEOUT_SECONDS = int(\"120\")" in generated
        compile(generated, "api_caller.py", "exec")


class TestTemplateLookupIsCwdIndependent:
    """模板路径必须按 `__file__` 定位

    早先用的是相对路径，依赖进程的当前工作目录；从仓库根之外启动 NA 就是 `FileNotFoundError`，
    而它会被 `message_service` 的兜底 except 吞成一行日志——表现是「Bot 不理人」。
    """

    def test_template_path_is_absolute(self) -> None:
        assert ext_caller._EXT_CALLER_CODE_PATH.is_absolute()

    def test_template_is_readable_from_an_unrelated_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert ext_caller._EXT_CALLER_CODE_PATH.read_text(encoding="utf-8").startswith('"""沙盒环境下的扩展方法调用代理"""')

    def test_no_code_line_reads_the_template_by_relative_path(self) -> None:
        source = Path(ext_caller.__file__).read_text(encoding="utf-8")
        code_lines = [line for line in source.splitlines() if not line.strip().startswith("#")]
        assert all('Path("nekro_agent/' not in line for line in code_lines)
        assert "_EXT_CALLER_CODE_PATH.read_text(" in source


class TestSandboxCallbackAccounting:
    """回调计数：用来区分「回调不通」与「模型写了个死循环」"""

    CONTAINER_KEY = "sandbox_test_callback_accounting"
    CHAT_KEY = "onebot_v11-ob1-group_1"

    @pytest.fixture(autouse=True)
    def _clean_grant(self):
        revoke_sandbox_call_grant(self.CONTAINER_KEY)
        yield
        revoke_sandbox_call_grant(self.CONTAINER_KEY)

    def test_a_fresh_grant_starts_at_zero(self) -> None:
        issue_sandbox_call_grant(self.CONTAINER_KEY, self.CHAT_KEY, ["send_msg_text"])
        assert sandbox_callback_count(self.CONTAINER_KEY) == 0

    def test_every_arriving_callback_counts(self) -> None:
        issue_sandbox_call_grant(self.CONTAINER_KEY, self.CHAT_KEY, ["send_msg_text"])
        for expected in (1, 2, 3):
            note_sandbox_callback(self.CONTAINER_KEY)
            assert sandbox_callback_count(self.CONTAINER_KEY) == expected

    def test_a_rejected_callback_still_counts(self) -> None:
        """计的是「包到得了 NA」，不是「这次调用合法」：401 同样证明网络路径是通的，
        把它算成零回调会让自愈逻辑对着完好的防火墙反复重装规则"""
        issue_sandbox_call_grant(self.CONTAINER_KEY, self.CHAT_KEY, ["send_msg_text"])
        note_sandbox_callback(self.CONTAINER_KEY)
        assert resolve_sandbox_call_grant(self.CONTAINER_KEY, "wrong-token", self.CHAT_KEY) is None
        assert sandbox_callback_count(self.CONTAINER_KEY) == 1

    def test_reissuing_resets_the_counter(self) -> None:
        """容器键按频道稳定复用，不清零的话上一轮的回调会让这一轮误判成「通的」"""
        issue_sandbox_call_grant(self.CONTAINER_KEY, self.CHAT_KEY, ["send_msg_text"])
        note_sandbox_callback(self.CONTAINER_KEY)
        issue_sandbox_call_grant(self.CONTAINER_KEY, self.CHAT_KEY, ["send_msg_text"])
        assert sandbox_callback_count(self.CONTAINER_KEY) == 0

    def test_unknown_container_counts_as_zero(self) -> None:
        assert sandbox_callback_count("sandbox_never_issued") == 0

    def test_counting_an_unknown_container_does_not_create_state(self) -> None:
        note_sandbox_callback("sandbox_never_issued")
        assert sandbox_callback_count("sandbox_never_issued") == 0


class TestContainerStartFailureTranslation:
    """容器根本没跑起来 ≠ 跑起来了但超时

    这几类此前一起消失在上层的兜底 except 里，只剩一行「执行失败」。
    """

    IMAGE = "kromiose/nekro-agent-sandbox"
    NETWORK = "nekro_sandbox_net"

    @pytest.mark.parametrize(
        ("status", "message", "expected_type", "must_mention"),
        [
            (
                404,
                "No such image: kromiose/nekro-agent-sandbox:latest",
                SandboxImageUnavailableError,
                "docker pull",
            ),
            (
                404,
                "manifest for kromiose/nekro-agent-sandbox:latest not found",
                SandboxImageUnavailableError,
                "docker pull",
            ),
            (
                404,
                "network nekro_sandbox_net not found",
                SandboxStartupError,
                "nekro_sandbox_net",
            ),
            (
                409,
                "Conflict. The container name is already in use",
                SandboxStartupError,
                "409",
            ),
            (
                500,
                "driver failed programming external connectivity",
                SandboxStartupError,
                "500",
            ),
        ],
    )
    def test_docker_errors_are_translated_into_distinguishable_failures(
        self,
        status: int,
        message: str,
        expected_type: type,
        must_mention: str,
    ) -> None:
        error = describe_container_start_failure(
            aiodocker.DockerError(status, message),
            self.IMAGE,
            self.NETWORK,
        )
        assert isinstance(error, expected_type)
        assert must_mention in str(error)

    def test_image_failure_is_a_dedicated_type(self) -> None:
        """镜像缺失是运维一条命令就能修的，值得单独一个类型好让上层将来能区别对待"""
        assert issubclass(SandboxImageUnavailableError, SandboxStartupError)
        network_error = describe_container_start_failure(
            aiodocker.DockerError(404, "network nekro_sandbox_net not found"),
            self.IMAGE,
            self.NETWORK,
        )
        assert not isinstance(network_error, SandboxImageUnavailableError)

    def test_every_translation_names_the_image_and_the_network(self) -> None:
        for status, message in ((404, "No such image: x"), (500, "boom")):
            text = str(describe_container_start_failure(aiodocker.DockerError(status, message), self.IMAGE, self.NETWORK))
            assert self.IMAGE in text

    def test_failed_start_schedules_container_cleanup(self) -> None:
        """`AutoRemove` 只对**跑过**的容器生效：建出来但没起来的容器不主动回收，
        每一次启动失败都会在宿主上留一具尸体"""
        source = Path(runner.__file__).read_text(encoding="utf-8")
        block = source.split("except aiodocker.DockerContainerError as e:")[1].split("except aiodocker.DockerError")[0]
        assert "_schedule_container_cleanup(e.container_id)" in block

    def test_original_docker_message_is_preserved(self) -> None:
        """翻译不能把原文吃掉：Docker 的原话往往才是真正有用的那半句"""
        text = str(
            describe_container_start_failure(
                aiodocker.DockerError(500, "driver failed programming external connectivity"),
                self.IMAGE,
                self.NETWORK,
            ),
        )
        assert "driver failed programming external connectivity" in text


class _FakeNetwork:
    def __init__(self, detail: Dict) -> None:
        self._detail = detail

    async def show(self) -> Dict:
        return self._detail


class _FakeNetworks:
    def __init__(self, details: Dict[str, Dict]) -> None:
        self._details = details
        self.requested: List[str] = []
        self.created: List[str] = []

    async def get(self, name: str) -> _FakeNetwork:
        self.requested.append(name)
        if name not in self._details:
            raise RuntimeError(f"network {name} not found")
        return _FakeNetwork(self._details[name])

    async def create(self, config: Dict) -> None:
        # 先记账再抛：`best_effort` 档位会把 `_ensure_network` 抛出的**任何**异常当成
        # 「隔离没装上」吞掉并回落到 LEGACY_PLAN，于是「fake 压根没被命中」这件事
        # 不会让断言 LEGACY_PLAN 的那几条变红——它们会以另一个理由继续绿着。
        # 记一笔账，调用方才有办法直接断言这次回落不是因为 fake 脱靶
        self.created.append(str(config.get("Name", "")))
        raise AssertionError("单测里不该真的去建 Docker 网络")


class _FakeSystem:
    def __init__(self, info: Dict) -> None:
        self._info = info

    async def info(self) -> Dict:
        return self._info


class _FakeDocker:
    """只实现 `prepare_sandbox_network` 用到的那几个动作"""

    def __init__(self, details: Dict[str, Dict], info: Optional[Dict] = None) -> None:
        self.networks = _FakeNetworks(details)
        self.system = _FakeSystem(info if info is not None else {"OSType": "windows"})


def _instance_name() -> str:
    """`prepare_sandbox_network` 此刻眼里的作用域

    读的是同一个 `deployment_scope()`（显式 `NEKRO_INSTANCE_NAME` 优先，没设时由
    `NEKRO_DATA_DIR` 派生），而且是**调用时**读，不是 import 时——测试可以
    `monkeypatch.setattr(OsEnv, "INSTANCE_NAME", ...)` 换掉它。

    ⚠️ 不能写成 `OsEnv.INSTANCE_NAME or ""`：那是产品代码 2026-08-12 之前的口径，
    未设变量时它给 `""` 而被测代码给的是派生值，fake 会整个脱靶（§2.9）。
    """
    return deployment_scope()


def _sandbox_network_detail(gateway: Optional[str]) -> Dict:
    ipam: List[Dict[str, str]] = []
    if gateway:
        ipam.append({"Subnet": "172.28.0.0/16", "Gateway": gateway})
    return {
        "Id": "a" * 64,
        # 网桥名同样由实例名派生（`sandbox_bridge_name`），写死等于让 fake 和被测代码各说各话
        "Options": {"com.docker.network.bridge.name": sandbox_bridge_name(_instance_name())},
        "IPAM": {"Config": ipam},
    }


def _sandbox_docker(gateway: Optional[str], info: Optional[Dict] = None) -> _FakeDocker:
    """按**当前环境**算出的网络名建 fake

    网络名是 `sandbox_network_name(OsEnv.INSTANCE_NAME)`，带 `NEKRO_INSTANCE_NAME` 前缀。
    早先这里写死成 `nekro_sandbox_net`，于是**设了该变量再跑这份测试**（服务器上的验证脚本
    正需要设它来避免与生产共用 iptables 标记）时 fake 整个脱靶：`_ensure_network` 查不到网络，
    转去真的 `networks.create`，`strict` 那两条报错文案对不上而变红，`best_effort` 那几条则
    以「建网络失败」为由回落、继续绿着——测的已经不是它们声称的那件事。
    """
    return _FakeDocker({sandbox_network_name(_instance_name()): _sandbox_network_detail(gateway)}, info=info)


class TestIsolationFailureFallsBackToDefaultBridge:
    """隔离没装上时的容器网络

    改动前：`best_effort` 下照样把容器挂到隔离专用网络上，只是打一条告警。而规则**不在关闭时
    移除**，链上完全可能残留着上一次或别的部署装的兜底 DROP——这时把容器挂上去等于送进黑洞，
    结果比 `off` 更糟：沙盒每一次执行都卡到超时。所以必须回落到默认网桥。
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        reset_isolation_state()
        yield
        reset_isolation_state()

    @pytest.fixture()
    def best_effort(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "best_effort")

    async def test_no_iptables_channel_falls_back_to_legacy_plan(
        self,
        monkeypatch: pytest.MonkeyPatch,
        best_effort: None,
    ) -> None:
        monkeypatch.setattr(config, "SANDBOX_CHAT_API_URL", "http://host.docker.internal:8021/api")
        docker = _sandbox_docker("172.28.0.1")
        plan = await prepare_sandbox_network(docker)  # type: ignore[arg-type]
        assert plan == LEGACY_PLAN
        assert plan.network_mode == "bridge"
        assert plan.isolated is False
        assert docker.networks.created == [], "回落的理由必须是「没有 iptables 通道」，不是 fake 脱靶"
        # 光断言「回落了」不够：`prepare_sandbox_network` 把安装段里的**任何**异常都吞成
        # 「隔离没装上」并回落，于是这条断言可以被任何理由满足（§2.9）。把理由本身钉住，
        # 「网络建不出来」（reason 为空，那条路径根本走不到这里）与「端点解析不出来」都会变红
        assert "iptables" in network_guard._last_failure_reason, (
            f"回落理由不是「没有 iptables 通道」而是 {network_guard._last_failure_reason!r}，这条测试测的已经不是它声称的那件事"
        )

    async def test_off_mode_is_byte_for_byte_the_legacy_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "off")
        docker = _sandbox_docker("172.28.0.1")
        assert await prepare_sandbox_network(docker) == LEGACY_PLAN  # type: ignore[arg-type]

    async def test_strict_still_refuses_to_start_the_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "strict")
        monkeypatch.setattr(config, "SANDBOX_CHAT_API_URL", "http://host.docker.internal:8021/api")
        docker = _sandbox_docker("172.28.0.1")
        with pytest.raises(SandboxNetworkIsolationError, match="iptables"):
            await prepare_sandbox_network(docker)  # type: ignore[arg-type]
        assert docker.networks.created == []


class TestTheseFakesAreClosedOverTheInstanceName:
    """fake 的网络名必须跟着 `deployment_scope()` 走

    这一组测的是**测试自己**而不是产品代码，理由是上面那两组的失效方式极不显眼：
    `best_effort` 会把「fake 脱靶 -> 去真的建 Docker 网络 -> AssertionError」当成
    「隔离没装上」吞掉，测试照绿，实际测的却是另一条分支；而设 `NEKRO_INSTANCE_NAME`
    恰恰是本仓库推荐的「跑验证脚本别打瘫生产」的做法（见 §2.6），也就是说这条路一定会被走到。

    2026-08-12 起作用域多了一维：未设 `NEKRO_INSTANCE_NAME` 时由 `NEKRO_DATA_DIR` 派生
    （#42），所以这里两个变量都要参与参数化——只钉住其中一个，另一个上的写死就测不出来。
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        reset_isolation_state()
        yield
        reset_isolation_state()

    @pytest.mark.parametrize("instance_name", ["", "r12verify", "tenant_a-"])
    @pytest.mark.parametrize("data_dir", ["./data/nekro_agent", "/opt/na-b/data/nekro_agent"])
    async def test_the_fake_is_keyed_by_the_name_the_code_computes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        instance_name: str,
        data_dir: str,
    ) -> None:
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        monkeypatch.setattr(OsEnv, "DATA_DIR", data_dir)
        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "best_effort")
        monkeypatch.setattr(config, "SANDBOX_CHAT_API_URL", "http://host.docker.internal:8021/api")
        docker = _sandbox_docker("172.28.0.1")
        await prepare_sandbox_network(docker)  # type: ignore[arg-type]
        # 只钉第一次查询：那一次就是沙盒专用网络。后面还会查一次 `bridge`
        # （`_default_bridge_gateway` 取默认网桥网关），那是另一条契约，不该由这里锁死
        assert docker.networks.requested[:1] == [sandbox_network_name(deployment_scope())]
        assert docker.networks.created == [], "fake 脱靶了：被测代码要的网络名与 fake 提供的对不上"
        # 脱靶时 `_ensure_network` 抛出的异常走的是**另一条** early return（那条根本不写 reason），
        # 所以这一条既钉住了「走到了安装段」，也钉住了「停在没有 iptables 通道那一步」
        assert "iptables" in network_guard._last_failure_reason

    @pytest.mark.parametrize("data_dir", ["./data/nekro_agent", "/opt/na-b/data/nekro_agent"])
    async def test_without_instance_name_the_network_is_no_longer_the_shared_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        data_dir: str,
    ) -> None:
        """未设 `NEKRO_INSTANCE_NAME` 时不能再落到全宿主共用的那张网络上

        这条是 #42 的核心契约：`nekro_sandbox_net` / `nekro-sbx0` / `nekro-sandbox:default`
        以前是**两套部署共用**的一组名字。把 `deployment_scope()` 换回 `OsEnv.INSTANCE_NAME`
        这条就红——而上面那条不会（它两侧用同一个函数算，改一处两侧一起动）。
        """
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        monkeypatch.setattr(OsEnv, "DATA_DIR", data_dir)
        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "best_effort")
        monkeypatch.setattr(config, "SANDBOX_CHAT_API_URL", "http://host.docker.internal:8021/api")
        docker = _sandbox_docker("172.28.0.1")
        await prepare_sandbox_network(docker)  # type: ignore[arg-type]
        assert docker.networks.requested[:1] != ["nekro_sandbox_net"]
        assert network_guard.isolation_marker(deployment_scope()) != "nekro-sandbox:default"


class TestUnresolvableCallbackEndpointIsRefused:
    """回调端点解析不出来时，宁可不装规则

    装一套「只有兜底拒绝、没有 RPC 放行」的规则是**保证**把沙盒打瘫，而且瘫得毫无线索。
    这条把它变成启动/执行前就说得清的错误。
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        reset_isolation_state()
        yield
        reset_isolation_state()

    @pytest.fixture(autouse=True)
    def _no_real_iptables(self, monkeypatch: pytest.MonkeyPatch):
        """这一组的 fake 宿主是 Linux（`OSType: linux`），因此在 Linux 上跑测试时
        `resolve_executor` 会真的探到本机的 iptables 并动宿主规则。契约本身要求在走到
        那一步之前就拒绝，所以这里把执行通道换成一枚地雷：万一将来拒绝的时机被挪后，
        表现是测试变红，而不是一份单测悄悄改写了宿主的包过滤规则"""

        async def _mine(docker):  # noqa: ANN001, ANN202
            raise AssertionError("端点解析不出来时不该走到 iptables，更不该在单测里动宿主规则")

        monkeypatch.setattr(network_guard, "resolve_executor", _mine)

    @pytest.fixture()
    def unresolvable(self, monkeypatch: pytest.MonkeyPatch):
        # 空主机名：不做任何真实 DNS 查询就能得到「解析不出端点」，单测因此不依赖网络
        monkeypatch.setattr(config, "SANDBOX_CHAT_API_URL", "http://")

    def _docker(self) -> _FakeDocker:
        # 网关取不到 -> 候选宿主地址为空 -> 端点集合为空
        return _sandbox_docker(None, info={"OSType": "linux"})

    async def test_strict_reports_the_offending_setting(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unresolvable: None,
    ) -> None:
        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "strict")
        docker = self._docker()
        with pytest.raises(SandboxNetworkIsolationError, match="SANDBOX_CHAT_API_URL"):
            await prepare_sandbox_network(docker)  # type: ignore[arg-type]
        assert docker.networks.created == []

    async def test_best_effort_falls_back_instead_of_installing_a_deny_only_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unresolvable: None,
    ) -> None:
        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "best_effort")
        docker = self._docker()
        plan = await prepare_sandbox_network(docker)  # type: ignore[arg-type]
        assert plan == LEGACY_PLAN
        assert docker.networks.created == [], "回落的理由必须是「端点解析不出来」，不是 fake 脱靶"
        # 上一行只挡掉「fake 脱靶」这一种冒名顶替。`_no_real_iptables` 那枚地雷抛的 AssertionError
        # 同样会被安装段的 `except Exception` 吞掉、同样回落到 LEGACY_PLAN —— 实测：把拒绝时机挪到
        # `resolve_executor` 之后，同组另外两条变红而这一条照绿。理由必须一起钉住
        assert "SANDBOX_CHAT_API_URL" in network_guard._last_failure_reason, (
            f"回落理由不是「端点解析不出来」而是 {network_guard._last_failure_reason!r}"
        )

    async def test_failure_reason_points_at_the_host_side_resolution(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unresolvable: None,
    ) -> None:
        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "best_effort")
        await prepare_sandbox_network(self._docker())  # type: ignore[arg-type]
        assert "host.docker.internal" in network_guard._last_failure_reason


class _ReplayIptables:
    """把预先准备好的 `iptables -S` 文本回放给核对逻辑"""

    description = "replay"

    def __init__(self, chains: Dict[str, str]) -> None:
        self._chains = chains

    async def run(self, family: str, args) -> tuple:  # noqa: ANN001
        if family != "ipv4":
            return 1, "ip6tables unavailable"
        if len(args) == 2 and args[0] == "-S" and args[1] in self._chains:
            return 0, self._chains[args[1]]
        return 1, "no such chain"


class TestBackgroundVerificationSelfHeal:
    """后台回读发现规则被改写之后的完整动作

    这一组测的是接线本身：作废缓存、**清掉失败重试的节流**、留下能进日志的原因。
    节流那一条尤其容易漏——上一次尝试是**成功**的，不清的话下一次跑沙盒会被节流挡住不重装，
    白白多降级一分钟。
    """

    SPEC = network_guard.SandboxNetworkSpec(
        bridge_interface="nekro-sbx0",
        ipv4_subnet="172.28.0.0/16",
        rpc_endpoints=(("172.28.0.1", 8021),),
        marker="nekro-sandbox:selfheal-test",
    )
    SIGNATURE = ("nekro-sbx0", "172.28.0.0/16", "172.28.0.1", "http://host.docker.internal:8021/api")

    @pytest.fixture(autouse=True)
    def _clean(self):
        reset_isolation_state()
        yield
        reset_isolation_state()

    def _install_fake_channel(self, monkeypatch: pytest.MonkeyPatch, chains: Dict[str, str]) -> None:
        class _FakeDockerClient:
            async def close(self) -> None:
                return None

        async def _fake_resolve_executor(docker):  # noqa: ANN001, ANN202
            return _ReplayIptables(chains)

        monkeypatch.setattr(network_guard.aiodocker, "Docker", _FakeDockerClient)
        monkeypatch.setattr(network_guard, "resolve_executor", _fake_resolve_executor)

    def _rendered(self, replace_port: Optional[str] = None) -> Dict[str, str]:
        rules = network_guard.build_isolation_rules(self.SPEC, "ipv4", "DOCKER-USER")
        chains: Dict[str, str] = {}
        for chain in ("DOCKER-USER", "INPUT"):
            lines = [f"-A {chain} " + " ".join(rule.args) for rule in rules if rule.chain == chain]
            text = "\n".join([f"-N {chain}", *lines])
            chains[chain] = text.replace("8021", replace_port) if replace_port else text
        return chains

    def _seed_applied_state(self) -> None:
        network_guard._applied_signature = self.SIGNATURE
        network_guard._applied_spec = self.SPEC
        network_guard._last_attempt_at = 12345.0

    async def test_intact_rules_leave_the_cache_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_channel(monkeypatch, self._rendered())
        self._seed_applied_state()
        await network_guard._verify_applied_sandbox_rules(self.SPEC, self.SIGNATURE)
        assert network_guard._applied_signature == self.SIGNATURE
        assert network_guard._last_attempt_at == 12345.0

    async def test_rewritten_rules_invalidate_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_channel(monkeypatch, self._rendered(replace_port="19311"))
        self._seed_applied_state()
        await network_guard._verify_applied_sandbox_rules(self.SPEC, self.SIGNATURE)
        assert network_guard._applied_signature is None
        assert network_guard._applied_spec is None

    async def test_rewritten_rules_also_clear_the_retry_throttle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_channel(monkeypatch, self._rendered(replace_port="19311"))
        self._seed_applied_state()
        await network_guard._verify_applied_sandbox_rules(self.SPEC, self.SIGNATURE)
        assert network_guard._last_attempt_at == 0.0

    async def test_failure_reason_survives_for_the_next_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_channel(monkeypatch, self._rendered(replace_port="19311"))
        self._seed_applied_state()
        await network_guard._verify_applied_sandbox_rules(self.SPEC, self.SIGNATURE)
        assert "19311" in network_guard._last_failure_reason

    async def test_a_signature_that_moved_on_is_not_clobbered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """核对是异步的，期间配置可能已经改过并重装成功；那时这次核对的结论已经过期"""
        self._install_fake_channel(monkeypatch, self._rendered(replace_port="19311"))
        self._seed_applied_state()
        newer = (*self.SIGNATURE, "changed")
        network_guard._applied_signature = newer
        await network_guard._verify_applied_sandbox_rules(self.SPEC, self.SIGNATURE)
        assert network_guard._applied_signature == newer


class TestVerificationScheduling:
    """回读自检的节流：既要能自愈，也不能把 iptables 调用打成风暴"""

    KEY = "nekro-sandbox:test-scheduling"

    @pytest.fixture(autouse=True)
    def _clean(self):
        network_guard.reset_rule_verification(self.KEY)
        yield
        network_guard.reset_rule_verification(self.KEY)

    async def test_first_call_runs_the_verification(self) -> None:
        ran: List[int] = []

        async def verify() -> None:
            ran.append(1)

        network_guard.schedule_rule_verification(self.KEY, verify)
        task = network_guard._verify_states[self.KEY].task
        assert task is not None
        await task
        assert ran == [1]

    async def test_a_second_call_within_the_interval_is_skipped(self) -> None:
        ran: List[int] = []

        async def verify() -> None:
            ran.append(1)

        network_guard.schedule_rule_verification(self.KEY, verify)
        first = network_guard._verify_states[self.KEY].task
        assert first is not None
        await first
        network_guard.schedule_rule_verification(self.KEY, verify)
        assert network_guard._verify_states[self.KEY].task is first
        assert ran == [1]

    async def test_marking_verified_suppresses_the_next_round(self) -> None:
        """刚装完规则就等于刚核对过，没必要立刻再读一遍"""
        ran: List[int] = []

        async def verify() -> None:
            ran.append(1)

        network_guard.mark_rules_verified(self.KEY)
        network_guard.schedule_rule_verification(self.KEY, verify)
        assert network_guard._verify_states[self.KEY].task is None
        assert ran == []

    async def test_verification_failure_does_not_escape_the_task(self) -> None:
        """后台任务里的异常没人接；让它冒出去只会在日志里刷 `Task exception was never retrieved`"""

        async def verify() -> None:
            raise RuntimeError("iptables busy")

        network_guard.schedule_rule_verification(self.KEY, verify)
        task = network_guard._verify_states[self.KEY].task
        assert task is not None
        await task
        assert task.exception() is None


class TestRpcBridgeFailureDiagnosis:
    """「这一轮失败是回调链路不通」的判据，以及每条判据配得上多重的处置

    两条判据缺一不可：装上容器侧连接超时之后，故障形态从「卡满 120 秒的 TIMEOUT」变成
    「10 秒后的 ERROR」，只盯 TIMEOUT 的判据会正好错过它。

    但两条的**误报率差了一个数量级**，所以强度必须分开记：
    `CODE_PREAMBLE` 不做任何回调，模型的代码在触达第一个插件方法之前就卡住（死循环、
    长耗时计算、慢 `pip install`）时，「超时 + 零回调」天然成立 —— 与「包被防火墙吞掉」
    在这三个输入上完全无法区分。把它当确证用会把健康部署指成防火墙故障，
    并让每一次模型死循环都触发一轮 iptables 全量删装。
    """

    TAG = ext_caller.RPC_UNREACHABLE_TAG

    def test_tag_matches_the_template_verbatim(self) -> None:
        """模板是被当作文本下发的、没法 import，两处各写了一份，对不上就等于这条判据静默失效"""
        assert f'RPC_UNREACHABLE_TAG = "{self.TAG}"' in TEMPLATE_PATH.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        ("stop_type", "output", "callbacks", "should_flag", "confirmed"),
        [
            # 装了容器侧超时之后的常态形态：10 秒后 ERROR 退出，输出里带标记 —— 确证
            (ExecStopType.ERROR, f"{TAG} Cannot reach ...", 0, True, True),
            # 标记出现在一大段输出中间同样要认出来
            (ExecStopType.ERROR, f"hello\n{TAG} Cannot reach ...\nTraceback", 0, True, True),
            # 老形态：跑满超时窗口且全程零回调 —— 只到存疑
            (ExecStopType.TIMEOUT, "", 0, True, False),
            # ↓ 这一条是分档的理由：模型写了个死循环，还没走到第一个插件方法就被超时杀掉。
            #   回调数天然是 0，与「包被吞掉」的三个输入一模一样。必须**不能**被当成确证，
            #   否则健康部署会被指成防火墙故障，而且每次死循环都白重装一轮规则
            (ExecStopType.TIMEOUT, "starting heavy loop", 0, True, False),
            # 死循环发生在调过方法之后 —— 规则显然是好的，连怀疑都不该有
            (ExecStopType.TIMEOUT, "some output", 3, False, False),
            # 正常执行
            (ExecStopType.NORMAL, "done", 1, False, False),
            # 一次回调都没有但正常退出：代码本来就没调任何方法，不构成任何证据
            (ExecStopType.NORMAL, "done", 0, False, False),
            # 代码自己写错了报错退出，与回调无关
            (ExecStopType.ERROR, "NameError: name 'foo' is not defined", 2, False, False),
            (ExecStopType.ERROR, "NameError: name 'foo' is not defined", 0, False, False),
            # Agent 方法正常返回
            (ExecStopType.AGENT, "agent result", 1, False, False),
            # 即便调过方法，只要容器明确报告连不上就算 —— 后半程断掉同样是回调链路故障
            (ExecStopType.ERROR, f"ok\n{TAG} boom", 2, True, True),
        ],
    )
    def test_diagnosis_truth_table(
        self,
        stop_type: ExecStopType,
        output: str,
        callbacks: int,
        should_flag: bool,
        confirmed: bool,
    ) -> None:
        diagnosis = runner.diagnose_rpc_bridge_failure(stop_type, output, callbacks)
        assert bool(diagnosis) is should_flag
        assert diagnosis.confirmed is confirmed

    def test_only_the_container_s_own_report_counts_as_confirmed(self) -> None:
        """确证只能来自容器打印的标记 —— 那句话只可能由捕获到连接失败的分支打出来。
        任何靠「超时 + 计数」推出来的结论都与模型写慢了不可区分，不配触发重装"""
        for stop_type in ExecStopType:
            assert runner.diagnose_rpc_bridge_failure(stop_type, "no tag here", 0).confirmed is False

    def test_reason_text_distinguishes_the_two_shapes(self) -> None:
        """两种形态的排查方向不同，日志里要看得出来是哪一种"""
        explicit = runner.diagnose_rpc_bridge_failure(ExecStopType.ERROR, f"{self.TAG} x", 0)
        silent = runner.diagnose_rpc_bridge_failure(ExecStopType.TIMEOUT, "", 0)
        assert explicit.reason != silent.reason
        assert explicit and silent


class TestOnlyConfirmedFailuresTriggerSelfHeal:
    """自愈动作的门槛

    作废规则缓存意味着下一次跑沙盒要把整套 iptables 规则删掉重装，NA 在容器里时每条命令
    还要起一个临时容器。让「模型写了个死循环」也能触发它，等于给日常操作加了一条又贵又
    指错方向的路径。所以只有容器**自己报告**连不上（零误报）才配得上自愈。
    """

    @pytest.fixture()
    def healed(self, monkeypatch: pytest.MonkeyPatch) -> List[int]:
        """记录 `reset_isolation_state` 被调了几次

        这里换掉的是**观察点**而不是被测逻辑：判据本身、缓存作废本身都各自有自己的测试，
        这一组只问「这个判据该不该触发那个动作」。
        """
        calls: List[int] = []
        monkeypatch.setattr(runner, "reset_isolation_state", lambda: calls.append(1))
        return calls

    def test_confirmed_failure_invalidates_the_rule_cache(self, healed: List[int]) -> None:
        diagnosis = runner.RpcBridgeDiagnosis(reason="容器明确报告连不上", confirmed=True)
        assert runner.handle_rpc_bridge_diagnosis(diagnosis, "nekro-sbx-x") is True
        assert healed == [1]

    def test_mere_suspicion_does_not_touch_the_rule_cache(self, healed: List[int]) -> None:
        """「超时 + 零回调」与「模型在第一个插件方法之前就卡住」不可区分，不配触发重装"""
        diagnosis = runner.RpcBridgeDiagnosis(reason="容器跑满整个超时窗口且全程零回调", confirmed=False)
        assert runner.handle_rpc_bridge_diagnosis(diagnosis, "nekro-sbx-x") is False
        assert healed == []

    def test_a_healthy_run_does_nothing(self, healed: List[int]) -> None:
        assert runner.handle_rpc_bridge_diagnosis(runner.RpcBridgeDiagnosis(reason="", confirmed=False), "x") is False
        assert healed == []

    def test_the_real_dead_loop_shape_end_to_end(self, healed: List[int]) -> None:
        """从原始输入一路走到处置：模型写了死循环、还没走到第一个插件方法就被超时杀掉，
        整条链路上不能出现任何一次规则重装"""
        diagnosis = runner.diagnose_rpc_bridge_failure(ExecStopType.TIMEOUT, "crunching...", 0)
        assert runner.handle_rpc_bridge_diagnosis(diagnosis, "nekro-sbx-x") is False
        assert healed == []

    def test_the_real_blackhole_shape_end_to_end(self, healed: List[int]) -> None:
        """反过来钉住真正的事故形态仍然会自愈"""
        output = f"{ext_caller.RPC_UNREACHABLE_TAG} Cannot reach the NekroAgent callback endpoint"
        diagnosis = runner.diagnose_rpc_bridge_failure(ExecStopType.ERROR, output, 0)
        assert runner.handle_rpc_bridge_diagnosis(diagnosis, "nekro-sbx-x") is True
        assert healed == [1]

    @pytest.fixture()
    def logged(self, monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[str]]:
        """按级别收集日志

        整起事故的成因就是「什么都没说」，所以「存疑档也必须留下一条线索」本身是契约的一部分：
        把那个分支整个删掉不能让测试保持全绿。
        """
        records: Dict[str, List[str]] = {"error": [], "warning": []}
        recorder = type(
            "_Recorder",
            (),
            {
                "error": staticmethod(lambda message: records["error"].append(message)),
                "warning": staticmethod(lambda message: records["warning"].append(message)),
            },
        )
        monkeypatch.setattr(runner, "logger", recorder)
        return records

    def test_confirmed_failure_is_reported_as_an_error(
        self,
        healed: List[int],
        logged: Dict[str, List[str]],
    ) -> None:
        runner.handle_rpc_bridge_diagnosis(runner.RpcBridgeDiagnosis(reason="容器明确报告连不上", confirmed=True), "c")
        assert len(logged["error"]) == 1
        assert not logged["warning"]

    def test_suspicion_is_still_reported_but_only_as_a_warning(
        self,
        healed: List[int],
        logged: Dict[str, List[str]],
    ) -> None:
        """降级不等于噤声：真被防火墙吞掉而容器又没来得及打标记时，这条是唯一的线索"""
        runner.handle_rpc_bridge_diagnosis(runner.RpcBridgeDiagnosis(reason="容器跑满整个超时窗口", confirmed=False), "c")
        assert len(logged["warning"]) == 1
        assert not logged["error"], "存疑档打成 ERROR 会让日志失去可信度"

    def test_a_healthy_run_says_nothing(self, healed: List[int], logged: Dict[str, List[str]]) -> None:
        runner.handle_rpc_bridge_diagnosis(runner.RpcBridgeDiagnosis(reason="", confirmed=False), "c")
        assert not logged["error"]
        assert not logged["warning"]


def _calls_made_inside(source: str, function_name: str) -> set:
    """收集某个函数体内所有被调用的名字（含点号形式）

    单独把「调用点存不存在」立成契约，是因为这一整组修复的每一个零件都能各自测得好好的，
    而只要调用点没了，零件就全部变成死代码、测试却照样全绿 —— 本仓库已经在别处踩过一次
    「把真正的路径整个换掉、于是漏洞在测试里根本不存在」。
    """
    target = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    names: set = set()
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
            parts: List[str] = []
            cursor: ast.expr = func
            while isinstance(cursor, ast.Attribute):
                parts.append(cursor.attr)
                cursor = cursor.value
            if isinstance(cursor, ast.Name):
                parts.append(cursor.id)
                names.add(".".join(reversed(parts)))
    return names


class TestTheFixesAreActuallyWiredIn:
    """每个零件都真的**被调用了**

    上面的每一组都在直接调用被测函数，因此它们全绿只证明零件本身是对的。把调用点删掉
    —— 路由不再计数、runner 不再翻译 Docker 报错、`prepare_sandbox_network` 不再排核对 ——
    那些测试一条都不会红，而整套修复会静默失效。这一组专门堵这个。
    """

    RUNNER_SOURCE = Path(runner.__file__).read_text(encoding="utf-8")
    GUARD_SOURCE = Path(network_guard.__file__).read_text(encoding="utf-8")

    async def test_rpc_route_counts_the_callback_before_authenticating(self) -> None:
        """计数必须发生在鉴权**之前**：它量的是「包到得了 NA」，不是「这次调用合法」。
        排在鉴权之后的话，令牌过期/被拒的回调会被算成零回调，
        「超时 + 零回调」这条判据就会对着一个完好的防火墙反复喊冤"""
        from nekro_agent.routers.rpc import rpc_exec  # noqa: PLC0415  局部导入，避免整份测试依赖路由层

        container_key = "sandbox_wiring_probe"
        issue_sandbox_call_grant(container_key, "onebot_v11-ob1-group_1", ["send_msg_text"])
        assert sandbox_callback_count(container_key) == 0
        with pytest.raises(Exception):  # noqa: B017, PT011  UnauthorizedError 的具体类型不是本条的契约
            await rpc_exec(
                container_key=container_key,
                from_chat_key="onebot_v11-ob1-group_1",
                data=None,  # type: ignore[arg-type]  鉴权失败在读 body 之前，这里不会被用到
                x_rpc_token="a-wrong-token",
            )
        assert sandbox_callback_count(container_key) == 1, "被拒的回调没被计数，判据会把它当成「包没到」"
        revoke_sandbox_call_grant(container_key)

    def test_sandbox_runner_creates_containers_through_the_translator(self) -> None:
        """绕过 `_create_sandbox_container` 的话，镜像缺失/网络没了/守护进程 500
        又会一起消失在上层同一个兜底 except 里"""
        calls = _calls_made_inside(self.RUNNER_SOURCE, "run_code_in_sandbox")
        assert "_create_sandbox_container" in calls
        assert "docker.containers.run" not in calls, "直连 containers.run 会跳过全部报错翻译"

    def test_sandbox_runner_runs_the_diagnosis_and_can_self_heal(self) -> None:
        calls = _calls_made_inside(self.RUNNER_SOURCE, "run_code_in_sandbox")
        assert "diagnose_rpc_bridge_failure" in calls
        assert "sandbox_callback_count" in calls, "不取回调计数，判据的第二个输入就永远是默认值"
        assert "handle_rpc_bridge_diagnosis" in calls, "判断出来了却没有任何处置，等于没判断"
        healing = _calls_made_inside(self.RUNNER_SOURCE, "handle_rpc_bridge_diagnosis")
        assert "reset_isolation_state" in healing, "确证之后没有自愈动作，下一轮还是同样的故障"

    def test_prepare_sandbox_network_schedules_the_readback(self) -> None:
        """这是整套修复的核心：不排核对，`_applied_signature` 就永远只是「我上次装了什么」，
        规则被别人换掉之后 NA 到死都不会知道"""
        calls = _calls_made_inside(self.GUARD_SOURCE, "prepare_sandbox_network")
        assert "schedule_rule_verification" in calls
        assert "mark_rules_verified" in calls, "装完不拨时钟，紧接着就会多做一轮无谓的回读"

    def test_workspace_guard_schedules_the_readback_too(self) -> None:
        """CC 工作区守卫是同款内存缓存，漏掉它就是「只修了一半」"""
        source = Path(workspace_network_guard.__file__).read_text(encoding="utf-8")
        calls = _calls_made_inside(source, "prepare_workspace_network")
        assert "schedule_rule_verification" in calls
        assert "mark_rules_verified" in calls
