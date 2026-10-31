"""docker-guard 反向代理：在 NA 与宿主 docker.sock 之间插一层按动作放行的薄代理。

部署形态（opt-in，见 docker/docker-compose-x-hardened.yml）：

    NA 容器  --DOCKER_HOST=unix:///run/nekro-guard/guard.sock-->  guard 代理  --/var/run/docker.sock-->  宿主 Docker

NA 容器**不再直接挂载** docker.sock；只有 guard 这个独立进程持有它。guard 对每个请求跑
`policy.evaluate()`，deny 的直接回 403，从不转发给宿主。攻击者即便在 NA 进程内执行任意代码，
能对 Docker 做的也被收敛到 NA 真正需要的那几个动作，且 `create` 不允许特权 / 挂宿主目录。

判据全部在 `policy.py`（纯函数、表驱动单测覆盖）；本文件只做 HTTP 转发与流式透传。
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web
from yarl import URL

from nekro_agent.core.logger import logger
from nekro_agent.services.docker_guard.policy import (
    Decision,
    PolicyContext,
    body_guard_rule,
    container_ownership_target,
    evaluate,
    is_safe_container_ref,
    labels_indicate_ownership,
    network_connect_target,
    normalize_path,
)

# 转发时必须剥掉的逐跳头，交给两侧的 HTTP 框架各自管理，避免 framing 冲突。
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    },
)

_MAX_CREATE_BODY = 1 * 1024 * 1024  # create 请求体上限 1MB，足够容纳任何合法配置

# 记住本代理亲手建出来的容器 ID 的上限。沙盒容器是一次性的（AutoRemove），删除时会从集合里摘掉；
# 这个上限只是防御性兜底，避免异常情形下无界增长。超限时清空（大不了退回按 Labels 现查归属）。
_MAX_OWNED_IDS = 8192


def build_context() -> PolicyContext:
    """从环境变量构造判据上下文。

    `NEKRO_DATA_DIR` 是所有合法 bind source 的根（沙盒/CC/NapCat 的挂载都在其下）。
    guard 与 NA 拿到的是同一个宿主路径值。
    """
    roots: List[str] = []
    data_dir = os.environ.get("NEKRO_DATA_DIR", "").strip()
    if data_dir:
        roots.append(posixpath.normpath(data_dir))
    extra = os.environ.get("NEKRO_DOCKER_GUARD_EXTRA_BIND_ROOTS", "").strip()
    for item in extra.split(":"):
        item = item.strip()
        if item:
            roots.append(posixpath.normpath(item))
    return PolicyContext(allowed_bind_roots=tuple(roots))


class DockerGuardProxy:
    def __init__(self, upstream_socket: str, ctx: PolicyContext) -> None:
        self._upstream_socket = upstream_socket
        self._ctx = ctx
        self._session: Optional[aiohttp.ClientSession] = None
        # 本代理亲手 create 出来的容器 ID（快路径：这些容器的后续 exec/logs/… 无需再 inspect 判归属）。
        # 长命容器（NapCat / CC 工作区）跨代理重启后不在此集合里，会走 inspect + Labels 兜底判归属。
        self._owned_ids: "OrderedDict[str, None]" = OrderedDict()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.UnixConnector(path=self._upstream_socket)
            # 不设总超时：logs follow / image pull / wait 都是长连接
            self._session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=None))
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def _decide(self, method: str, raw_path: str, body: Optional[Dict[str, Any]]) -> Decision:
        return evaluate(method, raw_path, self._ctx, body=body)

    def _remember_owned(self, container_id: str) -> None:
        if not container_id:
            return
        self._owned_ids[container_id] = None
        self._owned_ids.move_to_end(container_id)
        while len(self._owned_ids) > _MAX_OWNED_IDS:
            self._owned_ids.popitem(last=False)

    def _forget_owned(self, container_ref: str) -> None:
        self._owned_ids.pop(container_ref, None)

    def _note_created_response(self, status: int, raw: bytes) -> None:
        """从 create 的上游响应里解析新容器 ID 记进快表（仅 2xx）。"""
        if not 200 <= status < 300:
            return
        try:
            created = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.debug("[docker-guard] create 响应无法解析出 Id，跳过快表登记")
            return
        if isinstance(created, dict) and isinstance(created.get("Id"), str):
            self._remember_owned(created["Id"])

    def _is_owned_id(self, container_ref: str) -> bool:
        # create 返回的是完整 64 位 ID；后续请求可能用完整 ID、短 ID 或名称。
        # 完整 ID 精确命中；短 ID（>=12 位）按前缀匹配一次亲手建的容器。
        if container_ref in self._owned_ids:
            return True
        if len(container_ref) >= 12:
            return any(full.startswith(container_ref) for full in self._owned_ids)
        return False

    async def _inspect_labels(self, container_ref: str) -> Optional[Dict[str, Any]]:
        """用代理自己的上游连接 inspect 目标容器，取出 Labels（判归属用，本身不受归属门限制）。

        返回 None 表示容器不存在或 inspect 失败——归属无法确证，调用方按「非本部署」处理。
        """
        if not is_safe_container_ref(container_ref):
            # 请求体里来的引用可能含 `/` 或 `..`，会被 yarl 改写成对另一个容器的 inspect
            logger.warning(f"[docker-guard] 非法容器引用，拒绝判归属: {container_ref!r}")
            return None
        session = await self._get_session()
        url = f"http://docker/containers/{container_ref}/json"
        # 归属判定是同步挡在转发前的一步：inspect 必须自带超时，不能让一次慢 inspect 把请求挂死
        # （§2.6 教训②：任何等待类调用都要有超时）。inspect 是控制面小请求，10s 足够宽。
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, ValueError, asyncio.TimeoutError) as e:
            logger.debug(f"[docker-guard] 归属 inspect 失败 {container_ref}: {e}")
            return None
        config = data.get("Config") if isinstance(data, dict) else None
        labels = config.get("Labels") if isinstance(config, dict) else None
        return labels if isinstance(labels, dict) else {}

    async def _is_owned_container(self, container_ref: str) -> bool:
        """目标容器是否归属本部署：先查亲手建的快表，未命中再 inspect 看 Labels。"""
        if self._is_owned_id(container_ref):
            return True
        labels = await self._inspect_labels(container_ref)
        if labels is None:
            return False
        if labels_indicate_ownership(labels):
            # 命中一次就记进快表，避免对同一个长命容器反复 inspect
            self._remember_owned(container_ref)
            return True
        return False

    async def handle(self, request: web.Request) -> web.StreamResponse:
        method = request.method.upper()
        raw_path = request.raw_path  # 含查询串
        path = normalize_path(raw_path)

        # 需要判据的请求体（create / exec 创建 / exec 启动 / 网络 connect）由端点表统一决定，
        # 不在这里另写一份「哪些路径要读 body」的判断（那就是第二处实现）。
        body_bytes = b""
        parsed_body: Optional[Dict[str, Any]] = None
        guard_rule = body_guard_rule(method, raw_path)
        needs_create_capture = guard_rule == "create"
        wants_body = bool(guard_rule)
        if request.can_read_body:
            body_bytes = await request.read()
        if wants_body:
            if len(body_bytes) > _MAX_CREATE_BODY:
                return self._deny(Decision(False, "请求体过大", "body.too_large"), method, path)
            try:
                loaded = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
                parsed_body = loaded if isinstance(loaded, dict) else None
            except (ValueError, UnicodeDecodeError):
                return self._deny(Decision(False, "请求体非合法 JSON", "body.invalid"), method, path)

        decision = self._decide(method, raw_path, parsed_body)
        if not decision.allow:
            return self._deny(decision, method, path)

        # 归属门：按 ID 指向具体容器的端点（exec/logs/inspect/start/stop/…）必须确认目标归属本部署。
        # 路径判据只管「能做哪个动作」，管不到「对哪个容器做」——不加这道门，NA 被攻破后就能
        # exec 进宿主上别人的容器（挂了 docker.sock 的 → 宿主 root）、或 inspect 兄弟 postgres 读凭据。
        # 第二个出口在**请求体**里：`POST /networks/{id}/connect` 的目标容器写在 body 上。
        target = container_ownership_target(method, raw_path)
        body_target = network_connect_target(method, raw_path, parsed_body)
        for checked in (target, body_target):
            if checked is not None and not await self._is_owned_container(checked):
                return self._deny_foreign(checked, method, path)

        # create 成功后要记住新容器 ID（快路径判归属），因此对它缓冲整段响应而非流式透传。
        if needs_create_capture:
            return await self._forward(request, body_bytes, capture_created=True)

        response = await self._forward(request, body_bytes)
        # 容器被删掉后从快表摘掉，避免 ID 复用时误判 + 控制集合规模
        if method == "DELETE" and target is not None and response.status < 400:
            self._forget_owned(target)
        return response

    def _deny(self, decision: Decision, method: str, path: str) -> web.Response:
        logger.warning(f"[docker-guard] 拒绝 {method} {path} | rule={decision.rule} | {decision.reason}")
        payload = {"message": f"docker-guard denied: {decision.reason}"}
        return web.json_response(payload, status=403)

    def _deny_foreign(self, container_ref: str, method: str, path: str) -> web.Response:
        """归属门的拒绝：回 **404**（Docker 的「没有这个容器」形状），不是 403。

        两条理由，缺一不可：

        1. **误伤**：`_is_owned_container` 对「容器压根不存在」和「存在但不是本部署的」返回同一个
           False。回 403 会把前者也变成 403，而 NA 有一批分支正是靠 Docker 的 404 来判断
           「容器还没建」——`services/console/container_manager.py:_inspect_container`
           （`if e.status == 404: return None`，NapCat **首次创建**必经）、
           `adapters/onebot_v11/routers.py` 的候选名遍历（404 才 continue）、
           `_ensure_image`（404 才去 pull）。403 会让这些「不存在」分支永远走不到，
           首次装 NapCat 直接失败。回 404 之后这些路径逐字恢复原行为。
        2. **不泄露存在性**：403 等于告诉调用方「这个容器在，只是你碰不到」。项目既定约定
           就是越权回 404（见 PROJECT_STATE §1.4），这里与它一致。

        拒绝本身没有被削弱：请求同样不会转发到宿主 socket，日志里同样有 rule 与目标。
        """
        decision = Decision(
            False,
            f"目标容器不归属本部署（未持 nekro.* 标签）或不存在: {container_ref}",
            "ownership.foreign_container",
        )
        logger.warning(f"[docker-guard] 拒绝 {method} {path} | rule={decision.rule} | {decision.reason}")
        # 逐字沿用 Docker 自己的 404 文案，让 aiodocker / docker CLI 的既有分支照常识别
        return web.json_response({"message": f"No such container: {container_ref}"}, status=404)

    async def _forward(
        self,
        request: web.Request,
        body_bytes: bytes,
        capture_created: bool = False,
    ) -> web.StreamResponse:
        session = await self._get_session()
        fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        # `encoded=True`：**必须**按原样字节转发，不能让 yarl 替我们改写路径。
        # 传字符串 URL 时 yarl 会自己消解 `..`——
        #   URL("http://docker/images/x/../../containers/victim/json").path == "/containers/victim/json"
        # 于是判据看到的是 `images.inspect`（不查归属），Docker 收到的却是对**任意容器**的 inspect：
        # 归属门被整条绕过，兄弟 postgres 的 `POSTGRES_PASSWORD` 直接读走。判据侧已经把含相对段的
        # 路径 deny 掉（`policy.path_has_dot_segments`），这里是同一条洞的另一半：
        # **判到的路径与发出去的路径必须逐字节相同**。
        url = URL(f"http://docker{request.raw_path}", encoded=True)

        try:
            upstream = await session.request(
                request.method,
                url,
                headers=fwd_headers,
                data=body_bytes if body_bytes else None,
                # 不跟随上游重定向：一次 30x 就能把已判过的路径换成一条没判过的（Docker 的
                # 路由器对不规范路径会回 301 到清理后的路径）。让客户端自己拿到 30x，
                # 它再发一次就会重新过一遍判据。
                allow_redirects=False,
            )
        except aiohttp.ClientError as e:
            logger.warning(f"[docker-guard] 上游连接失败: {e}")
            return web.json_response({"message": f"docker-guard upstream error: {e}"}, status=502)

        # create 的响应短小非流式：整段读回，解析出新容器 ID 记进快表，再原样回给 NA。
        if capture_created:
            try:
                raw = await upstream.read()
            finally:
                upstream.release()
            self._note_created_response(upstream.status, raw)
            out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
            return web.Response(status=upstream.status, body=raw, headers=out_headers)

        response = web.StreamResponse(status=upstream.status)
        for key, value in upstream.headers.items():
            if key.lower() in _HOP_BY_HOP:
                continue
            response.headers[key] = value
        # 交给 aiohttp 用分块编码透传：logs follow / pull 是无 Content-Length 的流
        response.enable_chunked_encoding()

        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
        except (aiohttp.ClientError, ConnectionResetError) as e:
            logger.debug(f"[docker-guard] 转发中断: {e}")
        finally:
            upstream.release()
        await response.write_eof()
        return response


def _resolve_upstream_socket() -> str:
    configured = os.environ.get("NEKRO_DOCKER_GUARD_UPSTREAM", "").strip()
    if configured:
        return configured
    for candidate in ("/var/run/docker.sock", "/run/docker.sock"):
        if Path(candidate).exists():
            return candidate
    return "/var/run/docker.sock"


async def create_app() -> tuple[web.Application, DockerGuardProxy]:
    ctx = build_context()
    proxy = DockerGuardProxy(_resolve_upstream_socket(), ctx)
    app = web.Application(client_max_size=_MAX_CREATE_BODY + 1024)
    app.router.add_route("*", "/{tail:.*}", proxy.handle)

    async def _on_cleanup(_: web.Application) -> None:
        await proxy.close()

    app.on_cleanup.append(_on_cleanup)
    logger.info(
        f"[docker-guard] 上游 socket={proxy._upstream_socket} | 允许的 bind 根={list(ctx.allowed_bind_roots)}",
    )
    return app, proxy


def run() -> None:
    """启动 guard 代理。监听地址由环境变量决定，默认 unix socket。"""
    import asyncio

    listen_socket = os.environ.get("NEKRO_DOCKER_GUARD_LISTEN_SOCKET", "/run/nekro-guard/guard.sock").strip()
    listen_host = os.environ.get("NEKRO_DOCKER_GUARD_LISTEN_HOST", "").strip()
    listen_port = os.environ.get("NEKRO_DOCKER_GUARD_LISTEN_PORT", "").strip()

    async def _main() -> None:
        app, _proxy = await create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        if listen_host and listen_port:
            site: web.BaseSite = web.TCPSite(runner, host=listen_host, port=int(listen_port))
            logger.info(f"[docker-guard] 监听 tcp://{listen_host}:{listen_port}")
        else:
            sock_path = Path(listen_socket)
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            if sock_path.exists():
                sock_path.unlink()
            site = web.UnixSite(runner, path=str(sock_path))
            logger.info(f"[docker-guard] 监听 unix://{sock_path}")
        await site.start()
        # 放宽 socket 权限，令 NA 容器内的进程可连接
        if not (listen_host and listen_port):
            try:
                os.chmod(listen_socket, 0o660)
            except OSError:
                pass
        # 常驻
        import asyncio as _asyncio

        await _asyncio.Event().wait()

    asyncio.run(_main())
