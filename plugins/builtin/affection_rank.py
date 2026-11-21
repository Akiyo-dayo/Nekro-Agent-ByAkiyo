"""
# 好感度排行榜 (Affection Rank)

把当前群里近期活跃成员的好感度渲染成一张排行榜图片。

## 主要功能

- **群内指令**: `/affection_rank`（别名"好感度""好感排行""好感榜"）即可查看本群榜单。
- **AI 主动展示**: AI 也可以在合适的时候自己调用工具把榜单发出来。
- **发起人高亮**: 发指令的人自己那一行会被高亮，不用在两百行里找自己。

## 展示刻度

榜上的分数是内部好感度除以 2 后保留一位小数（内部上限 200 对应满分 100.0）。
这只是展示换算，不改变任何内部数值语义，详见
`nekro_agent/services/imagecard/affection_rank.py` 的模块文档。

## 上榜规则

成员集合取自本频道近 N 天的发言记录，与好感度记录取交集——好感度本身按
**(域, 人, 人设)** 隔离，不分频道，所以"这个群有谁"必须由消息记录回答。
没有好感度记录的人不上榜，但会计入副标题里的活跃人数。

## 排序范围 = 当前域 + 当前人设

榜单只统计**发起会话所在的那一份好感**：跨域或跨人设排名是信息泄露
（群 A 的人不该出现在群 B 的榜上，更不该因为换了人设就把另一个人格下的关系抖出来）。
范围由 `services/identity_scope.py` 解析，本插件不自己拼键，判据见
`nekro_agent/services/imagecard/affection_rank.py` 的模块文档。

榜上的单位是**人**不是账号：合并过的账号（换了 QQ 号）只出现一次，
发起人用副号发指令也能高亮到自己那一行。

沙盒方法的 `chat_key` 是模型自由填写的参数，因此**只接受当前会话**
（见 `_assert_own_channel`）：范围锁在"发起会话所在的那一份"才有意义，
放任它指向别的频道就是把别人那一份读进本沙盒。
"""

import asyncio
import time
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import Field

from nekro_agent.api import core, i18n
from nekro_agent.api.plugin import ConfigBase, ExtraField, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import (
    CommandExecutionContext,
    CommandOutputSegment,
    CommandOutputSegmentType,
    CommandResponse,
)
from nekro_agent.services.imagecard import fonts_available
from nekro_agent.services.plugin.scope_guard import CHANNEL_NOT_FOUND
from nekro_agent.services.imagecard.affection_rank import (
    MAX_ENTRIES,
    RankView,
    build_rank_view,
    collect_rank_entries,
    render_rank_card,
)

plugin = NekroPlugin(
    name="好感度排行榜",
    module_name="affection_rank",
    description="把群内近期活跃成员的好感度渲染成排行榜图片",
    version="0.1.0",
    author="Akiyo_dayo",
    url="https://github.com/Akiyo-dayo/Nekro-Agent-ByAkiyo",
    i18n_name=i18n.i18n_text(zh_CN="好感度排行榜", en_US="Affection Rank"),
    i18n_description=i18n.i18n_text(
        zh_CN="把群内近期活跃成员的好感度渲染成排行榜图片",
        en_US="Renders the affection scores of recently active group members as a ranking image",
    ),
    allow_sleep=True,
    sleep_brief="用于展示群内好感度排行榜，仅在有人问起好感度排名时激活。",
)


@plugin.mount_config()
class AffectionRankConfig(ConfigBase):
    """好感度排行榜配置"""

    ACTIVE_DAYS: int = Field(
        default=30,
        title="活跃统计天数",
        description="只统计这段时间内在本群发过言的人",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="活跃统计天数", en_US="Active window (days)"),
            i18n_description=i18n.i18n_text(
                zh_CN="只统计这段时间内在本群发过言的人",
                en_US="Only members who spoke in this channel within the window are counted",
            ),
        ).model_dump(),
    )
    MAX_ROWS: int = Field(
        default=MAX_ENTRIES,
        title="榜单最多展示人数",
        description="超出的部分按分数从低到高截断",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="榜单最多展示人数", en_US="Maximum rows"),
            i18n_description=i18n.i18n_text(
                zh_CN="超出的部分按分数从低到高截断",
                en_US="Entries beyond this limit are cut from the lowest score up",
            ),
        ).model_dump(),
    )
    SCAN_MESSAGE_LIMIT: int = Field(
        default=20000,
        title="统计成员时最多回溯的消息条数",
        description="大群一个月的消息量可能非常大，这个上限防止一次查询拖垮数据库",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="统计成员时最多回溯的消息条数", en_US="Message scan limit"),
            i18n_description=i18n.i18n_text(
                zh_CN="防止大群的一次查询拖垮数据库",
                en_US="Prevents a single query from overloading the database in busy groups",
            ),
        ).model_dump(),
    )
    ALLOW_PRIVATE_CHAT: bool = Field(
        default=False,
        title="允许在私聊中查看",
        description="私聊里的活跃成员只有一个人，榜单意义不大",
        json_schema_extra=ExtraField(
            i18n_title=i18n.i18n_text(zh_CN="允许在私聊中查看", en_US="Allow in private chats"),
            i18n_description=i18n.i18n_text(
                zh_CN="私聊里的活跃成员只有一个人，榜单意义不大",
                en_US="A private chat has a single active member, so the ranking is nearly meaningless",
            ),
        ).model_dump(),
    )


config = plugin.get_config(AffectionRankConfig)

# 渲染好的卡片先落到插件数据目录，框架随后会把它物化进上传目录再发出去。
# 保留时间到点即清，避免这个目录无限增长
CARD_RETENTION_SECONDS = 3600


async def _build_view(
    db_chat_channel: DBChatChannel,
    *,
    highlight_userid: str,
) -> Tuple[RankView, str]:
    """取数并整理成榜单视图

    排序范围（域 + 人设）与"发起人是哪个人"都由 `collect_rank_entries` 解析，本函数只负责
    把它们接到视图上——**不要**在这里用原始账号做任何比对。

    Returns:
        `(榜单视图, 副标题)`

    Raises:
        ValueError: 没有任何可展示的记录
    """
    collection = await collect_rank_entries(
        channel=db_chat_channel,
        active_days=config.ACTIVE_DAYS,
        scan_limit=config.SCAN_MESSAGE_LIMIT,
        highlight_userid=highlight_userid,
    )
    view = build_rank_view(
        collection.entries,
        total_active=collection.total_active,
        # 高亮比对的是解析后的"人"：发起人用副号发的指令仍要高亮到他自己那一行
        highlight_person_uid=collection.highlight_person_uid,
        max_entries=config.MAX_ROWS,
    )
    if not view.rows:
        raise ValueError("本群近期还没有任何好感度记录")

    # 群号只能从 channel_id 取：chat_key 的适配器前缀本身可能含下划线
    channel_label = db_chat_channel.channel_id.partition("_")[2] or db_chat_channel.channel_id
    # 这里的 "scene" 说的是"群还是私聊"，与隔离键那个"域"无关，名字刻意分开
    scene = "群" if db_chat_channel.chat_type == ChatType.GROUP else "会话"
    subtitle = f"{scene} {channel_label} · 近{config.ACTIVE_DAYS}天活跃 {view.total_active} 人"
    return view, subtitle


def _text_fallback(view: RankView, subtitle: str, limit: int = 10) -> str:
    """图片渲染不可用时的文本降级

    只列前若干名：一条几百行的文本消息在群里比不发还糟。
    """
    lines = [f"好感度排行 · {subtitle}"]
    lines.extend(
        f"{index}. {row.display_name}({row.suffix})  {row.score_text}"
        for index, row in enumerate(view.rows[:limit], start=1)
    )
    if len(view.rows) > limit:
        lines.append(f"…… 共 {len(view.rows)} 人")
    return "\n".join(lines)


def _card_path(chat_key: str) -> Path:
    """卡片文件落盘位置，并顺手清理过期文件"""
    card_dir = plugin.get_plugin_data_dir() / "cards"
    card_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.time() - CARD_RETENTION_SECONDS
    for stale in card_dir.glob("*.png"):
        try:
            if stale.stat().st_mtime < deadline:
                stale.unlink()
        except OSError:  # noqa: PERF203 - 清理失败不应影响本次渲染
            core.logger.debug(f"清理过期好感度卡片失败: {stale}")

    safe_key = "".join(char if char.isalnum() else "_" for char in chat_key)
    return card_dir / f"{safe_key}_{int(time.time() * 1000)}.png"


def _assert_own_channel(ctx: AgentCtx, chat_key: str) -> None:
    """沙盒方法只准对**发起会话自己**出榜

    `chat_key` 是模型自由填写的参数。榜单的取数范围恒等于"这个 chat_key 所在频道"的
    (域 + 人设) 那一份好感，所以不设防就等于让 A 实例（A 租户）的沙盒指定 B 的会话，
    读出 B 那一份：

    - 渲染好的 PNG 经 `_ctx.fs.mixed_forward_file` 落进**发起方自己的**沙盒互通目录
      （`FileSystem` 的 `chat_key` 是 ctx 的 `from_chat_key`，不是这里的参数），
      模型下一步就能把它发到自己的群里；
    - 返回串里的"共 N 人上榜、活跃 M 人"直接进发起方模型的上下文；
    - 连"这个频道有没有好感记录"都是一个可探测的信号（没有记录会抛 ValueError）。

    这与 `basic.send_msg_text` 那类"AI 可以跨频道调用"的通用发送能力不是一回事：
    那边流出去的是**本会话自己的**内容，出站总闸（`universal_chat_service.
    _assert_instance_active`）管的也是"能不能发"；这里流进来的是**别人的数据**，
    出站闸门一个字都拦不住。因此归属校验必须落在本方法自己身上。

    Raises:
        ValueError: `chat_key` 不是当前会话，或 ctx 根本不挂在某个会话上（如 Webhook 触发）
    """
    own_chat_key = ctx.from_chat_key or ""
    if not own_chat_key:
        raise ValueError("当前上下文不属于任何会话，无法查看好感度排行榜")
    if chat_key != own_chat_key:
        raise ValueError("好感度排行榜只能查看当前会话，chat_key 必须是当前会话标识")


@plugin.mount_command(
    name="affection_rank",
    description="查看本群好感度排行榜",
    aliases=["好感度", "好感排行", "好感榜"],
    permission=CommandPermission.PUBLIC,
    usage="/affection_rank",
    category="好感度",
    tags=["好感度", "排行榜"],
    i18n_description=i18n.i18n_text(
        zh_CN="查看本群好感度排行榜",
        en_US="Show the affection ranking of this group",
    ),
)
async def affection_rank_cmd(context: CommandExecutionContext) -> CommandResponse:
    """好感度排行榜指令"""
    db_chat_channel = await DBChatChannel.get_channel(chat_key=context.chat_key)
    if db_chat_channel.chat_type != ChatType.GROUP and not config.ALLOW_PRIVATE_CHAT:
        return CmdCtl.failed("好感度排行榜仅支持群聊")

    try:
        view, subtitle = await _build_view(db_chat_channel, highlight_userid=context.user_id)
    except ValueError as exc:
        return CmdCtl.failed(str(exc))

    if not fonts_available():
        core.logger.warning("环境缺少可用中文字体，好感度排行榜降级为文本")
        return CmdCtl.success(_text_fallback(view, subtitle))

    try:
        path = _card_path(context.chat_key)
        # 榜单是 6 列 × 最多 200 行，实测单次渲染 500ms 量级——在事件循环里跑等于让
        # 本进程内所有实例停摆半秒。渲染与落盘一起丢进线程。
        image = await asyncio.to_thread(render_rank_card, view, subtitle=subtitle)
        await asyncio.to_thread(path.write_bytes, image)
    except Exception as exc:  # noqa: BLE001 - 渲染失败要降级而不是把异常抛给用户
        core.logger.exception(f"渲染好感度排行榜失败，降级为文本: {exc}")
        return CmdCtl.success(_text_fallback(view, subtitle))

    return CmdCtl.success(
        [CommandOutputSegment(type=CommandOutputSegmentType.IMAGE, file_path=str(path))],
    )


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, "发送群好感度排行榜图片")
async def send_affection_rank(_ctx: AgentCtx, chat_key: str, highlight_user_id: str = "") -> str:
    """发送本群好感度排行榜图片

    把当前群里近期活跃成员的好感度渲染成一张排行榜图片并发送回本群。
    有人问起"谁的好感度最高""我排第几"这类问题时使用。

    Args:
        chat_key (str): 聊天的唯一标识符，必须是当前会话
        highlight_user_id (str): 需要在榜单上高亮的用户 ID，留空则不高亮

    Returns:
        str: 操作结果描述
    """
    _assert_own_channel(_ctx, chat_key)
    db_chat_channel = await DBChatChannel.get_channel(chat_key=chat_key)
    if db_chat_channel.chat_type != ChatType.GROUP and not config.ALLOW_PRIVATE_CHAT:
        raise ValueError("好感度排行榜仅支持群聊")

    view, subtitle = await _build_view(db_chat_channel, highlight_userid=highlight_user_id)

    if not fonts_available():
        # 走插件推送方法：发送前再判一次插件在当前频道是否生效（方法能被调用通常已经生效，
        # 但发送发生在渲染之后，中途被关掉时不能硬发）
        if not await plugin.send_text(chat_key, _text_fallback(view, subtitle), _ctx):
            raise ValueError(CHANNEL_NOT_FOUND)
        return f"环境缺少中文字体，已改为发送文本榜单（共 {len(view.rows)} 人）"

    image = await asyncio.to_thread(render_rank_card, view, subtitle=subtitle)
    sandbox_path = await _ctx.fs.mixed_forward_file(image, file_name=f"affection_rank_{int(time.time())}.png")
    if not await plugin.send_image(chat_key, sandbox_path, _ctx):
        raise ValueError(CHANNEL_NOT_FOUND)
    return f"已发送好感度排行榜图片，共 {len(view.rows)} 人上榜，近{config.ACTIVE_DAYS}天活跃 {view.total_active} 人"


@plugin.mount_collect_methods()
async def collect_available_methods(_ctx: AgentCtx) -> List[object]:
    """私聊里没有榜单可言，不要把这个方法暴露给模型"""
    channel_type: Optional[str] = _ctx.channel_type
    if channel_type != ChatType.GROUP.value and not config.ALLOW_PRIVATE_CHAT:
        return []
    return [send_affection_rank]


@plugin.mount_cleanup_method()
async def clean_up() -> None:
    """清理插件"""
