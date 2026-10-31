import { useState } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  Chip,
  Divider,
  Skeleton,
  Typography,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { chatChannelApi, type AnnouncementStatus } from '../../../../services/api/chat-channel'
import TablePaginationStyled from '../../../../components/common/TablePaginationStyled'

const PAGE_SIZE = 10

const STATUS_COLORS: Record<AnnouncementStatus, 'default' | 'info' | 'success' | 'error' | 'warning'> = {
  pending: 'default',
  running: 'info',
  completed: 'success',
  failed: 'error',
  cancelled: 'warning',
}

function AnnouncementHistoryDetail({ announcementId }: { announcementId: number }) {
  const { t } = useTranslation('chat-announcement')
  const { data, isLoading } = useQuery({
    queryKey: ['announcement-detail', announcementId],
    queryFn: () => chatChannelApi.getAnnouncementDetail(announcementId),
  })

  if (isLoading || !data) {
    return <Skeleton variant="rounded" height={80} />
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        {data.content}
      </Typography>

      {data.failure_reason && (
        <Typography variant="caption" color="error">
          {data.failure_reason}
        </Typography>
      )}

      <Divider />

      {data.deliveries.length === 0 ? (
        <Typography variant="caption" color="text.secondary">
          {t('history.noDeliveries')}
        </Typography>
      ) : (
        <Box sx={{ maxHeight: 220, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 0.5 }}>
          {data.deliveries.map(delivery => (
            <Box
              key={`${delivery.chat_key}-${delivery.send_time}`}
              sx={{ display: 'flex', justifyContent: 'space-between', gap: 1 }}
            >
              <Typography variant="caption" noWrap sx={{ minWidth: 0 }}>
                {delivery.channel_name || delivery.chat_key}
              </Typography>
              <Typography variant="caption" color={delivery.ok ? 'success.main' : 'error'}>
                {delivery.ok ? t('result.ok') : delivery.error || t('result.failed')}
              </Typography>
            </Box>
          ))}
        </Box>
      )}
    </Box>
  )
}

export default function AnnouncementHistoryPanel() {
  const { t } = useTranslation('chat-announcement')
  const [page, setPage] = useState(1)
  const [expandedId, setExpandedId] = useState<number | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['announcement-history', page],
    queryFn: () => chatChannelApi.getAnnouncementHistory({ page, page_size: PAGE_SIZE }),
  })

  const items = data?.items ?? []

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, minHeight: 0 }}>
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
        {t('history.title')}
      </Typography>

      {isLoading ? (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
          {Array.from({ length: 3 }).map((_, index) => (
            <Skeleton key={index} variant="rounded" height={56} />
          ))}
        </Box>
      ) : items.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t('history.empty')}
        </Typography>
      ) : (
        <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          {items.map(item => (
            <Accordion
              key={item.id}
              disableGutters
              expanded={expandedId === item.id}
              onChange={(_, expanded) => setExpandedId(expanded ? item.id : null)}
            >
              <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                <Box
                  sx={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: 1,
                    width: '100%',
                    flexWrap: 'wrap',
                  }}
                >
                  <Box sx={{ minWidth: 0, display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Chip
                      size="small"
                      color={STATUS_COLORS[item.status]}
                      label={t(`status.${item.status}`)}
                    />
                    <Typography variant="body2" noWrap sx={{ minWidth: 0 }}>
                      {item.content}
                    </Typography>
                  </Box>
                  <Typography variant="caption" color="text.secondary">
                    {t('history.meta', {
                      time: item.create_time,
                      scope: t(`scope.${item.scope_type}`),
                      success: item.success_count,
                      failure: item.failure_count,
                      total: item.total,
                    })}
                  </Typography>
                </Box>
              </AccordionSummary>
              <AccordionDetails>
                {expandedId === item.id && <AnnouncementHistoryDetail announcementId={item.id} />}
              </AccordionDetails>
            </Accordion>
          ))}
        </Box>
      )}

      <TablePaginationStyled
        component="div"
        count={data?.total ?? 0}
        page={page - 1}
        rowsPerPage={PAGE_SIZE}
        rowsPerPageOptions={[PAGE_SIZE]}
        onPageChange={(_, nextPage) => setPage(nextPage + 1)}
        loading={isLoading}
      />
    </Box>
  )
}
