/**
 * 平台方：实例层 / 频道层策略编辑（两处共用一个对话框）
 *
 * 每一列都有「继承」选项，选中即提交 `null`。后端逐字段继承，`null` 是**有意义的值**
 * （「这一层不设，跟上一层」），所以这里不能用「空就不提交」的写法——那会把「改成继承」
 * 变成「什么都不改」，用户点了保存却没有任何变化。
 *
 * 刻意**不显示**「当前继承到的值」：后端没有下发继承来源（哪一层给的、给的是多少），
 * 前端自己按三层拼一遍就是同一个判据的第二份实现，拼错的方向可能是把额度显示大十倍。
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  CircularProgress,
  MenuItem,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../../components/common/ActionButton'
import NekroDialog from '../../../components/common/NekroDialog'
import QueryErrorState from '../../../components/common/QueryErrorState'
import { useNotification } from '../../../hooks/useNotification'
import {
  CONSOLE_CHANNEL_QUOTA_QUERY_KEYS,
  consoleChannelQuotaApi,
  type ChannelQuotaAction,
  type ChannelQuotaPolicyInfo,
  type ChannelQuotaUnit,
  type ScopedQuotaPolicyUpdate,
} from '../../../services/api/consoleChannelQuota'

/** 提示语长度上限与后端 `MAX_PAUSE_MESSAGE_LENGTH` 一致 */
const MAX_PAUSE_MESSAGE_LENGTH = 200

/** 「继承」在表单里的占位值：空串表示这一列不设 */
const INHERIT = ''

export type ChannelQuotaPolicyScope = 'instance' | 'channel'

interface ChannelQuotaPolicyDialogProps {
  open: boolean
  onClose: () => void
  scope: ChannelQuotaPolicyScope
  /** 实例层必填 */
  adapterKey?: string
  instanceKey?: string
  /** 频道层必填 */
  chatKey?: string
}

interface PolicyForm {
  enabled: string
  unit: string
  groupLimit: string
  privateLimit: string
  action: string
  pauseMessage: string
}

const EMPTY_FORM: PolicyForm = {
  enabled: INHERIT,
  unit: INHERIT,
  groupLimit: INHERIT,
  privateLimit: INHERIT,
  action: INHERIT,
  pauseMessage: '',
}

function formOf(policy: ChannelQuotaPolicyInfo): PolicyForm {
  return {
    enabled: policy.enabled === null ? INHERIT : policy.enabled ? 'on' : 'off',
    unit: policy.unit ?? INHERIT,
    groupLimit: policy.group_daily_limit === null ? INHERIT : String(policy.group_daily_limit),
    privateLimit: policy.private_daily_limit === null ? INHERIT : String(policy.private_daily_limit),
    action: policy.over_limit_action ?? INHERIT,
    pauseMessage: policy.pause_message ?? '',
  }
}

/** 限额输入：空串 = 继承，其余必须是不小于 0 的整数 */
function parseLimit(value: string): number | null | undefined {
  const text = value.trim()
  if (!text) return null
  const parsed = Number(text)
  if (!Number.isInteger(parsed) || parsed < 0) return undefined
  return parsed
}

export default function ChannelQuotaPolicyDialog({
  open,
  onClose,
  scope,
  adapterKey = '',
  instanceKey = '',
  chatKey = '',
}: ChannelQuotaPolicyDialogProps) {
  const { t } = useTranslation('console-channel-quota')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const [form, setForm] = useState<PolicyForm>(EMPTY_FORM)
  const [error, setError] = useState('')

  const policyQuery = useQuery({
    queryKey:
      scope === 'instance'
        ? CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.instancePolicy(adapterKey, instanceKey)
        : CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.channelPolicy(chatKey),
    queryFn: () =>
      scope === 'instance'
        ? consoleChannelQuotaApi.getInstancePolicy(adapterKey, instanceKey)
        : consoleChannelQuotaApi.getChannelPolicy(chatKey),
    enabled: open && (scope === 'instance' ? Boolean(adapterKey && instanceKey) : Boolean(chatKey)),
    staleTime: 0,
    retry: false,
  })

  // 打开或换目标时按后端值回填；依赖 data 而不是 queryKey，重新取回后也要跟着刷新
  useEffect(() => {
    if (!open) return
    if (policyQuery.data) {
      setForm(formOf(policyQuery.data))
      setError('')
    }
  }, [open, policyQuery.data])

  const saveMutation = useMutation({
    mutationFn: (body: ScopedQuotaPolicyUpdate) =>
      scope === 'instance'
        ? consoleChannelQuotaApi.updateInstancePolicy(adapterKey, instanceKey, body)
        : consoleChannelQuotaApi.updateChannelPolicy(chatKey, body),
    onSuccess: () => {
      notification.success(t('policyDialog.saveSuccess'))
      // 列表与详情读的是同一份策略，一处改完全部失效
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-daily'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-status'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-history'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-policy-channel'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-policy-instance'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-tenant-channel'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-tenant-instance'] })
      onClose()
    },
    onError: (mutationError: Error) => notification.error(mutationError.message),
  })

  const buildBody = (): ScopedQuotaPolicyUpdate | null => {
    const groupLimit = parseLimit(form.groupLimit)
    const privateLimit = parseLimit(form.privateLimit)
    if (groupLimit === undefined || privateLimit === undefined) {
      setError(t('policyDialog.limitInvalid'))
      return null
    }
    if (form.pauseMessage.length > MAX_PAUSE_MESSAGE_LENGTH) {
      setError(t('policyDialog.pauseTooLong', { max: MAX_PAUSE_MESSAGE_LENGTH }))
      return null
    }
    return {
      enabled: form.enabled === INHERIT ? null : form.enabled === 'on',
      unit: form.unit === INHERIT ? null : (form.unit as ChannelQuotaUnit),
      group_daily_limit: groupLimit,
      private_daily_limit: privateLimit,
      over_limit_action: form.action === INHERIT ? null : (form.action as ChannelQuotaAction),
      // 空串在后端会被规范成 null（= 继承），这里原样送过去
      pause_message: form.pauseMessage.trim() ? form.pauseMessage : null,
    }
  }

  const title = useMemo(
    () => (scope === 'instance' ? t('policyDialog.titleInstance') : t('policyDialog.titleChannel')),
    [scope, t]
  )

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
          <ActionButton
            tone="primary"
            disabled={saveMutation.isPending || policyQuery.isLoading || policyQuery.isError}
            onClick={() => {
              const body = buildBody()
              if (body) saveMutation.mutate(body)
            }}
          >
            {saveMutation.isPending ? <CircularProgress size={18} /> : t('actions.save')}
          </ActionButton>
        </>
      }
    >
      {policyQuery.isError ? (
        <QueryErrorState
          compact
          error={policyQuery.error}
          onRetry={() => void policyQuery.refetch()}
        />
      ) : policyQuery.isLoading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress />
        </Box>
      ) : (
        <Stack spacing={2}>
          <Typography variant="caption" color="text.secondary">
            {t('policyDialog.inheritHint')}
          </Typography>

          <TextField
            select
            size="small"
            label={t('policyDialog.enabled')}
            value={form.enabled}
            onChange={event => setForm(previous => ({ ...previous, enabled: event.target.value }))}
          >
            <MenuItem value={INHERIT}>{t('policyDialog.inherit')}</MenuItem>
            <MenuItem value="on">{t('policyDialog.enabledOn')}</MenuItem>
            <MenuItem value="off">{t('policyDialog.enabledOff')}</MenuItem>
          </TextField>

          <TextField
            select
            size="small"
            label={t('policyDialog.unit')}
            value={form.unit}
            onChange={event => setForm(previous => ({ ...previous, unit: event.target.value }))}
          >
            <MenuItem value={INHERIT}>{t('policyDialog.inherit')}</MenuItem>
            <MenuItem value="replies">{t('unit.replies')}</MenuItem>
            <MenuItem value="tokens">{t('unit.tokens')}</MenuItem>
          </TextField>

          <TextField
            size="small"
            label={t('policyDialog.groupLimit')}
            value={form.groupLimit}
            placeholder={t('policyDialog.inherit')}
            helperText={t('policyDialog.limitHint')}
            slotProps={{ htmlInput: { min: 0, step: 1 } }}
            onChange={event => setForm(previous => ({ ...previous, groupLimit: event.target.value }))}
          />

          <TextField
            size="small"
            label={t('policyDialog.privateLimit')}
            value={form.privateLimit}
            placeholder={t('policyDialog.inherit')}
            helperText={t('policyDialog.limitHint')}
            slotProps={{ htmlInput: { min: 0, step: 1 } }}
            onChange={event => setForm(previous => ({ ...previous, privateLimit: event.target.value }))}
          />

          <TextField
            select
            size="small"
            label={t('policyDialog.action')}
            value={form.action}
            onChange={event => setForm(previous => ({ ...previous, action: event.target.value }))}
          >
            <MenuItem value={INHERIT}>{t('policyDialog.inherit')}</MenuItem>
            <MenuItem value="alert">{t('action.alert')}</MenuItem>
            <MenuItem value="pause">{t('action.pause')}</MenuItem>
          </TextField>

          <TextField
            multiline
            minRows={2}
            size="small"
            label={t('policyDialog.pauseMessage')}
            value={form.pauseMessage}
            helperText={t('policyDialog.pauseMessageHint', { max: MAX_PAUSE_MESSAGE_LENGTH })}
            onChange={event =>
              setForm(previous => ({ ...previous, pauseMessage: event.target.value }))
            }
          />
          <Typography variant="caption" color="text.secondary" align="right">
            {t('global.charCount', {
              value: form.pauseMessage.length,
              max: MAX_PAUSE_MESSAGE_LENGTH,
            })}
          </Typography>

          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      )}
    </NekroDialog>
  )
}
