/**
 * 实例作用域相关 API
 *
 * 配置在本项目里是分层叠加的：全局 -> 适配器 -> 实例 -> 会话，
 * 后一层覆盖前一层。本模块提供「读取生效值 + 写入/清除本层覆盖」的接口，
 * 页面据此把「当前生效值」与「来自哪一层」同时展示出来。
 */

import axios, { ApiError } from './axios'
import { unifiedConfigApi } from './unified-config'
import type { ConfigItem, ConfigService } from '../../components/common/ConfigTable'

/**
 * 配置层级标识
 *
 * 后端两套接口用了两组同义的层名：插件作用域用 `global/instance/session`
 * （`nekro_agent/services/plugin/scope.py`），配置溯源用 `system/adapter/instance/channel`
 * （`nekro_agent/services/config_resolver.py`）。前端统一收进一个联合类型，
 * 展示时各自映射到对应文案。
 */
export const CONFIG_LAYERS = [
  'global',
  'system',
  'adapter',
  'instance',
  'session',
  'channel',
] as const
export type ConfigLayer = (typeof CONFIG_LAYERS)[number]

export const isConfigLayer = (value: unknown): value is ConfigLayer =>
  typeof value === 'string' && CONFIG_LAYERS.includes(value as ConfigLayer)

/** 单个插件在某作用域下的生效状态 */
export interface PluginScopeStatus {
  /** 插件标识，形如 `author.module_name`，与插件列表的 `id` 一致 */
  plugin_key: string
  /** 插件显示名 */
  name: string
  /** 当前生效的启用状态 */
  enabled: boolean
  /** 生效值来自哪一层：session > instance > global */
  source: ConfigLayer
  /** 清除本层覆盖后会回落到的状态 */
  inherited_enabled: boolean
  /** 本实例的覆盖值；null 表示未覆盖，继承上层 */
  instance_override: boolean | null
  /** 本会话的覆盖值；null 表示未覆盖 */
  session_override: boolean | null
  /** 插件在本进程内是否已加载（进程级状态，与作用域可见性正交） */
  loaded: boolean
}

export interface PluginScopeResponse {
  instance_key: string
  chat_key: string
  items: PluginScopeStatus[]
}

/** 写入插件作用域覆盖；enabled 传 null 表示清除覆盖、回落上层 */
export interface PluginScopeUpdateRequest {
  enabled: boolean | null
}

/** 单个配置字段的有效值与来源 */
export interface EffectiveConfigFieldTrace {
  key: string
  title: string
  value: unknown
  source: ConfigLayer
  overridable: boolean
  /** 字段确实被覆盖时给出：全局值与各覆盖层的值 */
  layer_values?: Record<string, unknown> | null
}

/** 频道有效配置溯源结果 */
export interface EffectiveConfigTrace {
  chat_key: string
  adapter_key: string
  instance_key: string
  version: number
  /** 本次参与合并的覆盖层，优先级从高到低 */
  active_layers: ConfigLayer[]
  items: EffectiveConfigFieldTrace[]
}

export interface ScopeActionResponse {
  ok: boolean
}

const encode = encodeURIComponent

export const instanceScopeApi = {
  /** 各插件在该实例下的生效状态与来源层级 */
  getPluginScope: async (instanceKey: string): Promise<PluginScopeResponse> => {
    const response = await axios.get<PluginScopeResponse>(`/plugins/scope/${encode(instanceKey)}`)
    return response.data
  },

  /** 写入/清除某插件的实例级启停覆盖 */
  setPluginScope: async (
    instanceKey: string,
    pluginKey: string,
    enabled: boolean | null
  ): Promise<ScopeActionResponse> => {
    const body: PluginScopeUpdateRequest = { enabled }
    const response = await axios.put<ScopeActionResponse>(
      `/plugins/scope/${encode(instanceKey)}/${encode(pluginKey)}`,
      body
    )
    return response.data
  },

  /** 实例级插件配置 */
  getPluginInstanceConfig: async (
    pluginKey: string,
    instanceKey: string
  ): Promise<ConfigItem[]> => {
    const response = await axios.get<ConfigItem[]>(
      `/plugins/${encode(pluginKey)}/config/${encode(instanceKey)}`
    )
    return response.data
  },

  /** 写入实例级插件配置 */
  savePluginInstanceConfig: async (
    pluginKey: string,
    instanceKey: string,
    configs: Record<string, string>
  ): Promise<ScopeActionResponse> => {
    const response = await axios.put<ScopeActionResponse>(
      `/plugins/${encode(pluginKey)}/config/${encode(instanceKey)}`,
      { configs }
    )
    return response.data
  },

  /** 实例级系统配置覆盖 */
  getInstanceConfig: async (adapterKey: string, instanceKey: string): Promise<ConfigItem[]> => {
    const response = await axios.get<ConfigItem[]>(
      `/config/instance/${encode(adapterKey)}/${encode(instanceKey)}`
    )
    return response.data
  },

  /** 写入实例级系统配置覆盖 */
  saveInstanceConfig: async (
    adapterKey: string,
    instanceKey: string,
    configs: Record<string, string>
  ): Promise<ScopeActionResponse> => {
    const response = await axios.put<ScopeActionResponse>(
      `/config/instance/${encode(adapterKey)}/${encode(instanceKey)}`,
      { configs }
    )
    return response.data
  },

  /** 有效配置溯源（每个字段最终值 + 来自哪一层） */
  getEffectiveConfig: async (chatKey: string): Promise<EffectiveConfigTrace> => {
    const response = await axios.get<EffectiveConfigTrace>(`/config/effective/${encode(chatKey)}`)
    return response.data
  },
}

/**
 * 判断错误是否为「后端接口尚未就绪」
 *
 * 实例作用域的分层配置接口分批上线，前端先行开发。命中时页面显示明确提示，
 * 而不是把用户丢进白屏或误导性的空列表。
 *
 * ⚠️ **对租户身份不要直接拿这个结论当文案**：同样一个 404，对平台方只可能是
 * 「这条路由不存在」，对租户还可能是 `assert_instance_accessible` 的**越权 404**
 * （实例已被平台方划走 —— 项目约定越权返回 404 而非 403，见 PROJECT_STATE §1.4）。
 * 把后者说成"接口没上线"、再递一个「切回全局视图」的按钮是两句都错：
 * 租户根本没有全局视图那一档。调用方需要按身份分档，见 `pages/settings/system.tsx`。
 */
export function isScopeEndpointUnavailable(error: unknown): boolean {
  if (error instanceof ApiError) {
    return error.type === 'NotFoundError' || error.type === 'NotImplementedError'
  }
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const status = (error as { response?: { status?: number } }).response?.status
    return status === 404 || status === 405 || status === 501
  }
  return false
}

/**
 * 从会话标识解析实例标识
 *
 * 与后端 `nekro_agent/adapters/onebot_v11/core/channel.py` 的解析规则保持一致：
 * `{adapter_key}-{instance_key}-{channel_id}`，升级前的单实例格式没有实例段。
 * 实例段的字符集刻意排除 `-`，因此可以无歧义地按 `-` 切分。
 */
export function parseInstanceKeyFromChatKey(chatKey: string): string {
  const separatorIndex = chatKey.indexOf('-')
  if (separatorIndex < 0) {
    return ''
  }
  const remainder = chatKey.slice(separatorIndex + 1)
  const nextSeparatorIndex = remainder.indexOf('-')
  if (nextSeparatorIndex < 0) {
    // 历史格式 `onebot_v11-group_123456`，没有实例段
    return ''
  }
  const candidate = remainder.slice(0, nextSeparatorIndex)
  return /^[a-z0-9_]{1,32}$/.test(candidate) ? candidate : ''
}

/** 该会话是否属于给定作用域（全局作用域下恒为 true） */
export function isChatKeyInScope(chatKey: string, instanceKey: string | null): boolean {
  if (!instanceKey) {
    return true
  }
  return parseInstanceKeyFromChatKey(chatKey) === instanceKey
}

/**
 * 面向 `ConfigTable` 的实例级配置服务
 *
 * 保持与 `createConfigService` 相同的接口形状，页面在实例作用域下直接换用它，
 * 读写都落到实例级覆盖，不会误改全局配置。
 *
 * 这是四层配置里「实例覆盖层」在整个前端的**唯一**写入口，也是租户身份唯一写得动的一层：
 * 后端 `PUT /config/instance/{adapter}/{instance}` 接了 `assert_instance_accessible`
 * （别人的实例 404）与 `_assert_tenant_may_write`（承载凭据的字段、平台方边界项、
 * 超过配额天花板的写入一律拒绝），共享层（`system` / `adapter_*`）的写入对租户是 403。
 * 因此**不要**为了"让页面有东西可显示"在租户身份下回落 `createConfigService('system')`。
 */
export function createInstanceConfigService(
  adapterKey: string,
  instanceKey: string
): ConfigService {
  return {
    getConfigList: () => instanceScopeApi.getInstanceConfig(adapterKey, instanceKey),
    getModelGroups: unifiedConfigApi.getModelGroups,
    getModelTypes: unifiedConfigApi.getModelTypes,
    batchUpdateConfig: async (_configKey: string, configs: Record<string, string>) => {
      await instanceScopeApi.saveInstanceConfig(adapterKey, instanceKey, configs)
    },
    saveConfig: async () => {
      // 实例级覆盖在 PUT 时即已落盘，无需额外的保存动作
    },
    reloadConfig: async () => {
      // 实例级覆盖没有独立的重载语义，交由上层刷新查询完成
    },
  }
}
