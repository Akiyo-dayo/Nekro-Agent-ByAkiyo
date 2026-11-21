import { useEffect, useMemo, useState } from 'react'
import {
  Box,
  Chip,
  Divider,
  Menu,
  MenuItem,
  Stack,
  Typography,
  alpha,
  useTheme,
} from '@mui/material'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import ArrowDropDownRoundedIcon from '@mui/icons-material/ArrowDropDownRounded'
import { useDeepLinkInstance } from '../../hooks/useDeepLinkInstance'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import QueryErrorState from '../../components/common/QueryErrorState'
import SegmentedControl from '../../components/common/SegmentedControl'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../../components/common/ServerDataTable'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import BulkActionDialog from '../../components/memory/BulkActionDialog'
import MemoryItemDialog from '../../components/memory/MemoryItemDialog'
import MemoryNotFoundState from '../../components/memory/MemoryNotFoundState'
import {
  MemoryBlockedHitsChip,
  MemoryFlagIcons,
  MemorySensitiveChip,
  MemoryStatusChip,
} from '../../components/memory/MemoryStatusChips'
import ParagraphGroupsView from '../../components/memory/ParagraphGroupsView'
import PersonNames from '../../components/memory/PersonNames'
import ScopeFilterBar from '../../components/memory/ScopeFilterBar'
import {
  formatFullDateTime,
  formatListTime,
  truncateText,
} from '../../components/memory/format'
import { labelsOrAccounts } from '../../components/memory/personLabels'
import {
  DEFAULT_MEMORY_PAGE_SIZE,
  MEMORY_PAGE_SIZE_OPTIONS,
  MEMORY_TAB_FILTER_FIELDS,
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
import { BORDER_RADIUS, CHIP_VARIANTS } from '../../theme/variants'
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
 *
 * 段落另有两种看法：按人、按事件折叠成组（`ParagraphGroupsView`）。分组只是换个角度看
 * 同一批段落，筛选不变；批量与排序留在平铺列表里，组里「在列表里看」会带着这一组的筛选跳回来。
 */

interface ItemsTabProps {
  scope: MemoryScopeParams
  isScopeReady: boolean
}

/** 段落的看法：平铺列表，或按人 / 按事件折叠成组（地址栏 `view`，平铺时不写） */
type ParagraphView = 'list' | 'person' | 'topic'

const PARAGRAPH_FIELDS: Array<keyof MemoryFilterState> = [...MEMORY_TAB_FILTER_FIELDS.items]

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

// 「批量操作」菜单：隔离是主按钮，其余按「放回 / 冻结 / 解冻 / 删除 / 彻底删除」排列
const BULK_MENU_ACTIONS: Array<{ action: BulkAction; labelKey: string }> = [
  { action: 'restore', labelKey: 'actions.restore' },
  { action: 'freeze', labelKey: 'actions.freeze' },
  { action: 'unfreeze', labelKey: 'actions.unfreeze' },
  { action: 'delete', labelKey: 'actions.delete' },
  { action: 'hard_delete', labelKey: 'actions.hardDelete' },
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
  // 段落的看法：平铺列表，或按人 / 按事件折叠成组。事件本身没有分组视图
  const viewParam = searchParams.get('view')
  const view: ParagraphView =
    kind === 'paragraph' && (viewParam === 'person' || viewParam === 'topic') ? viewParam : 'list'
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
  const [bulkMenuAnchor, setBulkMenuAnchor] = useState<HTMLElement | null>(null)
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
    nextPageSize: number = pageSize,
    nextView: ParagraphView = view
  ) => {
    const params = filtersToSearchParams(nextFilters)
    if (nextKind !== 'paragraph') params.set('kind', nextKind)
    if (nextKind === 'paragraph' && nextView !== 'list') params.set('view', nextView)
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
      sensitive: nextKind === 'episode' ? '' : filters.sensitive,
      originKind: nextKind === 'episode' ? '' : filters.originKind,
      episode: nextKind === 'episode' ? '' : filters.episode,
    }
    writeQuery(nextFilters, nextKind, pageSize, nextKind === 'episode' ? 'list' : view)
  }

  const handleViewChange = (nextView: ParagraphView) => {
    if (nextView === view) return
    clearSelection()
    writeQuery({ ...filters, page: 1 }, kind, pageSize, nextView)
  }

  /** 分组视图里「在列表里看这一组」：回到平铺列表，带上这一组的人 / 事件筛选 */
  const showGroupInList = (patch: Partial<MemoryFilterState>) => {
    clearSelection()
    writeQuery({ ...filters, ...patch, page: 1 }, 'paragraph', pageSize, 'list')
  }

  const handlePageSizeChange = (value: string) => {
    writeQuery({ ...filters, page: 1 }, kind, parsePageSize(value))
  }

  const paragraphsQuery = useQuery({
    queryKey: ['memory', 'paragraphs', scope, filters, pageSize],
    queryFn: () =>
      memoryApi.listParagraphs({ ...filtersToParagraphQuery(filters, pageSize), ...scope }),
    enabled: isScopeReady && kind === 'paragraph' && view === 'list',
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

  const partitionLabel = (workspaceId: number) =>
    workspaceId === 0
      ? t('partition.domain')
      : t('partition.workspaceWithId', { id: workspaceId })

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
        // 分区列已删：分区与截断前的全文合在同一个 tooltip 里，不再叠浏览器原生 title
        <SimpleTooltip
          title={
            <Box>
              <Box>{truncateText(row.summary || row.content, 160)}</Box>
              <Box sx={{ mt: 0.5, opacity: 0.72 }}>
                {t('filters.chipValue', {
                  label: t('filters.partition'),
                  value: partitionLabel(row.workspace_id),
                })}
              </Box>
            </Box>
          }
        >
          <Box sx={{ minWidth: 0 }}>
            <Stack direction="row" spacing={0.5} alignItems="center" sx={{ minWidth: 0 }}>
              {row.origin_kind === 'affection' && (
                <Chip
                  size="small"
                  label={t('origin.affection')}
                  sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.secondary.main, true)}
                />
              )}
              <Typography variant="body2" noWrap sx={{ minWidth: 0, flex: 1 }}>
                {row.summary || row.content}
              </Typography>
              {row.is_sensitive && <MemorySensitiveChip />}
              {row.blocked_round_hits > 0 && (
                <MemoryBlockedHitsChip count={row.blocked_round_hits} />
              )}
            </Stack>
            {row.summary && (
              <Typography variant="caption" color="text.secondary" noWrap display="block">
                {truncateText(row.content, 80)}
              </Typography>
            )}
          </Box>
        </SimpleTooltip>
      ),
    },
    {
      key: 'status',
      header: t('columns.status'),
      width: 110,
      render: row => (
        <Stack direction="row" spacing={0.5} alignItems="center">
          <MemoryStatusChip status={row.status} />
          <MemoryFlagIcons isFrozen={row.is_frozen} isProtected={row.is_protected} />
        </Stack>
      ),
    },
    {
      key: 'persons',
      header: t('columns.persons'),
      width: 180,
      hideOnMobile: true,
      // 名字在前、称呼并排，号码进悬停提示（列表里一串号码认不出是谁）
      render: row => (
        <PersonNames persons={labelsOrAccounts(row.persons, row.person_accounts ?? [])} />
      ),
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
      key: 'time',
      header: t('columns.time'),
      width: 130,
      hideOnMobile: true,
      render: row => (
        <SimpleTooltip title={formatFullDateTime(row.create_time)}>
          <Typography variant="body2" noWrap>
            {formatListTime(row.create_time)}
          </Typography>
        </SimpleTooltip>
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
          <Stack direction="row" spacing={0.5} alignItems="center" sx={{ minWidth: 0 }}>
            <Typography variant="body2" noWrap title={row.title} sx={{ minWidth: 0, flex: 1 }}>
              {row.title || '—'}
            </Typography>
            {row.blocked_round_hits > 0 && (
              <MemoryBlockedHitsChip count={row.blocked_round_hits} />
            )}
          </Stack>
          <Typography variant="caption" color="text.secondary" noWrap display="block">
            {truncateText(row.narrative_summary, 80)}
          </Typography>
        </Box>
      ),
    },
    {
      key: 'status',
      header: t('columns.status'),
      width: 110,
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
      key: 'timeRange',
      header: t('columns.time'),
      width: 210,
      hideOnMobile: true,
      render: row => (
        <SimpleTooltip
          title={`${formatFullDateTime(row.time_start)} – ${formatFullDateTime(row.time_end)}`}
        >
          <Typography variant="body2" noWrap>
            {formatListTime(row.time_start)} – {formatListTime(row.time_end)}
          </Typography>
        </SimpleTooltip>
      ),
    },
  ]

  const bulkSelection: BulkSelection = selectAllFiltered
    ? { filter: filtersToParagraphSelection(filters) }
    : { ids: Array.from(selectedIds).map(id => Number(id)) }

  const toggleSelectAllFiltered = () => {
    setSelectAllFiltered(previous => !previous)
  }

  const openBulkAction = (action: BulkAction) => {
    setBulkMenuAnchor(null)
    setBulkAction(action)
  }

  const showSelectionBar = selectedIds.size > 0 || selectAllFiltered

  const renderSelectionBar = () => (
    <Box
      sx={{
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: 1,
        minHeight: 48,
        px: 1.5,
        borderRadius: BORDER_RADIUS.DEFAULT,
        bgcolor: 'background.paper',
        backgroundImage: `linear-gradient(${alpha(
          theme.palette.primary.main,
          0.06
        )}, ${alpha(theme.palette.primary.main, 0.06)})`,
        border: '1px solid',
        borderColor: 'divider',
      }}
    >
      <Typography variant="body2" fontWeight={600}>
        {selectAllFiltered
          ? t('bulk.selectedAll', { total })
          : t('bulk.selected', { count: selectedIds.size })}
      </Typography>
      <Typography variant="body2" color="text.secondary">
        ·
      </Typography>
      <ActionButton tone="ghost" size="small" onClick={toggleSelectAllFiltered}>
        {selectAllFiltered ? t('bulk.selectOnlyChecked') : t('bulk.selectAll', { total })}
      </ActionButton>
      <Box sx={{ flex: 1 }} />
      <ActionButton
        tone="secondary"
        size="small"
        onClick={() => setBulkAction('quarantine')}
      >
        {t('actions.quarantine')}
      </ActionButton>
      <ActionButton
        tone="secondary"
        size="small"
        endIcon={<ArrowDropDownRoundedIcon />}
        onClick={event => setBulkMenuAnchor(event.currentTarget)}
      >
        {t('bulk.more')}
      </ActionButton>
      <Menu
        anchorEl={bulkMenuAnchor}
        open={Boolean(bulkMenuAnchor)}
        onClose={() => setBulkMenuAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        {BULK_MENU_ACTIONS.slice(0, 3).map(item => (
          <MenuItem key={item.action} onClick={() => openBulkAction(item.action)}>
            <Typography variant="body2">{t(item.labelKey)}</Typography>
          </MenuItem>
        ))}
        <Divider sx={{ my: 0.5 }} />
        {BULK_MENU_ACTIONS.slice(3).map(item => (
          <MenuItem
            key={item.action}
            onClick={() => openBulkAction(item.action)}
            sx={item.action === 'hard_delete' ? { color: 'error.main' } : undefined}
          >
            <Typography variant="body2" color="inherit">
              {t(item.labelKey)}
            </Typography>
          </MenuItem>
        ))}
      </Menu>
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
        trailing={
          kind === 'paragraph' ? (
            <SegmentedControl
              value={view}
              options={[
                { value: 'list', label: t('view.list') },
                { value: 'person', label: t('view.byPerson') },
                { value: 'topic', label: t('view.byTopic') },
              ]}
              onChange={handleViewChange}
            />
          ) : undefined
        }
      />

      {kind === 'paragraph' && filters.episode && (
        <Box>
          <Chip
            size="small"
            label={
              filters.episode === 'none'
                ? t('groups.episodeFilterNone')
                : t('groups.episodeFilter', { id: filters.episode })
            }
            onDelete={() => handleFiltersChange({ ...filters, episode: '' })}
            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true)}
          />
        </Box>
      )}

      {kind === 'paragraph' && view !== 'list' ? (
        <ParagraphGroupsView
          by={view}
          scope={scope}
          filters={filters}
          isScopeReady={isScopeReady}
          channelLabel={channelLabel}
          onOpenParagraph={id => openRowDetail({ memoryType: 'paragraph', id })}
          onShowInList={showGroupInList}
        />
      ) : kind === 'paragraph' ? (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, flex: 1, minHeight: 0 }}>
          {showSelectionBar && renderSelectionBar()}
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
            paginationExtra={pageSizeSelect}
          />
        </Box>
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
