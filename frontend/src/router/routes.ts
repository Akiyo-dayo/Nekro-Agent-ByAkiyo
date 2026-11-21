export const WORKSPACE_DETAIL_TABS = [
  'overview',
  'sandbox',
  'comm',
  'memory',
  'knowledge',
  'extensions',
  'mcp',
  'resources',
  'servers',
  'prompt',
  'config',
] as const

export type WorkspaceDetailTab = (typeof WORKSPACE_DETAIL_TABS)[number]

export const DEFAULT_WORKSPACE_DETAIL_TAB: WorkspaceDetailTab = 'overview'

export const isWorkspaceDetailTab = (value: string | null | undefined): value is WorkspaceDetailTab =>
  value !== undefined && value !== null && WORKSPACE_DETAIL_TABS.includes(value as WorkspaceDetailTab)

export const workspaceListPath = () => '/workspace'

export const workspaceDetailPath = (
  workspaceId: number | string,
  tab: WorkspaceDetailTab = DEFAULT_WORKSPACE_DETAIL_TAB
) => `/workspace/${workspaceId}/${tab}`

export const pluginsManagementPath = (pluginId?: string | null) =>
  pluginId ? `/plugins/management/${encodeURIComponent(pluginId)}` : '/plugins/management'

export const CHAT_CHANNEL_DETAIL_TABS = [
  'message-history',
  'override-settings',
  'basic-info',
  'plugin-data',
  'command-permissions',
  'memory',
] as const

export type ChatChannelDetailTab = (typeof CHAT_CHANNEL_DETAIL_TABS)[number]

export const DEFAULT_CHAT_CHANNEL_DETAIL_TAB: ChatChannelDetailTab = 'message-history'

export const isChatChannelDetailTab = (value: string | null | undefined): value is ChatChannelDetailTab =>
  value !== undefined && value !== null && CHAT_CHANNEL_DETAIL_TABS.includes(value as ChatChannelDetailTab)

export const chatChannelPath = (chatKey?: string | null, tab?: ChatChannelDetailTab | null) =>
  chatKey
    ? tab
      ? `/chat-channel/${encodeURIComponent(chatKey)}/${tab}`
      : `/chat-channel/${encodeURIComponent(chatKey)}`
    : '/chat-channel'
export const chatChannelAnnouncementPath = () => '/chat-channel/announcement'

export const loginPath = (redirectTo?: string | null) =>
  redirectTo ? `/login?redirect=${encodeURIComponent(redirectTo)}` : '/login'

export const presetsPath = (options?: { editRemoteId?: string | null }) => {
  if (!options?.editRemoteId) return '/presets'

  const searchParams = new URLSearchParams({ editRemoteId: options.editRemoteId })
  return `/presets?${searchParams.toString()}`
}

export const sanitizeRedirectTarget = (target: string | null | undefined) => {
  if (!target) return null
  if (!target.startsWith('/')) return null
  if (target.startsWith('//')) return null
  if (target.startsWith('/login')) return null
  return target
}

/**
 * 当前应用 path（保留 query，不含外层 hash）。
 * 用于登录重定向等场景，确保手动打开的页面状态可在重新登录后恢复。
 */
export const getCurrentAppPath = () => {
  if (typeof window === 'undefined') return '/'

  const hash = window.location.hash
  if (!hash.startsWith('#')) return '/'

  const currentPath = hash.slice(1)
  return currentPath || '/'
}

export const MEMORY_PAGE_TABS = ['items', 'quarantine', 'summaries', 'prompt-rounds'] as const

export type MemoryPageTab = (typeof MEMORY_PAGE_TABS)[number]

export const DEFAULT_MEMORY_PAGE_TAB: MemoryPageTab = 'items'

export const isMemoryPageTab = (value: string | null | undefined): value is MemoryPageTab =>
  value !== undefined && value !== null && MEMORY_PAGE_TABS.includes(value as MemoryPageTab)

/**
 * 记忆页地址
 *
 * 标签属于主视图，进 path；筛选与分页属于可恢复状态，由页面自己写进 query，
 * 这里只负责把调用方给的那份字符串参数拼上（空值不写）。
 */
export const memoryPath = (
  tab: MemoryPageTab = DEFAULT_MEMORY_PAGE_TAB,
  query?: Record<string, string>
) => {
  const base = `/memory/${tab}`
  if (!query) return base
  const params = new URLSearchParams()
  Object.entries(query).forEach(([key, value]) => {
    if (value) params.set(key, value)
  })
  const search = params.toString()
  return search ? `${base}?${search}` : base
}

export const WORLDBOOK_PAGE_TABS = ['books', 'quarantine', 'test'] as const

export type WorldbookPageTab = (typeof WORLDBOOK_PAGE_TABS)[number]

export const DEFAULT_WORLDBOOK_PAGE_TAB: WorldbookPageTab = 'books'

export const isWorldbookPageTab = (
  value: string | null | undefined
): value is WorldbookPageTab =>
  value !== undefined &&
  value !== null &&
  WORLDBOOK_PAGE_TABS.includes(value as WorldbookPageTab)

/**
 * 世界书页地址
 *
 * 标签属于主视图，进 path；筛选与分页属于可恢复状态，由页面自己写进 query，
 * 这里只负责把调用方给的那份字符串参数拼上（空值不写）。
 */
export const worldbookPath = (
  tab: WorldbookPageTab = DEFAULT_WORLDBOOK_PAGE_TAB,
  query?: Record<string, string>
) => {
  const base = `/worldbook/${tab}`
  if (!query) return base
  const params = new URLSearchParams()
  Object.entries(query).forEach(([key, value]) => {
    if (value) params.set(key, value)
  })
  const search = params.toString()
  return search ? `${base}?${search}` : base
}

export const worldbookBookPath = (bookId: number | string) => `/worldbook/books/${bookId}`

export const MAILBOX_PAGE_TABS = ['accounts', 'mails', 'send-logs', 'user-prefs', 'festival'] as const

export type MailboxPageTab = (typeof MAILBOX_PAGE_TABS)[number]

export const DEFAULT_MAILBOX_PAGE_TAB: MailboxPageTab = 'accounts'

export const isMailboxPageTab = (value: string | null | undefined): value is MailboxPageTab =>
  value !== undefined && value !== null && MAILBOX_PAGE_TABS.includes(value as MailboxPageTab)

/** 邮箱页地址：标签进 path，与世界书页同一形态；不认识的标签由页面重定向 */
export const mailboxPath = (tab: MailboxPageTab = DEFAULT_MAILBOX_PAGE_TAB) => `/mailbox/${tab}`

export const PROMPT_LAB_PAGE_TABS = ['preview', 'trial', 'audit'] as const

export type PromptLabPageTab = (typeof PROMPT_LAB_PAGE_TABS)[number]

export const DEFAULT_PROMPT_LAB_PAGE_TAB: PromptLabPageTab = 'preview'

export const isPromptLabPageTab = (
  value: string | null | undefined
): value is PromptLabPageTab =>
  value !== undefined &&
  value !== null &&
  PROMPT_LAB_PAGE_TABS.includes(value as PromptLabPageTab)

/**
 * 提示词预览页地址
 *
 * 标签属于主视图，进 path：分享一个「试发」结果时打开的就是试发标签，而不是先落到
 * 预览再由页面自己跳一次。与 `worldbookPath` 同一形态，不认识的标签由页面重定向。
 */
export const promptLabPath = (tab: PromptLabPageTab = DEFAULT_PROMPT_LAB_PAGE_TAB) =>
  `/prompt-lab/${tab}`
