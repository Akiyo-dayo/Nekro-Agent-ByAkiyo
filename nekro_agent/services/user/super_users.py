"""聊天命令里的超级用户：名单匹配 ∧ 来源可信

## 为什么不能只比用户 ID

超级用户能执行 `sh` / `exec`、跨频道跨租户批量操作，冒充成它就等于拿到宿主。而「这条消息的发送者是谁」
只在部分来源上是平台认证过的身份：

- 发送者 ID 由调用方或中继自己填的适配器（SSE 的 `from_id`、邮件的 `From`、弹幕 / MC 中继、wechatpad 的 webhook），
  谁都能填成名单里的号码；
- OneBot 的 `user_id` 由协议端上报，NA 不校验。协议端是谁在跑，决定了这个值可不可信：租户自己运行的远程协议端、
  匿名连入后被自动登记的待认领实例，都能把它填成名单里的号码；
- 不同平台的 ID 会撞号：某个 Telegram 数字 ID 恰好等于名单里的 QQ 号。

所以超级用户 = **名单匹配**（号码属于哪个平台）∧ **来源可信**（这条消息的身份是不是平台方能担保的）。
先做名单匹配：绝大多数消息在这一步就返回假，来源判定（要查库、读记录）只在匹配之后才做。

## 名单格式（向后兼容，仍是字符串列表）

- 纯号码 = OneBot V11 的 QQ 号，与配置项说明一致，只在 `onebot_v11` 上匹配；
- `适配器标识:用户 ID` = 只在该适配器上匹配。只认已登记的适配器标识作前缀，ID 本身含冒号也不会被误拆。

## OneBot：可信要求这条连接背后的令牌只有平台方持有

入站事件按握手请求头里的账号（`bot.self_id`）归到实例，而握手按「这个账号所属实例」的令牌校验
（`adapters/onebot_v11/core/access_token.py`）。所以「实例可信」不够，还得问这条事件所在的连接是凭什么握手的：

- 实例这一侧：存在、未软删、已认领、不属于租户，应当出示的令牌非空，且这枚令牌**从没对租户可见过**
  （`services/user/token_exposure.py`：实例收回、租户实例改令牌或被删之后，租户手里那枚都还在）；
- 连接这一侧：握手时账号就属于这个实例，凭的就是这个实例**现在**应当出示的那枚令牌。

全部判据收在 `judge_onebot_source` 一个纯函数里。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, FrozenSet, Iterable, List, Optional, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.user.token_exposure import (
    ensure_exposure_record,
    load_exposed_fingerprints,
    record_exposed_tokens,
    token_fingerprint,
)

if TYPE_CHECKING:
    from nekro_agent.adapters.onebot_v11.core.access_token import HandshakeRecord

logger = get_sub_logger("user.super_users")

ONEBOT_ADAPTER_KEY = "onebot_v11"

# 发送者 ID 由调用方或中继填写的适配器：名单里写了带前缀的条目也不算
UNTRUSTED_SENDER_ADAPTERS: FrozenSet[str] = frozenset({"sse", "email", "bilibili_live", "minecraft", "wechatpad"})

_UNCLAIMED_STATUS = "unclaimed"
_TOMBSTONE_STATUS = "deleted"

# 握手凭的是这个实例名下的令牌（`TokenSource` 的取值）：
# - `instance` / `deployment`：账号握手时就绑在这个实例上；
# - `awaiting_account`：账号握手时还没绑定，出示的令牌只匹配上一个待绑实例（匹配多个时握手记录不带实例），
#   之后账号绑到的正是这个实例——证明的同样是这个实例自己那枚令牌，平台方新建的内置 NapCat 扫码登录后不必重连。
#   租户拿自家待绑实例的令牌抢平台方的账号时，握手记录里是租户那个实例，对不上；
# 查库失败退回的部署级令牌（`deployment_fallback`）不在其中：那时连「这个账号属于哪个实例」都没判出来
_TRUSTED_HANDSHAKE_SOURCES: FrozenSet[str] = frozenset({"instance", "deployment", "awaiting_account"})


def _registered_adapter_keys() -> FrozenSet[str]:
    try:
        from nekro_agent.adapters import ADAPTER_REGISTRY

        return frozenset(ADAPTER_REGISTRY)
    except Exception:  # noqa: BLE001 - 拿不到注册表时退回只认 OneBot 的纯号码写法
        logger.warning("读取适配器注册表失败，超级用户名单里带前缀的条目本次不生效", exc_info=True)
        return frozenset()


def parse_super_user_entry(entry: str, adapter_keys: Iterable[str]) -> Tuple[str, str]:
    """把一条名单解析成 `(适配器标识, 用户 ID)`；纯号码归 OneBot

    前缀不分大小写、两侧空白不计（适配器标识本身都是小写），手填的 `Telegram : 555` 与 `telegram:555` 等价；
    用户 ID 只去两侧空白，大小写原样保留。
    """
    text = str(entry).strip()
    prefix, sep, rest = text.partition(":")
    prefix = prefix.strip().lower()
    if sep and prefix in set(adapter_keys) and rest.strip():
        return prefix, rest.strip()
    return ONEBOT_ADAPTER_KEY, text


def matches_super_users(adapter_key: str, user_id: str, entries: Optional[Iterable[str]] = None) -> bool:
    """只做名单匹配，不判来源（展示口用；判定权限一律用 `is_super_user`）"""
    if entries is None:
        from nekro_agent.core.config import config

        entries = config.SUPER_USERS
    entries = [str(item) for item in entries if str(item).strip()]
    user_id = str(user_id).strip()
    if not entries or not user_id:
        return False
    adapter_keys = _registered_adapter_keys()
    return any(parse_super_user_entry(item, adapter_keys) == (adapter_key, user_id) for item in entries)


@dataclass(frozen=True)
class OneBotSourceFacts:
    """判定一个 OneBot 来源要用到的、关于实例的事实"""

    instance_key: str
    tenant_id: Optional[int]
    status: str
    effective_token: str
    """这个实例的协议端现在应当出示的令牌：自己的令牌，没有就是部署级令牌（与握手校验同一个值）"""


def expected_onebot_fingerprint(facts: Optional[OneBotSourceFacts], exposed: Optional[FrozenSet[str]]) -> str:
    """实例这一侧的判据都满足时，返回它现在应当出示的令牌指纹；任何一条不满足返回空串

    - 实例存在、未软删、已认领、不属于任何租户；
    - 应当出示的令牌非空（空 = 不校验，谁都能带着这个账号连进来）；
    - 这枚令牌没有对租户可见过；`exposed` 为 None 表示记录读不出来，按全部可见处理。
    """
    if facts is None or facts.tenant_id is not None:
        return ""
    if facts.status.strip().lower() in (_UNCLAIMED_STATUS, _TOMBSTONE_STATUS):
        return ""
    if exposed is None:
        return ""
    fingerprint = token_fingerprint(facts.effective_token)
    if not fingerprint or fingerprint in exposed:
        return ""
    return fingerprint


def judge_onebot_source(
    facts: Optional[OneBotSourceFacts],
    exposed: Optional[FrozenSet[str]],
    handshake: Optional["HandshakeRecord"],
) -> bool:
    """OneBot 来源可不可信的全部判据

    实例这一侧见 `expected_onebot_fingerprint`。连接这一侧要求这条事件所在的连接：

    - 经过了握手中间件：没有握手记录一律不可信，任何没走中间件的路径都落到这一格；
    - 凭这个实例名下的令牌握手（`_TRUSTED_HANDSHAKE_SOURCES`）；
    - 握手时解析到的就是这个实例（账号已绑定时是所属实例，还没绑定时是唯一匹配上的待绑实例）：
      握手之后才认领 / 并入 / 改绑到别的实例的旧连接不算，协议端重连一次即恢复；
    - 握手出示的就是这个实例**现在**应当出示的令牌：换过令牌的旧连接不算。
    """
    expected = expected_onebot_fingerprint(facts, exposed)
    if not expected or facts is None or handshake is None:
        return False
    return (
        handshake.source in _TRUSTED_HANDSHAKE_SOURCES
        and handshake.instance_key == facts.instance_key
        and handshake.token_fingerprint == expected
    )


async def _onebot_source_facts(instance_key: str) -> Optional[OneBotSourceFacts]:
    """`instance_key` 为空不会出现在 OneBot 入站链路上（`resolve_instance_key` 对空号码直接抛错），按不存在处理"""
    if not instance_key:
        return None

    from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
    from nekro_agent.services.console.container_manager import resolve_access_token

    row = (
        await DBAdapterInstance.filter(adapter_key=ONEBOT_ADAPTER_KEY, instance_key=instance_key)
        .exclude(status=TOMBSTONE_STATUS)
        .order_by("id")
        .first()
    )
    if row is None:
        return None
    return OneBotSourceFacts(
        instance_key=row.instance_key,
        tenant_id=row.tenant_id,
        status=row.status,
        effective_token=resolve_access_token(row),
    )


async def _deployment_token_reaches_tenants() -> bool:
    """是否存在「租户名下、没有自己令牌」的 OneBot 实例：它只能靠部署级令牌连入，部署级令牌就在租户手里"""
    from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
    from nekro_agent.services.console.container_manager import read_container_metadata

    rows = await DBAdapterInstance.filter(adapter_key=ONEBOT_ADAPTER_KEY, tenant_id__not_isnull=True).exclude(
        status=TOMBSTONE_STATUS,
    )
    return any(not read_container_metadata(row).access_token.strip() for row in rows)


async def exposed_fingerprints_with_deployment_check(*, deployment_in_use: bool) -> Optional[FrozenSet[str]]:
    """读出曾对租户可见的令牌指纹（记录不可用时先重建）

    `deployment_in_use` 为真（这次判定靠的是部署级令牌）时，先补记「此刻有租户实例也靠它认证」。
    超级用户的来源判定与未认领实例的入站闸门（`services/unclaimed_dispatch.py`）共用这一步。
    """
    from nekro_agent.core.os_env import resolve_deployment_onebot_access_token

    await ensure_exposure_record()
    deployment = resolve_deployment_onebot_access_token()
    if deployment_in_use and deployment and await _deployment_token_reaches_tenants():
        record_exposed_tokens([deployment], reason="有租户实例正靠部署级令牌认证")
    return load_exposed_fingerprints()


async def _exposed_fingerprints(facts: Optional[OneBotSourceFacts]) -> Optional[FrozenSet[str]]:
    """这个实例靠部署级令牌时带上补记那一步，见 `exposed_fingerprints_with_deployment_check`"""
    from nekro_agent.core.os_env import resolve_deployment_onebot_access_token

    deployment = resolve_deployment_onebot_access_token()
    return await exposed_fingerprints_with_deployment_check(
        deployment_in_use=facts is not None and bool(deployment) and facts.effective_token == deployment,
    )


async def is_trusted_super_user_source(adapter_key: str, instance_key: str) -> bool:
    """这条消息上报的发送者身份能不能用来认超级用户；只能在入站事件自己的处理任务里调用，任何异常按不可信处理

    OneBot 要读当前事件所在连接的握手记录：离开事件处理链路（比如管理接口）调用，一律判不可信。
    **也不要在事件链路里懒创建的长寿任务里调用**（后台 worker、debounce、Agent 任务）：它们复制的是
    创建那一刻的上下文，之后别的连接的事件交给它处理时，读到的仍是最初那条连接的握手记录。
    """
    if adapter_key in UNTRUSTED_SENDER_ADAPTERS:
        return False
    if adapter_key != ONEBOT_ADAPTER_KEY:
        return True
    try:
        from nekro_agent.adapters.onebot_v11.core.access_token import current_handshake

        facts = await _onebot_source_facts(instance_key)
        return judge_onebot_source(facts, await _exposed_fingerprints(facts), current_handshake())
    except Exception:  # noqa: BLE001 - 判不出来就不给，这一档是宿主权限
        logger.warning(
            f"判定超级用户来源失败，按不可信处理: adapter={adapter_key}, instance={instance_key}",
            exc_info=True,
        )
        return False


async def is_super_user_source_trusted_for_display(adapter_key: str, instance_key: str) -> bool:
    """只用于展示（频道用户列表）：没有事件就没有连接，只判实例这一侧；不能用来放行任何东西"""
    if adapter_key in UNTRUSTED_SENDER_ADAPTERS:
        return False
    if adapter_key != ONEBOT_ADAPTER_KEY:
        return True
    try:
        facts = await _onebot_source_facts(instance_key)
        await ensure_exposure_record()
        return bool(expected_onebot_fingerprint(facts, load_exposed_fingerprints()))
    except Exception:  # noqa: BLE001
        logger.warning(f"判定超级用户来源失败，按不可信处理: adapter={adapter_key}, instance={instance_key}", exc_info=True)
        return False


async def is_super_user(adapter_key: str, instance_key: str, user_id: str) -> bool:
    """聊天命令里的超级用户判定：名单匹配 ∧ 来源可信"""
    if not matches_super_users(adapter_key, user_id):
        return False
    return await is_trusted_super_user_source(adapter_key, instance_key)


def _empty_deployment_token_warning(platform_on_deployment_token: List[str]) -> str:
    """部署级令牌为空的那一条：未认领实例不分发与超级用户不生效合成一条，启动日志里不重复"""
    text = "部署级 ONEBOT_ACCESS_TOKEN 为空：新连进来的 OneBot 协议端会被登记为待认领实例，消息暂不处理，要在总后台认领"
    if platform_on_deployment_token:
        text += (
            f"；靠它认证的 OneBot 实例里超级用户命令也不生效（{', '.join(platform_on_deployment_token)}），"
            "给这些实例配自己的令牌"
        )
    return text + "。建议设置 ONEBOT_ACCESS_TOKEN"


async def collect_super_user_warnings() -> List[str]:
    """启动时的告警：部署级令牌为空，以及哪些部署形态会让名单里的人在哪些地方失去超级用户

    部署级令牌为空那一条与名单无关（未认领实例的消息不处理，`services/unclaimed_dispatch.py`），名单为空也报；
    其余各条名单为空时不报：没有人会因此失去什么。要在启动扫描（`scan_tenant_token_exposure`）之后调用，
    那样「曾对租户可见」的记录已经是最新的。
    """
    from nekro_agent.core.config import config
    from nekro_agent.core.os_env import resolve_deployment_onebot_access_token

    entries = [str(item).strip() for item in config.SUPER_USERS if str(item).strip()]
    deployment = resolve_deployment_onebot_access_token()
    if not entries:
        return [] if deployment else [_empty_deployment_token_warning([])]

    from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
    from nekro_agent.services.console.container_manager import read_container_metadata

    warnings: List[str] = []
    exposed = load_exposed_fingerprints()
    rows = await DBAdapterInstance.filter(adapter_key=ONEBOT_ADAPTER_KEY).exclude(status=TOMBSTONE_STATUS)
    platform_rows = [row for row in rows if row.tenant_id is None and row.status.strip().lower() != _UNCLAIMED_STATUS]
    platform_on_deployment_token = [
        row.instance_key for row in platform_rows if not read_container_metadata(row).access_token.strip()
    ]
    platform_own_token_exposed = [
        row.instance_key
        for row in platform_rows
        if read_container_metadata(row).access_token.strip()
        and (exposed is None or token_fingerprint(read_container_metadata(row).access_token) in exposed)
    ]
    unclaimed = [row.instance_key for row in rows if row.status.strip().lower() == _UNCLAIMED_STATUS]

    if not deployment:
        warnings.append(_empty_deployment_token_warning(platform_on_deployment_token))
    elif platform_on_deployment_token and (exposed is None or token_fingerprint(deployment) in exposed):
        warnings.append(
            "部署级 ONEBOT_ACCESS_TOKEN 曾对租户可见：靠它认证的平台方 OneBot 实例里，超级用户命令不生效"
            f"（{', '.join(platform_on_deployment_token)}）。更换部署级令牌并重启后恢复；"
            "仍有租户实例靠部署级令牌认证时，换了也会再次被记下，先给那些租户实例配自己的令牌",
        )
    if platform_own_token_exposed:
        warnings.append(
            f"这些平台方 OneBot 实例的令牌曾对租户可见（{', '.join(platform_own_token_exposed)}）：在那里超级用户命令不生效，"
            "换了令牌才恢复（实例令牌目前没有更换入口）",
        )
    if unclaimed:
        warnings.append(
            f"有待认领的 OneBot 实例（{', '.join(unclaimed)}）：在那里超级用户命令不生效，认领后协议端重连一次即恢复；"
            "认领之前，消息只在协议端凭未曝光的部署级令牌连上来时才处理",
        )

    adapter_keys = _registered_adapter_keys()
    plain_entries = [item for item in entries if parse_super_user_entry(item, adapter_keys)[0] == ONEBOT_ADAPTER_KEY]
    # 归到 OneBot 却不是纯数字的条目永远匹配不上（OneBot V11 的 user_id 是整数），多半是前缀写错了
    unmatchable = [item for item in plain_entries if not item.isdigit()]
    if unmatchable:
        warnings.append(
            f"超级用户名单里这些条目既不是 QQ 号、前缀也不是已登记的适配器标识，不会匹配任何人（{', '.join(unmatchable)}）。"
            "其他平台请写成「适配器标识:用户ID」",
        )
    other_enabled = _enabled_non_onebot_adapters()
    if plain_entries and other_enabled:
        warnings.append(
            f"超级用户名单里的纯号码只当 OneBot 的 QQ 号；已启用的其他适配器（{', '.join(other_enabled)}）"
            "上的超级用户请写成「适配器标识:用户ID」，例如 telegram:123456",
        )
    return warnings


def _enabled_non_onebot_adapters() -> List[str]:
    try:
        from nekro_agent.adapters import loaded_adapters

        return sorted(key for key in loaded_adapters if key != ONEBOT_ADAPTER_KEY)
    except Exception:  # noqa: BLE001 - 只用于告警文案
        return []


async def log_super_user_warnings() -> None:
    """启动时调用一次；失败只记日志"""
    try:
        for message in await collect_super_user_warnings():
            logger.warning(message)
    except Exception:  # noqa: BLE001
        logger.warning("检查超级用户来源配置失败", exc_info=True)
