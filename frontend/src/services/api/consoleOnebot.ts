/**
 * OneBot 协议端状态的总后台 API 客户端
 *
 * 类型对齐后端 `nekro_agent/schemas/console_onebot.py`。这里只有只读状态：协议端自报的身份，
 * 以及本次连接以来 NA 发出多少条、收到多少条自身消息回传。
 */

import axios from './axios'

/** 协议端实现：只有 SnowLuma / NapCat 有专门的开法提示，其余原样显示 */
export type OnebotImplementation = 'snowluma' | 'napcat' | 'other' | 'unknown'

/** 「上报自身消息」的判断结果；`off_suspected` 是推测，不是事实 */
export type OnebotSelfReportState = 'on' | 'off_suspected' | 'unknown'

export interface OnebotEndpointStatus {
  online: boolean
  account_id: string
  app_name: string
  app_version: string
  protocol_version: string
  implementation: OnebotImplementation
  self_report: OnebotSelfReportState
  /** 本次连接以来 NA 成功发出、带 message_id 的消息数 */
  own_sends: number
  /** 本次连接以来收到的自身消息回传数（含 NA 自己那部分） */
  echoes: number
  connected_at: number | null
  last_echo_at: number | null
}

export async function getOnebotEndpointStatus(instanceKey: string): Promise<OnebotEndpointStatus> {
  const response = await axios.get<OnebotEndpointStatus>(
    `/console/instances/${encodeURIComponent(instanceKey)}/onebot/endpoint`
  )
  return response.data
}
