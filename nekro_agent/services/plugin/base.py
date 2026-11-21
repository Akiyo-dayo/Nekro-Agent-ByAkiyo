import asyncio
import inspect
import mimetypes
import re
from pathlib import Path
from posixpath import normpath
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Callable,
    Coroutine,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

import aiofiles
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from nekro_agent.core.config import config
from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.schemas.i18n import I18nDict, SupportedLang, get_text
from nekro_agent.schemas.signal import MsgSignal
from nekro_agent.services.plugin.isolation import call_plugin, release_quarantine
from nekro_agent.services.plugin.scope import (
    current_instance_key,
    invalidate_scoped_config,
    plugin_effective_in_chat,
    resolve_scoped_config,
)
from nekro_agent.services.plugin.task import TaskRunner
from nekro_agent.tools.text_sanitize import strip_nul

from .injection import render_injection_inline
from .schema import PromptInjectMethod, PromptInjectResult, SandboxMethod, SandboxMethodType, WebhookMethod

if TYPE_CHECKING:
    from nekro_agent.models.db_chat_channel import DBChatChannel
    from nekro_agent.schemas.agent_message import AgentMessageSegment
    from nekro_agent.services.ops_agent.triggers import RoundTrigger
    from nekro_agent.tools.untrusted_content import UntrustedToolResult

T = TypeVar("T", bound=ConfigBase)
CollectMethodsFunc = Callable[[AgentCtx], Coroutine[Any, Any, List[SandboxMethod]]]


class ScopedPluginConfig:
    """插件配置代理：字段在属性访问时按当前实例上下文解析

    第三方插件普遍在模块级写 `config = plugin.get_config(XxxConfig)`，然后在任意位置
    直接读 `config.FIELD`。若给 `get_config()` 增加实例参数，插件市场里的插件全部得改。
    因此这里不动 `get_config()` 的签名，改为返回代理对象，把解析推迟到属性访问时：

    - 无实例上下文（单实例部署，或调用方没有设置实例）→ 直接取全局配置字段值，
      行为与改造前**完全一致**；
    - 有实例上下文但该实例无覆盖 → 同样落到全局值；
    - 有实例上下文且该实例有覆盖 → 取实例级值。

    写入（`config.FIELD = x`）始终作用于全局配置实例：实例级覆盖只能通过 WebUI / API
    显式设置，避免插件里一次运行时赋值意外变成某个实例的私有配置。
    """

    __slots__ = ("_plugin",)

    def __init__(self, plugin: "NekroPlugin") -> None:
        object.__setattr__(self, "_plugin", plugin)

    def resolve_global_config(self) -> ConfigBase:
        """当前插件的全局配置实例"""
        return self._plugin.get_global_config()

    def resolve_effective_config(self) -> ConfigBase:
        """当前实例上下文下生效的配置实例；无上下文或无覆盖时即全局实例"""
        plugin = self._plugin
        global_config = plugin.get_global_config()
        instance_key = current_instance_key()
        if not instance_key:
            return global_config
        return resolve_scoped_config(plugin.key, plugin.config_class, global_config, instance_key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.resolve_effective_config(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        plugin = self._plugin
        setattr(plugin.get_global_config(), name, value)
        invalidate_scoped_config(plugin.key)

    def __iter__(self) -> Iterator[Tuple[str, Any]]:
        return iter(self.resolve_effective_config())

    def __repr__(self) -> str:
        return repr(self.resolve_effective_config())


class NekroPlugin:
    """Nekro 插件基类

    用于描述 Nekro 插件的基本信息和方法挂载功能。
    """

    _Configs: Type[ConfigBase] = ConfigBase
    _config: ConfigBase

    def __init__(
        self,
        name: str,
        module_name: str,
        description: str,
        version: str,
        author: str,
        url: str,
        support_adapter: Optional[List[str]] = None,
        is_builtin: bool = False,
        is_package: bool = False,
        i18n_name: Optional[I18nDict] = None,
        i18n_description: Optional[I18nDict] = None,
        allow_sleep: bool | None = None,
        sleep_brief: str = "",
        webui_path: str | None = None,
    ):
        """
        Args:
            name: 插件名称（默认）
            module_name: 插件模块名
            description: 插件描述（默认）
            version: 插件版本
            author: 插件作者
            url: 插件地址
            support_adapter: 支持的适配器
            i18n_name: 插件名称国际化
            i18n_description: 插件描述国际化
            webui_path: 插件 WebUI 路径。以 / 开头时表示插件路由内页面路径；
                相对路径时必须指向插件目录内的 HTML 文件。

        可用回调方法:
            init_method: 初始化方法
            collect_methods_func: 收集可用方法的重写函数
            prompt_inject_method: 提示注入方法
            on_user_message_method: 消息回调方法
            on_system_message_method: 系统消息回调方法
            sandbox_methods: 沙盒方法
            on_reset_method: 重置频道回调方法
            on_round_finished_method: 一轮对话收尾回调方法（带本轮结局）
            cleanup_method: 清理方法
            mount_router: 挂载路由方法
        """

        # 回调方法
        self.init_method: Optional[Callable[..., Coroutine[Any, Any, Any]]] = None
        self.cleanup_method: Optional[Callable[..., Coroutine[Any, Any, Any]]] = None
        self.prompt_inject_method: Optional[PromptInjectMethod] = None
        self.on_reset_method: Optional[Callable[[AgentCtx], Coroutine[Any, Any, Any]]] = None
        self.on_round_finished_method: Optional[Callable[[AgentCtx, str], Coroutine[Any, Any, Any]]] = None
        self.on_user_message_method: Optional[Callable[[AgentCtx, ChatMessage], Coroutine[Any, Any, MsgSignal | None]]] = None
        self.on_system_message_method: Optional[Callable[[AgentCtx, str], Coroutine[Any, Any, MsgSignal | None]]] = None

        self.sandbox_methods: List[SandboxMethod] = []
        self.webhook_methods: Dict[str, WebhookMethod] = {}
        self.name = name
        self.module_name = _validate_name(module_name, "Module Name")
        self.description = description.strip()
        self.version = version.strip()
        self.author = _validate_name(author, "Author")
        self.url = url.strip()
        self.support_adapter = support_adapter or []
        self.i18n_name = i18n_name
        self.i18n_description = i18n_description
        self.allow_sleep = allow_sleep
        self.sleep_brief = sleep_brief.strip()
        # 能力摘要的动态来源（可选）。能力摘要会进系统提示词，而 `sleep_brief` 在构造时就定死了；
        # 个别内置插件的措辞要随运行时开关变，又不能动开关关着时的那份文字，就设这个。
        # `get_plugin_brief` 优先取它返回的非空文字，取不到（未设置、返回空、抛错）时回落到原来的规则。
        self.brief_resolver: Optional[Callable[[], str]] = None
        self.webui_path = _normalize_webui_path(webui_path)
        self._is_enabled = True
        self._key = f"{self.author}.{self.module_name}"

        # 插件子 logger：用于前端按插件过滤日志
        # 约定字段：
        # - subsystem="plugin"
        # - plugin_key=author.module_name
        self.logger = get_sub_logger("plugin", log_name=f"plugin.{self._key}", plugin_key=self._key)
        self._collect_methods_func: Optional[CollectMethodsFunc] = None
        self._is_builtin: bool = is_builtin  # 标记是否为内置插件
        self._is_package: bool = is_package  # 标记是否为包
        self._module: Optional["ModuleType"] = None  # 模块对象

        # 路由相关
        self._router_func: Optional[Callable[[], APIRouter]] = None
        self._router: Optional[APIRouter] = None

        self._plugin_config_path = Path(OsEnv.DATA_DIR) / "plugin_data" / self.key / "config.yaml"
        self._plugin_path = Path(OsEnv.DATA_DIR) / "plugin_data" / self.key
        self._scoped_config: Optional[ScopedPluginConfig] = None
        self._store = PluginStore(self)

        # 生命周期回调存储
        self._on_enabled_callbacks: List[Callable[[], Coroutine[Any, Any, None]]] = []
        self._on_disabled_callbacks: List[Callable[[], Coroutine[Any, Any, None]]] = []

        # 异步任务注册
        self._async_tasks: Dict[str, Callable[..., AsyncGenerator[Any, None]]] = {}

        # 命令注册
        self._commands: List[Any] = []  # List[PluginCommand]

    def reset_methods(self) -> None:
        self.init_method = None
        self.cleanup_method = None
        self.prompt_inject_method = None
        self.on_reset_method = None
        self.on_round_finished_method = None
        self.sandbox_methods = []
        self.webhook_methods = {}
        self._collect_methods_func = None
        # 重置路由相关
        self._router_func = None
        self._router = None
        # 重置异步任务
        self._async_tasks = {}
        # 重置命令
        self._commands = []

    def mount_init_method(self) -> Callable[[Callable[..., Coroutine[Any, Any, Any]]], Callable[..., Coroutine[Any, Any, Any]]]:
        """挂载初始化方法

        Returns:
            装饰器函数
        """

        def decorator(func: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Coroutine[Any, Any, Any]]:
            self.init_method = func
            return func

        return decorator

    def mount_config(self) -> Callable[[Type[T]], Type[T]]:
        """挂载配置类

        用于挂载配置类，提供给 Nekro 插件使用。

        Returns:
            装饰器函数
        """

        def decorator(cls: Type[T]) -> Type[T]:
            if not issubclass(cls, ConfigBase):
                raise TypeError("Config class must inherit from ConfigBase")
            self._Configs = cls
            self.get_config()
            return cls

        return decorator

    @property
    def config_class(self) -> Type[ConfigBase]:
        """插件注册的配置类型"""
        return self._Configs

    def get_global_config(self, config_cls: Type[T] = ConfigBase) -> T:
        """获取插件的**全局**配置实例

        不经过实例级覆盖，返回的就是落盘在 `plugin_data/{key}/config.yaml` 的那一份。
        WebUI 的全局配置读写、`save_config()` 落盘都必须走这里。
        插件侧读配置请继续用 `get_config()`，它会按当前实例上下文解析。

        Args:
            config_cls: 配置类型类，如果不提供则使用插件注册的配置类型

        Returns:
            全局配置实例，类型为指定的配置类型
        """
        self._plugin_config_path.parent.mkdir(parents=True, exist_ok=True)

        # 获取配置对象
        if not hasattr(self, "_config"):
            # 设置配置类的配置键和文件路径
            config_key = f"plugin_{self.key}"
            self._Configs.set_config_key(config_key)
            self._Configs.set_config_file_path(self._plugin_config_path)

            try:
                self._config = self._Configs.load_config(file_path=self._plugin_config_path, auto_register=True)
            except Exception:
                self.logger.exception(f"读取插件配置失败: {self.key} | 配置文件格式错误")
                raise
            self._config.dump_config(self._plugin_config_path)
        return cast(config_cls, self._config)  # pyright: ignore[reportInvalidTypeForm]

    def get_config(self, config_cls: Type[T] = ConfigBase) -> T:
        """获取插件配置

        返回的是**配置代理**（`ScopedPluginConfig`）：字段在属性访问时按当前实例上下文
        解析——实例有覆盖取实例值，否则取全局值。因此插件侧沿用
        `config = plugin.get_config(XxxConfig)` + `config.FIELD` 的写法即可，
        一行代码都不用改就获得了按实例分控的能力；单实例部署下取值与改造前完全一致。

        Args:
            config_cls: 配置类型类，如果不提供则使用插件注册的配置类型

        Returns:
            配置代理对象，类型标注为指定的配置类型
        """
        self.get_global_config()
        if self._scoped_config is None:
            self._scoped_config = ScopedPluginConfig(self)
        return cast(config_cls, self._scoped_config)  # pyright: ignore[reportInvalidTypeForm]

    def save_config(self, config: ConfigBase) -> None:
        """保存插件配置

        始终写入全局配置文件。传入 `get_config()` 返回的代理对象时会自动解包为全局实例，
        保证插件既有的 `cfg = plugin.get_config(X); cfg.A = 1; plugin.save_config(cfg)`
        写法照常工作。实例级覆盖请走 `scope.save_instance_config_overrides()`。

        Args:
            config: 配置实例或配置代理，必须对应已注册的配置类型

        Raises:
            TypeError: 当传入的配置实例类型与注册的配置类型不匹配时
            Exception: 当配置保存失败时
        """
        if isinstance(config, ScopedPluginConfig):
            config = config.resolve_global_config()

        if not isinstance(config, self._Configs):
            raise TypeError("存入的配置项类型有误")

        self._plugin_config_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # 更新内部配置实例
            self._config = config
            # 保存配置到文件
            config.dump_config(self._plugin_config_path)
            # 实例级配置是全局值与覆盖项合并出来的，全局变了必须整体重算
            invalidate_scoped_config(self.key)
            self.logger.debug(f"插件 {self.key} 配置保存成功")
        except Exception:
            self.logger.exception(f"保存插件配置失败: {self.key} | 配置文件写入错误")
            raise

    def get_plugin_path(self) -> Path:
        """获取插件路径
        
        .. deprecated:: 
            使用 get_plugin_data_dir() 代替，语义更明确
        """
        import warnings
        warnings.warn(
            "get_plugin_path() 已弃用，请使用 get_plugin_data_dir() 代替",
            DeprecationWarning,
            stacklevel=2,
        )
        self._plugin_path.mkdir(parents=True, exist_ok=True)
        return self._plugin_path
    
    def get_plugin_data_dir(self) -> Path:
        """获取插件数据目录
        
        返回插件专属的数据存储目录，用于存储插件的运行时数据、日志、缓存等。
        目录位于 DATA_DIR/plugin_data/{author}.{module_name}/
        
        Returns:
            Path: 插件数据目录的 Path 对象，目录会自动创建
            
        Example:
            ```python
            data_dir = plugin.get_plugin_data_dir()
            log_file = data_dir / "logs" / "app.log"
            ```
        """
        self._plugin_path.mkdir(parents=True, exist_ok=True)
        return self._plugin_path

    def mount_router(self) -> Callable[[Callable[[], APIRouter]], Callable[[], APIRouter]]:
        """挂载路由生成方法

        用于挂载插件的自定义路由，提供完整的FastAPI路由功能。
        插件路由将挂载在 /plugins/{plugin_key} 路径下。

        Example:
            ```python
            @plugin.mount_router()
            def create_router():
                router = APIRouter()

                @router.get("/hello")
                async def hello():
                    return {"message": "Hello from plugin!"}

                @router.post("/data")
                async def create_data(data: dict):
                    return {"received": data}

                return router
            ```

        Returns:
            装饰器函数
        """

        def decorator(func: Callable[[], APIRouter]) -> Callable[[], APIRouter]:
            self._router_func = func
            # 清除缓存的路由实例，确保重新生成
            self._router = None
            self.logger.debug(f"插件 {self.name} 路由生成函数注册完成")
            return func

        return decorator

    def get_plugin_router(self) -> Optional[APIRouter]:
        """获取插件路由实例

        Returns:
            Optional[APIRouter]: 插件路由实例，如果插件没有注册路由则返回None
        """

        # 如果已有缓存的路由实例，直接返回
        if self._router is not None:
            return self._router

        if not self._router_func and not self._is_webui_html_file():
            return None

        try:
            merged_router = APIRouter()
            has_routes = False

            if self._router_func:
                # 调用路由生成函数
                plugin_router = self._router_func()

                if not isinstance(plugin_router, APIRouter):
                    self.logger.error(f"插件 {self.name} 的路由生成函数必须返回 APIRouter 实例，实际返回: {type(plugin_router)}")
                    return None
                if plugin_router.routes:
                    merged_router.include_router(plugin_router)
                    has_routes = True

            webui_router = self._create_webui_file_router()
            if webui_router:
                merged_router.include_router(webui_router)
                has_routes = True

            if not has_routes:
                return None
            self._router = merged_router
        except Exception:
            self.logger.exception(f"插件 {self.name} 生成路由时出错")
            return None
        else:
            self.logger.info(f"插件 {self.name} 路由生成成功，路由数量: {len(self._router.routes)}")
            return self._router

    def get_webui_url_path(self) -> str | None:
        """获取前端可直接加载的插件 WebUI URL 路径。"""
        if not self.webui_path:
            return None
        if self.webui_path.startswith("/"):
            if not self._router_func:
                return None
            return f"/plugins/{self.key}{self.webui_path}"
        html_file = self._resolve_webui_html_file()
        if not html_file:
            return None
        return f"/plugins/{self.key}/__webui__/{html_file.name}"

    def get_webui_type(self) -> Literal["route", "file"] | None:
        """获取插件 WebUI 类型，用于前端决定可用操作。"""
        if not self.webui_path:
            return None
        if self.webui_path.startswith("/"):
            return "route" if self._router_func else None
        return "file" if self._resolve_webui_html_file() else None

    def _is_webui_html_file(self) -> bool:
        return bool(self.webui_path and not self.webui_path.startswith("/"))

    def _get_source_dir(self) -> Path | None:
        if not self._module or not self._module.__file__:
            return None
        return Path(self._module.__file__).resolve().parent

    def _is_package_module(self) -> bool:
        """插件是否以包的形式加载（有自己的目录），而不是共用目录里的单个 `.py` 文件"""
        return bool(self._module and self._module.__file__ and Path(self._module.__file__).name == "__init__.py")

    def _resolve_webui_html_file(self) -> Path | None:
        if not self._is_webui_html_file() or not self.webui_path:
            return None

        source_dir = self._get_source_dir()
        if not source_dir:
            self.logger.error(f"插件 {self.name} 未绑定模块文件，无法解析 WebUI HTML: {self.webui_path}")
            return None

        html_path = (source_dir / Path(*self.webui_path.split("/"))).resolve()
        source_root = source_dir.resolve()
        if not html_path.is_relative_to(source_root):
            self.logger.error(f"插件 {self.name} 的 WebUI HTML 越界: {self.webui_path}")
            return None
        if not html_path.is_file():
            self.logger.error(f"插件 {self.name} 的 WebUI HTML 文件不存在: {html_path}")
            return None
        return html_path

    def _create_webui_file_router(self) -> APIRouter | None:
        html_file = self._resolve_webui_html_file()
        if not html_file:
            return None

        router = APIRouter(include_in_schema=False)
        static_root = html_file.parent.resolve()
        # 本分支额外的安全硬化（与上游的有意差异）。上游以 HTML 所在目录为静态根、不加筛选，而这些路由不鉴权：
        # - HTML 放在插件根目录时，插件源码、本地数据库、配置文件都会被当成静态文件提供。这里只提供网页资源后缀，
        #   并拒绝 __pycache__ 与点开头的路径（白名单而不是黑名单：`.pyd`、`*.db` 之类不会漏网）；
        # - 单文件插件的模块文件位于所有插件共用的目录里，静态根会把别的插件的文件也带出来，
        #   因此只提供 HTML 入口本身，不提供同目录的其他文件。
        entry_only = not self._is_package_module()
        not_found = HTTPException(status_code=404, detail="File not found")

        async def serve_file(file_path: str) -> FileResponse:
            normalized_path = normpath(file_path).lstrip("/")
            if not normalized_path or normalized_path == ".":
                target_file = html_file
            else:
                if entry_only:
                    raise not_found
                relative_parts = tuple(part for part in normalized_path.split("/") if part not in {"", "."})
                if _is_forbidden_webui_asset(relative_parts):
                    raise not_found
                try:
                    target_file = (static_root / Path(*relative_parts)).resolve()
                    # 解析后的真实路径同样要检查：符号链接不能把越界或被拒绝的文件带进来
                    servable = (
                        target_file.is_relative_to(static_root)
                        and not _is_forbidden_webui_asset(target_file.relative_to(static_root).parts)
                        and target_file.is_file()
                    )
                except (OSError, ValueError):
                    # 路径里带 NUL 等非法字符时 resolve() / is_file() 会抛错，按「文件不存在」处理，不让它变成 500
                    servable = False
                if not servable:
                    raise not_found

            media_type, _encoding = mimetypes.guess_type(target_file.name)
            if target_file.suffix.lower() in {".html", ".htm"}:
                media_type = "text/html"
            return FileResponse(target_file, media_type=media_type)

        @router.get("/__webui__")
        async def plugin_webui_root() -> FileResponse:
            return await serve_file("")

        @router.get("/__webui__/")
        async def plugin_webui_index() -> FileResponse:
            return await serve_file("")

        @router.get("/__webui__/{file_path:path}")
        async def plugin_webui_static(file_path: str) -> FileResponse:
            return await serve_file(file_path)

        return router

    def mount_sandbox_method(
        self,
        method_type: SandboxMethodType,
        name: str,
        description: str = "",
    ) -> Callable[[Callable], Callable]:
        """挂载沙盒方法

        用于挂载沙盒方法，提供给 Nekro 插件使用。

        Args:
            method_type (SandboxMethodType): 方法类型
            name (str): 方法名称
            description (str): 方法描述

        Returns:
            装饰器函数
        """

        def decorator(func: Callable) -> Callable:
            func._method_type = method_type  # noqa: SLF001  # pyright: ignore[reportFunctionMemberAccess]
            self.sandbox_methods.append(SandboxMethod(method_type, name, description, func))
            # logger.debug(f"从插件 {self.name} 挂载沙盒方法 {name} 成功")
            return func

        return decorator

    def mount_prompt_inject_method(
        self,
        name: str,
        description: str = "",
    ) -> Callable[
        [Callable[[AgentCtx], Coroutine[Any, Any, PromptInjectResult]]],
        Callable[[AgentCtx], Coroutine[Any, Any, PromptInjectResult]],
    ]:
        """挂载提示注入方法

        用于挂载提示注入方法，在对话开始前执行，返回内容会注入到对话提示中。返回 `str` 时
        原样放进历史消息开头的插件上下文；返回 :class:`PromptInjection` 时可以要求按不可信内容
        包裹、或插到聊天记录的某个深度（见 `services/plugin/injection.py`）。

        Args:
            name (str): 挂载的提示注入方法的名称
            description (str): 挂载的提示注入方法的描述

        Returns:
            装饰器函数
        """

        def decorator(
            func: Callable[[AgentCtx], Coroutine[Any, Any, PromptInjectResult]],
        ) -> Callable[[AgentCtx], Coroutine[Any, Any, PromptInjectResult]]:
            self.prompt_inject_method = PromptInjectMethod(name, description, func)
            return func

        return decorator

    def mount_on_channel_reset(
        self,
    ) -> Callable[[Callable[[AgentCtx], Coroutine[Any, Any, Any]]], Callable[[AgentCtx], Coroutine[Any, Any, Any]]]:
        """挂载重置频道回调方法

        用于挂载重置频道时的回调方法，在频道重置时执行。

        Returns:
            装饰器函数
        """

        def decorator(func: Callable[[AgentCtx], Coroutine[Any, Any, Any]]) -> Callable[[AgentCtx], Coroutine[Any, Any, Any]]:
            self.on_reset_method = func
            return func

        return decorator

    def mount_on_round_finished(
        self,
    ) -> Callable[
        [Callable[[AgentCtx, str], Coroutine[Any, Any, Any]]],
        Callable[[AgentCtx, str], Coroutine[Any, Any, Any]],
    ]:
        """挂载一轮对话收尾时的回调

        每轮对话（真正发给模型的那种，不含预览）收尾时调用一次，第二个参数是本轮结局：
        `ok`（正常收尾）、`blocked`（被内容审核拦下或被服务商拒答）、`failed`、`cancelled`。
        只想在回复成功后做事的插件判断 `outcome == "ok"`。

        回调在后台任务里跑，核心不等它的结果，也不让它影响这一轮：抛错、超时只记在这个回调
        自己的故障统计里（单独计数、单独隔离，不连带插件的提示注入等回调）。只对在这个频道生效
        的插件调用。

        **回调要快速返回**：超时取全局插件超时，最多 30 秒（全局设成不限时也一样）。要调模型、
        写库这类长活，在回调里自己另起任务（并持有它的引用），不要在回调里等。

        Returns:
            装饰器函数
        """

        def decorator(
            func: Callable[[AgentCtx, str], Coroutine[Any, Any, Any]],
        ) -> Callable[[AgentCtx, str], Coroutine[Any, Any, Any]]:
            self.on_round_finished_method = func
            return func

        return decorator

    def mount_on_user_message(
        self,
    ) -> Callable[
        [Callable[[AgentCtx, ChatMessage], Coroutine[Any, Any, MsgSignal | None]]],
        Callable[[AgentCtx, ChatMessage], Coroutine[Any, Any, MsgSignal | None]],
    ]:
        """挂载消息回调方法

        用于挂载消息回调方法，在收到消息时执行。

        Returns:
            装饰器函数
        """

        def decorator(
            func: Callable[[AgentCtx, ChatMessage], Coroutine[Any, Any, MsgSignal | None]],
        ) -> Callable[[AgentCtx, ChatMessage], Coroutine[Any, Any, MsgSignal | None]]:
            self.on_user_message_method = func
            return func

        return decorator

    def mount_on_system_message(
        self,
    ) -> Callable[
        [Callable[[AgentCtx, str], Coroutine[Any, Any, MsgSignal | None]]],
        Callable[[AgentCtx, str], Coroutine[Any, Any, MsgSignal | None]],
    ]:
        """挂载系统消息回调方法"""

        def decorator(
            func: Callable[[AgentCtx, str], Coroutine[Any, Any, MsgSignal | None]],
        ) -> Callable[[AgentCtx, str], Coroutine[Any, Any, MsgSignal | None]]:
            self.on_system_message_method = func
            return func

        return decorator

    def mount_webhook_method(self, endpoint: str, name: str, description: str = "") -> Callable[[Callable], Callable]:
        """挂载 Webhook 方法

        用于挂载 Webhook 方法，提供 Webhook 事件触发能力

        Args:
            endpoint (str): 挂载的 Webhook 方法的端点
            name (str): 挂载的 Webhook 方法的名称
            description (str): 挂载的 Webhook 方法的描述

        Returns:
            装饰器函数
        """

        def decorator(func: Callable) -> Callable:
            self.webhook_methods[endpoint] = WebhookMethod(name, description, func)
            return func

        return decorator

    def mount_cleanup_method(
        self,
    ) -> Callable[[Callable[..., Coroutine[Any, Any, Any]]], Callable[..., Coroutine[Any, Any, Any]]]:
        """挂载清理方法
    
        用于挂载清理方法,在插件卸载时执行。
    
        Returns:
            装饰器函数
        """
    
        def decorator(func: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Coroutine[Any, Any, Any]]:
            self.cleanup_method = func
            return func
    
        return decorator

    def mount_async_task(
        self,
        task_type: str,
    ) -> Callable[[Callable[..., AsyncGenerator[Any, None]]], Callable[..., AsyncGenerator[Any, None]]]:
        """挂载异步任务

        用于注册异步任务函数，任务函数通过 yield TaskCtl 报告状态，
        通过 AsyncTaskHandle.wait() 暂停等待外部信号。

        Args:
            task_type: 任务类型标识，用于启动和查询任务

        Returns:
            装饰器函数

        Example:
            ```python
            @plugin.mount_async_task("video_gen")
            async def video_generation(handle: AsyncTaskHandle, prompt: str):
                yield TaskCtl.report_progress("等待审批")
                approved = await handle.wait("approval", timeout=3600)
                if approved:
                    yield TaskCtl.success("完成", data=url)
                else:
                    yield TaskCtl.fail("审批被拒绝")
            ```
        """
        def decorator(func: Callable[..., AsyncGenerator[Any, None]]) -> Callable[..., AsyncGenerator[Any, None]]:
            self._async_tasks[task_type] = func
            # 注册到全局 TaskRunner
            TaskRunner().register_task_type(task_type, func)
            self.logger.debug(f"插件 {self.name} 注册异步任务: {task_type}")
            return func

        return decorator
    
    def on_enabled(self) -> Callable[[Callable[[], Coroutine[Any, Any, None]]], Callable[[], Coroutine[Any, Any, None]]]:
        """装饰器：注册插件启用时的回调函数
    
        回调将在插件状态变更为启用后立即执行（在配置保存和路由挂载之前）。
    
        Returns:
            装饰器函数
    
        Example:
            ```python
            @plugin.on_enabled()
            async def handle_enabled():
                print("插件已启用")
            ```
        """
    
        def decorator(func: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], Coroutine[Any, Any, None]]:
            self._on_enabled_callbacks.append(func)
            return func
    
        return decorator
    
    def on_disabled(self) -> Callable[[Callable[[], Coroutine[Any, Any, None]]], Callable[[], Coroutine[Any, Any, None]]]:
        """装饰器：注册插件禁用时的回调函数
    
        回调将在插件状态变更为禁用后立即执行（在配置保存之前）。
    
        Returns:
            装饰器函数
    
        Example:
            ```python
            @plugin.on_disabled()
            async def handle_disabled():
                print("插件已禁用")
            ```
        """
    
        def decorator(func: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], Coroutine[Any, Any, None]]:
            self._on_disabled_callbacks.append(func)
            return func
    
        return decorator

    def mount_command(
        self,
        name: str,
        description: str,
        aliases: Optional[List[str]] = None,
        permission: Any = None,
        usage: str = "",
        category: str = "plugin",
        tags: Optional[List[str]] = None,
        internal: bool = False,
        i18n_description: Optional[Dict[str, str]] = None,
        i18n_usage: Optional[Dict[str, str]] = None,
        i18n_category: Optional[Dict[str, str]] = None,
    ) -> Callable[[Callable], Callable]:
        """挂载命令

        用于注册插件命令，插件加载时自动注册到命令系统。
        命名空间和 source 由框架自动填充为插件的 module_name 和 key。

        Args:
            name: 命令名
            description: 命令描述
            aliases: 别名列表
            permission: 权限级别 (CommandPermission 枚举值)
            usage: 使用说明
            category: 分类
            tags: 标签 (便于 Agent 检索)
            internal: 是否为内部命令 (不在帮助列表和补全中显示)
            i18n_description: 国际化描述 (由 i18n_text() 创建)
            i18n_usage: 国际化用法说明
            i18n_category: 国际化分类

        Returns:
            装饰器函数

        Example:
            ```python
            from nekro_agent.services.command.base import CommandPermission
            from nekro_agent.services.command.schemas import Arg, CommandExecutionContext, CommandResponse
            from nekro_agent.services.command.ctl import CmdCtl

            @plugin.mount_command(
                name="weather",
                description="查询天气",
                aliases=["天气"],
                permission=CommandPermission.PUBLIC,
            )
            async def weather_command(
                context: CommandExecutionContext,
                location: Annotated[str, Arg("城市名称", positional=True)] = "北京",
            ) -> CommandResponse:
                return CmdCtl.success(f"{location} 当前天气：晴")
            ```
        """

        def decorator(func: Callable) -> Callable:
            from nekro_agent.services.command.base import CommandPermission, PluginCommand

            perm = permission or CommandPermission.PUBLIC
            if isinstance(perm, str):
                perm = CommandPermission(perm)

            cmd = PluginCommand(
                name=name,
                description=description,
                i18n_description=i18n_description,
                aliases=aliases or [],
                permission=perm,
                usage=usage,
                i18n_usage=i18n_usage,
                category=category,
                i18n_category=i18n_category,
                source=self.key,
                namespace=self.module_name,
                tags=tags or [],
                internal=internal,
                execute_func=func,
            )
            self._commands.append(cmd)
            return func

        return decorator

    def mount_command_group(
        self,
        name: str,
        description: str,
        permission: Any = None,
        category: str = "plugin",
        tags: Optional[List[str]] = None,
    ) -> Any:
        """创建命令组

        命令组将多个子命令组织在一个前缀下，子命令在注册表中展平为 ``group.sub`` 的形式。

        Args:
            name: 组名（如 "config"）
            description: 组描述
            permission: 默认权限级别（子命令可覆盖）
            category: 默认分类
            tags: 默认标签

        Returns:
            CommandGroup 实例，通过其 ``command()`` 装饰器注册子命令

        Example:
            ```python
            config_group = plugin.mount_command_group(
                name="config",
                description="配置管理",
                permission=CommandPermission.ADVANCED,
            )

            @config_group.command(name="set", description="设置配置项")
            async def config_set(context, key, value):
                return CmdCtl.success(f"已设置 {key} = {value}")
            ```

            调用: ``/config.set key:"model" value:"gpt-4"``
        """
        from nekro_agent.services.command.group import CommandGroup

        return CommandGroup(
            group_name=name,
            description=description,
            commands_list=self._commands,
            source=self.key,
            namespace=self.module_name,
            permission=permission,
            category=category,
            tags=tags,
        )

    async def trigger_callbacks(self, event_type: Literal["enabled", "disabled"]) -> None:
        """触发回调函数

        并行执行所有已注册的回调函数，异常会被捕获并记录日志。
        此方法由插件管理器在启用/禁用插件时调用。
    
        Args:
            event_type: 事件类型（"enabled" 或 "disabled"）
        """
        callbacks = self._on_enabled_callbacks if event_type == "enabled" else self._on_disabled_callbacks
    
        if not callbacks:
            return
    
        self.logger.debug(f"插件 {self.name} 触发 {event_type} 回调，共 {len(callbacks)} 个")
    
        # 并行执行所有回调；单个回调卡住不该把插件开关操作永远挂在那儿
        limit = float(config.PLUGIN_LIFECYCLE_TIMEOUT_SECONDS)
        pending = [asyncio.wait_for(cb(), timeout=limit) if limit > 0 else cb() for cb in callbacks]
        results = await asyncio.gather(*pending, return_exceptions=True)

        # 记录异常
        for i, result in enumerate(results):
            if isinstance(result, asyncio.TimeoutError):
                self.logger.error(f"插件 {self.name} 的 {event_type} 回调超过 {limit:.0f}s 未返回 (回调索引 {i})，已放弃等待")
            elif isinstance(result, Exception):
                self.logger.error(f"插件 {self.name} 的 {event_type} 回调执行失败 (回调索引 {i}): {result}")

    async def collect_available_methods(self, ctx: AgentCtx) -> List[SandboxMethod]:
        """收集可用方法

        Args:
            ctx (AgentCtx): 上下文

        Returns:
            List[SandboxMethod]: 可用方法列表
        """
        if self._collect_methods_func:
            # 收集函数坏掉时回落到"一个方法都不给"而不是"全给"：这个钩子的用途正是
            # 按上下文隐藏方法（例如某实例没开这项能力），失败时放开等于把本该隐藏的
            # 能力交给模型。
            collected = await call_plugin(self, "collect_methods", self._collect_methods_func, ctx, default=[])
            available_methods: List[SandboxMethod] = []
            for func in collected:
                if isinstance(func, SandboxMethod):
                    available_methods.append(func)
                else:
                    for method in self.sandbox_methods:
                        if method.func == func:
                            available_methods.append(method)
                            break
                    else:
                        # 插件返回了没挂载过的东西。过去这里直接抛，会顺着提示词渲染把整轮对话带崩；
                        # 现在只丢掉这一个条目并留下日志——插件写错不该让所有实例停摆。
                        self.logger.warning(
                            f"插件 {self.name} 的 collect_methods 返回了未挂载的方法 "
                            f"{getattr(func, '__name__', func)!s}，已忽略该条目。",
                        )
            return available_methods
        return self.sandbox_methods

    def mount_collect_methods(
        self,
    ) -> Callable:
        """挂载收集可用方法的重写函数

        此装饰器允许插件开发者自定义如何收集和过滤可用的方法，根据上下文决定哪些方法对当前用户可用。

        Returns:
            装饰器函数
        """

        def decorator(func: CollectMethodsFunc) -> CollectMethodsFunc:
            self._collect_methods_func = func
            return func

        return decorator

    @property
    def is_enabled(self) -> bool:
        """插件的**进程级加载状态**

        表示插件对象在本进程内是否活着——生命周期回调是否已触发、自定义路由是否已挂载。
        它与 `services/plugin/scope.py` 里的「作用域可见性」（某实例 / 某会话是否看得见
        这个插件）是两个正交概念：只要任意一个作用域还需要它，进程级状态就必须为启用；
        某个实例是否真的能用到它，由 `scope.resolve_active_plugins()` 另行解析。
        """
        return self._is_enabled

    async def enable(self) -> None:
        """启用插件并触发相应的回调函数

        变更的是**进程级加载状态**，不是某个实例 / 会话的可见性。
        此方法是启用插件的统一入口，确保无论从何处调用，
        都会自动执行状态变更和触发回调事件。
        """
        if self._is_enabled:
            return  # 已经启用，无需重复操作

        # 手动重新启用是用户"我处理过了"的表态，自动隔离应当就此清掉
        release_quarantine(self.key, reason="插件被重新启用")
        self._is_enabled = True
        # 自动触发启用回调
        await self.trigger_callbacks("enabled")

    async def disable(self) -> None:
        """禁用插件并触发相应的回调函数

        变更的是**进程级加载状态**：调用后插件在本进程内彻底下线，任何作用域都用不了它。
        只想让某个实例 / 会话看不见插件时，请写作用域覆盖而不是调用本方法。
        此方法是禁用插件的统一入口，确保无论从何处调用，
        都会自动执行状态变更和触发回调事件。
        """
        if not self._is_enabled:
            return  # 已经禁用，无需重复操作
        
        self._is_enabled = False
        # 自动触发禁用回调
        await self.trigger_callbacks("disabled")

    @property
    def key(self) -> str:
        return self._key

    def get_name(self, lang: SupportedLang = SupportedLang.ZH_CN) -> str:
        """获取本地化插件名称

        Args:
            lang: 目标语言

        Returns:
            本地化插件名称，如果无对应翻译则返回默认名称
        """
        return get_text(self.i18n_name, self.name, lang)

    def get_description(self, lang: SupportedLang = SupportedLang.ZH_CN) -> str:
        """获取本地化插件描述

        Args:
            lang: 目标语言

        Returns:
            本地化插件描述，如果无对应翻译则返回默认描述
        """
        return get_text(self.i18n_description, self.description, lang)

    async def render_prompt_injection(self, ctx: AgentCtx) -> PromptInjectResult:
        """调用提示注入方法，取回原始返回值（`str` 或 :class:`PromptInjection`）

        包裹与落点由核心按本轮令牌决定（`services/plugin/injection.py`），插件自己不拼标签。
        """
        if self.prompt_inject_method:
            # 提示注入直接坐在主对话链路上：这里抛异常或卡住，整轮回复就没了。
            # 回落到空串等于本轮少一段插件上下文，主对话照常。
            return await call_plugin(self, "prompt_inject", self.prompt_inject_method.func, ctx, default="")
        return ""

    async def render_inject_prompt(self, ctx: AgentCtx) -> str:
        """渲染提示注入提示词

        返回 `str` 的注入方法与以前逐字相同。这个入口的调用方之一是沙盒里激活插件的方法，
        返回值会回到沙盒、还会落成一条系统消息，所以**这里绝不用本轮令牌**：结构化返回只在
        `untrusted=False` 且不指定深度时给出正文，其余（不可信的、要插到深度的）一律不给。
        核心组装提示词走的是 :meth:`render_prompt_injection`，由核心按令牌包裹。

        Returns:
            str: 插件提示
        """
        raw = await self.render_prompt_injection(ctx)
        if isinstance(raw, str):
            return raw
        return render_injection_inline(raw, module_name=self.module_name)

    async def render_sandbox_methods_prompt(self, ctx: AgentCtx) -> str:
        """渲染沙盒方法提示词

        Returns:
            str: 沙盒方法提示
        """
        prompts: List[str] = []
        methods = await self.collect_available_methods(ctx) if self._collect_methods_func else self.sandbox_methods
        for method in methods:
            agent_tag = (
                " - **[AGENT METHOD - STOP AFTER CALL]**"
                if method.method_type in [SandboxMethodType.AGENT, SandboxMethodType.MULTIMODAL_AGENT]
                else ""
            )
            doc = (method.func.__doc__ or "").strip()
            if not doc:
                # 缺文档的方法过去会被整条丢弃，模型完全看不到这个能力存在，只能自己用通用库硬实现。
                # 第三方插件很容易漏写文档，这里退回到注册元信息 + 调用签名：粗糙的描述也远好过能力凭空消失。
                self.logger.warning(f"方法 {method.func.__name__} 没有可用的文档注解，已回落到注册元信息。")
                try:
                    params = [str(param) for name, param in inspect.signature(method.func).parameters.items() if name != "_ctx"]
                except (TypeError, ValueError):
                    params = []
                headline = f"{method.name}: {method.description}" if method.description else method.name
                doc = f"{headline}\nSignature: {method.func.__name__}({', '.join(params)})"
            prompts.append(f"* {method.func.__name__}{agent_tag}\n{doc}")
        return "\n".join(prompts)

    @property
    def store(self) -> "PluginStore":
        return PluginStore(self)

    async def _allow_plugin_push(self, chat_key: str, action: str) -> bool:
        """后台推送的统一前置判定：插件在这个频道生效、且频道没被标成 Bot 不在群，才允许往下走

        进程级 `is_enabled` 只说明插件在进程里活着；租户可以在自己的实例 / 频道上关掉它。
        后台推送、定时任务、Webhook 回调都不经过会话里的作用域过滤，必须在这里补上。
        判不出来（多租户部署里频道行缺失）按不生效处理。

        被定期核对标成「Bot 不在群」的频道同样跳过：往 Bot 已经被移出的群里推消息、发图，
        只会被 QQ 拒绝（`你已被移出该群`），系统消息开的回合还要白调一轮模型。只读标记，
        不停用频道；群里再有人说话，入站那一步就把标记改回「在群」。
        """
        if not await plugin_effective_in_chat(self.key, chat_key):
            self.logger.debug(f"插件在频道未生效，跳过推送: plugin={self.key} chat={chat_key} action={action}")
            return False
        # 函数内导入：频道模型反过来依赖插件收集器，模块级导入会成环
        from nekro_agent.models.db_chat_channel import is_chat_key_marked_bot_not_in_group

        if await is_chat_key_marked_bot_not_in_group(chat_key):
            self.logger.debug(f"频道已标记 Bot 不在群，跳过推送: plugin={self.key} chat={chat_key} action={action}")
            return False
        return True

    async def push_system_message(
        self,
        chat_key: str,
        agent_messages: Union[str, List["AgentMessageSegment"]],
        trigger_agent: bool = False,
        db_chat_channel: Optional["DBChatChannel"] = None,
    ) -> bool:
        """以插件身份推送系统消息

        先判「插件在这个频道是否生效」，不生效直接返回 `False`、不调下游。
        生效时转给 `message_service.push_system_message` 并返回 `True`（下游没有返回值；
        需要下游结果时直接调 `message_service`，不要绕过这里的判定）。
        """
        if not await self._allow_plugin_push(chat_key, "push_system_message"):
            return False
        from nekro_agent.services.message_service import message_service

        await message_service.push_system_message(
            chat_key=chat_key,
            agent_messages=agent_messages,
            trigger_agent=trigger_agent,
            db_chat_channel=db_chat_channel,
        )
        return True

    async def push_untrusted_message(
        self,
        chat_key: str,
        result: "UntrustedToolResult",
        trigger_agent: bool = False,
        db_chat_channel: Optional["DBChatChannel"] = None,
    ) -> bool:
        """以插件身份推送外部内容（不可信块）

        先判「插件在这个频道是否生效」，不生效直接返回 `False`、不调下游。
        生效时转给 `message_service.push_untrusted_message` 并返回 `True`。
        """
        if not await self._allow_plugin_push(chat_key, "push_untrusted_message"):
            return False
        from nekro_agent.services.message_service import message_service

        await message_service.push_untrusted_message(
            chat_key=chat_key,
            result=result,
            trigger_agent=trigger_agent,
            db_chat_channel=db_chat_channel,
        )
        return True

    async def schedule_agent_task(
        self,
        chat_key: Optional[str] = None,
        message: Optional[ChatMessage] = None,
        ctx: Optional[AgentCtx] = None,
        *,
        bypass_channel_quota: bool = False,
        trigger: Optional["RoundTrigger"] = None,
    ) -> bool:
        """以插件身份调度一轮 Agent

        先判「插件在目标频道是否生效」，不生效直接返回 `False`、不调下游。
        生效时转给 `message_service.schedule_agent_task` 并返回 `True`。
        目标频道优先取 `message.chat_key`，没有消息时取 `chat_key`，与下游一致。
        """
        target_chat_key = (message.chat_key if message is not None else None) or chat_key or ""
        if not await self._allow_plugin_push(target_chat_key, "schedule_agent_task"):
            return False
        from nekro_agent.services.message_service import message_service

        await message_service.schedule_agent_task(
            chat_key=chat_key,
            message=message,
            ctx=ctx,
            bypass_channel_quota=bypass_channel_quota,
            trigger=trigger,
        )
        return True

    async def send_text(
        self,
        chat_key: str,
        message: str,
        ctx: AgentCtx,
        *,
        record: bool = True,
        ref_msg_id: Optional[str] = None,
    ) -> bool:
        """以插件身份往频道发文本

        先判「插件在这个频道是否生效」，不生效直接返回 `False`、不调下游。
        生效时转给 `nekro_agent.api.message.send_text` 并返回 `True`。
        """
        if not await self._allow_plugin_push(chat_key, "send_text"):
            return False
        from nekro_agent.api.message import send_text

        await send_text(chat_key, message, ctx, record=record, ref_msg_id=ref_msg_id)
        return True

    async def send_image(
        self,
        chat_key: str,
        image_path: str,
        ctx: AgentCtx,
        *,
        record: bool = True,
        ref_msg_id: Optional[str] = None,
    ) -> bool:
        """以插件身份往频道发图片

        先判「插件在这个频道是否生效」，不生效直接返回 `False`、不调下游。
        生效时转给 `nekro_agent.api.message.send_image` 并返回 `True`。
        """
        if not await self._allow_plugin_push(chat_key, "send_image"):
            return False
        from nekro_agent.api.message import send_image

        await send_image(chat_key, image_path, ctx, record=record, ref_msg_id=ref_msg_id)
        return True

    async def send_file(
        self,
        chat_key: str,
        file_path: str,
        ctx: AgentCtx,
        *,
        record: bool = True,
        ref_msg_id: Optional[str] = None,
    ) -> bool:
        """以插件身份往频道发文件

        先判「插件在这个频道是否生效」，不生效直接返回 `False`、不调下游。
        生效时转给 `nekro_agent.api.message.send_file` 并返回 `True`。
        """
        if not await self._allow_plugin_push(chat_key, "send_file"):
            return False
        from nekro_agent.api.message import send_file

        await send_file(chat_key, file_path, ctx, record=record, ref_msg_id=ref_msg_id)
        return True

    async def send_segments(
        self,
        chat_key: str,
        segments: List["AgentMessageSegment"],
        ctx: AgentCtx,
        *,
        record: bool = True,
        ref_msg_id: Optional[str] = None,
    ) -> bool:
        """以插件身份往频道发图文混合消息

        先判「插件在这个频道是否生效」，不生效直接返回 `False`、不调下游。
        生效时转给 `nekro_agent.api.message.send_segments` 并返回 `True`。
        """
        if not await self._allow_plugin_push(chat_key, "send_segments"):
            return False
        from nekro_agent.api.message import send_segments

        await send_segments(chat_key, segments, ctx, record=record, ref_msg_id=ref_msg_id)
        return True

    def _update_plugin_type(self, is_builtin: bool, is_package: bool) -> None:
        if is_builtin:
            self._is_builtin = True
            self._is_package = False
        elif is_package:
            self._is_builtin = False
            self._is_package = True

    @property
    def is_builtin(self) -> bool:
        return self._is_builtin

    @property
    def is_package(self) -> bool:
        return self._is_package

    def get_vector_collection_name(self, key: str = "") -> str:
        """获取向量数据库集合名称

        Args:
            key (str): 向量数据库集合名称

        Returns:
            str: 向量数据库集合名称
        """
        return f"{self.key}-{key}" if key else self.key

    def _set_module(self, module: "ModuleType") -> None:
        """设置模块对象，由加载器调用"""
        self._module = module

    async def get_docs(self) -> Optional[str]:
        """获取插件文档

        优先级:
        1. 插件模块同级目录下的 README.md 文件
        2. 插件模块的 __doc__
        """
        if not self._module or not self._module.__file__:
            return None

        try:
            module_path = Path(self._module.__file__)
            readme_path = module_path.parent / "README.md"

            if readme_path.is_file():
                async with aiofiles.open(readme_path, "r", encoding="utf-8") as f:
                    return await f.read()

            if self._module.__doc__:
                return self._module.__doc__.strip()
        except Exception as e:
            self.logger.exception(f"获取插件 {self.key} 文档失败")
            return f"获取文档失败: {e}"

        return None


class PluginStore:

    def __init__(self, plugin: NekroPlugin):
        self._plugin = plugin

    async def get(self, chat_key: str = "", user_key: str = "", store_key: str = "") -> Optional[str]:
        """获取插件存储

        Args:
            chat_key (str): 对话键
            user_key (str): 用户键
            store_key (str): 存储键

        Returns:
            Optional[str]: 存储值
        """
        query_result = await DBPluginData.filter(
            plugin_key=self._plugin.key,
            target_chat_key=chat_key,
            target_user_id=user_key,
            data_key=store_key,
        ).first()
        return query_result.data_value if query_result else None

    async def set(self, chat_key: str = "", user_key: str = "", store_key: str = "", value: str = "") -> Literal[0, 1]:
        """设置插件存储

        Args:
            chat_key (str): 对话键
            user_key (str): 用户键
            store_key (str): 存储键
            value (str): 存储值

        Returns:
            int: 设置状态: 0 表示创建成功，1 表示更新成功
        """
        # 插件存的值可能来自协议端文本（昵称、消息原文）。PostgreSQL 的 text 列不收 0x00，而这张表
        # 不在 ORM 保存信号的兜底范围里，`filter().update()` 也不走保存信号，所以两条写入分支都用清理过的值
        safe_value = strip_nul(value)
        if await DBPluginData.filter(
            plugin_key=self._plugin.key,
            target_chat_key=chat_key,
            target_user_id=user_key,
            data_key=store_key,
        ).first():
            await DBPluginData.filter(
                plugin_key=self._plugin.key,
                target_chat_key=chat_key,
                target_user_id=user_key,
                data_key=store_key,
            ).update(data_value=safe_value)
            return 1
        await DBPluginData.create(
            plugin_key=self._plugin.key,
            target_chat_key=chat_key,
            target_user_id=user_key,
            data_key=store_key,
            data_value=safe_value,
        )
        return 0

    async def delete(self, chat_key: str = "", user_key: str = "", store_key: str = "") -> Literal[0, 1]:
        """删除插件存储

        Args:
            chat_key (str): 对话键
            user_key (str): 用户键
            store_key (str): 存储键

        Returns:
            int: 删除状态: 0 表示删除成功，1 表示删除失败
        """
        record = await DBPluginData.filter(
            plugin_key=self._plugin.key,
            target_chat_key=chat_key,
            target_user_id=user_key,
            data_key=store_key,
        ).first()
        if record:
            await record.delete()
            return 0
        return 1


def _validate_name(name: str, field_name: str) -> str:
    """命名检查，必须由英文字母、数字、下划线组成，且不能以数字开头"""
    name = name.strip()
    if not name:
        raise ValueError(f"{field_name} cannot be empty")
    if not re.match(r"^[a-zA-Z0-9_]+$", name):
        raise ValueError(f"{field_name} must contain only letters, numbers, and underscores")
    if re.match(r"^\d", name):
        raise ValueError(f"{field_name} must not start with a number")
    return name


# 插件 WebUI 静态路由允许提供的文件后缀：只有网页本身需要的资源
_WEBUI_ASSET_SUFFIXES = frozenset(
    {
        ".html", ".htm", ".js", ".mjs", ".css", ".map", ".json", ".wasm", ".txt",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico",
        ".woff", ".woff2", ".ttf", ".otf",
        ".mp3", ".ogg", ".wav", ".mp4", ".webm",
    },
)


def _is_forbidden_webui_asset(path_parts: tuple[str, ...]) -> bool:
    """WebUI 静态请求路径是否不该提供：不在网页资源后缀白名单内，或经过 __pycache__ / 点开头的路径段"""
    if not path_parts:
        return False
    if any(part == "__pycache__" or part.startswith(".") for part in path_parts):
        return True
    return Path(path_parts[-1]).suffix.lower() not in _WEBUI_ASSET_SUFFIXES


def _normalize_webui_path(webui_path: str | None) -> str | None:
    """规范化插件 WebUI 路径，只允许插件路由路径或插件内 HTML 相对路径。"""
    if webui_path is None:
        return None

    normalized = webui_path.strip()
    if not normalized:
        return None
    if "\\" in normalized:
        raise ValueError("webui_path 不支持反斜杠路径")
    if "://" in normalized or normalized.startswith("//"):
        raise ValueError("webui_path 不支持外部 URL")

    if normalized.startswith("/"):
        if "?" in normalized or "#" in normalized:
            raise ValueError("webui_path 路由模式只支持路径，不支持查询参数或锚点")
        if any(part in {".", ".."} for part in normalized.split("/")):
            raise ValueError("webui_path 路由模式包含非法路径片段")

        route_path = normpath(normalized)
        if not route_path.startswith("/"):
            raise ValueError("webui_path 路由模式必须是插件内绝对路径")
        if normalized.endswith("/") and route_path != "/":
            route_path = f"{route_path}/"
        return route_path

    normalized = normpath(normalized)
    path = Path(normalized)
    if normalized == ".." or normalized.startswith("../") or path.is_absolute():
        raise ValueError("webui_path 不允许越过插件目录")
    if path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("webui_path 使用相对路径时必须指向 .html 或 .htm 文件")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("webui_path 包含非法路径片段")
    return normalized
