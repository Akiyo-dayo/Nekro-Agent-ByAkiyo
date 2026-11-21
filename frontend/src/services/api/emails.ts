import axios from './axios'

export interface Email {
  id: number
  account_username: string
  subject: string
  sender: string
  date: string | null
  body_preview: string
  has_attachments: boolean
  create_time: string
}

export interface EmailListResponse {
  items: Email[]
  pagination: {
    offset: number
    limit: number
    total: number
    has_more: boolean
  }
}

export interface EmailDetail {
  id: number
  account_username: string
  email_uid: string
  message_id: string
  subject: string
  sender: string
  recipients: string
  date: string | null
  body_text: string
  has_attachments: boolean
  attachment_names: string
  attachments: EmailAttachment[]
  in_reply_to: string
  references: string
  create_time: string
}

export interface EmailAttachment {
  name: string
  url: string
  extension: string
  preview_type: 'image' | 'markdown' | 'text' | 'pdf' | 'download'
}

export interface EmailRawContent {
  html_content: string
  text_content: string
}

/**
 * 看哪个邮箱：不传是平台方邮箱（与改造前相同），传了是那个实例的自有邮箱
 *
 * 服务端按它判归属：租户必须传，平台方传了就看那个实例的。附件链接（`/api/emails/{id}/attachments/…`）
 * 本身不带实例参数，要由这里补上，否则实例邮箱的附件一律 404。
 */
export interface EmailScope {
  adapter_key: string
  instance_key: string
}

function scopeParams(scope?: EmailScope): Record<string, string> {
  return scope ? { adapter_key: scope.adapter_key, instance_key: scope.instance_key } : {}
}

/**
 * 按 Authorization 头取回一个附件（axios 实例的拦截器带上令牌）
 *
 * **不把登录令牌拼进链接**：令牌是长期有效的，拼进 `img` / `iframe` / `a` 的链接就会落进 uvicorn 与反向代理的
 * 访问日志和浏览器历史。图片、PDF、下载一律先取回成 Blob，再用 `blob:` 链接显示（见 `AttachmentPreview`）。
 * 附件链接是服务端给的同源绝对路径（`/api/emails/…`），所以这里清掉 axios 实例的 `baseURL`，与原来直接
 * `fetch` 这条路径的效果相同。
 */
async function fetchAttachment(url: string, scope?: EmailScope): Promise<Blob> {
  const response = await axios.get<Blob>(url, {
    baseURL: '',
    params: scopeParams(scope),
    responseType: 'blob',
    timeout: 120000,
  })
  return response.data
}

export const emailsApi = {
  getEmails: async (params: { offset: number; limit: number; account?: string }, scope?: EmailScope) => {
    const response = await axios.get<EmailListResponse>('/emails', {
      params: { ...params, ...scopeParams(scope) },
    })
    return response.data
  },

  getEmailDetail: async (emailId: number, scope?: EmailScope) => {
    const response = await axios.get<EmailDetail>(`/emails/${emailId}`, { params: scopeParams(scope) })
    return response.data
  },

  getEmailRawContent: async (emailId: number, scope?: EmailScope) => {
    const response = await axios.get<EmailRawContent>(`/emails/${emailId}/raw-content`, {
      params: scopeParams(scope),
    })
    return response.data
  },

  getAttachmentText: async (url: string, scope?: EmailScope) => (await fetchAttachment(url, scope)).text(),

  getAttachmentBlob: fetchAttachment,

  /** 取回附件并让浏览器另存（`blob:` 链接用完即撤） */
  downloadAttachment: async (url: string, filename: string, scope?: EmailScope) => {
    const blob = await fetchAttachment(url, scope)
    const objectUrl = URL.createObjectURL(blob)
    try {
      const link = document.createElement('a')
      link.href = objectUrl
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
    } finally {
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
    }
  },
}
