import axios from './axios'
import type { PresetSwitchImpact } from './types'

export interface Preset {
  id: number
  remote_id: string | null
  name: string
  title: string
  avatar: string
  description: string
  tags: string
  author: string
  is_remote: boolean
  on_shared: boolean
  create_time: string
  update_time: string
  /** 归属实例标识；空串表示全局共享人设 */
  owner_instance_key?: string
  /** 是否为全局共享人设（实例作用域下只读） */
  is_global?: boolean
  /**
   * 这个人设是否有酒馆扩展行
   *
   * 只要库里有扩展行就是 true，**包括 `card_spec` 为空的那种**（NA 原生人设建的扩展行）。
   * 判断「是不是从酒馆卡导入的」要看 `card_spec`，不是这个字段。
   */
  has_card?: boolean
  /** 卡片版本：`v1` / `v2` / `v3`；空串表示 NA 原生扩展行 */
  card_spec?: string
}

export interface PresetDetail extends Preset {
  content: string
}

export interface PresetListResponse {
  total: number
  items: Preset[]
}

export interface ShareResponse {
  remote_id: string
}

export interface RefreshStatusResponse {
  updated_count: number
  total_cloud_presets: number
}

export interface TagInfo {
  tag: string
  count: number
}

export interface ActionResponse {
  ok: boolean
}

export interface AvatarUploadResponse {
  avatar: string
}

/**
 * 同步云端人设的结果
 *
 * `ok` 表示这次请求成功；`note` 是「成功但有话要说」：比如卡片信封还原不了、或者只还原了
 * 人设与扩展行而没动人设书。空串表示没有任何要提醒的。把 `note` 当错误弹会让一次成功的
 * 同步看起来失败，所以两者必须分开取。
 */
export interface PresetSyncResponse {
  ok: boolean
  note?: string
}

/** 某实例的默认人设（人设回退链的第二层：频道 -> 实例默认 -> 系统默认 -> 内置） */
export interface InstanceDefaultPresetResponse {
  instance_key: string
  preset_id: number | null
  preset: Preset | null
}

/**
 * 实例作用域参数
 *
 * 留空（或不传）表示平台方全局视图；带上实例标识时只可见「全局共享人设 + 该实例私有人设」。
 */
type InstanceScopeParam = { instance_key?: string }

export const presetsApi = {
  getList: async (
    params: {
      page: number
      page_size: number
      search?: string
      tag?: string
      tags?: string
      remote_only?: boolean
    } & InstanceScopeParam
  ): Promise<PresetListResponse> => {
    const response = await axios.get<PresetListResponse>('/presets/list', { params })
    return response.data
  },

  getDetail: async (id: number, instanceKey?: string): Promise<PresetDetail> => {
    const response = await axios.get<PresetDetail>(`/presets/${id}`, {
      params: { instance_key: instanceKey || undefined },
    })
    return response.data
  },

  getDetailByRemoteId: async (remoteId: string, instanceKey?: string): Promise<PresetDetail> => {
    const response = await axios.get<PresetDetail>(
      `/presets/by-remote/${encodeURIComponent(remoteId)}`,
      { params: { instance_key: instanceKey || undefined } }
    )
    return response.data
  },

  create: async (
    data: {
      name: string
      title?: string
      avatar: string
      content: string
      description?: string
      tags?: string
      author?: string
    } & InstanceScopeParam
  ): Promise<ActionResponse> => {
    const response = await axios.post<ActionResponse>('/presets', data)
    return response.data
  },

  update: async (
    id: number,
    data: {
      name: string
      title?: string
      avatar: string
      content: string
      description?: string
      tags?: string
      author?: string
      remove_remote?: boolean
    } & InstanceScopeParam
  ): Promise<ActionResponse> => {
    const response = await axios.put<ActionResponse>(`/presets/${id}`, data)
    return response.data
  },

  /** 读取某实例的默认人设 */
  getInstanceDefault: async (instanceKey: string): Promise<InstanceDefaultPresetResponse> => {
    const response = await axios.get<InstanceDefaultPresetResponse>(
      `/presets/instance/${encodeURIComponent(instanceKey)}/default`
    )
    return response.data
  },

  /**
   * 预览「改实例默认人设」的影响面
   *
   * 这是人设回退链的**第二层**：频道自己没绑人设时说话的就是它。改掉它，
   * 该实例下所有未绑人设的频道同时换人 —— 影响面比逐个频道改大得多，
   * 因此与频道那一处同形，先看影响面再确认。
   */
  getInstanceDefaultImpact: async (
    instanceKey: string,
    presetId: number | null
  ): Promise<PresetSwitchImpact> => {
    const response = await axios.get<PresetSwitchImpact>(
      `/presets/instance/${encodeURIComponent(instanceKey)}/default/impact`,
      { params: { preset_id: presetId } }
    )
    return response.data
  },

  /**
   * 设置或清除某实例的默认人设（preset_id 传 null 表示清除）
   *
   * `confirmed` 与频道那一处同义：生效人设真的变了而没带它时后端 400。
   */
  setInstanceDefault: async (
    instanceKey: string,
    presetId: number | null,
    confirmed = false
  ): Promise<ActionResponse> => {
    const response = await axios.put<ActionResponse>(
      `/presets/instance/${encodeURIComponent(instanceKey)}/default`,
      { preset_id: presetId, confirmed }
    )
    return response.data
  },

  // 以下写接口都作用在「某个作用域下可写的人设」上，因此和读接口一样必须带 instance_key：
  // 租户身份下省略它会被后端按「未指定作用域」拒绝，而不是退化成全局视图。
  delete: async (id: number, instanceKey?: string): Promise<ActionResponse> => {
    const response = await axios.delete<ActionResponse>(`/presets/${id}`, {
      params: { instance_key: instanceKey || undefined },
    })
    return response.data
  },

  sync: async (id: number, instanceKey?: string): Promise<PresetSyncResponse> => {
    const response = await axios.post<PresetSyncResponse>(`/presets/${id}/sync`, null, {
      params: { instance_key: instanceKey || undefined },
    })
    return response.data
  },

  uploadAvatar: async (file: File): Promise<AvatarUploadResponse> => {
    const formData = new FormData()
    formData.append('file', file)
    const response = await axios.post<AvatarUploadResponse>('/presets/upload-avatar', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    })
    return response.data
  },

  shareToCloud: async (
    id: number | string,
    is_sfw: boolean = true,
    instanceKey?: string
  ): Promise<ShareResponse> => {
    const response = await axios.post<ShareResponse>(`/presets/${id}/share`, null, {
      params: { is_sfw, instance_key: instanceKey || undefined },
    })
    return response.data
  },

  unshare: async (id: number, instanceKey?: string): Promise<ActionResponse> => {
    const response = await axios.post<ActionResponse>(`/presets/${id}/unshare`, null, {
      params: { instance_key: instanceKey || undefined },
    })
    return response.data
  },

  syncToCloud: async (
    id: number,
    is_sfw: boolean = true,
    instanceKey?: string
  ): Promise<ActionResponse> => {
    const response = await axios.post<ActionResponse>(`/presets/${id}/sync-to-cloud`, null, {
      params: { is_sfw, instance_key: instanceKey || undefined },
    })
    return response.data
  },

  refreshSharedStatus: async (): Promise<RefreshStatusResponse> => {
    const response = await axios.post<RefreshStatusResponse>('/presets/refresh-shared-status')
    return response.data
  },

  getTags: async (instanceKey?: string): Promise<TagInfo[]> => {
    const response = await axios.get<TagInfo[]>('/presets/tags', {
      params: { instance_key: instanceKey || undefined },
    })
    return response.data
  },
}
