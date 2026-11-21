import React, { useCallback, useMemo, useState, useEffect } from 'react'
import {
  Box,
  List,
  ListItemText,
  Typography,
  Divider,
  Switch,
  Card,
  CardContent,
  TextField,
  Chip,
  Tab,
  FormControlLabel,
  CircularProgress,
  Alert,
  Tooltip,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
  Stack,
  Collapse,
  Link,
  InputAdornment,
  useMediaQuery,
  useTheme,
  Drawer,
  Fab,
  ListItemButton,
  Menu,
  MenuItem,
} from '@mui/material'
import MarkdownRenderer from '../../components/common/MarkdownRenderer'
import {
  Refresh as RefreshIcon,
  Code as CodeIcon,
  Settings as SettingsIcon,
  InfoOutlined as InfoIcon,
  ArrowBack as ArrowBackIcon,
  Delete as DeleteIcon,
  Edit as EditIcon,
  ContentCopy as ContentCopyIcon,
  WebhookOutlined as WebhookIcon,
  KeyboardArrowUp as KeyboardArrowUpIcon,
  KeyboardArrowDown as KeyboardArrowDownIcon,
  Storage as StorageIcon,
  Extension as ExtensionIcon,
  Description as DescriptionIcon,
  Person as PersonIcon,
  VpnKey as VpnKeyIcon,
  Link as LinkIcon,
  Category as CategoryIcon,
  Bookmark as BookmarkIcon,
  MoreVert as MoreVertIcon,
  Close as CloseIcon,
  WebAsset as WebAssetIcon,
  OpenInNew as OpenInNewIcon,
  LowPriority as CallPriorityIcon,
} from '@mui/icons-material'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Method, Plugin, PluginCallPriority, pluginsApi } from '../../services/api/plugins'

import { unifiedConfigApi, createConfigService } from '../../services/api/unified-config'
import ConfigTable from '../../components/common/ConfigTable'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  pluginTypeColors,
  methodTypeColors,
  CHIP_VARIANTS,
  CARD_VARIANTS,
} from '../../theme/variants'
import { useNotification } from '../../hooks/useNotification'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import { getLocalizedText } from '../../services/api/types'
import { copyText } from '../../utils/clipboard'
import { pluginsManagementPath } from '../../router/routes'
import { PageTabs } from '../../components/common/NekroTabs'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import ScopeOverrideControl from '../../components/common/ScopeOverrideControl'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { useAuthStore } from '../../stores/auth'
import {
  instanceScopeApi,
  isScopeEndpointUnavailable,
  type PluginScopeStatus,
} from '../../services/api/instance-scope'

// 添加 server_addr 配置
const server_addr = window.location.origin

// 后端 `Role.Super` 的数值，全进程一份的插件设置（调用优先级）按它放行
const SUPER_PERM_LEVEL = 3

/**
 * 自带网页控制台的插件（插件路由提供的单页，不在 NA 前端工程里）
 *
 * 控制台按实例限定范围：链接带上当前实例；平台方在全局视图下打开时看的是全部（服务端判）。
 * 页面直接读后台的登录态，所以必须同源打开。
 */
const PLUGIN_CONSOLES: Record<string, string> = {
  'KroMiose.emotion': '/plugins/KroMiose.emotion/console/',
}

function PluginConsoleLink({ pluginId, label }: { pluginId: string; label: string }) {
  const { currentInstanceKey, currentInstance } = useInstanceScope()
  const base = PLUGIN_CONSOLES[pluginId]
  if (!base) return null
  const params = new URLSearchParams()
  if (currentInstanceKey && currentInstance?.adapter_key) {
    params.set('adapter_key', currentInstance.adapter_key)
    params.set('instance_key', currentInstanceKey)
  }
  const href = params.toString() ? `${base}?${params.toString()}` : base
  return (
    <Stack direction="row" spacing={1.5} alignItems="center">
      <LinkIcon color="action" />
      <Link href={href} target="_blank" rel="noopener" variant="body2" sx={{ fontWeight: 600 }}>
        {label}
      </Link>
    </Stack>
  )
}

// 插件国际化辅助函数
const getPluginName = (plugin: Plugin, language: string) => {
  return getLocalizedText(plugin.i18n_name, plugin.name, language)
}

const getPluginDescription = (plugin: Plugin, language: string) => {
  return getLocalizedText(plugin.i18n_description, plugin.description, language)
}

type PluginTabKey = 'info' | 'config' | 'webui' | 'methods' | 'webhook' | 'data'

interface PluginDetailProps {
  plugin: Plugin
  activationStrategyLoading: boolean
  onBack: () => void
  onToggleEnabled: (id: string, enabled: boolean) => void
  onOpenPlugin: (pluginId: string) => void
  /** 当前实例作用域；null 表示全局视图 */
  scopeInstanceKey: string | null
  /** 该插件在当前实例下的生效状态与来源层级 */
  scopeStatus: PluginScopeStatus | null
  /** 实例级启停接口是否不可用 */
  scopeUnavailable: boolean
  /** 清除实例级覆盖、回落上层 */
  onRestoreInherit: (id: string) => void
}

// 插件详情组件
function PluginDetails({
  plugin,
  activationStrategyLoading,
  onBack,
  onToggleEnabled,
  onOpenPlugin,
  scopeInstanceKey,
  scopeStatus,
  scopeUnavailable,
  onRestoreInherit,
}: PluginDetailProps) {
  const [activeTab, setActiveTab] = useState<PluginTabKey>('info')
  const [webuiLoaded, setWebuiLoaded] = useState(false)
  const [reloadConfirmOpen, setReloadConfirmOpen] = useState(false)
  const [resetDataConfirmOpen, setResetDataConfirmOpen] = useState(false)
  const [errorDetailOpen, setErrorDetailOpen] = useState(false)
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set())
  const [expandedDataRows, setExpandedDataRows] = useState<Set<number>>(new Set())
  const [updateConfirmOpen, setUpdateConfirmOpen] = useState(false)
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)
  const [deleteDataConfirmOpen, setDeleteDataConfirmOpen] = useState(false)
  const [deleteDataId, setDeleteDataId] = useState<number | null>(null)
  const [clearDataOnDelete, setClearDataOnDelete] = useState(false) // 新增：删除时是否清除数据的状态
  const [moreMenuAnchorEl, setMoreMenuAnchorEl] = useState<null | HTMLElement>(null) // 更多操作菜单锚点
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('md'))
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  const notification = useNotification()
  const { t, i18n } = useTranslation('plugins')
  const permLevel = useAuthStore(state => state.userInfo?.perm_level ?? 0)

  // 获取插件配置：实例作用域下读写实例级配置，全局视图保持原行为
  const { data: pluginConfig, isLoading: configLoading } = useQuery({
    queryKey: ['plugin-config', plugin?.id, scopeInstanceKey],
    queryFn: () =>
      scopeInstanceKey
        ? instanceScopeApi.getPluginInstanceConfig(plugin.id, scopeInstanceKey)
        : unifiedConfigApi.getPluginConfig(plugin?.id),
    enabled: !!plugin && activeTab === 'config' && plugin.hasConfig,
    retry: false,
  })

  const pluginConfigService = useMemo(() => {
    if (!scopeInstanceKey) {
      return createConfigService(`plugin_${plugin.id}`)
    }
    return {
      getConfigList: () => instanceScopeApi.getPluginInstanceConfig(plugin.id, scopeInstanceKey),
      getModelGroups: unifiedConfigApi.getModelGroups,
      getModelTypes: unifiedConfigApi.getModelTypes,
      batchUpdateConfig: async (_key: string, configs: Record<string, string>) => {
        await instanceScopeApi.savePluginInstanceConfig(plugin.id, scopeInstanceKey, configs)
      },
      saveConfig: async () => {},
      reloadConfig: async () => {},
    }
  }, [plugin.id, scopeInstanceKey])

  // 获取插件文档
  const {
    data: pluginDocs,
    isLoading: docsLoading,
    error: docsError,
  } = useQuery({
    queryKey: ['plugin-docs', plugin?.id],
    queryFn: () => pluginsApi.getPluginDocs(plugin.id),
    enabled: !!plugin && activeTab === 'info',
  })

  // 获取插件数据
  const { data: pluginData = [], isLoading: isDataLoading } = useQuery({
    queryKey: ['plugin-data', plugin?.id],
    queryFn: () => pluginsApi.getPluginData(plugin.id),
    enabled: !!plugin && activeTab === 'data',
  })

  // 重载插件
  const reloadMutation = useMutation({
    mutationFn: async () => {
      if (!plugin.moduleName) {
        throw new Error(t('messages.invalidModuleName'))
      }
      const result = await pluginsApi.reloadPlugins(plugin.moduleName)
      if (!result.success) {
        throw new Error(result.errorMsg || t('messages.reloadFailed'))
      }
      return true
    },
    onSuccess: () => {
      notification.success(t('messages.reloadSuccess', { name: plugin.name }))
      queryClient.invalidateQueries({ queryKey: ['plugins'] })
      queryClient.invalidateQueries({ queryKey: ['plugin-config', plugin.id] })
    },
    onError: (error: Error) => {
      notification.error(error.message)
    },
  })

  // 删除单条数据
  const deleteDataMutation = useMutation({
    mutationFn: (dataId: number) => pluginsApi.deletePluginData(plugin.id, dataId),
    onSuccess: () => {
      notification.success(t('messages.dataDeleted'))
      queryClient.invalidateQueries({ queryKey: ['plugin-data', plugin.id] })
    },
    onError: (error: Error) => {
      notification.error(`${t('messages.deleteFailed')}: ${error.message}`)
    },
  })

  // 删除插件所有数据
  const resetDataMutation = useMutation({
    mutationFn: () => pluginsApi.resetPluginData(plugin.id),
    onSuccess: () => {
      notification.success(t('messages.dataResetSuccess'))
      queryClient.invalidateQueries({ queryKey: ['plugin-data', plugin.id] })
    },
    onError: (error: Error) => {
      notification.error(`${t('messages.resetFailed')}: ${error.message}`)
    },
  })

  const updateCallPriorityMutation = useMutation({
    mutationFn: (priority: PluginCallPriority) =>
      pluginsApi.updatePluginCallPriority(plugin.id, priority),
    onSuccess: () => {
      notification.success(t('messages.callPriorityUpdated'))
      queryClient.invalidateQueries({ queryKey: ['plugins'] })
      queryClient.invalidateQueries({ queryKey: ['plugin-detail', plugin.id] })
    },
    onError: (error: Error) => {
      notification.error(error.message)
    },
  })

  const updateActivationStrategyMutation = useMutation({
    mutationFn: (strategy: 'auto' | 'allow_sleep' | 'forbid_sleep') =>
      pluginsApi.updatePluginActivationStrategy(plugin.id, strategy),
    onSuccess: () => {
      notification.success(t('messages.activationStrategyUpdated'))
      queryClient.invalidateQueries({ queryKey: ['plugins'] })
      queryClient.invalidateQueries({ queryKey: ['plugin-detail', plugin.id] })
    },
    onError: (error: Error) => {
      notification.error(error.message)
    },
  })

  // 删除云端插件
  const removePackageMutation = useMutation({
    mutationFn: () => pluginsApi.removePackage(plugin.moduleName, clearDataOnDelete),
    onSuccess: () => {
      notification.success(
        clearDataOnDelete
          ? t('messages.deleteWithDataSuccess', { name: plugin.name })
          : t('messages.deleteSuccess', { name: plugin.name })
      )
      queryClient.invalidateQueries({ queryKey: ['plugins'] })
      onBack() // 返回插件列表
    },
    onError: (error: Error) => {
      notification.error(`${t('messages.deleteFailed')}: ${error.message}`)
    },
  })

  // 更新云端插件
  const updatePackageMutation = useMutation({
    mutationFn: async () => {
      if (!plugin.moduleName) {
        throw new Error(t('messages.invalidModuleName'))
      }
      const result = await pluginsApi.updatePackage(plugin.moduleName)
      if (!result.success) {
        throw new Error(result.errorMsg || t('messages.updateFailed'))
      }
      return true
    },
    onSuccess: () => {
      notification.success(t('messages.updateSuccess', { name: plugin.name }))
      queryClient.invalidateQueries({ queryKey: ['plugins'] })
      queryClient.invalidateQueries({ queryKey: ['plugin-config', plugin.id] })
    },
    onError: (error: Error) => {
      notification.error(error.message)
    },
  })

  // 获取插件类型
  const getPluginType = () => {
    if (plugin.isBuiltin) return 'builtin'
    if (plugin.isPackage) return 'package'
    return 'local'
  }

  // 获取插件类型中文名
  const getPluginTypeText = () => {
    const type = getPluginType()
    return t(`types.${type}`)
  }

  const activationStrategy = plugin.activationStrategy
  const activationConfigurable =
    !!activationStrategy && !plugin.loadFailed && activationStrategy.canChangeStrategy
  const activationControllerEnabled = activationStrategy?.controller.enabled ?? true

  // 调用优先级全进程一份：租户看得到档位（用来理解提示词），只有平台方能改。
  // 判据与后端 `require_role(Role.Super)` 相同：`/user/me` 回的 `perm_level` 就是后端比较的那个字段。
  // 按租户角色判会放过两种人：身份接口出错时兜底成平台方的租户，以及未启用多租户时的普通管理员，
  // 他们看到一个可用的下拉框、点了必然 403。登录信息还没取到时按 0 处理，即只读
  const callPriority = plugin.callPriority
  const canEditCallPriority = permLevel >= SUPER_PERM_LEVEL

  // 实例作用域下，生效值来自分层叠加的结果；接口不可用时退回全局值并禁用开关
  const effectiveEnabled = scopeStatus ? scopeStatus.enabled : plugin.enabled
  const hasInstanceOverride = scopeStatus?.instance_override !== null && scopeStatus !== null

  // 按钮点击处理函数
  const handleNavigateToEditor = () => {
    navigate('/plugins/editor')
  }

  const pluginTabs = useMemo(
    () =>
      [
        { key: 'info' as const, label: t('tabs.info'), icon: <InfoIcon />, isVisible: true },
        {
          key: 'config' as const,
          label: t('tabs.config'),
          icon: <SettingsIcon />,
          isVisible: plugin.hasConfig,
        },
        {
          key: 'webui' as const,
          label: t('tabs.webui'),
          icon: <WebAssetIcon />,
          // 停用的插件不挂载路由，WebUI 打开只会是 404，这时不显示该标签页
          isVisible: Boolean(plugin.webuiPath && !plugin.loadFailed && plugin.enabled),
        },
        { key: 'methods' as const, label: t('tabs.methods'), icon: <CodeIcon />, isVisible: true },
        { key: 'webhook' as const, label: t('tabs.webhook'), icon: <WebhookIcon />, isVisible: true },
        { key: 'data' as const, label: t('tabs.data'), icon: <StorageIcon />, isVisible: true },
      ].filter(tab => tab.isVisible),
    [plugin.hasConfig, plugin.loadFailed, plugin.webuiPath, plugin.enabled, t]
  )

  const handleOpenWebuiInNewPage = () => {
    if (!plugin.webuiPath) return
    const openedWindow = window.open(plugin.webuiPath, '_blank', 'noopener,noreferrer')
    if (openedWindow) {
      openedWindow.opener = null
    }
  }

  useEffect(() => {
    if (!pluginTabs.some(tab => tab.key === activeTab)) {
      setActiveTab('info')
    }
  }, [activeTab, pluginTabs])

  // 离开 WebUI 标签页时 iframe 会被卸载，回来时重新加载；加载状态要一起重置，否则看到的是一片空白而不是加载提示
  useEffect(() => {
    if (activeTab !== 'webui') {
      setWebuiLoaded(false)
    }
  }, [activeTab])

  useEffect(() => {
    setWebuiLoaded(false)
  }, [plugin.id, plugin.webuiPath])

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        minHeight: 0,
        gap: 2,
        overflow: 'hidden',
      }}
    >
      {/* 标题和总开关 */}
      <Card sx={{ ...CARD_VARIANTS.default.styles, p: 2, flexShrink: 0 }}>
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            flexWrap: 'wrap',
            gap: 2,
          }}
        >
          <Box
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1.5,
              flexGrow: 1,
              overflow: 'hidden',
            }}
          >
            {isMobile && (
              <IconActionButton onClick={onBack} edge="start">
                <ArrowBackIcon />
              </IconActionButton>
            )}
            <Chip
              label={getPluginTypeText()}
              size="small"
              color={pluginTypeColors[getPluginType()]}
              sx={CHIP_VARIANTS.base(isSmall)}
            />
            <Typography
              variant="h6"
              component="div"
              sx={{
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontWeight: 600,
              }}
            >
              {getPluginName(plugin, i18n.language)}
            </Typography>
            {plugin.quarantine && (
              // 自动隔离必须是用户看得见的事件，否则表现出来就是"某个功能悄悄消失了"
              <Tooltip
                title={t('status.quarantinedTooltip', {
                  stage: plugin.quarantine.stage,
                  failures: plugin.quarantine.failures,
                  detail: plugin.quarantine.detail,
                })}
              >
                <Chip
                  label={t('status.quarantined')}
                  color="warning"
                  size="small"
                  sx={CHIP_VARIANTS.base(isSmall)}
                />
              </Tooltip>
            )}
          </Box>
          {plugin.loadFailed ? (
            // 加载失败时显示错误信息
            <Chip
              label={t('status.loadFailed')}
              color="error"
              size="small"
              sx={{ mr: 0, ml: 'auto', fontWeight: 600 }}
            />
          ) : scopeInstanceKey ? (
            // 实例作用域：开关写实例级覆盖，同时展示生效值与来源层级
            <Box sx={{ ml: 'auto' }}>
              <ScopeOverrideControl
                source={scopeStatus?.source ?? 'global'}
                overridden={hasInstanceOverride}
                effectiveLabel={
                  effectiveEnabled
                    ? t('plugins.effectiveEnabled', { ns: 'instance-scope' })
                    : t('plugins.effectiveDisabled', { ns: 'instance-scope' })
                }
                onRestoreInherit={() => onRestoreInherit(plugin.id)}
                restoreDisabled={scopeUnavailable}
              >
                <Tooltip title={t('plugins.scopeToggleTooltip', { ns: 'instance-scope' })}>
                  <span>
                    <Switch
                      checked={effectiveEnabled}
                      disabled={scopeUnavailable}
                      onChange={e => onToggleEnabled(plugin.id, e.target.checked)}
                      color="primary"
                    />
                  </span>
                </Tooltip>
              </ScopeOverrideControl>
            </Box>
          ) : (
            // 正常加载时显示开关
            <FormControlLabel
              control={
                <Switch
                  checked={plugin.enabled}
                  onChange={e => onToggleEnabled(plugin.id, e.target.checked)}
                  color="primary"
                />
              }
              label={plugin.enabled ? t('status.enabled') : t('status.disabled')}
              sx={{ mr: 0, ml: 'auto' }}
            />
          )}
        </Box>
      </Card>

      {/* 选项卡导航 */}
      <Card sx={{ ...CARD_VARIANTS.default.styles }}>
        <Box sx={{ display: 'flex', alignItems: 'center', px: { xs: 1, sm: 1.5 } }}>
          <PageTabs
            value={activeTab}
            onChange={(_, newValue: PluginTabKey) => setActiveTab(newValue)}
            variant="scrollable"
            scrollButtons="auto"
            allowScrollButtonsMobile
            sx={{
              flexGrow: 1,
              '& .MuiTab-root': {
                minHeight: 48,
                minWidth: 'auto',
                px: { xs: 1.5, sm: 2 },
                flexDirection: 'row',
                gap: 1,
                '& .MuiTab-iconWrapper': {
                  marginBottom: 0,
                  mr: 0.5,
                },
              },
            }}
          >
            {pluginTabs.map(tab => (
              <Tab key={tab.key} value={tab.key} label={tab.label} icon={tab.icon} />
            ))}
          </PageTabs>

          {/* 操作按钮组 */}
          <Stack direction="row" spacing={1} sx={{ pl: 2, flexShrink: 0 }}>
            {isMobile ? (
              // 移动端：只显示更多操作按钮
              <>
                <IconActionButton
                  size="small"
                  onClick={event => setMoreMenuAnchorEl(event.currentTarget)}
                  sx={{ border: 1, borderColor: 'divider' }}
                >
                  <MoreVertIcon />
                </IconActionButton>
                <Menu
                  anchorEl={moreMenuAnchorEl}
                  open={Boolean(moreMenuAnchorEl)}
                  onClose={() => setMoreMenuAnchorEl(null)}
                  transformOrigin={{ horizontal: 'right', vertical: 'top' }}
                  anchorOrigin={{ horizontal: 'right', vertical: 'bottom' }}
                >
                  {!plugin.isBuiltin && (
                    <MenuItem
                      onClick={() => {
                        if (plugin.isPackage) {
                          setDeleteConfirmOpen(true)
                        } else {
                          handleNavigateToEditor()
                        }
                        setMoreMenuAnchorEl(null)
                      }}
                      sx={{ color: plugin.isPackage ? 'error.main' : 'warning.main' }}
                    >
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        {plugin.isPackage ? (
                          <DeleteIcon fontSize="small" />
                        ) : (
                          <EditIcon fontSize="small" />
                        )}
                        {plugin.isPackage ? t('actions.delete') : t('actions.edit')}
                      </Box>
                    </MenuItem>
                  )}
                  {plugin.isPackage && (
                    <MenuItem
                      onClick={() => {
                        setUpdateConfirmOpen(true)
                        setMoreMenuAnchorEl(null)
                      }}
                      sx={{ color: 'success.main' }}
                    >
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <RefreshIcon fontSize="small" />
                        {t('actions.update')}
                      </Box>
                    </MenuItem>
                  )}
                  <MenuItem
                    onClick={() => {
                      setResetDataConfirmOpen(true)
                      setMoreMenuAnchorEl(null)
                    }}
                    disabled={plugin.loadFailed}
                    sx={{ color: plugin.loadFailed ? 'text.disabled' : 'warning.main' }}
                  >
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      <DeleteIcon fontSize="small" />
                      {t('actions.reset')}
                    </Box>
                  </MenuItem>
                  <MenuItem
                    onClick={() => {
                      setReloadConfirmOpen(true)
                      setMoreMenuAnchorEl(null)
                    }}
                    disabled={plugin.isBuiltin || (plugin.loadFailed && plugin.isPackage)}
                    sx={{
                      color:
                        plugin.isBuiltin || (plugin.loadFailed && plugin.isPackage)
                          ? 'text.disabled'
                          : 'primary.main',
                    }}
                  >
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      <RefreshIcon fontSize="small" />
                      {t('actions.reload')}
                    </Box>
                  </MenuItem>
                </Menu>
              </>
            ) : (
              // 桌面端：显示所有按钮
              <>
                {!plugin.isBuiltin && (
                  <ActionButton
                    tone={plugin.isPackage ? 'danger' : 'secondary'}
                    startIcon={plugin.isPackage ? <DeleteIcon /> : <EditIcon />}
                    onClick={() =>
                      plugin.isPackage ? setDeleteConfirmOpen(true) : handleNavigateToEditor()
                    }
                    size="small"
                    sx={!plugin.isPackage ? { color: 'warning.main', borderColor: 'warning.main' } : undefined}
                  >
                    {plugin.isPackage ? t('actions.delete') : t('actions.edit')}
                  </ActionButton>
                )}
                {plugin.isPackage && (
                  <ActionButton
                    tone="secondary"
                    startIcon={<RefreshIcon />}
                    onClick={() => setUpdateConfirmOpen(true)}
                    size="small"
                    sx={{ color: 'success.main', borderColor: 'success.main' }}
                  >
                    {t('actions.update')}
                  </ActionButton>
                )}
                <ActionButton
                  tone="secondary"
                  startIcon={<DeleteIcon />}
                  onClick={() => setResetDataConfirmOpen(true)}
                  disabled={plugin.loadFailed}
                  size="small"
                  sx={{ color: 'warning.main', borderColor: 'warning.main' }}
                >
                  {t('actions.reset')}
                </ActionButton>
                <ActionButton
                  tone="secondary"
                  startIcon={<RefreshIcon />}
                  onClick={() => setReloadConfirmOpen(true)}
                  disabled={plugin.isBuiltin || (plugin.loadFailed && plugin.isPackage)}
                  size="small"
                >
                  {t('actions.reload')}
                </ActionButton>
              </>
            )}
          </Stack>
        </Box>
      </Card>

      {/* 选项卡内容 */}
      <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        {activeTab === 'info' && (
          <Stack spacing={2}>
            {/* 插件信息 */}
            <Card sx={CARD_VARIANTS.default.styles}>
              <CardContent sx={{ p: { xs: 2, sm: 3 } }}>
                {plugin.loadFailed ? (
                  <Alert severity="error" sx={{ mb: 2 }}>
                    <Typography sx={{ fontWeight: 600, mb: 1 }}>
                      {t('status.loadFailed')}
                    </Typography>
                    <Typography variant="body2" sx={{ fontFamily: 'monospace', whiteSpace: 'pre-wrap' }}>
                      {plugin.description}
                    </Typography>
                    {plugin.errorType && (
                      <Typography variant="caption" sx={{ display: 'block', mt: 1 }}>
                        {t('info.errorType')}：{plugin.errorType}
                      </Typography>
                    )}
                    {plugin.filePath && (
                      <Typography variant="caption" sx={{ display: 'block', mt: 0.5 }}>
                        {t('info.filePath')}：{plugin.filePath}
                      </Typography>
                    )}
                    {plugin.stackTrace && (
                      <ActionButton
                        size="small"
                        onClick={() => setErrorDetailOpen(true)}
                        sx={{ mt: 1.5, textTransform: 'none' }}
                      >
                        {t('actions.viewDetails')}
                      </ActionButton>
                    )}
                  </Alert>
                ) : (
                  <Typography variant="body1" color="text.secondary" sx={{ lineHeight: 1.6, mb: 3 }}>
                    {getPluginDescription(plugin, i18n.language)}
                  </Typography>
                )}
                <Divider />
                <Box
                  sx={{
                    display: 'grid',
                    gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, 1fr)' },
                    gap: 2,
                    mt: 3,
                  }}
                >
                  <Stack direction="row" spacing={1.5} alignItems="center">
                    <PersonIcon color="action" />
                    <Typography variant="body2">
                      <strong>{t('info.author')}：</strong> {plugin.author}
                    </Typography>
                  </Stack>
                  <Stack direction="row" spacing={1.5} alignItems="center">
                    <VpnKeyIcon color="action" />
                    <Typography variant="body2">
                      <strong>{t('info.moduleName')}：</strong> {plugin.moduleName}
                    </Typography>
                  </Stack>
                  <Stack direction="row" spacing={1.5} alignItems="center">
                    <BookmarkIcon color="action" />
                    <Typography variant="body2">
                      <strong>{t('info.version')}：</strong> {plugin.version}
                    </Typography>
                  </Stack>
                  <Stack direction="row" spacing={1.5} alignItems="center">
                    <CategoryIcon color="action" />
                    <Typography variant="body2">
                      <strong>{t('info.type')}：</strong> {getPluginTypeText()}
                    </Typography>
                  </Stack>
                  <Stack direction="row" spacing={1.5} alignItems="center">
                    <LinkIcon color="action" />
                    <Typography variant="body2">
                      <strong>{t('info.link')}：</strong>{' '}
                      <Link
                        href={plugin.url}
                        target="_blank"
                        rel="noreferrer"
                        sx={{ verticalAlign: 'middle' }}
                      >
                        {plugin.url || t('info.none')}
                      </Link>
                    </Typography>
                  </Stack>
                  {!plugin.loadFailed && <PluginConsoleLink pluginId={plugin.id} label={t('info.openConsole')} />}
                  {!plugin.loadFailed && activationStrategy && (
                    <Stack direction="row" spacing={1.5} alignItems="center">
                      <ExtensionIcon color="action" />
                      <Box sx={{ minWidth: 0, flex: 1 }}>
                        <Stack
                          direction={{ xs: 'column', sm: 'row' }}
                          spacing={1}
                          alignItems={{ xs: 'stretch', sm: 'center' }}
                        >
                          <Typography variant="body2" sx={{ whiteSpace: 'nowrap', minWidth: 'fit-content' }}>
                            <strong>{t('activation.strategyLabel')}：</strong>
                          </Typography>
                          <TextField
                            select
                            size="small"
                            value={activationStrategy.configured}
                            disabled={
                              !activationConfigurable ||
                              !activationControllerEnabled ||
                              updateActivationStrategyMutation.isPending
                            }
                            onChange={e =>
                              updateActivationStrategyMutation.mutate(
                                e.target.value as 'auto' | 'allow_sleep' | 'forbid_sleep'
                              )
                            }
                            sx={{ minWidth: { xs: '100%', sm: 136 }, maxWidth: { sm: 164 } }}
                          >
                            <MenuItem value="auto">{t('activation.options.auto')}</MenuItem>
                            <MenuItem value="allow_sleep" disabled={!activationStrategy.canEnableSleep}>
                              {t('activation.options.allow_sleep')}
                            </MenuItem>
                            <MenuItem value="forbid_sleep">{t('activation.options.forbid_sleep')}</MenuItem>
                          </TextField>
                          {activationStrategyLoading ? (
                            <Chip
                              icon={<CircularProgress size={14} color="inherit" />}
                              label={t('activation.loadingStatus')}
                              size="small"
                              sx={CHIP_VARIANTS.base(isSmall)}
                            />
                          ) : (
                            <Tooltip
                              arrow
                              placement="top"
                              title={
                                <Stack spacing={1} sx={{ maxWidth: 340, py: 0.25 }}>
                                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                                    {t('activation.strategyLabel')}
                                  </Typography>
                                  <Typography variant="caption" sx={{ opacity: 0.9 }}>
                                    {t('activation.description')}
                                  </Typography>
                                  {!activationControllerEnabled && (
                                    <Typography variant="body2" sx={{ lineHeight: 1.6 }}>
                                      {t('activation.controllerDisabled')}
                                    </Typography>
                                  )}
                                  {activationStrategy.isProtected && (
                                    <Typography variant="body2" sx={{ lineHeight: 1.6 }}>
                                      {t('activation.protected')}
                                    </Typography>
                                  )}
                                  <Typography variant="body2" sx={{ lineHeight: 1.6 }}>
                                    {t('activation.note')}
                                  </Typography>
                                  <Typography variant="body2" sx={{ lineHeight: 1.6 }}>
                                    {t('activation.currentStrategy', {
                                      strategy: t(`activation.options.${activationStrategy.configured}`),
                                    })}
                                    {' · '}
                                    {t(`activation.effective.${activationStrategy.effective}`)}
                                  </Typography>
                                  {activationStrategy.canEnableSleep ? (
                                    <Box>
                                      <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.25 }}>
                                        {t('activation.sleepBrief')}
                                      </Typography>
                                      <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>
                                        {activationStrategy.sleepBrief || t('activation.none')}
                                      </Typography>
                                    </Box>
                                  ) : activationStrategy.isProtected ? (
                                    <Typography variant="body2" sx={{ lineHeight: 1.6 }}>
                                      {t('activation.protectedNoSleep')}
                                    </Typography>
                                  ) : (
                                    <Typography variant="body2" sx={{ lineHeight: 1.6 }}>
                                      {t('activation.noSleepBrief')}
                                    </Typography>
                                  )}
                                </Stack>
                              }
                            >
                              <Chip
                                icon={<InfoIcon />}
                                label={
                                  activationControllerEnabled
                                    ? t(`activation.effective.${activationStrategy.effective}`)
                                    : t('activation.controllerOff')
                                }
                                color={
                                  !activationControllerEnabled
                                    ? 'warning'
                                    : activationStrategy.effective === 'sleep'
                                    ? 'warning'
                                    : 'success'
                                }
                                size="small"
                                sx={CHIP_VARIANTS.base(isSmall)}
                                clickable={!activationControllerEnabled}
                                onClick={
                                  !activationControllerEnabled
                                    ? () => onOpenPlugin(activationStrategy.controller.pluginId)
                                    : undefined
                                }
                              />
                            </Tooltip>
                          )}
                        </Stack>
                      </Box>
                    </Stack>
                  )}
                  {!plugin.loadFailed && callPriority && (
                    <Stack direction="row" spacing={1.5} alignItems="center">
                      <CallPriorityIcon color="action" />
                      <Box sx={{ minWidth: 0, flex: 1 }}>
                        <Stack
                          direction={{ xs: 'column', sm: 'row' }}
                          spacing={1}
                          alignItems={{ xs: 'stretch', sm: 'center' }}
                        >
                          <Typography variant="body2" sx={{ whiteSpace: 'nowrap', minWidth: 'fit-content' }}>
                            <strong>{t('callPriority.label')}：</strong>
                          </Typography>
                          <Tooltip
                            arrow
                            placement="top"
                            title={
                              <Stack spacing={0.5} sx={{ maxWidth: 320, py: 0.25 }}>
                                <Typography variant="body2" sx={{ lineHeight: 1.6 }}>
                                  {t('callPriority.description')}
                                </Typography>
                                <Typography variant="body2" sx={{ lineHeight: 1.6 }}>
                                  {t('callPriority.globalNote')}
                                </Typography>
                                {!canEditCallPriority && (
                                  <Typography variant="body2" sx={{ lineHeight: 1.6, fontWeight: 600 }}>
                                    {t('callPriority.platformOnly')}
                                  </Typography>
                                )}
                              </Stack>
                            }
                          >
                            <TextField
                              select
                              size="small"
                              value={callPriority.configured}
                              disabled={!canEditCallPriority || updateCallPriorityMutation.isPending}
                              onChange={e =>
                                updateCallPriorityMutation.mutate(e.target.value as PluginCallPriority)
                              }
                              sx={{ minWidth: { xs: '100%', sm: 112 }, maxWidth: { sm: 140 } }}
                            >
                              <MenuItem value="auto">{t('callPriority.options.auto')}</MenuItem>
                              <MenuItem value="high">{t('callPriority.options.high')}</MenuItem>
                              <MenuItem value="medium">{t('callPriority.options.medium')}</MenuItem>
                              <MenuItem value="low">{t('callPriority.options.low')}</MenuItem>
                            </TextField>
                          </Tooltip>
                        </Stack>
                      </Box>
                    </Stack>
                  )}
                </Box>
              </CardContent>
            </Card>

            {/* 插件文档 */}
            {docsLoading ? (
              <Card sx={CARD_VARIANTS.default.styles}>
                <CardContent sx={{ textAlign: 'center', p: 3 }}>
                  <CircularProgress size={32} />
                  <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
                    {t('info.loadingDocs')}
                  </Typography>
                </CardContent>
              </Card>
            ) : docsError ? (
              <Alert severity="error" sx={{ m: 2 }}>
                {t('info.loadDocsFailed')}：{(docsError as Error).message}
              </Alert>
            ) : pluginDocs?.exists ? (
              <Card sx={CARD_VARIANTS.default.styles}>
                <CardContent sx={{ p: { xs: 2, sm: 3 } }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 2 }}>
                    <DescriptionIcon color="primary" />
                    <Typography variant="h6" sx={{ fontWeight: 600 }}>
                      {t('info.pluginDocs')}
                    </Typography>
                  </Box>
                  <MarkdownRenderer>{pluginDocs.docs || ''}</MarkdownRenderer>
                </CardContent>
              </Card>
            ) : (
              <Alert severity="info" icon={<InfoIcon />}>
                {t('info.noDocs')}
              </Alert>
            )}
          </Stack>
        )}

        {/* 配置项 */}
        {plugin.hasConfig && activeTab === 'config' && (
          <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
            {pluginConfig && pluginConfig.length > 0 ? (
              <ConfigTable
                configKey={`plugin_${plugin.id}`}
                configService={pluginConfigService}
                configs={pluginConfig}
                loading={configLoading}
                onRefresh={() =>
                  queryClient.invalidateQueries({ queryKey: ['plugin-config', plugin.id] })
                }
                emptyMessage={t('config.noConfig')}
              />
            ) : (
              <Card sx={CARD_VARIANTS.default.styles}>
                <CardContent>
                  <Alert severity="info">{t('config.noConfig')}</Alert>
                </CardContent>
              </Card>
            )}
          </Box>
        )}

        {/* 插件页面 */}
        {activeTab === 'webui' && plugin.webuiPath && (
          <Card
            sx={{
              ...CARD_VARIANTS.default.styles,
              height: '100%',
              minHeight: { xs: 520, md: 640 },
              overflow: 'hidden',
            }}
          >
            <Box
              sx={{
                position: 'relative',
                height: '100%',
                minHeight: { xs: 520, md: 640 },
                '& iframe': {
                  width: '100%',
                  height: '100%',
                  border: 'none',
                  opacity: webuiLoaded ? 1 : 0,
                  transition: 'opacity 0.3s ease',
                },
              }}
            >
              {plugin.webuiType === 'route' && (
                <ActionButton
                  size="small"
                  startIcon={<OpenInNewIcon fontSize="small" />}
                  onClick={handleOpenWebuiInNewPage}
                  sx={{
                    position: 'absolute',
                    top: 12,
                    right: 12,
                    zIndex: 2,
                    bgcolor: 'background.paper',
                    boxShadow: 1,
                    opacity: 0.55,
                    transition: 'opacity 0.2s ease, background-color 0.2s ease, box-shadow 0.2s ease',
                    '&:hover': {
                      opacity: 1,
                      bgcolor: 'background.paper',
                      boxShadow: 2,
                    },
                  }}
                >
                  {t('webui.openInNewPage')}
                </ActionButton>
              )}
              <Box
                component="iframe"
                title={t('webui.title', { name: getPluginName(plugin, i18n.language) })}
                src={plugin.webuiPath}
                onLoad={() => setWebuiLoaded(true)}
              />
              <Box
                sx={{
                  position: 'absolute',
                  inset: 0,
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: 2,
                  bgcolor: 'background.paper',
                  opacity: webuiLoaded ? 0 : 1,
                  pointerEvents: webuiLoaded ? 'none' : 'auto',
                  transition: 'opacity 0.3s ease',
                }}
              >
                <CircularProgress size={32} />
                <Typography variant="body2" color="text.secondary">
                  {t('webui.loading')}
                </Typography>
              </Box>
            </Box>
          </Card>
        )}

        {/* 方法列表 */}
        {activeTab === 'methods' && (
          <Card sx={CARD_VARIANTS.default.styles}>
            <CardContent sx={{ p: isSmall ? 1.5 : 2 }}>
              {plugin.methods && plugin.methods.length > 0 ? (
                <TableContainer>
                  <Table size={isSmall ? 'small' : 'medium'}>
                    <TableHead>
                      <TableRow>
                        <TableCell width={isMobile ? '30%' : '20%'} sx={{ py: isSmall ? 1 : 1.5 }}>
                          {t('methods.name')}
                        </TableCell>
                        <TableCell width={isMobile ? '25%' : '18%'} sx={{ py: isSmall ? 1 : 1.5 }}>
                          {t('methods.title')}
                        </TableCell>
                        <TableCell width={isMobile ? '25%' : '15%'} sx={{ py: isSmall ? 1 : 1.5 }}>
                          {t('methods.type')}
                        </TableCell>
                        <TableCell sx={{ py: isSmall ? 1 : 1.5 }}>
                          {t('methods.description')}
                        </TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {plugin.methods.map((method: Method) => (
                        <TableRow key={method.name}>
                          <TableCell sx={{ py: isSmall ? 0.75 : 1.25 }}>
                            <Typography
                              variant="body2"
                              sx={{
                                fontFamily: 'monospace',
                                fontWeight: 'bold',
                                fontSize: isSmall ? '0.7rem' : '0.875rem',
                                overflowWrap: 'break-word',
                                wordBreak: 'break-all',
                              }}
                            >
                              {method.name}
                            </Typography>
                          </TableCell>
                          <TableCell sx={{ py: isSmall ? 0.75 : 1.25 }}>
                            <Typography
                              variant="body2"
                              sx={{
                                fontWeight: 600,
                                fontSize: isSmall ? '0.75rem' : '0.875rem',
                                overflowWrap: 'break-word',
                                wordBreak: 'break-word',
                              }}
                            >
                              {method.title}
                            </Typography>
                          </TableCell>
                          <TableCell sx={{ py: isSmall ? 0.75 : 1.25 }}>
                            <Tooltip
                              title={t(`methodDescriptions.${method.type}`)}
                              arrow
                              placement="top"
                            >
                              <Chip
                                label={t(`methodTypes.${method.type}`)}
                                color={methodTypeColors[method.type]}
                                size="small"
                                sx={CHIP_VARIANTS.base(isSmall)}
                              />
                            </Tooltip>
                          </TableCell>
                          <TableCell sx={{ py: isSmall ? 0.75 : 1.25 }}>
                            <Typography
                              variant="body2"
                              sx={{ fontSize: isSmall ? '0.75rem' : '0.875rem' }}
                            >
                              {method.description}
                            </Typography>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
              ) : (
                <Alert severity="info" sx={{ mt: 2 }}>
                  {t('methods.noMethods')}
                </Alert>
              )}
            </CardContent>
          </Card>
        )}

        {/* Webhook 列表 */}
        {activeTab === 'webhook' && (
          <Card sx={CARD_VARIANTS.default.styles}>
            <CardContent sx={{ p: isSmall ? 1.5 : 2 }}>
              {plugin.webhooks && plugin.webhooks.length > 0 ? (
                <TableContainer>
                  <Table size={isSmall ? 'small' : 'medium'}>
                    <TableHead>
                      <TableRow>
                        <TableCell width={isSmall ? 100 : 150} sx={{ py: isSmall ? 1 : 1.5 }}>
                          {t('webhook.endpoint')}
                        </TableCell>
                        <TableCell sx={{ py: isSmall ? 1 : 1.5 }}>{t('webhook.name')}</TableCell>
                        <TableCell
                          width={isSmall ? 80 : 132}
                          align="center"
                          sx={{ py: isSmall ? 1 : 1.5 }}
                        >
                          {t('webhook.actions')}
                        </TableCell>
                        <TableCell width={36} padding="none" />
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {plugin.webhooks.map(webhook => (
                        <React.Fragment key={webhook.endpoint}>
                          <TableRow>
                            <TableCell>
                              <Typography
                                variant="body2"
                                sx={{
                                  fontFamily: 'monospace',
                                  fontWeight: 'bold',
                                  fontSize: isSmall ? '0.7rem' : '0.875rem',
                                  overflowWrap: 'break-word',
                                  wordBreak: 'break-all',
                                }}
                              >
                                {webhook.endpoint}
                              </Typography>
                            </TableCell>
                            <TableCell>
                              <Typography
                                variant="body2"
                                sx={{ fontSize: isSmall ? '0.75rem' : '0.875rem' }}
                              >
                                {webhook.name}
                              </Typography>
                            </TableCell>
                            <TableCell align="right">
                              <ActionButton
                                size="small"
                                startIcon={<ContentCopyIcon fontSize="small" />}
                                onClick={async () => {
                                  const url = `${server_addr}/api/webhook/${webhook.endpoint}`
                                  const success = await copyText(url)
                                  if (success) {
                                    notification.success(t('webhook.copied'))
                                  } else {
                                    notification.error(t('common.messages.operationFailed') || 'Failed to copy')
                                  }
                                }}
                                sx={{
                                  textTransform: 'none',
                                  color: 'primary.main',
                                  '&:hover': {
                                    backgroundColor: 'transparent',
                                    textDecoration: 'underline',
                                  },
                                  fontSize: isSmall ? '0.7rem' : '0.8rem',
                                  px: isSmall ? 0.5 : 1,
                                  minWidth: 'auto',
                                  '& .MuiButton-startIcon': {
                                    mr: isSmall ? 0.3 : 0.5,
                                    '& svg': {
                                      fontSize: isSmall ? '0.9rem' : '1rem',
                                    },
                                  },
                                }}
                              >
                                {t('webhook.copy')}
                              </ActionButton>
                            </TableCell>
                            <TableCell padding="none">
                              <IconActionButton
                                size="small"
                                onClick={() => {
                                  const newExpandedRows = new Set(expandedRows)
                                  if (newExpandedRows.has(webhook.endpoint)) {
                                    newExpandedRows.delete(webhook.endpoint)
                                  } else {
                                    newExpandedRows.add(webhook.endpoint)
                                  }
                                  setExpandedRows(newExpandedRows)
                                }}
                              >
                                {expandedRows.has(webhook.endpoint) ? (
                                  <KeyboardArrowUpIcon fontSize={isSmall ? 'small' : 'medium'} />
                                ) : (
                                  <KeyboardArrowDownIcon fontSize={isSmall ? 'small' : 'medium'} />
                                )}
                              </IconActionButton>
                            </TableCell>
                          </TableRow>
                          <TableRow>
                            <TableCell
                              colSpan={4}
                              sx={{
                                py: 0,
                                borderBottom: expandedRows.has(webhook.endpoint)
                                  ? undefined
                                  : 'none',
                              }}
                            >
                              <Collapse
                                in={expandedRows.has(webhook.endpoint)}
                                timeout="auto"
                                unmountOnExit
                              >
                                <Box sx={{ py: 2 }}>
                                  <Typography variant="subtitle2" gutterBottom>
                                    {t('webhook.description')}
                                  </Typography>
                                  <Typography
                                    variant="body2"
                                    color="text.secondary"
                                    sx={{
                                      pl: 2,
                                      fontSize: isSmall ? '0.75rem' : '0.875rem',
                                    }}
                                  >
                                    {webhook.description || t('data.noData')}
                                  </Typography>
                                </Box>
                              </Collapse>
                            </TableCell>
                          </TableRow>
                        </React.Fragment>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
              ) : (
                <Alert severity="info" sx={{ mt: 2 }}>
                  {t('webhook.noEndpoints') || 'No webhook endpoints defined for this plugin.'}
                </Alert>
              )}
            </CardContent>
          </Card>
        )}

        {/* 数据管理 */}
        {activeTab === 'data' && (
          <Card sx={CARD_VARIANTS.default.styles}>
            <CardContent>
              {isDataLoading ? (
                <Box sx={{ display: 'flex', justifyContent: 'center', pt: 4 }}>
                  <CircularProgress />
                </Box>
              ) : pluginData.length > 0 ? (
                <TableContainer>
                  <Table size={isSmall ? 'small' : 'medium'}>
                    <TableHead>
                      <TableRow>
                        <TableCell width={isMobile ? 80 : 150}>{t('data.channel')}</TableCell>
                        <TableCell width={isMobile ? 80 : 150}>{t('data.user')}</TableCell>
                        <TableCell>{t('data.storageKey')}</TableCell>
                        <TableCell width={isMobile ? 100 : 132} align="center">
                          {t('data.actions')}
                        </TableCell>
                        <TableCell width={36} padding="none" />
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {pluginData.map(data => (
                        <React.Fragment key={data.id}>
                          <TableRow>
                            <TableCell>
                              <Typography
                                variant="body2"
                                sx={{ fontSize: isSmall ? '0.7rem' : '0.875rem' }}
                              >
                                {data.target_chat_key || t('data.global')}
                              </Typography>
                            </TableCell>
                            <TableCell>
                              <Typography
                                variant="body2"
                                sx={{ fontSize: isSmall ? '0.7rem' : '0.875rem' }}
                              >
                                {data.target_user_id || t('data.global')}
                              </Typography>
                            </TableCell>
                            <TableCell>
                              <Typography
                                variant="body2"
                                sx={{
                                  fontFamily: 'monospace',
                                  fontSize: isSmall ? '0.7rem' : '0.875rem',
                                  overflowWrap: 'break-word',
                                  wordBreak: 'break-all',
                                }}
                              >
                                {data.data_key}
                              </Typography>
                            </TableCell>
                            <TableCell align="right">
                              <Stack
                                direction="row"
                                spacing={0.5}
                                justifyContent="flex-end"
                                flexWrap="wrap"
                              >
                                <ActionButton
                                  size="small"
                                  startIcon={<ContentCopyIcon fontSize="small" />}
                                  onClick={async () => {
                                    const success = await copyText(data.data_value)
                                    if (success) {
                                      notification.success(t('messages.dataCopied'))
                                    } else {
                                      notification.error(t('common.messages.operationFailed') || 'Failed to copy')
                                    }
                                  }}
                                  sx={{
                                    textTransform: 'none',
                                    color: 'primary.main',
                                    '&:hover': {
                                      backgroundColor: 'transparent',
                                      textDecoration: 'underline',
                                    },
                                    fontSize: isSmall ? '0.7rem' : '0.8rem',
                                    px: isSmall ? 0.5 : 1,
                                    minWidth: 'auto',
                                    '& .MuiButton-startIcon': {
                                      mr: isSmall ? 0.3 : 0.5,
                                      '& svg': {
                                        fontSize: isSmall ? '0.9rem' : '1rem',
                                      },
                                    },
                                  }}
                                >
                                  {t('actions.copy')}
                                </ActionButton>
                                <ActionButton
                                  size="small"
                                  tone="danger"
                                  startIcon={<DeleteIcon fontSize="small" />}
                                  onClick={() => {
                                    setDeleteDataId(data.id)
                                    setDeleteDataConfirmOpen(true)
                                  }}
                                  sx={{
                                    textTransform: 'none',
                                    color: 'error.main',
                                    '&:hover': {
                                      backgroundColor: 'transparent',
                                      textDecoration: 'underline',
                                    },
                                    fontSize: isSmall ? '0.7rem' : '0.8rem',
                                    px: isSmall ? 0.5 : 1,
                                    minWidth: 'auto',
                                    '& .MuiButton-startIcon': {
                                      mr: isSmall ? 0.3 : 0.5,
                                      '& svg': {
                                        fontSize: isSmall ? '0.9rem' : '1rem',
                                      },
                                    },
                                  }}
                                >
                                  {t('data.delete')}
                                </ActionButton>
                              </Stack>
                            </TableCell>
                            <TableCell padding="none">
                              <IconActionButton
                                size="small"
                                onClick={() => {
                                  const newExpandedRows = new Set(expandedDataRows)
                                  if (newExpandedRows.has(data.id)) {
                                    newExpandedRows.delete(data.id)
                                  } else {
                                    newExpandedRows.add(data.id)
                                  }
                                  setExpandedDataRows(newExpandedRows)
                                }}
                              >
                                {expandedDataRows.has(data.id) ? (
                                  <KeyboardArrowUpIcon fontSize={isSmall ? 'small' : 'medium'} />
                                ) : (
                                  <KeyboardArrowDownIcon fontSize={isSmall ? 'small' : 'medium'} />
                                )}
                              </IconActionButton>
                            </TableCell>
                          </TableRow>
                          <TableRow>
                            <TableCell
                              colSpan={5}
                              sx={{
                                py: 0,
                                borderBottom: expandedDataRows.has(data.id) ? undefined : 'none',
                              }}
                            >
                              <Collapse
                                in={expandedDataRows.has(data.id)}
                                timeout="auto"
                                unmountOnExit
                              >
                                <Box sx={{ py: 2 }}>
                                  <Typography variant="subtitle2" gutterBottom>
                                    {t('data.value')}
                                  </Typography>
                                  <Typography
                                    variant="body2"
                                    color="text.secondary"
                                    sx={{
                                      pl: 2,
                                      whiteSpace: 'pre-wrap',
                                      wordBreak: 'break-all',
                                      display: '-webkit-box',
                                      WebkitLineClamp: 10,
                                      WebkitBoxOrient: 'vertical',
                                      overflow: 'hidden',
                                      fontSize: isSmall ? '0.75rem' : '0.875rem',
                                    }}
                                  >
                                    {data.data_value}
                                  </Typography>
                                </Box>
                              </Collapse>
                            </TableCell>
                          </TableRow>
                        </React.Fragment>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
              ) : (
                <Alert severity="info" sx={{ mt: 2 }}>
                  {t('data.noData')}
                </Alert>
              )}
            </CardContent>
          </Card>
        )}
      </Box>

      {/* 重置数据确认对话框 */}
      <Dialog open={resetDataConfirmOpen} onClose={() => setResetDataConfirmOpen(false)}>
        <DialogTitle>{t('dialogs.resetTitle')}</DialogTitle>
        <DialogContent>
          <DialogContentText>{t('dialogs.resetMessage', { name: plugin.name })}</DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <ActionButton
            tone="secondary"
            onClick={() => setResetDataConfirmOpen(false)}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone="danger"
            onClick={() => {
              resetDataMutation.mutate()
              setResetDataConfirmOpen(false)
            }}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('actions.confirm')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 重载确认对话框 */}
      <Dialog open={reloadConfirmOpen} onClose={() => setReloadConfirmOpen(false)}>
        <DialogTitle>{t('dialogs.reloadTitle')}</DialogTitle>
        <DialogContent>
          <DialogContentText>{t('dialogs.reloadMessage', { name: plugin.name })}</DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <ActionButton
            tone="secondary"
            onClick={() => setReloadConfirmOpen(false)}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone="primary"
            onClick={() => {
              reloadMutation.mutate()
              setReloadConfirmOpen(false)
            }}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('actions.confirm')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 删除云端插件确认对话框 */}
      <Dialog open={deleteConfirmOpen} onClose={() => setDeleteConfirmOpen(false)}>
        <DialogTitle>{t('dialogs.deleteTitle')}</DialogTitle>
        <DialogContent>
          <DialogContentText>{t('dialogs.deleteMessage', { name: plugin.name })}</DialogContentText>
          <FormControlLabel
            control={
              <Switch
                checked={clearDataOnDelete}
                onChange={e => setClearDataOnDelete(e.target.checked)}
                color="error"
              />
            }
            label={t('dialogs.deleteDataOption')}
            sx={{ mt: 2, mb: 1 }}
          />
          {clearDataOnDelete && (
            <Alert severity="warning" sx={{ mt: 1 }}>
              {t('dialogs.deleteDataWarning')}
            </Alert>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <ActionButton
            tone="secondary"
            onClick={() => {
              setDeleteConfirmOpen(false)
              setClearDataOnDelete(false) // 重置勾选状态
            }}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone="danger"
            onClick={() => {
              removePackageMutation.mutate()
              setDeleteConfirmOpen(false)
              setClearDataOnDelete(false) // 重置勾选状态
            }}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('actions.confirm')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 更新云端插件确认对话框 */}
      <Dialog open={updateConfirmOpen} onClose={() => setUpdateConfirmOpen(false)} maxWidth="md">
        <DialogTitle>{t('dialogs.updateTitle')}</DialogTitle>
        <DialogContent>
          <Alert severity="warning" sx={{ mb: 2 }}>
            <Typography variant="body2" component="div">
              {t('dialogs.updateSecurityWarning')}
            </Typography>
          </Alert>

          <DialogContentText sx={{ mt: 2 }}>
            {t('dialogs.updateConfirmMessage', { name: plugin.name })}
          </DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <ActionButton
            tone="secondary"
            onClick={() => setUpdateConfirmOpen(false)}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone="primary"
            onClick={() => {
              updatePackageMutation.mutate()
              setUpdateConfirmOpen(false)
            }}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('actions.confirm')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 删除数据确认对话框 */}
      <Dialog open={deleteDataConfirmOpen} onClose={() => setDeleteDataConfirmOpen(false)}>
        <DialogTitle>{t('dialogs.deleteDataTitle')}</DialogTitle>
        <DialogContent>
          <DialogContentText>{t('dialogs.deleteDataMessage')}</DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <ActionButton
            tone="secondary"
            onClick={() => setDeleteDataConfirmOpen(false)}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone="danger"
            onClick={() => {
              if (deleteDataId !== null) {
                deleteDataMutation.mutate(deleteDataId)
                setDeleteDataConfirmOpen(false)
                setDeleteDataId(null)
              }
            }}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('data.confirm')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 错误详情对话框 */}
      <Dialog open={errorDetailOpen} onClose={() => setErrorDetailOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle>{t('status.loadFailed')}</DialogTitle>
        <DialogContent>
          <Box sx={{ mt: 2 }}>
            <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1 }}>
              {t('info.errorType')}
            </Typography>
            <Typography
              variant="body2"
              sx={{ fontFamily: 'monospace', mb: 2, p: 1.5, bgcolor: 'grey.100', borderRadius: 1 }}
            >
              {plugin.errorType}
            </Typography>

            <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1 }}>
              {t('info.filePath')}
            </Typography>
            <Typography
              variant="body2"
              sx={{ fontFamily: 'monospace', mb: 2, p: 1.5, bgcolor: 'grey.100', borderRadius: 1 }}
            >
              {plugin.filePath}
            </Typography>

            <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1 }}>
              {t('info.stackTrace')}
            </Typography>
            <Box
              sx={{
                fontFamily: 'monospace',
                fontSize: '0.75rem',
                p: 1.5,
                bgcolor: 'grey.900',
                color: 'grey.100',
                borderRadius: 1,
                overflow: 'auto',
                maxHeight: '400px',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {plugin.stackTrace}
            </Box>
          </Box>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <ActionButton
            onClick={() => setErrorDetailOpen(false)}
            sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
          >
            {t('actions.cancel')}
          </ActionButton>
        </DialogActions>
      </Dialog>
    </Box>
  )
}

export default function PluginsManagementPage() {
  const queryClient = useQueryClient()
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('md'))
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  const navigate = useNavigate()
  const [drawerOpen, setDrawerOpen] = useState(false)
  const { pluginId } = useParams<{ pluginId: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const notification = useNotification()
  const { t, i18n } = useTranslation('plugins')
  const searchTerm = searchParams.get('search') ?? ''
  const [localSearchTerm, setLocalSearchTerm] = useState(searchTerm)
  const [isComposing, setIsComposing] = useState(false)
  const buildPluginsUrl = useCallback((nextPluginId?: string | null) => {
    const basePath = pluginsManagementPath(nextPluginId)
    const query = searchParams.toString()
    return query ? `${basePath}?${query}` : basePath
  }, [searchParams])

  // 同步 URL 搜索参数到本地状态
  useEffect(() => {
    setLocalSearchTerm(searchTerm)
  }, [searchTerm])

  const { currentInstanceKey, setCurrentInstance } = useInstanceScope()

  // 获取插件列表 - 只获取基础列表，不获取详情
  const { data: plugins = [], isLoading } = useQuery({
    queryKey: ['plugins'],
    queryFn: () => pluginsApi.getPlugins(),
  })

  // 实例作用域下的各插件生效状态与来源层级
  const { data: pluginScope, error: pluginScopeError } = useQuery({
    queryKey: ['plugin-scope', currentInstanceKey],
    queryFn: () => instanceScopeApi.getPluginScope(currentInstanceKey as string),
    enabled: !!currentInstanceKey,
    retry: false,
  })

  const scopeUnavailable = !!currentInstanceKey && isScopeEndpointUnavailable(pluginScopeError)

  const scopeStatusMap = useMemo(() => {
    const map = new Map<string, PluginScopeStatus>()
    pluginScope?.items.forEach(item => map.set(item.plugin_key, item))
    return map
  }, [pluginScope])

  const getScopeStatus = useCallback(
    (id: string) => (currentInstanceKey ? scopeStatusMap.get(id) ?? null : null),
    [currentInstanceKey, scopeStatusMap]
  )

  const isPluginEffectivelyEnabled = useCallback(
    (plugin: Plugin) => getScopeStatus(plugin.id)?.enabled ?? plugin.enabled,
    [getScopeStatus]
  )

  const selectedPluginId = useMemo(() => {
    if (!pluginId) return null

    const target = plugins.find(
      plugin =>
        plugin.id === pluginId ||
        plugin.id.endsWith(`.${pluginId}`) ||
        plugin.moduleName === pluginId,
    )
    return target?.id ?? pluginId
  }, [pluginId, plugins])

  useEffect(() => {
    if (!pluginId || plugins.length === 0 || !selectedPluginId) return
    if (pluginId === selectedPluginId) return

    navigate(buildPluginsUrl(selectedPluginId), { replace: true })
  }, [buildPluginsUrl, navigate, pluginId, plugins.length, selectedPluginId])

  // 获取当前选中插件的详情
  const {
    data: pluginDetail,
    isFetching: isPluginDetailFetching,
    isLoading: isPluginDetailLoading,
  } = useQuery({
    queryKey: ['plugin-detail', selectedPluginId],
    queryFn: () => pluginsApi.getPluginDetail(selectedPluginId as string),
    enabled: !!selectedPluginId,
  })
  const selectedPluginSummary =
    plugins.find(plugin => plugin.id === selectedPluginId) ?? null
  const selectedPlugin = pluginDetail ?? selectedPluginSummary
  const activationStrategyLoading = !!selectedPluginId && (isPluginDetailLoading || isPluginDetailFetching)

  // 切换插件启用状态：全局视图改全局开关，实例作用域写实例级覆盖
  const toggleEnabledMutation = useMutation({
    mutationFn: async ({ id, enabled }: { id: string; enabled: boolean | null }) => {
      if (currentInstanceKey) {
        await instanceScopeApi.setPluginScope(currentInstanceKey, id, enabled)
        return
      }
      await pluginsApi.togglePluginEnabled(id, enabled ?? false)
    },
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['plugins'] })
      queryClient.invalidateQueries({ queryKey: ['plugin-detail'] })
      queryClient.invalidateQueries({ queryKey: ['plugin-scope'] })
      if (variables.enabled === null) {
        notification.success(t('override.restoreSuccess', { ns: 'instance-scope' }))
        return
      }
      notification.success(t(variables.enabled ? 'status.enabled' : 'status.disabled'))
    },
    onError: (error: Error) => {
      notification.error(t('messages.updateFailedWithMessage', { message: error.message }))
    },
  })

  const handleToggleEnabled = (id: string, enabled: boolean) => {
    toggleEnabledMutation.mutate({ id, enabled })
  }

  const handleRestoreInherit = (id: string) => {
    toggleEnabledMutation.mutate({ id, enabled: null })
  }

  // 处理选择插件的逻辑
  const handleSelectPlugin = (plugin: Plugin) => {
    navigate(buildPluginsUrl(plugin.id))
    if (isMobile) {
      setDrawerOpen(false)
    }
  }

  const handleOpenPlugin = (pluginId: string) => {
    const target = plugins.find(
      plugin =>
        plugin.id === pluginId ||
        plugin.id.endsWith(`.${pluginId}`) ||
        plugin.moduleName === pluginId,
    )
    if (!target) {
      notification.error(t('messages.pluginNotFound'))
      return
    }
    navigate(buildPluginsUrl(target.id))
  }

  // 获取插件类型
  const getPluginType = (plugin: Plugin) => {
    if (plugin.isBuiltin) return 'builtin'
    if (plugin.isPackage) return 'package'
    return 'local'
  }

  // 过滤插件列表
  const filteredPlugins = plugins
    .filter(
      plugin => {
        const name = (getPluginName(plugin, i18n.language) || '').toLowerCase()
        const description = (getPluginDescription(plugin, i18n.language) || '').toLowerCase()
        const pluginId = (plugin.id || '').toLowerCase()
        const moduleName = (plugin.moduleName || '').toLowerCase()
        const search = searchTerm.toLowerCase()
        return (
          name.includes(search) ||
          description.includes(search) ||
          pluginId.includes(search) ||
          moduleName.includes(search)
        )
      }
    )
    .sort((a, b) => {
      // 基础交互插件(模块名为"basic")固定放在最前面
      if (a.moduleName === 'basic') return -1
      if (b.moduleName === 'basic') return 1

      // 失败的插件排在最后
      if (a.loadFailed !== b.loadFailed) {
        return a.loadFailed ? 1 : -1
      }

      // 优先按启用状态排序（启用的在前），实例作用域下按分层叠加后的生效值
      const aEnabled = isPluginEffectivelyEnabled(a)
      const bEnabled = isPluginEffectivelyEnabled(b)
      if (aEnabled !== bEnabled) {
        return aEnabled ? -1 : 1
      }

      // 按照插件类型排序：内置 -> 云端 -> 本地
      const getTypeOrder = (plugin: Plugin) => {
        if (plugin.isBuiltin) return 0
        if (plugin.isPackage) return 1
        return 2 // 本地插件
      }

      const typeOrderA = getTypeOrder(a)
      const typeOrderB = getTypeOrder(b)

      if (typeOrderA !== typeOrderB) {
        return typeOrderA - typeOrderB
      }

      // 最后按名称字母顺序排序
      return getPluginName(a, i18n.language).localeCompare(getPluginName(b, i18n.language))
    })

  const updateSearchTerm = (value: string) => {
    const nextParams = new URLSearchParams(searchParams)
    if (value) {
      nextParams.set('search', value)
    } else {
      nextParams.delete('search')
    }
    setSearchParams(nextParams, { replace: true })
  }

  const handleSearchChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value
    setLocalSearchTerm(value)
    if (!isComposing) {
      updateSearchTerm(value)
    }
  }

  const handleCompositionStart = () => {
    setIsComposing(true)
  }

  const handleCompositionEnd = (e: React.CompositionEvent<HTMLInputElement>) => {
    setIsComposing(false)
    updateSearchTerm(e.currentTarget.value)
  }

  const handleClearSearch = () => {
    setLocalSearchTerm('')
    updateSearchTerm('')
  }

  const pluginListContent = (
    <>
      <Box sx={{ p: 1.5, borderBottom: 1, borderColor: 'divider', flexShrink: 0, bgcolor: 'background.paper' }}>
        <Stack spacing={1.5}>
          <InstanceScopeBanner
            description={
              scopeUnavailable
                ? t('unavailable.plugins', { ns: 'instance-scope' })
                : t('banner.plugins', { ns: 'instance-scope' })
            }
            severity={scopeUnavailable ? 'warning' : 'info'}
            action={
              scopeUnavailable ? (
                <ActionButton tone="ghost" size="small" onClick={() => setCurrentInstance(null)}>
                  {t('unavailable.backToGlobal', { ns: 'instance-scope' })}
                </ActionButton>
              ) : undefined
            }
          />
        <TextField
          placeholder={t('list.search')}
          size="small"
          value={localSearchTerm}
          onChange={handleSearchChange}
          onCompositionStart={handleCompositionStart}
          onCompositionEnd={handleCompositionEnd}
          variant="outlined"
          fullWidth
          slotProps={{
            input: {
              endAdornment: localSearchTerm ? (
                <InputAdornment position="end">
                  <IconActionButton
                    size="small"
                    edge="end"
                    aria-label={t('actions.clear')}
                    onClick={handleClearSearch}
                  >
                    <CloseIcon fontSize="small" />
                  </IconActionButton>
                </InputAdornment>
              ) : undefined,
            },
          }}
          sx={{
            '& .MuiOutlinedInput-root': {
              borderRadius: 2,
            },
          }}
        />
        </Stack>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', bgcolor: 'background.paper' }}>
        {isLoading ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}>
            <CircularProgress />
          </Box>
        ) : filteredPlugins.length > 0 ? (
          <List sx={{ flex: 1, padding: 0 }}>
            {filteredPlugins.map(plugin => (
              <React.Fragment key={plugin.id}>
                <ListItemButton
                  onClick={() => handleSelectPlugin(plugin)}
                  selected={selectedPluginId === plugin.id}
                  sx={{
                    py: 1.5,
                    px: 2,
                    '&.Mui-selected': {
                      backgroundColor: theme.palette.action.selected,
                      '&:hover': {
                        backgroundColor: theme.palette.action.hover,
                      },
                    },
                  }}
                >
                  <Box
                    sx={{
                      width: 8,
                      height: 8,
                      borderRadius: '50%',
                      bgcolor: plugin.loadFailed
                        ? 'error.main'
                        : isPluginEffectivelyEnabled(plugin)
                        ? 'success.main'
                        : 'grey.400',
                      mr: 1.5,
                      flexShrink: 0,
                    }}
                  />
                  <ListItemText
                    primary={
                      <Box
                        sx={{
                          display: 'flex',
                          alignItems: 'center',
                          flexWrap: 'wrap',
                          gap: 0.5,
                          mb: 0.5,
                        }}
                      >
                        <Chip
                          label={t(`types.${getPluginType(plugin)}`)}
                          size="small"
                          color={pluginTypeColors[getPluginType(plugin)]}
                          sx={CHIP_VARIANTS.base(isSmall)}
                        />
                        <Typography
                          variant="body1"
                          sx={{
                            fontWeight: 'bold',
                            fontSize: '0.9rem',
                            ml: 0.5,
                          }}
                        >
                          {getPluginName(plugin, i18n.language)}
                        </Typography>
                        {plugin.hasConfig && (
                          <Tooltip title={t('list.hasConfig')}>
                            <SettingsIcon
                              fontSize="small"
                              sx={{ ml: 0.5, opacity: 0.6, fontSize: 16 }}
                            />
                          </Tooltip>
                        )}
                        {/* 实例作用域：一眼看出这个插件是全局启用还是被本实例覆盖 */}
                        {getScopeStatus(plugin.id) && (
                          <ScopeOverrideControl
                            compact
                            source={getScopeStatus(plugin.id)?.source ?? 'global'}
                            overridden={getScopeStatus(plugin.id)?.instance_override !== null}
                            effectiveLabel={null}
                          />
                        )}
                      </Box>
                    }
                    secondary={
                      <Typography
                        variant="body2"
                        sx={{
                          textOverflow: 'ellipsis',
                          overflow: 'hidden',
                          whiteSpace: 'nowrap',
                          maxWidth: 240,
                          fontSize: isSmall ? '0.75rem' : 'inherit',
                          color: 'text.secondary',
                        }}
                      >
                        {getPluginDescription(plugin, i18n.language)}
                      </Typography>
                    }
                  />
                </ListItemButton>
                <Divider />
              </React.Fragment>
            ))}
          </List>
        ) : (
          <Box
            sx={{
              display: 'flex',
              justifyContent: 'center',
              alignItems: 'center',
              height: '100%',
              p: 2,
              textAlign: 'center',
            }}
          >
            <Typography variant="body2" color="text.secondary">
              {t('list.noMatch')}
            </Typography>
          </Box>
        )}
      </Box>
    </>
  )

  return (
    <Box
      sx={{
        display: 'flex',
        height: '100%',
        minHeight: 0,
        gap: 1,
        p: { xs: 1, sm: 2 },
        boxSizing: 'border-box',
        overflow: 'hidden',
      }}
    >
      {isMobile ? (
        <>
          <Drawer
            anchor="left"
            open={drawerOpen}
            onClose={() => setDrawerOpen(false)}
            PaperProps={{
              sx: {
                width: isSmall ? 'min(88vw, 360px)' : 360,
                maxWidth: '100vw',
                backgroundColor: 'background.paper',
                backgroundImage: 'none',
                borderRight: `1px solid ${theme.palette.divider}`,
                display: 'flex',
                flexDirection: 'column',
                overflow: 'hidden',
                boxShadow: theme.shadows[12],
              },
            }}
          >
            {pluginListContent}
          </Drawer>

          {/* 移动端主内容区 */}
          <Box sx={{ flex: 1, minWidth: 0, minHeight: 0, overflow: 'hidden' }}>
            {selectedPlugin ? (
              <PluginDetails
                plugin={selectedPlugin}
                activationStrategyLoading={activationStrategyLoading}
                onBack={() => navigate(buildPluginsUrl())}
                onToggleEnabled={handleToggleEnabled}
                onOpenPlugin={handleOpenPlugin}
                scopeInstanceKey={currentInstanceKey}
                scopeStatus={getScopeStatus(selectedPlugin.id)}
                scopeUnavailable={scopeUnavailable}
                onRestoreInherit={handleRestoreInherit}
              />
            ) : (
              <Card
                sx={{
                  ...CARD_VARIANTS.default.styles,
                  display: 'flex',
                  flexDirection: 'column',
                  justifyContent: 'center',
                  alignItems: 'center',
                  height: '100%',
                  p: 3,
                  textAlign: 'center',
                }}
              >
                <ExtensionIcon sx={{ fontSize: 60, mb: 2, opacity: 0.7 }} />
                <Typography variant="h6" gutterBottom>
                  {t('welcome.title')}
                </Typography>
                <Typography variant="body2" color="text.secondary" paragraph>
                  {t('welcome.hintMobile')}
                </Typography>
              </Card>
            )}
          </Box>
        </>
      ) : (
        // 桌面端布局
        <>
          {/* 左侧插件列表 */}
          <Card
            sx={{
              ...CARD_VARIANTS.default.styles,
              width: 320,
              flexShrink: 0,
              display: 'flex',
              flexDirection: 'column',
              overflow: 'hidden',
            }}
          >
            {pluginListContent}
          </Card>

          {/* 右侧插件详情 */}
          <Box sx={{ flex: 1, minWidth: 0, minHeight: 0, overflow: 'hidden' }}>
            {selectedPlugin ? (
              <PluginDetails
                plugin={selectedPlugin}
                activationStrategyLoading={activationStrategyLoading}
                onBack={() => navigate(buildPluginsUrl())}
                onToggleEnabled={handleToggleEnabled}
                onOpenPlugin={handleOpenPlugin}
                scopeInstanceKey={currentInstanceKey}
                scopeStatus={getScopeStatus(selectedPlugin.id)}
                scopeUnavailable={scopeUnavailable}
                onRestoreInherit={handleRestoreInherit}
              />
            ) : (
              <Card
                sx={{
                  ...CARD_VARIANTS.default.styles,
                  display: 'flex',
                  flexDirection: 'column',
                  justifyContent: 'center',
                  alignItems: 'center',
                  height: '100%',
                  p: 3,
                  textAlign: 'center',
                }}
              >
                <ExtensionIcon sx={{ fontSize: 60, mb: 2, opacity: 0.7 }} />
                <Typography variant="h6" gutterBottom>
                  {t('welcome.title')}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  {t('welcome.hintDesktop')}
                </Typography>
              </Card>
            )}
          </Box>
        </>
      )}

      {/* 移动端展示插件列表的Fab按钮 - 始终可见 */}
      {isMobile && !drawerOpen && (
        <Fab
          color="primary"
          size={isSmall ? 'medium' : 'large'}
          onClick={() => setDrawerOpen(true)}
          sx={{
            position: 'fixed',
            bottom: 16,
            right: 16,
            zIndex: theme.zIndex.drawer - 1,
            boxShadow: 3,
          }}
        >
          <ExtensionIcon />
        </Fab>
      )}
    </Box>
  )
}
