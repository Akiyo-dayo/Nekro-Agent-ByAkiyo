/**
 * 总后台（Console）API 客户端
 *
 * 本文件是前端与后端 `nekro_agent/schemas/console.py` 的唯一契约映射，
 * 所有类型定义均对齐该 Python 模块，后端契约变更时需同步修改此处。
 *
 * 三层模型：Console（总后台，全局唯一）-> Instance（实例，一个实例 = 一个 Bot 账号）-> Channel（会话）
 */

import axios from './axios'

/**
 * 实例背后的协议端形态
 *
 * `builtin_qbob`：附带的 qbob（QQ 官方机器人 → OneBot v11）里由 NA 托管的 bot，
 * 接入与运维见 `consoleQbob.ts`
 */
export const INSTANCE_PROVIDERS = ['builtin_napcat', 'remote_onebot', 'native', 'builtin_qbob'] as const
export type InstanceProvider = (typeof INSTANCE_PROVIDERS)[number]

/**
 * 实例健康状态
 *
 * `unbound` 与 `offline` 是**两件不同的事**，此前被合并成同一个红点：
 * 前者是「从来没绑过账号，因此从来没上线过」，下一步是去认领 / 绑定账号；
 * 后者是「绑过账号但连接断了」，下一步是去看协议端。两者对应的运维动作完全不同。
 */
export const INSTANCE_STATUSES = [
  'online',
  'connecting',
  'offline',
  'unbound',
  'login_required',
  'error',
  'unclaimed',
  'disabled',
  'archived',
] as const
export type InstanceStatus = (typeof INSTANCE_STATUSES)[number]

/** 协议端容器状态 */
export const CONTAINER_STATES = [
  'running',
  'exited',
  'created',
  'restarting',
  'paused',
  'absent',
] as const
export type ContainerState = (typeof CONTAINER_STATES)[number]

/** 协议端容器信息，仅 builtin_napcat 形态有值 */
export interface ContainerInfo {
  /** 容器名 */
  name: string
  /** 容器状态 */
  state: ContainerState
  /** 镜像 */
  image: string
  /** NapCat WebUI 宿主机端口 */
  webui_port: number | null
  /** 启动时间 */
  started_at: string | null
}

/** 实例的运行统计 */
export interface InstanceStats {
  /** 会话数 */
  channel_count: number
  /** 今日消息数 */
  message_count_today: number
  /** 最近活跃时间 */
  last_active_at: string | null
}

/** 实例概要 */
export interface InstanceInfo {
  /** 实例标识（稳定 slug，创建后不变） */
  instance_key: string
  /** 显示名称 */
  display_name: string
  /** 适配器标识 */
  adapter_key: string
  /** 协议端形态 */
  provider: InstanceProvider
  /** 绑定的平台账号（如 QQ 号） */
  provider_account_id: string
  /** 健康状态 */
  status: InstanceStatus
  /** 是否启用 */
  enabled: boolean
  /** 是否默认实例 */
  is_default: boolean
  /** 所属租户（多租户预留，当前恒为 null） */
  tenant_id: number | null
  /** 容器信息 */
  container: ContainerInfo | null
  /** 运行统计 */
  stats: InstanceStats
  /** 最近错误 */
  last_error: string
  /** 掉线起始时间 */
  offline_since: string | null
  /** 内置 qbob 实例已接入的 AppID；非空表示 qbob 里还挂着这个 bot */
  qbob_appid: string
}

/** 实例列表 */
export interface InstanceListResponse {
  items: InstanceInfo[]
  total: number
}

/**
 * 实例作用域切换器的候选条目（轻量）
 *
 * 是 `InstanceInfo` 的字段子集，**刻意不含 `container` 与 `stats`**：
 * 前者每个 builtin_napcat 实例要花一次 Docker inspect，后者是三条跨全表聚合，
 * 而切换器只需要「有哪些实例、叫什么、绑的哪个号、现在什么状态」。
 * 切换器挂在 MainLayout 上，每开一个后台标签页就要拉一次，大量实例的部署上这个差别不是常数级。
 */
export interface InstanceOption {
  instance_key: string
  display_name: string
  adapter_key: string
  /** 绑定的平台账号（如 QQ 号）；空串表示尚未绑定 */
  provider_account_id: string
  status: InstanceStatus
  enabled: boolean
}

/** 实例切换器候选列表 */
export interface InstanceOptionListResponse {
  items: InstanceOption[]
  total: number
}

/**
 * 新建实例
 *
 * `instance_key` 留空时由后端生成；一旦创建不可修改（它编码在所有 chat_key 里）。
 */
export interface InstanceCreateRequest {
  /** 实例标识，留空自动生成；仅小写字母数字下划线，最长 32 */
  instance_key?: string
  /** 显示名称 */
  display_name?: string
  /** 适配器标识 */
  adapter_key?: string
  /** 协议端形态 */
  provider?: InstanceProvider
  /** 已知的平台账号，可留空待登录后回填 */
  provider_account_id?: string
  /** 协议端 access token，留空自动生成 */
  access_token?: string
}

/** 更新实例的可变属性（instance_key 与 adapter_key 不可变） */
export interface InstanceUpdateRequest {
  display_name?: string
  enabled?: boolean
  is_default?: boolean
  provider_account_id?: string
}

/**
 * 删除实例
 *
 * 默认归档（数据只读保留）。彻底删除需要显式确认，且会级联清除会话、消息、记忆等全部数据。
 */
export interface InstanceDeleteRequest {
  /** 是否彻底删除数据；false 表示仅归档 */
  purge?: boolean
  /** 彻底删除时必须回填实例标识作为二次确认 */
  confirm_instance_key?: string
}

/**
 * 认领实例（协议端自行连入但尚未在后台登记）
 *
 * `instance_key` 是**认领后想要的标识**，不是那个自动生成的 `ob{账号}`。
 * 留空 = 就地认领（不搬运任何数据）；填了 = 建一个用该标识的实例并把协议端整体交给它
 * —— `instance_key` 编码在全部 chat_key 里，就地改列会让历史数据变孤儿。
 */
export interface InstanceClaimRequest {
  display_name?: string
  instance_key?: string
}

/** 把一个待认领协议端并入某个既有实例；路径上的实例是**接收方** */
export interface InstanceAdoptRequest {
  source_instance_key: string
  confirm_source_instance_key: string
}

/**
 * 认领的执行结果
 *
 * `ok=false` 表示这是一次**可重试的半成品**：部分会话迁移失败，因此账号没有过户、
 * 源实例也没有归档，重跑同一次操作即可补齐。
 */
export interface InstanceClaimResult {
  ok: boolean
  /** 认领后的实例（就地认领时即协议端自己那一行） */
  instance: InstanceInfo
  /** 被认领的协议端实例标识 */
  source_instance_key: string
  /** 账号绑定是否已过户 */
  account_moved: boolean
  /** 源实例是否已归档 */
  source_archived: boolean
  /** 历史搬运结果；就地认领时为 null */
  migration: InstanceMigrationResult | null
  message: string
}

/** 一个已连入但无人认领的协议端 */
export interface UnclaimedInstanceItem {
  /** 协议端自动登记时生成的实例标识，形如 ob10001 */
  instance_key: string
  adapter_key: string
  display_name: string
  /** 协议端上报的账号（如 QQ 号） */
  provider_account_id: string
  /** 已归到它名下的会话数——它回答「频道管理里找不到的会话在哪」 */
  channel_count: number
  message_count_today: number
  last_active_at: string | null
  first_seen_at: string | null
  /** 认领之前它的消息会不会被丢弃（部署级令牌为空、曾对租户可见、或曝光记录不可用） */
  dispatch_paused: boolean
  dispatch_paused_reason: string
}

/** 可以作为「并入」目标的既有实例 */
export interface InstanceAdoptTarget {
  instance_key: string
  display_name: string
  adapter_key: string
  /** 已绑定的账号；非空表示它已经有自己的账号，不能再接管别的 */
  provider_account_id: string
  status: InstanceStatus
  channel_count: number
}

/** 待认领协议端清单与可选的并入目标 */
export interface UnclaimedInstanceListResponse {
  items: UnclaimedInstanceItem[]
  total: number
  adopt_targets: InstanceAdoptTarget[]
}

/** 历史归属迁移里一个会话的去向 */
export interface InstanceMigrationChannelItem {
  /** 频道标识，形如 group_123456 */
  channel_id: string
  /** 频道名称 */
  channel_name: string
  /** 迁移前的会话标识 */
  source_chat_key: string
  /** 迁移后的会话标识 */
  target_chat_key: string
  /** 目标实例下已有同频道的行，本频道会被合并进去而不是整行搬过去 */
  conflict: boolean
  /** 该频道的历史消息数 */
  message_count: number
}

/** 历史归属迁移**不搬**的数据，连同原因一起展示 */
export interface InstanceMigrationResidueItem {
  resource: string
  count: number
  reason: string
}

/**
 * 历史归属迁移预览
 *
 * 迁移是破坏性且不可逆的，执行前必须先拿到本预览：`counts` 与执行结果用同一口径统计。
 */
export interface InstanceMigrationPreview {
  adapter_key: string
  source_instance_key: string
  source_display_name: string
  target_instance_key: string
  target_display_name: string
  /** 将要迁移的会话数 */
  channel_total: number
  /** 其中会与目标实例已有会话合并的数量 */
  conflict_total: number
  /** 各数据源将要改写的行数 */
  counts: Record<string, number>
  channels: InstanceMigrationChannelItem[]
  residues: InstanceMigrationResidueItem[]
  /** 执行前需要知道的风险提示 */
  warnings: string[]
}

/** 执行历史归属迁移；`confirm_source_instance_key` 必须回填源实例标识 */
export interface InstanceMigrationRequest {
  source_instance_key: string
  confirm_source_instance_key: string
}

/** 历史归属迁移的执行结果 */
export interface InstanceMigrationResult {
  /** 是否全部会话都已迁移 */
  ok: boolean
  source_instance_key: string
  target_instance_key: string
  /** 整行搬到目标实例的会话 */
  moved_channels: string[]
  /** 并进目标实例已有会话的会话 */
  merged_channels: string[]
  /** 迁移失败、仍留在源实例的会话；重跑一次即可补齐 */
  failed_channels: string[]
  counts: Record<string, number>
  /** 迁移后仍挂在源实例名下的行数，正常全为 0 */
  residual: Record<string, number>
  message: string
}

// 协议端网络配置已支持多条连接、多种类型，定义移到了 services/api/consoleContainer.ts
// （NetworkEntryType / NetworkEntry / NetworkConfig）。此处不再重复声明，避免同名类型并存误导调用方。

/** 扫码登录信息 */
export interface LoginQrCodeResponse {
  /** 当前是否有可用的二维码 */
  available: boolean
  /** 二维码图片 base64（data URI） */
  qrcode_base64: string
  /** 二维码内容 URL */
  qrcode_url: string
  /** 附加说明，例如已登录 / 容器未运行 */
  message: string
  /**
   * 本次已回写到该实例的协议端账号。
   *
   * 空串表示这一轮没有发生回写（还没登录、账号已被别的实例占用、或本来就绑着别的号），
   * 因此它只能用来「多说一句」，不能反过来当作「登录失败」的判据。
   */
  bound_account_id: string
}

/** 总后台首页概览 */
export interface ConsoleOverview {
  instance_total: number
  instance_online: number
  instance_offline: number
  /** 从未绑定账号的实例数；与 offline 分开计，否则空壳实例会被报成掉线 */
  instance_unbound: number
  instance_unclaimed: number
  channel_total: number
  message_count_today: number
  /** Docker 是否可用，决定能否托管内置容器 */
  docker_available: boolean
}

/** 通用操作结果 */
export interface ActionOkResponse {
  ok: boolean
  message: string
}

/** 新令牌的送达结果 */
export type AccessTokenDelivery =
  | 'restarted'
  | 'config_written'
  | 'rewired'
  | 'not_attached'
  | 'manual'
  | 'failed'

/**
 * 更换实例接入令牌的结果（平台方专属）
 *
 * 新令牌只在这条响应里下发；`delivery` 说明它有没有送到协议端，`failed` 时 `delivery_error`
 * 带原因——此时旧令牌已经失效、新令牌已经生效，只是协议端还没拿到。
 */
export interface InstanceAccessTokenRotateResponse {
  /** 服务端新生成的接入令牌 */
  token: string
  /** 凭旧令牌握手、已被断开的在线连接条数 */
  disconnected: number
  /** 新令牌的送达结果 */
  delivery: AccessTokenDelivery
  /** 送达失败的原因；空串表示没出错 */
  delivery_error: string
}

/** 单个实例的健康快照（含掉线时长） */
export interface InstanceHealthSnapshot {
  /** 适配器标识 */
  adapter_key: string
  /** 实例标识 */
  instance_key: string
  /** 实例显示名称 */
  display_name: string
  /** 当前健康状态 */
  status: InstanceStatus
  /** 是否启用 */
  enabled: boolean
  /** 是否纳入健康监测 */
  monitored: boolean
  /** 最近错误信息 */
  last_error: string
  /** 掉线起始时间（ISO 8601） */
  offline_since: string | null
  /** 已持续掉线时长（秒），在线时为 0 */
  offline_seconds: number
  /** 本次掉线是否已发出告警 */
  alerted: boolean
  /** 最近一次探活时间（ISO 8601） */
  last_check_at: string | null
  /** 最近一次探活是否成功 */
  last_probe_ok: boolean
  /** 连续探活失败次数 */
  consecutive_failures: number
  /** 该实例上生效的掉线告警延迟（秒），可被实例层覆盖 */
  alert_delay_seconds: number
}

/** 各实例健康状态 */
export interface InstanceHealthListResponse {
  items: InstanceHealthSnapshot[]
  total: number
  /** 当前探活周期（秒） */
  check_interval_seconds: number
  /** 全局掉线告警延迟（秒）；按实例生效的那份见 `InstanceHealthSnapshot.alert_delay_seconds` */
  alert_delay_seconds: number
}

/** 实例状态变更事件 */
export interface InstanceHealthEventItem {
  id: number
  adapter_key: string
  instance_key: string
  display_name: string
  event_type: string
  status_from: string
  status_to: string
  message: string
  /** 事件时间（ISO 8601） */
  create_time: string
}

/** 状态变更事件流水（分页） */
export interface InstanceHealthEventListResponse {
  items: InstanceHealthEventItem[]
  total: number
  page: number
  page_size: number
}

/** 健康事件流水查询参数 */
export interface InstanceHealthEventQuery {
  page?: number
  page_size?: number
  adapter_key?: string
  instance_key?: string
}

const CONSOLE_BASE = '/console'

const instancePath = (instanceKey: string) =>
  `${CONSOLE_BASE}/instances/${encodeURIComponent(instanceKey)}`

export const consoleApi = {
  /** 总后台首页概览 */
  async getOverview(): Promise<ConsoleOverview> {
    const response = await axios.get<ConsoleOverview>(`${CONSOLE_BASE}/overview`)
    return response.data
  },

  /** 实例列表 */
  async listInstances(): Promise<InstanceListResponse> {
    const response = await axios.get<InstanceListResponse>(`${CONSOLE_BASE}/instances`)
    return response.data
  },

  /**
   * 实例切换器候选列表（轻量，不含容器与统计）
   *
   * 路径不是 `/instances/options`：`options` 本身就是合法的 `instance_key`，
   * 挂在 `/instances/` 下会与 `/instances/{instance_key}` 互相遮蔽（后端同名注释）。
   */
  async listInstanceOptions(): Promise<InstanceOptionListResponse> {
    const response = await axios.get<InstanceOptionListResponse>(`${CONSOLE_BASE}/instance-options`)
    return response.data
  },

  /** 新建实例 */
  async createInstance(data: InstanceCreateRequest): Promise<InstanceInfo> {
    const response = await axios.post<InstanceInfo>(`${CONSOLE_BASE}/instances`, data)
    return response.data
  },

  /** 实例详情 */
  async getInstance(instanceKey: string): Promise<InstanceInfo> {
    const response = await axios.get<InstanceInfo>(instancePath(instanceKey))
    return response.data
  },

  /** 更新实例可变属性 */
  async updateInstance(instanceKey: string, data: InstanceUpdateRequest): Promise<InstanceInfo> {
    const response = await axios.patch<InstanceInfo>(instancePath(instanceKey), data)
    return response.data
  },

  /** 删除实例（默认归档，purge=true 为彻底删除） */
  async deleteInstance(
    instanceKey: string,
    data: InstanceDeleteRequest
  ): Promise<ActionOkResponse> {
    const response = await axios.delete<ActionOkResponse>(instancePath(instanceKey), { data })
    return response.data
  },

  /** 启用实例 */
  async enableInstance(instanceKey: string): Promise<ActionOkResponse> {
    const response = await axios.post<ActionOkResponse>(`${instancePath(instanceKey)}/enable`)
    return response.data
  },

  /** 停用实例 */
  async disableInstance(instanceKey: string): Promise<ActionOkResponse> {
    const response = await axios.post<ActionOkResponse>(`${instancePath(instanceKey)}/disable`)
    return response.data
  },

  /**
   * 待认领协议端清单（平台方专属）
   *
   * 路径不是 `/instances/unclaimed`：与 `/instance-options` 同一条理由，
   * `unclaimed` 本身就是合法的 `instance_key`。
   */
  async listUnclaimedInstances(): Promise<UnclaimedInstanceListResponse> {
    const response = await axios.get<UnclaimedInstanceListResponse>(
      `${CONSOLE_BASE}/unclaimed-instances`
    )
    return response.data
  },

  /**
   * 认领为新实例（出路一）
   *
   * 路径上的是**被认领的协议端**；`data.instance_key` 留空即就地认领。
   */
  async claimInstance(instanceKey: string, data: InstanceClaimRequest): Promise<InstanceClaimResult> {
    const response = await axios.post<InstanceClaimResult>(
      `${instancePath(instanceKey)}/claim`,
      data
    )
    return response.data
  },

  /**
   * 并入已有实例（出路二）
   *
   * 路径上的是**接收方**（与历史归属迁移同一形态："把某个东西并到本实例来"）。
   */
  async adoptInstance(
    targetInstanceKey: string,
    data: InstanceAdoptRequest
  ): Promise<InstanceClaimResult> {
    const response = await axios.post<InstanceClaimResult>(
      `${instancePath(targetInstanceKey)}/adopt`,
      data
    )
    return response.data
  },

  /**
   * 预览把某个实例的历史归属迁到目标实例会改动什么（只读，平台方专属）
   *
   * 路径上的是**目标**实例（"把某个实例的历史并到本实例来"），源实例走查询参数。
   */
  async previewInstanceMigration(
    targetInstanceKey: string,
    sourceInstanceKey: string
  ): Promise<InstanceMigrationPreview> {
    const response = await axios.get<InstanceMigrationPreview>(
      `${instancePath(targetInstanceKey)}/migration/preview`,
      { params: { source_instance_key: sourceInstanceKey } }
    )
    return response.data
  },

  /** 执行历史归属迁移（破坏性且不可逆，平台方专属） */
  async migrateInstanceHistory(
    targetInstanceKey: string,
    data: InstanceMigrationRequest
  ): Promise<InstanceMigrationResult> {
    const response = await axios.post<InstanceMigrationResult>(
      `${instancePath(targetInstanceKey)}/migration`,
      data
    )
    return response.data
  },

  /** 各实例健康状态与掉线时长 */
  async getInstanceHealth(): Promise<InstanceHealthListResponse> {
    const response = await axios.get<InstanceHealthListResponse>(`${CONSOLE_BASE}/health/instances`)
    return response.data
  },

  /** 状态变更事件流水（分页） */
  async getHealthEvents(
    query: InstanceHealthEventQuery = {}
  ): Promise<InstanceHealthEventListResponse> {
    const response = await axios.get<InstanceHealthEventListResponse>(
      `${CONSOLE_BASE}/health/events`,
      { params: query }
    )
    return response.data
  },

  /**
   * 更换实例自己的 OneBot 接入令牌（平台方专属）
   *
   * 旧令牌当场失效、凭它握手的在线连接被断开；新令牌只由服务端生成，随响应返回。
   * 这是一次会重启容器 / 重新接线的写操作，失败也不回滚（响应里的 `delivery` 说明结果）。
   */
  async rotateAccessToken(instanceKey: string): Promise<InstanceAccessTokenRotateResponse> {
    const response = await axios.post<InstanceAccessTokenRotateResponse>(
      `${instancePath(instanceKey)}/access-token/rotate`
    )
    return response.data
  },
}

/** 实例详情页的主视图 Tab（主视图进 path，不使用 query） */
export const CONSOLE_INSTANCE_DETAIL_TABS = ['overview', 'protocol', 'events'] as const
export type ConsoleInstanceDetailTab = (typeof CONSOLE_INSTANCE_DETAIL_TABS)[number]

export const DEFAULT_CONSOLE_INSTANCE_DETAIL_TAB: ConsoleInstanceDetailTab = 'overview'

export const isConsoleInstanceDetailTab = (
  value: string | null | undefined
): value is ConsoleInstanceDetailTab =>
  !!value && CONSOLE_INSTANCE_DETAIL_TABS.includes(value as ConsoleInstanceDetailTab)

/** Console 页面路径（跨页跳转统一使用，禁止散落硬编码拼接） */
export const consolePaths = {
  home: () => '/console',
  instanceList: () => '/console/instances',
  instanceCreate: () => '/console/instances/new',
  instanceDetail: (
    instanceKey: string,
    tab: ConsoleInstanceDetailTab = DEFAULT_CONSOLE_INSTANCE_DETAIL_TAB
  ) => `/console/instances/${encodeURIComponent(instanceKey)}/${tab}`,
}

/**
 * 该状态是否表示实例当前不可用（用于列表高亮与告警）
 *
 * 刻意不含 `unbound`：它不是"掉了"而是"还没接上"，用告警色标它会把
 * 「你还没做配置」显示成「你的 Bot 出故障了」，正是本轮要修的那条误导。
 */
export const isInstanceOffline = (status: InstanceStatus): boolean =>
  status === 'offline' || status === 'error' || status === 'login_required'

/** 实例是否处于「等着管理员做一件事」的状态，界面要给可执行的提示而不是一个红点 */
export const isInstanceActionable = (status: InstanceStatus): boolean =>
  status === 'unbound' || status === 'unclaimed' || status === 'login_required'

/** 计算掉线时长（分钟），未掉线或无起始时间返回 null */
export const getOfflineMinutes = (offlineSince: string | null | undefined): number | null => {
  if (!offlineSince) return null
  const startedAt = new Date(offlineSince).getTime()
  if (Number.isNaN(startedAt)) return null
  const elapsed = Date.now() - startedAt
  if (elapsed <= 0) return 0
  return Math.floor(elapsed / 60_000)
}
