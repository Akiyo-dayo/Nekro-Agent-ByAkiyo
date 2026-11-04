import time
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nekro_agent.adapters import load_adapters_api
from nekro_agent.adapters.email.routers import router as email_adapter_config_router
from nekro_agent.adapters.onebot_v11.core.access_token import OneBotAccessTokenMiddleware
from nekro_agent.core.args import Args
from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.schemas.user import UserLogin, UserToken
from nekro_agent.services.user.util import user_login
from nekro_agent.tools.common_util import get_app_version

from .adapters import router as adapters_router
from .affection import router as affection_router
from .cc_model_presets import router as cc_model_presets_router
from .cc_relay import router as cc_relay_router
from .chat_channel import router as chat_channel_router
from .cloud.announcement import router as cloud_announcement_router
from .cloud.auth import router as cloud_auth_router
from .cloud.favorites import router as cloud_favorites_router
from .cloud.plugins_market import router as plugins_market_router
from .cloud.presets_market import router as presets_market_router
from .cloud.telemetry import router as telemetry_router
from .commands import router as commands_router
from .common import router as common_router
from .config import router as config_router
from .console import router as console_router
from .dashboard import router as dashboard_router
from .deployment import router as deployment_router
from .email import router as email_router
from .events import router as events_router
from .kb_library import router as kb_library_router
from .logs import router as logs_router
from .mcp import router as mcp_router
from .plugin_editor import router as plugin_editor_router
from .plugins import router as plugins_router
from .presets import router as presets_router
from .resources import router as resources_router
from .restart import router as restart_router
from .rpc import router as exec_router
from .sandbox import router as sandbox_router
from .skills import router as skills_router
from .space_cleanup import router as space_cleanup_router
from .timers import router as timers_router
from .user import router as user_router
from .user_manager import router as user_manager_router
from .webhook import router as webhook_router
from .workspace_kb import router as workspace_kb_router
from .workspaces import router as workspaces_router

# 注意：插件路由现在通过插件路由管理器动态挂载，支持热重载
# 不再使用静态路由挂载方式


def _elapsed_color(elapsed_ms: float) -> str:
    if elapsed_ms >= 3000:
        return "red"
    if elapsed_ms >= 1000:
        return "yellow"
    if elapsed_ms >= 300:
        return "blue"
    return "green"


class RequestTimingMiddleware:
    """记录 HTTP 请求耗时，并回写 `X-Process-Time` 响应头

    - 普通请求记录的是完整处理耗时
    - 流式响应记录的是开始返回响应前的耗时，不包含整个流结束时间

    纯 ASGI，只包一层 `send`，不碰响应体。以前是 `@app.middleware("http")`（`BaseHTTPMiddleware`）：
    它把内层的响应体搬进内存流、再由自己补发结束帧，于是内层流式响应被中途切断时（视频转发的发送超时、
    时长上限），外层照样补 `more_body: False`，分块传输下浏览器会把截短的内容当成完整的。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started_at = time.perf_counter()
        started = False

        def log(status_code: int, elapsed_ms: float) -> None:
            color = _elapsed_color(elapsed_ms)
            client = scope.get("client")
            client_host = client[0] if client else "unknown"
            # 颜色标记只写在模板里（取值来自固定的几种颜色），请求方能控制的方法、路径、地址一律作参数传入：
            # loguru 只解析模板里的标记。直接拼进去的话，匿名请求 `/x%3C/y%3E` 解码出的 `</y>`
            # 会被当成颜色标签，有 DEBUG 级别的输出时抛 ValueError，变成一个 500
            template = "[HTTP] {} {} status={} elapsed=<" + color + ">{:.2f}ms</" + color + "> client={}"
            logger.opt(colors=True).debug(template, scope.get("method", ""), scope.get("path", ""), status_code, elapsed_ms, client_host)

        async def send_with_timing(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start" and not started:
                started = True
                elapsed_ms = (time.perf_counter() - started_at) * 1000
                # ASGI 规定 `headers` 可以省略
                message.setdefault("headers", [])
                MutableHeaders(scope=message)["X-Process-Time"] = f"{elapsed_ms:.2f}ms"
                log(message.get("status", 200), elapsed_ms)
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        except Exception:
            if not started:
                log(500, (time.perf_counter() - started_at) * 1000)
            raise


def mount_middlewares(app: FastAPI):
    """挂载中间件和全局处理器"""
    # OneBot V11 协议端接入令牌的**按实例**校验。必须挂在这里而不是适配器初始化里：
    # `add_middleware` 只能在应用启动之前调用，而 `mount_middlewares` 正是在
    # `nekro_agent/__init__.py` 模块级、`on_startup` 之前执行的那一步。
    # 为什么是中间件而不是继承 NoneBot 的适配器，见该模块的文档。
    app.add_middleware(OneBotAccessTokenMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(RequestTimingMiddleware)

    # 注册统一的全局异常处理器
    # 支持 AppError 业务错误、验证错误、HTTPException 及通用异常
    register_exception_handlers(app)


def mount_api_routes(app: FastAPI):
    """挂载 API 路由"""
    api = APIRouter(prefix="/api")

    api.include_router(user_router)
    api.include_router(user_manager_router)
    api.include_router(exec_router)
    api.include_router(logs_router)
    api.include_router(config_router)
    api.include_router(plugins_router)
    api.include_router(plugin_editor_router)
    api.include_router(sandbox_router)
    api.include_router(dashboard_router)
    api.include_router(deployment_router)
    api.include_router(chat_channel_router)
    api.include_router(webhook_router)
    api.include_router(presets_router)
    api.include_router(restart_router)
    api.include_router(resources_router)
    api.include_router(telemetry_router)
    api.include_router(presets_market_router)
    api.include_router(plugins_market_router)
    api.include_router(cloud_favorites_router)
    api.include_router(cloud_auth_router)
    api.include_router(cloud_announcement_router)
    api.include_router(adapters_router)
    api.include_router(email_adapter_config_router, prefix="/adapters/email", tags=["Adapter:email"])
    api.include_router(common_router)
    api.include_router(space_cleanup_router)
    api.include_router(timers_router)
    api.include_router(workspaces_router)
    api.include_router(workspace_kb_router)
    api.include_router(kb_library_router)
    api.include_router(skills_router)
    api.include_router(cc_model_presets_router)
    api.include_router(cc_relay_router)
    api.include_router(events_router)
    api.include_router(commands_router)
    api.include_router(mcp_router)
    api.include_router(email_router)
    api.include_router(console_router)
    api.include_router(affection_router)

    api.include_router(load_adapters_api())

    @api.get("/health", tags=["Health"], summary="健康检查")
    async def _() -> dict:
        """测试服务是否正常运行"""
        return {"ok": True}

    @api.post("/token", response_model=UserToken, tags=["User"], summary="OpenAPI OAuth2 授权")
    async def _(form_data: OAuth2PasswordRequestForm = Depends()) -> UserToken:
        """登陆获取token"""
        return await user_login(
            UserLogin(username=form_data.username, password=form_data.password),
        )

    if Args.DOCS or OsEnv.ENABLE_OPENAPI_DOCS:
        # 挂载 API 文档
        @api.get("/docs", include_in_schema=False)
        async def custom_swagger_ui_html(request: Request):
            return get_swagger_ui_html(
                openapi_url=request.app.openapi_url,
                title="Nekro Agent API",
                oauth2_redirect_url="/api/token",
            )

        # redoc
        @api.get("/redoc", include_in_schema=False)
        async def redoc_html(request: Request):
            return get_redoc_html(
                openapi_url=request.app.openapi_url,
                title="Nekro Agent API",
            )

        @api.get("/openapi.json", include_in_schema=False)
        async def custom_openapi(request: Request):
            """生成并缓存全局 OpenAPI 文档"""
            app_instance = request.app
            # 总是重新生成，以反映动态添加/删除的路由
            openapi_schema = get_openapi(
                title="Nekro Agent API",
                version=get_app_version(),
                routes=app_instance.routes,
                description="Nekro Agent API 文档（包含动态插件路由）",
            )
            app_instance.openapi_schema = openapi_schema

            # 添加HTTP头，强制浏览器不缓存OpenAPI文档
            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
            return JSONResponse(openapi_schema, headers=headers)

    app.include_router(api)

    # 内置 Web Chat MCP：与 NA 同进程，入口自带鉴权（`na_app.AuthenticatedMcpApp`）。
    # 挂载点必须在 `/api` 之外单独 mount：MCP SDK 自己接管这个前缀下的路径与协议。
    from nekro_agent.services.mcp.web_chat_mcp.na_app import create_web_chat_mcp_app

    app.mount("/api/mcp/web-chat", create_web_chat_mcp_app(), name="web-chat-mcp")

    # 🎯 正确的静态文件挂载方案：/webui + 根路径重定向
    static_dir = Path(OsEnv.STATIC_DIR)
    if static_dir.exists():
        app.mount("/webui", StaticFiles(directory=str(static_dir), html=True), name="webui")
        logger.info(f"✅ 前端静态文件已挂载到 /webui 路径: {static_dir}")

        @app.get("/", include_in_schema=False)
        async def redirect_to_webui():
            """根路径重定向到前端界面"""
            return RedirectResponse(url="/webui", status_code=302)

        @app.get("/index.html", include_in_schema=False)
        async def redirect_index_to_webui():
            """index.html 重定向到前端界面"""
            return RedirectResponse(url="/webui", status_code=302)

        logger.info("✅ 根路径重定向已配置：/ -> /webui/")
    else:
        logger.debug(f"静态文件目录不存在: {static_dir}")

    # 将 OpenAPI 文档生成和 URL 设置移到 app 上，确保全局生效
    app.openapi_url = "/api/openapi.json"
