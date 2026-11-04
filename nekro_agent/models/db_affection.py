"""好感度数据模型

## 隔离键

好感度按 **(记忆域, 人, 人设)** 三元组隔离，键的三维在本表上分别落成：

| 维度 | 列 | 说明 |
|---|---|---|
| 域 | `domain_group_id` + `adapter_key` + `instance_key` | `domain_group_id = 0` 表示**自成一域**，域的身份由本行的 `(adapter_key, instance_key)` 决定；`domain_group_id > 0` 表示属于该实例组，此时 `adapter_key` / `instance_key` **必须写成空串**（域是组，不是任何一个成员实例） |
| 人 | `platform_userid` | 语义已从"平台用户 ID"变成**"人的标识"**（= 该人主账号的平台用户 ID）。从没合并过的人两者逐字相同，因此列名与存量值都不用动 |
| 人设 | `preset_id` | 频道当前生效的人设行 ID；`0` = 无人设档（回落到内置默认人设） |

**列名 `platform_userid` 是有意保留的。** 改名会让 aerich 生成 drop+add（存量数据直接消失），
而它存的值本来就等于该人的 `person_uid`。代价是名字比语义窄，所以：
**任何地方都不要拿它直接当"发消息的那个账号"用**，那个值在 `DBAffectionEvent.source_platform_userid` 里。

**域为什么用三列而不是一个字符串。** 自成一域的域标识（`i:<适配器>:<实例>`）是**逐行不同**的，
不可能作为列默认值。用一个 `domain_group_id INT DEFAULT 0` 加上本行既有的
`(adapter_key, instance_key)`，存量行**不需要任何回填**就落在
「自成一域 / 人 = 自己 / 无人设档」这个确定位置上，迁移因此是纯 DDL。
`identity_scope.ScopeDomain` 是这套编码的唯一产生者，**不要在别处自己拼这三列**。

**已知代价（留给后续几组）**：一个实例从"自成一域"被放进某个组时，它此前的好感行
（`domain_group_id = 0`）不会自动跟过去。上线是全新启动、没有存量，
但后台的"加入域"动作应当配一个把成员既有好感并入组域的选项，否则运营会看到一次归零。

## 隔离键为什么不含会话

好感度记录的是"这个人"，而不是"这个人在这个房间里"。分开存会产生两种坏结果：
私聊聊了很久的人一进群就变回陌生人（叙事断裂），而群聊里人多话少、那一份几乎永远长不起来
（数据稀疏）。场合分寸是**表达**问题，由提示词里的语境说明约束，不由数据结构约束。
`last_chat_key` 只用于后台展示"最近在哪儿聊的"，不参与隔离。

**注意「群聊私聊合并记一份」只在同一人设下成立**：同一个人在群 A（人设甲）与群 B（人设乙）
是两份好感，这是"换了个人设就不会记得以前的恩怨情仇"这条产品语义的直接结果。

## 群聊与私聊不分开

好感度记录的是"这个人"，而不是"这个人在这个房间里"。分开存会产生两种坏结果：
私聊聊了很久的人一进群就变回陌生人（叙事断裂），而群聊里人多话少、那一份几乎永远长不起来
（数据稀疏）。场合分寸是**表达**问题，由提示词里的语境说明约束，不由数据结构约束。
`last_chat_key` 只用于后台展示"最近在哪儿聊的"，不参与隔离。

## 两层结构：数值是里子，描述是面子

`score` / `stage` 是内部实现，供门槛判定、排序、后台调整使用；
`state_text` / `last_event_text` 是由 LLM 按当前人设口吻生成的状态描述，是真正注入提示词、
呈现给用户的那一层。两者都必须存在：只有数值就变成冷冰冰的审计表，只有描述就没法做判定和管理。

**`state_text` / `last_event_text` / `nickname` 全部是不可信内容**：它们由 LLM 生成或由用户
提供，写库前经 `tools/untrusted_content.py` 清洗，注入提示词时再包一层带会话令牌的边界标签。
"""

from tortoise import fields
from tortoise.models import Model


class DBAffection(Model):
    """好感度记录（每个域下的每个人在每个人设下一行）"""

    id = fields.IntField(pk=True, generated=True, description="ID")

    # === 域（三列一组，见模块文档；只由 identity_scope.ScopeDomain 产生） ===
    # `domain_group_id > 0` 时这两列恒为空串：域是实例组，不是任何一个成员实例。
    adapter_key = fields.CharField(max_length=64, index=True, description="自成一域时的适配器标识；属于实例组时为空串")
    instance_key = fields.CharField(
        max_length=64,
        default="",
        index=True,
        description="自成一域时的实例标识；属于实例组时为空串",
    )
    domain_group_id = fields.IntField(default=0, index=True, description="所属实例组 ID；0 表示自成一域")

    # === 人 ===
    # 语义是"人的标识"（= 主账号平台用户 ID），不是"发消息的那个账号"。列名见模块文档。
    platform_userid = fields.CharField(max_length=256, index=True, description="人的标识（= 主账号平台用户 ID）")

    # === 人设 ===
    # 频道当前生效的人设行 ID；0 = 无人设档。人设删除后本列不动，数据留成孤儿（不级联删）。
    preset_id = fields.IntField(default=0, index=True, description="人设标识；0 表示无人设档")

    # 最近一次见到的昵称，仅供后台展示定位；由用户自行设置，属不可信内容
    nickname = fields.CharField(max_length=128, default="", description="最近一次见到的昵称")

    score = fields.IntField(default=0, index=True, description="好感度数值")
    # 由 score 派生，冗余落库以便后台按阶段筛选与排序
    stage = fields.CharField(max_length=32, default="stranger", index=True, description="好感度阶段")

    state_text = fields.TextField(default="", description="LLM 生成的状态描述（不可信内容）")
    last_event_text = fields.TextField(default="", description="LLM 生成的最近一次变化叙述（不可信内容）")
    state_generated_at = fields.DatetimeField(null=True, description="状态描述生成时间")

    interaction_count = fields.IntField(default=0, description="累计互动轮次")
    last_interact_at = fields.DatetimeField(null=True, description="最近互动时间")
    last_chat_key = fields.CharField(max_length=256, default="", description="最近互动的会话标识")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "affection"
        # 新键是旧键 `(adapter_key, instance_key, platform_userid)` 的**超集**，
        # 因此存量行在两个新列取默认值（0 / 0）时自动满足它，迁移不需要任何回填。
        unique_together = (("adapter_key", "instance_key", "domain_group_id", "platform_userid", "preset_id"),)


class DBAffectionEvent(Model):
    """好感度变化事件

    存在的意义是"为什么变了"这一层叙事：后台看到的是一条条带原因的时间线，
    而不是一个孤零零的当前分数。同时也是人工调整的审计轨迹。

    键的三维与 `DBAffection` 逐列对应（本表没有唯一约束，它是流水）。
    合并账号时**流水取并集、保留来源账号**：来源账号落在 `source_platform_userid`，
    没有它的话，两个号并成一个人之后就再也说不清"这条是哪个号干的"。
    """

    id = fields.IntField(pk=True, generated=True, description="ID")

    # 域三列，语义与 DBAffection 逐字相同
    adapter_key = fields.CharField(max_length=64, index=True, description="自成一域时的适配器标识；属于实例组时为空串")
    instance_key = fields.CharField(
        max_length=64,
        default="",
        index=True,
        description="自成一域时的实例标识；属于实例组时为空串",
    )
    domain_group_id = fields.IntField(default=0, index=True, description="所属实例组 ID；0 表示自成一域")

    platform_userid = fields.CharField(max_length=256, index=True, description="人的标识（= 主账号平台用户 ID）")
    # 触发本条事件的原始账号。空串表示"与 platform_userid 相同"（未合并过的人，也是存量行的取值）。
    source_platform_userid = fields.CharField(
        max_length=256,
        default="",
        index=True,
        description="触发本条事件的原始账号；空串表示与 platform_userid 相同",
    )
    preset_id = fields.IntField(default=0, index=True, description="人设标识；0 表示无人设档")

    chat_key = fields.CharField(max_length=256, default="", index=True, description="触发变化的会话标识")

    delta = fields.IntField(default=0, description="本次变化量")
    score_after = fields.IntField(default=0, description="变化后的好感度数值")
    # llm / manual / reset / ... —— 见 services/affection/stage.py 的 AffectionEventSource
    source = fields.CharField(max_length=16, default="llm", index=True, description="变化来源")
    reason = fields.TextField(default="", description="变化原因叙述（不可信内容）")
    operator = fields.CharField(max_length=128, default="", description="人工调整的操作者用户名")

    # 软删：管理员只删这一条的理由文字。`reason` 置空，行与 `delta` / `score_after` 原样保留——
    # 流水是账本（滚动窗口额度从它算），删行会改掉别人的额度，也抹掉了「这里删过东西」的痕迹。
    deleted_at = fields.DatetimeField(null=True, description="理由被删除的时间；NULL 表示没删过")
    deleted_by = fields.CharField(max_length=128, default="", description="删除理由的操作者用户名")

    create_time = fields.DatetimeField(auto_now_add=True, index=True, description="创建时间")

    class Meta:  # type: ignore
        table = "affection_event"
