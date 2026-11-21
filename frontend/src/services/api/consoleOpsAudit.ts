/**
 * 总后台运维审计 API 客户端
 *
 * 契约对齐 `nekro_agent/routers/console/ops_audit.py`。这一页只读：审计行写入后不提供
 * 修改入口；参数在入库前已经脱敏限长，前端只负责原样展示。
 *
 * 权限边界由后端裁决（整条路由是 `require_platform_owner()`），租户身份一律 403。
 */

import axios from './axios'

const CONSOLE_OPS_AUDIT_BASE = '/console/ops-audit'

/** 一条运维审计记录（与表的列一一对应） */
export interface OpsAuditItem {
  /** 记录号，也就是回执里的「记录号」 */
  id: number
  /** 写入时间（ISO） */
  created_at: string
  /** 关闭时间（ISO）；未关闭为 null */
  finished_at: string | null
  /** 来源：agent / na_ops */
  source: string
  /** 授权上的运维人员，逗号连接 */
  operator: string
  /** 来源适配器 */
  adapter_key: string
  /** 来源实例 */
  instance_key: string
  /** 来源会话 */
  chat_key: string
  /** 回合标识；/na_ops 为空串 */
  round_id: string
  /** 工具名或 na_ops:<指令> */
  tool: string
  /** 脱敏限长后的调用参数 */
  args_json: Record<string, unknown>
  /** pending / executed / refused / failed */
  outcome: string
  /** 拒绝原因码或失败类别 */
  refuse_reason: string
  /** 宿主写的结果摘要 */
  result_summary: string
  /** 异常类名，不含原文 */
  error_type: string
}

/** 分页的审计记录 */
export interface OpsAuditListResponse {
  /** 符合筛选条件的总条数 */
  total: number
  /** 当前页记录，按记录号倒序 */
  items: OpsAuditItem[]
}

/** 列表筛选与分页 */
export interface OpsAuditQuery {
  page: number
  page_size: number
  outcome?: string
  tool?: string
  source?: string
  operator?: string
}

export const CONSOLE_OPS_AUDIT_QUERY_KEYS = {
  list: (query: OpsAuditQuery) => ['console-ops-audit', query] as const,
}

export const consoleOpsAuditApi = {
  /** 按记录号倒序分页读取运维审计 */
  async listEvents(query: OpsAuditQuery): Promise<OpsAuditListResponse> {
    const response = await axios.get<OpsAuditListResponse>(CONSOLE_OPS_AUDIT_BASE, {
      params: query,
    })
    return response.data
  },
}
