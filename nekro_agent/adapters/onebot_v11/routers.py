"""OneBot V11 适配器的 NapCat 容器集成路由

## 容器名从哪里来（多实例改造后）

总后台按 `services/console/container_manager.py` 的模板逐实例建容器（模板名带
`deployment_scope()` 前缀），所以本模块的每个端点都接受一个 `instance_key` 查询参数，
容器名一律由 `container_manager` 生成（候选表见下），不在这里另写一套查找逻辑。

单实例时代这些端点取的是适配器配置 `NAPCAT_CONTAINER_NAME`（默认 `nekro_napcat`）。
那类部署升级上来之后，宿主机上仍然只有这一个没有归属标签的容器，而数据库里会因为
协议端自动登记多出一行实例记录——若只按模板找，它们会在升级当天集体 404。
因此容器名是一个**候选列表**，按「越新越优先」排序：

1. 当前口径的模板名（带部署前缀，`container_manager.build_container_name`）——新部署命中；
2. 升级前的裸模板名（`nekro_napcat_{instance_key}`，模板加上部署前缀之前建的容器顶着它）；
3. 单实例时代的 `NAPCAT_CONTAINER_NAME`——遗留部署命中。

**命中前两个模板名的候选要过归属校验**（`container_manager.assert_container_owned`，
与总后台 `_assert_owned` 同一份判据）：升级前的裸模板名在两套部署上逐字相同，同宿主上
另一套部署里同名的 `instance_key` 会解析到同一个容器，而本模块的 `/logs` 会把带着
WebUI 令牌的日志吐出来、`/restart` 会真的重启它。第三个候选（配置遗留名）
不校验——那个容器身上没有任何标签，校验它等于让所有单实例遗留部署集体 403。

## 为什么整组端点都是 Role.Super

`/logs` 与 `/logs/stream` 会把 NapCat 的启动日志原样吐出来，里面就带着 WebUI 的
访问令牌（前端 `logs.tsx` 的 `extractWebuiToken` 正是从日志里抠它），因此它们与
`/napcat-token` 是同一件事，判据同 `plugin_editor` / `commands`：能读走凭据的入口
一律平台方专属。`/restart` 是容器生命周期操作，总后台的同类端点也要求 `Role.Super`。
另一方面，`instance_key` 参数化之后若仍停在 `Role.Admin`，租户管理员就能指定别人的
实例标识去读别人的协议端日志——这是参数化本身引入的横向越权，必须在这里堵死。
"""

import asyncio
import contextlib
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncGenerator, List, Optional


import aiodocker
from aiodocker.containers import DockerContainer
from aiodocker.exceptions import DockerError
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from nekro_agent.adapters.onebot_v11.core.access_token import resolve_expected_token
from nekro_agent.adapters.utils import adapter_utils
from nekro_agent.core.config import config as core_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import (

    NAPCAT_ONEBOT_ADAPTER_DIR,
    resolve_deployment_onebot_access_token,
    resolve_self_hostname,
)
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    NotFoundError,
    OperationFailedError,
    PermissionDeniedError,
    ValidationError,
)
from nekro_agent.services.console import container_manager
from nekro_agent.services.runtime_state import is_shutting_down
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

logger = get_sub_logger("adapter.onebot_v11")
if TYPE_CHECKING:
    from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

router = APIRouter(prefix="/container", tags=["OneBot V11 Container"])


class TokenResponse(BaseModel):
    token: Optional[str]


class ContainerStatus(BaseModel):
    running: bool
    started_at: str


class ActionResponse(BaseModel):
    ok: bool = True


@router.get("/onebot-token")
@require_role(Role.Super)
async def get_onebot_token(
    instance_key: Optional[str] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TokenResponse:
    """获取 OneBot 协议端接入令牌

    **按实例寻址**，与同族的 `/napcat-token` 一样：握手时的校验是按实例做的
    （`adapters/onebot_v11/core/access_token.py`），所以"该抄进协议端配置的那枚令牌"
    也必须按实例回答。判据直接复用 `container_manager.resolve_access_token`——
    它正是下发进 NapCat 配置、也正是握手时被拿来比对的那个值，这里另写一份就会分叉，
    而分叉的表现是用户照着页面抄完令牌、协议端却握手 403。

    - 带 `instance_key`：该实例的令牌（实例没有自己那枚时按定义退回部署级值）；
    - 不带：部署级 `ONEBOT_ACCESS_TOKEN`，与改造前**逐字相同**——单实例老部署的
      前端本来就不带这个参数。

    ⚠️ 部署级令牌拿到就能冒充"没有自己令牌的协议端"连上来，因此整条仍是 `Role.Super`。
    """
    key = (instance_key or "").strip()
    if key:
        instance = await container_manager.resolve_instance(key)
        return TokenResponse(token=container_manager.resolve_access_token(instance) or None)
    return TokenResponse(token=resolve_deployment_onebot_access_token() or None)


@router.get("/napcat-token")
@require_role(Role.Super)
async def get_napcat_token(
    instance_key: Optional[str] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TokenResponse:
    """获取 NapCat WebUI 访问令牌"""
    try:
        async with get_container(instance_key) as container:
            state = (await container.show())["State"]
            if not state["Running"]:
                raise OperationFailedError(operation="获取 NapCat WebUI 访问令牌")

        config_file_path = next((path for path in webui_config_paths(instance_key) if path.exists()), None)
        if config_file_path is None:
            raise NotFoundError(resource="NapCat 配置文件")

        config_text = config_file_path.read_text(encoding="utf-8")
        try:
            config_data = json.loads(config_text)
        except json.JSONDecodeError as e:
            raise ValidationError(reason="配置文件格式错误") from e

        token = config_data.get("token")
        if not token:
            raise ValidationError(reason="配置文件中未找到 token 字段")

        return TokenResponse(token=token)
    # `PermissionDeniedError` 必须原样透出：它是 `get_container()` 的归属校验拒绝了一个
    # **别的部署**的容器（403）。落进下面那条兜底 `except` 的话，一次越权会被改写成
    # "获取令牌失败"（500），既误导排查，也把这条安全信号从响应里抹掉。
    except (NotFoundError, ValidationError, OperationFailedError, PermissionDeniedError):
        raise
    except Exception as e:
        logger.warning(f"无法获取容器状态: {e!s}")
        raise OperationFailedError(operation="获取 NapCat WebUI 访问令牌") from e


async def get_docker():
    """获取或创建 Docker 客户端"""
    return aiodocker.Docker()


def legacy_container_name() -> str:
    """单实例时代那个固定的 NapCat 容器名

    多实例改造后 `NAPCAT_CONTAINER_NAME` 不再是"当前容器叫什么"，而降级为
    **遗留部署的兼容名**：只有在没指定实例、或该实例的模板容器还不存在时才会用到。
    留着它是因为老部署宿主机上那个 `nekro_napcat` 容器不带归属标签，除了名字没有别的抓手。
    """
    from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

    adapter = adapter_utils.get_typed_adapter("onebot_v11", OnebotV11Adapter)
    return adapter.config.NAPCAT_CONTAINER_NAME.strip()


async def container_name_candidates(instance_key: Optional[str]) -> List[str]:
    """按优先级列出要尝试的容器名

    指定实例时候选表是 `container_manager.container_name_candidates()`（当前口径的
    带前缀名在前、升级前的裸模板名在后，`build_container_name` 顺带校验 `instance_key`
    的字符集，挡住路径穿越与构造名），再接单实例时代的遗留配置名。
    未指定实例时只认遗留配置名——那正是老客户端与单实例部署的形态。
    """
    key = (instance_key or "").strip()
    legacy = legacy_container_name()
    if not key:
        if not legacy:
            raise ValidationError(reason="未指定实例标识，且未设置 NapCat 容器名称")
        return [legacy]

    # 未知实例直接 404，不拿一个不存在的实例标识去 Docker 那里试
    await container_manager.resolve_instance(key)
    candidates = container_manager.container_name_candidates(key)
    if legacy and legacy not in candidates:
        candidates.append(legacy)
    return candidates


def webui_config_paths(instance_key: Optional[str]) -> List[Path]:
    """NapCat WebUI 配置文件的候选路径

    顺序与 `container_name_candidates` 一致：实例配置目录优先，回退到单实例时代的
    `{DATA_DIR}/napcat_data/napcat/`（即 `NAPCAT_ONEBOT_ADAPTER_DIR`）。
    """
    legacy = Path(NAPCAT_ONEBOT_ADAPTER_DIR) / "webui.json"
    key = (instance_key or "").strip()
    if not key:
        return [legacy]
    return [container_manager.get_napcat_config_dir(key) / "webui.json", legacy]


@asynccontextmanager
async def get_container(instance_key: Optional[str] = None) -> AsyncGenerator[DockerContainer, None]:
    """获取 NapCat 容器实例，并确保底层 Docker 客户端被正确关闭

    容器不存在时抛 404 而不是 500——"这个实例还没建容器"是一个正常状态，
    不该和 Docker 守护进程连不上混成同一个错误。

    `yield` 刻意放在异常处理之外：早先的写法把整个 `with` 语句体也包在
    `except Exception` 里，调用方自己抛的业务异常会被统统改写成"获取容器失败"。
    """
    candidates = await container_name_candidates(instance_key)
    key = (instance_key or "").strip()
    # 模板名的候选（当前口径的带前缀名 + 升级前的裸模板名）要过归属校验，配置遗留名不过
    # ——理由见下面循环里的注释。这里调的就是 `container_name_candidates` 用来拼模板名的那份
    # 候选表，两边不可能分叉。
    template_names = set(container_manager.container_name_candidates(key)) if key else set()
    client = await get_docker()
    try:
        container: Optional[DockerContainer] = None
        for name in candidates:
            try:
                found = await client.containers.get(name)
            except DockerError as e:
                if e.status == 404:
                    continue
                logger.error(f"获取容器 {name} 失败: {e!s}")
                raise OperationFailedError(operation="获取容器") from e
            except Exception as e:
                logger.error(f"获取容器 {name} 失败: {e!s}")
                raise OperationFailedError(operation="获取容器") from e

            # 模板名（带前缀的新名与升级前的裸模板名都是）在两套部署上可能逐字相同
            # （裸模板名不带部署前缀），同宿主上另一套部署里同名的 `instance_key`
            # 会解析到同一个容器。本模块的端点里 `/logs` 与 `/logs/stream` 会把 NapCat 日志
            # （**含 WebUI 访问令牌**）原样吐出、`/restart` 直接重启容器，因此必须与总后台
            # 走同一套归属校验，否则这里就是 #46 的第二个出口。
            # 校验刻意放在上面的 try 之外：`PermissionDeniedError` 不能被那两条
            # `except` 改写成 `OperationFailedError`（越权会变成一个 500）。
            #
            # 遗留配置名（`NAPCAT_CONTAINER_NAME`，默认 `nekro_napcat`）**刻意不校验**：
            # 那个容器由 compose / 运维建、身上没有任何归属标签，对它套用标签判据等于让所有
            # 单实例遗留部署的这几个端点集体 403。单部署形态的行为在这条改动前后逐字不变。
            if name in template_names:
                await container_manager.assert_container_owned(found, key)

            container = found
            break

        if container is None:
            logger.warning(f"未找到 NapCat 容器，已依次尝试: {candidates}")
            raise NotFoundError(resource=f"容器 '{candidates[0]}'")

        yield container
    finally:
        await client.close()


@router.get("/status")
@require_role(Role.Super)
async def get_status(
    instance_key: Optional[str] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ContainerStatus:
    """获取容器状态"""
    async with get_container(instance_key) as container:
        state = (await container.show())["State"]
        return ContainerStatus(
            running=state["Running"],
            started_at=state["StartedAt"],
        )


@router.get("/logs")
@require_role(Role.Super)
async def get_logs(
    tail: Optional[int] = 100,
    instance_key: Optional[str] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> list[str]:
    """获取最近的容器日志"""
    async with get_container(instance_key) as container:
        logs = await container.log(stdout=True, stderr=True, tail=tail)
        return logs


@router.get("/logs/stream")
@require_role(Role.Super)
async def stream_logs(
    request: Request,
    instance_key: Optional[str] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> EventSourceResponse:
    """实时日志流"""
    # 先把实例标识解析掉：`EventSourceResponse` 一旦返回，状态码就已经是 200，
    # 生成器里再抛 404 只是一条建好又立刻断掉的连接，调用方看不出发生了什么
    await container_name_candidates(instance_key)

    async def generate() -> AsyncGenerator[str, None]:
        async with get_container(instance_key) as container:
            initial_logs = await container.log(stdout=True, stderr=True, tail=100, timestamps=False)
            init_time = time.time()
            for log in initial_logs:
                if is_shutting_down() or await request.is_disconnected():
                    return
                yield log
                await asyncio.sleep(0.01)

            queue: asyncio.Queue[object] = asyncio.Queue(maxsize=256)
            sentinel = object()

            async def pump_logs() -> None:
                try:
                    async for log in container.log(
                        stdout=True,
                        stderr=True,
                        follow=True,
                        since=int(init_time),
                        timestamps=False,
                    ):
                        await queue.put(log)
                except asyncio.CancelledError:
                    raise
                finally:
                    await queue.put(sentinel)

            log_task = asyncio.create_task(pump_logs())
            try:
                while not is_shutting_down():
                    if await request.is_disconnected():
                        return
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        yield {"comment": "ping"}
                        continue
                    if item is sentinel:
                        return
                    yield str(item)
                    await asyncio.sleep(0.01)
            finally:
                if not log_task.done():
                    log_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await log_task

    return EventSourceResponse(generate())


ADAPTER_TEMPLATE = """
{{
  "network": {{
    "httpServers": [],
    "httpSseServers": [],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": [
      {{
        "enable": true,
        "name": "ws",
        "url": "ws://{instance_name}:{port}/onebot/v11/ws",
        "reportSelfMessage": true,
        "messagePostFormat": "array",
        "token": "{token}",
        "debug": false,
        "heartInterval": 30000,
        "reconnectInterval": 3000
      }}
    ],
    "plugins": []
  }},
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false
}}
""".strip()


@router.post("/restart")
@require_role(Role.Super)
async def restart(
    instance_key: Optional[str] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """重启容器

    重启前把协议端的网络配置重新落盘，保证容器起来就能连回 NA：
    指定实例时复用总后台那套（`resync_network_config` 会按当前部署参数重建托管连接，
    用户自建的连接一条不动）；未指定实例时沿用单实例时代的行为——只在适配器文件
    缺失时按模板补一份，绝不覆盖已有文件。
    """
    key = (instance_key or "").strip()
    if key:
        instance = await container_manager.resolve_instance(key)
        # 远程协议端没有本机配置文件，`resync_network_config` 会拒绝；那种实例本来就没有容器可重启，
        # 后面的 `get_container` 会给出 404，这里不必抢先报错
        with contextlib.suppress(ValidationError):
            await container_manager.resync_network_config(instance)
    else:
        from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

        adapter = adapter_utils.get_typed_adapter("onebot_v11", OnebotV11Adapter)
        if adapter.config.BOT_QQ:
            adapter_file = Path(NAPCAT_ONEBOT_ADAPTER_DIR) / f"onebot11_{adapter.config.BOT_QQ}.json"
            if not adapter_file.exists():
                logger.info(f"未找到适配器文件: {adapter_file}, 自动创建...")
                # 令牌取**握手时会被拿来比对的那个值**，而不是直接写部署级令牌：这是协议端
                # 配置的第二个写入点（第一个是 `container_manager.build_managed_entry`），
                # 两个写入点与校验侧必须是同一条判据。单实例老部署上 `BOT_QQ` 要么没绑实例、
                # 要么绑的是自动登记的待认领实例（都没有自己的令牌），解析结果逐字等于
                # 部署级令牌 —— 行为不变；而账号被绑到一个有自己令牌的实例时，
                # 写部署级令牌就是写一枚会被自己拒掉的令牌。
                expected = await resolve_expected_token(str(adapter.config.BOT_QQ))
                # 主机名与 `build_managed_entry` 同一个口径（#47）：`resolve_self_hostname()`
                # 三级回落。改造前这里裸读 `OsEnv.INSTANCE_NAME` 无回落，空值时写出
                # `ws://:8021/...` 这种必然连不上的 URL。端口同理取 NA 进程监听的那个，
                # 不是只改宿主侧映射的 `EXPOSE_PORT`（见 `resolve_self_listen_port`）。
                adapter_file.write_text(
                    ADAPTER_TEMPLATE.format(
                        token=expected.token,
                        instance_name=resolve_self_hostname(),
                        port=container_manager.resolve_self_listen_port(),
                    ),
                    encoding="utf-8",
                )

    async def restart_container() -> None:
        # 后台任务里的异常没有调用方接住，不自己记一笔就只剩一条 asyncio 的
        # "Task exception was never retrieved"，前端只会看到状态一直没变
        try:
            async with get_container(key or None) as container:
                await container.restart(timeout=30)
        except Exception as e:  # noqa: BLE001 - 后台任务不允许把异常抛回事件循环
            logger.error(f"重启 NapCat 容器失败: {e!s}")

    asyncio.create_task(restart_container())
    return ActionResponse(ok=True)
