"""总后台（Console）的对外数据契约

本模块是后端各子模块与前端之间的唯一契约来源，改动需同步前端 `frontend/src/services/api/console.ts`。

## 三层模型

```
Console（总后台，全局唯一）
  └─ Instance（实例，一个实例 = 一个 Bot 账号）
       └─ Channel（会话）
```

`InstanceInfo.tenant_id` 是实例的租户归属，直接取自数据库列，None 表示归平台方。
它同时是前端「实例划归」界面判定勾选状态的唯一依据，任何把它固定成常量的实现
都会让界面显示的归属与库里的真实归属脱节。
"""

from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class InstanceProvider(str, Enum):
    """实例背后的协议端形态"""

    BUILTIN_NAPCAT = "builtin_napcat"  # 本机 Docker 内托管的 NapCat 容器
    REMOTE_ONEBOT = "remote_onebot"  # 连接到外部已有的 OneBot 协议端
    NATIVE = "native"  # 原生适配器（qqbot_openclaw / email / minecraft 等）
    BUILTIN_QBOB = "builtin_qbob"  # 附带的 qbob（QQ 官方机器人 → OneBot v11）里由 NA 托管的 bot


class InstanceStatus(str, Enum):
    """实例健康状态

    `UNBOUND` 与 `OFFLINE` 是**两件不同的事**，此前被合并成同一个红点，是「建了实例、连了协议端，
    实例管理却一直说离线」这条误导的直接来源：

    - `OFFLINE` = 绑过账号、连接断了 → 下一步是去看协议端
    - `UNBOUND` = 从来没绑过账号，因此从来没上线过 → 下一步是去认领 / 绑定账号

    两者对应的运维动作完全不同，混成一个状态就等于把「你什么都没接上」显示成「你的 Bot 掉线了」。
    """

    ONLINE = "online"
    CONNECTING = "connecting"
    OFFLINE = "offline"
    UNBOUND = "unbound"  # 实例已建好但从未绑定账号：不是掉线，是还没接上
    LOGIN_REQUIRED = "login_required"  # 容器在跑但未登录，需要扫码
    ERROR = "error"
    UNCLAIMED = "unclaimed"  # 协议端自行连入但尚未在后台认领
    DISABLED = "disabled"
    ARCHIVED = "archived"


class ContainerState(str, Enum):
    """协议端容器状态"""

    RUNNING = "running"
    EXITED = "exited"
    CREATED = "created"
    RESTARTING = "restarting"
    PAUSED = "paused"
    ABSENT = "absent"  # 容器不存在


class ContainerInfo(BaseModel):
    """协议端容器信息，仅 builtin_napcat 形态有值"""

    name: str = Field(default="", description="容器名")
    state: ContainerState = Field(default=ContainerState.ABSENT, description="容器状态")
    image: str = Field(default="", description="镜像")
    webui_port: Optional[int] = Field(default=None, description="NapCat WebUI 宿主机端口")
    started_at: Optional[str] = Field(default=None, description="启动时间")


class InstanceStats(BaseModel):
    """实例的运行统计"""

    channel_count: int = Field(default=0, description="会话数")
    message_count_today: int = Field(default=0, description="今日消息数")
    last_active_at: Optional[str] = Field(default=None, description="最近活跃时间")


class InstanceInfo(BaseModel):
    """实例概要"""

    instance_key: str = Field(..., description="实例标识（稳定 slug，创建后不变）")
    display_name: str = Field(default="", description="显示名称")
    adapter_key: str = Field(..., description="适配器标识")
    provider: InstanceProvider = Field(default=InstanceProvider.REMOTE_ONEBOT, description="协议端形态")
    provider_account_id: str = Field(default="", description="绑定的平台账号（如 QQ 号）")
    status: InstanceStatus = Field(default=InstanceStatus.OFFLINE, description="健康状态")
    enabled: bool = Field(default=True, description="是否启用")
    is_default: bool = Field(default=False, description="是否默认实例")
    tenant_id: Optional[int] = Field(default=None, description="所属租户；None 表示未归属任何租户（归平台方）")
    container: Optional[ContainerInfo] = Field(default=None, description="容器信息")
    stats: InstanceStats = Field(default_factory=InstanceStats, description="运行统计")
    last_error: str = Field(default="", description="最近错误")
    offline_since: Optional[str] = Field(default=None, description="掉线起始时间")
    qbob_appid: str = Field(
        default="",
        description="内置 qbob 实例已接入的 AppID；非空表示 qbob 里还挂着这个 bot（AppID 不是凭据）",
    )


class InstanceListResponse(BaseModel):
    """实例列表"""

    items: List[InstanceInfo] = Field(default_factory=list)
    total: int = Field(default=0)


class InstanceOption(BaseModel):
    """实例作用域切换器用的轻量实例条目

    只保留「选哪个实例」真正要用的字段：定位用的标识与适配器、认人用的显示名与绑定账号、
    以及判断值不值得切进去的状态位。

    **刻意不含 `container` 与 `stats`**，这是本模型存在的全部理由：
    前者每个 `builtin_napcat` 实例都要新建一个 Docker 客户端做一次 inspect
    （`container_manager._docker_client` 按操作粒度开闭，不复用），
    后者是三条跨 `chat_message` 全表的聚合。切换器挂在 `MainLayout` 上、每开一个后台
    标签页就要拉一次，而这两样它一个都用不到 —— 大量实例的部署上这就是 N 次（N 为实例数）
    Docker 往返换零信息量。
    """

    instance_key: str = Field(..., description="实例标识（稳定 slug，创建后不变）")
    display_name: str = Field(default="", description="显示名称")
    adapter_key: str = Field(..., description="适配器标识")
    provider_account_id: str = Field(default="", description="绑定的平台账号（如 QQ 号）；空串表示尚未绑定")
    status: InstanceStatus = Field(default=InstanceStatus.OFFLINE, description="健康状态")
    enabled: bool = Field(default=True, description="是否启用")


class InstanceOptionListResponse(BaseModel):
    """实例作用域切换器的候选列表"""

    items: List[InstanceOption] = Field(default_factory=list)
    total: int = Field(default=0)


class InstanceCreateRequest(BaseModel):
    """新建实例

    `instance_key` 留空时由后端生成；一旦创建不可修改（它编码在所有 chat_key 里）。
    """

    instance_key: str = Field(default="", description="实例标识，留空自动生成；仅小写字母数字下划线，最长 32")
    display_name: str = Field(default="", description="显示名称")
    adapter_key: str = Field(default="onebot_v11", description="适配器标识")
    provider: InstanceProvider = Field(default=InstanceProvider.BUILTIN_NAPCAT, description="协议端形态")
    provider_account_id: str = Field(default="", description="已知的平台账号，可留空待登录后回填")
    access_token: str = Field(default="", description="协议端 access token，留空自动生成")


class InstanceUpdateRequest(BaseModel):
    """更新实例的可变属性（instance_key 与 adapter_key 不可变）"""

    display_name: Optional[str] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)
    is_default: Optional[bool] = Field(default=None)
    provider_account_id: Optional[str] = Field(default=None)


class InstanceDeleteRequest(BaseModel):
    """删除实例

    默认归档（数据只读保留）。彻底删除需要显式确认，且会级联清除会话、消息、记忆等全部数据。
    """

    purge: bool = Field(default=False, description="是否彻底删除数据；False 表示仅归档")
    confirm_instance_key: str = Field(default="", description="彻底删除时必须回填实例标识作为二次确认")


class InstanceMigrationChannelItem(BaseModel):
    """归属迁移里一个频道的去向"""

    channel_id: str = Field(..., description="频道标识，形如 group_123456")
    channel_name: str = Field(default="", description="频道名称")
    source_chat_key: str = Field(..., description="迁移前的会话标识")
    target_chat_key: str = Field(..., description="迁移后的会话标识")
    conflict: bool = Field(default=False, description="目标实例下已有同频道的行，本频道会被合并进去")
    message_count: int = Field(default=0, description="该频道的历史消息数")


class InstanceMigrationResidueItem(BaseModel):
    """归属迁移**不搬**的数据，连同原因一起展示"""

    resource: str = Field(..., description="数据源名称")
    count: int = Field(default=0, description="留在源实例的行数")
    reason: str = Field(default="", description="为什么不搬")


class InstanceMigrationPreview(BaseModel):
    """归属迁移预览

    迁移是破坏性且不可逆的，执行前必须先拿到本预览：`counts` 与执行结果用同一口径统计，
    `channels` 里 `conflict=true` 的那些会被合并而不是整行搬过去。
    """

    adapter_key: str = Field(..., description="两个实例共同所属的适配器")
    source_instance_key: str = Field(..., description="交出历史的实例")
    source_display_name: str = Field(default="", description="源实例显示名称")
    target_instance_key: str = Field(..., description="接收历史的实例")
    target_display_name: str = Field(default="", description="目标实例显示名称")
    channel_total: int = Field(default=0, description="将要迁移的频道数")
    conflict_total: int = Field(default=0, description="其中会与目标实例已有频道合并的数量")
    counts: Dict[str, int] = Field(default_factory=dict, description="各数据源将要改写的行数")
    channels: List[InstanceMigrationChannelItem] = Field(default_factory=list, description="逐频道明细")
    residues: List[InstanceMigrationResidueItem] = Field(default_factory=list, description="不会被搬走的数据")
    warnings: List[str] = Field(default_factory=list, description="执行前需要知道的风险提示")


class InstanceMigrationRequest(BaseModel):
    """执行归属迁移

    `confirm_source_instance_key` 必须回填源实例标识，与彻底删除实例同一套二次确认形态。
    """

    source_instance_key: str = Field(..., description="交出历史的实例标识")
    confirm_source_instance_key: str = Field(default="", description="回填源实例标识作为二次确认")


class InstanceMigrationResult(BaseModel):
    """归属迁移的执行结果

    频道逐个结算：`failed_channels` 非空表示这些频道仍留在源实例，重跑一次即可补齐，
    已完成的频道不会被回退。
    """

    ok: bool = Field(default=True, description="是否全部频道都已迁移")
    source_instance_key: str = Field(..., description="交出历史的实例")
    target_instance_key: str = Field(..., description="接收历史的实例")
    moved_channels: List[str] = Field(default_factory=list, description="整行搬到目标实例的频道")
    merged_channels: List[str] = Field(default_factory=list, description="并进目标实例已有频道的频道")
    failed_channels: List[str] = Field(default_factory=list, description="迁移失败、仍留在源实例的频道")
    counts: Dict[str, int] = Field(default_factory=dict, description="各数据源实际改写的行数")
    residual: Dict[str, int] = Field(default_factory=dict, description="迁移后仍挂在源实例名下的行数，正常全为 0")
    message: str = Field(default="", description="结果摘要")


# ==================== 认领流程（未认领协议端的两条出路） ====================


class InstanceClaimRequest(BaseModel):
    """认领一个协议端自行连入、尚未在总后台登记的实例

    `instance_key` 是**认领后想要的标识**，不是路径上那个自动生成的 `ob{账号}`。
    留空则沿用自动生成的标识（就地认领，不搬运任何数据）；填了就等价于
    「建一个用这个标识的实例，再把协议端连同历史整体交给它」——`instance_key`
    编码在全部 `chat_key` 里，就地改列会让历史数据一次性变孤儿，所以只能这样表达。
    """

    display_name: str = Field(default="", description="认领后的显示名称；留空沿用协议端登记时的名称")
    instance_key: str = Field(
        default="",
        description="认领后想要的实例标识；留空表示沿用自动生成的 ob{账号}。仅小写字母数字下划线，最长 32",
    )


class InstanceAdoptRequest(BaseModel):
    """把一个未认领协议端并入某个既有实例

    路径上的实例是**接收方**（与历史归属迁移同一形态："把某个东西并到本实例来"）。
    """

    source_instance_key: str = Field(..., description="要被并入的未认领实例标识")
    confirm_source_instance_key: str = Field(default="", description="回填未认领实例标识作为二次确认")


class InstanceClaimResult(BaseModel):
    """认领的执行结果

    `ok=false` 表示这是一次**可重试的半成品**：部分会话迁移失败，因此账号没有过户、
    源实例也没有归档，界面上仍然看得到那个未认领的协议端，重跑同一次操作即可补齐。
    """

    ok: bool = Field(default=True, description="是否已完整完成")
    instance: InstanceInfo = Field(..., description="认领后的实例（就地认领时即协议端自己那一行）")
    source_instance_key: str = Field(..., description="被认领的协议端实例标识")
    account_moved: bool = Field(default=False, description="账号绑定是否已过户到目标实例")
    source_archived: bool = Field(default=False, description="源实例是否已归档")
    migration: Optional[InstanceMigrationResult] = Field(default=None, description="历史搬运结果；就地认领时为 null")
    message: str = Field(default="", description="结果摘要")


class UnclaimedInstanceItem(BaseModel):
    """一个已连入但无人认领的协议端

    `channel_count` 是这份清单里最要紧的一列：它直接回答「频道管理里空空如也的那些会话，
    是不是都挂在这个没人认领的实例底下」。
    """

    instance_key: str = Field(..., description="协议端自动登记时生成的实例标识")
    adapter_key: str = Field(..., description="适配器标识")
    display_name: str = Field(default="", description="自动登记时写入的显示名称")
    provider_account_id: str = Field(default="", description="协议端上报的账号（如 QQ 号）")
    channel_count: int = Field(default=0, description="已归到它名下的会话数")
    message_count_today: int = Field(default=0, description="今日消息数")
    last_active_at: Optional[str] = Field(default=None, description="最近活跃时间")
    first_seen_at: Optional[str] = Field(default=None, description="首次连入时间")
    dispatch_paused: bool = Field(
        default=False,
        description="认领之前它的消息会不会被丢弃（部署级令牌为空、曾对租户可见、或曝光记录不可用时为真）",
    )
    dispatch_paused_reason: str = Field(default="", description="消息暂不处理的原因，dispatch_paused 为假时为空")


class InstanceAdoptTarget(BaseModel):
    """可以作为「并入」目标的既有实例"""

    instance_key: str = Field(..., description="实例标识")
    display_name: str = Field(default="", description="显示名称")
    adapter_key: str = Field(..., description="适配器标识")
    provider_account_id: str = Field(default="", description="已绑定的账号；非空表示它已经有自己的账号")
    status: InstanceStatus = Field(default=InstanceStatus.OFFLINE, description="当前状态")
    channel_count: int = Field(default=0, description="名下会话数")


class UnclaimedInstanceListResponse(BaseModel):
    """未认领协议端清单与可选的并入目标

    两者一起下发是有意的：界面要在同一处同时给出「有 N 个协议端已连接但未认领」
    和「可以并进哪个既有实例」，分两次请求会让引导在网络抖动时只出现一半。
    """

    items: List[UnclaimedInstanceItem] = Field(default_factory=list)
    total: int = Field(default=0)
    adopt_targets: List[InstanceAdoptTarget] = Field(default_factory=list, description="可作为并入目标的既有实例")


class NetworkEntryType(str, Enum):
    """协议端网络连接类型

    取值直接对应 NapCat `onebot11.json` 里 `network` 下的数组名，避免多一层映射表。
    """

    WS_CLIENT = "websocketClients"  # 反向 WS：协议端主动连 NekroAgent（默认形态）
    WS_SERVER = "websocketServers"  # 正向 WS：由协议端监听，NekroAgent 主动连接
    HTTP_SERVER = "httpServers"  # HTTP 服务端：协议端监听，供外部调用
    HTTP_SSE_SERVER = "httpSseServers"  # HTTP + SSE 服务端
    HTTP_CLIENT = "httpClients"  # HTTP 上报：协议端把事件 POST 到指定地址


class NetworkEntry(BaseModel):
    """一条协议端网络连接配置

    不同类型用到的字段不同：client 形态用 `url`，server 形态用 `host` + `port`。
    未用到的字段保持默认值即可，写入配置时会按类型裁剪。
    """

    type: NetworkEntryType = Field(default=NetworkEntryType.WS_CLIENT, description="连接类型")
    name: str = Field(default="", description="连接名称，同类型内唯一")
    enable: bool = Field(default=True, description="是否启用")

    url: str = Field(default="", description="目标地址（client 形态）")
    host: str = Field(default="0.0.0.0", description="监听地址（server 形态）")
    port: int = Field(default=0, description="监听端口（server 形态）")

    token: str = Field(default="", description="鉴权令牌")
    message_post_format: str = Field(default="array", description="消息上报格式：array / string")
    report_self_message: bool = Field(default=False, description="是否上报自身消息")
    debug: bool = Field(default=False, description="调试模式")

    heart_interval_ms: int = Field(default=30000, description="心跳间隔（毫秒）")
    reconnect_interval_ms: int = Field(default=3000, description="重连间隔（毫秒）")
    enable_force_push_event: bool = Field(default=True, description="强制推送事件（HTTP 上报形态）")

    managed_by_nekro: bool = Field(
        default=False,
        description="是否由 NekroAgent 托管。托管条目的地址与令牌由后端生成，重写配置时会被重建；"
        "用户自行添加的条目不带该标记，重写时必须原样保留",
    )


class NetworkConfig(BaseModel):
    """协议端网络配置（写入 NapCat 的 onebot11 适配器配置文件）

    一个协议端可以同时开多条连接、多种类型——例如在连接 NekroAgent 的同时，
    再开一条正向 WS 供用户自己的工具接入。因此这里是一个列表而不是单条。

    NekroAgent 只托管其中带 `managed_by_nekro` 标记的那条（保证机器人能连上），
    其余条目属于用户，任何重写操作都必须原样保留。
    """

    entries: List[NetworkEntry] = Field(default_factory=list, description="全部网络连接配置")
    music_sign_url: str = Field(default="", description="音乐签名地址")
    enable_local_file_to_url: bool = Field(default=False, description="本地文件转 URL")
    parse_multi_msg: bool = Field(default=False, description="解析合并转发消息")


class LoginQrCodeResponse(BaseModel):
    """扫码登录信息"""

    available: bool = Field(default=False, description="当前是否有可用的二维码")
    qrcode_base64: str = Field(default="", description="二维码图片 base64（data URI）")
    qrcode_url: str = Field(default="", description="二维码内容 URL")
    message: str = Field(default="", description="附加说明，例如已登录 / 容器未运行")
    # 登录成功并已回写到本实例的账号号码。空串表示这一轮没有发生回写（还没登录、
    # 账号已被别的实例占用、或者本来就绑好了别的号），前端据此决定要不要报出号码。
    bound_account_id: str = Field(default="", description="本次已回写到该实例的协议端账号")


class ConsoleOverview(BaseModel):
    """总后台首页概览"""

    instance_total: int = Field(default=0)
    instance_online: int = Field(default=0)
    instance_offline: int = Field(default=0)
    # 「建好了但从未绑定账号」的实例。与 offline 分开计数，否则首页会把一堆从来没接上的
    # 空壳实例报成"离线"，管理员据此去查协议端，而协议端一切正常
    instance_unbound: int = Field(default=0, description="从未绑定账号的实例数")
    instance_unclaimed: int = Field(default=0)
    channel_total: int = Field(default=0)
    message_count_today: int = Field(default=0)
    docker_available: bool = Field(default=False, description="Docker 是否可用，决定能否托管内置容器")


class ActionOkResponse(BaseModel):
    """通用操作结果"""

    ok: bool = Field(default=True)
    message: str = Field(default="")


class InstanceAccessTokenRotateResponse(BaseModel):
    """更换实例接入令牌的结果

    新令牌只在这一次响应里给出：平台方本来就能从 `network-config` / `onebot-token` 读到它，
    这里只是省一次往返。`delivery` 说明它有没有送到协议端，`failed` 时 `delivery_error` 带原因
    （此时旧令牌已经失效、新令牌已经生效，只是协议端还没拿到）。
    """

    token: str = Field(..., description="服务端新生成的接入令牌，只在此响应里下发")
    disconnected: int = Field(default=0, description="凭旧令牌握手、已被断开的在线连接条数")
    delivery: Literal["restarted", "config_written", "rewired", "not_attached", "manual", "failed"] = Field(
        ...,
        description=(
            "新令牌的送达结果：restarted=内置 NapCat 已重启；config_written=容器未建、配置已重新下发；"
            "rewired=已写回 qbob；not_attached=qbob 里还没有接入 bot；manual=远程协议端需人工抄写；"
            "failed=送达失败"
        ),
    )
    delivery_error: str = Field(default="", description="送达失败的原因；空串表示没出错")


# ==================== 实例配额 ====================


class InstanceConcurrencyInfo(BaseModel):
    """实例的沙盒并发运行状况

    取自 `services/instance_concurrency.py` 的进程内调度器，重启即清零；
    它反映的是"此刻挤不挤"，不是历史账。
    """

    active: int = Field(default=0, description="正在占用的并发名额")
    queued: int = Field(default=0, description="正在排队等待名额的执行数")
    max_concurrent: int = Field(default=0, description="该实例的并发上限，0 表示不额外限制")
    queue_limit: int = Field(default=0, description="该实例的排队上限，0 表示不限制")
    admitted_total: int = Field(default=0, description="累计放行次数")
    rejected_total: int = Field(default=0, description="累计被拒次数（排队满 + 等待超时）")
    max_wait_seconds: float = Field(default=0.0, description="排队等待的最长耗时")


class InstanceQuotaInfo(BaseModel):
    """单个实例的配额视图

    `daily_limit` 是**每频道**每日 AI 回复上限（`AI_CHAT_DAILY_REPLY_LIMIT` 的语义），
    在实例层设置即对该实例的所有频道生效；0 表示不限制。
    """

    instance_key: str = Field(..., description="实例标识")
    display_name: str = Field(default="", description="显示名称")
    adapter_key: str = Field(..., description="适配器标识")
    tenant_id: Optional[int] = Field(default=None, description="所属租户ID；None 表示归平台方")
    enabled: bool = Field(default=True, description="实例是否启用")

    daily_limit: int = Field(default=0, description="当前生效的每频道每日回复上限，0 表示不限制")
    limit_source: str = Field(default="system", description="生效值来自哪一层：instance / adapter / system")
    inherited_limit: int = Field(default=0, description="清除实例层覆盖后会继承到的值")
    ceiling: Optional[int] = Field(default=None, description="平台方为该实例设定的上限；None 表示未设上限")

    replies_today: int = Field(default=0, description="今日该实例的 Bot 回复总数")
    active_channels: int = Field(default=0, description="今日有过回复的频道数")
    peak_channel_replies: int = Field(default=0, description="今日单频道最高回复数")
    channels_at_limit: int = Field(default=0, description="今日已撞到上限的频道数")

    concurrency: InstanceConcurrencyInfo = Field(default_factory=InstanceConcurrencyInfo, description="并发运行状况")


class InstanceQuotaListResponse(BaseModel):
    """配额列表"""

    items: List[InstanceQuotaInfo] = Field(default_factory=list)
    total: int = Field(default=0)
    editable_ceiling: bool = Field(default=False, description="当前身份能否设置平台方上限（仅平台方为真）")
    global_capacity: int = Field(default=0, description="全进程沙盒并发容量")
    global_active: int = Field(default=0, description="全进程正在占用的并发名额")
    global_queued: int = Field(default=0, description="全进程正在排队的执行数")


class InstanceQuotaUpdateRequest(BaseModel):
    """设置单个实例的配额"""

    daily_limit: Optional[int] = Field(
        default=None,
        description="每频道每日回复上限，0 表示不限制；传 null 表示清除实例层覆盖、回落到上层继承值",
    )
    clear_override: bool = Field(default=False, description="为真时忽略 daily_limit，直接清除实例层覆盖")
    ceiling: Optional[int] = Field(default=None, description="平台方上限；仅平台方可写，0 或负数表示取消上限")
    set_ceiling: bool = Field(default=False, description="为真时才处理 ceiling 字段，避免默认 null 被当成「取消上限」")


class InstanceQuotaBatchRequest(BaseModel):
    """批量设置多个实例的配额"""

    instance_keys: List[str] = Field(default_factory=list, description="目标实例标识列表")
    update: InstanceQuotaUpdateRequest = Field(..., description="要套用到每个实例的改动")


class InstanceQuotaBatchResponse(BaseModel):
    """批量设置结果

    逐个实例给出结论而不是整体成功/失败：批量操作里一个实例越权或写盘失败，
    不应该让其余已经生效的改动看起来也没发生。
    """

    ok: bool = Field(default=True)
    succeeded: List[str] = Field(default_factory=list, description="设置成功的实例")
    failed: List[str] = Field(default_factory=list, description="设置失败的实例")
    message: str = Field(default="")
