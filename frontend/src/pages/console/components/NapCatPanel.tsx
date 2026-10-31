import { useEffect, useState } from 'react'
import {
  Alert,
  Box,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  FormControlLabel,
  Grid,
  InputAdornment,
  MenuItem,
  Stack,
  Switch,
  TextField,
  Typography,
  alpha,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import AddCircleOutlineIcon from '@mui/icons-material/AddCircleOutline'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import QrCode2Icon from '@mui/icons-material/QrCode2'
import RestartAltIcon from '@mui/icons-material/RestartAlt'
import StopIcon from '@mui/icons-material/Stop'
import ActionButton from '../../../components/common/ActionButton'
import IconActionButton from '../../../components/common/IconActionButton'
import NekroDialog from '../../../components/common/NekroDialog'
import { useNotification } from '../../../hooks/useNotification'
import { copyText } from '../../../utils/clipboard'
import { BORDER_RADIUS, CARD_VARIANTS, CHIP_VARIANTS } from '../../../theme/variants'
import { consoleApi, type ContainerState } from '../../../services/api/console'
import {
  CLIENT_URL_SCHEMES,
  CONTAINER_PRESENT_STATES,
  NETWORK_ENTRY_TYPES,
  consoleContainerApi,
  createNetworkEntry,
  isServerEntryType,
  type NetworkConfig,
  type NetworkEntry,
} from '../../../services/api/consoleContainer'
import { useIdentity } from '../../../hooks/useIdentity'
import QrCodeLoginDialog from './QrCodeLoginDialog'

interface NapCatPanelProps {
  instanceKey: string
}

/** 容器状态轮询间隔，与既有 NapCat 页保持一致 */
const CONTAINER_POLL_INTERVAL_MS = 5000

const MIN_PORT = 1
const MAX_PORT = 65535

const EMPTY_NETWORK_CONFIG: NetworkConfig = {
  entries: [],
  music_sign_url: '',
  enable_local_file_to_url: false,
  parse_multi_msg: false,
}

/** 心跳/重连间隔输入框只接受非负整数毫秒，非法输入回退到当前值而不是写入 NaN */
const parseIntervalInput = (raw: string, fallback: number): number => {
  const parsed = Number.parseInt(raw, 10)
  if (Number.isNaN(parsed) || parsed < 0) return fallback
  return parsed
}

/** 新连接的默认名称，避开已占用的名字，省得用户一加就撞名 */
const nextEntryName = (entries: NetworkEntry[]): string => {
  const used = new Set(entries.map(entry => entry.name.trim()))
  for (let index = 1; ; index += 1) {
    const candidate = `connection_${index}`
    if (!used.has(candidate)) return candidate
  }
}

interface NetworkEntryCardProps {
  entry: NetworkEntry
  index: number
  /** 实例是否托管容器；远程协议端只能查看，配置得在对方那边改 */
  editable: boolean
  onChange: (index: number, patch: Partial<NetworkEntry>) => void
  onRemove: (index: number) => void
  onCopy: (value: string) => void
}

/**
 * 单条协议端网络连接的编辑卡片
 *
 * NekroAgent 托管的那条只读：地址与令牌由后端生成，改了机器人就连不上，因此这里只
 * 展示并允许复制，不提供删除入口。
 */
function NetworkEntryCard({
  entry,
  index,
  editable,
  onChange,
  onRemove,
  onCopy,
}: NetworkEntryCardProps) {
  const { t } = useTranslation('console-setup')
  const theme = useTheme()
  const managed = entry.managed_by_nekro
  const locked = managed || !editable
  const isServer = isServerEntryType(entry.type)
  const isWebsocket = entry.type === 'websocketClients' || entry.type === 'websocketServers'

  const copyAdornment = (value: string) => (
    <InputAdornment position="end">
      <IconActionButton size="small" aria-label={t('common.copy')} onClick={() => onCopy(value)}>
        <ContentCopyIcon fontSize="small" />
      </IconActionButton>
    </InputAdornment>
  )

  return (
    <Box
      sx={{
        borderRadius: BORDER_RADIUS.DEFAULT,
        border: `1px solid ${alpha(theme.palette.divider, 0.8)}`,
        backgroundColor: managed ? alpha(theme.palette.primary.main, 0.04) : 'transparent',
        p: 2,
      }}
    >
      <Stack spacing={2}>
        <Stack
          direction={{ xs: 'column', sm: 'row' }}
          spacing={1}
          alignItems={{ xs: 'flex-start', sm: 'center' }}
          justifyContent="space-between"
        >
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <Typography variant="subtitle2" fontWeight={600}>
              {entry.name || t('panel.network.entry.unnamed')}
            </Typography>
            {managed ? (
              <Chip size="small" color="primary" label={t('panel.network.entry.managedBadge')} />
            ) : null}
          </Stack>
          <Stack direction="row" spacing={1} alignItems="center">
            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={entry.enable}
                  disabled={locked}
                  onChange={event => onChange(index, { enable: event.target.checked })}
                />
              }
              label={t('panel.network.entry.enable')}
            />
            {managed || !editable ? null : (
              <IconActionButton
                size="small"
                aria-label={t('panel.network.entry.remove')}
                onClick={() => onRemove(index)}
              >
                <DeleteOutlineIcon fontSize="small" />
              </IconActionButton>
            )}
          </Stack>
        </Stack>

        {managed ? (
          <Alert severity="info" variant="outlined">
            {t('panel.network.entry.managedHint')}
          </Alert>
        ) : null}

        <Grid container spacing={2}>
          <Grid item xs={12} md={6}>
            <TextField
              select
              fullWidth
              size="small"
              label={t('panel.network.entry.type')}
              value={entry.type}
              disabled={locked}
              helperText={t(`panel.network.type.${entry.type}Hint`)}
              onChange={event =>
                onChange(index, { type: event.target.value as NetworkEntry['type'] })
              }
            >
              {NETWORK_ENTRY_TYPES.map(type => (
                <MenuItem key={type} value={type}>
                  {t(`panel.network.type.${type}`)}
                </MenuItem>
              ))}
            </TextField>
          </Grid>
          <Grid item xs={12} md={6}>
            <TextField
              fullWidth
              size="small"
              label={t('panel.network.entry.name')}
              helperText={t('panel.network.entry.nameHint')}
              value={entry.name}
              disabled={locked}
              onChange={event => onChange(index, { name: event.target.value })}
            />
          </Grid>

          {isServer ? (
            <>
              <Grid item xs={12} md={6}>
                <TextField
                  fullWidth
                  size="small"
                  label={t('panel.network.entry.host')}
                  helperText={t('panel.network.entry.hostHint')}
                  value={entry.host}
                  disabled={locked}
                  onChange={event => onChange(index, { host: event.target.value })}
                />
              </Grid>
              <Grid item xs={12} md={6}>
                <TextField
                  fullWidth
                  size="small"
                  type="number"
                  label={t('panel.network.entry.port')}
                  helperText={t('panel.network.entry.portHint', { min: MIN_PORT, max: MAX_PORT })}
                  value={entry.port}
                  disabled={locked}
                  onChange={event =>
                    onChange(index, { port: parseIntervalInput(event.target.value, entry.port) })
                  }
                />
              </Grid>
            </>
          ) : (
            <Grid item xs={12}>
              <TextField
                fullWidth
                size="small"
                label={t('panel.network.entry.url')}
                helperText={t('panel.network.entry.urlHint', {
                  schemes: (CLIENT_URL_SCHEMES[entry.type] ?? []).join(' / '),
                })}
                value={entry.url}
                disabled={locked}
                onChange={event => onChange(index, { url: event.target.value })}
                InputProps={{ endAdornment: copyAdornment(entry.url) }}
              />
            </Grid>
          )}

          <Grid item xs={12} md={6}>
            <TextField
              fullWidth
              size="small"
              label={t('panel.network.entry.token')}
              helperText={t('panel.network.entry.tokenHint')}
              value={entry.token}
              disabled={locked}
              onChange={event => onChange(index, { token: event.target.value })}
              InputProps={{ endAdornment: copyAdornment(entry.token) }}
            />
          </Grid>
          <Grid item xs={12} md={6}>
            <TextField
              select
              fullWidth
              size="small"
              label={t('panel.network.entry.messagePostFormat')}
              value={entry.message_post_format}
              disabled={locked}
              onChange={event => onChange(index, { message_post_format: event.target.value })}
            >
              <MenuItem value="array">array</MenuItem>
              <MenuItem value="string">string</MenuItem>
            </TextField>
          </Grid>

          {isWebsocket ? (
            <Grid item xs={12} md={6}>
              <TextField
                fullWidth
                size="small"
                type="number"
                label={t('panel.network.entry.heartInterval')}
                value={entry.heart_interval_ms}
                disabled={!editable}
                onChange={event =>
                  onChange(index, {
                    heart_interval_ms: parseIntervalInput(
                      event.target.value,
                      entry.heart_interval_ms
                    ),
                  })
                }
              />
            </Grid>
          ) : null}
          {entry.type === 'websocketClients' ? (
            <Grid item xs={12} md={6}>
              <TextField
                fullWidth
                size="small"
                type="number"
                label={t('panel.network.entry.reconnectInterval')}
                value={entry.reconnect_interval_ms}
                disabled={!editable}
                onChange={event =>
                  onChange(index, {
                    reconnect_interval_ms: parseIntervalInput(
                      event.target.value,
                      entry.reconnect_interval_ms
                    ),
                  })
                }
              />
            </Grid>
          ) : null}
        </Grid>

        <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
          <FormControlLabel
            control={
              <Switch
                size="small"
                checked={entry.report_self_message}
                disabled={locked}
                onChange={event => onChange(index, { report_self_message: event.target.checked })}
              />
            }
            label={t('panel.network.entry.reportSelfMessage')}
          />
          {entry.type === 'websocketServers' ? (
            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={entry.enable_force_push_event}
                  disabled={locked}
                  onChange={event =>
                    onChange(index, { enable_force_push_event: event.target.checked })
                  }
                />
              }
              label={t('panel.network.entry.forcePushEvent')}
            />
          ) : null}
          <FormControlLabel
            control={
              <Switch
                size="small"
                checked={entry.debug}
                disabled={!editable}
                onChange={event => onChange(index, { debug: event.target.checked })}
              />
            }
            label={t('panel.network.entry.debug')}
          />
        </Stack>
      </Stack>
    </Box>
  )
}

export default function NapCatPanel({ instanceKey }: NapCatPanelProps) {
  // 两档，与后端 `routers/console/containers.py` 的划分一一对应：
  //
  // - `canManageContainers`（平台方）：建 / 删容器、起 / 停、以及网络配置卡片。前两类是宿主级
  //   资源动作，网络配置的响应里还带着托管连接的 OneBot 访问令牌，整块对租户不渲染。
  // - `canOperateProtocol`（平台方或租户管理员）：重启 / 扫码登录。R19 明写过租户
  //   「最多能重启我的 NapCat」，掉线自愈要的就是这两个。
  //
  // 隐藏入口只是避免点了才发现无权，真正的边界在服务端（归属校验落在实例与容器两维上）。
  const { canManageContainers, canOperateProtocol } = useIdentity()
  const { t } = useTranslation(['console-setup', 'console'])
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()

  const [qrDialogOpen, setQrDialogOpen] = useState(false)
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [form, setForm] = useState<NetworkConfig>(EMPTY_NETWORK_CONFIG)
  const [formDirty, setFormDirty] = useState(false)

  const containerQueryKey = ['console-container', instanceKey]
  const networkQueryKey = ['console-network-config', instanceKey]

  // 与实例详情页共用缓存键，面板嵌在详情页里时不会产生额外请求
  const { data: instance, isLoading: instanceLoading } = useQuery({
    queryKey: ['console-instance', instanceKey],
    queryFn: () => consoleApi.getInstance(instanceKey),
    enabled: Boolean(instanceKey),
  })

  const provider = instance?.provider
  const hostsContainer = provider === 'builtin_napcat'
  const usesOneBotNetwork = provider === 'builtin_napcat' || provider === 'remote_onebot'

  const {
    data: container,
    isLoading: containerLoading,
    error: containerError,
  } = useQuery({
    queryKey: containerQueryKey,
    queryFn: () => consoleContainerApi.getContainer(instanceKey),
    refetchInterval: CONTAINER_POLL_INTERVAL_MS,
    enabled: Boolean(instanceKey) && hostsContainer,
  })

  // 网络配置的响应里带着托管连接的 OneBot 访问令牌，后端是 `Role.Super`。
  // 租户身份下不发这个请求，避免一个注定 403 的往返（判据与下面那张卡片的渲染条件同一个）
  const { data: networkConfig } = useQuery({
    queryKey: networkQueryKey,
    queryFn: () => consoleContainerApi.getNetworkConfig(instanceKey),
    enabled: Boolean(instanceKey) && usesOneBotNetwork && canManageContainers,
  })

  useEffect(() => {
    if (networkConfig && !formDirty) {
      setForm(networkConfig)
    }
  }, [networkConfig, formDirty])

  const refreshContainer = () => {
    void queryClient.invalidateQueries({ queryKey: containerQueryKey })
  }

  /**
   * 扫码 / 快速登录成功后要刷新的**不只是容器**。
   *
   * 后端此刻已经把登录到的账号回写进了实例行（`bind_login_account`，状态一并从
   * `unbound` 落回 `offline`），而「绑定账号」与状态标都读自 `['console-instance', key]`
   * 这个查询。只失效容器查询的话，弹窗里报着"账号 xxx 已绑定到本实例"，身后的详情页
   * 却还写着"未绑定 / —" —— 正是本轮要消灭的那条误导，换了个地方重演一遍。
   */
  const refreshAfterLogin = () => {
    refreshContainer()
    void queryClient.invalidateQueries({ queryKey: ['console-instance', instanceKey] })
    void queryClient.invalidateQueries({ queryKey: ['console-instances'] })
  }

  const createMutation = useMutation({
    mutationFn: () => consoleContainerApi.createContainer(instanceKey),
    onSuccess: result => {
      notification.success(result.message || t('panel.createStarted'))
      refreshContainer()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const startMutation = useMutation({
    mutationFn: () => consoleContainerApi.startContainer(instanceKey),
    onSuccess: result => {
      notification.success(result.message || t('panel.started'))
      refreshContainer()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const stopMutation = useMutation({
    mutationFn: () => consoleContainerApi.stopContainer(instanceKey),
    onSuccess: result => {
      notification.success(result.message || t('panel.stopped'))
      refreshContainer()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const restartMutation = useMutation({
    mutationFn: () => consoleContainerApi.restartContainer(instanceKey),
    onSuccess: result => {
      notification.success(result.message || t('panel.restarted'))
      refreshContainer()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const deleteMutation = useMutation({
    mutationFn: () => consoleContainerApi.deleteContainer(instanceKey),
    onSuccess: result => {
      notification.success(result.message || t('panel.deleted'))
      setDeleteDialogOpen(false)
      refreshContainer()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const saveNetworkMutation = useMutation({
    mutationFn: () => consoleContainerApi.updateNetworkConfig(instanceKey, form),
    onSuccess: result => {
      notification.success(result.message || t('panel.network.saved'))
      notification.info(
        hostsContainer ? t('panel.network.restartRequired') : t('panel.network.remoteSyncRequired')
      )
      setFormDirty(false)
      void queryClient.invalidateQueries({ queryKey: networkQueryKey })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const state: ContainerState = container?.state ?? 'absent'
  const containerPresent = CONTAINER_PRESENT_STATES.includes(state)
  const isRunning = state === 'running'
  const busy =
    createMutation.isPending ||
    startMutation.isPending ||
    stopMutation.isPending ||
    restartMutation.isPending ||
    deleteMutation.isPending

  const stateColor = (() => {
    switch (state) {
      case 'running':
        return theme.palette.success.main
      case 'restarting':
      case 'created':
        return theme.palette.warning.main
      case 'paused':
      case 'exited':
        return theme.palette.error.main
      default:
        return theme.palette.text.secondary
    }
  })()

  const webuiUrl =
    container?.webui_port != null
      ? `${window.location.protocol}//${window.location.hostname}:${container.webui_port}`
      : ''

  const updateEntry = (index: number, patch: Partial<NetworkEntry>) => {
    setFormDirty(true)
    setForm(prev => ({
      ...prev,
      entries: prev.entries.map((entry, i) => (i === index ? { ...entry, ...patch } : entry)),
    }))
  }

  const removeEntry = (index: number) => {
    setFormDirty(true)
    setForm(prev => ({ ...prev, entries: prev.entries.filter((_, i) => i !== index) }))
  }

  const addEntry = () => {
    setFormDirty(true)
    setForm(prev => ({
      ...prev,
      entries: [...prev.entries, createNetworkEntry('websocketServers', nextEntryName(prev.entries))],
    }))
  }

  /**
   * 保存前的本地体检
   *
   * 后端会再校验一遍（包括出站地址的公网检查），这里只拦最常见的手滑，省一次往返。
   * 托管条目的地址与令牌由后端生成，跳过内容校验。
   */
  const findEntryError = (): string => {
    const seen = new Set<string>()
    for (const entry of form.entries) {
      const name = entry.name.trim()
      if (!name) return t('panel.network.errors.nameRequired')
      const key = `${entry.type}:${name}`
      if (seen.has(key)) return t('panel.network.errors.duplicateName', { name })
      seen.add(key)
      if (entry.managed_by_nekro) continue
      if (isServerEntryType(entry.type)) {
        if (!Number.isInteger(entry.port) || entry.port < MIN_PORT || entry.port > MAX_PORT) {
          return t('panel.network.errors.invalidPort', { name, min: MIN_PORT, max: MAX_PORT })
        }
        continue
      }
      const schemes = CLIENT_URL_SCHEMES[entry.type] ?? []
      if (!schemes.some(scheme => entry.url.startsWith(scheme))) {
        return t('panel.network.errors.invalidUrl', { name, schemes: schemes.join(' / ') })
      }
    }
    return ''
  }

  const handleSaveNetwork = () => {
    const error = findEntryError()
    if (error) {
      notification.error(error)
      return
    }
    saveNetworkMutation.mutate()
  }

  const handleCopy = async (value: string) => {
    if (!value) return
    const ok = await copyText(value)
    if (ok) {
      notification.success(t('common.copied'))
    } else {
      notification.error(t('common.copyFailed'))
    }
  }

  if (instanceLoading || (hostsContainer && containerLoading)) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
        <CircularProgress />
      </Box>
    )
  }

  return (
    <Stack spacing={2}>
      {containerError ? <Alert severity="error">{(containerError as Error).message}</Alert> : null}

      {instance && !hostsContainer ? (
        <Alert severity="info">
          {provider === 'remote_onebot'
            ? t('panel.remoteNotice')
            : t('panel.nativeNotice', { adapter: instance.adapter_key })}
        </Alert>
      ) : null}

      {hostsContainer ? (
        <Card sx={CARD_VARIANTS.default.styles}>
          <CardContent>
            {containerPresent ? (
              <Stack spacing={2}>
                <Stack
                  direction={{ xs: 'column', md: 'row' }}
                  spacing={1.5}
                  alignItems={{ xs: 'flex-start', md: 'center' }}
                  justifyContent="space-between"
                >
                  <Stack spacing={0.5}>
                    <Stack direction="row" spacing={1} alignItems="center">
                      <Typography variant="subtitle1" fontWeight={600}>
                        {container?.name || t('panel.unnamedContainer')}
                      </Typography>
                      <Chip
                        size="small"
                        label={t(`console:container.${state}`)}
                        sx={CHIP_VARIANTS.getCustomColorChip(stateColor, true)}
                      />
                    </Stack>
                    <Typography variant="caption" color="text.secondary">
                      {container?.image || '-'}
                      {container?.started_at ? ` · ${container.started_at}` : ''}
                    </Typography>
                  </Stack>

                  <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                    {canManageContainers &&
                      (isRunning ? (
                        <ActionButton
                          tone="secondary"
                          size="small"
                          startIcon={<StopIcon />}
                          disabled={busy}
                          onClick={() => stopMutation.mutate()}
                        >
                          {t('panel.stop')}
                        </ActionButton>
                      ) : (
                        <ActionButton
                          tone="primary"
                          size="small"
                          startIcon={<PlayArrowIcon />}
                          disabled={busy}
                          onClick={() => startMutation.mutate()}
                        >
                          {t('panel.start')}
                        </ActionButton>
                      ))}
                    {/* 掉线自愈的两个按钮。Docker 的 restart 对已停止的容器就是启动它，
                        所以租户侧不给起/停也不缺能力 */}
                    {canOperateProtocol ? (
                      <ActionButton
                        tone="secondary"
                        size="small"
                        startIcon={<RestartAltIcon />}
                        disabled={busy}
                        onClick={() => restartMutation.mutate()}
                      >
                        {t('panel.restart')}
                      </ActionButton>
                    ) : null}
                    {canOperateProtocol ? (
                      <ActionButton
                        tone="secondary"
                        size="small"
                        startIcon={<QrCode2Icon />}
                        disabled={!isRunning}
                        onClick={() => setQrDialogOpen(true)}
                      >
                        {t('panel.scanLogin')}
                      </ActionButton>
                    ) : null}
                    {/* NapCat WebUI 只绑在宿主回环上、且是另一套弱口令后台，
                        这个跳转与容器生命周期同档 */}
                    {webuiUrl && canManageContainers ? (
                      <ActionButton
                        tone="ghost"
                        size="small"
                        startIcon={<OpenInNewIcon />}
                        onClick={() => window.open(webuiUrl, '_blank', 'noopener,noreferrer')}
                      >
                        {t('panel.openWebui')}
                      </ActionButton>
                    ) : null}
                    {canManageContainers ? (
                      <ActionButton
                        tone="danger"
                        size="small"
                        startIcon={<DeleteOutlineIcon />}
                        disabled={busy}
                        onClick={() => setDeleteDialogOpen(true)}
                      >
                        {t('panel.delete')}
                      </ActionButton>
                    ) : null}
                  </Stack>
                </Stack>

                {!isRunning ? <Alert severity="warning">{t('panel.notRunningHint')}</Alert> : null}
              </Stack>
            ) : (
              <Stack spacing={1.5} alignItems="center" sx={{ py: 4 }}>
                <Typography variant="subtitle1" fontWeight={600}>
                  {t('panel.emptyTitle')}
                </Typography>
                <Typography variant="body2" color="text.secondary" textAlign="center">
                  {canManageContainers ? t('panel.emptyDescription') : t('panel.emptyNoPermission')}
                </Typography>
                {canManageContainers ? (
                  <ActionButton
                    tone="primary"
                    startIcon={<AddCircleOutlineIcon />}
                    disabled={createMutation.isPending}
                    onClick={() => createMutation.mutate()}
                  >
                    {t('panel.createContainer')}
                  </ActionButton>
                ) : null}
              </Stack>
            )}
          </CardContent>
        </Card>
      ) : null}

      {usesOneBotNetwork && canManageContainers ? (
        <Card sx={CARD_VARIANTS.default.styles}>
          <CardContent>
            <Stack spacing={2}>
              <Stack
                direction="row"
                alignItems="center"
                justifyContent="space-between"
                flexWrap="wrap"
                useFlexGap
                spacing={1}
              >
                <Stack spacing={0.25}>
                  <Typography variant="subtitle1" fontWeight={600}>
                    {t('panel.network.title')}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {t('panel.network.subtitle')}
                  </Typography>
                </Stack>
                {hostsContainer ? (
                  <Stack direction="row" spacing={1}>
                    <ActionButton
                      tone="secondary"
                      size="small"
                      startIcon={<AddCircleOutlineIcon />}
                      onClick={addEntry}
                    >
                      {t('panel.network.addEntry')}
                    </ActionButton>
                    <ActionButton
                      tone="ghost"
                      size="small"
                      disabled={!formDirty}
                      onClick={() => {
                        setForm(networkConfig ?? EMPTY_NETWORK_CONFIG)
                        setFormDirty(false)
                      }}
                    >
                      {t('common.reset')}
                    </ActionButton>
                    <ActionButton
                      tone="primary"
                      size="small"
                      disabled={!formDirty || saveNetworkMutation.isPending}
                      onClick={handleSaveNetwork}
                    >
                      {t('common.save')}
                    </ActionButton>
                  </Stack>
                ) : null}
              </Stack>

              <Divider />

              {form.entries.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  {t('panel.network.empty')}
                </Typography>
              ) : (
                <Stack spacing={2}>
                  {form.entries.map((entry, index) => (
                    <NetworkEntryCard
                      key={`${entry.type}-${index}`}
                      entry={entry}
                      index={index}
                      editable={hostsContainer}
                      onChange={updateEntry}
                      onRemove={removeEntry}
                      onCopy={value => void handleCopy(value)}
                    />
                  ))}
                </Stack>
              )}

              <Box
                sx={{
                  borderRadius: BORDER_RADIUS.SMALL,
                  backgroundColor: alpha(theme.palette.info.main, 0.08),
                  px: 1.5,
                  py: 1,
                }}
              >
                <Typography variant="caption" color="text.secondary">
                  {hostsContainer
                    ? t('panel.network.restartRequired')
                    : t('panel.network.remoteSyncRequired')}
                </Typography>
              </Box>
            </Stack>
          </CardContent>
        </Card>
      ) : null}

      <QrCodeLoginDialog
        open={qrDialogOpen}
        instanceKey={instanceKey}
        onClose={() => setQrDialogOpen(false)}
        onLoggedIn={refreshAfterLogin}
      />

      <NekroDialog
        open={deleteDialogOpen}
        onClose={() => setDeleteDialogOpen(false)}
        maxWidth="xs"
        title={t('panel.deleteConfirmTitle')}
        actions={
          <Stack direction="row" spacing={1}>
            <ActionButton tone="ghost" onClick={() => setDeleteDialogOpen(false)}>
              {t('common.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={deleteMutation.isPending}
              onClick={() => deleteMutation.mutate()}
            >
              {t('panel.delete')}
            </ActionButton>
          </Stack>
        }
      >
        <Typography variant="body2">
          {t('panel.deleteConfirmBody', { name: container?.name || instanceKey })}
        </Typography>
      </NekroDialog>
    </Stack>
  )
}
