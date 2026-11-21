"""聊天登记凭据之后的原文清理

## 为什么要清理

凭据一旦在聊天里出现，NA 自己存下来的副本就不该继续留着：频道的聊天记录每一轮都会
作为上下文再发给模型服务商，执行记录与提示词日志也会被翻出来看；原文留着等于每一轮
都把密码再送出去一次。清理只替换登记过的那几段凭据文字，整条消息的其余部分、撤回标记
都不动，也不调用撤回——撤不撤由模型自己判断。

## 为什么是"精确片段"而不是正则

凭据是任意文本，没有可靠的形态。这里只拿登记时确实见过的值做精确子串替换：密码与
口令短语整串、私钥整段以及私钥正文里每一行（聊天里贴私钥时换行可能被改过）。短于
6 个字符的片段一律不收，避免把常见短词换掉。替换顺序按长度从长到短，避免短片段先
把长片段拆碎。

## 同一个值落盘时的几种写法

同一个密码在不同地方存的不是同一串字符：

- `content_data` / `ext_data` 是 `json.dumps` 存的，`"`、`\\`、控制字符都被转义；
  历史正是从 `content_data` 渲染出来发给模型的。这两列**解析成 JSON、只在字符串值里
  替换**再写回：既认得出转义后的写法，又不会因为密码长得像 JSON 语法（例如 `","text":"`）
  把整列弄坏、让整个频道的历史渲染不出来。解析不了的按纯文本处理。
- 提示词日志是 JSON 文件，执行记录里的代码是 Python 字面量：纯文本替换时连同
  JSON 转义（中英两种）与单引号字面量的写法一起匹配。
- `raw_cq_code` 里 `& [ ] ,` 按 CQ 码转义；同样连同转义写法一起匹配。它只存档、不再
  被解析，用普通占位即可。

## 范围只在这个频道

聊天记录、执行记录、记忆都按 `chat_key` 过滤；提示词日志的文件名里没有频道，所以
`note_prompt_log` 在落盘时记下「哪个频道写了哪个文件」，清理只碰这个频道自己写过的
文件。不能扫整个目录：那会改写别的频道、别的租户的日志，审计里的命中计数还会变成
一个「某段文字在别处出现过没有」的探针。NA 重启之前写的日志因此清不到，见纪要残留。

## 本轮还会继续写

登记发生在一轮中间，之后执行记录、提示词日志才会落盘。`register_pending` 把这一轮
的片段存进进程内的待清理表（默认 15 分钟），`apply_pending` 在落盘前做同样的替换。
没有待清理项时只是字典里查一次，正文逐字不变。聊天记录与记忆的落库点太多，改成在
回合收尾（`finish_round`）再扫一遍。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from nekro_agent.core import os_env
from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("ssh_gateway.scrub")

PLACEHOLDER = "[已登记的凭据]"

# 短于这个长度的片段不替换：太容易误伤正常文字
MIN_FRAGMENT_LENGTH = 6
# 私钥正文里被单独当作片段的行至少这么长：base64 行远长于此，短行多半是标记或注释
MIN_PRIVATE_KEY_LINE_LENGTH = 20

# 清理窗口：至少 1 小时，对话上下文过期时间更长时跟着它（上下文里还在的消息都要清），
# 但不超过 7 天——这是登记时的一次性扫描，不能变成对整个频道历史的全表扫。
MIN_WINDOW_SECONDS = 3600
MAX_WINDOW_SECONDS = 7 * 24 * 3600

# 每个频道记住最近这么多个提示词日志文件
_PROMPT_LOG_MEMORY = 500

# 单调时钟；测试注入它来验证过期，不必真等
_clock = time.monotonic

# chat_key -> (片段, 过期时刻)。只放进程内存：重启后本轮已经结束，没有"之后还会写"的东西
_pending: Dict[str, Tuple[List[str], float]] = {}

# chat_key -> [(写入时刻 epoch 秒, 路径)]
_prompt_logs: Dict[str, Deque[Tuple[float, str]]] = {}

_COUNT_KEYS = (
    "chat_messages",
    "exec_code",
    "prompt_logs",
    "mem_paragraphs",
    "mem_summaries",
    "mem_episodes",
    "mem_entities",
)


def scrub_window_seconds() -> int:
    """清理与来源核对共用的回看窗口"""
    try:
        from nekro_agent.core.config import config

        context_seconds = int(getattr(config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", 0) or 0)
    except Exception:  # noqa: BLE001 - 读不到配置就用下限
        context_seconds = 0
    return max(MIN_WINDOW_SECONDS, min(MAX_WINDOW_SECONDS, context_seconds))


def _usable(fragments: Iterable[str]) -> List[str]:
    """过滤空片段与过短片段，去重并按长度从长到短排"""
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in fragments:
        value = str(raw or "")
        if len(value) < MIN_FRAGMENT_LENGTH or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    ordered.sort(key=len, reverse=True)
    return ordered


def _cq_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;").replace(",", "&#44;")


def _variants(fragment: str) -> List[str]:
    """一个片段落盘时可能的几种写法：原文、JSON 转义（中英两种）、单引号字面量、CQ 转义"""
    return [
        fragment,
        json.dumps(fragment, ensure_ascii=False)[1:-1],
        json.dumps(fragment, ensure_ascii=True)[1:-1],
        fragment.replace("\\", "\\\\").replace("'", "\\'"),
        _cq_escape(fragment),
    ]


def _expanded(fragments: Iterable[str]) -> List[str]:
    expanded: List[str] = []
    for fragment in _usable(fragments):
        expanded.extend(_variants(fragment))
    return _usable(expanded)


def secret_fragments(password: str, private_key: str, passphrase: str) -> List[str]:
    """一次登记里值得清理的片段

    密码与口令短语用整串；私钥用整段（换行没被改动时能整段命中），再加上正文里每一行
    长度达到阈值的行（换行被改过、只剩行内容时仍能命中）。短于 6 个字符的一律不收。
    """
    candidates: List[str] = []
    for value in (str(password or ""), str(passphrase or "")):
        if value:
            candidates.append(value)

    key_text = str(private_key or "").strip()
    if key_text:
        candidates.append(key_text)
        for line in key_text.splitlines():
            stripped = line.strip()
            if len(stripped) >= MIN_PRIVATE_KEY_LINE_LENGTH:
                candidates.append(stripped)

    return _usable(candidates)


def replace_secrets(
    text: str,
    fragments: Iterable[str],
    *,
    placeholder: str = PLACEHOLDER,
) -> Tuple[str, int]:
    """纯文本里把凭据片段（含几种转义写法）换成占位，返回 `(新文本, 替换次数)`

    没有命中时原样返回同一个字符串，调用方可以据此判断"不用写库"。
    """
    value = str(text or "")
    if not value:
        return value, 0
    count = 0
    for fragment in _expanded(fragments):
        if fragment not in value:
            continue
        count += value.count(fragment)
        value = value.replace(fragment, placeholder)
    return value, count


def _replace_in_json_value(value: Any, fragments: Sequence[str]) -> Tuple[Any, int]:
    if isinstance(value, str):
        return replace_secrets(value, fragments)
    if isinstance(value, list):
        total = 0
        items: List[Any] = []
        for item in value:
            new_item, hits = _replace_in_json_value(item, fragments)
            items.append(new_item)
            total += hits
        return items, total
    if isinstance(value, dict):
        total = 0
        mapping: Dict[Any, Any] = {}
        for key, item in value.items():
            new_item, hits = _replace_in_json_value(item, fragments)
            mapping[key] = new_item
            total += hits
        return mapping, total
    return value, 0


def replace_secrets_in_json(text: str, fragments: Iterable[str]) -> Tuple[str, int]:
    """JSON 文本：解析后只在字符串值里替换，再按 `ensure_ascii=False` 写回

    解析不了的当纯文本处理。没有命中时原样返回同一个字符串。
    """
    raw = str(text or "")
    if not raw:
        return raw, 0
    usable = _usable(fragments)
    if not usable:
        return raw, 0
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return replace_secrets(raw, usable)
    new_data, count = _replace_in_json_value(data, usable)
    if not count:
        return raw, 0
    dumped = json.dumps(new_data, ensure_ascii=False)
    if raw.endswith("\n"):
        dumped += "\n"
    return dumped, count


def _replace_truncated_tails(text: str, fragments: Iterable[str], separators: str = "；") -> Tuple[str, int]:
    """被截断的片段：派生文本按长度截取原文时，凭据可能只剩开头一段留在某一截的末尾

    只用于确实由命中段落派生出来的文本（Episode 的标题与叙事摘要），所以误伤面很小。
    """
    value = str(text or "")
    if not value:
        return value, 0
    count = 0
    boundary = "(?=" + "|".join(re.escape(ch) for ch in separators) + "|$)" if separators else "(?=$)"
    for fragment in _usable(fragments):
        for length in range(len(fragment) - 1, MIN_FRAGMENT_LENGTH - 1, -1):
            pattern = re.compile(re.escape(fragment[:length]) + boundary)
            value, hits = pattern.subn(PLACEHOLDER, value)
            count += hits
    return value, count


def register_pending(chat_key: str, fragments: Iterable[str], ttl_seconds: int = 900) -> None:
    """记下这一轮之后还要清理的片段

    同一轮里多次登记时合并而不是覆盖：先登记的片段不能因为后一次登记被忘掉。
    过期时刻每次重新登记都会往后推，仍是同一轮。
    """
    if not chat_key:
        return
    usable = _usable(fragments)
    if not usable:
        return
    key = str(chat_key)
    existing = _pending.get(key)
    merged: List[str] = []
    if existing is not None:
        merged.extend(existing[0])
    merged.extend(usable)
    expires_at = _clock() + max(0.0, float(ttl_seconds))
    _pending[key] = (_usable(merged), expires_at)


def pending_fragments(chat_key: str) -> List[str]:
    """取这个频道当前仍有效的待清理片段；过期项顺手丢掉"""
    item = _pending.get(str(chat_key))
    if item is None:
        return []
    fragments, expires_at = item
    if _clock() >= expires_at:
        _pending.pop(str(chat_key), None)
        return []
    return list(fragments)


def apply_pending(chat_key: str, text: str) -> str:
    """落盘前替换这个频道的待清理片段；没有待清理项时逐字返回原值"""
    fragments = pending_fragments(chat_key)
    if not fragments:
        return text
    replaced, _count = replace_secrets(text, fragments)
    return replaced


def reset_pending() -> None:
    """清空待清理表（供测试与关停使用）"""
    _pending.clear()


def note_prompt_log(chat_key: str, path: Any) -> None:
    """提示词日志落盘时记下它属于哪个频道；清理只碰这个频道自己写过的文件"""
    key = str(chat_key or "")
    if not key or not path:
        return
    now = time.time()
    entries = _prompt_logs.get(key)
    if entries is None:
        entries = deque(maxlen=_PROMPT_LOG_MEMORY)
        _prompt_logs[key] = entries
    entries.append((now, str(path)))
    # 顺手丢掉早已超出任何清理窗口的频道，免得字典随频道数单向增长
    horizon = now - MAX_WINDOW_SECONDS
    for stale_key in [k for k, items in _prompt_logs.items() if items and items[-1][0] < horizon]:
        _prompt_logs.pop(stale_key, None)


def reset_prompt_log_registry() -> None:
    """清空提示词日志归属表（供测试使用）"""
    _prompt_logs.clear()


def scrub_code_files(chat_key: str, paths: Iterable[Path]) -> int:
    """沙盒跑完后，按待清理表重写留在宿主共享目录里的代码文件；返回替换次数

    没有待清理项时一个文件都不读。

    共享目录挂进沙盒、0777：沙盒里的代码能把这两个文件换成指向宿主任意位置的链接。
    所以按目录句柄、`O_NOFOLLOW` 读，写临时文件再 rename（替换的是目录项本身）；
    是链接或不是普通文件的直接跳过，不顺着链接读、也不往链接那头写。
    """
    fragments = pending_fragments(chat_key)
    if not fragments:
        return 0
    from nekro_agent.services.workspace import safe_fs

    total = 0
    for path in paths:
        try:
            with safe_fs.open_dir(Path(path).parent) as directory:
                data = directory.read_bytes(Path(path).name, max_bytes=_CODE_FILE_MAX_BYTES)
                if data is None:
                    continue
                text, encoding = _decode_text(data)
                replaced, hits = replace_secrets(text, fragments)
                if not hits:
                    continue
                directory.write_bytes(Path(path).name, replaced.encode(encoding))
            total += hits
        except OSError:
            continue
    return total


# 代码文件的读取上限：模型一轮生成的代码不会到这个量级，超了就不读（也就不清理）
_CODE_FILE_MAX_BYTES = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# 落盘后的清理
# ---------------------------------------------------------------------------


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _read_text(path: Path) -> Tuple[str, str]:
    """按原编码读文本：先 UTF-8，再 GB18030，最后退回 latin-1（总能成功）"""
    return _decode_text(path.read_bytes())


def _decode_text(data: bytes) -> Tuple[str, str]:
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def _write_text_atomically(path: Path, text: str, encoding: str) -> None:
    """写临时文件再替换，避免清理到一半留下半截文件"""
    directory = path.parent
    fd, raw_temp = tempfile.mkstemp(dir=str(directory), prefix=path.name + ".", suffix=".scrub")
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, path.stat().st_mode)
        except OSError:
            pass
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _prompt_log_dirs() -> List[Path]:
    """两个提示词日志目录；读模块属性而不是 import 时的常量，测试能换目录"""
    return [Path(str(os_env.PROMPT_LOG_DIR)), Path(str(os_env.PROMPT_ERROR_LOG_DIR))]


def _inside_prompt_dirs(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for directory in _prompt_log_dirs():
        try:
            resolved.relative_to(directory.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _scrub_prompt_logs(chat_key: str, fragments: List[str], since: datetime.datetime) -> int:
    """清理这个频道在窗口内写过的提示词日志文件；返回替换次数"""
    since_ts = since.timestamp()
    entries = list(_prompt_logs.get(str(chat_key), ()))
    seen: set[str] = set()
    count = 0
    for written_at, raw_path in entries:
        if written_at < since_ts or raw_path in seen:
            continue
        seen.add(raw_path)
        path = Path(raw_path)
        # 只认两个提示词日志目录里的文件：归属表是内存里的，但写回磁盘这一步不能被带到别处
        if not _inside_prompt_dirs(path):
            continue
        try:
            if not path.is_file():
                continue
            text, encoding = _read_text(path)
            replaced, hits = replace_secrets_in_json(text, fragments)
            if not hits:
                continue
            _write_text_atomically(path, replaced, encoding)
            count += hits
        except OSError:
            # 单个日志文件被清理任务占用 / 没权限，不该打断其余清理
            continue
    return count


async def _scrub_chat_messages(chat_key: str, fragments: List[str], since: datetime.datetime) -> int:
    from nekro_agent.models.db_chat_message import DBChatMessage

    rows = await DBChatMessage.filter(chat_key=chat_key, create_time__gte=since).values(
        "id",
        "content_text",
        "content_data",
        "raw_cq_code",
        "ext_data",
    )
    total = 0
    for row in rows:
        updates: Dict[str, object] = {}
        hits = 0
        for field in ("content_text", "raw_cq_code"):
            replaced, field_hits = replace_secrets(str(row.get(field) or ""), fragments)
            if field_hits:
                updates[field] = replaced
                hits += field_hits
        for field in ("content_data", "ext_data"):
            replaced, field_hits = replace_secrets_in_json(str(row.get(field) or ""), fragments)
            if field_hits:
                updates[field] = replaced
                hits += field_hits
        if not updates:
            continue
        updates["update_time"] = _now()
        await DBChatMessage.filter(id=row["id"]).update(**updates)
        total += hits
    return total


async def _scrub_exec_code(chat_key: str, fragments: List[str], since: datetime.datetime) -> int:
    from nekro_agent.models.db_exec_code import DBExecCode

    rows = await DBExecCode.filter(chat_key=chat_key, create_time__gte=since)
    total = 0
    for row in rows:
        updates: Dict[str, object] = {}
        hits = 0
        for field in ("code_text", "outputs", "thought_chain"):
            replaced, field_hits = replace_secrets(str(getattr(row, field) or ""), fragments)
            if field_hits:
                updates[field] = replaced
                hits += field_hits
        if not updates:
            continue
        updates["update_time"] = _now()
        await DBExecCode.filter(id=row.id).update(**updates)
        total += hits
    return total


async def _scrub_paragraphs(chat_key: str, fragments: List[str], since: datetime.datetime) -> List[int]:
    """命中的段落先把正文与摘要里的凭据换掉，再软删（一并清向量）；返回命中的段落 id

    只软删不换正文的话，明文仍留在库里，后台「恢复」还会把它重新嵌入。
    """
    from nekro_agent.models.db_mem_paragraph import DBMemParagraph
    from nekro_agent.services.memory import admin_service

    rows = await DBMemParagraph.filter(origin_chat_key=chat_key, create_time__gte=since).values(
        "id",
        "content",
        "summary",
    )
    hit_ids: List[int] = []
    for row in rows:
        updates: Dict[str, object] = {}
        content, content_hits = replace_secrets(str(row.get("content") or ""), fragments)
        if content_hits:
            updates["content"] = content
        summary, summary_hits = replace_secrets(str(row.get("summary") or ""), fragments)
        if summary_hits:
            # 占位可能比被换掉的片段长：摘要列是 512 的定长字符串，超长在 PG 上会直接报错
            updates["summary"] = summary[:512]
        if not updates:
            continue
        await DBMemParagraph.filter(id=row["id"]).update(**updates)
        hit_ids.append(int(row["id"]))
    if hit_ids:
        await admin_service.soft_delete_paragraphs(hit_ids)
    return hit_ids


async def _scrub_episodes(chat_key: str, fragments: List[str], since: datetime.datetime, hit_ids: List[int]) -> int:
    """Episode 的标题与叙事摘要是从段落原文截出来拼的，命中段落的派生文本连截断的尾巴一起换"""
    from nekro_agent.models.db_mem_episode import DBMemEpisode

    hit_set = set(hit_ids)
    rows = await DBMemEpisode.filter(origin_chat_key=chat_key, update_time__gte=since)
    total = 0
    for row in rows:
        linked = bool(hit_set & {int(item) for item in (row.paragraph_ids or []) if str(item).lstrip("-").isdigit()})
        title, title_hits = replace_secrets(str(row.title or ""), fragments)
        narrative, narrative_hits = replace_secrets(str(row.narrative_summary or ""), fragments)
        if linked:
            title, extra_title = _replace_truncated_tails(title, fragments, separators="")
            narrative, extra_narrative = _replace_truncated_tails(narrative, fragments)
            title_hits += extra_title
            narrative_hits += extra_narrative
        updates: Dict[str, object] = {}
        if title_hits:
            updates["title"] = title[:256]
        if narrative_hits:
            updates["narrative_summary"] = narrative
        if not updates:
            continue
        await DBMemEpisode.filter(id=row.id).update(**updates)
        total += title_hits + narrative_hits
    return total


async def _scrub_entities(fragments: List[str], hit_ids: List[int]) -> int:
    """经命中段落抽出来的关系谓词与两端实体的名字里若有凭据，也换掉"""
    if not hit_ids:
        return 0
    from nekro_agent.models.db_mem_entity import DBMemEntity
    from nekro_agent.models.db_mem_relation import DBMemRelation

    total = 0
    entity_ids: set[int] = set()
    for relation in await DBMemRelation.filter(paragraph_id__in=hit_ids):
        entity_ids.add(int(relation.subject_entity_id))
        entity_ids.add(int(relation.object_entity_id))
        predicate, hits = replace_secrets(str(relation.predicate or ""), fragments)
        if hits:
            await DBMemRelation.filter(id=relation.id).update(predicate=predicate[:64])
            total += hits
    if not entity_ids:
        return total
    for entity in await DBMemEntity.filter(id__in=sorted(entity_ids)):
        updates: Dict[str, object] = {}
        hits_here = 0
        for field in ("name", "canonical_name"):
            replaced, hits = replace_secrets(str(getattr(entity, field) or ""), fragments)
            if hits:
                updates[field] = replaced[:256]
                hits_here += hits
        aliases = entity.aliases if isinstance(entity.aliases, list) else []
        new_aliases, alias_hits = _replace_in_json_value(list(aliases), fragments)
        if alias_hits:
            updates["aliases"] = new_aliases
            hits_here += alias_hits
        if not updates:
            continue
        try:
            await DBMemEntity.filter(id=entity.id).update(**updates)
        except Exception as exc:  # noqa: BLE001 - 规范名唯一约束等冲突：这一条换不了也不该拦住其余清理
            if "canonical_name" in updates:
                # 替换后的规范名可能撞上另一条实体：补一个只属于本行的后缀再试一次
                retry_updates = dict(updates)
                retry_updates["canonical_name"] = f"{updates['canonical_name']}#{entity.id}"[:256]
                try:
                    await DBMemEntity.filter(id=entity.id).update(**retry_updates)
                except Exception as retry_exc:  # noqa: BLE001 - 再失败只记一笔，继续清其余实体
                    logger.warning(
                        f"清理记忆实体里的凭据失败: entity_id={entity.id}, {type(retry_exc).__name__}",
                    )
                    continue
            else:
                logger.warning(
                    f"清理记忆实体里的凭据失败: entity_id={entity.id}, {type(exc).__name__}",
                )
                continue
        total += hits_here
    return total


async def _scrub_summaries(chat_key: str, fragments: List[str], since: datetime.datetime) -> int:
    from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary

    total = 0
    for row in await DBMemChannelSummary.filter(chat_key=chat_key, create_time__gte=since):
        replaced, hits = replace_secrets(str(row.content or ""), fragments)
        if not hits:
            continue
        await DBMemChannelSummary.filter(id=row.id).update(content=replaced)
        total += hits
    return total


async def _scrub_memories(chat_key: str, fragments: List[str], since: datetime.datetime) -> Dict[str, int]:
    hit_ids = await _scrub_paragraphs(chat_key, fragments, since)
    return {
        "mem_paragraphs": len(hit_ids),
        "mem_summaries": await _scrub_summaries(chat_key, fragments, since),
        "mem_episodes": await _scrub_episodes(chat_key, fragments, since, hit_ids),
        "mem_entities": await _scrub_entities(fragments, hit_ids),
    }


async def _write_scrub_audit(chat_key: str, counts: Dict[str, int]) -> None:
    """记一条不含原文的 `scrub` 审计：detail 只有本频道各处的替换计数"""
    from nekro_agent.models.db_chat_channel import DBChatChannel
    from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
    from nekro_agent.services.ssh_gateway import constants as C

    channel = await DBChatChannel.get_or_none(chat_key=chat_key)
    workspace_id = int(channel.workspace_id) if channel is not None and channel.workspace_id else 0
    await DBWsSshAudit.create(
        workspace_id=workspace_id,
        credential_id=None,
        owner_kind=C.OWNER_USER,
        owner_account="",
        adapter_key=str(channel.adapter_key or "") if channel is not None else "",
        instance_key=str(channel.instance_key or "") if channel is not None else "",
        event=C.EVENT_SCRUB,
        allowed=True,
        reason="",
        detail=json.dumps(counts, ensure_ascii=False, sort_keys=True),
        actor="chat",
    )


async def scrub_after_registration(
    *,
    chat_key: str,
    fragments: Iterable[str],
    window_seconds: Optional[int] = None,
) -> Dict[str, int]:
    """把这个频道最近窗口内已落盘的凭据原文换成占位

    覆盖聊天记录（全部发送者，含 Bot 自己）、执行记录、这个频道写过的提示词日志、记忆
    段落及其派生（Episode、关系与实体）与频道摘要。每一处都只在真的出现片段时才写；
    没有出现的不碰。最后写一条 `scrub` 审计，detail 只有计数，不含任何原文。
    """
    usable = _usable(fragments)
    counts: Dict[str, int] = {key: 0 for key in _COUNT_KEYS}
    if not usable:
        await _write_scrub_audit(chat_key, counts)
        return counts

    window = scrub_window_seconds() if window_seconds is None else max(0, int(window_seconds))
    since = _now() - datetime.timedelta(seconds=window)

    counts["chat_messages"] = await _scrub_chat_messages(chat_key, usable, since)
    counts["exec_code"] = await _scrub_exec_code(chat_key, usable, since)
    counts["prompt_logs"] = await asyncio.to_thread(_scrub_prompt_logs, chat_key, usable, since)
    counts.update(await _scrub_memories(chat_key, usable, since))

    await _write_scrub_audit(chat_key, counts)
    return counts


async def finish_round(chat_key: str, *, window_seconds: Optional[int] = None) -> int:
    """回合收尾时再扫一遍这个频道的聊天记录与记忆

    登记发生在回合中途；之后这一轮还会落库新的聊天记录（典型是 Bot 自己的回复，模型若没管住
    嘴把凭据复述了一遍，它就在库里），记忆整理也可能恰好在这期间把含原文的段落写了库。
    执行记录与提示词日志在落盘那一刻已经按待清理表替换。

    没有待清理项时只查一次字典就返回，回合收尾的开销与改造前相同。真的替换了才写审计。
    """
    fragments = pending_fragments(chat_key)
    if not fragments:
        return 0
    window = scrub_window_seconds() if window_seconds is None else max(0, int(window_seconds))
    since = _now() - datetime.timedelta(seconds=window)
    counts: Dict[str, int] = {key: 0 for key in _COUNT_KEYS}
    counts["chat_messages"] = await _scrub_chat_messages(chat_key, fragments, since)
    counts.update(await _scrub_memories(chat_key, fragments, since))
    replaced = sum(counts.values())
    if replaced:
        await _write_scrub_audit(chat_key, counts)
    return replaced


__all__ = [
    "MIN_FRAGMENT_LENGTH",
    "MIN_PRIVATE_KEY_LINE_LENGTH",
    "PLACEHOLDER",
    "apply_pending",
    "finish_round",
    "note_prompt_log",
    "pending_fragments",
    "register_pending",
    "replace_secrets",
    "replace_secrets_in_json",
    "reset_pending",
    "reset_prompt_log_registry",
    "scrub_after_registration",
    "scrub_code_files",
    "scrub_window_seconds",
    "secret_fragments",
]
