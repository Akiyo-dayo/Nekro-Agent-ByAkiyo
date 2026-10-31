/**
 * 孤儿隔离规则运维 API 客户端（平台方专属）
 *
 * 契约对齐 `nekro_agent/routers/console/network_rules.py`。
 *
 * 两个端点严格分工：`GET /orphans` 只读盘点，`POST /orphans/cleanup` 才动链上的规则。
 * 合成一个的话，页面还没把「要删哪些规则、旧网桥上还有几个容器」显示给人看，
 * 规则就已经被一次 GET（预取、扫描器、链接预览）删掉了。
 *
 * 判定完全由后端做：前端**不复算** `verdict`，只负责把 `blockers` 原样显示出来。
 * 复算一份等于把「这个标记能不能删」的判据写成两份，而两份分叉时没有任何运行时信号。
 */

import axios from './axios'

const BASE = '/console/network-rules'

/** 链上一条规则的原文 */
export interface OrphanRuleLine {
  family: string
  chain: string
  /** `iptables -S` 读回来的原文，逐字展示 */
  text: string
}

/** 规则匹配的网桥上的容器情况 */
export interface OrphanRuleNetwork {
  interface: string
  /** 对应的 Docker 网络名；空表示宿主上没有容器引用它 */
  network: string
  /** 这张网上还有多少个容器（含已停止但 NetworkMode 仍钉在这张网上的） */
  container_total: number
  container_names: string[]
}

/** `self` = 本部署自用；`blocked` = 有实证或判不准；`orphan` = 可清理 */
export type OrphanRuleVerdict = 'self' | 'blocked' | 'orphan'

/** 一个标记的判定结果 */
export interface OrphanRuleCandidate {
  marker: string
  guard_label: string
  verdict: OrphanRuleVerdict
  rule_count: number
  chains: string[]
  rules: OrphanRuleLine[]
  networks: OrphanRuleNetwork[]
  container_total: number
  /** 拦住它的原因，verdict != orphan 时非空 */
  blockers: string[]
  /** 判定过程中的事实，不构成拦截 */
  notes: string[]
}

export interface OrphanRulePlan {
  scope: string
  scope_source: string
  available: boolean
  unavailable_reason: string
  candidates: OrphanRuleCandidate[]
}

export interface OrphanRuleCleanupResult {
  ok: boolean
  marker: string
  expected: number
  deleted: number
  residual: number
  failures: string[]
  message: string
}

export const CONSOLE_NETWORK_RULE_QUERY_KEYS = {
  orphans: ['console-network-rule-orphans'] as const,
}

export const consoleNetworkRulesApi = {
  /** 盘点链上的全部本项目标记，只读 */
  async listOrphans(): Promise<OrphanRulePlan> {
    const response = await axios.get<OrphanRulePlan>(`${BASE}/orphans`)
    return response.data
  },

  /**
   * 清理一个被判为孤儿的标记
   *
   * `confirm_marker` 必须与 `marker` 逐字相同——与认领 / 并入 / 历史归属迁移同一套回填确认。
   * 后端在执行前会**重跑一遍判定**，所以这里拿到 400 是正常结果而不是异常状态。
   */
  async cleanupOrphan(marker: string, confirmMarker: string): Promise<OrphanRuleCleanupResult> {
    const response = await axios.post<OrphanRuleCleanupResult>(`${BASE}/orphans/cleanup`, {
      marker,
      confirm_marker: confirmMarker,
    })
    return response.data
  },
}
