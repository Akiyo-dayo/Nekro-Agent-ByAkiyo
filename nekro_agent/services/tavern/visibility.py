"""按当前身份筛出「看得到」的人设书

导出与共享到云端都要把知识书一起带走，而两者共用同一条可见性判据：平台方看全部；
租户只看自己实例名下的书，归属为空（全局）的书对租户不可见。判据放在服务层而不是
路由里，是为了让导出接口与共享接口用同一份实现——两处各写一遍，迟早会出现「导出的
卡只带自己的书、共享的卡却把别人的书也传出去」这种只在出站时才暴露的分叉。
"""

from __future__ import annotations

from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.services.memory.instance_scope import admin_row_visible
from nekro_agent.services.user.tenant_perm import get_tenant_scope
from nekro_agent.services.worldbook import service as wb_service
from nekro_agent.services.worldbook.types import ScopeKind


async def visible_preset_books(user: DBUser, preset_id: int) -> list[tuple[DBWbBook, list[DBWbEntry]]]:
    """返回当前身份看得到的人设书与条目

    平台方的全局视图返回全部；租户逐本按 ``admin_row_visible`` 判归属，空归属的书
    对租户不可见（那里的空串不是通配）。条目按书内既有顺序返回，调用方不再排序。
    """
    books = await DBWbBook.filter(scope_kind=ScopeKind.PRESET.value, preset_id=preset_id).order_by("id")
    tenant_scope = get_tenant_scope(user)
    result: list[tuple[DBWbBook, list[DBWbEntry]]] = []
    for book in books:
        if tenant_scope is not None and not await admin_row_visible(user, book.adapter_key, book.instance_key):
            continue
        result.append((book, await wb_service.list_entries(book)))
    return result


__all__ = ["visible_preset_books"]
