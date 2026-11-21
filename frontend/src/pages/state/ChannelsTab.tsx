import { useMemo, useState } from 'react'
import {
  Alert,
  Box,
  FormControlLabel,
  Switch,
  Typography,
} from '@mui/material'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import SearchField from '../../components/common/SearchField'
import ServerDataTable, { type ServerDataTableColumn } from '../../components/common/ServerDataTable'
import StatusChip from '../../components/common/StatusChip'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import { isNotFoundError } from '../../services/api/axios'
import {
  stateTableApi,
  type StateChannelItem,
  type StateScope,
} from '../../services/api/stateTable'
import { formatFullTime, formatListTime } from '../../utils/listTime'
import StateDetailDialog from './StateDetailDialog'
import { channelName, enumText } from './stateUtils'

const PAGE_SIZE = 20

/** 「频道状态」标签：一个实例下每个群 / 私聊的状态树入口 */
export default function ChannelsTab({ scope }: { scope: StateScope }) {
  const { t } = useTranslation('state')
  const { t: tCommon } = useTranslation('common')
  const scopeKey = `${scope.adapter_key}|${scope.instance_key}`

  const [search, setSearch] = useState('')
  const [onlyPaused, setOnlyPaused] = useState(false)
  const [page, setPage] = useState(1)
  const [detail, setDetail] = useState<StateChannelItem | null>(null)

  const query = useQuery({
    queryKey: ['state-table', 'channels', scopeKey, page, search, onlyPaused],
    queryFn: () =>
      stateTableApi.listChannels(scope, {
        q: search.trim() || undefined,
        paused: onlyPaused ? true : undefined,
        page,
        size: PAGE_SIZE,
      }),
    retry: false,
    placeholderData: keepPreviousData,
  })

  const columns: ServerDataTableColumn<StateChannelItem>[] = useMemo(
    () => [
      {
        key: 'name',
        header: t('channels.columns.name'),
        width: 220,
        render: row => (
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="body2" noWrap>
              {channelName(row)}
            </Typography>
            {row.channel_name && (
              <Typography variant="caption" color="text.secondary" noWrap display="block">
                {row.chat_key}
              </Typography>
            )}
          </Box>
        ),
      },
      {
        key: 'preset',
        header: t('channels.columns.preset'),
        width: 140,
        render: row => <Typography variant="body2">{row.preset_name || '—'}</Typography>,
      },
      {
        key: 'status',
        header: t('channels.columns.status'),
        width: 160,
        render: row => {
          if (row.consecutive_failures > 0) {
            return (
              <StatusChip
                tone="failed"
                label={t('channels.failures', { count: row.consecutive_failures })}
              />
            )
          }
          if (row.paused) {
            return <StatusChip tone="paused" label={t('channels.paused')} />
          }
          return <StatusChip tone="active" label={t('channels.normal')} />
        },
      },
      {
        key: 'lastUpdate',
        header: t('channels.columns.lastUpdate'),
        width: 220,
        render: row => {
          const updated = row.last_update_at || row.update_time
          return (
            <Box sx={{ minWidth: 0 }}>
              {/* 主行是时间本身，次行才是这次更新的摘要：两行同为灰色小字会分不出主次 */}
              <SimpleTooltip title={formatFullTime(updated)}>
                <span>
                  <Typography variant="body2" noWrap>
                    {formatListTime(updated)}
                  </Typography>
                </span>
              </SimpleTooltip>
              <Typography variant="caption" color="text.secondary" display="block" noWrap>
                {[
                  row.last_source
                    ? enumText(t, 'enums.source', row.last_source)
                    : '',
                  row.last_result
                    ? enumText(t, 'enums.result', row.last_result)
                    : '',
                ]
                  .filter(Boolean)
                  .join(' · ') || '—'}
              </Typography>
            </Box>
          )
        },
      },
    ],
    [t]
  )

  if (isNotFoundError(query.error)) {
    return <Alert severity="info">{t('notEnabled')}</Alert>
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0, flex: 1 }}>
      <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', flexWrap: 'wrap' }}>
        <SearchField
          value={search}
          onChange={value => {
            setSearch(value)
            setPage(1)
          }}
          onClear={() => {
            setSearch('')
            setPage(1)
          }}
          clearAriaLabel={tCommon('actions.clear')}
          placeholder={t('channels.searchPlaceholder')}
          sx={{ flex: 1, minWidth: 200, maxWidth: 320 }}
        />
        <FormControlLabel
          control={
            <Switch
              size="small"
              checked={onlyPaused}
              onChange={event => {
                setOnlyPaused(event.target.checked)
                setPage(1)
              }}
            />
          }
          label={t('channels.onlyPaused')}
        />
      </Box>

      <ServerDataTable
        columns={columns}
        rows={query.data?.items ?? []}
        getRowId={row => row.id}
        total={query.data?.total ?? 0}
        page={page}
        pageSize={PAGE_SIZE}
        onPageChange={setPage}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        onRetry={() => void query.refetch()}
        emptyTitle={t('channels.emptyTitle')}
        emptyHint={t('channels.emptyHint')}
        onRowClick={row => setDetail(row)}
      />

      {detail && (
        <StateDetailDialog
          scope={scope}
          kind="channel"
          target={detail}
          onClose={() => setDetail(null)}
        />
      )}
    </Box>
  )
}
