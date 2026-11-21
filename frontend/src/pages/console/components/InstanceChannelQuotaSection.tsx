/**
 * 实例详情页的「频道额度」一节
 *
 * 两种身份看到的是**两块不同的东西**，不是同一块加一层只读：
 * - 平台方看的是实例层策略（三层继承里的中间那层），字段可以留「继承」；
 * - 租户管理员看的是「我的额度」——只能在平台方给的额度以内再调低，外加自定义暂停提示。
 *
 * 后端本来就是两组请求模型（租户那一组的请求体里根本没有平台方字段），这里照它分两块画，
 * 免得出现一个「看起来能改平台方额度、点了却 400」的表单。
 *
 * 只读成员整节不渲染：调用方按身份判断，这里只负责内容。
 */

import { useEffect, useState } from 'react'
import {
  Alert,
  Box,
  CircularProgress,
  Divider,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../../components/common/ActionButton'
import QueryErrorState from '../../../components/common/QueryErrorState'
import { useIdentity } from '../../../hooks/useIdentity'
import { useNotification } from '../../../hooks/useNotification'
import {
  CONSOLE_CHANNEL_QUOTA_QUERY_KEYS,
  consoleChannelQuotaApi,
  type TenantInstanceQuotaUpdate,
} from '../../../services/api/consoleChannelQuota'
import ChannelQuotaPolicyDialog from './ChannelQuotaPolicyDialog'
import {
  channelQuotaActionLabel,
  channelQuotaUnitLabel,
  formatQuotaInt,
  formatQuotaLimit,
} from './channelQuotaUi'

const MAX_PAUSE_MESSAGE_LENGTH = 200

/** 空串 = 清除自己设的值；正整数原样；其余非法 */
function parseTenantLimit(value: string): number | null | undefined {
  const text = value.trim()
  if (!text) return null
  const parsed = Number(text)
  if (!Number.isInteger(parsed) || parsed < 1) return undefined
  return parsed
}

function PlatformPolicyPanel({ adapterKey, instanceKey }: { adapterKey: string; instanceKey: string }) {
  const { t } = useTranslation('console-channel-quota')
  const [dialogOpen, setDialogOpen] = useState(false)

  const policyQuery = useQuery({
    queryKey: CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.instancePolicy(adapterKey, instanceKey),
    queryFn: () => consoleChannelQuotaApi.getInstancePolicy(adapterKey, instanceKey),
    staleTime: 15_000,
    retry: false,
  })

  if (policyQuery.isError) {
    return (
      <QueryErrorState
        compact
        error={policyQuery.error}
        onRetry={() => void policyQuery.refetch()}
      />
    )
  }
  if (policyQuery.isLoading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
        <CircularProgress size={24} />
      </Box>
    )
  }

  const policy = policyQuery.data
  const rows: { label: string; value: string }[] = [
    {
      label: t('policyDialog.enabled'),
      value:
        policy?.enabled === null || policy?.enabled === undefined
          ? t('policyDialog.inherit')
          : policy.enabled
            ? t('policyDialog.enabledOn')
            : t('policyDialog.enabledOff'),
    },
    {
      label: t('policyDialog.unit'),
      value: policy?.unit ? channelQuotaUnitLabel(t, policy.unit) : t('policyDialog.inherit'),
    },
    {
      label: t('policyDialog.groupLimit'),
      value:
        policy?.group_daily_limit === null || policy?.group_daily_limit === undefined
          ? t('policyDialog.inherit')
          : formatQuotaInt(policy.group_daily_limit),
    },
    {
      label: t('policyDialog.privateLimit'),
      value:
        policy?.private_daily_limit === null || policy?.private_daily_limit === undefined
          ? t('policyDialog.inherit')
          : formatQuotaInt(policy.private_daily_limit),
    },
    {
      label: t('policyDialog.action'),
      value: policy?.over_limit_action
        ? channelQuotaActionLabel(t, policy.over_limit_action)
        : t('policyDialog.inherit'),
    },
    {
      label: t('policyDialog.pauseMessage'),
      value: policy?.pause_message || t('policyDialog.inherit'),
    },
  ]

  return (
    <Box>
      <Stack spacing={0.5}>
        {rows.map(row => (
          <Box key={row.label} sx={{ display: 'flex', alignItems: 'baseline', gap: 2 }}>
            <Typography variant="caption" color="text.secondary" sx={{ minWidth: 96 }}>
              {row.label}
            </Typography>
            <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
              {row.value}
            </Typography>
          </Box>
        ))}
      </Stack>
      <Box sx={{ mt: 1.5 }}>
        <ActionButton tone="secondary" size="small" onClick={() => setDialogOpen(true)}>
          {t('instanceSection.editPolicy')}
        </ActionButton>
      </Box>
      <ChannelQuotaPolicyDialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        scope="instance"
        adapterKey={adapterKey}
        instanceKey={instanceKey}
      />
    </Box>
  )
}

function TenantLimitPanel({ adapterKey, instanceKey }: { adapterKey: string; instanceKey: string }) {
  const { t } = useTranslation('console-channel-quota')
  const notification = useNotification()
  const queryClient = useQueryClient()

  const [groupLimit, setGroupLimit] = useState('')
  const [privateLimit, setPrivateLimit] = useState('')
  const [pauseMessage, setPauseMessage] = useState('')
  const [error, setError] = useState('')

  const quotaQuery = useQuery({
    queryKey: CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.tenantInstance(adapterKey, instanceKey),
    queryFn: () => consoleChannelQuotaApi.getTenantInstanceQuota(adapterKey, instanceKey),
    staleTime: 15_000,
    retry: false,
  })

  useEffect(() => {
    if (!quotaQuery.data) return
    setGroupLimit(
      quotaQuery.data.group_daily_limit === null ? '' : String(quotaQuery.data.group_daily_limit)
    )
    setPrivateLimit(
      quotaQuery.data.private_daily_limit === null
        ? ''
        : String(quotaQuery.data.private_daily_limit)
    )
    setPauseMessage(quotaQuery.data.pause_message ?? '')
  }, [quotaQuery.data])

  const saveMutation = useMutation({
    mutationFn: (body: TenantInstanceQuotaUpdate) =>
      consoleChannelQuotaApi.updateTenantInstanceQuota(adapterKey, instanceKey, body),
    onSuccess: () => {
      notification.success(t('tenantDialog.success'))
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-daily'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-status'] })
      void queryClient.invalidateQueries({
        queryKey: CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.tenantInstance(adapterKey, instanceKey),
      })
    },
    onError: (mutationError: Error) => notification.error(mutationError.message),
  })

  if (quotaQuery.isError) {
    return (
      <QueryErrorState
        compact
        error={quotaQuery.error}
        onRetry={() => void quotaQuery.refetch()}
      />
    )
  }
  if (quotaQuery.isLoading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
        <CircularProgress size={24} />
      </Box>
    )
  }

  const data = quotaQuery.data
  const ceilingGroup = data?.platform_group_daily_limit ?? 0
  const ceilingPrivate = data?.platform_private_daily_limit ?? 0

  const submit = () => {
    const group = parseTenantLimit(groupLimit)
    const privateValue = parseTenantLimit(privateLimit)
    if (group === undefined || privateValue === undefined) {
      setError(t('tenantDialog.invalid'))
      return
    }
    // 上限 0 表示平台方不限；这里只是提前提示，最终以后端 400 为准
    if (group !== null && ceilingGroup > 0 && group > ceilingGroup) {
      setError(t('tenantDialog.overCeiling', { value: formatQuotaInt(ceilingGroup) }))
      return
    }
    if (privateValue !== null && ceilingPrivate > 0 && privateValue > ceilingPrivate) {
      setError(t('tenantDialog.overCeiling', { value: formatQuotaInt(ceilingPrivate) }))
      return
    }
    if (pauseMessage.length > MAX_PAUSE_MESSAGE_LENGTH) {
      setError(t('policyDialog.pauseTooLong', { max: MAX_PAUSE_MESSAGE_LENGTH }))
      return
    }
    setError('')
    saveMutation.mutate({
      group_daily_limit: group,
      private_daily_limit: privateValue,
      pause_message: pauseMessage.trim() ? pauseMessage : null,
    })
  }

  return (
    <Stack spacing={1.5}>
      <Typography variant="caption" color="text.secondary">
        {t('tenantDialog.unit', { unit: channelQuotaUnitLabel(t, data?.unit ?? 'replies') })}
      </Typography>
      <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap>
        <TextField
          size="small"
          type="number"
          label={t('tenantDialog.groupLimit')}
          value={groupLimit}
          helperText={t('tenantDialog.platformCeiling', { value: formatQuotaLimit(t, ceilingGroup) })}
          slotProps={{ htmlInput: { min: 1, step: 1 } }}
          onChange={event => setGroupLimit(event.target.value)}
          sx={{ maxWidth: 220 }}
        />
        <TextField
          size="small"
          type="number"
          label={t('tenantDialog.privateLimit')}
          value={privateLimit}
          helperText={t('tenantDialog.platformCeiling', {
            value: formatQuotaLimit(t, ceilingPrivate),
          })}
          slotProps={{ htmlInput: { min: 1, step: 1 } }}
          onChange={event => setPrivateLimit(event.target.value)}
          sx={{ maxWidth: 220 }}
        />
      </Stack>
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
      <Box>
        <ActionButton tone="primary" size="small" onClick={submit} disabled={saveMutation.isPending}>
          {saveMutation.isPending ? <CircularProgress size={16} /> : t('actions.save')}
        </ActionButton>
      </Box>
    </Stack>
  )
}

export default function InstanceChannelQuotaSection({
  adapterKey,
  instanceKey,
}: {
  adapterKey: string
  instanceKey: string
}) {
  const { t } = useTranslation('console-channel-quota')
  const { isPlatformOwner, isTenantAdmin, isLoading: isIdentityLoading } = useIdentity()

  // 身份没确认之前 `isPlatformOwner` 按兜底角色算成平台方：租户管理员会先看到平台方那块、
  // 并打出一条必然 403 的策略请求。等身份确认了再画，与 `useInstanceScope` 的既定写法一致
  if (isIdentityLoading) {
    return null
  }

  return (
    <Box>
      <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
        {t('instanceSection.title')}
      </Typography>
      <Divider sx={{ mb: 1 }} />
      <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1.5 }}>
        {t('instanceSection.subtitle')}
      </Typography>

      {isPlatformOwner && (
        <Box sx={{ mb: isTenantAdmin ? 2 : 0 }}>
          <Typography variant="body2" fontWeight={600} sx={{ mb: 1 }}>
            {t('instanceSection.platformPolicy')}
          </Typography>
          <PlatformPolicyPanel adapterKey={adapterKey} instanceKey={instanceKey} />
        </Box>
      )}

      {isTenantAdmin && (
        <Box>
          <Typography variant="body2" fontWeight={600} sx={{ mb: 0.5 }}>
            {t('instanceSection.myLimit')}
          </Typography>
          <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
            {t('instanceSection.myLimitHint')}
          </Typography>
          <TenantLimitPanel adapterKey={adapterKey} instanceKey={instanceKey} />
        </Box>
      )}
    </Box>
  )
}
