"""SSH 网关的本轮授权判据

## 为什么按「这一轮是谁叫醒的」判

容器里的 CC 是异步的：委托的那一刻只是提交，真正执行往往在那一轮结束很久之后。所以
授权不能沿用 D6「回合结束就收回」的窗口，必须拍成一张跟着 CC 任务走的租约（`lease.py`）。
这张租约在拍下的那一刻要回答一个问题：**这一轮是不是凭据主人本人叫醒的**。

本模块只做两件事：

- 把「本轮触发快照」与一组凭据行对照，算出哪些行被这一轮覆盖（`compute_*_coverage`）；
- 给网关在每次连接、每个通道前复核一份已生效的租约（`recheck`）。

两类行分得很开：平台方的服务器只认所有者名单里的本人；用户的服务器只认凭据主人本人，
而且必须还是同一个实例上的、此刻仍在用户白名单里。

## 为什么这么严

防抖会把窗口内几个人的消息并成一轮。只要混进一条别人的消息、一条系统推送、一个定时器，
这一轮就不算「本人叫醒的」，一行都不覆盖。宁可少给一次，也不能把服务器交给一个只是
在同一个群里说话的人。

## 值守模式

平台方行可以标成「值守」（行上 `duty_mode`，空则跟随 `SSH_GATEWAY_DEFAULT_DUTY_MODE`）。
值守行在严格判据不过时还有第二条路：本轮快照非空、不「不完整」、至少一条 `human` 触发，
频道所属实例归平台方、且在 `SSH_GATEWAY_DUTY_INSTANCES`（非空时）里。不看是谁、不看来源
可信。按这条覆盖的行记进 `Coverage.duty_ids`；严格判据过了的（所有者本人叫醒）照旧按严格记。
复核时 `duty_ids` 里的行不比所有者名单，改判「仍是值守、实例仍合格」。用户行与严格行一字不变。

## 关闸与故障

总开关关着时不做任何查询，直接返回空覆盖：存量部署的行为必须与改造前逐字一致。
判定过程里任何一步出错都按「不覆盖」处理，网关那边没有「判不出来就放行」的分支。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, List, Optional

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import NON_DISPATCHABLE_STATUSES, DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.services.ops_agent.triggers import TRIGGER_KIND_HUMAN, RoundTrigger, TriggerBatch
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway.secrets import secret_is_undecryptable
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.super_users import matches_super_users
from nekro_agent.services.user.tenant_perm import get_tenant_scope
from nekro_agent.services.workspace.credential_policy import platform_credentials_allowed

if TYPE_CHECKING:
    from nekro_agent.models.db_chat_channel import DBChatChannel
    from nekro_agent.services.ssh_gateway.lease import SshLease


@dataclass(frozen=True)
class Coverage:
    """一轮叫醒覆盖到的凭据行

    Attributes:
        credential_ids: 被覆盖的行 id。租约把这组 id 定格在拍下的那一刻，任务中途新登记
            的行不在里面
        triggered_by: 本轮快照里的发送者账号，去重保序。审计用它回答「谁叫醒的」
        duty_ids: 其中按值守判据覆盖的行（`credential_ids` 的子集）；所有者本人叫醒的不算
    """

    credential_ids: frozenset[int] = frozenset()
    triggered_by: tuple[str, ...] = ()
    duty_ids: frozenset[int] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.credential_ids)


def triggered_by_accounts(batch: TriggerBatch) -> tuple[str, ...]:
    """快照里的发送者账号，去重保序"""
    seen: set[str] = set()
    ordered: List[str] = []
    for trigger in batch.triggers:
        account = str(trigger.sender_id or "")
        if not account or account in seen:
            continue
        seen.add(account)
        ordered.append(account)
    return tuple(ordered)


def batch_all_trusted_by(
    batch: TriggerBatch,
    accepts: Callable[[RoundTrigger], bool],
    *,
    require_trusted_source: bool = True,
) -> bool:
    """这一批是不是「完整的、每一条都是可信人类、且每一条都满足 accepts」

    四条缺一不可：批次非空、不是「不完整」、每一条都是 `human`、每一条来源可信。
    只做到「至少有一条满足」是不够的：同一条别人的消息照样并进了这一轮。

    `require_trusted_source=False` 只给租户实例上的用户行用（#211 ②，所有者定）：租户自己的
    协议端本来就不算可信来源，要求它等于这些行永远用不了。代价是租户能伪造发送者——在租户
    Bot 上登记服务器，就等于信任这个租户。其余三条照旧。
    """
    if not batch.triggers or batch.incomplete:
        return False
    for trigger in batch.triggers:
        if trigger.kind != TRIGGER_KIND_HUMAN:
            return False
        if require_trusted_source and not trigger.source_trusted:
            return False
        if not accepts(trigger):
            return False
    return True


def covers_platform_row(batch: TriggerBatch, owner_entries: Iterable[str]) -> bool:
    """平台方行：本轮每一条触发都来自所有者名单里的本人

    名单为空等于「平台方服务器谁都用不了」，不是「不限制」。
    """
    entries = [str(item) for item in owner_entries if str(item).strip()]
    if not entries:
        return False
    return batch_all_trusted_by(
        batch,
        lambda trigger: matches_super_users(trigger.adapter_key, trigger.sender_id, entries),
    )


def effective_duty_mode(row: DBWsSshCredential) -> str:
    """这一行此刻生效的模式：只有平台方行可能是值守；认不出的取值一律按严格"""
    if str(row.owner_kind or "") != C.OWNER_PLATFORM:
        return C.DUTY_MODE_STRICT
    mode = str(getattr(row, "duty_mode", "") or "").strip()
    if mode == C.DUTY_MODE_INHERIT:
        mode = str(config.SSH_GATEWAY_DEFAULT_DUTY_MODE or "").strip()
    return C.DUTY_MODE_DUTY if mode == C.DUTY_MODE_DUTY else C.DUTY_MODE_STRICT


def duty_instance_entries() -> List[str]:
    """`SSH_GATEWAY_DUTY_INSTANCES` 去空白、去空项"""
    return [str(item).strip() for item in (config.SSH_GATEWAY_DUTY_INSTANCES or []) if str(item).strip()]


def duty_instance_ref(adapter_key: str, instance_key: str) -> str:
    """名单条目的写法：`适配器:实例`（与邮箱的 `SEND_SHARE_INSTANCES` 同一种）"""
    return f"{adapter_key}:{instance_key}"


async def duty_instance_eligible(adapter_key: str, instance_key: str) -> bool:
    """这个实例上的频道此刻能不能触发值守：实例行存在、归平台方、名单非空时在名单里

    与 `instance_belongs_to_tenant` 不同，查不到实例行按「不合格」处理：这是放宽的那一侧，
    判不出来就不放。名单只能收窄：租户实例写进名单也不合格。
    """
    if not adapter_key or not instance_key:
        return False
    entries = duty_instance_entries()
    if entries and duty_instance_ref(adapter_key, instance_key) not in entries:
        return False
    row = await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).first()
    if row is None or row.tenant_id is not None:
        return False
    # 停用、归档、软删除的实例不再能触发值守
    return bool(row.enabled) and str(row.status or "").strip().lower() not in NON_DISPATCHABLE_STATUSES


def covers_duty_row(batch: TriggerBatch) -> bool:
    """值守行的触发条件：快照非空、不是「不完整」、至少一条 `human`

    不要求触发的人是所有者，也不要求每一条都是真人、来源可信；只靠定时器、系统推送、
    不可信推送叫醒的一轮不给（值守的前提是有人来找 Bot）。实例条件由调用方另判。
    """
    if not batch.triggers or batch.incomplete:
        return False
    return any(trigger.kind == TRIGGER_KIND_HUMAN for trigger in batch.triggers)


def covers_user_row(
    batch: TriggerBatch,
    *,
    row_adapter_key: str,
    row_instance_key: str,
    owner_account: str,
    channel_adapter_key: str,
    channel_instance_key: str,
    user_entries: Iterable[str],
    tenant_instance: bool = False,
) -> bool:
    """用户行：本轮每一条触发都是凭据主人本人，且频道与行属于同一个实例

    频道实例与行实例必须一致：用户行跟着实例走，别的实例上的同名账号不是同一个人。
    凭据主人此刻要仍在用户白名单里；名单为空等于用户服务器功能关。
    行所在实例归租户时（`tenant_instance`）不要求触发来源可信（#211 ②），其余判据照旧。
    """
    entries = [str(item) for item in user_entries if str(item).strip()]
    if not entries or not owner_account:
        return False
    if (channel_adapter_key, channel_instance_key) != (row_adapter_key, row_instance_key):
        return False
    if not matches_super_users(row_adapter_key, owner_account, entries):
        return False
    return batch_all_trusted_by(
        batch,
        lambda trigger: (
            trigger.sender_id == owner_account
            and (trigger.adapter_key, trigger.instance_key) == (row_adapter_key, row_instance_key)
        ),
        require_trusted_source=not tenant_instance,
    )


async def instance_belongs_to_tenant(adapter_key: str, instance_key: str) -> bool:
    """这个实例对此刻归不归租户；空实例、查不到实例行都按平台方处理"""
    if not instance_key:
        return False
    row = await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).first()
    return row is not None and row.tenant_id is not None


def _row_is_decryptable(row: DBWsSshCredential) -> bool:
    """三个 `_enc` 列里任何一列解不开，整行都不能用"""
    return not any(
        secret_is_undecryptable(str(getattr(row, column) or ""))
        for column in C.CREDENTIAL_SECRET_COLUMNS
    )


def _user_row_covered(
    batch: TriggerBatch,
    row: DBWsSshCredential,
    channel: "DBChatChannel",
    *,
    tenant_instance: bool,
) -> bool:
    return covers_user_row(
        batch,
        row_adapter_key=str(row.adapter_key or ""),
        row_instance_key=str(row.instance_key or ""),
        owner_account=str(row.owner_account or ""),
        channel_adapter_key=str(channel.adapter_key or ""),
        channel_instance_key=str(channel.instance_key or ""),
        user_entries=config.SSH_GATEWAY_USER_ACCOUNTS,
        tenant_instance=tenant_instance,
    )


async def compute_chat_coverage(workspace_id: int, db_chat_channel: "DBChatChannel") -> Coverage:
    """聊天委托这一刻，这个工作区上被本轮覆盖的凭据行

    总开关关着时直接返回空覆盖，一次数据库都不碰。平台方行要求工作区此刻仍过
    `platform_credentials_allowed`，这个判据一次算好给所有平台方行共用。
    """
    if not bool(config.SSH_GATEWAY_ENABLED):
        return Coverage()

    chat_key = str(getattr(db_chat_channel, "chat_key", "") or "")
    batch = round_trigger_book_batch(chat_key)
    triggered_by = triggered_by_accounts(batch)

    rows = await DBWsSshCredential.filter(
        workspace_id=workspace_id,
        status=C.STATUS_ACTIVE,
    ).order_by("id")
    if not rows:
        return Coverage(credential_ids=frozenset(), triggered_by=triggered_by)

    workspace: Optional[DBWorkspace] = None
    platform_allowed = False
    strict_ok = False
    duty_ok: Optional[bool] = None  # 用到值守行时才查实例
    if any(row.owner_kind == C.OWNER_PLATFORM for row in rows):
        workspace = await DBWorkspace.get_or_none(id=workspace_id)
        platform_allowed = workspace is not None and await platform_credentials_allowed(workspace)
        strict_ok = platform_allowed and covers_platform_row(batch, config.SSH_GATEWAY_OWNER_ACCOUNTS)

    covered: set[int] = set()
    duty: set[int] = set()
    tenant_instances: dict[tuple[str, str], bool] = {}
    for row in rows:
        if not _row_is_decryptable(row):
            # 解不开的行不是「可用凭据」：不把密文当钥匙使，也不在这里替人删
            continue
        if row.owner_kind == C.OWNER_PLATFORM:
            if strict_ok:
                # 所有者本人叫醒的按严格记，值守行也不进 duty_ids
                covered.add(int(row.id))
            elif platform_allowed and effective_duty_mode(row) == C.DUTY_MODE_DUTY:
                if duty_ok is None:
                    duty_ok = covers_duty_row(batch) and await duty_instance_eligible(
                        str(getattr(db_chat_channel, "adapter_key", "") or ""),
                        str(getattr(db_chat_channel, "instance_key", "") or ""),
                    )
                if duty_ok:
                    covered.add(int(row.id))
                    duty.add(int(row.id))
        elif row.owner_kind == C.OWNER_USER:
            pair = (str(row.adapter_key or ""), str(row.instance_key or ""))
            if pair not in tenant_instances:
                tenant_instances[pair] = await instance_belongs_to_tenant(*pair)
            if _user_row_covered(batch, row, db_chat_channel, tenant_instance=tenant_instances[pair]):
                covered.add(int(row.id))
    return Coverage(credential_ids=frozenset(covered), triggered_by=triggered_by, duty_ids=frozenset(duty))


def round_trigger_book_batch(chat_key: str) -> TriggerBatch:
    """读本轮快照；没开始过回合时当空批处理（判定侧只关心「有没有人叫醒」）"""
    from nekro_agent.services.ops_agent.triggers import round_trigger_book

    return round_trigger_book.current_batch(chat_key) or TriggerBatch(triggers=[])


def _is_platform_console_user(user: DBUser) -> bool:
    """后台直发的发起人是不是平台方账号（作用域为全局，权限到超级用户）"""
    try:
        scope = get_tenant_scope(user)
    except Exception:  # noqa: BLE001 - 脏数据给不出合法作用域，按非平台方处理
        return False
    if scope is not None:
        return False
    return int(user.perm_level) >= int(Role.Super)


async def compute_console_coverage(workspace_id: int, user: DBUser) -> Coverage:
    """后台直接给 CC 发任务时的覆盖

    这一路没有「这一轮是谁叫醒的」，只有发起人。所有者定下的口径是：平台方后台账号可用
    这个工作区上的平台方服务器；用户服务器一律不能。租户账号即使在自己的工作区里也不覆盖。
    """
    if not bool(config.SSH_GATEWAY_ENABLED):
        return Coverage()
    if not _is_platform_console_user(user):
        return Coverage()

    workspace = await DBWorkspace.get_or_none(id=workspace_id)
    if workspace is None or not await platform_credentials_allowed(workspace):
        return Coverage()

    rows = await DBWsSshCredential.filter(
        workspace_id=workspace_id,
        owner_kind=C.OWNER_PLATFORM,
        status=C.STATUS_ACTIVE,
    ).order_by("id")
    covered = {int(row.id) for row in rows if _row_is_decryptable(row)}
    return Coverage(credential_ids=frozenset(covered), triggered_by=())


def _lease_expired(lease: "SshLease") -> bool:
    """租约是否过了最长有效期；时间源与租约模块共用，测试才能注入"""
    from nekro_agent.services.ssh_gateway.lease import lease_expired

    return lease_expired(lease)


async def _console_actor_still_platform(actor: str, actor_user_id: int = 0) -> bool:
    """后台直发租约的操作者现在还是不是平台方超级用户

    按账号 id 重查（用户名不唯一，聊天用户的昵称可能撞上后台账号名）；名下的租户归属
    或权限被改过、账号被封禁或删除，都拒绝。旧租约没有 id 时才退回按用户名查。
    """
    user: Optional[DBUser]
    if actor_user_id:
        user = await DBUser.get_or_none(id=int(actor_user_id))
    elif actor:
        user = await DBUser.filter(username=actor).order_by("id").first()
    else:
        return False
    if user is None:
        return False
    if _user_is_banned(user):
        return False
    return _is_platform_console_user(user)


def _user_is_banned(user: DBUser) -> bool:
    """账号被封禁时不再认它的直发授权；判不出来按封禁处理"""
    try:
        return not bool(user.is_active)
    except Exception:  # noqa: BLE001
        return True


async def recheck(lease: "SshLease", row: DBWsSshCredential) -> Optional[str]:
    """网关每次连接、每个通道前复核一次

    Returns:
        `None` 表示放行；否则是拒绝理由码（取值见 `constants.py`）。
    """
    if not bool(config.SSH_GATEWAY_ENABLED):
        return C.REASON_GATEWAY_OFF

    # 延迟导入：租约模块为了拿 `Coverage` 在顶层 import 本模块，这里顶层反向导入会成环。
    from nekro_agent.services.ssh_gateway.lease import LEASE_STATE_RUNNING

    if lease.state != LEASE_STATE_RUNNING or _lease_expired(lease):
        return C.REASON_NO_LEASE
    if int(row.workspace_id) != int(lease.workspace_id):
        return C.REASON_NOT_COVERED
    if int(row.id) not in lease.credential_ids:
        return C.REASON_NOT_COVERED
    if row.status != C.STATUS_ACTIVE:
        return C.REASON_ROW_INACTIVE
    if not _row_is_decryptable(row):
        return C.REASON_UNDECRYPTABLE

    if row.owner_kind == C.OWNER_PLATFORM:
        workspace = await DBWorkspace.get_or_none(id=row.workspace_id)
        if workspace is None or not await platform_credentials_allowed(workspace):
            return C.REASON_WORKSPACE_NOT_PLATFORM
        # 聊天来源：叫醒这一轮的 Bot 停了（停用、归档、软删除，或所属租户停用），以它名义开着的
        # 服务器访问一并断，严格行与值守行都一样。用户行在下面按行上的实例判，本来就断。
        # 实例行不存在、实例标识为空时 `is_instance_active` 放行，与消息闸门同一口径
        if lease.origin == "chat" and not await DBAdapterInstance.is_instance_active(
            str(lease.adapter_key or ""),
            str(lease.instance_key or ""),
        ):
            return C.REASON_INSTANCE_INACTIVE
        if lease.origin == "chat" and int(row.id) in getattr(lease, "duty_ids", frozenset()):
            # 按值守拍下的行：不比所有者名单，改判「仍是值守、频道实例仍合格」。后台把它改回
            # 严格会发凭据变更通知；改默认模式、改实例名单靠周期复核（缓存键里带着这两项）
            if effective_duty_mode(row) != C.DUTY_MODE_DUTY:
                return C.REASON_DUTY_REVOKED
            if not await duty_instance_eligible(str(lease.adapter_key or ""), str(lease.instance_key or "")):
                return C.REASON_INSTANCE_INELIGIBLE
            return None
        if lease.origin == "chat":
            # 聊天来源必须有「谁叫醒的」才能复核名单；空列表会让下面的 all() 空真通过，
            # 等于把一次授权永久留在名单之外，因此直接拒绝。
            if not lease.triggered_by:
                return C.REASON_OWNER_CHANGED
            owner_entries = config.SSH_GATEWAY_OWNER_ACCOUNTS
            owner_adapter = str(lease.adapter_key or "")
            if not all(
                matches_super_users(owner_adapter, account, owner_entries)
                for account in lease.triggered_by
            ):
                return C.REASON_OWNER_CHANGED
        elif lease.origin == "console":
            if not await _console_actor_still_platform(
                str(lease.actor or ""),
                int(getattr(lease, "actor_user_id", 0) or 0),
            ):
                return C.REASON_OWNER_CHANGED
        else:
            return C.REASON_OWNER_CHANGED
        return None

    if row.owner_kind == C.OWNER_USER:
        if not matches_super_users(
            str(row.adapter_key or ""),
            str(row.owner_account or ""),
            config.SSH_GATEWAY_USER_ACCOUNTS,
        ):
            return C.REASON_OWNER_CHANGED
        adapter_key = str(row.adapter_key or "")
        instance_key = str(row.instance_key or "")
        # `is_instance_active` 对「实例行不存在」放行（为不用多租户的部署保持原样）；用户行
        # 跟着实例走，实例删了还留着的行一律不能用
        if instance_key and not await DBAdapterInstance.filter(
            adapter_key=adapter_key,
            instance_key=instance_key,
        ).exists():
            return C.REASON_OWNER_CHANGED
        if not await DBAdapterInstance.is_instance_active(adapter_key, instance_key):
            return C.REASON_OWNER_CHANGED
        return None

    return C.REASON_NOT_COVERED


__all__ = [
    "Coverage",
    "batch_all_trusted_by",
    "compute_chat_coverage",
    "compute_console_coverage",
    "covers_duty_row",
    "covers_platform_row",
    "covers_user_row",
    "duty_instance_eligible",
    "duty_instance_entries",
    "duty_instance_ref",
    "effective_duty_mode",
    "recheck",
    "triggered_by_accounts",
]
