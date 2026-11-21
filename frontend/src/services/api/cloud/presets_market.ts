import axios from '../axios'

export interface CloudPreset {
  remote_id: string
  is_local: boolean
  name: string
  title: string
  avatar: string
  content: string
  description: string
  tags: string
  author: string
  create_time: string
  update_time: string
  favoriteCount?: number
  isFavorited?: boolean
}

export interface CloudPresetListResponse {
  total: number
  items: CloudPreset[]
  page: number
  page_size: number
  total_pages: number
}

export interface UserPreset {
  id: string
  name: string
  title: string
  avatar?: string
  favoriteCount?: number
}

export interface UserPresetListResponse {
  items: UserPreset[]
  total: number
}

export interface ActionResponse {
  ok: boolean
}

/**
 * 下载云端人设的结果
 *
 * 与 `presetsApi.sync` 同形：`ok` 表示下载本身成功，`note` 是「人设下回来了，但附带的
 * 酒馆卡数据没还原」这类需要告诉用户、又不该算失败的说明。没有话要说时是空串。
 */
export interface PresetDownloadResponse {
  ok: boolean
  note?: string
}

export const presetsMarketApi = {
  /**
   * 获取云端人设列表
   */
  getList: async (params: {
    page: number
    page_size: number
    keyword?: string
    tag?: string
  }): Promise<CloudPresetListResponse> => {
    const response = await axios.get<CloudPresetListResponse>('/cloud/presets-market/list', { params })
    return response.data
  },

  /**
   * 获取用户上传的人设列表
   */
  getUserPresets: async (): Promise<UserPresetListResponse> => {
    const response = await axios.get<UserPresetListResponse>('/cloud/presets-market/user-presets')
    return response.data
  },

  /**
   * 获取人设详情
   */
  getPresetDetail: async (remote_id: string): Promise<CloudPreset> => {
    const response = await axios.get<CloudPreset>(`/cloud/presets-market/detail/${remote_id}`)
    return response.data
  },

  /**
   * 下载云端人设到本地
   *
   * 返回的 `note` 非空时要单独提示一次：那说的是卡片数据没还原，用户手上的这份人设
   * 会缺后置指令、开场白之类的部分。
   */
  downloadPreset: async (remote_id: string): Promise<PresetDownloadResponse> => {
    const response = await axios.post<PresetDownloadResponse>(`/cloud/presets-market/download/${remote_id}`)
    return response.data
  },
}
