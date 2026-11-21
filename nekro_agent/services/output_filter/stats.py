"""命中统计：只记规则与次数，不记消息原文

统计落在数据目录的一份 JSON 里，按「适配器/实例」分桶。它同时是后台「命中统计」与
「被拒规则」的数据来源。写盘走 `atomic_write_text`，避免进程中途退出留下半截 JSON。
"""

import asyncio
import json
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from nekro_agent.core.core_utils import atomic_write_text
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

logger = get_sub_logger("output_filter")

HITS_PATH = Path(OsEnv.DATA_DIR) / "output_filter" / "hits.json"
MAX_RULES_PER_INSTANCE = 1000


def _instance_key(adapter_key: str, instance_key: str) -> str:
    return f"{adapter_key}/{instance_key}"


def _split_instance_key(key: str) -> tuple[str, str]:
    adapter_key, _, instance_key = key.partition("/")
    return adapter_key, instance_key


class HitStore:
    """命中计数的内存表 + 落盘"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._instances: dict[str, dict[str, dict[str, Any]]] = {}
        self._loaded_path: Optional[Path] = None

    def _ensure_loaded_locked(self) -> None:
        """惰性加载；路径被替换后（测试注入）重新读一份，避免沿用上一个路径的内存表"""
        path = HITS_PATH
        if self._loaded_path == path:
            return
        self._instances = self._load(path)
        self._loaded_path = path

    @staticmethod
    def _load(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 文件坏了按空表处理，统计不能挡发送
            logger.warning(f"读取输出过滤命中统计失败，按空表处理: path={path}, 错误: {exc}")
            return {}

        instances = raw.get("instances") if isinstance(raw, dict) else None
        if not isinstance(instances, dict):
            logger.warning(f"输出过滤命中统计结构不正确，按空表处理: path={path}")
            return {}

        loaded: dict[str, dict[str, dict[str, Any]]] = {}
        for key, bucket in instances.items():
            if not isinstance(key, str) or not isinstance(bucket, dict):
                continue
            rules: dict[str, dict[str, Any]] = {}
            for rule_key, entry in bucket.items():
                if not isinstance(rule_key, str) or not isinstance(entry, dict):
                    continue
                count = entry.get("count")
                if not isinstance(count, int):
                    continue
                last_hit_at = entry.get("last_hit_at")
                rules[rule_key] = {
                    "count": count,
                    "last_hit_at": last_hit_at if isinstance(last_hit_at, str) else "",
                }
            loaded[key] = rules
        return loaded

    async def record(self, adapter_key: str, instance_key: str, hits: Counter[str]) -> None:
        """记一次命中；空计数直接返回，不产生任何磁盘写入"""
        if not hits:
            return
        key = _instance_key(adapter_key, instance_key)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._ensure_loaded_locked()
            bucket = self._instances.setdefault(key, {})
            for rule_key, count in hits.items():
                entry = bucket.setdefault(rule_key, {"count": 0, "last_hit_at": now})
                entry["count"] = int(entry.get("count", 0)) + int(count)
                entry["last_hit_at"] = now
            self._prune(bucket)
        await asyncio.to_thread(self._flush)

    @staticmethod
    def _prune(bucket: dict[str, dict[str, Any]]) -> None:
        """单实例最多保留 1000 条规则，按最近命中时间淘汰最旧的"""
        if len(bucket) <= MAX_RULES_PER_INSTANCE:
            return
        ordered = sorted(bucket.items(), key=lambda item: str(item[1].get("last_hit_at") or ""))
        for rule_key, _entry in ordered[: len(bucket) - MAX_RULES_PER_INSTANCE]:
            bucket.pop(rule_key, None)

    def _flush(self) -> None:
        """在锁内取快照，锁外写盘；写盘失败只记日志"""
        path = HITS_PATH
        with self._lock:
            self._ensure_loaded_locked()
            snapshot = {
                "version": 1,
                "instances": {
                    key: {rule_key: dict(entry) for rule_key, entry in bucket.items()}
                    for key, bucket in self._instances.items()
                },
            }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 - 统计失败不能影响发送
            logger.warning(f"写入输出过滤命中统计失败: path={path}, 错误: {exc}")

    def snapshot(
        self,
        adapter_key: Optional[str] = None,
        instance_key: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """按可选作用域导出统计，供后台接口直接序列化"""
        with self._lock:
            self._ensure_loaded_locked()
            instances: list[dict[str, Any]] = []
            for key, bucket in self._instances.items():
                current_adapter, current_instance = _split_instance_key(key)
                if adapter_key is not None and current_adapter != adapter_key:
                    continue
                if instance_key is not None and current_instance != instance_key:
                    continue
                rules = []
                for rule_key, entry in bucket.items():
                    kind, _, rule = rule_key.partition(":")
                    rules.append(
                        {
                            "kind": kind,
                            "rule": rule,
                            "count": int(entry.get("count", 0)),
                            "last_hit_at": entry.get("last_hit_at") or None,
                        },
                    )
                rules.sort(key=lambda item: (-item["count"], item["rule"]))
                instances.append(
                    {
                        "adapter_key": current_adapter,
                        "instance_key": current_instance,
                        "rules": rules,
                    },
                )
        instances.sort(key=lambda item: (item["adapter_key"], item["instance_key"]))
        return instances


hit_store = HitStore()


async def record_hits_for_chat(chat_key: str, hits: Counter[str]) -> None:
    """按频道记命中：实例标识从频道记录反查，查不到按单实例部署的空串处理"""
    if not hits:
        return
    try:
        from nekro_agent.adapters import resolve_adapter_key_from_chat_key
        from nekro_agent.models.db_chat_channel import DBChatChannel

        adapter_key = resolve_adapter_key_from_chat_key(chat_key)
        channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
        instance_key = channel.instance_key if channel else ""
        await hit_store.record(adapter_key, instance_key, hits)
    except Exception as exc:  # noqa: BLE001 - 统计失败不能影响发送
        logger.warning(f"记录输出过滤命中失败: chat_key={chat_key}, 错误: {exc}")


async def record_hits_for_instance(adapter_key: str, instance_key: str, hits: Counter[str]) -> None:
    """按实例记命中（OneBot 钩子用）"""
    if not hits:
        return
    try:
        await hit_store.record(adapter_key, instance_key, hits)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"记录输出过滤命中失败: {adapter_key}/{instance_key}, 错误: {exc}")
