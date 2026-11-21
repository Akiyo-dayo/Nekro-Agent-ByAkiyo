/**
 * 输出过滤后台接口
 *
 * 两个端点都只读：命中统计（含被拒规则）与起步词表。作用域由 `adapterKey` /
 * `instanceKey` 决定，不传就是全局视图（平台方）。
 */

import axios from './axios'

/** 单条规则的命中统计 */
export interface OutputFilterRuleHit {
  /** `word` / `regex`，引擎异常时是 `error` */
  kind: string
  /** 管理员填写的规则原文 */
  rule: string
  count: number
  /** 最近一次命中的 ISO8601 UTC 时间 */
  last_hit_at: string | null
}

/** 一个实例的命中统计 */
export interface OutputFilterInstanceHits {
  adapter_key: string
  instance_key: string
  rules: OutputFilterRuleHit[]
}

/** 被拒规则：词表 / 正则编译时被挡下，或运行期因过慢被停用 */
export interface OutputFilterRejectedRule {
  rule: string
  reason: string
}

export interface OutputFilterHitsResponse {
  instances: OutputFilterInstanceHits[]
  rejected_rules: OutputFilterRejectedRule[]
}

export interface OutputFilterStarterWordsResponse {
  text: string
}

export interface OutputFilterHitsQuery {
  adapterKey?: string
  instanceKey?: string
}

export const outputFilterApi = {
  /** 命中统计与被拒规则 */
  getHits: async (params: OutputFilterHitsQuery = {}): Promise<OutputFilterHitsResponse> => {
    const query = new URLSearchParams()
    if (params.adapterKey) query.set('adapter_key', params.adapterKey)
    if (params.instanceKey) query.set('instance_key', params.instanceKey)
    const suffix = query.toString()
    const response = await axios.get<OutputFilterHitsResponse>(
      `/config/output-filter/hits${suffix ? `?${suffix}` : ''}`
    )
    return response.data
  },

  /** 起步词表全文（默认不启用，复制到过滤词里保存才生效） */
  getStarterWords: async (): Promise<OutputFilterStarterWordsResponse> => {
    const response = await axios.get<OutputFilterStarterWordsResponse>(
      '/config/output-filter/starter-words'
    )
    return response.data
  },
}
