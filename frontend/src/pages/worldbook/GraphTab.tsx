import { useEffect, useMemo, useRef, useState } from 'react'
import { Box, CircularProgress, Drawer, MenuItem, Stack, TextField, Typography, useMediaQuery } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import SearchField from '../../components/common/SearchField'
import { ThemedTooltip } from '../../components/common/ThemedTooltip'
import AtlasEmptyState from '../../components/memory/atlas/AtlasEmptyState'
import AtlasGraph from '../../components/memory/atlas/AtlasGraph'
import AtlasLegend, { type AtlasLegendEdge } from '../../components/memory/atlas/AtlasLegend'
import AtlasToolbar from '../../components/memory/atlas/AtlasToolbar'
import AtlasZoomControl from '../../components/memory/atlas/AtlasZoomControl'
import {
  ATLAS_HEAVY_GRAPH,
  buildAtlasLayout,
  clusterAccent,
  clusterTitle,
  legendClusterOrder,
  presentEdgeTypes,
} from '../../components/memory/atlas/layout'
import type { AtlasViewMode, InformationDensity } from '../../components/memory/atlas/types'
import { useAtlasViewport } from '../../components/memory/atlas/useAtlasViewport'
import WorldbookGraphPanel from '../../components/worldbook/WorldbookGraphPanel'
import { createWorldbookAtlasConfig } from '../../components/worldbook/worldbookAtlasConfig'
import { isNotFoundError } from '../../services/api/axios'
import {
  worldbookApi,
  type WorldbookGraphResponse,
  type WorldbookScopeParams,
} from '../../services/api/worldbook'
import { BORDER_RADIUS, SCROLLBAR_VARIANTS } from '../../theme/variants'

/**
 * 世界书图谱
 *
 * 书在中间，条目在内圈，人设 / 频道 / 人合成的「绑定」在外圈；常显的只有书 → 条目。
 * 只读：点节点在右侧看详情，书与条目可以直接跳到原来的书详情页。
 *
 * 顶部只有书下拉、搜索与工具条（密度、显示停用 / 隔离）。世界书没有「事件 / 原子」两种视图，
 * 所以工具条关掉视图切换，视图固定成事件视图：`AtlasGraph` 的环境连线只在这个视图里画，
 * 书 → 条目的包含关系才能一眼看到。
 */

const GRAPH_LIMIT = 240
const PANEL_WIDTH = 360
const SEARCH_DEBOUNCE_MS = 300
const VIEW_MODE: AtlasViewMode = 'episode'

interface GraphTabProps {
  scope: WorldbookScopeParams
  isScopeReady: boolean
}

export default function GraphTab({ scope, isScopeReady }: GraphTabProps) {
  const theme = useTheme()
  const isWide = useMediaQuery(theme.breakpoints.up('lg'))
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))
  const { t } = useTranslation('worldbook')
  const { t: tAtlas } = useTranslation('atlas')

  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [bookId, setBookId] = useState<number | null>(null)
  const [includeInactive, setIncludeInactive] = useState(false)
  const [density, setDensity] = useState<InformationDensity>('medium')
  const [focusCluster, setFocusCluster] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const viewport = useAtlasViewport()
  const { zoom } = viewport

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [search])

  // 换实例 / 换适配器后，上一本书与上一个选中节点都不再属于当前作用域
  const scopeKey = `${scope.adapter_key ?? ''}|${scope.instance_key ?? ''}`
  const lastScopeKeyRef = useRef(scopeKey)
  useEffect(() => {
    if (lastScopeKeyRef.current === scopeKey) return
    lastScopeKeyRef.current = scopeKey
    setBookId(null)
    setSelectedId(null)
    setFocusCluster(null)
  }, [scopeKey])

  const config = useMemo(
    () =>
      createWorldbookAtlasConfig({
        titles: {
          book: { title: t('graph.clusters.book') },
          entry: { title: t('graph.clusters.entry') },
          person: { title: t('graph.clusters.person') },
          binding: { title: t('graph.clusters.binding') },
        },
      }),
    [t]
  )

  const booksQuery = useQuery({
    queryKey: ['worldbook', 'books', 'graph-picker', scopeKey],
    queryFn: () => worldbookApi.listBooks({ ...scope, page: 1, page_size: 100 }),
    enabled: isScopeReady,
    retry: false,
  })

  const graphQuery = useQuery({
    queryKey: [
      'worldbook',
      'graph',
      scopeKey,
      bookId,
      debouncedSearch,
      includeInactive,
    ],
    queryFn: () =>
      worldbookApi.getGraph({
        ...scope,
        book_id: bookId ?? undefined,
        q: debouncedSearch || undefined,
        include_inactive: includeInactive,
        limit: GRAPH_LIMIT,
      }),
    enabled: isScopeReady,
    retry: false,
  })
  const graph: WorldbookGraphResponse | undefined = graphQuery.data

  // 节点太多时第一次先退到「疏」，之后听用户的
  const heavyHandledRef = useRef(false)
  useEffect(() => {
    if (heavyHandledRef.current || !graph || graph.nodes.length <= ATLAS_HEAVY_GRAPH) return
    heavyHandledRef.current = true
    setDensity('low')
  }, [graph])

  const atlas = useMemo(
    () => buildAtlasLayout(theme, graph, zoom, density, VIEW_MODE, config),
    [config, density, graph, theme, zoom]
  )
  const shownLayout = useMemo(
    () => (focusCluster ? { ...atlas, nodes: atlas.nodes.filter(node => node.cluster === focusCluster) } : atlas),
    [atlas, focusCluster]
  )
  const nodeById = useMemo(() => new Map((graph?.nodes ?? []).map(node => [node.id, node])), [graph])
  const selectedNode = selectedId ? nodeById.get(selectedId) ?? null : null
  const selectedAccent = selectedNode
    ? clusterAccent(config, config.resolveCluster(selectedNode))
    : theme.palette.info.main

  useEffect(() => {
    if (focusCluster && !legendClusterOrder(config, VIEW_MODE).includes(focusCluster)) setFocusCluster(null)
  }, [config, focusCluster])

  useEffect(() => {
    if (selectedId && graph && !graph.nodes.some(node => node.id === selectedId)) setSelectedId(null)
  }, [graph, selectedId])

  const kindLabel = (kind: string) => t(`graph.kind.${kind}`, { defaultValue: kind })

  const loading = graphQuery.isPending
  const legendClusters = legendClusterOrder(config, VIEW_MODE).map(cluster => ({
    key: cluster,
    label: clusterTitle(config, cluster, VIEW_MODE),
    color: clusterAccent(config, cluster),
    shown: loading || graphQuery.isError ? null : atlas.clusterStats[cluster]?.shown ?? 0,
    total: loading || graphQuery.isError ? null : atlas.clusterStats[cluster]?.total ?? 0,
  }))
  const legendEdges = useMemo<AtlasLegendEdge[]>(() => {
    const present = presentEdgeTypes(graph?.edges ?? [])
    const result: AtlasLegendEdge[] = []
    if (present.has('book_entry')) {
      result.push({
        key: 'book_entry',
        label: t('graph.edges.bookEntry'),
        color: clusterAccent(config, 'book'),
      })
    }
    if (present.has('book_binding')) {
      result.push({
        key: 'book_binding',
        label: t('graph.edges.bookBinding'),
        color: clusterAccent(config, 'binding'),
      })
    }
    if (present.has('key_reference')) {
      result.push({
        key: 'key_reference',
        label: t('graph.edges.keyReference'),
        dash: '5 5',
        color: theme.palette.text.secondary,
      })
    }
    return result
  }, [config, graph, t, theme])

  const legendNotice = useMemo(() => {
    if (!graph) return null
    const notices: string[] = []
    if (graph.books_truncated) {
      notices.push(t('graph.legend.booksTruncated', { drawn: graph.books_drawn, total: graph.books_total }))
    }
    if (graph.entries_total > graph.entries_drawn) {
      notices.push(
        t('graph.legend.entriesTruncated', { drawn: graph.entries_drawn, total: graph.entries_total })
      )
    }
    if (graph.references_truncated) notices.push(t('graph.legend.referencesTruncated'))
    return notices.length > 0 ? notices.join(' ') : null
  }, [graph, t])

  const hasFilters = bookId !== null || debouncedSearch.length > 0 || includeInactive
  const hasBooks = (graph?.books_total ?? 0) > 0
  const emptyState = graphQuery.isError ? (
    isNotFoundError(graphQuery.error) ? (
      <AtlasEmptyState title={t('graph.empty.notFound')} description={t('graph.empty.notFoundHint')} />
    ) : (
      <AtlasEmptyState>
        <QueryErrorState error={graphQuery.error} onRetry={() => void graphQuery.refetch()} compact />
      </AtlasEmptyState>
    )
  ) : (
    <AtlasEmptyState
      title={hasBooks ? t('graph.empty.noEntries') : t('graph.empty.noBooks')}
      description={
        hasFilters
          ? t('graph.empty.filteredHint')
          : hasBooks
            ? t('graph.empty.noEntriesHint')
            : t('graph.empty.noBooksHint')
      }
      action={
        hasFilters ? (
          <ActionButton
            tone="secondary"
            size="small"
            onClick={() => {
              setSearch('')
              setDebouncedSearch('')
              setBookId(null)
              setIncludeInactive(false)
              setSelectedId(null)
              setFocusCluster(null)
            }}
          >
            {t('graph.empty.clearFilters')}
          </ActionButton>
        ) : undefined
      }
    />
  )

  const panel = selectedNode ? (
    <Box
      sx={{
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        bgcolor: 'background.paper',
        ...(isWide
          ? {
              borderRadius: BORDER_RADIUS.LARGE,
              border: '1px solid',
              borderColor: alpha(theme.palette.primary.main, 0.12),
              overflow: 'hidden',
            }
          : {}),
      }}
    >
      <Stack direction="row" justifyContent="flex-end" sx={{ px: 1.25, pt: 1, flexShrink: 0 }}>
        <IconActionButton
          size="small"
          onClick={() => setSelectedId(null)}
          aria-label={tAtlas('panel.close')}
          title={tAtlas('panel.close')}
        >
          <CloseRoundedIcon fontSize="small" />
        </IconActionButton>
      </Stack>
      <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', px: 1.5, pb: 1.5, ...SCROLLBAR_VARIANTS.thin.styles }}>
        <WorldbookGraphPanel
          node={selectedNode}
          graph={graph}
          nodeById={nodeById}
          accent={selectedAccent}
          scope={scope}
          onSelect={setSelectedId}
        />
      </Box>
    </Box>
  ) : null

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, flex: 1, minHeight: 0 }}>
      <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap" alignItems="center">
        <TextField
          select
          size="small"
          value={bookId ?? ''}
          onChange={event => {
            const value = event.target.value
            setBookId(value === '' ? null : Number(value))
            setSelectedId(null)
            setFocusCluster(null)
          }}
          SelectProps={{ displayEmpty: true }}
          inputProps={{ 'aria-label': t('graph.allBooks') }}
          sx={{ minWidth: { xs: '100%', sm: 220 } }}
        >
          <MenuItem value="">{t('graph.allBooks')}</MenuItem>
          {(booksQuery.data?.items ?? []).map(book => (
            <MenuItem key={book.id} value={book.id}>
              {book.name}
            </MenuItem>
          ))}
        </TextField>
        <SearchField
          value={search}
          onChange={value => {
            setSearch(value)
            setSelectedId(null)
          }}
          onClear={() => {
            setSearch('')
            setSelectedId(null)
          }}
          clearAriaLabel={t('graph.clearSearch')}
          placeholder={t('graph.searchPlaceholder')}
          sx={{ width: { xs: '100%', sm: 260 } }}
        />
        <Box sx={{ ml: 'auto' }}>
          <AtlasToolbar
            viewMode={VIEW_MODE}
            onViewModeChange={() => undefined}
            density={density}
            onDensityChange={setDensity}
            showInactive={{
              value: includeInactive,
              onChange: value => {
                setIncludeInactive(value)
                setSelectedId(null)
              },
            }}
            showViewSwitch={false}
          />
        </Box>
      </Stack>

      {booksQuery.isError && !graphQuery.isError && (
        <Typography variant="body2" color="text.secondary">
          {t('graph.bookPickerError')}
        </Typography>
      )}

      <Box sx={{ display: 'flex', flex: 1, minHeight: 0 }}>
        <Box
          sx={{
            position: 'relative',
            flex: 1,
            minWidth: 0,
            minHeight: 'max(480px, calc(100vh - 280px))',
            borderRadius: BORDER_RADIUS.LARGE,
            overflow: 'hidden',
            border: '1px solid',
            borderColor: alpha(theme.palette.primary.main, 0.12),
            background: `linear-gradient(180deg, ${alpha(theme.palette.background.paper, 0.96)}, ${alpha(theme.palette.background.default, 0.99)})`,
          }}
        >
          <Box
            sx={{
              position: 'absolute',
              inset: 0,
              backgroundImage: `radial-gradient(${alpha(theme.palette.text.secondary, 0.05)} 1px, transparent 1px)`,
              backgroundSize: '24px 24px',
              pointerEvents: 'none',
            }}
          />
          <AtlasGraph
            viewport={viewport}
            layout={graphQuery.isError ? { ...atlas, nodes: [] } : shownLayout}
            edges={graph?.edges ?? []}
            config={config}
            viewMode={VIEW_MODE}
            informationDensity={density}
            selectedNodeId={selectedId}
            selectedIsCore={selectedNode?.memory_type === 'book'}
            onSelect={node => setSelectedId(node ? node.id : null)}
            loading={loading && !graphQuery.isError}
            empty={emptyState}
            fallbackSubtitle={node => node.subtitle || kindLabel(node.memory_type)}
            renderNodeBadge={node =>
              node.metadata.starved === true ? (
                <ThemedTooltip title={t('graph.panel.starvedShort')}>
                  <Box
                    component="span"
                    sx={{
                      display: 'inline-block',
                      width: 7,
                      height: 7,
                      ml: 0.5,
                      borderRadius: '50%',
                      bgcolor: theme.palette.warning.main,
                      flexShrink: 0,
                    }}
                  />
                </ThemedTooltip>
              ) : null
            }
          />
          <AtlasLegend
            clusters={legendClusters}
            edges={legendEdges}
            focusCluster={focusCluster}
            onFocusCluster={cluster => {
              setSelectedId(null)
              setFocusCluster(cluster)
            }}
            notice={legendNotice}
            defaultCollapsed={isMobile}
          />
          <AtlasZoomControl viewport={viewport} />
          {graphQuery.isFetching && !graphQuery.isPending && (
            <Box sx={{ position: 'absolute', right: 16, top: 16, zIndex: 6, display: 'flex' }}>
              <CircularProgress size={18} />
            </Box>
          )}
        </Box>

        {isWide && (
          <Box
            sx={{
              width: selectedNode ? PANEL_WIDTH : 0,
              ml: selectedNode ? 1.5 : 0,
              flexShrink: 0,
              overflow: 'hidden',
              transition: 'width 0.22s ease, margin 0.22s ease',
              display: 'flex',
            }}
          >
            <Box sx={{ width: PANEL_WIDTH, flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
              {panel}
            </Box>
          </Box>
        )}
      </Box>

      {!isWide && (
        <Drawer
          anchor="right"
          open={!!selectedNode}
          onClose={() => setSelectedId(null)}
          PaperProps={{ sx: { width: 'min(420px, 100%)' } }}
        >
          {panel}
        </Drawer>
      )}
    </Box>
  )
}
