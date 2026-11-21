import { useMemo, useState } from 'react'
import {
  Box,
  Menu,
  MenuItem,
  Popover,
  Stack,
  Typography,
} from '@mui/material'
import FilterListRoundedIcon from '@mui/icons-material/FilterListRounded'
import MoreHorizRoundedIcon from '@mui/icons-material/MoreHorizRounded'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import IconActionButton from '../../components/common/IconActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import SearchField from '../../components/common/SearchField'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../../components/common/ServerDataTable'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import {
  WorldbookEnabledChip,
  WorldbookReadOnlyChip,
  WorldbookScopeChip,
} from '../../components/worldbook/chips'
import {
  worldbookBindingMissingText,
  worldbookBindingText,
} from '../../components/worldbook/display'
import {
  DEFAULT_WORLDBOOK_PAGE_SIZE,
  worldbookFiltersFromSearchParams,
  worldbookFiltersToQuery,
  worldbookFiltersToSearchParams,
  type WorldbookBooksFilterState,
} from '../../components/worldbook/filters'
import { truncateText } from '../../components/memory/format'
import { DEFAULT_MEMORY_FILTERS, type MemoryFilterState } from '../../components/memory/filters'
import PersonFilterField from '../../components/memory/PersonFilterField'
import { useNotification } from '../../hooks/useNotification'
import { useWorldbookScopeOptions } from '../../components/worldbook/useWorldbookScopeOptions'
import BookFormDialog from '../../components/worldbook/BookFormDialog'
import ImportDialog from '../../components/worldbook/ImportDialog'
import WorldbookNotFoundState from '../../components/worldbook/WorldbookNotFoundState'
import { isNotFoundError } from '../../services/api/axios'
import {
  WORLDBOOK_SCOPE_KINDS,
  worldbookApi,
  type WorldbookBookItem,
  type WorldbookScopeParams,
} from '../../services/api/worldbook'
import { formatFullTime, formatListTime } from '../../utils/listTime'
import { worldbookBookPath } from '../../router/routes'

/**
 * 「书」标签
 *
 * 一个实例下可以有很多本书，筛选条件全部进 query；换任何一个筛选都把页码置回 1，
 * 否则会停在一个已经不存在的页上。
 *
 * 常用筛选只有搜索与作用域；人设/频道/人物/启用收进「更多筛选」，免得工具条先占两行。
 * 状态列直接就是启用 Chip，旁边的「⋯」切换启用，不再单列一列。
 */

interface BooksTabProps {
  scope: WorldbookScopeParams
  isScopeReady: boolean
}

export default function BooksTab({ scope, isScopeReady }: BooksTabProps) {
  const { t } = useTranslation('worldbook')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [advancedAnchor, setAdvancedAnchor] = useState<HTMLElement | null>(null)
  const [statusMenu, setStatusMenu] = useState<{
    anchor: HTMLElement
    book: WorldbookBookItem
  } | null>(null)

  const filters = useMemo(() => worldbookFiltersFromSearchParams(searchParams), [searchParams])
  const { data: options } = useWorldbookScopeOptions(scope)

  const writeQuery = (next: WorldbookBooksFilterState) => {
    setSearchParams(worldbookFiltersToSearchParams(next), { replace: true })
  }

  const update = (patch: Partial<WorldbookBooksFilterState>) => {
    writeQuery({ ...filters, ...patch, page: 1 })
  }

  const listQuery = useQuery({
    queryKey: ['worldbook', 'books', scope, filters],
    queryFn: () => worldbookApi.listBooks({ ...worldbookFiltersToQuery(filters), ...scope }),
    enabled: isScopeReady,
    retry: false,
  })

  const toggleMutation = useMutation({
    mutationFn: (book: WorldbookBookItem) =>
      worldbookApi.updateBook(book.id, { enabled: !book.enabled }),
    onSuccess: updated => {
      setStatusMenu(null)
      notification.success(
        updated.enabled ? t('books.enabledOn') : t('books.enabledOff')
      )
      void queryClient.invalidateQueries({ queryKey: ['worldbook'] })
    },
    onError: (error: unknown) => {
      notification.error(error instanceof Error ? error.message : String(error))
    },
  })

  // 人物候选来自记忆：只借用它的选择器，候选条件跟随这里的频道/人设
  const personFilterState: MemoryFilterState = useMemo(
    () => ({
      ...DEFAULT_MEMORY_FILTERS,
      chatKey: filters.chatKey,
      presetId: filters.presetId,
      person: filters.person,
    }),
    [filters.chatKey, filters.person, filters.presetId]
  )

  const scopeKindOptions = [
    { value: 'all', label: t('books.filters.scopeAll') },
    ...WORLDBOOK_SCOPE_KINDS.map(kind => ({
      value: kind,
      label: t(`scope.${kind}`),
    })),
  ]

  const presetOptions = [
    { value: 'all', label: t('books.filters.presetAll') },
    ...(options?.presets ?? []).map(preset => ({
      value: String(preset.preset_id),
      label: preset.name,
    })),
  ]

  const channelOptions = [
    { value: 'all', label: t('books.filters.channelAll') },
    ...(options?.channels ?? []).map(channel => ({
      value: channel.chat_key,
      label: channel.channel_name || channel.chat_key,
    })),
  ]

  const enabledOptions = [
    { value: 'all', label: t('books.filters.enabledAll') },
    { value: 'enabled', label: t('books.filters.enabledOn') },
    { value: 'disabled', label: t('books.filters.enabledOff') },
  ]

  const advancedCount =
    (filters.presetId !== null ? 1 : 0) +
    (filters.chatKey ? 1 : 0) +
    (filters.person ? 1 : 0) +
    (filters.enabled !== 'all' ? 1 : 0)

  const clearAdvanced = () => {
    update({ presetId: null, chatKey: '', person: '', enabled: 'all' })
  }

  const columns: ServerDataTableColumn<WorldbookBookItem>[] = [
    {
      key: 'name',
      header: t('books.columns.name'),
      width: 260,
      render: row => (
        <Box sx={{ minWidth: 0 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
            <Typography variant="body2" fontWeight={600}>
              {row.name}
            </Typography>
            {row.read_only && <WorldbookReadOnlyChip />}
          </Box>
          {row.description && (
            <Typography variant="caption" color="text.secondary" display="block">
              {truncateText(row.description, 60)}
            </Typography>
          )}
        </Box>
      ),
    },
    {
      key: 'effectiveScope',
      header: t('books.columns.effectiveScope'),
      width: 270,
      render: row => {
        const binding = worldbookBindingText(row)
        const instance = row.instance_key || t('common.globalPreset')
        return (
          <Box sx={{ minWidth: 0 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
              <SimpleTooltip title={instance}>
                <span>
                  <WorldbookScopeChip scopeKind={row.scope_kind} />
                </span>
              </SimpleTooltip>
              {binding && (
                <Typography variant="caption" color="text.secondary" noWrap>
                  {binding}
                </Typography>
              )}
            </Box>
            {row.binding_missing && (
              <Typography variant="caption" display="block" color="warning.main">
                {worldbookBindingMissingText(row.scope_kind, t)}
              </Typography>
            )}
          </Box>
        )
      },
    },
    {
      key: 'entries',
      header: t('books.columns.entries'),
      width: 120,
      align: 'right',
      render: row => (
        <Box>
          <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }}>
            {row.entry_count.toLocaleString()}
          </Typography>
          {row.quarantined_count > 0 && (
            <Typography variant="caption" display="block" color="warning.main">
              {t('books.quarantinedCount', { count: row.quarantined_count })}
            </Typography>
          )}
        </Box>
      ),
    },
    {
      key: 'enabled',
      header: t('books.columns.status'),
      width: 140,
      render: row => (
        <Stack
          direction="row"
          spacing={0.5}
          alignItems="center"
          onClick={event => event.stopPropagation()}
        >
          <WorldbookEnabledChip enabled={row.enabled} />
          <IconActionButton
            size="small"
            aria-label={t('books.toggleStatus')}
            title={t('books.toggleStatus')}
            onClick={event =>
              setStatusMenu({ anchor: event.currentTarget, book: row })
            }
          >
            <MoreHorizRoundedIcon fontSize="small" />
          </IconActionButton>
        </Stack>
      ),
    },
    {
      key: 'updated',
      header: t('books.columns.updatedAt'),
      width: 180,
      hideOnMobile: true,
      render: row => (
        <SimpleTooltip title={formatFullTime(row.update_time)}>
          <span>
            <Typography variant="caption" color="text.secondary">
              {formatListTime(row.update_time)}
            </Typography>
          </span>
        </SimpleTooltip>
      ),
    },
  ]

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, flex: 1, minHeight: 0 }}>
      <Box
        sx={{
          display: 'flex',
          gap: 1,
          flexWrap: 'wrap',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', flex: 1, minWidth: 0 }}>
          <Box sx={{ width: { xs: '100%', sm: 220 } }}>
            <SearchField
              label={t('books.filters.search')}
              placeholder={t('books.filters.searchPlaceholder')}
              value={filters.search}
              onChange={value => update({ search: value })}
              onClear={() => update({ search: '' })}
            />
          </Box>
          <Box sx={{ width: { xs: '100%', sm: 150 } }}>
            <FilterSelect
              label={t('books.filters.scopeKind')}
              value={filters.scopeKind || 'all'}
              options={scopeKindOptions}
              onChange={value =>
                update({
                  scopeKind:
                    value === 'all'
                      ? ''
                      : (value as WorldbookBooksFilterState['scopeKind']),
                })
              }
            />
          </Box>
          {/* 与记忆页同一套：ghost 按钮 + 「(n)」计数，清空放进浮层底部 */}
          <ActionButton
            tone="ghost"
            size="small"
            startIcon={<FilterListRoundedIcon />}
            onClick={event => setAdvancedAnchor(event.currentTarget)}
            sx={{ flexShrink: 0, whiteSpace: 'nowrap', alignSelf: 'center' }}
          >
            {advancedCount > 0
              ? t('books.filters.moreWithCount', { count: advancedCount })
              : t('books.filters.more')}
          </ActionButton>
        </Box>

        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', flexShrink: 0 }}>
          <ActionButton tone="primary" onClick={() => setCreateOpen(true)}>
            {t('books.create')}
          </ActionButton>
          <ActionButton tone="secondary" onClick={() => setImportOpen(true)}>
            {t('books.import')}
          </ActionButton>
        </Box>
      </Box>

      <Popover
        open={advancedAnchor !== null}
        anchorEl={advancedAnchor}
        onClose={() => setAdvancedAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        transformOrigin={{ vertical: 'top', horizontal: 'left' }}
        slotProps={{
          paper: {
            sx: { p: 1.5, width: { xs: 'min(92vw, 340px)', sm: 540 }, maxWidth: '100%' },
          },
        }}
      >
        <Stack spacing={1.5}>
          <Box
            sx={{
              display: 'grid',
              gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))' },
              gap: 1.5,
              alignItems: 'center',
            }}
          >
          <Box>
            <FilterSelect
              label={t('books.filters.preset')}
              value={filters.presetId === null ? 'all' : String(filters.presetId)}
              options={presetOptions}
              onChange={value =>
                update({ presetId: value === 'all' ? null : Number(value) })
              }
            />
          </Box>
          <Box>
            <FilterSelect
              label={t('books.filters.channel')}
              value={filters.chatKey || 'all'}
              options={channelOptions}
              onChange={value => update({ chatKey: value === 'all' ? '' : value })}
            />
          </Box>
          <PersonFilterField
            value={filters.person}
            onChange={value => update({ person: value })}
            scope={scope}
            filters={personFilterState}
          />
          <Box>
            <FilterSelect
              label={t('books.filters.enabled')}
              value={filters.enabled}
              options={enabledOptions}
              onChange={value =>
                update({ enabled: value as WorldbookBooksFilterState['enabled'] })
              }
            />
          </Box>
          </Box>
          <Stack direction="row" spacing={1} justifyContent="flex-end">
            <ActionButton
              tone="ghost"
              size="small"
              disabled={advancedCount === 0}
              onClick={clearAdvanced}
            >
              {t('books.filters.clear')}
            </ActionButton>
            <ActionButton tone="secondary" size="small" onClick={() => setAdvancedAnchor(null)}>
              {t('books.filters.done')}
            </ActionButton>
          </Stack>
        </Stack>
      </Popover>

      <Menu
        anchorEl={statusMenu?.anchor ?? null}
        open={statusMenu !== null}
        onClose={() => setStatusMenu(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        {statusMenu && (
          <MenuItem
            disabled={toggleMutation.isPending}
            onClick={() => toggleMutation.mutate(statusMenu.book)}
          >
            {statusMenu.book.enabled ? t('books.disable') : t('books.enable')}
          </MenuItem>
        )}
      </Menu>

      <Box sx={{ flex: 1, minHeight: 0 }}>
        <ServerDataTable
          columns={columns}
          rows={listQuery.data?.items ?? []}
          getRowId={row => row.id}
          total={listQuery.data?.total ?? 0}
          page={filters.page}
          pageSize={DEFAULT_WORLDBOOK_PAGE_SIZE}
          onPageChange={page => writeQuery({ ...filters, page })}
          loading={listQuery.isPending}
          fetching={listQuery.isFetching}
          error={listQuery.error}
          onRetry={() => void listQuery.refetch()}
          renderError={error =>
            isNotFoundError(error) ? (
              <WorldbookNotFoundState compact />
            ) : (
              <QueryErrorState
                error={error}
                onRetry={() => void listQuery.refetch()}
                compact
              />
            )
          }
          emptyTitle={t('books.emptyTitle')}
          emptyHint={t('books.emptyHint')}
          onRowClick={row => navigate(worldbookBookPath(row.id))}
        />
      </Box>

      {createOpen && (
        <BookFormDialog
          open
          mode="create"
          scope={scope}
          onClose={() => setCreateOpen(false)}
          onCreated={book => {
            setCreateOpen(false)
            navigate(worldbookBookPath(book.id))
          }}
        />
      )}

      {importOpen && (
        <ImportDialog
          open
          mode="new"
          scope={scope}
          onClose={() => {
            setImportOpen(false)
            void queryClient.invalidateQueries({ queryKey: ['worldbook'] })
          }}
          onViewBook={bookId => {
            setImportOpen(false)
            navigate(worldbookBookPath(bookId))
          }}
        />
      )}
    </Box>
  )
}
