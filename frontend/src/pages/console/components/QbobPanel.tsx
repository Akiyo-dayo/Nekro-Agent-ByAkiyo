import { useState } from 'react'
import {
  Alert,
  Box,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  FormControlLabel,
  Stack,
  Switch,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import CableIcon from '@mui/icons-material/Cable'
import LinkOffIcon from '@mui/icons-material/LinkOff'
import RestartAltIcon from '@mui/icons-material/RestartAlt'
import ActionButton from '../../../components/common/ActionButton'
import NekroDialog from '../../../components/common/NekroDialog'
import { useIdentity } from '../../../hooks/useIdentity'
import { useNotification } from '../../../hooks/useNotification'
import { CARD_VARIANTS, CHIP_VARIANTS } from '../../../theme/variants'
import {
  consoleQbobApi,
  type QbobBotInfo,
  type QbobBotOptions,
} from '../../../services/api/consoleQbob'
import QbobConnectCard from './QbobConnectCard'
import QbobLicenseNote from './QbobLicenseNote'
import QbobServiceCredentialForm from './QbobServiceCredentialForm'

interface QbobPanelProps {
  instanceKey: string
}

/** 状态轮询间隔：连接状态变化不需要秒级 */
const BOT_POLL_INTERVAL_MS = 10_000

/**
 * 内置 qbob 实例的协议端面板
 *
 * 两档，与后端 `routers/console/qbob.py` 一一对应：
 *
 * - `isPlatformOwner`（平台方）：接入（扫码 / 手填）、重新接线、解绑。接入决定哪个 QQ 机器人
 *   接到哪个实例，并写入带实例令牌的出站端点，属于接入拓扑。
 * - `canOperateProtocol`（平台方或租户管理员）：看状态、启用 / 停用、两项开关、重载。
 *
 * 隐藏入口只是避免点了才发现无权，真正的边界在服务端。
 */
export default function QbobPanel({ instanceKey }: QbobPanelProps) {
  const { isPlatformOwner, canOperateProtocol } = useIdentity()
  const { t } = useTranslation('console-setup')
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const [detachOpen, setDetachOpen] = useState(false)

  const botQueryKey = ['console-qbob-bot', instanceKey]

  const botQuery = useQuery({
    queryKey: botQueryKey,
    queryFn: () => consoleQbobApi.getBot(instanceKey),
    refetchInterval: BOT_POLL_INTERVAL_MS,
    enabled: Boolean(instanceKey) && canOperateProtocol,
  })

  // 服务账号这一层只有平台方能补救（接口是 Role.Super），租户身份下不发这个请求
  const serviceQuery = useQuery({
    queryKey: ['console-qbob-service'],
    queryFn: () => consoleQbobApi.getService(),
    enabled: isPlatformOwner,
    staleTime: 30_000,
  })
  const service = serviceQuery.data
  const needsCredential = Boolean(
    isPlatformOwner && service?.configured && service.reachable && !service.service_account_ready
  )

  /** 接入 / 解绑会改实例绑定的账号，实例查询与列表也要失效 */
  const refreshAll = (bot?: QbobBotInfo) => {
    if (bot) queryClient.setQueryData(botQueryKey, bot)
    void queryClient.invalidateQueries({ queryKey: botQueryKey })
    void queryClient.invalidateQueries({ queryKey: ['console-instance', instanceKey] })
    void queryClient.invalidateQueries({ queryKey: ['console-instances'] })
  }

  const optionsMutation = useMutation({
    mutationFn: (options: QbobBotOptions) => consoleQbobApi.updateOptions(instanceKey, options),
    onSuccess: bot => queryClient.setQueryData(botQueryKey, bot),
    onError: (error: Error) => notification.error(error.message),
  })

  const reloadMutation = useMutation({
    mutationFn: () => consoleQbobApi.reload(instanceKey),
    onSuccess: result => {
      notification.success(result.message || t('qbob.panel.reloaded'))
      void queryClient.invalidateQueries({ queryKey: botQueryKey })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const rewireMutation = useMutation({
    mutationFn: () => consoleQbobApi.rewire(instanceKey),
    onSuccess: bot => {
      notification.success(t('qbob.panel.rewired'))
      queryClient.setQueryData(botQueryKey, bot)
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const detachMutation = useMutation({
    mutationFn: () => consoleQbobApi.detach(instanceKey),
    onSuccess: result => {
      notification.success(result.message || t('qbob.panel.detached'))
      setDetachOpen(false)
      refreshAll()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  if (!canOperateProtocol) {
    return <Alert severity="info">{t('qbob.panel.readOnly')}</Alert>
  }

  if (botQuery.isLoading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
        <CircularProgress />
      </Box>
    )
  }

  if (botQuery.error) {
    return <Alert severity="error">{(botQuery.error as Error).message}</Alert>
  }

  const bot = botQuery.data
  if (!bot) return null

  if (needsCredential) {
    return (
      <Stack spacing={2}>
        <Alert severity="warning">{service?.message || bot.message}</Alert>
        <Card sx={CARD_VARIANTS.default.styles}>
          <CardContent>
            <QbobServiceCredentialForm />
          </CardContent>
        </Card>
      </Stack>
    )
  }

  if (!bot.configured) {
    return <Alert severity="warning">{bot.message || t('qbob.serviceUnavailable')}</Alert>
  }

  if (!bot.attached) {
    return (
      <Stack spacing={2}>
        <Alert severity="info">{bot.message || t('qbob.panel.notAttached')}</Alert>
        {isPlatformOwner ? (
          <Card sx={CARD_VARIANTS.default.styles}>
            <CardContent>
              <QbobConnectCard instanceKey={instanceKey} onAttached={refreshAll} />
            </CardContent>
          </Card>
        ) : (
          <Typography variant="body2" color="text.secondary">
            {t('qbob.panel.askProvider')}
          </Typography>
        )}
      </Stack>
    )
  }

  const statusChip = (ok: boolean, label: string) => (
    <Chip
      size="small"
      label={label}
      sx={CHIP_VARIANTS.getCustomColorChip(ok ? theme.palette.success.main : theme.palette.error.main, true)}
    />
  )

  const busy =
    optionsMutation.isPending || reloadMutation.isPending || rewireMutation.isPending || detachMutation.isPending

  return (
    <Stack spacing={2}>
      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent>
          <Stack spacing={2}>
            <Stack
              direction={{ xs: 'column', md: 'row' }}
              spacing={1.5}
              alignItems={{ xs: 'flex-start', md: 'center' }}
              justifyContent="space-between"
            >
              <Stack spacing={0.5}>
                <Typography variant="subtitle1" fontWeight={600}>
                  {bot.name || bot.appid}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {t('qbob.panel.ids', { appid: bot.appid, selfId: bot.self_id || '—' })}
                </Typography>
              </Stack>
              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                <ActionButton
                  tone="secondary"
                  size="small"
                  startIcon={<RestartAltIcon />}
                  disabled={busy || !bot.found}
                  onClick={() => reloadMutation.mutate()}
                >
                  {t('qbob.panel.reload')}
                </ActionButton>
                {isPlatformOwner ? (
                  <ActionButton
                    tone="secondary"
                    size="small"
                    startIcon={<CableIcon />}
                    disabled={busy || !bot.found}
                    onClick={() => rewireMutation.mutate()}
                  >
                    {t('qbob.panel.rewire')}
                  </ActionButton>
                ) : null}
                {isPlatformOwner ? (
                  <ActionButton
                    tone="danger"
                    size="small"
                    startIcon={<LinkOffIcon />}
                    disabled={busy}
                    onClick={() => setDetachOpen(true)}
                  >
                    {t('qbob.panel.detach')}
                  </ActionButton>
                ) : null}
              </Stack>
            </Stack>

            {bot.found ? (
              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                {statusChip(bot.running && bot.enabled, bot.running && bot.enabled ? t('qbob.panel.running') : t('qbob.panel.stopped'))}
                {statusChip(bot.qq_connected, t('qbob.panel.qqConnected'))}
                {statusChip(bot.onebot_connected, t('qbob.panel.onebotConnected'))}
                {bot.endpoint_ok ? null : statusChip(false, t('qbob.panel.endpointMismatch'))}
              </Stack>
            ) : null}

            {bot.message ? <Alert severity="warning">{bot.message}</Alert> : null}
            {bot.last_error ? (
              <Alert severity="error">{t('qbob.panel.lastError', { error: bot.last_error })}</Alert>
            ) : null}

            {bot.found ? (
              <Stack spacing={0.5}>
                <FormControlLabel
                  control={
                    <Switch
                      checked={bot.enabled}
                      disabled={busy}
                      onChange={event => optionsMutation.mutate({ enabled: event.target.checked })}
                    />
                  }
                  label={t('qbob.panel.enabled')}
                />
                <FormControlLabel
                  control={
                    <Switch
                      checked={bot.markdown_enabled}
                      disabled={busy}
                      onChange={event => optionsMutation.mutate({ markdown_enabled: event.target.checked })}
                    />
                  }
                  label={t('qbob.panel.markdown')}
                />
                <FormControlLabel
                  control={
                    <Switch
                      checked={bot.report_self_message}
                      disabled={busy}
                      onChange={event => optionsMutation.mutate({ report_self_message: event.target.checked })}
                    />
                  }
                  label={t('qbob.panel.reportSelf')}
                />
              </Stack>
            ) : null}
          </Stack>
        </CardContent>
      </Card>

      <Typography variant="caption" color="text.secondary">
        {t('qbob.groupHint')}
      </Typography>
      {isPlatformOwner ? (
        <Typography variant="caption" color="text.secondary">
          {t('qbob.panel.advancedHint')}
        </Typography>
      ) : null}
      <QbobLicenseNote qbobRef={service?.qbob_ref} />

      <NekroDialog
        open={detachOpen}
        onClose={() => setDetachOpen(false)}
        maxWidth="xs"
        title={t('qbob.panel.detachConfirmTitle')}
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setDetachOpen(false)}>
              {t('common.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={detachMutation.isPending}
              onClick={() => detachMutation.mutate()}
            >
              {t('qbob.panel.detach')}
            </ActionButton>
          </>
        }
      >
        <Typography variant="body2">{t('qbob.panel.detachConfirmBody', { appid: bot.appid })}</Typography>
      </NekroDialog>
    </Stack>
  )
}
