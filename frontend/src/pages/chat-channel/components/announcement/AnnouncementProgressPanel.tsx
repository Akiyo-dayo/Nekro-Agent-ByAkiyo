import { Alert, Box, Chip, Divider, LinearProgress, Typography } from '@mui/material'
import CancelRoundedIcon from '@mui/icons-material/CancelRounded'
import { useTranslation } from 'react-i18next'
import type { AnnouncementSummary } from '../../../../services/api/chat-channel'
import ActionButton from '../../../../components/common/ActionButton'
import type {
  AnnouncementConnectionState,
  AnnouncementDeliveryLine,
} from './useAnnouncementJob'

interface AnnouncementProgressPanelProps {
  summary: AnnouncementSummary
  deliveries: AnnouncementDeliveryLine[]
  connectionState: AnnouncementConnectionState
  isActive: boolean
  isCancelling: boolean
  onCancel: () => void
  onDismiss: () => void
}

export default function AnnouncementProgressPanel({
  summary,
  deliveries,
  connectionState,
  isActive,
  isCancelling,
  onCancel,
  onDismiss,
}: AnnouncementProgressPanelProps) {
  const { t } = useTranslation('chat-announcement')

  const completed = summary.success_count + summary.failure_count
  const progressValue = Math.round((completed / Math.max(summary.total, 1)) * 100)
  const failedDeliveries = deliveries.filter(line => !line.ok)

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.25 }}>
      <Box
        sx={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 1,
          flexWrap: 'wrap',
        }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
            {t('progress.title')}
          </Typography>
          <Chip size="small" label={t(`status.${summary.status}`)} />
          {isActive && connectionState === 'disconnected' && (
            <Chip size="small" color="warning" label={t('progress.reconnecting')} />
          )}
        </Box>

        {isActive ? (
          <ActionButton
            tone="danger"
            size="small"
            startIcon={<CancelRoundedIcon />}
            onClick={onCancel}
            disabled={isCancelling}
          >
            {t('progress.cancel')}
          </ActionButton>
        ) : (
          <ActionButton tone="ghost" size="small" onClick={onDismiss}>
            {t('progress.dismiss')}
          </ActionButton>
        )}
      </Box>

      <LinearProgress
        variant="determinate"
        value={progressValue}
        sx={{ height: 8, borderRadius: 999 }}
      />

      <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 1, flexWrap: 'wrap' }}>
        <Typography variant="body2">
          {t('progress.count', { completed, total: summary.total })}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {t('progress.summary', {
            success: summary.success_count,
            failure: summary.failure_count,
          })}
        </Typography>
      </Box>

      {summary.failure_reason && (
        <Alert severity="error" variant="outlined">
          {summary.failure_reason}
        </Alert>
      )}

      {isActive && (
        <Typography variant="caption" color="text.secondary">
          {t('progress.backgroundHint')}
        </Typography>
      )}

      {failedDeliveries.length > 0 && (
        <>
          <Divider />
          <Typography variant="caption" color="text.secondary">
            {t('progress.failedList')}
          </Typography>
          <Box sx={{ maxHeight: 140, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 0.5 }}>
            {failedDeliveries.map(line => (
              <Typography key={line.chatKey} variant="caption" color="error">
                {line.channelName || line.chatKey}: {line.error}
              </Typography>
            ))}
          </Box>
        </>
      )}
    </Box>
  )
}
