"""隔离键的**唯一**解析器：`(记忆域, 人, 人设)`

## 这个模块的全部意义

好感与记忆的隔离键从 `(adapter_key, instance_key, platform_userid)` 变成
`(记忆域, 人, 人设)`。三维各自都有回退规则、各自都能被运营改，而消费点有十几处
（提示词注入、后台评估、榜单插件、后台路由、记忆检索的四个出口……）。

**所有消费方只准调本模块，不要各自拼键。** 本项目已经在"同一个判据的第二处实现"上
反复栽跟头：`extract_query_terms` 的第二个调用点（§2.7）、`relay_listen_port` 这对兄弟函数
（§2.9 ②）、`instance_key` 过滤的四个出口里只测了一个（#45）。隔离键是**授权边界**，
在这上面出现第二份实现的后果不是"少几行"，而是一个租户读到另一个租户的私密内容。

## 三维的定义

### 记忆域

载体是实例组（`models/db_instance_group.py`）。**没有绑定行 = 自成一域 = 今天的行为。**
好感与记忆共用同一个域；绑定行上另有一个可空的 `affection_group_id` 覆盖字段，
NULL 表示跟随记忆域（**前端不暴露**，留给"将来好感互通但记忆不互通"）。

域有两种编码，**只由 :class:`ScopeDomain` 产生**：

- **三列键**（好感表）：`(adapter_key, instance_key, domain_group_id)`，
  见 `models/db_affection.py` 的模块文档——这样存量行零回填就能落位。
- **字符串**（别名表、以及一切要把域当成一个值传递/展示的地方）：
  `g:<组ID>` / `i:<适配器>:<实例>`。

### 人

`person_uid` 恒等于**该人主账号的平台用户 ID**；只有被合并过的账号才在
`identity_alias` 里有一行 `(域, 账号) → 人`。因此从没合并过的人零额外数据，
**存量好感行里的 `platform_userid` 恰好就是他的 `person_uid`**。
别名解析**只做一跳**（不成链，理由见 `models/db_identity.py`）。

### 人设

取频道**当前生效**的人设行 ID（`DBChatChannel.get_effective_preset_id`，
与提示词用的是同一条回退链）。**无人设档**（回落到内置默认人设，库里没有那一行）
定死为 :data:`PRESET_NONE` = ``0``。编辑人设内容不算换人设——判据是 preset 行的 ID，
不是内容 hash。

## 缓存策略：**刻意不做缓存**

这条路径每条消息都要走，看起来正是该加缓存的地方。仍然不加，理由与
`DBAdapterInstance.is_instance_active` 的 docstring 逐字同源：

1. **换人设、改域、合并/拆分账号必须热生效。** 这三件事都发生在人手边——运营点完"换人设"
   会立刻在群里试一句，管理员合完账号会立刻让用户去验。任何 TTL 都会把"热生效"变成
   "TTL 之后生效"，而这种延迟在现场表现为"功能没生效"，运营会再点一次、再合一次，
   把一次操作变成几次。
2. **版本号失效（`config_resolver.bump_version()` 那套）在这里不成立。**
   它的版本号是**进程内**的整数；多进程部署下 A 进程的写入不会让 B 进程的缓存失效。
   四层配置能接受这个（改配置本来就有可见的生效入口），把它用在**授权边界**上不行——
   "撤销一次错误合并"在另一个进程里可能永远不生效。
3. **成本是可算的**：每轮对话两次小表点查（域一次、别名一次批量 `IN`），
   人设复用调用方已经取过的 `get_preset()` 结果（`preset_id=` 参数）。
   只有**确实命中了别名**时才会多一次查询（判断这些别名有没有指向另一个别名，
   见 :func:`_warn_on_alias_chains`）——没合并过的人一次都不会有。
   `instance_domain_binding` 只有"被显式放进组"的实例才有行，零配置时是空表；
   `identity_alias` 上是唯一索引点查。相比之下，好感度快照那一层**本来就有** TTL 缓存
   （`AFFECTION_CACHE_TTL_SECONDS`），只要它的键换成解析后的三元组，
   改域/合并之后旧快照会因为键不同而自然失效——**不要**把 TTL 补到本模块来。

## 零配置 = 今天的行为

没有实例组绑定、没有别名、频道没绑人设时：域 = 自成一域（三列键与今天的
`(adapter_key, instance_key)` 逐字相同）、人 = `platform_userid` 本身、人设 = 0。
于是好感表的键退化成今天的三元组，记忆的允许集合退化成
`memory.instance_scope.resolve_allowed_instance_keys` 的原样输出。
这条由 `tests/test_identity_scope_foundation.py::TestZeroConfigIsByteForByteToday` 钉死。
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Protocol, Sequence

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_instance_group import SELF_DOMAIN_GROUP_ID, DBInstanceDomainBinding
from nekro_agent.services.memory.instance_scope import (
    LEGACY_INSTANCE_KEY,
    resolve_allowed_instance_keys,
)

logger = get_sub_logger("identity_scope")

# 「无人设档」。频道没绑人设、实例没配默认人设、系统也没配默认人设时，
# 说话的是内置默认人设——它在库里没有行，因此没有 ID 可用，这里定死成 0。
# 0 不会与任何真实人设撞车：`presets.id` 是 SERIAL，从 1 开始。
PRESET_NONE = 0


class ChannelLike(Protocol):
    """解析隔离键需要频道提供的最小接口

    用 Protocol 而不是直接依赖 `DBChatChannel`：后者会把插件收集器、适配器注册表、
    配置解析器整条导入链拖进来，而本模块只需要三样东西。测试里的频道替身
    （`tests/test_affection_system.py::_StubChannel`）因此可以直接喂进来。
    """

    adapter_key: str
    instance_key: str

    async def get_effective_preset_id(self) -> int: ...


class ScopeDomain(NamedTuple):
    """一个域的身份

    两种编码的**唯一产生者**。不要在别处自己拼 `domain_id` 字符串，
    也不要自己写那三列的过滤条件。

    Attributes:
        group_id: 所属实例组 ID；`SELF_DOMAIN_GROUP_ID`（0）表示自成一域
        adapter_key: 自成一域时的适配器标识；属于实例组时恒为空串
        instance_key: 自成一域时的实例标识；属于实例组时恒为空串
    """

    group_id: int
    adapter_key: str
    instance_key: str

    @classmethod
    def of_instance(cls, adapter_key: str, instance_key: str) -> "ScopeDomain":
        """自成一域（默认形态）"""
        return cls(SELF_DOMAIN_GROUP_ID, adapter_key, instance_key)

    @classmethod
    def of_group(cls, group_id: int) -> "ScopeDomain":
        """属于某个实例组

        组 ID 非正是调用方的 bug（组 ID 是 SERIAL，从 1 开始），直接抛而不是悄悄退化成
        自成一域——后者会让一整个组的数据静默落到一个谁也读不到的位置。
        """
        if group_id <= SELF_DOMAIN_GROUP_ID:
            raise ValueError(f"实例组 ID 必须为正: {group_id}")
        return cls(group_id, "", "")

    @property
    def is_group(self) -> bool:
        """是否属于某个显式实例组（否则是自成一域）"""
        return self.group_id != SELF_DOMAIN_GROUP_ID

    @property
    def domain_id(self) -> str:
        """域的字符串编码

        `g:<组ID>` / `i:<适配器>:<实例>`。单实例适配器的实例段是空串（`i:minecraft:`），
        这与它在好感表里的取值一致，不是缺失。
        """
        if self.is_group:
            return f"g:{self.group_id}"
        return f"i:{self.adapter_key}:{self.instance_key}"

    def as_columns(self) -> Dict[str, object]:
        """域在好感表上的三列取值

        建行与查询共用这一份：`DBAffection.filter(**domain.as_columns(), ...)` 与
        `DBAffection.create(**domain.as_columns(), ...)` 必须落在同一组列上，
        否则写进去的行自己查不到。
        """
        return {
            "adapter_key": self.adapter_key,
            "instance_key": self.instance_key,
            "domain_group_id": self.group_id,
        }


class IdentityScope(NamedTuple):
    """一次解析的结果：隔离键的三维（外加好感域这一侧的两项）

    Attributes:
        domain: 记忆域，也是好感域的默认值
        affection_domain: 好感域；未配置覆盖时与 `domain` 逐字相同
        person_uid: 记忆域内的人标识
        affection_person_uid: 好感域内的人标识；两个域相同时与 `person_uid` 相同
        preset_id: 人设标识，`PRESET_NONE`（0）表示无人设档
        platform_userid: 解析时喂进来的原始账号，供流水表记"来源账号"用
    """

    domain: ScopeDomain
    affection_domain: ScopeDomain
    person_uid: str
    affection_person_uid: str
    preset_id: int
    platform_userid: str

    @property
    def domain_id(self) -> str:
        """记忆域的字符串编码"""
        return self.domain.domain_id

    @property
    def is_alias(self) -> bool:
        """本次解析是否命中了账号别名（即这个账号是某个人的副号）"""
        return self.affection_person_uid != self.platform_userid

    def affection_columns(self) -> Dict[str, object]:
        """好感表的完整隔离键列值（域三列 + 人 + 人设）

        好感的读写只准用这一份。写侧建行、读侧过滤、缓存做键都取它，
        少一列就是串号，多一列就是永远查不到自己写的行。
        """
        return {
            **self.affection_domain.as_columns(),
            "platform_userid": self.affection_person_uid,
            "preset_id": self.preset_id,
        }


async def resolve_memory_domain(adapter_key: str, instance_key: str) -> ScopeDomain:
    """解析某个实例的**记忆域**

    Args:
        adapter_key: 适配器标识
        instance_key: 实例标识；单实例适配器为空串（空串是合法实例标识，不是"缺失"）

    Returns:
        该实例所在的域。没有绑定行、或绑定行的 `memory_group_id` 为 NULL 时是自成一域。

    绑定行指向一个**已被删除**的组时仍然按那个组 ID 成立，不回落成自成一域：
    回落会让整组的好感与记忆当场"消失"，而这正是删组时最不该发生的事。
    清理绑定行是删组动作的一部分（后续几组负责）。

    热路径上**只查 `instance_domain_binding` 这一张表**，不查 `instance_group`——
    少一次查询，也让"组被删了但绑定还在"这种脏态有确定行为。
    """
    binding = await DBInstanceDomainBinding.get_binding(adapter_key, instance_key)
    return _memory_domain_of(adapter_key, instance_key, binding)


async def resolve_affection_domain(adapter_key: str, instance_key: str) -> ScopeDomain:
    """解析某个实例的**好感域**

    默认与记忆域相同；绑定行上的 `affection_group_id` 非空时改用它。
    该覆盖字段**前端不暴露**，存在的意义见 `models/db_instance_group.py` 的模块文档。
    """
    binding = await DBInstanceDomainBinding.get_binding(adapter_key, instance_key)
    return _affection_domain_of(adapter_key, instance_key, binding)


async def resolve_domain_instance_keys(adapter_key: str, instance_key: str) -> Optional[List[str]]:
    """把记忆域换算成「允许检索的 instance_key 集合」，供记忆检索侧过滤

    Args:
        adapter_key: 适配器标识
        instance_key: 当前实例标识，单实例适配器为空串

    Returns:
        允许命中的 `instance_key` 列表；`None` 表示不施加任何实例过滤。

    **返回值里没有适配器维，这是记忆表的形状决定的**：`mem_*` 四张表只有 `instance_key`
    这一列（外加 `workspace_id`），没有 `adapter_key`，所以过滤条件也只能表达到实例标识这一层。
    组成员本来是 `(adapter_key, instance_key)` 二元组，这里**刻意丢掉前一半**。
    代价是：同一个 `instance_key` 若同时存在于两个适配器下，把其中一个放进组会让另一个的记忆
    在同一工作区内一并可见。目前挡住这件事的是创建侧的**全局**重名校验
    （`console/instance_manager.py` 认领新实例时 `filter(instance_key__iexact=slug).exists()` 不带适配器），
    而不是这里——数据库上的唯一键是 `(adapter_key, instance_key)`，允许重名。
    要真正按适配器隔离，得先给 `mem_*` 加列并回填，不在本轮范围内。

    这是 `memory.instance_scope.resolve_allowed_instance_keys` 的**超集**，
    两者的关系刻意写成"包含"而不是"替换"：

    - 该函数是上一轮的载体（全局配置项 `MEMORY_INSTANCE_SHARING_GROUPS`）。
      直接废掉它，已经配了共享组的部署会在升级那一刻静默失去共享——
      而记忆失联在现场看起来就是"Bot 失忆了"，没有任何报错指向配置。
    - 因此这里的规则是：**先取旧的那份，再并上实例组带来的成员**。
      配置项从此不再是新配置的入口（新配置走实例组），但已有的值继续成立。

    零配置时逐字等于旧函数的输出（同一个对象语义，包含返回 `None`），
    这条由 `TestZeroConfigIsByteForByteToday` 钉死。
    """
    legacy = resolve_allowed_instance_keys(instance_key)
    domain = await resolve_memory_domain(adapter_key, instance_key)
    if not domain.is_group:
        return legacy

    allowed: List[str] = []
    for _member_adapter, member_instance in await DBInstanceDomainBinding.members_of_memory_group(domain.group_id):
        if member_instance not in allowed:
            allowed.append(member_instance)
    for key in legacy or []:
        if key not in allowed:
            allowed.append(key)
    if instance_key not in allowed:
        allowed.append(instance_key)
    # 升级前写入的记忆 `instance_key` 为空串，任何非空域都必须额外允许它，否则升级后旧记忆
    # 会被过滤掉、直接失忆。与旧函数同一条理由，见 `LEGACY_INSTANCE_KEY`。
    #
    # 这一步**目前是被上面两步蕴含的**（旧函数对非空实例的输出必定以空串结尾，空实例则由
    # `instance_key not in allowed` 那步补上），因此变异掉它不会有用例变红——这是一个已核实的
    # 等价变异，不是覆盖缺口。留着是因为它守的是**后置条件**而不是某条分支：旧函数哪天不再
    # 自带空串兜底，这里就立刻变成唯一挡住"升级即失忆"的那一行。
    if LEGACY_INSTANCE_KEY not in allowed:
        allowed.append(LEGACY_INSTANCE_KEY)
    return allowed


async def resolve_domain_members(adapter_key: str, instance_key: str) -> tuple[tuple[str, str], ...]:
    """把记忆域换算成「域成员对」列表：``(adapter_key, instance_key)`` 的去重排序元组。

    为什么需要它：记忆行上的可见范围要按「域」而不是按单个实例过滤，而域成员是在检索
    那一刻现算的（实例加入 / 移出组必须立刻生效，不缓存）。返回值至少含当前实例自己，
    **永远不为空**。

    成员来源有两层，取并集：

    1. 实例组：``DBInstanceDomainBinding.members_of_memory_group`` 的成员；自成一域时
       只有自己；
    2. 旧配置 ``MEMORY_INSTANCE_SHARING_GROUPS`` 里与本实例同组的实例名。该配置只有
       实例名、没有适配器，因此统一配上**当前频道的适配器**：创建侧对实例 slug 做了
       全局查重，同一个 slug 不会同时出现在两个适配器下。

    **不放行空串通配。** 空串在单实例适配器上是一个合法的实例标识（此时它就是「自己」），
    但它绝不能变成「匹配所有实例」：旧配置里的空白项一律跳过，域成员对按二元组精确匹配。
    """
    domain = await resolve_memory_domain(adapter_key, instance_key)
    members: list[tuple[str, str]] = []
    if domain.is_group:
        members.extend(await DBInstanceDomainBinding.members_of_memory_group(domain.group_id))
    members.append((adapter_key, instance_key))

    normalized_self = instance_key.strip()
    for group_members in config.MEMORY_INSTANCE_SHARING_GROUPS.values():
        normalized_members = [member.strip() for member in group_members]
        if not normalized_self or normalized_self not in normalized_members:
            continue
        for member in normalized_members:
            if member:
                members.append((adapter_key, member))

    # 去重 + 排序：条件树的渲染结果必须与集合的迭代顺序无关。
    return tuple(sorted(set(members)))


# 别名表一条 `IN (...)` 最多放多少个账号（与记忆后台服务的批量上限相同）
_ALIAS_IN_CHUNK = 1000


async def resolve_person_uid(domain: ScopeDomain, platform_userid: str) -> str:
    """把一个平台账号解析成它在该域里的「人」

    没有别名行时**就是账号自己**（零默认数据，见 `models/db_identity.py`）。
    """
    return (await resolve_person_uids(domain, [platform_userid]))[platform_userid]


async def resolve_person_uids(domain: ScopeDomain, platform_userids: Sequence[str]) -> Dict[str, str]:
    """批量解析「人」

    一轮对话要看好几个人，逐个点查会把查询数乘上人数，因此合并成一次 `IN`。
    入参里的每一个账号在返回值里都有一项。

    别名**只跟一跳**：`person_uid` 指向的账号自己又是别名时，本函数打 WARNING 并按一跳的
    结果返回。静默追链会让一次错误合并的影响范围随时间悄悄扩大，而告警能让它被发现；
    维护"不成链"这条不变式是合并服务的职责。

    后台的分组视图一次会交进来几千个账号：去重保持入参顺序、线性时间，`IN` 按
    `_ALIAS_IN_CHUNK` 分块（每块一条查询）。
    """
    unique_ids: List[str] = list(dict.fromkeys(platform_userids))
    if not unique_ids:
        return {}

    alias_of: Dict[str, str] = {}
    for start in range(0, len(unique_ids), _ALIAS_IN_CHUNK):
        rows = await DBIdentityAlias.filter(
            domain_id=domain.domain_id,
            platform_userid__in=unique_ids[start : start + _ALIAS_IN_CHUNK],
        ).order_by("id")
        for row in rows:
            # 唯一约束保证一个账号只有一行；脏行下取最早那行，与本项目其他读侧取舍一致。
            alias_of.setdefault(row.platform_userid, row.person_uid)

    resolved = {platform_userid: alias_of.get(platform_userid, platform_userid) for platform_userid in unique_ids}
    await _warn_on_alias_chains(domain, alias_of)
    return resolved


async def _warn_on_alias_chains(domain: ScopeDomain, alias_of: Dict[str, str]) -> None:
    """本次命中的别名如果指向了另一个别名，打 WARNING（**不追链**）

    `alias_of` 只包含本批查过的那些账号，因此**不能**拿它自己去判断链——那样只有当
    别名与它的目标恰好同批时才发现得了，而热路径上一次只解析一个人，等于永远不告警。
    要判链就得再查一次"这些目标自己是不是别名"，所以这一步只在**确实命中了别名**时才发生：
    没合并过的部署（也就是绝大多数轮次）`alias_of` 是空的，一次额外查询都不会有。

    追链是另一回事，仍然不做：静默追链会让一次错误合并的影响范围随时间悄悄扩大。
    这里只负责让破了的不变式**可见**，修复它是合并服务的职责。
    """
    if not alias_of:
        return
    targets = set(alias_of.values())
    # 同批里已经查到的那些目标直接复用，剩下的才补一次 `IN`
    next_hop: Dict[str, str] = {target: alias_of[target] for target in targets if target in alias_of}
    unknown = sorted(targets - alias_of.keys())
    for start in range(0, len(unknown), _ALIAS_IN_CHUNK):
        rows = await DBIdentityAlias.filter(
            domain_id=domain.domain_id,
            platform_userid__in=unknown[start : start + _ALIAS_IN_CHUNK],
        ).values_list("platform_userid", "person_uid")
        for platform_userid, person_uid in rows:
            next_hop.setdefault(str(platform_userid), str(person_uid))
    # 指向自己的退化行不是链，别把它报成链——那会让真正的链淹没在噪声里
    chained = sorted(target for target, hop in next_hop.items() if hop != target)
    if chained:
        logger.warning(
            f"账号别名出现链式指向，本次只解析一跳: domain={domain.domain_id}, targets={chained}；"
            "合并服务应当把指向被合并方的别名一并改写",
        )


async def resolve_preset_id(channel: ChannelLike) -> int:
    """解析频道当前生效的人设标识；无人设档时返回 :data:`PRESET_NONE`

    薄封装，判据完全在 `DBChatChannel.get_effective_preset_id` 里（回退链只有一份）。
    """
    preset_id = await channel.get_effective_preset_id()
    return preset_id if preset_id > PRESET_NONE else PRESET_NONE


async def resolve_identity_scope(
    channel: ChannelLike,
    platform_userid: str,
    *,
    preset_id: Optional[int] = None,
) -> IdentityScope:
    """解析单个用户在当前频道下的隔离键 `(域, 人, 人设)`

    Args:
        channel: 当前频道。`adapter_key` / `instance_key` 是域的权威来源，
            **不要解析 `chat_key` 字符串**（实例段的规则是适配器私有的）。
        platform_userid: 发消息的那个平台账号
        preset_id: 已经取好的人设标识；`None` 时本函数自己去解析。
            热路径上调用方通常在别处已经调过 `get_preset()`，传进来可省掉最多三次点查。

    Returns:
        `IdentityScope`。好感的读写用 :meth:`IdentityScope.affection_columns`，
        记忆检索的允许集合用 :func:`resolve_domain_instance_keys`。
    """
    return (await resolve_identity_scopes(channel, [platform_userid], preset_id=preset_id))[platform_userid]


async def resolve_identity_scopes(
    channel: ChannelLike,
    platform_userids: Sequence[str],
    *,
    preset_id: Optional[int] = None,
) -> Dict[str, IdentityScope]:
    """批量解析隔离键，入参里的每个账号在返回值里都有一项

    整批共用同一次域解析与同一次人设解析（它们与用户无关），别名解析合并成一次 `IN`。
    好感度提示词注入、后台评估、榜单插件都是"一次看好几个人"，走这个入口。
    """
    adapter_key = channel.adapter_key
    instance_key = channel.instance_key
    binding = await DBInstanceDomainBinding.get_binding(adapter_key, instance_key)
    # 判据一律走 `_memory_domain_of` / `_affection_domain_of`：在这里内联一份"等价"的写法，
    # 就是本模块开头点名要避免的那个形态（同一个判据的第二处实现）。
    memory_domain = _memory_domain_of(adapter_key, instance_key, binding)
    affection_domain = _affection_domain_of(adapter_key, instance_key, binding)
    resolved_preset_id = preset_id if preset_id is not None else await resolve_preset_id(channel)
    resolved_preset_id = resolved_preset_id if resolved_preset_id > PRESET_NONE else PRESET_NONE

    persons = await resolve_person_uids(memory_domain, platform_userids)
    # 两个域相同时（未配置好感域覆盖，也就是产品当前暴露的全部形态）复用同一次查询；
    # 不同则必须在**各自的域里**再解析一次——别名是按域隔离的，跨域套用就是串域。
    affection_persons = persons if affection_domain == memory_domain else await resolve_person_uids(
        affection_domain,
        platform_userids,
    )

    return {
        platform_userid: IdentityScope(
            domain=memory_domain,
            affection_domain=affection_domain,
            person_uid=persons[platform_userid],
            affection_person_uid=affection_persons[platform_userid],
            preset_id=resolved_preset_id,
            platform_userid=platform_userid,
        )
        for platform_userid in persons
    }


async def resolve_identity_scope_by_chat_key(chat_key: str, platform_userid: str) -> Optional[IdentityScope]:
    """按会话标识解析隔离键；频道行不存在时返回 `None`

    只给手上确实只有 `chat_key` 的调用方（插件、命令）用，比传频道对象多一次查询。
    **定位不到频道时返回 `None` 而不是退化成某个默认域**：这里是授权边界，
    "算不出来就当作全局"正是要堵的那个泄露形态（见 `memory_tools` 的同款判据）。
    """
    from nekro_agent.models.db_chat_channel import DBChatChannel

    channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
    if channel is None:
        logger.debug(f"隔离键解析失败：频道不存在: chat_key={chat_key}")
        return None
    return await resolve_identity_scope(channel, platform_userid)


def _group_column(binding: DBInstanceDomainBinding, value: Optional[int], column: str) -> Optional[int]:
    """把绑定行上的一个组列读成「组 ID 或没有组」

    `NULL` 与**非正整数**都读成"没有组"。后者理论上写不进去（合法组 ID 是从 1 开始的 SERIAL），
    但它是**数据**而不是调用参数：`ScopeDomain.of_group` 对非正值抛异常是为了挡住调用方的 bug，
    照搬到读侧就变成"库里一个 0 让这个实例的每一条消息都炸在解析器里"，
    整个 Bot 当场失声。回落到自成一域是安全的那一侧——它等于今天的行为——并留下告警。
    """
    if value is None:
        return None
    if value <= SELF_DOMAIN_GROUP_ID:
        logger.warning(
            f"实例域绑定上的 {column} 不是合法组 ID，按自成一域处理: "
            f"adapter_key={binding.adapter_key}, instance_key={binding.instance_key}, value={value}",
        )
        return None
    return value


def _memory_domain_of(
    adapter_key: str,
    instance_key: str,
    binding: Optional[DBInstanceDomainBinding],
) -> ScopeDomain:
    """记忆域的判据（记忆组 > 自成一域），只此一份"""
    if binding is not None:
        group_id = _group_column(binding, binding.memory_group_id, "memory_group_id")
        if group_id is not None:
            return ScopeDomain.of_group(group_id)
    return ScopeDomain.of_instance(adapter_key, instance_key)


def _affection_domain_of(
    adapter_key: str,
    instance_key: str,
    binding: Optional[DBInstanceDomainBinding],
) -> ScopeDomain:
    """好感域的判据（域覆盖 > 记忆域 > 自成一域），只此一份"""
    if binding is not None:
        group_id = _group_column(binding, binding.affection_group_id, "affection_group_id")
        if group_id is not None:
            return ScopeDomain.of_group(group_id)
    return _memory_domain_of(adapter_key, instance_key, binding)


__all__ = [
    "PRESET_NONE",
    "SELF_DOMAIN_GROUP_ID",
    "ChannelLike",
    "IdentityScope",
    "ScopeDomain",
    "resolve_affection_domain",
    "resolve_domain_instance_keys",
    "resolve_domain_members",
    "resolve_identity_scope",
    "resolve_identity_scope_by_chat_key",
    "resolve_identity_scopes",
    "resolve_memory_domain",
    "resolve_person_uid",
    "resolve_person_uids",
    "resolve_preset_id",
]
