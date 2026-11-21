/**
 * 频道每日额度 API 客户端
 *
 * 契约对齐 `nekro_agent/schemas/channel_quota.py` 与 `nekro_agent/routers/console/channel_quota.py`。
 *
 * 两组写接口是**两组模型**，不是同一组字段加一层校验：
 * - 平台方字段（开关、单位、额度、超额动作、提示语）只有平台方能写，请求体里也根本没有
 *   `tenant_*` 那几列；
 * - 租户只能写自己那一组（「我的额度」），写进去也只是「在平台方给的额度以内再调低」。
 *
 * 越权一律 **404**（不是 403）：403 会把平台方的实例与频道清单变成可枚举的。
 * 页面上「换个会话标识试试」拿到的与「这个频道不存在」不可区分，这是**有意的**。
 */

import axios from './axios'

const CONSOLE_CHANNEL_QUOTA_BASE = '/console/channel-quota'

/** 额度单位：回复次数 / token */
export type ChannelQuotaUnit = 'replies' | 'tokens'

/** 超额动作：只告警 / 暂停 */
export type ChannelQuotaAction = 'alert' | 'pause'

/**
 * 一个频道当天的状态
 *
 * `state` 的六个取值与后端状态机一一对应，前端按它分支显示标签；
 * `disabled` 时 `used` 仍是真实计数（显示用量不依赖开关）。
 */
export type ChannelQuotaState =
  | 'disabled'
  | 'unlimited'
  | 'normal'
  | 'warning'
  | 'exceeded'
  | 'paused'

/** 列表与单频道状态共用的一份（后端是同一个模型） */
export interface ChannelQuotaDailyItem {
  chat_key: string
  channel_name: string
  chat_type: string
  adapter_key: string
  instance_key: string
  tenant_id: number | null
  tenant_name: string
  unit: ChannelQuotaUnit
  used: number
  /** 生效额度；0 表示不限 */
  limit: number
  /** 平台方当天的临时加额 */
  bonus: number
  /** 剩余；`limit` 为 0（不限）时是 null */
  remaining: number | null
  /** 用量占「额度 + 加额」的百分比；不限时为 0，可以超过 100 */
  percent: number
  state: ChannelQuotaState
  action: ChannelQuotaAction
}

export interface ChannelQuotaDailyListResponse {
  total: number
  page: number
  page_size: number
  /** 实际查询的日期键（额度时区下的 YYYY-MM-DD） */
  day: string
  items: ChannelQuotaDailyItem[]
}

export interface ChannelQuotaHistoryDay {
  day: string
  replies: number
  tokens_all: number
  tokens_reply: number
  bonus: number
}

export interface ChannelQuotaSceneUsage {
  scene: string
  token_total: number
}

/** 历史里附带的「此刻生效的策略」，只读 */
export interface ChannelQuotaPolicySummary {
  unit: ChannelQuotaUnit
  platform_limit: number
  tenant_limit: number | null
  effective_limit: number
  over_limit_action: ChannelQuotaAction
  pause_message: string
}

export interface ChannelQuotaHistoryResponse {
  chat_key: string
  days: ChannelQuotaHistoryDay[]
  by_scene: ChannelQuotaSceneUsage[]
  policy: ChannelQuotaPolicySummary
}

/** 一层策略的完整内容（读取用）；NULL = 继承上一层 */
export interface ChannelQuotaPolicyInfo {
  scope: 'global' | 'instance' | 'channel' | string
  adapter_key: string
  instance_key: string
  chat_key: string
  enabled: boolean | null
  unit: ChannelQuotaUnit | null
  group_daily_limit: number | null
  private_daily_limit: number | null
  over_limit_action: ChannelQuotaAction | null
  pause_message: string | null
  updated_by: string
  update_time: string | null
}

/**
 * 全局策略的写入
 *
 * 全局是最后一层：开关、单位、超额动作不允许留空（后端模型上就不是可空字段）。
 */
export interface GlobalQuotaPolicyUpdate {
  enabled: boolean
  unit: ChannelQuotaUnit
  group_daily_limit?: number | null
  private_daily_limit?: number | null
  over_limit_action: ChannelQuotaAction
  pause_message?: string | null
}

/** 实例层与频道层的写入：每一列都可以留空表示「跟上一层」 */
export interface ScopedQuotaPolicyUpdate {
  enabled?: boolean | null
  unit?: ChannelQuotaUnit | null
  group_daily_limit?: number | null
  private_daily_limit?: number | null
  over_limit_action?: ChannelQuotaAction | null
  pause_message?: string | null
}

export interface TenantInstanceQuotaInfo {
  adapter_key: string
  instance_key: string
  unit: ChannelQuotaUnit
  group_daily_limit: number | null
  private_daily_limit: number | null
  pause_message: string | null
  /** 平台方给的群额度；0 表示不限 */
  platform_group_daily_limit: number
  /** 平台方给的私聊额度；0 表示不限 */
  platform_private_daily_limit: number
}

export interface TenantInstanceQuotaUpdate {
  group_daily_limit?: number | null
  private_daily_limit?: number | null
  pause_message?: string | null
}

export interface TenantChannelQuotaInfo {
  chat_key: string
  chat_type: string
  unit: ChannelQuotaUnit
  daily_limit: number | null
  pause_message: string | null
  platform_limit: number
  effective_limit: number
}

export interface TenantChannelQuotaUpdate {
  daily_limit?: number | null
  pause_message?: string | null
}

export interface ChannelQuotaBonusRequest {
  /** 正整数 */
  amount: number
}

export interface ChannelQuotaBonusResponse {
  chat_key: string
  day: string
  bonus: number
  status: ChannelQuotaDailyItem
}

/** 按账本重算某一天之后的回执：只报哪一天、动了几个频道 */
export interface ChannelQuotaRecountResponse {
  day: string
  channels: number
}

/** 频道列表的筛选条件；空串表示不过滤 */
export interface ChannelQuotaDailyFilters {
  /** 日期键 YYYY-MM-DD；留空表示今天（按额度时区） */
  day?: string
  state?: ChannelQuotaState | ''
  chatType?: 'group' | 'private' | ''
  /** 租户作用域；只有平台方有意义 */
  tenantId?: number | null
  page?: number
  pageSize?: number
}

export const CONSOLE_CHANNEL_QUOTA_QUERY_KEYS = {
  globalPolicy: ['console-channel-quota-policy-global'] as const,
  instancePolicy: (adapterKey: string, instanceKey: string) =>
    ['console-channel-quota-policy-instance', adapterKey, instanceKey] as const,
  channelPolicy: (chatKey: string) =>
    ['console-channel-quota-policy-channel', chatKey] as const,
  daily: (filters: ChannelQuotaDailyFilters) =>
    ['console-channel-quota-daily', filters] as const,
  status: (chatKey: string) => ['console-channel-quota-status', chatKey] as const,
  history: (chatKey: string, days: number) =>
    ['console-channel-quota-history', chatKey, days] as const,
  tenantInstance: (adapterKey: string, instanceKey: string) =>
    ['console-channel-quota-tenant-instance', adapterKey, instanceKey] as const,
  tenantChannel: (chatKey: string) =>
    ['console-channel-quota-tenant-channel', chatKey] as const,
}

const instancePath = (adapterKey: string, instanceKey: string) =>
  `${encodeURIComponent(adapterKey)}/${encodeURIComponent(instanceKey)}`

export const consoleChannelQuotaApi = {
  /** 频道列表（服务端分页，按用量百分比倒序） */
  async listDaily(filters: ChannelQuotaDailyFilters = {}): Promise<ChannelQuotaDailyListResponse> {
    const response = await axios.get<ChannelQuotaDailyListResponse>(`${CONSOLE_CHANNEL_QUOTA_BASE}/daily`, {
      params: {
        ...(filters.day ? { day: filters.day } : {}),
        ...(filters.state ? { state: filters.state } : {}),
        ...(filters.chatType ? { chat_type: filters.chatType } : {}),
        ...(filters.tenantId !== undefined && filters.tenantId !== null
          ? { tenant_id: filters.tenantId }
          : {}),
        page: filters.page ?? 1,
        page_size: filters.pageSize ?? 50,
      },
    })
    return response.data
  },

  /** 单个频道今天的状态（频道详情页顶部那一行） */
  async getStatus(chatKey: string): Promise<ChannelQuotaDailyItem> {
    const response = await axios.get<ChannelQuotaDailyItem>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/channels/${encodeURIComponent(chatKey)}/status`
    )
    return response.data
  },

  /** 逐天明细 + 按场景的 token 合计 + 此刻生效的策略 */
  async getHistory(chatKey: string, days = 7): Promise<ChannelQuotaHistoryResponse> {
    const response = await axios.get<ChannelQuotaHistoryResponse>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/channels/${encodeURIComponent(chatKey)}/history`,
      { params: { days } }
    )
    return response.data
  },

  /** 今天临时加额（平台方） */
  async addBonus(chatKey: string, amount: number): Promise<ChannelQuotaBonusResponse> {
    const response = await axios.post<ChannelQuotaBonusResponse>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/channels/${encodeURIComponent(chatKey)}/bonus`,
      { amount } satisfies ChannelQuotaBonusRequest
    )
    return response.data
  },

  /** 按账本重算某个频道某一天的 token（平台方）；回复次数不重算 */
  async recountChannel(chatKey: string, day?: string): Promise<ChannelQuotaDailyItem> {
    const response = await axios.post<ChannelQuotaDailyItem>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/channels/${encodeURIComponent(chatKey)}/recount`,
      null,
      { params: day ? { day } : {} }
    )
    return response.data
  },

  /** 按账本重算某一天所有频道的 token（平台方）；回复次数不重算 */
  async recountAll(day?: string): Promise<ChannelQuotaRecountResponse> {
    const response = await axios.post<ChannelQuotaRecountResponse>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/recount`,
      null,
      { params: day ? { day } : {} }
    )
    return response.data
  },

  async getGlobalPolicy(): Promise<ChannelQuotaPolicyInfo> {
    const response = await axios.get<ChannelQuotaPolicyInfo>(`${CONSOLE_CHANNEL_QUOTA_BASE}/policy`)
    return response.data
  },

  async updateGlobalPolicy(body: GlobalQuotaPolicyUpdate): Promise<ChannelQuotaPolicyInfo> {
    const response = await axios.put<ChannelQuotaPolicyInfo>(`${CONSOLE_CHANNEL_QUOTA_BASE}/policy`, body)
    return response.data
  },

  async getInstancePolicy(adapterKey: string, instanceKey: string): Promise<ChannelQuotaPolicyInfo> {
    const response = await axios.get<ChannelQuotaPolicyInfo>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/policy/instance/${instancePath(adapterKey, instanceKey)}`
    )
    return response.data
  },

  async updateInstancePolicy(
    adapterKey: string,
    instanceKey: string,
    body: ScopedQuotaPolicyUpdate
  ): Promise<ChannelQuotaPolicyInfo> {
    const response = await axios.put<ChannelQuotaPolicyInfo>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/policy/instance/${instancePath(adapterKey, instanceKey)}`,
      body
    )
    return response.data
  },

  async getChannelPolicy(chatKey: string): Promise<ChannelQuotaPolicyInfo> {
    const response = await axios.get<ChannelQuotaPolicyInfo>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/policy/channel/${encodeURIComponent(chatKey)}`
    )
    return response.data
  },

  async updateChannelPolicy(
    chatKey: string,
    body: ScopedQuotaPolicyUpdate
  ): Promise<ChannelQuotaPolicyInfo> {
    const response = await axios.put<ChannelQuotaPolicyInfo>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/policy/channel/${encodeURIComponent(chatKey)}`,
      body
    )
    return response.data
  },

  async getTenantInstanceQuota(adapterKey: string, instanceKey: string): Promise<TenantInstanceQuotaInfo> {
    const response = await axios.get<TenantInstanceQuotaInfo>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/tenant/instance/${instancePath(adapterKey, instanceKey)}`
    )
    return response.data
  },

  async updateTenantInstanceQuota(
    adapterKey: string,
    instanceKey: string,
    body: TenantInstanceQuotaUpdate
  ): Promise<TenantInstanceQuotaInfo> {
    const response = await axios.put<TenantInstanceQuotaInfo>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/tenant/instance/${instancePath(adapterKey, instanceKey)}`,
      body
    )
    return response.data
  },

  async getTenantChannelQuota(chatKey: string): Promise<TenantChannelQuotaInfo> {
    const response = await axios.get<TenantChannelQuotaInfo>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/tenant/channel/${encodeURIComponent(chatKey)}`
    )
    return response.data
  },

  async updateTenantChannelQuota(
    chatKey: string,
    body: TenantChannelQuotaUpdate
  ): Promise<TenantChannelQuotaInfo> {
    const response = await axios.put<TenantChannelQuotaInfo>(
      `${CONSOLE_CHANNEL_QUOTA_BASE}/tenant/channel/${encodeURIComponent(chatKey)}`,
      body
    )
    return response.data
  },
}
