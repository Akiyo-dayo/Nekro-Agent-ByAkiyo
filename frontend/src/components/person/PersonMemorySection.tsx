import { Box, Card, Chip, CircularProgress, Stack, Typography, useTheme } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import ActionButton from '../common/ActionButton'
import { withDeepLinkInstance } from '../../hooks/useDeepLinkInstance'
import { memoryPath } from '../../router/routes'
import { memoryApi, type ParagraphListItem } from '../../services/api/memory'
import { CARD_VARIANTS, CHIP_VARIANTS } from '../../theme/variants'
import { formatListTime } from '../../utils/listTime'

/**
 * 一个人在记忆里最近提到的几段
 *
 * 人物卡的第四段与好感详情共用：只读、最多 5 行，行点开记忆条目，段尾去记忆页看这个人的全部。
 * 请求失败与别的段分开算：这里只在自己的卡片里给一行错误和「重试」，不把整张卡带崩。
 */

interface PersonMemorySectionProps {
  account: string
  adapterKey: string
  instanceKey: string
  /** 跳走之前收起宿主（人物卡或好感详情），免得回来时它还开着 */
  onNavigate?: () => void
}

const MEMORY_PREVIEW_SIZE = 5

export default function PersonMemorySection({
  account,
  adapterKey,
  instanceKey,
  onNavigate,
}: PersonMemorySectionProps) {
  const theme = useTheme()
  const navigate = useNavigate()
  const { t } = useTranslation('person')
  const { t: tMemory } = useTranslation('memory')

  const query = useQuery({
    queryKey: ['person-card', 'memory', adapterKey, instanceKey, account],
    queryFn: () =>
      memoryApi.listParagraphs({
        person: account,
        instance_key: instanceKey,
        adapter_key: adapterKey,
        page: 1,
        page_size: MEMORY_PREVIEW_SIZE,
        sort: 'create_time',
        order: 'desc',
      }),
    enabled: Boolean(account && instanceKey && adapterKey),
    retry: false,
  })

  const rows: ParagraphListItem[] = query.data?.items ?? []
  const total = query.data?.total ?? 0

  const openParagraph = (row: ParagraphListItem) => {
    onNavigate?.()
    navigate(
      withDeepLinkInstance(
        memoryPath('items', { item: String(row.id) }),
        row.instance_key || instanceKey
      )
    )
  }

  const openAll = () => {
    onNavigate?.()
    navigate(withDeepLinkInstance(memoryPath('items', { person: account }), instanceKey))
  }

  return (
    <Card
      sx={{
        ...(CARD_VARIANTS.default.styles as object),
        p: 1.5,
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
      }}
    >
      <Stack direction="row" spacing={1} alignItems="center" sx={{ minWidth: 0 }}>
        <Typography variant="subtitle2" fontWeight={700}>
          {t('memory.title')}
        </Typography>
        {query.isSuccess && (
          <Typography variant="caption" color="text.secondary">
            {t('memory.total', { count: total })}
          </Typography>
        )}
      </Stack>

      {query.isPending ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 1 }}>
          <CircularProgress size={20} />
        </Box>
      ) : query.isError ? (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ minWidth: 0 }}>
          <Typography variant="body2" color="error.main" noWrap sx={{ flex: 1, minWidth: 0 }}>
            {query.error instanceof Error && query.error.message
              ? query.error.message
              : t('error')}
          </Typography>
          <ActionButton tone="ghost" size="small" onClick={() => void query.refetch()}>
            {t('retry')}
          </ActionButton>
        </Stack>
      ) : total === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t('memory.empty')}
        </Typography>
      ) : (
        <Stack spacing={0.5}>
          {rows.map(row => (
            <Box
              key={row.id}
              role="button"
              tabIndex={0}
              onClick={() => openParagraph(row)}
              onKeyDown={event => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  openParagraph(row)
                }
              }}
              sx={{
                display: 'grid',
                gridTemplateColumns: 'auto auto minmax(0, 1fr)',
                gap: 1,
                alignItems: 'center',
                px: 0.75,
                py: 0.5,
                borderRadius: 1,
                cursor: 'pointer',
                '&:hover': { bgcolor: 'action.hover' },
              }}
            >
              <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: 'nowrap' }}>
                {formatListTime(row.create_time)}
              </Typography>
              <Chip
                size="small"
                label={tMemory(`knowledge.${row.knowledge_type}`, {
                  defaultValue: row.knowledge_type || '—',
                })}
                sx={{
                  ...(CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true) as object),
                  maxWidth: 96,
                }}
              />
              <Typography variant="body2" noWrap title={row.summary || row.content}>
                {row.summary || row.content}
              </Typography>
            </Box>
          ))}
        </Stack>
      )}

      {total > 0 && (
        <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
          <ActionButton tone="ghost" size="small" onClick={openAll}>
            {t('memory.viewAll')}
          </ActionButton>
        </Box>
      )}
    </Card>
  )
}
