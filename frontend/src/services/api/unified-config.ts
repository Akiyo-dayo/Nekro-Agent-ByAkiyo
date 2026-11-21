import axios from './axios'
import {
  ConfigItem,
  ModelGroupConfig as SharedModelGroupConfig,
  ModelTypeOption,
} from '../../components/common/ConfigTable'
import type { ModelTestErrorCode } from './tenant'

/**
 * 模型组完整配置
 *
 * 形状与配置表共用（`ConfigTable` 里那份），这里补上只在模型组编辑里用到的
 * 预填方式。`PREFILL_MODE` 决定请求末尾要不要跟一条 assistant 消息，落库字段名
 * 就是它，后端默认 `off`。
 */
export interface ModelGroupConfig extends SharedModelGroupConfig {
  PREFILL_MODE?: 'off' | 'assistant' | 'deepseek'
  /** 聊天接口格式；后端默认 openai（OpenAI 兼容） */
  API_FORMAT?: 'openai' | 'gemini'
  /** 模型上下文窗口（token）；0 = 未填 */
  CONTEXT_LENGTH?: number
  /** 分词系数（相对 o200k_base）；0 = 按模型名自动取 */
  TOKEN_COUNT_FACTOR?: number
}

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

/** 世界书预算的换算依据：这一层生效的模型组、上下文长度、分词系数与已保存的旋钮 */
export interface WorldbookBudgetBasis {
  config_key: string
  /** 这个作用域生效的主模型组名 */
  model_group: string
  context_length: number
  context_length_set: boolean
  default_context_length: number
  output_reserve: number
  factor: number
  factor_source: 'group' | 'model_name' | 'default'
  factor_family: string
  tokens_per_char: number
  estimator: 'tiktoken' | 'chars'
  percent: number
  cap_tokens: number
  base: number
  budget: number
  approx_chars: number
  /** 上一层的值；全局层为 null */
  inherited_percent: number | null
  /** 上一层的值；0 表示上一层不设上限；全局层为 null */
  inherited_cap_tokens: number | null
  /** 备用模型组上下文比主组小时才非空 */
  fallback_model_group: string
  fallback_context_length: number
}

export interface WorldbookBudgetNotice {
  lines: string[]
}

export interface WorldbookBudgetNoticeAck {
  ok: boolean
}

export interface FetchModelsRequest {
  base_url: string
  api_key: string
  proxy_url?: string
  /** openai（默认）走 OpenAI 兼容 /models；gemini 走原生 models 列表 */
  api_format?: 'openai' | 'gemini'
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
  /** openai（默认）走 OpenAI 兼容格式；gemini 走原生 REST */
  api_format?: 'openai' | 'gemini'
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

  /** 世界书预算的换算依据；`configKey` 与配置页用的同一个 */
  getWorldbookBudgetBasis: async (configKey: string): Promise<WorldbookBudgetBasis> => {
    const response = await axios.get<WorldbookBudgetBasis>(
      `/config/worldbook-budget/${encodeURIComponent(configKey)}`
    )
    return response.data
  },

  /** 世界书预算迁移的一次性提示；租户身份恒为空数组 */
  getWorldbookBudgetNotice: async (): Promise<WorldbookBudgetNotice> => {
    const response = await axios.get<WorldbookBudgetNotice>('/config/worldbook-budget-notice')
    return response.data
  },

  /** 平台方点「知道了」：之后再取提示就是空的 */
  ackWorldbookBudgetNotice: async (): Promise<WorldbookBudgetNoticeAck> => {
    const response = await axios.post<WorldbookBudgetNoticeAck>(
      '/config/worldbook-budget-notice/ack'
    )
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

  // 批量更新指定配置。`base` 是每个改动项在页面读取时的值：带上它服务端就比对现值，
  // 打开页面之后被别处改过的项整批拒绝（`ConfigStaleWriteError`）
  batchUpdateConfig: async (
    configKey: string,
    configs: Record<string, string>,
    base?: Record<string, unknown>
  ): Promise<void> => {
    await axios.post(`/config/batch/${configKey}`, base ? { configs, base } : { configs })
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
  // 组名可以是中文，按路径段编码（与 `tenantApi` 的模型组接口同一做法）
  updateModelGroup: async (groupName: string, config: ModelGroupConfig): Promise<void> => {
    await axios.post(`/config/model-groups/${encodeURIComponent(groupName)}`, config)
  },

  deleteModelGroup: async (groupName: string): Promise<void> => {
    await axios.delete(`/config/model-groups/${encodeURIComponent(groupName)}`)
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
  batchUpdateConfig: (
    key: string,
    configs: Record<string, string>,
    base?: Record<string, unknown>
  ) => unifiedConfigApi.batchUpdateConfig(key || configKey, configs, base),
  saveConfig: (key: string = configKey) => unifiedConfigApi.saveConfig(key),
  reloadConfig: (key: string = configKey) => unifiedConfigApi.reloadConfig(key),
})

export default unifiedConfigApi
