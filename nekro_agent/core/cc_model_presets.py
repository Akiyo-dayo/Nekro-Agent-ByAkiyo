"""File-based persistence store for CC model presets."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Literal, Optional

from pydantic import BaseModel

from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import OsEnv

CC_PRESETS_PATH = Path(OsEnv.DATA_DIR) / "configs" / "cc-model-presets.json"

DEFAULT_PRESET_NAME = "default"

# 键名里含这些片段的 `extra_env` 项按「像凭据」处理：脱敏时按字面量遮蔽，
# 不下发给不能拿平台方凭据的工作区。
SECRET_ENV_KEY_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD")


def is_secret_env_key(key: str) -> bool:
    """这个环境变量名看起来是不是一份凭据

    判据是**名字**而不是值：值与取值形态无关（空串也可能是占位），而名字是运维唯一
    显式表达「这一项是密钥」的地方。大小写不敏感——`apiKey` 与 `API_KEY` 是同一回事。
    """
    upper = str(key).upper()
    return any(marker in upper for marker in SECRET_ENV_KEY_MARKERS)


class CCModelPresetItem(BaseModel):
    id: int
    name: str
    description: str = ""
    base_url: str = "https://api.nekro.ai/claude"
    auth_token: str = ""
    api_timeout_ms: str = "3000000"
    model_type: Literal["preset", "manual"] = "preset"
    preset_model: str = "opus"
    anthropic_model: str = ""
    small_fast_model: str = ""
    default_sonnet: str = ""
    default_opus: str = ""
    default_haiku: str = ""
    extra_env: Dict[str, str] = {}
    is_default: bool = False
    create_time: str = ""
    update_time: str = ""

    def to_config_json(self) -> Dict[str, Any]:
        env: Dict[str, str] = {
            "ANTHROPIC_BASE_URL": self.base_url,
            "ANTHROPIC_AUTH_TOKEN": self.auth_token,
            "API_TIMEOUT_MS": self.api_timeout_ms,
        }
        if self.model_type == "preset":
            env.update(self.extra_env)
            return {"env": env, "model": self.preset_model, "includeCoAuthoredBy": False}
        # manual — add non-empty model env vars
        for key, val in [
            ("ANTHROPIC_MODEL", self.anthropic_model),
            ("ANTHROPIC_SMALL_FAST_MODEL", self.small_fast_model),
            ("ANTHROPIC_DEFAULT_SONNET_MODEL", self.default_sonnet),
            ("ANTHROPIC_DEFAULT_OPUS_MODEL", self.default_opus),
            ("ANTHROPIC_DEFAULT_HAIKU_MODEL", self.default_haiku),
        ]:
            if val:
                env[key] = val
        env.update(self.extra_env)
        return {"env": env, "includeCoAuthoredBy": False}


class CCModelPresetsStore:
    def __init__(self, path: Path = CC_PRESETS_PATH):
        self.path = path
        self._default_checked = False

    def _load_raw(self) -> dict:
        if not self.path.exists():
            return {"presets": [], "_next_id": 1}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save_raw(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def ensure_default(self) -> "CCModelPresetItem":
        """确保默认预设存在，若不存在则自动创建。"""
        if self._default_checked:
            raw = self._load_raw()
            for p in raw["presets"]:
                if p.get("is_default"):
                    return CCModelPresetItem(**p)
        self._default_checked = True
        raw = self._load_raw()
        for p in raw["presets"]:
            if p.get("is_default"):
                return CCModelPresetItem(**p)
        # 创建默认预设
        now = datetime.now(timezone.utc).isoformat()
        preset_id = raw["_next_id"]
        item = CCModelPresetItem(
            id=preset_id,
            name=DEFAULT_PRESET_NAME,
            description="默认 CC 模型配置（不可删除）",
            is_default=True,
            extra_env={
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            },
            create_time=now,
            update_time=now,
        )
        raw["presets"].insert(0, item.model_dump())
        raw["_next_id"] = preset_id + 1
        self._save_raw(raw)
        return item

    def get_default(self) -> Optional["CCModelPresetItem"]:
        """获取默认预设。"""
        raw = self._load_raw()
        for p in raw["presets"]:
            if p.get("is_default"):
                return CCModelPresetItem(**p)
        return None

    def secret_values(self) -> FrozenSet[str]:
        """预设里需要按字面量遮蔽的凭据

        CC 模型预设存在 `configs/cc-model-presets.json` 这个独立文件里，不在 `ConfigManager`
        的配置树上，所以 `collect_platform_secrets()` 收不到它——上游报错回显预设凭据时，
        脱敏集合里必须显式带上这些值。

        **原值与 `strip()` 后的取值都要收**（理由同 `routers/cc_relay.py` 的
        `_relay_secret_values`）：真实上线的是 strip 之后的那份，而预设里可能存着带尾随
        换行的 Key，只收原文就遮不住上游回显的那一份。

        只读 `_load_raw()`：`ensure_default` / `list_all` 会写文件，脱敏不能有落盘副作用。
        文件不存在时返回空集合；读文件或解析出错时记 WARNING 并返回空集合，不抛。
        """
        values: set[str] = set()
        try:
            raw = self._load_raw()
            for preset in raw.get("presets", []):
                token = str(preset.get("auth_token") or "")
                values.update(candidate for candidate in (token, token.strip()) if candidate)
                extra_env = preset.get("extra_env") or {}
                if not isinstance(extra_env, dict):
                    continue
                for key, value in extra_env.items():
                    if not is_secret_env_key(key):
                        continue
                    text = str(value or "")
                    values.update(candidate for candidate in (text, text.strip()) if candidate)
        except Exception as e:
            logger.warning(f"[cc_model_presets] 读取预设凭据失败，脱敏集合按空处理: {e}")
            return frozenset()
        return frozenset(values)

    def list_all(self) -> List[CCModelPresetItem]:
        self.ensure_default()
        raw = self._load_raw()
        return [CCModelPresetItem(**p) for p in raw["presets"]]

    def get_by_id(self, preset_id: int) -> Optional[CCModelPresetItem]:
        raw = self._load_raw()
        for p in raw["presets"]:
            if p["id"] == preset_id:
                return CCModelPresetItem(**p)
        return None

    def name_exists(self, name: str, exclude_id: Optional[int] = None) -> bool:
        raw = self._load_raw()
        for p in raw["presets"]:
            if p["name"] == name and p["id"] != exclude_id:
                return True
        return False

    def create(self, **kwargs) -> CCModelPresetItem:
        raw = self._load_raw()
        now = datetime.now(timezone.utc).isoformat()
        preset_id = raw["_next_id"]
        item = CCModelPresetItem(id=preset_id, create_time=now, update_time=now, **kwargs)
        raw["presets"].append(item.model_dump())
        raw["_next_id"] = preset_id + 1
        self._save_raw(raw)
        return item

    def update(self, preset_id: int, **kwargs) -> Optional[CCModelPresetItem]:
        raw = self._load_raw()
        for i, p in enumerate(raw["presets"]):
            if p["id"] == preset_id:
                # is_default 不允许通过 update 修改
                kwargs.pop("is_default", None)
                p.update(kwargs)
                p["update_time"] = datetime.now(timezone.utc).isoformat()
                raw["presets"][i] = p
                self._save_raw(raw)
                return CCModelPresetItem(**p)
        return None

    def delete(self, preset_id: int) -> str:
        """删除预设。返回 'ok' | 'not_found' | 'protected'"""
        raw = self._load_raw()
        for p in raw["presets"]:
            if p["id"] == preset_id:
                if p.get("is_default"):
                    return "protected"
                raw["presets"] = [x for x in raw["presets"] if x["id"] != preset_id]
                self._save_raw(raw)
                return "ok"
        return "not_found"


cc_presets_store = CCModelPresetsStore()
