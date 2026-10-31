from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class CCModelPresetCreate(BaseModel):
    name: str = Field(..., max_length=64)
    description: str = Field(default="", max_length=512)
    base_url: str = Field(default="https://api.nekro.ai/claude", max_length=256)
    auth_token: str = Field(default="", max_length=256)
    api_timeout_ms: str = Field(default="3000000", max_length=16)
    model_type: Literal["preset", "manual"] = "preset"
    preset_model: str = Field(default="opus", max_length=64)
    anthropic_model: str = Field(default="", max_length=128)
    small_fast_model: str = Field(default="", max_length=128)
    default_sonnet: str = Field(default="", max_length=128)
    default_opus: str = Field(default="", max_length=128)
    default_haiku: str = Field(default="", max_length=128)
    extra_env: Dict[str, str] = {}


class CCModelPresetUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = Field(default=None, max_length=512)
    base_url: Optional[str] = Field(default=None, max_length=256)
    auth_token: Optional[str] = Field(default=None, max_length=256)
    api_timeout_ms: Optional[str] = Field(default=None, max_length=16)
    model_type: Optional[Literal["preset", "manual"]] = None
    preset_model: Optional[str] = Field(default=None, max_length=64)
    anthropic_model: Optional[str] = Field(default=None, max_length=128)
    small_fast_model: Optional[str] = Field(default=None, max_length=128)
    default_sonnet: Optional[str] = Field(default=None, max_length=128)
    default_opus: Optional[str] = Field(default=None, max_length=128)
    default_haiku: Optional[str] = Field(default=None, max_length=128)
    extra_env: Optional[Dict[str, str]] = None


class CCModelPresetInfo(BaseModel):
    id: int
    name: str
    description: str
    base_url: str
    auth_token: str
    api_timeout_ms: str
    model_type: str
    preset_model: str
    anthropic_model: str
    small_fast_model: str
    default_sonnet: str
    default_opus: str
    default_haiku: str
    extra_env: Dict[str, str]
    is_default: bool
    create_time: str
    update_time: str
    # 对租户身份整份置空：这份 JSON 是要写进容器 settings.json 的环境变量，
    # 里面带 ANTHROPIC_AUTH_TOKEN，掩掉单个字段没有部分可用的语义。
    # 必须是 Optional，否则脱敏后的 None 会在响应序列化时抛 ValidationError，
    # 把租户的读端点整个打成 400（与 workspaces 的 get_cc_model_preset 同一约定）。
    config_json: Optional[Dict[str, Any]] = None


class CCModelPresetListResponse(BaseModel):
    total: int
    items: List[CCModelPresetInfo]
