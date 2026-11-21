/**
 * 实例邮箱（`/api/instance-email/*`）
 *
 * 这一族接口回答的都是「某个实例的邮箱」：参数一律带 `adapter_key` + `instance_key`，
 * 平台方也要带。越权与不存在由服务端回同一个 404。
 *
 * 凭据只写不读：列表与详情只回 `HAS_PASSWORD`，修改时 `password` 留空表示不改
 * （但改了服务商或服务器地址时服务端要求重新填）。
 */

import axios from './axios'

export interface MailboxScope {
  adapter_key: string
  instance_key: string
}

/** 服务商预设（`自定义` 那一项 `is_custom` 为真，排在最后） */
export interface MailProviderPreset {
  key: string
  imap_host: string
  imap_port: number | null
  smtp_host: string
  smtp_port: number | null
  smtp_ssl_port: number | null
  smtp_use_ssl: boolean
  is_custom: boolean
  /** 页面上照写的那句说明（去哪里开授权码等） */
  note: string
}

export const CUSTOM_PROVIDER_KEY = '自定义'

export interface CustomMailSettings {
  imap_host?: string
  imap_port?: number
  smtp_host?: string
  smtp_port?: number
  smtp_ssl_port?: number
  smtp_use_ssl?: boolean
  last_test_success?: boolean | null
  last_test_message?: string
  last_test_time?: number
  last_test_smtp?: boolean
  last_test_imap?: boolean
  last_receive_error?: string
  [key: string]: unknown
}

export interface InstanceMailAccount {
  id: number
  adapter_key: string
  instance_key: string
  username: string
  display_name: string
  provider: string
  settings: CustomMailSettings
  HAS_PASSWORD: boolean
  enabled: boolean
  receive_enabled: boolean
  send_enabled: boolean
  is_default_sender: boolean
  notify_chat_key: string
  attachment_dir: string
}

export interface InstanceMailAccountPayload {
  username: string
  password: string
  display_name: string
  provider: string
  settings: CustomMailSettings
  enabled: boolean
  receive_enabled: boolean
  send_enabled: boolean
  is_default_sender: boolean
  notify_chat_key: string
}

export interface PlatformMailAccount {
  display_label: string
  account_ref: string
  is_default_sender: boolean
}

export interface ConnectionTestPart {
  success: boolean
  message: string
}

export interface ConnectionTestResult {
  success: boolean
  smtp: ConnectionTestPart
  imap: ConnectionTestPart
  message: string
  tested_at: number
}

export interface SendLogItem {
  id: number
  account_kind: 'platform' | 'instance' | string
  display_label: string
  purpose: 'send_email' | 'festival' | string
  to_address: string
  subject: string
  reason: string
  ok: boolean
  error: string
  create_time: string
}

export interface UserPrefItem {
  id: number
  platform_userid: string
  email: string
  festival_subscribed: boolean
  unsubscribed: boolean
  unsubscribed_at: string | null
  set_in_chat_key: string
}

export interface PagedResult<T> {
  items: T[]
  total: number
}

export type OutboxStatus = 'queued' | 'paused' | 'sent' | 'failed' | 'skipped'

export interface OutboxBatch {
  batch_key: string
  festival_name: string
  date: string
  counts: Partial<Record<OutboxStatus, number>>
  first_queued_at: string | null
}

const params = (scope: MailboxScope, extra?: Record<string, unknown>) => ({
  adapter_key: scope.adapter_key,
  instance_key: scope.instance_key,
  ...(extra ?? {}),
})

export const mailboxApi = {
  listProviders: async (): Promise<MailProviderPreset[]> => {
    const response = await axios.get<MailProviderPreset[]>('/instance-email/providers')
    return response.data
  },

  listAccounts: async (scope: MailboxScope): Promise<InstanceMailAccount[]> => {
    const response = await axios.get<InstanceMailAccount[]>('/instance-email/accounts', {
      params: params(scope),
    })
    return response.data
  },

  createAccount: async (
    scope: MailboxScope,
    payload: InstanceMailAccountPayload
  ): Promise<InstanceMailAccount> => {
    const response = await axios.post<InstanceMailAccount>('/instance-email/accounts', payload, {
      params: params(scope),
    })
    return response.data
  },

  updateAccount: async (
    scope: MailboxScope,
    accountId: number,
    payload: InstanceMailAccountPayload
  ): Promise<InstanceMailAccount> => {
    const response = await axios.put<InstanceMailAccount>(
      `/instance-email/accounts/${accountId}`,
      payload,
      { params: params(scope) }
    )
    return response.data
  },

  deleteAccount: async (scope: MailboxScope, accountId: number): Promise<{ success: boolean }> => {
    const response = await axios.delete<{ success: boolean }>(`/instance-email/accounts/${accountId}`, {
      params: params(scope),
    })
    return response.data
  },

  testAccount: async (scope: MailboxScope, accountId: number): Promise<ConnectionTestResult> => {
    const response = await axios.post<ConnectionTestResult>(
      `/instance-email/accounts/${accountId}/test`,
      undefined,
      { params: params(scope) }
    )
    return response.data
  },

  fetchAccount: async (
    scope: MailboxScope,
    accountId: number
  ): Promise<{ success: boolean; fetched: number }> => {
    const response = await axios.post<{ success: boolean; fetched: number }>(
      `/instance-email/accounts/${accountId}/fetch`,
      undefined,
      { params: params(scope) }
    )
    return response.data
  },

  /** 平台方专属：把实例自有邮箱转回平台方 */
  transferToPlatform: async (
    scope: MailboxScope,
    accountId: number
  ): Promise<{ success: boolean; id: number; attachment_dir: string }> => {
    const response = await axios.post<{ success: boolean; id: number; attachment_dir: string }>(
      `/instance-email/accounts/${accountId}/to-platform`,
      undefined,
      { params: params(scope) }
    )
    return response.data
  },

  listPlatformAccounts: async (scope: MailboxScope): Promise<PlatformMailAccount[]> => {
    const response = await axios.get<PlatformMailAccount[]>('/instance-email/platform-accounts', {
      params: params(scope),
    })
    return response.data
  },

  listSendLogs: async (
    scope: MailboxScope,
    offset: number,
    limit: number
  ): Promise<PagedResult<SendLogItem>> => {
    const response = await axios.get<PagedResult<SendLogItem>>('/instance-email/send-logs', {
      params: params(scope, { offset, limit }),
    })
    return response.data
  },

  listUserPrefs: async (
    scope: MailboxScope,
    offset: number,
    limit: number
  ): Promise<PagedResult<UserPrefItem>> => {
    const response = await axios.get<PagedResult<UserPrefItem>>('/instance-email/user-prefs', {
      params: params(scope, { offset, limit }),
    })
    return response.data
  },

  deleteUserPref: async (
    scope: MailboxScope,
    prefId: number
  ): Promise<{ success: boolean; unsubscribed: boolean }> => {
    const response = await axios.delete<{ success: boolean; unsubscribed: boolean }>(
      `/instance-email/user-prefs/${prefId}`,
      { params: params(scope) }
    )
    return response.data
  },

  listOutboxBatches: async (scope: MailboxScope): Promise<OutboxBatch[]> => {
    const response = await axios.get<OutboxBatch[]>('/instance-email/outbox/batches', {
      params: params(scope),
    })
    return response.data
  },

  /** 平台方专属：暂停 / 恢复 / 作废一批节日邮件 */
  outboxAction: async (
    scope: MailboxScope,
    action: 'pause' | 'resume' | 'void',
    batchKey: string
  ): Promise<{ success: boolean; changed: number }> => {
    const response = await axios.post<{ success: boolean; changed: number }>(
      `/instance-email/outbox/batches/${action}`,
      { ...params(scope), batch_key: batchKey }
    )
    return response.data
  },
}
