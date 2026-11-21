"""
# 表情包 (Emotion)

提供收集、搜索、使用表情包的能力，使用向量数据库进行智能语义搜索。

## 套组与会话可见性

表情按**套组**组织：平台方的预设套组、存量（待整理）套组、每个实例一个收集库。一个会话能读到
哪些套组由 `nekro_agent/services/emotion_library.py` 按实例归属与当前人设计算；插件只把配置
（`ENABLED_SETS` / `PERSONA_SET_RULES`）交进去。配置只能在「可用池」里挑，写了池外的套组 ID
一律忽略——配置不能给自己授权。

能改能删的只有本实例收集库（`W(S)`）。平台方预设套组与存量套组只能进控制台改，平台方实例的
会话也一样：会话侧的模型参数不可信，放开它就等于把「整理好的套组」交给群友。

## 主要功能

- **智能收藏**: 在聊天时发送图片，AI 会自动询问是否要收藏为表情包，并为其添加描述和标签。
- **语义搜索**: 可以用自然语言描述来搜索表情包，比如 "来张开心的猫猫图"。
- **后台管理**: 提供了一系列命令，方便用户管理自己的表情包收藏；控制台接口见 `mount_router`。

## 使用方法

- **与 AI 对话**: 在聊天中发送图片，AI 会引导您完成收藏。您也可以直接命令 AI "帮我收藏这张图"。
- **使用命令**: 通过 `emo_search` 等命令直接搜索和管理表情包。

## 命令列表

**注意：命令仅在支持图片收发的聊天适配器下可用。**

- `emo_search <关键词>`: 在本频道可读的套组里语义搜索。
- `emo_stats`: 查看本频道可读套组的统计信息。
- `emo_list [页码]`: 分页列出本频道可读的表情包，并默认预览图片。
- `emo_migrate`: 修存量套组里那些还记着绝对路径的行（只改文件名）。
- `emo_reindex -y`: 重建索引（高级功能，一般无需使用）。

## 配置说明

- **自动收集**: 关掉后 `collect_emotion` 直接拒绝，提示词里也不再引导收集。
- **启用的套组 / 人设套组规则**: 本实例用哪些套组，见 `EmotionConfig`。
- **严格表情包模式**: 开启后，插件会尝试拒绝收藏截图、照片等非表情包内容的图片。
- **嵌入模型组**: 需要正确配置一个向量模型才能使用语义搜索功能。
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, ClassVar, Dict, List, Optional, Set, Tuple

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, field_validator
from qdrant_client import models as qdrant_models

from nekro_agent.api import i18n, schemas
from nekro_agent.api.core import ModelConfigGroup, get_qdrant_client, logger
from nekro_agent.api.core import config as core_config
from nekro_agent.api.plugin import (
    ConfigBase,
    ExtraField,
    NekroPlugin,
    SandboxMethodType,
)
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_emotion_item import DBEmotionItem
from nekro_agent.models.db_emotion_set import DBEmotionSet
from nekro_agent.models.db_emotion_set_assignment import DBEmotionSetAssignment
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.schemas.errors import AppError, NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.i18n import I18nDict
from nekro_agent.services import emotion_library
from nekro_agent.services.agent.creator import ContentSegment, OpenAIChatMessage
from nekro_agent.services.agent.openai import gen_openai_embeddings
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import (
    Arg,
    CommandExecutionContext,
    CommandOutputSegment,
    CommandOutputSegmentType,
    CommandResponse,
)
from nekro_agent.services.plugin.route_auth import PluginRouteScope, plugin_route_scope
from nekro_agent.services.plugin.scope import instance_scope, resolve_chat_plugin_instance
from nekro_agent.services.usage import LLMUsageContext, LLMUsageScene, usage_scope
from nekro_agent.tools.common_util import UnsafeDownloadUrlError, copy_to_upload_dir, fetch_url_bytes
from nekro_agent.tools.path_convertor import convert_filename_to_sandbox_upload_path
from nekro_agent.tools.untrusted_content import echo_inline, sanitize_block_text, sanitize_inline_text

plugin = NekroPlugin(
    name="表情包插件",
    module_name="emotion",
    description="提供收集、搜索、使用表情包能力，使用Qdrant向量数据库",
    version="0.1.0",
    author="KroMiose",
    url="https://github.com/KroMiose/nekro-agent",
    support_adapter=["onebot_v11", "discord", "qqbot_openclaw"],
    i18n_name=i18n.i18n_text(
        zh_CN="表情包插件",
        en_US="Emotion Pack Plugin",
    ),
    i18n_description=i18n.i18n_text(
        zh_CN="提供收集、搜索、使用表情包能力，使用Qdrant向量数据库",
        en_US="Provides emotion pack collection, search and usage features using Qdrant vector database",
    ),
    sleep_brief="用于收藏、搜索和发送表情包，在需要图片表情语义检索或表情管理时激活。",
)


class PersonaSetRule(BaseModel):
    """「当前人设是它时用这几个套组」的一条规则

    做成可编辑的对象列表而不是 `人设ID:套组ID,...` 的字符串：套组 ID 是控制台里的数字，规则
    经常要一次列五六个，字符串格式填错一个逗号就整条失效，而对象列表在前端是逐项表单、坏值
    能被指出是哪一项。配置里存的仍是 JSON，兼容旧版只读的情形。
    """

    PRESET_ID: int = Field(
        default=0,
        title="人设 ID",
        description="当前生效人设的 ID；填 0 表示「内置人设」档，不会匹配任何人设",
    )
    SET_IDS: List[int] = Field(
        default_factory=list,
        title="套组 ID 列表",
        description="这个人设生效时启用哪些套组；只能在可用池里挑，池外的 ID 会被忽略",
    )


def _parse_int_list(value: Any, *, field_name: str) -> List[int]:
    """把「一串套组 ID」的几种写法读成 `List[int]`，坏值记 WARNING 后丢掉"""
    if value is None:
        return []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [int(value)]
    if isinstance(value, str):
        # 逗号、中文逗号、空白都当分隔符；`#` 之后是注释
        items = [piece for line in value.splitlines() if not line.strip().startswith("#") for piece in re.split(r"[,，\s]+", line)]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        logger.warning(f"{field_name} 不是列表，已忽略: {type(value).__name__}")
        return []

    result: List[int] = []
    for item in items:
        if isinstance(item, bool) or item is None:
            logger.warning(f"{field_name} 里有一项不是套组 ID，已跳过: {item!r}")
            continue
        text = str(item).strip()
        if not text:
            continue
        try:
            number = int(text)
        except (TypeError, ValueError):
            logger.warning(f"{field_name} 里有一项不是套组 ID，已跳过: {item!r}")
            continue
        if number <= 0:
            logger.warning(f"{field_name} 里的套组 ID 必须大于 0，已跳过: {item!r}")
            continue
        if number not in result:
            result.append(number)
    return result


def _parse_persona_rules_text(text: str) -> List[PersonaSetRule]:
    """读 `人设ID:套组ID,套组ID` 的文本写法（每行一条），坏行跳过"""
    rules: List[PersonaSetRule] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", maxsplit=1)[0].strip()
        if not line:
            continue
        preset_part, _, sets_part = line.partition(":")
        try:
            preset_id = int(preset_part.strip())
        except (TypeError, ValueError):
            logger.warning(f"人设套组规则的一行缺人设 ID，已跳过: {raw_line!r}")
            continue
        set_ids = _parse_int_list(sets_part, field_name="PERSONA_SET_RULES")
        if not set_ids:
            logger.warning(f"人设套组规则的一行没有有效的套组 ID，已跳过: {raw_line!r}")
            continue
        rules.append(PersonaSetRule(PRESET_ID=preset_id, SET_IDS=set_ids))
    return rules

@plugin.mount_config()
class EmotionConfig(ConfigBase):
    """表情包配置"""

    MAX_RECENT_EMOTION_COUNT: int = Field(
        default=5,
        title="最近添加表情包显示数量",
        description="最近添加表情包最大显示数量",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="最近添加表情包显示数量",
                en_US="Recent Emotion Display Count",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="最近添加表情包最大显示数量",
                en_US="Maximum number of recently added emotions to display",
            ),
        ).model_dump(),
    )
    MAX_SEARCH_RESULTS: int = Field(
        default=3,
        title="搜索结果显示数量",
        description="搜索结果显示数量",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="搜索结果显示数量",
                en_US="Search Result Display Count",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="搜索结果显示数量",
                en_US="Number of search results to display",
            ),
        ).model_dump(),
    )
    EMBEDDING_MODEL: str = Field(
        default="text-embedding",
        title="嵌入模型组",
        description="在此填入向量嵌入模型组名称",
        json_schema_extra=ExtraField(
            ref_model_groups=True,
            required=True,
            platform_only=True,
            model_type="embedding",
            i18n_title=i18n.i18n_text(
                zh_CN="嵌入模型组",
                en_US="Embedding Model Group",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="在此填入向量嵌入模型组名称",
                en_US="Enter the vector embedding model group name here",
            ),
        ).model_dump(),
    )
    EMBEDDING_DIMENSION: int = Field(
        default=1024,
        title="嵌入维度",
        description="嵌入维度",
        json_schema_extra=ExtraField(
            platform_only=True,
            i18n_title=i18n.i18n_text(
                zh_CN="嵌入维度",
                en_US="Embedding Dimension",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="嵌入维度",
                en_US="Dimension of embeddings",
            ),
        ).model_dump(),
    )
    STRICT_EMOTION_COLLECT: bool = Field(
        default=False,
        title="严格表情包模式",
        description="是否严格限制表情包收集并拒绝非表情包内容 (截图、非表情包内容等)",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="严格表情包模式",
                en_US="Strict Emotion Mode",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="是否严格限制表情包收集并拒绝非表情包内容 (截图、非表情包内容等)",
                en_US="Whether to strictly limit emotion collection and reject non-emotion content (screenshots, etc.)",
            ),
        ).model_dump(),
    )
    EMBEDDING_REQUEST_TIMEOUT: int = Field(
        default=5,
        title="向量化请求超时时间",
        description="生成嵌入向量时的 API 请求超时时间（秒）",
        json_schema_extra=ExtraField(
            platform_only=True,
            i18n_title=i18n.i18n_text(
                zh_CN="向量化请求超时时间",
                en_US="Embedding Request Timeout",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="生成嵌入向量时的 API 请求超时时间（秒）",
                en_US="Timeout in seconds for embedding API requests",
            ),
        ).model_dump(),
    )
    IMAGE_DOWNLOAD_TIMEOUT: int = Field(
        default=30,
        title="图片下载超时时间",
        description="下载表情包图片时的超时时间（秒）",
        json_schema_extra=ExtraField(
            platform_only=True,
            i18n_title=i18n.i18n_text(
                zh_CN="图片下载超时时间",
                en_US="Image Download Timeout",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="下载表情包图片时的超时时间（秒）",
                en_US="Timeout in seconds for downloading emotion images",
            ),
        ).model_dump(),
    )
    AUTO_COLLECT_ENABLED: bool = Field(
        default=True,
        title="自动收集",
        description="关掉后 Agent 不能再收集新表情，提示词里也不再引导收集；已有表情照常搜索使用",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="自动收集",
                en_US="Automatic Collection",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="关掉后 Agent 不能再收集新表情，提示词里也不再引导收集；已有表情照常搜索使用",
                en_US=(
                    "When off, the agent can no longer collect new emotions and the prompt stops "
                    "guiding collection; existing emotions can still be searched and used"
                ),
            ),
        ).model_dump(),
    )
    ENABLED_SETS: List[int] = Field(
        default_factory=list,
        title="启用的套组",
        description="本实例启用哪些套组（填套组 ID，控制台里每个套组都显示 ID）；留空表示可用池里的全部",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="启用的套组",
                en_US="Enabled Sets",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="本实例启用哪些套组（填套组 ID，控制台里每个套组都显示 ID）；留空表示可用池里的全部",
                en_US=(
                    "Which sets this instance enables (set IDs are shown in the console); "
                    "empty means every set in the available pool"
                ),
            ),
        ).model_dump(),
    )
    PERSONA_SET_RULES: List[PersonaSetRule] = Field(
        default_factory=list,
        title="人设套组规则",
        description="按当前生效人设挑套组；没有匹配的条目时回落到「启用的套组」",
        json_schema_extra=ExtraField(
            sub_item_name="人设规则",
            i18n_title=i18n.i18n_text(
                zh_CN="人设套组规则",
                en_US="Persona Set Rules",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="按当前生效人设挑套组；没有匹配的条目时回落到「启用的套组」",
                en_US="Pick sets by the effective persona; falls back to the enabled sets when nothing matches",
            ),
        ).model_dump(),
    )
    INJECT_SET_SUMMARY: bool = Field(
        default=True,
        title="注入套组摘要",
        description="在提示词里列出本会话可读套组的名字、分类与条数（最多 10 套）",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(
                zh_CN="注入套组摘要",
                en_US="Inject Set Summary",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="在提示词里列出本会话可读套组的名字、分类与条数（最多 10 套）",
                en_US="List the readable sets of this session with name, category and count in the prompt (up to 10)",
            ),
        ).model_dump(),
    )

    @field_validator("ENABLED_SETS", mode="before")
    @classmethod
    def _coerce_enabled_sets(cls, value: Any) -> List[int]:
        """容忍几种手填形状：`[3, 4]`、`["3", "4"]`、`"3,4"`、`"3 4"`、带 `#` 的注释行

        控制台的数字输入框可能回传字符串，人也会直接在 JSON 里写一段逗号分隔；填错的值记一条
        WARNING 后丢掉，而不是让整份配置校验失败——一个手滑的套组 ID 不该让插件起不来。
        """
        return _parse_int_list(value, field_name="ENABLED_SETS")

    @field_validator("PERSONA_SET_RULES", mode="before")
    @classmethod
    def _coerce_persona_set_rules(cls, value: Any) -> List[PersonaSetRule]:
        """容忍 `{PRESET_ID, SET_IDS}` 对象列表与 `人设ID:套组ID,套组ID` 的字符串写法

        字符串写法（每行一条）是给习惯直接编辑配置文本的人留的后路；坏行跳过并记 WARNING，
        与上面同一个理由：配置里的一处手误不该挡住插件加载。
        """
        if value is None:
            return []
        if isinstance(value, str):
            return _parse_persona_rules_text(value)
        if not isinstance(value, list):
            logger.warning(f"人设套组规则不是列表，已忽略: {type(value).__name__}")
            return []

        rules: List[PersonaSetRule] = []
        for item in value:
            if isinstance(item, PersonaSetRule):
                rules.append(item)
            elif isinstance(item, dict):
                try:
                    rules.append(
                        PersonaSetRule(
                            PRESET_ID=int(item.get("PRESET_ID") or item.get("preset_id") or 0),
                            SET_IDS=_parse_int_list(item.get("SET_IDS", item.get("set_ids")), field_name="SET_IDS"),
                        ),
                    )
                except (TypeError, ValueError) as e:
                    logger.warning(f"人设套组规则的一项无法解析，已跳过: {item!r}, error={e}")
            else:
                logger.warning(f"人设套组规则的一项不是对象，已跳过: {item!r}")
        return rules


def _scope_options() -> Tuple[List[int], List[Tuple[int, List[int]]]]:
    """配置 → `(enabled_sets, persona_set_rules)`，所有入口共用这一份转换

    读的是 `plugin.get_config()` 的代理，因此调用方在 `instance_scope()` 里时拿到的是那个实例
    覆盖后的值。集中一处是为了让「配置怎么喂给服务层」只有一种口径：四个沙盒方法、提示词注入、
    五条命令都从这里取，谁也不会漏掉 `PERSONA_SET_RULES`。
    """
    enabled_sets = [int(set_id) for set_id in (emotion_config.ENABLED_SETS or [])]
    rules: List[Tuple[int, List[int]]] = []
    for rule in emotion_config.PERSONA_SET_RULES or []:
        preset_id = int(getattr(rule, "PRESET_ID", 0) or 0)
        set_ids = [int(set_id) for set_id in (getattr(rule, "SET_IDS", []) or [])]
        rules.append((preset_id, set_ids))
    return enabled_sets, rules


# 获取配置。这里拿到的是**按实例解析**的代理：在 `instance_scope()` 里读会返回该实例覆盖后的值。
emotion_config: EmotionConfig = plugin.get_config(EmotionConfig)


async def _load_legacy_store_raw() -> Optional[str]:
    """读出旧版那一整块 JSON（**只读**，这是全文件唯一读写这个键的地方）

    旧版把整库存在插件存储的 `store_key="emotion_store"` 里，一行 JSON。改造后库表是权威，
    但那一行不能改也不能删：它是回滚的唯一凭据，而且改造前收集的表情只在这里有来源记录。
    导入是幂等的（服务层按 `emotion_id` 跳过已有的），所以重复启动不会重复插入。

    已经删掉的表情不会被重新导入：它们当初就写在同一份 JSON 里，导入时逐条按 `emotion_id`
    查库、已有的跳过；改造后删除只删库表行，不再回写 JSON，所以 JSON 里仍留着它们，但既然
    库里查得到同 ID 的记录就说明是「保留的那条」，查不到才会插入——也就是说「删除」这个动作
    在 JSON 里从来不会被记账，靠标记行 `LEGACY_IMPORT_MARKER` 的条数比对来判断「已经导入过」，
    已经导入过就整段跳过，不给已删的条目第二次机会。
    """
    try:
        return await plugin.store.get(store_key="emotion_store")
    except Exception as e:
        logger.warning(f"读取旧版表情库 JSON 失败，跳过导入: {e}")
        return None


def _legacy_store_count(raw: Optional[str]) -> int:
    """数一下旧版 JSON 里有几条表情；读不出来按 0"""
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return 0
    emotions = payload.get("emotions") if isinstance(payload, dict) else None
    return len(emotions) if isinstance(emotions, dict) else 0


async def _import_legacy_store_once() -> None:
    """把旧版 JSON 导进「存量（待整理）」套组，并写下「已导入」的标记

    标记里记的是**导入时看到的条数**：条数一致就认为已经导过，不再碰库表。这样既不会重复
    插入，也不必在每次启动时把整份 JSON 重新解析一遍交给服务层。
    """
    raw = await _load_legacy_store_raw()
    expected = _legacy_store_count(raw)
    if expected == 0:
        return

    marker = await plugin.store.get(store_key=emotion_library.LEGACY_IMPORT_MARKER)
    try:
        imported = int(marker) if marker else 0
    except (TypeError, ValueError):
        imported = 0
    if imported == expected:
        logger.info(f"旧版表情库已导入过 {expected} 条，跳过导入")
        return

    counts = await emotion_library.import_legacy_store(raw)
    await plugin.store.set(store_key=emotion_library.LEGACY_IMPORT_MARKER, value=str(expected))
    logger.info(
        f"旧版表情库导入完成: total={counts['total']}, inserted={counts['inserted']}, "
        f"existing={counts['existing']}, missing_files={counts['missing_files']}",
    )


@plugin.mount_init_method()
async def init_vector_db():
    """初始化向量集合、导入旧版 JSON、回填向量点的套组归属"""
    # 获取Qdrant客户端
    client = await get_qdrant_client()
    if client is None:
        logger.warning("无法获取Qdrant客户端，跳过向量数据库初始化")
        return

    collection_name = emotion_library.EMOTION_VECTOR_COLLECTION

    # 检查集合是否存在
    try:
        collections = await client.get_collections()
    except Exception as e:
        raise ValueError(f"获取Qdrant集合失败: {e}") from e

    collection_names = [collection.name for collection in collections.collections]

    if collection_name not in collection_names:
        logger.info(f"正在创建表情包向量数据库集合: {collection_name}")
        # 创建集合
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=qdrant_models.VectorParams(
                size=emotion_config.EMBEDDING_DIMENSION,
                distance=qdrant_models.Distance.COSINE,
            ),
        )
        logger.success(f"表情包向量数据库集合 {collection_name} 创建成功")

    # 旧版 JSON 的导入放在集合建好之后：导入会按批改写向量载荷的 `set_id`，集合不在时那些点
    # 根本不存在，改写只会白跑一遍。
    try:
        await _import_legacy_store_once()
    except Exception as e:
        # 导入失败不该挡住插件启动：库表里已有的表情照常可用，下次启动会再试一次
        logger.warning(f"旧版表情库导入失败: {e}")

    # 回填放后台：要逐套组扫一遍库表、按批问向量库，大库上能跑几十秒，不能卡住插件加载。
    # 失败只记 WARNING —— 回填只是把载荷补齐，搜索时的库表复核会兜住缺的那部分。
    asyncio.create_task(_resync_vector_set_ids_in_background())


async def _embed_item_for_backfill(item: DBEmotionItem) -> Optional[List[float]]:
    """回填时给「库表有、向量库没有点」的表情重新生成嵌入

    文件已经不在的不补（返回 None）：与 `emo_reindex` 同口径——没有图的表情补进向量库只会
    被搜出来、再在发送那一步失败。检索文本与收集、重建索引同一个 `_embedding_text`。
    """
    if _emotion_file_of(item) is None:
        return None
    return await _safe_embedding(_embedding_text(item.description or "", list(item.tags or [])))


async def _resync_vector_set_ids_in_background() -> None:
    """后台回填向量载荷里的 `set_id`（并补上向量库里缺的点），异常只记 WARNING"""
    try:
        counts = await emotion_library.resync_vector_set_ids(embed_missing=_embed_item_for_backfill)
    except Exception as e:
        logger.warning(f"回填表情向量套组归属失败: {e}")
        return
    if counts.get("sets"):
        logger.info(
            f"表情向量套组归属回填完成: sets={counts['sets']}, points={counts['points']}, "
            f"batches={counts['batches']}, missing={counts.get('missing', 0)}, "
            f"reembedded={counts.get('reembedded', 0)}",
        )


# region: 表情包工具方法


# 只读条目的标记：中文，直接拼在搜索结果行里给模型看
PLATFORM_READONLY_NOTE = "（平台方提供，只读）"


async def generate_embedding(text: str, max_retries: int = 3) -> List[float]:
    """生成文本嵌入向量（带重试机制）

    Args:
        text: 要生成嵌入向量的文本
        max_retries: 最大重试次数，默认3次

    Returns:
        List[float]: 嵌入向量

    Raises:
        ValueError: 向量维度不匹配
        Exception: 重试次数耗尽后仍然失败
    """
    model_group: ModelConfigGroup = core_config.get_model_group_info(
        emotion_config.EMBEDDING_MODEL,
    )

    last_exception = None

    def _validate_dimension(vector: List[float]) -> None:
        """验证向量维度"""
        vector_dimension = len(vector)
        if vector_dimension != emotion_config.EMBEDDING_DIMENSION:
            error_msg = (
                f"嵌入向量维度错误！预期为 {emotion_config.EMBEDDING_DIMENSION} 维，"
                f"但实际获取到 {vector_dimension} 维。"
                f"请更新配置中的 EMBEDDING_DIMENSION 值为 {vector_dimension} 。"
            )
            logger.error(
                f"嵌入向量维度不匹配！预期: {emotion_config.EMBEDDING_DIMENSION}, 实际: {vector_dimension}",
            )
            raise ValueError(error_msg)

    # 表情包检索的向量化：与 `memory/embedding_service.py` 同一档、同一条理由。
    # 归属只到模型组这一层（本函数只拿得到一段文本），但钱一分不少地进账
    usage_context = LLMUsageContext(scene=LLMUsageScene.EMBEDDING).with_model_group(
        emotion_config.EMBEDDING_MODEL,
        model_group,
    )

    for attempt in range(max_retries):
        try:
            with usage_scope(usage_context):
                embedding_vector = await gen_openai_embeddings(
                    model=model_group.CHAT_MODEL,
                    input=text,
                    dimensions=emotion_config.EMBEDDING_DIMENSION,
                    api_key=model_group.API_KEY,
                    base_url=model_group.BASE_URL,
                    timeout=emotion_config.EMBEDDING_REQUEST_TIMEOUT,
                )

            logger.debug(
                f"生成嵌入向量: {text[:10]}... 向量维度: {len(embedding_vector)}",
            )

            # 验证维度是否一致
            _validate_dimension(embedding_vector)

        except ValueError:
            # 维度错误不需要重试，直接抛出
            raise
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.TimeoutException) as e:
            # 超时异常，提供更详细的错误信息
            last_exception = e
            timeout_seconds = emotion_config.EMBEDDING_REQUEST_TIMEOUT
            error_msg = f"请求超时（超时时间: {timeout_seconds}秒）"
            if attempt < max_retries - 1:
                # 指数退避：1秒、2秒、4秒
                wait_time = 2**attempt
                logger.warning(
                    f"生成嵌入向量超时 (尝试 {attempt + 1}/{max_retries}): {error_msg}，{wait_time}秒后重试...",
                )
                await asyncio.sleep(wait_time)
            else:
                logger.error(
                    f"生成嵌入向量超时，已达最大重试次数 ({max_retries}): {error_msg}",
                )
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                # 指数退避：1秒、2秒、4秒
                wait_time = 2**attempt
                logger.warning(
                    f"生成嵌入向量失败 (尝试 {attempt + 1}/{max_retries}): {e}，{wait_time}秒后重试...",
                )
                await asyncio.sleep(wait_time)
            else:
                logger.exception(
                    f"生成嵌入向量失败，已达最大重试次数 ({max_retries}): {e}",
                )
        else:
            # 成功生成并验证通过，返回结果
            return embedding_vector

    # 所有重试都失败
    timeout_seconds = emotion_config.EMBEDDING_REQUEST_TIMEOUT
    if isinstance(
        last_exception,
        (httpx.ReadTimeout, httpx.WriteTimeout, httpx.TimeoutException),
    ):
        error_msg = (
            f"生成嵌入向量失败，已重试 {max_retries} 次。"
            f"请求超时（超时时间: {timeout_seconds}秒）。"
            f"请检查网络连接或增加配置中的 EMBEDDING_REQUEST_TIMEOUT 值。"
        )
    else:
        error_msg = f"生成嵌入向量失败，已重试 {max_retries} 次: {last_exception}"
    raise Exception(error_msg) from last_exception


def _embedding_text(description: str, tags: List[str]) -> str:
    """一条表情的检索文本：描述加标签，与改造前同一形状

    搜索、重建索引、收集、更新都从这里取，四处口径一致才不会出现「收集时进得去、
    重建后搜不到」这种只有翻库才看得出的偏差。
    """
    return f"{description} {' '.join(str(tag) for tag in tags)}".strip()


async def _safe_embedding(text: str) -> Optional[List[float]]:
    """算嵌入，失败返回 None 并记 WARNING

    嵌入是一次外部模型调用，失败不该把已经落库的行、已经写好的文件回滚掉：
    调用方把「暂时搜不到」如实告诉模型，下次重建索引就补上了。
    """
    try:
        return await generate_embedding(text)
    except Exception as e:
        logger.warning(f"生成表情嵌入向量失败，本次跳过向量写入: error={e}")
        return None


# 远程图片被下载守卫拒绝时交给模型的话：不带原因细节（可能含解析出的内网地址）
_REMOTE_IMAGE_REFUSED = "Error: 不能从这个地址获取图片（地址不可访问、指向内网或保留地址，或图片超过 10 MB）"


async def _read_image_bytes(_ctx: schemas.AgentCtx, source_path: str) -> bytes:
    """读一张图的内容：URL 就下载，沙盒路径就映射回宿主再读

    路径走 `_ctx.fs.get_file`：它只认共享目录与上传目录，并且会再校验一次拼接结果没有
    借 `..` 逃出去。表情来源是模型给的字符串，不能直接 `open()` 了事。
    """
    if source_path.startswith(("http://", "https://")):
        # 链接是模型给的：不能在主进程里直接 GET。走与上传下载同一套守卫——按解析出的 IP 拒回环 /
        # 私有 / 链路本地地址、只连校验过的地址、跳转逐跳复检、只收未压缩正文、边读边计数（上限与控制台上传相同）。
        # 失败的原因只进日志，交给模型的是同一句不含细节的话：被拒的原因可能带解析出的内网地址，
        # 网络错误（连接被拒 / 超时 / 对端核对没过）分得开的话，模型就能拿收集表情探测 NA 所在的内网
        try:
            return await fetch_url_bytes(
                source_path,
                max_bytes=_MAX_UPLOAD_BYTES,
                timeout=float(emotion_config.IMAGE_DOWNLOAD_TIMEOUT),
            )
        except UnsafeDownloadUrlError as e:
            logger.warning(f"拒绝下载表情图片: {e}")
            raise ValueError(_REMOTE_IMAGE_REFUSED) from None
        except Exception as e:  # noqa: BLE001 - 网络错误一律归成同一句
            logger.warning(f"下载表情图片失败: {type(e).__name__}: {e}")
            raise ValueError(_REMOTE_IMAGE_REFUSED) from None

    host_path = _ctx.fs.get_file(source_path)
    # 共享目录在沙盒里可写：判断与读取都不跟随链接，否则判完换成链接就能把宿主文件收进表情库
    from nekro_agent.tools.sandbox_shared_files import host_file_is_regular, read_host_file_bytes

    if not await asyncio.to_thread(host_file_is_regular, Path(host_path)):
        raise ValueError(f"Error: 图片文件不存在: {source_path}")
    return await asyncio.to_thread(read_host_file_bytes, Path(host_path), max_bytes=_MAX_UPLOAD_BYTES)


async def _search_readable_items(
    scope: emotion_library.EmotionScope,
    query: str,
    *,
    limit: int,
) -> List[DBEmotionItem]:
    """在可读集合里做一次语义搜索，返回最多 `limit` 条库表行

    向量搜索只用来缩候选集：载荷里的 `set_id` 是可能过时的（移动失败、手改、历史点没有这个
    字段），所以每个命中都再按库表与本次解析出的可读集合复核一遍，对不上的直接丢掉。
    多取三倍是为了给这一层复核留出被丢的余量。
    """
    if limit <= 0 or not scope.readable_set_ids:
        return []
    embedding = await generate_embedding(query)
    hits = await emotion_library.vector_search(
        embedding,
        set_ids=scope.readable_set_ids,
        limit=limit * 3,
    )
    found: List[DBEmotionItem] = []
    seen: Set[str] = set()
    for emotion_id, _score in hits:
        if emotion_id in seen:
            continue
        seen.add(emotion_id)
        item = await emotion_library.get_readable_item(scope, emotion_id)
        if item is None:
            continue
        found.append(item)
        if len(found) >= limit:
            break
    return found


def _emotion_file_of(item: DBEmotionItem) -> Optional[Path]:
    """库表行 → 磁盘上真实存在的文件；没有就返回 None"""
    path = emotion_library.resolve_emotion_file(item.file_path)
    if path is None or not path.is_file():
        logger.warning(f"表情文件不存在: emotion_id={item.emotion_id}, file_path={item.file_path}")
        return None
    return path


# endregion: 表情包工具方法


# region: 表情包命令


async def _command_scope(chat_key: str) -> emotion_library.EmotionScope:
    """命令入口的作用域：先按频道找实例，再用**那个实例**的配置解析

    命令上下文只有 `chat_key`，没有实例上下文变量（适配器入站时才写），所以这里显式
    套一层 `instance_scope`：否则读到的永远是全局配置，「按实例覆盖的套组」在命令里不生效。
    频道解析不出实例时传空串（读全局配置），是否可用仍由归属解析判（多租户下会 fail-closed）。
    """
    instance_key = await resolve_chat_plugin_instance(chat_key)
    with instance_scope(instance_key or ""):
        enabled_sets, persona_set_rules = _scope_options()
    return await emotion_library.resolve_emotion_scope_for_chat(
        chat_key,
        enabled_sets=enabled_sets,
        persona_set_rules=persona_set_rules,
    )


@plugin.mount_command(
    name="emo_search",
    description="语义搜索表情包",
    aliases=[],
    usage="emo_search <关键词>",
    permission=CommandPermission.SUPER_USER,
    category="表情包",
)
async def emo_search_cmd(
    context: CommandExecutionContext,
    keyword: Annotated[str, Arg("搜索关键词", positional=True, greedy=True)] = "",
) -> CommandResponse:
    scope = await _command_scope(context.chat_key)
    if not scope.readable_set_ids:
        return CmdCtl.failed("表情库在当前频道不可用")

    if not keyword:
        return CmdCtl.failed("请输入要搜索的关键词")

    try:
        items = await _search_readable_items(scope, keyword, limit=max(1, emotion_config.MAX_SEARCH_RESULTS))
    except Exception as e:
        logger.exception(f"搜索表情包失败: {e}")
        return CmdCtl.failed(f"搜索失败: {e!s}")

    if not items:
        return CmdCtl.failed(f"没有找到和「{keyword}」相关的表情包")

    output_segments: List[CommandOutputSegment] = []
    for index, item in enumerate(items, 1):
        file_path = _emotion_file_of(item)
        if file_path is None:
            continue
        tags_str = "、".join(str(tag) for tag in (item.tags or [])) or "暂无标签"
        output_segments.append(
            CommandOutputSegment(
                type=CommandOutputSegmentType.TEXT,
                text=f"{index}. ID: {item.emotion_id}\n描述: {sanitize_inline_text(item.description or '', 60)}\n标签: {sanitize_inline_text(tags_str, 80)}",
            ),
        )
        output_segments.append(
            CommandOutputSegment(
                type=CommandOutputSegmentType.IMAGE,
                file_path=str(file_path),
            ),
        )

    if not output_segments:
        return CmdCtl.failed(f"没有找到和「{keyword}」相关的可用表情包")

    return CmdCtl.success([
        CommandOutputSegment(
            type=CommandOutputSegmentType.TEXT,
            text=f"和「{keyword}」相关的表情包，共 {len(output_segments) // 2} 个",
        ),
        *output_segments,
    ])


@plugin.mount_command(
    name="emo_stats",
    description="查看表情包统计信息",
    aliases=[],
    permission=CommandPermission.SUPER_USER,
    category="表情包",
)
async def emo_stats_cmd(context: CommandExecutionContext) -> CommandResponse:
    scope = await _command_scope(context.chat_key)
    if not scope.readable_set_ids:
        return CmdCtl.failed("表情库在当前频道不可用")

    try:
        counts = await emotion_library.count_items_by_set(scope.readable_set_ids)
        total_count = sum(counts.values())
        sets = await DBEmotionSet.filter(id__in=sorted(scope.readable_set_ids)).order_by("sort", "id")
        all_tags: Set[str] = set()
        for item in await DBEmotionItem.filter(set_id__in=sorted(scope.readable_set_ids)):
            all_tags.update(str(tag) for tag in (item.tags or []))

        vector_count: Any = 0
        client = await get_qdrant_client()
        if client is not None:
            try:
                info = await client.get_collection(emotion_library.EMOTION_VECTOR_COLLECTION)
                vector_count = getattr(info, "points_count", 0) or 0
            except Exception as e:
                logger.warning(f"读取表情向量集合信息失败: {e}")

        sorted_tags = sorted(all_tags)[:32]
        tags_str = "、".join(sanitize_inline_text(tag, 24) for tag in sorted_tags) if sorted_tags else "暂无标签"
        set_lines = "\n".join(
            f"- {sanitize_inline_text(row.name, 40)}（ID {row.id}，{counts.get(row.id, 0)} 个）" for row in sets
        )
        return CmdCtl.success(
            f"当前表情包统计信息：\n可读套组：{len(sets)} 个\n表情总数：{total_count} 个\n"
            f"向量数量：{vector_count} 个\n标签集合（top 32）：{tags_str}\n{set_lines}",
        )
    except Exception as e:
        logger.error(f"统计表情包失败: {e}")
        return CmdCtl.failed(f"统计失败: {e!s}")


def _parse_emo_list_page(args_str: str) -> int:
    """解析 emo_list 的页码。

    兼容旧版 `-i/--image` 预览参数，但现在默认就会带图片。
    """
    page = 1

    for token in args_str.split():
        if token in {"-i", "--image", "--images", "--preview"}:
            continue
        if token.lstrip("-").isdigit():
            page = max(1, int(token))
            continue
        raise ValueError(f"不支持的参数: {token}")

    return page


@plugin.mount_command(
    name="emo_list",
    description="分页列出本频道可读的表情包",
    aliases=["emo_ls"],
    usage="emo_list [页码]",
    permission=CommandPermission.SUPER_USER,
    category="表情包",
)
async def emo_list_cmd(
    context: CommandExecutionContext,
    args_str: Annotated[str, Arg("参数", positional=True, greedy=True)] = "",
) -> CommandResponse:
    scope = await _command_scope(context.chat_key)
    if not scope.readable_set_ids:
        return CmdCtl.failed("表情库在当前频道不可用")

    try:
        page = _parse_emo_list_page(args_str)
    except ValueError as e:
        return CmdCtl.failed(str(e))

    page_size = 10
    total_count, page_items = await emotion_library.list_items(
        scope.readable_set_ids,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    if total_count == 0:
        return CmdCtl.success("当前还没有收藏任何表情包")

    total_pages = (total_count + page_size - 1) // page_size
    if page > total_pages:
        return CmdCtl.failed(f"当前只有 {total_pages} 页，请输入有效的页码")

    output_segments: List[CommandOutputSegment] = []
    for item in page_items:
        file_path = _emotion_file_of(item)
        if file_path is None:
            continue
        tags = [str(tag) for tag in (item.tags or [])]
        tags_str = "、".join(tags[:3]) + ("..." if len(tags) > 3 else "")
        output_segments.append(
            CommandOutputSegment(
                type=CommandOutputSegmentType.TEXT,
                text=f"ID: {item.emotion_id}\n描述: {sanitize_inline_text(item.description or '', 30)}...\n标签: {sanitize_inline_text(tags_str, 60)}",
            ),
        )
        output_segments.append(
            CommandOutputSegment(
                type=CommandOutputSegmentType.IMAGE,
                file_path=str(file_path),
            ),
        )

    if not output_segments:
        return CmdCtl.failed(f"第 {page}/{total_pages} 页没有可预览的表情包图片")

    return CmdCtl.success([
        CommandOutputSegment(
            type=CommandOutputSegmentType.TEXT,
            text=f"第 {page}/{total_pages} 页表情包预览，共 {len(output_segments) // 2} 个，使用 emo_list <页码> 查看其他页面",
        ),
        *output_segments,
    ])


async def _migrate_legacy_file_paths() -> int:
    """把存量套组里还记着绝对路径的行改成文件名，返回改了几行

    只动存量套组：预设套组与收集库里的 `file_path` 都是本插件自己写的内容寻址路径，绝不
    可能是迁移前的宿主绝对路径，跟着一起改只会把一个合法值改坏。
    """
    legacy = await DBEmotionSet.filter(kind=emotion_library.KIND_LEGACY).order_by("id").first()
    if legacy is None:
        return 0
    migrated = 0
    for item in await DBEmotionItem.filter(set_id=legacy.id).order_by("id"):
        raw = Path(item.file_path or "")
        if not (raw.anchor or raw.drive):
            continue
        item.file_path = raw.name
        await item.save()
        migrated += 1
    return migrated


@plugin.mount_command(
    name="emo_migrate",
    description="迁移存量表情包的绝对路径",
    aliases=[],
    permission=CommandPermission.SUPER_USER,
    category="表情包",
)
async def emo_migrate_cmd(context: CommandExecutionContext) -> CommandResponse:
    del context  # 迁移只看存量套组，与频道无关
    migrated_count = await _migrate_legacy_file_paths()
    return CmdCtl.success(f"路径迁移完成！成功迁移 {migrated_count} 个表情包路径")


@plugin.mount_command(
    name="emo_reindex",
    description="重建表情包索引",
    aliases=[],
    usage="emo_reindex -y",
    permission=CommandPermission.SUPER_USER,
    category="表情包",
)
async def emo_reindex_cmd(
    context: CommandExecutionContext,
    args_str: Annotated[str, Arg("参数", positional=True, greedy=True)] = "",
) -> AsyncIterator[CommandResponse]:
    del context  # 重建是全集合维护，与命令所在频道无关
    if "-y" not in args_str:
        yield CmdCtl.failed("请输入 -y 确认重建表情包索引")
        return

    rows = await DBEmotionItem.all().order_by("id")
    if not rows:
        yield CmdCtl.success("当前没有任何表情包需要重建索引")
        return

    yield CmdCtl.message(f"开始重建 {len(rows)} 个表情包的索引，这可能需要一些时间...")

    client = await get_qdrant_client()
    if client is None:
        yield CmdCtl.failed("无法连接到向量数据库，重建索引失败")
        return

    collection_name = emotion_library.EMOTION_VECTOR_COLLECTION
    try:
        collections = await client.get_collections()
        if collection_name in {collection.name for collection in collections.collections}:
            await client.delete_collection(collection_name=collection_name)
            logger.info(f"已删除现有集合: {collection_name}")

        await client.create_collection(
            collection_name=collection_name,
            vectors_config=qdrant_models.VectorParams(
                size=emotion_config.EMBEDDING_DIMENSION,
                distance=qdrant_models.Distance.COSINE,
            ),
        )
        logger.info(f"已创建新集合: {collection_name}")
    except Exception as e:
        logger.error(f"重置向量集合失败: {e}")
        yield CmdCtl.failed(f"重置向量集合失败: {e!s}")
        return

    success_count = 0
    error_count = 0
    missing_file_count = 0
    for item in rows:
        try:
            if _emotion_file_of(item) is None:
                missing_file_count += 1
                continue
            embedding = await generate_embedding(_embedding_text(item.description or "", list(item.tags or [])))
            await emotion_library.vector_upsert(
                item.emotion_id,
                embedding,
                set_id=item.set_id,
                description=item.description or "",
                tags=list(item.tags or []),
            )
            success_count += 1
        except Exception as e:
            logger.error(f"处理表情包失败: {item.emotion_id}, 错误: {e}")
            error_count += 1

    # 输出只有条数：模型拿不到任何一条描述或文件名，就没法把它们编进别的话里
    result = (
        f"表情包索引重建完成！\n总计: {len(rows)} 个\n成功: {success_count} 个\n"
        f"失败: {error_count} 个\n文件缺失: {missing_file_count} 个"
    )
    yield CmdCtl.success(result)


# endregion: 表情包命令


# region: 表情包提示注入


# 提示词用英文：与插件其余提示词同一语言，模型对它的指令遵循更好
_RECENT_HEADER = "Recent Emotions:"
_NO_RECENT_WITH_COLLECT = (
    "Recent Emotions: No emotions added yet. You can use `collect_emotion` to add some. "
    "(You should not send 'ID' in your message directly.)"
)
_NO_RECENT_WITHOUT_COLLECT = (
    "Recent Emotions: No emotions added yet. (You should not send 'ID' in your message directly.)"
)
_COLLECT_GUIDANCE = (
    "Attention: Emotion Plugin is a isolated self-managed plugin, you should not record the emotion ID manually "
    "by using other plugins. Just use the `collect_emotion` tool to collect the emotion and use `search_emotion` "
    "to search the emotion if you need."
)
_SEARCH_ONLY_GUIDANCE = (
    "Attention: Emotion Plugin is a isolated self-managed plugin, you should not record the emotion ID manually "
    "by using other plugins. Just use `search_emotion` to search the emotion if you need."
)
_MAX_SUMMARY_SETS = 10


@plugin.mount_prompt_inject_method("emotion_prompt_inject")
async def emotion_prompt_inject(_ctx: schemas.AgentCtx) -> str:
    """表情包提示注入

    只列**本实例收集库**的最近几条：别的套组（平台方预设、别的实例的收集库）里的描述是别人
    写的，进了系统提示词就等于给群友开了一条往提示词里塞字的通道。套组清单只给名字、分类与
    条数，且同样过清洗。
    """
    enabled_sets, persona_set_rules = _scope_options()
    scope = await emotion_library.resolve_emotion_scope(
        _ctx,
        enabled_sets=enabled_sets,
        persona_set_rules=persona_set_rules,
    )
    if not scope.readable_set_ids:
        return ""

    collect_enabled = bool(emotion_config.AUTO_COLLECT_ENABLED)
    parts: List[str] = []

    recent = (
        await emotion_library.recent_items(scope.collect_set_id, max(0, emotion_config.MAX_RECENT_EMOTION_COUNT))
        if scope.collect_set_id is not None
        else []
    )
    if recent:
        parts.append(_RECENT_HEADER)
        for index, item in enumerate(recent, 1):
            # 描述是多行文本：先按多行清洗（剥控制字符、压空行），再把换行折成空格。
            # 折行是必须的——一条描述里带一个换行，后面就能凭空接出一行看不出边界的条目。
            description = sanitize_block_text(item.description or "", 30).replace("\n", " ")
            tags = [sanitize_inline_text(str(tag), 16) for tag in (item.tags or [])[:3]]
            tags_str = ", ".join(tags) + ("..." if len(item.tags or []) > 3 else "")
            parts.append(f"{index}. ID: {item.emotion_id} - {description}... [Tags: {tags_str}]")
    else:
        parts.append(_NO_RECENT_WITH_COLLECT if collect_enabled else _NO_RECENT_WITHOUT_COLLECT)

    if emotion_config.INJECT_SET_SUMMARY:
        sets = await DBEmotionSet.filter(id__in=sorted(scope.readable_set_ids)).order_by("sort", "id")
        counts = await emotion_library.count_items_by_set(scope.readable_set_ids)
        if sets:
            parts.append("Readable Emotion Sets (use `search_emotion` to search inside them):")
            for row in sets[:_MAX_SUMMARY_SETS]:
                name = sanitize_inline_text(row.name or "", 40)
                category = sanitize_inline_text(row.category or "", 20) or "-"
                kind = "mine" if row.id == scope.collect_set_id else "read-only"
                parts.append(f"- {name} [category: {category}, items: {counts.get(row.id, 0)}, {kind}]")

    parts.append(_COLLECT_GUIDANCE if collect_enabled else _SEARCH_ONLY_GUIDANCE)
    return "\n".join(parts)


# endregion: 表情包提示注入


# region: 表情包沙盒方法


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="收集表情包",
    description="收集表情包并进行特征打标，保存到向量数据库",
)
async def collect_emotion(
    _ctx: schemas.AgentCtx,
    source_path: str,
    description: str,
    tags: List[str],
    vision_and_emotion_confirmed: bool,
) -> str:
    """Collect Emotion (表情包)

    Collect an expression image/GIF to the emotion database.
    **IMPORTANT:** Only collect actual expression images or reaction GIFs, **DO NOT COLLECT ANY screenshots, photos, or other images!** You can only collect the images you are sure about (visible in vision content). Do not collect anything send by yourself!

    Args:
        source_path (str): The path or URL of the expression image
        description (str): A detailed description of the emotion/expression (used for searching. You Must fill it in carefully)
        tags (List[str]): Tags for the emotion (e.g. ["happy", "excited", "anime"])
        vision_and_emotion_confirmed (bool): Whether the image content is confirmed by vision (default: False, You Can Only Collect Images That Are Visible in Vision Content and NEVER Guess the content!)

    Returns:
        str: The emotion ID

    Example:
        ```python
        # Collect from URL
        emotion_id = collect_emotion("https://example.com/happy_cat.gif", "一只开心的猫跳来跳去", ["开心", "猫", "可爱", "Q版"], vision_and_emotion_confirmed=True)

        # Collect from local path
        emotion_id = collect_emotion("/app/uploads/surprised_anime_girl.png", "蓝色头发的动漫女孩一脸惊讶", ["动漫", "惊讶", "反应"], vision_and_emotion_confirmed=True) # Do not send the emotion ID in your message directly!

        # DO NOT COLLECT ANY screenshots, photos, or other images!
        emotion_id = collect_emotion("/app/uploads/user_screenshot.png", "用户截图", ["截图", "用户", "游戏"], vision_and_emotion_confirmed=False)  # Rejected.
        ```
    """
    enabled_sets, persona_set_rules = _scope_options()
    scope = await emotion_library.resolve_emotion_scope(
        _ctx,
        enabled_sets=enabled_sets,
        persona_set_rules=persona_set_rules,
    )

    if not emotion_config.AUTO_COLLECT_ENABLED:
        raise ValueError("Error: 本实例没有开启自动收集")

    if scope.collect_set_id is None:
        raise ValueError("Error: 表情库在当前频道不可用")

    # 参数验证
    if not source_path:
        raise ValueError("Error: Source path cannot be empty!")

    if not description:
        raise ValueError("Error: Description cannot be empty!")

    if tags is None:
        tags = []
    if not isinstance(tags, list):
        raise TypeError("Error: Tags must be a list!")
    tags = [str(tag) for tag in tags]

    if not vision_and_emotion_confirmed:
        await _ctx.push_system(
            "Image content is not confirmed by vision or it is a screenshot! You CANT COLLECT ANY screenshots, photos, or other images! Rejected.",
        )
        return ""

    data = await _read_image_bytes(_ctx, source_path)
    extension = emotion_library.detect_image_extension(data)
    if extension is None:
        raise ValueError("Error: 不是支持的图片格式")

    digest = emotion_library.sha256_of_bytes(data)
    duplicate = await emotion_library.find_readable_by_sha256(scope, digest)
    if duplicate is not None:
        # 同一张图已经在可读集合里：直接把它交回去，不改它的描述、标签与文件。改造前这里会
        # 覆盖那条记录，于是别人收集的表情被当前会话的描述改写。
        await _ctx.push_system(f"这张图已经在表情库里：{echo_inline(duplicate.emotion_id)}")
        return duplicate.emotion_id

    # 写文件与建行分开：文件按内容哈希落盘（同一张图只有一份），行的归属由 scope 决定
    relative_path, file_sha256 = await asyncio.to_thread(
        emotion_library.store_bytes_by_hash,
        data,
        extension,
    )
    item = await emotion_library.create_item(
        set_id=scope.collect_set_id,
        description=description,
        tags=tags,
        file_path=relative_path,
        file_sha256=file_sha256,
        source_path=source_path,
        collected_chat_key=_ctx.from_chat_key,
    )
    emotion_id = item.emotion_id

    embedding = await _safe_embedding(_embedding_text(description, tags))
    if embedding is None:
        # 行与文件都留着：下次重建索引会补上向量，现在只是暂时搜不到
        await _ctx.push_system(
            f"表情已收集但暂时搜不到：{echo_inline(emotion_id)}（向量写入失败，稍后会补上）",
        )
        return emotion_id

    await emotion_library.vector_upsert(
        emotion_id,
        embedding,
        set_id=scope.collect_set_id,
        description=description,
        tags=tags,
    )
    # 这句话会落成 SYSTEM 消息：描述、标签、来源路径都是模型传入的，压成单行、加引号再回显
    await _ctx.push_system(
        f"Successfully collected emotion: {emotion_id} - description={echo_inline(description)} "
        f"tags={echo_inline(' '.join(tags))} source_path={echo_inline(source_path)}",
    )

    return emotion_id


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="更新表情包",
    description="更新表情包的描述和标签",
)
async def update_emotion(
    _ctx: schemas.AgentCtx,
    emotion_id: str,
    description: str,
    tags: List[str],
) -> str:
    """Update Emotion (更新表情包)

    Update the metadata of an existing emotion.

    Args:
        emotion_id (str): The ID of the emotion to update
        description (str): New description for the emotion
        tags (List[str]): New tags for the emotion

    Returns:
        str: The emotion ID of the updated emotion

    Example:
        ```python
        # Update emotion metadata
        updated_id = update_emotion("a1b2c3d4", description="一只超可爱的猫猫", tags=["可爱", "猫咪", "萌"])
        ```
    """
    enabled_sets, persona_set_rules = _scope_options()
    scope = await emotion_library.resolve_emotion_scope(
        _ctx,
        enabled_sets=enabled_sets,
        persona_set_rules=persona_set_rules,
    )

    if not emotion_id:
        raise ValueError("Error: Emotion ID cannot be empty!")

    if not isinstance(tags, list):
        raise TypeError("Error: Tags must be a list!")

    item, error = await emotion_library.get_writable_item(scope, emotion_id)
    if item is None:
        # 「不存在」与「越权」逐字相同；只有「看得见但不可写」才换成平台方只读那句
        raise ValueError(error)

    tags = [str(tag) for tag in tags]
    await emotion_library.update_item(item, description=description, tags=tags)

    embedding = await _safe_embedding(_embedding_text(description, tags))
    if embedding is not None:
        await emotion_library.vector_upsert(
            emotion_id,
            embedding,
            set_id=item.set_id,
            description=description,
            tags=tags,
        )

    await _ctx.push_system(f"Successfully updated emotion metadata: {echo_inline(emotion_id)}")
    return emotion_id


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="删除表情包",
    description="删除表情包，并从向量数据库中移除",
)
async def remove_emotion(_ctx: schemas.AgentCtx, emotion_id: str) -> str:
    """Remove Emotion (删除表情包)

    Remove an emotion from the database by its ID.

    Args:
        emotion_id (str): The ID of the emotion to be deleted

    Returns:
        str: A message indicating the success or failure of the operation

    Example:
        ```python
        # Remove an emotion
        result = remove_emotion("a1b2c3d4")
        ```
    """
    enabled_sets, persona_set_rules = _scope_options()
    scope = await emotion_library.resolve_emotion_scope(
        _ctx,
        enabled_sets=enabled_sets,
        persona_set_rules=persona_set_rules,
    )

    if not emotion_id:
        raise ValueError("Error: Emotion ID cannot be empty!")

    item, error = await emotion_library.get_writable_item(scope, emotion_id)
    if item is None:
        raise ValueError(error)

    await emotion_library.delete_item(item)
    await _ctx.push_system(f"Successfully removed emotion: {echo_inline(emotion_id)}")
    return f"表情包 {emotion_id} 已成功删除"


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="获取表情包路径",
    description="获取表情包文件路径",
)
async def get_emotion_path(_ctx: schemas.AgentCtx, emotion_id: str) -> str:
    """Get Emotion Path

    Get the path of an emotion by its ID.

    Args:
        emotion_id (str): The emotion ID (**NOT PATH**)

    Returns:
        str: The path of the emotion file

    Example:
        ```python
        # Get emotion path and use it in another function
        emotion_file_path = get_emotion_path("a1b2c3d4")
        # Send it or do something ... But DO NOT do any addition record for this emotion file!
        ```
    """
    enabled_sets, persona_set_rules = _scope_options()
    scope = await emotion_library.resolve_emotion_scope(
        _ctx,
        enabled_sets=enabled_sets,
        persona_set_rules=persona_set_rules,
    )

    if not emotion_id:
        raise ValueError("Error: Emotion ID cannot be empty!")

    item = await emotion_library.get_readable_item(scope, emotion_id)
    if item is None:
        raise ValueError(emotion_library.emotion_not_found_message(emotion_id))

    file_path = _emotion_file_of(item)
    if file_path is None:
        raise ValueError("Error: Emotion file not found")

    try:
        # 复制进这个频道的上传目录再交回沙盒路径：表情文件在插件数据目录里，沙盒看不到它，
        # 而且不能把宿主路径直接告诉模型
        copied_path, _ = await copy_to_upload_dir(
            str(file_path),
            file_path.name,
            from_chat_key=_ctx.fs.chat_key,
        )
    except Exception as e:
        raise ValueError(f"Error reading emotion file: {e}") from e
    return str(convert_filename_to_sandbox_upload_path(Path(copied_path)))


@plugin.mount_sandbox_method(
    SandboxMethodType.MULTIMODAL_AGENT,
    name="搜索表情包",
    description="根据文本描述搜索表情包",
)
async def search_emotion(
    _ctx: schemas.AgentCtx,
    query: str,
    max_results: Optional[int] = None,
) -> Dict[str, Any]:
    """Search Emotion

    Search for emotions using a text query and display the results.

    Args:
        query (str): The search query
        max_results (int, optional): Maximum number of results to observe (recommended: 3-5)

    Returns:
        Dict: OpenAI chat message format containing the search results

    Example:
        ```python
        # Search for happy cat emotions
        search_results = search_emotion("开心猫")
        ```
    """
    enabled_sets, persona_set_rules = _scope_options()
    scope = await emotion_library.resolve_emotion_scope(
        _ctx,
        enabled_sets=enabled_sets,
        persona_set_rules=persona_set_rules,
    )

    if not query:
        raise ValueError("Error: Search query cannot be empty!")

    if not scope.readable_set_ids:
        return OpenAIChatMessage.from_text(
            "user",
            f"No emotions found for query: '{query}'. Try another search term or collect more emotions.",
        ).to_dict()

    search_limit = emotion_config.MAX_SEARCH_RESULTS
    if max_results is not None:
        search_limit = max(1, min(int(max_results), emotion_config.MAX_SEARCH_RESULTS))

    try:
        items = await _search_readable_items(scope, query, limit=search_limit)
    except Exception as e:
        logger.error(f"向量搜索失败: {e}")
        return OpenAIChatMessage.from_text(
            "user",
            f"Failed to search emotions: {e}. Please try again later or check your query.",
        ).to_dict()

    if not items:
        return OpenAIChatMessage.from_text(
            "user",
            f"No emotions found for query: '{query}'. Try another search term or collect more emotions.",
        ).to_dict()

    msg = OpenAIChatMessage.from_text(
        "user",
        f"Here are the emotions You can use for '{query}':",
    )

    has_writable = False
    for index, item in enumerate(items):
        file_path = _emotion_file_of(item)
        if file_path is None:
            continue
        writable = item.set_id in scope.writable_set_ids
        has_writable = has_writable or writable
        tags_str = ", ".join(str(tag) for tag in (item.tags or [])) or "No tags"
        note = "" if writable else PLATFORM_READONLY_NOTE
        msg.batch_add(
            [
                ContentSegment.text_content(
                    f"\nEmotion {index + 1} (ID: {item.emotion_id}){note}:\n"
                    f"Description: {sanitize_block_text(item.description or '', 300)}\nTags: {sanitize_inline_text(tags_str, 200)}",
                ),
                ContentSegment.image_content_from_path(str(file_path)),
            ],
        )

    if has_writable:
        # 只对收集库里的说这句：平台方套组本来就只能由平台方在控制台改，让模型去 update 它
        # 只会换来一句「只能使用不能修改」，还占掉一轮
        msg.add(
            ContentSegment.text_content(
                "If an emotion from your own collection doesn't match its description, please use `update_emotion` "
                "IMMEDIATELY to correct it. If you find any non-expression images (like screenshots, photos, or "
                "irrelevant content) in your own collection, please use `remove_emotion` IMMEDIATELY to delete them. "
                "Expression images should only be reaction GIFs or meme images. ALWAYS KEEP YOUR COLLECTIONS CLEAN AND "
                "ACCURATE. After that, continue to Your task.",
            ),
        )

    return msg.to_dict()


# endregion: 表情包沙盒方法


# region: 控制台接口


_SET_NOT_FOUND = "表情套组"
_ITEM_NOT_FOUND = "表情"
_MAX_PAGE_LIMIT = 100
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_MAX_DESCRIPTION_CHARS = 500
_MAX_TAGS = 20
_MAX_TAG_CHARS = 32
_IMAGE_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


class SetCreateRequest(BaseModel):
    """建预设套组"""

    name: str = Field(min_length=1, max_length=64)
    category: str = Field(default="", max_length=32)
    description: str = Field(default="", max_length=_MAX_DESCRIPTION_CHARS)


class SetUpdateRequest(BaseModel):
    """改套组的展示属性；没给的项不动"""

    name: Optional[str] = Field(default=None, max_length=64)
    category: Optional[str] = Field(default=None, max_length=32)
    description: Optional[str] = Field(default=None, max_length=_MAX_DESCRIPTION_CHARS)
    sort: Optional[int] = None


class ItemUpdateRequest(BaseModel):
    """改一条表情的描述与标签"""

    description: Optional[str] = Field(default=None, max_length=_MAX_DESCRIPTION_CHARS)
    tags: Optional[List[str]] = None


class ItemsMoveRequest(BaseModel):
    """把一批表情移到另一个套组"""

    emotion_ids: List[str] = Field(default_factory=list)
    target_set_id: int


class AssignmentRequest(BaseModel):
    """加一条分配"""

    set_id: int
    target: str
    adapter_key: str = ""
    instance_key: str = ""
    preset_id: int = 0


class PlatformReadonlyError(AppError):
    """看得见、但这条表情不归你改

    就地定义而不放进 `schemas/errors.py`：与 `tenant_perm.TenantSuspendedError` 同一个理由
    （不与并行开发的其他模块争抢同一个共享文件），全局异常处理器按 `AppError` 捕获，落在哪个
    模块不影响响应结构。文案取自服务层那一份，与沙盒方法抛的 `ValueError` 逐字相同——同一件
    事在两条通道上必须是同一句话，否则「控制台改不动」和「模型改不动」会被当成两回事。
    """

    http_status: ClassVar[int] = 403
    i18n_message: ClassVar[I18nDict] = i18n.i18n_text(
        zh_CN=emotion_library.PLATFORM_READONLY_MESSAGE,
        en_US="This emotion is provided by the platform and is read-only",
    )


def _validate_tags(tags: Optional[List[str]]) -> Optional[List[str]]:
    """标签的数量与长度上限；超了按 422 拒

    用 422 而不是领域错误：这是请求体形状问题，控制台据此把输入框标红。全局异常处理器把
    pydantic 校验错误统一映射成 400，拿不到「哪一项超了」的语义，所以这里显式抛。
    """
    if tags is None:
        return None
    if len(tags) > _MAX_TAGS:
        raise HTTPException(status_code=422, detail=f"标签最多 {_MAX_TAGS} 个")
    cleaned: List[str] = []
    for tag in tags:
        text = str(tag).strip()
        if not text:
            continue
        if len(text) > _MAX_TAG_CHARS:
            raise HTTPException(status_code=422, detail=f"单个标签最多 {_MAX_TAG_CHARS} 个字符")
        cleaned.append(text)
    return cleaned


def _set_payload(row: DBEmotionSet, count: int, writable: bool) -> Dict[str, Any]:
    """控制台里一个套组的展示字段（不含任何文件路径）"""
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "category": row.category,
        "description": row.description,
        "count": count,
        "writable": writable,
    }


async def _instance_owned_by_platform(adapter_key: str, instance_key: str) -> bool:
    """这个实例对是不是平台方的（决定「可用池」里要不要算平台方实例的分配）"""
    row = await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).first()
    return row is None or row.tenant_id is None


async def _instance_visible_set_ids(adapter_key: str, instance_key: str) -> Set[int]:
    """控制台用：这个实例的会话能读到的套组（含按人设分配的）

    与 `emotion_library.resolve_emotion_scope` 的判据同源，但这里不需要某一个具体频道：
    人设取「这个实例可见的人设」，也就是 `routers/presets.visible_presets` 的口径
    （全局共享的 + 实例自己的）。控制台是给人整理数据用的，把「本实例将来可能用到的
    人设套组」也算进来比漏掉更安全：这些套组本来就是平台方显式分给它们的。
    """
    from nekro_agent.routers.presets import visible_presets

    pool = await emotion_library.instance_pool_set_ids(
        adapter_key,
        instance_key,
        is_platform_instance=await _instance_owned_by_platform(adapter_key, instance_key),
    )
    preset_ids = [int(preset_id) for preset_id in await visible_presets(instance_key).values_list("id", flat=True)]
    if preset_ids:
        assigned = await DBEmotionSetAssignment.filter(
            target=emotion_library.TARGET_PRESET,
            preset_id__in=preset_ids,
        ).values_list("set_id", flat=True)
        pool.update(int(set_id) for set_id in assigned)
    return pool


async def _console_visible_set_ids(scope: PluginRouteScope) -> Optional[Set[int]]:
    """这次请求能看见的套组；`None` 表示「全部」（平台方的全局视图）"""
    if not scope.instance_key:
        return None
    return await _instance_visible_set_ids(scope.adapter_key, scope.instance_key)


async def _console_set_or_404(scope: PluginRouteScope, set_id: int) -> DBEmotionSet:
    """取一个本次请求看得见的套组；看不见与不存在回同一个 404"""
    visible = await _console_visible_set_ids(scope)
    row = await DBEmotionSet.get_or_none(id=set_id)
    if row is None or (visible is not None and row.id not in visible):
        raise NotFoundError(resource=f"{_SET_NOT_FOUND} '{set_id}'")
    return row


def _console_can_write_set(scope: PluginRouteScope, row: DBEmotionSet) -> bool:
    """这个用户对这套组有没有写权限

    租户只能写自己实例的收集库；平台方走控制台可以写任何套组（含别的实例的收集库，实例被
    划走后平台方要能清理）。会话侧没有这个例外，见 `resolve_emotion_scope`。
    """
    if scope.is_platform:
        return True
    return (
        row.kind == emotion_library.KIND_COLLECT
        and row.owner_adapter_key == scope.adapter_key
        and row.owner_instance_key == scope.instance_key
    )


async def _console_writable_item(scope: PluginRouteScope, emotion_id: str) -> DBEmotionItem:
    """取一条本次请求能改的表情；看不见 404，看得见但不可写 403"""
    item = await DBEmotionItem.filter(emotion_id=emotion_id).first()
    if item is None:
        raise NotFoundError(resource=f"{_ITEM_NOT_FOUND} '{emotion_id}'")
    row = await _console_set_or_404(scope, item.set_id)
    if not _console_can_write_set(scope, row):
        raise PlatformReadonlyError
    return item


def _require_platform(scope: PluginRouteScope) -> None:
    """平台方专属接口：租户一律 403，与其余后台接口同一档"""
    if not scope.is_platform:
        raise PermissionDeniedError


# 控制台页面（插件自己出的单页，不进 NA 前端工程）。
#
# 页面本身不含任何数据，匿名取得到也无妨：它在同源下读 NA 后台的登录态（`auth-storage`），
# 带 `Authorization` 头调同一路由下的 `/console/api/*`；`<img>` 带不了头，图片用短时效票据。
# 实例范围取自地址栏的 `adapter_key` / `instance_key`（后台插件详情页的链接会带上当前实例）。
# 模型写的描述、标签一律走 `textContent`，不拼进 HTML。
CONSOLE_PAGE_PATH = "/console/"

_CONSOLE_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>表情控制台</title>
<style>
:root { --bg:#f6f7fb; --panel:#fff; --text:#1f2430; --muted:#6b7280; --line:#e5e7eb; --accent:#6750a4; --accent-soft:#ece6f8; --danger:#c62828; --ok:#2e7d32; }
@media (prefers-color-scheme: dark) { :root { --bg:#14151a; --panel:#1d1f26; --text:#e7e8ee; --muted:#9aa0ad; --line:#2d303a; --accent:#c4b5fd; --accent-soft:#2c2640; --danger:#ef9a9a; --ok:#a5d6a7; } }
* { box-sizing: border-box; }
body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background:var(--bg); color:var(--text); font-size:14px; }
header { display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding:12px 16px; background:var(--panel); border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; }
header h1 { font-size:18px; margin:0; }
.chip { display:inline-block; padding:2px 8px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; }
.muted { color:var(--muted); }
button { cursor:pointer; border:1px solid var(--line); background:var(--panel); color:var(--text); border-radius:6px; padding:4px 10px; font-size:13px; }
button.primary { background:var(--accent); border-color:var(--accent); color:var(--panel); }
button.danger { color:var(--danger); border-color:var(--danger); }
button:disabled { opacity:.5; cursor:not-allowed; }
input, textarea, select { background:var(--panel); color:var(--text); border:1px solid var(--line); border-radius:6px; padding:4px 8px; font-size:13px; }
main { display:grid; grid-template-columns: 300px 1fr; gap:16px; padding:16px; }
@media (max-width: 760px) { main { grid-template-columns: 1fr; } }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; }
.set { display:block; width:100%; text-align:left; margin:4px 0; padding:8px; }
.set.active { border-color:var(--accent); background:var(--accent-soft); }
.group-title { margin:12px 0 4px; font-size:12px; color:var(--muted); }
.row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:6px 0; }
.grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap:12px; margin-top:12px; }
.card { border:1px solid var(--line); border-radius:8px; padding:8px; display:flex; flex-direction:column; gap:6px; }
.card img { width:100%; height:140px; object-fit:contain; background:var(--bg); border-radius:6px; }
.tag { font-size:11px; padding:1px 6px; border-radius:999px; border:1px solid var(--line); color:var(--muted); }
.banner { margin:12px 16px 0; padding:10px 12px; border-radius:8px; border:1px solid var(--line); background:var(--panel); display:none; }
.banner.error { border-color:var(--danger); color:var(--danger); }
h2 { font-size:16px; margin:4px 0; }
h3 { font-size:14px; margin:12px 0 4px; }
.desc { white-space:pre-wrap; word-break:break-word; }
</style>
</head>
<body>
<header>
  <h1>表情控制台</h1>
  <span id="scope-chip" class="chip"></span>
  <button id="refresh" type="button">刷新</button>
</header>
<div id="login-hint" class="banner">请先登录 NA 后台（同一个浏览器、同一个地址），再刷新这一页。</div>
<div id="error" class="banner error"></div>
<main id="main" style="display:none">
  <aside class="panel">
    <div id="platform-tools" style="display:none">
      <div id="legacy" class="muted"></div>
      <h3>新建预设套组</h3>
      <div class="row"><input id="new-name" placeholder="名称" maxlength="64"><input id="new-category" placeholder="分类（可空）" maxlength="32"></div>
      <div class="row"><input id="new-desc" placeholder="说明（可空）" maxlength="500" style="flex:1"><button id="create-set" class="primary" type="button">新建</button></div>
    </div>
    <div id="sets"></div>
  </aside>
  <section class="panel" id="detail"><p class="muted">在左边选一个套组。</p></section>
</main>
<script>
(function () {
  const PLUGIN_KEY = decodeURIComponent(window.location.pathname.split('/')[2] || '');
  const params = new URLSearchParams(window.location.search);
  const adapterKey = params.get('adapter_key') || '';
  const instanceKey = params.get('instance_key') || '';
  const API = '/plugins/' + encodeURIComponent(PLUGIN_KEY) + '/console/api';
  const PAGE_SIZE = 40;
  const KIND_LABEL = { collect: '收集库', preset: '预设套组', legacy: '存量（待整理）' };
  const state = { token: null, isPlatform: false, sets: [], selectedId: null, page: 0, keyword: '', selection: new Set(), ticket: '', ticketExpiresAt: 0 };

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(function (entry) {
      const key = entry[0], value = entry[1];
      if (value === undefined || value === null || value === false) return;
      if (key === 'text') node.textContent = String(value);
      else if (key === 'onclick') node.addEventListener('click', value);
      else if (key === 'className') node.className = value;
      else node.setAttribute(key, value === true ? '' : String(value));
    });
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }

  function readLoginToken() {
    try {
      const raw = window.localStorage.getItem('auth-storage');
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return parsed && parsed.state ? parsed.state.token : null;
    } catch (error) { return null; }
  }

  function showError(message) {
    const box = document.getElementById('error');
    box.textContent = message || '';
    box.style.display = message ? 'block' : 'none';
  }

  function scopeQuery(extra) {
    const qs = new URLSearchParams();
    if (adapterKey) qs.set('adapter_key', adapterKey);
    if (instanceKey) qs.set('instance_key', instanceKey);
    Object.entries(extra || {}).forEach(function (entry) { if (entry[1] !== undefined && entry[1] !== '') qs.set(entry[0], String(entry[1])); });
    return qs.toString();
  }

  async function api(path, options) {
    const opts = options || {};
    const headers = { Authorization: 'Bearer ' + state.token };
    let body;
    if (opts.form) body = opts.form;
    else if (opts.body !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(opts.body); }
    const response = await fetch(API + path + '?' + scopeQuery(opts.query), { method: opts.method || 'GET', headers: headers, body: body });
    if (response.status === 401) {
      document.getElementById('login-hint').style.display = 'block';
      throw new Error('登录已失效，请重新登录 NA 后台');
    }
    const data = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : '';
      throw new Error(data.message || detail || ('请求失败（HTTP ' + response.status + '）'));
    }
    return data;
  }

  async function ensureTicket() {
    const now = Date.now();
    if (state.ticket && now < state.ticketExpiresAt - 15000) return state.ticket;
    const response = await fetch('/api/plugin-route/ticket', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + state.token },
      body: JSON.stringify({ plugin_key: PLUGIN_KEY, adapter_key: adapterKey, instance_key: instanceKey }),
    });
    if (!response.ok) throw new Error('图片票据换取失败（HTTP ' + response.status + '）');
    const data = await response.json();
    state.ticket = data.ticket;
    state.ticketExpiresAt = now + (Number(data.expires_in) || 120) * 1000;
    return state.ticket;
  }

  async function run(action) {
    showError('');
    try { await action(); } catch (error) { showError(error && error.message ? error.message : String(error)); }
  }

  function selectedSet() { return state.sets.find(function (item) { return item.id === state.selectedId; }) || null; }

  async function loadSets() {
    const data = await api('/sets');
    state.sets = data.items || [];
    if (state.selectedId !== null && !selectedSet()) state.selectedId = null;
    renderSets();
    await renderDetail();
  }

  async function loadLegacy() {
    try {
      const data = await api('/legacy-progress');
      state.isPlatform = true;
      document.getElementById('platform-tools').style.display = 'block';
      document.getElementById('legacy').textContent = data.set_id
        ? '存量（待整理）还剩 ' + data.remaining + ' 条，其中 ' + data.already_in_preset + ' 条已经在预设套组里有同一张图。'
        : '没有存量套组。';
    } catch (error) {
      state.isPlatform = false;
    }
  }

  function renderSets() {
    const box = document.getElementById('sets');
    box.replaceChildren();
    if (!state.sets.length) { box.appendChild(el('p', { className: 'muted', text: '这里还没有套组。' })); return; }
    const groups = new Map();
    state.sets.forEach(function (item) {
      const key = item.kind === 'collect' ? '收集库' : (item.category || KIND_LABEL[item.kind] || item.kind);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    });
    groups.forEach(function (items, key) {
      box.appendChild(el('div', { className: 'group-title', text: key }));
      items.forEach(function (item) {
        const button = el('button', { type: 'button', className: 'set' + (item.id === state.selectedId ? ' active' : ''), onclick: function () {
          state.selectedId = item.id; state.page = 0; state.keyword = ''; state.selection.clear();
          renderSets(); run(renderDetail);
        } }, [
          el('div', { text: item.name }),
          el('div', { className: 'muted', text: (KIND_LABEL[item.kind] || item.kind) + ' · ' + item.count + ' 条 · ID ' + item.id + (item.writable ? '' : ' · 只读') }),
        ]);
        box.appendChild(button);
      });
    });
  }

  function copyText(text) {
    if (navigator.clipboard) navigator.clipboard.writeText(text).catch(function () {});
  }

  async function renderDetail() {
    const box = document.getElementById('detail');
    box.replaceChildren();
    const set = selectedSet();
    if (!set) { box.appendChild(el('p', { className: 'muted', text: '在左边选一个套组。' })); return; }
    box.appendChild(el('h2', { text: set.name }));
    box.appendChild(el('div', { className: 'row' }, [
      el('span', { className: 'chip', text: KIND_LABEL[set.kind] || set.kind }),
      el('span', { className: 'muted', text: 'ID ' + set.id }),
      el('button', { type: 'button', text: '复制 ID', onclick: function () { copyText(String(set.id)); } }),
      set.writable ? null : el('span', { className: 'muted', text: '只读：这是平台方提供的套组' }),
    ]));
    if (set.description) box.appendChild(el('p', { className: 'desc muted', text: set.description }));
    if (state.isPlatform && set.kind !== 'collect') box.appendChild(renderSetEditor(set));
    if (state.isPlatform) box.appendChild(renderAssignments(set));
    box.appendChild(await renderItems(set));
  }

  function renderSetEditor(set) {
    const name = el('input', { value: set.name, maxlength: 64, placeholder: '名称' });
    const category = el('input', { value: set.category || '', maxlength: 32, placeholder: '分类' });
    const desc = el('input', { value: set.description || '', maxlength: 500, placeholder: '说明', style: 'flex:1' });
    const file = el('input', { type: 'file', accept: 'image/*' });
    const uploadDesc = el('input', { placeholder: '图片描述', maxlength: 500, style: 'flex:1' });
    const uploadTags = el('input', { placeholder: '标签（逗号分隔）' });
    return el('div', {}, [
      el('h3', { text: '套组设置' }),
      el('div', { className: 'row' }, [name, category, desc,
        el('button', { type: 'button', text: '保存', onclick: function () { run(async function () {
          await api('/sets/' + set.id, { method: 'PATCH', body: { name: name.value, category: category.value, description: desc.value } });
          await loadSets();
        }); } }),
        el('button', { type: 'button', className: 'danger', text: '删除套组', onclick: function () {
          if (!window.confirm('删除「' + set.name + '」？里面的表情一起删除，不能恢复。')) return;
          run(async function () { await api('/sets/' + set.id, { method: 'DELETE' }); state.selectedId = null; await loadSets(); });
        } }),
      ]),
      el('h3', { text: '上传图片' }),
      el('div', { className: 'row' }, [file, uploadDesc, uploadTags,
        el('button', { type: 'button', className: 'primary', text: '上传', onclick: function () { run(async function () {
          if (!file.files || !file.files[0]) throw new Error('先选一张图片');
          const form = new FormData();
          form.append('image', file.files[0]);
          form.append('description', uploadDesc.value);
          form.append('tags', uploadTags.value);
          const result = await api('/sets/' + set.id + '/upload', { method: 'POST', form: form });
          if (result.exists) showError('这张图已经在这个套组里了（' + result.emotion_id + '）');
          await loadSets();
        }); } }),
      ]),
    ]);
  }

  function renderAssignments(set) {
    const wrap = el('div', {}, [el('h3', { text: '分配' })]);
    if (set.kind === 'collect') { wrap.appendChild(el('p', { className: 'muted', text: '收集库只归它自己的实例，不能分配。' })); return wrap; }
    const list = el('div');
    (set.assignments || []).forEach(function (item) {
      const label = item.target === 'preset'
        ? '人设 #' + item.preset_id + (item.preset_missing ? '（人设已删除，不生效）' : '')
        : '实例 ' + item.adapter_key + ':' + item.instance_key;
      list.appendChild(el('div', { className: 'row' }, [
        el('span', { text: label }),
        el('button', { type: 'button', className: 'danger', text: '移除', onclick: function () { run(async function () {
          await api('/assignments/' + item.id, { method: 'DELETE' }); await loadSets();
        }); } }),
      ]));
    });
    if (!(set.assignments || []).length) list.appendChild(el('p', { className: 'muted', text: '还没有分配：除了平台方在控制台里，谁都用不到它。' }));
    wrap.appendChild(list);
    const target = el('select', {}, [el('option', { value: 'instance', text: '分给实例' }), el('option', { value: 'preset', text: '分给人设' })]);
    const adapter = el('input', { placeholder: 'adapter_key', value: adapterKey || 'onebot_v11' });
    const instance = el('input', { placeholder: 'instance_key', value: instanceKey });
    const preset = el('input', { placeholder: '人设 ID', type: 'number' });
    wrap.appendChild(el('div', { className: 'row' }, [target, adapter, instance, preset,
      el('button', { type: 'button', text: '添加分配', onclick: function () { run(async function () {
        const body = { set_id: set.id, target: target.value };
        if (target.value === 'preset') body.preset_id = Number(preset.value) || 0;
        else { body.adapter_key = adapter.value.trim(); body.instance_key = instance.value.trim(); }
        await api('/assignments', { method: 'POST', body: body });
        await loadSets();
      }); } }),
    ]));
    return wrap;
  }

  async function renderItems(set) {
    const wrap = el('div', {}, [el('h3', { text: '表情' })]);
    const search = el('input', { placeholder: '按描述或标签搜索', value: state.keyword, style: 'flex:1' });
    const toolbar = el('div', { className: 'row' }, [search, el('button', { type: 'button', text: '搜索', onclick: function () {
      state.keyword = search.value.trim(); state.page = 0; run(renderDetail);
    } })]);
    wrap.appendChild(toolbar);
    if (state.isPlatform) {
      const targets = el('select', {}, state.sets.filter(function (item) { return item.id !== set.id; }).map(function (item) {
        return el('option', { value: item.id, text: item.name + '（ID ' + item.id + '）' });
      }));
      const count = el('span', { className: 'muted', text: '已选 ' + state.selection.size + ' 条' });
      const bulk = function (kind) { return function () { run(async function () {
        if (!state.selection.size) throw new Error('先勾选要' + (kind === 'move' ? '移动' : '复制') + '的表情');
        await api('/items/' + kind, { method: 'POST', body: { emotion_ids: Array.from(state.selection), target_set_id: Number(targets.value) } });
        state.selection.clear(); await loadSets();
      }); }; };
      wrap.appendChild(el('div', { className: 'row' }, [count, el('span', { text: '到' }), targets,
        el('button', { type: 'button', text: '移动', onclick: bulk('move') }),
        el('button', { type: 'button', text: '复制', onclick: bulk('copy') }),
      ]));
    }
    const data = await api('/items', { query: { set_id: set.id, offset: state.page * PAGE_SIZE, limit: PAGE_SIZE, keyword: state.keyword } });
    const grid = el('div', { className: 'grid' });
    let ticket = '';
    try { ticket = await ensureTicket(); } catch (error) { showError(error.message); }
    (data.items || []).forEach(function (item) { grid.appendChild(renderCard(item, set, ticket)); });
    if (!(data.items || []).length) grid.appendChild(el('p', { className: 'muted', text: state.keyword ? '没有搜到。' : '这个套组还是空的。' }));
    wrap.appendChild(grid);
    const pages = Math.max(1, Math.ceil((data.total || 0) / PAGE_SIZE));
    wrap.appendChild(el('div', { className: 'row' }, [
      el('button', { type: 'button', text: '上一页', disabled: state.page <= 0, onclick: function () { state.page -= 1; run(renderDetail); } }),
      el('span', { className: 'muted', text: '第 ' + (state.page + 1) + ' / ' + pages + ' 页，共 ' + (data.total || 0) + ' 条' }),
      el('button', { type: 'button', text: '下一页', disabled: state.page + 1 >= pages, onclick: function () { state.page += 1; run(renderDetail); } }),
    ]));
    return wrap;
  }

  function renderCard(item, set, ticket) {
    const card = el('div', { className: 'card' });
    if (state.isPlatform) {
      const box = el('input', { type: 'checkbox' });
      box.checked = state.selection.has(item.emotion_id);
      box.addEventListener('change', function () { if (box.checked) state.selection.add(item.emotion_id); else state.selection.delete(item.emotion_id); });
      card.appendChild(el('label', { className: 'muted' }, [box, el('span', { text: ' 选中' })]));
    }
    if (ticket) {
      card.appendChild(el('img', { loading: 'lazy', alt: item.description || item.emotion_id, src: API + '/items/' + encodeURIComponent(item.emotion_id) + '/image?' + scopeQuery({ ticket: ticket }) }));
    }
    const view = el('div', {}, [
      el('div', { className: 'desc', text: item.description || '（没有描述）' }),
      el('div', { className: 'row' }, (item.tags || []).map(function (tag) { return el('span', { className: 'tag', text: tag }); })),
      el('div', { className: 'muted', text: item.emotion_id }),
    ]);
    card.appendChild(view);
    if (set.writable) {
      card.appendChild(el('div', { className: 'row' }, [
        el('button', { type: 'button', text: '编辑', onclick: function () {
          const desc = el('textarea', { rows: 3, maxlength: 500, style: 'width:100%' });
          desc.value = item.description || '';
          const tags = el('input', { placeholder: '标签（逗号分隔）', style: 'width:100%' });
          tags.value = (item.tags || []).join(', ');
          view.replaceChildren(desc, tags, el('div', { className: 'row' }, [
            el('button', { type: 'button', className: 'primary', text: '保存', onclick: function () { run(async function () {
              await api('/items/' + encodeURIComponent(item.emotion_id), { method: 'PATCH', body: { description: desc.value, tags: tags.value.split(/[,，\s]+/).filter(Boolean) } });
              await renderDetail();
            }); } }),
            el('button', { type: 'button', text: '取消', onclick: function () { run(renderDetail); } }),
          ]));
        } }),
        el('button', { type: 'button', className: 'danger', text: '删除', onclick: function () {
          if (!window.confirm('删除这条表情？不能恢复。')) return;
          run(async function () { await api('/items/' + encodeURIComponent(item.emotion_id), { method: 'DELETE' }); await loadSets(); });
        } }),
      ]));
    }
    return card;
  }

  async function start() {
    state.token = readLoginToken();
    document.getElementById('scope-chip').textContent = instanceKey ? ('实例 ' + (adapterKey ? adapterKey + ':' : '') + instanceKey) : '全局视图（只有平台方能用）';
    if (!state.token) { document.getElementById('login-hint').style.display = 'block'; return; }
    document.getElementById('main').style.display = '';
    await loadLegacy();
    await loadSets();
  }

  document.getElementById('refresh').addEventListener('click', function () { run(async function () { await loadLegacy(); await loadSets(); }); });
  document.getElementById('create-set').addEventListener('click', function () { run(async function () {
    const name = document.getElementById('new-name').value.trim();
    if (!name) throw new Error('先填套组名称');
    const created = await api('/sets', { method: 'POST', body: { name: name, category: document.getElementById('new-category').value.trim(), description: document.getElementById('new-desc').value.trim() } });
    state.selectedId = created.id;
    document.getElementById('new-name').value = '';
    await loadSets();
  }); });
  run(start);
})();
</script>
</body>
</html>
"""


@plugin.mount_router()
def create_router() -> APIRouter:
    """表情控制台：页面（`/console/`）与接口（`/console/api`）

    接口：租户整理自己实例的收集库，平台方管预设套组、上传、移动 / 复制与分配。每条接口都挂了
    `plugin_route_scope`，实例范围与「插件在这个实例上是否生效」由它统一判；本文件只在它给出的
    范围内判「这条数据归谁」。页面是不含数据的 HTML 壳（`_CONSOLE_PAGE_HTML`），匿名可取，
    在 `tests/test_plugin_route_guard.py` 的白名单里写明了理由。
    """
    router = APIRouter(prefix="/console/api")

    @router.get("/sets", summary="列出可见的表情套组")
    async def list_sets(
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        visible = await _console_visible_set_ids(scope)
        rows = (
            await DBEmotionSet.all().order_by("sort", "id")
            if visible is None
            else await DBEmotionSet.filter(id__in=sorted(visible)).order_by("sort", "id")
        )
        counts = await emotion_library.count_items_by_set([row.id for row in rows])
        assignments: Dict[int, List[Dict[str, Any]]] = {}
        if scope.is_platform and rows:
            preset_ids = {int(row.preset_id) for row in await DBEmotionSetAssignment.filter(target=emotion_library.TARGET_PRESET)}
            existing = {int(preset_id) for preset_id in await DBPreset.filter(id__in=sorted(preset_ids)).values_list("id", flat=True)} if preset_ids else set()
            for row in await DBEmotionSetAssignment.filter(set_id__in=[item.id for item in rows]).order_by("id"):
                entry = {
                    "id": row.id,
                    "target": row.target,
                    "adapter_key": row.adapter_key,
                    "instance_key": row.instance_key,
                    "preset_id": row.preset_id,
                }
                if row.target == emotion_library.TARGET_PRESET:
                    entry["preset_missing"] = row.preset_id not in existing
                assignments.setdefault(row.set_id, []).append(entry)

        items = []
        for row in rows:
            payload = _set_payload(row, counts.get(row.id, 0), _console_can_write_set(scope, row))
            if scope.is_platform:
                payload["assignments"] = assignments.get(row.id, [])
            items.append(payload)
        return {"total": len(items), "items": items}

    @router.get("/items", summary="分页列出套组里的表情")
    async def list_items_api(
        set_id: int,
        offset: int = 0,
        limit: int = 50,
        keyword: str = "",
        # 平台方不选实例也要看得到表情：新建的预设套组还没分给任何实例，按实例范围看不到它
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        row = await _console_set_or_404(scope, set_id)
        total, page = await emotion_library.list_items(
            [row.id],
            offset=max(0, offset),
            limit=max(0, min(limit, _MAX_PAGE_LIMIT)),
            keyword=keyword,
        )
        return {
            "total": total,
            "set": _set_payload(row, total, _console_can_write_set(scope, row)),
            "items": [
                {
                    "emotion_id": item.emotion_id,
                    "set_id": item.set_id,
                    "description": item.description,
                    "tags": list(item.tags or []),
                    "added_at": item.added_at,
                    "updated_at": item.updated_at,
                }
                for item in page
            ],
        }

    @router.get("/items/{emotion_id}/image", summary="按 ID 取表情图片")
    async def get_item_image(
        emotion_id: str,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_ticket=True, allow_global_view=True)),
    ) -> FileResponse:
        item = await DBEmotionItem.filter(emotion_id=emotion_id).first()
        if item is None:
            raise NotFoundError(resource=f"{_ITEM_NOT_FOUND} '{emotion_id}'")
        await _console_set_or_404(scope, item.set_id)
        file_path = _emotion_file_of(item)
        if file_path is None:
            raise NotFoundError(resource=f"{_ITEM_NOT_FOUND} '{emotion_id}'")
        suffix = file_path.suffix.lower().lstrip(".")
        # 媒体类型按真实文件类型给（控制台不知道扩展名），响应里只有内容，没有路径
        media_type = _IMAGE_MEDIA_TYPES.get(suffix, "application/octet-stream")
        return FileResponse(path=str(file_path), media_type=media_type, filename=f"{emotion_id}.{suffix or 'bin'}")

    @router.patch("/items/{emotion_id}", summary="改表情的描述与标签")
    async def update_item_api(
        emotion_id: str,
        payload: ItemUpdateRequest,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        item = await _console_writable_item(scope, emotion_id)
        tags = _validate_tags(payload.tags)
        await emotion_library.update_item(item, description=payload.description, tags=tags)
        description = item.description or ""
        final_tags = list(item.tags or [])
        embedding = await _safe_embedding(_embedding_text(description, final_tags))
        if embedding is not None:
            await emotion_library.vector_upsert(
                item.emotion_id,
                embedding,
                set_id=item.set_id,
                description=description,
                tags=final_tags,
            )
        return {
            "emotion_id": item.emotion_id,
            "set_id": item.set_id,
            "description": description,
            "tags": final_tags,
        }

    @router.delete("/items/{emotion_id}", summary="删一条表情")
    async def delete_item_api(
        emotion_id: str,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        item = await _console_writable_item(scope, emotion_id)
        set_id = item.set_id
        await emotion_library.delete_item(item)
        return {"emotion_id": emotion_id, "set_id": set_id, "deleted": True}

    @router.post("/sets", summary="建预设套组")
    async def create_set_api(
        payload: SetCreateRequest,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        _require_platform(scope)
        row = await emotion_library.create_preset_set(
            name=payload.name,
            category=payload.category,
            description=payload.description,
            created_by=scope.user.username,
        )
        return _set_payload(row, 0, True)

    @router.patch("/sets/{set_id}", summary="改套组")
    async def update_set_api(
        set_id: int,
        payload: SetUpdateRequest,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        _require_platform(scope)
        try:
            row = await emotion_library.update_set(
                set_id,
                name=payload.name,
                category=payload.category,
                description=payload.description,
                sort=payload.sort,
                updated_by=scope.user.username,
            )
        except ValueError as e:
            raise NotFoundError(resource=f"{_SET_NOT_FOUND} '{set_id}'") from e
        counts = await emotion_library.count_items_by_set([row.id])
        return _set_payload(row, counts.get(row.id, 0), True)

    @router.delete("/sets/{set_id}", summary="删预设 / 存量套组")
    async def delete_set_api(
        set_id: int,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        _require_platform(scope)
        row = await DBEmotionSet.get_or_none(id=set_id)
        if row is None:
            raise NotFoundError(resource=f"{_SET_NOT_FOUND} '{set_id}'")
        if row.kind == emotion_library.KIND_COLLECT:
            raise ValidationError(reason="收集库不能删除：它跟着实例走，清空请用实例生命周期接口")
        try:
            counts = await emotion_library.delete_set(set_id)
        except ValueError as e:
            raise ValidationError(reason=str(e)) from e
        return {"set_id": set_id, **counts}

    @router.post("/sets/{set_id}/upload", summary="上传图片进预设 / 存量套组")
    async def upload_into_set(
        set_id: int,
        image: UploadFile = File(...),
        description: str = Form(default=""),
        tags: str = Form(default=""),
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        _require_platform(scope)
        row = await _console_set_or_404(scope, set_id)
        if row.kind == emotion_library.KIND_COLLECT:
            raise ValidationError(reason="收集库不接受控制台上传：它只放 Agent 收集的表情")

        if len(description) > _MAX_DESCRIPTION_CHARS:
            raise HTTPException(status_code=422, detail=f"描述最多 {_MAX_DESCRIPTION_CHARS} 个字符")
        tag_list = _validate_tags([piece for piece in re.split(r"[,，\s]+", tags) if piece]) or []

        data = await image.read()
        if len(data) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=422, detail="图片不能超过 10MB")
        extension = emotion_library.detect_image_extension(data)
        if extension is None:
            raise HTTPException(status_code=422, detail="不是支持的图片格式")

        digest = emotion_library.sha256_of_bytes(data)
        existing = await DBEmotionItem.filter(set_id=row.id, file_sha256=digest).order_by("id").first()
        if existing is not None:
            # 同一套组里同一张图只留一条：重复上传返回已有那条，不新建也不改它
            return {
                "emotion_id": existing.emotion_id,
                "set_id": row.id,
                "exists": True,
                "description": existing.description,
                "tags": list(existing.tags or []),
            }

        relative_path, file_sha256 = await asyncio.to_thread(
            emotion_library.store_bytes_by_hash,
            data,
            extension,
        )
        item = await emotion_library.create_item(
            set_id=row.id,
            description=description,
            tags=tag_list,
            file_path=relative_path,
            file_sha256=file_sha256,
            source_path=f"console:{image.filename or ''}",
        )
        embedding = await _safe_embedding(_embedding_text(description, tag_list))
        if embedding is not None:
            await emotion_library.vector_upsert(
                item.emotion_id,
                embedding,
                set_id=row.id,
                description=description,
                tags=tag_list,
            )
        return {
            "emotion_id": item.emotion_id,
            "set_id": row.id,
            "exists": False,
            "description": description,
            "tags": tag_list,
        }

    @router.post("/items/move", summary="把表情移到另一个套组")
    async def move_items_api(
        payload: ItemsMoveRequest,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        _require_platform(scope)
        if await DBEmotionSet.filter(id=payload.target_set_id).count() == 0:
            raise NotFoundError(resource=f"{_SET_NOT_FOUND} '{payload.target_set_id}'")
        moved = await emotion_library.move_items(payload.emotion_ids, payload.target_set_id)
        return {"moved": moved, "target_set_id": payload.target_set_id}

    @router.post("/items/copy", summary="把表情复制到另一个套组")
    async def copy_items_api(
        payload: ItemsMoveRequest,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        _require_platform(scope)
        if await DBEmotionSet.filter(id=payload.target_set_id).count() == 0:
            raise NotFoundError(resource=f"{_SET_NOT_FOUND} '{payload.target_set_id}'")
        created = await emotion_library.copy_items(payload.emotion_ids, payload.target_set_id)
        return {"created": created, "target_set_id": payload.target_set_id}

    @router.get("/assignments", summary="列出套组分配")
    async def list_assignments(
        set_id: Optional[int] = None,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        _require_platform(scope)
        query = DBEmotionSetAssignment.all()
        if set_id is not None:
            query = query.filter(set_id=set_id)
        rows = await query.order_by("id")
        preset_ids = {int(row.preset_id) for row in rows if row.target == emotion_library.TARGET_PRESET}
        existing = (
            {int(preset_id) for preset_id in await DBPreset.filter(id__in=sorted(preset_ids)).values_list("id", flat=True)}
            if preset_ids
            else set()
        )
        items = []
        for row in rows:
            items.append({
                "id": row.id,
                "set_id": row.set_id,
                "target": row.target,
                "adapter_key": row.adapter_key,
                "instance_key": row.instance_key,
                "preset_id": row.preset_id,
                "preset_missing": row.target == emotion_library.TARGET_PRESET and row.preset_id not in existing,
            })
        return {"total": len(items), "items": items}

    @router.post("/assignments", summary="加一条分配")
    async def create_assignment(
        payload: AssignmentRequest,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        _require_platform(scope)
        try:
            row = await emotion_library.add_assignment(
                payload.set_id,
                target=payload.target,
                adapter_key=payload.adapter_key,
                instance_key=payload.instance_key,
                preset_id=payload.preset_id,
                created_by=scope.user.username,
            )
        except ValueError as e:
            # 未知目标 / 缺实例对 / 收集库不可分配：都是请求本身不成立
            raise ValidationError(reason=str(e)) from e
        return {
            "id": row.id,
            "set_id": row.set_id,
            "target": row.target,
            "adapter_key": row.adapter_key,
            "instance_key": row.instance_key,
            "preset_id": row.preset_id,
        }

    @router.delete("/assignments/{assignment_id}", summary="删一条分配")
    async def delete_assignment(
        assignment_id: int,
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        _require_platform(scope)
        await emotion_library.remove_assignment(assignment_id)
        return {"id": assignment_id, "deleted": True}

    @router.get("/legacy-progress", summary="存量套组的整理进度")
    async def legacy_progress(
        scope: PluginRouteScope = Depends(plugin_route_scope(plugin.key, allow_global_view=True)),
    ) -> Dict[str, Any]:
        _require_platform(scope)
        legacy = await DBEmotionSet.filter(kind=emotion_library.KIND_LEGACY).order_by("id").first()
        if legacy is None:
            return {"set_id": 0, "remaining": 0, "already_in_preset": 0, "preset_items": 0}
        rows = await DBEmotionItem.filter(set_id=legacy.id)
        preset_set_ids = [
            int(set_id) for set_id in await DBEmotionSet.filter(kind=emotion_library.KIND_PRESET).values_list("id", flat=True)
        ]
        preset_hashes = set()
        if preset_set_ids:
            preset_hashes = {
                str(sha)
                for sha in await DBEmotionItem.filter(set_id__in=preset_set_ids).values_list("file_sha256", flat=True)
                if sha
            }
        already = sum(1 for item in rows if item.file_sha256 and item.file_sha256 in preset_hashes)
        return {
            "set_id": legacy.id,
            "remaining": len(rows),
            "already_in_preset": already,
            "preset_items": await DBEmotionItem.filter(set_id__in=preset_set_ids).count() if preset_set_ids else 0,
        }

    page_router = APIRouter()

    @page_router.get(CONSOLE_PAGE_PATH, response_class=HTMLResponse, summary="表情控制台页面")
    async def console_page() -> HTMLResponse:
        """不含数据的单页：登录态、实例范围与数据都由页面里的脚本去取"""
        return HTMLResponse(content=_CONSOLE_PAGE_HTML)

    page_router.include_router(router)
    return page_router


# endregion: 控制台接口


@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件"""
