import { useMemo, useState } from 'react'
import { Box, Stack, Typography } from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import DeleteForeverIcon from '@mui/icons-material/DeleteForever'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import RestoreIcon from '@mui/icons-material/Restore'
import IconActionButton from '../../components/common/IconActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../../components/common/ServerDataTable'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import DeleteConfirmDialog from '../../components/memory/DeleteConfirmDialog'
import MemoryItemDialog from '../../components/memory/MemoryItemDialog'
import MemoryNotFoundState from '../../components/memory/MemoryNotFoundState'
import { MemoryBlockedHitsChip, MemoryKindChip } from '../../components/memory/MemoryStatusChips'
import ScopeFilterBar from '../../components/memory/ScopeFilterBar'
import { formatDateTime } from '../../components/memory/format'
import {
  DEFAULT_MEMORY_PAGE_SIZE,
  filtersFromSearchParams,
  filtersToQuarantineQuery,
  filtersToSearchParams,
  type MemoryFilterState,
} from '../../components/memory/filters'
import { useMemoryScopeOptions } from '../../components/memory/useMemoryScopeOptions'
import { useNotification } from '../../hooks/useNotification'
import { isNotFoundError } from '../../services/api/axios'
import {
  memoryApi,
  type MemoryScopeParams,
  type MemoryType,
  type QuarantineListItem,
} from '../../services/api/memory'
import { memoryErrorMessage } from '../../components/memory/errorMessage'

/**
 * 「隔离区」标签
 *
 * 隔离是「暂时不注入、不参与检索」，不是删除。所以每一行最显眼的动作是「放回」，
 * 删除与彻底删除退居其后；放回之后向量会重新写入，通知里把这一点说清楚。
 *
 * 筛选只保留频道、人设与分区：隔离区是段落与事件合并后的视图，按人、按类型筛
 * 只会让其中一侧永远为空。
 */

interface QuarantineTabProps {
  scope: MemoryScopeParams
  isScopeReady: boolean
}

const FIELDS: Array<keyof MemoryFilterState> = ['chatKey', 'presetId', 'partition']

const QUARANTINE_REASON_KEYS: Record<string, string> = {
  manual: 'quarantineReason.manual',
  auto_blocked: 'quarantineReason.auto_blocked',
}

/** 隔离区只有段落与事件；其余类型不会出现在这个列表里 */
function memoryTypeOf(value: string): MemoryType {
  return value === 'episode' ? 'episode' : 'paragraph'
}

interface DeleteTarget {
  memoryType: MemoryType
  id: number
  hard: boolean
}

export default function QuarantineTab({ scope, isScopeReady }: QuarantineTabProps) {
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('memory')
  const { t: tCommon } = useTranslation('common')
  const [searchParams, setSearchParams] = useSearchParams()

  const filters = useMemo(() => filtersFromSearchParams(searchParams), [searchParams])
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null)
  const [detailTarget, setDetailTarget] = useState<{
    memoryType: MemoryType
    id: number
  } | null>(null)

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['memory'] })
  }

  const writeQuery = (next: MemoryFilterState) => {
    setSearchParams(filtersToSearchParams(next), { replace: true })
  }

  const listQuery = useQuery({
    queryKey: ['memory', 'quarantine', scope, filters],
    queryFn: () =>
      memoryApi.listQuarantine({
        ...filtersToQuarantineQuery(filters, DEFAULT_MEMORY_PAGE_SIZE),
        ...scope,
      }),
    enabled: isScopeReady,
    retry: false,
  })

  const optionsQuery = useMemoryScopeOptions(scope)
  const channelNames = useMemo(() => {
    const map = new Map<string, string>()
    for (const channel of optionsQuery.data?.channels ?? []) {
      map.set(channel.chat_key, channel.channel_name || channel.chat_key)
    }
    return map
  }, [optionsQuery.data])

  const channelLabel = (chatKey: string | null) =>
    chatKey ? channelNames.get(chatKey) ?? chatKey : '—'

  const reasonLabel = (reason: string) =>
    reason ? t(QUARANTINE_REASON_KEYS[reason] ?? 'quarantineReason.unknown') : '—'

  const restoreMutation = useMutation({
    mutationFn: (row: QuarantineListItem) =>
      memoryApi.actOnItem(memoryTypeOf(row.memory_type), row.id, 'restore'),
    onSuccess: () => {
      notification.success(t('quarantine.restoreSuccess'))
      invalidate()
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const items = listQuery.data?.items ?? []
  const total = listQuery.data?.total ?? 0

  const columns: ServerDataTableColumn<QuarantineListItem>[] = [
    {
      key: 'type',
      header: t('columns.type'),
      width: 90,
      render: row => <MemoryKindChip memoryType={row.memory_type} />,
    },
    {
      key: 'content',
      header: t('columns.content'),
      // 固定布局下百分比会被其余像素宽的列挤没
      width: 280,
      render: row => (
        <Typography variant="body2" noWrap title={row.title_or_summary}>
          {row.title_or_summary || '—'}
        </Typography>
      ),
    },
    {
      key: 'channel',
      header: t('columns.channel'),
      width: 150,
      hideOnMobile: true,
      render: row => (
        <Typography variant="body2" noWrap title={row.origin_chat_key ?? ''}>
          {channelLabel(row.origin_chat_key)}
        </Typography>
      ),
    },
    {
      key: 'reason',
      header: t('columns.quarantineReason'),
      width: 160,
      hideOnMobile: true,
      render: row => (
        <Typography variant="body2" noWrap>
          {reasonLabel(row.quarantine_reason)}
        </Typography>
      ),
    },
    {
      key: 'hits',
      header: t('columns.blockedHits'),
      width: 90,
      align: 'right',
      hideOnMobile: true,
      render: row => <MemoryBlockedHitsChip count={row.blocked_round_hits} />,
    },
    {
      key: 'quarantinedAt',
      header: t('columns.quarantinedAt'),
      width: 170,
      hideOnMobile: true,
      render: row => (
        <Typography variant="body2" noWrap>
          {formatDateTime(row.quarantined_at)}
        </Typography>
      ),
    },
    {
      key: 'actions',
      header: t('columns.actions'),
      width: 150,
      align: 'center',
      render: row => (
        <Stack direction="row" spacing={0.5} justifyContent="center">
          <SimpleTooltip title={t('actions.restore')}>
            <span>
              <IconActionButton
                tone="primary"
                size="small"
                aria-label={t('actions.restore')}
                disabled={restoreMutation.isPending}
                onClick={event => {
                  event.stopPropagation()
                  restoreMutation.mutate(row)
                }}
              >
                <RestoreIcon fontSize="small" />
              </IconActionButton>
            </span>
          </SimpleTooltip>
          <SimpleTooltip title={t('actions.delete')}>
            <span>
              <IconActionButton
                tone="subtle"
                size="small"
                aria-label={t('actions.delete')}
                onClick={event => {
                  event.stopPropagation()
                  setDeleteTarget({
                    memoryType: memoryTypeOf(row.memory_type),
                    id: row.id,
                    hard: false,
                  })
                }}
              >
                <DeleteOutlineIcon fontSize="small" />
              </IconActionButton>
            </span>
          </SimpleTooltip>
          <SimpleTooltip title={t('actions.hardDelete')}>
            <span>
              <IconActionButton
                tone="danger"
                size="small"
                aria-label={t('actions.hardDelete')}
                onClick={event => {
                  event.stopPropagation()
                  setDeleteTarget({
                    memoryType: memoryTypeOf(row.memory_type),
                    id: row.id,
                    hard: true,
                  })
                }}
              >
                <DeleteForeverIcon fontSize="small" />
              </IconActionButton>
            </span>
          </SimpleTooltip>
        </Stack>
      ),
    },
  ]

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0, flex: 1 }}>
      <ScopeFilterBar
        value={filters}
        onChange={next => writeQuery({ ...next, page: 1 })}
        fields={FIELDS}
        scope={scope}
      />

      <ServerDataTable<QuarantineListItem>
        columns={columns}
        rows={items}
        getRowId={row => `${row.memory_type}-${row.id}`}
        total={total}
        page={filters.page}
        pageSize={DEFAULT_MEMORY_PAGE_SIZE}
        onPageChange={page => writeQuery({ ...filters, page })}
        loading={listQuery.isPending}
        fetching={listQuery.isFetching}
        error={listQuery.error}
        onRetry={() => void listQuery.refetch()}
        renderError={error =>
          isNotFoundError(error) ? (
            <MemoryNotFoundState />
          ) : (
            <QueryErrorState error={error} onRetry={() => void listQuery.refetch()} compact />
          )
        }
        emptyTitle={t('empty.quarantineTitle')}
        emptyHint={t('empty.quarantineHint')}
        onRowClick={row =>
          setDetailTarget({ memoryType: memoryTypeOf(row.memory_type), id: row.id })
        }
      />

      <MemoryItemDialog
        memoryType={detailTarget?.memoryType ?? 'paragraph'}
        itemId={detailTarget?.id ?? null}
        scope={scope}
        onClose={() => setDetailTarget(null)}
        onChanged={invalidate}
      />

      {deleteTarget && (
        <DeleteConfirmDialog
          open
          memoryType={deleteTarget.memoryType}
          itemId={deleteTarget.id}
          hard={deleteTarget.hard}
          scope={scope}
          onClose={() => setDeleteTarget(null)}
          onDeleted={() => {
            setDeleteTarget(null)
            invalidate()
          }}
        />
      )}
    </Box>
  )
}
