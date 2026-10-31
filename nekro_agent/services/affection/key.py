"""好感度的隔离键

## 为什么单独一个模块

隔离键从 `(适配器, 实例, 平台用户)` 变成 `(域, 人, 人设)` 之后，它就不再是"数值层"的概念了：
域要查实例组、人要查别名表，两者都在 `services/identity_scope.py` 里。而 :mod:`.stage`
的立身之本是**不碰数据库、不碰配置以外的任何全局状态**（模块文档第一句），
把键留在那里就等于让整个数值层跟着把模型层拖进来。所以键搬到这里，
:mod:`.stage` 继续是一个可以裸测的纯函数模块。

## 键只有三个来源

1. :meth:`AffectionKey.of_scope` —— 从解析器的结果构造，**热路径上唯一的入口**；
2. :meth:`AffectionKey.of_domain` —— 手上是"域 + 人 + 人设"而不是频道的后台路径
   （账号合并逐人设补偿时就是这样：它按域操作，根本没有会话）；
3. `store.key_of_record` —— 从一行已经落库的记录读回它自己的键（后台详情、人工调整、
   重置都是"对着一行已经存在的数据操作"，它们手上没有频道，也就没法解析）。

**任何地方都不要自己 `AffectionKey(...)` 拼一个出来。** 五个字段里有三个是域的编码，
拼错一个的后果不是报错而是查不到自己写的行（好一点）或读到别人的行（坏的那种）。
这条与 `identity_scope.ScopeDomain` 是"域两种编码的唯一产生者"同源。

## 列名与字段名不一致是有意的

人这一维在库里的列名是 `platform_userid`（改名会让 aerich 生成 drop+add，
理由见 `models/db_affection.py`），但它存的值是**人**的标识而不是"发消息的那个账号"。
键上的字段因此叫 `person_uid`，:meth:`AffectionKey.as_columns` 负责这一次改名。
"发消息的那个账号"只在写事件流水时出现，走 `store.apply_delta` 的
`source_platform_userid` 参数，不进键。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, NamedTuple

from nekro_agent.services.identity_scope import SELF_DOMAIN_GROUP_ID, ScopeDomain

if TYPE_CHECKING:
    from nekro_agent.services.identity_scope import IdentityScope


class AffectionKey(NamedTuple):
    """好感度隔离键 `(域, 人, 人设)`

    Attributes:
        adapter_key: 自成一域时的适配器标识；属于实例组时为空串
        instance_key: 自成一域时的实例标识；属于实例组时为空串
        domain_group_id: 所属实例组 ID；0 表示自成一域
        person_uid: 人的标识（= 该人主账号的平台用户 ID）
        preset_id: 人设标识；0 表示无人设档

    前三项合起来才是一个域（见 `models/db_affection.py` 的"域为什么用三列"），
    单独拿 `instance_key` 去过滤在实例被放进组之后会**一行都查不到**。
    """

    adapter_key: str
    instance_key: str
    domain_group_id: int
    person_uid: str
    preset_id: int

    @classmethod
    def of_scope(cls, scope: "IdentityScope") -> "AffectionKey":
        """从解析器的结果构造键——热路径上唯一的入口

        取的是 `affection_domain` / `affection_person_uid` 这一侧而不是记忆那一侧：
        好感域可以被 `affection_group_id` 覆盖（前端不暴露，见
        `models/db_instance_group.py`），而人是**按域**解析的，跨域套用另一个域的解析结果
        就是串域。两者未配置覆盖时与记忆侧逐字相同，这条由
        `TestAffectionDomainFollowsMemoryDomain` 钉死。
        """
        domain = scope.affection_domain
        return cls(
            adapter_key=domain.adapter_key,
            instance_key=domain.instance_key,
            domain_group_id=domain.group_id,
            person_uid=scope.affection_person_uid,
            preset_id=scope.preset_id,
        )

    @classmethod
    def of_domain(cls, domain: ScopeDomain, *, person_uid: str, preset_id: int) -> "AffectionKey":
        """从一个已经解析出来的域构造键

        给"按域操作、手上没有会话"的后台路径用（账号合并的逐人设补偿）。域必须来自
        `identity_scope`（它是域编码的唯一产生者），**不要在调用处自己拼那三列**——
        组域的 `adapter_key` / `instance_key` 必须是空串，写错就是一份谁也读不到的数据。
        """
        return cls(
            adapter_key=domain.adapter_key,
            instance_key=domain.instance_key,
            domain_group_id=domain.group_id,
            person_uid=person_uid,
            preset_id=preset_id,
        )

    @property
    def domain(self) -> ScopeDomain:
        """键所在的域

        `ScopeDomain` 的两个工厂方法是域编码的唯一产生者，这里只做"哪一种"的分派：
        `domain_group_id` 落在组上就是组域，否则是自成一域。
        """
        if self.domain_group_id != SELF_DOMAIN_GROUP_ID:
            return ScopeDomain.of_group(self.domain_group_id)
        return ScopeDomain.of_instance(self.adapter_key, self.instance_key)

    @property
    def domain_id(self) -> str:
        """域的字符串编码（`g:<组ID>` / `i:<适配器>:<实例>`），别名表用的就是它"""
        return self.domain.domain_id

    def as_columns(self) -> Dict[str, object]:
        """键在 `affection` / `affection_event` 两张表上的完整列值

        建行、查询、事件流水共用这一份。少一列就是串号（比如漏掉 `preset_id`
        会让两个人设的记录互相覆盖），多一列就是永远查不到自己写的行。
        """
        return {
            **self.domain.as_columns(),
            "platform_userid": self.person_uid,
            "preset_id": self.preset_id,
        }

    def scope_columns(self) -> Dict[str, object]:
        """域三列 + 人设，**不含人**

        供"这个人的全部账号"这类查询做前缀：合并过的账号在库里仍然是各自的
        `platform_userid`，取并集时需要把人这一维换成 `platform_userid__in=[...]`。
        """
        return {**self.domain.as_columns(), "preset_id": self.preset_id}


__all__ = ["AffectionKey"]
