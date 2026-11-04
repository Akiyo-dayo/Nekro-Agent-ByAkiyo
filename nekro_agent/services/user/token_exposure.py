"""曾对租户可见的 OneBot 令牌：只记指纹的集合

超级用户的来源判定要求「这条连接背后的令牌只有平台方持有」（`services/user/super_users.py`）。
令牌一旦被租户拿到过，之后无论实例从租户收回、租户实例改用自己的令牌还是被删，租户手里那一枚都还在。
所以这里记的是**历史**而不是现状：凡是曾经能被租户拿到的令牌——实例自己的令牌与部署级令牌一视同仁——
把 SHA-256 指纹记进同一个集合，判定时令牌指纹在集合里就不可信。

不给实例另打标记：令牌一换，指纹就不在集合里了，所以用任何方式换了令牌都会自动恢复可信，
不依赖某个专门的轮换入口。只存指纹不存明文：这份文件泄露了也拼不出令牌。

## 什么时候记

- 实例划入、收回租户时，记它当时生效的令牌（`routers/console/tenants.py`）；
- 软删的租户实例被复活时，记它此刻生效的令牌；
- 账号落到租户实例上时（并入、改绑、登录回写），记这个账号的协议端此前出示过的令牌
  （`record_account_landing_on_tenant`）：租户自己跑的协议端先凭部署级令牌连上来、被登记成待认领，
  再被并进租户实例，部署级令牌就在租户手里了；
- 每次启动扫描一遍租户名下的实例；
- 判定时发现有租户实例此刻正靠部署级令牌认证，补记部署级令牌（`super_users.py`）。

「当时生效的令牌」与握手校验用同一个值（`container_manager.resolve_access_token`）：
实例有自己的令牌就是那一枚，没有就是部署级令牌。

## 建档与重建

文件里的 `seeded` 为真表示建档完成。没完成时（文件不存在、刚挪开坏文件、上次扫描有行读不出来）做的是**全量扫描**：
租户名下的实例连同软删的一起扫——软删的那些曾经在租户手里，它们当时靠的部署级令牌也就交出去过；
全部行都扫成功才把 `seeded` 置真。全量扫描发生在启动时，也发生在判定前（`ensure_exposure_record`）：
记录一旦不可用，下一次判定就马上重扫重建。

库里没有「划入 / 收回租户」的历史记录可以追溯，所以全量扫描补不回「从租户收回过的实例」：
每次全量扫描完成且库里有租户时出一条告警，提示更换对应令牌。

建档完成之后的启动扫描只看未软删的实例：软删实例当年的令牌在它还活着的时候就已经记过了，
再按软删行补记部署级令牌，会把换过之后的**新**部署级令牌也判成暴露过，换令牌就永远恢复不了可信。

## 失败方向：一律按不可信

- 文件不存在或读不出来：读不出来的那份先挪开（改名留档），然后按「待重建」处理；「待重建」期间判定一律不可信，
  直到一次全量扫描成功；
- **管理员发起的归属变更**（划入 / 收回租户、并入、复活租户的软删实例）先记后改：记录写不进去就拒绝这次操作，
  库里什么都不改（`refuse_on_failure=True`）。否则收回之后实例回到平台方名下、前租户手里的令牌又没记下，
  而全量重建补不回它（那时它已经不在租户名下了）；
- 自动路径（登录回写、改绑）写不进去时操作照常完成：记 error、置为待重建，这一笔的指纹留在内存里，
  下一次写成功（包括重建）时一起落盘。进程在那之前重启就丢了，这是已知残留。
"""

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, FrozenSet, Iterable, List, Optional, Set

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.schemas.errors import OperationFailedError

if TYPE_CHECKING:
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

logger = get_sub_logger("user.token_exposure")

ONEBOT_ADAPTER_KEY = "onebot_v11"
EXPOSURE_FILE_NAME = "onebot_tenant_exposed_tokens.json"
_FORMAT_VERSION = 1

# 写记录失败过、又没能把「待重建」落进文件：只在内存里记着，下一次全量扫描成功时清掉
_rebuild_pending = False
_rebuild_lock: Optional[asyncio.Lock] = None
# 自动路径写失败的那几笔指纹：保留到下一次写成功为止
_unwritten_fingerprints: Set[str] = set()


@dataclass(frozen=True)
class _ExposureState:
    fingerprints: FrozenSet[str]
    seeded: bool


def token_fingerprint(token: str) -> str:
    """令牌的 SHA-256 指纹；空令牌没有指纹（空串）

    不做任何规整：握手比对的就是原样的字符串，两侧必须是同一个值。
    """
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def exposure_file() -> Path:
    return Path(OsEnv.DATA_DIR) / "system" / EXPOSURE_FILE_NAME


def _read_state() -> Optional[_ExposureState]:
    """文件不存在返回 None；读不出来时把它挪开留档，同样返回 None"""
    path = exposure_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fingerprints = data["fingerprints"]
        seeded = data.get("seeded", False)
        if not isinstance(fingerprints, list) or not all(isinstance(item, str) for item in fingerprints):
            raise TypeError("fingerprints 必须是字符串列表")
        if not isinstance(seeded, bool):
            raise TypeError("seeded 必须是布尔值")
    except Exception:  # noqa: BLE001 - 读不出来就挪开重建，见模块文档
        aside = path.with_name(f"{path.name}.broken-{time.strftime('%Y%m%d%H%M%S')}")
        try:
            os.replace(path, aside)
        except OSError:
            logger.error(f"曾对租户可见的令牌记录读不出来，也挪不开（{path}），OneBot 上的超级用户一律不生效", exc_info=True)
        else:
            logger.error(f"曾对租户可见的令牌记录读不出来，已挪到 {aside}，马上重建；重建成功之前 OneBot 上的超级用户一律不生效", exc_info=True)
        return None
    return _ExposureState(fingerprints=frozenset(fingerprints), seeded=seeded)


def load_exposed_fingerprints() -> Optional[FrozenSet[str]]:
    """判定用：读出曾对租户可见的令牌指纹；记录不存在、读不出来、待重建都返回 None（按全部暴露处理）"""
    if _rebuild_pending:
        return None
    state = _read_state()
    if state is None or not state.seeded:
        return None
    # 不必并上 `_unwritten_fingerprints`：它非空时一定处于待重建（上面已经返回 None），重建成功时它们一起落盘
    return state.fingerprints


def _write(fingerprints: Iterable[str], *, seeded: bool) -> None:
    """先写同目录临时文件再整体替换：写到一半被打断时，旧文件仍然完整"""
    path = exposure_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        json.dumps(
            {"version": _FORMAT_VERSION, "seeded": seeded, "fingerprints": sorted(fingerprints)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def record_exposed_fingerprints(fingerprints: Iterable[str], *, reason: str, refuse_on_failure: bool = False) -> None:
    """把这些指纹记成「曾对租户可见」；已经记过的不重写文件

    记录不存在或刚挪开坏文件时，新写一份带着这些指纹、`seeded=false` 的记录，等全量扫描补齐。
    记不下来时：

    - `refuse_on_failure=True`（管理员发起的归属变更）：抛 `OperationFailedError`，调用方还没改库，这次操作就此拒绝；
    - 否则（自动路径）不抛：记 error、置为待重建（重建成功之前一律判不可信），这一笔留在内存里等下一次写成功。
    """
    global _rebuild_pending

    wanted = set(fingerprints) - {""}
    if not wanted:
        return
    try:
        state = _read_state()
        current = state.fingerprints if state else frozenset()
        target = current | wanted | _unwritten_fingerprints
        if target == current:
            return
        _write(target, seeded=state.seeded if state else False)
    except Exception as e:
        if refuse_on_failure:
            logger.error(f"记不下曾对租户可见的 OneBot 令牌（{reason}），这次操作已拒绝", exc_info=True)
            raise OperationFailedError(
                operation="写入令牌曝光记录",
                detail="曾对租户可见的接入令牌记不下来，这次操作没有执行，请检查数据目录能否写入",
            ) from e
        _unwritten_fingerprints.update(wanted)
        _rebuild_pending = True
        logger.error(
            f"记不下曾对租户可见的 OneBot 令牌（{reason}）：已置为待重建，重建成功之前 OneBot 上的超级用户一律不生效；"
            "这一笔留在内存里等下一次写成功，进程在那之前重启就丢了，涉及的实例请更换令牌",
            exc_info=True,
        )
        return
    _unwritten_fingerprints.clear()
    logger.warning(
        f"记下 {len(target - current)} 枚曾对租户可见的 OneBot 令牌（{reason}）；靠这些令牌认证的平台方实例里，超级用户命令不再生效",
    )


def record_exposed_tokens(tokens: Iterable[str], *, reason: str, refuse_on_failure: bool = False) -> None:
    """把这些令牌记成「曾对租户可见」；空令牌跳过"""
    record_exposed_fingerprints(
        {token_fingerprint(token) for token in tokens}, reason=reason, refuse_on_failure=refuse_on_failure,
    )


def record_instances_exposure(
    instances: Iterable["DBAdapterInstance"], *, reason: str, refuse_on_failure: bool = False,
) -> None:
    """记下这些 OneBot 实例此刻生效的令牌（有自己的令牌记自己那枚，没有就记部署级令牌）"""
    from nekro_agent.services.console.container_manager import resolve_access_token

    rows = [row for row in instances if row.adapter_key == ONEBOT_ADAPTER_KEY]
    if not rows:
        return
    record_exposed_tokens(
        [resolve_access_token(row) for row in rows],
        reason=f"{reason}：{', '.join(sorted({row.instance_key for row in rows}))}",
        refuse_on_failure=refuse_on_failure,
    )


async def record_account_landing_on_tenant(
    instance: "DBAdapterInstance", account: str, *, reason: str, refuse_on_failure: bool = False,
) -> None:
    """账号要落到租户实例上了（并入、改绑、登录回写）：这个账号的协议端此前出示过的令牌，此刻都在那个租户手里

    两路证据：本进程里这个账号握手时出示过的令牌（`access_token.fingerprints_presented_by`，重启清空），
    以及库里其他绑着这个账号的 OneBot 行（含待认领、已归档、软删）此刻生效的令牌——
    待认领行没有自己的令牌，它生效的正是部署级令牌。在改账号**之前**调用。
    """
    if instance.tenant_id is None or instance.adapter_key != ONEBOT_ADAPTER_KEY or not account:
        return

    from nekro_agent.adapters.onebot_v11.core.access_token import fingerprints_presented_by
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance
    from nekro_agent.services.console.container_manager import resolve_access_token

    holders = await DBAdapterInstance.filter(adapter_key=ONEBOT_ADAPTER_KEY, provider_account_id=account).exclude(
        id=instance.id,
    )
    fingerprints: Set[str] = set(fingerprints_presented_by(account))
    fingerprints.update(token_fingerprint(resolve_access_token(row)) for row in holders)
    record_exposed_fingerprints(fingerprints, reason=f"{reason}（账号 {account}）", refuse_on_failure=refuse_on_failure)


async def scan_tenant_token_exposure() -> None:
    """记下租户名下实例此刻生效的令牌；记录没建完档（或待重建）时做全量扫描，见模块文档

    启动时调用一次；判定前发现记录不可用时经 `ensure_exposure_record` 再调。写不进去时抛异常。
    """
    global _rebuild_pending

    from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
    from nekro_agent.models.db_tenant import DBTenant
    from nekro_agent.services.console.container_manager import resolve_access_token

    state = _read_state()
    full = state is None or not state.seeded or _rebuild_pending
    query = DBAdapterInstance.filter(adapter_key=ONEBOT_ADAPTER_KEY, tenant_id__not_isnull=True)
    if not full:
        query = query.exclude(status=TOMBSTONE_STATUS)

    fingerprints: Set[str] = set()
    failed: List[str] = []
    for row in await query:
        try:
            fingerprints.add(token_fingerprint(resolve_access_token(row)))
        except Exception:  # noqa: BLE001 - 一行读不出来不拖垮其余行，但建档不算完成
            logger.error(f"读取租户实例 {row.instance_key} 的接入令牌失败，这一行没能记下", exc_info=True)
            failed.append(row.instance_key)
    fingerprints.discard("")

    current = state.fingerprints if state else frozenset()
    seeded = not full or not failed
    fingerprints |= _unwritten_fingerprints
    added = fingerprints - current
    _write(current | fingerprints, seeded=seeded)
    _unwritten_fingerprints.clear()
    if full and seeded:
        _rebuild_pending = False
    if added:
        logger.warning(f"记下 {len(added)} 枚曾对租户可见的 OneBot 令牌（{'全量扫描' if full else '启动扫描'}）")
    if failed:
        logger.error(
            f"有租户实例的令牌没能记下（{', '.join(failed)}）"
            + ("，记录没能建完，下次判定或启动时重扫；在此之前 OneBot 上的超级用户一律不生效" if full else ""),
        )

    if full and seeded and await DBTenant.all().exists():
        logger.warning(
            "曾对租户可见的 OneBot 令牌记录是新建（或丢失后重建）的，之前的历史无从追溯：在那之前从租户收回过的实例、"
            "或者租户实例用过部署级令牌的，请更换对应令牌（部署级令牌改 ONEBOT_ACCESS_TOKEN 后重启；"
            "实例自己的令牌在总后台实例详情里更换接入令牌），否则那些实例里的超级用户命令仍可能被冒充",
        )


async def ensure_exposure_record() -> bool:
    """判定前调用：记录不存在、读不出来、没建完档或待重建时，马上重扫重建；返回记录此刻是否可用"""
    global _rebuild_lock

    if load_exposed_fingerprints() is not None:
        return True
    if _rebuild_lock is None:
        _rebuild_lock = asyncio.Lock()
    async with _rebuild_lock:
        if load_exposed_fingerprints() is not None:
            return True
        try:
            await scan_tenant_token_exposure()
        except Exception:  # noqa: BLE001 - 重建失败就继续按不可信，下次判定再试
            logger.error("重建曾对租户可见的 OneBot 令牌记录失败，OneBot 上的超级用户继续不生效", exc_info=True)
            return False
    return load_exposed_fingerprints() is not None
