import axios from './axios'

export interface ConfigItem {
  key: string
  value: string | number | boolean | Array<string | number | boolean>
  title: string
  description?: string
  placeholder?: string
  type: string
  element_type?: string // 列表元素类型
  enum?: string[]
  is_secret?: boolean
  is_textarea?: boolean
  ref_model_groups?: boolean
  is_hidden?: boolean
  required?: boolean // 添加必填属性
  model_type?: string
}

export interface BatchUpdateConfigRequest {
  configs: Record<string, string>
}

export interface ModelGroupConfig {
  CHAT_MODEL: string
  CHAT_PROXY: string
  BASE_URL: string
  API_KEY: string
  MODEL_TYPE?: string
  TEMPERATURE?: number | null
  TOP_P?: number | null
  TOP_K?: number | null
  PRESENCE_PENALTY?: number | null
  FREQUENCY_PENALTY?: number | null
  EXTRA_BODY?: string | null
  ENABLE_VISION?: boolean
  ENABLE_COT?: boolean
}

export interface ModelTypeOption {
  value: string
  label: string
  description?: string
  color?: string
  icon?: string
}

/** 本分支的三个版本号：分支版本（界面展示）、插件兼容版本（市场兼容判定）、所基于的上游版本 */
export interface VersionInfo {
  fork_version: string
  compat_version: string
  upstream_base_version: string
}

export const configApi = {
  /** 插件兼容版本，插件市场按它判断 minNaVersion / maxNaVersion；界面展示产品版本请用 getVersionInfo */
  getVersion: async () => {
    const response = await axios.get<string>('/config/version')
    return response.data
  },
  getVersionInfo: async () => {
    const response = await axios.get<VersionInfo>('/config/version-info')
    return response.data
  },
}
