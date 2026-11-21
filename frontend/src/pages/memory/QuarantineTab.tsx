import { useMemo, useState } from 'react'
import {
  Box,
  Chip,
  Divider,
  Menu,
  MenuItem,
  Stack,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import MoreHorizRoundedIcon from '@mui/icons-material/MoreHorizRounded'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import IconActionButton from '../../components/common/IconActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../../components/common/ServerDataTable'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import DeleteConfirmDialog from '../../components/memory/DeleteConfirmDialog'
import MemoryItemDialog from '../../components/memory/MemoryItemDialog'
import MemoryNotFoundState from '../../components/memory/MemoryNotFoundState'
import { MemoryKindChip } from '../../components/memory/MemoryStatusChips'
import ScopeFilterBar from '../../components/memory/ScopeFilterBar'
import { formatFullDateTime, formatListTime } from '../../components/memory/format'
import {
  DEFAULT_MEMORY_PAGE_SIZE,
  MEMORY_PAGE_SIZE_OPTIONS,
  MEMORY_TAB_FILTER_FIELDS,
  filtersFromSearchParams,
  filtersToQuarantineQuery,
  filtersToSearchParams,
  parsePageSize,
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
import { CHIP_VARIANTS } from '../../theme/variants'

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

const FIELDS: Array<keyof MemoryFilterState> = [...MEMORY_TAB_FILTER_FIELDS.quarantine]

const QUARANTINE_REASON_KEYS: Record<string, string> = {
  manual: 'quarantineReason.manual',
  auto_blocked: 'quarantineReason.auto_blocked',
  dedup: 'quarantineReason.dedup',
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

interface ActionMenuState {
  anchorEl: HTMLElement
  row: QuarantineListItem
}

export default function QuarantineTab({ scope, isScopeReady }: QuarantineTabProps) {
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('memory')
  const { t: tCommon } = useTranslation('common')
  const [searchParams, setSearchParams] = useSearchParams()

  const filters = useMemo(() => filtersFromSearchParams(searchParams), [searchParams])
  const pageSize = parsePageSize(searchParams.get('pageSize'))
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null)
  const [actionMenu, setActionMenu] = useState<ActionMenuState | null>(null)
  const [detailTarget, setDetailTarget] = useState<{
    memoryType: MemoryType
    id: number
  } | null>(null)

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['memory'] })
  }

  const writeQuery = (
    nextFilters: MemoryFilterState,
    nextPageSize: number = pageSize
  ) => {
    const params = filtersToSearchParams(nextFilters)
    if (nextPageSize !== DEFAULT_MEMORY_PAGE_SIZE) {
      params.set('pageSize', String(nextPageSize))
    }
    setSearchParams(params, { replace: true })
  }

  const listQuery = useQuery({
    queryKey: ['memory', 'quarantine', scope, filters, pageSize],
    queryFn: () =>
      memoryApi.listQuarantine({
        ...filtersToQuarantineQuery(filters, pageSize),
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

  const reasonColor = (reason: string) => {
    if (reason === 'manual') return theme.palette.info.main
    if (reason === 'auto_blocked') return theme.palette.warning.main
    return theme.palette.text.secondary
  }

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

  const pageSizeSelect = (
    <FilterSelect
      label={t('filters.pageSize')}
      value={String(pageSize)}
      fullWidth={false}
      options={MEMORY_PAGE_SIZE_OPTIONS.map(size => ({
        value: String(size),
        label: t('filters.pageSizeOption', { count: size }),
      }))}
      onChange={value => writeQuery({ ...filters, page: 1 }, parsePageSize(value))}
      sx={{ width: 150 }}
    />
  )

  const columns: ServerDataTableColumn<QuarantineListItem>[] = [
    {
      key: 'type',
      header: t('columns.type'),
      width: 140,
      // 段落行带知识类型，与「段落 / 事件」并排；事件行没有这一项，不占位
      render: row => (
        <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" useFlexGap>
          <MemoryKindChip memoryType={row.memory_type} />
          {row.knowledge_type ? (
            <Chip
              size="small"
              label={t(`knowledge.${row.knowledge_type}`, { defaultValue: row.knowledge_type })}
              sx={CHIP_VARIANTS.base(true)}
            />
          ) : null}
        </Stack>
      ),
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
      width: 170,
      hideOnMobile: true,
      render: row =>
        row.quarantine_reason ? (
          // 被拦次数并进原因的 tooltip；没被拦过就不弹一个「0」
          <SimpleTooltip
            title={
              row.blocked_round_hits > 0
                ? t('quarantineReason.hitsTooltip', { count: row.blocked_round_hits })
                : ''
            }
          >
            <Chip
              size="small"
              label={reasonLabel(row.quarantine_reason)}
              sx={CHIP_VARIANTS.getCustomColorChip(reasonColor(row.quarantine_reason), true)}
            />
          </SimpleTooltip>
        ) : (
          <Typography variant="body2" color="text.secondary">
            —
          </Typography>
        ),
    },
    {
      key: 'quarantinedAt',
      header: t('columns.quarantinedAt'),
      width: 150,
      hideOnMobile: true,
      render: row => (
        <SimpleTooltip title={formatFullDateTime(row.quarantined_at)}>
          <Typography variant="body2" noWrap>
            {formatListTime(row.quarantined_at)}
          </Typography>
        </SimpleTooltip>
      ),
    },
    {
      key: 'actions',
      header: t('columns.actions'),
      width: 130,
      align: 'right',
      render: row => (
        <Stack
          direction="row"
          spacing={0.5}
          justifyContent="flex-end"
          alignItems="center"
        >
          <ActionButton
            tone="ghost"
            size="small"
            disabled={restoreMutation.isPending}
            onClick={event => {
              event.stopPropagation()
              restoreMutation.mutate(row)
            }}
          >
            {t('actions.restore')}
          </ActionButton>
          <IconActionButton
            tone="subtle"
            size="small"
            aria-label={tCommon('actions.more')}
            onClick={event => {
              event.stopPropagation()
              setActionMenu({ anchorEl: event.currentTarget, row })
            }}
          >
            <MoreHorizRoundedIcon fontSize="small" />
          </IconActionButton>
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
        pageSize={pageSize}
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
        paginationExtra={pageSizeSelect}
      />

      <Menu
        anchorEl={actionMenu?.anchorEl ?? null}
        open={Boolean(actionMenu)}
        onClose={() => setActionMenu(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        <MenuItem
          onClick={() => {
            const row = actionMenu?.row
            setActionMenu(null)
            if (!row) return
            setDeleteTarget({
              memoryType: memoryTypeOf(row.memory_type),
              id: row.id,
              hard: false,
            })
          }}
        >
          <Typography variant="body2">{t('actions.delete')}</Typography>
        </MenuItem>
        <Divider sx={{ my: 0.5 }} />
        <MenuItem
          sx={{ color: 'error.main' }}
          onClick={() => {
            const row = actionMenu?.row
            setActionMenu(null)
            if (!row) return
            setDeleteTarget({
              memoryType: memoryTypeOf(row.memory_type),
              id: row.id,
              hard: true,
            })
          }}
        >
          <Typography variant="body2" color="inherit">
            {t('actions.hardDelete')}
          </Typography>
        </MenuItem>
      </Menu>

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
