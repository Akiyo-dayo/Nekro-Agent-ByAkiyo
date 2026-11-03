import axios from './axios'
import type { PresetSwitchImpact } from './types'
import { createEventStream, createSharedEventStreamManager } from './utils/stream'

export interface ActionResponse {
  ok: boolean
  detail?: string
  error?: string
}

export type AnnouncementScopeType = 'all' | 'instances' | 'channels'

export type AnnouncementStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'

export const ANNOUNCEMENT_TERMINAL_STATUSES: ReadonlySet<AnnouncementStatus> = new Set<AnnouncementStatus>([
  'completed',
  'failed',
  'cancelled',
])

export interface AnnouncementInstanceRef {
  adapter_key: string
  instance_key: string
}

export interface AnnouncementScope {
  scope_type: AnnouncementScopeType
  instances: AnnouncementInstanceRef[]
  chat_keys: string[]
}

export interface AnnouncementInstanceOption {
  adapter_key: string
  instance_key: string
  display_name: string
  channel_count: number
}

export interface AnnouncementPreview {
  channel_count: number
  instance_count: number
  max_channels: number
  exceeds_limit: boolean
  instances: AnnouncementInstanceOption[]
}

export interface AnnouncementSummary {
  id: number
  content: string
  scope_type: AnnouncementScopeType
  scope_detail: string[]
  status: AnnouncementStatus
  total: number
  success_count: number
  failure_count: number
  failure_reason: string
  created_by: string
  create_time: string
  finish_time: string | null
}

export interface AnnouncementDeliveryItem {
  chat_key: string
  channel_name: string
  adapter_key: string
  instance_key: string
  ok: boolean
  error: string
  send_time: string
}

export interface AnnouncementDetail extends AnnouncementSummary {
  deliveries: AnnouncementDeliveryItem[]
}

export interface AnnouncementListResponse {
  total: number
  items: AnnouncementSummary[]
}

export interface AnnouncementSubmitResponse {
  announcement_id: number
  total: number
  status: AnnouncementStatus
}

export interface AnnouncementSnapshotEvent {
  type: 'snapshot'
  announcement: AnnouncementSummary
}

export interface AnnouncementDeliveryEvent {
  type: 'delivery'
  announcement_id: number
  chat_key: string
  channel_name: string
  instance_key: string
  ok: boolean
  error: string
  completed: number
  total: number
  success_count: number
  failure_count: number
}

export interface AnnouncementStatusEvent {
  type: 'status'
  announcement_id: number
  status: AnnouncementStatus
  total: number
  success_count: number
  failure_count: number
  failure_reason: string
}

export type AnnouncementEvent =
  | AnnouncementSnapshotEvent
  | AnnouncementDeliveryEvent
  | AnnouncementStatusEvent

const parseAnnouncementEvent = (eventName: string, rawData: string): AnnouncementEvent | null => {
  if (!['snapshot', 'delivery', 'status'].includes(eventName)) return null
  try {
    const data: unknown = JSON.parse(rawData)
    if (typeof data !== 'object' || data === null) return null
    return data as AnnouncementEvent
  } catch {
    return null
  }
}

export interface ChatChannel {
  id: number
  chat_key: string
  channel_name: string | null
  custom_channel_name: string | null
  is_active: boolean
  status: 'active' | 'observe' | 'disabled'
  chat_type: string
  message_count: number
  create_time: string
  update_time: string
  last_message_time: string | null
}

export interface ChannelDirectoryEntry {
  id: number
  chat_key: string
  channel_name: string | null
  custom_channel_name: string | null
  is_active: boolean
  status: 'active' | 'observe' | 'disabled'
  chat_type: string
}

export interface ChatChannelDetail extends ChatChannel {
  unique_users: number
  conversation_start_time: string
  preset_id?: number | null
  can_send?: boolean
  ai_always_include_msg_id?: boolean
}

export interface ChatMessageSegment {
  type: 'text' | 'image' | 'file' | 'voice' | 'video' | 'at' | 'json_card' | 'poke' | 'forward'
  text: string
  // file / image / voice / video
  file_name?: string
  local_path?: string
  remote_url?: string
  // OneBot 视频占位：没有落盘文件，`text` 是给 AI 看的 `[Video: message:<编号>]`，不展示给人
  video_name?: string
  file_size?: number
  quoted?: boolean
  // at
  target_platform_userid?: string
  target_nickname?: string
  // json_card
  json_data?: Record<string, unknown>
  card_title?: string
  card_desc?: string
  card_icon?: string
  card_preview?: string
  card_url?: string
  share_from_nick?: string
  // forward
  forward_content?: ForwardMessageItem[]
  // poke
  action_img_url?: string
  poke_style?: string
  poke_style_suffix?: string
  target_id?: string
}

/**
 * 视频预览的探测结果。`<video>` 的 onError 读不到响应体，放不了的原因靠这里的 `status` 说清楚
 */
export interface MessageVideoAccess {
  status: 'ok' | 'recalled' | 'offline' | 'expired' | 'unavailable' | 'not_found'
  url: string
  expires_at: number
  file_name: string
  file_size: number
}

export interface ForwardMessageItem {
  sender: string
  content: string
  images: string[]
  forward_content?: ForwardMessageItem[]
}

export interface ChatMessage {
  id: number
  sender_id: string
  sender_name: string
  sender_nickname: string
  platform_userid: string
  content: string
  content_data: ChatMessageSegment[]
  chat_key: string
  create_time: string
  message_id?: string
  ref_msg_id?: string
}

export interface ChatChannelListResponse {
  total: number
  items: ChatChannel[]
}

export interface ChatMessageListResponse {
  total: number
  items: ChatMessage[]
}

export interface ChatChannelUser {
  platform_userid: string
  nickname: string
  command_permission: 'user' | 'advanced' | 'super_user'
  // 平台方看得到 super_users / global；租户看到的这两类统一是 platform
  command_permission_source: 'super_users' | 'global' | 'platform' | 'channel' | 'group_role' | null
  channel_granted: boolean
}

export interface ChatChannelUsersResponse {
  total: number
  items: ChatChannelUser[]
}

export interface ChatPluginData {
  id: number
  plugin_key: string
  data_key: string
  data_value: string
  target_user_id: string
  create_time: string
  update_time: string
}

export interface ChatPluginDataResponse {
  total: number
  items: ChatPluginData[]
  plugin_keys: string[]
  plugin_names: Record<string, string>
}

export interface ChannelDeletePreview {
  message_count: number
  timer_job_count: number
  plugin_data_count: number
  mem_paragraph_count: number
  mem_episode_count: number
  upload_dir_exists: boolean
  sandbox_dir_exists: boolean
}

export interface ChannelListStreamEvent {
  event_type: string
  chat_key: string
  channel_name?: string | null
  custom_channel_name?: string | null
  is_active?: boolean | null
  status?: ChannelDirectoryEntry['status'] | null
}

const channelListStreamManager = createSharedEventStreamManager({
  endpoint: '/chat-channel/list/stream',
  closeDelayMs: 1500,
})

export function getChannelDisplayName(channel: Pick<ChatChannel, 'chat_key' | 'channel_name' | 'custom_channel_name'>): string {
  return channel.custom_channel_name?.trim() || channel.channel_name?.trim() || channel.chat_key
}

export const chatChannelApi = {
  getList: async (params: {
    page: number
    page_size: number
    search?: string
    chat_type?: string
    status?: 'active' | 'observe' | 'disabled'
    is_active?: boolean
  }): Promise<ChatChannelListResponse> => {
    const response = await axios.get<ChatChannelListResponse>('/chat-channel/list', { params })
    return response.data
  },

  getDetail: async (chatKey: string): Promise<ChatChannelDetail> => {
    const response = await axios.get<ChatChannelDetail>(`/chat-channel/detail/${chatKey}`)
    return response.data
  },

  getDirectory: async (): Promise<ChannelDirectoryEntry[]> => {
    const response = await axios.get<{ items: ChannelDirectoryEntry[] }>('/chat-channel/directory')
    return response.data.items
  },

  setActive: async (chatKey: string, isActive: boolean): Promise<ActionResponse> => {
    const response = await axios.post<ActionResponse>(`/chat-channel/${chatKey}/active`, null, {
      params: { is_active: isActive },
    })
    return response.data
  },

  setStatus: async (chatKey: string, status: 'active' | 'observe' | 'disabled'): Promise<ActionResponse> => {
    const response = await axios.post<ActionResponse>(`/chat-channel/${chatKey}/status`, null, {
      params: { status },
    })
    return response.data
  },

  reset: async (chatKey: string): Promise<ActionResponse> => {
    const response = await axios.post<ActionResponse>(`/chat-channel/${chatKey}/reset`)
    return response.data
  },

  getMessages: async (params: {
    chat_key: string
    before_id?: number
    page_size?: number
  }): Promise<ChatMessageListResponse> => {
    const response = await axios.get<ChatMessageListResponse>(
      `/chat-channel/${params.chat_key}/messages`,
      {
      params: { 
        before_id: params.before_id,
        page_size: params.page_size || 32,
      },

      }
    )
    return response.data
  },

  /**
   * 预览换人设的影响面
   *
   * 人设是隔离键的一维：换掉它，本频道整体切到另一份好感与记忆上，群里所有人的好感
   * **当场看起来归零**（实为切到另一份，旧的还在）。所以设置之前必须先把这几个数
   * 摆给运营看，否则那一刻会被当成数据丢失。
   */
  getPresetImpact: async (
    chatKey: string,
    presetId: number | null
  ): Promise<PresetSwitchImpact> => {
    const response = await axios.get<PresetSwitchImpact>(
      `/chat-channel/${encodeURIComponent(chatKey)}/preset-impact`,
      { params: { preset_id: presetId } }
    )
    return response.data
  },

  /**
   * 设置频道人设
   *
   * `confirmed` 不是走过场：生效人设真的变了而没带它时，后端会 400 并把影响面写进理由。
   * 那道闸是给**别的调用方**（curl、脚本、以后新加的页面）留的，前端这边先弹确认框。
   */
  setPreset: async (
    chatKey: string,
    presetId: number | null,
    confirmed = false
  ): Promise<ActionResponse> => {
    const response = await axios.post<ActionResponse>(`/chat-channel/${chatKey}/preset`, null, {
      params: { preset_id: presetId, confirmed },
    })
    return response.data
  },

  setCustomChannelName: async (chatKey: string, customChannelName: string | null): Promise<ActionResponse> => {
    const response = await axios.put<ActionResponse>(`/chat-channel/${encodeURIComponent(chatKey)}/custom-name`, {
      custom_channel_name: customChannelName,
    })
    return response.data
  },

  sendMessage: async (chatKey: string, message: string, file?: File, senderType?: 'bot' | 'system' | 'none'): Promise<ActionResponse> => {
    const formData = new FormData()
    formData.append('message', message)
    if (file) {
      formData.append('file', file)
    }
    if (senderType) {
      formData.append('sender_type', senderType)
    }
    const response = await axios.post<ActionResponse>(`/chat-channel/${chatKey}/send`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return response.data
  },

  submitAnnouncement: async (payload: {
    content: string
    scope: AnnouncementScope
  }): Promise<AnnouncementSubmitResponse> => {
    const response = await axios.post<AnnouncementSubmitResponse>('/chat-channel/announcement', payload)
    return response.data
  },

  previewAnnouncement: async (scope: AnnouncementScope): Promise<AnnouncementPreview> => {
    const response = await axios.post<AnnouncementPreview>('/chat-channel/announcement/preview', { scope })
    return response.data
  },

  getAnnouncementInstances: async (): Promise<AnnouncementInstanceOption[]> => {
    const response = await axios.get<{ items: AnnouncementInstanceOption[] }>(
      '/chat-channel/announcement/instances'
    )
    return response.data.items
  },

  getAnnouncementHistory: async (params: {
    page: number
    page_size: number
  }): Promise<AnnouncementListResponse> => {
    const response = await axios.get<AnnouncementListResponse>('/chat-channel/announcement', { params })
    return response.data
  },

  getAnnouncementDetail: async (announcementId: number): Promise<AnnouncementDetail> => {
    const response = await axios.get<AnnouncementDetail>(`/chat-channel/announcement/${announcementId}`)
    return response.data
  },

  cancelAnnouncement: async (announcementId: number): Promise<ActionResponse> => {
    const response = await axios.post<ActionResponse>(
      `/chat-channel/announcement/${announcementId}/cancel`
    )
    return response.data
  },

  subscribeAnnouncementEvents: (
    announcementId: number,
    options: {
      onEvent: (event: AnnouncementEvent) => void
      onError?: (error: Error) => void
    }
  ): (() => void) => {
    /**
     * Subscribe to announcement task progress via SSE.
     * The backend always replays a `snapshot` event on (re)connect, so a dropped
     * connection never leaves the UI with a stale or partial progress state.
     */
    return createEventStream({
      endpoint: `/chat-channel/announcement/${announcementId}/events`,
      onEvent: (eventName, data) => {
        const event = parseAnnouncementEvent(eventName, data)
        if (event) options.onEvent(event)
      },
      onError: options.onError,
    })
  },

  getMessageVideoAccess: async (chatKey: string, messageId: string): Promise<MessageVideoAccess> => {
    const response = await axios.get<MessageVideoAccess>(
      `/chat-channel/${encodeURIComponent(chatKey)}/messages/${messageId}/video-access`
    )
    return response.data
  },

  getUsers: async (chatKey: string): Promise<ChatChannelUsersResponse> => {
    const response = await axios.get<ChatChannelUsersResponse>(`/chat-channel/${chatKey}/users`)
    return response.data
  },

  setUserCommandPermission: async (
    chatKey: string,
    platformUserId: string,
    permission: 'advanced' | 'user'
  ): Promise<ActionResponse> => {
    const response = await axios.put<ActionResponse>(
      `/chat-channel/${encodeURIComponent(chatKey)}/users/${encodeURIComponent(platformUserId)}/command-permission`,
      { permission }
    )
    return response.data
  },

  sendPoke: async (chatKey: string, targetUserId: string): Promise<ActionResponse> => {
    const response = await axios.post<ActionResponse>(`/chat-channel/${chatKey}/poke`, {
      target_user_id: targetUserId,
    })
    return response.data
  },

  streamMessages: (chatKey: string, onMessage: (msg: ChatMessage) => void, onError?: (error: Error) => void): (() => void) => {
    /**
     * Subscribe to real-time messages for a chat channel using SSE
     * @param chatKey - The chat channel key
     * @param onMessage - Callback when a new message is received
     * @param onError - Optional error callback
     * @returns Cleanup function to unsubscribe
     */
    return createEventStream({
      endpoint: `/chat-channel/${chatKey}/stream`,
      onMessage: (data: string) => {
        const trimmedData = data.trim()
        if (!trimmedData || !trimmedData.startsWith('{')) return
        try {
          const message = JSON.parse(trimmedData) as ChatMessage
          onMessage(message)
        } catch (error) {
          console.error('Failed to parse message:', error)
        }
      },
      onError,
    })
  },

  getPluginData: async (chatKey: string, params?: {
    plugin_key?: string
    page?: number
    page_size?: number
  }): Promise<ChatPluginDataResponse> => {
    const response = await axios.get<ChatPluginDataResponse>(`/chat-channel/${chatKey}/plugin-data`, { params })
    return response.data
  },

  updatePluginData: async (chatKey: string, dataId: number, dataValue: string): Promise<ActionResponse> => {
    const response = await axios.put<ActionResponse>(`/chat-channel/${chatKey}/plugin-data/${dataId}`, {
      data_value: dataValue,
    })
    return response.data
  },

  deletePluginData: async (chatKey: string, dataId: number): Promise<ActionResponse> => {
    const response = await axios.delete<ActionResponse>(`/chat-channel/${chatKey}/plugin-data/${dataId}`)
    return response.data
  },

  getDeletePreview: async (chatKey: string): Promise<ChannelDeletePreview> => {
    const response = await axios.get<ChannelDeletePreview>(`/chat-channel/${encodeURIComponent(chatKey)}/delete-preview`)
    return response.data
  },

  deleteChannel: async (chatKey: string): Promise<ActionResponse> => {
    const response = await axios.delete<ActionResponse>(`/chat-channel/${encodeURIComponent(chatKey)}`)
    return response.data
  },

  streamChannels: (onMessage: (event: ChannelListStreamEvent) => void, onError?: (error: Error) => void): (() => void) => {
    /**
     * Subscribe to real-time channel list updates using SSE
     * @param onMessage - Callback when a channel event is received (created, updated, deleted, activated, deactivated)
     * @param onError - Optional error callback
     * @returns Cleanup function to unsubscribe
     */
    return channelListStreamManager.subscribe({
      onMessage: (data: string) => {
        const trimmedData = data.trim()
        if (!trimmedData || !trimmedData.startsWith('{')) return
        try {
          const event = JSON.parse(trimmedData)
          onMessage(event)
        } catch (error) {
          console.error('Failed to parse channel event:', error)
        }
      },
      onError,
    })
  },
} 
