"""状态图：画图的并发上限、命令的冷却、自动发图

- **画图**：放进线程，全进程最多 :data:`RENDER_SLOTS` 张同时画（命令、自动发图、后台预览共用）；没有空位
  就立刻放弃这一次，不排队。环境里没有中文字体时不画，调用方退回文字。
- **冷却**：按频道各算一个冷却（文字、图分开；「/状态 图」与后台预览共用图的那一个）。秒数取三者里最大的：
  代码里的下限 :data:`MIN_COOLDOWN_SECONDS`、平台（插件全局配置）设的、实例设的——实例只能往上调。
- **自动发图**（核心配置 `STATE_IMAGE_SEND_MODE`）：只数结局为 ok 的轮次；`every_reply` 按 1 轮算；
  实际间隔 = max(N, `STATE_IMAGE_MIN_N`)。私聊里等这一轮的分步更新跑完（最多 :data:`PRIVATE_WAIT_SECONDS`）
  再发，群聊立刻发。群里的图只画频道状态里不私密的部分（不带任何人的档案）；私聊带上对方的档案。
  图不进聊天记录。计数只在进程内，重启从零数。
"""

from __future__ import annotations

import asyncio
import time
import weakref
from pathlib import Path
from typing import Any, Optional

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.imagecard import fonts_available
from nekro_agent.services.imagecard.state_card import StateCardData, render_state_card

from . import updater, view
from .plugin import StateTableConfig, plugin

logger = get_sub_logger("state_table.card")

RENDER_SLOTS = 2
MIN_COOLDOWN_SECONDS = {"text": 3, "image": 10}
_COOLDOWN_FIELDS = {"text": "COMMAND_TEXT_COOLDOWN_SECONDS", "image": "COMMAND_IMAGE_COOLDOWN_SECONDS"}
PRIVATE_WAIT_SECONDS = 30.0
CARD_RETENTION_SECONDS = 3600
MAX_COUNTERS = 4096

_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_cooldowns: dict[tuple[str, str], float] = {}
_round_counts: dict[str, int] = {}
_TASKS: set["asyncio.Task[None]"] = set()


def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(RENDER_SLOTS)
    return slot


async def render(data: StateCardData) -> Optional[bytes]:
    """在有并发上限的线程里画一张；没有字体、没有空位或画失败时返回 None（日志不带内容）"""
    if not fonts_available():
        return None
    slot = _slot()
    if slot.locked():
        # 不排队：排着的请求会把冷却之外的压力堆在这里，回文字（或报忙）更好
        logger.info("状态图的画图位都占着，这一次不画")
        return None
    await slot.acquire()
    try:
        return await asyncio.to_thread(render_state_card, data)
    except Exception as error:  # noqa: BLE001 - 画不出来就退回文字或不发
        logger.warning(f"状态图渲染失败: error={type(error).__name__}")
        return None
    finally:
        slot.release()


def cooldown_seconds(instance_key: str, kind: str) -> float:
    """这个实例上 `kind`（text / image）的冷却秒数：代码下限、平台设的、实例设的取最大"""
    name = _COOLDOWN_FIELDS[kind]
    platform_value = getattr(plugin.get_global_config(StateTableConfig), name, 0)
    instance_value = getattr(updater.scoped_config(instance_key), name, 0)
    return float(max(MIN_COOLDOWN_SECONDS[kind], int(platform_value or 0), int(instance_value or 0)))


def cooldown_left(chat_key: str, kind: str, seconds: float) -> int:
    """这个频道这一种命令还要冷却几秒；为 0 时记下这一次（调用方随即执行）"""
    now = time.monotonic()
    key = (chat_key, kind)
    last = _cooldowns.get(key)
    if last is not None and now - last < seconds:
        return max(1, int(seconds - (now - last) + 0.999))
    if len(_cooldowns) >= MAX_COUNTERS:
        _cooldowns.clear()
    _cooldowns[key] = now
    return 0


def card_file(chat_key: str, image: bytes) -> Path:
    """把图落到插件数据目录（顺手清掉一小时前的旧图），返回路径"""
    directory = plugin.get_plugin_data_dir() / "cards"
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.time() - CARD_RETENTION_SECONDS
    for stale in directory.glob("*.png"):
        try:
            if stale.stat().st_mtime < deadline:
                stale.unlink()
        except OSError:  # noqa: PERF203 - 清理失败不影响这一次
            continue
    safe = "".join(char if char.isalnum() else "_" for char in chat_key)[:80]
    path = directory / f"state_{safe}_{time.time_ns()}.png"
    path.write_bytes(image)
    return path


def _due(chat_key: str, config: Any) -> bool:
    """按自动发图的档位数这一轮；到了返回真（计数清零）"""
    mode = str(getattr(config, "STATE_IMAGE_SEND_MODE", "command"))
    if mode not in ("every_reply", "every_n"):
        _round_counts.pop(chat_key, None)
        return False
    every = 1 if mode == "every_reply" else int(getattr(config, "STATE_IMAGE_EVERY_N", 10) or 10)
    every = max(every, int(getattr(config, "STATE_IMAGE_MIN_N", 1) or 1), 1)
    count = _round_counts.get(chat_key, 0) + 1
    if count >= every:
        _round_counts[chat_key] = 0
        return True
    if len(_round_counts) >= MAX_COUNTERS:
        _round_counts.clear()
    _round_counts[chat_key] = count
    return False


async def after_round(ctx: AgentCtx, channel: DBChatChannel, outcome: str, *, counterpart: str = "") -> None:
    """回合收尾时调：结局为 ok、到了间隔就排一个后台任务发图（不等它）"""
    if outcome != "ok":
        return
    config = await channel.get_effective_config()
    if not _due(channel.chat_key, config):
        return
    task = asyncio.create_task(_send(ctx, channel, counterpart))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _send(ctx: AgentCtx, channel: DBChatChannel, counterpart: str) -> None:
    try:
        private = getattr(channel, "chat_type", None) == ChatType.PRIVATE
        if private:
            deadline = time.monotonic() + PRIVATE_WAIT_SECONDS
            while updater.is_running(channel.chat_key) and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
        data = await view.build_card(
            channel, requester_uid=counterpart, include_requester=private and bool(counterpart)
        )
        if data is None:
            return
        image = await render(data)
        if image is None:
            return
        # 与好感榜的沙盒方法同一个写法：图先交给这一轮的文件区，再以插件身份发（发之前插件再判一次生效）
        sandbox_path = await ctx.fs.mixed_forward_file(image, file_name=f"state_{time.time_ns()}.png")
        await plugin.send_image(channel.chat_key, sandbox_path, ctx, record=False)
    except Exception as error:  # noqa: BLE001 - 后台任务：发不出去只记一条
        logger.warning(f"自动发状态图失败: chat_key={channel.chat_key}, error={type(error).__name__}")


async def drain() -> None:
    """等全部自动发图结束；只给用例用"""
    while _TASKS:
        await asyncio.gather(*list(_TASKS), return_exceptions=True)


def reset_for_tests() -> None:
    _cooldowns.clear()
    _round_counts.clear()


__all__ = ["after_round", "card_file", "cooldown_left", "cooldown_seconds", "drain", "render", "reset_for_tests"]
