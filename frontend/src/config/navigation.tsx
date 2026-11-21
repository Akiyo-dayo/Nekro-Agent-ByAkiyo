import {
  Terminal as TerminalIcon,
  Settings as SettingsIcon,
  Storage as StorageIcon,
  Tune as TuneIcon,
  Extension as ExtensionIcon,
  Chat as ChatIcon,
  Campaign as CampaignIcon,
  Code as CodeIcon,
  Dashboard as DashboardIcon,
  Group as GroupIcon,
  Face as FaceIcon,
  Favorite as FavoriteIcon,
  Psychology as PsychologyIcon,
  MenuBook as MenuBookIcon,
  MailOutline as MailOutlineIcon,
  AccountCircle as AccountCircleIcon,
  CloudDownload as CloudDownloadIcon,
  Palette as PaletteIcon,
  Hub as HubIcon,
  CleaningServices as CleaningServicesIcon,
  Workspaces as WorkspacesIcon,
  ListAlt as ListAltIcon,
  Schedule as ScheduleIcon,
  SystemUpdateAlt as SystemUpdateAltIcon,
  SpaceDashboard as SpaceDashboardIcon,
  SmartToy as SmartToyIcon,
  Business as BusinessIcon,
  Speed as SpeedIcon,
  Security as SecurityIcon,
  History as HistoryIcon,
  Paid as PaidIcon,
  Science as ScienceIcon,
  VpnKey as VpnKeyIcon,
} from '@mui/icons-material'
import i18next from './i18n'

/**
 * 菜单项的可见性开关
 *
 * 多租户下同一套 UI 按身份收敛：租户身份看不到平台级入口，
 * 未启用多租户时租户相关入口整体不出现。这里只声明「受哪一个开关控制」，
 * 具体取值由 `MainLayout` 依据当前身份计算。
 */
export type MenuVisibilityKey =
  | 'platformOnly'
  | 'tenantAdminOnly'
  | 'tenantManagement'
  | 'tenantModelGroups'
  | 'tenantSelfService'
  | 'instanceConfigOverrides'

export interface MenuVisibility {
  /** 平台方专属入口（平台级系统设置、用户管理、云服务等） */
  platformOnly: boolean
  /**
   * 至少要 `Role.Admin` 才够得着的入口（平台方 + 租户管理员可见，**只读租户成员不可见**）
   *
   * 这一档与 `platformOnly` 的区别是**挡谁**：`platformOnly` 挡整个租户，这一档只挡
   * 租户里的只读成员（`tenant_user` → `tenant_perm_level()` → `Role.User`）。
   *
   * 判据不是"感觉上像管理功能"，而是**这一页要打的每一条读请求都带
   * `@require_role(Role.Admin)`**。逐条核过（`nekro_agent/routers/`）：
   *
   * | 入口 | 它的读接口 | 只读成员 |
   * |---|---|---|
   * | `/console`、`/console/instances` | `GET /console/overview`、`GET /console/instances` | 403 |
   * | `/console/quota` | `GET /console/quota/instances` | 403 |
   * | `/console/usage` | `GET /console/usage/summary` 一族 | 403 |
   * | `/console/identity` | `GET /console/identity/domains` 一族 | 403 |
   * | `/chat-channel/management`、`/chat-channel/announcement` | `routers/chat_channel.py` 全族 | 403 |
   * | `/affection`、`/presets`、`/plugins/management`、`/workspace/*` | 各自路由全族 | 403 |
   *
   * **不列进来的两类**：`/console/team`（`GET /tenants/id` 与成员列表刻意对成员开放，
   * 见 `TENANT_READABLE_BY_MEMBERS`）、`/dashboard`（前端默认落地页，后端无角色守卫）。
   *
   * 为什么是"藏入口"而不是"把只读端点降级"：降级要重新判定每一族的作用域收敛，
   * 面积是几十个端点，而只读成员真正要看的东西（自己团队、名下 Bot 的运行概况）
   * `/console/team` 与 `/dashboard` 已经给了。见本轮报告里的取舍说明。
   *
   * 藏入口只是第一层，直接敲 URL 由 `TENANT_ADMIN_ONLY_PATHS` 兜住；两层都做完，
   * 页面自己**仍然**要有 403 分支——那是这一轮真正在修的东西（把 403 渲染成 0）。
   */
  tenantAdminOnly: boolean
  /** 租户管理入口；平台方名下没有任何租户时不出现 */
  tenantManagement: boolean
  /**
   * 总后台下的模型组入口
   *
   * 只对租户身份开放。模型组只有一个页面实现，平台方从「系统配置 → 模型管理」进入，
   * 租户从这里进入；两个入口同时出现在一个人的菜单里只会让人以为是两套东西。
   */
  tenantModelGroups: boolean
  /**
   * 租户自助入口（「我的团队」）
   *
   * 只对租户身份开放：它渲染的是**自己这一个**租户，不是租户列表。平台方要管成员时
   * 从 `/console/tenants/{id}` 进，两个入口同时出现在一个人的菜单里只会让人以为
   * 是两套东西——与 `tenantModelGroups` 同一条理由。
   */
  tenantSelfService: boolean
  /**
   * 实例级配置覆盖页（`/settings/system`）
   *
   * 这一页对**租户管理员**是开的（见 `PLATFORM_ONLY_PATHS` 那段注释），但对
   * **只读租户成员**（`tenant_user` = `Role.User`）整页零可用：它要打的两条请求
   * `GET /console/instance-options` 与 `GET /config/instance/{adapter}/{instance}`
   * 都带 `@require_role(Role.Admin)`，双双 403。实测的后果比"点了才发现无权"更糟——
   * 实例列表因此恒为空，页面会把它说成「名下暂时没有实例」，而同一时刻
   * `/user/me/identity` 明写着 `accessible_instance_keys: ["inst1"]`。
   *
   * 与 `useIdentity.canOperateProtocol` 同一条既定立场：后端会 403 的入口不该画出来。
   */
  instanceConfigOverrides: boolean
}

/**
 * 未启用多租户时的默认可见性
 *
 * 平台级入口照常展示，租户相关入口全部隐藏，界面与多租户改造前完全一致。
 */
export const DEFAULT_MENU_VISIBILITY: MenuVisibility = {
  platformOnly: true,
  // 未启用多租户时登录的永远是平台方（`Role.Super`），这一档照常出现
  tenantAdminOnly: true,
  tenantManagement: false,
  tenantModelGroups: false,
  tenantSelfService: false,
  // 未启用多租户时登录的永远是平台方，这一格照常出现，界面与改造前逐字一致
  instanceConfigOverrides: true,
}

export interface PageConfig {
  path: string
  text: string
  translationKey?: string // 翻译键（可选，适配器配置可能没有）
  icon: JSX.Element
  parent?: string // 父菜单的 key
  visibility?: MenuVisibilityKey
}

export interface MenuGroup {
  key: string
  text: string
  translationKey?: string // 翻译键（可选，适配器配置可能没有）
  icon: JSX.Element
  children: PageConfig[]
  visibility?: MenuVisibilityKey
}

/**
 * 仅平台方可访问的路径前缀
 *
 * 菜单隐藏只是第一层；`MainLayout` 用这份清单做兜底，避免租户身份直接输入 URL 进入
 * 平台级页面。
 *
 * ⚠️ **它与页面配置里的 `visibility: 'platformOnly'` 是两份分开维护的清单**，
 * 只登记一份不会有任何运行时信号：只加 `visibility` 的话菜单藏住了、URL 照样进得去；
 * 只加这里的话页面进不去、菜单却还画着。新增平台方专属页面时两处都要写，
 * 由 `tests/test_console_platform_only_paths.py` 把两份对起来。
 */
export const PLATFORM_ONLY_PATHS = [
  '/console/tenants',
  // 宿主级的 iptables 隔离规则运维。菜单里已经标了 `visibility: 'platformOnly'`，
  // 但那只管**画不画这一格**；租户直接敲 URL 时拦住它的是下面这份清单。
  // 两份是分开维护的（`getPageConfigs()` 与本数组），漏登记的那一半没有任何运行时信号
  // ——`tests/test_console_platform_only_paths.py` 就是为此把两份对起来的看门人。
  '/console/network-rules',
  // 运维审计：一行可能同时提到平台方实例与租户会话，后端整条路由是
  // `require_platform_owner()`。菜单标 `visibility: 'platformOnly'`，这里管 URL 兜底，
  // 两份成对登记（`tests/test_console_platform_only_paths.py`）。
  '/console/ops-audit',
  // SSH 网关：全部凭据、网关状态、存量导入与全局审计都是平台方专属
  // （后端路由是 `Role.Super`）。菜单标 `visibility: 'platformOnly'`，这里管 URL 兜底。
  '/console/ssh-gateway',
  '/user-manager',
  '/plugins/editor',
  // 宿主级运行日志与沙盒日志：两族端点全是 `Role.Super`，日志里混着全部租户的数据。
  // 与 `visibility: 'platformOnly'` 成对登记，缺一份就是「藏了菜单、URL 照进」或反过来
  '/logs',
  '/sandbox-logs',
  // ⚠️ `/settings/system` **刻意不在这份清单里**（对照 `/console/quota`）。
  // 它是四层配置里「实例覆盖层」的唯一写入口：租户在实例作用域下改的是
  // `PUT /config/instance/{adapter}/{instance}`（后端接了 `assert_instance_accessible`
  // + `_assert_tenant_may_write`），共享层（system / adapter）的写入后端一律 403。
  // 登记在这里等于把「换模型组不用逐个频道点」整条链路封死 —— 页面自己按身份收敛，
  // 见 `pages/settings/system.tsx` 顶部的注释。
  '/settings/deployment',
  '/settings/models',
  '/settings/commands',
  '/settings/space-cleanup',
  // 社区账号主页（本部署那**一个** NekroAI 账号的发布物与收藏）现在挂在 `/cloud/profile`，
  // 由这条前缀一并兜住。它此前占着 `/profile`，把登录者自己的账号页挤成了死代码；
  // 两页按"主体是谁"拆开之后，`/profile` 是登录者本人的页面，**不再**是平台方专属 ——
  // 租户成员的自助改口令是它唯一的入口，登记在这里就等于继续锁死。
  '/cloud',
  // 网页聊天：整块能力只给平台方（适配器子路由每个端点都是 `Role.Admin` +
  // `require_platform_owner()`，租户身份一律 403）。菜单那一格标了
  // `visibility: 'platformOnly'`，这里管直接敲 URL 时拦住，两份成对登记。
  '/web-chat',
  // 网页适配器自己的后台页（含「MCP 密钥」页签）：同样是平台方专属的一整块能力，
  // 租户敲 `/adapters/web/mcp-auth` 会在每个请求上 403，所以连页面一起拦。
  '/adapters/web',
] as const

export const isPlatformOnlyPath = (pathname: string): boolean =>
  PLATFORM_ONLY_PATHS.some(prefix => pathname === prefix || pathname.startsWith(`${prefix}/`))

/**
 * 只读租户成员（`tenant_user`）够不着的路径前缀
 *
 * 与 `PLATFORM_ONLY_PATHS` 是同一个形态的第二份清单，只是挡的人不同（见
 * `MenuVisibility.tenantAdminOnly`）。同样是**两份分开维护**：这里管「直接敲 URL 进不进得去」，
 * 菜单那一格的 `visibility: 'tenantAdminOnly'` 管「画不画」。只登记一份没有任何运行时信号，
 * 由 `tests/test_readonly_member_console_surface.py` 把两份对起来。
 *
 * ⚠️ **这不是权限边界**，只是别把人送进一页必然 403 的界面。真正的边界永远在服务端。
 */
export const TENANT_ADMIN_ONLY_PATHS = [
  // 总后台：概览 / 实例列表 / 实例详情 / 新建向导 / 配额 / 身份与域 / 模型组。
  // `/console/team` 刻意不在此列，所以这里逐条写而不是写 `/console`
  '/console/instances',
  '/console/quota',
  '/console/channel-quota',
  '/console/usage',
  '/console/identity',
  '/console/model-groups',
  // 会话管理与机器人公告：`routers/chat_channel.py` 全族 `@require_role(Role.Admin)`
  '/chat-channel',
  // 网页聊天：它挂在「聊天管理」组下，但自己标的是更严的 `platformOnly`（见
  // `PLATFORM_ONLY_PATHS`）。这里再登记一次是给「组里每个子项都得被 URL 兜底清单覆盖」
  // 那条契约用的（`tests/test_readonly_member_console_surface.py`）：只读成员确实进不去，
  // 只是拦他的是平台方那一档。**不是**把网页聊天降级成「租户管理员可见」——
  // `isTenantAdminOnlyPath()` 只在只读成员身上生效。
  '/web-chat',
  '/affection',
  '/memory',
  '/worldbook',
  '/mailbox',
  '/prompt-lab',
  '/presets',
  '/plugins',
  '/workspace',
] as const

/**
 * `/console` 本身（总后台概览）
 *
 * 单独一条：它是精确匹配，不能当前缀用 —— 写成前缀会把 `/console/team` 也兜进去，
 * 而那一页是只读成员唯一够得着的总后台页面。
 */
export const TENANT_ADMIN_ONLY_EXACT_PATHS = ['/console'] as const

/**
 * 去掉结尾的 `/` 再比
 *
 * ⚠️ **精确匹配这一档必须自己处理结尾斜杠。** react-router 匹配路由时会忽略结尾的 `/`
 * （`#/console/` 照样渲染总后台首页），但 `location.pathname` 原样保留它 ——
 * 于是 `pathname === '/console'` 不成立，兜底整条失效，只读成员敲 `#/console/` 就进去了。
 *
 * 前缀那一档不受影响（`'/console/instances/'.startsWith('/console/instances/')` 恒成立），
 * 这也是这个缺陷只藏在精确清单里、`PLATFORM_ONLY_PATHS` 那边看不到的原因。
 * 根路径 `/` 要保留，否则会被削成空串。
 */
const stripTrailingSlash = (pathname: string): string =>
  pathname.length > 1 && pathname.endsWith('/') ? pathname.replace(/\/+$/, '') || '/' : pathname

export const isTenantAdminOnlyPath = (pathname: string): boolean => {
  const normalized = stripTrailingSlash(pathname)
  return (
    TENANT_ADMIN_ONLY_EXACT_PATHS.some(exact => normalized === exact) ||
    TENANT_ADMIN_ONLY_PATHS.some(
      prefix => normalized === prefix || normalized.startsWith(`${prefix}/`)
    )
  )
}

// 获取翻译的辅助函数
const t = (key: string) => i18next.t(key, { ns: 'navigation' })

// 获取页面配置（动态生成以支持语言切换）
export const getPageConfigs = (): (PageConfig | MenuGroup)[] => [
  {
    key: 'cloud',
    text: t('menu.cloud'),
    translationKey: 'menu.cloud',
    icon: <CloudDownloadIcon />,
    visibility: 'platformOnly',
    children: [
      {
        path: '/cloud/telemetry',
        text: t('menu.telemetry'),
        translationKey: 'menu.telemetry',
        icon: <DashboardIcon />,
        parent: 'cloud',
      },
      {
        path: '/cloud/presets-market',
        text: t('menu.presetsMarket'),
        translationKey: 'menu.presetsMarket',
        icon: <FaceIcon />,
        parent: 'cloud',
      },
      {
        path: '/cloud/plugins-market',
        text: t('menu.pluginsMarket'),
        translationKey: 'menu.pluginsMarket',
        icon: <ExtensionIcon />,
        parent: 'cloud',
      },
      {
        // 本部署那一个社区账号的主页。整组已经是 platformOnly，这里再标一次是为了让
        // `tests/test_console_platform_only_paths.py` 能把它与 `PLATFORM_ONLY_PATHS`
        // 的 `/cloud` 前缀对起来（那条测试只收自己带 path 的格子）。
        path: '/cloud/profile',
        text: t('menu.cloudProfile'),
        translationKey: 'menu.cloudProfile',
        icon: <AccountCircleIcon />,
        parent: 'cloud',
        visibility: 'platformOnly',
      },
    ],
  },
  {
    path: '/dashboard',
    text: t('menu.dashboard'),
    translationKey: 'menu.dashboard',
    icon: <DashboardIcon />,
  },
  {
    key: 'console',
    text: t('menu.console'),
    translationKey: 'menu.console',
    icon: <SpaceDashboardIcon />,
    children: [
      {
        // `GET /console/overview` 是 `Role.Admin`：只读成员进来整页 403，
        // 而这一页此前会把那次 403 画成「实例总数 0」——见 MenuVisibility.tenantAdminOnly
        path: '/console',
        text: t('menu.consoleOverview'),
        translationKey: 'menu.consoleOverview',
        icon: <SpaceDashboardIcon />,
        parent: 'console',
        visibility: 'tenantAdminOnly',
      },
      {
        // `GET /console/instances` 同上；403 此前画成一张空表
        path: '/console/instances',
        text: t('menu.consoleInstances'),
        translationKey: 'menu.consoleInstances',
        icon: <SmartToyIcon />,
        parent: 'console',
        visibility: 'tenantAdminOnly',
      },
      {
        // 平台方名下没有任何租户时该入口不出现，用户感知不到多了这一层
        path: '/console/tenants',
        text: t('menu.consoleTenants'),
        translationKey: 'menu.consoleTenants',
        icon: <BusinessIcon />,
        parent: 'console',
        visibility: 'tenantManagement',
      },
      {
        path: '/console/model-groups',
        text: t('menu.consoleModelGroups'),
        translationKey: 'menu.consoleModelGroups',
        icon: <StorageIcon />,
        parent: 'console',
        visibility: 'tenantModelGroups',
      },
      {
        // 租户自助的「我的团队」：只渲染**自己这一个**租户 + 成员增删，不是租户列表。
        // 平台方走租户详情页（那页还带客户资料编辑、停用、实例划归，全是平台方专属的
        // 写操作），两个入口不合并。
        // 注：本文件的菜单项注释里**不要出现成对花括号**——两份契约测试都按最内层的
        // 一对花括号切分菜单格子，注释里出现一对就会把这一格切成两半，判据从此看不见它。
        path: '/console/team',
        text: t('menu.consoleTeam'),
        translationKey: 'menu.consoleTeam',
        icon: <GroupIcon />,
        parent: 'console',
        visibility: 'tenantSelfService',
      },
      {
        // **不是** platformOnly：租户管理员也要能管自己实例的配额，页面数据由后端按租户作用域收敛。
        // 但整族端点都是 `Role.Admin`，只读成员进来是一页 403，所以走 tenantAdminOnly 这一档。
        path: '/console/quota',
        text: t('menu.consoleQuota'),
        translationKey: 'menu.consoleQuota',
        icon: <SpeedIcon />,
        parent: 'console',
        visibility: 'tenantAdminOnly',
      },
      {
        // 频道额度：平台方给每个群 / 私聊承诺的每日额度。**不是** platformOnly，租户也要看得到
        // 自己名下频道的用量与剩余，可见范围由后端按租户作用域收敛；整族端点都是 `Role.Admin`，
        // 只读成员进来是一页 403，所以与配额同档走 tenantAdminOnly。
        path: '/console/channel-quota',
        text: t('menu.consoleChannelQuota'),
        translationKey: 'menu.consoleChannelQuota',
        icon: <ListAltIcon />,
        parent: 'console',
        visibility: 'tenantAdminOnly',
      },
      {
        // 用量与费用：定制版客户要看得到自己烧了多少 token、折算多少钱，所以**不是** platformOnly；
        // 可见范围由后端按租户作用域收敛。整族端点都是 `Role.Admin`，只读成员进来是一页 403，
        // 因此与配额同档走 tenantAdminOnly。
        path: '/console/usage',
        text: t('menu.consoleUsage'),
        translationKey: 'menu.consoleUsage',
        icon: <PaidIcon />,
        parent: 'console',
        visibility: 'tenantAdminOnly',
      },
      {
        // 域配置与账号合并是租户要自助做的事，后端按租户作用域收敛；同上，整族 `Role.Admin`。
        // 注意本文件的菜单项注释里不要出现成对花括号，见下方 /console/team 那一格的说明。
        path: '/console/identity',
        text: t('menu.consoleIdentity'),
        translationKey: 'menu.consoleIdentity',
        icon: <HubIcon />,
        parent: 'console',
        visibility: 'tenantAdminOnly',
      },
      {
        // 宿主级的 iptables 隔离规则运维：规则不属于任何租户，后端两个端点都是 Role.Super
        path: '/console/network-rules',
        text: t('menu.consoleNetworkRules'),
        translationKey: 'menu.consoleNetworkRules',
        icon: <SecurityIcon />,
        parent: 'console',
        visibility: 'platformOnly',
      },
      {
        // 运维审计：行里可能带着租户会话与平台方实例，后端整条路由只给平台方
        path: '/console/ops-audit',
        text: t('menu.consoleOpsAudit'),
        translationKey: 'menu.consoleOpsAudit',
        icon: <HistoryIcon />,
        parent: 'console',
        visibility: 'platformOnly',
      },
      {
        // SSH 网关：状态、全部凭据、存量导入与全局审计都只给平台方
        path: '/console/ssh-gateway',
        text: t('menu.consoleSshGateway'),
        translationKey: 'menu.consoleSshGateway',
        icon: <VpnKeyIcon />,
        parent: 'console',
        visibility: 'platformOnly',
      },
    ],
  },
  {
    // 整组对只读成员不出现：`routers/chat_channel.py` 与 `routers/affection.py`
    // 每一条读接口都是 `@require_role(Role.Admin)`，一格都点不动。
    // 组上标一次比逐格标少一份漏登记的机会，子项仍各自标一次，让两份契约测试收得到
    key: 'chatManagement',
    text: t('menu.chatManagement'),
    translationKey: 'menu.chatManagement',
    icon: <ChatIcon />,
    visibility: 'tenantAdminOnly',
    children: [
      {
        path: '/chat-channel/management',
        text: t('menu.channelManagement'),
        translationKey: 'menu.channelManagement',
        icon: <ChatIcon />,
        parent: 'chatManagement',
        visibility: 'tenantAdminOnly',
      },
      {
        // 网页聊天整块能力都是平台方专属：适配器子路由每个端点都是
        // `@require_role(Role.Admin)` 加 `require_platform_owner()`，租户身份一律 403。
        // 与 `PLATFORM_ONLY_PATHS` 的 `/web-chat` 成对登记。
        path: '/web-chat',
        text: t('menu.webChat'),
        translationKey: 'menu.webChat',
        icon: <CodeIcon />,
        parent: 'chatManagement',
        visibility: 'platformOnly',
      },
      {
        // 机器人公告：六个端点全是 `Role.Admin`。此前菜单画得出、整页 403，
        // 而页面把 403 画成「一个频道都没有」的空选择器 + 一个点了必然 403 的发送按钮
        path: '/chat-channel/announcement',
        text: t('menu.botAnnouncement'),
        translationKey: 'menu.botAnnouncement',
        icon: <CampaignIcon />,
        parent: 'chatManagement',
        visibility: 'tenantAdminOnly',
      },
      {
        path: '/affection',
        text: t('menu.affection'),
        translationKey: 'menu.affection',
        icon: <FavoriteIcon />,
        parent: 'chatManagement',
        visibility: 'tenantAdminOnly',
      },
      {
        // 记忆管理：`routers/memory_admin.py` 全族 `Role.Admin`
        path: '/memory',
        text: t('menu.memory'),
        translationKey: 'menu.memory',
        icon: <PsychologyIcon />,
        parent: 'chatManagement',
        visibility: 'tenantAdminOnly',
      },
      {
        // 世界书管理：`routers/worldbook.py` 全族 `Role.Admin`
        path: '/worldbook',
        text: t('menu.worldbook'),
        translationKey: 'menu.worldbook',
        icon: <MenuBookIcon />,
        parent: 'chatManagement',
        visibility: 'tenantAdminOnly',
      },
      {
        // 邮箱：`routers/instance_email.py` 全族 `Role.Admin`，按实例收敛
        path: '/mailbox',
        text: t('menu.mailbox'),
        translationKey: 'menu.mailbox',
        icon: <MailOutlineIcon />,
        parent: 'chatManagement',
        visibility: 'tenantAdminOnly',
      },
      {
        // 提示词预览：`routers/tavern.py` 整族 `Role.Admin`
        path: '/prompt-lab',
        text: t('menu.promptLab'),
        translationKey: 'menu.promptLab',
        icon: <ScienceIcon />,
        parent: 'chatManagement',
        visibility: 'tenantAdminOnly',
      },
    ],
  },
  {
    path: '/user-manager',
    text: t('menu.userManager'),
    translationKey: 'menu.userManager',
    icon: <GroupIcon />,
    visibility: 'platformOnly',
  },
  {
    // `routers/presets.py` 全族 `Role.Admin`
    path: '/presets',
    text: t('menu.presets'),
    translationKey: 'menu.presets',
    icon: <FaceIcon />,
    visibility: 'tenantAdminOnly',
  },
  {
    // 插件管理是 `Role.Admin`，插件编辑器是 `Role.Super`（下面单独标 platformOnly）；
    // 只读成员两格都够不着，所以整组不出现
    key: 'plugins',
    text: t('menu.plugins'),
    translationKey: 'menu.plugins',
    icon: <ExtensionIcon />,
    visibility: 'tenantAdminOnly',
    children: [
      {
        path: '/plugins/management',
        text: t('menu.pluginManagement'),
        translationKey: 'menu.pluginManagement',
        icon: <ExtensionIcon />,
        parent: 'plugins',
        visibility: 'tenantAdminOnly',
      },
      {
        path: '/plugins/editor',
        text: t('menu.pluginEditor'),
        translationKey: 'menu.pluginEditor',
        icon: <CodeIcon />,
        parent: 'plugins',
        visibility: 'platformOnly',
      },
    ],
  },
  {
    // `routers/workspaces.py` / `workspace_kb.py` / `skills.py` / `resources.py` / `timers.py`
    // 的读接口最低都是 `Role.Admin`，整组对只读成员是死的
    key: 'workspace',
    text: t('menu.workspace'),
    translationKey: 'menu.workspace',
    icon: <WorkspacesIcon />,
    visibility: 'tenantAdminOnly',
    children: [
      {
        path: '/workspace',
        text: t('menu.workspaceList'),
        translationKey: 'menu.workspaceList',
        icon: <WorkspacesIcon />,
        parent: 'workspace',
      },
      {
        path: '/workspace/skills',
        text: t('menu.skillsLibrary'),
        translationKey: 'menu.skillsLibrary',
        icon: <ExtensionIcon />,
        parent: 'workspace',
      },
      {
        path: '/workspace/mcp-services',
        text: t('menu.mcpServices'),
        translationKey: 'menu.mcpServices',
        icon: <HubIcon />,
        parent: 'workspace',
      },
      {
        path: '/workspace/resources',
        text: t('menu.resourceCenter'),
        translationKey: 'menu.resourceCenter',
        icon: <StorageIcon />,
        parent: 'workspace',
      },
      {
        path: '/workspace/kb-library',
        text: t('menu.kbLibrary'),
        translationKey: 'menu.kbLibrary',
        icon: <StorageIcon />,
        parent: 'workspace',
      },
      {
        path: '/workspace/timers',
        text: t('menu.timers'),
        translationKey: 'menu.timers',
        icon: <ScheduleIcon />,
        parent: 'workspace',
      },
    ],
  },
  {
    // 宿主级的运行日志：`routers/logs.py` 四个端点**全是** `Role.Super`
    // （日志里混着全部租户的会话、上游报错与凭据脱敏前的上下文）。
    // 此前对租户管理员也画着，点进去是一页 403 —— 那是「点不动的按钮」的整页版
    path: '/logs',
    text: t('menu.logs'),
    translationKey: 'menu.logs',
    icon: <TerminalIcon />,
    visibility: 'platformOnly',
  },
  {
    // 同上：`routers/sandbox.py` 四个端点全是 `Role.Super`，沙盒日志是宿主级资源
    path: '/sandbox-logs',
    text: t('menu.sandboxLogs'),
    translationKey: 'menu.sandboxLogs',
    icon: <CodeIcon />,
    visibility: 'platformOnly',
  },
  {
    path: '/adapters',
    text: t('menu.adapters'),
    translationKey: 'menu.adapters',
    icon: <HubIcon />,
  },
  {
    key: 'settings',
    text: t('menu.settings'),
    translationKey: 'menu.settings',
    icon: <SettingsIcon />,
    children: [
      {
        // **不是** `platformOnly`（对照 `/console/quota`）：租户管理员也要能改自己实例的
        // 配置覆盖，尤其是 `USE_MODEL_GROUP` —— 否则换个模型组只能去 `/chat-channel`
        // 逐个频道点。作用域收敛与「共享层不可写」由页面 + 后端
        // 各守一道。
        // 但也**不能完全不设 visibility**：整页（读与写）都是 `Role.Admin`，
        // 只读租户成员进来是一页纯 403，详见 `MenuVisibility.instanceConfigOverrides`。
        path: '/settings/system',
        text: t('menu.systemSettings'),
        translationKey: 'menu.systemSettings',
        icon: <TuneIcon />,
        parent: 'settings',
        visibility: 'instanceConfigOverrides',
      },
      {
        path: '/settings/deployment',
        text: t('menu.deployment'),
        translationKey: 'menu.deployment',
        icon: <SystemUpdateAltIcon />,
        parent: 'settings',
        visibility: 'platformOnly',
      },
      {
        // 与 /console/model-groups 是同一个页面组件；租户走那个入口，平台方走这个
        path: '/settings/models',
        text: t('menu.modelGroups'),
        translationKey: 'menu.modelGroups',
        icon: <StorageIcon />,
        parent: 'settings',
        visibility: 'platformOnly',
      },
      {
        path: '/settings/commands',
        text: t('menu.commandCenter'),
        translationKey: 'menu.commandCenter',
        icon: <ListAltIcon />,
        parent: 'settings',
        visibility: 'platformOnly',
      },
      {
        path: '/settings/theme',
        text: t('menu.theme'),
        translationKey: 'menu.theme',
        icon: <PaletteIcon />,
        parent: 'settings',
      },
      {
        path: '/settings/space-cleanup',
        text: t('menu.spaceCleanup'),
        translationKey: 'menu.spaceCleanup',
        icon: <CleaningServicesIcon />,
        parent: 'settings',
        visibility: 'platformOnly',
      },
    ],
  },
  {
    // 登录者自己的账号页。**不设 visibility**：租户成员的口令由平台方在建号时一次性设定，
    // 这里是他们唯一的自助更换入口，藏起来等于永远换不了。
    path: '/profile',
    text: t('menu.profile'),
    translationKey: 'menu.profile',
    icon: <AccountCircleIcon />,
  },
]

// 为了向后兼容，保留 PAGE_CONFIGS（但建议使用 getPageConfigs）
export const PAGE_CONFIGS = getPageConfigs()

const isEntryVisible = (
  entry: { visibility?: MenuVisibilityKey },
  visibility: MenuVisibility
): boolean => (entry.visibility ? visibility[entry.visibility] : true)

// 转换配置为菜单项的工具函数；按当前身份收敛可见入口
export const createMenuItems = (visibility: MenuVisibility = DEFAULT_MENU_VISIBILITY) => {
  return getPageConfigs()
    .filter(config => isEntryVisible(config, visibility))
    .map(config => {
      if ('children' in config) {
        return {
          text: config.text,
          icon: config.icon,
          path: undefined,
          key: config.key,
          children: config.children
            .filter(child => isEntryVisible(child, visibility))
            .map(child => ({
              text: child.text,
              icon: child.icon,
              path: child.path,
            })),
        }
      }
      return {
        text: config.text,
        icon: config.icon,
        path: config.path,
      }
    })
    .filter(item => !item.children || item.children.length > 0)
}

// 获取当前页面信息的工具函数
export const getCurrentPageFromConfigs = (pathname: string) => {
  if (pathname.startsWith('/chat-channel') && !pathname.startsWith('/chat-channel/announcement')) {
    const page = getPageConfigs().flatMap(config =>
      'children' in config ? config.children : [config]
    ).find(item => 'path' in item && (item as PageConfig).path === '/chat-channel/management')
    if (page) return page
  }
  // 扁平化所有页面配置
  const allPages = getPageConfigs().flatMap(config =>
    'children' in config ? config.children : [config]
  )
  // 最长路径优先匹配：避免短路径（如 /workspace）在访问长路径（如 /workspace/skills）时也高亮
  const matches = allPages.filter(
    page =>
      'path' in page &&
      (page.path === pathname || pathname.startsWith((page as PageConfig).path + '/'))
  )
  return matches.sort(
    (a, b) => ((b as PageConfig).path?.length ?? 0) - ((a as PageConfig).path?.length ?? 0)
  )[0]
}

// 获取当前页面标题的工具函数
export const getCurrentTitleFromConfigs = (pathname: string) => {
  const page = getCurrentPageFromConfigs(pathname)
  if (page && 'translationKey' in page && page.translationKey) {
    return t(page.translationKey)
  }
  return t('menu.defaultTitle')
}
