import axios from './axios'

/**
 * 服务商预设键：清单以服务端为准（`GET /instance-email/providers`），这里只列页面上要特殊处理的几个。
 * `string & {}` 让其余预设键（126、腾讯企业邮……）也能放进来，同时保留这几个字面量的补全。
 */
export type EmailProvider = 'QQ邮箱' | '163邮箱' | 'Gmail' | 'Outlook' | '自定义' | (string & {})
export type EmailAuthType = 'password' | 'oauth2'
export type EmailTransportType = 'imap_smtp' | 'gmail_api' | 'microsoft_graph'
export type EmailOAuthProvider = '' | 'google' | 'microsoft'
/** 平台方邮箱共享给哪些租户实例发信：所有实例 / 只给列出的 / 不共享（平台方实例不受限制） */
export type EmailSendShare = 'all_instances' | 'listed' | 'none'

export interface EmailAccount {
  index?: number
  DISPLAY_NAME: string
  DISPLAY_LABEL?: string
  EMAIL_ACCOUNT: EmailProvider
  CUSTOM_IMAP_HOST: string
  CUSTOM_IMAP_PORT: number
  CUSTOM_SMTP_HOST: string
  CUSTOM_SMTP_PORT: number
  CUSTOM_SMTP_SSL_PORT: number
  CUSTOM_SMTP_USE_SSL: boolean
  /** 平台方「自定义」服务器：不校验服务器证书（默认关，只给内网自签证书的邮件服务器用） */
  SKIP_TLS_VERIFY?: boolean
  ENABLED: boolean
  RECEIVE_ENABLED?: boolean
  USE_PROXY: boolean
  USERNAME: string
  PASSWORD?: string
  HAS_PASSWORD?: boolean
  AUTH_TYPE: EmailAuthType
  TRANSPORT_TYPE: EmailTransportType
  OAUTH_PROVIDER: EmailOAuthProvider
  CLIENT_ID: string
  CLIENT_SECRET?: string
  TENANT_ID: string
  ACCESS_TOKEN?: string
  REFRESH_TOKEN?: string
  TOKEN_EXPIRES_AT: number
  LAST_TEST_SUCCESS?: boolean | null
  LAST_TEST_MESSAGE?: string
  LAST_TEST_TIME?: number
  SEND_ENABLED: boolean
  IS_DEFAULT_SENDER: boolean
  SEND_SHARE?: EmailSendShare
  /** 每项 `<adapter_key>:<instance_key>`，只在 `SEND_SHARE` 为 `listed` 时用 */
  SEND_SHARE_INSTANCES?: string[]
  /** 内部标记（从实例转回的账户），页面不显示也不改，保存时原样带回 */
  TENANT_SUPPLIED_HOSTS?: string
}

export interface EmailAccountsResponse {
  accounts: EmailAccount[]
}

export interface EmailAuthorizeUrlResponse {
  authorize_url: string
  state: string
}

export interface EmailPullError {
  stage: string
  email_id?: string
  message: string
}

export interface EmailPullDebugStep {
  stage: string
  [key: string]: unknown
}

export interface EmailPullResult {
  success: boolean
  account_index?: number
  account_username: string
  provider?: EmailProvider
  auth_type?: EmailAuthType
  transport_type?: EmailTransportType
  mailbox: string
  search_unseen_only: boolean
  mark_as_seen_after_fetch: boolean
  max_per_poll: number
  requested_limit?: number | null
  effective_limit: number
  found_count: number
  processed_count: number
  failed_count: number
  marked_seen_count: number
  mark_seen_failed_count: number
  skipped_count: number
  reconnect_attempted: boolean
  reconnect_success?: boolean | null
  started_at: number
  finished_at: number
  duration_ms: number
  errors: EmailPullError[]
  debug_steps: EmailPullDebugStep[]
}

export const emailApi = {
  getAccounts: async (): Promise<EmailAccountsResponse> => {
    const response = await axios.get<EmailAccountsResponse>('/adapters/email/accounts')
    return response.data
  },

  createAccount: async (account: EmailAccount): Promise<{ success: boolean; index: number }> => {
    const response = await axios.post<{ success: boolean; index: number }>('/adapters/email/accounts', account)
    return response.data
  },

  updateAccount: async (index: number, account: Partial<EmailAccount>): Promise<{ success: boolean }> => {
    const response = await axios.put<{ success: boolean }>(`/adapters/email/accounts/${index}`, account)
    return response.data
  },

  deleteAccount: async (index: number): Promise<{ success: boolean }> => {
    const response = await axios.delete<{ success: boolean }>(`/adapters/email/accounts/${index}`)
    return response.data
  },

  /**
   * 把平台方的一个账户转为某个实例自有（只支持授权码方式）
   *
   * `expectedUsername` 是页面上看到的那个地址：账户按数组下标定位，列表加载之后别人加删过账户时同一个下标
   * 就是别的邮箱，服务端对不上地址会回 409，不会把别的邮箱转出去。
   */
  transferToInstance: async (
    index: number,
    expectedUsername: string,
    target: { adapter_key: string; instance_key: string }
  ): Promise<{ success: boolean; id: number }> => {
    const response = await axios.post<{ success: boolean; id: number }>(
      `/adapters/email/accounts/${index}/to-instance`,
      { ...target, expected_username: expectedUsername }
    )
    return response.data
  },

  testAccount: async (index: number): Promise<{ success: boolean; message: string; tested_at: number }> => {
    const response = await axios.post<{ success: boolean; message: string; tested_at: number }>(`/adapters/email/accounts/${index}/test`)
    return response.data
  },

  pullAccountInbox: async (
    index: number,
    payload: { unseen_only?: boolean; limit?: number } = { unseen_only: false }
  ): Promise<EmailPullResult> => {
    const response = await axios.post<EmailPullResult>(`/adapters/email/accounts/${index}/pull`, payload)
    return response.data
  },

  createAuthorizeUrl: async (
    index: number,
    redirectUri: string,
    state: string
  ): Promise<EmailAuthorizeUrlResponse> => {
    const response = await axios.post<EmailAuthorizeUrlResponse>(`/adapters/email/accounts/${index}/oauth/authorize-url`, {
      redirect_uri: redirectUri,
      state,
    })
    return response.data
  },

  handleOAuthCallback: async (index: number, code: string, redirectUri: string): Promise<{ success: boolean }> => {
    const response = await axios.post<{ success: boolean }>(`/adapters/email/accounts/${index}/oauth/callback`, {
      code,
      redirect_uri: redirectUri,
    })
    return response.data
  },
}
