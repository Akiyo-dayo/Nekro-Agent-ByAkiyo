"""内置命令 - 身份类: merge_request

「换号不丢数据」的用户侧入口：用户在 Bot 里申报「那个账号也是我」，
留一张 pending 单等管理员在后台批准——批准才建别名，申报本身不碰任何数据行。
"""

from typing import Annotated

from nekro_agent.schemas.i18n import i18n_text, resolve_i18n, t
from nekro_agent.services.command.base import BaseCommand, CommandMetadata, CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import Arg, CommandExecutionContext, CommandResponse

_STATUS_LABELS = {
    "pending": i18n_text(zh_CN="待审批", en_US="pending"),
    "approved": i18n_text(zh_CN="已批准", en_US="approved"),
    "rejected": i18n_text(zh_CN="已拒绝", en_US="rejected"),
    "canceled": i18n_text(zh_CN="已撤回", en_US="canceled"),
}


class MergeRequestCommand(BaseCommand):
    """自助申报账号合并：/merge_request <账号> [备注]"""

    @property
    def metadata(self) -> CommandMetadata:
        return CommandMetadata(
            name="merge_request",
            aliases=["merge_req", "claim_account"],
            description="申报账号合并（我的另一个号），等管理员批准",
            i18n_description=i18n_text(
                zh_CN="申报账号合并（我的另一个号），等管理员批准",
                en_US="Report another account as yours, pending admin approval",
            ),
            usage="merge_request [<账号> [备注] | cancel]",
            i18n_usage=i18n_text(
                zh_CN="merge_request [<账号> [备注] | cancel]",
                en_US="merge_request [<account> [note] | cancel]",
            ),
            permission=CommandPermission.USER,
            category="用户",
            i18n_category=i18n_text(zh_CN="用户", en_US="User"),
            params_schema=self._auto_params_schema(),
        )

    async def execute(
        self,
        context: CommandExecutionContext,
        args_str: Annotated[str, Arg("账号与备注，或 cancel", positional=True, greedy=True)] = "",
    ) -> CommandResponse:
        from nekro_agent.services.identity_requests import (
            AliasRequestError,
            cancel_alias_request,
            list_requests_by_reporter,
            submit_alias_request,
        )
        from nekro_agent.services.identity_scope import resolve_identity_scope_by_chat_key

        scope = await resolve_identity_scope_by_chat_key(context.chat_key, context.user_id)
        if scope is None:
            return CmdCtl.failed(
                t(
                    zh_CN="找不到当前会话，无法申报；请先正常发一条消息再试",
                    en_US="Cannot locate this chat; send a normal message first, then retry",
                )
            )
        domain = scope.domain

        parts = args_str.strip().split(maxsplit=1) if args_str.strip() else []

        if parts and parts[0] == "cancel":
            try:
                request = await cancel_alias_request(domain, context.user_id)
            except AliasRequestError as error:
                return CmdCtl.failed(str(error))
            return CmdCtl.success(
                t(
                    zh_CN=f"已撤回并入 {request.alias_platform_userid} 的申报",
                    en_US=f"Withdrew the request to merge in {request.alias_platform_userid}",
                )
            )

        if not parts:
            return self._list_my_requests(await list_requests_by_reporter(domain, context.user_id))

        claimed, note = parts[0], (parts[1] if len(parts) > 1 else "")
        try:
            request = await submit_alias_request(
                domain,
                reporter_userid=context.user_id,
                reporter_chat_key=context.chat_key,
                claimed_userid=claimed,
                note=note.strip(),
            )
        except AliasRequestError as error:
            return CmdCtl.failed(str(error))
        return CmdCtl.success(
            t(
                zh_CN=(
                    f"已提交申报 #{request.id}：把 {request.alias_platform_userid} 并入你当前账号，"
                    "等管理员批准后生效。用 `merge_request` 查进度，`merge_request cancel` 撤回。"
                ),
                en_US=(
                    f"Request #{request.id} submitted: merge {request.alias_platform_userid} into "
                    "your current account. It takes effect after admin approval. "
                    "Use `merge_request` to check progress, `merge_request cancel` to withdraw."
                ),
            ),
            data={"request_id": request.id},
        )

    def _list_my_requests(self, requests: list) -> CommandResponse:
        if not requests:
            return CmdCtl.success(
                t(
                    zh_CN="你还没有提交过账号合并申报。用法：merge_request <账号> [备注]",
                    en_US="No merge requests yet. Usage: merge_request <account> [note]",
                )
            )
        title = t(zh_CN="我的账号合并申报：", en_US="My merge requests:")
        lines = [title]
        for item in requests:
            status = resolve_i18n(_STATUS_LABELS.get(item.status, item.status))
            line = f"  #{item.id} [{status}] {item.alias_platform_userid}"
            if item.review_note:
                line += f" — {item.review_note}"
            lines.append(line)
        return CmdCtl.success("\n".join(lines))
