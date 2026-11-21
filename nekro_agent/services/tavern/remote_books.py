"""卡片脚本要下载的世界书：静态认出来源，由管理员确认后 NA 代为下载、转换，作为人设的参考资料导入

酒馆助手脚本在酒馆里运行时会从网上下载一本世界书，用 `createOrReplaceWorldbook` 建好、挂到角色上。
NA 不执行卡片脚本（脚本是卡作者写的任意代码，在服务器上跑能碰到所有租户的数据），改成：

## 认出

只看会改写世界书的脚本（`unsupported.writes_worldbook`），从脚本文字里取写死的完整地址：路径以
`.json` 结尾、文件名里带 worldbook / lorebook / world_info 一类字样的才算。同一个文件名的几个地址
算同一个来源（主地址在前、镜像在后）。用字符串拼出来的地址（`${…}`）认不出，也不去认——那要执行脚本。
来源的 `key` 是文件名的摘要；下载、检查更新、手动上传都由服务端从人设存着的卡片原文重新认一遍再按
`key` 找，不接受前端传来的地址。

## 下载

`common_util.fetch_url_bytes`，即下载素材那一套守卫：只 http(s)、拒内网（认
`DOWNLOAD_EXTRA_ALLOWED_NETWORKS`）、逐跳复检、只收未压缩正文、限大小。**租户触发的按租户的出站判据**
（`tenant=True`）：放行网段同一份，但不认部署方的 `ALLOW_PRIVATE_NETWORK_DOWNLOAD`，环境变量里的 SOCKS
代理直接拒——地址写在卡里，卡是租户自己上传的。平台方照下载素材的判据。系统默认代理是平台方的出口，只有
平台方能勾（只支持 HTTP(S) 代理，经代理时同样 `CONNECT` 校验过的地址）。每个地址 60 秒、整次 150 秒封顶
（守卫自己只有每次读的超时）。主地址与镜像依次试，第一个能下载、又认得出是世界书的就停。失败原因只写
主机名；「解析不到」与「指向不允许访问的地址」说同一句话（分开说就等于把 NA 这边的解析结果告诉了租户）。

## 导入

新建一本绑在这个人设上的书，条目一律不可信（参考资料）：文件来自网上，导入人是平台方也一样。来源、
实际用的地址、版本、摘要记在书级 `ext` 的 `na_remote_source` 里。不自动更新；「检查更新」再下载一遍
只比较，「更新到新版本」在同一个事务里替换这本书的全部条目（书的设置不动）。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional
from urllib.parse import unquote, urlsplit

import httpx

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.errors import (
    AppError,
    RemoteWorldbookBusyError,
    RemoteWorldbookDownloadError,
    RemoteWorldbookImportedError,
)
from nekro_agent.schemas.tavern import (
    RemoteBookCheckResult,
    RemoteBookImported,
    RemoteBookImportResult,
    RemoteBookSource,
    RemoteBookView,
)
from nekro_agent.schemas.worldbook import MAX_DESCRIPTION_CHARS, BookCreateRequest
from nekro_agent.services.network import proxy_manager
from nekro_agent.services.worldbook import purpose as wb_purpose
from nekro_agent.services.worldbook import service as wb_service
from nekro_agent.services.worldbook import st_format
from nekro_agent.services.worldbook import unsupported as wb_unsupported
from nekro_agent.services.worldbook.types import MAX_NAME_CHARS, ScopeKind
from nekro_agent.tools import common_util
from nekro_agent.tools.common_util import UnsafeDownloadUrlError

logger = get_sub_logger("tavern.remote_books")

# 一张卡最多认几个来源、每个来源最多几个地址、每个来源最多记几个脚本名
MAX_SOURCES = 8
MAX_URLS_PER_SOURCE = 6
MAX_SCRIPTS_PER_SOURCE = 4
# 每个脚本最多看前多少个地址（几十万字的脚本也是线性的，这里再封一道）
MAX_URL_MATCHES_PER_SCRIPT = 2000
# 下载上限：与样本脚本自己的上限一致（8 MiB），比世界书文件导入的安全上限小得多
REMOTE_BOOK_MAX_BYTES = 8 * 1024 * 1024
# 时限：每个地址（连同读正文）最多 60 秒，整次下载（主地址加镜像）最多 150 秒。下载守卫自己只有每次读的
# 超时，一个每 15 秒吐一个字节的服务器能让下载一直挂着、把这个租户的下载名额一直占着；整次的时限也要比
# 前端等待的时限（180 秒）短，前端才拿得到原因
REMOTE_URL_TIMEOUT_SECONDS = 60.0
REMOTE_TOTAL_TIMEOUT_SECONDS = 150.0
# 整次时限只剩这么多时不再开始下一个地址（定时器会提前一个时钟粒度触发，剩下的零头也下不完一个文件）
REMOTE_MIN_URL_SECONDS = 1.0
# 认出的来源按（人设，扩展行的编号与更新时间）缓存多少个人设
MAX_SOURCE_CACHE = 256
MAX_FILE_NAME_CHARS = 200
SCRIPT_NAME_CHARS = 40
# 书级 `ext` 里记来源的键
REMOTE_SOURCE_KEY = "na_remote_source"

# 写死的完整地址；主机名只认 ASCII（国际化域名脚本里写的也是 punycode），路径遇到引号、空白、括号、
# 花括号（`${…}` 拼出来的地址）就停
_URL = re.compile(
    r"https?://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?/[^\s\"'`<>(){}\[\]\\]{1,2048}", re.IGNORECASE | re.ASCII
)
_WORLDBOOK_NAME_HINTS = (
    "worldbook",
    "world-book",
    "world_book",
    "lorebook",
    "lore-book",
    "lore_book",
    "worldinfo",
    "world-info",
    "world_info",
    "世界书",
)
# 地址里写的分支或版本：jsdelivr 的 `@main` / `@v0.2.2`，或者路径里单独一段 `v1.2.3`
_VERSION_AT = re.compile(r"@([A-Za-z0-9._-]{1,64})(?:/|$)", re.ASCII)
_VERSION_SEGMENT = re.compile(r"/(v?[0-9]{1,6}(?:\.[0-9]{1,6}){1,3})(?:/|$)", re.ASCII)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------------------
# 认出
# ---------------------------------------------------------------------------


def source_key(file_name: str) -> str:
    """来源的标识：文件名（不分大小写）的摘要"""
    return hashlib.sha256(file_name.lower().encode("utf-8")).hexdigest()[:16]


def _worldbook_file_name(url: str) -> Optional[str]:
    """地址指向的文件名；不是 http(s)、不是 `.json`、名字里没有世界书字样时为 None"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    name = unquote(parts.path.rsplit("/", 1)[-1]).strip()
    lowered = name.lower()
    if not lowered.endswith(".json") or not any(hint in lowered for hint in _WORLDBOOK_NAME_HINTS):
        return None
    return name


def _version_hint(url: str) -> str:
    path = urlsplit(url).path
    match = _VERSION_AT.search(path) or _VERSION_SEGMENT.search(path)
    return match.group(1) if match else ""


def _script_label(name: str) -> str:
    text = _CONTROL_CHARS.sub("", name or "").strip()
    return text[:SCRIPT_NAME_CHARS] or "（未命名脚本）"


def detect_remote_books(extensions: Mapping[str, Any]) -> list[RemoteBookSource]:
    """卡片扩展里的酒馆助手脚本要下载的世界书，按出现顺序；见模块说明「认出」"""
    sources: dict[str, RemoteBookSource] = {}
    for name, content in wb_unsupported.tavern_helper_scripts(extensions):
        if not wb_unsupported.writes_worldbook(content):
            continue
        label = _script_label(name)
        for index, match in enumerate(_URL.finditer(content)):
            if index >= MAX_URL_MATCHES_PER_SCRIPT:
                break
            url = match.group(0).rstrip(".,;:!?")
            file_name = _worldbook_file_name(url)
            if file_name is None:
                continue
            key = source_key(file_name)
            source = sources.get(key)
            if source is None:
                if len(sources) >= MAX_SOURCES:
                    continue
                source = sources[key] = RemoteBookSource(key=key, file_name=file_name[:MAX_FILE_NAME_CHARS])
            if url not in source.urls and len(source.urls) < MAX_URLS_PER_SOURCE:
                source.urls.append(url)
            if label not in source.scripts and len(source.scripts) < MAX_SCRIPTS_PER_SOURCE:
                source.scripts.append(label)
            if not source.version_hint:
                source.version_hint = _version_hint(url)
    return list(sources.values())


def card_raw_extensions(card_raw: Any) -> Mapping[str, Any]:
    """导入时存下的卡片原文里的 `extensions`（V2 / V3 在 `data` 下；V1 没有）"""
    if not isinstance(card_raw, Mapping):
        return {}
    data = card_raw.get("data")
    holder = data if isinstance(data, Mapping) else card_raw
    extensions = holder.get("extensions")
    return extensions if isinstance(extensions, Mapping) else {}


async def detect_remote_books_async(extensions: Mapping[str, Any]) -> list[RemoteBookSource]:
    """同 :func:`detect_remote_books`，放在线程里跑：几 MiB 的脚本扫一遍要几百毫秒，不能占着事件循环"""
    return await asyncio.to_thread(detect_remote_books, extensions)


# 人设 → ((扩展行编号, 更新时间), 认出的来源)。扩展行每次保存都会更新 `update_time`（导入、云端同步都走
# `save()`），所以卡片原文没变就不必再取一遍原文、再扫一遍脚本；按最近使用淘汰
_source_cache: "OrderedDict[int, tuple[tuple[int, str], list[RemoteBookSource]]]" = OrderedDict()


async def preset_sources(preset_id: int) -> list[RemoteBookSource]:
    """这个人设的卡片脚本要下载的世界书（从扩展行存着的卡片原文认，按扩展行的更新时间缓存）"""
    rows = await DBPresetCard.filter(preset_id=preset_id).order_by("id").limit(1).values("id", "update_time")
    if not rows:
        _source_cache.pop(preset_id, None)
        return []
    stamp = (int(rows[0]["id"]), str(rows[0]["update_time"]))
    cached = _source_cache.get(preset_id)
    if cached is None or cached[0] != stamp:
        card = await DBPresetCard.get_or_none(id=stamp[0])
        if card is None:
            return []
        sources = await detect_remote_books_async(card_raw_extensions(card.card_raw))
        _source_cache[preset_id] = (stamp, sources)
        while len(_source_cache) > MAX_SOURCE_CACHE:
            _source_cache.popitem(last=False)
        cached = (stamp, sources)
    _source_cache.move_to_end(preset_id)
    return [source.model_copy(deep=True) for source in cached[1]]


def clear_source_cache() -> None:
    """清空认出来源的缓存（用例用）"""
    _source_cache.clear()


async def find_source(preset_id: int, key: str) -> Optional[RemoteBookSource]:
    for source in await preset_sources(preset_id):
        if source.key == key:
            return source
    return None


# ---------------------------------------------------------------------------
# 已经导入的书
# ---------------------------------------------------------------------------


def _source_info(book: DBWbBook) -> Mapping[str, Any]:
    info = (book.ext or {}).get(REMOTE_SOURCE_KEY) if isinstance(book.ext, Mapping) else None
    return info if isinstance(info, Mapping) else {}


async def _imported_books(preset_id: int, owner: tuple[str, str]) -> dict[str, DBWbBook]:
    """这个人设上、归这个实例的书里，由远程来源导入的：`key → 书`（同一来源有几本时取最早那本）

    只看归属与当前作用域相同的书：租户看平台方人设时，看不到别的实例导进来的书。
    """
    books = (
        await DBWbBook.filter(
            scope_kind=ScopeKind.PRESET.value,
            preset_id=preset_id,
            adapter_key=owner[0],
            instance_key=owner[1],
        )
        .order_by("id")
        .all()
    )
    found: dict[str, DBWbBook] = {}
    for book in books:
        key = _source_info(book).get("key")
        if isinstance(key, str) and key and key not in found:
            found[key] = book
    return found


def _imported_view(book: DBWbBook, entries: int) -> RemoteBookImported:
    info = _source_info(book)
    downloaded_at: Optional[datetime] = None
    raw_time = info.get("downloaded_at")
    if isinstance(raw_time, str):
        try:
            downloaded_at = datetime.fromisoformat(raw_time)
        except ValueError:
            downloaded_at = None
    return RemoteBookImported(
        book_id=book.id,
        book_name=book.name,
        version=str(info.get("version") or ""),
        downloaded_at=downloaded_at,
        host=str(info.get("host") or ""),
        manual=bool(info.get("manual")),
        entries=entries,
    )


async def list_view(preset: DBPreset, owner: tuple[str, str]) -> list[RemoteBookView]:
    """人设页的列表：每个来源，加上已经导入的书"""
    sources = await preset_sources(preset.id)
    if not sources:
        return []
    imported = await _imported_books(preset.id, owner)
    views: list[RemoteBookView] = []
    for source in sources:
        book = imported.get(source.key)
        view = RemoteBookView(**source.model_dump())
        if book is not None:
            view.imported = _imported_view(book, await DBWbEntry.filter(book_id=book.id).count())
        views.append(view)
    return views


def proxy_available() -> bool:
    """后台配了能用的系统默认代理（HTTP(S) 代理；没写协议的按 http:// 算）；SOCKS 这条路用不了，不给勾"""
    raw = (config.DEFAULT_PROXY or "").strip()
    if not raw:
        return False
    scheme = raw.split("://", 1)[0].lower() if "://" in raw else "http"
    return scheme in ("http", "https")


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

# 每个租户同一时间一个远程下载（事件循环单线程，集合的增删之间没有 await，不需要锁）
_running: set[str] = set()


@contextmanager
def _download_slot(tenant_key: str) -> Iterator[None]:
    if tenant_key in _running:
        raise RemoteWorldbookBusyError()
    _running.add(tenant_key)
    try:
        yield
    finally:
        _running.discard(tenant_key)


def _proxy_url(use_proxy: bool, *, is_platform: bool) -> Optional[str]:
    """系统默认代理是平台方的出口，只有平台方能用；租户传了 `use_proxy` 直接拒（接口层已先拒一遍）"""
    if not use_proxy:
        return None
    if not is_platform:
        raise RemoteWorldbookDownloadError(reason="系统默认代理只有平台方能用")
    try:
        proxy = proxy_manager.build_default_proxy_url()
    except ValueError as exc:
        raise RemoteWorldbookDownloadError(reason=f"系统默认代理的配置有误：{exc}") from None
    if not proxy:
        raise RemoteWorldbookDownloadError(reason="后台没有配置系统默认代理（DEFAULT_PROXY），不能走代理下载")
    return proxy


def _failure_reason(exc: BaseException) -> str:
    """一个地址失败的原因；不带地址与查询串，「解析不到」与「指向不允许的地址」不分开说"""
    if isinstance(exc, st_format.WorldFileRejected):
        return f"下载到了，但不是能导入的世界书（{exc}）"
    if isinstance(exc, UnsafeDownloadUrlError):
        if "大小上限" in str(exc):
            return f"文件超过 {REMOTE_BOOK_MAX_BYTES // (1024 * 1024)} MiB"
        return "被下载规则拒绝（主机名解析不到、指向不允许访问的地址，或者跳转、内容编码不符合下载规则）"
    if isinstance(exc, httpx.ProxyError):
        return "代理出错（只支持 HTTP(S) 代理，或者代理连不上）"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        # `TimeoutError`：每个地址的时限（`asyncio.timeout`）或下载守卫等响应头的时限到了
        return "超时"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"服务器返回 HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "连不上"
    logger.opt(exception=exc).warning("远程世界书下载出了意外的错")
    return "下载出错"


@dataclass(frozen=True)
class _Fetched:
    parsed: st_format.ParsedWorld
    url: str
    size: int
    sha256: str


async def _parse(data: bytes, *, tenant_key: str) -> st_format.ParsedWorld:
    """解析与转换是纯 CPU 活：放进导入专用的执行器（每个租户同一时间一个）"""
    return await wb_purpose.run_import_analysis(st_format.parse_world_payload, data, tenant_key=tenant_key)


async def _fetch(source: RemoteBookSource, *, use_proxy: bool, is_platform: bool, tenant_key: str) -> _Fetched:
    """依次试主地址与镜像；租户触发的按租户的出站判据（`fetch_url_bytes(tenant=True)`），每个地址与整次都有时限"""
    proxy = _proxy_url(use_proxy, is_platform=is_platform)
    reasons: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REMOTE_TOTAL_TIMEOUT_SECONDS
    for url in source.urls:
        host = (urlsplit(url).hostname or "").lower() or "（地址无效）"
        remaining = deadline - loop.time()
        if remaining < REMOTE_MIN_URL_SECONDS:
            reasons.append(f"{host}：没来得及试（整次下载最多 {int(REMOTE_TOTAL_TIMEOUT_SECONDS)} 秒）")
            continue
        try:
            async with asyncio.timeout(min(REMOTE_URL_TIMEOUT_SECONDS, remaining)):
                data = await common_util.fetch_url_bytes(
                    url, max_bytes=REMOTE_BOOK_MAX_BYTES, proxy=proxy, tenant=not is_platform
                )
            # 解析不在时限里：8 MiB 以内很快，而且它在执行器里跑，取消等待也停不下那个线程
            parsed = await _parse(data, tenant_key=tenant_key)
        except AppError:
            # 导入分析名额被占（409）之类：不是这个地址的问题，原样交回
            raise
        except Exception as exc:  # noqa: BLE001 - 每个地址的失败都只记一句原因，换下一个地址
            reasons.append(f"{host}：{_failure_reason(exc)}")
            continue
        return _Fetched(parsed, url, len(data), hashlib.sha256(data).hexdigest())
    via = "（经系统代理）" if proxy else ""
    raise RemoteWorldbookDownloadError(reason=f"下载失败{via}，地址都试过了：\n" + "\n".join(reasons))


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


def _version_of(book_ext: Mapping[str, Any]) -> str:
    value = book_ext.get("version")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()[:64]


def _book_name(book_ext: Mapping[str, Any], file_name: str) -> str:
    raw = book_ext.get("book_name")
    name = raw.strip() if isinstance(raw, str) else ""
    if not name:
        name = file_name[: -len(".json")] if file_name.lower().endswith(".json") else file_name
    name = _CONTROL_CHARS.sub("", name).strip()
    return name[:MAX_NAME_CHARS] or "远程世界书"


async def _store(
    *,
    preset: DBPreset,
    source: RemoteBookSource,
    owner: tuple[str, str],
    fetched: _Fetched,
    existing: Optional[DBWbBook],
    manual: bool,
    username: str,
    is_platform: bool,
    tenant_key: str,
) -> RemoteBookImportResult:
    version = _version_of(fetched.parsed.book_ext)
    host = "" if manual else (urlsplit(fetched.url).hostname or "").lower()
    book_ext = dict(fetched.parsed.book_ext)
    book_ext[REMOTE_SOURCE_KEY] = {
        "key": source.key,
        "file_name": source.file_name,
        "urls": list(source.urls),
        "url": "" if manual else fetched.url,
        "host": host,
        "scripts": list(source.scripts),
        "version": version,
        "size": fetched.size,
        "sha256": fetched.sha256,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "manual": manual,
    }
    parsed = st_format.ParsedWorld(book_ext=book_ext, entries=fetched.parsed.entries)
    if existing is not None:
        # 替换在事务里重新取书（下载期间书可能被改名、改设置或删掉），这里也按库里现在的名字回报
        report = await wb_service.replace_book_entries(
            existing.id,
            parsed,
            is_platform=is_platform,
            username=username,
            tenant_key=tenant_key,
            trusted=False,
        )
        book_id = existing.id
        current = await DBWbBook.get_or_none(id=book_id)
        book_name = current.name if current is not None else existing.name
    else:
        book_name = _book_name(book_ext, source.file_name)
        scripts = "、".join(f"「{name}」" for name in source.scripts) or "卡片脚本"
        where = "手动上传" if manual else f"从 {host} 下载"
        description = f"{scripts}要下载的世界书，由 NA {where}，条目按参考资料导入。"
        report = await wb_service.import_new_book(
            owner,
            BookCreateRequest(
                name=book_name,
                description=description[:MAX_DESCRIPTION_CHARS],
                scope_kind=ScopeKind.PRESET,
                preset_id=preset.id,
                # 与世界书文件、卡内书导入同一口径：新导入的酒馆书默认扫描时带上说话人名字。
                scan_names=True,
            ),
            parsed,
            is_platform=is_platform,
            username=username,
            tenant_key=tenant_key,
            trusted=False,
        )
        book_id = report.book_id
    return RemoteBookImportResult(
        book_id=book_id,
        book_name=book_name,
        version=version,
        host=host,
        manual=manual,
        replaced=existing is not None,
        report=report,
    )


async def _target_book(preset_id: int, owner: tuple[str, str], key: str, *, replace: bool) -> Optional[DBWbBook]:
    """要替换的那本书；已经导入过又没要求替换时 409"""
    existing = (await _imported_books(preset_id, owner)).get(key)
    if existing is not None and not replace:
        raise RemoteWorldbookImportedError(book_name=existing.name)
    return existing


async def download_and_import(
    *,
    preset: DBPreset,
    source: RemoteBookSource,
    owner: tuple[str, str],
    use_proxy: bool,
    replace: bool,
    username: str,
    is_platform: bool,
    tenant_key: str,
) -> RemoteBookImportResult:
    """下载并导入；`replace` 为真时替换已经导入的那本书的条目（没导入过就新建）"""
    existing = await _target_book(preset.id, owner, source.key, replace=replace)
    with _download_slot(tenant_key):
        fetched = await _fetch(source, use_proxy=use_proxy, is_platform=is_platform, tenant_key=tenant_key)
        return await _store(
            preset=preset,
            source=source,
            owner=owner,
            fetched=fetched,
            existing=existing,
            manual=False,
            username=username,
            is_platform=is_platform,
            tenant_key=tenant_key,
        )


async def import_upload(
    *,
    preset: DBPreset,
    source: RemoteBookSource,
    owner: tuple[str, str],
    data: bytes,
    replace: bool,
    username: str,
    is_platform: bool,
    tenant_key: str,
) -> RemoteBookImportResult:
    """手动上传（下载不下来时）：同一条导入路径，来源记成手动上传

    不占下载名额（下载卡住时正要靠它），并发由导入分析的名额管（每个租户同一时间一个，409）。
    """
    existing = await _target_book(preset.id, owner, source.key, replace=replace)
    try:
        parsed = await _parse(data, tenant_key=tenant_key)
    except st_format.WorldFileRejected as exc:
        raise RemoteWorldbookDownloadError(reason=f"不是能导入的世界书：{exc}") from None
    fetched = _Fetched(parsed, "", len(data), hashlib.sha256(data).hexdigest())
    return await _store(
        preset=preset,
        source=source,
        owner=owner,
        fetched=fetched,
        existing=existing,
        manual=True,
        username=username,
        is_platform=is_platform,
        tenant_key=tenant_key,
    )


async def check_update(
    *,
    preset: DBPreset,
    source: RemoteBookSource,
    owner: tuple[str, str],
    use_proxy: bool,
    is_platform: bool,
    tenant_key: str,
) -> RemoteBookCheckResult:
    """再下载一遍，只比较、不导入：内容一字不差为 `same`，版本号不同为 `newer`，否则 `changed`"""
    existing = (await _imported_books(preset.id, owner)).get(source.key)
    with _download_slot(tenant_key):
        fetched = await _fetch(source, use_proxy=use_proxy, is_platform=is_platform, tenant_key=tenant_key)
    remote_version = _version_of(fetched.parsed.book_ext)
    host = (urlsplit(fetched.url).hostname or "").lower()
    if existing is None:
        return RemoteBookCheckResult(status="not_imported", remote_version=remote_version, host=host)
    info = _source_info(existing)
    local_version = str(info.get("version") or "")
    status: Literal["same", "newer", "changed"]
    if info.get("sha256") == fetched.sha256:
        status = "same"
    elif remote_version != local_version:
        status = "newer"
    else:
        status = "changed"
    return RemoteBookCheckResult(status=status, local_version=local_version, remote_version=remote_version, host=host)


__all__ = [
    "MAX_SOURCES",
    "MAX_URLS_PER_SOURCE",
    "REMOTE_BOOK_MAX_BYTES",
    "REMOTE_SOURCE_KEY",
    "card_raw_extensions",
    "check_update",
    "clear_source_cache",
    "detect_remote_books",
    "detect_remote_books_async",
    "download_and_import",
    "find_source",
    "import_upload",
    "list_view",
    "preset_sources",
    "proxy_available",
    "source_key",
]
