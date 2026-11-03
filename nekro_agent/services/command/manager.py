"""命令状态管理 - 基于 JSON 文件存储"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from nonebot import logger

from nekro_agent.core.os_env import (
    COMMAND_CHANNEL_PERMISSION_DIR,
    COMMAND_CHANNEL_STATE_DIR,
    COMMAND_CHANNEL_USER_PERMISSION_FILE,
    COMMAND_STATE_DIR,
    COMMAND_SYSTEM_PERMISSION_FILE,
    COMMAND_SYSTEM_STATE_FILE,
    COMMAND_USER_PERMISSION_FILE,
)
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.command.base import BUILT_IN_SOURCE, CommandMetadata, CommandPermission
from nekro_agent.tools.path_convertor import sanitize_chat_key_for_path

UserPermissionSource = Literal["manual", "platform"]
USER_PERMISSION_SOURCE_MANUAL: UserPermissionSource = "manual"
USER_PERMISSION_SOURCE_PLATFORM: UserPermissionSource = "platform"

# 一个频道里按频道授权的人数上限。所有租户的频道授权在同一个文件里，每次写入整份重写，
# 不设上限的话一个租户就能把它撑大、拖慢所有人的写入
CHANNEL_USER_PERMISSION_LIMIT = 200

# 命令权限覆盖里 `advanced` 的含义在引入用户级权限时变了：之前实际上只有 SUPER_USERS 够得着，
# 之后频道管理员也够得着。这个标记文件记下「存量覆盖已经按旧含义迁移过」
PERMISSION_SEMANTICS_MARKER_NAME = "permission_semantics.json"


@dataclass(frozen=True)
class UserPermissionRecord:
    permission: CommandPermission
    source: UserPermissionSource = USER_PERMISSION_SOURCE_MANUAL
    channel_id: Optional[str] = None


def _write_json_atomic(path: Path, data: object) -> None:
    """先写同目录临时文件再整体替换：写到一半被打断时，旧文件仍然完整

    读坏文件按空处理是 fail-closed 的（授权全部失效），但下一次写入会把「空」当成现状落盘，
    其余人的授权就真的丢了。两份用户授权文件因此不用 `write_text` 直接覆盖。
    """
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


async def is_group_admin_detection_enabled(chat_key: str) -> bool:
    """这个频道是否把群主 / 群管理员自动识别为频道管理员

    按 频道 > 实例 > 适配器 > 全局 解析 `COMMAND_GROUP_ADMIN_AS_CHANNEL_ADMIN`，默认关；
    解析失败按关处理：识别出来的是一档权限，拿不准时不该多给。
    """
    from nekro_agent.services.config_resolver import config_resolver

    try:
        effective = await config_resolver.get_effective_config(chat_key)
    except Exception:
        logger.warning(f"解析群角色识别开关失败，按关闭处理: chat_key={chat_key}", exc_info=True)
        return False
    return bool(effective.COMMAND_GROUP_ADMIN_AS_CHANNEL_ADMIN)


def _set_aside_unreadable(path: Path) -> None:
    """读不出来的授权文件挪到一边再按空处理

    不挪的话，下一次写入（可能是新群管理注册时的自动登记）会把「空」整份写回去，
    文件里其余人的授权就真的没了；挪开之后至少还能人工找回。
    """
    backup = path.with_name(f"{path.name}.unreadable-{int(time.time())}")
    try:
        path.rename(backup)
        logger.warning(f"命令授权文件无法读取，已改名保留为 {backup.name}，按空处理")
    except OSError as e:
        logger.warning(f"命令授权文件无法读取，且改名保留失败（{e}），按空处理: {path}")


def _user_permission_record_to_json(record: UserPermissionRecord) -> dict[str, Optional[str]]:
    return {
        "permission": record.permission.value,
        "source": record.source,
        "channel_id": record.channel_id,
    }


def channel_state_file(state_dir: str, chat_key: str) -> Path:
    """频道级命令状态/权限文件的落盘路径

    `chat_key` 来自接口参数，原样拼进文件名等于把写盘位置交给调用方：
    `../../configs/nekro-agent` 这类取值会写到数据目录之外。清洗规则复用
    `tools/path_convertor.sanitize_chat_key_for_path`（与上传目录、沙盒挂载源同一套），
    它把 `[a-zA-Z0-9_.-]` 之外的字符全部替换成 `_`，路径分隔符因而无法出现在结果里。

    清洗只对含特殊字符的 `chat_key` 改变结果（例如 wechat_ilink_multi 的 `a:b`），
    这些频道此前已经按原名落过盘，因此顺带把旧文件迁到新文件名——不迁移的话用户
    既有的命令启停配置会在升级后凭空失效，旧文件还会永远留在磁盘上。
    迁移只认与目标同目录的直接子文件：穿越出数据目录的旧路径本就不该存在，
    更不该被搬进来。
    """
    directory = Path(state_dir)
    path = directory / f"{sanitize_chat_key_for_path(chat_key)}.json"
    if path.exists():
        return path

    legacy = directory / f"{chat_key}.json"
    try:
        is_adoptable = legacy != path and legacy.is_file() and legacy.parent.resolve() == directory.resolve()
    except OSError:  # 非法文件名在部分平台上连 stat 都会失败
        return path
    if is_adoptable:
        try:
            legacy.rename(path)
        except OSError as e:
            logger.warning(f"迁移频道命令状态文件失败: {legacy} -> {path}, 错误: {e}")
    return path


class CommandManager:
    """命令状态管理 - 基于 JSON 文件存储

    状态优先级: 频道级 > 系统级 > 默认启用
    """

    def __init__(self):
        Path(COMMAND_STATE_DIR).mkdir(parents=True, exist_ok=True)
        Path(COMMAND_CHANNEL_STATE_DIR).mkdir(parents=True, exist_ok=True)
        Path(COMMAND_CHANNEL_PERMISSION_DIR).mkdir(parents=True, exist_ok=True)
        self._system_cache: Optional[dict[str, bool]] = None
        self._channel_cache: dict[str, dict[str, bool]] = {}
        self._system_permission_cache: Optional[dict[str, CommandPermission]] = None
        self._channel_permission_cache: dict[str, dict[str, CommandPermission]] = {}
        self._user_permission_cache: Optional[dict[str, UserPermissionRecord]] = None
        self._channel_user_permission_cache: Optional[dict[str, dict[str, UserPermissionRecord]]] = None

    def _load_system_state(self) -> dict[str, bool]:
        """加载系统级状态（带缓存）"""
        if self._system_cache is None:
            system_state_file = Path(COMMAND_SYSTEM_STATE_FILE)
            if system_state_file.exists():
                try:
                    self._system_cache = json.loads(system_state_file.read_text(encoding="utf-8"))
                except (ValueError, OSError) as e:
                    logger.warning(f"加载系统级命令状态失败: {e}")
                    self._system_cache = {}
            else:
                self._system_cache = {}
        assert self._system_cache is not None
        return self._system_cache

    def _load_channel_state(self, chat_key: str) -> dict[str, bool]:
        """加载频道级状态（带缓存）"""
        if chat_key not in self._channel_cache:
            path = channel_state_file(COMMAND_CHANNEL_STATE_DIR, chat_key)
            if path.exists():
                try:
                    self._channel_cache[chat_key] = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError) as e:
                    logger.warning(f"加载频道 {chat_key} 命令状态失败: {e}")
                    self._channel_cache[chat_key] = {}
            else:
                self._channel_cache[chat_key] = {}
        return self._channel_cache[chat_key]

    @staticmethod
    def _normalize_permission_state(raw_state: object) -> dict[str, CommandPermission]:
        if not isinstance(raw_state, dict):
            return {}

        normalized: dict[str, CommandPermission] = {}
        for command_name, permission in raw_state.items():
            if not isinstance(command_name, str) or not isinstance(permission, str):
                continue
            try:
                normalized[command_name] = CommandPermission(permission)
            except ValueError:
                logger.warning(f"检测到无效命令权限配置，已忽略: {command_name}={permission}")
        return normalized

    def _load_system_permission_state(self) -> dict[str, CommandPermission]:
        """加载系统级权限覆盖（带缓存）。

        返回内部共享缓存，调用方仅应在 setter 流程中原地修改并立即持久化。
        """
        if self._system_permission_cache is None:
            system_permission_file = Path(COMMAND_SYSTEM_PERMISSION_FILE)
            if system_permission_file.exists():
                try:
                    raw_state = json.loads(system_permission_file.read_text(encoding="utf-8"))
                    self._system_permission_cache = self._normalize_permission_state(raw_state)
                except (ValueError, OSError) as e:
                    logger.warning(f"加载系统级命令权限失败: {e}")
                    self._system_permission_cache = {}
            else:
                self._system_permission_cache = {}
        assert self._system_permission_cache is not None
        return self._system_permission_cache

    def _load_channel_permission_state(self, chat_key: str) -> dict[str, CommandPermission]:
        """加载频道级权限覆盖（带缓存）。

        返回内部共享缓存，调用方仅应在 setter 流程中原地修改并立即持久化。
        """
        if chat_key not in self._channel_permission_cache:
            path = channel_state_file(COMMAND_CHANNEL_PERMISSION_DIR, chat_key)
            if path.exists():
                try:
                    raw_state = json.loads(path.read_text(encoding="utf-8"))
                    self._channel_permission_cache[chat_key] = self._normalize_permission_state(raw_state)
                except (ValueError, OSError) as e:
                    logger.warning(f"加载频道 {chat_key} 命令权限失败: {e}")
                    self._channel_permission_cache[chat_key] = {}
            else:
                self._channel_permission_cache[chat_key] = {}
        return self._channel_permission_cache[chat_key]

    def _load_user_permission_state(self, *, for_write: bool = False) -> dict[str, UserPermissionRecord]:
        """加载平台用户命令权限覆盖（带缓存）。

        读不到与读坏了分开处理：
        - 读不到（`OSError`：瞬时 IO 错误、权限还没就绪）不代表内容有问题——不挪文件、不缓存，
          这次读按空（少给权限），写直接拒绝（否则会用「只剩这一条」覆盖掉完好的文件），下次再读；
        - 读坏了（`ValueError`：坏 JSON、非 UTF-8 字节）挪开改名保留，按空继续。
        """
        if self._user_permission_cache is None:
            user_permission_file = Path(COMMAND_USER_PERMISSION_FILE)
            if user_permission_file.exists():
                try:
                    raw_bytes = user_permission_file.read_bytes()
                except OSError as e:
                    logger.warning(f"读取用户命令权限失败（未改动文件，下次重试）: {e}")
                    if for_write:
                        raise
                    return {}
                try:
                    raw_state = json.loads(raw_bytes.decode("utf-8"))
                    self._user_permission_cache = self._normalize_user_permission_state(raw_state)
                except ValueError as e:
                    logger.warning(f"加载用户命令权限失败: {e}")
                    _set_aside_unreadable(user_permission_file)
                    self._user_permission_cache = {}
            else:
                self._user_permission_cache = {}
        assert self._user_permission_cache is not None
        return self._user_permission_cache

    @staticmethod
    def _normalize_user_permission_state(raw_state: object) -> dict[str, UserPermissionRecord]:
        if not isinstance(raw_state, dict):
            return {}

        normalized: dict[str, UserPermissionRecord] = {}
        for key, raw_record in raw_state.items():
            if not isinstance(key, str):
                continue

            raw_permission: object
            source: UserPermissionSource = USER_PERMISSION_SOURCE_MANUAL
            channel_id: Optional[str] = None

            if isinstance(raw_record, str):
                raw_permission = raw_record
            elif isinstance(raw_record, dict):
                raw_permission = raw_record.get("permission")
                raw_source = raw_record.get("source")
                if raw_source == USER_PERMISSION_SOURCE_PLATFORM:
                    source = USER_PERMISSION_SOURCE_PLATFORM
                raw_channel_id = raw_record.get("channel_id")
                if isinstance(raw_channel_id, str) and raw_channel_id:
                    channel_id = raw_channel_id
            else:
                continue

            if not isinstance(raw_permission, str):
                continue
            try:
                normalized[key] = UserPermissionRecord(
                    permission=CommandPermission(raw_permission),
                    source=source,
                    channel_id=channel_id,
                )
            except ValueError:
                logger.warning(f"检测到无效用户命令权限配置，已忽略: {key}={raw_permission}")
        return normalized

    def _load_channel_user_permission_state(self, *, for_write: bool = False) -> dict[str, dict[str, UserPermissionRecord]]:
        """加载按频道的用户命令授权（带缓存）。

        外层键是完整 `chat_key`，内层沿用上游用户权限的记录形状。这一份只装 `advanced`：
        写入口只写得出这一档（`set_channel_user_permission`），手改出来的别的取值一律不认，
        免得一行脏数据在某个频道里造出超级用户。读不到与读坏了的分别处理同 `_load_user_permission_state`。
        """
        if self._channel_user_permission_cache is None:
            state: dict[str, dict[str, UserPermissionRecord]] = {}
            path = Path(COMMAND_CHANNEL_USER_PERMISSION_FILE)
            if path.exists():
                try:
                    raw_bytes = path.read_bytes()
                except OSError as e:
                    logger.warning(f"读取频道用户命令授权失败（未改动文件，下次重试）: {e}")
                    if for_write:
                        raise
                    return {}
                try:
                    raw_state = json.loads(raw_bytes.decode("utf-8"))
                except ValueError as e:
                    logger.warning(f"加载频道用户命令授权失败: {e}")
                    _set_aside_unreadable(path)
                    raw_state = {}
                if isinstance(raw_state, dict):
                    for chat_key, raw_records in raw_state.items():
                        if not isinstance(chat_key, str) or not chat_key:
                            continue
                        records = {
                            key: record
                            for key, record in self._normalize_user_permission_state(raw_records).items()
                            if record.permission == CommandPermission.ADVANCED
                        }
                        if records:
                            state[chat_key] = records
            self._channel_user_permission_cache = state
        return self._channel_user_permission_cache

    def _save_system_state(self, state: dict[str, bool]) -> None:
        Path(COMMAND_SYSTEM_STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        self._system_cache = state

    def _save_channel_state(self, chat_key: str, state: dict[str, bool]) -> None:
        path = channel_state_file(COMMAND_CHANNEL_STATE_DIR, chat_key)
        if state:
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        self._channel_cache[chat_key] = state

    def _save_system_permission_state(self, state: dict[str, CommandPermission]) -> None:
        Path(COMMAND_SYSTEM_PERMISSION_FILE).write_text(
            json.dumps({key: value.value for key, value in state.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._system_permission_cache = state

    def _save_channel_permission_state(self, chat_key: str, state: dict[str, CommandPermission]) -> None:
        path = channel_state_file(COMMAND_CHANNEL_PERMISSION_DIR, chat_key)
        if state:
            path.write_text(
                json.dumps({key: value.value for key, value in state.items()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            path.unlink(missing_ok=True)
        self._channel_permission_cache[chat_key] = state

    def _save_user_permission_state(self, state: dict[str, UserPermissionRecord]) -> None:
        path = Path(COMMAND_USER_PERMISSION_FILE)
        if state:
            _write_json_atomic(path, {key: _user_permission_record_to_json(record) for key, record in state.items()})
        else:
            path.unlink(missing_ok=True)
        self._user_permission_cache = state

    def _save_channel_user_permission_state(self, state: dict[str, dict[str, UserPermissionRecord]]) -> None:
        path = Path(COMMAND_CHANNEL_USER_PERMISSION_FILE)
        payload = {
            chat_key: {key: _user_permission_record_to_json(record) for key, record in records.items()}
            for chat_key, records in state.items()
            if records
        }
        if payload:
            _write_json_atomic(path, payload)
        else:
            path.unlink(missing_ok=True)
        self._channel_user_permission_cache = {chat_key: records for chat_key, records in state.items() if records}

    @staticmethod
    def _user_permission_key(adapter_key: str, platform_userid: str) -> str:
        return f"{adapter_key}:{platform_userid}"

    @staticmethod
    def _is_plugin_command_source_enabled(
        source: str,
        plugin_enabled_cache: Optional[dict[str, bool]] = None,
    ) -> bool:
        """检查插件来源命令对应的插件是否已启用。"""
        if source == BUILT_IN_SOURCE:
            return True

        if plugin_enabled_cache is not None and source in plugin_enabled_cache:
            return plugin_enabled_cache[source]

        from nekro_agent.services.plugin.collector import plugin_collector

        plugin = plugin_collector.get_plugin(source)
        enabled = bool(plugin and plugin.is_enabled)
        if plugin_enabled_cache is not None:
            plugin_enabled_cache[source] = enabled
        return enabled

    def _is_command_enabled_impl(
        self,
        command_name: str,
        chat_key: Optional[str] = None,
        *,
        meta: Optional[CommandMetadata] = None,
        plugin_enabled_cache: Optional[dict[str, bool]] = None,
    ) -> bool:
        """统一的命令启用检查实现。"""
        target_meta = meta
        if target_meta is None:
            from nekro_agent.services.command.registry import command_registry

            command = command_registry.resolve(command_name)
            if command is not None:
                target_meta = command.metadata

        state_key = target_meta.name if target_meta is not None else command_name
        if target_meta is not None and not self._is_plugin_command_source_enabled(
            target_meta.source,
            plugin_enabled_cache,
        ):
            return False

        if chat_key:
            channel_state = self._load_channel_state(chat_key)
            if state_key in channel_state:
                return channel_state[state_key]

        system_state = self._load_system_state()
        if state_key in system_state:
            return system_state[state_key]

        return True

    def is_command_enabled_for_meta(
        self,
        meta: CommandMetadata,
        chat_key: Optional[str] = None,
        *,
        plugin_enabled_cache: Optional[dict[str, bool]] = None,
    ) -> bool:
        """按命令元数据查询启用状态，同时校验插件启用态。"""
        return self._is_command_enabled_impl(
            meta.name,
            chat_key,
            meta=meta,
            plugin_enabled_cache=plugin_enabled_cache,
        )

    def is_command_enabled(self, command_name: str, chat_key: Optional[str] = None) -> bool:
        """查询命令是否启用（频道级 > 系统级 > 默认），并兼容插件启用态。"""
        return self._is_command_enabled_impl(command_name, chat_key)

    def get_command_permission(
        self,
        command_name: str,
        default_permission: CommandPermission,
        chat_key: Optional[str] = None,
    ) -> CommandPermission:
        """查询命令生效权限（频道级 > 系统级 > 注册默认）"""
        if chat_key:
            channel_state = self._load_channel_permission_state(chat_key)
            if command_name in channel_state:
                return channel_state[command_name]

        system_state = self._load_system_permission_state()
        if command_name in system_state:
            return system_state[command_name]

        return default_permission

    def has_permission_override(
        self,
        command_name: str,
        chat_key: Optional[str] = None,
    ) -> bool:
        """当前作用域下是否存在权限覆盖"""
        if chat_key:
            return command_name in self._load_channel_permission_state(chat_key)
        return command_name in self._load_system_permission_state()

    def get_user_permission(self, adapter_key: str, platform_userid: str) -> CommandPermission:
        """查询平台用户的命令权限等级，默认普通用户。"""
        return self.get_user_permission_record(adapter_key, platform_userid).permission

    def get_user_permission_record(self, adapter_key: str, platform_userid: str) -> UserPermissionRecord:
        """查询平台用户命令权限记录，包含授权来源。"""
        state = self._load_user_permission_state()
        return state.get(self._user_permission_key(adapter_key, platform_userid), UserPermissionRecord(CommandPermission.USER))

    def get_channel_user_permission(self, chat_key: str, adapter_key: str, platform_userid: str) -> CommandPermission:
        """查询某人在**这一个频道**里的授权，没有授权为普通用户。

        只按完整 `chat_key` 精确匹配：租户 A 在自己频道里给的授权，换到任何别的频道都不存在。
        """
        records = self._load_channel_user_permission_state().get(chat_key, {})
        record = records.get(self._user_permission_key(adapter_key, platform_userid))
        return record.permission if record is not None else CommandPermission.USER

    def list_channel_user_permissions(self, chat_key: str, adapter_key: str) -> dict[str, UserPermissionRecord]:
        """列出某频道里的全部授权，键是平台用户 ID。"""
        prefix = f"{adapter_key}:"
        return {
            key[len(prefix):]: record
            for key, record in self._load_channel_user_permission_state().get(chat_key, {}).items()
            if key.startswith(prefix)
        }

    @staticmethod
    def _resolve_command(command_name: str) -> tuple[str, CommandPermission]:
        """解析命令名（含别名）到规范名和默认权限，命令不存在则抛出 ValidationError。"""
        from nekro_agent.services.command.registry import command_registry

        command = command_registry.resolve(command_name)
        if command is None:
            raise ValidationError(reason=f"命令不存在: {command_name}")
        return command.metadata.name, command.metadata.permission

    @staticmethod
    def _get_command_default_permission(command_name: str) -> CommandPermission:
        _, permission = CommandManager._resolve_command(command_name)
        return permission

    async def set_command_enabled(
        self,
        command_name: str,
        enabled: bool,
        chat_key: Optional[str] = None,
    ) -> None:
        """设置命令启用状态"""
        canonical_name, _ = self._resolve_command(command_name)
        if chat_key:
            state = self._load_channel_state(chat_key)
            state[canonical_name] = enabled
            self._save_channel_state(chat_key, state)
        else:
            state = self._load_system_state()
            state[canonical_name] = enabled
            self._save_system_state(state)
        await self.notify_commands_changed(chat_key)

    async def set_command_permission(
        self,
        command_name: str,
        permission: CommandPermission | str,
        chat_key: Optional[str] = None,
    ) -> None:
        """设置命令权限覆盖"""
        normalized_permission = (
            permission if isinstance(permission, CommandPermission) else CommandPermission(permission)
        )
        canonical_name, default_permission = self._resolve_command(command_name)
        if chat_key:
            state = self._load_channel_permission_state(chat_key)
            inherited_permission = self.get_command_permission(canonical_name, default_permission, None)
            if normalized_permission == inherited_permission:
                state.pop(canonical_name, None)
            else:
                state[canonical_name] = normalized_permission
            self._save_channel_permission_state(chat_key, state)
        else:
            state = self._load_system_permission_state()
            if normalized_permission == default_permission:
                state.pop(canonical_name, None)
            else:
                state[canonical_name] = normalized_permission
            self._save_system_permission_state(state)
        await self.notify_commands_changed(chat_key)

    async def reset_command_state(
        self,
        command_name: str,
        chat_key: Optional[str] = None,
    ) -> None:
        """重置命令状态（删除覆盖，回退到上级）"""
        from nekro_agent.services.command.registry import command_registry

        resolved = command_registry.resolve(command_name)
        canonical_name = resolved.metadata.name if resolved is not None else command_name
        if chat_key:
            state = self._load_channel_state(chat_key)
            state.pop(canonical_name, None)
            self._save_channel_state(chat_key, state)
        else:
            state = self._load_system_state()
            state.pop(canonical_name, None)
            self._save_system_state(state)
        await self.notify_commands_changed(chat_key)

    async def reset_command_permission(
        self,
        command_name: str,
        chat_key: Optional[str] = None,
    ) -> None:
        """重置命令权限覆盖（删除覆盖，回退到上级）"""
        from nekro_agent.services.command.registry import command_registry

        resolved = command_registry.resolve(command_name)
        canonical_name = resolved.metadata.name if resolved is not None else command_name
        if chat_key:
            state = self._load_channel_permission_state(chat_key)
            state.pop(canonical_name, None)
            self._save_channel_permission_state(chat_key, state)
        else:
            state = self._load_system_permission_state()
            state.pop(canonical_name, None)
            self._save_system_permission_state(state)
        await self.notify_commands_changed(chat_key)

    async def set_user_permission(
        self,
        adapter_key: str,
        platform_userid: str,
        permission: CommandPermission | str,
        *,
        source: UserPermissionSource = USER_PERMISSION_SOURCE_MANUAL,
        channel_id: Optional[str] = None,
    ) -> None:
        """设置平台用户的命令权限等级。"""
        normalized_permission = (
            permission if isinstance(permission, CommandPermission) else CommandPermission(permission)
        )
        state = self._load_user_permission_state(for_write=True)
        key = self._user_permission_key(adapter_key, platform_userid)
        if normalized_permission == CommandPermission.USER:
            state.pop(key, None)
        else:
            state[key] = UserPermissionRecord(
                permission=normalized_permission,
                source=source,
                channel_id=channel_id,
            )
        self._save_user_permission_state(state)

    async def reset_user_permission(
        self,
        adapter_key: str,
        platform_userid: str,
        *,
        source: Optional[UserPermissionSource] = None,
        channel_id: Optional[str] = None,
    ) -> None:
        """重置平台用户命令权限，回退到普通用户。"""
        state = self._load_user_permission_state(for_write=True)
        key = self._user_permission_key(adapter_key, platform_userid)
        record = state.get(key)
        if record is None:
            return
        if source is not None and record.source != source:
            return
        if channel_id is not None and record.channel_id != channel_id:
            return
        state.pop(key, None)
        self._save_user_permission_state(state)

    async def set_channel_user_permission(
        self,
        chat_key: str,
        adapter_key: str,
        platform_userid: str,
        permission: CommandPermission | str,
    ) -> None:
        """设置某人在**这一个频道**里的命令授权：只收 `advanced`（授予）与 `user`（撤销）

        调用方负责校验当前身份能否操作 `chat_key`（后台入口是 `assert_channel_accessible`）。
        这里只钉住两件与身份无关的事：写得出的档位只有一档，空的标识不落盘。
        """
        normalized_permission = (
            permission if isinstance(permission, CommandPermission) else CommandPermission(permission)
        )
        if normalized_permission not in (CommandPermission.ADVANCED, CommandPermission.USER):
            raise ValidationError(reason=f"频道授权只能是 advanced 或 user: {normalized_permission.value}")
        if not chat_key or not adapter_key or not platform_userid:
            raise ValidationError(reason="频道、适配器与平台用户 ID 都不能为空")

        state = self._load_channel_user_permission_state(for_write=True)
        records = dict(state.get(chat_key, {}))
        key = self._user_permission_key(adapter_key, platform_userid)
        if normalized_permission == CommandPermission.USER:
            if key not in records:
                return
            records.pop(key)
        else:
            if key not in records and len(records) >= CHANNEL_USER_PERMISSION_LIMIT:
                raise ValidationError(reason=f"一个频道最多授权 {CHANNEL_USER_PERMISSION_LIMIT} 人")
            records[key] = UserPermissionRecord(
                permission=CommandPermission.ADVANCED,
                source=USER_PERMISSION_SOURCE_MANUAL,
                channel_id=chat_key,
            )
        new_state = dict(state)
        new_state[chat_key] = records
        self._save_channel_user_permission_state(new_state)

    def migrate_legacy_advanced_overrides(self) -> list[str]:
        """把引入用户级权限之前写下的 `advanced` 命令权限覆盖改成 `super_user`，只做一次

        之前 `is_advanced_user` 只可能对 SUPER_USERS 成立，所以平台方当时把某条命令覆盖成
        `advanced`，实际效果是「只有超级用户能用」。引入用户级权限之后，同一个值会让频道管理员
        （租户授权的人、开了群角色识别的群管理）也够得着——一条平台级命令（配额、插件启停）
        就这样悄悄对所有租户的群管理开放了。这里按旧的实际效果改写，保持升级前后谁能用不变。

        升级之后平台方新写的 `advanced` 是新含义，不能再被改掉，所以迁过的文件以后不再碰，靠标记文件记账：

        - 读不出来（坏 JSON、非 UTF-8、瞬时 IO 错误）或写不进去的文件不改，记进标记的 `pending`；
          以后每次启动只重试这几个文件——人工修好之后它们仍是升级前写下的内容，按旧含义改写是对的；
        - 标记本身读不出来时当作「迁过、无待重试」：宁可少迁，也不去改写可能是新含义的覆盖；
        - 标记写不进去（磁盘满等）只记错误日志：下次启动会把全部文件再过一遍，期间平台方新写的
          `advanced` 会被改成 `super_user`。这个方向只收紧不放宽。

        启动时在注册内置命令之后、在任何消息处理之前同步调用一次。

        Returns:
            本次改写的覆盖，形如 `system:quota_set`、`channel_permissions/xxx.json:na_on`
        """
        marker = Path(COMMAND_STATE_DIR) / PERMISSION_SEMANTICS_MARKER_NAME
        candidates: list[tuple[str, Path]] = [("system", Path(COMMAND_SYSTEM_PERMISSION_FILE))]
        channel_dir = Path(COMMAND_CHANNEL_PERMISSION_DIR)
        if channel_dir.is_dir():
            candidates.extend((f"channel_permissions/{path.name}", path) for path in sorted(channel_dir.glob("*.json")))

        previously_migrated: list[str] = []
        if marker.exists():
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
            except (ValueError, OSError) as e:
                logger.warning(f"命令权限迁移标记读不出来，按已迁移处理: {e}")
                return []
            pending_before = previous.get("pending") if isinstance(previous, dict) else None
            if not isinstance(pending_before, list) or not pending_before:
                return []
            candidates = [(label, path) for label, path in candidates if label in set(pending_before)]
            raw_migrated = previous.get("migrated")
            previously_migrated = [str(item) for item in raw_migrated] if isinstance(raw_migrated, list) else []

        migrated: list[str] = []
        pending: list[str] = []
        for label, path in candidates:
            if not path.exists():
                continue
            try:
                raw_state = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as e:
                logger.warning(f"迁移命令权限覆盖时读不出 {label}，留待下次启动重试: {e}")
                pending.append(label)
                continue
            if not isinstance(raw_state, dict):
                continue
            hits = [name for name, value in raw_state.items() if value == CommandPermission.ADVANCED.value]
            if not hits:
                continue
            for name in hits:
                raw_state[name] = CommandPermission.SUPER_USER.value
            try:
                _write_json_atomic(path, raw_state)
            except OSError as e:
                logger.warning(f"迁移命令权限覆盖时写不进 {label}，留待下次启动重试: {e}")
                pending.append(label)
                continue
            migrated.extend(f"{label}:{name}" for name in hits)
        self._system_permission_cache = None
        self._channel_permission_cache.clear()

        try:
            _write_json_atomic(
                marker,
                {"advanced": "channel_admin", "migrated": previously_migrated + migrated, "pending": pending},
            )
        except OSError as e:
            logger.error(f"命令权限迁移标记写不进去，下次启动会重新检查全部覆盖: {e}")
        if migrated:
            logger.warning(
                "命令权限覆盖里的 advanced 已改为 super_user，保持升级前「只有超级用户能用」的效果；"
                f"需要对频道管理员开放的命令请重新设置: {', '.join(migrated)}",
            )
        if pending:
            logger.warning(f"以下命令权限覆盖文件没能迁移，下次启动重试: {', '.join(pending)}")
        return migrated

    async def get_all_command_states(
        self,
        chat_key: Optional[str] = None,
    ) -> list[dict]:
        """获取所有命令及其状态（用于 WebUI 展示）"""
        from nekro_agent.services.command.registry import command_registry
        from nekro_agent.services.plugin.collector import plugin_collector

        commands = command_registry.list_all_commands()
        channel_state = self._load_channel_state(chat_key) if chat_key else {}
        plugin_enabled_cache: dict[str, bool] = {}
        result = []
        for meta in commands:
            enabled = self.is_command_enabled_for_meta(meta, chat_key, plugin_enabled_cache=plugin_enabled_cache)
            permission = self.get_command_permission(meta.name, meta.permission, chat_key)
            has_channel_override = chat_key is not None and meta.name in channel_state
            has_permission_override = self.has_permission_override(meta.name, chat_key)
            source_display_name = "内置" if meta.source == BUILT_IN_SOURCE else meta.source
            if meta.source != BUILT_IN_SOURCE:
                plugin = plugin_collector.get_plugin(meta.source)
                if plugin:
                    source_display_name = plugin.name

            result.append({
                "name": meta.name,
                "namespace": meta.namespace,
                "aliases": meta.aliases,
                "description": meta.description,
                "usage": meta.usage,
                "permission": permission.value,
                "default_permission": meta.permission.value,
                "category": meta.category,
                "source": meta.source,
                "source_display_name": source_display_name,
                "enabled": enabled,
                "has_channel_override": has_channel_override,
                "has_permission_override": has_permission_override,
                "params_schema": meta.params_schema,
                "i18n_description": meta.i18n_description,
                "i18n_usage": meta.i18n_usage,
                "i18n_category": meta.i18n_category,
            })
        return result

    async def notify_commands_changed(self, chat_key: Optional[str] = None) -> None:
        """通知所有支持补全的适配器重新同步命令列表"""
        from nekro_agent.adapters import loaded_adapters

        for adapter in loaded_adapters.values():
            if adapter.supports_command_completion:
                try:
                    await adapter.sync_commands(chat_key)
                except Exception as e:
                    logger.warning(f"适配器 {adapter.key} 同步命令失败: {e}")

    def invalidate_cache(self) -> None:
        """清除缓存（配置文件外部修改后调用）"""
        self._system_cache = None
        self._channel_cache.clear()
        self._system_permission_cache = None
        self._channel_permission_cache.clear()
        self._user_permission_cache = None
        self._channel_user_permission_cache = None


command_manager = CommandManager()
