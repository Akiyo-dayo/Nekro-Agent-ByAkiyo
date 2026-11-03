"""模型组表

模型组原本是 `CoreConfig.MODEL_GROUPS` 这个全局字典（见 `core/config.py`），
整份配置只有一份、也只有一个归属，撑不住多租户：定制版客户要能自建模型组接自己的 API，
同时**永远不能读到平台方那份凭据**。所以模型组提升为数据表，归属与可见性下沉到行上。

## 与 `CoreConfig.MODEL_GROUPS` 的关系

本表是**唯一事实来源**；`CoreConfig.MODEL_GROUPS` 降级为由本表同步填充的**运行时视图**，
供大量既有读取路径（`config.MODEL_GROUPS[config.USE_MODEL_GROUP]` 等）继续无感使用。
同步逻辑见 `services/model_group_service.py`。

## 命名唯一性

`name` 全局唯一。因为运行时视图是一个扁平字典，且 `USE_MODEL_GROUP` 等配置项按名字引用模型组，
一旦允许不同租户重名，同名的两个组在字典里会互相覆盖。租户创建重名组时直接报冲突。

## `api_key` 的静态加密

`api_key` 用 `EncryptedTextField`：**Python 这一侧恒为明文，数据库那一侧恒为密文**。
解密发生在模型层（Tortoise 的 `to_python_value`），因此 `can_view_group()`、掩码
（`build_model_group_info` / `mask_config_group`）、运行时取 Key（`to_config_group`）
以及任何 `row.api_key` 的读取点**一个字都不用改**——也不需要在各处撒解密调用。
威胁模型（挡库 dump、不挡宿主被拿下）见 `tools/credential_cipher.py`。

## 解不开的密文在一次普通保存中原样保留

密钥丢了之后那一行的 ORM 属性是空串（fail-closed，正确），但改造前任何一次
`row.save()` 都会把空串加密写回、**把还可能被原密钥救回来的密文永久抹掉**。
`DBModelGroup.save()` 上有一道回查守卫：要保存的行已存在、本次的 `api_key`
属性是空串、而库内原始列是**当前密钥解不开的密文**时，`api_key` 这一列被排除在
本次 UPDATE 之外（其余字段照常写），并打一条 WARNING 点名该组。

语义取舍（为什么空串不再等于"清空"）：

- **解得开的行不受影响**——守卫只在"解不开"时触发，正常部署里"清空 Key"
  （提交空串）与"换新 Key"（提交非空）都照旧；
- **解不开的行上"清空"被有意堵死**——那正是要堵的破坏动作：把一段还可能被
  原密钥救回的密文覆盖掉。想替换就填新 Key（非空属性不受守卫限制）；
  想找回原值就先恢复密钥。真正的"丢弃密文"只应发生在恢复过密钥之后。

## 配置文件里的占位符

`CoreConfig.dump_config` 把非空 Key 写成 `MODEL_GROUP_API_KEY_PLACEHOLDER`，
`import_config_group` 识别到它时按"没有可导入的 Key"处理（空 Key 落库 + 点名 WARNING），
绝不把占位串本身加密入库、也绝不覆盖库内已有密文。见 `core/config.py` 的契约说明。

生产路径上占位符在 `CoreConfig.load_config` 时已被归一成空串，所以走到这里的不是占位符本身：
`bootstrap_model_groups()` 按加载时的记账把这些组还原成占位符再交给本方法，上面的告警分支
因此在真实启动路径上可达（此前只有测试直接传占位符才走得到）。
"""

from enum import Enum
from typing import Any, Iterable, List, Optional, Union

from tortoise import Tortoise, fields
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.models import Model

from nekro_agent.core.config import ModelConfigGroup, is_model_group_api_key_placeholder
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.tenant import ModelGroupOwnership
from nekro_agent.tools.credential_cipher import (
    classify_stored_credential,
    decrypt_credential,
    encrypt_credential,
)

logger = get_sub_logger("model_group")


class ModelGroupVisibility(str, Enum):
    """平台模型组对租户的开放程度

    仅对 `ownership=platform` 的组有意义；租户自建组恒为该租户私有。
    """

    SHARED = "shared"  # 租户可选用（但凭据始终掩码）
    PRIVATE = "private"  # 仅平台方可见可用


# 新建模型组时的可见性默认值。**唯一的一份**：字段默认、`create_group()` 的形参默认、
# `upsert_platform_group()` 与 `import_config_group()` 全部引用它，
# 不允许任何调用点自己写一个字面量当默认（那正是"两份清单只改一份"的经典形态）。
#
# 取 private 的理由：默认 shared 等于「新开的任何租户开箱即可选用平台方的模型组」，
# 也就是开箱即可烧平台方的账单。#51 堵住的是租户主动越权引用 private 组那条路，
# 而默认值本身是一条**平台方从没做过的授权**，越权闸门管不到它。
# 开放给租户必须是平台方在后台的一次显式动作。
DEFAULT_VISIBILITY = ModelGroupVisibility.PRIVATE


class EncryptedTextField(fields.TextField):
    """静态加密的文本字段

    读写都在这里收口：`to_db_value` 出去时加密、`to_python_value` 回来时解密，
    模型属性因此永远是明文，上层代码不需要知道这一列是加密的。

    ⚠️ **不要按这一列做查询过滤**（`filter(api_key=...)`）。每次加密都用新的随机 nonce，
    同一个明文两次写入得到两个不同的密文，等值匹配永远不成立 —— 而它会静默地返回空集，
    不会报错。目前全仓没有这样的调用点（凭据本来也没有按值检索的用途）。

    ⚠️ 建表 DDL 仍是 `TEXT`，`api_key` 这个列名也没变：**列名不承诺明文**，而值自己带
    `nkenc1:` 前缀说明自己是密文。刻意不新造一个叫 `xxx_encrypted` 的列，
    `secret_payload_encrypted` 就是前车之鉴（名字写着加密、存的是明文）。
    """

    def to_db_value(self, value: Any, instance: "Union[type[Model], Model]") -> Any:
        value = super().to_db_value(value, instance)
        if value is None:
            return None
        return encrypt_credential(str(value))

    def to_python_value(self, value: Any) -> Any:
        value = super().to_python_value(value)
        if value is None:
            return None
        return decrypt_credential(str(value))


class DBModelGroup(Model):
    """模型组"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    name = fields.CharField(max_length=128, unique=True, description="模型组名（全局唯一，等同运行时视图的字典键）")

    # ==== 归属与可见性 ====
    ownership = fields.CharField(
        max_length=32,
        default=ModelGroupOwnership.PLATFORM.value,
        index=True,
        description="归属：platform=平台方 / tenant=租户自建",
    )
    # NULL 表示归平台方。未启用多租户时全表恒为 NULL，判定一律走"归平台方"分支
    owner_tenant_id = fields.IntField(null=True, index=True, description="归属租户ID；平台组为 NULL")
    # 默认值取自 `DEFAULT_VISIBILITY`（private），理由见那里。
    # 存量行不受影响：字段默认只作用于新建行，存量组的可见性要由运维逐个确认。
    # ⚠️ 前端表单的默认值必须与这里一致
    # （`frontend/src/components/model-group/ModelGroupFormDialog.tsx` 的 `DEFAULT_VISIBILITY`）；
    # 两侧相反踩过一次（`ENABLE_VISION` 前端 true / 后端 false，#32），
    # 现由 `tests/test_model_group_default_visibility.py` 把两边钉在一起。
    visibility = fields.CharField(
        max_length=32,
        default=DEFAULT_VISIBILITY.value,
        description="平台组对租户的开放程度：shared=租户可选用 / private=仅平台方",
    )

    # ==== 以下字段与 ModelConfigGroup 一一对应 ====
    group_name = fields.CharField(max_length=128, default="", description="模型组显示名称")
    chat_model = fields.CharField(max_length=256, default="", description="聊天模型名称")
    chat_proxy = fields.CharField(max_length=512, default="", description="聊天模型访问代理")
    base_url = fields.CharField(max_length=512, default="", description="接口地址")
    # 静态加密：库里是密文，Python 属性是明文。见上面的 `EncryptedTextField`
    api_key = EncryptedTextField(default="", description="接口凭据（静态加密存储）")
    model_type = fields.CharField(max_length=32, default="chat", description="模型类型 chat/embedding/draw")
    enable_vision = fields.BooleanField(default=False, description="启用视觉功能")
    enable_cot = fields.BooleanField(default=False, description="启用外置思维链")
    token_input_rate = fields.FloatField(default=1.0, description="输入 Token 倍率")
    token_completion_rate = fields.FloatField(default=1.0, description="补全 Token 倍率")
    model_price_rate = fields.FloatField(default=1.0, description="模型价格倍率")
    temperature = fields.FloatField(null=True, description="温度值")
    top_p = fields.FloatField(null=True, description="Top P")
    top_k = fields.IntField(null=True, description="Top K")
    presence_penalty = fields.FloatField(null=True, description="提示重复惩罚")
    frequency_penalty = fields.FloatField(null=True, description="补全重复惩罚")
    extra_body = fields.TextField(null=True, description="额外参数 (JSON)")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "model_group"

    async def save(
        self,
        using_db: Optional[BaseDBAsyncClient] = None,
        update_fields: Optional[Iterable[str]] = None,
        force_create: bool = False,
        force_update: bool = False,
    ) -> None:
        """带 #64 守卫的保存：解不开的库内密文不因一次普通保存被空串覆盖

        守卫只裁剪 `api_key` 一个字段的 UPDATE，其余行为与 `Model.save` 逐字一致。
        唯一的特殊分支是 `update_fields` 只含 `api_key` 且被守卫拦下：那次保存
        无事可做，直接返回（`update_time` 也不推进——本来就没改任何东西）。
        """
        if not force_create:
            update_fields = await self._preserve_undecryptable_api_key(using_db, update_fields)
            if update_fields is not None and not update_fields:
                return
        await super().save(
            using_db=using_db,
            update_fields=update_fields,
            force_create=force_create,
            force_update=force_update,
        )

    async def _preserve_undecryptable_api_key(
        self,
        using_db: Optional[BaseDBAsyncClient],
        update_fields: Optional[Iterable[str]],
    ) -> Optional[List[str]]:
        """若本次保存会把一段解不开的密文覆盖成空串，则把 `api_key` 裁出 UPDATE

        命中条件（三者同时成立）：

        1. 行已存在（`pk` 非空）——新建行走 INSERT，没有"原密文"可保护；
        2. 内存属性恰为空串——密钥解不开时 `to_python_value` 给的就是它；
        3. 库内**原始列**（绕开 ORM 读，否则读到的又是一次解密尝试）是结构上
           合法、但当前密钥解不开的信封——用 `classify_stored_credential` 判档，
           这样"解得开的行"（用户想清空/换 Key 的正常对象）不会被误拦。

        返回值是调整后的 `update_fields`：None 表示不受限（原样全量更新）；
        非 None 时是不含 `api_key` 的字段清单，可能为空（调用方据此跳过保存）。

        为什么不能在字段层做：`to_python_value()` 拿不到实例、`to_db_value()` 拿不到
        旧值（Tortoise 的转换接口都没有这两个入参），"读库里的旧值"只能发生在
        拿得到 `self.pk` 的模型方法里——所以落点是 `save()`。
        """
        if self.pk is None or self.api_key:
            return update_fields
        if update_fields is not None and "api_key" not in update_fields:
            # 这次保存本来就不动这一列，不必多查一趟库
            return update_fields
        db = using_db or Tortoise.get_connection("default")
        # 主键转 int 后内联进 SQL，**不用占位符**：SQLite 驱动认 `?`，asyncpg 不转换占位符
        # （只认 `$n`），Postgres 上 `WHERE "id" = ?` 是语法错误——任何空 Key 的已有行
        # （含内置 `default` 组）的保存都会因此失败，而 SQLite 测试看不见。
        # `int()` 同时是注入防线：`pk` 是 IntField，这里不会有任何字符串能溜进 SQL。
        # 判据见 `tests/test_model_group_postgres_dialect.py`（全仓禁 `?` + 真 PG 用例）。
        rows = await db.execute_query_dict(
            f'SELECT "api_key" FROM "model_group" WHERE "id" = {int(self.pk)}',
        )
        stored = rows[0].get("api_key") if rows else ""
        if not isinstance(stored, str) or not stored:
            return update_fields
        if classify_stored_credential(stored) != "undecryptable":
            return update_fields

        logger.warning(
            f"模型组 {self.name} 的库内凭据当前密钥解不开，本次保存已原样保留该密文"
            "（未按空串覆盖）。想替换请直接填入新的 API Key；要找回原值请先恢复原密钥"
            "（NEKRO_CREDENTIAL_SECRET_KEY 或数据目录里的密钥文件）。",
        )
        if update_fields is None:
            # 全量更新：等价于"除 api_key 外的所有库字段"，与 executor 的默认投影同源
            return [name for name in self._meta.fields_db_projection if name != "api_key"]  # noqa: SLF001
        return [name for name in update_fields if name != "api_key"]

    @property
    def is_platform_owned(self) -> bool:
        """是否归平台方

        以 `owner_tenant_id` 为准而不是 `ownership` 字段：真正决定凭据可见性的是行上的租户外键，
        两者若因数据异常不一致，按"更保守的那一侧"判定，避免把平台凭据判给租户。
        """
        return self.owner_tenant_id is None or self.ownership == ModelGroupOwnership.PLATFORM.value

    def to_config_group(self) -> ModelConfigGroup:
        """转成运行时视图使用的 `ModelConfigGroup`"""
        return ModelConfigGroup(
            GROUP_NAME=self.group_name,
            CHAT_MODEL=self.chat_model,
            CHAT_PROXY=self.chat_proxy,
            BASE_URL=self.base_url,
            API_KEY=self.api_key,
            MODEL_TYPE=self.model_type if self.model_type in ("chat", "embedding", "draw") else "chat",
            ENABLE_VISION=self.enable_vision,
            ENABLE_COT=self.enable_cot,
            TOKEN_INPUT_RATE=self.token_input_rate,
            TOKEN_COMPLETION_RATE=self.token_completion_rate,
            MODEL_PRICE_RATE=self.model_price_rate,
            TEMPERATURE=self.temperature,
            TOP_P=self.top_p,
            TOP_K=self.top_k,
            PRESENCE_PENALTY=self.presence_penalty,
            FREQUENCY_PENALTY=self.frequency_penalty,
            EXTRA_BODY=self.extra_body,
        )

    def apply_config_group(self, group: ModelConfigGroup) -> None:
        """用 `ModelConfigGroup` 覆盖本行的模型字段（不动归属与可见性）"""
        self.group_name = group.GROUP_NAME
        self.chat_model = group.CHAT_MODEL
        self.chat_proxy = group.CHAT_PROXY
        self.base_url = group.BASE_URL
        self.api_key = group.API_KEY
        self.model_type = group.MODEL_TYPE
        self.enable_vision = group.ENABLE_VISION
        self.enable_cot = group.ENABLE_COT
        self.token_input_rate = group.TOKEN_INPUT_RATE
        self.token_completion_rate = group.TOKEN_COMPLETION_RATE
        self.model_price_rate = group.MODEL_PRICE_RATE
        self.temperature = group.TEMPERATURE
        self.top_p = group.TOP_P
        self.top_k = group.TOP_K
        self.presence_penalty = group.PRESENCE_PENALTY
        self.frequency_penalty = group.FREQUENCY_PENALTY
        self.extra_body = group.EXTRA_BODY

    @classmethod
    async def import_config_group(cls, name: str, group: ModelConfigGroup) -> Optional["DBModelGroup"]:
        """把配置文件里的模型组导入为平台组；已存在同名行时不动它

        存量迁移靠这条保证幂等：重复启动只会命中"已存在"分支，**绝不覆盖库内行**
        （包括其中的密文），返回新建的行，未新建时返回 None。

        `API_KEY` 等于配置文件占位符（`MODEL_GROUP_API_KEY_PLACEHOLDER`，#61）时按
        "没有可导入的 Key"处理：占位串不是凭据，绝不加密入库；该组以空 Key 落地并打
        WARNING 点名——拿一份运行时镜像恢复部署的人需要被告知"Key 得去哪儿补"。

        导入的组按 `DEFAULT_VISIBILITY`（private）落地：配置文件里的模型组是平台方自己的凭据，
        多租户落地之前压根没有"共享给租户"这回事，把它们默认开放给所有租户是在替平台方做一个
        他从没做过的决定。要开放是后台里的一次显式动作。
        """
        if await cls.filter(name=name).exists():
            return None
        row = cls(
            name=name,
            ownership=ModelGroupOwnership.PLATFORM.value,
            owner_tenant_id=None,
            visibility=DEFAULT_VISIBILITY.value,
        )
        row.apply_config_group(group)
        if is_model_group_api_key_placeholder(group.API_KEY):
            row.api_key = ""
            logger.warning(
                f"配置文件里的模型组 {name} 的 API_KEY 是占位符（真实密文只存数据库），"
                "本次导入的该组 Key 为空。若这是从运行时镜像恢复的部署，请在后台为它重填 API Key。",
            )
        await row.save()
        return row
