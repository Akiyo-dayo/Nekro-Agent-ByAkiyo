"""租户实例归属的回归测试

保护的是「界面上看到的归属必须等于库里的归属」这条契约。它曾经断过一次，
表现是用户报的这个 BUG：**把实例划归租户后再回到界面，租户又变回未归属**。

链路是这样断的：`PUT /console/tenants/{id}/instances` 确实把 `tenant_id` 写进了库
（`GET /console/tenants/{id}` 的 `instance_count` 也随之变化），但列表接口用的
`build_instance_info()` 把 `tenant_id` 硬编码成了 `None`。前端的划归界面正是靠
`InstanceInfo.tenant_id == 当前租户` 算出勾选集合的，于是勾选恒为空、归属列恒显示未归属。

这不只是显示问题：该接口是**整表覆盖**语义，勾选集合就是提交后该租户的全部实例。
界面把已绑定的实例显示成未勾选，用户再勾一个别的保存，就会把原有归属全部解绑。

因此这里钉死三件事：

1. `build_instance_info()` 必须原样下发库里的 `tenant_id`（回归本体）；
2. `InstanceInfo.tenant_id` 不带业务默认值，未归属就是 None；
3. 覆盖语义下「没带 instance_keys」与「显式传空列表」必须可区分——
   前者是客户端故障，按空列表处理会把一次失败的请求变成一次全量解绑。
"""

from typing import Optional

import pytest

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.schemas.console import InstanceStats
from nekro_agent.schemas.tenant import TenantInstanceBindRequest
from nekro_agent.services.console.instance_manager import build_instance_info

TENANT_ID = 7
OTHER_TENANT_ID = 9


def make_instance(instance_key: str, tenant_id: Optional[int]) -> DBAdapterInstance:
    """构造一条未落库的实例行

    `build_instance_info()` 只读字段、不发查询（非 builtin_napcat 形态直接跳过容器查询），
    因此这里不需要数据库。
    """
    return DBAdapterInstance(
        adapter_key="onebot_v11",
        instance_key=instance_key,
        display_name=instance_key,
        status="online",
        provider="remote_onebot",
        tenant_id=tenant_id,
        metadata_json="",
    )


class TestInstanceInfoCarriesTenant:
    """列表/详情接口下发的归属必须等于库里的归属"""

    @pytest.mark.parametrize("tenant_id", [TENANT_ID, OTHER_TENANT_ID])
    async def test_bound_instance_reports_its_tenant(self, tenant_id: int) -> None:
        """回归本体：库里绑了租户，接口就必须报出这个租户

        曾经这里恒为 None，导致划归界面永远显示未归属。
        """
        info = await build_instance_info(make_instance("inst1", tenant_id), InstanceStats())
        assert info.tenant_id == tenant_id

    async def test_unbound_instance_reports_none(self) -> None:
        """未归属就是 None，不能被填成任何占位值"""
        info = await build_instance_info(make_instance("inst2", None), InstanceStats())
        assert info.tenant_id is None

    async def test_selection_derived_from_response_matches_bound_set(self) -> None:
        """按前端的算法从响应还原勾选集合，必须与库里的绑定完全一致

        划归界面就是这么算 `boundKeys` 的（见 `TenantInstanceBinder`）。这条用例把
        「显示的归属 == 真实的归属」直接表达出来：任何把 tenant_id 抹平的实现，
        算出来的集合都会是空的，而空集合提交回去就是一次全量解绑。
        """
        rows = [
            make_instance("inst1", TENANT_ID),
            make_instance("inst2", TENANT_ID),
            make_instance("inst3", OTHER_TENANT_ID),
            make_instance("inst4", None),
        ]
        infos = [await build_instance_info(row, InstanceStats()) for row in rows]

        selected = [info.instance_key for info in infos if info.tenant_id == TENANT_ID]
        assert selected == ["inst1", "inst2"]

        owned_by_other = [
            info.instance_key for info in infos if info.tenant_id is not None and info.tenant_id != TENANT_ID
        ]
        assert owned_by_other == ["inst3"]


class TestBindRequestDistinguishesOmissionFromClear:
    """覆盖语义下，缺字段与显式清空必须是两回事"""

    def test_omitted_field_is_none_not_empty_list(self) -> None:
        """字段缺失必须留成 None 交给路由层拒绝

        默认成空列表会让一次残缺的请求静默变成一次全量解绑。
        """
        assert TenantInstanceBindRequest().instance_keys is None

    def test_explicit_empty_list_is_preserved(self) -> None:
        """显式传空列表是「解绑全部」的有意操作，必须原样保留"""
        assert TenantInstanceBindRequest(instance_keys=[]).instance_keys == []

    def test_explicit_list_is_preserved(self) -> None:
        assert TenantInstanceBindRequest(instance_keys=["inst1", "inst2"]).instance_keys == ["inst1", "inst2"]
