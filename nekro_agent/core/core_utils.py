import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Literal,
    Optional,
    Type,
    TypeVar,
    Union,
)
from urllib.parse import quote_plus

import nonebot
import yaml
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, PrivateAttr

from nekro_agent.schemas.i18n import I18nDict


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """原子地写一个文本文件：要么是完整的新内容，要么还是旧内容

    `Path.write_text` 先截断再写。写到一半失败（磁盘满、进程被杀、机器断电）留下的是一个
    被截断的 YAML，而 `CoreConfig` 的加载是严格校验的——下次启动直接起不来，运维只能靠手工
    重建配置。配置是「丢了就回不来」的那类数据，所以先写临时文件、`os.replace` 换上去：
    换名在 POSIX 与 Windows 上都是原子的，读者永远只会看到完整的旧内容或完整的新内容。

    临时文件必须落在**目标同目录**：只有同一文件系统内 `os.replace` 才是原子重命名，
    跨盘会退化成复制 + 删除。名字以点开头，不会被配置目录的扫描当成一个配置文件。

    刻意不用 `tempfile.mkstemp`：它建出来的文件权限固定 0600，而 `write_text` 新建的文件是
    0666 & ~umask（通常 0644）。配置里可能有挂载卷上其他用户需要读的内容，悄无声息地把它
    变严就是一次权限回归。这里用 `os.open(..., 0o666)` 让 umask 决定，与 `write_text` 一致。

    目标已存在时先 `copymode` 把原权限位搬过来：`write_text` 是就地改写，权限位本来就不变，
    换成"新建再换名"之后必须显式保留，否则同样是一次权限回归。

    任何一步失败都把临时文件删掉再原样抛出（用 `raise`，不包装）：调用方需要看到真实的
    `OSError`（磁盘满 / 权限不足），而不是一个被包装过的类型。成功路径上目录里不留临时文件。
    """
    tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    # 只在 O_CREAT | O_EXCL 下生效的权限位；O_BINARY 是 Windows 上防止 CRT 再做一次换行翻译
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd: Optional[int] = os.open(tmp_path, flags, 0o666)
    file = None
    try:
        file = os.fdopen(fd, "w", encoding=encoding)
        # 句柄所有权已交给 file，下面出错时由 file.close() 负责关掉它
        fd = None
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
        file.close()
        file = None
        if path.exists():
            # 就地改写时权限位不会变；这里是新文件，必须显式搬过来
            shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        if file is not None:
            try:
                file.close()
            except OSError:
                pass
        elif fd is not None:
            # `os.fdopen` 就失败了的路径：句柄还没交出去，必须自己关
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            # 临时文件可能压根没建出来（os.open 就失败了），或已经被 replace 走
            pass
        raise


class ArgTypes:

    @staticmethod
    def _use_arg(arg_key: str, default: Any = None) -> Any:
        if arg_key in sys.argv:
            index = sys.argv.index(arg_key)
            if index + 1 < len(sys.argv):
                return sys.argv[index + 1]
        return default

    @staticmethod
    def Str(key: str, default: str = "") -> str:
        return str(ArgTypes._use_arg(key, default))

    @staticmethod
    def Int(key: str, default: int = 0) -> int:
        return int(ArgTypes._use_arg(key, default))

    @staticmethod
    def Float(key: str, default: float = 0.0) -> float:
        return float(ArgTypes._use_arg(key, default))

    @staticmethod
    def Bool(key: str) -> bool:
        return key in sys.argv


class OsEnvTypes:

    @staticmethod
    def _use_env(env_key: str, default: Union[Any, Callable[[], Any]]) -> Any:
        if callable(default):
            return default()
        return os.environ.get(f"NEKRO_{env_key.upper()}", default)

    @staticmethod
    def Str(key: str, default: Union[str, Callable[[], str]] = "") -> str:
        if callable(default):
            return default()
        return str(OsEnvTypes._use_env(key, default))

    @staticmethod
    def Int(key: str, default: Union[int, Callable[[], int]] = 0) -> int:
        if callable(default):
            return default()
        return int(OsEnvTypes._use_env(key, default))

    @staticmethod
    def Float(key: str, default: Union[float, Callable[[], float]] = 0.0) -> float:
        if callable(default):
            return default()
        return float(OsEnvTypes._use_env(key, default))

    @staticmethod
    def Bool(key: str, default: Union[bool, Callable[[], bool]] = False) -> bool:
        if callable(default):
            default = default()
        return str(OsEnvTypes._use_env(key, default)).lower() == "true"


class ExtraField(BaseModel):
    """配置字段的扩展元数据

    用于定义配置字段的 UI 行为和国际化支持。
    所有字段描述都应添加对应的 i18n 字段以支持多语言。
    """

    is_hidden: bool = Field(default=False, title="配置项可见性控制", description="设置为True时，该配置项将在WebUI界面中被隐藏")
    is_secret: bool = Field(
        default=False,
        title="敏感信息保护",
        description="设置为True时，输入内容将以密码形式('••••')显示，用于保护API密钥等敏感数据",
    )
    placeholder: str = Field(default="", title="输入提示文本", description="当字段未填写时在输入框中显示的提示文本")
    is_textarea: bool = Field(default=False, title="多行文本支持", description="设置为True时，将使用多行文本区域而非单行输入框")
    ref_model_groups: bool = Field(
        default=False,
        title="模型组引用标识",
        description="设置为True时，表示该字段需要从系统中已配置的模型组中选择",
    )
    ref_email_accounts_send_enabled: bool = Field(
        default=False,
        title="引用启用发信的邮箱账户",
        description="设置为True时，表示该字段需要从 Email 适配器配置中已启用发信的邮箱账户列表中选择",
    )
    model_type: Literal["chat", "embedding", "draw"] = Field(
        default="chat",
        title="模型类型规范",
        description="指定引用的模型类型标识符，仅在ref_model_groups为True时生效",
    )
    required: bool = Field(
        default=False,
        title="必填字段标识",
        description="设置为True时，前端将验证该字段必须填写才能提交表单",
    )
    overridable: bool = Field(
        default=False,
        title="可覆盖字段标识",
        description="设置为True时，该配置项将可以在适配器或频道级进行独立覆盖",
    )
    scope_owned_secret: bool = Field(
        default=False,
        title="凭据归属跟随作用域",
        description=(
            "设置为True时，该敏感项写在覆盖层（实例/频道）上的取值归该作用域的主人所有，"
            "租户可以在自己名下的作用域里读写它；全局/适配器等跨租户共享层仍然只有平台方可写。"
            "仅对 is_secret 项有意义，见 routers/config.py:_assert_tenant_may_write"
        ),
    )
    is_outbound_url: bool = Field(
        default=False,
        title="出网目标地址",
        description=(
            "设置为True时，该项的取值会让服务端主动发起出站请求，写入前必须过 SSRF 校验"
            "（租户提交的地址强制公网，见 model_group_service.assert_outbound_urls_allowed）"
        ),
    )
    sub_item_name: str = Field(
        default="项目",
        title="子项名称",
        description="指定该配置项为列表时，子项目的元素名称，默认值为'项目'",
    )
    load_to_sysenv: bool = Field(
        default=False,
        title="环境变量加载控制",
        description="设置为True时，该配置项的值将被加载到系统环境变量中",
    )
    load_sysenv_as: str = Field(
        default="",
        title="环境变量名称定义",
        description="指定将配置项加载为环境变量时使用的变量名，仅在load_to_env为True时生效",
    )
    load_to_nonebot_env: bool = Field(
        default=False,
        title="nonebot环境变量加载控制",
        description="设置为True时，该配置项的值将被加载到nonebot的环境变量中",
    )
    load_nbenv_as: str = Field(
        default="",
        title="nonebot环境变量名称定义",
        description="指定将配置项加载到nonebot的环境变量时使用的变量名，仅在load_to_nbenv_as为True时生效",
    )
    is_need_restart: bool = Field(
        default=False,
        title="重启需求标识",
        description="设置为True时，修改该配置项后需要重启应用才能生效",
    )
    ref_presets: bool = Field(
        default=False,
        title="人设引用标识",
        description="设置为True时，表示该字段需要从系统中已配置的人设中选择，值为人设ID",
    )
    ref_presets_no_default: bool = Field(
        default=False,
        title="人设引用无默认选项",
        description="设置为True时，人设选择器不显示'默认人设'选项，仅显示已有人设列表",
    )
    ref_presets_multiple: bool = Field(
        default=False,
        title="多人设引用标识",
        description="设置为True时，表示该字段支持选择多个人设，值为人设ID列表",
    )
    i18n_category: Optional[I18nDict] = Field(
        default=None,
        title="配置项分类",
        description="用于在前端UI中分类显示配置项，例如'云服务'、'应用设置'等，支持多语言",
    )

    # i18n 扩展字段（可选，向后兼容，以 i18n_ 前缀便于字母排序聚合）
    i18n_title: Optional[I18nDict] = Field(
        default=None,
        title="字段标题国际化",
        description="字段标题的多语言翻译，格式: {'zh-CN': '中文', 'en-US': 'English'}",
    )
    i18n_description: Optional[I18nDict] = Field(
        default=None,
        title="字段描述国际化",
        description="字段描述的多语言翻译，格式: {'zh-CN': '中文', 'en-US': 'English'}",
    )

    def model_dump(self, **kwargs: Any) -> Dict[str, Any]:
        """落成 `json_schema_extra` 用的字典：与 JSON Schema 关键字撞名的键换成 `x-` 扩展键

        这个字典会被 pydantic 原样并进**属性级** JSON Schema。`required` 在 JSON Schema 里是对象级的字符串数组，
        属性上出现 `required: true / false` 会让 FastAPI 的 OpenAPI 校验直接拒绝：开着 `ENABLE_OPENAPI_DOCS` 时
        `/api/openapi.json` 返回 400、`/api/docs` 打不开（裸机部署验收时发现）。
        换名放在这里而不是各使用处：全仓 180 多处 `ExtraField(...).model_dump()`（含适配器 / 插件的配置，
        以及邮箱账户这类嵌套的普通 BaseModel）都经过它，以后谁写 `required=True` 也不会再撞。
        读取方 `config_service._extract_extra_fields` 按 `EXTRA_FIELD_DUMP_KEYS` 换回来，WebUI 看到的仍是 `required`。
        """
        data = super().model_dump(**kwargs)
        for field_name, dump_key in EXTRA_FIELD_DUMP_KEYS.items():
            if field_name in data:
                data[dump_key] = data.pop(field_name)
        return data


# `ExtraField` 字段名 -> 它在 `json_schema_extra` 字典里的键。只有与 JSON Schema 关键字撞名的字段需要换名
EXTRA_FIELD_DUMP_KEYS: Dict[str, str] = {"required": "x-field-required"}


class ConfigManager:
    """配置管理器 - 统一管理所有配置实例"""

    _configs: Dict[str, "ConfigBase"] = {}

    @classmethod
    def register_config(cls, config_key: str, config_instance: "ConfigBase") -> None:
        """注册配置实例"""
        cls._configs[config_key] = config_instance

    @classmethod
    def get_config(cls, config_key: str) -> Optional["ConfigBase"]:
        """获取配置实例"""
        return cls._configs.get(config_key)

    @classmethod
    def get_all_config_keys(cls) -> list[str]:
        """获取所有配置键"""
        return list(cls._configs.keys())

    @classmethod
    def unregister_config(cls, config_key: str) -> None:
        """注销配置实例"""
        cls._configs.pop(config_key, None)


T_ConfigBase = TypeVar("T_ConfigBase", bound="ConfigBase")


class ConfigBase(BaseModel):
    # 类变量用于存储配置元数据
    _config_key: ClassVar[Optional[str]] = None
    _config_file_path_class: ClassVar[Optional[Path]] = None

    # 实例变量，用于动态配置
    _config_file_path: Optional[Path] = PrivateAttr(default=None)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # 自动生成配置键（如果没有手动设置）
        if cls._config_key is None:
            cls._config_key = cls._generate_config_key()

    @classmethod
    def _generate_config_key(cls) -> str:
        """生成默认的配置键"""
        # 从类名生成配置键，例如 PluginConfig -> plugin, ModelConfigGroup -> model_groups
        class_name = cls.__name__
        if class_name.endswith("Config"):
            base_name = class_name[:-6]  # 移除 "Config" 后缀
        elif class_name.endswith("Group"):
            base_name = class_name
        else:
            base_name = class_name

        # 转换为snake_case
        return re.sub(r"(?<!^)(?=[A-Z])", "_", base_name).lower()

    @classmethod
    def set_config_key(cls, config_key: str) -> None:
        """设置配置键"""
        cls._config_key = config_key

    @classmethod
    def get_config_key(cls) -> str:
        """获取配置键"""
        return cls._config_key or cls._generate_config_key()

    @classmethod
    def set_config_file_path(cls, file_path: Path) -> None:
        """设置配置文件路径"""
        cls._config_file_path_class = file_path

    def get_config_file_path(self) -> Optional[Path]:
        """获取配置文件路径"""
        return self._config_file_path or self.__class__._config_file_path_class  # noqa: SLF001

    def set_instance_config_file_path(self, file_path: Path) -> None:
        """设置实例的配置文件路径"""
        self._config_file_path = file_path

    @classmethod
    def load_config(cls: Type[T_ConfigBase], file_path: Optional[Path] = None, auto_register: bool = True) -> T_ConfigBase:
        """加载配置文件"""
        # 使用传入的路径或类默认路径
        target_path = file_path or cls._config_file_path_class
        if not target_path:
            raise ValueError(f"配置文件路径未设置，请调用 {cls.__name__}.set_config_file_path() 或提供 file_path 参数")

        instance = cls.load_from_path(target_path)

        # 自动注册到配置管理器
        if auto_register:
            ConfigManager.register_config(cls.get_config_key(), instance)

        instance.load_config_to_env()
        return instance

    @classmethod
    def load_from_path(cls: Type[T_ConfigBase], path: Path) -> T_ConfigBase:
        """从指定路径加载配置实例"""
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            instance = cls()
        else:
            content: str = path.read_text(encoding="utf-8")
            if path.suffix == ".json":
                instance = cls.model_validate_json(content)
            elif path.suffix in [".yaml", ".yml"]:
                instance = cls.model_validate(yaml.safe_load(content))
            else:
                raise ValueError(f"Unsupported file type: {path}")

        instance.set_instance_config_file_path(path)  # 存储路径
        return instance

    def load_config_to_env(self):
        """将标记了环境变量相关配置的配置项加载到对应环境中"""
        for field_name, value in self:
            field_info = self.model_fields[field_name]
            # 获取字段的元数据
            schema_extra = getattr(field_info, "json_schema_extra", None)
            if not schema_extra or not isinstance(schema_extra, dict):
                continue

            # 只处理有环境变量标记的字段
            load_to_sysenv = schema_extra.get("load_to_sysenv", False)
            load_to_nonebot_env = schema_extra.get("load_to_nonebot_env", False)

            # 如果没有任何环境变量标记，跳过
            if not (load_to_sysenv or load_to_nonebot_env):
                continue

            # 1. 处理系统环境变量
            if load_to_sysenv:
                # 获取环境变量名
                env_name = schema_extra.get("load_sysenv_as", field_name)
                # 序列化为 JSON 字符串
                os.environ[env_name] = json.dumps(
                    value,
                    default=jsonable_encoder,
                    ensure_ascii=False,
                )

            # 2. 处理 nonebot 环境变量
            if load_to_nonebot_env:
                try:
                    driver = nonebot.get_driver()

                    # 获取 nonebot 环境变量名
                    nb_env_name = schema_extra.get("load_nbenv_as", field_name)

                    final_value = value
                    # 尝试将字符串值解析为 JSON 对象
                    if isinstance(value, str):
                        try:
                            json_value = json.loads(value)
                            final_value = json_value  # 如果解析成功，使用解析后的值
                        except json.JSONDecodeError:
                            # 解析失败，保持原始字符串值
                            pass

                    # 设置 nonebot 环境变量
                    setattr(driver.config, nb_env_name, final_value)
                except ImportError:
                    pass  # nonebot 未安装，跳过
                except Exception as e:
                    print(f"ERROR: 加载配置到 nonebot 环境变量失败: {e}", file=sys.stderr)

    def _dump_data(self) -> Dict[str, Any]:
        """取出要写进 YAML 的序列化数据（钩子）

        默认就是 `model_dump()`，对不覆盖它的子类是恒等的。需要「落盘形态与内存形态不同」的
        子类（目前只有 `CoreConfig`：模型组 `API_KEY` 在文件里是占位符，内存里是明文）覆盖它，
        **只改这份序列化结果**——不要为了改落盘内容去临时替换 `self` 上的字段（那会在序列化
        期间把运行中的对象换成另一个，任何同时读它的代码拿到的都是被换过的值）。

        只作用于 YAML 路径：JSON 路径仍是 `model_dump_json()`，不经过这个钩子；覆盖了钩子的
        子类应当自己拒绝 `.json` 目标，免得一个没人看的出口绕过它（见 `CoreConfig.dump_config`）。
        """
        return self.model_dump()

    def dump_config(self, file_path: Optional[Path] = None) -> None:
        """保存配置文件"""
        # 使用传入的路径或类默认路径
        target_path = file_path or self.get_config_file_path()
        if not target_path:
            raise ValueError(
                f"配置文件路径未设置，请调用 {self.__class__.__name__}.set_config_file_path() 或提供 file_path 参数",
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)

        if target_path.suffix == ".json":
            atomic_write_text(target_path, self.model_dump_json(indent=2), encoding="utf-8")
        elif target_path.suffix in [".yaml", ".yml"]:
            yaml_str = yaml.dump(
                data=self._dump_data(),
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
            atomic_write_text(target_path, yaml_str, encoding="utf-8")
        else:
            raise ValueError(f"Unsupported file type: {target_path}")

    @classmethod
    def get_field_title(cls, field_name: str) -> str:
        """获取字段的中文标题"""
        field = cls.model_fields.get(field_name)
        if field and field.title:
            return field.title
        return ""  # Return empty string if field or title is not found

    @classmethod
    def get_field_placeholder(cls, field_name: str) -> str:
        """获取字段的占位符文本"""
        field = cls.model_fields.get(field_name)
        if field and hasattr(field, "json_schema_extra") and isinstance(field.json_schema_extra, dict):
            placeholder = field.json_schema_extra.get("placeholder")
            return str(placeholder) if placeholder is not None else ""
        return ""


def gen_mysql_conn_str(
    host: str,
    port: int,
    user: str,
    password: str,
    db: str,
    proxy_host: Optional[str] = None,
    proxy_port: Optional[int] = None,
) -> str:
    """生成 MySQL 连接字符串，支持代理

    Args:
        host (str): 主机名或 IP 地址
        port (int): 端口号
        user (str): 用户名
        password (str): 密码
        db (str): 数据库名
        proxy_host (str): 代理服务器地址
        proxy_port (int): 代理服务器端口号

    Returns:
        str: 连接字符串
    """
    user = quote_plus(user)
    password = quote_plus(password)
    db = quote_plus(db)

    conn_str = f"mysql://{user}:{password}@{host}:{port}/{db}"

    if proxy_host and proxy_port:
        conn_str += f"?proxyhost={quote_plus(proxy_host)}&proxyport={proxy_port}"

    return conn_str


def gen_postgres_conn_str(
    host: str,
    port: int,
    user: str,
    password: str,
    db: str,
    sslrootcert: Optional[str] = None,
    sslcert: Optional[str] = None,
    sslkey: Optional[str] = None,
) -> str:
    """生成 PostgreSQL 连接字符串

    Args:
        host (str): 主机名或 IP 地址
        port (int): 端口号
        user (str): 用户名
        password (str): 密码
        db (str): 数据库名
        sslmode (str, optional): SSL 连接模式. Defaults to "require".
        sslrootcert (Optional[str], optional): 根证书路径. Defaults to None.
        sslcert (Optional[str], optional): 客户端证书路径. Defaults to None.
        sslkey (Optional[str], optional): 客户端密钥路径. Defaults to None.

    Returns:
        str: 连接字符串
    """
    user = quote_plus(user)
    password = quote_plus(password)
    db = quote_plus(db)

    conn_str = f"postgres://{user}:{password}@{host}:{port}/{db}?"

    if sslrootcert:
        conn_str += f"&sslrootcert={quote_plus(sslrootcert)}"
    if sslcert:
        conn_str += f"&sslcert={quote_plus(sslcert)}"
    if sslkey:
        conn_str += f"&sslkey={quote_plus(sslkey)}"

    return conn_str


def gen_sqlite_db_url(db_path: str) -> str:
    """生成 SQLite 数据库连接 URL"""

    if not db_path.startswith("/") and not db_path.startswith("./"):
        db_path = f"./{db_path}"

    return f"sqlite:///{db_path}"
