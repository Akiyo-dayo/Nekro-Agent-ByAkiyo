import { useEffect, useMemo, useState } from 'react'
import { Box, Chip, Stack, Typography, useTheme } from '@mui/material'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { useDeepLinkInstance } from '../../hooks/useDeepLinkInstance'
import ActionButton, { type ActionButtonTone } from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import QueryErrorState from '../../components/common/QueryErrorState'
import SegmentedControl from '../../components/common/SegmentedControl'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../../components/common/ServerDataTable'
import BulkActionDialog from '../../components/memory/BulkActionDialog'
import MemoryItemDialog from '../../components/memory/MemoryItemDialog'
import MemoryNotFoundState from '../../components/memory/MemoryNotFoundState'
import {
  MemoryBlockedHitsChip,
  MemoryFlagChips,
  MemoryStatusChip,
} from '../../components/memory/MemoryStatusChips'
import ScopeFilterBar from '../../components/memory/ScopeFilterBar'
import { formatDateTime, truncateText } from '../../components/memory/format'
import {
  DEFAULT_MEMORY_PAGE_SIZE,
  MEMORY_PAGE_SIZE_OPTIONS,
  filtersFromSearchParams,
  filtersToEpisodeQuery,
  filtersToParagraphQuery,
  filtersToParagraphSelection,
  filtersToSearchParams,
  isMemoryKind,
  parsePageSize,
  type MemoryFilterState,
  type MemoryKind,
} from '../../components/memory/filters'
import { useMemoryScopeOptions } from '../../components/memory/useMemoryScopeOptions'
import { isNotFoundError } from '../../services/api/axios'
import { CHIP_VARIANTS } from '../../theme/variants'
import {
  memoryApi,
  type BulkAction,
  type BulkSelection,
  type EpisodeListItem,
  type MemoryScopeParams,
  type MemoryType,
  type ParagraphListItem,
} from '../../services/api/memory'

/**
 * 「记忆条目」标签
 *
 * 段落与事件共用一条查询流程，但筛选字段与列不一样：事件没有「某个人 / 认知类型 /
 * 知识类型」，也没有批量（后端只提供段落批量接口）。所以两种 kind 各建一张表，
 * 而不是在同一张表里塞一堆条件分支。
 */

interface ItemsTabProps {
  scope: MemoryScopeParams
  isScopeReady: boolean
}

const PARAGRAPH_FIELDS: Array<keyof MemoryFilterState> = [
  'search',
  'status',
  'chatKey',
  'presetId',
  'partition',
  'domain',
  'person',
  'cognitiveType',
  'knowledgeType',
  'originKind',
  'timeFrom',
  'timeTo',
  'sort',
  'order',
]

const EPISODE_FIELDS: Array<keyof MemoryFilterState> = [
  'search',
  'status',
  'chatKey',
  'presetId',
  'partition',
  'domain',
  'timeFrom',
  'timeTo',
  'sort',
  'order',
]

const BULK_BUTTONS: Array<{ action: BulkAction; labelKey: string; tone: ActionButtonTone }> = [
  { action: 'quarantine', labelKey: 'actions.quarantine', tone: 'secondary' },
  { action: 'restore', labelKey: 'actions.restore', tone: 'secondary' },
  { action: 'freeze', labelKey: 'actions.freeze', tone: 'secondary' },
  { action: 'unfreeze', labelKey: 'actions.unfreeze', tone: 'secondary' },
  { action: 'delete', labelKey: 'actions.delete', tone: 'danger' },
  { action: 'hard_delete', labelKey: 'actions.hardDelete', tone: 'danger' },
]

export default function ItemsTab({ scope, isScopeReady }: ItemsTabProps) {
  const theme = useTheme()
  const queryClient = useQueryClient()
  const { t } = useTranslation('memory')
  const [searchParams, setSearchParams] = useSearchParams()
  // 从好感页跳过来的链接带着 `instance` 时先切到那一段所在的实例（规则见 hook 的注释）
  useDeepLinkInstance()

  const filters = useMemo(() => filtersFromSearchParams(searchParams), [searchParams])
  const kindParam = searchParams.get('kind')
  const kind: MemoryKind = isMemoryKind(kindParam) ? kindParam : 'paragraph'
  const pageSize = parsePageSize(searchParams.get('pageSize'))
  // `?item=<段落编号>`：从好感页「在记忆页查看」跳过来时直接打开那一段的详情
  const itemParam = searchParams.get('item')
  const itemFromUrl = useMemo(() => {
    if (!itemParam) return null
    const parsed = Number(itemParam)
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null
  }, [itemParam])

  const [selectedIds, setSelectedIds] = useState<Set<string | number>>(new Set())
  const [selectAllFiltered, setSelectAllFiltered] = useState(false)
  const [bulkAction, setBulkAction] = useState<BulkAction | null>(null)
  const [detailTarget, setDetailTarget] = useState<{ memoryType: MemoryType; id: number } | null>(
    null
  )

  // 地址栏里的 `item` 就是「这一段正开着」：进页面、刷新、前进后退都靠它恢复
  useEffect(() => {
    if (itemFromUrl === null) return
    setDetailTarget({ memoryType: 'paragraph', id: itemFromUrl })
  }, [itemFromUrl])

  /** 关掉详情时把 `item` 一起去掉，免得刷新之后它又自己弹出来 */
  const closeDetail = () => {
    setDetailTarget(null)
    if (itemFromUrl === null) return
    const next = new URLSearchParams(searchParams)
    next.delete('item')
    setSearchParams(next, { replace: true })
  }

  /**
   * 点行打开详情
   *
   * 换成另一行时要把地址栏里的 `item` 抹掉：留着它，刷新回来看到的还是跳进来那一条，
   * 而眼前这一条反倒没了。
   */
  const openRowDetail = (target: { memoryType: MemoryType; id: number }) => {
    setDetailTarget(target)
    if (itemFromUrl === null) return
    const next = new URLSearchParams(searchParams)
    next.delete('item')
    setSearchParams(next, { replace: true })
  }

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['memory'] })
  }

  const writeQuery = (
    nextFilters: MemoryFilterState,
    nextKind: MemoryKind = kind,
    nextPageSize: number = pageSize
  ) => {
    const params = filtersToSearchParams(nextFilters)
    if (nextKind !== 'paragraph') params.set('kind', nextKind)
    if (nextPageSize !== DEFAULT_MEMORY_PAGE_SIZE) params.set('pageSize', String(nextPageSize))
    // 详情是开着的那一段，翻页 / 改筛选不该把它从地址栏里抹掉
    if (itemFromUrl !== null) params.set('item', String(itemFromUrl))
    setSearchParams(params, { replace: true })
  }

  const clearSelection = () => {
    setSelectedIds(new Set())
    setSelectAllFiltered(false)
  }

  const handleFiltersChange = (next: MemoryFilterState) => {
    clearSelection()
    writeQuery({ ...next, page: 1 })
  }

  const handleKindChange = (nextKind: MemoryKind) => {
    if (nextKind === kind) return
    clearSelection()
    const nextFilters: MemoryFilterState = {
      ...filters,
      page: 1,
      // 事件不支持这几项，切过去时清掉，避免 URL 与界面各说一套
      person: nextKind === 'episode' ? '' : filters.person,
      cognitiveType: nextKind === 'episode' ? '' : filters.cognitiveType,
      knowledgeType: nextKind === 'episode' ? '' : filters.knowledgeType,
      originKind: nextKind === 'episode' ? '' : filters.originKind,
    }
    writeQuery(nextFilters, nextKind)
  }

  const handlePageSizeChange = (value: string) => {
    writeQuery({ ...filters, page: 1 }, kind, parsePageSize(value))
  }

  const paragraphsQuery = useQuery({
    queryKey: ['memory', 'paragraphs', scope, filters, pageSize],
    queryFn: () =>
      memoryApi.listParagraphs({ ...filtersToParagraphQuery(filters, pageSize), ...scope }),
    enabled: isScopeReady && kind === 'paragraph',
    retry: false,
  })

  const episodesQuery = useQuery({
    queryKey: ['memory', 'episodes', scope, filters, pageSize],
    queryFn: () =>
      memoryApi.listEpisodes({ ...filtersToEpisodeQuery(filters, pageSize), ...scope }),
    enabled: isScopeReady && kind === 'episode',
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

  const paragraphs = paragraphsQuery.data?.items ?? []
  const episodes = episodesQuery.data?.items ?? []
  const total = kind === 'paragraph' ? (paragraphsQuery.data?.total ?? 0) : (episodesQuery.data?.total ?? 0)

  const pageSizeSelect = (
    <FilterSelect
      label={t('filters.pageSize')}
      value={String(pageSize)}
      fullWidth={false}
      options={MEMORY_PAGE_SIZE_OPTIONS.map(size => ({
        value: String(size),
        label: t('filters.pageSizeOption', { count: size }),
      }))}
      onChange={handlePageSizeChange}
      sx={{ width: 150 }}
    />
  )

  const paragraphColumns: ServerDataTableColumn<ParagraphListItem>[] = [
    {
      key: 'content',
      header: t('columns.content'),
      // 表格是固定布局：其余列都是像素宽，这一列也给像素宽，百分比会被挤没
      width: 300,
      render: row => (
        <Box sx={{ minWidth: 0 }}>
          <Stack direction="row" spacing={0.5} alignItems="center" sx={{ minWidth: 0 }}>
            {row.origin_kind === 'affection' && (
              <Chip
                size="small"
                label={t('origin.affection')}
                sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.secondary.main, true)}
              />
            )}
            <Typography variant="body2" noWrap title={row.summary || row.content}>
              {row.summary || row.content}
            </Typography>
          </Stack>
          {row.summary && (
            <Typography variant="caption" color="text.secondary" noWrap display="block">
              {truncateText(row.content, 80)}
            </Typography>
          )}
        </Box>
      ),
    },
    {
      key: 'status',
      header: t('columns.status'),
      width: 130,
      render: row => (
        <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap' }}>
          <MemoryStatusChip status={row.status} />
          <MemoryFlagChips isFrozen={row.is_frozen} isProtected={row.is_protected} />
        </Stack>
      ),
    },
    {
      key: 'persons',
      header: t('columns.persons'),
      width: 150,
      hideOnMobile: true,
      render: row => {
        const persons = row.person_accounts ?? []
        if (persons.length === 0) {
          return (
            <Typography variant="body2" color="text.secondary">
              —
            </Typography>
          )
        }
        const shown = persons.slice(0, 2).join(', ')
        const rest = persons.length - 2
        return (
          <Typography variant="body2" noWrap title={persons.join(', ')}>
            {rest > 0 ? `${shown} +${rest}` : shown}
          </Typography>
        )
      },
    },
    {
      key: 'channel',
      header: t('columns.channel'),
      width: 140,
      hideOnMobile: true,
      render: row => (
        <Typography variant="body2" noWrap title={row.origin_chat_key ?? ''}>
          {channelLabel(row.origin_chat_key)}
        </Typography>
      ),
    },
    {
      key: 'partition',
      header: t('columns.partition'),
      width: 110,
      hideOnMobile: true,
      render: row => (
        <Typography variant="body2" noWrap>
          {row.workspace_id === 0
            ? t('partition.domain')
            : t('partition.workspaceWithId', { id: row.workspace_id })}
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
      key: 'time',
      header: t('columns.time'),
      width: 160,
      hideOnMobile: true,
      render: row => (
        <Typography variant="body2" noWrap>
          {formatDateTime(row.create_time)}
        </Typography>
      ),
    },
  ]

  const episodeColumns: ServerDataTableColumn<EpisodeListItem>[] = [
    {
      key: 'content',
      header: t('columns.content'),
      width: 300,
      render: row => (
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="body2" noWrap title={row.title}>
            {row.title || '—'}
          </Typography>
          <Typography variant="caption" color="text.secondary" noWrap display="block">
            {truncateText(row.narrative_summary, 80)}
          </Typography>
        </Box>
      ),
    },
    {
      key: 'status',
      header: t('columns.status'),
      width: 130,
      render: row => <MemoryStatusChip status={row.status} />,
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
      key: 'hits',
      header: t('columns.blockedHits'),
      width: 100,
      align: 'right',
      hideOnMobile: true,
      render: row => <MemoryBlockedHitsChip count={row.blocked_round_hits} />,
    },
    {
      key: 'timeRange',
      header: t('columns.time'),
      width: 230,
      hideOnMobile: true,
      render: row => (
        <Typography variant="body2" noWrap>
          {formatDateTime(row.time_start)} – {formatDateTime(row.time_end)}
        </Typography>
      ),
    },
  ]

  const bulkSelection: BulkSelection = selectAllFiltered
    ? { filter: filtersToParagraphSelection(filters) }
    : { ids: Array.from(selectedIds).map(id => Number(id)) }

  const toggleSelectAllFiltered = () => {
    setSelectAllFiltered(previous => !previous)
  }

  const renderBulkBar = () => (
    <Box
      sx={{
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: 1,
        width: '100%',
      }}
    >
      <Typography variant="body2" fontWeight={600}>
        {selectAllFiltered
          ? t('bulk.selectedAll', { total })
          : t('bulk.selected', { count: selectedIds.size })}
      </Typography>
      <ActionButton tone="ghost" size="small" onClick={toggleSelectAllFiltered}>
        {selectAllFiltered ? t('bulk.selectOnlyChecked') : t('bulk.selectAll', { total })}
      </ActionButton>
      <Box sx={{ flex: 1 }} />
      {BULK_BUTTONS.map(button => (
        <ActionButton
          key={button.action}
          tone={button.tone}
          size="small"
          onClick={() => setBulkAction(button.action)}
        >
          {t(button.labelKey)}
        </ActionButton>
      ))}
    </Box>
  )

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0, flex: 1 }}>
      <ScopeFilterBar
        value={filters}
        onChange={handleFiltersChange}
        fields={kind === 'paragraph' ? PARAGRAPH_FIELDS : EPISODE_FIELDS}
        scope={scope}
        leading={
          <SegmentedControl
            value={kind}
            options={[
              { value: 'paragraph', label: t('kind.paragraph') },
              { value: 'episode', label: t('kind.episode') },
            ]}
            onChange={handleKindChange}
          />
        }
      />

      {kind === 'paragraph' ? (
        <ServerDataTable<ParagraphListItem>
          columns={paragraphColumns}
          rows={paragraphs}
          getRowId={row => row.id}
          total={total}
          page={filters.page}
          pageSize={pageSize}
          onPageChange={page => writeQuery({ ...filters, page })}
          loading={paragraphsQuery.isPending}
          fetching={paragraphsQuery.isFetching}
          error={paragraphsQuery.error}
          onRetry={() => void paragraphsQuery.refetch()}
          renderError={error =>
            isNotFoundError(error) ? (
              <MemoryNotFoundState />
            ) : (
              <QueryErrorState
                error={error}
                onRetry={() => void paragraphsQuery.refetch()}
                compact
              />
            )
          }
          emptyTitle={t('empty.itemsTitle')}
          emptyHint={t('empty.itemsHint')}
          onRowClick={row => openRowDetail({ memoryType: 'paragraph', id: row.id })}
          selection={{
            selectedIds,
            onChange: next => {
              setSelectedIds(next)
              // 手动改勾选就不再是「当前筛选的全部」
              setSelectAllFiltered(false)
            },
          }}
          renderBulkBar={renderBulkBar}
          paginationExtra={pageSizeSelect}
        />
      ) : (
        <ServerDataTable<EpisodeListItem>
          columns={episodeColumns}
          rows={episodes}
          getRowId={row => row.id}
          total={total}
          page={filters.page}
          pageSize={pageSize}
          onPageChange={page => writeQuery({ ...filters, page }, kind)}
          loading={episodesQuery.isPending}
          fetching={episodesQuery.isFetching}
          error={episodesQuery.error}
          onRetry={() => void episodesQuery.refetch()}
          renderError={error =>
            isNotFoundError(error) ? (
              <MemoryNotFoundState />
            ) : (
              <QueryErrorState
                error={error}
                onRetry={() => void episodesQuery.refetch()}
                compact
              />
            )
          }
          emptyTitle={t('empty.episodesTitle')}
          emptyHint={t('empty.episodesHint')}
          onRowClick={row => openRowDetail({ memoryType: 'episode', id: row.id })}
          paginationExtra={pageSizeSelect}
        />
      )}

      <BulkActionDialog
        action={bulkAction}
        selection={bulkSelection}
        scope={scope}
        onClose={() => setBulkAction(null)}
        onDone={() => {
          setBulkAction(null)
          clearSelection()
          invalidate()
        }}
      />

      <MemoryItemDialog
        memoryType={detailTarget?.memoryType ?? 'paragraph'}
        itemId={detailTarget?.id ?? null}
        scope={scope}
        onClose={closeDetail}
        onChanged={invalidate}
      />
    </Box>
  )
}
