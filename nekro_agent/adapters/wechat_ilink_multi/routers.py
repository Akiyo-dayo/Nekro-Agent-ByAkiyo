import re
from typing import cast

from fastapi import APIRouter, Depends, Response

from nekro_agent.adapters import loaded_adapters
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.models.db_adapter_instance_session import DBAdapterInstanceSession
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.services.adapter_instance_service import adapter_instance_service
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, get_tenant_scope, require_platform_owner

from .adapter import WeChatILinkMultiAdapter
from .bot_manager import BotManager
from .schemas import (
    BindStartResponse,
    BindStatusResponse,
    EventResponse,
    InstanceCreateRequest,
    InstanceCreateResponse,
    InstanceDetailResponse,
    InstanceListResponse,
    RenewPolicyRequest,
)

ADAPTER_KEY = "wechat_ilink_multi"

# 新建实例时标识的形态：与核心配置实例层覆盖的路径段同一条（`config_service._SAFE_IDENTIFIER_PATTERN`）。
# 标识会拼进插件设置、配置覆盖的目录名：带尾随空格或点的名字在 Windows 上与去掉它们的名字落到同一个目录，
# `..` 之类更是路径片段——两个实例共用一份设置就是跨租户读写
_NEW_INSTANCE_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _assert_new_instance_key(instance_key: str) -> None:
    if not _NEW_INSTANCE_KEY_PATTERN.fullmatch(instance_key):
        raise ValidationError(reason="instance_key 只能由字母、数字、下划线和连字符组成，长度 1~128")


async def _assert_tenant_owns(user: DBUser, instance_key: str) -> None:
    """租户身份下校验实例归属，越权与不存在同为 404；平台方直接放行，行为与改造前一致

    必须带上本适配器的标识：另一个适配器下可以有同名实例归这个租户，
    只按标识判会把那一行的归属当成这一行的。
    """
    if get_tenant_scope(user) is None:
        return
    await assert_instance_accessible(user, instance_key, adapter_key=ADAPTER_KEY)


def _get_bot_manager() -> BotManager:
    """获取当前加载的 BotManager 实例。"""
    adapter = loaded_adapters.get(ADAPTER_KEY)
    if adapter is None or not isinstance(adapter, WeChatILinkMultiAdapter):
        raise ValidationError(reason=f"适配器 {ADAPTER_KEY} 未加载或不可用")
    return cast(WeChatILinkMultiAdapter, adapter).bot_manager


def _to_detail_response(instance: DBAdapterInstance, *, hide_metadata: bool = False) -> InstanceDetailResponse:
    """将 DBAdapterInstance 转换为 InstanceDetailResponse（不包含凭据）。

    `hide_metadata` 用于租户视图——元数据由平台方维护（配额上限、协议端令牌等），不下发给租户。
    """
    return InstanceDetailResponse(
        instance_key=instance.instance_key,
        display_name=instance.display_name,
        status=instance.status,
        enabled=instance.enabled,
        is_default=instance.is_default,
        provider=instance.provider,
        provider_account_id=instance.provider_account_id,
        metadata_json="" if hide_metadata else instance.metadata_json,
        last_error=instance.last_error or None,
        last_active_at=instance.last_active_at,
        next_renew_at=instance.next_renew_at,
        renew_before_minutes=instance.renew_before_minutes,
        create_time=instance.create_time,
        update_time=instance.update_time,
    )


def create_router() -> APIRouter:
    """创建适配器管理路由

    ## 租户能碰到哪些端点

    实例的新建、删除、启停、续期与续期策略是平台方的运维动作，与总后台的实例生命周期同一档，
    租户身份一律 403（`Depends(require_platform_owner())`）。租户只能看、只能登录自己名下的实例：
    列表按归属过滤，详情、事件与扫码绑定对别人的实例一律 404。扫码绑定是租户给自己的 Bot
    登录的自助入口，绑定确认后连接由 `BotManager` 自己拉起，不需要租户再调启动接口。
    """
    router = APIRouter(prefix="/instances", tags=["WeChat iLink Multi Instance Management"])

    @router.get("", response_model=InstanceListResponse)
    @require_role(Role.Admin)
    async def list_instances(
        _current_user: DBUser = Depends(get_current_active_user),
    ) -> InstanceListResponse:
        """获取实例列表（租户只看得到自己名下的实例）"""
        instances = await adapter_instance_service.list_instances(adapter_key=ADAPTER_KEY)
        scope = get_tenant_scope(_current_user)
        if scope is None:
            return InstanceListResponse(instances=[_to_detail_response(inst) for inst in instances])
        return InstanceListResponse(
            instances=[_to_detail_response(inst, hide_metadata=True) for inst in instances if inst.tenant_id == scope],
        )

    @router.post("", response_model=InstanceCreateResponse, status_code=201)
    @require_role(Role.Admin)
    async def create_instance(
        body: InstanceCreateRequest,
        response: Response,
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> InstanceCreateResponse:
        """创建实例（幂等）"""
        if not body.instance_key:
            raise ValidationError(reason="instance_key 不能为空")
        if len(body.instance_key) > 128:
            # 超过列宽的标识不可能有已存在的行；不先拦的话下面按它查库就是一个 500
            _assert_new_instance_key(body.instance_key)

        existing = await adapter_instance_service.get_instance(
            adapter_key=ADAPTER_KEY,
            instance_key=body.instance_key,
        )
        # 已存在且未软删 -> 幂等返回；软删实例走下方 create_instance 复活流程（避免 instance_key 永久占用为僵尸）
        if existing is not None and existing.status != "deleted":
            response.status_code = 200
            return InstanceCreateResponse(
                instance_key=existing.instance_key,
                status=existing.status,
                renew_before_minutes=existing.renew_before_minutes,
                created_at=existing.create_time,
                existing=True,
            )

        # 已有的行（含早先用别的字符建的）照旧幂等返回、可复活；只有新标识才按形态校验
        if existing is None:
            _assert_new_instance_key(body.instance_key)
        instance = await adapter_instance_service.create_instance(
            adapter_key=ADAPTER_KEY,
            instance_key=body.instance_key,
            display_name=body.display_name,
            provider=body.provider,
        )
        return InstanceCreateResponse(
            instance_key=instance.instance_key,
            status=instance.status,
            renew_before_minutes=instance.renew_before_minutes,
            created_at=instance.create_time,
            existing=False,
        )

    @router.get("/{instance_key}", response_model=InstanceDetailResponse)
    @require_role(Role.Admin)
    async def get_instance(
        instance_key: str,
        _current_user: DBUser = Depends(get_current_active_user),
    ) -> InstanceDetailResponse:
        """获取实例详情"""
        await _assert_tenant_owns(_current_user, instance_key)
        instance = await adapter_instance_service.get_instance(
            adapter_key=ADAPTER_KEY,
            instance_key=instance_key,
        )
        if instance is None:
            raise NotFoundError(resource=f"实例 {instance_key}")
        return _to_detail_response(instance, hide_metadata=get_tenant_scope(_current_user) is not None)

    @router.delete("/{instance_key}", status_code=204)
    @require_role(Role.Admin)
    async def delete_instance(
        instance_key: str,
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> None:
        """软删除实例，并停掉它仍在运行的连接

        只落软删标记不会让连接退出：BotConnection 持有独立的长轮询任务，会一直空转拉取并推进同步游标
        （入站闸门 `is_instance_active` 会丢掉这些消息，但拉下来的消息也就此丢了）。

        顺序是先落软删标记、再停连接：「不可接收」在数据库侧先一步生效，停止窗口里到达的在途消息
        被闸门挡下；`BotManager.stop_instance` 不会把 `deleted` 覆写成 `offline`。
        """
        instance = await adapter_instance_service.get_instance(
            adapter_key=ADAPTER_KEY,
            instance_key=instance_key,
        )
        if instance is None:
            raise NotFoundError(resource=f"实例 {instance_key}")
        await adapter_instance_service.soft_delete_instance(instance)

        # 适配器可能没有加载（配置里关掉了），这时没有可停的连接，不阻塞删除
        try:
            bot_manager = _get_bot_manager()
        except ValidationError:
            return
        await bot_manager.stop_instance(instance_key, update_status=False)

    @router.post("/{instance_key}/bind/start", response_model=BindStartResponse)
    @require_role(Role.Admin)
    async def start_bind(
        instance_key: str,
        _current_user: DBUser = Depends(get_current_active_user),
    ) -> BindStartResponse:
        """开始二维码绑定"""
        await _assert_tenant_owns(_current_user, instance_key)
        bot_manager = _get_bot_manager()
        result = await bot_manager.start_bind(instance_key)
        return BindStartResponse(
            bind_session_id=result.session_id,
            qr_url=result.qr_url,
            status="ok",
            bind_status="qr_generated",
            expires_at=result.expires_at,
        )

    @router.get("/{instance_key}/bind/status/{bind_session_id}", response_model=BindStatusResponse)
    @require_role(Role.Admin)
    async def get_bind_status(
        instance_key: str,
        bind_session_id: str,
        _current_user: DBUser = Depends(get_current_active_user),
    ) -> BindStatusResponse:
        """轮询绑定状态"""
        await _assert_tenant_owns(_current_user, instance_key)
        bot_manager = _get_bot_manager()
        result = await bot_manager.poll_bind(instance_key, bind_session_id)
        message = result.error_message
        if result.status.value == "confirmed":
            message = "绑定成功"
        elif result.status.value == "pending":
            message = "等待扫码"
        elif result.status.value == "scanned":
            message = "二维码已扫描"
        elif result.status.value == "expired":
            message = "绑定会话已过期"
        # 返回规范的绑定子状态（qr_generated/qr_scanned/login_confirmed/session_persisted）与主生命周期状态。
        # poll_bind 已将 session.session_state 推进到对应子状态；无会话时回退到原始 poll 状态。
        instance = await adapter_instance_service.get_instance(ADAPTER_KEY, instance_key)
        session = await DBAdapterInstanceSession.get_or_none(instance_id=instance.id) if instance is not None else None
        bind_substate = session.session_state if session is not None and session.session_state else result.status.value
        return BindStatusResponse(
            status="ok",
            instance_status=instance.status if instance is not None else "",
            bind_status=bind_substate,
            message=message,
        )

    @router.post("/{instance_key}/start", status_code=204)
    @require_role(Role.Admin)
    async def start_instance(
        instance_key: str,
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> None:
        """启动实例"""
        bot_manager = _get_bot_manager()
        connection = await bot_manager.start_instance(instance_key)
        if connection is None:
            raise ValidationError(reason=f"实例 {instance_key} 启动失败，请检查会话凭据")

    @router.post("/{instance_key}/stop", status_code=204)
    @require_role(Role.Admin)
    async def stop_instance(
        instance_key: str,
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> None:
        """停止实例"""
        bot_manager = _get_bot_manager()
        await bot_manager.stop_instance(instance_key)

    @router.post("/{instance_key}/renew", response_model=InstanceDetailResponse)
    @require_role(Role.Admin)
    async def renew_instance(
        instance_key: str,
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> InstanceDetailResponse:
        """触发续期"""
        bot_manager = _get_bot_manager()
        instance = await bot_manager.renew_instance(instance_key)
        return _to_detail_response(instance)

    @router.patch("/{instance_key}/renew-policy", response_model=InstanceDetailResponse)
    @require_role(Role.Admin)
    async def update_renew_policy(
        instance_key: str,
        body: RenewPolicyRequest,
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> InstanceDetailResponse:
        """更新续期策略"""
        instance = await adapter_instance_service.get_instance(
            adapter_key=ADAPTER_KEY,
            instance_key=instance_key,
        )
        if instance is None:
            raise NotFoundError(resource=f"实例 {instance_key}")

        instance.renew_before_minutes = body.renew_before_minutes
        # 只写这一列：整行写回会把读出之后别处改的列（租户改划、停用、连接写的状态）冲回旧值
        await instance.save(update_fields=["renew_before_minutes", "update_time"])
        return _to_detail_response(instance)

    @router.get("/{instance_key}/events", response_model=list[EventResponse])
    @require_role(Role.Admin)
    async def list_instance_events(
        instance_key: str,
        _current_user: DBUser = Depends(get_current_active_user),
    ) -> list[EventResponse]:
        """获取实例事件列表"""
        await _assert_tenant_owns(_current_user, instance_key)
        instance = await adapter_instance_service.get_instance(
            adapter_key=ADAPTER_KEY,
            instance_key=instance_key,
        )
        if instance is None:
            raise NotFoundError(resource=f"实例 {instance_key}")

        events = await DBAdapterInstanceEvent.filter(instance_id=instance.id).order_by("-create_time")
        return [
            EventResponse(
                event_type=e.event_type,
                status_from=e.status_from or None,
                status_to=e.status_to or None,
                message=e.message or None,
                create_time=e.create_time,
            )
            for e in events
        ]

    return router
