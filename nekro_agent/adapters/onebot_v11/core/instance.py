"""OneBot V11 实例注册表与当前实例上下文

## 实例身份为什么不用 QQ 号

一个实例对应一个 Bot 账号，但实例的身份必须**先于账号存在**：
在总后台新建实例时容器还没起、还没扫码，此时并不知道 QQ 号。

更重要的是账号可换绑。若用 QQ 号作身份，账号被封换号后该实例的插件配置、人设、
好感度、记忆、历史会话会全部变成孤儿数据。因此实例身份是一个创建时分配的稳定 slug，
QQ 号只是它的一个绑定属性（存在 `DBAdapterInstance.provider_account_id`）。

## 当前实例上下文

一个实例只对应一个账号，因此在某个实例的处理链路里"当前 Bot"是唯一确定的。
入站时把实例写进 contextvar，异步任务链自动继承，第三方插件里既有的无参
`get_bot()` 写法在多实例部署下也能解析到正确的账号，而不是静默串号。

沙盒 RPC 回调是独立的 HTTP 请求、contextvar 不会传播，但它们携带 chat_key，
调用方应改用 `get_bot_by_chat_key()`。

## 自动登记之前先问一句「是不是我自己起的容器刚登录上」

内置 NapCat 扫码成功后，协议端带着一个 NA 还没见过的账号连回来。只看数据库的话它与
「用户自己在别处架的协议端」一模一样，于是会被登记成一行 `ob{QQ}` 未认领实例 —— 用总后台
一键起容器 + 扫码的人，照样得去认领一次。

缺的信息不在数据库里，在容器里：内置 NapCat 的容器是 NA 亲手建的，**「容器 ↔ 实例」没有歧义**，
问一句容器自己的 WebUI 就能补上「容器 ↔ 账号」。因此 `resolve_instance_key()` 在落到自动登记
之前先做一次归属推导（`container_manager.claim_account_from_builtin_login`），推得出来就把账号
回写给那个实例并直接返回它。

**这一步刻意做在 `resolve_instance_key` 内部，而不是另起一个监听。** 回写与自动登记是同一个
决定的两面：分成两处就有了先后，谁先跑到取决于调度，而「回写晚了一步」的结果正好是这次要
消灭的那一行 `ob{QQ}`。放在同一个分支里，「绑给既有实例」与「登记成未认领」在结构上互斥。
"""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Dict, Iterator, Optional

from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance

from .channel import ADAPTER_KEY, is_valid_instance_key

logger = get_sub_logger("adapter.onebot_v11")

# 归属推导的总预算。超时按「推不出来」处理，落回自动登记。
#
# 这条链路是入站消息的第一步，绝不能被一个卡住的容器 WebUI 拖成不确定的挂起——
# 与 §2.6 那次沙盒事故同一条教训：任何等待类调用都必须自带上限。
# 单次探测已有 `NAPCAT_LOGIN_PROBE_TIMEOUT_SECONDS`，这里是把重试轮次也一起兜住。
#
# **预算是权威，重试是尽力而为**：候选全部挂在 TCP 连接超时上时三轮凑不满就会被这里砍断。
# 那种情形下砍不砍断结论一样（都是推不出来），而砍断保证了入站延迟有一个确定的上界。
LOGIN_ATTRIBUTION_BUDGET_SECONDS = 8.0


@dataclass(frozen=True)
class InstanceContext:
    """当前处理链路所属的实例

    同时持有账号号码，是为了让第三方插件里既有的**同步**无参 `get_bot()` 调用
    无需查库即可解析到正确的连接。
    """

    instance_key: str
    account_id: str


_current_instance: ContextVar[Optional[InstanceContext]] = ContextVar("onebot_v11_current_instance", default=None)

# self_id <-> instance_key 双向映射，避免每条消息都查库。
#
# 两个方向**不对称**，这一点是有意的：`账号 -> 实例` 决定一条入站消息归谁处理，
# 一个账号被多个实例绑定时它必须与 `get_by_bound_account` 给出同一个答案，
# 因此只有入站解析（`resolve_instance_key`，它套用了那条判据并告警）可以写这个方向。
# 出站解析 `resolve_account_id` 是从实例出发的、按 `instance_key` 命中的单行查询，
# 它对「这个账号归谁」这个问题没有发言权，写进去只会把入站路由改判到另一个实例上。
_account_to_instance: Dict[str, str] = {}
_instance_to_account: Dict[str, str] = {}


def get_current_instance() -> Optional[InstanceContext]:
    """获取当前处理链路所属的实例"""
    return _current_instance.get()


def set_current_instance(instance_key: str, account_id: str) -> None:
    """标记当前处理链路所属的实例

    应在入站事件处理的最外层调用，之后派生的异步任务会自动继承。
    """
    _current_instance.set(InstanceContext(instance_key=instance_key, account_id=account_id))


def clear_instance_cache() -> None:
    """清空账号与实例的映射缓存

    实例的账号绑定关系变更后必须调用，否则消息会继续路由到旧实例。
    绑定关系变了，在线连接也要按新绑定复核：这里顺手排一个后台复核（登记表为空或没有运行中的
    事件循环时什么都不做），见 `core/access_token.py:schedule_live_connection_revalidation`。
    """
    _account_to_instance.clear()
    _instance_to_account.clear()
    # 延迟导入：`access_token` 模块级不导入本模块，但本模块被 `container_manager` 与
    # `instance_manager` 在模块级导入，反过来在模块级导入 `access_token` 会把那条链拖成环
    from .access_token import schedule_live_connection_revalidation

    schedule_live_connection_revalidation()


def lookup_cached_account_id(instance_key: str) -> Optional[str]:
    """从缓存同步查出实例绑定的账号号码，未命中返回 None"""
    return _instance_to_account.get(instance_key)


def lookup_cached_instance_key(self_id: str) -> Optional[str]:
    """从缓存同步查出账号号码所属的实例标识，未命中返回 None

    只读缓存、不做任何解析：出站钩子每发一条消息都会调用它，而 `resolve_instance_key`
    会在账号未登记时自动建实例，出站路径不该有这种副作用。
    """
    return _account_to_instance.get(self_id)


def _generate_instance_key(self_id: str) -> str:
    """为自动登记的协议端生成一个合法且稳定的实例标识

    以账号号码为种子只是为了让首次生成的标识便于人眼辨认；生成后即固定不变，
    后续换绑账号不会改变它。
    """
    candidate = f"ob{self_id}".lower()
    if is_valid_instance_key(candidate):
        return candidate
    return f"ob{abs(hash(self_id)) % 10**10}"


# 自动登记时最多试几个实例标识。
#
# 标识由账号派生，所以候选序列是**按账号分命名空间**的：要把它耗光，得有人手工把
# `ob{QQ}` 与它的全部退让标识都建成别的实例。给一个上界只是不让"找不到空位"退化成死循环。
_MAX_INSTANCE_KEY_CANDIDATES = 10


def _instance_key_candidates(self_id: str) -> Iterator[str]:
    """自动登记可用的实例标识候选，按人眼友好程度降序

    第一个是 `ob{QQ}`、第二个是 `ob{QQ}x`——与改造前逐字相同，因此常见路径上的标识不变；
    再往后才是 `ob{QQ}x2`、`ob{QQ}x3`……改造前只有前两个，而第二个**直接 `create`**：
    它一旦也被占着就是一条 `IntegrityError` 从入站链路第一步冲出去（实测复现），
    与 #31 原本那条 `MultipleObjectsReturned` 同样的爆炸半径。
    """
    base = _generate_instance_key(self_id)
    yield base
    for index in range(1, _MAX_INSTANCE_KEY_CANDIDATES):
        candidate = f"{base}x" if index == 1 else f"{base}x{index}"
        if is_valid_instance_key(candidate):
            yield candidate


async def _sweep_settings_before_register(instance_key: str) -> str:
    """登记之前清掉这个标识上残留的插件设置与配置覆盖，返回新行该带的 `metadata_json`

    这条路径在入站链路的第一步：清不掉只记 ERROR、照样登记，不能因为一份残留的设置文件
    就把协议端的消息整个挡掉。但新行带上补扫标记（`instance_settings.SWEEP_PENDING_KEY`）：
    认领 / 并入 / 划转之前必须补扫成功，否则残留的设置会跟着实例交出去。
    """
    from nekro_agent.services import instance_settings

    try:
        await instance_settings.sweep_before_create(ADAPTER_KEY, instance_key)
    except Exception as e:  # noqa: BLE001 - 入站链路不能因残留设置失败
        logger.error(
            f"自动登记实例 {instance_key} 之前清扫残留设置失败，照样登记、带补扫标记"
            f"（认领 / 并入 / 划转前补扫）: {type(e).__name__}: {e}",
        )
        return instance_settings.sweep_pending_metadata()
    return ""


async def _register_unclaimed_instance(self_id: str) -> DBAdapterInstance:
    """把一个没人认领的协议端登记成待认领实例，并返回它

    ## 两件在这里必须做对的事

    1. **绝不把入站消息交给一行软删墓碑。** 账号占用判据（`get_by_bound_account` /
       `find_account_binding_conflict`）刻意排除墓碑，而按标识取行的这一步不排除：
       一个叫 `ob{QQ}`、还记着同一个账号的墓碑会被原样取回来当成"就是它"，于是该账号的消息
       全部投给一个 `deleted` 实例——`is_instance_active` 把它们静默丢掉，而总后台列表里
       连这一行都看不见（实测复现）。跳过它、另起一行，管理员至少能在待认领面板里看见协议端。
    2. **标识被占用不能炸。** 见 `_instance_key_candidates`。

    并发下同一个账号的两条入站消息会算出同一串候选，因此 `IntegrityError` 之后回读那一行、
    与"本来就存在"走同一套判据即可——这也是改造前 `get_or_create` 提供的那半个保证。
    """
    for candidate in _instance_key_candidates(self_id):
        existing = await DBAdapterInstance.get_or_none(adapter_key=ADAPTER_KEY, instance_key=candidate)
        if existing is None:
            # `~Q(a, b)` 而不是 `.exclude(a=, b=)`：后者在 Tortoise 里生成的是 `NOT a AND NOT b`
            if (
                await DBAdapterInstance.filter(instance_key__iexact=candidate)
                .filter(~Q(adapter_key=ADAPTER_KEY, instance_key=candidate))
                .exists()
            ):
                # 标识已被别的适配器占用（不分大小写）：同名实例会在只按 `instance_key` 存取的数据上串到一起
                # （见 `AdapterInstanceService.create_instance`），退让到下一个候选。
                # 精确同一个（适配器, 标识）的那一行不算：那是并发的另一条入站消息刚建好的，要落到下面
                # `IntegrityError` 之后的回读上——算成占用的话会退到下一个候选、给同一个账号再建一行
                logger.warning(f"实例标识 {candidate} 已被其他适配器的实例占用，改用下一个候选标识登记账号 {self_id}")
                continue
            metadata_json = await _sweep_settings_before_register(candidate)
            try:
                created = await DBAdapterInstance.create(
                    adapter_key=ADAPTER_KEY,
                    instance_key=candidate,
                    display_name=f"未认领实例 {self_id}",
                    provider="onebot_v11",
                    provider_account_id=self_id,
                    status="unclaimed",
                    metadata_json=metadata_json,
                )
            except IntegrityError:
                # 并发的另一条入站消息刚用同一个标识建好了行，回读它并按下面同一套判据处理
                existing = await DBAdapterInstance.get_or_none(adapter_key=ADAPTER_KEY, instance_key=candidate)
                if existing is None:
                    continue
            else:
                logger.info(f"检测到未登记的 OneBot 协议端，已自动登记为待认领实例: {self_id} -> {candidate}")
                return created

        if existing.status.strip().lower() == TOMBSTONE_STATUS:
            logger.warning(f"实例标识 {candidate} 是一行已删除实例的墓碑，改用下一个候选标识登记账号 {self_id}")
            continue
        if existing.provider_account_id == self_id:
            return existing
        logger.warning(
            f"实例标识冲突，改用下一个候选标识: {candidate} 已绑定 "
            f"{existing.provider_account_id or '（未绑定账号）'}",
        )

    raise ValueError(f"账号 {self_id} 的实例标识候选已全部被占用，无法自动登记待认领实例")


async def _claim_builtin_napcat_login(self_id: str) -> Optional[DBAdapterInstance]:
    """陌生账号连上来时，尝试认定它属于某个刚扫码成功的内置 NapCat 实例

    推不出来（没有内置候选、容器答不上、Docker/WebUI 不可用、超预算）一律返回 None，
    调用方随即走原来的自动登记。**任何异常都不向上抛**：这是入站链路的第一步，
    一个附加的便利功能没有资格让整条 Bot 收不到消息。
    """
    try:
        # 延迟导入：`container_manager` 会拖进 aiodocker / httpx / console schemas，
        # 而 `services/console/instance_manager` 又在模块级导入本模块，模块级导入会成环。
        #
        # **这条 import 必须写在 try 之内。** 写在外面时，这条导入链上任何一个依赖在某台部署上
        # 导不进来（缺可选依赖、或本模块被卷进一次循环导入），抛出的 `ImportError` 就会顺着
        # `resolve_instance_key` 一路冲出入站链路——而 `matchers/message.py` 对这一步没有任何
        # 兜底，结果恰恰是本函数明文承诺不会发生的那件事：这个账号的消息一条都进不来。
        # 实测复现过：把该模块置成不可导入后，`resolve_instance_key` 直接抛 ModuleNotFoundError。
        from nekro_agent.services.console.container_manager import claim_account_from_builtin_login

        return await asyncio.wait_for(
            claim_account_from_builtin_login(ADAPTER_KEY, self_id),
            timeout=LOGIN_ATTRIBUTION_BUDGET_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"推导账号 {self_id} 的内置容器归属超时，按未认领协议端处理")
    except Exception as e:
        logger.warning(f"推导账号 {self_id} 的内置容器归属失败，按未认领协议端处理: {e}")
    return None


async def resolve_instance_key(self_id: str) -> str:
    """把协议端账号号码解析为实例标识

    未登记的协议端会先过一次「是不是本机某个内置 NapCat 刚登录成功」的归属推导
    （见模块文档），推得出来就把账号回写给那个实例；推不出来才自动登记为待认领实例
    ——直接拒绝会让用户在总后台里看不到任何线索，不知道该去认领谁。

    查询走 `DBAdapterInstance.get_by_bound_account`，它在一个账号被多个实例绑定时确定性地
    取最早那行并告警，而不是抛 `MultipleObjectsReturned`。这是整条入站链路的第一步，
    在这里抛异常等于该账号的消息全部处理不了；账号唯一性由创建/更新侧与启动期的存量修复负责，
    这里只负责在脏数据面前不把消息链路一起拖垮。**同一条规矩也管着自动登记那一支**：
    实例标识被占用、或占着它的是一行软删墓碑，都退让到下一个候选标识，
    而不是让一条 `IntegrityError` 或一行收不到消息的墓碑冲出这条链路（见
    `_register_unclaimed_instance`）。

    Args:
        self_id: 协议端上报的账号号码

    Returns:
        该账号所属的实例标识

    Raises:
        ValueError: 协议端没有上报账号号码（空号码会匹配到全部**未绑定账号**的实例，
            把消息静默投给一个毫不相干的实例）；或该账号派生的实例标识候选已全部被占用。
    """
    if not self_id:
        raise ValueError("协议端未上报账号号码，无法解析实例归属")

    cached = _account_to_instance.get(self_id)
    if cached:
        return cached

    instance = await DBAdapterInstance.get_by_bound_account(ADAPTER_KEY, self_id)
    if instance is None:
        instance = await _claim_builtin_napcat_login(self_id)
    if instance is None:
        instance = await _register_unclaimed_instance(self_id)

    _account_to_instance[self_id] = instance.instance_key
    _instance_to_account[instance.instance_key] = self_id
    return instance.instance_key


async def resolve_account_id(instance_key: str) -> Optional[str]:
    """把实例标识解析回绑定的协议端账号号码

    出站时需要它来定位具体连接。

    **只写 `实例 -> 账号` 这一个方向的缓存。** 顺手把反向也写上曾经是这里的做法，代价是：
    重复绑定尚未被收拾干净时（创建/改绑与协议端自行连入并发留下的窗口），任何一次出站
    ——主动推送、定时任务、掉线巡检——都会把 `账号 -> 实例` 改写成本次出站的那个实例，
    此后该账号的入站消息全部投给它，而且一条告警都没有。入站方向的判据只有
    `resolve_instance_key` 有，见上面 `_account_to_instance` 的注释。
    """
    cached = _instance_to_account.get(instance_key)
    if cached:
        return cached
    instance = await DBAdapterInstance.get_or_none(adapter_key=ADAPTER_KEY, instance_key=instance_key)
    if instance is None or not instance.provider_account_id:
        return None
    _instance_to_account[instance_key] = instance.provider_account_id
    return instance.provider_account_id
