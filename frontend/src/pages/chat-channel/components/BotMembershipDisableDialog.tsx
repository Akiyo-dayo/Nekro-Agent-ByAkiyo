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
import { useSnackbar } from 'notistack'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../../components/common/ActionButton'
import type { InstanceOption } from '../../../services/api/console'
import {
  chatChannelApi,
  getChannelDisplayName,
  type BotMembershipDisablePreview,
} from '../../../services/api/chat-channel'
import { CHAT_CHANNEL_LIST_KEY } from '../queryKeys'

interface BotMembershipDisableDialogProps {
  open: boolean
  onClose: () => void
  currentInstance: InstanceOption | null
}

export default function BotMembershipDisableDialog({
  open,
  onClose,
  currentInstance,
}: BotMembershipDisableDialogProps) {
  const { t } = useTranslation('chat-channel')
  const queryClient = useQueryClient()
  const { enqueueSnackbar } = useSnackbar()
  const [preview, setPreview] = useState<BotMembershipDisablePreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const instanceKey = currentInstance?.instance_key
  const adapterKey = currentInstance?.adapter_key

  useEffect(() => {
    if (!open) {
      return
    }
    let cancelled = false
    const loadPreview = async () => {
      setLoading(true)
      setError('')
      setPreview(null)
      try {
        const data = await chatChannelApi.getBotMembershipDisablePreview(
          instanceKey ? { instance_key: instanceKey, adapter_key: adapterKey } : undefined,
        )
        if (!cancelled) {
          setPreview(data)
        }
      } catch (cause) {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : String(cause))
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }
    void loadPreview()
    return () => {
      cancelled = true
    }
  }, [open, instanceKey, adapterKey])

  const confirm = async () => {
    if (!preview || preview.count === 0) {
      return
    }
    setSubmitting(true)
    try {
      const result = await chatChannelApi.disableNotInGroupChannels(
        preview.items.map(item => item.chat_key),
      )
      enqueueSnackbar(
        t('botMembership.disable.done', { disabled: result.disabled, skipped: result.skipped }),
        { variant: 'success' },
      )
      void queryClient.invalidateQueries({ queryKey: [CHAT_CHANNEL_LIST_KEY] })
      onClose()
    } catch (cause) {
      enqueueSnackbar(
        cause instanceof Error ? cause.message : t('botMembership.disable.failed'),
        { variant: 'error' },
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>{t('botMembership.disable.dialogTitle')}</DialogTitle>
      <DialogContent>
        {loading ? (
          <Stack direction="row" spacing={1} alignItems="center">
            <CircularProgress size={18} />
            <Typography variant="body2" color="textSecondary">
              {t('botMembership.disable.loading')}
            </Typography>
          </Stack>
        ) : error ? (
          <Typography variant="body2" color="error">
            {t('botMembership.disable.previewFailed', { message: error })}
          </Typography>
        ) : preview ? (
          <Stack spacing={1.5}>
            {preview.count === 0 ? (
              <Typography variant="body2" color="textSecondary">
                {t('botMembership.disable.empty')}
              </Typography>
            ) : (
              <>
                <Typography variant="body2">
                  {t('botMembership.disable.count', { count: preview.count })}
                </Typography>
                <Box
                  sx={{
                    maxHeight: 240,
                    overflowY: 'auto',
                    border: '1px solid',
                    borderColor: 'divider',
                    borderRadius: 1,
                    p: 1,
                  }}
                >
                  <Stack spacing={0.5}>
                    {preview.items.map(item => (
                      <Typography key={item.chat_key} variant="body2" noWrap>
                        {getChannelDisplayName(item)}
                      </Typography>
                    ))}
                  </Stack>
                </Box>
              </>
            )}
            <Typography variant="caption" color="textSecondary">
              {t('botMembership.disable.manualHint')}
            </Typography>
          </Stack>
        ) : null}
      </DialogContent>
      <DialogActions>
        <ActionButton onClick={onClose}>{t('botMembership.disable.cancel')}</ActionButton>
        <ActionButton
          tone="danger"
          disabled={loading || submitting || !preview || preview.count === 0}
          onClick={() => void confirm()}
        >
          {submitting ? <CircularProgress size={16} /> : t('botMembership.disable.confirm')}
        </ActionButton>
      </DialogActions>
    </Dialog>
  )
}
