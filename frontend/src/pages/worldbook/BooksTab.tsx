import { useMemo, useState } from 'react'
import { Box, Typography } from '@mui/material'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import QueryErrorState from '../../components/common/QueryErrorState'
import SearchField from '../../components/common/SearchField'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../../components/common/ServerDataTable'
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
import { formatDateTime, truncateText } from '../../components/memory/format'
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
import { worldbookBookPath } from '../../router/routes'

/**
 * 「书」标签
 *
 * 一个实例下可以有很多本书，筛选条件全部进 query；换任何一个筛选都把页码置回 1，
 * 否则会停在一个已经不存在的页上。
 */

interface BooksTabProps {
  scope: WorldbookScopeParams
  isScopeReady: boolean
}

export default function BooksTab({ scope, isScopeReady }: BooksTabProps) {
  const { t } = useTranslation('worldbook')
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [createOpen, setCreateOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)

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

  const columns: ServerDataTableColumn<WorldbookBookItem>[] = [
    {
      key: 'name',
      header: t('books.columns.name'),
      width: 280,
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
      key: 'scope',
      header: t('books.columns.scope'),
      width: 240,
      render: row => (
        <Box>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
            <WorldbookScopeChip scopeKind={row.scope_kind} />
            {worldbookBindingText(row) && (
              <Typography variant="caption" color="text.secondary">
                {worldbookBindingText(row)}
              </Typography>
            )}
          </Box>
          {row.binding_missing && (
            <Typography variant="caption" display="block" color="warning.main">
              {worldbookBindingMissingText(row.scope_kind, t)}
            </Typography>
          )}
        </Box>
      ),
    },
    {
      key: 'instance',
      header: t('books.columns.instance'),
      width: 150,
      hideOnMobile: true,
      render: row => (
        <Typography variant="body2" color="text.secondary">
          {row.instance_key || t('common.globalPreset')}
        </Typography>
      ),
    },
    {
      key: 'entries',
      header: t('books.columns.entries'),
      width: 120,
      render: row => (
        <Box>
          <Typography variant="body2">{row.entry_count}</Typography>
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
      header: t('books.columns.enabled'),
      width: 100,
      render: row => <WorldbookEnabledChip enabled={row.enabled} />,
    },
    {
      key: 'updated',
      header: t('books.columns.updatedAt'),
      width: 180,
      hideOnMobile: true,
      render: row => (
        <Typography variant="caption" color="text.secondary">
          {formatDateTime(row.update_time)}
        </Typography>
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
          <Box sx={{ width: { xs: '100%', sm: 150 } }}>
            <FilterSelect
              label={t('books.filters.scopeKind')}
              value={filters.scopeKind || 'all'}
              options={scopeKindOptions}
              onChange={value =>
                update({ scopeKind: value === 'all' ? '' : (value as WorldbookBooksFilterState['scopeKind']) })
              }
            />
          </Box>
          <Box sx={{ width: { xs: '100%', sm: 170 } }}>
            <FilterSelect
              label={t('books.filters.preset')}
              value={filters.presetId === null ? 'all' : String(filters.presetId)}
              options={presetOptions}
              onChange={value =>
                update({ presetId: value === 'all' ? null : Number(value) })
              }
            />
          </Box>
          <Box sx={{ width: { xs: '100%', sm: 190 } }}>
            <FilterSelect
              label={t('books.filters.channel')}
              value={filters.chatKey || 'all'}
              options={channelOptions}
              onChange={value => update({ chatKey: value === 'all' ? '' : value })}
            />
          </Box>
          <Box sx={{ width: { xs: '100%', sm: 160 } }}>
            <SearchField
              label={t('books.filters.person')}
              placeholder={t('books.filters.personPlaceholder')}
              value={filters.person}
              onChange={value => update({ person: value })}
              onClear={() => update({ person: '' })}
            />
          </Box>
          <Box sx={{ width: { xs: '100%', sm: 130 } }}>
            <FilterSelect
              label={t('books.filters.enabled')}
              value={filters.enabled}
              options={enabledOptions}
              onChange={value =>
                update({ enabled: value as WorldbookBooksFilterState['enabled'] })
              }
            />
          </Box>
          <Box sx={{ width: { xs: '100%', sm: 220 } }}>
            <SearchField
              label={t('books.filters.search')}
              placeholder={t('books.filters.searchPlaceholder')}
              value={filters.search}
              onChange={value => update({ search: value })}
              onClear={() => update({ search: '' })}
            />
          </Box>
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
