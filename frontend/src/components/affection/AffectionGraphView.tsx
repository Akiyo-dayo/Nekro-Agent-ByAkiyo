import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Box, Chip, CircularProgress, Divider, Drawer, Stack, Typography, useMediaQuery } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import ActionButton from '../common/ActionButton'
import IconActionButton from '../common/IconActionButton'
import QueryErrorState from '../common/QueryErrorState'
import StatusChip from '../common/StatusChip'
import PersonCardDrawer from '../person/PersonCardDrawer'
import AtlasEmptyState from '../memory/atlas/AtlasEmptyState'
import AtlasGraph from '../memory/atlas/AtlasGraph'
import AtlasLegend, { type AtlasLegendEdge } from '../memory/atlas/AtlasLegend'
import AtlasToolbar from '../memory/atlas/AtlasToolbar'
import AtlasZoomControl from '../memory/atlas/AtlasZoomControl'
import {
  ATLAS_HEAVY_GRAPH,
  buildAtlasLayout,
  clusterAccent,
  clusterTitle,
  legendClusterOrder,
  presentEdgeTypes,
} from '../memory/atlas/layout'
import type { AtlasViewMode, InformationDensity } from '../memory/atlas/types'
import { useAtlasViewport } from '../memory/atlas/useAtlasViewport'
import { isNotFoundError } from '../../services/api/axios'
import {
  affectionApi,
  type AffectionGraphNode,
  type AffectionGraphRecord,
  type AffectionGraphResponse,
  type AffectionStageValue,
} from '../../services/api/affection'
import { BORDER_RADIUS, CHIP_VARIANTS, SCROLLBAR_VARIANTS } from '../../theme/variants'
import { NEGATIVE_STAGES, affectionStageColor, createAffectionAtlasConfig } from './affectionAtlasConfig'

/**
 * 好感关系图：人设在中间，人、关系里程碑、频道围在外面
 *
 * 画布、工具条、图例、缩放与记忆图谱是同一套（`components/memory/atlas`）：工具条（视图、密度）在
 * 画布上方，图例在画布左上，缩放在右下；点节点后宽屏在右边让出一栏放详情，窄屏从右侧抽屉滑出。
 * 人到人设的线常显，粗细看分数、颜色看阶段（`affectionStageColor`）；人与人之间不画线。
 * 点一个人看他在各个人设下的那几份好感、称呼、关系里程碑与最近一次出现；「完整详情」打开好感页
 * 原有的详情对话框（调分、重置、编辑印象都在那里）。
 *
 * 搜索与阶段筛选沿用好感页顶上那两个，作用域跟随顶栏的实例切换器。
 */

interface AffectionGraphViewProps {
  scopeParam?: string
  /** 适配器：人物卡要适配器 + 实例 + 账号三样齐全才给入口 */
  adapterKey?: string
  isScopeReady: boolean
  search: string
  stage?: AffectionStageValue
  /**
   * 页面列表用的阶段配色。关系图改用主题里的 `affectionStageColor`（亲密不再是报错红），
   * 这个参数保留只是为了不动调用方，图里不再读它。
   */
  stageColor?: (stage: AffectionStageValue) => string
  onOpenDetail: (recordId: number) => void
}

// 一次最多画多少份好感（最近更新的在前）；服务端另有上限
const GRAPH_LIMIT = 150
const PANEL_WIDTH = 360

function metaString(metadata: Record<string, unknown>, key: string): string {
  const value = metadata[key]
  return typeof value === 'string' ? value : ''
}

function metaNumber(metadata: Record<string, unknown>, key: string): number {
  const value = metadata[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function isRecord(value: unknown): value is AffectionGraphRecord {
  return typeof value === 'object' && value !== null && typeof (value as { record_id?: unknown }).record_id === 'number'
}

function recordsOf(node: AffectionGraphNode): AffectionGraphRecord[] {
  const value = node.metadata.records
  return Array.isArray(value) ? value.filter(isRecord) : []
}

function formatTime(value: string | null | undefined, fallback: string): string {
  if (!value) return fallback
  const timestamp = new Date(value).getTime()
  return Number.isNaN(timestamp) ? fallback : new Date(timestamp).toLocaleString()
}

function StatChip({ label, color }: { label: string; color: string }) {
  return <Chip size="small" label={label} sx={CHIP_VARIANTS.getCustomColorChip(color, true)} />
}

export default function AffectionGraphView({
  scopeParam,
  adapterKey,
  isScopeReady,
  search,
  stage,
  onOpenDetail,
}: AffectionGraphViewProps) {
  const theme = useTheme()
  const isWide = useMediaQuery(theme.breakpoints.up('lg'))
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))
  const { t } = useTranslation('affection')
  const { t: tAtlas } = useTranslation('atlas')
  const [viewMode, setViewMode] = useState<AtlasViewMode>('episode')
  const [density, setDensity] = useState<InformationDensity>('medium')
  const [focusCluster, setFocusCluster] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [cardAccount, setCardAccount] = useState<string | null>(null)
  const viewport = useAtlasViewport()
  const { zoom } = viewport
  const canOpenPersonCard = Boolean(adapterKey && scopeParam)

  const stageColor = useMemo(() => (value: AffectionStageValue) => affectionStageColor(theme, value), [theme])

  const config = useMemo(
    () =>
      createAffectionAtlasConfig({
        titles: {
          preset: { title: t('graph.clusters.preset'), episodeTitle: t('graph.clusters.preset') },
          person: { title: t('graph.clusters.person') },
          milestone: { title: t('graph.clusters.milestone') },
          channel: { title: t('graph.clusters.channel') },
        },
        stageColor,
      }),
    [stageColor, t]
  )

  const graphQuery = useQuery({
    queryKey: ['affection', 'graph', scopeParam, search.trim(), stage],
    queryFn: () =>
      affectionApi.getGraph({
        search: search.trim() || undefined,
        stage,
        limit: GRAPH_LIMIT,
        instance_key: scopeParam,
      }),
    enabled: isScopeReady,
    retry: false,
  })
  const graph: AffectionGraphResponse | undefined = graphQuery.data

  // 数据很多时第一次画先退到「星形 + 疏」，之后听用户的
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

  useEffect(() => {
    if (focusCluster && !legendClusterOrder(config, viewMode).includes(focusCluster)) setFocusCluster(null)
  }, [config, focusCluster, viewMode])

  const kindLabel = (kind: string) => t(`graph.kind.${kind}`, { defaultValue: kind })

  const loading = graphQuery.isPending
  const legendClusters = legendClusterOrder(config, viewMode).map(cluster => ({
    key: cluster,
    label: clusterTitle(config, cluster, viewMode),
    color: clusterAccent(config, cluster),
    shown: loading || graphQuery.isError ? null : atlas.clusterStats[cluster]?.shown ?? 0,
    total: loading || graphQuery.isError ? null : atlas.clusterStats[cluster]?.total ?? 0,
  }))
  const legendEdges = useMemo<AtlasLegendEdge[]>(() => {
    const edges = graph?.edges ?? []
    const present = presentEdgeTypes(edges)
    const result: AtlasLegendEdge[] = []
    if (present.has('affection')) {
      result.push({ key: 'affection', label: tAtlas('edges.affection'), color: theme.palette.primary.main })
      const hasNegative = edges.some(
        edge => edge.edge_type === 'affection' && NEGATIVE_STAGES.has(String(edge.metadata.stage ?? ''))
      )
      if (hasNegative) {
        result.push({
          key: 'affectionNegative',
          label: tAtlas('edges.affectionNegative'),
          dash: '5 5',
          color: theme.palette.warning.main,
        })
      }
    }
    if (present.has('milestone')) result.push({ key: 'milestone', label: tAtlas('edges.milestone'), dash: '3 5' })
    if (present.has('last_seen')) result.push({ key: 'lastSeen', label: tAtlas('edges.lastSeen'), dash: '6 6' })
    return result
  }, [graph, tAtlas, theme])
  // 服务端只给了最近更新的一部分时说清楚；否则数据很多被前端截断时提示
  const legendNotice = graph
    ? graph.total > graph.drawn + graph.skipped_merged
      ? t('graph.truncated', { drawn: graph.drawn, total: graph.total })
      : totalNodes > ATLAS_HEAVY_GRAPH || atlas.capped
        ? tAtlas('legend.heavy', { count: atlas.nodes.length })
        : graph.skipped_merged > 0
          ? t('graph.skippedMerged', { count: graph.skipped_merged })
          : null
    : null

  const filtered = !!search.trim() || !!stage
  const emptyState = graphQuery.isError ? (
    isNotFoundError(graphQuery.error) ? (
      <AtlasEmptyState title={tAtlas('empty.affectionTitle')} description={t('graph.notFound')} />
    ) : (
      <AtlasEmptyState>
        <QueryErrorState error={graphQuery.error} onRetry={() => void graphQuery.refetch()} compact />
      </AtlasEmptyState>
    )
  ) : (
    <AtlasEmptyState
      title={tAtlas('empty.affectionTitle')}
      description={filtered ? tAtlas('empty.affectionFiltered') : tAtlas('empty.affectionUnfiltered')}
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
      <Stack direction="row" spacing={0.75} alignItems="center" sx={{ px: 1.5, pt: 1.25, pb: 1, flexShrink: 0 }}>
        <Stack direction="row" spacing={0.75} alignItems="center" sx={{ flex: 1, minWidth: 0 }}>
          <StatChip label={kindLabel(selectedNode.memory_type)} color={selectedAccent} />
          {selectedNode.memory_type === 'milestone' && selectedNode.status !== 'active' && (
            <StatusChip
              tone="inactive"
              label={t(`detail.milestonesStatus.${selectedNode.status}`, { defaultValue: selectedNode.status })}
            />
          )}
        </Stack>
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
        <NodeDetail
          node={selectedNode}
          graph={graph}
          nodeById={nodeById}
          stageColor={stageColor}
          onOpenDetail={onOpenDetail}
          onSelect={setSelectedId}
          canOpenPersonCard={canOpenPersonCard}
          onOpenPersonCard={setCardAccount}
        />
      </Box>
    </Box>
  ) : null

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, flex: 1, minHeight: 0 }}>
      <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
        <AtlasToolbar
          viewMode={viewMode}
          onViewModeChange={value => {
            setSelectedId(null)
            setViewMode(value)
          }}
          viewLabels={{ episode: t('graph.toolbar.viewStar'), atomic: t('graph.toolbar.viewClusters') }}
          density={density}
          onDensityChange={setDensity}
        />
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
            selectedIsCore={selectedNode?.memory_type === 'preset'}
            onSelect={node => setSelectedId(node ? node.id : null)}
            loading={loading && !graphQuery.isError}
            empty={emptyState}
            fallbackSubtitle={node => kindLabel(node.memory_type)}
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
      {canOpenPersonCard && (
        <PersonCardDrawer
          open={cardAccount !== null}
          onClose={() => setCardAccount(null)}
          adapterKey={adapterKey ?? ''}
          instanceKey={scopeParam ?? ''}
          account={cardAccount ?? ''}
        />
      )}
    </Box>
  )
}

interface NodeDetailProps {
  node: AffectionGraphNode
  graph: AffectionGraphResponse | undefined
  nodeById: Map<string, AffectionGraphNode>
  stageColor: (stage: AffectionStageValue) => string
  onOpenDetail: (recordId: number) => void
  onSelect: (nodeId: string) => void
  canOpenPersonCard: boolean
  onOpenPersonCard: (account: string) => void
}

/** 右侧面板的正文：按节点种类给不同的内容（类型标签与关闭在面板头上） */
function NodeDetail({
  node,
  graph,
  nodeById,
  stageColor,
  onOpenDetail,
  onSelect,
  canOpenPersonCard,
  onOpenPersonCard,
}: NodeDetailProps) {
  const theme = useTheme()
  const { t } = useTranslation('affection')
  const { t: tPerson } = useTranslation('person')
  const edges = graph?.edges ?? []
  const neighbours = (kind: string) =>
    edges
      .filter(edge => edge.source === node.id || edge.target === node.id)
      .map(edge => nodeById.get(edge.source === node.id ? edge.target : edge.source))
      .filter((item): item is AffectionGraphNode => item !== undefined && item.memory_type === kind)
  const none = t('graph.panel.none')
  const surface = { borderRadius: BORDER_RADIUS.DEFAULT, bgcolor: alpha(theme.palette.background.default, 0.5) }

  const fieldBox = (label: string, value: ReactNode) => (
    <Box sx={{ px: 1, py: 0.85, minWidth: 0, ...surface }}>
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
      <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>
        {value}
      </Typography>
    </Box>
  )

  const neighbourList = (items: AffectionGraphNode[]) =>
    items.length === 0 ? (
      <Typography variant="body2" color="text.secondary">
        {none}
      </Typography>
    ) : (
      <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
        {items.map(item => (
          <Chip key={item.id} size="small" label={item.label || item.id} onClick={() => onSelect(item.id)} />
        ))}
      </Stack>
    )

  if (node.memory_type === 'person') {
    const records = recordsOf(node)
    const milestones = neighbours('milestone')
    const lastChat = metaString(node.metadata, 'last_chat_key')
    const lastChannel = lastChat ? nodeById.get(`channel-${lastChat}`) : undefined
    const callName = metaString(node.metadata, 'call_name')
    return (
      <Stack spacing={1.5}>
        <Box>
          <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap" useFlexGap>
            <Typography variant="h6" sx={{ fontWeight: 800, wordBreak: 'break-word', lineHeight: 1.3 }}>
              {node.label}
            </Typography>
            {callName && <StatChip label={callName} color={theme.palette.secondary.main} />}
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace' }}>
            {metaString(node.metadata, 'account')}
          </Typography>
          {canOpenPersonCard && (
            <Box sx={{ mt: 0.5 }}>
              <ActionButton
                tone="ghost"
                size="small"
                startIcon={<PersonOutlineIcon />}
                onClick={() => onOpenPersonCard(metaString(node.metadata, 'account'))}
                disabled={!metaString(node.metadata, 'account')}
              >
                {tPerson('open')}
              </ActionButton>
            </Box>
          )}
        </Box>
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 1 }}>
          {fieldBox(t('graph.panel.lastSeen'), formatTime(metaString(node.metadata, 'last_interact_at'), none))}
          {fieldBox(t('graph.panel.lastChannel'), lastChannel?.label || lastChat || none)}
        </Box>
        <Divider />
        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
          {t('graph.panel.records')}
        </Typography>
        <Stack spacing={0.9}>
          {records.map(record => (
            <Box key={record.record_id} sx={{ p: 1, ...surface }}>
              <Stack direction="row" spacing={0.6} alignItems="center" flexWrap="wrap" useFlexGap>
                <Typography variant="body2" sx={{ fontWeight: 700 }}>
                  {record.preset_name}
                </Typography>
                <StatChip
                  label={t('graph.panel.stageScore', { stage: record.stage_label, score: record.score })}
                  color={stageColor(record.stage)}
                />
              </Stack>
              {record.call_name && (
                <Typography variant="caption" color="text.secondary" display="block">
                  {t('graph.panel.callNameLine', { name: record.call_name })}
                </Typography>
              )}
              {record.state_text && (
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.4, wordBreak: 'break-word' }}>
                  {record.state_text}
                </Typography>
              )}
              <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.4 }}>
                {t('graph.panel.recordMeta', {
                  time: formatTime(record.last_interact_at, none),
                  count: record.interaction_count,
                })}
              </Typography>
              <ActionButton
                tone="ghost"
                size="small"
                startIcon={<OpenInNewIcon />}
                onClick={() => onOpenDetail(record.record_id)}
                sx={{ mt: 0.5 }}
              >
                {t('graph.panel.openDetail')}
              </ActionButton>
            </Box>
          ))}
        </Stack>
        <Divider />
        <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
          {t('graph.panel.milestones', { count: metaNumber(node.metadata, 'milestone_count') })}
        </Typography>
        {milestones.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            {none}
          </Typography>
        ) : (
          <Stack spacing={0.6}>
            {milestones.map(item => (
              <Box
                key={item.id}
                onClick={() => onSelect(item.id)}
                sx={{
                  cursor: 'pointer',
                  p: 0.8,
                  ...surface,
                  '&:hover': { bgcolor: alpha(theme.palette.primary.main, 0.06) },
                }}
              >
                <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>
                  {item.label || t('graph.panel.milestoneHidden')}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {item.subtitle}
                  {item.status !== 'active' ? ` · ${t(`detail.milestonesStatus.${item.status}`, { defaultValue: item.status })}` : ''}
                </Typography>
              </Box>
            ))}
          </Stack>
        )}
      </Stack>
    )
  }

  if (node.memory_type === 'milestone') {
    const recordId = metaNumber(node.metadata, 'record_id')
    const content = metaString(node.metadata, 'content')
    return (
      <Stack spacing={1.5}>
        <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
          {content || t('graph.panel.milestoneHidden')}
        </Typography>
        {fieldBox(t('graph.panel.eventTime'), formatTime(metaString(node.metadata, 'event_time'), none))}
        <Box>
          <Typography variant="caption" color="text.secondary">
            {t('graph.panel.person')}
          </Typography>
          <Box sx={{ mt: 0.5 }}>{neighbourList(neighbours('person'))}</Box>
        </Box>
        {recordId > 0 && (
          <ActionButton tone="ghost" size="small" startIcon={<OpenInNewIcon />} onClick={() => onOpenDetail(recordId)}>
            {t('graph.panel.openDetail')}
          </ActionButton>
        )}
      </Stack>
    )
  }

  if (node.memory_type === 'channel') {
    return (
      <Stack spacing={1.5}>
        <Box>
          <Typography variant="body1" sx={{ fontWeight: 800, wordBreak: 'break-word' }}>
            {node.label}
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-all' }}>
            {metaString(node.metadata, 'chat_key')}
          </Typography>
        </Box>
        <Box>
          <Typography variant="caption" color="text.secondary">
            {t('graph.panel.seenHere')}
          </Typography>
          <Box sx={{ mt: 0.5 }}>{neighbourList(neighbours('person'))}</Box>
        </Box>
      </Stack>
    )
  }

  // 人设：这一份人设下的人，按分数从高到低
  const people = edges
    .filter(edge => edge.target === node.id && edge.edge_type === 'affection')
    .sort((a, b) => b.weight - a.weight)
    .map(edge => ({ edge, person: nodeById.get(edge.source) }))
    .filter((item): item is { edge: (typeof edges)[number]; person: AffectionGraphNode } => item.person !== undefined)
  return (
    <Stack spacing={1.5}>
      <Box>
        <Typography variant="body1" sx={{ fontWeight: 800, wordBreak: 'break-word' }}>
          {node.label}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {metaString(node.metadata, 'domain_label')}
        </Typography>
      </Box>
      <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 1 }}>
        {fieldBox(t('graph.panel.personCount'), metaNumber(node.metadata, 'person_count').toLocaleString())}
        {fieldBox(t('graph.panel.inUseChannels'), metaNumber(node.metadata, 'in_use_channels').toLocaleString())}
      </Box>
      <Divider />
      <Stack spacing={0.5}>
        {people.map(({ edge, person }) => {
          const stage = typeof edge.metadata.stage === 'string' ? (edge.metadata.stage as AffectionStageValue) : 'stranger'
          return (
            <Stack
              key={edge.id}
              direction="row"
              spacing={0.75}
              alignItems="center"
              onClick={() => onSelect(person.id)}
              sx={{
                cursor: 'pointer',
                p: 0.6,
                borderRadius: BORDER_RADIUS.DEFAULT,
                '&:hover': { bgcolor: alpha(theme.palette.primary.main, 0.06) },
              }}
            >
              <Typography variant="body2" sx={{ flex: 1, minWidth: 0 }} noWrap>
                {person.label}
              </Typography>
              <StatChip label={t('graph.panel.stageScore', { stage: edge.label, score: edge.weight })} color={stageColor(stage)} />
            </Stack>
          )
        })}
      </Stack>
    </Stack>
  )
}
