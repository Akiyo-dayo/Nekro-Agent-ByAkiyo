/**
 * 运维 Bot 确认深链 API 客户端
 *
 * 运维 Bot 在 IM 里只发链接不执行写操作，链接落到 `/console/ops-confirm`，
 * 由本模块调用后端的预览 / 确认端点。契约对齐
 * `nekro_agent/routers/console/ops_actions.py`。
 *
 * 令牌一律放在请求体而不是查询串：查询串会进访问日志与浏览器历史。
 * 预览与确认是两个端点——预览不消费令牌，确认才消费，
 * 这样页面可以先把「这条链接要做什么」显示给人看再动手。
 */

import axios from './axios'
import type { ActionOkResponse } from './console'

const CONSOLE_OPS_BASE = '/console/ops'

/** 一条待确认的运维写操作 */
export interface OpsActionPreview {
  /** 写操作标识，如 disable-instance */
  action: string
  /** 该操作的自然语言说明，必须原样展示给确认人 */
  summary: string
  /** 操作目标 */
  target: string
  /** 目标参数的名称，如「实例标识」 */
  target_label: string
  /** 在运维频道里发起该操作的账号 */
  issuer_user_id: string
  /** 发起该操作的运维频道 */
  issuer_chat_key: string
  /** 过期时间（unix 秒） */
  expires_at: number
}

export const CONSOLE_OPS_QUERY_KEYS = {
  preview: (token: string) => ['console-ops-preview', token] as const,
}

export const consoleOpsApi = {
  /** 校验并预览确认链接，不消费令牌 */
  async preview(token: string): Promise<OpsActionPreview> {
    const response = await axios.post<OpsActionPreview>(`${CONSOLE_OPS_BASE}/actions/preview`, {
      token,
    })
    return response.data
  },

  /** 消费确认链接并执行写操作 */
  async confirm(token: string): Promise<ActionOkResponse> {
    const response = await axios.post<ActionOkResponse>(`${CONSOLE_OPS_BASE}/actions/confirm`, {
      token,
    })
    return response.data
  },
}
