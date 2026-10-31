import { useState, useEffect, Suspense, useCallback, useMemo } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import {
  Box,
  Drawer,
  AppBar,
  Toolbar,
  Typography,
  List,
  ListItem,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Collapse,
  Chip,
  Link,
  useMediaQuery,
  useTheme,
  CircularProgress,
} from '@mui/material'
import {
  Logout as LogoutIcon,
  ExpandLess as ExpandLessIcon,
  ExpandMore as ExpandMoreIcon,
  GitHub as GitHubIcon,
  Menu as MenuIcon,
  ChevronLeft as ChevronLeftIcon,
  MenuBook as MenuBookIcon,
  Business as BusinessIcon,
  Public as PublicIcon,
} from '@mui/icons-material'
import { SimpleTooltip } from '../components/common/ThemedTooltip'
import { useQueryClient } from '@tanstack/react-query'
import { useAuthStore } from '../stores/auth'
import { registerInstanceScopeQueryClient } from '../stores/instanceScope'
import InstanceScopeSwitcher from '../components/common/InstanceScopeSwitcher'
import { configApi } from '../services/api/config'
import { motion } from 'framer-motion'
import { UI_STYLES, getAnimationDuration } from '../theme/themeApi'
import ThemeToggleButton from '../theme/ThemeToggleButton'
import { useNotification } from '../hooks/useNotification'
import { alpha } from '@mui/material/styles'
import { CHIP_VARIANTS } from '../theme/variants'
import { useWallpaperStore } from '../stores/wallpaper'
import WallpaperBackground from '../components/common/WallpaperBackground'
import {
  createMenuItems,
  getCurrentPageFromConfigs,
  getCurrentTitleFromConfigs,
  isPlatformOnlyPath,
  isTenantAdminOnlyPath,
  type MenuVisibility,
} from '../config/navigation'
import { useTenancyVisibility, type TenancyVisibility } from '../hooks/useIdentity'
import { useIdentityStore } from '../stores/identity'
import { useDevModeStore } from '../stores/devMode'
import { useSecretCode } from '../hooks/useSecretCode'
import LocaleToggleButton from '../components/common/LocaleToggleButton'
import { useSystemEventsContext } from '../contexts/SystemEventsContext'
import CommunityAvatar from '../components/common/CommunityAvatar'
import AgentActivityCard from '../components/common/AgentActivityCard'
import SystemEventsProvider from '../components/providers/SystemEventsProvider'
import ChannelDirectoryProvider from '../components/providers/ChannelDirectoryProvider'
import { useTranslation } from 'react-i18next'
import { useLocaleStore } from '../stores/locale'
import ActionButton from '../components/common/ActionButton'
import IconActionButton from '../components/common/IconActionButton'
import { oobeApi } from '../services/api/oobe'
import { authApi } from '../services/api/auth'

const DEV_MODE_SEQUENCE = ['nekro', 'nekro', 'nekro', 'agent', 'nekro', 'nekro', 'agent', 'agent']
let initialLocaleSyncPromise: Promise<void> | null = null
let initialVersionPromise: Promise<string> | null = null

/**
 * 顶栏当前身份标识
 *
 * 租户身份下必须一眼看出「我在哪个租户的作用域里」，因此用强对比的 Chip；
 * 平台方只有在确实存在租户时才显示一个弱标识。未启用多租户时整个组件不渲染，
 * 顶栏与多租户改造前完全一致。
 */
function TenantIdentityBadge({ tenancy }: { tenancy: TenancyVisibility }) {
  const theme = useTheme()
  const { t } = useTranslation('tenant')

  if (!tenancy.isTenancyVisible) {
    return null
  }

  const isTenant = tenancy.isTenantScoped
  const label = isTenant
    ? tenancy.tenantName || t(`role.${tenancy.role}`)
    : t('identity.platformLabel')
  const tooltip = isTenant
    ? `${t('identity.tenantTooltip', { tenant: tenancy.tenantName })}${
        tenancy.isReadOnly ? ` · ${t('identity.readOnlyTooltip')}` : ''
      }`
    : t('identity.platformTooltip')

  return (
    <SimpleTooltip title={tooltip}>
      <Chip
        size="small"
        icon={isTenant ? <BusinessIcon sx={{ fontSize: 14 }} /> : <PublicIcon sx={{ fontSize: 14 }} />}
        label={label}
        aria-label={t('identity.ariaLabel')}
        sx={{
          maxWidth: { xs: 96, sm: 180 },
          height: 22,
          mr: { xs: 0, sm: 0.5 },
          fontWeight: 700,
          color: theme.palette.common.white,
          backgroundColor: isTenant
            ? alpha(theme.palette.secondary.main, 0.9)
            : alpha(theme.palette.common.white, 0.18),
          '& .MuiChip-icon': { color: theme.palette.common.white },
        }}
      />
    </SimpleTooltip>
  )
}

function MainLayoutContent() {
  const navigate = useNavigate()
  const location = useLocation()
  const { token, userInfo, logout, setUserInfo } = useAuthStore()
  const theme = useTheme()
  const [starCount, setStarCount] = useState<number | null>(null)
  const [version, setVersion] = useState('0.0.0')
  const [openMenus, setOpenMenus] = useState<Record<string, boolean>>({})
  const [drawerOpen, setDrawerOpen] = useState(true)
  const [refreshKey, setRefreshKey] = useState(0)
  const isMobile = useMediaQuery(theme.breakpoints.down('md'))
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  const notification = useNotification()
  const { devMode, toggleDevMode } = useDevModeStore()
  const { t } = useTranslation()
  const { currentLocale, syncFromBackend } = useLocaleStore()
  const { agentActives, agentRuntimeStatuses, workspaceStatuses, workspaceCcActive, workspaceCcRuntimeStatuses } = useSystemEventsContext()
  const queryClient = useQueryClient()
  // 当前身份是所有「显示 / 隐藏 / 只读」判断的依据，登录后拉一次即可全局共享
  const tenancy = useTenancyVisibility()
  const resetIdentity = useIdentityStore(state => state.resetIdentity)

  // 切换实例作用域时需要整体失效查询缓存，把应用级 QueryClient 交给作用域 store
  useEffect(() => {
    registerInstanceScopeQueryClient(queryClient)
  }, [queryClient])
  const isChatChannelManagementLike =
    location.pathname.startsWith('/chat-channel') &&
    !location.pathname.startsWith('/chat-channel/announcement')
  const pageTransitionBaseKey = (() => {
    const segments = location.pathname.split('/').filter(Boolean)

    if (segments[0] === 'chat-channel') return '/chat-channel'
    if (segments[0] === 'plugins' && segments[1] === 'management') return '/plugins/management'
    if (segments[0] === 'workspace' && segments[1]) return `/workspace/${segments[1]}`
    if (segments[0] === 'adapters' && segments[1]) return `/adapters/${segments[1]}`

    return `/${segments.slice(0, 3).join('/')}`
  })()

  // 初始化时从后端同步语言设置
  useEffect(() => {
    if (initialLocaleSyncPromise === null) {
      initialLocaleSyncPromise = syncFromBackend().catch((error) => {
        initialLocaleSyncPromise = null
        throw error
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!token || userInfo) {
      return
    }

    let cancelled = false
    authApi
      .getUserInfo()
      .then(nextUserInfo => {
        if (!cancelled) {
          setUserInfo(nextUserInfo)
        }
      })
      .catch(() => {
        // axios 拦截器会处理失效登录；这里不阻塞主布局渲染。
      })

    return () => {
      cancelled = true
    }
  }, [token, userInfo, setUserInfo])

  // 侧边栏宽度：英文模式需要更宽以容纳较长的文本
  const drawerWidth = currentLocale === 'en-US' ? 260 : 240

  // 菜单按当前身份收敛：租户身份看不到平台级入口，未启用多租户时租户入口整体不出现
  const menuVisibility = useMemo<MenuVisibility>(
    () => ({
      platformOnly: !tenancy.isTenantScoped,
      // 至少 `Role.Admin` 才够得着的那一档：只读租户成员整族 403，不给他画入口。
      // 身份未确认时 `isReadOnly` 为 false（`useIdentity` 的既定约定：一律按平台方处理），
      // 因此未启用多租户的部署与平台方逐字不变。
      tenantAdminOnly: !tenancy.isReadOnly,
      tenantManagement: tenancy.canManageTenants && tenancy.hasTenants,
      // 模型组只有一个页面实现：平台方从「系统配置 → 模型管理」进入，租户从总后台进入。
      // 两个入口都摆出来会让人以为是两套东西，实际读写的是同一张表。
      // 再叠一个 `!isReadOnly`：`GET /console/model-groups` 是 `Role.Admin`，
      // 只读成员进去是一页 403（这一格用不了 `tenantAdminOnly`，它还要挡平台方）。
      tenantModelGroups: tenancy.isTenantScoped && !tenancy.isReadOnly,
      // 「我的团队」是租户自助管成员的入口，渲染的是自己这一个租户。
      // 平台方从 `/console/tenants/{id}` 进，同上，不给它两个入口。
      tenantSelfService: tenancy.isTenantScoped,
      // 实例级配置覆盖页：整页（读与写）都是 `Role.Admin`，只读租户成员进去是一页纯 403。
      // 身份未确认时 `isReadOnly` 为 false（`useIdentity` 的既定约定：一律按平台方处理），
      // 因此未启用多租户的部署与平台方逐字不变。
      instanceConfigOverrides: !tenancy.isReadOnly,
    }),
    [tenancy.isTenantScoped, tenancy.canManageTenants, tenancy.hasTenants, tenancy.isReadOnly]
  )

  const menuItems = useMemo(() => createMenuItems(menuVisibility), [menuVisibility])

  // 菜单隐藏之外的第二层：租户身份直接输入平台级 URL 时也进不去
  const isPlatformOnlyBlocked = tenancy.isTenantScoped && isPlatformOnlyPath(location.pathname)
  // 同一形态的第二档：只读租户成员直接敲 `/console` / `/chat-channel/announcement` 之类
  // 的 URL。藏菜单只管画不画，这里管进不进得去——不拦的话他落在一页纯 403 上，
  // 而这一轮修的正是「那一页会把 403 说成 0」。
  const isReadOnlyBlocked = tenancy.isReadOnly && isTenantAdminOnlyPath(location.pathname)
  const isGuardBlocked = isPlatformOnlyBlocked || isReadOnlyBlocked
  /**
   * 兜底页那个「返回」按钮该指向哪儿
   *
   * ⚠️ 判据是**当前身份够不够得着 `/console`**，不是「这次撞的是哪一堵墙」。
   * 两档的路径清单是**相交**的：`/logs`、`/sandbox-logs`、`/user-manager`、
   * `/settings/models`、`/cloud/*` 只在 `PLATFORM_ONLY_PATHS` 里，只读成员敲这些 URL
   * 走的是 `isPlatformOnlyBlocked` 这一支 —— 按「哪堵墙」分档就会给他一个指向 `/console`
   * 的按钮，而 `/console` 对他同样是挡住的：点下去落在**另一页**「无权访问」上。
   * 那正是本轮在消灭的「点不动的按钮」，只是它藏在两份清单的交集里。
   */
  const guardBackTarget = tenancy.isReadOnly ? '/dashboard' : '/console'

  // 使用壁纸store
  const { mainWallpaper, mainWallpaperMode, mainWallpaperBlur, mainWallpaperDim } =
    useWallpaperStore()

  // 当检测到移动设备时，默认关闭抽屉
  useEffect(() => {
    setDrawerOpen(!isMobile)
  }, [isMobile])

  // 开发者模式 "组合键"
  const enableDevMode = useCallback(() => {
    if (!devMode) {
      toggleDevMode()
      notification.success(t('devMode.enabled', { ns: 'layout-MainLayout' }))
    }
  }, [devMode, toggleDevMode, notification, t])

  const recordClick = useSecretCode(DEV_MODE_SEQUENCE, enableDevMode)

  const handleLogoClick = useCallback(
    (part: 'nekro' | 'agent') => {
      if (devMode) {
        toggleDevMode()
        notification.info(t('devMode.disabled', { ns: 'layout-MainLayout' }))
      } else {
        recordClick(part)
      }
    },
    [devMode, toggleDevMode, notification, t, recordClick]
  )

  // 自动展开包含当前路由的菜单项
  useEffect(() => {
    menuItems.forEach(item => {
      if (item.children && item.key) {
        const hasActiveChild = item.children.some(
          child =>
            location.pathname === child.path || location.pathname.startsWith(child.path + '/')
        )
        const shouldOpen =
          hasActiveChild || (item.key === 'chatManagement' && isChatChannelManagementLike)
        if (shouldOpen) {
          setOpenMenus(prev => ({
            ...prev,
            [item.key]: true,
          }))
        }
      }
    })
  }, [location.pathname, menuItems, isChatChannelManagementLike])

  const getCurrentPage = () => {
    return getCurrentPageFromConfigs(location.pathname)
  }

  const getCurrentTitle = () => {
    return getCurrentTitleFromConfigs(location.pathname)
  }

  const handleComponentRefresh = () => {
    setRefreshKey(prev => prev + 1)
    notification.success(t('topbar.refreshPage', { ns: 'layout-MainLayout' }), { autoHideDuration: 1500 })
  }

  const handleLogout = () => {
    logout()
    // 身份不做持久化，退出时立刻清空，避免下一个账号看到上一个身份
    resetIdentity()
    navigate('/login')
    notification.info(t('footer.logout', { ns: 'navigation' }))
  }

  const handleMenuItemClick = (path?: string, key?: string) => {
    if (key && path === undefined) {
      // 如果是带子菜单的项，切换展开状态
      setOpenMenus(prev => ({
        ...prev,
        [key]: !prev[key],
      }))
    } else if (path) {
      // 如果是导航项，跳转到对应路径
      navigate(path)
      // 在移动端上，点击菜单项后自动收起侧边栏
      if (isMobile) {
        setDrawerOpen(false)
      }
    }
  }

  const logoPartSx = {
    display: 'inline-block', // to allow transform
    transition: 'transform 0.1s ease-in-out',
    userSelect: 'none',
    '&:active': {
      transform: 'scale(0.95)',
    },
  }

  useEffect(() => {
    if (!userInfo) {
      return
    }
    if (location.pathname === '/oobe') {
      return
    }
    let cancelled = false
    oobeApi
      .getGuideStatus()
      .then(status => {
        if (!cancelled && status.shouldShow) {
          navigate('/oobe', { replace: true })
        }
      })
      .catch(() => {
        // 登录后的主页面不因 OOBE 状态读取失败而阻塞。
      })
    return () => {
      cancelled = true
    }
  }, [location.pathname, navigate, userInfo])

  const drawer = (
    <Box className="h-full flex flex-col">
      <Toolbar
        disableGutters
        sx={{
          overflow: 'visible',
          minHeight: '64px !important',
          width: '100%',
          px: '0 !important',
          py: 0,
          margin: 0,
        }}
      >
        <Box
          className="flex items-center justify-center relative"
          sx={{
            overflow: 'visible',
            position: 'relative',
            width: '100%',
            minHeight: '64px',
            pt: 2,
            px: 2,
            margin: 0,
          }}
        >
          {/* Logo - 居中 */}
          <Typography
            variant="h6"
            noWrap
            className="font-sans font-black tracking-[0.2rem] select-none cursor-default text-[1.3rem] transition-transform duration-300 hover:scale-105"
            sx={{
              overflow: 'visible',
              '@keyframes bounce': {
                '0%': { transform: 'translateY(0)' },
                '100%': { transform: 'translateY(-3px)' },
              },
              '@keyframes wave': {
                '0%': { transform: 'rotate(-2deg)' },
                '50%': { transform: 'rotate(2deg)' },
                '100%': { transform: 'rotate(-2deg)' },
              },
              '& .highlight, & .text': {
                display: 'inline-block',
                transition: 'transform 0.3s ease-out',
                transformOrigin: 'center',
                willChange: 'transform',
              },
              '& .highlight': {
                color: theme.palette.primary.main,
                fontWeight: 900,
                fontSize: '1.5rem',
                textShadow:
                  '0 0 10px rgba(255,255,255,0.1), 0 0 20px rgba(255,100,100,0.05), 0 0 30px rgba(255,100,100,0.05)',
                '&:not(:hover)': {
                  animation: 'none',
                  transform: 'translateY(0)',
                  transition: 'all 0.3s ease-out',
                },
              },
              '& .text': {
                fontWeight: 800,
                fontSize: '1.2rem',
                textShadow: '0 0 5px rgba(255,255,255,0.15)',
                '&:not(:hover)': {
                  animation: 'none',
                  transform: 'rotate(0)',
                  transition: 'all 0.3s ease-out',
                },
              },
              '&:hover': {
                '& .highlight': {
                  animation: 'bounce 0.5s ease infinite alternate',
                },
                '& .text': {
                  animation: 'wave 1s ease infinite',
                },
              },
            }}
          >
            <Box component="span" sx={logoPartSx} onClick={() => handleLogoClick('nekro')}>
              <span className="highlight">N</span>
              <span className="text">ekro</span>
            </Box>{' '}
            <Box component="span" sx={logoPartSx} onClick={() => handleLogoClick('agent')}>
              <span className="highlight">A</span>
              <span className="text">gent</span>
            </Box>
          </Typography>

          {/* 版本号 - 相对于外层 Box 右上角 */}
          <Chip
            label={`v ${version}`}
            size="small"
            variant="outlined"
            className="version-tag"
            sx={{
              ...CHIP_VARIANTS.base(true),
              position: 'absolute',
              top: '8px',
              right: '8px',
              fontSize: '0.65rem',
              letterSpacing: '-0.02em',
              backgroundColor: theme.palette.background.paper,
              borderColor: 'rgba(255,255,255,0.2)',
              color: theme.palette.primary.light,
              transition: 'transform 0.3s ease',
              transform: 'scale(1)',
              height: '18px',
              minWidth: 'auto',
              '.MuiChip-label': {
                px: 0.5,
                py: 0,
                lineHeight: 1,
              },
              '&:hover': {
                transform: 'scale(1.05) translateY(-1px)',
              },
            }}
          />

          {/* 开发标志 - 相对于外层 Box 左上角 */}
          {devMode && (
            <Chip
              label="DEV"
              size="small"
              color="secondary"
              sx={{
                ...CHIP_VARIANTS.baseWithColor(true, theme.palette.secondary.main),
                position: 'absolute',
                top: '8px',
                left: '8px',
                fontSize: '0.6rem',
                letterSpacing: '0.05em',
                height: '18px',
                minWidth: 'auto',
                '.MuiChip-label': {
                  px: 0.5,
                  py: 0,
                  lineHeight: 1,
                },
              }}
            />
          )}
        </Box>
      </Toolbar>
      <List className="flex-grow overflow-y-auto">
        {menuItems.map(item => (
          <Box key={item.text}>
            <ListItem disablePadding>
              <ListItemButton
                onClick={() => handleMenuItemClick(item.path, item.key)}
                selected={Boolean(
                  !item.children &&
                    (location.pathname === item.path ||
                      (item.path && location.pathname.startsWith(item.path + '/')))
                )}
                sx={{
                  '&.Mui-selected': {
                    backgroundColor: UI_STYLES.SELECTED,
                    borderLeft: UI_STYLES.BORDERS.MENU.ACTIVE,
                    '&:hover': {
                      backgroundColor: UI_STYLES.SELECTED_HOVER,
                    },
                  },
                  '&:hover': {
                    backgroundColor: UI_STYLES.HOVER,
                  },
                  py: 1,
                  borderLeft: '3px solid transparent',
                  transition: 'all 0.2s ease-in-out',
                }}
              >
                <ListItemIcon>{item.icon}</ListItemIcon>
                <ListItemText
                  primary={item.text}
                  primaryTypographyProps={{
                    fontSize: isSmall ? '0.9rem' : 'inherit',
                    whiteSpace: 'nowrap',
                  }}
                />
                {item.children &&
                  item.key &&
                  (openMenus[item.key] ? <ExpandLessIcon /> : <ExpandMoreIcon />)}
              </ListItemButton>
            </ListItem>
            {item.children && item.key && (
              <Collapse in={openMenus[item.key]} timeout="auto" unmountOnExit>
                <List component="div" disablePadding>
                  {item.children.map(child => (
                    <ListItemButton
                      key={child.text}
                      onClick={() => {
                        navigate(child.path)
                        if (isMobile) {
                          setDrawerOpen(false)
                        }
                      }}
                      selected={Boolean(
                        (location.pathname === child.path ||
                          location.pathname.startsWith(child.path + '/')) &&
                          !item.children.some(
                            sibling =>
                              sibling.path !== child.path &&
                              (location.pathname === sibling.path ||
                                location.pathname.startsWith(sibling.path + '/')) &&
                              sibling.path.length > child.path.length
                          )
                      ) || Boolean(
                        item.key === 'chatManagement' &&
                          child.path === '/chat-channel/management' &&
                          isChatChannelManagementLike
                      )}
                      sx={{
                        pl: 4,
                        py: isSmall ? 0.75 : 1,
                        '&.Mui-selected': {
                          backgroundColor: UI_STYLES.SELECTED,
                          borderLeft: UI_STYLES.BORDERS.MENU.ACTIVE,
                          '&:hover': {
                            backgroundColor: UI_STYLES.SELECTED_HOVER,
                          },
                        },
                        '&:hover': {
                          backgroundColor: UI_STYLES.HOVER,
                        },
                        borderLeft: '3px solid transparent',
                        transition: 'all 0.2s ease-in-out',
                      }}
                    >
                      <ListItemIcon>{child.icon}</ListItemIcon>
                      <ListItemText
                        primary={child.text}
                        primaryTypographyProps={{
                          fontSize: isSmall ? '0.85rem' : 'inherit',
                          whiteSpace: 'nowrap',
                        }}
                      />
                    </ListItemButton>
                  ))}
                </List>
              </Collapse>
            )}
          </Box>
        ))}
      </List>
      <List>
        <ListItem disablePadding>
          <ListItemButton
            onClick={handleLogout}
            sx={{
              py: isSmall ? 0.75 : 1,
              '&:hover': {
                backgroundColor: UI_STYLES.HOVER,
              },
              transition: 'all 0.2s ease-in-out',
            }}
          >
            <ListItemIcon>
              <LogoutIcon />
            </ListItemIcon>
            <ListItemText
              primary={t('footer.logout', { ns: 'navigation' })}
              secondary={userInfo?.username}
              primaryTypographyProps={{
                fontSize: isSmall ? '0.9rem' : 'inherit',
              }}
              secondaryTypographyProps={{
                fontSize: isSmall ? '0.75rem' : 'inherit',
              }}
            />
          </ListItemButton>
        </ListItem>
      </List>
      <Box
        className="pb-2 -mt-2 text-center border-t"
        sx={{ borderColor: 'rgba(128,128,128,0.2)' }}
      >
        <Typography variant="caption" color="text.secondary">
          © {new Date().getFullYear()}{' '}
          <Link
            href="https://github.com/KroMiose/nekro-agent"
            target="_blank"
            rel="noopener noreferrer"
          >
            NekroAgent
          </Link>
          . {t('footer.copyright', { ns: 'navigation' })}.
        </Typography>
        {/* LICENSE 要求：界面里以可点击超链接标明「基于 NekroAgent 项目构建」，字号不小于主文字的 80%（0.875rem 对侧栏 1rem 主文字为 87.5%）、对比度可读。任何分发版本都必须保留。 */}
        <Typography
          component="div"
          color="text.secondary"
          sx={{ fontSize: '0.875rem', lineHeight: 1.5, mt: 0.5 }}
        >
          {t('footer.builtOnPrefix', { ns: 'navigation' })}
          <Link
            href="https://github.com/KroMiose/nekro-agent"
            target="_blank"
            rel="noopener noreferrer"
            underline="always"
            sx={{ color: 'primary.main' }}
          >
            NekroAgent
          </Link>
          {t('footer.builtOnSuffix', { ns: 'navigation' })}
        </Typography>
      </Box>
    </Box>
  )

  useEffect(() => {
    fetch('https://api.github.com/repos/KroMiose/nekro-agent')
      .then(response => response.json())
      .then(data => {
        if (data.stargazers_count) {
          setStarCount(data.stargazers_count)
        }
      })
      .catch(() => {
        // 如果获取失败，保持为 null
      })
  }, [])

  useEffect(() => {
    if (initialVersionPromise === null) {
      initialVersionPromise = configApi.getVersion().catch((error) => {
        initialVersionPromise = null
        throw error
      })
    }
    initialVersionPromise
      .then(version => {
        setVersion(version)
      })
      .catch(() => {
        setVersion('0.0.0')
      })
  }, [])

  return (
    <Box
      className="flex"
      sx={{
        position: 'relative',
        background: UI_STYLES.BACKGROUND.PRIMARY,
        backgroundColor: theme.palette.mode === 'dark' ? '#181818' : '#f8f8f8',
        minHeight: '100vh',
        transition: 'background 0.5s ease',
        padding: 0,
      }}
    >
      {/* 壁纸背景组件 */}
      {mainWallpaper && (
        <Box
          sx={{
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            zIndex: 0,
            overflow: 'hidden',
          }}
        >
          <WallpaperBackground
            wallpaperUrl={mainWallpaper}
            mode={mainWallpaperMode as 'cover' | 'contain' | 'repeat' | 'center'}
            blur={mainWallpaperBlur}
            dim={mainWallpaperDim}
          />
        </Box>
      )}

      {/* 其余布局内容，添加相对定位确保在壁纸上层 */}
      <AppBar
        position="fixed"
        sx={{
          width: {
            xs: '100%',
            md: drawerOpen ? `calc(100% - ${drawerWidth}px)` : '100%',
          },
          ml: {
            xs: 0,
            md: drawerOpen ? `${drawerWidth}px` : 0,
          },
          transition: theme =>
            theme.transitions.create(['width', 'margin-left'], {
              easing: theme.transitions.easing.sharp,
              duration: theme.transitions.duration.leavingScreen,
            }),
          backdropFilter: 'blur(12px)',
          backgroundColor: 'transparent',
          backgroundImage: `linear-gradient(90deg, ${alpha(theme.palette.primary.dark, 0.75)}, ${alpha(theme.palette.primary.main, 0.75)})`,
          boxShadow:
            '0 4px 15px rgba(0, 0, 0, 0.12), 0 1px 3px rgba(0, 0, 0, 0.05), 0 0 10px rgba(0, 0, 0, 0.03)',
          borderBottom:
            theme.palette.mode === 'dark' ? `1px solid rgba(255, 255, 255, 0.08)` : 'none',
          color: theme.palette.common.white,
          zIndex: theme.zIndex.drawer + 1,
        }}
      >
        <Toolbar sx={{ minHeight: { xs: 56, sm: 64 } }}>
          <IconActionButton
            edge="start"
            onClick={() => setDrawerOpen(!drawerOpen)}
            sx={{ mr: 2, color: 'white' }}
            aria-label={drawerOpen ? t('aria.collapseSidebar', { ns: 'layout-MainLayout' }) : t('aria.expandSidebar', { ns: 'layout-MainLayout' })}
          >
            {drawerOpen && isMobile ? <ChevronLeftIcon /> : <MenuIcon />}
          </IconActionButton>
          <Box className="flex items-center gap-2 flex-grow select-none overflow-hidden">
            {getCurrentPage()?.icon}
            <Typography
              variant="h6"
              noWrap
              component="div"
              className="font-medium select-none text-ellipsis overflow-hidden"
              onClick={handleComponentRefresh}
              sx={{
                color: 'inherit',
                fontSize: { xs: '1rem', sm: '1.25rem' },
                textShadow: '0 0 2px rgba(255,255,255,0.15)',
                cursor: 'pointer',
                transition: 'all 0.15s ease-in-out',
                '&:hover': {
                  opacity: 0.9,
                },
                '&:active': {
                  transform: 'scale(0.98)',
                  opacity: 0.8,
                },
              }}
            >
              {getCurrentTitle()}
            </Typography>
          </Box>

          {/* 当前身份标识（未启用多租户时不渲染） */}
          <TenantIdentityBadge tenancy={tenancy} />

          {/* 实例作用域切换器（单实例部署下不渲染） */}
          <InstanceScopeSwitcher />

          <ThemeToggleButton sx={{ color: 'white' }} />

          {/* 语言切换按钮 */}
          <LocaleToggleButton mode="compact" sx={{ color: 'white' }} />

          <IconActionButton
            size={isSmall ? 'small' : 'medium'}
            onClick={() => window.open('https://doc.nekro.ai', '_blank')}
            sx={{
              color: 'white',
              transition: 'transform 0.3s ease, opacity 0.2s ease',
              '&:hover': {
                transform: 'rotate(12deg) scale(1.05)',
              },
              '&:active': {
                transform: 'scale(0.95)',
              },
            }}
            aria-label={t('aria.openDocumentation', { ns: 'layout-MainLayout' })}
          >
            <MenuBookIcon sx={{ transition: 'all 0.3s ease' }} />
          </IconActionButton>

          <ActionButton
            tone="ghost"
            variant="text"
            size={isSmall ? 'small' : 'medium'}
            startIcon={<GitHubIcon />}
            onClick={() => window.open('https://github.com/KroMiose/nekro-agent', '_blank')}
            className="normal-case"
            sx={(theme) => ({
              mr: { xs: 0, sm: 0.5 },
              ml: { xs: 0.5, sm: 0.5 },
              minWidth: 0,
              px: 0.5,
              color: 'white',
              border: 'none',
              boxShadow: 'none',
              backgroundColor: 'transparent',
              '& .MuiButton-startIcon': {
                mr: { xs: 0, sm: 0.5 },
                color: 'white',
              },
              '&:hover': {
                backgroundColor:
                  theme.palette.mode === 'light'
                    ? 'rgba(0, 0, 0, 0.045)'
                    : 'rgba(255, 255, 255, 0.08)',
              },
              '&:active': {
                backgroundColor:
                  theme.palette.mode === 'light'
                    ? 'rgba(0, 0, 0, 0.08)'
                    : 'rgba(255, 255, 255, 0.16)',
              },
            })}
          >
            <Box sx={{ display: { xs: 'none', sm: 'block' } }}>
              Stars {starCount !== null ? starCount : '...'}
            </Box>
          </ActionButton>

          <CommunityAvatar />
        </Toolbar>
      </AppBar>

      {/* AI 响应活动播报卡片（FPS 风格，全局可见） */}
      <AgentActivityCard
        agentActives={agentActives}
        agentRuntimeStatuses={agentRuntimeStatuses}
        workspaceStatuses={workspaceStatuses}
        workspaceCcActive={workspaceCcActive}
        workspaceCcRuntimeStatuses={workspaceCcRuntimeStatuses}
      />
      <Box
        component="nav"
        sx={{
          width: { sm: drawerOpen ? drawerWidth : 0 },
          flexShrink: { sm: 0 },
          transition: theme =>
            theme.transitions.create('width', {
              easing: theme.transitions.easing.sharp,
              duration: theme.transitions.duration.enteringScreen,
            }),
        }}
      >
        <Drawer
          variant={isMobile ? 'temporary' : 'permanent'}
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          sx={{
            '& .MuiDrawer-paper': {
              boxSizing: 'border-box',
              width: drawerWidth,
              backgroundColor: theme =>
                theme.palette.mode === 'dark'
                  ? alpha(theme.palette.background.paper, 0.68)
                  : alpha(theme.palette.background.paper, 0.86),
              transition: theme =>
                theme.transitions.create('width', {
                  easing: theme.transitions.easing.sharp,
                  duration: theme.transitions.duration.enteringScreen,
                }),
              '& > *': {
                paddingLeft: '0 !important',
                paddingRight: '0 !important',
              },
            },
            display: { xs: 'block', sm: drawerOpen ? 'block' : 'none' },
          }}
        >
          {drawer}
        </Drawer>
      </Box>
      <Box
        component="main"
        className="flex-grow h-screen overflow-hidden flex flex-col"
        sx={{
          position: 'relative',
          flexGrow: 1,
          width: {
            xs: '100%',
            md: drawerOpen ? `calc(100% - ${drawerWidth}px)` : '100%',
          },
          transition: theme =>
            theme.transitions.create('width', {
              easing: theme.transitions.easing.sharp,
              duration: theme.transitions.duration.leavingScreen,
            }),
          zIndex: 1,
        }}
      >
        <Toolbar sx={{ flexShrink: 0, minHeight: { xs: 56, sm: 64 } }} />
        <motion.div
          key={`${pageTransitionBaseKey}-${refreshKey}`}
          initial={{ opacity: 0, x: 20, scale: 0.98 }}
          animate={{ opacity: 1, x: 0, scale: 1 }}
          transition={{
            duration: getAnimationDuration(0.36),
            ease: [0.4, 0, 0.2, 1],
          }}
          className="h-full flex-grow overflow-auto rounded-xl performance-adaptive motion-div"
          style={{
            position: 'relative',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              right: 0,
              height: '2px',
              background: `linear-gradient(90deg, 
                ${alpha(theme.palette.primary.main, 0.3)}, 
                ${alpha(theme.palette.secondary.main, 0.3)})`,
              opacity: 0.4,
              zIndex: 1,
            }}
          />
          <Suspense
            fallback={
              <Box
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  height: '100%',
                }}
              >
                <CircularProgress />
              </Box>
            }
          >
            {isGuardBlocked ? (
              <Box
                sx={{
                  height: '100%',
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: 1.5,
                  p: 3,
                  textAlign: 'center',
                }}
              >
                <Typography variant="subtitle1" fontWeight={700}>
                  {t('guard.title', { ns: 'tenant' })}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  {/* 两档的原因不一样，说法也要不一样：一个是「这功能只给平台方」，
                      另一个是「你这个账号是只读的」。共用一句会让租户管理员和
                      只读成员都读不懂自己撞的是哪一堵墙 */}
                  {isPlatformOnlyBlocked
                    ? t('guard.description', { ns: 'tenant' })
                    : t('guard.readOnlyDescription', { ns: 'tenant' })}
                </Typography>
                {/* 只读成员的「返回」不能指向 `/console`：那一页对他同样是挡住的，
                    点了会落在另一页「无权访问」上。判据见 `guardBackTarget` —— 用的是
                    「他够不够得着 /console」，不是「这次撞的是哪一堵墙」 */}
                <ActionButton
                  tone="primary"
                  size="small"
                  onClick={() => navigate(guardBackTarget)}
                >
                  {tenancy.isReadOnly
                    ? t('guard.backToDashboard', { ns: 'tenant' })
                    : t('guard.back', { ns: 'tenant' })}
                </ActionButton>
              </Box>
            ) : (
              <Outlet />
            )}
          </Suspense>
        </motion.div>
      </Box>
    </Box>
  )
}

export default function MainLayout() {
  return (
    <SystemEventsProvider>
      <ChannelDirectoryProvider>
        <MainLayoutContent />
      </ChannelDirectoryProvider>
    </SystemEventsProvider>
  )
}
