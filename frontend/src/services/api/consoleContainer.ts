/**
 * 协议端容器 API 客户端
 *
 * 实例本身的增删改查在 `console.ts`；本文件只负责 builtin_napcat 形态下的容器生命周期、
 * 扫码登录与协议端网络配置。类型契约统一复用 `console.ts`，不在此重复声明。
 */

import axios from './axios'
import type {
  ActionOkResponse,
  ContainerInfo,
  ContainerState,
  LoginQrCodeResponse,
} from './console'

/** 实例标识规则：仅小写字母、数字、下划线，最长 32 位（对齐后端 InstanceCreateRequest） */
export const INSTANCE_KEY_PATTERN = /^[a-z0-9_]{1,32}$/

/**
 * 协议端网络连接类型
 *
 * 取值直接对应 NapCat `onebot11.json` 里 `network` 下的数组名，与后端
 * `schemas/console.py::NetworkEntryType` 一一对应，不做额外映射。
 */
export type NetworkEntryType =
  | 'websocketClients'
  | 'websocketServers'
  | 'httpServers'
  | 'httpSseServers'
  | 'httpClients'

/** 表单里可选的连接类型，顺序即下拉框顺序 */
export const NETWORK_ENTRY_TYPES: NetworkEntryType[] = [
  'websocketClients',
  'websocketServers',
  'httpServers',
  'httpSseServers',
  'httpClients',
]

/** 在协议端内部监听端口的类型，用 host + port 描述；其余类型用 url */
export const SERVER_ENTRY_TYPES: NetworkEntryType[] = [
  'websocketServers',
  'httpServers',
  'httpSseServers',
]

/** 由协议端主动向外连接的类型及其允许的协议 */
export const CLIENT_URL_SCHEMES: Record<string, string[]> = {
  websocketClients: ['ws://', 'wss://'],
  httpClients: ['http://', 'https://'],
}

/** 判断某类型是否为「协议端监听」形态 */
export const isServerEntryType = (type: NetworkEntryType): boolean =>
  SERVER_ENTRY_TYPES.includes(type)

/** 一条协议端网络连接配置（对齐后端 `NetworkEntry`，字段为 snake_case） */
export interface NetworkEntry {
  type: NetworkEntryType
  /** 连接名称，同类型内唯一 */
  name: string
  enable: boolean
  /** 目标地址（client 形态） */
  url: string
  /** 监听地址（server 形态） */
  host: string
  /** 监听端口（server 形态） */
  port: number
  token: string
  /** 消息上报格式：array / string */
  message_post_format: string
  report_self_message: boolean
  debug: boolean
  heart_interval_ms: number
  reconnect_interval_ms: number
  /** 强制推送事件（正向 WS 形态） */
  enable_force_push_event: boolean
  /**
   * 是否由 NekroAgent 托管。
   *
   * 托管条目的地址与令牌由后端生成，提交时对应字段会被后端覆盖；用户自建的条目不带该
   * 标记，任何一次保存都必须把它们原样带回，否则就是数据丢失。
   */
  managed_by_nekro: boolean
}

/**
 * 协议端网络配置（写入 NapCat 的 onebot11 适配器配置文件）
 *
 * 一个协议端可以同时开多条连接、多种类型，因此这里是列表而不是单条。
 */
export interface NetworkConfig {
  entries: NetworkEntry[]
  music_sign_url: string
  enable_local_file_to_url: boolean
  parse_multi_msg: boolean
}

/** 新建一条连接的默认值，与后端 `NetworkEntry` 的字段默认值保持一致 */
export const createNetworkEntry = (type: NetworkEntryType, name: string): NetworkEntry => ({
  type,
  name,
  enable: true,
  url: '',
  host: '0.0.0.0',
  port: 0,
  token: '',
  message_post_format: 'array',
  report_self_message: false,
  debug: false,
  heart_interval_ms: 30000,
  reconnect_interval_ms: 3000,
  enable_force_push_event: true,
  managed_by_nekro: false,
})

/** 处于这些状态说明托管容器已存在，可执行启停/重启/删除 */
export const CONTAINER_PRESENT_STATES: ContainerState[] = [
  'running',
  'exited',
  'created',
  'restarting',
  'paused',
]

/**
 * 把后端返回的二维码归一化为可直接渲染的图片 data URI。
 *
 * 后端既可能给出完整 data URI，也可能只给裸 base64；同时拒绝非图片协议的内容，
 * 避免把任意字符串塞进 img src 触发 CSP 或注入问题。
 */
export const toQrCodeDataUri = (raw: string): string => {
  const value = raw.trim()
  if (!value) return ''
  if (value.startsWith('data:image/')) return value
  if (value.startsWith('data:')) return ''
  return `data:image/png;base64,${value}`
}

const containerPath = (instanceKey: string) =>
  `/console/instances/${encodeURIComponent(instanceKey)}/container`

const networkConfigPath = (instanceKey: string) =>
  `/console/instances/${encodeURIComponent(instanceKey)}/network-config`

export const consoleContainerApi = {
  /** 查询协议端容器信息 */
  async getContainer(instanceKey: string): Promise<ContainerInfo> {
    const response = await axios.get<ContainerInfo>(containerPath(instanceKey))
    return response.data
  },

  /** 创建协议端容器 */
  async createContainer(instanceKey: string): Promise<ActionOkResponse> {
    const response = await axios.post<ActionOkResponse>(containerPath(instanceKey))
    return response.data
  },

  /** 启动容器 */
  async startContainer(instanceKey: string): Promise<ActionOkResponse> {
    const response = await axios.post<ActionOkResponse>(`${containerPath(instanceKey)}/start`)
    return response.data
  },

  /** 停止容器 */
  async stopContainer(instanceKey: string): Promise<ActionOkResponse> {
    const response = await axios.post<ActionOkResponse>(`${containerPath(instanceKey)}/stop`)
    return response.data
  },

  /** 重启容器 */
  async restartContainer(instanceKey: string): Promise<ActionOkResponse> {
    const response = await axios.post<ActionOkResponse>(`${containerPath(instanceKey)}/restart`)
    return response.data
  },

  /** 删除容器 */
  async deleteContainer(instanceKey: string): Promise<ActionOkResponse> {
    const response = await axios.delete<ActionOkResponse>(containerPath(instanceKey))
    return response.data
  },

  /** 获取扫码登录二维码 */
  async getQrCode(instanceKey: string): Promise<LoginQrCodeResponse> {
    const response = await axios.get<LoginQrCodeResponse>(`${containerPath(instanceKey)}/qrcode`)
    return response.data
  },

  /** 快速登录（复用容器内缓存的登录态） */
  async quickLogin(instanceKey: string): Promise<ActionOkResponse> {
    const response = await axios.post<ActionOkResponse>(`${containerPath(instanceKey)}/quick-login`)
    return response.data
  },

  /** 获取协议端网络配置 */
  async getNetworkConfig(instanceKey: string): Promise<NetworkConfig> {
    const response = await axios.get<NetworkConfig>(networkConfigPath(instanceKey))
    return response.data
  },

  /** 更新协议端网络配置（需重启容器生效） */
  async updateNetworkConfig(instanceKey: string, data: NetworkConfig): Promise<ActionOkResponse> {
    const response = await axios.put<ActionOkResponse>(networkConfigPath(instanceKey), data)
    return response.data
  },
}
