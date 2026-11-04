import { Suspense, lazy, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  AlertTitle,
  Box,
  Card,
  CardContent,
  CircularProgress,
  Divider,
  SxProps,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tab,
  TextField,
  Theme,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ArrowBackRoundedIcon from '@mui/icons-material/ArrowBackRounded'
import LinkRoundedIcon from '@mui/icons-material/LinkRounded'
import MoveDownRoundedIcon from '@mui/icons-material/MoveDownRounded'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import ActionButton from '../../components/common/ActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import { PageTabs } from '../../components/common/NekroTabs'
import { useIdentity } from '../../hooks/useIdentity'
import { useNotification } from '../../hooks/useNotification'
import { CARD_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  DEFAULT_CONSOLE_INSTANCE_DETAIL_TAB,
  consoleApi,
  consolePaths,
  isConsoleInstanceDetailTab,
  type ConsoleInstanceDetailTab,
  type InstanceInfo,
} from '../../services/api/console'
import { isNotFoundError } from '../../services/api/axios'
import InstanceMigrationDialog from './components/InstanceMigrationDialog'
import InstanceStatusChip from './components/InstanceStatusChip'
import UnclaimedProtocolPanel from './components/UnclaimedProtocolPanel'

// 协议端面板由 NapCat 面板路实现，按需加载避免阻塞详情页首屏
const AccessTokenCard = lazy(() => import('./components/AccessTokenCard'))
const NapCatPanel = lazy(() => import('./components/NapCatPanel'))
const QbobPanel = lazy(() => import('./components/QbobPanel'))
const OnebotEndpointCard = lazy(() => import('./components/OnebotEndpointCard'))

function formatDateTime(value: string | null, fallback: string): string {
  if (!value) return fallback
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return fallback
  return new Date(timestamp).toLocaleString()
}

function InfoRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'baseline',
        gap: 2,
        py: 0.75,
      }}
    >
      <Typography variant="caption" color="text.secondary" sx={{ minWidth: 96, flexShrink: 0 }}>
        {label}
      </Typography>
      <Box sx={{ minWidth: 0, flex: 1 }}>
        {typeof value === 'string' ? (
          <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
            {value}
          </Typography>
        ) : (
          value
        )}
      </Box>
    </Box>
  )
}

/**
 * 绑定 / 改绑 / 解绑实例账号
 *
 * 「未绑定账号」给出的下一步有两条：认领一个已连入的协议端，或者手工填上账号。
 * 后者在此之前**界面上根本不存在**——`PATCH /console/instances/{key}` 一直接受
 * `provider_account_id`，但前端没有任何一处调用过它（新建向导也不收这个字段）。
 * 于是提示语指着一个不存在的输入框，而「先建实例、协议端还没连上来」——外部 OneBot
 * 最常见的开局——是一条彻底的死路：待认领面板此刻没有任何东西可认领。
 *
 * 原生适配器不出现这个入口：它们的连接来自适配器自身配置，`provider_account_id` 恒为空，
 * 给出输入框等于请用户填一个不会被读的值（判据与后端 `account_binding_required` 一致）。
 *
 * 解绑走同一个入口（清空后提交）：后端 `sync_account_binding_status()` 会把状态一并
 * 落回 `unbound`，否则解绑后的实例会顶着 `offline` 继续被掉线监测告警。
 */
function AccountBindDialog({
  instance,
  open,
  onClose,
}: {
  instance: InstanceInfo
  open: boolean
  onClose: () => void
}) {
  const { t } = useTranslation('console')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const [account, setAccount] = useState(instance.provider_account_id)

  // 每次重新打开都回到库里的当前值：这是一次会改路由归属的写操作，不能让上一次的输入残留
  useEffect(() => {
    if (open) setAccount(instance.provider_account_id)
  }, [open, instance.provider_account_id])

  const bindMutation = useMutation({
    mutationFn: () =>
      consoleApi.updateInstance(instance.instance_key, { provider_account_id: account.trim() }),
    onSuccess: updated => {
      notification.success(
        updated.provider_account_id ? t('messages.accountBound') : t('messages.accountUnbound')
      )
      onClose()
      void queryClient.invalidateQueries({ queryKey: ['console-instance', instance.instance_key] })
      void queryClient.invalidateQueries({ queryKey: ['console-instances'] })
      void queryClient.invalidateQueries({ queryKey: ['console-overview'] })
      void queryClient.invalidateQueries({ queryKey: ['console-unclaimed-instances'] })
      void queryClient.invalidateQueries({ queryKey: ['instance-scope', 'instances'] })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const unchanged = account.trim() === instance.provider_account_id

  return (
    <NekroDialog
      open={open}
      onClose={onClose}
      title={t('accountDialog.title')}
      maxWidth="xs"
      actions={
        <>
          <ActionButton tone="ghost" onClick={onClose}>
            {tCommon('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone="primary"
            disabled={bindMutation.isPending || unchanged}
            onClick={() => bindMutation.mutate()}
          >
            {tCommon('actions.save')}
          </ActionButton>
        </>
      }
    >
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
        <Typography variant="body2" color="text.secondary">
          {t('accountDialog.description')}
        </Typography>
        <TextField
          size="small"
          fullWidth
          autoComplete="off"
          label={t('detail.fields.account')}
          helperText={t('accountDialog.helper')}
          value={account}
          onChange={event => setAccount(event.target.value)}
        />
      </Box>
    </NekroDialog>
  )
}

function OverviewTab({ instance }: { instance: InstanceInfo }) {
  const { t } = useTranslation('console')
  const { t: tCommon } = useTranslation('common')
  const { isPlatformOwner } = useIdentity()
  const navigate = useNavigate()
  const [migrationOpen, setMigrationOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)

  // 账号绑定是平台方能力（后端 `PATCH /instances/{key}` 要求 Role.Super），
  // 原生适配器没有账号这一说；内置 qbob 的账号由接入流程按 bot 的 self_id 写入，
  // 通用改绑 / 解绑会让 bot 重连时落到未认领实例，必须走协议端页的解绑
  const canBindAccount =
    isPlatformOwner && instance.provider !== 'native' && instance.provider !== 'builtin_qbob'

  // 换接入令牌只对平台方开放（后端 `Role.Super`），且只对「有自己的令牌」的 OneBot 实例有意义：
  // 未认领实例的协议端凭部署级令牌连着，凭空给它一枚会让它下一次握手就被拒（后端同样拒绝）
  const canRotateAccessToken =
    isPlatformOwner &&
    instance.adapter_key === 'onebot_v11' &&
    ['builtin_napcat', 'remote_onebot', 'builtin_qbob'].includes(instance.provider) &&
    instance.status !== 'unclaimed'

  return (
    <Box
      sx={{
        display: 'grid',
        gridTemplateColumns: { xs: '1fr', md: 'repeat(2, minmax(0, 1fr))' },
        gap: 2,
        alignContent: 'start',
      }}
    >
      {/* 「尚未绑定账号」要说清楚它意味着什么、以及下一步去哪，而不是一个安静的状态字。
          面板本身在没有待认领协议端时不渲染，于是这里天然分成两句话：
          有协议端在等 -> 直接给认领入口；一个都没有 -> 告诉他协议端还没连上来 */}
      {isPlatformOwner && instance.status === 'unbound' && (
        <Box sx={{ gridColumn: '1 / -1', display: 'flex', flexDirection: 'column', gap: 1 }}>
          <Alert severity="warning" variant="outlined">
            <AlertTitle sx={{ fontWeight: 700 }}>{t('detail.unboundTitle')}</AlertTitle>
            {instance.provider === 'builtin_qbob'
              ? t('detail.qbobUnboundHint')
              : t('detail.unboundHint')}
            {/* qbob 实例的下一步是到协议端页扫码 / 填凭据接入，不走认领或手工改绑 */}
            {instance.provider === 'builtin_qbob' ? (
              <Box sx={{ mt: 1.5 }}>
                <ActionButton
                  tone="primary"
                  size="small"
                  startIcon={<LinkRoundedIcon fontSize="small" />}
                  onClick={() =>
                    navigate(consolePaths.instanceDetail(instance.instance_key, 'protocol'))
                  }
                >
                  {t('actions.goToProtocol')}
                </ActionButton>
              </Box>
            ) : canBindAccount ? (
              <Box sx={{ mt: 1.5 }}>
                <ActionButton
                  tone="primary"
                  size="small"
                  startIcon={<LinkRoundedIcon fontSize="small" />}
                  onClick={() => setAccountOpen(true)}
                >
                  {t('actions.bindAccount')}
                </ActionButton>
              </Box>
            ) : null}
          </Alert>
          {/* qbob 的 bot 由接入流程绑定，不会以未认领实例的形式出现在这里 */}
          {instance.provider === 'builtin_qbob' ? null : (
            <UnclaimedProtocolPanel preferredTargetKey={instance.instance_key} />
          )}
        </Box>
      )}

      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent>
          <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
            {t('detail.baseInfo')}
          </Typography>
          <Divider sx={{ mb: 1 }} />
          <InfoRow label={t('detail.fields.instanceKey')} value={instance.instance_key} />
          <InfoRow
            label={t('detail.fields.displayName')}
            value={instance.display_name || instance.instance_key}
          />
          <InfoRow label={t('detail.fields.adapterKey')} value={instance.adapter_key} />
          <InfoRow label={t('detail.fields.provider')} value={t(`provider.${instance.provider}`)} />
          <InfoRow
            label={t('detail.fields.account')}
            value={
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
                  {instance.provider_account_id || '—'}
                </Typography>
                {canBindAccount && (
                  <ActionButton tone="ghost" size="small" onClick={() => setAccountOpen(true)}>
                    {instance.provider_account_id
                      ? t('actions.rebindAccount')
                      : t('actions.bindAccount')}
                  </ActionButton>
                )}
              </Box>
            }
          />
          <InfoRow
            label={t('detail.fields.enabled')}
            value={instance.enabled ? tCommon('status.enabled') : tCommon('status.disabled')}
          />
          <InfoRow
            label={t('detail.fields.isDefault')}
            value={instance.is_default ? tCommon('common.yes') : tCommon('common.no')}
          />
          {instance.last_error && (
            <InfoRow label={t('detail.fields.lastError')} value={instance.last_error} />
          )}
          {instance.offline_since && (
            <InfoRow
              label={t('detail.fields.offlineSince')}
              value={formatDateTime(instance.offline_since, '—')}
            />
          )}
        </CardContent>
      </Card>

      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Card sx={CARD_VARIANTS.default.styles}>
          <CardContent>
            <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
              {t('detail.runtime')}
            </Typography>
            <Divider sx={{ mb: 1 }} />
            <InfoRow
              label={t('list.columns.channels')}
              value={String(instance.stats.channel_count)}
            />
            <InfoRow
              label={t('list.columns.messagesToday')}
              value={String(instance.stats.message_count_today)}
            />
            <InfoRow
              label={t('list.columns.lastActive')}
              value={formatDateTime(instance.stats.last_active_at, t('card.never'))}
            />
          </CardContent>
        </Card>

        <Card sx={CARD_VARIANTS.default.styles}>
          <CardContent>
            <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
              {t('detail.container')}
            </Typography>
            <Divider sx={{ mb: 1 }} />
            {instance.container ? (
              <>
                <InfoRow
                  label={t('detail.fields.containerName')}
                  value={instance.container.name || '—'}
                />
                <InfoRow
                  label={t('detail.fields.containerState')}
                  value={t(`container.${instance.container.state}`)}
                />
                <InfoRow
                  label={t('detail.fields.containerImage')}
                  value={instance.container.image || '—'}
                />
                <InfoRow
                  label={t('detail.fields.webuiPort')}
                  value={
                    instance.container.webui_port === null
                      ? '—'
                      : String(instance.container.webui_port)
                  }
                />
                <InfoRow
                  label={t('detail.fields.startedAt')}
                  value={formatDateTime(instance.container.started_at, '—')}
                />
              </>
            ) : (
              <Typography variant="body2" color="text.secondary">
                {t('detail.containerNone')}
              </Typography>
            )}
          </CardContent>
        </Card>

        {/* 跨实例迁移会移动数据归属，是平台方专属能力；租户身份下整张卡片不出现 */}
        {isPlatformOwner && (
          <Card sx={CARD_VARIANTS.default.styles}>
            <CardContent>
              <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                {t('detail.migration')}
              </Typography>
              <Divider sx={{ mb: 1 }} />
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                {t('detail.migrationHint')}
              </Typography>
              <ActionButton
                tone="secondary"
                size="small"
                startIcon={<MoveDownRoundedIcon fontSize="small" />}
                onClick={() => setMigrationOpen(true)}
              >
                {t('actions.migrate')}
              </ActionButton>
            </CardContent>
          </Card>
        )}

        {/* 换接入令牌：平台方专属，且只对 OneBot 形态、已认领的实例有意义 */}
        {canRotateAccessToken && (
          <Suspense fallback={null}>
            <AccessTokenCard instance={instance} />
          </Suspense>
        )}
      </Box>

      {isPlatformOwner && (
        <InstanceMigrationDialog
          open={migrationOpen}
          onClose={() => setMigrationOpen(false)}
          target={instance}
        />
      )}

      {canBindAccount && (
        <AccountBindDialog
          instance={instance}
          open={accountOpen}
          onClose={() => setAccountOpen(false)}
        />
      )}
    </Box>
  )
}

function EventsTab({ instanceKey }: { instanceKey: string }) {
  const { t } = useTranslation('console')

  const eventsQuery = useQuery({
    queryKey: ['console-health-events', instanceKey],
    queryFn: () =>
      consoleApi.getHealthEvents({ instance_key: instanceKey, page: 1, page_size: 50 }),
    staleTime: 15_000,
    retry: false,
  })

  if (eventsQuery.isLoading) {
    return (
      <Box sx={{ minHeight: 200, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <CircularProgress />
      </Box>
    )
  }

  if (eventsQuery.isError) {
    // 这一格的入口已经按 `canOperateProtocol` 收敛过（只读成员看不到这个页签），
    // 但直接敲 `/console/instances/xxx/events` 仍会走到这里；403 要说「你无权查看」，
    // 不能与「状态记录加载失败」混成一句
    return (
      <QueryErrorState error={eventsQuery.error} onRetry={() => void eventsQuery.refetch()} />
    )
  }

  const events = eventsQuery.data?.items ?? []
  if (events.length === 0) {
    return (
      <Box sx={{ minHeight: 200, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Typography variant="body2" color="text.secondary">
          {t('detail.events.empty')}
        </Typography>
      </Box>
    )
  }

  return (
    <Box sx={UNIFIED_TABLE_STYLES.tableContentContainer as SxProps<Theme>}>
      <Box sx={UNIFIED_TABLE_STYLES.tableViewport as SxProps<Theme>}>
        <Table stickyHeader size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('detail.events.columns.time')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('detail.events.columns.type')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('detail.events.columns.change')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('detail.events.columns.message')}
              </TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {events.map(event => (
              <TableRow key={event.id} hover sx={UNIFIED_TABLE_STYLES.row as SxProps<Theme>}>
                <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                  {formatDateTime(event.create_time, '—')}
                </TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                  {event.event_type}
                </TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                  {event.status_from || '—'} → {event.status_to || '—'}
                </TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                  {event.message || '—'}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Box>
    </Box>
  )
}

export default function InstanceDetailPage() {
  const navigate = useNavigate()
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('console')
  const { isPlatformOwner, canOperateProtocol } = useIdentity()
  const params = useParams<{ instanceKey: string; tab?: string }>()
  const instanceKey = params.instanceKey ?? ''

  const [pendingToggle, setPendingToggle] = useState(false)

  const activeTab: ConsoleInstanceDetailTab = isConsoleInstanceDetailTab(params.tab)
    ? params.tab
    : DEFAULT_CONSOLE_INSTANCE_DETAIL_TAB

  const instanceQuery = useQuery({
    queryKey: ['console-instance', instanceKey],
    queryFn: () => consoleApi.getInstance(instanceKey),
    enabled: Boolean(instanceKey),
    staleTime: 15_000,
    // 403/404 都不会因为再试一次变成 200
    retry: false,
  })

  const toggleMutation = useMutation({
    mutationFn: (enable: boolean) =>
      enable ? consoleApi.enableInstance(instanceKey) : consoleApi.disableInstance(instanceKey),
    onMutate: () => setPendingToggle(true),
    onSettled: () => setPendingToggle(false),
    onSuccess: (_data, enable) => {
      notification.success(enable ? t('messages.enableSuccess') : t('messages.disableSuccess'))
      void queryClient.invalidateQueries({ queryKey: ['console-instance', instanceKey] })
      void queryClient.invalidateQueries({ queryKey: ['console-instances'] })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  // 状态流水读 `/console/health/events`：它现在按可见实例过滤后对 `Role.Admin` 开放，
  // 所以租户管理员看得到自己的那份；只读的租户普通用户仍会 403，对他不画这一格，
  // 否则点开就是一条红色的「加载失败」。
  const tabs = useMemo(
    () =>
      [
        { value: 'overview' as const, label: t('detail.tabs.overview') },
        { value: 'protocol' as const, label: t('detail.tabs.protocol') },
        ...(canOperateProtocol
          ? [{ value: 'events' as const, label: t('detail.tabs.events') }]
          : []),
      ] satisfies { value: ConsoleInstanceDetailTab; label: string }[],
    [t, canOperateProtocol]
  )

  const instance = instanceQuery.data
  // 直接敲 URL 也不能落在一张画不出来的页上；身份未确认时 `canOperateProtocol`
  // 按平台方兜底（`FALLBACK_ROLE`），不会误伤
  const effectiveTab: ConsoleInstanceDetailTab =
    activeTab === 'events' && !canOperateProtocol ? DEFAULT_CONSOLE_INSTANCE_DETAIL_TAB : activeTab

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 3 },
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        overflow: 'hidden',
        boxSizing: 'border-box',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<ArrowBackRoundedIcon />}
          onClick={() => navigate(consolePaths.instanceList())}
        >
          {t('detail.backToList')}
        </ActionButton>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography variant="h6" fontWeight={700} noWrap>
            {instance?.display_name || instanceKey}
          </Typography>
          <Typography variant="caption" color="text.secondary" noWrap component="div">
            {instanceKey}
          </Typography>
        </Box>
        {instance && (
          <>
            <InstanceStatusChip
              status={instance.status}
              offlineSince={instance.offline_since}
              lastError={instance.last_error}
            />
            {/* 启用 / 停用实例是 `Role.Super`（`POST /console/instances/{key}/{enable,disable}`）。
                同页的迁移卡片、绑定账号入口都老老实实带了 `isPlatformOwner`，只有这里漏了，
                于是租户身上摆着一个点了必然 403 的按钮 —— 而它就在状态标旁边，
                恰好是「我的 Bot 掉线了」时最容易被点的那一个 */}
            {isPlatformOwner && (
              <ActionButton
                tone={instance.enabled ? 'secondary' : 'primary'}
                size="small"
                disabled={pendingToggle}
                startIcon={
                  instance.enabled ? (
                    <StopRoundedIcon fontSize="small" />
                  ) : (
                    <PlayArrowRoundedIcon fontSize="small" />
                  )
                }
                onClick={() => toggleMutation.mutate(!instance.enabled)}
              >
                {instance.enabled ? t('actions.disable') : t('actions.enable')}
              </ActionButton>
            )}
          </>
        )}
      </Box>

      {instance?.last_error && (
        <Alert severity="error" variant="outlined" sx={{ color: theme.palette.error.main }}>
          {instance.last_error}
        </Alert>
      )}

      <Box sx={{ flexShrink: 0, ...(UNIFIED_TABLE_STYLES.pageTabContainer as object) }}>
        <PageTabs
          value={effectiveTab}
          onChange={(_event, value: ConsoleInstanceDetailTab) =>
            navigate(consolePaths.instanceDetail(instanceKey, value), { replace: true })
          }
          variant="scrollable"
          scrollButtons="auto"
        >
          {tabs.map(tab => (
            <Tab key={tab.value} value={tab.value} label={tab.label} />
          ))}
        </PageTabs>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        {instanceQuery.isLoading ? (
          <Box
            sx={{
              minHeight: 240,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <CircularProgress />
          </Box>
        ) : isNotFoundError(instanceQuery.error) ? (
          /* 404 必须继续说「不存在」：越权在本项目里就是 404，
             改口说「你无权查看」等于把后端刻意藏起来的"这个实例存在"讲了出来 */
          <Alert severity="warning">{t('detail.notFound')}</Alert>
        ) : instanceQuery.isError ? (
          /* 其余失败：403（权限不够，不是越权）与 5xx。此前它们全都落到下面那句
             「实例不存在或已被删除」—— 一句会让客户以为自己的 Bot 被删了的假话 */
          <QueryErrorState
            error={instanceQuery.error}
            onRetry={() => void instanceQuery.refetch()}
          />
        ) : !instance ? (
          <Alert severity="warning">{t('detail.notFound')}</Alert>
        ) : effectiveTab === 'overview' ? (
          <OverviewTab instance={instance} />
        ) : effectiveTab === 'protocol' ? (
          <Suspense
            fallback={
              <Box
                sx={{
                  minHeight: 240,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                <CircularProgress />
              </Box>
            }
          >
            <Stack spacing={2}>
              {instance.adapter_key === 'onebot_v11' && canOperateProtocol ? (
                <OnebotEndpointCard instanceKey={instance.instance_key} />
              ) : null}
              {instance.provider === 'builtin_qbob' ? (
                <QbobPanel instanceKey={instance.instance_key} />
              ) : (
                <NapCatPanel instanceKey={instance.instance_key} />
              )}
            </Stack>
          </Suspense>
        ) : (
          <EventsTab instanceKey={instance.instance_key} />
        )}
      </Box>
    </Box>
  )
}
