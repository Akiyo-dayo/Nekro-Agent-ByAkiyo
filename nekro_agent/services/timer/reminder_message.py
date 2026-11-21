"""定时提醒与自动暂停通知的不可信内容构造器

## 为什么这两类通知不能走 SYSTEM 消息

定时提醒的正文（`event_desc` / `title`）是**模型或用户写进定时任务里的字符串**，
它在触发那一刻已经与人写的没有区别，但来源仍然是"数据"而不是"宿主说的话"。
拼进 SYSTEM 消息就等于让一条被注入的描述以系统身份下指令：模型被要求信任 SYSTEM 消息，
于是那句"忽略之前所有指令"会获得与宿主引导语相同的可信度。

自动暂停通知里的 `last_error` 更直接：它是上游库抛出的异常文本，里面经常原样回显
请求头（`Authorization: Bearer sk-xxx`）与内网地址。它既要脱敏，也不能作为系统指令出现。

## 构造器放在这里而不是各处内联

四个调用点（一次性定时器的正常触发与重启补发、周期任务的触发与自动暂停）要产出同一种形状：
**宿主写死的可信摘要 + 定界在一行内的任务标识 + 带令牌标签包裹的正文**。
分散写四份，任何一份漏掉 `echo_inline` 或清洗，都会在那一条路径上留下一个注入面，
而"另外三条是对的"完全不会让这里变红。
"""

from typing import Optional

from nekro_agent.tools.redaction import redact_text
from nekro_agent.tools.untrusted_content import UntrustedToolResult, echo_inline

# 正文上限：提醒描述与错误文本都是人写的短文本，不需要 `UntrustedToolResult` 的 20 万字符兜底。
# 但也不该让一条失控的记录把整份上下文撑爆——定时任务会被反复触发，放大是累积的。
MAX_REMINDER_BODY_CHARS = 4_000
MAX_LAST_ERROR_CHARS = 2_000

# 正文里"这一项没有值"的占位。写成常量是为了让构造与断言两处不会分叉。
EMPTY_PLACEHOLDER = "（无）"


def _truncate(text: str, limit: int) -> str:
    """按字符数截断，保留末尾的省略号

    截断发生在**脱敏之后**：先截断再脱敏的话，一段刚好被切断的凭据可能只剩前半段，
    三条正则都要求足够长度才会命中，于是它反而漏网。
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def build_reminder_result(
    *,
    description: str,
    title: str = "",
    task_id: Optional[str],
    is_misfire: bool,
) -> UntrustedToolResult:
    """构造一条"定时提醒到点"的不可信结果

    Args:
        description: 设置这条提醒时记下的描述（`event_desc`）。**完全不可信**：
            它由模型或用户写入，可能带换行、可能是一段指令。
        title: 周期任务的标题，可为空。同样不可信。
        task_id: 任务标识，用于让人认出是哪一条提醒。它由宿主生成，但仍然经
            `echo_inline` 回显——回显的目的不是"这个值可能恶意"，而是"它一旦带上换行，
            就能在可信摘要里接出一行宿主没说过的话"。
        is_misfire: 是否是"服务重启期间错过了原定时间"的补发。

    Returns:
        `UntrustedToolResult`：`summary` 里只有宿主写死的引导语与回显的任务标识，
        标题与描述一律只出现在 `body` 里。
    """
    if is_misfire:
        lead = "⏰ 定时提醒到点（补发：服务重启期间错过了原定时间）"
    else:
        lead = "⏰ 定时提醒到点"
    # 没有任务标识时不写"任务 ID"那一段：留一个空的引号串反而像是有个值为空的 ID
    task_part = f"（任务 ID {echo_inline(task_id)}）" if task_id else ""

    lines = []
    if title:
        lines.append(f"标题：{title}")
        lines.append(f"描述：{description}")
    else:
        lines.append(description)
    body = _truncate("\n".join(lines), MAX_REMINDER_BODY_CHARS)

    return UntrustedToolResult(
        kind="TimerReminder",
        body=body,
        summary=f"{lead}，下方是你设置这条提醒时记下的描述{task_part}。",
    )


def build_auto_pause_result(
    *,
    job_id: str,
    title: str,
    last_error: Optional[str],
) -> UntrustedToolResult:
    """构造一条"定时任务已自动暂停"的不可信结果

    与提醒的差别在于它**不能触发对话**（`trigger_agent=False`）：任务已经因为连续失败被
    停掉，此刻再唤起一轮对话，很可能只是把同一次失败重演一遍。

    Args:
        job_id: 周期任务标识；经 `echo_inline` 回显，理由同 :func:`build_reminder_result`。
        title: 任务标题，不可信，只进正文。
        last_error: 最近一次错误文本。上游异常经常原样回显请求头与内网地址，所以
            **先 `redact_text` 再进正文**；脱敏之后再截断（顺序见 :func:`_truncate`）。

    Returns:
        `UntrustedToolResult`：`summary` 里只有宿主写死的说明与回显的任务标识。
    """
    summary = (
        f"⏸️ 定时任务已自动暂停：连续触发失败次数过多（任务 ID {echo_inline(job_id)}）。"
        "标题与最近一次错误见下方不可信块。"
        "你可以让 AI 调用 resume_recurring_timer 恢复，或 update_recurring_timer 修正参数。"
    )

    safe_error = _truncate(redact_text(last_error), MAX_LAST_ERROR_CHARS) if last_error else ""
    body = f"标题：{title or EMPTY_PLACEHOLDER}\n最近错误：{safe_error or EMPTY_PLACEHOLDER}"

    return UntrustedToolResult(
        kind="TimerAutoPaused",
        body=body,
        summary=summary,
    )
