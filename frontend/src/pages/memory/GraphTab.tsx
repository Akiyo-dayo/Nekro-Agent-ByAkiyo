import { useEffect, useMemo, useRef, useState } from 'react'
import { Box, CircularProgress, Drawer, useMediaQuery } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'
import FavoriteRoundedIcon from '@mui/icons-material/FavoriteRounded'
import ActionButton from '../../components/common/ActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import GraphNodePanel, { type GraphNodeNeighbour } from '../../components/memory/GraphNodePanel'
import MemoryItemDialog from '../../components/memory/MemoryItemDialog'
import MemoryNotFoundState from '../../components/memory/MemoryNotFoundState'
import ScopeFilterBar from '../../components/memory/ScopeFilterBar'
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
import { createMemoryAtlasConfig } from '../../components/memory/atlas/memoryAtlasConfig'
import type { AtlasGraphEdgeData, AtlasViewMode, InformationDensity } from '../../components/memory/atlas/types'
import { useAtlasViewport } from '../../components/memory/atlas/useAtlasViewport'
import {
  DEFAULT_MEMORY_FILTERS,
  MEMORY_TAB_FILTER_FIELDS,
  filtersFromSearchParams,
  filtersToGraphQuery,
  filtersToSearchParams,
  type MemoryFilterState,
} from '../../components/memory/filters'
import { isMilestoneNode } from '../../components/memory/graphNodes'
import { useMemoryScopeOptions } from '../../components/memory/useMemoryScopeOptions'
import { chatChannelPath, memoryPath } from '../../router/routes'
import { isNotFoundError } from '../../services/api/axios'
import { memoryApi, type MemoryScopeParams, type MemoryType } from '../../services/api/memory'
import { BORDER_RADIUS } from '../../theme/variants'

/**
 * 「图谱」标签：把当前范围里的记忆画成一张图
 *
 * 画布、布局与工作区记忆页是同一份（`components/memory/atlas`）。版式：筛选与图谱工具条（视图、密度、
 * 显示失活）在画布上方同一行；画布左上是图例（每簇显示多少 / 共多少，点一行只看这一簇），右下是缩放；
 * 点节点后宽屏在右边让出一栏放详情，窄屏从右侧抽屉滑出。数据与段落列表同一个取数范围（看得到域记忆），
 * 多一个「人」簇，关系里程碑段落带一枚小心形。
 *
 * 筛选进地址栏，视图 / 密度 / 显示失活 / 只看哪一簇是看图的手感，留在本地状态里。
 */

interface GraphTabProps {
  scope: MemoryScopeParams
  isScopeReady: boolean
}

const GRAPH_FIELDS: Array<keyof MemoryFilterState> = [...MEMORY_TAB_FILTER_FIELDS.graph]

const PANEL_WIDTH = 360

/** 图例里的线型：几种边并成一行（同一种画法、同一种意思） */
const EDGE_LEGEND: Array<{ key: string; types: string[] }> = [
  { key: 'episode', types: ['episode_paragraph', 'episode_entity'] },
  { key: 'paragraphEntity', types: ['paragraph_entity'] },
  { key: 'paragraphPerson', types: ['paragraph_person'] },
  { key: 'relation', types: ['relation_paragraph', 'relation_subject', 'relation_object'] },
]

function sampleEdge(edgeType: string): AtlasGraphEdgeData {
  return {
    id: edgeType,
    source: '',
    target: '',
    edge_type: edgeType,
    label: '',
    weight: 0,
    strength: 0,
    status: 'active',
    cognitive_type: null,
    metadata: {},
  }
}

export default function GraphTab({ scope, isScopeReady }: GraphTabProps) {
  const theme = useTheme()
  const isWide = useMediaQuery(theme.breakpoints.up('lg'))
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { t } = useTranslation('memory')
  const { t: tAtlas } = useTranslation('atlas')
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => filtersFromSearchParams(searchParams), [searchParams])

  const [viewMode, setViewMode] = useState<AtlasViewMode>('episode')
  const [density, setDensity] = useState<InformationDensity>('medium')
  const [includeInactive, setIncludeInactive] = useState(false)
  const [focusCluster, setFocusCluster] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [fullTarget, setFullTarget] = useState<{ memoryType: MemoryType; id: number } | null>(null)
  const viewport = useAtlasViewport()
  const { zoom } = viewport

  const config = useMemo(
    () =>
      createMemoryAtlasConfig({
        withPersons: true,
        titles: {
          episode: { title: t('graph.clusters.episode'), episodeTitle: t('graph.clusters.episodeCore') },
          semantic: { title: t('graph.clusters.semantic'), episodeTitle: t('graph.clusters.semanticCore') },
          episodic: { title: t('graph.clusters.episodic'), episodeTitle: t('graph.clusters.episodicCore') },
          relation: { title: t('graph.clusters.relation'), episodeTitle: t('graph.clusters.relationCore') },
          entity: { title: t('graph.clusters.entity'), episodeTitle: t('graph.clusters.entityCore') },
          person: { title: t('graph.clusters.person'), episodeTitle: t('graph.clusters.personCore') },
        },
      }),
    [t]
  )

  const graphParams = useMemo(() => filtersToGraphQuery(filters, includeInactive), [filters, includeInactive])
  const graphQuery = useQuery({
    queryKey: ['memory', 'graph', scope, graphParams],
    queryFn: () => memoryApi.getGraph({ ...graphParams, ...scope }),
    enabled: isScopeReady,
    retry: false,
  })
  // 段落的知识类型、实体的类型换成界面语言再画（工作区记忆页那边原样显示英文枚举，这里有现成的译文）
  const graph = useMemo(() => {
    const data = graphQuery.data
    if (!data) return undefined
    return {
      ...data,
      nodes: data.nodes.map(node => {
        if (!node.subtitle) return node
        if (node.memory_type === 'paragraph') {
          return { ...node, subtitle: t(`knowledge.${node.subtitle}`, { defaultValue: node.subtitle }) }
        }
        if (node.memory_type === 'entity') {
          return { ...node, subtitle: t(`graph.entityType.${node.subtitle}`, { defaultValue: node.subtitle }) }
        }
        return node
      }),
    }
  }, [graphQuery.data, t])

  // 数据很多（上千个节点）时，第一次画先退到「事件视图 + 疏」，之后用户怎么调都听用户的
  const heavyHandledRef = useRef(false)
  const totalNodes = graph?.nodes.length ?? 0
  useEffect(() => {
    if (heavyHandledRef.current || totalNodes <= ATLAS_HEAVY_GRAPH) return
    heavyHandledRef.current = true
    setViewMode('episode')
    setDensity('low')
  }, [totalNodes])

  const atlas = useMemo(
    () => buildAtlasLayout(theme, graph, zoom, density, viewMode, config),
    [config, density, graph, theme, viewMode, zoom]
  )
  const shownLayout = useMemo(
    () => (focusCluster ? { ...atlas, nodes: atlas.nodes.filter(node => node.cluster === focusCluster) } : atlas),
    [atlas, focusCluster]
  )
  const nodeById = useMemo(() => new Map((graph?.nodes ?? []).map(node => [node.id, node])), [graph])
  const selectedNode = selectedId ? nodeById.get(selectedId) ?? null : null
  const selectedAccent = selectedNode ? clusterAccent(config, config.resolveCluster(selectedNode)) : theme.palette.info.main

  const neighbours = useMemo<GraphNodeNeighbour[]>(() => {
    if (!selectedId || !graph) return []
    const seen = new Set<string>()
    const result: GraphNodeNeighbour[] = []
    graph.edges.forEach(edge => {
      if (edge.source !== selectedId && edge.target !== selectedId) return
      const otherId = edge.source === selectedId ? edge.target : edge.source
      if (seen.has(otherId)) return
      const other = nodeById.get(otherId)
      if (!other) return
      seen.add(otherId)
      result.push({
        id: other.id,
        label: other.label,
        memoryType: other.memory_type,
        color: clusterAccent(config, config.resolveCluster(other)),
      })
    })
    return result
  }, [config, graph, nodeById, selectedId])

  // 视图一换，原先只看的那一簇可能在新视图里不画（例如事件视图没有关系簇），退回全部
  useEffect(() => {
    if (focusCluster && !legendClusterOrder(config, viewMode).includes(focusCluster)) setFocusCluster(null)
  }, [config, focusCluster, viewMode])

  const loading = graphQuery.isPending
  const legendClusters = legendClusterOrder(config, viewMode).map(cluster => ({
    key: cluster,
    label: clusterTitle(config, cluster, viewMode),
    color: clusterAccent(config, cluster),
    shown: loading || graphQuery.isError ? null : atlas.clusterStats[cluster]?.shown ?? 0,
    total: loading || graphQuery.isError ? null : atlas.clusterStats[cluster]?.total ?? 0,
  }))
  const legendEdges = useMemo<AtlasLegendEdge[]>(() => {
    const present = presentEdgeTypes(graph?.edges)
    return EDGE_LEGEND.filter(entry => entry.types.some(type => present.has(type))).map(entry => ({
      key: entry.key,
      label: tAtlas(`edges.${entry.key}`),
      dash: config.edgeStyle(sampleEdge(entry.types.find(type => present.has(type)) ?? entry.types[0])).dash,
    }))
  }, [config, graph, tAtlas])
  const legendNotice =
    totalNodes > ATLAS_HEAVY_GRAPH || atlas.capped ? tAtlas('legend.heavy', { count: atlas.nodes.length }) : null

  const optionsQuery = useMemoryScopeOptions(scope)
  const channelNames = useMemo(() => {
    const map = new Map<string, string>()
    for (const channel of optionsQuery.data?.channels ?? []) {
      map.set(channel.chat_key, channel.channel_name || channel.chat_key)
    }
    return map
  }, [optionsQuery.data])
  const channelLabel = (chatKey: string | null) => (chatKey ? channelNames.get(chatKey) ?? chatKey : '—')

  const typeLabel = (memoryType: string) =>
    memoryType === 'person' ? t('graph.kind.person') : t(`kind.${memoryType}`, { defaultValue: memoryType })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['memory'] })
  }

  const handleFiltersChange = (next: MemoryFilterState) => {
    setSelectedId(null)
    setSearchParams(filtersToSearchParams({ ...next, page: 1 }), { replace: true })
  }

  const hasGraphFilters = GRAPH_FIELDS.some(field => filters[field] !== DEFAULT_MEMORY_FILTERS[field])
  const clearGraphFilters = () =>
    handleFiltersChange({
      ...filters,
      ...Object.fromEntries(GRAPH_FIELDS.map(field => [field, DEFAULT_MEMORY_FILTERS[field]])),
    })

  const focusPerson = (account: string) => handleFiltersChange({ ...filters, person: account })

  /** 到「记忆条目」里看这个人：带上当前的范围筛选，换成这个人 */
  const showPersonInItems = (account: string) => {
    const params = filtersToSearchParams({
      ...DEFAULT_MEMORY_FILTERS,
      chatKey: filters.chatKey,
      presetId: filters.presetId,
      partition: filters.partition,
      domain: filters.domain,
      timeFrom: filters.timeFrom,
      timeTo: filters.timeTo,
      person: account,
    })
    navigate(memoryPath('items', Object.fromEntries(params.entries())))
  }

  const panel = selectedNode ? (
    <GraphNodePanel
      key={selectedNode.id}
      node={selectedNode}
      accent={selectedAccent}
      scope={scope}
      neighbours={neighbours}
      variant={isWide ? 'column' : 'drawer'}
      channelLabel={channelLabel}
      onClose={() => setSelectedId(null)}
      onChanged={invalidate}
      onOpenFull={(memoryType, id) => setFullTarget({ memoryType, id })}
      onOpenChannel={chatKey => navigate(chatChannelPath(chatKey, 'memory'))}
      onSelectNode={setSelectedId}
      onFocusPerson={focusPerson}
      onShowPersonInItems={showPersonInItems}
    />
  ) : null

  const emptyState = graphQuery.isError ? (
    <AtlasEmptyState>
      {isNotFoundError(graphQuery.error) ? (
        <MemoryNotFoundState compact />
      ) : (
        <QueryErrorState error={graphQuery.error} onRetry={() => void graphQuery.refetch()} compact />
      )}
    </AtlasEmptyState>
  ) : (
    <AtlasEmptyState
      title={tAtlas('empty.memoryTitle')}
      description={hasGraphFilters ? tAtlas('empty.memoryFiltered') : tAtlas('empty.memoryUnfiltered')}
      action={
        hasGraphFilters ? (
          <ActionButton tone="ghost" size="small" onClick={clearGraphFilters}>
            {tAtlas('empty.clearFilters')}
          </ActionButton>
        ) : undefined
      }
    />
  )

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0, flex: 1 }}>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', gap: 1 }}>
        <Box sx={{ flex: '1 1 560px', minWidth: 0 }}>
          <ScopeFilterBar value={filters} onChange={handleFiltersChange} fields={GRAPH_FIELDS} scope={scope} />
        </Box>
        <Box sx={{ flex: '0 0 auto', ml: 'auto' }}>
          <AtlasToolbar
            viewMode={viewMode}
            onViewModeChange={value => {
              setSelectedId(null)
              setViewMode(value)
            }}
            density={density}
            onDensityChange={setDensity}
            showInactive={{
              value: includeInactive,
              onChange: value => {
                setSelectedId(null)
                setIncludeInactive(value)
              },
            }}
          />
        </Box>
      </Box>

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
            viewMode={viewMode}
            informationDensity={density}
            selectedNodeId={selectedId}
            selectedIsCore={selectedNode?.memory_type === 'episode'}
            onSelect={node => setSelectedId(node ? node.id : null)}
            loading={loading && !graphQuery.isError}
            empty={emptyState}
            fallbackSubtitle={node =>
              node.memory_type === 'episode' ? t('graph.episodeFallback') : typeLabel(node.memory_type)
            }
            renderNodeBadge={node =>
              isMilestoneNode(node) ? (
                <FavoriteRoundedIcon
                  titleAccess={t('origin.affection')}
                  sx={{ fontSize: 11, color: theme.palette.secondary.main, flexShrink: 0 }}
                />
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
            <Box sx={{ width: PANEL_WIDTH, flexShrink: 0, display: 'flex', flexDirection: 'column' }}>{panel}</Box>
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

      <MemoryItemDialog
        memoryType={fullTarget?.memoryType ?? 'paragraph'}
        itemId={fullTarget?.id ?? null}
        scope={scope}
        onClose={() => setFullTarget(null)}
        onChanged={invalidate}
      />
    </Box>
  )
}
