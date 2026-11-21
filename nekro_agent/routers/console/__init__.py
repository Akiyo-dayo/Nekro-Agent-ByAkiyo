"""总后台（Console）路由包

聚合 `/console` 下的全部子路由。子路由自身**不要**再带 `/console` 前缀：
它们的路径都相对本包的前缀展开。

容器管理（`containers`）与掉线监测（`health`）由并行开发的模块提供。它们尚未落地时
按缺席处理，避免整个总后台因为某个子模块还没写完而无法加载。
"""

from fastapi import APIRouter

from nekro_agent.core.logger import get_sub_logger

from .instances import router as instances_router
from .overview import router as overview_router

logger = get_sub_logger("console_api")

router = APIRouter(prefix="/console", tags=["Console"])

router.include_router(overview_router)
router.include_router(instances_router)

try:
    from .containers import router as containers_router
except ImportError:
    logger.debug("Console 容器管理路由尚未就绪，跳过挂载")
else:
    router.include_router(containers_router)

try:
    from .health import router as health_router
except ImportError:
    logger.debug("Console 掉线监测路由尚未就绪，跳过挂载")
else:
    router.include_router(health_router)

try:
    from .tenants import router as tenants_router
except ImportError:
    logger.debug("Console 租户管理路由尚未就绪，跳过挂载")
else:
    router.include_router(tenants_router)

try:
    from .model_groups import router as model_groups_router
except ImportError:
    logger.debug("Console 模型组路由尚未就绪，跳过挂载")
else:
    router.include_router(model_groups_router)

try:
    from .quota import router as quota_router
except ImportError:
    logger.debug("Console 配额管理路由尚未就绪，跳过挂载")
else:
    router.include_router(quota_router)

try:
    from .network_rules import router as network_rules_router
except ImportError:
    logger.debug("Console 隔离规则运维路由尚未就绪，跳过挂载")
else:
    router.include_router(network_rules_router)

try:
    from .identity import router as identity_router
except ImportError:
    logger.debug("Console 身份与域路由尚未就绪，跳过挂载")
else:
    router.include_router(identity_router)

try:
    from .usage import router as usage_router
except ImportError:
    logger.debug("Console 用量计量路由尚未就绪，跳过挂载")
else:
    router.include_router(usage_router)

try:
    from .channel_quota import router as channel_quota_router
except ImportError:
    logger.debug("Console 频道额度路由尚未就绪，跳过挂载")
else:
    router.include_router(channel_quota_router)

try:
    from .napcat_orphans import router as napcat_orphans_router
except ImportError:
    logger.debug("Console NapCat 残留回收路由尚未就绪，跳过挂载")
else:
    router.include_router(napcat_orphans_router)

try:
    from .qbob import router as qbob_router
except ImportError:
    logger.debug("Console qbob 托管路由尚未就绪，跳过挂载")
else:
    router.include_router(qbob_router)

try:
    from .onebot_endpoint import router as onebot_endpoint_router
except ImportError:
    logger.debug("Console OneBot 协议端状态路由尚未就绪，跳过挂载")
else:
    router.include_router(onebot_endpoint_router)

try:
    from .ops_audit import router as ops_audit_router
except ImportError:
    logger.debug("Console 运维审计路由尚未就绪，跳过挂载")
else:
    router.include_router(ops_audit_router)
