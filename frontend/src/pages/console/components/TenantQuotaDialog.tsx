/**
 * 租户管理员：「我的额度」对话框（频道层与实例层共用）
 *
 * 租户能填的只是「在平台方给的额度以内再调低」，所以两个上限都在旁边写着，并在提交前
 * 做一次**提示性**校验；最终以后端 400 为准——前端算出来的上限与后端解析出来的生效额度
 * 之间还隔着单位快照、临时加额这些后端才知道的东西，把前端那句当成最终判据就会两头不一致。
 *
 * 留空 = 清除自己设的值（回到平台方给的额度），不是「填 0」。0 在平台方字段里表示不限，
 * 而在租户字段里没有这个语义：租户不能用「填 0」把自己那份变成不限。
 */

import { useEffect, useMemo, useState } from 'react'
import { Alert, Box, CircularProgress, Stack, TextField, Typography } from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../../components/common/ActionButton'
import NekroDialog from '../../../components/common/NekroDialog'
import QueryErrorState from '../../../components/common/QueryErrorState'
import { useNotification } from '../../../hooks/useNotification'
import {
  CONSOLE_CHANNEL_QUOTA_QUERY_KEYS,
  consoleChannelQuotaApi,
  type ChannelQuotaUnit,
  type TenantChannelQuotaUpdate,
  type TenantInstanceQuotaUpdate,
} from '../../../services/api/consoleChannelQuota'
import { channelQuotaUnitLabel, formatQuotaInt } from './channelQuotaUi'

const MAX_PAUSE_MESSAGE_LENGTH = 200

export type TenantQuotaScope = 'instance' | 'channel'

interface TenantQuotaDialogProps {
  open: boolean
  onClose: () => void
  scope: TenantQuotaScope
  adapterKey?: string
  instanceKey?: string
  chatKey?: string
}

/** 空串 = 清除（null）；正整数原样；其余非法 */
function parseTenantLimit(value: string): number | null | undefined {
  const text = value.trim()
  if (!text) return null
  const parsed = Number(text)
  if (!Number.isInteger(parsed) || parsed < 1) return undefined
  return parsed
}

export default function TenantQuotaDialog({
  open,
  onClose,
  scope,
  adapterKey = '',
  instanceKey = '',
  chatKey = '',
}: TenantQuotaDialogProps) {
  const { t } = useTranslation('console-channel-quota')
  const notification = useNotification()
  const queryClient = useQueryClient()

  const [groupLimit, setGroupLimit] = useState('')
  const [privateLimit, setPrivateLimit] = useState('')
  const [dailyLimit, setDailyLimit] = useState('')
  const [pauseMessage, setPauseMessage] = useState('')
  const [error, setError] = useState('')

  const instanceQuery = useQuery({
    queryKey: CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.tenantInstance(adapterKey, instanceKey),
    queryFn: () => consoleChannelQuotaApi.getTenantInstanceQuota(adapterKey, instanceKey),
    enabled: open && scope === 'instance' && Boolean(adapterKey && instanceKey),
    staleTime: 0,
    retry: false,
  })

  const channelQuery = useQuery({
    queryKey: CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.tenantChannel(chatKey),
    queryFn: () => consoleChannelQuotaApi.getTenantChannelQuota(chatKey),
    enabled: open && scope === 'channel' && Boolean(chatKey),
    staleTime: 0,
    retry: false,
  })

  useEffect(() => {
    if (!open) return
    setError('')
    if (scope === 'instance' && instanceQuery.data) {
      setGroupLimit(instanceQuery.data.group_daily_limit === null ? '' : String(instanceQuery.data.group_daily_limit))
      setPrivateLimit(
        instanceQuery.data.private_daily_limit === null ? '' : String(instanceQuery.data.private_daily_limit)
      )
      setPauseMessage(instanceQuery.data.pause_message ?? '')
    }
    if (scope === 'channel' && channelQuery.data) {
      setDailyLimit(channelQuery.data.daily_limit === null ? '' : String(channelQuery.data.daily_limit))
      setPauseMessage(channelQuery.data.pause_message ?? '')
    }
  }, [open, scope, instanceQuery.data, channelQuery.data])

  const saveMutation = useMutation<unknown, Error, void>({
    mutationFn: () => {
      if (scope === 'instance') {
        const body: TenantInstanceQuotaUpdate = {
          group_daily_limit: parseTenantLimit(groupLimit) ?? null,
          private_daily_limit: parseTenantLimit(privateLimit) ?? null,
          pause_message: pauseMessage.trim() ? pauseMessage : null,
        }
        return consoleChannelQuotaApi.updateTenantInstanceQuota(adapterKey, instanceKey, body)
      }
      const body: TenantChannelQuotaUpdate = {
        daily_limit: parseTenantLimit(dailyLimit) ?? null,
        pause_message: pauseMessage.trim() ? pauseMessage : null,
      }
      return consoleChannelQuotaApi.updateTenantChannelQuota(chatKey, body)
    },
    onSuccess: () => {
      notification.success(t('tenantDialog.success'))
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-daily'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-status'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-history'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-tenant-instance'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-tenant-channel'] })
      onClose()
    },
    onError: (mutationError: Error) => notification.error(mutationError.message),
  })

  const unit: ChannelQuotaUnit | null = useMemo(() => {
    if (scope === 'instance') return instanceQuery.data?.unit ?? null
    return channelQuery.data?.unit ?? null
  }, [scope, instanceQuery.data, channelQuery.data])

  const unitText = unit ? channelQuotaUnitLabel(t, unit) : '—'

  const submit = () => {
    if (scope === 'instance') {
      const group = parseTenantLimit(groupLimit)
      const privateLimitValue = parseTenantLimit(privateLimit)
      if (group === undefined || privateLimitValue === undefined) {
        setError(t('tenantDialog.invalid'))
        return
      }
      const ceilingGroup = instanceQuery.data?.platform_group_daily_limit ?? 0
      const ceilingPrivate = instanceQuery.data?.platform_private_daily_limit ?? 0
      // 上限为 0 表示平台方不限，此时任何正整数都行
      if (group !== null && ceilingGroup > 0 && group > ceilingGroup) {
        setError(t('tenantDialog.overCeiling', { value: formatQuotaInt(ceilingGroup) }))
        return
      }
      if (privateLimitValue !== null && ceilingPrivate > 0 && privateLimitValue > ceilingPrivate) {
        setError(t('tenantDialog.overCeiling', { value: formatQuotaInt(ceilingPrivate) }))
        return
      }
    } else {
      const daily = parseTenantLimit(dailyLimit)
      if (daily === undefined) {
        setError(t('tenantDialog.invalid'))
        return
      }
      const ceiling = channelQuery.data?.platform_limit ?? 0
      if (daily !== null && ceiling > 0 && daily > ceiling) {
        setError(t('tenantDialog.overCeiling', { value: formatQuotaInt(ceiling) }))
        return
      }
    }
    if (pauseMessage.length > MAX_PAUSE_MESSAGE_LENGTH) {
      setError(t('policyDialog.pauseTooLong', { max: MAX_PAUSE_MESSAGE_LENGTH }))
      return
    }
    saveMutation.mutate()
  }

  const loading = instanceQuery.isLoading || channelQuery.isLoading
  const failed = instanceQuery.isError || channelQuery.isError
  const failure = instanceQuery.error ?? channelQuery.error
  const title = scope === 'instance' ? t('tenantDialog.titleInstance') : t('tenantDialog.titleChannel')

  return (
    <NekroDialog
      open={open}
      onClose={onClose}
      title={title}
      maxWidth="sm"
      actions={
        <>
          <ActionButton tone="ghost" onClick={onClose} disabled={saveMutation.isPending}>
            {t('actions.cancel')}
          </ActionButton>
          <ActionButton tone="primary" onClick={submit} disabled={saveMutation.isPending || loading || failed}>
            {saveMutation.isPending ? <CircularProgress size={18} /> : t('actions.save')}
          </ActionButton>
        </>
      }
    >
      {failed ? (
        <QueryErrorState
          compact
          error={failure}
          onRetry={() => {
            void instanceQuery.refetch()
            void channelQuery.refetch()
          }}
        />
      ) : loading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress />
        </Box>
      ) : (
        <Stack spacing={2}>
          <Typography variant="caption" color="text.secondary">
            {t('tenantDialog.unit', { unit: unitText })}
          </Typography>

          {scope === 'instance' ? (
            <>
              <Typography variant="caption" color="text.secondary">
                {t('tenantDialog.platformCeiling', {
                  value: formatQuotaInt(instanceQuery.data?.platform_group_daily_limit ?? 0),
                })}{' '}
                ·{' '}
                {t('tenantDialog.platformCeiling', {
                  value: formatQuotaInt(instanceQuery.data?.platform_private_daily_limit ?? 0),
                })}
              </Typography>
              <TextField
                size="small"
                label={t('tenantDialog.groupLimit')}
                value={groupLimit}
                helperText={t('tenantDialog.clearHint')}
                slotProps={{ htmlInput: { min: 1, step: 1 } }}
                onChange={event => setGroupLimit(event.target.value)}
              />
              <TextField
                size="small"
                label={t('tenantDialog.privateLimit')}
                value={privateLimit}
                helperText={t('tenantDialog.clearHint')}
                slotProps={{ htmlInput: { min: 1, step: 1 } }}
                onChange={event => setPrivateLimit(event.target.value)}
              />
            </>
          ) : (
            <>
              <Typography variant="caption" color="text.secondary">
                {t('tenantDialog.platformCeiling', {
                  value: formatQuotaInt(channelQuery.data?.platform_limit ?? 0),
                })}
              </Typography>
              <TextField
                size="small"
                label={t('tenantDialog.limit')}
                value={dailyLimit}
                helperText={t('tenantDialog.clearHint')}
                slotProps={{ htmlInput: { min: 1, step: 1 } }}
                onChange={event => setDailyLimit(event.target.value)}
              />
            </>
          )}

          <TextField
            multiline
            minRows={2}
            size="small"
            label={t('tenantDialog.pauseMessage')}
            value={pauseMessage}
            helperText={t('tenantDialog.pauseMessageHint', { max: MAX_PAUSE_MESSAGE_LENGTH })}
            onChange={event => setPauseMessage(event.target.value)}
          />
          <Typography variant="caption" color="text.secondary" align="right">
            {t('global.charCount', { value: pauseMessage.length, max: MAX_PAUSE_MESSAGE_LENGTH })}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('tenantDialog.ceilingHint')}
          </Typography>

          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      )}
    </NekroDialog>
  )
}
