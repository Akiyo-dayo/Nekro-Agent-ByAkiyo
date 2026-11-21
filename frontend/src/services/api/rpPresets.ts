import axios from './axios'

/**
 * 角色扮演预设（酒馆对话补全预设）接口
 *
 * 路径相对 `/rp-presets`，权限一律 `Role.Admin`。类型与
 * `nekro_agent/routers/rp_presets.py` 一一对应：那边加字段这里也要加，两处形状不一致时
 * 前端会静默拿到 `undefined`。
 *
 * 上传（inspect / import）的 `instance_key` 是**表单字段**：后端这两个端点按 `Form(...)`
 * 收，写成 query 会被静默当成空串，租户的导入就落到全局视图里。其余端点走 query。
 */

/** 作用域参数；留空表示平台方全局视图，租户身份下必须带自己的实例 */
export interface RpPresetScope {
  instance_key?: string
}

/** 列表 / 检查接口里的段落：不带正文，只给字数与排布信息 */
export interface RpPresetSegmentBrief {
  identifier: string
  name: string
  role: string
  /** 占位段：没有正文，由 NA 在运行时填入 */
  marker: boolean
  /** `relative`（聊天记录之前/之后）或 `in_chat`（按深度插入） */
  position: string
  depth: number
  injection_order: number
  enabled: boolean
  chars: number
}

/** 详情接口里的段落：带正文 */
export interface RpPresetSegmentDetail extends RpPresetSegmentBrief {
  content: string
  forbid_overrides: boolean
}

/** 一条解析说明；`kind` 是机读分类，`text` 是给管理员看的原文 */
export interface RpPresetNote {
  kind?: string
  text?: string
  [key: string]: unknown
}

export interface RpPresetInspectResponse {
  /** 按文件名推出的默认显示名 */
  name: string
  segments: RpPresetSegmentBrief[]
  notes: RpPresetNote[]
  regex_count: number
}

export interface RpPresetSummary {
  id: number
  name: string
  /** 归属实例标识；空串表示全局预设 */
  owner_instance_key: string
  segment_count: number
  enabled_count: number
  /** 当前身份下只读（租户看平台方的全局预设） */
  read_only: boolean
  update_time: string
}

export interface RpPresetListResponse {
  total: number
  items: RpPresetSummary[]
}

export interface RpPresetDetail extends RpPresetSummary {
  segments: RpPresetSegmentDetail[]
  notes: RpPresetNote[]
  regex_count: number
}

/** 只改名字与开关；正文与顺序不在这一版的可编辑范围内 */
export interface RpPresetUpdateRequest {
  name?: string
  enabled?: Record<string, boolean>
}

/** 一处配置选用了这个预设：层的种类 + 该层自己的标识 */
export interface RpPresetReference {
  layer: 'system' | 'adapter' | 'instance' | 'channel'
  adapter_key: string
  instance_key: string
  chat_key: string
  /** 人读的名字：适配器 / 实例 / 频道名；全局配置为空串 */
  label: string
}

export interface RpPresetReferencesResponse {
  total: number
  items: RpPresetReference[]
}

export interface RpPresetDeleteResponse {
  ok: boolean
  /**
   * 删除时是否还有地方选用了它
   *
   * 由删除时现扫各层配置得出；想知道「都是哪些地方」用 `getReferences`。
   */
  referenced: boolean
}

/** 去掉空值；`0`、`false` 都保留 */
function compact<T extends object>(params: T): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '')
  )
}

/**
 * 从 `Content-Disposition` 取文件名
 *
 * 优先认 RFC 5987 的 `filename*=UTF-8''…`：中文预设名只能是这个形态，`filename=` 那一份
 * 是给老客户端看的 ASCII 回退。顺序反了会永远拿到 `rp-preset-12.json`。
 */
function exportFilename(contentDisposition: unknown, presetId: number): string {
  if (typeof contentDisposition === 'string') {
    const encoded = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)
    if (encoded?.[1]) {
      try {
        return decodeURIComponent(encoded[1])
      } catch {
        // 解不开就继续往下试，坏文件名不该让一次成功的导出变成失败
      }
    }
    const plain = contentDisposition.match(/filename="?([^";]+)"?/)
    if (plain?.[1]) return plain[1]
  }
  return `rp-preset-${presetId}.json`
}

export const rpPresetsApi = {
  /** 列出可见预设：全局视图看全部，实例视图看全局 + 自己 */
  list: async (scope: RpPresetScope = {}): Promise<RpPresetListResponse> => {
    const response = await axios.get<RpPresetListResponse>('/rp-presets', {
      params: compact(scope),
    })
    return response.data
  },

  /** 检查一份预设（只解析、不落库）；错误用 `ApiError.message` 原样给管理员看 */
  inspect: async (file: File, scope: RpPresetScope = {}): Promise<RpPresetInspectResponse> => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('instance_key', scope.instance_key ?? '')
    const response = await axios.post<RpPresetInspectResponse>('/rp-presets/inspect', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return response.data
  },

  /** 导入一份预设；名字留空时后端取文件名 */
  import: async (
    file: File,
    name: string | null,
    scope: RpPresetScope = {}
  ): Promise<RpPresetDetail> => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('instance_key', scope.instance_key ?? '')
    if (name && name.trim()) {
      formData.append('name', name.trim())
    }
    const response = await axios.post<RpPresetDetail>('/rp-presets/import', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return response.data
  },

  /** 详情：段落带正文 */
  get: async (presetId: number, scope: RpPresetScope = {}): Promise<RpPresetDetail> => {
    const response = await axios.get<RpPresetDetail>(`/rp-presets/${presetId}`, {
      params: compact(scope),
    })
    return response.data
  },

  /** 改名字或段落开关；只提交改过的项 */
  update: async (
    presetId: number,
    patch: RpPresetUpdateRequest,
    scope: RpPresetScope = {}
  ): Promise<RpPresetDetail> => {
    const response = await axios.put<RpPresetDetail>(`/rp-presets/${presetId}`, patch, {
      params: compact(scope),
    })
    return response.data
  },

  /** 删除；正在用它的频道会回到不用预设的样子 */
  remove: async (presetId: number, scope: RpPresetScope = {}): Promise<RpPresetDeleteResponse> => {
    const response = await axios.delete<RpPresetDeleteResponse>(`/rp-presets/${presetId}`, {
      params: compact(scope),
    })
    return response.data
  },

  /** 还有哪些层的配置选用了这个预设（删除前提示用；不阻止删除） */
  getReferences: async (
    presetId: number,
    scope: RpPresetScope = {}
  ): Promise<RpPresetReferencesResponse> => {
    const response = await axios.get<RpPresetReferencesResponse>(
      `/rp-presets/${presetId}/references`,
      { params: compact(scope) }
    )
    return response.data
  },

  /** 导出原文件并直接触发浏览器下载 */
  download: async (presetId: number, scope: RpPresetScope = {}): Promise<void> => {
    const response = await axios.get<Blob>(`/rp-presets/${presetId}/export`, {
      params: compact(scope),
      responseType: 'blob',
    })
    const blob =
      response.data instanceof Blob
        ? response.data
        : new Blob([response.data], { type: 'application/json' })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.setAttribute('download', exportFilename(response.headers['content-disposition'], presetId))
    document.body.appendChild(link)
    link.click()
    window.URL.revokeObjectURL(url)
    document.body.removeChild(link)
  },
}
