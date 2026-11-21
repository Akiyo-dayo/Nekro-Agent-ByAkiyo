import { useEffect, useState } from 'react'
import {
  Box,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Typography,
} from '@mui/material'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../../components/common/ActionButton'
import type { InstanceOption } from '../../../services/api/console'
import {
  chatChannelApi,
  type BotMembershipCheckResult,
} from '../../../services/api/chat-channel'
import { CHAT_CHANNEL_LIST_KEY } from '../queryKeys'

interface BotMembershipCheckDialogProps {
  open: boolean
  onClose: () => void
  instances: InstanceOption[]
  /** 有当前作用域实例时只核对它 */
  currentInstanceKey: string | null
}

interface CheckRowState {
  loading: boolean
  result?: BotMembershipCheckResult
  error?: string
}

export default function BotMembershipCheckDialog({
  open,
  onClose,
  instances,
  currentInstanceKey,
}: BotMembershipCheckDialogProps) {
  const { t } = useTranslation('chat-channel')
  const queryClient = useQueryClient()
  const [rows, setRows] = useState<Record<string, CheckRowState>>({})

  // 每次重新打开都清掉上一次的结果，避免把旧结论当成这次核对的
  useEffect(() => {
    if (open) {
      setRows({})
    }
  }, [open])

  // 有当前作用域实例时只列它；作用域是别的适配器时没有可核对的 OneBot 实例，
  // 这时退回列全部 OneBot 实例，而不是给一个空对话框。
  const scopedByCurrent = currentInstanceKey
    ? instances.filter(instance => instance.instance_key === currentInstanceKey)
    : instances
  const scopedInstances = scopedByCurrent.length > 0 ? scopedByCurrent : instances

  const describeResult = (result: BotMembershipCheckResult): string => {
    switch (result.outcome) {
      case 'checked':
        return t('botMembership.check.results.checked', {
          groups: result.group_count,
          channels: result.channel_count,
          notInGroup: result.not_in_group_count,
          marked: result.newly_marked,
          cleared: result.cleared,
        })
      case 'offline':
        return t('botMembership.check.results.offline')
      case 'call_failed':
        return t('botMembership.check.results.call_failed', { message: result.message })
      case 'empty':
        return t('botMembership.check.results.empty')
      case 'malformed':
        return t('botMembership.check.results.malformed')
      case 'incomplete':
        return t('botMembership.check.results.incomplete', { message: result.message })
      default:
        return result.message
    }
  }

  const runCheck = async (instance: InstanceOption) => {
    setRows(previous => ({
      ...previous,
      [instance.instance_key]: { loading: true },
    }))
    try {
      const result = await chatChannelApi.checkBotMembership({
        instance_key: instance.instance_key,
        adapter_key: instance.adapter_key,
      })
      setRows(previous => ({
        ...previous,
        [instance.instance_key]: { loading: false, result },
      }))
      if (result.outcome === 'checked') {
        void queryClient.invalidateQueries({ queryKey: [CHAT_CHANNEL_LIST_KEY] })
      }
    } catch (cause) {
      setRows(previous => ({
        ...previous,
        [instance.instance_key]: {
          loading: false,
          error: cause instanceof Error ? cause.message : String(cause),
        },
      }))
    }
  }

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>{t('botMembership.check.dialogTitle')}</DialogTitle>
      <DialogContent>
        <Typography variant="body2" color="textSecondary" sx={{ mb: 2 }}>
          {t('botMembership.check.dialogHint')}
        </Typography>
        <Stack spacing={2}>
          {scopedInstances.map(instance => {
            const row = rows[instance.instance_key]
            return (
              <Box key={instance.instance_key}>
                <Stack
                  direction="row"
                  spacing={1}
                  alignItems="center"
                  justifyContent="space-between"
                  flexWrap="wrap"
                  useFlexGap
                >
                  <Box sx={{ minWidth: 0 }}>
                    <Typography variant="body2" className="font-medium" noWrap>
                      {instance.display_name || instance.instance_key}
                    </Typography>
                    <Typography variant="caption" color="textSecondary" noWrap>
                      {instance.instance_key} · {instance.status}
                    </Typography>
                  </Box>
                  <ActionButton
                    size="small"
                    tone="primary"
                    disabled={row?.loading}
                    onClick={() => void runCheck(instance)}
                  >
                    {row?.loading ? <CircularProgress size={16} /> : t('botMembership.check.run')}
                  </ActionButton>
                </Stack>
                {row?.result && (
                  <Typography variant="caption" color="textSecondary" sx={{ display: 'block', mt: 0.5 }}>
                    {describeResult(row.result)}
                  </Typography>
                )}
                {row?.error && (
                  <Typography variant="caption" color="error" sx={{ display: 'block', mt: 0.5 }}>
                    {row.error}
                  </Typography>
                )}
              </Box>
            )
          })}
        </Stack>
      </DialogContent>
      <DialogActions>
        <ActionButton onClick={onClose}>{t('botMembership.check.close')}</ActionButton>
      </DialogActions>
    </Dialog>
  )
}
