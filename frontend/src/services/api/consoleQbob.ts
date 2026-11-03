/**
 * 附带 qbob（QQ 官方机器人 → OneBot v11）的总后台 API 客户端
 *
 * 类型对齐后端 `nekro_agent/schemas/console_qbob.py`。这里的响应**一律不含** AppSecret、
 * OneBot 访问令牌与 qbob 服务账号口令；AppSecret 只在「手填接入」的请求体里出现一次。
 * 设计见 `docs/qbob-integration.md`。
 */

import axios from './axios'
import type { ActionOkResponse } from './console'

/** 本部署附带的 qbob 服务是否可用 */
export interface QbobServiceStatus {
  /** 是否配置了 NEKRO_QBOB_ADMIN_URL（没有 = 本部署未附带 qbob） */
  configured: boolean
  reachable: boolean
  service_account_ready: boolean
  version: string
  /** 本机镜像所用的上游提交 */
  qbob_ref: string
  bot_count: number
  /** 不可用时的说明 */
  message: string
}

/** 实例接入的 qbob bot 的状态 */
export interface QbobBotInfo {
  configured: boolean
  attached: boolean
  appid: string
  name: string
  /** qbob 给这个 bot 的 15 位 OneBot self_id */
  self_id: string
  /** qbob 里是否还有这个 bot */
  found: boolean
  running: boolean
  enabled: boolean
  qq_connected: boolean
  onebot_connected: boolean
  /** 反向 WS 端点是否与 NA 期望的一致 */
  endpoint_ok: boolean
  markdown_enabled: boolean
  report_self_message: boolean
  last_error: string
  message: string
}

export interface QbobQrStartResponse {
  task_id: string
  /** 二维码内容：手机 QQ 扫码打开的地址 */
  connect_url: string
  qr_svg: string
}

export interface QbobQrPollResponse {
  /** conflict / failed 时 message 说明原因；之后再轮询同一任务得到同样的结果 */
  status: 'pending' | 'expired' | 'attached' | 'conflict' | 'failed'
  message: string
  bot: QbobBotInfo | null
}

export interface QbobBotOptions {
  enabled?: boolean
  markdown_enabled?: boolean
  report_self_message?: boolean
}

const qbobPath = (instanceKey: string) => `/console/instances/${encodeURIComponent(instanceKey)}/qbob`

export const consoleQbobApi = {
  /** 本部署的 qbob 服务状态（平台方） */
  async getService(): Promise<QbobServiceStatus> {
    const response = await axios.get<QbobServiceStatus>('/console/qbob/service')
    return response.data
  },

  /** 手工提供 qbob 服务账号（平台方，只写不读） */
  async setServiceCredential(username: string, password: string): Promise<ActionOkResponse> {
    const response = await axios.put<ActionOkResponse>(
      '/console/qbob/service/credential',
      { username, password },
      { timeout: 120_000 }
    )
    return response.data
  },

  /** 实例的 bot 状态（租户管理员可读自己的） */
  async getBot(instanceKey: string): Promise<QbobBotInfo> {
    const response = await axios.get<QbobBotInfo>(qbobPath(instanceKey))
    return response.data
  },

  /** 生成扫码接入二维码（平台方） */
  async startQrConnect(instanceKey: string): Promise<QbobQrStartResponse> {
    const response = await axios.post<QbobQrStartResponse>(
      `${qbobPath(instanceKey)}/qrconnect`,
      undefined,
      { timeout: 120_000 }
    )
    return response.data
  },

  /** 轮询扫码结果；扫码完成后由后端直接接入（平台方） */
  async pollQrConnect(instanceKey: string, taskId: string): Promise<QbobQrPollResponse> {
    const response = await axios.get<QbobQrPollResponse>(
      `${qbobPath(instanceKey)}/qrconnect/${encodeURIComponent(taskId)}`,
      { timeout: 120_000 }
    )
    return response.data
  },

  /** 手填 AppID / AppSecret 接入（平台方） */
  async attach(instanceKey: string, appid: string, secret: string): Promise<QbobBotInfo> {
    const response = await axios.post<QbobBotInfo>(
      `${qbobPath(instanceKey)}/attach`,
      { appid, secret },
      { timeout: 120_000 }
    )
    return response.data
  },

  /** 把反向 WS 端点写回 NA 期望的那一条（平台方） */
  async rewire(instanceKey: string): Promise<QbobBotInfo> {
    const response = await axios.post<QbobBotInfo>(
      `${qbobPath(instanceKey)}/rewire`,
      undefined,
      { timeout: 120_000 }
    )
    return response.data
  },

  /** 从 qbob 删除本实例托管的 bot 并解除账号绑定（平台方） */
  async detach(instanceKey: string): Promise<ActionOkResponse> {
    const response = await axios.delete<ActionOkResponse>(qbobPath(instanceKey))
    return response.data
  },

  /** 启用 / 停用与两项开关（租户管理员可改自己的） */
  async updateOptions(instanceKey: string, options: QbobBotOptions): Promise<QbobBotInfo> {
    const response = await axios.patch<QbobBotInfo>(qbobPath(instanceKey), options, {
      timeout: 120_000,
    })
    return response.data
  },

  /** 重载 bot（租户管理员可操作自己的） */
  async reload(instanceKey: string): Promise<ActionOkResponse> {
    const response = await axios.post<ActionOkResponse>(
      `${qbobPath(instanceKey)}/reload`,
      undefined,
      { timeout: 120_000 }
    )
    return response.data
  },
}
