"""账号合并自助申报：「后台手动为主、Bot 内自助申报为辅」的后者

## 这条链路的形状

```
用户在 Bot 里: /merge_request <旧账号> [备注]
    -> DBIdentityAliasRequest(pending)      # 本模块写，一个数据行都不碰
管理员在后台: 待审批列表 -> 批准 / 拒绝
    -> 批准 = merge_accounts(source=self_report)   # 与手动合并完全同一条路径
    -> 拒绝 = 只留痕，永远不动别名表
```

审批结果通过 `reporter_chat_key` 回申报时所在的那个会话通知申报人。

## 为什么批准时要复核「人」标识

申报单上存的是**提交那一刻**解析出的 `reporter_person_uid`。提交之后申报人可能自己
被合并进了别人（`reporter -> X`），此时兑现这张单子会把副账号并进**另一个人**名下——
单子签发时的语义已经变了。批准时重新解析一次，不一致直接拒，让申报人重新申报。

## 滥用的闸门在哪

- 同一申报人在一个域里**最多一张 pending**（先撤回再发新的）；
- 同一个账号在一个域里**最多被一张 pending 指着**（先到先得，第二个申报人拿到明确拒绝）；
  这两条的最终裁决是数据库里的**部分唯一索引**（迁移 26，`WHERE status = 'pending'`），
  不是事务内的先查后建——后者在 READ COMMITTED 下挡不住并发双写，只负责给出友好文案；
  撞索引的 `IntegrityError` 在 `submit_alias_request` 里翻成 `AliasRequestError`；
- 批准/拒绝读单时对该行加 `SELECT ... FOR UPDATE`，并发的两条审批互斥，不会互相覆盖；
- 批准=手动合并的同一条代码路径，"别名不成链""已被合并"这些校验一条不少；
- 每一条都落在表上留痕（谁报的、谁批的、什么时候、批语），**不做自动推断**——
  这与 DECISIONS「把两个不同的人合并，代价远大于多点一下」是同一条原则。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from tortoise import timezone
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_identity import (
    ALIAS_REQUEST_APPROVED,
    ALIAS_REQUEST_CANCELED,
    ALIAS_REQUEST_PENDING,
    ALIAS_REQUEST_REJECTED,
    ALIAS_REQUEST_USERID_MAX_LENGTH,
    ALIAS_SOURCE_SELF_REPORT,
    DBIdentityAlias,
    DBIdentityAliasRequest,
)
from nekro_agent.services.affection.merge import MergeResult, merge_accounts
from nekro_agent.services.affection.store import invalidate_cache
from nekro_agent.services.identity_scope import ScopeDomain, resolve_person_uid

logger = get_sub_logger("identity_admin")


class AliasRequestError(ValueError):
    """申报单操作被拒绝；message 面向操作者，路由层转成 400、命令层原样回"""


async def submit_alias_request(
    domain: ScopeDomain,
    *,
    reporter_userid: str,
    reporter_chat_key: str,
    claimed_userid: str,
    note: str = "",
) -> DBIdentityAliasRequest:
    """申报人声明「`claimed_userid` 这个账号也是我」

    申报语义：批准后把 `claimed_userid` 合并成**申报人**的副账号
    （`alias=claimed -> person=申报人的人标识`）。申报人是此刻在场的那一个，
    旧号折进在用的身份——与后台手动合并「副号并进主账号」同一个方向。

    Raises:
        AliasRequestError: 账号非法 / 已有 pending / 目标已被合并等
    """
    reporter = reporter_userid.strip()
    claimed = claimed_userid.strip()
    if not reporter or not claimed:
        raise AliasRequestError("申报需要非空的账号标识")
    # 两个账号列都是 256 宽：超长在这里给一句人话，不让它到数据库里变成截断错误
    if len(claimed) > ALIAS_REQUEST_USERID_MAX_LENGTH:
        raise AliasRequestError(f"要并入的账号标识过长（{len(claimed)} 个字符，上限 {ALIAS_REQUEST_USERID_MAX_LENGTH}）")
    if len(reporter) > ALIAS_REQUEST_USERID_MAX_LENGTH:
        raise AliasRequestError(f"你的账号标识过长（{len(reporter)} 个字符，上限 {ALIAS_REQUEST_USERID_MAX_LENGTH}）")
    if claimed == reporter:
        raise AliasRequestError("不能申报自己：那个账号本来就是你")

    # 申报人此刻解析到的「人」存下来，批准时复核（见模块文档「为什么批准时要复核」）
    reporter_person_uid = await resolve_person_uid(domain, reporter)

    try:
        async with in_transaction():
            # 下面三道先查是**友好文案**，不是闸门：READ COMMITTED 下两个并发事务可以各自查到没有、
            # 各自建出来。pending 闸门的最终裁决是 `identity_alias_request` 上的两条部分唯一索引
            # （迁移 26，见 `models/db_identity.py`），穿过先查的那一路在 create 处撞索引。
            existing_pending = await DBIdentityAliasRequest.filter(
                domain_id=domain.domain_id,
                reporter_userid=reporter,
                status=ALIAS_REQUEST_PENDING,
            ).first()
            if existing_pending is not None:
                raise AliasRequestError(_reporter_has_pending_message(existing_pending))

            claimed_pending = await DBIdentityAliasRequest.filter(
                domain_id=domain.domain_id,
                alias_platform_userid=claimed,
                status=ALIAS_REQUEST_PENDING,
            ).first()
            if claimed_pending is not None:
                raise AliasRequestError(_claimed_has_pending_message(claimed))

            claimed_alias = (
                await DBIdentityAlias.filter(domain_id=domain.domain_id, platform_userid=claimed)
                .order_by("id")
                .first()
            )
            if claimed_alias is not None:
                if claimed_alias.person_uid == reporter_person_uid:
                    raise AliasRequestError(f"账号 {claimed} 已经并入你的名下了，不需要再申报")
                raise AliasRequestError(
                    f"账号 {claimed} 已经并入了其他人名下，如属误并请联系管理员先撤销再申报",
                )

            request = await DBIdentityAliasRequest.create(
                domain_id=domain.domain_id,
                alias_platform_userid=claimed,
                reporter_userid=reporter,
                reporter_person_uid=reporter_person_uid,
                reporter_chat_key=reporter_chat_key,
                note=note,
            )
    except IntegrityError as error:
        # 输掉并发的一方。Postgres 里出错的事务已经被上面的 `in_transaction` 回滚，
        # 这里可以安全地回读是谁占了位子，给出与先查路径同样的文案。
        raise await _explain_gate_conflict(domain, reporter, claimed) from error

    logger.info(
        f"账号合并申报: domain={domain.domain_id}, {claimed} -> {reporter_person_uid} "
        f"(by {reporter}), request_id={request.id}",
    )
    return request


def _reporter_has_pending_message(existing: DBIdentityAliasRequest) -> str:
    return (
        f"你已经有一张待审批的申报（并入 {existing.alias_platform_userid}），"
        "请等审批结果或先用 `merge_request cancel` 撤回"
    )


def _claimed_has_pending_message(claimed: str) -> str:
    return f"账号 {claimed} 已经有一张待审批的申报，不能重复申报"


async def _explain_gate_conflict(domain: ScopeDomain, reporter: str, claimed: str) -> AliasRequestError:
    """部分唯一索引拒了这次插入：回读是哪一道闸门被占，翻成与先查路径一致的文案

    两条索引里到底撞了哪一条，驱动给的错误文本不可靠（跨版本措辞不同），所以按事实回读：
    先看申报人自己是否已有 pending，再看这个账号是否已被别的 pending 指着。
    两者都查不到（占位的单子在这一刻刚被审结/撤回）说明这次是真的撞车但已经松开，
    让操作者重试即可——**不要**把它冒泡成 IntegrityError。
    """
    existing = await DBIdentityAliasRequest.filter(
        domain_id=domain.domain_id,
        reporter_userid=reporter,
        status=ALIAS_REQUEST_PENDING,
    ).first()
    if existing is not None:
        return AliasRequestError(_reporter_has_pending_message(existing))
    if await DBIdentityAliasRequest.filter(
        domain_id=domain.domain_id,
        alias_platform_userid=claimed,
        status=ALIAS_REQUEST_PENDING,
    ).exists():
        return AliasRequestError(_claimed_has_pending_message(claimed))
    return AliasRequestError("申报提交时与另一条申报撞车了，请稍后重试")


async def list_requests(
    domain: ScopeDomain,
    *,
    status: Optional[str] = None,
) -> List[DBIdentityAliasRequest]:
    """列出一个域里的申报单；`status=None` 时全部状态都回（后台审计要看得见历史）

    全量视图按「pending 优先、其余新的在前」排——管理员打开历史页时
    仍待处理的单子永远在最上面，不混在已审完的旧账里。
    """
    query = DBIdentityAliasRequest.filter(domain_id=domain.domain_id)
    if status:
        query = query.filter(status=status)
    items = await query.order_by("-id")
    return sorted(items, key=lambda item: (item.status != ALIAS_REQUEST_PENDING, -item.id))


async def list_requests_by_reporter(domain: ScopeDomain, reporter_userid: str) -> List[DBIdentityAliasRequest]:
    """申报人查自己的单子（`merge_request` 无参时的输出）"""
    return (
        await DBIdentityAliasRequest.filter(domain_id=domain.domain_id, reporter_userid=reporter_userid)
        .order_by("-id")
        .limit(10)
    )


async def get_request_in_domain(
    domain: ScopeDomain,
    request_id: int,
    *,
    for_update: bool = False,
) -> DBIdentityAliasRequest:
    """按 `(域, 单号)` 取申报单

    双条件缺一不可：只按单号取等于让任何一个能进某域后台的人审批别的域的单子。
    查不到时抛 `AliasRequestError`——调用方（路由层）决定翻译成 404 还是别的。

    `for_update=True` 给批准/拒绝用：**必须在事务里调**，对这一行加行锁（`SELECT ... FOR UPDATE`），
    并发的另一条审批要等这一条提交之后才读得到单子，读到的已经是审结后的状态。
    不加锁时两条审批各自读到 pending、各自写成功，后写的把先写的覆盖掉，
    申报人收到的通知与库里的状态对不上。SQLite 没有行锁，Tortoise 在该方言下忽略它。
    """
    query = DBIdentityAliasRequest.filter(domain_id=domain.domain_id, id=request_id)
    if for_update:
        query = query.select_for_update()
    request = await query.first()
    if request is None:
        raise AliasRequestError(f"申报单 #{request_id} 不存在")
    return request


async def cancel_alias_request(domain: ScopeDomain, reporter_userid: str) -> DBIdentityAliasRequest:
    """申报人撤回自己的 pending 单

    与批准/拒绝**同一把行锁**（`select_for_update`），写入再带 `status=pending` 条件：
    批准正持锁做合并时，撤回要等它提交，读到的是已审结的单子、直接报「没有待审批的申报」。
    原先无锁读取 + 整行 save，撤回与批准并发会把 `approved` 覆盖成 `canceled`——别名已经建了、
    补偿已经记了，单子却显示申报人撤回了（round-27 复核 P2-4）。SQLite 没有行锁，
    条件 UPDATE 本身是原子的，仍能挡住这种覆盖。
    """
    async with in_transaction():
        request = (
            await DBIdentityAliasRequest.filter(
                domain_id=domain.domain_id,
                reporter_userid=reporter_userid,
                status=ALIAS_REQUEST_PENDING,
            )
            .select_for_update()
            .first()
        )
        if request is None:
            raise AliasRequestError("你没有待审批的申报可以撤回")
        # `QuerySet.update` 不触发 `auto_now`，更新时间显式带上
        updated = await DBIdentityAliasRequest.filter(id=request.id, status=ALIAS_REQUEST_PENDING).update(
            status=ALIAS_REQUEST_CANCELED,
            update_time=timezone.now(),
        )
        if not updated:
            raise AliasRequestError("你没有待审批的申报可以撤回")
    request.status = ALIAS_REQUEST_CANCELED
    logger.info(f"账号合并申报已撤回: request_id={request.id}, reporter={reporter_userid}")
    return request


async def approve_alias_request(
    domain: ScopeDomain,
    request_id: int,
    *,
    operator: str,
    note: str = "",
) -> Tuple[DBIdentityAliasRequest, MergeResult]:
    """批准申报：兑现成一条 `source=self_report` 的别名

    合并走 `merge_accounts`，它自己的校验（不成链、没被合并到别人）一条不少；
    这里加的两条是它不知道的：单子必须还是 pending，以及申报人的「人」标识
    不能中途漂走（漂了说明语义已变，拒掉让申报人重新申报）。

    Raises:
        AliasRequestError: 单子不在本域 / 已审过 / 归属漂移 / 合并校验不过
    """
    async with in_transaction():
        # 行锁：与并发的另一条审批（批准/拒绝）互斥，见 `get_request_in_domain`
        request = await get_request_in_domain(domain, request_id, for_update=True)
        if request.status != ALIAS_REQUEST_PENDING:
            raise AliasRequestError(f"申报单 #{request_id} 已经审过了（{request.status}）")

        # 复核「人」标识：提交后发生过别的合并时，兑现方向就不是签发时的方向了
        current_person = await resolve_person_uid(domain, request.reporter_userid)
        if current_person != request.reporter_person_uid:
            raise AliasRequestError(
                f"申报人 {request.reporter_userid} 的归属已变化"
                f"（{request.reporter_person_uid} -> {current_person}），请其重新申报",
            )

        try:
            result = await merge_accounts(
                domain,
                person_uid=request.reporter_person_uid,
                alias_platform_userid=request.alias_platform_userid,
                operator=operator,
                source=ALIAS_SOURCE_SELF_REPORT,
                note=note or f"自助申报 #{request.id}" + (f"：{request.note}" if request.note else ""),
            )
        except ValueError as error:
            raise AliasRequestError(str(error)) from error

        request.status = ALIAS_REQUEST_APPROVED
        request.reviewed_by = operator[:128]
        request.review_note = note
        request.reviewed_at = timezone.now()
        await request.save(update_fields=["status", "reviewed_by", "review_note", "reviewed_at", "update_time"])

    # 补偿在上面的事务里经 `apply_delta` 写入，它自带的缓存失效发生在**提交之前**：并发读者在失效与提交之间
    # 读到的还是旧分数、并把它重新缓存起来，补进去的好感在 TTL 内读不到（round-27 第二轮复核 P2-7，
    # 与入域并入的 P2-6 同一类问题）。提交之后再失效一次才是权威的那一次；事务内的那次无害、被它覆盖。
    # 全量失效而不是按键：合并让副号读到的「人」变了，缓存里副号名下的旧快照同样要丢。
    invalidate_cache()

    logger.info(
        f"账号合并申报已批准: request_id={request.id}, {result.alias_platform_userid} -> {result.person_uid}, "
        f"operator={operator}, 补偿 {result.total_delta} 点",
    )
    await _notify_reporter(
        request,
        f"你的账号合并申报已通过：{request.alias_platform_userid} 已并入你当前账号名下。",
    )
    return request, result


async def reject_alias_request(
    domain: ScopeDomain,
    request_id: int,
    *,
    operator: str,
    note: str = "",
) -> DBIdentityAliasRequest:
    """拒绝申报：只留痕，永远不动别名表"""
    async with in_transaction():
        request = await get_request_in_domain(domain, request_id, for_update=True)
        if request.status != ALIAS_REQUEST_PENDING:
            raise AliasRequestError(f"申报单 #{request_id} 已经审过了（{request.status}）")
        request.status = ALIAS_REQUEST_REJECTED
        request.reviewed_by = operator[:128]
        request.review_note = note
        request.reviewed_at = timezone.now()
        await request.save(update_fields=["status", "reviewed_by", "review_note", "reviewed_at", "update_time"])

    logger.info(f"账号合并申报已拒绝: request_id={request.id}, operator={operator}, note={note or '-'}")
    await _notify_reporter(
        request,
        f"你的账号合并申报（并入 {request.alias_platform_userid}）未通过"
        + (f"：{note}" if note else "。"),
    )
    return request


async def _notify_reporter(request: DBIdentityAliasRequest, message: str) -> None:
    """把审批结果回申报时所在的会话

    通知失败**不**影响审批结果：适配器下线/会话已删都不是把审批回滚的理由，
    单子上已经留痕，申报人下次 `merge_request` 也能查到结果。
    """
    try:
        from nekro_agent.services.chat.universal_chat_service import universal_chat_service

        await universal_chat_service.send_operation_message(request.reporter_chat_key, message)
    except Exception as error:  # noqa: BLE001 - 通知是尽力而为，审批结果不靠它成立
        logger.warning(f"申报审批结果通知发送失败: request_id={request.id}, chat_key={request.reporter_chat_key}: {error}")


__all__ = [
    "AliasRequestError",
    "approve_alias_request",
    "cancel_alias_request",
    "get_request_in_domain",
    "list_requests",
    "list_requests_by_reporter",
    "submit_alias_request",
]
