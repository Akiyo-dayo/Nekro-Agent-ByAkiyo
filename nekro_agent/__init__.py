import asyncio
import logging
import time

from nonebot import get_app, get_driver
from nonebot.adapters.onebot.v11 import Bot
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel

from nekro_agent.adapters import cleanup_adapters, init_adapters
from nekro_agent.core.args import Args
from nekro_agent.core.database import init_db
from nekro_agent.core.db_migration import run_db_migrations
from nekro_agent.core.logger import logger
from nekro_agent.routers import mount_api_routes, mount_middlewares
from nekro_agent.services.console.instance_health import instance_health_service
from nekro_agent.services.mail.mail_service import send_bot_status_email
from nekro_agent.services.memory.feature_flags import is_memory_system_enabled
from nekro_agent.services.memory.scheduler import memory_scheduler
from nekro_agent.services.plugin.collector import init_plugins
from nekro_agent.services.runtime_state import mark_shutting_down, mark_started
from nekro_agent.services.timer.recurring_timer_service import recurring_timer_service
from nekro_agent.services.timer.timer_service import timer_service
from nekro_agent.systems.cloud.scheduler import start_telemetry_task, stop_telemetry_task

logging.getLogger("passlib").setLevel(logging.ERROR)


async def _init_memory_scheduler() -> None:
    """初始化记忆调度器"""
    if not is_memory_system_enabled():
        logger.info("记忆系统总开关关闭，跳过记忆调度器初始化")
        return

    from nekro_agent.services.memory.consolidator import consolidate_workspace
    from nekro_agent.services.memory.embedding_service import get_memory_embedding_dimension
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

    # 确保 Qdrant Collection 存在
    try:
        await memory_qdrant_manager.ensure_collection(get_memory_embedding_dimension())
    except Exception as e:
        logger.warning(f"记忆系统 Qdrant Collection 初始化失败（可能 Qdrant 未启用）: {e}")

    async def consolidation_handler(workspace_id: int, chat_key: str) -> None:
        """沉淀处理器"""
        try:
            result = await consolidate_workspace(workspace_id, chat_key)
            logger.debug(
                f"记忆沉淀完成: workspace={workspace_id}, "
                f"paragraphs={result.paragraphs_created}, "
                f"entities={result.entities_created}",
            )
        except Exception as e:
            logger.warning(f"记忆沉淀失败: workspace={workspace_id}, error={e}")

    memory_scheduler.set_consolidation_handler(consolidation_handler)
    await memory_scheduler.start()


async def _init_kb_collection() -> None:
    """初始化知识库向量 Collection（工作区 + 全局）。"""
    try:
        from nekro_agent.services.kb.index_service import ensure_kb_collection

        await ensure_kb_collection()
    except Exception as e:
        logger.warning(f"知识库 Qdrant Collection 初始化失败（可能 Qdrant 未启用）: {e}")

    try:
        from nekro_agent.services.kb.library_index_service import ensure_kb_library_collection

        await ensure_kb_library_collection()
    except Exception as e:
        logger.warning(f"全局知识库 Qdrant Collection 初始化失败（可能 Qdrant 未启用）: {e}")


async def _recover_stale_kb_tasks() -> None:
    """恢复因服务重启而卡在非终态的知识库文档/资产，重新调度索引。"""
    try:
        from nekro_agent.models.db_kb_asset import DBKBAsset
        from nekro_agent.models.db_kb_document import DBKBDocument
        from nekro_agent.services.kb.index_service import schedule_rebuild_document
        from nekro_agent.services.kb.library_index_service import schedule_rebuild_asset

        stale_statuses = ["extracting", "indexing", "pending"]

        stale_docs = await DBKBDocument.filter(sync_status__in=stale_statuses).exclude(extract_status="failed").all()
        for doc in stale_docs:
            logger.info(f"恢复非终态知识库文档索引: document_id={doc.id}, status={doc.sync_status}")
            await schedule_rebuild_document(doc)

        stale_assets = await DBKBAsset.filter(sync_status__in=stale_statuses).exclude(extract_status="failed").all()
        for asset in stale_assets:
            logger.info(f"恢复非终态全局知识库资产索引: asset_id={asset.id}, status={asset.sync_status}")
            await schedule_rebuild_asset(asset)

        total = len(stale_docs) + len(stale_assets)
        if total:
            logger.info(f"共恢复 {total} 个非终态知识库索引任务（文档 {len(stale_docs)}，资产 {len(stale_assets)}）")
    except Exception as e:
        logger.warning(f"恢复非终态知识库索引任务失败: {e}")


async def _report_onebot_instance_status(adapter_name: str, self_id: str, *, is_online: bool) -> None:
    """把 NoneBot 的连接/断连事件同步到适配器实例健康状态

    该 driver 钩子只对走 NoneBot 的适配器生效，这里进一步限定为 OneBot V11：
    只有它的 `self_id` 能通过实例注册表解析为实例标识，其他适配器直接跳过。

    Args:
        adapter_name: NoneBot 适配器名称
        self_id: 协议端上报的账号号码
        is_online: 是否为连接事件
    """
    if adapter_name != "OneBot V11":
        return

    try:
        from nekro_agent.adapters.onebot_v11.core.channel import ADAPTER_KEY
        from nekro_agent.adapters.onebot_v11.core.instance import resolve_instance_key
        from nekro_agent.services.console.instance_health import instance_health_service

        instance_key = await resolve_instance_key(self_id)
        if is_online:
            await instance_health_service.report_connect(ADAPTER_KEY, instance_key, message=f"协议端已连接: {self_id}")
        else:
            await instance_health_service.report_disconnect(ADAPTER_KEY, instance_key, message=f"协议端已断开: {self_id}")
    except Exception as e:
        # 连接事件回调失败不应影响 NoneBot 的连接生命周期
        logger.warning(f"同步 OneBot 实例连接状态失败: self_id={self_id}, online={is_online}, error={e}")


async def _should_mail_bot_status(adapter_name: str, self_id: str) -> bool:
    """这次连上 / 断开要不要给管理员发状态邮件

    OneBot 只给已认领、在用的实例发：连接钩子在入站闸门之前执行，部署级令牌为空时任何人都能带一个陌生账号
    连上来（被自动登记成待认领实例），反复连断就能刷管理员邮箱。「在用」与收发闸门同一个判据
    （`DBAdapterInstance.is_instance_active`：实例停用 / 归档 / 软删、所属租户停用都算不在用）——
    不认识的待认领实例，管理员的处理就是归档，之后再连上来不该再发信。查不到绑定这个账号的实例
    （自动登记失败一类）同样不发。其他适配器照旧，靠 `send_bot_status_email` 的限频兜底。
    """
    if adapter_name != "OneBot V11":
        return True
    try:
        from nekro_agent.adapters.onebot_v11.core.channel import ADAPTER_KEY
        from nekro_agent.models.db_adapter_instance import DBAdapterInstance
        from nekro_agent.services.unclaimed_dispatch import UNCLAIMED_STATUS

        row = await DBAdapterInstance.get_by_bound_account(ADAPTER_KEY, self_id)
        if row is None or row.status.strip().lower() == UNCLAIMED_STATUS:
            return False
        return await DBAdapterInstance.is_instance_active(ADAPTER_KEY, row.instance_key)
    except Exception as e:
        logger.warning(f"查询 OneBot 实例失败，本次不发连接状态邮件: self_id={self_id}, error={e}")
        return False


async def _on_bot_status_change(bot: Bot, *, is_online: bool) -> None:
    """NoneBot 连接 / 断开钩子的全部工作

    先同步实例状态再决定发不发邮件：陌生账号要到 `_report_onebot_instance_status` 里才被登记成待认领实例
    （或归给刚扫码登录的内置 NapCat），邮件按登记之后的状态判。
    """
    adapter: str = bot.adapter.get_name()
    bot_id: str = bot.self_id
    if is_online and adapter == "OneBot V11":
        # 协议端识别与自检是附加信息：查询失败不能影响下面的连接状态同步
        try:
            from nekro_agent.adapters.onebot_v11.core.endpoint_status import on_onebot_connect

            await on_onebot_connect(bot)
        except Exception as e:
            logger.warning(f"初始化 OneBot 协议端自检失败: {e}")
    await _report_onebot_instance_status(adapter, bot_id, is_online=is_online)
    if await _should_mail_bot_status(adapter, bot_id):
        await send_bot_status_email(adapter, bot_id, is_online)


class _Config(BaseModel):
    pass

def _try_get_driver():
    try:
        return get_driver()
    except ValueError:
        return None


_driver = _try_get_driver()

if _driver is not None:
    # 在应用启动前挂载中间件和主路由
    # 这是确保应用生命周期正确的唯一方法
    app = get_app()
    mount_middlewares(app)
    mount_api_routes(app)


if _driver is not None:
    @_driver.on_startup
    async def on_startup():
        mark_started()

        # 启动时不再挂载主路由，它们已在启动前挂载完毕
        app = get_app()

        # 初始化数据库、适配器和插件
        await init_db()

        # 合并存量重复频道行。必须夹在迁移之前：频道唯一约束的迁移撞上重复行会直接失败，
        # 服务将卡在启动阶段，而重复行本身正是「升级后人设/工作区绑定丢失」的成因
        from nekro_agent.services.chat_key_migration import merge_duplicate_chat_channels

        try:
            await merge_duplicate_chat_channels()
        except Exception as e:
            logger.warning(f"合并存量重复频道失败（非致命，唯一约束迁移可能因此失败）: {e}")

        # 同理：解除存量里「一个协议端账号绑了多个实例」的多余绑定。重复绑定会让入站消息
        # 找不到唯一的归属实例；同样排在迁移之前，将来补上账号唯一索引时这个顺序是必须的
        from nekro_agent.services.console.instance_manager import unbind_duplicate_account_bindings

        try:
            await unbind_duplicate_account_bindings()
        except Exception as e:
            logger.warning(f"解除重复账号绑定失败（非致命，重复绑定的实例路由仍不确定）: {e}")

        await run_db_migrations()

        # 沙盒回调地址若还是早先按旧端口自动生成的默认值，按当前部署纠正。必须在下面两件事之前：
        # `bootstrap_model_groups()` 会落盘配置（纠正值随之写回），沙盒隔离规则按回调端点放行
        try:
            from nekro_agent.services.sandbox.network_guard import reconcile_sandbox_chat_api_url

            reconcile_sandbox_chat_api_url()
        except Exception as e:
            logger.warning(f"核对沙盒回调地址失败（非致命，沿用配置值）: {e}")

        # 把配置文件里的存量模型组导入数据库，并据此重建 config.MODEL_GROUPS 运行时视图。
        # 必须紧跟迁移之后：适配器与插件初始化都会同步读 config.MODEL_GROUPS
        from nekro_agent.services.model_group_service import bootstrap_model_groups

        await bootstrap_model_groups()

        # 只读自检：跨适配器重名的实例标识会让只按标识存取的数据串用，提示管理员改名，不改数据。
        # 放在迁移之后：此时表结构已是当前版本
        from nekro_agent.services.console.instance_manager import warn_cross_adapter_instance_key_collisions

        try:
            await warn_cross_adapter_instance_key_collisions()
        except Exception as e:
            logger.warning(f"检查跨适配器重名的实例标识失败（非致命）: {e}")

        # 回收上次运行遗留的沙盒容器（必须在适配器接入消息之前完成，避免误杀新启动的沙盒）
        try:
            from nekro_agent.services.sandbox.runner import cleanup_sandbox_containers

            await cleanup_sandbox_containers()
        except Exception as e:
            logger.warning(f"清理遗留沙盒容器失败（非致命）: {e}")

        # 建好沙盒专用网络并在宿主侧装上内网隔离规则。
        # 放在启动阶段而不是只等第一次跑沙盒，是为了让「规则没装上」在启动日志里就暴露出来
        try:
            from nekro_agent.services.sandbox.network_guard import setup_sandbox_network_isolation

            await setup_sandbox_network_isolation()
        except Exception as e:
            logger.warning(f"初始化沙盒网络隔离失败（非致命）: {e}")

        # 同上，CC 工作区容器跑的同样是模型生成的代码，且原本与 NA 及其数据库共处一个网络
        try:
            from nekro_agent.services.workspace.network_guard import setup_workspace_network_isolation

            await setup_workspace_network_isolation()
        except Exception as e:
            logger.warning(f"初始化 CC 工作区网络隔离失败（非致命）: {e}")

        await init_adapters(app)

        # 轮换 Web Chat MCP 的运行期密钥，并重写引用内置条目的工作区 .mcp.json。
        # 必须排在 init_adapters 之后：它要读适配器注册表判断内置条目是否可用
        from nekro_agent.services.mcp.web_chat_auth import init_web_chat_mcp_runtime_auth

        await init_web_chat_mcp_runtime_auth()

        # 注册内置命令
        from nekro_agent.services.command.built_in import register_built_in_commands

        register_built_in_commands()
        logger.info("Built-in commands registered")

        # 存量命令权限覆盖里的 advanced 按升级前的实际效果改成 super_user，只做一次（见该方法说明）
        try:
            from nekro_agent.services.command.manager import command_manager

            command_manager.migrate_legacy_advanced_overrides()
        except Exception as e:
            logger.warning(f"迁移存量命令权限覆盖失败（非致命，下次启动重试）: {e}")

        # 超级用户只认平台方可信的来源：先把租户名下实例此刻生效的令牌记成「曾对租户可见」，
        # 再说清楚哪些部署形态会让名单里的人在哪些实例上失去超级用户
        from nekro_agent.services.unclaimed_dispatch import auto_claim_unclaimed_with_history
        from nekro_agent.services.user.super_users import log_super_user_warnings
        from nekro_agent.services.user.token_exposure import scan_tenant_token_exposure

        # 未认领实例从此只在凭部署级令牌连上来时才分发：升级前就有聊天记录的存量未认领实例先就地认领一次，
        # 否则它们会在升级后当场失声（一次性，见该函数说明）
        try:
            await auto_claim_unclaimed_with_history()
        except Exception as e:
            logger.error(f"升级时自动认领存量未认领实例失败（下次启动重试）: {e}")
        try:
            await scan_tenant_token_exposure()
        except Exception as e:
            logger.error(f"记录曾对租户可见的 OneBot 令牌失败（下次启动重试）: {e}")
        await log_super_user_warnings()

        await init_plugins()

        # 初始化默认人设（需要在数据库和迁移完成后执行）
        from nekro_agent.services.preset_service import init_default_preset

        await init_default_preset()
        logger.info("Default preset initialized")

        # 初始化插件路由管理器并挂载插件路由
        try:
            from nekro_agent.services.plugin.collector import plugin_collector
            from nekro_agent.services.plugin.router_manager import plugin_router_manager

            # 绑定FastAPI应用实例到路由管理器
            plugin_router_manager.set_app(app)

            # 挂载所有启用的插件路由
            plugins_with_router = plugin_collector.get_plugins_with_router()
            success_count = 0
            for plugin in plugins_with_router:
                if plugin_router_manager.mount_plugin_router(plugin):
                    success_count += 1

            logger.info(f"插件路由热挂载完成，成功挂载 {success_count} 个插件的路由")

        except Exception as e:
            logger.exception(f"初始化插件路由管理器失败: {e}")

        await timer_service.start()
        logger.info("Timer service initialized")

        await recurring_timer_service.start()
        logger.info("Recurring timer service initialized")

        # 实例健康监测（主动探活 + 掉线告警）需在适配器就绪后启动
        await instance_health_service.start()
        logger.info("Instance health service initialized")

        # 初始化记忆调度器
        await _init_memory_scheduler()
        logger.info("Memory scheduler initialized")

        # 初始化知识库 Collection
        await _init_kb_collection()
        logger.info("Knowledge base collection initialized")

        # 恢复因重启而卡在非终态的知识库索引任务
        await _recover_stale_kb_tasks()

        # 公告后台任务只存在于进程内存中，重启后残留的 pending/running 任务必须落终态，避免僵尸任务
        try:
            from nekro_agent.services.announcement import announcement_service

            recovered_announcements = await announcement_service.recover_stale_tasks()
            if recovered_announcements:
                logger.info(f"共标记 {recovered_announcements} 个因重启中断的公告任务为失败")
        except Exception as e:
            logger.warning(f"恢复公告任务状态失败（非致命）: {e}")

        # 遥测任务
        start_telemetry_task()

        # 延迟恢复：等待所有服务和插件就绪后执行
        async def _recover_cc_pending() -> None:
            await asyncio.sleep(5)  # 等待 message_service、频道路由等完全就绪

            # 同步工作区状态：检查 active 工作区的容器实际运行状态，修正 NA 重启后残留的 stale active 记录
            try:
                from nekro_agent.services.workspace.container import SandboxContainerManager

                await SandboxContainerManager.recover_on_startup()
                logger.info("[cc_workspace] 工作区容器状态同步完成")
            except Exception as e:
                logger.warning(f"[cc_workspace] 工作区容器状态同步失败（非致命）: {e}")

            # 取回 CC 在 NA 断线期间完成的任务结果
            try:
                from builtin.cc_workspace.main import recover_pending_cc_results

                await recover_pending_cc_results()
            except ImportError:
                pass  # cc_workspace 插件未加载，跳过
            except Exception as e:
                logger.error(f"[cc_workspace] CC 待投递结果恢复任务失败: {e}")

            # 恢复因重启中断的工作区记忆重建任务
            if is_memory_system_enabled():
                try:
                    from nekro_agent.services.memory.rebuild import recover_pending_memory_rebuilds

                    await recover_pending_memory_rebuilds()
                except Exception as e:
                    logger.error(f"[memory] 记忆重建恢复任务失败: {e}")

        asyncio.create_task(_recover_cc_pending())

    @_driver.on_shutdown
    async def on_shutdown():
        shutdown_started_at = time.perf_counter()
        mark_shutting_down()
        logger.debug("[shutdown] begin")

        step_started_at = time.perf_counter()
        logger.debug("[shutdown] stopping telemetry task")
        await stop_telemetry_task()
        logger.debug(f"[shutdown] telemetry task stopped in {time.perf_counter() - step_started_at:.3f}s")

        step_started_at = time.perf_counter()
        logger.debug("[shutdown] stopping memory scheduler")
        await memory_scheduler.stop()
        logger.debug(f"[shutdown] memory scheduler stopped in {time.perf_counter() - step_started_at:.3f}s")

        # 必须早于适配器清理：探活会调用适配器，适配器拆完再探活会抛一片噪声异常
        step_started_at = time.perf_counter()
        logger.debug("[shutdown] stopping instance health service")
        await instance_health_service.stop()
        logger.debug(f"[shutdown] instance health service stopped in {time.perf_counter() - step_started_at:.3f}s")

        step_started_at = time.perf_counter()
        logger.debug("[shutdown] stopping recurring timer service")
        await recurring_timer_service.stop()
        logger.debug(f"[shutdown] recurring timer service stopped in {time.perf_counter() - step_started_at:.3f}s")

        step_started_at = time.perf_counter()
        logger.debug("[shutdown] stopping timer service")
        await timer_service.stop()
        logger.debug(f"[shutdown] timer service stopped in {time.perf_counter() - step_started_at:.3f}s")

        step_started_at = time.perf_counter()
        logger.debug("[shutdown] cleaning up adapters")
        await cleanup_adapters(get_app())
        logger.debug(f"[shutdown] adapters cleaned up in {time.perf_counter() - step_started_at:.3f}s")

        # 停止 CC 后台结果监听器
        step_started_at = time.perf_counter()
        try:
            logger.debug("[shutdown] stopping cc result watcher")
            from builtin.cc_workspace.main import shutdown_cc_result_watcher

            await shutdown_cc_result_watcher()
            logger.debug(f"[shutdown] cc result watcher stopped in {time.perf_counter() - step_started_at:.3f}s")
        except ImportError:
            logger.debug("[shutdown] cc result watcher not loaded, skipped")
            pass  # cc_workspace 插件未加载，跳过
        except Exception as e:
            logger.warning(f"[cc_workspace] 停止后台结果监听器失败: {e}")

        step_started_at = time.perf_counter()
        try:
            logger.debug("[shutdown] cleaning up plugins")
            from nekro_agent.services.plugin.collector import plugin_collector

            await plugin_collector.cleanup_all_plugins()
            logger.debug(f"[shutdown] plugins cleaned up in {time.perf_counter() - step_started_at:.3f}s")
        except Exception as e:
            logger.exception(f"清理插件时发生错误: {e}")

        # 把还在飞的用量流水写完
        #
        # 记账是**脱手**的后台任务（`services/usage/ledger.py`：算钱不占主链路，所以
        # `record_llm_usage` 只入队就返回）。进程退出时事件循环一关，队列里没跑完的那些
        # 连同它们记录的花销一起消失——而且是静默消失，正是这本账最不该有的性质。
        # 位置在适配器清理**之后**：那之前还可能有新的对话进来，flush 完又会积起新的。
        step_started_at = time.perf_counter()
        try:
            logger.debug("[shutdown] flushing llm usage ledger")
            from nekro_agent.services.usage import flush_llm_usage, usage_recorder_stats

            flushed = await flush_llm_usage()
            pending, dropped, failed = usage_recorder_stats()
            # **INFO 而不是 DEBUG**（周围那些收尾步骤都是 DEBUG，这一条故意不是）：
            # `APP_LOG_LEVEL` 默认 INFO，DEBUG 在生产上根本不落盘。而
            # `docs/operations.md §2.3` 把这一行当成"账本收尾过、这次运行一条都没丢"的
            # **唯一凭据**——写成 DEBUG 的话那条判据在健康部署上恒为 0 命中，
            # 运维会去查一个不存在的故障，而真的丢账时同样是 0 命中，两者分不开。
            logger.info(
                f"[shutdown] llm usage ledger flushed in {time.perf_counter() - step_started_at:.3f}s "
                f"(written={flushed} pending={pending} dropped={dropped} failed={failed})",
            )
            if dropped or failed:
                # 本次进程生命周期内**确实丢过账**。逐条的 WARNING 淹在日志里翻不出来，
                # 收尾时给一条汇总：这两个数不为零就说明账本对不平，要去查为什么。
                logger.warning(f"用量流水本次运行期间丢弃 {dropped} 条、落库失败 {failed} 条，账面可能少计")
        except Exception as e:
            logger.warning(f"用量流水收尾失败: {e}")

        # 关闭 LLM/Embedding 共享 HTTP 连接池
        step_started_at = time.perf_counter()
        try:
            logger.debug("[shutdown] closing shared http clients")
            from nekro_agent.services.agent.openai import reset_http_clients

            await reset_http_clients()
            logger.debug(f"[shutdown] shared http clients closed in {time.perf_counter() - step_started_at:.3f}s")
        except Exception as e:
            logger.warning(f"关闭共享 HTTP 客户端失败: {e}")

        logger.debug(f"[shutdown] finished in {time.perf_counter() - shutdown_started_at:.3f}s")
        logger.info("Timer service stopped")

    @_driver.on_bot_connect
    async def on_bot_connect(bot: Bot):
        await _on_bot_status_change(bot, is_online=True)

    @_driver.on_bot_disconnect
    async def on_bot_disconnect(bot: Bot):
        await _on_bot_status_change(bot, is_online=False)


__plugin_meta__ = PluginMetadata(
    name="nekro-agent",
    description="集代码执行/高度可扩展性为一体的聊天机器人，应用了容器化技术快速构建沙盒环境",
    usage="",
    type="application",
    homepage="https://github.com/KroMiose/nekro-agent",
    supported_adapters={"~onebot.v11"},
    config=_Config,
)

global_config = _driver.config if _driver is not None else None  # 我觉得这玩意可以删掉 没人用


# 启动 Api 服务进程
# threading.Thread(target=start, daemon=True).start()

if Args.LOAD_TEST:
    logger.success("Plugin load tested successfully")
    exit(0)
