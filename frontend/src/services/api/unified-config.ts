import axios from './axios'
import { ConfigItem, ModelGroupConfig, ModelTypeOption } from '../../components/common/ConfigTable'
import type { ModelTestErrorCode } from './tenant'

export interface BatchUpdateConfigRequest {
  configs: Record<string, string>
}

export interface ConfigInfo {
  config_key: string
  config_class: string
  config_file_path?: string
  config_type: string
  field_count: number
}

export interface UiOnboardingGuideStatus {
  should_show: boolean
}

export interface FetchModelsRequest {
  base_url: string
  api_key: string
  proxy_url?: string
}

/**
 * 基础模型组检测结果
 *
 * 凭据隔离的关键：`response_text` / `error_message` 里的上游原文只在当前身份本就能看到
 * 该模型组凭据时才有值。租户测平台方的模型组时，后端只回 `error_code` 与有限集合内的
 * `message`，上游响应体、响应头、异常原文一律不下发。
 */
export interface ModelGroupTestItem {
  group_name: string
  model_name: string
  success: boolean
  latency_ms: number
  /** 归一化结论，前端据此做 i18n */
  error_code?: ModelTestErrorCode
  /** 上游 HTTP 状态码；非 HTTP 失败为 null */
  status_code?: number | null
  /** 后端预定义的有限文案；error_code 缺少译文时兜底展示 */
  message?: string
  used_model?: string | null
  response_text?: string | null
  input_tokens?: number
  output_tokens?: number
  error_message?: string | null
}

export interface ModelGroupInlineTestRequest {
  group_name: string
  chat_model: string
  base_url: string
  api_key: string
  model_type: string
  chat_proxy?: string
  temperature?: number | null
  top_p?: number | null
  top_k?: number | null
  presence_penalty?: number | null
  frequency_penalty?: number | null
  extra_body?: string | null
}

export const unifiedConfigApi = {
  // 获取所有配置键
  getConfigKeys: async (): Promise<string[]> => {
    const response = await axios.get<string[]>('/config/keys')
    return response.data
  },

  getUiOnboardingGuideStatus: async (): Promise<UiOnboardingGuideStatus> => {
    const response = await axios.get<UiOnboardingGuideStatus>('/config/ui-onboarding-guide/status')
    return response.data
  },

  // 获取配置基本信息
  getConfigInfo: async (configKey: string): Promise<ConfigInfo> => {
    const response = await axios.get<ConfigInfo>(`/config/info/${configKey}`)
    return response.data
  },

  // 获取指定配置的配置列表
  getConfigList: async (configKey: string): Promise<ConfigItem[]> => {
    const response = await axios.get<ConfigItem[]>(`/config/list/${configKey}`)
    return response.data
  },

  // 获取指定配置的配置项
  getConfigItem: async (configKey: string, itemKey: string): Promise<ConfigItem> => {
    const response = await axios.get<ConfigItem>(`/config/get/${configKey}/${itemKey}`)
    return response.data
  },

  // 设置指定配置的配置项值
  setConfigValue: async (configKey: string, itemKey: string, value: string): Promise<void> => {
    await axios.post(`/config/set/${configKey}/${itemKey}`, null, {
      params: { value },
    })
  },

  // 批量更新指定配置
  batchUpdateConfig: async (configKey: string, configs: Record<string, string>): Promise<void> => {
    await axios.post(`/config/batch/${configKey}`, { configs })
  },

  // 保存指定配置
  saveConfig: async (configKey: string): Promise<void> => {
    await axios.post(`/config/save/${configKey}`)
  },

  // 重载指定配置
  reloadConfig: async (configKey: string): Promise<void> => {
    await axios.post(`/config/reload/${configKey}`)
  },

  // 获取模型组列表（兼容性API）
  getModelGroups: async (): Promise<Record<string, ModelGroupConfig>> => {
    const response = await axios.get<Record<string, ModelGroupConfig>>('/config/model-groups')
    return response.data
  },

  // 获取模型类型列表（兼容性API）
  getModelTypes: async (): Promise<ModelTypeOption[]> => {
    const response = await axios.get<ModelTypeOption[]>('/config/model-types')
    return response.data
  },

  // 插件配置相关API - 使用统一配置系统
  getPluginConfig: async (pluginId: string): Promise<ConfigItem[]> => {
    return unifiedConfigApi.getConfigList(`plugin_${pluginId}`)
  },

  savePluginConfig: async (pluginId: string, configs: Record<string, string>): Promise<void> => {
    await unifiedConfigApi.batchUpdateConfig(`plugin_${pluginId}`, configs)
  },

  // 模型组管理API - 保持与原有API兼容
  updateModelGroup: async (groupName: string, config: ModelGroupConfig): Promise<void> => {
    await axios.post(`/config/model-groups/${groupName}`, config)
  },

  deleteModelGroup: async (groupName: string): Promise<void> => {
    await axios.delete(`/config/model-groups/${groupName}`)
  },

  fetchModels: async (body: FetchModelsRequest): Promise<string[]> => {
    const response = await axios.post<{ models: string[] }>('/config/model-groups/actions/fetch-models', body)
    return response.data.models
  },

  testModelGroups: async (groupNames: string[]): Promise<ModelGroupTestItem[]> => {
    const response = await axios.post<{ items: ModelGroupTestItem[] }>('/config/model-groups/actions/test', {
      group_names: groupNames,
    })
    return response.data.items
  },

  testModelGroupInline: async (body: ModelGroupInlineTestRequest): Promise<ModelGroupTestItem> => {
    const response = await axios.post<ModelGroupTestItem>('/config/model-groups/actions/test-inline', body)
    return response.data
  },
}

// 创建配置服务适配器，用于适配ConfigTable组件的接口
export const createConfigService = (configKey: string) => ({
  getConfigList: (key: string = configKey) => unifiedConfigApi.getConfigList(key),
  getModelGroups: unifiedConfigApi.getModelGroups,
  getModelTypes: unifiedConfigApi.getModelTypes,
  batchUpdateConfig: (key: string, configs: Record<string, string>) =>
    unifiedConfigApi.batchUpdateConfig(key || configKey, configs),
  saveConfig: (key: string = configKey) => unifiedConfigApi.saveConfig(key),
  reloadConfig: (key: string = configKey) => unifiedConfigApi.reloadConfig(key),
})

export default unifiedConfigApi
