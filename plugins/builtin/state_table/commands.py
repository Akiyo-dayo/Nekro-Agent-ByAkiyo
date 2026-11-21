"""`/状态`（`/state`）：看、画图、重置、恢复更新

- **看**（`/状态`）与**画图**（`/状态 图`）谁都能用。数据范围只看频道类型与发命令的人（见 `view`）：群里只有
  频道状态里不私密的部分，加上发命令的人自己档案里不私密的部分；不看权限高低。
- **重置**（`/状态 重置`）、**恢复更新**（`/状态 恢复`）只给这个频道的高级用户与超级用户；各写一行审计，
  操作者记平台账号。重置只清频道状态，个人档案不动。
- 文字最多 :data:`TEXT_LIMIT` 字，超了提示改用图；文字里的 `@` 与 `[CQ:` 换成全角，状态里的字不会变成
  @ 人或协议码。图里的字本来就当纯文本画。
- 冷却按频道算（文字、图各一个，秒数在插件配置里），冷却中回一句。
- 查看时就地初始化：这个人设有模板、这个频道还没有状态行时按模板初值建一行再显示；没有模板也推导不出时
  说明去哪里加。
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated

from nekro_agent.api import i18n
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import (
    Arg,
    CommandExecutionContext,
    CommandOutputSegment,
    CommandOutputSegmentType,
    CommandResponse,
)
from nekro_agent.services.identity_scope import resolve_preset_id
from nekro_agent.services.imagecard.state_card import card_text

from . import card, session, store, updater, view
from .plugin import plugin

TEXT_LIMIT = 1500
NO_TEMPLATE_HINT = (
    "这个人设还没有状态模板。请管理员到后台「角色状态 → 模板」给当前人设套用一个模板，"
    "或在人设的世界书里加一条 [initvar] 条目。"
)
_ACTIONS = {
    "": "text",
    "看": "text",
    "文字": "text",
    "text": "text",
    "图": "image",
    "图片": "image",
    "image": "image",
    "重置": "reset",
    "reset": "reset",
    "恢复": "resume",
    "恢复更新": "resume",
    "resume": "resume",
}
_CQ = re.compile(r"\[(?=\s*cq\s*:)", re.IGNORECASE)
USAGE = "/状态 [图|重置|恢复]"


def safe_text(text: str) -> str:
    """命令发出去的文字里，`@` 与 `[CQ:` 换成全角：状态里的字不变成 @ 人或协议码"""
    return _CQ.sub("［", text.replace("@", "＠"))


def _editor(context: CommandExecutionContext) -> str:
    return f"{context.adapter_key}:{context.user_id}"[:64]


async def _current_row(channel: DBChatChannel):  # noqa: ANN202 - 返回 DBStateChannel 或 None
    return await store.get_channel(channel.chat_key, await resolve_preset_id(channel))


async def _ensure_state(channel: DBChatChannel) -> str:
    """按模板（或书里的 `[initvar]`）给这个频道建行；参数来源与 `main._load` 一致，命令里没有宏上下文"""
    return await session.ensure_channel_row(channel, platform_words=updater.platform_words(), expand=None)


async def _manage(context: CommandExecutionContext, channel: DBChatChannel, kind: str) -> CommandResponse:
    if not (context.is_advanced_user or context.is_super_user):
        return CmdCtl.failed("只有这个频道的管理员能这样做")
    row = await _current_row(channel)
    if row is None:
        if await _ensure_state(channel) == "no_template":
            return CmdCtl.success(NO_TEMPLATE_HINT)
        row = await _current_row(channel)
        if row is None:
            return CmdCtl.success(NO_TEMPLATE_HINT)
    keep = int(updater.scoped_config(str(channel.instance_key or "")).SNAPSHOT_KEEP)
    if kind == "reset":
        from .main import reset_channel_state

        if not await reset_channel_state(row, editor=_editor(context), source="reset", keep=keep):
            return CmdCtl.failed("没有状态模板，重置不了")
        return CmdCtl.success("角色状态已清回初值（个人档案没动）。")
    await store.update_channel_fields(row, paused=False, consecutive_failures=0)
    await store.write_snapshot(
        target_kind="channel",
        target_id=int(row.id),
        chat_key=str(row.chat_key),
        adapter_key=str(row.adapter_key or ""),
        instance_key=str(row.instance_key or ""),
        source="admin",
        result="applied",
        applied=[{"op": "resume", "path": ""}],
        editor=_editor(context),
        keep=keep,
    )
    return CmdCtl.success("已恢复角色状态的自动更新。")


@plugin.mount_command(
    name="state",
    description="查看角色状态（图：画成图片；重置、恢复：频道管理员用）",
    aliases=["状态"],
    permission=CommandPermission.PUBLIC,
    usage=USAGE,
    category="角色状态",
    tags=["角色状态"],
    i18n_description=i18n.i18n_text(
        zh_CN="查看角色状态（图：画成图片；重置、恢复：频道管理员用）",
        en_US="Show the character state (image: as a picture; reset / resume: for channel admins)",
    ),
)
async def state_command(
    context: CommandExecutionContext,
    action: Annotated[str, Arg("图 / 重置 / 恢复；留空看文字", positional=True)] = "",
) -> CommandResponse:
    """角色状态命令"""
    kind = _ACTIONS.get(str(action or "").strip().lower())
    if kind is None:
        return CmdCtl.failed(f"用法：{USAGE}")
    channel = await DBChatChannel.filter(chat_key=context.chat_key).order_by("id").first()
    if channel is None:
        return CmdCtl.failed("找不到这个频道")
    if kind in ("reset", "resume"):
        return await _manage(context, channel, kind)

    seconds = card.cooldown_seconds(str(channel.instance_key or ""), kind)
    left = card.cooldown_left(context.chat_key, kind, seconds)
    if left:
        return CmdCtl.success(f"刚看过，{left} 秒后再试。")
    data = await view.build_card(channel, requester_uid=str(context.user_id or ""), include_requester=True)
    if data is None:
        if await _ensure_state(channel) == "no_template":
            return CmdCtl.success(NO_TEMPLATE_HINT)
        data = await view.build_card(channel, requester_uid=str(context.user_id or ""), include_requester=True)
        if data is None:
            return CmdCtl.success(NO_TEMPLATE_HINT)
    if kind == "image":
        image = await card.render(data)
        if image is not None:
            path = await asyncio.to_thread(card.card_file, context.chat_key, image)
            return CmdCtl.success([CommandOutputSegment(type=CommandOutputSegmentType.IMAGE, file_path=str(path))])
    text = card_text(data, TEXT_LIMIT)
    if kind == "text" and text.endswith("后面没写）"):
        text += "\n完整内容请用「/状态 图」。"
    return CmdCtl.success(safe_text(text))


__all__ = ["NO_TEMPLATE_HINT", "TEXT_LIMIT", "safe_text", "state_command"]
