import json
import time
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from tortoise import timezone
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.models.db_adapter_instance_session import DBAdapterInstanceSession
from nekro_agent.schemas.errors import ConflictError
from nekro_agent.services.system_broadcast import publish_adapter_instance_status
from nekro_agent.tools.text_sanitize import strip_nul

logger = get_sub_logger("adapter_instance")

# 管理员对实例的处置：停用、归档、软删。只有管理员动作（经 `set_status`）能写入或离开它们；
# 连接、续期、探活这些自动来源经 `set_runtime_status`，碰到它们就不写。
# 与 console 层的 `STATUS_DISABLED` / `STATUS_ARCHIVED` 同值（服务层不依赖 console 模块，用例钉住两边一致）。
ADMIN_INTENT_STATUSES: frozenset[str] = frozenset({"disabled", "archived", TOMBSTONE_STATUS})


class AdapterInstanceService:
    """适配器实例服务，管理实例生命周期、状态转换、会话与审计事件。

    设计原则:
    - 所有写入操作均为异步，使用 Tortoise ORM 原生 API
    - 状态变更自动记录审计事件 (DBAdapterInstanceEvent)
    - 支持幂等创建与软删除，审计行永不硬删除
    - 不涉及任何具体适配器协议代码，保持通用性
    """

    # ------------------------------------------------------------------
    # Account binding
    # ------------------------------------------------------------------

    async def release_account_if_taken(self, instance: DBAdapterInstance) -> str:
        """把一行**重新变回「活的」之前**，确认它记着的账号还归它；不归它就就地解绑

        ## 为什么复活也是一条账号写入路径

        账号唯一性（`DBAdapterInstance.find_account_binding_conflict`）的判据是「排除软删墓碑后
        只能有一行」——墓碑刻意不算占用，否则管理员会被一个总后台列表里根本看不到的行挡住改绑。
        代价是墓碑活着的那段时间里，**别的实例可以合法地把这个账号绑走**（创建、改绑、认领、
        内置 NapCat 回写四条路都会放行）。

        于是「复活」变成了一次隐蔽的账号写入：把墓碑重新变成活行，它记着的那个陈旧账号就
        重新进入唯一性判据的取值范围，库里立刻出现两行活着的实例绑同一个账号 ——
        正是唯一性判据要防止的状态。更难看的是 `get_by_bound_account` 取的是**最早那行**，而墓碑的 id 通常
        更小：入站消息会从那个一直在收发的实例，静默改判给刚复活的空壳。

        ## 为什么是解绑而不是拒绝复活

        账号是墓碑上的**陈旧数据**，实例的身份是 `instance_key`；复活不该把一个早已被重新分配
        出去的绑定一起复活。而拒绝复活会把两条正常路径一起打掉：总后台的「启用」，以及
        `create_instance` 幂等复活（`adapters/wechat_ilink_multi` 的登记接口靠它，不然
        `(adapter_key, instance_key)` 会被墓碑永久占住）。

        解绑与启动期存量修复 `unbind_duplicate_account_bindings` 是同一条处置：保留正在收发的
        那一行，另一行只解绑、数据分毫不动，管理员随后可以改绑别的账号，也可以走归属迁移。

        Returns:
            被解绑的说明文本；账号仍归本行（或本行本来就没绑账号）时返回空串。
        """
        account = instance.provider_account_id.strip()
        if not account:
            return ""

        holder = await DBAdapterInstance.find_account_binding_conflict(
            instance.adapter_key,
            account,
            exclude_id=instance.id,
        )
        if holder is None:
            return ""

        reason = f"账号 {account} 已由实例 '{holder.instance_key}' 持有，本实例恢复时已解绑该账号"
        instance.provider_account_id = ""
        await instance.save(update_fields=["provider_account_id", "update_time"])
        await self.record_event(
            instance=instance,
            event_type="account_released",
            status_from=instance.status,
            status_to=instance.status,
            message=reason,
        )
        # 账号绑定关系变了就清路由缓存，与 `instance_manager.update_instance` /
        # `container_manager.bind_login_account` 同一条规矩。这一行本来就路由不到
        # （墓碑被 `get_by_bound_account` 排除），清一次只是不让这条不变式依赖推理。
        from nekro_agent.adapters.onebot_v11.core.instance import clear_instance_cache

        clear_instance_cache()
        logger.warning(f"[AdapterInstanceService] 实例 {instance.instance_key} 恢复时解绑陈旧账号: {reason}")
        return reason

    # ------------------------------------------------------------------
    # Instance CRUD
    # ------------------------------------------------------------------

    async def create_instance(
        self,
        adapter_key: str,
        instance_key: str,
        display_name: str,
        provider: str,
        metadata: dict[str, Any] | None = None,
    ) -> DBAdapterInstance:
        """幂等创建或更新适配器实例。

        若 (adapter_key, instance_key) 已存在，则更新 display_name、provider
        与 metadata_json，其余字段保持不变；否则新建实例。

        Args:
            adapter_key: 适配器唯一标识
            instance_key: 实例唯一标识
            display_name: 实例显示名称
            provider: 服务提供方
            metadata: 实例元数据字典，序列化为 JSON 字符串存储

        Returns:
            DBAdapterInstance: 创建或更新后的实例对象

        Raises:
            ConflictError: 新建时该 `instance_key` 已被**另一个适配器**的实例占用
        """
        metadata_json: str = json.dumps(metadata, ensure_ascii=False) if metadata else ""

        existing: DBAdapterInstance | None = await DBAdapterInstance.get_or_none(
            adapter_key=adapter_key,
            instance_key=instance_key,
        )

        if existing is not None:
            existing.display_name = display_name
            existing.provider = provider
            existing.metadata_json = metadata_json
            # 只写本分支改的列：读出之后（例如下面的账号核对期间）别处改的租户归属等列不能被写回
            changed = ["display_name", "provider", "metadata_json", "update_time"]
            revived = existing.status == "deleted"
            released = ""
            if revived:
                # 复活软删实例：否则该 (adapter_key, instance_key) 将因唯一约束永久占用为 deleted 僵尸。
                # 复活之前先确认它记着的账号还归它——墓碑期间别人可以合法绑走，
                # 直接复活就会写出两行绑同一账号的活实例（见 `release_account_if_taken`）
                released = await self.release_account_if_taken(existing)
                existing.status = "pending"
                existing.enabled = True
                existing.last_error = released
                changed += ["status", "enabled", "last_error"]
            await existing.save(update_fields=changed)
            if revived:
                await self.record_event(
                    instance=existing,
                    event_type="status_change",
                    status_from="deleted",
                    status_to="pending",
                    message="软删实例被重新创建并复活",
                )
            logger.info(
                f"[AdapterInstanceService] {'复活' if revived else '更新'}实例: adapter={adapter_key}, "
                f"instance={instance_key}, id={existing.id}",
            )
            return existing

        # 数据库的唯一约束只到 (adapter_key, instance_key)，但人设、插件开关、好感与记忆、协议端
        # 容器名都只按 `instance_key` 存取，同名的两个实例会在这些地方共用一份数据；两行再分属
        # 两个租户，就是跨租户的读写。总后台建实例时查的是全表重名，这里与它保持同一条判据。
        if await DBAdapterInstance.filter(instance_key=instance_key).exclude(adapter_key=adapter_key).exists():
            raise ConflictError(resource=f"实例 '{instance_key}'")

        instance: DBAdapterInstance = await DBAdapterInstance.create(
            adapter_key=adapter_key,
            instance_key=instance_key,
            display_name=display_name,
            provider=provider,
            metadata_json=metadata_json,
        )
        logger.info(
            f"[AdapterInstanceService] 创建实例: adapter={adapter_key}, "
            f"instance={instance_key}, id={instance.id}",
        )
        return instance

    async def list_instances(self, adapter_key: str | None = None) -> list[DBAdapterInstance]:
        """获取实例列表。

        Args:
            adapter_key: 可选，按适配器键筛选

        Returns:
            list[DBAdapterInstance]: 实例列表
        """
        query = DBAdapterInstance.all()
        if adapter_key is not None:
            query = query.filter(adapter_key=adapter_key)
        return await query

    async def get_instance(self, adapter_key: str, instance_key: str) -> DBAdapterInstance | None:
        """获取单个适配器实例。

        Args:
            adapter_key: 适配器唯一标识
            instance_key: 实例唯一标识

        Returns:
            DBAdapterInstance | None: 实例对象，若不存在则返回 None
        """
        return await DBAdapterInstance.get_or_none(
            adapter_key=adapter_key,
            instance_key=instance_key,
        )

    # ------------------------------------------------------------------
    # Status & Session State
    # ------------------------------------------------------------------

    async def set_status(
        self,
        instance: DBAdapterInstance,
        new_status: str,
        message: str = "",
        payload: dict[str, Any] | None = None,
        *,
        extra_fields: Iterable[str] = (),
    ) -> DBAdapterInstance:
        """设置实例主生命周期状态并记录审计事件。

        ## 只写状态那几列

        落库只写 `status`、`last_error`（加 `update_time`），外加调用方在 `extra_fields` 里**显式声明**它改过的列。
        传进来的对象常常是一段时间以前读出来的：微信 iLink 的连接拿着建立时读出的实例跑几个小时，掉线监测拿着本轮
        开头读出的对象去探活。整行写回会把这期间别处改的列冲回旧值——平台方刚改划给别的租户的 `tenant_id`、
        刚停用的 `enabled`、刚改的账号与名称。调用方若在对象上改了别的列、想借这一次写入落库（启停改 `enabled`、
        认领改名称），就把那几列放进 `extra_fields`，不要指望整行保存顺带写进去。

        ## 离开墓碑态时要先核对账号绑定

        「把一行从 `deleted` 变成别的状态」就是复活，而复活是一条隐蔽的**账号写入路径**：
        墓碑不算账号占用，因此它躺着的那段时间里账号可能已经被别人合法绑走，直接复活会写出
        两行绑同一账号的活实例（见 `release_account_if_taken`）。判据挂在这里而不是各个调用点，
        是因为 `set_status` 是唯一的状态写入口——总后台的启用（`set_instance_enabled`）、
        适配器自己的状态流转都从这里过，钉在这里就不会有"某条路径忘了校验"的那一半。

        Args:
            instance: 目标实例对象
            new_status: 新状态值
            message: 状态变更说明
            payload: 附加载荷字典，序列化为 JSON 字符串存储
            extra_fields: 调用方改过、要随本次一起落库的其他列

        Returns:
            DBAdapterInstance: 更新后的实例对象
        """
        old_status: str = instance.status
        released = ""
        if old_status == TOMBSTONE_STATUS and new_status != TOMBSTONE_STATUS:
            released = await self.release_account_if_taken(instance)
        instance.status = new_status
        instance.last_error = message if new_status in ("error", "deleted") else ""
        if released:
            # 复活后落到 offline/unbound 时 `last_error` 本会被清空，而"我启用的实例
            # 怎么没了账号"必须在总后台看得见——这条说明是唯一的现场线索
            instance.last_error = released
        # 去重：PostgreSQL 对同一列赋值两次直接报错（调用方在 extra_fields 里多写了 status 之类）
        await instance.save(update_fields=list(dict.fromkeys(["status", "last_error", "update_time", *extra_fields])))
        await self._announce_status_change(instance, old_status, new_status, message, payload)
        return instance

    async def set_runtime_status(
        self,
        instance: DBAdapterInstance,
        new_status: str,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> DBAdapterInstance:
        """自动来源（连接、续期、绑定流程、探活、掉线监测、账号回写）写实例的运行态

        与 `set_status` 的区别：自动来源永远不覆盖管理员的处置（`ADMIN_INTENT_STATUSES`：停用、归档、软删）。
        写入是一条条件更新——实例此刻仍启用、且不在这三种状态——判据落在写入那一刻，而不是调用方手里那份
        可能早已过时的对象：连接拿着建立时读出的实例跑几个小时，掉线监测拿着探活之前读出的对象，按旧对象的状态
        判断再写，会把刚停用的实例写回在线、把刚软删的墓碑写回活态（墓碑的账号核对也随之被绕开）。

        条件不满足时什么都不写：不记审计事件、不广播，只记 debug 日志。离开墓碑态只走管理员恢复。

        内存对象的 `status` / `last_error` 无论写没写都更新成调用方要的值：微信 iLink 的连接靠它记「这条连接
        自己的运行态」（续期循环据此判断凭据是否已过期、要不要停止重试），库里写没写由条件更新决定，两者互不干扰。

        `new_status` 若本身是管理员意图状态（调用方由旧状态推导出来的，例如续期「恢复原状态」时原状态正是停用），
        同样不写、不改内存，只记一条告警：抛错会把这种边角变成接口 500 或续期循环里的一次假失败。
        """
        if new_status in ADMIN_INTENT_STATUSES:
            logger.warning(
                f"[AdapterInstanceService] 自动来源想把实例写成管理员意图状态，已忽略: id={instance.id}, 想写 {new_status}",
            )
            return instance
        old_status = instance.status
        # `filter().update()` 不走 ORM 的保存信号；错误信息来自上游/协议端，可能是带 NUL 的原文
        last_error = strip_nul(message) if new_status == "error" else ""
        written = (
            await DBAdapterInstance.filter(id=instance.id, enabled=True)
            .exclude(status__in=list(ADMIN_INTENT_STATUSES))
            .update(status=new_status, last_error=last_error, update_time=timezone.now())
        )
        instance.status = new_status
        instance.last_error = last_error
        if not written:
            logger.debug(
                f"[AdapterInstanceService] 实例已停用、归档或软删，自动来源不改写状态: id={instance.id}, 想写 {new_status}",
            )
            return instance
        await self._announce_status_change(instance, old_status, new_status, message, payload)
        return instance

    async def _announce_status_change(
        self,
        instance: DBAdapterInstance,
        old_status: str,
        new_status: str,
        message: str,
        payload: dict[str, Any] | None,
    ) -> None:
        """状态已经落库之后：记审计事件、广播给 SSE 订阅者"""
        await self.record_event(
            instance=instance,
            event_type="status_change",
            status_from=old_status,
            status_to=new_status,
            message=message,
            payload_json=json.dumps(payload, ensure_ascii=False) if payload else "",
        )

        await publish_adapter_instance_status(
            adapter_key=instance.adapter_key,
            instance_key=instance.instance_key,
            status=new_status,
            previous_status=old_status,
            message=message,
            last_error=instance.last_error,
            updated_at=int(time.time() * 1000),
        )

        logger.info(
            f"[AdapterInstanceService] 实例状态变更: id={instance.id}, "
            f"{old_status} -> {new_status}",
        )

    async def set_session_state(
        self,
        instance: DBAdapterInstance,
        session_state: str,
    ) -> DBAdapterInstanceSession:
        """设置实例会话子状态（如 bind_status）。

        若实例尚无关联会话，则自动创建一条空会话记录。

        本方法只写 `session_state`，**不碰 `sync_state_json`**：那个字段是同步游标 `SyncState` 的
        序列化载体，读取方按 `extra="forbid"` 严格解析。以前这里收一个 `payload` 并整体覆盖它，
        扫码绑定时传进来的 `{bind_session_id, qr_url}` 于是在绑定成功后让实例每次启动都解析失败、
        监控起不来、收不到消息。绑定过程的附加信息记在 `record_event` 的 `payload_json` 里。

        Args:
            instance: 目标实例对象
            session_state: 会话子状态值

        Returns:
            DBAdapterInstanceSession: 更新后的会话对象
        """
        await self._ensure_session_row(instance.id)
        async with in_transaction() as conn:
            session = await DBAdapterInstanceSession.select_for_update().using_db(conn).get(instance_id=instance.id)
            session.session_state = session_state
            await session.save(using_db=conn, update_fields=["session_state", "update_time"])

        # 不在此处广播 SSE：会话子状态(bind_status)是绑定内部细节，且其 status==previous_status
        # 会产生 from==to 的无意义“状态转换”事件。主生命周期状态变更已由 set_status 正确广播；
        # 绑定子状态通过 bind/status API 查询。

        logger.info(
            f"[AdapterInstanceService] 会话状态更新: instance_id={instance.id}, "
            f"state={session_state}",
        )
        return session

    # ------------------------------------------------------------------
    # Session Management
    # ------------------------------------------------------------------

    @staticmethod
    async def _ensure_session_row(instance_id: int) -> None:
        """确保该实例的会话行存在，供随后加锁更新。

        并发保证靠 `instance_id` 的唯一约束：两个调用同时判断为「不存在」时，落败的那次插入撞约束，
        这里吞掉 `IntegrityError` 即可，最终只有一行。

        故意放在事务之外，调用方也不能在事务里调它：PostgreSQL 里事务内一旦出现 `IntegrityError`，
        整个事务进入 aborted 状态，后面的语句全部失败；在事务外，失败的只是这一次插入。
        """
        if await DBAdapterInstanceSession.exists(instance_id=instance_id):
            return
        try:
            await DBAdapterInstanceSession.create(instance_id=instance_id)
            logger.info(f"[AdapterInstanceService] 会话创建: instance_id={instance_id}")
        except IntegrityError:
            logger.debug(f"[AdapterInstanceService] 会话已由并发请求创建: instance_id={instance_id}")

    async def upsert_session(
        self,
        instance: DBAdapterInstance,
        credentials_json: str,
        sync_state_json: str,
        expires_at: datetime | None,
        last_cursor: str = "",
    ) -> DBAdapterInstanceSession:
        """创建或更新实例会话。

        只写凭据、同步状态、过期时间、游标这几列，不碰 `session_state`（绑定子状态由 `set_session_state` 管）。

        Args:
            instance: 目标实例对象
            credentials_json: 凭据 JSON 字符串
            sync_state_json: 同步状态 JSON 字符串
            expires_at: 会话过期时间
            last_cursor: 最近同步游标

        Returns:
            DBAdapterInstanceSession: 创建或更新后的会话对象
        """
        await self._ensure_session_row(instance.id)
        async with in_transaction() as conn:
            session = await DBAdapterInstanceSession.select_for_update().using_db(conn).get(instance_id=instance.id)
            session.credentials_json = credentials_json
            session.sync_state_json = sync_state_json
            session.expires_at = expires_at
            session.last_cursor = last_cursor
            await session.save(
                using_db=conn,
                update_fields=["credentials_json", "sync_state_json", "expires_at", "last_cursor", "update_time"],
            )

        logger.info(
            f"[AdapterInstanceService] 会话更新: instance_id={instance.id}, "
            f"session_id={session.id}",
        )
        return session

    # ------------------------------------------------------------------
    # Renew Scheduling
    # ------------------------------------------------------------------

    async def schedule_renew(
        self,
        instance: DBAdapterInstance,
        expires_at: datetime | None,
        renew_before_minutes: int,
    ) -> DBAdapterInstance:
        """设置实例续期计划。

        计算 next_renew_at = expires_at - renew_before_minutes，并持久化。
        对于不会过期的会话 (expires_at 为 None)，直接返回、不安排续期。

        Args:
            instance: 目标实例对象
            expires_at: 凭据过期时间；None 表示会话不过期，跳过续期安排
            renew_before_minutes: 提前续期的分钟数

        Returns:
            DBAdapterInstance: 更新后的实例对象
        """
        if expires_at is None:
            logger.debug(
                f"[AdapterInstanceService] 会话不过期，跳过续期安排: instance_id={instance.id}",
            )
            return instance

        next_renew_at: datetime = expires_at - timedelta(minutes=renew_before_minutes)
        instance.next_renew_at = next_renew_at
        instance.renew_before_minutes = renew_before_minutes
        # 只写续期计划：调用方（微信 iLink 的连接）拿着的是连接建立时读出的对象，理由同 `set_status`
        await instance.save(update_fields=["next_renew_at", "renew_before_minutes", "update_time"])

        logger.info(
            f"[AdapterInstanceService] 续期计划: instance_id={instance.id}, "
            f"next_renew_at={next_renew_at.isoformat()}",
        )
        return instance

    # ------------------------------------------------------------------
    # Event Recording
    # ------------------------------------------------------------------

    async def record_event(
        self,
        instance: DBAdapterInstance,
        event_type: str,
        status_from: str,
        status_to: str,
        message: str,
        payload_json: str = "",
    ) -> DBAdapterInstanceEvent:
        """追加审计事件行。

        Args:
            instance: 目标实例对象
            event_type: 事件类型标识
            status_from: 变更前状态
            status_to: 变更后状态
            message: 事件说明
            payload_json: 事件载荷 JSON 字符串

        Returns:
            DBAdapterInstanceEvent: 创建的事件对象
        """
        event: DBAdapterInstanceEvent = await DBAdapterInstanceEvent.create(
            instance_id=instance.id,
            event_type=event_type,
            status_from=status_from,
            status_to=status_to,
            message=message,
            payload_json=payload_json,
        )
        return event

    # ------------------------------------------------------------------
    # Soft Delete
    # ------------------------------------------------------------------

    async def soft_delete_instance(self, instance: DBAdapterInstance) -> DBAdapterInstance:
        """软删除实例。

        统一经 set_status 变更为 "deleted"：记录审计事件、复用 last_error 逻辑，
        并广播 AdapterInstanceStatusEvent，使 SSE 订阅者与数据库状态保持同步。
        额外禁用实例 (enabled=False)。不会删除关联的审计行与会话记录。

        Args:
            instance: 目标实例对象

        Returns:
            DBAdapterInstance: 更新后的实例对象
        """
        # 先置 enabled=False，再交由 set_status 落库 + 广播：`enabled` 经 `extra_fields` 随状态一次写入，
        # 避免直改状态导致 SSE 消费者漏掉删除事件。
        instance.enabled = False
        instance = await self.set_status(instance, "deleted", message="实例已软删除", extra_fields=["enabled"])
        logger.info(f"[AdapterInstanceService] 实例软删除: id={instance.id}")
        return instance


# 全局适配器实例服务
adapter_instance_service = AdapterInstanceService()
