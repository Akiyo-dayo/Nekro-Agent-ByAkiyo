"""落库前的 NUL 保险：所有写入 `chat_message` / `user` / `chat_channel` 的路径统一清洗

入口处（`message_service.push_human_message`、`register_platform_user`、
`DBChatChannel.get_or_create`）已经先清洗了一遍，为的是让触发判断、落库、广播看到同一份数据。
但写这三张表的路不止入口那几条：Bot 回复、系统消息、工具结果、后台改名都会直接
`create` / `save`。逐个调用点补一遍迟早会漏，所以在这里挂一层 `pre_save` 监听器兜底——
即使某个调用点漏了，PG 也不会因为一个 NUL 把整条记录丢掉。

监听器里的日志刻意不写字段内容：NUL 常常出现在群名片与正文里，属于用户数据。
"""

from tortoise import fields
from tortoise.models import Model

from nekro_agent.core.logger import logger
from nekro_agent.tools.text_sanitize import strip_nul

# 只清洗这几类会落到 PG text / varchar / jsonb 的字段。
# 主键、时间、整型等不可能含 NUL，遍历它们没有意义。
_SANITIZED_FIELD_TYPES = (fields.CharField, fields.TextField, fields.JSONField)


async def strip_nul_before_save(
    sender: type[Model],
    instance: Model,
    using_db: object = None,
    update_fields: object = None,
) -> None:
    """`pre_save` 监听器：把待写字段里的 NUL 剔除掉

    只有清洗结果与原值不等时才 `setattr` 并打日志：入口（`push_human_message` 等）已经清洗过的
    正常数据在这里是零改动，所以这条日志一旦出现，就说明有某条写库路径漏了入口清洗。

    `using_db` / `update_fields` 由 tortoise 传入，这里用不上，保留是为了对齐监听器签名。

    partial 实例（只查了部分列）上取不到的字段直接跳过：`getattr` 的默认值 `None`
    本身就会被 `strip_nul` 原样返回，但显式判一下更清楚。
    """
    del using_db, update_fields
    for field_name, field in instance._meta.fields_map.items():  # noqa: SLF001
        if not isinstance(field, _SANITIZED_FIELD_TYPES):
            continue
        value = getattr(instance, field_name, None)
        if value is None:
            continue
        cleaned = strip_nul(value)
        if cleaned != value:
            setattr(instance, field_name, cleaned)
            # 只报位置，不报内容：群名片与正文都是用户数据
            logger.warning(f"{sender.__name__}.{field_name} 含 NUL，已剔除")


__all__ = ["strip_nul_before_save"]
