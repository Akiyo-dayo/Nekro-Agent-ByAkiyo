import copy
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from urllib.parse import quote_plus

import nonebot
import yaml
from fastapi.encoders import jsonable_encoder
from loguru import logger as _loguru_logger
from pydantic import BaseModel, Field, PrivateAttr
from pydantic import ValidationError as PydanticValidationError

from nekro_agent.schemas.i18n import I18nDict
from nekro_agent.tools.private_file import (
    describe_private_file_protection,
    write_private_file,
)


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
    platform_only: bool = Field(
        default=False,
        title="平台方专属",
        description=(
            "设置为True时，租户写实例覆盖时不能改它的取值；读照常返回、前端显示为只读。"
            "平台方与未启用多租户的部署不受此限。"
        ),
    )
    tenant_may_only_lower: bool = Field(
        default=False,
        title="租户只能调低",
        description=(
            "设置为True时，租户身份只能把该字段的生效值往低处调；任何让写入后生效值高于写入前的提交都会被拒。"
            "bool 字段的「低」是关，int 字段的「低」是数值小；平台方与未启用多租户的部署不受此限。"
            "见 routers/config.py:_assert_tenant_may_not_raise_only_field"
        ),
    )
    zero_means_unlimited: bool = Field(
        default=False,
        title="0 表示不设上限",
        description=(
            "设置为True时，这个整数项的 0 表示「不设上限」而不是最小值：「租户只能调低」的判据把 0 当作无穷大"
            "（否则租户写 0 就绕过了平台方的上限），前端把 0 显示成「不设上限」。"
            "标了它的项必须同时标 tenant_may_only_lower（导入期校验）"
        ),
    )
    slider: bool = Field(
        default=False,
        title="用滑块编辑",
        description="设置为True时，前端用滑块编辑这个整数项，范围取字段的 ge / le（配置项列表里的 minimum / maximum）",
    )
    is_outbound_url: bool = Field(
        default=False,
        title="出网目标地址",
        description=(
            "设置为True时，该项的取值会让服务端主动发起出站请求，写入前必须过 SSRF 校验"
            "（租户提交的地址强制公网，见 model_group_service.assert_outbound_urls_allowed）"
        ),
    )
    audit_changes: bool = Field(
        default=False,
        title="改动写审计",
        description=(
            "设置为True时，这一项的每次改动都写进配置变更审计；文字只记长度与摘要，不记原文。"
            "见 services/tavern/audit.py 与 routers/config.py:audited_fields"
        ),
    )
    enable_confirm: Optional[I18nDict] = Field(
        default=None,
        title="打开时的确认文案",
        description=(
            "布尔项从关切到开时，前端先弹这段多语言确认文字；为空则不弹。"
            "只用于需要管理员明确知情的高风险开关。"
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
    ref_rp_presets: bool = Field(
        default=False,
        title="角色扮演预设引用标识",
        description="设置为True时，表示该字段需要从已导入的酒馆对话补全预设中选择，值为预设ID",
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
T_Model = TypeVar("T_Model", bound=BaseModel)


# =============================================================================
# 配置文件里的非法值：逐项修正，不让一个字段拖垮整份配置
# =============================================================================
#
# 以前读配置一律 `model_validate`：任何一个字段越界、类型不对，整份配置抛异常。全局配置在
# 模块导入时加载，失败就 `exit(1)`（NA 起不来）；实例 / 频道覆盖文件失败时整份被丢掉（那一层
# 所有覆盖项悄悄失效）；插件的实例覆盖同样整份回落到全局。现在先照常校验，失败时按报错位置
# 逐项修正：越界的截到边界，超长的截到上限，其余（类型不对、枚举外的值……）去掉这一项、回到
# 默认值；每修一处记一条 ERROR，写明文件、字段、原值与改用的值。修不动的（模型级的错、顶层
# 不是映射）照旧抛出——宁可起不来，也不拿默认值整份覆盖掉用户的配置文件。
#
# 覆盖层（压在另一层配置之上的那一层）里去掉一项时，回到的应当是**上一层**的值，不是代码默认值：
# 插件的实例覆盖是「全局值 + 覆盖」合并后校验，调用方把全局值作为 `fallback` 传进来，顶层某项
# 去掉时改用 `fallback` 里的值；核心配置的实例 / 频道覆盖文件是带 `enable_<字段>` 开关的覆盖模型
# （`_is_override_layer`），去掉一项时连同开关一起关掉，生效值由解析器从上一层继承。只在顶层
# 回落；越界、超长仍是截到边界（截完的值本来就落在合法范围内，不涉及回到哪一层）。
#
# 全局配置在导入时修正后会立刻整份写回（`core/config.py` 末尾那次 dump），被去掉的原值就从文件
# 里消失了，而文件里还可能有手写的明文凭据。所以声明了 `_backup_original_on_repair` 的配置类
# （目前只有 `CoreConfig`）在这次加载确实做过修正时，先把原文件逐字节备份成仅属主可访问的
# `<文件名>.pre-repair-<时间>`，ERROR 日志里写出备份路径。

# 一份配置最多修几轮：去掉一项后可能露出「必填缺失」这类新错，需要再修一轮
_MAX_REPAIR_PASSES = 10
# 记下的修正保留多少条（启动后补记到日志、测试读取用）
_MAX_RECORDED_REPAIRS = 200
# 字段名里带这些字样的，日志里不写原值
_SECRET_NAME_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
_BOUND_ERRORS = {
    "less_than_equal": "le",
    "greater_than_equal": "ge",
    "less_than": "lt",
    "greater_than": "gt",
}
_LENGTH_ERRORS = ("string_too_long", "too_long")


@dataclass(frozen=True)
class ConfigRepair:
    """一处被修正的非法配置值"""

    source: str
    location: str
    original: str
    replacement: str
    reason: str
    # 写回前备份原文件的结果（备份到了哪 / 没备份成）；不备份的配置类为空
    backup_note: str = ""

    def describe(self) -> str:
        text = (
            f"配置文件 {self.source} 的 {self.location} 取值不合法：{self.original}（{self.reason}），"
            f"已改用 {self.replacement}；其他配置项照常加载"
        )
        return f"{text}；{self.backup_note}" if self.backup_note else text


_recorded_repairs: List[ConfigRepair] = []


def recorded_config_repairs() -> List[ConfigRepair]:
    """本进程里做过的配置修正（按发生顺序，最多保留最近的若干条）"""
    return list(_recorded_repairs)


def _is_secret_location(cls: Type[BaseModel], loc: Tuple[Any, ...]) -> bool:
    first = loc[0] if loc else None
    if isinstance(first, str):
        field = cls.model_fields.get(first)
        extra = getattr(field, "json_schema_extra", None) if field is not None else None
        if isinstance(extra, dict) and extra.get("is_secret"):
            return True
    return any(isinstance(part, str) and any(hint in part.upper() for hint in _SECRET_NAME_HINTS) for part in loc)


def _brief(value: Any, secret: bool) -> str:
    if secret:
        return "（凭据，不记原值）"
    text = repr(value)
    return text if len(text) <= 160 else text[:157] + "..."


def _dotted(loc: Tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in loc) if loc else "（整份配置）"


def _locate(data: Any, loc: Tuple[Any, ...]) -> Tuple[Any, Any, int]:
    """沿 `loc` 在数据里走到最深处，返回 (所在容器, 键, 走过的层数)

    `loc` 里数据中不存在的尾巴（联合类型的分支标签等）忽略；一层都走不进去时容器为 None。
    """
    container: Any = None
    key: Any = None
    depth = 0
    current = data
    for part in loc:
        if isinstance(current, dict) and part in current:
            container, key, current = current, part, current[part]
        elif isinstance(current, list) and isinstance(part, int) and 0 <= part < len(current):
            container, key, current = current, part, current[part]
        else:
            break
        depth += 1
    return container, key, depth


def _nearest_value(error_type: str, ctx: Dict[str, Any], value: Any) -> Tuple[bool, Any]:
    """越界、超长时给出就近的合法值；给不出时返回 (False, None)，由调用方回落默认值"""
    if error_type in _BOUND_ERRORS:
        bound = ctx.get(_BOUND_ERRORS[error_type])
        if isinstance(bound, bool) or not isinstance(bound, (int, float)):
            return False, None
        if error_type in ("less_than_equal", "greater_than_equal"):
            return True, bound
        # 开区间：整数取边界内最近的整数；小数没有「最近」，回落默认值
        if isinstance(bound, int) and not isinstance(value, float):
            return True, bound - 1 if error_type == "less_than" else bound + 1
        return False, None
    if error_type in _LENGTH_ERRORS:
        limit = ctx.get("max_length")
        if isinstance(limit, int) and isinstance(value, (str, list)):
            return True, value[:limit]
    return False, None


def _value_at(instance: Any, loc: Tuple[Any, ...]) -> Tuple[bool, Any]:
    current = instance
    for part in loc:
        if isinstance(current, BaseModel) and isinstance(part, str) and hasattr(current, part):
            current = getattr(current, part)
        elif isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and isinstance(part, int) and 0 <= part < len(current):
            current = current[part]
        else:
            return False, None
    return True, current


def validate_config_data(
    cls: Type[T_Model],
    data: Any,
    source: str,
    fallback: Optional[Mapping[str, Any]] = None,
) -> T_Model:
    """校验一份配置数据；单个字段不合法时就近修正、记 ERROR，其余字段照常加载

    Args:
        cls: 配置模型类
        data: 从文件读出来的数据（`None` 视为空映射：空文件就是「全部取默认值」）
        source: 写进日志的来源（通常是文件路径）
        fallback: 上一层配置的取值。顶层某项要去掉时，这里有就改用这里的值，而不是代码默认值

    Raises:
        pydantic.ValidationError: 顶层不是映射、模型级校验失败等无法逐项修正的情形
    """
    instance, repairs = _repair_config_data(cls, data, source, fallback)
    _report_config_repairs(repairs)
    return instance


def _report_config_repairs(repairs: List[ConfigRepair]) -> None:
    for record in repairs:
        _recorded_repairs.append(record)
        del _recorded_repairs[:-_MAX_RECORDED_REPAIRS]
        _loguru_logger.error(record.describe())


def _override_toggle(cls: Type[BaseModel], name: Any) -> Optional[str]:
    """覆盖模型里 `name` 对应的 `enable_<name>` 开关字段名；不是覆盖模型或没有开关时为 None"""
    if not getattr(cls, "_is_override_layer", False) or not isinstance(name, str):
        return None
    toggle = f"enable_{name}"
    return toggle if toggle in cls.model_fields and name in cls.model_fields else None


def _repair_config_data(
    cls: Type[T_Model],
    data: Any,
    source: str,
    fallback: Optional[Mapping[str, Any]] = None,
) -> Tuple[T_Model, List[ConfigRepair]]:
    """`validate_config_data` 的修正部分：返回校验通过的实例与做过的修正，**不记日志**

    分开是为了让加载文件的调用方在记日志前先做点别的（比如先备份原文件，再把备份路径写进日志）。
    修不动时照旧记 ERROR 并抛出。
    """
    working: Any = {} if data is None else copy.deepcopy(data)
    # 每条待补的「改用了什么值」：(记录在列表里的位置, 修正位置)；默认值要等校验通过后才知道
    pending: List[Tuple[int, Tuple[Any, ...]]] = []
    repairs: List[Dict[str, Any]] = []
    # 截到边界过一次的位置：截完仍不合法（比如还有别的约束）就不再截，改为回落默认值
    clamped: set = set()
    # 改用过上一层取值的位置：上一层的值本身也过不了校验（不该发生）时不再循环，改为去掉
    fell_back: set = set()
    last_error: Optional[PydanticValidationError] = None

    for _ in range(_MAX_REPAIR_PASSES):
        try:
            instance = cls.model_validate(working)
        except PydanticValidationError as exc:
            last_error = exc
        else:
            for index, loc in pending:
                found, value = _value_at(instance, loc)
                repairs[index]["replacement"] = (
                    _brief(value, _is_secret_location(cls, loc)) if found else "默认值（这一项已移除）"
                )
            return instance, [ConfigRepair(**item) for item in repairs]

        if not isinstance(working, dict):
            break
        # 先改值、后删项；同一个列表里的删除从后往前删，免得下标错位
        to_set: List[Tuple[Any, Any, Any]] = []
        to_delete: List[Tuple[Any, Any]] = []
        for err in last_error.errors(include_url=False):
            loc = tuple(err.get("loc") or ())
            if not loc:
                continue
            error_type = str(err.get("type", ""))
            container, key, depth = _locate(working, loc)
            if error_type == "missing":
                # 去掉一项后必填项缺了：连这一条（字典里的一个条目、列表里的一项）一起去掉
                parent_loc = loc[:-1]
                container, key, depth = _locate(working, parent_loc)
                if not parent_loc or depth != len(parent_loc) or container is None:
                    continue
                path = parent_loc
                original = container[key]
                to_delete.append((container, key))
                # 这一条里早先修过、还等着回读「改用了什么值」的项，随这一条一起没了
                for index, pending_loc in list(pending):
                    if pending_loc[: len(path)] == path:
                        repairs[index]["replacement"] = "（随所在条目一起移除）"
                        pending.remove((index, pending_loc))
                repairs.append(
                    {
                        "source": source,
                        "location": _dotted(path),
                        "original": _brief(original, _is_secret_location(cls, path)),
                        "replacement": "（整条移除）",
                        "reason": f"{_dotted(loc)} 缺失：{err.get('msg', '')}",
                    }
                )
                continue
            if container is None:
                continue
            path = loc[:depth]
            original = container[key]
            secret = _is_secret_location(cls, path)
            reason = str(err.get("msg", error_type))
            ok, nearest = _nearest_value(error_type, err.get("ctx") or {}, original)
            if ok and path not in clamped:
                clamped.add(path)
                to_set.append((container, key, nearest))
                repairs.append(
                    {
                        "source": source,
                        "location": _dotted(path),
                        "original": _brief(original, secret),
                        "replacement": _brief(nearest, secret),
                        "reason": reason,
                    }
                )
                continue
            if len(path) == 1 and fallback is not None and key in fallback and path not in fell_back:
                # 顶层这一项回到上一层的取值
                fell_back.add(path)
                upper = copy.deepcopy(fallback[key])
                to_set.append((container, key, upper))
                repairs.append(
                    {
                        "source": source,
                        "location": _dotted(path),
                        "original": _brief(original, secret),
                        "replacement": f"上一层的值 {_brief(upper, secret)}",
                        "reason": reason,
                    }
                )
                continue
            toggle = _override_toggle(cls, key) if len(path) == 1 else None
            if toggle is not None and container.get(toggle) not in (None, False):
                # 覆盖层：这一项的覆盖整个停用（开关关掉、取值去掉），生效值由解析器从上一层继承
                to_set.append((container, toggle, False))
                to_delete.append((container, key))
                repairs.append(
                    {
                        "source": source,
                        "location": _dotted(path),
                        "original": _brief(original, secret),
                        "replacement": f"上一层的值（{toggle} 已关闭，这一项不再覆盖、改为继承上一层）",
                        "reason": reason,
                    }
                )
                continue
            to_delete.append((container, key))
            if isinstance(container, list):
                # 列表里的一项整项去掉：后面的项会挪上来，按下标回读会读到别的项
                replacement = "（这一项已移除）"
            else:
                replacement = "默认值"
                pending.append((len(repairs), path))
            repairs.append(
                {
                    "source": source,
                    "location": _dotted(path),
                    "original": _brief(original, secret),
                    "replacement": replacement,
                    "reason": reason,
                }
            )

        if not to_set and not to_delete:
            break
        for container, key, value in to_set:
            container[key] = value
        seen_deletes: set = set()
        dict_deletes: List[Tuple[Any, Any]] = []
        list_deletes: List[Tuple[Any, Any]] = []
        for container, key in to_delete:
            marker = (id(container), key)
            if marker in seen_deletes:
                continue
            seen_deletes.add(marker)
            (list_deletes if isinstance(container, list) else dict_deletes).append((container, key))
        for container, key in dict_deletes:
            container.pop(key, None)
        for container, key in sorted(list_deletes, key=lambda item: (id(item[0]), item[1]), reverse=True):
            if 0 <= key < len(container):
                del container[key]

    assert last_error is not None
    _loguru_logger.error(f"配置文件 {source} 有无法逐项修正的错误，未加载：{last_error}")
    raise last_error


# 修正前的原文件备份：`<原文件名>.pre-repair-<UTC 时间>`，与原文件同目录
PRE_REPAIR_BACKUP_INFIX = ".pre-repair-"


def _backup_original_before_rewrite(path: Path) -> str:
    """把修正前的原文件逐字节备份成仅属主可访问的新文件，返回写进 ERROR 日志的那半句

    备份失败不抛：启动照常进行（写回那一步多半也会因同样的原因失败，原文件就还在），日志里如实说明。
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}{PRE_REPAIR_BACKUP_INFIX}{stamp}")
    try:
        owner_only = write_private_file(backup, path.read_bytes())
    except OSError as exc:
        return (
            f"修正前的原文件没能备份（{type(exc).__name__}: {exc}），"
            "写回后被修正项的原值只剩日志里的记录（凭据项不记原值）"
        )
    return (
        f"修正前的原文件已备份到 {backup}（{describe_private_file_protection(owner_only)}；"
        "内容与修正前逐字节一致，可能含明文凭据，核对完请删除）"
    )


class ConfigBase(BaseModel):
    # 类变量用于存储配置元数据
    _config_key: ClassVar[Optional[str]] = None
    _config_file_path_class: ClassVar[Optional[Path]] = None
    # 加载时修正过非法值，就先把原文件备份一份（加载后会立刻整份写回的配置才需要，见 `load_from_path`）
    _backup_original_on_repair: ClassVar[bool] = False
    # 覆盖层模型（字段成对出现：`enable_<字段>` 开关 + 取值）：非法值去掉时连同开关一起关掉，继承上一层
    _is_override_layer: ClassVar[bool] = False

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
                data = json.loads(content)
            elif path.suffix in [".yaml", ".yml"]:
                data = yaml.safe_load(content)
            else:
                raise ValueError(f"Unsupported file type: {path}")
            # 单个字段不合法时就近修正并记 ERROR，不让它拖垮整份配置（见 `validate_config_data`）
            instance, repairs = _repair_config_data(cls, data, source=str(path))
            if repairs and cls._backup_original_on_repair:
                note = _backup_original_before_rewrite(path)
                repairs = [dataclass_replace(record, backup_note=note) for record in repairs]
            _report_config_repairs(repairs)

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
