"""角色状态插件的声明与配置

可选内置插件，默认不启用（不在 `PLUGIN_ENABLED` 的默认值里），按「会话 > 实例 > 全局」逐个打开。
状态块的 token 上限与「在场的人」的时间窗要能按频道覆盖，所以放在核心配置
（`STATE_PROMPT_MAX_TOKENS`、`STATE_PRESENT_WINDOW_MINUTES`）；其余在这里，按实例覆盖。

群聊每 N 条消息更新一次，N 默认取这个频道的记忆沉淀阈值（`MEMORY_CONSOLIDATION_MSG_THRESHOLD`，默认 50）。
扮演群里想让状态变得更勤，可以把 `GROUP_UPDATE_EVERY_N` 调小。

插件出厂不带任何绕过审核的话术。「更新前置消息 / 更新预填」两项对应原「记忆增强表格」插件放话术的位置，
留空；只在实例打开了高级提示词槽位（`PROMPT_ADVANCED_SLOTS_ENABLED`）时才发送，内容及其后果由填写的
管理员负责。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from nekro_agent.api import i18n
from nekro_agent.api.plugin import ConfigBase, ExtraField, NekroPlugin

plugin = NekroPlugin(
    name="角色状态",
    module_name="state_table",
    description="给角色扮演维护一份「故事的当前状态」（时间、地点、在场的人、物品、关系），每轮注入、回复后另发一次请求更新",
    version="0.1.0",
    author="Akiyo_dayo",
    url="https://github.com/KroMiose/nekro-agent",
    i18n_name=i18n.i18n_text(zh_CN="角色状态", en_US="Character State"),
    i18n_description=i18n.i18n_text(
        zh_CN=(
            "给角色扮演维护一份「故事的当前状态」：时间、地点、在场的人、物品、关系。初值来自人设卡的 [initvar]，"
            "每轮作为参考注入，回复后用单独选的模型组更新（群聊每 N 条消息一次）。默认不启用。"
        ),
        en_US=(
            "Keeps the story's current state (time, place, who is present, items, relations) for role play. "
            "Initial values come from the card's [initvar]; the state is injected each round as reference and "
            "updated after replies with a separately chosen model group (every N messages in groups). Off by default."
        ),
    ),
    allow_sleep=False,
    sleep_brief="维护角色扮演的当前状态（时间、地点、在场的人、物品、关系）。",
)


def _extra(zh_title: str, en_title: str, zh_desc: str, en_desc: str, **flags: Any) -> dict[str, Any]:
    return ExtraField(
        i18n_title=i18n.i18n_text(zh_CN=zh_title, en_US=en_title),
        i18n_description=i18n.i18n_text(zh_CN=zh_desc, en_US=en_desc),
        **flags,
    ).model_dump()


@plugin.mount_config()
class StateTableConfig(ConfigBase):
    UPDATE_MODEL_GROUP: str = Field(
        default="",
        title="更新用的模型组（留空用主对话模型组）",
        description="回复后更新状态用的模型组。留空时用这个频道的主对话模型组；建议指一个便宜的、不带推理的组。",
        json_schema_extra=_extra(
            "更新用的模型组（留空用主对话模型组）",
            "Update Model Group (blank uses the main chat group)",
            "回复后更新状态用的模型组。留空时用这个频道的主对话模型组；建议指一个便宜的、不带推理的组。",
            "Model group used to update the state after replies. When blank, this channel's main chat model group is "
            "used; a cheap group without reasoning is recommended.",
            ref_model_groups=True,
            model_type="chat",
        ),
    )
    GROUP_UPDATE_EVERY_N: int = Field(
        default=0,
        ge=0,
        le=500,
        title="群聊每 N 条消息更新一次",
        description="0 表示取这个频道的记忆沉淀阈值（默认 50）。私聊每次回复后都更新，不看这一项。",
        json_schema_extra=_extra(
            "群聊每 N 条消息更新一次",
            "Group Update Every N Messages",
            "0 表示取这个频道的记忆沉淀阈值（默认 50）。私聊每次回复后都更新，不看这一项。",
            "0 uses the channel's memory consolidation threshold (50 by default). Private chats update after "
            "every reply regardless.",
        ),
    )
    UPDATE_CONTEXT_MESSAGES: int = Field(
        default=2,
        ge=1,
        le=30,
        title="私聊更新时带几条消息",
        description="私聊更新时给模型看最近几条消息；群聊看上次更新以来的消息（最多 30 条）。",
        json_schema_extra=_extra(
            "私聊更新时带几条消息",
            "Private Update Context Messages",
            "私聊更新时给模型看最近几条消息；群聊看上次更新以来的消息（最多 30 条）。",
            "How many recent messages a private-chat update sees; group updates see the messages since the last "
            "update (at most 30).",
        ),
    )
    UPDATE_MAX_OUTPUT_TOKENS: int = Field(
        default=1024,
        ge=128,
        le=4096,
        title="更新调用的输出上限（token）",
        description="更新调用最多输出多少 token。租户只能往下调。",
        json_schema_extra=_extra(
            "更新调用的输出上限（token）",
            "Update Output Token Cap",
            "更新调用最多输出多少 token。租户只能往下调。",
            "The most tokens an update call may output. Tenants may only lower it.",
            tenant_may_only_lower=True,
        ),
    )
    MAX_OPS_PER_UPDATE: int = Field(
        default=20,
        ge=1,
        le=50,
        title="一次更新最多改几处",
        description="多出来的修改丢掉并记下原因。",
        json_schema_extra=_extra(
            "一次更新最多改几处",
            "Max Changes Per Update",
            "多出来的修改丢掉并记下原因。",
            "Changes beyond this are dropped and recorded with a reason.",
        ),
    )
    FAILURE_PAUSE_AFTER: int = Field(
        default=5,
        ge=1,
        le=50,
        title="连续失败几次后暂停更新",
        description="解析失败、全部被拒、调用出错连续这么多次后，这个频道暂停更新（状态照常注入），管理员可以在后台恢复。",
        json_schema_extra=_extra(
            "连续失败几次后暂停更新",
            "Pause After Failures",
            "解析失败、全部被拒、调用出错连续这么多次后，这个频道暂停更新（状态照常注入），管理员可以在后台恢复。",
            "After this many consecutive failures the channel stops updating (the state is still injected) until "
            "an administrator resumes it.",
        ),
    )
    SNAPSHOT_KEEP: int = Field(
        default=20,
        ge=1,
        le=100,
        title="保留几份快照",
        description="每个频道 / 每个人保留最近几份可回滚的快照。租户只能往下调。",
        json_schema_extra=_extra(
            "保留几份快照",
            "Snapshots To Keep",
            "每个频道 / 每个人保留最近几份可回滚的快照。租户只能往下调。",
            "How many recent snapshots to keep per channel or person for rollback. Tenants may only lower it.",
            tenant_may_only_lower=True,
        ),
    )
    RESET_ON_CHANNEL_RESET: bool = Field(
        default=True,
        title="频道重置时清回初值",
        description="频道 /reset 时把频道状态清回初值；个人档案跨频道，不随某个频道重置。",
        json_schema_extra=_extra(
            "频道重置时清回初值",
            "Reset With Channel",
            "频道 /reset 时把频道状态清回初值；个人档案跨频道，不随某个频道重置。",
            "Reset the channel state to its initial values on /reset. Personal profiles span channels and are not reset.",
        ),
    )
    STATE_POSITION: str = Field(
        default="depth",
        title="状态块的位置",
        description="depth：插进聊天记录（深度见下一项，默认最新一条之前）；history_top：放在聊天记录开头。",
        json_schema_extra=_extra(
            "状态块的位置",
            "State Block Position",
            "depth：插进聊天记录（深度见下一项，默认最新一条之前）；history_top：放在聊天记录开头。",
            "depth: inserted into the chat history (see the next item; by default before the latest message); "
            "history_top: at the start of the chat history.",
        ),
    )
    STATE_DEPTH: int = Field(
        default=1,
        ge=0,
        le=20,
        title="状态块的深度",
        description="插在倒数第几条消息之前；0 是最末尾。",
        json_schema_extra=_extra(
            "状态块的深度",
            "State Block Depth",
            "插在倒数第几条消息之前；0 是最末尾。",
            "Inserted before the N-th message from the end; 0 is after the last one.",
        ),
    )
    PERSON_SCOPE: str = Field(
        default="window",
        title="个人档案带谁",
        description=(
            "window：最近一段时间（核心配置「在场的人」的分钟数）里说过话的人；"
            "round：上一条 Bot 消息之后说过话的人加上触发这一轮的人。两种都最多 8 人。"
        ),
        json_schema_extra=_extra(
            "个人档案带谁",
            "Whose Profiles",
            "window：最近一段时间（核心配置「在场的人」的分钟数）里说过话的人；"
            "round：上一条 Bot 消息之后说过话的人加上触发这一轮的人。两种都最多 8 人。",
            "window: people who spoke within the recent window (the core 'present' minutes setting); "
            "round: people who spoke since the bot's last message, plus whoever triggered this round. "
            "Both include at most 8 people.",
        ),
    )
    COMMAND_TEXT_COOLDOWN_SECONDS: int = Field(
        default=10,
        ge=0,
        le=3600,
        title="「/状态」的冷却（秒）",
        description=(
            "同一个频道里两次「/状态」（文字）至少隔这么久；冷却中只回一句。平台设的是下限，实例只能调得更长；最短 3 秒。"
        ),
        json_schema_extra=_extra(
            "「/状态」的冷却（秒）",
            "Text Command Cooldown (Seconds)",
            "同一个频道里两次「/状态」（文字）至少隔这么久；冷却中只回一句。平台设的是下限，实例只能调得更长；最短 3 秒。",
            "Minimum time between two text /state commands in the same channel; during the cooldown "
            "only a short note is sent. The platform value is a floor that instances can only raise; at least 3 seconds.",
        ),
    )
    COMMAND_IMAGE_COOLDOWN_SECONDS: int = Field(
        default=30,
        ge=0,
        le=3600,
        title="「/状态 图」的冷却（秒）",
        description=(
            "同一个频道里两次「/状态 图」（含后台预览）至少隔这么久；冷却中只回一句。平台设的是下限，实例只能调得更长；"
            "最短 10 秒。画图全进程最多同时两张，没有空位时回文字。"
        ),
        json_schema_extra=_extra(
            "「/状态 图」的冷却（秒）",
            "Image Command Cooldown (Seconds)",
            "同一个频道里两次「/状态 图」（含后台预览）至少隔这么久；冷却中只回一句。平台设的是下限，实例只能调得更长；"
            "最短 10 秒。画图全进程最多同时两张，没有空位时回文字。",
            "Minimum time between two /state image commands (including the admin preview) in the same channel; "
            "during the cooldown only a short note is sent. The platform value is a floor that instances can only "
            "raise; at least 10 seconds. At most two images are drawn at the same time per process; when none is "
            "free, text is sent instead.",
        ),
    )
    INLINE_UPDATE_ENABLED: bool = Field(
        default=False,
        title="私聊里让主模型顺手更新",
        description=(
            "打开后，私聊里给主模型一个更新状态的方法，它可以在回复的同时改状态（同样过系统的校验，"
            "这一轮回复成功后才生效）。这一轮已经顺手改过的，就不再另发一次更新请求。群聊里不提供。"
        ),
        json_schema_extra=_extra(
            "私聊里让主模型顺手更新",
            "Inline Updates in Private Chats",
            "打开后，私聊里给主模型一个更新状态的方法，它可以在回复的同时改状态（同样过系统的校验，"
            "这一轮回复成功后才生效）。这一轮已经顺手改过的，就不再另发一次更新请求。群聊里不提供。",
            "When on, the main model gets a method in private chats to change the state while it replies "
            "(checked by the same system validation and applied only after the round succeeds). Rounds that "
            "already changed the state this way skip the separate update request. Not offered in groups.",
        ),
    )
    PRIVATE_WORDS: str = Field(
        default="性爱,私密,处女,罩杯,性经验,敏感带",
        title="私密词表",
        description="从 [initvar] 推导模板时，路径或字段名包含这些词（逗号分隔）的字段自动标私密：只在私聊里注入与更新。",
        json_schema_extra=_extra(
            "私密词表",
            "Private Words",
            "从 [initvar] 推导模板时，路径或字段名包含这些词（逗号分隔）的字段自动标私密：只在私聊里注入与更新。",
            "When deriving a template from [initvar], fields whose path or name contains any of these words "
            "(comma separated) are marked private: injected and updated only in private chats.",
        ),
    )
    UPDATE_PRELUDE: str = Field(
        default="",
        title="更新前置消息",
        description="出厂留空。只在实例打开高级提示词槽位时发送，放在更新规则之后；内容及其后果由填写的管理员负责。",
        json_schema_extra=_extra(
            "更新前置消息",
            "Update Prelude",
            "出厂留空。只在实例打开高级提示词槽位时发送，放在更新规则之后；内容及其后果由填写的管理员负责。",
            "Empty by default. Sent only when the instance enables advanced prompt slots, after the update rules; "
            "the administrator who fills it is responsible for its content and consequences.",
            is_textarea=True,
        ),
    )
    UPDATE_PREFILL: str = Field(
        default="",
        title="更新预填",
        description="出厂留空。只在实例打开高级提示词槽位时作为助手消息的开头发送；内容及其后果由填写的管理员负责。",
        json_schema_extra=_extra(
            "更新预填",
            "Update Prefill",
            "出厂留空。只在实例打开高级提示词槽位时作为助手消息的开头发送；内容及其后果由填写的管理员负责。",
            "Empty by default. Sent as the start of the assistant message only when the instance enables advanced "
            "prompt slots; the administrator who fills it is responsible for its content and consequences.",
            is_textarea=True,
        ),
    )

    def private_words(self) -> list[str]:
        return [word.strip() for word in self.PRIVATE_WORDS.replace("，", ",").split(",") if word.strip()]


def person_scope_of(cfg: Any) -> str:
    """配置里的 `PERSON_SCOPE`：`window` 或 `round`；没有或认不出的按 `window`"""
    return "round" if str(getattr(cfg, "PERSON_SCOPE", "window")).strip().lower() == "round" else "window"


__all__ = ["StateTableConfig", "person_scope_of", "plugin"]
