import asyncio
import contextlib
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import aiodocker
from aiodocker.docker import DockerContainer

from nekro_agent.adapters.onebot_v11.core.channel import (
    is_valid_instance_key,
    parse_instance_key,
)
from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import (
    SANDBOX_PACKAGE_DIR,
    SANDBOX_PIP_CACHE_DIR,
    SANDBOX_SHARED_HOST_DIR,
    USER_UPLOAD_DIR,
    OsEnv,
    deployment_scope,
)
from nekro_agent.models.db_exec_code import DBExecCode, ExecStopType
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.schemas.sandbox import SandboxCodeExtData
from nekro_agent.services.agent.openai import OpenAIResponse
from nekro_agent.services.agent.resolver import ParsedCodeRunData
from nekro_agent.services.instance_concurrency import instance_slot
from nekro_agent.tools.common_util import limited_text_output

from .credential_guard import sandbox_callback_count
from .ext_caller import CODE_PREAMBLE, RPC_UNREACHABLE_TAG, get_api_caller_code
from .network_guard import prepare_sandbox_network, reset_isolation_state

# 主机共享目录

logger = get_sub_logger("sandbox_runtime")
HOST_SHARED_DIR = (
    Path(SANDBOX_SHARED_HOST_DIR) if SANDBOX_SHARED_HOST_DIR.startswith("/") else Path(SANDBOX_SHARED_HOST_DIR).resolve()
)
# 用户上传目录
USER_UPLOAD_DIR = Path(USER_UPLOAD_DIR) if USER_UPLOAD_DIR.startswith("/") else Path(USER_UPLOAD_DIR).resolve()
# 主机pip缓存目录
HOST_PIP_CACHE_DIR = (
    Path(SANDBOX_PIP_CACHE_DIR) if SANDBOX_PIP_CACHE_DIR.startswith("/") else Path(SANDBOX_PIP_CACHE_DIR).resolve()
)
# 主机包目录
HOST_PACKAGE_DIR = Path(SANDBOX_PACKAGE_DIR) if SANDBOX_PACKAGE_DIR.startswith("/") else Path(SANDBOX_PACKAGE_DIR).resolve()

CONTAINER_NAME_PREFIX = "nekro-agent-sandbox-"  # 沙盒容器名前缀
# 沙盒容器标签：用于启动时识别并回收本部署遗留的孤儿容器。
# 用标签而非名字前缀，是为了在同一宿主机上部署多个 NA 实例时不会互相误杀。
SANDBOX_CONTAINER_LABEL = "nekro.sandbox.instance"
CONTAINER_SHARE_DIR = "/app/shared"  # 容器内共享目录 (读写)
CONTAINER_UPLOAD_DIR = "/app/uploads"  # 容器上传目录 (只读)
CONTAINER_WORK_DIR = "/app"  # 容器工作目录
CONTAINER_PIP_CACHE_DIR = "/app/.pip_cache"  # 容器pip缓存目录
CONTAINER_PACKAGE_DIR = "/app/packages"  # 容器包缓存目录


def sandbox_container_owner() -> str:
    """沙盒容器归属标签的取值：本部署的作用域

    这个值同时决定「打什么标签」与「启动时按什么标签回收」，两侧必须是同一个函数算出来的。
    从前它是 `OsEnv.INSTANCE_NAME or "default"` 且在 import 期算一次，于是同宿主上两套
    都没设 `NEKRO_INSTANCE_NAME` 的部署会共用 `default`——`cleanup_sandbox_containers`
    在启动时按这个标签 `kill + delete`，**后启动的一方会把先启动一方正在跑的沙盒容器杀掉**
    （那段代码的注释还写着「同一宿主机上的其他 NA 部署不受影响」，是一句静默失效的承诺）。
    改成 `deployment_scope()` 后两套部署的标签结构上不可能相同。

    调用时读、不在 import 期定死：测试要能 monkeypatch `OsEnv.INSTANCE_NAME` / `DATA_DIR`。
    """
    return deployment_scope()

CODE_FILENAME = "run_script.py.code"  # 要执行的代码文件名
RUN_CODE_FILENAME = "run_script.py"  # 要执行的代码文件名

API_CALLER_FILENAME = "api_caller.py.code"  # 外部 API 调用器文件名
RUN_API_CALLER_FILENAME = "api_caller.py"  # 外部 API 调用器文件名

# 代码运行结束标记
CODE_RUN_END_FLAGS = {
    ExecStopType.NORMAL: "[SANDBOX_RUN_ENDS_WITH_NORMAL]",  # 正常结束 (exit code 0)
    ExecStopType.ERROR: "[SANDBOX_RUN_ENDS_WITH_ERROR]",  # 错误停止 (exit code 非0)
    ExecStopType.TIMEOUT: "[SANDBOX_RUN_ENDS_WITH_TIMEOUT]",  # 超时停止
    ExecStopType.AGENT: "[SANDBOX_RUN_ENDS_WITH_AGENT]",  # 代理停止 (exit code 8)
    ExecStopType.MANUAL: "[SANDBOX_RUN_ENDS_WITH_MANUAL]",  # 手动停止 (exit code 9)
    ExecStopType.MULTIMODAL_AGENT: "[SANDBOX_RUN_ENDS_WITH_MULTIMODAL_AGENT]",  # 多模态代理停止 (exit code 11)
}

EXEC_SCRIPT = f"""
rm -f {CONTAINER_WORK_DIR}/{RUN_CODE_FILENAME} &&
cp {CONTAINER_SHARE_DIR}/{CODE_FILENAME} {CONTAINER_WORK_DIR}/{RUN_CODE_FILENAME} &&
cp {CONTAINER_SHARE_DIR}/{API_CALLER_FILENAME} {CONTAINER_WORK_DIR}/{RUN_API_CALLER_FILENAME} &&
export MPLCONFIGDIR=/app/tmp/matplotlib &&
python {RUN_CODE_FILENAME}
exit_code=$?
if [ $exit_code -eq 0 ]; then
    echo "{CODE_RUN_END_FLAGS[ExecStopType.NORMAL]}"
elif [ $exit_code -eq 8 ]; then
    echo "{CODE_RUN_END_FLAGS[ExecStopType.AGENT]}"
elif [ $exit_code -eq 9 ]; then
    echo "{CODE_RUN_END_FLAGS[ExecStopType.MANUAL]}"
elif [ $exit_code -eq 11 ]; then
    echo "{CODE_RUN_END_FLAGS[ExecStopType.MULTIMODAL_AGENT]}"
else
    echo "{CODE_RUN_END_FLAGS[ExecStopType.ERROR]}"
fi
"""

# 频道沙盒活跃时间记录表
chat_key_sandbox_map: Dict[str, float] = {}

# 频道沙盒容器 ID 记录表
# 只保存容器 ID 而非 DockerContainer 对象：后者持有创建它的 aiodocker 客户端，
# 而该客户端在本次执行结束时就已关闭，之后基于该对象的任何 kill/delete 都会抛
# "Session is closed"，导致所有延迟清理路径实际从未生效
chat_key_sandbox_container_map: Dict[str, str] = {}

# 频道清理任务记录表
chat_key_sandbox_cleanup_task_map: Dict[str, asyncio.Task] = {}

# 后台容器回收任务集合，持有引用避免任务被 GC 提前回收
_pending_container_cleanups: set[asyncio.Task] = set()

# 沙盒并发限制由 `services/instance_concurrency.py` 统一把守。
# 这里原本是一个全局 `asyncio.Semaphore(SANDBOX_MAX_CONCURRENT)`：所有实例挤在同一条
# FIFO 等待队列上，一个实例先塞进几十个等待者就能把其他实例顶到队尾（详见该模块的说明）。
# 现在换成「每实例队列 + 公平轮转 + 每实例并发上限」，全局容量仍取 SANDBOX_MAX_CONCURRENT。

# 无实例标识（非 onebot 适配器或升级前的历史 chat_key）时使用的包目录名，
# 保持与改造前的共享语义一致
SHARED_INSTANCE_PACKAGE_DIR_NAME = "_shared"


class SandboxStartupError(RuntimeError):
    """沙盒容器没能被创建/启动起来

    存在的理由是**可辨识**：容器根本没跑起来与「跑起来了但超时」是完全不同的故障，
    却曾经一起消失在同一个兜底 except 里。上层（`services/message_service.py`）打的是
    `logger.exception`，异常类型与文案就是运维手上唯一的线索，所以这两样都要说人话。
    """


class SandboxImageUnavailableError(SandboxStartupError):
    """沙盒镜像在宿主上不存在，且拉取也没成功"""


# Docker 报「找不到」时用来区分「找不到镜像」与「找不到别的东西（网络等）」的关键词。
# aiodocker 的 `containers.run` 遇到 404 会先尝试 pull 再重试 create，所以走到这里的
# 404 要么是拉取失败，要么根本不是镜像的问题——两者的处置完全不同，必须分开报
_IMAGE_ERROR_KEYWORDS: Tuple[str, ...] = ("image", "manifest", "pull", "not found in registry")


def describe_container_start_failure(error: aiodocker.DockerError, image: str, network_mode: str) -> SandboxStartupError:
    """把 Docker 的报错翻译成一条能直接照着排查的沙盒启动异常

    Args:
        error: aiodocker 抛出的原始错误
        image: 本次使用的沙盒镜像名
        network_mode: 本次使用的容器网络

    Returns:
        待抛出的异常实例（未抛出，由调用方 `raise ... from error`）
    """
    message = str(getattr(error, "message", "") or error)
    lowered = message.lower()
    if error.status == 404 and (image.lower() in lowered or any(word in lowered for word in _IMAGE_ERROR_KEYWORDS)):
        return SandboxImageUnavailableError(
            f"沙盒镜像 `{image}` 在宿主上不存在且拉取失败（Docker {error.status}: {message}）。"
            f"请先 `docker pull {image}`，或把 SANDBOX_IMAGE_NAME 改成本地已有的镜像",
        )
    if error.status == 404:
        return SandboxStartupError(
            f"创建沙盒容器失败：Docker 报找不到所需资源（{message}）。"
            f"本次使用的网络是 `{network_mode}`，镜像是 `{image}`，请确认它们都还在",
        )
    return SandboxStartupError(
        f"创建/启动沙盒容器失败（Docker {error.status}: {message}）。"
        f"镜像 `{image}`，网络 `{network_mode}`",
    )


@dataclass(frozen=True)
class RpcBridgeDiagnosis:
    """「这一轮失败是不是回调链路不通」的判断，以及这个判断有多硬

    分两档是必须的，因为两条判据的**误报率差了一个数量级**，而处置动作（打 ERROR、
    作废隔离规则缓存强制重装）只配得上硬的那一档。

    Attributes:
        reason: 判据描述；不构成怀疑时为空串
        confirmed: 容器**自己说了**连不上（打印了固定标记）。这是零误报的证据：
            那句话只可能由 `ext_caller_code` 里捕获到 `ConnectionError`/`Timeout` 的分支打出来
    """

    reason: str
    confirmed: bool

    def __bool__(self) -> bool:
        return bool(self.reason)


_NO_RPC_BRIDGE_FAILURE = RpcBridgeDiagnosis(reason="", confirmed=False)


def diagnose_rpc_bridge_failure(stop_type: ExecStopType, output_text: str, callback_count: int) -> RpcBridgeDiagnosis:
    """判断这一轮沙盒执行是不是「回调链路不通」

    两条判据，强度完全不同：

    1. **确证**：容器自己打印了 `RPC_UNREACHABLE_TAG`。装了容器侧连接超时之后这就是这类故障的
       常态形态——从「卡满 120 秒的 TIMEOUT」变成「10 秒后的 ERROR」，只盯 TIMEOUT 会正好错过它。
       这句话只可能由捕获到 `ConnectionError`/`Timeout` 的那个分支打出来，因此可以直接据此
       打 ERROR 并作废规则缓存；
    2. **存疑**：容器跑满整个超时窗口且全程零回调。这一条**不能**当确证用：
       `CODE_PREAMBLE` 不做任何回调，模型写的代码在触达第一个插件方法之前就卡住（死循环、
       长耗时计算、慢 `pip install`）时，回调数天然就是 0 —— 与「包被防火墙吞掉」在这三个
       输入上完全无法区分。据此打 ERROR 会把健康的部署指成防火墙故障，据此重装规则会让
       每一次模型死循环都触发一轮 iptables 全量删装（NA 在容器里时每条命令还要起一个临时容器）。
       所以它只降级为一条说清了「也可能只是模型写慢了」的告警。

    降级不丢覆盖：真正的规则改写有两条独立防线接住——判据 1（容器侧连接超时必然打标记），
    以及后台的规则回读自检（`network_guard.schedule_rule_verification`）。

    Args:
        stop_type: 本轮沙盒的退出类型
        output_text: 容器的完整输出（未截断，见 `run_container_with_timeout`）
        callback_count: 本轮到达 `/ext/rpc_exec` 的回调次数

    Returns:
        判断结果；不构成怀疑时 `reason` 为空串
    """
    if RPC_UNREACHABLE_TAG in output_text:
        return RpcBridgeDiagnosis(reason="容器明确报告连不上 NA 的回调端点", confirmed=True)
    if stop_type == ExecStopType.TIMEOUT and callback_count == 0:
        return RpcBridgeDiagnosis(reason="容器跑满整个超时窗口且全程零回调", confirmed=False)
    return _NO_RPC_BRIDGE_FAILURE


def handle_rpc_bridge_diagnosis(diagnosis: RpcBridgeDiagnosis, container_name: str) -> bool:
    """按判据强度决定「怎么报」和「要不要自愈」

    单独成函数是为了让这个决策**可测**：它原本长在 `run_code_in_sandbox` 中段，
    而那个函数要真容器、真数据库才跑得起来，于是「存疑档不该重装规则」这条契约
    在测试里根本没有落点——把 `if diagnosis.confirmed` 改回 `if diagnosis` 也不会有一条测试变红。

    Args:
        diagnosis: `diagnose_rpc_bridge_failure` 的判断
        container_name: 容器名，只用于日志定位

    Returns:
        是否作废了隔离规则缓存（即下一次跑沙盒会强制重装规则）
    """
    if diagnosis.confirmed:
        # 把「现象」与「真因」重新接上：事故当时 NA 侧九个多小时只有 4 条「容器运行超过 120 秒」，
        # 没有任何别的线索，而真因是宿主的包过滤规则被外部改写、回调包被静默吞掉
        logger.error(
            f"沙盒容器 {container_name} 未能回连 NA（{diagnosis.reason}）：沙盒 -> NA 的 RPC 回调链路不通"
            "（宿主包过滤规则被同标记的其他部署/脚本改写、SANDBOX_CHAT_API_URL 指向了容器不可达的地址等）。"
            "已作废隔离规则缓存，下一次运行沙盒将强制重装规则",
        )
        # 廉价的一次自愈机会：规则若是被别的进程按同一标记删掉的，重装就能修好
        reset_isolation_state()
        return True
    if diagnosis:
        # 只到「存疑」为止：这三个输入区分不了「包被吞掉」与「模型在触达第一个插件方法之前
        # 就卡住了」，而后者是日常。指错原因会让日志失去可信度，重装规则会让每一次模型死循环
        # 都触发一轮 iptables 全量删装 —— 两样都比漏报贵。
        # 规则真被改写时由容器侧连接超时（必然打标记）和后台回读自检各自独立兜住
        logger.warning(
            f"沙盒容器 {container_name} {diagnosis.reason}。"
            "若本轮代码本应调用插件方法，请检查沙盒 -> NA 的 RPC 回调链路"
            "（宿主包过滤规则、SANDBOX_CHAT_API_URL 是否指向容器可达的地址）；"
            "若只是模型写了死循环或长耗时代码，这条可以忽略",
        )
    return False


def _sanitize_docker_name_part(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "_", value)
    return sanitized or "unknown"


def build_container_key(from_chat_key: str) -> str:
    """构建会话对应的沙盒容器键

    该键会随沙盒 RPC 回调原样传回服务端，是把回调请求关联回本轮 Agent 运行的唯一线索
    （见 `services/agent/session_token.py`）。因此生成规则必须只有这一处，
    否则两边算出不同的键会导致关联静默失效。

    Args:
        from_chat_key: 会话标识

    Returns:
        沙盒容器键
    """
    return f"sandbox_{_sanitize_docker_name_part(from_chat_key)}"


def _get_instance_package_dir(from_chat_key: str) -> Path:
    """获取该会话所属实例的沙盒动态包目录

    `.packages` 过去被所有会话共享且可写，而沙盒内的 `dynamic_importer` 会把
    `/app/packages` 插到 `sys.path` 最前面：任一会话都能往共享目录写入恶意模块，
    再被其他会话的沙盒直接 import 执行。多实例部署后这就是跨实例代码注入。
    因此按实例切分子目录，让注入面收敛到同一实例内部。

    历史的共享 `.packages` 根目录内容不做删除，仅不再挂载给容器。

    Args:
        from_chat_key: 会话标识

    Returns:
        该实例专属的宿主机包目录
    """
    instance_key = parse_instance_key(from_chat_key)
    # 目录名直接取自 chat_key，必须按实例标识的字符集严格校验后才能拼接：
    # `..` 之类的分段会穿出 .packages 目录，那正是本函数要堵的注入面
    if not instance_key or not is_valid_instance_key(instance_key):
        instance_key = SHARED_INSTANCE_PACKAGE_DIR_NAME
    return HOST_PACKAGE_DIR / instance_key


def _get_instance_pip_cache_dir(from_chat_key: str) -> Path:
    """获取该会话所属实例的 pip 下载缓存目录

    共享可写缓存意味着一个实例可以污染缓存里的 wheel，再被另一个实例装上执行
    （pip 默认不校验哈希），因此按实例切分。

    该目录挂到容器的 `/app/.pip_cache`，镜像里由 `PIP_CACHE_DIR` 指过去（见
    `sandbox/dockerfile`）。缺了那个环境变量时容器以 nobody 运行，pip 会去找
    `$HOME/.cache/pip`，而 nobody 的 HOME 是 `/nonexistent`，缓存被整体关掉，
    这个挂载也就一直是空的。

    Args:
        from_chat_key: 会话标识

    Returns:
        该实例专属的宿主机 pip 缓存目录
    """
    instance_key = parse_instance_key(from_chat_key)
    if not instance_key or not is_valid_instance_key(instance_key):
        instance_key = SHARED_INSTANCE_PACKAGE_DIR_NAME
    return HOST_PIP_CACHE_DIR / _sanitize_docker_name_part(instance_key)


async def _remove_container_by_id(container_id: str) -> None:
    """按容器 ID 强制回收容器

    独立建立 Docker 客户端，不依赖创建容器时那个可能已关闭的客户端会话。

    Args:
        container_id: 容器 ID
    """
    docker = aiodocker.Docker()
    try:
        container = await docker.containers.get(container_id)
        with contextlib.suppress(Exception):
            await container.kill()
        with contextlib.suppress(Exception):
            await container.delete(force=True)
    except Exception as e:
        if "404" in str(e):
            logger.debug(f"沙盒容器已不存在: {container_id}")
        else:
            logger.warning(f"回收沙盒容器失败: {container_id} | {e}")
    finally:
        await docker.close()


def _schedule_container_cleanup(container_id: str) -> None:
    """在后台回收容器

    调用方可能正处于取消流程中，此时直接 await 会被立刻再次取消，容器将永远留在
    宿主机上运行。因此把回收动作放到独立任务里执行。

    Args:
        container_id: 容器 ID
    """
    task = asyncio.create_task(_remove_container_by_id(container_id))
    _pending_container_cleanups.add(task)
    task.add_done_callback(_pending_container_cleanups.discard)


async def limited_run_code(
    code_run_data: ParsedCodeRunData,
    from_chat_key: str,
    output_limit: int = 1000,
    llm_response: Optional[OpenAIResponse] = None,
    chat_message: Optional[ChatMessage] = None,
    ctx: Optional[AgentCtx] = None,
    llm_retry_errors: Optional[list[str]] = None,
) -> Tuple[str, str, int]:
    """限制并发运行代码

    Args:
        code_run_data: 代码执行数据
        from_chat_key: 频道键
        output_limit: 输出限制
        llm_response: LLM 响应
        chat_message: 聊天消息
        ctx: Agent 上下文
        llm_retry_errors: LLM 重试过程中产生的错误信息列表

    Returns:
        Tuple[str, str, int]: 最终输出结果、原始输出结果和退出类型

    Raises:
        InstanceConcurrencyRejected: 本实例排队已满或等待名额超时；调用链必须把它变成
            用户可见的结果，不能吞掉（见 `services/message_service.py`）
    """

    async with instance_slot(from_chat_key):
        return await run_code_in_sandbox(
            code_run_data=code_run_data,
            from_chat_key=from_chat_key,
            output_limit=output_limit,
            llm_response=llm_response,
            chat_message=chat_message,
            ctx=ctx,
            llm_retry_errors=llm_retry_errors,
        )


async def run_code_in_sandbox(
    code_run_data: ParsedCodeRunData,
    from_chat_key: str,
    output_limit: int,
    llm_response: Optional[OpenAIResponse] = None,
    chat_message: Optional[ChatMessage] = None,
    ctx: Optional[AgentCtx] = None,
    llm_retry_errors: Optional[list[str]] = None,
) -> Tuple[str, str, int]:
    """在沙盒容器中运行代码并获取输出"""

    # 记录开始时间
    start_time = time.time()

    generation_time_ms = llm_response.generation_time_ms if llm_response else 0

    # container_key = f'{time.strftime("%Y%m%d%H%M%S")}_{os.urandom(4).hex()}'
    container_key = build_container_key(from_chat_key)
    container_name = f"{CONTAINER_NAME_PREFIX}{container_key}-{os.urandom(4).hex()}"

    host_shared_dir = Path(HOST_SHARED_DIR / container_key)
    host_shared_dir.mkdir(parents=True, exist_ok=True)

    host_package_dir = _get_instance_package_dir(from_chat_key)
    host_package_dir.mkdir(parents=True, exist_ok=True)
    instance_pip_cache_dir = _get_instance_pip_cache_dir(from_chat_key)
    instance_pip_cache_dir.mkdir(parents=True, exist_ok=True)

    # 共享目录按频道复用、0777 挂进沙盒：上一轮沙盒里的代码能把这两个文件换成指向宿主任意
    # 位置的链接，按路径写就等于以 NA 的身份往那里写模型生成的内容。这里在目录句柄上写临时
    # 文件再 rename（替换的是目录项本身），权限固定 0644，不沿用上一轮留下的那个文件的权限
    from nekro_agent.services.workspace import safe_fs

    # 写入预置依赖代码
    safe_fs.write_text(
        host_shared_dir,
        (API_CALLER_FILENAME,),
        await get_api_caller_code(container_key=container_key, from_chat_key=from_chat_key, ctx=ctx),
        mode=0o644,
        create_dirs=False,
    )

    # 写入要执行的代码
    safe_fs.write_text(
        host_shared_dir,
        (CODE_FILENAME,),
        f"{CODE_PREAMBLE.strip()}\n\n{code_run_data.code_content}",
        mode=0o644,
        create_dirs=False,
    )

    # 设置目录权限
    try:
        Path.chmod(host_shared_dir, 0o777)
        logger.debug(f"设置目录权限: {host_shared_dir} 777")
        Path.chmod(instance_pip_cache_dir, 0o777)
        logger.debug(f"设置目录权限: {instance_pip_cache_dir} 777")
        Path.chmod(host_package_dir, 0o777)
        logger.debug(f"设置目录权限: {host_package_dir} 777")
    except Exception as e:
        logger.error(f"设置目录权限失败: {e}")

    # 清理过期任务
    if from_chat_key in chat_key_sandbox_cleanup_task_map:
        try:
            chat_key_sandbox_cleanup_task_map[from_chat_key].cancel()
            logger.debug(f"清理过期任务: {from_chat_key}")
        except Exception as e:
            logger.error(f"清理过期任务失败: {e}")
        del chat_key_sandbox_cleanup_task_map[from_chat_key]

    # 清理过期沙盒
    stale_container_id = chat_key_sandbox_container_map.pop(from_chat_key, None)
    if stale_container_id:
        await _remove_container_by_id(stale_container_id)
        logger.debug(f"清理过期沙盒: {from_chat_key} | {stale_container_id}")

    # 启动容器
    # 使用 try/finally 确保 Docker 客户端（及其底层 aiohttp UnixConnector）在使用后被正确关闭，
    # 防止连接泄漏导致连接池耗尽后 docker.containers.run() 永久挂起
    docker = aiodocker.Docker()
    container_id = ""
    try:
        # 网络隔离参数必须在容器创建前就绪：strict 档位下这里会抛异常，沙盒直接拒绝启动
        network_plan = await prepare_sandbox_network(docker)
        container: DockerContainer = await _create_sandbox_container(
            docker,
            name=container_name,
            container_config={
                "Image": config.SANDBOX_IMAGE_NAME,
                "Labels": {SANDBOX_CONTAINER_LABEL: sandbox_container_owner()},
                "Cmd": ["bash", "-c", EXEC_SCRIPT],
                "HostConfig": {
                    "Binds": [
                        # pip 缓存**按实例隔离**后可写：投毒面被收敛在同一实例内部，
                        # 而实例是一个 Bot 账号，其沙盒代码本就同源，不构成跨租户风险。
                        # （若改回跨实例共享则必须只读，否则一个实例污染缓存就能让其他实例装到被篡改的包。）
                        # 镜像里的 PIP_CACHE_DIR 指向这个挂载点，见 _get_instance_pip_cache_dir
                        f"{instance_pip_cache_dir}:{CONTAINER_PIP_CACHE_DIR}:rw",
                        # 动态包目录按实例隔离，避免跨实例的沙盒代码注入，见 _get_instance_package_dir
                        f"{host_package_dir}:{CONTAINER_PACKAGE_DIR}:rw",
                        f"{host_shared_dir}:{CONTAINER_SHARE_DIR}:rw",
                        f"{USER_UPLOAD_DIR}/{_sanitize_docker_name_part(from_chat_key)}:{CONTAINER_UPLOAD_DIR}:ro",
                    ],
                    "Memory": 512 * 1024 * 1024,  # 内存限制 (512MB)
                    # CPU 限制 (1 core)。键名必须是 Docker 的规范拼写 `NanoCpus`：Docker 的 JSON 解码大小写不敏感，
                    # 所以旧拼写 `NanoCPUs` 也能生效，但 docker-guard 对请求体键名按精确键白名单校验、
                    # 一律拒绝大小写变体（见 docs/docker-guard.md），写成变体会让硬化部署下的沙盒起不来
                    "NanoCpus": 1000000000,
                    # `no-new-privileges` 阻断容器内通过 setuid/setgid 程序提权。
                    # 它在项目最初是开启的，1.0.0 重构时随着「NA 跑在容器里就整段清空 SecurityOpt」
                    # 一起被注释掉，提交里没有留下任何说明，也没有配套的 issue 或失败记录——
                    # 从改动形态看是连带清理，而非针对它本身的问题。
                    # 沙盒容器以 nobody 运行、执行的只有 python 与 pip，不依赖任何 setuid 程序，
                    # 因此重新启用，并且两条分支都启用：它与 AppArmor 无关，
                    # NA 是否跑在容器里也不改变宿主 Docker 对该选项的处理。
                    "SecurityOpt": (
                        ["no-new-privileges"]
                        if OsEnv.RUN_IN_DOCKER
                        else [
                            "no-new-privileges",  # 禁止提升权限
                            "apparmor=unconfined",  # 禁止 AppArmor 配置
                        ]
                    ),
                    # 沙盒挂在专用网络上，并由宿主侧包过滤规则切断内网访问，见 network_guard。
                    # 容器本身不授予 NET_ADMIN——否则容器内的代码可以自行改掉这些规则。
                    "NetworkMode": network_plan.network_mode,
                    "ExtraHosts": list(network_plan.extra_hosts),
                },
                "User": "nobody",  # 非特权用户
                # 这里曾经还有一个顶层的 `"AutoRemove": True`。`AutoRemove` 是 `HostConfig` 的字段，写在顶层
                # Docker 并不读（只有「请求里没有 HostConfig 键」的旧扁平写法才会读顶层的 HostConfig 字段，
                # 而本请求带着 HostConfig），所以它从来没有生效过；docker-guard 把「顶层出现 HostConfig 字段」
                # 当作扁平写法绕过一律拒绝，保留它会让硬化部署下的沙盒起不来。删除不改变任何 Docker 行为：
                # 沙盒容器的回收仍由显式清理负责（`_remove_container_by_id` / `_schedule_container_cleanup`）。
                # 不要「顺手」把它挪进 HostConfig——那会让容器退出即被删，而退出后 NA 还要读日志与退出码。
            },
            network_mode=network_plan.network_mode,
        )
        container_id = container.id
        chat_key_sandbox_container_map[from_chat_key] = container_id
        logger.debug(f"启动容器: {container_name} | ID: {container_id}")

        # 获取输出和退出类型
        output_text, stop_type = await run_container_with_timeout(
            container,
            config.SANDBOX_RUNNING_TIMEOUT,
        )
    except BaseException:
        # 覆盖 asyncio.CancelledError：取消 Agent 任务时若不回收容器，
        # 模型生成的死循环代码会一直占用 512MB 内存与 1 核 CPU，且没有任何兜底能清掉它
        if container_id:
            _schedule_container_cleanup(container_id)
            chat_key_sandbox_container_map.pop(from_chat_key, None)
        raise
    finally:
        await docker.close()

    handle_rpc_bridge_diagnosis(
        diagnose_rpc_bridge_failure(stop_type, output_text, sandbox_callback_count(container_key)),
        container_name,
    )

    # 记录执行耗时
    exec_time = int((time.time() - start_time) * 1000)  # 转换为毫秒
    # 记录总耗时（生成耗时 + 执行耗时）
    total_time = generation_time_ms + exec_time

    logger.debug(f"容器 {container_name} 输出: {limited_text_output(output_text)} | 退出类型: {stop_type}")

    # 沙盒共享目录超过 30 分钟未活动，则自动清理
    async def cleanup_container_shared_dir(box_last_active_time: float, cleanup_container_id: str) -> None:
        await asyncio.sleep(30 * 60)
        if box_last_active_time != chat_key_sandbox_map.get(from_chat_key):
            return
        try:
            shutil.rmtree(host_shared_dir)
        except Exception as e:
            logger.error(f"清理容器共享目录时发生错误: {e}")
        await _remove_container_by_id(cleanup_container_id)
        # 该频道已闲置，一并清空记录，避免模块级字典随频道数单向增长
        chat_key_sandbox_map.pop(from_chat_key, None)
        chat_key_sandbox_container_map.pop(from_chat_key, None)
        chat_key_sandbox_cleanup_task_map.pop(from_chat_key, None)

    box_last_active_time = time.time()
    chat_key_sandbox_map[from_chat_key] = box_last_active_time
    chat_key_sandbox_cleanup_task_map[from_chat_key] = asyncio.create_task(
        cleanup_container_shared_dir(box_last_active_time, container_id),
    )

    final_output = (
        output_text
        if len(output_text) <= output_limit
        else limited_text_output(
            output_text,
            limit=output_limit,
            placeholder=f"...(output too long, hidden {len(output_text) - output_limit} characters)...",
        )
    )

    # 聊天里登记过服务器时，本轮后续产生的执行记录落盘前要换成占位。没有待清理项时
    # `apply_pending` 只查一次字典、原样返回，行为与改造前逐字一致。
    from nekro_agent.services.ssh_gateway.scrub import apply_pending, scrub_code_files

    # 共享目录里的代码文件要留到 30 分钟后才删：登记服务器的那段代码里就有凭据原文，
    # 容器已经退出，现在就按待清理表重写一遍（没有待清理项时一个文件都不读）
    scrub_code_files(
        from_chat_key,
        [Path(host_shared_dir) / CODE_FILENAME, Path(host_shared_dir) / RUN_CODE_FILENAME],
    )

    scrubbed_code = apply_pending(from_chat_key, code_run_data.code_content)
    scrubbed_thought = apply_pending(
        from_chat_key,
        code_run_data.thought_chain or (llm_response.thought_chain if llm_response else ""),
    )
    scrubbed_output = apply_pending(from_chat_key, final_output)

    await DBExecCode.create(
        chat_key=from_chat_key,
        code_text=scrubbed_code,
        thought_chain=scrubbed_thought,
        outputs=scrubbed_output,
        success=stop_type
        in [
            ExecStopType.NORMAL,
            ExecStopType.AGENT,
            ExecStopType.MULTIMODAL_AGENT,
        ],  # AGENT 状态也视为成功
        stop_type=stop_type,
        use_model=(llm_response and llm_response.use_model) or "",
        exec_time_ms=exec_time,
        generation_time_ms=generation_time_ms,
        total_time_ms=total_time,
        trigger_user_id=str(chat_message.sender_id or "0") if chat_message else "",
        trigger_user_name=chat_message.sender_name if chat_message else "System",
        extra_data=SandboxCodeExtData.create_from_llm_response(llm_response, llm_retry_errors=llm_retry_errors).model_dump_json() if llm_response else "",
    )

    return final_output, output_text, stop_type.value


async def _create_sandbox_container(
    docker: aiodocker.Docker,
    name: str,
    container_config: Dict,
    network_mode: str,
) -> DockerContainer:
    """创建并启动沙盒容器，把 Docker 的原始报错翻译成可辨识的启动异常

    `aiodocker.containers.run` 在 create 收到 404 时会自己去 pull 一次再重试，所以
    「镜像缺失」最终以拉取失败的形态冒出来；不翻译的话它与「网络没了」「守护进程 500」
    在上层长得一模一样，而这几类的处置完全不同。

    Args:
        docker: Docker 客户端
        name: 容器名
        container_config: 容器创建参数
        network_mode: 本次使用的容器网络，仅用于报错文案

    Returns:
        已启动的容器

    Raises:
        SandboxImageUnavailableError: 镜像不存在且拉取失败
        SandboxStartupError: 其余的创建/启动失败
    """
    try:
        return await docker.containers.run(name=name, config=container_config)
    except aiodocker.DockerContainerError as e:
        # 容器建出来了但没起来。`AutoRemove` 只对**跑过**的容器生效，
        # 不主动回收的话每一次启动失败都会在宿主上留一具尸体，直到人工 `docker rm`
        if e.container_id:
            _schedule_container_cleanup(e.container_id)
        raise describe_container_start_failure(e, str(container_config.get("Image") or ""), network_mode) from e
    except aiodocker.DockerError as e:
        raise describe_container_start_failure(e, str(container_config.get("Image") or ""), network_mode) from e


async def run_container_with_timeout(container: DockerContainer, timeout: int) -> Tuple[str, ExecStopType]:
    """运行容器并返回输出结果和退出类型

    容器内的执行脚本本身没有超时机制，因此无论正常结束还是超时，都必须保证容器被回收。
    读取日志可能因 Docker 抖动失败，该失败不得阻断后续的 kill/delete。
    任务被取消时 CancelledError 会向上传播，由调用方负责回收容器。
    """
    timed_out = False
    try:
        await asyncio.wait_for(container.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        logger.warning(f"容器 {container.id} 运行超过 {timeout} 秒，强制停止容器")

    outputs: list[str] = []
    try:
        outputs = await container.log(stdout=True, stderr=True)
    except Exception as e:
        logger.warning(f"读取容器日志失败: {container.id} | {e}")

    if timed_out:
        outputs.append(f"# This container has been killed because it exceeded the {timeout} seconds limit.")
        with contextlib.suppress(Exception):
            await container.kill()
    with contextlib.suppress(Exception):
        await container.delete(force=True)

    output_text = "".join(outputs).strip()

    if timed_out:
        # 移除所有可能的结束标记
        for end_flag in CODE_RUN_END_FLAGS.values():
            output_text = output_text.replace(end_flag, "").strip()
        return output_text, ExecStopType.TIMEOUT

    logger.info(f"容器 {container.id} 运行结束退出")

    # 检查输出中的结束标记来确定退出类型
    stop_type = ExecStopType.ERROR  # 默认为错误退出
    for _type, end_flag in CODE_RUN_END_FLAGS.items():
        if end_flag in output_text:
            stop_type = _type
            output_text = output_text.replace(end_flag, "").strip()
            break
    return output_text, stop_type


async def cleanup_sandbox_containers() -> None:
    """清理本部署遗留的沙盒容器

    仅在**本部署**启动时调用：此时本部署不存在正在执行的沙盒。

    「不会误杀同宿主上的另一套部署」这句承诺由标签取值负责——见 `sandbox_container_owner()`：
    标签从前是 `INSTANCE_NAME or "default"`，两套都没设变量的部署会共用它，
    于是这里的 `kill + delete` 会打到邻居正在跑的沙盒上。现在标签按部署身份派生，撞不上。
    升级前创建的容器带的是旧标签值，不会被本次清理带走，由 AutoRemove 或人工处理。
    """
    docker = aiodocker.Docker()
    cleaned = 0
    owner = sandbox_container_owner()
    try:
        containers = await docker.containers.list(all=True)
        for container in containers:
            try:
                container_info = await container.show()
                labels = (container_info.get("Config") or {}).get("Labels") or {}
                container_display_name = str(container_info.get("Name", "")).lstrip("/")
            except Exception:
                continue
            if labels.get(SANDBOX_CONTAINER_LABEL) != owner:
                continue
            with contextlib.suppress(Exception):
                await container.kill()
            with contextlib.suppress(Exception):
                await container.delete(force=True)
            cleaned += 1
            logger.info(f"已清理遗留沙盒容器 {container_display_name}")
    except Exception as e:
        logger.warning(f"清理遗留沙盒容器失败: {e}")
    finally:
        await docker.close()
    if cleaned:
        logger.info(f"共清理 {cleaned} 个遗留沙盒容器")
