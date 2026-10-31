/**
 * 总后台实例配额 API 客户端
 *
 * 契约对齐 `nekro_agent/routers/console/quota.py`。配额的语义是**每频道每日 AI 回复上限**，
 * 在实例层设置即对该实例的所有频道生效，0 表示不限制。
 *
 * 权限边界由后端裁决，前端只负责如实呈现：`editable_ceiling` 决定平台上限列能不能改，
 * 越权与超上限一律由后端返回 403/404，页面直接展示 `ApiError.message`。
 */

import axios from './axios'

const CONSOLE_QUOTA_BASE = '/console/quota'

/** 实例的沙盒并发运行状况；取自进程内调度器，重启即清零 */
export interface InstanceConcurrencyInfo {
  /** 正在占用的并发名额 */
  active: number
  /** 正在排队等待名额的执行数 */
  queued: number
  /** 该实例的并发上限，0 表示不额外限制 */
  max_concurrent: number
  /** 该实例的排队上限，0 表示不限制 */
  queue_limit: number
  /** 累计放行次数 */
  admitted_total: number
  /** 累计被拒次数（排队满 + 等待超时） */
  rejected_total: number
  /** 排队等待的最长耗时（秒） */
  max_wait_seconds: number
}

/** 配额生效值来自哪一层 */
export type InstanceQuotaLimitSource = 'instance' | 'adapter' | 'system'

/** 单个实例的配额视图 */
export interface InstanceQuotaInfo {
  /** 实例标识 */
  instance_key: string
  /** 显示名称 */
  display_name: string
  /** 适配器标识 */
  adapter_key: string
  /** 所属租户ID；null 表示归平台方 */
  tenant_id: number | null
  /** 实例是否启用 */
  enabled: boolean
  /** 当前生效的每频道每日回复上限，0 表示不限制 */
  daily_limit: number
  /** 生效值来自哪一层 */
  limit_source: InstanceQuotaLimitSource
  /** 清除实例层覆盖后会继承到的值 */
  inherited_limit: number
  /** 平台方为该实例设定的上限；null 表示未设上限 */
  ceiling: number | null
  /** 今日该实例的 Bot 回复总数 */
  replies_today: number
  /** 今日有过回复的频道数 */
  active_channels: number
  /** 今日单频道最高回复数 */
  peak_channel_replies: number
  /** 今日已撞到上限的频道数 */
  channels_at_limit: number
  /** 并发运行状况 */
  concurrency: InstanceConcurrencyInfo
}

/** 配额列表 */
export interface InstanceQuotaListResponse {
  /** 当前身份可见的实例配额 */
  items: InstanceQuotaInfo[]
  /** 条目总数 */
  total: number
  /** 当前身份能否设置平台方上限（仅平台方为真） */
  editable_ceiling: boolean
  /** 全进程沙盒并发容量 */
  global_capacity: number
  /** 全进程正在占用的并发名额 */
  global_active: number
  /** 全进程正在排队的执行数 */
  global_queued: number
}

/** 设置单个实例的配额 */
export interface InstanceQuotaUpdateRequest {
  /** 每频道每日回复上限，0 表示不限制；null 且未要求清除覆盖时表示不动配额 */
  daily_limit?: number | null
  /** 为真时忽略 daily_limit，直接清除实例层覆盖、回落到上层继承值 */
  clear_override?: boolean
  /** 平台方上限；仅平台方可写，0 或负数表示取消上限 */
  ceiling?: number | null
  /** 为真时才处理 ceiling 字段，避免默认 null 被当成「取消上限」 */
  set_ceiling?: boolean
}

/** 批量设置多个实例的配额 */
export interface InstanceQuotaBatchRequest {
  /** 目标实例标识列表 */
  instance_keys: string[]
  /** 要套用到每个实例的改动 */
  update: InstanceQuotaUpdateRequest
}

/** 批量设置结果；逐个实例给出结论，一个失败不影响其余已生效的改动 */
export interface InstanceQuotaBatchResponse {
  /** 是否全部成功 */
  ok: boolean
  /** 设置成功的实例 */
  succeeded: string[]
  /** 设置失败的实例 */
  failed: string[]
  /** 结果摘要，直接展示给操作者 */
  message: string
}

export const CONSOLE_QUOTA_QUERY_KEYS = {
  list: ['console-quota-instances'] as const,
}

export const consoleQuotaApi = {
  /** 按实例列出当前配额、今日已用量与并发运行状况 */
  async listInstanceQuotas(): Promise<InstanceQuotaListResponse> {
    const response = await axios.get<InstanceQuotaListResponse>(`${CONSOLE_QUOTA_BASE}/instances`)
    return response.data
  },

  /** 设置单个实例的每日回复上限，以及（仅平台方）平台方上限 */
  async updateInstanceQuota(
    instanceKey: string,
    payload: InstanceQuotaUpdateRequest
  ): Promise<InstanceQuotaInfo> {
    const response = await axios.patch<InstanceQuotaInfo>(
      `${CONSOLE_QUOTA_BASE}/instances/${encodeURIComponent(instanceKey)}`,
      payload
    )
    return response.data
  },

  /** 把同一份改动套用到多个实例 */
  async batchUpdateInstanceQuota(
    payload: InstanceQuotaBatchRequest
  ): Promise<InstanceQuotaBatchResponse> {
    const response = await axios.post<InstanceQuotaBatchResponse>(
      `${CONSOLE_QUOTA_BASE}/instances/batch`,
      payload
    )
    return response.data
  },
}
