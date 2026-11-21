import i18n from '../../config/i18n'
import axios, { ApiError } from './axios'

/**
 * 角色状态插件（`Akiyo_dayo.state_table`）的管理接口
 *
 * 插件路由不在 `/api` 下，所以每条请求都清掉 axios 实例的 `baseURL`；作用域（适配器 + 实例）
 * 是服务端判归属的唯一依据，每条请求都必须带上：这组接口没有全局视图，不带实例一律 400。
 */

export const STATE_TABLE_BASE = '/plugins/Akiyo_dayo.state_table'

export interface StateScope {
  adapter_key: string
  instance_key: string
}

/** 字段类型：决定模板能写什么、状态图怎么画 */
export type StateFieldType = 'string' | 'number' | 'boolean' | 'object' | 'array'

/**
 * 模板里的一个字段
 *
 * 只有 `path` 与 `type` 一定在，其余键缺省即默认值。这里把接口说明里列出的键都写全，
 * 编辑时没出现在表格里的键靠对象展开原样带回，不会因为前端不认就丢掉。
 */
export interface StateFieldDef {
  path: string[]
  type: StateFieldType
  description?: string
  pair?: boolean
  readonly?: boolean
  private?: boolean
  hidden?: boolean
  minimum?: number | null
  maximum?: number | null
  max_length?: number
  max_items?: number
  extensible?: boolean
  template?: unknown
  required?: boolean
  priority?: number
  person?: boolean
  affection?: 'score' | 'stage' | null
  headline?: boolean
}

export interface StateFieldDefs {
  fields: StateFieldDef[]
  strict_set?: boolean
  private_words?: string[]
  theme?: string
}

/** 状态模板的主题取值；空串表示默认主题 */
export const STATE_THEMES = ['default', 'night', 'sakura', 'forest', 'ink'] as const
export type StateTheme = (typeof STATE_THEMES)[number]

// ---- 频道状态 ----

export interface StateChannelItem {
  id: number
  chat_key: string
  preset_id: number
  version: number
  paused: boolean
  consecutive_failures: number
  last_update_at: string | null
  update_time: string | null
  /** 列表里才有的显示名；详情接口不返回 */
  channel_name?: string
  preset_name?: string
  last_source: string | null
  last_result: string | null
}

export interface StateChannelMeta {
  id: number
  chat_key: string
  preset_id: number
  version: number
  paused: boolean
  consecutive_failures: number
  last_update_at: string | null
  update_time: string | null
}

export interface StateSnapshotOp {
  op: string
  path?: string
  reason?: string
}

/** 一条快照或更新记录；`has_data` 为真才看得到内容、才回滚得动 */
export interface StateSnapshot {
  id: number
  source: string
  result: string
  has_data: boolean
  applied: StateSnapshotOp[]
  rejected: StateSnapshotOp[]
  model_group: string
  editor: string
  create_time: string
}

export interface StateSnapshotDetail extends StateSnapshot {
  /** `has_data` 为假时为 null */
  data: Record<string, unknown> | null
}

export interface StateChannelDetail extends StateChannelMeta {
  data: Record<string, unknown>
  snapshots: StateSnapshot[]
}

export interface StatePage<T> {
  items: T[]
  total: number
  page: number
  size: number
}

export interface StateChannelQuery {
  preset_id?: number
  /** `true` / `false`；不传表示都看 */
  paused?: boolean
  q?: string
  page?: number
  size?: number
}

// ---- 个人档案 ----

export interface StatePersonItem {
  id: number
  preset_id: number
  platform_userid: string
  nickname: string
  /** 大于 0 表示这一份属于实例组，只有平台方看得到 */
  domain_group_id: number
  version: number
  update_time: string | null
}

export interface StatePersonDetail {
  id: number
  preset_id: number
  platform_userid: string
  /** 详情里的昵称恒为空串，显示时用列表那一行的 */
  nickname: string
  domain_group_id: number
  version: number
  update_time: string | null
  data: Record<string, unknown>
  snapshots: StateSnapshot[]
}

export interface StatePersonQuery {
  preset_id?: number
  uid?: string
  page?: number
  size?: number
}

export interface StatePersonLookupQuery {
  preset_id: number
  uid: string
  domain_group_id: number
}

/** 改动类接口只回编号与新的版本号 */
export interface StateVersionResult {
  id: number
  version: number
}

// ---- 模板 ----

export interface StateTemplateSummary {
  preset_id: number
  preset_name: string
  global: boolean
  writable: boolean
  exists: boolean
  /** 还没有模板时为 null */
  source: string | null
  version: number
  fields: number
}

export interface StateTemplateDetail {
  preset_id: number
  exists: boolean
  writable: boolean
  /** 没有模板时后端不返回这几项 */
  field_defs?: StateFieldDefs
  initial?: Record<string, unknown>
  source?: string
  version?: number
  edited_by?: string
}

export interface StateTemplateSavePayload {
  field_defs: StateFieldDefs
  initial: Record<string, unknown>
  /** 还没有模板时传 0 */
  version: number
}

export interface StateTemplateSaveResult {
  preset_id: number
  version: number
}

export interface StateInitvarEntry {
  entry_id: number
  title: string
}

export interface StateTemplateFromInitvarResult {
  preset_id: number
  version: number
  fields: number
}

export interface StateTemplateFromBuiltinResult {
  preset_id: number
  version: number
  added: string[]
  skipped: string[]
}

export type StateBuiltinKind = 'scene' | 'inventory'

// ---- 启用进度 ----

/**
 * 这个实例的启用进度（只读）
 *
 * 插件没在实例层启用时，`/setup-status` 本身也是 404 —— 页面把 404 当作「第 1 步还没做」，
 * 因此这个接口是整页唯一允许在未启用时发出的请求。
 */
export interface StateSetupStatus {
  /** 可见人设里已有状态模板的个数 */
  template_count: number
  /** 这个实例下已经有状态的频道数 */
  channel_count: number
  /** 可以看到的个人档案数 */
  person_count: number
  /** 单独指定的更新模型组；空串表示没指定 */
  update_model_group: string
  /** 实际生效的模型组：没单独指定时回落到主对话模型组 */
  update_model_group_effective: string
  /** 为真表示正在用主对话模型组 */
  update_uses_fallback: boolean
  /** 私聊里是否顺手更新 */
  inline_private_update: boolean
}

// ---- 审计 ----

export interface StateAuditItem {
  id: number
  target_kind: string
  target_id: number
  /** 只有频道行带这个键 */
  chat_key?: string
  source: string
  ops: StateSnapshotOp[]
  editor: string
  create_time: string
}

export interface StateAuditQuery {
  kind?: string
  source?: string
  page?: number
  size?: number
}

function scopeParams(scope: StateScope): Record<string, string> {
  return { adapter_key: scope.adapter_key, instance_key: scope.instance_key }
}

/**
 * 把 Blob 形式的错误响应读成 `ApiError`
 *
 * `responseType: 'blob'` 时错误响应体也是 Blob，拦截器认不出后端的标准错误格式，只能报
 * 「服务器内部错误」；而状态图被拒的原因（冷却还剩几秒、画图位占满、没有中文字体）正是要给人看的。
 */
async function blobError(body: unknown, status: number): Promise<ApiError> {
  try {
    if (body instanceof Blob) {
      const parsed: unknown = JSON.parse(await body.text())
      if (
        typeof parsed === 'object' &&
        parsed !== null &&
        typeof (parsed as { message?: unknown }).message === 'string'
      ) {
        const { error, message, detail } = parsed as { error?: unknown; message: string; detail?: unknown }
        return new ApiError(
          typeof error === 'string' ? error : 'UnknownError',
          message,
          typeof detail === 'string' ? detail : null,
          undefined,
          status
        )
      }
    }
  } catch {
    // 不是 JSON：落到下面的通用文案
  }
  return new ApiError(
    'UnknownError',
    i18n.t('serverError', { ns: 'errors', defaultValue: '服务器内部错误' }),
    null,
    undefined,
    status
  )
}

export const stateTableApi = {
  getSetupStatus: async (scope: StateScope) => {
    const response = await axios.get<StateSetupStatus>(`${STATE_TABLE_BASE}/setup-status`, {
      baseURL: '',
      params: scopeParams(scope),
    })
    return response.data
  },

  listChannels: async (scope: StateScope, params: StateChannelQuery = {}) => {
    const response = await axios.get<StatePage<StateChannelItem>>(`${STATE_TABLE_BASE}/channels`, {
      baseURL: '',
      params: { ...params, ...scopeParams(scope) },
    })
    return response.data
  },

  getChannel: async (scope: StateScope, id: number) => {
    const response = await axios.get<StateChannelDetail>(`${STATE_TABLE_BASE}/channels/${id}`, {
      baseURL: '',
      params: scopeParams(scope),
    })
    return response.data
  },

  updateChannel: async (scope: StateScope, id: number, data: Record<string, unknown>, version: number) => {
    const response = await axios.put<StateChannelMeta>(
      `${STATE_TABLE_BASE}/channels/${id}`,
      { data, version },
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  resetChannel: async (scope: StateScope, id: number) => {
    const response = await axios.post<StateChannelMeta>(
      `${STATE_TABLE_BASE}/channels/${id}/reset`,
      undefined,
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  resumeChannel: async (scope: StateScope, id: number) => {
    const response = await axios.post<StateChannelMeta>(
      `${STATE_TABLE_BASE}/channels/${id}/resume`,
      undefined,
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  getChannelSnapshot: async (scope: StateScope, id: number, snapshotId: number) => {
    const response = await axios.get<StateSnapshotDetail>(
      `${STATE_TABLE_BASE}/channels/${id}/snapshots/${snapshotId}`,
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  rollbackChannel: async (scope: StateScope, id: number, snapshotId: number, version: number) => {
    const response = await axios.post<StateChannelMeta>(
      `${STATE_TABLE_BASE}/channels/${id}/rollback/${snapshotId}`,
      { version },
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  /**
   * 频道里那份状态图；画不了（冷却中、画图位占满、没有中文字体）时后端给 400，原因在 `ApiError.message`
   *
   * 4xx 在这里自己读出后端的文案（见 `blobError`）；401 仍交给拦截器去登出，5xx 走拦截器的通用文案。
   */
  getChannelImage: async (scope: StateScope, id: number) => {
    const response = await axios.get<Blob>(`${STATE_TABLE_BASE}/channels/${id}/image`, {
      baseURL: '',
      params: scopeParams(scope),
      responseType: 'blob',
      validateStatus: status =>
        (status >= 200 && status < 300) || (status >= 400 && status < 500 && status !== 401),
    })
    if (response.status >= 400) {
      throw await blobError(response.data, response.status)
    }
    return response.data
  },

  listPeople: async (scope: StateScope, params: StatePersonQuery = {}) => {
    const response = await axios.get<StatePage<StatePersonItem>>(`${STATE_TABLE_BASE}/people`, {
      baseURL: '',
      params: { ...params, ...scopeParams(scope) },
    })
    return response.data
  },

  lookupPerson: async (scope: StateScope, params: StatePersonLookupQuery) => {
    const response = await axios.get<{ id: number }>(`${STATE_TABLE_BASE}/people/lookup`, {
      baseURL: '',
      params: { ...params, ...scopeParams(scope) },
    })
    return response.data
  },

  getPerson: async (scope: StateScope, id: number) => {
    const response = await axios.get<StatePersonDetail>(`${STATE_TABLE_BASE}/people/${id}`, {
      baseURL: '',
      params: scopeParams(scope),
    })
    return response.data
  },

  updatePerson: async (scope: StateScope, id: number, data: Record<string, unknown>, version: number) => {
    const response = await axios.put<StateVersionResult>(
      `${STATE_TABLE_BASE}/people/${id}`,
      { data, version },
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  resetPerson: async (scope: StateScope, id: number) => {
    const response = await axios.post<StateVersionResult>(
      `${STATE_TABLE_BASE}/people/${id}/reset`,
      undefined,
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  getPersonSnapshot: async (scope: StateScope, id: number, snapshotId: number) => {
    const response = await axios.get<StateSnapshotDetail>(
      `${STATE_TABLE_BASE}/people/${id}/snapshots/${snapshotId}`,
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  rollbackPerson: async (scope: StateScope, id: number, snapshotId: number, version: number) => {
    const response = await axios.post<StateVersionResult>(
      `${STATE_TABLE_BASE}/people/${id}/rollback/${snapshotId}`,
      { version },
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  listTemplates: async (scope: StateScope, q?: string) => {
    const response = await axios.get<{ items: StateTemplateSummary[] }>(
      `${STATE_TABLE_BASE}/templates`,
      { baseURL: '', params: { ...(q ? { q } : {}), ...scopeParams(scope) } }
    )
    return response.data
  },

  getTemplate: async (scope: StateScope, presetId: number) => {
    const response = await axios.get<StateTemplateDetail>(
      `${STATE_TABLE_BASE}/templates/${presetId}`,
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  updateTemplate: async (scope: StateScope, presetId: number, payload: StateTemplateSavePayload) => {
    const response = await axios.put<StateTemplateSaveResult>(
      `${STATE_TABLE_BASE}/templates/${presetId}`,
      payload,
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  listInitvarEntries: async (scope: StateScope, presetId: number) => {
    const response = await axios.get<{ items: StateInitvarEntry[] }>(
      `${STATE_TABLE_BASE}/templates/${presetId}/initvar-entries`,
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  templateFromInitvar: async (scope: StateScope, presetId: number, version: number, entryId?: number) => {
    const body: { version: number; entry_id?: number } = { version }
    if (entryId !== undefined) body.entry_id = entryId
    const response = await axios.post<StateTemplateFromInitvarResult>(
      `${STATE_TABLE_BASE}/templates/${presetId}/from-initvar`,
      body,
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  templateFromBuiltin: async (
    scope: StateScope,
    presetId: number,
    kinds: StateBuiltinKind[],
    version: number
  ) => {
    const response = await axios.post<StateTemplateFromBuiltinResult>(
      `${STATE_TABLE_BASE}/templates/${presetId}/builtin`,
      { kinds, version },
      { baseURL: '', params: scopeParams(scope) }
    )
    return response.data
  },

  listAudit: async (scope: StateScope, params: StateAuditQuery = {}) => {
    const response = await axios.get<StatePage<StateAuditItem>>(`${STATE_TABLE_BASE}/audit`, {
      baseURL: '',
      params: { ...params, ...scopeParams(scope) },
    })
    return response.data
  },
}
