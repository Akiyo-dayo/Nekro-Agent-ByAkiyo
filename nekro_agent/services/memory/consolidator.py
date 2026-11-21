"""情景记忆沉淀器

从聊天消息中提取、压缩、沉淀情景记忆。
使用第三人称视角，结合人设上下文。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import json5
import openai
from tortoise.timezone import now as db_now

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import MEMORY_PARSE_LOG_DIR
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType, MemorySource
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.identity_scope import resolve_preset_id
from nekro_agent.services.memory.embedding_service import embed_text
from nekro_agent.services.memory.feature_flags import is_memory_system_enabled
from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

logger = get_sub_logger("memory.consolidator")
MEMORY_PARSE_LOG_PATH = Path(MEMORY_PARSE_LOG_DIR)
MEMORY_PARSE_LOG_PATH.mkdir(parents=True, exist_ok=True)

# 解析失败转储的三重上限。转储只用于事后排查一次解析失败，没有任何长期价值，
# 而单进程托管几十个实例时它的写入频率是实例数的线性倍数，不设限就是一条磁盘泄漏。
MEMORY_PARSE_LOG_RETENTION_SECONDS = 7 * 86400
MEMORY_PARSE_LOG_MAX_FILES = 200
MEMORY_PARSE_LOG_MAX_TOTAL_BYTES = 32 * 1024 * 1024
# 单条 LLM 回复可以任意长，截断到够定位问题的长度即可，避免一条超长回复独占整个配额
MEMORY_PARSE_LOG_MAX_SECTION_CHARS = 20000


def _truncate_dump_section(text: str) -> str:
    """截断转储正文，超长时留下明确标记而不是悄悄截掉"""
    if len(text) <= MEMORY_PARSE_LOG_MAX_SECTION_CHARS:
        return text
    dropped = len(text) - MEMORY_PARSE_LOG_MAX_SECTION_CHARS
    return f"{text[:MEMORY_PARSE_LOG_MAX_SECTION_CHARS]}\n...[truncated {dropped} chars]"


def prune_parse_failure_dumps(now_ts: float) -> int:
    """按 TTL + 文件数 + 总体积三重上限轮转解析失败转储，返回删除的文件数。

    同步实现：调用方在 `asyncio.to_thread` 里执行，几千个文件的 stat 不能压在事件循环上。
    """
    entries: list[tuple[float, int, Path]] = []
    try:
        for path in MEMORY_PARSE_LOG_PATH.glob("*.log"):
            try:
                stat_result = path.stat()
            except OSError:
                continue
            entries.append((stat_result.st_mtime, stat_result.st_size, path))
    except OSError as e:
        logger.warning(f"解析失败转储轮转跳过（目录不可读）: {e}")
        return 0

    entries.sort(key=lambda item: item[0])
    cutoff_ts = now_ts - MEMORY_PARSE_LOG_RETENTION_SECONDS

    survivors: list[tuple[float, int, Path]] = []
    doomed: list[Path] = []
    for entry in entries:
        if entry[0] < cutoff_ts:
            doomed.append(entry[2])
        else:
            survivors.append(entry)

    excess_files = len(survivors) - MEMORY_PARSE_LOG_MAX_FILES
    if excess_files > 0:
        doomed.extend(entry[2] for entry in survivors[:excess_files])
        survivors = survivors[excess_files:]

    total_bytes = sum(entry[1] for entry in survivors)
    index = 0
    while total_bytes > MEMORY_PARSE_LOG_MAX_TOTAL_BYTES and index < len(survivors):
        total_bytes -= survivors[index][1]
        doomed.append(survivors[index][2])
        index += 1

    removed = 0
    for path in doomed:
        try:
            path.unlink()
        except OSError as e:
            logger.debug(f"解析失败转储删除失败: path={path}, error={e}")
            continue
        removed += 1
    if removed:
        logger.info(f"解析失败转储已轮转: removed={removed}, kept={len(entries) - removed}")
    return removed


@dataclass
class PersonaContext:
    """人设身份上下文"""

    bot_sender_id: str  # Bot 在消息中的 sender_id
    persona_name: str  # 人设名称
    persona_brief: str  # 人设简述
    relationship_hint: str | None = None  # 与用户的关系提示


@dataclass
class ConsolidationResult:
    """沉淀结果"""

    paragraphs_created: int = 0
    entities_created: int = 0
    entities_updated: int = 0
    relations_created: int = 0
    relations_updated: int = 0
    messages_processed: int = 0
    last_processed_message_db_id: int = 0
    errors: list[str] | None = None
    # 这一批为什么被跳过 / 失败，供调用方区分「已推进」与「原地重试」
    skipped_reason: str | None = None
    failed_reason: str | None = None
    retry_after: str | None = None
    # 这一轮只初始化了首次沉淀的游标，没有取消息、没有调模型
    cursor_initialized: bool = False


class MemoryResponseParseError(RuntimeError):
    """记忆提取响应解析失败。"""

    def __init__(self, classification: str, message: str):
        super().__init__(message)
        self.classification = classification


class ConsolidationBatchSkipped(RuntimeError):
    """这一批不可能成功，跳过并推进进度。

    内容被拦、供应商拒答、解析重试用尽都属于这一类：同样的输入重发多少次结果都一样，
    原样留在游标上只会让后面所有消息永远排在这一批后面。
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ConsolidationBatchFailed(RuntimeError):
    """临时故障，不推进进度，下次再试。

    鉴权、限流、超时、5xx 都是「现在不行、以后可能行」，推进游标等于把这一段时间的
    对话永久丢掉。
    """

    def __init__(self, reason: str, original: Exception | None = None):
        super().__init__(reason)
        self.reason = reason
        self.original_error = original


def _classify_llm_failure(exc: Exception) -> str:
    """把沉淀模型调用的失败归到「能不能原地重试」的那几类里。

    只用于决定进度推不推进与日志分类，不改变异常本身的传播方式。
    """
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return "auth"
    if isinstance(exc, openai.RateLimitError):
        return "rate_limit"
    # `APITimeoutError` 是 `APIConnectionError` 的子类，两类都按超时处理
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return "timeout"
    if isinstance(exc, openai.InternalServerError):
        return "server_error"
    if isinstance(exc, openai.APIStatusError) and exc.status_code >= 500:
        return "server_error"
    return "unknown"


def _parse_retry_after(value: Any) -> datetime | None:
    """解析进度里存的 ISO 退避截止时间；格式不对就当作没有退避。

    写进去的是 `datetime.now().isoformat()`（本地朴素时间），这里统一去掉时区再比较，
    避免历史数据带偏移量时在比较处抛 `TypeError`。
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


class EpisodicConsolidator:
    """情景记忆沉淀器

    职责：
    1. 读取未沉淀的聊天消息
    2. 使用 LLM 提炼情景记忆
    3. 提取实体和关系
    4. 生成向量并存储
    """

    def __init__(self, workspace_id: int):
        self.workspace_id = workspace_id
        self.persona_context: PersonaContext | None = None
        # 当前沉淀来源的适配器与实例标识，单实例适配器的实例段为空串。
        # 适配器这一列是给用量流水用的：`(adapter_key, instance_key)` 才唯一确定一个实例，
        # 只有 instance_key 时 `usage.ledger._resolve_attribution` 归不出租户。
        self.adapter_key: str = ""
        self.instance_key: str = ""
        # 当前沉淀来源的人设标识（`identity_scope.PRESET_NONE` = 0 表示无人设档）。
        # 取的是**沉淀发生的那一刻**频道生效的人设，而不是消息发生时的——消息表上没有
        # 人设这一列，没有第二个可用的来源。沉淀是准实时的（`scheduler` 按批推进），
        # 窗口很小；运营正好在窗口里换人设的话，这一批会记到新人设名下。
        # 要做到逐条精确得给 `chat_message` 加列并回填，不在本轮范围内。
        self.preset_id: int = 0

    async def _load_channel(self, chat_key: str) -> DBChatChannel | None:
        """加载聊天频道对象，并同步本次沉淀的隔离标签 `(实例, 人设)`。"""
        channel = await DBChatChannel.filter(chat_key=chat_key).first()
        if channel is not None:
            self.adapter_key = channel.adapter_key or ""
            self.instance_key = channel.instance_key or ""
            self.preset_id = await resolve_preset_id(channel)
        return channel

    def _read_memory_state(self, channel: DBChatChannel) -> dict[str, Any]:
        """从频道 data 中读取记忆状态。"""
        try:
            data = json.loads(channel.data or "{}")
            if not isinstance(data, dict):
                return {}
            memory_state = data.get("memory_state", {})
            return memory_state if isinstance(memory_state, dict) else {}
        except json.JSONDecodeError:
            return {}

    async def _update_memory_state(
        self,
        chat_key: str,
        mutate: Callable[[dict[str, Any]], None],
    ) -> None:
        """读取频道 data 里的 memory_state，交给 `mutate` 原地修改后写回。

        进度、跳过计数、失败计数与退避时间都落在同一个字典里，读改写只留这一处，
        避免两个调用点各写一份「解析 data → 取 memory_state → 保存」。
        """
        channel = await self._load_channel(chat_key)
        if channel is None:
            return

        try:
            data = json.loads(channel.data or "{}")
            if not isinstance(data, dict):
                data = {}
        except json.JSONDecodeError:
            data = {}

        memory_state = data.get("memory_state", {})
        if not isinstance(memory_state, dict):
            memory_state = {}
        mutate(memory_state)
        data["memory_state"] = memory_state

        channel.data = json.dumps(data, ensure_ascii=False)
        await channel.save(update_fields=["data", "update_time"])

    @staticmethod
    def _advance_cursor(memory_state: dict[str, Any], last_message_db_id: int) -> None:
        """把游标推到这一批的最后一条，顺带清掉临时故障的计数与退避。

        游标能推进，说明模型这一轮应答了（哪怕是拒答或解析不出），「连续失败」就该从头算；
        跳过计数是历史记录，不在这里清。
        """
        memory_state["last_consolidated_message_db_id"] = last_message_db_id
        memory_state["last_consolidated_at"] = datetime.now().isoformat()
        if "consecutive_failures" in memory_state:
            memory_state["consecutive_failures"] = 0
        memory_state.pop("consolidation_retry_after", None)

    async def _mark_consolidated(self, chat_key: str, last_message_db_id: int) -> None:
        """记录最近一次已沉淀到的消息边界。"""
        await self._update_memory_state(
            chat_key,
            lambda memory_state: self._advance_cursor(memory_state, last_message_db_id),
        )

    async def load_persona_context(self, chat_key: str) -> PersonaContext | None:
        """加载人设上下文

        Args:
            chat_key: 聊天频道标识

        Returns:
            人设上下文，或 None
        """
        try:
            channel = await self._load_channel(chat_key)
            if not channel:
                return None

            # 获取频道有效配置
            channel_config = await channel.get_effective_config()

            # 提取人设名称
            preset = channel_config.AI_CHAT_PRESET_SETTING or ""
            persona_name = self._extract_persona_name(preset)

            self.persona_context = PersonaContext(
                bot_sender_id="-1",  # Bot 消息的 sender_id
                persona_name=persona_name,
                persona_brief=preset[:100] if preset else "AI 助手",
            )
            return self.persona_context

        except Exception as e:
            logger.warning(f"加载人设上下文失败: {e}")
            return None

    def _extract_persona_name(self, preset: str) -> str:
        """从人设文本中提取名称"""
        if not preset:
            return "Assistant"

        # 尝试匹配常见的名称声明模式
        import re

        patterns = [
            r"我(?:的名字)?(?:是|叫)[：:\s]*([^\s,，。.!！]+)",
            r"名(?:字|称)[：:\s]*([^\s,，。.!！]+)",
            r"\[(?:角色|名称|name)[：:\s]*([^\]]+)\]",
        ]

        for pattern in patterns:
            match = re.search(pattern, preset, re.IGNORECASE)
            if match:
                return match.group(1).strip()

        # 默认返回
        return "Assistant"

    async def consolidate(
        self,
        chat_key: str,
        max_message_db_id: int | None = None,
        start_after_db_id: int | None = None,
        persist_progress: bool = True,
    ) -> ConsolidationResult:
        """执行沉淀

        Args:
            chat_key: 聊天频道标识

        Returns:
            沉淀结果
        """
        result = ConsolidationResult()

        try:
            channel: DBChatChannel | None = None
            memory_state: dict[str, Any] = {}
            # 退避期内直接返回：不取消息、不调模型，等下次调度再试
            if persist_progress:
                channel = await self._load_channel(chat_key)
                if channel is not None:
                    memory_state = self._read_memory_state(channel)
                    retry_after = _parse_retry_after(memory_state.get("consolidation_retry_after"))
                    if retry_after is not None and retry_after > datetime.now():
                        logger.debug(
                            f"记忆沉淀处于退避期，跳过本轮: chat_key={chat_key}, retry_after={retry_after.isoformat()}",
                        )
                        result.failed_reason = "backoff"
                        result.retry_after = retry_after.isoformat()
                        return result

            # 首次沉淀初始化游标：没有游标键就从「当前最新消息」开始，不回溯开启之前的历史。
            # 临时故障只写了失败计数、同样没有游标键，所以判键而不是判整个 memory_state 是否为空。
            # 重建自己管游标（persist_progress=False），显式传了起点的调用也由调用方管，都不初始化。
            if (
                persist_progress
                and start_after_db_id is None
                and channel is not None
                and "last_consolidated_message_db_id" not in memory_state
            ):
                latest = await self._latest_message_db_id(chat_key)
                if isinstance(max_message_db_id, int) and max_message_db_id > 0:
                    latest = min(latest, max_message_db_id)
                started_at = datetime.now().isoformat()

                def init_cursor(state: dict[str, Any]) -> None:
                    state["last_consolidated_message_db_id"] = latest
                    state["consolidation_start_message_db_id"] = latest
                    state["consolidation_started_at"] = started_at

                await self._update_memory_state(chat_key, init_cursor)
                logger.info(
                    f"记忆沉淀首次初始化游标: chat_key={chat_key}, start_message_db_id={latest}; "
                    "不回溯开启之前的历史，要回溯走重建",
                )
                result.cursor_initialized = True
                return result

            # 加载人设上下文
            await self.load_persona_context(chat_key)

            # 获取待处理消息
            messages = await self._fetch_messages(
                chat_key,
                max_message_db_id=max_message_db_id,
                start_after_db_id=start_after_db_id,
            )
            if not messages:
                logger.debug(f"没有待处理消息: chat_key={chat_key}")
                return result

            result.messages_processed = len(messages)
            result.last_processed_message_db_id = messages[-1].id
            logger.info(f"开始沉淀 {len(messages)} 条消息: chat_key={chat_key}")
            last_message_db_id = messages[-1].id

            # 提炼记忆
            try:
                memory_items = await self._extract_memories(messages)
            except ConsolidationBatchSkipped as e:
                reason = e.reason
                # 这一批不可能成功：推进进度，否则后面的消息永远排在它后面
                if persist_progress:
                    now = datetime.now()

                    def record_skip(memory_state: dict[str, Any]) -> None:
                        # 推进游标与记跳过计数合成一次写盘
                        self._advance_cursor(memory_state, last_message_db_id)
                        memory_state["skipped_batches"] = int(memory_state.get("skipped_batches", 0)) + 1
                        memory_state["skipped_messages"] = int(memory_state.get("skipped_messages", 0)) + len(messages)
                        memory_state["last_skip_at"] = now.isoformat()
                        memory_state["last_skip_reason"] = reason

                    await self._update_memory_state(chat_key, record_skip)
                logger.warning(
                    f"记忆沉淀跳过这一批: chat_key={chat_key}, reason={reason}, "
                    f"message_range={messages[0].id}-{messages[-1].id}",
                )
                result.skipped_reason = reason
                return result
            except ConsolidationBatchFailed as e:
                reason = e.reason
                # 临时故障：进度原地不动，整批下次重来
                if persist_progress:
                    now = datetime.now()
                    failures = 0
                    retry_after_iso: str | None = None

                    def record_failure(memory_state: dict[str, Any]) -> None:
                        nonlocal failures, retry_after_iso
                        failures = int(memory_state.get("consecutive_failures", 0)) + 1
                        memory_state["consecutive_failures"] = failures
                        memory_state["last_failure_at"] = now.isoformat()
                        memory_state["last_failure_reason"] = reason
                        if failures >= 3:
                            # 30 分钟起步，每多一次失败翻倍，封顶 6 小时
                            delay_seconds = min(30 * 60 * 2 ** (failures - 3), 6 * 60 * 60)
                            retry_after_iso = (now + timedelta(seconds=delay_seconds)).isoformat()
                            memory_state["consolidation_retry_after"] = retry_after_iso

                    await self._update_memory_state(chat_key, record_failure)
                    result.retry_after = retry_after_iso
                    if failures == 3:
                        logger.error(
                            f"记忆沉淀连续 {failures} 次失败，已退避到 {retry_after_iso}: "
                            f"chat_key={chat_key}, reason={reason}",
                        )
                    else:
                        logger.warning(
                            f"记忆沉淀失败（连续 {failures} 次），进度不推进: "
                            f"chat_key={chat_key}, reason={reason}",
                        )
                else:
                    logger.warning(
                        f"记忆沉淀失败，进度不推进: chat_key={chat_key}, reason={reason}",
                    )
                if result.errors is None:
                    result.errors = []
                original = e.original_error
                result.errors.append(str(original) if original is not None else str(e))
                result.failed_reason = reason
                return result

            # 提炼成功（含正常返回空列表）：推进游标时顺带清掉失败计数与退避，历史跳过计数保留
            if not memory_items:
                if persist_progress:
                    await self._mark_consolidated(chat_key, last_message_db_id)
                logger.debug("没有提取到有价值的记忆")
                return result

            # 存储记忆
            for item in memory_items:
                try:
                    paragraph, entities, relations = await self._store_memory(item, chat_key, messages)
                    if paragraph:
                        result.paragraphs_created += 1
                    result.entities_created += len([e for e, created in entities if created])
                    result.entities_updated += len([e for e, created in entities if not created])
                    result.relations_created += len([r for r, created in relations if created])
                    result.relations_updated += len([r for r, created in relations if not created])
                except Exception as e:
                    logger.exception(f"存储记忆失败: {e}")
                    if result.errors is None:
                        result.errors = []
                    result.errors.append(str(e))
                # 主动让出事件循环，避免长批次沉淀连续占用处理时机。
                await asyncio.sleep(0)

            logger.info(
                f"沉淀完成: paragraphs={result.paragraphs_created}, "
                f"entities={result.entities_created}+{result.entities_updated}, "
                f"relations={result.relations_created}+{result.relations_updated}",
            )
            if result.paragraphs_created > 0:
                try:
                    from nekro_agent.services.memory.episode_aggregator import aggregate_workspace_episodes

                    await aggregate_workspace_episodes(self.workspace_id, chat_key)
                except Exception as e:
                    logger.warning(f"Episode 自动聚合失败: {e}")
            if persist_progress:
                await self._mark_consolidated(chat_key, last_message_db_id)
            cooldown = max(0.0, float(config.MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS))
            if cooldown > 0:
                await asyncio.sleep(cooldown)
            return result

        except Exception as e:
            logger.exception(f"沉淀过程失败: {e}")
            result.errors = [str(e)]
            return result

    async def _latest_message_db_id(self, chat_key: str) -> int:
        """取频道当前最大的消息 id，没有消息返回 0。

        单独成方法是为了让测试能打桩「频道此刻的最大消息 id」。
        """
        latest = await DBChatMessage.filter(chat_key=chat_key).order_by("-id").first()
        return latest.id if latest is not None else 0

    async def _fetch_messages(
        self,
        chat_key: str,
        max_message_db_id: int | None = None,
        start_after_db_id: int | None = None,
    ) -> list[DBChatMessage]:
        """获取待处理消息"""
        if isinstance(start_after_db_id, int) and start_after_db_id >= 0:
            last_consolidated_db_id = start_after_db_id
        else:
            channel = await self._load_channel(chat_key)
            last_consolidated_db_id = 0
            if channel is not None:
                memory_state = self._read_memory_state(channel)
                raw_db_id = memory_state.get("last_consolidated_message_db_id", 0)
                if isinstance(raw_db_id, int) and raw_db_id >= 0:
                    last_consolidated_db_id = raw_db_id

        query = DBChatMessage.filter(
            chat_key=chat_key,
            id__gt=last_consolidated_db_id,
        )
        if isinstance(max_message_db_id, int) and max_message_db_id > 0:
            query = query.filter(id__lte=max_message_db_id)
        return await query.order_by("id").limit(config.MEMORY_CONSOLIDATION_BATCH_SIZE)

    async def _extract_memories(
        self,
        messages: list[DBChatMessage],
    ) -> list[dict[str, Any]]:
        """使用 LLM 提取记忆

        Returns:
            记忆项列表，每项包含 content, summary, entities, knowledge_type
        """
        if not messages:
            return []

        # 构建对话文本
        conversation_text = self._build_conversation_text(messages)
        if len(conversation_text) < config.MEMORY_CONSOLIDATION_MIN_CONTENT_LENGTH:
            return []

        # 构建 prompt
        persona_hint = ""
        if self.persona_context:
            persona_hint = (
                f"\n注意：对话中 sender_id 为 -1 的消息是 '{self.persona_context.persona_name}' "
                f"（{self.persona_context.persona_brief}）发送的，请在记忆中使用该名称而非 'AI' 或 '机器人'。"
            )

        prompt = f"""请分析以下对话，提取值得记住的重要信息。

要求：
1. 使用第三人称视角描述
2. 只提取有价值的信息（事实、偏好、重要讨论、决策等）
3. 过滤掉闲聊和无意义内容
4. 每条记忆应该简洁但完整{persona_hint}

对话内容：
{conversation_text}

请以 JSON 格式返回，格式如下：
```json
{{
  "memories": [
    {{
      "content": "第三人称的记忆描述，必须保留关键对象、行为、结论，不要写成过短标签",
      "summary": "可读性摘要（建议 20-40 字，必须能独立看懂）",
      "knowledge_type": "conversation|preference|fact|experience|decision|emotion",
      "entities": ["涉及的人名或概念"],
      "relations": [
        {{
          "subject": "实体A",
          "predicate": "喜欢|使用|创建|提到|讨论|属于",
          "object": "实体B"
        }}
      ]
    }}
  ]
}}
```

额外要求：
1. 不要输出“某人提到某事”“讨论了某话题”这种空泛句，必须带上具体内容。
2. 优先保留可复用的信息：能力、偏好、决策、结论、承诺、计划、问题定位、技术事实。
3. summary 不能只是项目名或动词短语，单独展示时必须有参考价值。
4. 如果多条信息属于同一事件，优先合并成一条更完整的记忆，而不是切成多个碎片。

如果没有值得记住的内容，返回空数组：{{"memories": []}}"""

        from nekro_agent.services.agent.openai import LLMContentBlockedError
        from nekro_agent.services.agent.provider_refusal import detect_provider_refusal

        parse_retries = max(0, int(config.MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES))
        for attempt in range(parse_retries + 1):
            try:
                use_fallback_model = attempt == parse_retries and parse_retries > 0
                if attempt > 0:
                    logger.info(
                        "记忆解析重试开始: attempt=%s/%s, use_fallback_model=%s",
                        attempt + 1,
                        parse_retries + 1,
                        use_fallback_model,
                    )
                response = await self._call_llm(prompt, use_fallback_model=use_fallback_model)
                # 供应商把拒绝页当成普通正文返回时，解析重试没有任何意义
                refusal = detect_provider_refusal(response)
                if refusal is not None:
                    logger.warning(f"记忆沉淀响应是供应商拒答页，跳过这一批: provider={refusal.provider}")
                    raise ConsolidationBatchSkipped("provider_refusal")
                return await self._parse_memory_response(response)
            except ConsolidationBatchSkipped:
                raise
            except LLMContentBlockedError as e:
                logger.warning(f"记忆沉淀请求被内容策略拦截，跳过这一批: {e}")
                raise ConsolidationBatchSkipped("content_blocked") from e
            except MemoryResponseParseError as e:
                if attempt >= parse_retries:
                    logger.warning(
                        f"记忆响应解析在重试后仍失败: classification={e.classification}, attempts={attempt + 1}"
                    )
                    raise ConsolidationBatchSkipped("parse_failed") from e
                delay = min(2.0, 0.5 * (attempt + 1))
                logger.warning(
                    f"记忆响应解析失败，准备重新请求 LLM: classification={e.classification}, "
                    f"attempt={attempt + 1}/{parse_retries + 1}, "
                    f"next_use_fallback_model={attempt + 1 == parse_retries}, delay={delay}s"
                )
                await asyncio.sleep(delay)
            except Exception as e:
                reason = _classify_llm_failure(e)
                logger.exception(f"LLM 提取记忆失败: reason={reason}, error={e}")
                raise ConsolidationBatchFailed(reason, original=e) from e
        return []

    def _build_conversation_text(self, messages: list[DBChatMessage]) -> str:
        """构建对话文本"""
        lines = []
        for msg in messages:
            time_str = datetime.fromtimestamp(msg.send_timestamp).strftime("%H:%M")
            sender = msg.sender_nickname or msg.sender_name or f"User_{msg.sender_id}"

            # 标记 bot 消息
            if msg.sender_id == "-1" and self.persona_context:
                sender = self.persona_context.persona_name

            content = msg.content_text[:500]  # 限制单条长度
            lines.append(f"[{time_str}] {sender}: {content}")

        return "\n".join(lines)

    async def _call_llm(self, prompt: str, *, use_fallback_model: bool = False) -> str:
        """调用 LLM（带重试机制）

        使用指数退避策略重试，处理临时性错误（网络、速率限制等）。
        """
        from nekro_agent.services.agent.openai import gen_openai_chat_response, parse_extra_body
        from nekro_agent.services.usage import LLMUsageContext, LLMUsageScene, usage_scope

        primary_model_group_name = config.MEMORY_CONSOLIDATION_MODEL_GROUP or config.USE_MODEL_GROUP
        fallback_model_group_name = config.MEMORY_CONSOLIDATION_FALLBACK_MODEL_GROUP or primary_model_group_name
        model_group_name = fallback_model_group_name if use_fallback_model else primary_model_group_name
        model_group = config.get_model_group_info(model_group_name)
        max_retries = config.MEMORY_LLM_MAX_RETRIES
        last_error: Exception | None = None
        extra_body = parse_extra_body(
            model_group.EXTRA_BODY,
            source_hint=f"Memory consolidation model group: {model_group_name}",
        ) or {}
        enable_force_json = config.MEMORY_CONSOLIDATION_FORCE_JSON_OUTPUT
        if enable_force_json:
            extra_body.setdefault("response_format", {"type": "json_object"})

        for attempt in range(max_retries + 1):
            try:
                if attempt == 0:
                    logger.info(
                        "开始记忆沉淀模型请求: model_group=%s, model=%s, use_fallback_model=%s",
                        model_group_name,
                        model_group.CHAT_MODEL,
                        use_fallback_model,
                    )
                usage_context = LLMUsageContext(
                    scene=LLMUsageScene.MEMORY_CONSOLIDATE,
                    adapter_key=self.adapter_key,
                    instance_key=self.instance_key,
                    preset_id=self.preset_id,
                ).with_model_group(model_group_name, model_group)
                with usage_scope(usage_context):
                    response = await gen_openai_chat_response(
                        model=model_group.CHAT_MODEL,
                        messages=[
                            {"role": "system", "content": "你是一个记忆提取助手，负责从对话中提取重要信息。"},
                            {"role": "user", "content": prompt},
                        ],
                        api_key=model_group.API_KEY,
                        base_url=model_group.BASE_URL,
                        temperature=0.3,
                        max_tokens=3000,
                        extra_body=extra_body or None,
                    )
                return response.response_content
            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                if enable_force_json and "response_format" in error_str:
                    enable_force_json = False
                    extra_body.pop("response_format", None)
                    logger.warning(
                        f"记忆沉淀模型组 {model_group_name} 不支持 response_format，已自动回退普通输出模式: {e}",
                    )
                    continue

                # 判断是否为可重试的错误
                is_retryable = any(
                    keyword in error_str
                    for keyword in [
                        "timeout",
                        "rate limit",
                        "rate_limit",
                        "429",
                        "503",
                        "502",
                        "connection",
                        "network",
                        "temporarily",
                        "overloaded",
                    ]
                )

                if not is_retryable or attempt >= max_retries:
                    logger.warning(
                        f"LLM 调用失败（模型组={model_group_name}，不可重试或已达重试上限）: {e}"
                    )
                    raise

                # 指数退避：1s, 2s, 4s, ...
                delay = 2**attempt
                logger.info(
                    f"LLM 调用失败（模型组={model_group_name}），{delay}s 后重试"
                    f"（第 {attempt + 1}/{max_retries} 次）: {e}"
                )
                await asyncio.sleep(delay)

        # 理论上不会执行到这里，但为类型安全添加
        if last_error:
            raise last_error
        raise RuntimeError("LLM 调用失败")

    async def _parse_memory_response(self, response: str) -> list[dict[str, Any]]:
        """解析 LLM 返回的记忆"""
        normalized = (response or "").strip().lstrip("\ufeff")
        if not normalized:
            logger.warning("解析记忆 JSON 失败: LLM 返回为空响应")
            raise MemoryResponseParseError("empty", "LLM 返回为空响应")

        json_match = re.search(r"```json\s*(.*?)\s*```", normalized, re.DOTALL | re.IGNORECASE)
        if json_match:
            candidate = json_match.group(1)
        else:
            object_match = re.search(r"(\{[\s\S]*\})", normalized)
            candidate = object_match.group(1) if object_match else normalized

        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as json_error:
            try:
                data = json5.loads(candidate)
                logger.warning(f"记忆 JSON 标准解析失败，已使用 JSON5 容错解析: {json_error}")
            except Exception as json5_error:
                classification = self._classify_parse_failure(normalized, candidate, json_error)
                failure_path = await self._dump_parse_failure(
                    normalized_response=normalized,
                    candidate_response=candidate,
                    classification=classification,
                    json_error=json_error,
                    json5_error=json5_error,
                )
                preview = re.sub(r"\s+", " ", normalized)[:240]
                logger.warning(
                    f"解析记忆 JSON 失败[{classification}]: {json_error}; "
                    f"json5_error={json5_error}; failure_log={failure_path}; 响应预览={preview!r}"
                )
                raise MemoryResponseParseError(classification, f"{json_error}; failure_log={failure_path}")

        try:
            memories = data.get("memories", [])

            # 验证格式
            valid_memories = []
            for mem in memories:
                if isinstance(mem, dict) and "content" in mem:
                    valid_memories.append(
                        {
                            "content": mem["content"],
                            "summary": mem.get("summary", mem["content"][:50]),
                            "knowledge_type": mem.get("knowledge_type", "conversation"),
                            "entities": mem.get("entities", []),
                            "relations": mem.get("relations", []),
                        },
                    )
            return valid_memories

        except Exception as e:
            failure_path = await self._dump_parse_failure(
                normalized_response=normalized,
                candidate_response=candidate,
                classification="schema_invalid",
                json_error=e,
                json5_error=None,
            )
            preview = re.sub(r"\s+", " ", normalized)[:240]
            logger.warning(f"解析记忆结构失败[schema_invalid]: {e}; failure_log={failure_path}; 响应预览={preview!r}")
            raise MemoryResponseParseError("schema_invalid", f"{e}; failure_log={failure_path}")

    def _classify_parse_failure(self, normalized: str, candidate: str, error: Exception) -> str:
        lowered = normalized.lower()
        error_text = str(error).lower()
        if not normalized:
            return "empty"
        if lowered.startswith("**") or "extracting" in lowered or "i'm now focusing" in lowered:
            return "reasoning_text"
        if "unterminated string" in error_text:
            return "unterminated_string"
        if "expecting ',' delimiter" in error_text:
            return "truncated_or_missing_delimiter"
        if not candidate.startswith("{"):
            return "non_json_prefix"
        return "invalid_json"

    async def _dump_parse_failure(
        self,
        normalized_response: str,
        candidate_response: str,
        classification: str,
        json_error: Exception,
        json5_error: Exception | None,
    ) -> str:
        now = datetime.now()
        path = MEMORY_PARSE_LOG_PATH / f"{now.strftime('%Y%m%d_%H%M%S_%f')}_{classification}.log"
        body = "\n".join(
            [
                f"classification: {classification}",
                f"json_error: {json_error!r}",
                f"json5_error: {json5_error!r}",
                "===== normalized_response =====",
                _truncate_dump_section(normalized_response),
                "===== candidate_response =====",
                _truncate_dump_section(candidate_response),
            ],
        )
        # 写盘与轮转都是阻塞 IO，几十个实例共用一个事件循环时必须移出去
        await asyncio.to_thread(path.write_text, body, encoding="utf-8")
        await asyncio.to_thread(prune_parse_failure_dumps, now.timestamp())
        return str(path)

    async def _store_memory(
        self,
        memory_item: dict[str, Any],
        chat_key: str,
        messages: list[DBChatMessage],
    ) -> tuple[DBMemParagraph | None, list[tuple[DBMemEntity, bool]], list[tuple[DBMemRelation, bool]]]:
        """存储单条记忆

        Returns:
            (paragraph, [(entity, created), ...])
        """
        content = memory_item["content"]
        summary = memory_item["summary"][: config.MEMORY_CONSOLIDATION_MAX_SUMMARY_LENGTH]
        knowledge_type = memory_item.get("knowledge_type", "conversation")
        entity_names = memory_item.get("entities", [])
        relation_items = memory_item.get("relations", [])

        # 映射 knowledge_type
        kt_map = {
            "conversation": KnowledgeType.CONVERSATION,
            "preference": KnowledgeType.PREFERENCE,
            "fact": KnowledgeType.FACT,
            "experience": KnowledgeType.EXPERIENCE,
            "decision": KnowledgeType.DECISION,
            "emotion": KnowledgeType.EMOTION,
        }
        kt = kt_map.get(knowledge_type, KnowledgeType.CONVERSATION)

        # 计算锚定信息
        anchor_start = messages[0] if messages else None
        anchor_end = messages[-1] if messages else None

        # 去重检查：防止崩溃恢复后重复创建相同的记忆段落
        # 使用消息锚点范围 + 内容前缀匹配来判断是否已存在
        if anchor_start and anchor_end:
            existing = await DBMemParagraph.filter(
                workspace_id=self.workspace_id,
                instance_key=self.instance_key,
                preset_id=self.preset_id,
                origin_chat_key=chat_key,
                anchor_msg_id_start=anchor_start.message_id,
                anchor_msg_id_end=anchor_end.message_id,
            ).first()
            if existing and existing.content[:100] == content[:100]:
                logger.debug(f"跳过重复记忆段落: anchor={anchor_start.message_id}-{anchor_end.message_id}")
                return existing, [], []

        # 创建段落
        paragraph = await DBMemParagraph.create(
            workspace_id=self.workspace_id,
            instance_key=self.instance_key,
            preset_id=self.preset_id,
            memory_source="na",
            cognitive_type=CognitiveType.EPISODIC,
            knowledge_type=kt,
            content=content,
            summary=summary,
            # `event_time` 是 `DatetimeField`：写它的值一律带时区。`fromtimestamp` 不给 tz
            # 得到的是「进程本地时区的朴素值」，ORM 时区与进程时区不一致时会整体错开一个偏移量，
            # 而检索侧的时间窗拿的是 aware 值来比。带上 tz 之后表示的仍是同一个时刻。
            event_time=(
                datetime.fromtimestamp(anchor_end.send_timestamp, tz=timezone.utc) if anchor_end else db_now()
            ),
            half_life_seconds=config.MEMORY_EPISODIC_HALF_LIFE_SECONDS,
            origin_kind=OriginKind.CONSOLIDATION,
            origin_chat_key=chat_key,
            anchor_msg_id_start=anchor_start.message_id if anchor_start else None,
            anchor_msg_id_end=anchor_end.message_id if anchor_end else None,
            anchor_timestamp_start=anchor_start.send_timestamp if anchor_start else None,
            anchor_timestamp_end=anchor_end.send_timestamp if anchor_end else None,
        )

        # 生成向量并存储
        try:
            embedding = await embed_text(content)
            await memory_qdrant_manager.upsert_paragraph(
                paragraph_id=paragraph.id,
                embedding=embedding,
                payload=paragraph.to_qdrant_payload(),
            )
            paragraph.embedding_ref = str(paragraph.id)
            await paragraph.save(update_fields=["embedding_ref"])
        except Exception as e:
            logger.warning(f"向量化失败，记忆仍已保存: {e}")

        # 创建/更新实体
        entities: list[tuple[DBMemEntity, bool]] = []
        entity_map: dict[str, DBMemEntity] = {}
        for name in entity_names:
            if not name or len(name) < 2:
                continue
            try:
                # 简单判断实体类型
                entity_type = EntityType.PERSON if self._is_person_name(name) else EntityType.CONCEPT
                entity, created = await DBMemEntity.find_or_create(
                    workspace_id=self.workspace_id,
                    entity_type=entity_type,
                    name=name,
                    source=MemorySource.NA,
                    instance_key=self.instance_key,
                    preset_id=self.preset_id,
                )
                entities.append((entity, created))
                entity_map[name.strip().lower()] = entity
            except Exception as e:
                logger.warning(f"创建实体失败: {name}, {e}")

        relations: list[tuple[DBMemRelation, bool]] = []
        for rel in relation_items:
            try:
                if not isinstance(rel, dict):
                    continue
                subject_name = str(rel.get("subject", "")).strip()
                predicate = str(rel.get("predicate", "")).strip()
                object_name = str(rel.get("object", "")).strip()
                if not subject_name or not predicate or not object_name:
                    continue

                subject = entity_map.get(subject_name.lower())
                if subject is None:
                    subject_type = EntityType.PERSON if self._is_person_name(subject_name) else EntityType.CONCEPT
                    subject, _ = await DBMemEntity.find_or_create(
                        workspace_id=self.workspace_id,
                        entity_type=subject_type,
                        name=subject_name,
                        source=MemorySource.NA,
                        instance_key=self.instance_key,
                        preset_id=self.preset_id,
                    )
                    entity_map[subject_name.lower()] = subject

                obj = entity_map.get(object_name.lower())
                if obj is None:
                    object_type = EntityType.PERSON if self._is_person_name(object_name) else EntityType.CONCEPT
                    obj, _ = await DBMemEntity.find_or_create(
                        workspace_id=self.workspace_id,
                        entity_type=object_type,
                        name=object_name,
                        source=MemorySource.NA,
                        instance_key=self.instance_key,
                        preset_id=self.preset_id,
                    )
                    entity_map[object_name.lower()] = obj

                if subject.id == obj.id:
                    continue

                relation, created = await DBMemRelation.find_or_create(
                    workspace_id=self.workspace_id,
                    subject_entity_id=subject.id,
                    predicate=predicate,
                    object_entity_id=obj.id,
                    paragraph_id=paragraph.id,
                    memory_source="na",
                    cognitive_type=CognitiveType.EPISODIC.value,
                    instance_key=self.instance_key,
                    preset_id=self.preset_id,
                )
                if created:
                    relation.half_life_seconds = config.MEMORY_RELATION_HALF_LIFE_SECONDS
                    await relation.save(update_fields=["half_life_seconds", "update_time"])
                relations.append((relation, created))
            except Exception as e:
                logger.warning(f"创建关系失败: {rel}, {e}")

        return paragraph, entities, relations

    def _is_person_name(self, name: str) -> bool:
        """简单判断是否为人名"""
        # 如果是人设名称，返回 True
        if self.persona_context and name == self.persona_context.persona_name:
            return True
        # 简单启发式：中文名通常 2-4 字，英文名首字母大写
        if 2 <= len(name) <= 4 and all("\u4e00" <= c <= "\u9fff" for c in name):
            return True
        if name[0].isupper() and name.isalpha():
            return True
        return False


async def consolidate_workspace(
    workspace_id: int,
    chat_key: str,
    max_message_db_id: int | None = None,
    start_after_db_id: int | None = None,
    persist_progress: bool = True,
) -> ConsolidationResult:
    """便捷函数：执行工作区沉淀"""
    if not is_memory_system_enabled():
        return ConsolidationResult()

    consolidator = EpisodicConsolidator(workspace_id)
    return await consolidator.consolidate(
        chat_key,
        max_message_db_id=max_message_db_id,
        start_after_db_id=start_after_db_id,
        persist_progress=persist_progress,
    )
