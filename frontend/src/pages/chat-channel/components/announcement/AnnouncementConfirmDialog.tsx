import {
  Alert,
  Box,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  Typography,
} from '@mui/material'
import SendRoundedIcon from '@mui/icons-material/SendRounded'
import { useTranslation } from 'react-i18next'
import type { AnnouncementPreview } from '../../../../services/api/chat-channel'
import ActionButton from '../../../../components/common/ActionButton'

interface AnnouncementConfirmDialogProps {
  open: boolean
  message: string
  preview: AnnouncementPreview | null
  loading: boolean
  submitting: boolean
  onClose: () => void
  onConfirm: () => void
}

export default function AnnouncementConfirmDialog({
  open,
  message,
  preview,
  loading,
  submitting,
  onClose,
  onConfirm,
}: AnnouncementConfirmDialogProps) {
  const { t } = useTranslation('chat-announcement')

  const blocked = !preview || preview.channel_count === 0 || preview.exceeds_limit

  return (
    <Dialog open={open} onClose={submitting ? undefined : onClose} fullWidth maxWidth="sm">
      <DialogTitle>{t('confirm.title')}</DialogTitle>
      <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
        {loading || !preview ? (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, py: 2 }}>
            <CircularProgress size={20} />
            <Typography variant="body2">{t('confirm.loading')}</Typography>
          </Box>
        ) : (
          <>
            <Alert severity={preview.exceeds_limit ? 'error' : 'warning'} variant="outlined">
              {preview.exceeds_limit
                ? t('confirm.exceedsLimit', { max: preview.max_channels })
                : t('confirm.impact', {
                    instances: preview.instance_count,
                    channels: preview.channel_count,
                  })}
            </Alert>

            {preview.instances.length > 0 && (
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
                {preview.instances.map(instance => (
                  <Box
                    key={`${instance.adapter_key}::${instance.instance_key}`}
                    sx={{ display: 'flex', justifyContent: 'space-between', gap: 1 }}
                  >
                    <Typography variant="body2" noWrap>
                      {instance.display_name}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                      {t('scope.instanceChannels', { count: instance.channel_count })}
                    </Typography>
                  </Box>
                ))}
              </Box>
            )}

            <Divider />

            <Typography variant="caption" color="text.secondary">
              {t('confirm.messagePreview')}
            </Typography>
            <Box
              sx={{
                border: theme => `1px solid ${theme.palette.divider}`,
                borderRadius: 2,
                px: 1.5,
                py: 1,
                maxHeight: 180,
                overflowY: 'auto',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              <Typography variant="body2">{message}</Typography>
            </Box>

            <Typography variant="caption" color="text.secondary">
              {t('confirm.irreversible')}
            </Typography>
          </>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2, gap: 1 }}>
        <ActionButton tone="ghost" onClick={onClose} disabled={submitting}>
          {t('confirm.cancel')}
        </ActionButton>
        <ActionButton
          tone="danger"
          startIcon={<SendRoundedIcon />}
          onClick={onConfirm}
          disabled={loading || blocked || submitting}
        >
          {t('confirm.submit')}
        </ActionButton>
      </DialogActions>
    </Dialog>
  )
}
