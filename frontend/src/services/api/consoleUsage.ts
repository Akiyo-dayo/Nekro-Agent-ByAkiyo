/**
 * 总后台 LLM 用量与费用 API 客户端
 *
 * 契约对齐 `nekro_agent/routers/console/usage.py`。
 *
 * 三条读接口共用同一组筛选参数（实例 / 租户 / 时间窗），作用域由后端按当前身份收敛：
 * 平台方看全部，租户只看自己名下的行，越权一律 **404**（不是 403）——所以页面上
 * "换个实例标识试试"这种操作拿到的与"不存在"不可区分，这是**有意的**。
 *
 * ⚠️ `model_group_name` 可能是后端替换过的占位串（租户看不到的平台模型组），
 * 前端**不要**拿它去反查模型组详情，它未必对应任何一行真实数据。
 */

import axios from './axios'

const CONSOLE_USAGE_BASE = '/console/usage'

/** 一组聚合出来的量；五个数值列在每一档里都是同一个口径 */
export interface UsageMetrics {
  /** 请求条数（含失败与中断） */
  records: number
  /** 输入 token 合计 */
  token_input: number
  /** 输入里命中上游缓存的 token 合计 */
  token_input_cached: number
  /** 输出 token 合计 */
  token_output: number
  /** 上游给出的总 token 合计 */
  token_total: number
  /** 按**当时**倍率折算的费用合计 */
  cost: number
}

/** 某个维度下的一档 */
export interface UsageBreakdownItem extends UsageMetrics {
  /** 该分档的取值；空串表示该维度缺失（例如归不了属的那些行没有实例标识） */
  key: string
}

export interface UsageSummary {
  start_time: string
  end_time: string
  total: UsageMetrics
  by_instance: UsageBreakdownItem[]
  by_tenant: UsageBreakdownItem[]
  by_model_group: UsageBreakdownItem[]
  by_model: UsageBreakdownItem[]
  by_scene: UsageBreakdownItem[]
  by_status: UsageBreakdownItem[]
}

export interface UsageTrendPoint extends UsageMetrics {
  /** 桶的起点（ISO 8601） */
  bucket_start: string
}

export interface UsageTrend {
  start_time: string
  end_time: string
  bucket_hours: number
  points: UsageTrendPoint[]
}

/** 一条流水明细 */
export interface UsageRecord {
  id: number
  create_time: string
  adapter_key: string
  instance_key: string
  /** 记忆域的字符串编码（`g:<组ID>` / `i:<适配器>:<实例>`） */
  domain_id: string
  tenant_id: number | null
  chat_key: string
  person_uid: string
  preset_id: number
  /** 租户看不到的平台模型组在这里是占位串，见文件头 */
  model_group_name: string
  model_name: string
  stream_mode: boolean
  token_input: number
  /** 输入里命中上游缓存的 token 数（上游不回时为 0） */
  token_input_cached: number
  token_output: number
  token_total: number
  cost: number
  scene: string
  status: string
  /** 异常类名；**不含任何上游原文** */
  error_type: string
  /** 归一化后的上游收尾原因，取自固定集合 */
  finish_reason: string
  attempt_index: number
  attempt_total: number
  is_fallback: boolean
  generation_time_ms: number
}

export interface UsageRecordList {
  total: number
  items: UsageRecord[]
}

export interface UsageEnums {
  scenes: string[]
  statuses: string[]
  max_window_days: number
  max_trend_buckets: number
}

/** 三条读接口共用的筛选条件 */
export interface UsageFilters {
  /** 实例作用域；留空表示当前身份可见的全部实例 */
  instanceKey?: string
  /** 租户作用域；留空时平台方表示全部租户，租户身份恒为自己 */
  tenantId?: number | null
  /** 起始时间（ISO 8601，含） */
  startTime?: string
  /** 结束时间（ISO 8601，不含） */
  endTime?: string
}

const toParams = (filters: UsageFilters): Record<string, string | number> => {
  const params: Record<string, string | number> = {}
  if (filters.instanceKey) params.instance_key = filters.instanceKey
  if (filters.tenantId !== undefined && filters.tenantId !== null) params.tenant_id = filters.tenantId
  if (filters.startTime) params.start_time = filters.startTime
  if (filters.endTime) params.end_time = filters.endTime
  return params
}

export const CONSOLE_USAGE_QUERY_KEYS = {
  enums: ['console-usage-enums'] as const,
  summary: (filters: UsageFilters) => ['console-usage-summary', filters] as const,
  trend: (filters: UsageFilters, bucketHours: number) =>
    ['console-usage-trend', filters, bucketHours] as const,
  records: (filters: UsageFilters, scene: string, status: string, page: number, pageSize: number) =>
    ['console-usage-records', filters, scene, status, page, pageSize] as const,
}

export const consoleUsageApi = {
  /** 筛选器的固定取值（场景 / 收尾形态枚举 + 两个上限常量） */
  async getEnums(): Promise<UsageEnums> {
    const response = await axios.get<UsageEnums>(`${CONSOLE_USAGE_BASE}/enums`)
    return response.data
  },

  /** 按实例 / 租户 / 模型组 / 模型 / 场景 / 收尾形态六个维度分档汇总 */
  async getSummary(filters: UsageFilters): Promise<UsageSummary> {
    const response = await axios.get<UsageSummary>(`${CONSOLE_USAGE_BASE}/summary`, {
      params: toParams(filters),
    })
    return response.data
  },

  /** 时间序列 */
  async getTrend(filters: UsageFilters, bucketHours = 24): Promise<UsageTrend> {
    const response = await axios.get<UsageTrend>(`${CONSOLE_USAGE_BASE}/trend`, {
      params: { ...toParams(filters), bucket_hours: bucketHours },
    })
    return response.data
  },

  /** 逐条明细：出账要对得上号，聚合数字之外必须有可核对的原始行 */
  async listRecords(
    filters: UsageFilters,
    options: { scene?: string; status?: string; page?: number; pageSize?: number } = {}
  ): Promise<UsageRecordList> {
    const response = await axios.get<UsageRecordList>(`${CONSOLE_USAGE_BASE}/records`, {
      params: {
        ...toParams(filters),
        ...(options.scene ? { scene: options.scene } : {}),
        ...(options.status ? { status: options.status } : {}),
        page: options.page ?? 1,
        page_size: options.pageSize ?? 50,
      },
    })
    return response.data
  },
}
