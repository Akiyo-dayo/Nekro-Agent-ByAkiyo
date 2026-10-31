import axios from '../axios'
import { createEventStream } from '../utils/stream'

export interface ContainerStatus {
  running: boolean
  started_at: string
}

export interface TokenResponse {
  token: string | null
}

export interface ActionResponse {
  ok: boolean
}

/**
 * 容器相关端点全部按实例寻址：总后台按 `nekro_napcat_{实例标识}` 建容器，
 * 不带实例标识时后端回落到单实例时代那个固定容器名（老部署升级后仍能用）。
 */
export const oneBotV11Api = {
  /**
   * 获取容器状态
   */
  getContainerStatus: async (instanceKey?: string | null) => {
    const { data } = await axios.get<ContainerStatus>('/adapters/onebot_v11/container/status', {
      params: { instance_key: instanceKey || undefined },
    })
    return data
  },

  /**
   * 获取历史日志
   */
  getContainerLogs: async (tail = 500, instanceKey?: string | null) => {
    const { data } = await axios.get<string[]>('/adapters/onebot_v11/container/logs', {
      params: { tail, instance_key: instanceKey || undefined },
    })
    return data
  },

  /**
   * 获取实时日志流
   */
  streamContainerLogs: (
    onMessage: (data: string) => void,
    onError?: (error: Error) => void,
    instanceKey?: string | null
  ) => {
    const query = instanceKey ? `?instance_key=${encodeURIComponent(instanceKey)}` : ''
    return createEventStream({
      endpoint: `/adapters/onebot_v11/container/logs/stream${query}`,
      onMessage,
      onError,
    })
  },

  /**
   * 重启容器
   */
  restartContainer: async (instanceKey?: string | null) => {
    const { data } = await axios.post<ActionResponse>(
      '/adapters/onebot_v11/container/restart',
      null,
      { params: { instance_key: instanceKey || undefined } }
    )
    return data.ok
  },

  /**
   * 获取 OneBot 协议端接入令牌（**按实例**）
   *
   * 握手校验是按实例做的，所以"该抄进协议端配置的那枚令牌"也必须按实例取：
   * 总后台建出来的实例各有各的令牌，把部署级那枚抄过去只会握手 403。
   * 不带实例标识时返回部署级值，与单实例老部署行为一致。
   */
  getOneBotToken: async (instanceKey?: string | null) => {
    const { data } = await axios.get<TokenResponse>(
      '/adapters/onebot_v11/container/onebot-token',
      { params: { instance_key: instanceKey || undefined } }
    )
    return data.token
  },

  /**
   * 获取NapCat WebUI访问令牌
   */
  getNapcatToken: async (instanceKey?: string | null) => {
    const { data } = await axios.get<TokenResponse>(
      '/adapters/onebot_v11/container/napcat-token',
      { params: { instance_key: instanceKey || undefined } }
    )
    return data.token
  },
}
