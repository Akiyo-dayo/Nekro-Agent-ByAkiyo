import { useState } from 'react'
import { Box, Chip, Stack, Typography, useTheme } from '@mui/material'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import QueryErrorState from '../common/QueryErrorState'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../common/ServerDataTable'
import ScopeFilterBar from './ScopeFilterBar'
import SummaryDialog from './SummaryDialog'
import SummarySourceChip from './SummarySourceChip'
import MemoryNotFoundState from './MemoryNotFoundState'
import { formatShortDateTime, truncateText } from './format'
import { channelNameOf, presetNameOf } from './scopeLabels'
import { useMemoryScopeOptions } from './useMemoryScopeOptions'
import {
  DEFAULT_MEMORY_FILTERS,
  DEFAULT_MEMORY_PAGE_SIZE,
  type MemoryFilterState,
} from './filters'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { isNotFoundError } from '../../services/api/axios'
import {
  memoryApi,
  type MemoryScopeParams,
  type SummaryListEntry,
} from '../../services/api/memory'
import { CHIP_VARIANTS } from '../../theme/variants'

/**
 * 频道摘要列表
 *
 * 每个「频道 + 人设」一行，展示最新一版与版本数；点开某一行进详情。
 *
 * 筛选状态可以由调用方受控（记忆页把它放在 URL 里），也可以由组件自己持有
 * （频道详情的摘要页固定在当前频道，只需要一个人设下拉）。固定频道时不画频道筛选
 * 与频道列，因为那一列每行都一样。
 */

interface SummariesPanelProps {
  scope: MemoryScopeParams
  fixedChatKey?: string
  filters?: MemoryFilterState
  onFiltersChange?: (next: MemoryFilterState) => void
}

const SUMMARIES_FIELDS: Array<keyof MemoryFilterState> = ['chatKey', 'presetId']
const FIXED_CHANNEL_FIELDS: Array<keyof MemoryFilterState> = ['presetId']

export default function SummariesPanel({
  scope,
  fixedChatKey,
  filters: controlledFilters,
  onFiltersChange,
}: SummariesPanelProps) {
  const theme = useTheme()
  const { t } = useTranslation('memory')
  const queryClient = useQueryClient()
  const { isScopeReady } = useInstanceScope()
  const { data: options } = useMemoryScopeOptions(scope)

  const [internalFilters, setInternalFilters] = useState<MemoryFilterState>({
    ...DEFAULT_MEMORY_FILTERS,
  })
  const [target, setTarget] = useState<{ chatKey: string; presetId: number } | null>(null)

  const filters = controlledFilters ?? internalFilters
  const updateFilters = onFiltersChange ?? setInternalFilters
  const effectiveChatKey = fixedChatKey ?? (filters.chatKey || undefined)

  const listQuery = useQuery({
    queryKey: [
      'memory',
      'summaries',
      scope,
      effectiveChatKey ?? '',
      filters.presetId,
      filters.page,
    ],
    queryFn: () =>
      memoryApi.listSummaries({
        chat_key: effectiveChatKey,
        preset_id: filters.presetId ?? undefined,
        page: filters.page,
        page_size: DEFAULT_MEMORY_PAGE_SIZE,
        ...scope,
      }),
    enabled: isScopeReady,
    retry: false,
    placeholderData: keepPreviousData,
  })

  const columns: ServerDataTableColumn<SummaryListEntry>[] = []

  if (!fixedChatKey) {
    columns.push({
      key: 'channel',
      header: t('summaries.columns.channel'),
      render: row => (
        <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>
          {channelNameOf(options, row.chat_key)}
        </Typography>
      ),
    })
  }

  columns.push(
    {
      key: 'preset',
      header: t('summaries.columns.preset'),
      render: row => (
        <Typography variant="body2">
          {presetNameOf(options, row.preset_id, t('preset.none'), t('preset.consoleOnly'))}
        </Typography>
      ),
    },
    {
      key: 'latest',
      header: t('summaries.columns.latest'),
      // 用生成时间称呼最新一版（「11-11 23:10 生成」），版号只在悬停提示里
      render: row => (
        <Stack direction="row" spacing={0.5} alignItems="center" sx={{ flexWrap: 'wrap' }}>
          <Typography
            variant="body2"
            title={t('summaries.latestVersion', { version: row.version })}
          >
            {t('summaries.generatedAt', { time: formatShortDateTime(row.create_time) })}
          </Typography>
          <SummarySourceChip source={row.source} />
        </Stack>
      ),
    },
    {
      key: 'content',
      header: t('summaries.columns.content'),
      width: 260,
      render: row => (
        <Typography variant="body2" noWrap sx={{ maxWidth: 360 }}>
          {truncateText(row.content, 80)}
        </Typography>
      ),
    },
    {
      key: 'versionCount',
      header: t('summaries.columns.versionCount'),
      hideOnMobile: true,
      render: row => (
        <Typography variant="body2" color={row.version_count > 1 ? 'text.primary' : 'text.secondary'}>
          {row.version_count > 1
            ? t('summaries.historyCount', { count: row.version_count - 1 })
            : t('summaries.noHistory')}
        </Typography>
      ),
    },
    {
      key: 'status',
      header: t('summaries.columns.status'),
      render: row =>
        row.paused ? (
          <Chip
            size="small"
            label={t('summaries.pausedChip')}
            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.warning.main, true)}
          />
        ) : (
          <Typography variant="caption" color="text.secondary">
            —
          </Typography>
        ),
    }
  )

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['memory', 'summaries'] })
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0 }}>
      <ScopeFilterBar
        value={filters}
        onChange={updateFilters}
        fields={fixedChatKey ? FIXED_CHANNEL_FIELDS : SUMMARIES_FIELDS}
        scope={scope}
      />

      <ServerDataTable
        columns={columns}
        rows={listQuery.data?.items ?? []}
        getRowId={row => `${row.chat_key}::${row.preset_id}`}
        total={listQuery.data?.total ?? 0}
        page={filters.page}
        pageSize={DEFAULT_MEMORY_PAGE_SIZE}
        onPageChange={page => updateFilters({ ...filters, page })}
        loading={listQuery.isPending}
        fetching={listQuery.isFetching}
        error={listQuery.error}
        onRetry={() => void listQuery.refetch()}
        renderError={error =>
          isNotFoundError(error) ? (
            <MemoryNotFoundState compact />
          ) : (
            <QueryErrorState
              error={error}
              onRetry={() => void listQuery.refetch()}
              compact
            />
          )
        }
        emptyTitle={t('empty.summariesTitle')}
        emptyHint={t('empty.summariesHint')}
        onRowClick={row => setTarget({ chatKey: row.chat_key, presetId: row.preset_id })}
      />

      {target && (
        <SummaryDialog
          chatKey={target.chatKey}
          presetId={target.presetId}
          scope={scope}
          onClose={() => setTarget(null)}
          onChanged={invalidate}
        />
      )}
    </Box>
  )
}
