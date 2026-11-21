import asyncio
import json
import random
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import magic

from nekro_agent.adapters.interface.schemas.extra import PlatformMessageExt
from nekro_agent.adapters.interface.schemas.platform import PlatformSendResponse
from nekro_agent.adapters.utils import adapter_utils
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel, is_chat_key_marked_bot_not_in_group
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.agent_message import (
    AgentMessageSegment,
    AgentMessageSegmentType,
    convert_agent_message_to_prompt,
)
from nekro_agent.schemas.chat_message import (
    TOOL_RESULT_SENDER_ID,
    TOOL_RESULT_SENDER_NAME,
    ChatMessage,
    ChatMessageSegmentType,
    ChatMessageSegmentUntrusted,
    ChatType,
    merge_video_placeholders,
    segments_from_list,
)
from nekro_agent.schemas.errors import AdapterUnavailableError
from nekro_agent.schemas.signal import MsgSignal
from nekro_agent.services import channel_quota
from nekro_agent.services.agent.round_trigger_accounts import note_pending_trigger
from nekro_agent.services.channel_broadcaster import channel_broadcaster
from nekro_agent.services.channel_delivery import ChannelUndeliverableError, channel_delivery_blocks
from nekro_agent.services.memory.feature_flags import (
    is_domain_scope_enabled,
    is_memory_enabled_for_channel,
    is_memory_system_enabled,
)
from nekro_agent.services.message_broadcaster import message_broadcaster
from nekro_agent.services.ops_agent.policy import OpsAgentPolicy, is_operator
from nekro_agent.services.ops_agent.triggers import (
    TRIGGER_KIND_HUMAN,
    TRIGGER_KIND_OTHER,
    TRIGGER_KIND_SYSTEM_PUSH,
    TRIGGER_KIND_UNTRUSTED_PUSH,
    RoundTrigger,
    round_trigger_book,
)
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.user.super_users import (
    is_super_user,
    is_trusted_listed_user,
    is_trusted_super_user_source,
    matches_super_users,
)
from nekro_agent.tools.at_markup import AT_MARKUP_PATTERN, normalize_malformed_at_markup
from nekro_agent.tools.common_util import (
    check_content_trigger,
    check_forbidden_message,
    copy_to_upload_dir,
    random_chat_check,
)
from nekro_agent.tools.text_sanitize import strip_nul
from nekro_agent.tools.untrusted_content import MAX_TOOL_RESULT_BODY_CHARS, TRUNCATION_MARK, UntrustedToolResult

logger = get_sub_logger("message_pipeline")


def _without_nul(message: ChatMessage) -> ChatMessage:
    """把入站消息里的 NUL 先剔掉，落库、触发判断、广播看到同一份数据

    只在确有字段变化时才复制：绝大多数消息不带 NUL，白复制一次会让下游拿到的对象
    与调用方手里的不是同一个，插件按对象身份做的记录（若有）也会错位。

    `chat_key` / `sender_id` / `platform_userid` / `message_id` 刻意不在这里动：
    `chat_key` 用来查频道，改了会查不到；这几个字段本身也是协议端的标识，
    该不该清洗属于协议层的判断，库这一侧由 `nul_guard` 的 `pre_save` 兜底。

    `content_data` 先转成字典再清洗，不是在 `json.dumps` 之后的字符串上替换：
    `json.dumps` 会把 NUL 转义成字面的 `\\u0000`（不是真 NUL），字符上替换不掉；
    而它读回来又会变回真 NUL，流进提示词。
    """
    changed: Dict[str, Any] = {}
    for field_name in ("content_text", "sender_name", "sender_nickname", "raw_cq_code", "ext_data"):
        original = getattr(message, field_name)
        cleaned = strip_nul(original)
        if cleaned != original:
            changed[field_name] = cleaned

    dumped = [segment.model_dump() for segment in message.content_data]
    cleaned_segments = strip_nul(dumped)
    if cleaned_segments != dumped:
        changed["content_data"] = segments_from_list(cleaned_segments)

    return message.model_copy(update=changed) if changed else message


async def _notify_memory_scheduler(channel: DBChatChannel, content_length: int) -> None:
    """通知记忆调度器有新消息（非阻塞）

    调度器需要频道对象本身：闸开后判据是频道自己的 `MEMORY_ENABLE_FOR_CHAT`，
    分区取 `channel.workspace_id or 0`，不再要求频道绑了工作区。闸关时这里仍按旧规则
    提前挡掉未绑工作区的频道，让调用链与改造前逐字一致。
    """
    if not is_domain_scope_enabled():
        if channel.workspace_id is None or not is_memory_system_enabled():
            return
        try:
            from nekro_agent.services.memory.scheduler import memory_scheduler

            await memory_scheduler.on_new_message(
                chat_key=channel.chat_key,
                workspace_id=channel.workspace_id,
                content_length=content_length,
            )
        except Exception as e:
            logger.debug(f"记忆调度器通知失败（可忽略）: {e}")
        return

    if not await is_memory_enabled_for_channel(channel):
        return
    try:
        from nekro_agent.services.memory.scheduler import memory_scheduler

        await memory_scheduler.on_new_message(
            chat_key=channel.chat_key,
            workspace_id=0,
            content_length=content_length,
            adapter_key=channel.adapter_key,
            instance_key=channel.instance_key,
        )
    except Exception as e:
        logger.debug(f"记忆调度器通知失败（可忽略）: {e}")


async def is_quota_exempt(message: ChatMessage, config: Any) -> bool:
    """这条消息的发送者是否免配额：配额白名单，或（开了豁免时）超级用户

    两份名单都按聊天命令的超级用户同一套判定（名单匹配 ∧ 来源可信，名单格式也相同：纯号码只当 OneBot 的
    QQ 号，其他平台写 `适配器标识:用户ID`）。只比 `sender_id` 的话，别的平台恰好同号的人，邮件的 `From`、
    SSE 的 `from_id`、租户自运行协议端上报的号码，都能填成名单里的值，白用平台的配额。
    """
    if await is_trusted_listed_user(
        message.adapter_key,
        message.instance_key,
        message.sender_id,
        config.AI_CHAT_QUOTA_WHITELIST_USERS,
    ):
        return True
    return bool(config.AI_CHAT_QUOTA_SUPER_USERS_EXEMPT) and await is_super_user(
        message.adapter_key,
        message.instance_key,
        message.sender_id,
    )


class MessageService:
    """消息服务类，处理所有类型的消息推送"""

    def __init__(self):
        # 全局状态追踪
        self.running_tasks: Dict[str, asyncio.Task] = {}  # 记录每个频道正在执行的agent任务
        self.debounce_timers: Dict[str, float] = {}  # 记录每个频道的防抖计时器
        self.pending_messages: Dict[str, ChatMessage] = {}  # 记录每个频道待处理的最新消息

    async def cancel_agent_task(self, chat_key: str) -> bool:
        """取消指定频道正在执行的 agent 任务

        Args:
            chat_key: 频道标识

        Returns:
            bool: 是否成功取消了任务
        """
        cancelled = False

        # 仅取消正在执行的任务，不清理待处理消息队列
        # 这样排队中尚未触发的 @ 消息仍会正常处理
        if chat_key in self.running_tasks and not self.running_tasks[chat_key].done():
            self.running_tasks[chat_key].cancel()
            cancelled = True
        self.running_tasks.pop(chat_key, None)

        return cancelled

    async def _message_validation_check(self, message: ChatMessage) -> bool:
        """消息校验"""
        plaint_text = message.content_text.strip().replace(" ", "").lower()
        is_fake_message = False

        # 检查伪造消息
        if re.match(r"<.{4,12}\|messageseparator>", plaint_text):
            is_fake_message = True
        if re.match(r"<.{4,12}\|messageseperator>", plaint_text):
            is_fake_message = True

        if "message" in plaint_text and "(id:" in plaint_text:
            is_fake_message = True
        if "from_id:" in plaint_text:  # noqa: SIM103
            is_fake_message = True

        if is_fake_message:
            logger.warning(f"检测到伪造消息: {message.content_text} | 跳过本次处理...")
            return False

        return True

    async def schedule_agent_task(
        self,
        chat_key: Optional[str] = None,
        message: Optional[ChatMessage] = None,
        ctx: Optional[AgentCtx] = None,
        *,
        bypass_channel_quota: bool = False,
        trigger: Optional[RoundTrigger] = None,
    ):
        """调度 agent 任务，实现防抖和任务控制

        Args:
            bypass_channel_quota: 调用方**自己已经判过**频道额度时置真。
                入站消息那条路径判完才调度，它要放行（包括豁免的人——豁免的人不受频道
                额度约束，用量照常计入）；其余调用方留默认值，让闸门在这里生效。
            trigger: 这次调度由什么触发。运维工具要靠它判「这一轮是不是运维人员要求的」，
                而防抖会让多条触发并进一轮、`AgentCtx` 上记的那个人未必是本轮的人，
                所以触发必须在**到达时**记进 `services/ops_agent/triggers.py`。
                没传的调用记成 `other`（将来新增的调度入口至少不会冒充人类消息）。
        """
        if not message:
            if not chat_key:
                logger.error("调度 Agent 执行失败，目标 chat_key 为空")
                return
            message = ChatMessage.create_empty(chat_key)
        chat_key = message.chat_key

        # Agent 触发闸门。入站的两道闸门（`collect_message` 与 `push_human_message` /
        # `push_system_message`）都覆盖不到直接调进本方法的定时任务
        # （`services/timer/timer_service.py`）。只靠出站闸门的话，被停用的实例仍会为这些
        # 触发跑完整个 Agent 回合、烧掉平台方的额度，只是最后一步发不出去。这里挡在跑之前。
        if not await DBAdapterInstance.is_chat_key_active(chat_key):
            logger.debug(f"实例或其所属租户已停用，跳过 Agent 调度: chat_key={chat_key}")
            return

        # Bot 不在群闸门。定时提醒、插件推送、CC 结果投递这类宿主替插件发起的回合，落到 Bot
        # 已经被移出的群里，只会调一轮模型、再被 QQ 以「你已被移出该群」拒绝。这里只读定期核对
        # 打的标记，不停用频道。人类消息不受影响：群里有人说话本身就说明 Bot 还在，入站那一步
        # 已经把标记改回了「在群」。
        trigger_kind = trigger.kind if trigger is not None else TRIGGER_KIND_OTHER
        if trigger_kind != TRIGGER_KIND_HUMAN and await is_chat_key_marked_bot_not_in_group(chat_key):
            logger.debug(f"频道已标记 Bot 不在群，跳过 Agent 调度: chat_key={chat_key}, trigger={trigger_kind}")
            return

        # 频道每日额度暂停闸门。定时提醒、插件推送、CC 结果投递等带 `trigger_agent` 的
        # 推送直接走到这里，不经过 `push_human_message` 的入站判定；暂停（含租户自限）时
        # 内容照常落库、但不开回合——否则一个被暂停的频道照样能为每条定时提醒烧一轮模型。
        if not bypass_channel_quota:
            try:
                paused = await channel_quota.is_paused(chat_key)
            except Exception as e:  # noqa: BLE001 - 判定失败一律放行，不能让闸门自己变成故障点
                logger.warning(f"频道额度暂停判定失败，按未暂停处理: chat_key={chat_key} | {e}")
                paused = False
            if paused:
                logger.debug(f"频道 {chat_key} 今日额度已用完（暂停中），跳过 Agent 调度")
                return

        current_time = time.time()

        # 触发记账。位置在上面几道闸门**之后**：被挡下的调度不会跑任何回合，记下它反而会让
        # 「这一批触发全是运维人员」这个判据失真（一条被拦下的系统推送会把批次降级成不可用）。
        # 两家共用一本：D6 或 SSH 网关任一开着都记。两个都关着时不记——`open_round` 关着时
        # 也不取，记下的条目只会在本子里放到过期，开关一打开，每个频道的第一轮都会因为
        # 「批次里有过期条目」被拒一次。
        # 函数内导入：本模块里 `config` 这个名字到处是局部的频道配置，模块级导入全局配置会被遮蔽
        from nekro_agent.core.config import config as _global_config

        if _global_config.OPS_AGENT_TOOLS_ENABLED or _global_config.SSH_GATEWAY_ENABLED:
            round_trigger_book.record(chat_key, trigger or RoundTrigger(kind=TRIGGER_KIND_OTHER))

        # 插件日志用的「这一轮是谁叫的 Bot」另记一份，不受运维工具开关影响（见
        # `services/agent/round_trigger_accounts.py`）。只记账号或触发种类，纯内存，失败不影响调度
        try:
            note_pending_trigger(chat_key, trigger or RoundTrigger(kind=TRIGGER_KIND_OTHER))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"记录触发者失败（已忽略）: chat_key={chat_key} | {e}")

        # 更新待处理消息和防抖计时器
        self.pending_messages[chat_key] = message
        self.debounce_timers[chat_key] = current_time

        # 如果已有正在执行的任务，直接返回
        if chat_key in self.running_tasks and not self.running_tasks[chat_key].done():
            return

        # 创建防抖任务
        asyncio.create_task(self._debounce_task(chat_key, current_time, ctx))

    async def _debounce_task(self, chat_key: str, start_time: float, ctx: Optional[AgentCtx] = None):
        """防抖任务处理

        Args:
            chat_key (str): 频道标识
            start_time (float): 任务开始时间
        """
        db_chat_channel = await DBChatChannel.get(chat_key=chat_key)
        config = await db_chat_channel.get_effective_config()
        # 等待防抖时间
        await asyncio.sleep(config.AI_DEBOUNCE_WAIT_SECONDS)

        # 检查是否在防抖期间有新消息
        if start_time != self.debounce_timers[chat_key]:
            return

        # 获取最终要处理的消息
        final_message = self.pending_messages.pop(chat_key, None)
        if not final_message:
            return

        # 创建新的agent任务
        task = asyncio.create_task(
            self._run_chat_agent_task(
                chat_key=chat_key,
                message=final_message if not final_message.is_empty() else None,
                ctx=ctx,
            ),
        )
        self.running_tasks[chat_key] = task

    async def _run_chat_agent_task(self, chat_key: str, message: Optional[ChatMessage] = None, ctx: Optional[AgentCtx] = None):
        """执行agent任务"""
        from nekro_agent.services import instance_concurrency
        from nekro_agent.services.agent.failure_feedback import (
            NOTICE_ROUND_ERROR,
            NOTICE_ROUND_NO_CODE,
            NOTICE_ROUND_PROVIDER_REFUSAL,
            NOTICE_ROUND_STALLED,
            NOTICE_ROUND_TIMEOUT,
            NOTICE_ROUND_UNFINISHED,
            describe_llm_failure,
            failure_notice_throttle,
        )
        from nekro_agent.services.agent.run_agent import (
            AgentNoExecutableCodeError,
            AgentProviderRefusalError,
            AgentRoundStalledError,
            AgentRoundUnfinishedError,
            AllLLMRequestsFailedError,
            run_agent,
        )
        from nekro_agent.services.chat.universal_chat_service import universal_chat_service
        from nekro_agent.services.instance_concurrency import InstanceConcurrencyRejected
        from nekro_agent.services.system_broadcast import (
            AgentActiveEvent,
            AgentRuntimeStatusEvent,
            publish_system_event,
        )

        db_channel = await DBChatChannel.get_channel(chat_key=chat_key)
        preset = await db_channel.get_preset()
        config = await db_channel.get_effective_config()
        preset_id: Optional[int] = preset.id if hasattr(preset, "id") else None  # type: ignore[union-attr]
        preset_name: Optional[str] = preset.name if hasattr(preset, "name") else None  # type: ignore[union-attr]
        adapter = None

        try:
            try:
                adapter = await adapter_utils.get_adapter_for_chat(chat_key)
            except AdapterUnavailableError as e:
                logger.warning(f"[message_service] 频道 {chat_key} 适配器不可用，跳过 Agent 任务: {e}")
                return

            logger.info(f"Message From {chat_key} is ToMe, Running Chat Agent...")

            # 计算单次 Agent 任务的兜底总超时，防止底层 httpx/网络层超时失效时任务永久挂起占用频道锁
            _per_round_timeout = (config.AI_GENERATE_TIMEOUT or 180) + 60  # 每轮预算（含 sandbox 执行缓冲）
            _max_total_timeout = _per_round_timeout * (config.AI_SCRIPT_MAX_RETRY_TIMES + 1) + 120
            started_at = int(time.time() * 1000)

            # 广播 Agent 开始处理
            try:
                await publish_system_event(
                    AgentActiveEvent(
                        chat_key=chat_key,
                        active=True,
                        channel_name=db_channel.channel_name,
                        chat_type=db_channel.channel_type,
                        preset_id=preset_id,
                        preset_name=preset_name,
                        started_at=started_at,
                    )
                )
            except Exception as _e:
                logger.warning(f"[message_service] 广播 AgentActive 开始事件失败: {_e}")

            # 设置处理emoji；chat_key 用于让多账号适配器定位消息归属的账号连接。
            # 原先这里按异常文案 "No OneBot V11 bot instance found" 做兜底，该异常已随
            # onebot_v11 多实例改造删除（现为 BotInstanceOfflineError，且适配器内部已自行吞掉），
            # 文案匹配不再有任何意义。设置表情只是可选的视觉反馈，失败不应中断 Agent 任务，
            # 故统一降级为记录日志。
            if message and adapter.config.SESSION_PROCESSING_WITH_EMOJI and message.message_id:
                try:
                    await adapter.set_message_reaction(message.message_id, True, chat_key)
                except Exception as _e:
                    logger.warning(f"[message_service] 设置 emoji 失败: {_e}")

            async def _notify_round_failure(notice: str) -> None:
                """给用户一条可见回执

                四个前提缺一不可：开关开着、这个频道不是确定发不出去、冷却窗口过了、
                发送本身不能把 Agent 的失败变成第二个异常（适配器此刻很可能也不可用）。
                """
                if not config.SESSION_ENABLE_FAILED_LLM_FEEDBACK:
                    return
                blocked = channel_delivery_blocks.get(chat_key)
                if blocked is not None:
                    # 放在冷却闸门之前：注定发不出去的这条不该白占掉冷却窗口，
                    # 否则频道恢复后的第一次真实失败会被静默压掉
                    logger.info(
                        f"[message_service] 频道 {chat_key} 当前确定发不出去（{blocked.value}），不发失败回执",
                    )
                    return
                if not failure_notice_throttle.should_send(chat_key, config.SESSION_FAILED_FEEDBACK_COOLDOWN_SECONDS):
                    logger.info(f"[message_service] 频道 {chat_key} 的失败回执处于冷却窗口内，本次只记日志不发送")
                    return
                try:
                    await universal_chat_service.send_operation_message(chat_key, notice)
                except Exception as _e:
                    logger.warning(f"[message_service] 频道 {chat_key} 的失败回执发送失败: {_e}")

            try:
                async with asyncio.timeout(_max_total_timeout):
                    try:
                        await run_agent(chat_key=chat_key, chat_message=message, ctx=ctx)
                    except InstanceConcurrencyRejected as e:
                        # 实例并发配额耗尽。这里必须给出可见结果：静默返回在聊天里的表现是
                        # "机器人偶尔就是不理人"，排查时既没有日志线索也没有用户反馈。
                        # 提示本身按实例做冷却，避免过载时把提示刷成新的洪水。
                        logger.warning(f"[message_service] 频道 {chat_key} 的执行被实例并发配额拒绝: {e.detail}")
                        if instance_concurrency.scheduler.should_notify_rejection(e.instance_key):
                            await universal_chat_service.send_operation_message(
                                chat_key,
                                "当前实例同时处理的请求太多啦，这条先跳过了，稍后再说一次吧~ QwQ",
                            )
                    except Exception as e:
                        logger.exception(f"执行失败: {e}")
                        logger.error("Failed to Run Chat Agent.")
                        # 实例离线时 Agent 在取自身信息那一步就抛，根本没走到适配器的发送，
                        # 所以要在这里补记一次，紧接着的失败回执才会被挡住
                        if isinstance(e, ChannelUndeliverableError):
                            channel_delivery_blocks.mark(chat_key, e.undeliverable_reason, str(e))
                        # 改造前只有 `AllLLMRequestsFailedError` 会有回执，其余异常（沙盒侧、
                        # 多模态结果解析 ValueError 等）一律只留一行 ERROR —— 对用户就是装死。
                        # 现在每一类都有回执，文案按类别区分，并统一过会话冷却闸门。
                        # 两条**提前收尾**的路径各有各的回执：「模型没给代码」与「同一个错重复到底」
                        # 对用户意味着不同的下一步，都退回 `NOTICE_ROUND_UNFINISHED`（潜台词是
                        # 「再说一次也许就好」）会把用户往错的方向引。三条异常互不继承，
                        # 所以这条 isinstance 链的顺序不影响判定结果。
                        if isinstance(e, AllLLMRequestsFailedError):
                            await _notify_round_failure(describe_llm_failure(e.kinds))
                        elif isinstance(e, AgentNoExecutableCodeError):
                            await _notify_round_failure(NOTICE_ROUND_NO_CODE)
                        elif isinstance(e, AgentProviderRefusalError):
                            # 渠道把「拒绝受理」当正文返回。不能退回 `NOTICE_ROUND_NO_CODE`：
                            # 那条的潜台词是「模型没写代码」，而这一类请求压根没到模型那儿，
                            # 用户需要知道的是「换个说法」而不是「再说一次」。
                            await _notify_round_failure(NOTICE_ROUND_PROVIDER_REFUSAL)
                        elif isinstance(e, AgentRoundStalledError):
                            await _notify_round_failure(NOTICE_ROUND_STALLED)
                        elif isinstance(e, AgentRoundUnfinishedError):
                            await _notify_round_failure(NOTICE_ROUND_UNFINISHED)
                        else:
                            await _notify_round_failure(NOTICE_ROUND_ERROR)
            except TimeoutError:
                logger.error(
                    f"[message_service] 频道 {chat_key} Agent 任务超过兜底超时 {_max_total_timeout}s，强制终止以释放频道锁"
                )
                # 兜底超时同样不能静默：这条分支改造前只有一行 ERROR
                await _notify_round_failure(NOTICE_ROUND_TIMEOUT)
        finally:
            # 清理任务状态
            if chat_key in self.running_tasks:
                del self.running_tasks[chat_key]

            final_message = self.pending_messages.pop(chat_key, None)
            self.debounce_timers.pop(chat_key, None)

            # 取消处理emoji（如果设置过）；这里处于 finally 清理路径，任何失败都不能影响后续清理。
            # 同上：旧的 "No OneBot V11 bot instance found" 文案匹配分支已随多实例改造失效，删除。
            if adapter and adapter.config.SESSION_PROCESSING_WITH_EMOJI and message and message.message_id:
                try:
                    await adapter.set_message_reaction(message.message_id, False, chat_key)
                except Exception as _e:
                    logger.warning(f"[message_service] 取消 emoji 失败: {_e}")

            # 广播 Agent 处理结束
            try:
                await publish_system_event(
                    AgentActiveEvent(
                        chat_key=chat_key,
                        active=False,
                        channel_name=db_channel.channel_name,
                        chat_type=db_channel.channel_type,
                        preset_id=preset_id,
                        preset_name=preset_name,
                        started_at=started_at,
                    )
                )
            except Exception as _e:
                logger.warning(f"[message_service] 广播 AgentActive 结束事件失败: {_e}")
            try:
                await publish_system_event(
                    AgentRuntimeStatusEvent(
                        chat_key=chat_key,
                        active=False,
                        channel_name=db_channel.channel_name,
                        chat_type=db_channel.channel_type,
                        preset_id=preset_id,
                        preset_name=preset_name,
                        started_at=started_at,
                        updated_at=int(time.time() * 1000),
                    )
                )
            except Exception as _e:
                logger.warning(f"[message_service] 广播 AgentRuntime 结束事件失败: {_e}")

            # 如果有待处理消息，创建新的任务处理最后一条消息
            if final_message:
                new_task = asyncio.create_task(self._run_chat_agent_task(chat_key=chat_key, message=final_message, ctx=ctx))
                self.running_tasks[chat_key] = new_task

    async def push_human_message(
        self,
        message: ChatMessage,
        user: Optional[DBUser] = None,
        trigger_agent: bool = False,
        db_chat_channel: Optional[DBChatChannel] = None,
        self_sent: bool = False,
    ):
        """推送人类用户消息

        `self_sent` 为真表示这条是 Bot 账号自己由其他程序发出的（OneBot 的 `message_sent`）：
        只落库、广播，不调 `on_user_message`，也不进后面任何触发判断。两边都接在同一账号上时，
        这里只要漏出一条触发，NA 回话会让对方再回话，就是死循环；好感、配额、随机搭话、关键词
        也都在这条返回之后，一并绕开。
        """
        # 清洗必须排在最前面：下面要用 `message.chat_key` 查频道，而触发判断、落库、
        # 广播都应当基于同一份数据——否则落库的那行（被 pre_save 清洗过）与广播出去的
        # 那份（还带 NUL）不一致
        message = _without_nul(message)
        db_chat_channel = db_chat_channel or await DBChatChannel.get_channel(chat_key=message.chat_key)

        # 闸门必须排在本函数最前面，不能只挡 `schedule_agent_task`：
        # **本函数自己就会把消息发出去**。配额用尽时（下方 `should_notify_quota_exhausted` 分支）
        # 它直接调 `adapter.forward_message` 通报"今日回复配额已用完"，那条路径既不经过
        # `collect_message` 的入站闸门，也不经过 `send_agent_message` 的出站闸门——
        # 于是一个被停用的实例被戳一戳时仍会当着群里的面说话。
        # 通知事件（戳一戳、群成员增减）正是这样绕开 collector 直接调进来的
        # （`adapters/onebot_v11/matchers/notice.py`、`adapters/minecraft/matchers/notice.py`）。
        # 顺带一并挡住的还有插件的 `on_user_message` 挂载点与消息落库：被停用的实例不该再
        # 触发任何插件回调，也不该继续往会话里堆记录。
        if not await DBAdapterInstance.is_instance_active(db_chat_channel.adapter_key, db_chat_channel.instance_key):
            logger.debug(f"实例或其所属租户已停用，丢弃入站消息: chat_key={message.chat_key}")
            return

        config = await db_chat_channel.get_effective_config()
        preset = await db_chat_channel.get_preset()

        if not await self._message_validation_check(message):
            logger.warning("消息校验失败，跳过本次处理...")
            return

        if check_forbidden_message(message.content_text, config):
            logger.info(f"消息 {message.content_text} 被禁止，跳过本次处理...")
            return

        signal = MsgSignal.CONTINUE
        ctx: Optional[AgentCtx] = None
        if not self_sent:
            ctx = await AgentCtx.create_by_chat_key(chat_key=message.chat_key)
            ctx._trigger_db_user = user  # noqa: SLF001

            signal = await plugin_collector.handle_on_user_message(ctx, message)
            if signal == MsgSignal.BLOCK_ALL:
                logger.info(f"用户消息 {message.content_text} 被插件阻止响应，跳过本次处理...")
                return

        # 落库的文本补上 OneBot 视频占位；上面的伪造检查、禁词与下面的触发判断都用不带占位的原文
        await self._persist_human_message(message, db_chat_channel)

        if self_sent or ctx is None:
            return

        should_ignore = (user and user.is_prevent_trigger) or (user and not user.is_active)

        # 检查是否需要触发回复
        explicit_triggered = (
            trigger_agent
            or signal == MsgSignal.FORCE_TRIGGER
            or preset.name in message.content_text
            or message.is_tome
        )
        random_triggered = random_chat_check(config)
        content_triggered = check_content_trigger(message.content_text, config)
        should_trigger = explicit_triggered or random_triggered or content_triggered
        should_notify_quota_exhausted = explicit_triggered or content_triggered

        # 私聊纯媒体消息（无文本内容）不触发 AI 回复，仅记录；群聊仍允许直接触发
        _MEDIA_TYPES = {"file", "image", "voice", "video"}
        _MEDIA_OR_AT_TYPES = _MEDIA_TYPES | {"at"}
        is_media_only_message = all(seg.type in _MEDIA_OR_AT_TYPES for seg in message.content_data)
        if should_trigger and db_chat_channel.chat_type == ChatType.PRIVATE and is_media_only_message:
            if any(seg.type in _MEDIA_TYPES for seg in message.content_data):
                logger.info(f"私聊纯媒体消息，仅记录不触发: {message.content_text[:32]}...")
                should_trigger = False

        # 好感度软门：只作用于非明示触发（随机搭话 / 关键词命中）——@、私聊、喊名字、
        # trigger_agent、FORCE_TRIGGER 这类直接召唤永远放行；被降的是环境里的自发参与。
        # `affection_score_of` 自身 fail-open（读不到按 0 分 → 意愿 100%，不拦），
        # 好感度服务整层挂掉时本判断等价于不启用。
        if (
            should_trigger
            and not explicit_triggered
            and config.AFFECTION_ENABLE_SYSTEM
            and config.AFFECTION_ENABLE_REPLY_GATE
        ):
            from nekro_agent.services.affection.prompt import affection_score_of
            from nekro_agent.services.affection.stage import reply_eagerness

            score = await affection_score_of(db_chat_channel, message.platform_userid, config)
            eagerness = reply_eagerness(score)
            if random.random() * 100 >= eagerness:
                logger.info(
                    f"好感度软门拦下自发触发: {message.platform_userid} score={score} eagerness={eagerness}%"
                )
                should_trigger = False

        if not should_ignore and should_trigger:
            if not db_chat_channel.is_active:
                logger.info(f"聊天频道 {message.chat_key} 已被禁用，跳过本次处理...")
                return

            if db_chat_channel.observe_mode:
                logger.info(f"聊天频道 {message.chat_key} 处于旁观模式，跳过 Agent 触发...")
                return

            if signal not in [MsgSignal.CONTINUE, MsgSignal.FORCE_TRIGGER]:
                logger.info(f"用户消息 {message.content_text} 被插件阻止触发，跳过本次处理...")
                return

            # 配额豁免检查（用户白名单/管理员）
            _is_quota_exempt = await is_quota_exempt(message, config)

            # 统一闸门：平台方频道额度先判，再判租户自限（AI_CHAT_DAILY_REPLY_LIMIT，
            # 按回复回合数）。两层都按 CHANNEL_QUOTA_TIMEZONE 的零点换天；豁免的人
            # （白名单 / 超级用户）不受约束，但用量照常计入。
            if not _is_quota_exempt:
                result = await channel_quota.gate(db_chat_channel)
                if result.blocked:
                    if result.reason == "platform" and result.status is not None:
                        detail = f"{result.status.used}/{result.status.allowed} {result.status.unit}"
                    elif result.reason == "self_hourly" and result.self_status is not None:
                        detail = f"{result.self_status.hourly_used}/{result.self_status.hourly_limit}"
                    elif result.self_status is not None:
                        detail = f"{result.self_status.used}/{result.self_status.allowed}"
                    else:
                        detail = "-"
                    logger.info(
                        f"频道 {message.chat_key} 配额已用完 (reason={result.reason}, {detail})，跳过回复",
                    )
                    if should_notify_quota_exhausted:
                        if result.reason == "platform":
                            if result.status is not None:
                                await channel_quota.send_pause_notice_once(db_chat_channel, result.status)
                        elif result.self_status is not None:
                            await channel_quota.send_self_limit_notice_once(
                                db_chat_channel,
                                result.self_status,
                                result.reason,
                            )
                    return

            # 本函数已经判过统一闸门（平台额度 + 租户自限），豁免的人也要能过，
            # 所以让调度跳过它自己的闸门
            await self.schedule_agent_task(
                message=message,
                ctx=ctx,
                bypass_channel_quota=True,
                trigger=await self._human_trigger(message, db_chat_channel),
            )

    async def _human_trigger(self, message: ChatMessage, db_chat_channel: DBChatChannel) -> RoundTrigger:
        """入站人类消息的触发记录

        `operator_trusted` 必须在这里算：来源可信与否要读**当前事件所在连接**的握手记录，
        离开这个入站任务就算不出来了（见 `services/user/super_users.py: is_trusted_super_user_source`
        的调用约束）。`source_trusted` 是同一个来源判定的原值，只是不要求命中运维名单——
        网关有自己的所有者名单与用户白名单。

        来源只查一次：D6 开着且命中运维名单，或网关开着且命中「所有者 ∪ 用户白名单」，
        任一成立才查。两边都命中时也共用这一次结果；都不命中就一次都不查，存量部署
        不为没人用的功能每条消息多付一次查库、多一个故障点。
        """
        sender_id = str(message.platform_userid or "") or str(message.sender_id or "")
        adapter_key = str(db_chat_channel.adapter_key or "")
        instance_key = str(db_chat_channel.instance_key or "")

        # 函数内导入配置：`core.config` 在导入期会读环境与配置目录，模块级导入会拖慢并搅乱
        # 只 import 这个模块的测试
        from nekro_agent.core.config import config as _config

        operator_matched = False
        if sender_id and bool(_config.OPS_AGENT_TOOLS_ENABLED):
            policy = OpsAgentPolicy.from_config(_config)
            operator_matched = is_operator(policy, adapter_key, sender_id)

        gateway_matched = False
        if sender_id and bool(_config.SSH_GATEWAY_ENABLED):
            entries = [*_config.SSH_GATEWAY_OWNER_ACCOUNTS, *_config.SSH_GATEWAY_USER_ACCOUNTS]
            gateway_matched = matches_super_users(adapter_key, sender_id, entries)

        source_trusted = False
        if operator_matched or gateway_matched:
            source_trusted = await is_trusted_super_user_source(adapter_key, instance_key)

        return RoundTrigger(
            kind=TRIGGER_KIND_HUMAN,
            adapter_key=adapter_key,
            instance_key=instance_key,
            sender_id=sender_id,
            operator_trusted=operator_matched and source_trusted,
            source_trusted=source_trusted,
        )

    async def record_human_message(
        self,
        message: ChatMessage,
        db_chat_channel: Optional[DBChatChannel] = None,
    ) -> None:
        """只记录并广播人类用户消息，不触发插件和 Agent。

        命令消息会在适配器收集阶段被命令系统消费，不能复用
        `push_human_message()` 的完整流程，否则 Web 私聊命令会因为
        `is_tome=True` 被再次调度给 Agent。

        实例闸门与 `push_human_message` 同一处：被停用的实例不该继续往会话里堆记录，
        也不该把消息广播给实时流。NUL 清洗也与它同一处、同一个位置（理由见 `_without_nul`）。
        """
        message = _without_nul(message)
        db_chat_channel = db_chat_channel or await DBChatChannel.get_channel(chat_key=message.chat_key)

        if not await DBAdapterInstance.is_instance_active(db_chat_channel.adapter_key, db_chat_channel.instance_key):
            logger.debug(f"实例或其所属租户已停用，丢弃入站消息记录: chat_key={message.chat_key}")
            return

        if not await self._message_validation_check(message):
            logger.warning("消息校验失败，跳过本次处理...")
            return

        await self._persist_human_message(message, db_chat_channel)

    async def _persist_human_message(
        self,
        message: ChatMessage,
        db_chat_channel: DBChatChannel,
    ) -> None:
        """持久化并广播人类用户消息"""
        # 落库的文本补上 OneBot 视频占位；上面的伪造检查、禁词与下面的触发判断都用不带占位的原文
        stored_text = merge_video_placeholders(message.content_text, message.content_data)
        stored_message = message if stored_text == message.content_text else message.model_copy(update={"content_text": stored_text})

        content_data = [o.model_dump() for o in message.content_data]

        # 添加聊天记录
        await DBChatMessage.create(
            message_id=message.message_id,
            sender_id=message.sender_id,
            sender_name=message.sender_name,
            sender_nickname=message.sender_nickname,
            adapter_key=message.adapter_key,
            instance_key=message.instance_key,
            platform_userid=message.platform_userid,
            is_tome=message.is_tome,
            is_recalled=message.is_recalled,
            chat_key=message.chat_key,
            chat_type=message.chat_type,
            content_text=stored_text,
            content_data=json.dumps(content_data, ensure_ascii=False),
            raw_cq_code=message.raw_cq_code,
            ext_data=json.dumps(message.ext_data, ensure_ascii=False),
            send_timestamp=int(time.time()),  # 使用处理后的时间戳
        )

        # 通知记忆调度器（非阻塞）
        asyncio.create_task(
            _notify_memory_scheduler(
                channel=db_chat_channel,
                content_length=len(stored_text),
            ),
        )

        # 广播消息到所有订阅者：与库里的那一行一致
        await message_broadcaster.publish(message.chat_key, stored_message)

        # 同时广播频道更新事件，使频道列表实时更新（新消息会将频道移到最上面）
        await channel_broadcaster.publish_update(
            event_type="updated",
            chat_key=message.chat_key,
            channel_name=db_chat_channel.channel_name,
            is_active=db_chat_channel.is_active,
            status=db_chat_channel.channel_status,
        )

    async def push_bot_message(
        self,
        chat_key: str,
        agent_messages: Union[str, List[AgentMessageSegment]],
        plt_response: Optional[PlatformSendResponse] = None,
        db_chat_channel: Optional[DBChatChannel] = None,
        ref_msg_id: Optional[str] = None,
        normalize_at_markup: bool = True,
    ):
        """推送机器人消息"""
        logger.info(f"Pushing Bot Message To Chat {chat_key}")
        db_chat_channel = db_chat_channel or await DBChatChannel.get_channel(chat_key=chat_key)
        preset = await db_chat_channel.get_preset()

        if isinstance(agent_messages, str):
            agent_messages = [AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content=agent_messages)]

        if normalize_at_markup:
            for msg in agent_messages:
                if msg.type == AgentMessageSegmentType.TEXT:
                    msg.content = normalize_malformed_at_markup(msg.content)

        content_text = convert_agent_message_to_prompt(agent_messages)

        content_data = []
        for msg in agent_messages:
            if msg.type in (AgentMessageSegmentType.FILE, AgentMessageSegmentType.IMAGE):
                # 使用magic库检测文件MIME类型
                file_path = Path(msg.content)
                if file_path.exists():
                    mime_type = magic.from_file(str(file_path), mime=True)
                    # 复制文件到uploads目录
                    local_path, file_name = await copy_to_upload_dir(
                        str(file_path),
                        file_name=file_path.name,
                        from_chat_key=chat_key,
                    )

                    if msg.type == AgentMessageSegmentType.IMAGE or mime_type.startswith("image/"):
                        content_data.append(
                            {
                                "type": "image",
                                "text": "",
                                "file_name": file_name,
                                "local_path": local_path,
                                "remote_url": "",
                            },
                        )
                    else:
                        content_data.append(
                            {
                                "type": "file",
                                "text": f"[File: {file_name}]",
                                "file_name": file_name,
                                "local_path": local_path,
                                "remote_url": "",
                            },
                        )
            elif msg.type == AgentMessageSegmentType.TEXT:
                # 处理 AI 生成的 @提及 [@id:qq_id@]
                text = msg.content
                matches = list(AT_MARKUP_PATTERN.finditer(text))

                if matches:
                    # 有 AI 标记，需要解析并替换
                    last_end = 0
                    for match in matches:
                        # 添加标记前的文本
                        if match.start() > last_end:
                            text_before = text[last_end : match.start()]
                            if text_before.strip():
                                content_data.append(
                                    {
                                        "type": "text",
                                        "text": text_before,
                                    }
                                )

                        # 从用户管理里查询昵称
                        qq_id = match.group("uid")
                        marked_nickname = match.group("nickname")
                        user = await DBUser.get_by_union_id(
                            adapter_key=db_chat_channel.adapter_key,
                            platform_userid=qq_id,
                        )

                        nickname = marked_nickname or (user.username if user else f"User_{qq_id}")
                        content_data.append(
                            {
                                "type": "at",
                                "text": f"@{nickname}",
                                "target_platform_userid": qq_id,
                                "target_nickname": nickname,
                            }
                        )

                        last_end = match.end()

                    # 添加最后的文本
                    if last_end < len(text):
                        text_after = text[last_end:]
                        if text_after.strip():
                            content_data.append(
                                {
                                    "type": "text",
                                    "text": text_after,
                                }
                            )
                else:
                    # 没有 AI 标记，直接添加
                    content_data.append(
                        {
                            "type": "text",
                            "text": text,
                        },
                    )

        platform_userid = ""
        try:
            adapter = adapter_utils.get_adapter(db_chat_channel.adapter_key)
        except AdapterUnavailableError as e:
            logger.warning(f"[message_service] 写入 bot 消息时适配器不可用，使用空 platform_userid: {e}")
        else:
            platform_userid = (await adapter.get_self_info(chat_key)).user_id

        await DBChatMessage.create(
            message_id=plt_response.message_id if plt_response and plt_response.message_id else "",
            sender_id=-1,
            sender_name=preset.name,
            sender_nickname=preset.name,
            adapter_key=db_chat_channel.adapter_key,
            instance_key=db_chat_channel.instance_key,
            platform_userid=platform_userid,
            is_tome=0,
            is_recalled=False,
            chat_key=chat_key,
            chat_type=db_chat_channel.chat_type,
            content_text=content_text,
            content_data=json.dumps(content_data, ensure_ascii=False),
            raw_cq_code="",
            ext_data=json.dumps(PlatformMessageExt(ref_msg_id=ref_msg_id or "").model_dump(), ensure_ascii=False),
            send_timestamp=int(time.time()),
        )

        # 通知记忆调度器（非阻塞）
        asyncio.create_task(
            _notify_memory_scheduler(
                channel=db_chat_channel,
                content_length=len(content_text),
            ),
        )

        # 广播消息到所有订阅者 - 构建 ChatMessage 对象用于广播
        broadcast_message = ChatMessage(
            message_id=plt_response.message_id if plt_response and plt_response.message_id else "",
            sender_id="-1",
            sender_name=preset.name,
            sender_nickname=preset.name,
            adapter_key=db_chat_channel.adapter_key,
            instance_key=db_chat_channel.instance_key,
            platform_userid=platform_userid,
            is_tome=0,
            is_recalled=False,
            chat_key=chat_key,
            chat_type=ChatType(db_chat_channel.chat_type),
            content_text=content_text,
            content_data=content_data,
            raw_cq_code="",
            ext_data={},
            send_timestamp=int(time.time()),
        )
        await message_broadcaster.publish(chat_key, broadcast_message)

        # 同时广播频道更新事件，使频道列表实时更新
        await channel_broadcaster.publish_update(
            event_type="updated",
            chat_key=chat_key,
            channel_name=db_chat_channel.channel_name,
            is_active=db_chat_channel.is_active,
            status=db_chat_channel.channel_status,
        )

    async def push_system_message(
        self,
        chat_key: str,
        agent_messages: Union[str, List[AgentMessageSegment]],
        trigger_agent: bool = False,
        db_chat_channel: Optional[DBChatChannel] = None,
    ):
        """推送系统消息"""
        logger.info(f"Pushing System Message To Chat {chat_key}")
        db_chat_channel = db_chat_channel or await DBChatChannel.get_channel(chat_key=chat_key)

        # 与 `push_human_message` 同层同理由：定时任务、通知事件、沙盒方法回执都从这里进来，
        # 都绕开 collector。挡在最前面才能同时挡住插件的 `on_system_message` 挂载点与消息落库，
        # 只挡 `schedule_agent_task` 的话这两样对被停用的实例照样发生。
        if not await DBAdapterInstance.is_instance_active(db_chat_channel.adapter_key, db_chat_channel.instance_key):
            logger.debug(f"实例或其所属租户已停用，丢弃系统消息: chat_key={chat_key}")
            return

        if isinstance(agent_messages, str):
            agent_messages = [AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content=agent_messages)]

        content_text = convert_agent_message_to_prompt(agent_messages)

        ctx: AgentCtx = await AgentCtx.create_by_chat_key(chat_key=chat_key)

        signal = await plugin_collector.handle_on_system_message(ctx, content_text)
        if signal == MsgSignal.BLOCK_ALL:
            logger.info(f"系统消息 {content_text} 被插件阻止响应，跳过本次处理...")
            return

        await DBChatMessage.create(
            message_id="",
            sender_id=-1,
            sender_name="SYSTEM",
            sender_nickname="SYSTEM",
            adapter_key=db_chat_channel.adapter_key,
            instance_key=db_chat_channel.instance_key,
            platform_userid="0",
            is_tome=1 if trigger_agent else 0,
            is_recalled=False,
            chat_key=chat_key,
            chat_type=db_chat_channel.chat_type,
            content_text=content_text,
            content_data=json.dumps([], ensure_ascii=False),
            raw_cq_code="",
            ext_data={},
            send_timestamp=int(time.time()),
        )

        # 广播消息到所有订阅者 - 构建 ChatMessage 对象用于广播
        broadcast_message = ChatMessage(
            message_id="",
            sender_id="-1",
            sender_name="SYSTEM",
            sender_nickname="SYSTEM",
            adapter_key=db_chat_channel.adapter_key,
            instance_key=db_chat_channel.instance_key,
            platform_userid="0",
            is_tome=1 if trigger_agent else 0,
            is_recalled=False,
            chat_key=chat_key,
            chat_type=ChatType(db_chat_channel.chat_type),
            content_text=content_text,
            content_data=[],
            raw_cq_code="",
            ext_data={},
            send_timestamp=int(time.time()),
        )
        await message_broadcaster.publish(chat_key, broadcast_message)

        # 同时广播频道更新事件，使频道列表实时更新
        await channel_broadcaster.publish_update(
            event_type="updated",
            chat_key=chat_key,
            channel_name=db_chat_channel.channel_name,
            is_active=db_chat_channel.is_active,
            status=db_chat_channel.channel_status,
        )

        if trigger_agent or signal == MsgSignal.FORCE_TRIGGER:
            await self._trigger_after_push(
                chat_key,
                db_chat_channel,
                ctx,
                signal,
                f"系统消息 {content_text}",
                trigger_kind=TRIGGER_KIND_SYSTEM_PUSH,
            )

    async def _trigger_after_push(
        self,
        chat_key: str,
        db_chat_channel: DBChatChannel,
        ctx: AgentCtx,
        signal: MsgSignal,
        label: str,
        *,
        trigger_kind: str,
    ) -> None:
        """插件推送之后触发 Agent 的闸门：频道禁用、旁观模式、插件阻止触发时都不触发

        `trigger_kind` 区分「系统消息推来的」与「外部内容推来的」：两者都不是人类说的，
        运维工具因此都不可用，但审计与排查时要分得清是哪一种。
        """
        if not db_chat_channel.is_active:
            logger.info(f"聊天频道 {chat_key} 已被禁用，跳过本次处理...")
            return

        if db_chat_channel.observe_mode:
            logger.info(f"聊天频道 {chat_key} 处于旁观模式，跳过 Agent 触发...")
            return
        if signal not in [MsgSignal.CONTINUE, MsgSignal.FORCE_TRIGGER]:
            logger.info(f"{label} 被插件阻止触发，跳过本次处理...")
            return
        await self.schedule_agent_task(
            chat_key=chat_key,
            ctx=ctx,
            trigger=RoundTrigger(
                kind=trigger_kind,
                adapter_key=str(db_chat_channel.adapter_key or ""),
                instance_key=str(db_chat_channel.instance_key or ""),
            ),
        )

    async def push_untrusted_message(
        self,
        chat_key: str,
        result: UntrustedToolResult,
        trigger_agent: bool = False,
        db_chat_channel: Optional[DBChatChannel] = None,
    ) -> None:
        """插件主动推送外部内容（CC 的执行结果、GitHub 事件……），可选触发 Agent

        `push_system_message` 落的是 SYSTEM 消息，而提示词要求模型信任 SYSTEM 消息，外部内容不能走那条路。
        这里落库与 `push_tool_result` 相同：身份不是 SYSTEM，正文只进不可信块、渲染时用当轮令牌包裹，
        `content_text` 只放可信摘要。插件自己写的引导语放 `summary`，外部正文放 `body`。

        其余与 `push_system_message` 一致：实例停用即丢弃，经过 `on_system_message` 挂载点（挂载点只看得到
        可信摘要），触发时同样受频道禁用、旁观模式、插件阻止的约束。
        """
        db_chat_channel = db_chat_channel or await DBChatChannel.get_channel(chat_key=chat_key)
        if not await DBAdapterInstance.is_instance_active(db_chat_channel.adapter_key, db_chat_channel.instance_key):
            logger.debug(f"实例或其所属租户已停用，丢弃推送的外部内容: chat_key={chat_key}")
            return

        if len(result.body) > MAX_TOOL_RESULT_BODY_CHARS:
            # 与 RPC 出口同一条硬底线，见 `MAX_TOOL_RESULT_BODY_CHARS`
            result = replace(result, body=result.body[:MAX_TOOL_RESULT_BODY_CHARS] + TRUNCATION_MARK)
        if result.history_body is not None and len(result.history_body) > MAX_TOOL_RESULT_BODY_CHARS:
            result = replace(result, history_body=result.history_body[:MAX_TOOL_RESULT_BODY_CHARS] + TRUNCATION_MARK)

        ctx: AgentCtx = await AgentCtx.create_by_chat_key(chat_key=chat_key)
        signal = await plugin_collector.handle_on_system_message(ctx, result.summary)
        if signal == MsgSignal.BLOCK_ALL:
            logger.info(f"推送的 {result.kind} 被插件阻止响应，跳过本次处理...")
            return

        await self._record_untrusted_result(chat_key, db_chat_channel, result)
        await channel_broadcaster.publish_update(
            event_type="updated",
            chat_key=chat_key,
            channel_name=db_chat_channel.channel_name,
            is_active=db_chat_channel.is_active,
            status=db_chat_channel.channel_status,
        )

        if trigger_agent or signal == MsgSignal.FORCE_TRIGGER:
            await self._trigger_after_push(
                chat_key,
                db_chat_channel,
                ctx,
                signal,
                f"推送的 {result.kind}",
                trigger_kind=TRIGGER_KIND_UNTRUSTED_PUSH,
            )

    async def push_tool_result(self, chat_key: str, result: UntrustedToolResult) -> None:
        """落一条不可信的工具结果

        身份既不是 SYSTEM（提示词要求模型信任它）也不是 Bot（会占回复配额）。正文存在消息段里，
        不带令牌，渲染时才包裹；`content_text` 只放可信摘要，记忆整理与好感评估读的是它。
        """
        db_chat_channel = await DBChatChannel.get_channel(chat_key=chat_key)
        if not await DBAdapterInstance.is_instance_active(db_chat_channel.adapter_key, db_chat_channel.instance_key):
            logger.debug(f"实例或其所属租户已停用，丢弃工具结果: chat_key={chat_key}")
            return
        await self._record_untrusted_result(chat_key, db_chat_channel, result)

    async def _record_untrusted_result(self, chat_key: str, db_chat_channel: DBChatChannel, result: UntrustedToolResult) -> None:
        """落库并广播一条不可信结果行（`push_tool_result` 与 `push_untrusted_message` 共用）

        落库与广播的都是 `stored_body`：插件设了 `history_body` 时，完整正文只在本轮交给模型，
        聊天记录、消息接口与之后轮次的历史里看不到它。
        """
        segment = ChatMessageSegmentUntrusted(
            type=ChatMessageSegmentType.UNTRUSTED,
            text=result.stored_body,
            kind=result.kind,
            summary=result.summary,
            attrs=result.attrs,
        )
        identity = {
            "message_id": "",
            "sender_name": TOOL_RESULT_SENDER_NAME,
            "sender_nickname": TOOL_RESULT_SENDER_NAME,
            "adapter_key": db_chat_channel.adapter_key,
            "instance_key": db_chat_channel.instance_key,
            "platform_userid": TOOL_RESULT_SENDER_ID,
            "is_tome": 0,
            "is_recalled": False,
            "chat_key": chat_key,
            "content_text": result.summary,
            "raw_cq_code": "",
            "ext_data": {},
            "send_timestamp": int(time.time()),
        }
        await DBChatMessage.create(
            sender_id=TOOL_RESULT_SENDER_ID,
            chat_type=db_chat_channel.chat_type,
            content_data=json.dumps([segment.model_dump()], ensure_ascii=False),
            **identity,
        )
        await message_broadcaster.publish(
            chat_key,
            ChatMessage(
                sender_id=TOOL_RESULT_SENDER_ID,
                chat_type=ChatType(db_chat_channel.chat_type),
                content_data=[segment],
                **identity,
            ),
        )


# 全局消息服务实例
message_service = MessageService()
