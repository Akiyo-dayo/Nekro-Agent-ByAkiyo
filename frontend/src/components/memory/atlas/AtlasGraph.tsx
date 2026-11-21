import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent, type ReactNode } from 'react'
import { Box, CircularProgress, Stack, Typography } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useTranslation } from 'react-i18next'
import AcUnitRoundedIcon from '@mui/icons-material/AcUnitRounded'
import BlockRoundedIcon from '@mui/icons-material/BlockRounded'
import ShieldRoundedIcon from '@mui/icons-material/ShieldRounded'
import { AMBIENT_EDGE_CAP, WORLD_BOUNDS, clusterAccent, clusterTitleAndCenter } from './layout'
import type {
  AtlasClusterConfig,
  AtlasGraphEdgeData,
  AtlasGraphNodeData,
  AtlasLaidOutNode,
  AtlasLayout,
  AtlasViewMode,
  InformationDensity,
} from './types'
import type { AtlasViewport } from './useAtlasViewport'

/**
 * 记忆图谱画布
 *
 * 只管画与交互：拖动平移、滚轮平移、Ctrl / Command + 滚轮缩放、点节点选中、点空白取消；选中一个
 * 节点时把连着它的边画出来、其余节点调暗；事件视图里选中核心节点（事件）时只把它连着的留亮，
 * 并把视图移过去。悬停 300 毫秒后把这个节点的边淡淡地预览出来。节点放在哪、什么颜色由调用方用
 * `buildAtlasLayout` 算好传进来，缩放与平移来自调用方的 `useAtlasViewport`（缩放浮层要读同一份）。
 *
 * 视觉语汇：尺寸只表示重要度，颜色只表示簇的角色，状态只用叠加表现——失活 / 隔离是虚线描边加灰点，
 * 隔离、冻结、保护在名字后面跟一枚小图标；选中是节点色实线描边外加一圈主色细环。画布上不出现红色。
 */

export interface AtlasGraphProps<T extends AtlasGraphNodeData, C> {
  viewport: AtlasViewport
  layout: AtlasLayout<T>
  edges: AtlasGraphEdgeData[]
  config: AtlasClusterConfig<C>
  viewMode: AtlasViewMode
  informationDensity: InformationDensity
  /** 选中节点的 ID（`<类型>-<编号>`）；没选中为 null */
  selectedNodeId: string | null
  /** 选中的是不是核心簇的节点（事件视图下只留它连着的亮着）；选中节点可能因为密度没画出来，所以由调用方给 */
  selectedIsCore: boolean
  onSelect: (node: AtlasLaidOutNode<T> | null) => void
  loading: boolean
  /** 没有可画节点时显示的东西（画布中央） */
  empty: ReactNode
  /** 画布顶边（给画布内的其它浮层让位；缺省贴边） */
  top?: { xs: string | number; sm: string | number }
  /** 节点副标题为空时显示什么 */
  fallbackSubtitle: (node: AtlasLaidOutNode<T>) => string
  /** 节点名字后面的小标记（例如关系里程碑） */
  renderNodeBadge?: (node: AtlasLaidOutNode<T>) => ReactNode
}

const HOVER_PREVIEW_DELAY = 300
const INTERACTION_SETTLE_DELAY = 150

function truncateLabel(label: string, maxLength: number): string {
  if (label.length <= maxLength) return label
  return `${label.slice(0, maxLength - 1)}…`
}

function firstGlyph(label: string): string {
  return Array.from(label.trim())[0] ?? '?'
}

function nodeFlags(node: AtlasGraphNodeData) {
  return {
    inactive: node.status === 'inactive',
    quarantined: node.status === 'quarantined',
    frozen: node.metadata.is_frozen === true,
    protected: node.metadata.is_protected === true,
  }
}

export default function AtlasGraph<T extends AtlasGraphNodeData, C>({
  viewport,
  layout,
  edges,
  config,
  viewMode,
  informationDensity,
  selectedNodeId,
  selectedIsCore,
  onSelect,
  loading,
  empty,
  top = { xs: 0, sm: 0 },
  fallbackSubtitle,
  renderNodeBadge,
}: AtlasGraphProps<T, C>) {
  const theme = useTheme()
  const { t } = useTranslation('atlas')
  const {
    zoom,
    pan,
    sceneRef,
    getZoom,
    updateZoom,
    applyPanDelta,
    focusOn,
    syncViewportState,
    applySceneTransform,
  } = viewport
  const coreCluster = config.coreCluster
  const coreAccent = clusterAccent(config, coreCluster)
  const [isDragging, setIsDragging] = useState(false)
  const [isInteracting, setIsInteracting] = useState(false)
  const [hoverPreviewId, setHoverPreviewId] = useState<string | null>(null)
  const graphViewportRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef({ active: false, x: 0, y: 0 })
  const interactingRef = useRef(false)
  const settleTimerRef = useRef<number | null>(null)
  const hoverTimerRef = useRef<number | null>(null)

  // 拖动、缩放期间不画副标题（省掉一大片文字重排），停下 150 毫秒后恢复
  const markInteracting = useCallback(() => {
    if (!interactingRef.current) {
      interactingRef.current = true
      setIsInteracting(true)
    }
    if (settleTimerRef.current !== null) window.clearTimeout(settleTimerRef.current)
    settleTimerRef.current = window.setTimeout(() => {
      settleTimerRef.current = null
      interactingRef.current = false
      setIsInteracting(false)
    }, INTERACTION_SETTLE_DELAY)
  }, [])

  useEffect(
    () => () => {
      if (settleTimerRef.current !== null) window.clearTimeout(settleTimerRef.current)
      if (hoverTimerRef.current !== null) window.clearTimeout(hoverTimerRef.current)
    },
    []
  )

  const nodeById = useMemo(() => new Map(layout.nodes.map(node => [node.id, node])), [layout.nodes])
  const drawableEdges = useMemo(
    () => edges.filter(edge => nodeById.has(edge.source) && nodeById.has(edge.target)),
    [edges, nodeById]
  )

  const highlightedEdges = useMemo(() => {
    if (!selectedNodeId) return [] as AtlasGraphEdgeData[]
    return drawableEdges.filter(edge => edge.source === selectedNodeId || edge.target === selectedNodeId)
  }, [drawableEdges, selectedNodeId])
  const previewEdges = useMemo(() => {
    if (!hoverPreviewId || hoverPreviewId === selectedNodeId) return [] as AtlasGraphEdgeData[]
    return drawableEdges.filter(edge => edge.source === hoverPreviewId || edge.target === hoverPreviewId)
  }, [drawableEdges, hoverPreviewId, selectedNodeId])
  const selectedCoreRelatedNodeIds = useMemo(() => {
    const ids = new Set<string>()
    if (viewMode !== 'episode' || !selectedIsCore || !selectedNodeId) return ids
    ids.add(selectedNodeId)
    edges.forEach(edge => {
      if (edge.source === selectedNodeId || edge.target === selectedNodeId) {
        ids.add(edge.source)
        ids.add(edge.target)
      }
    })
    return ids
  }, [edges, selectedIsCore, selectedNodeId, viewMode])
  const ambientEdges = useMemo(() => {
    if (viewMode !== 'episode') return [] as AtlasGraphEdgeData[]
    // 失活、隔离的节点不参与环境连线
    const ambient = drawableEdges.filter(edge => {
      if (!config.ambientEdgeTypes.includes(edge.edge_type)) return false
      const source = nodeById.get(edge.source)
      const target = nodeById.get(edge.target)
      return !!source && !!target && source.status === 'active' && target.status === 'active'
    })
    if (selectedIsCore && selectedNodeId) {
      return ambient.filter(edge => edge.source === selectedNodeId || edge.target === selectedNodeId)
    }
    // 线太多时整片糊成一团，只在选中核心节点时画它那一部分
    return ambient.length > AMBIENT_EDGE_CAP ? [] : ambient
  }, [config.ambientEdgeTypes, drawableEdges, nodeById, selectedIsCore, selectedNodeId, viewMode])
  const detailIntensity =
    (informationDensity === 'high' ? 1 : informationDensity === 'medium' ? 0.75 : 0.55) +
    (zoom >= 1.28 ? 0.18 : zoom >= 1.08 ? 0.08 : 0)
  const zoomDetailLevel = zoom >= 1.32 ? 'high' : zoom >= 1.06 ? 'medium' : 'low'
  const highlightedNodeIds = useMemo(() => {
    const ids = new Set<string>()
    if (selectedNodeId) ids.add(selectedNodeId)
    highlightedEdges.forEach(edge => {
      ids.add(edge.source)
      ids.add(edge.target)
    })
    return ids
  }, [highlightedEdges, selectedNodeId])
  const selectedLaidOutNode = selectedNodeId ? nodeById.get(selectedNodeId) : undefined

  // 选中核心节点（事件）时把视图移过去；布局每次重算节点对象都会换，与上游一样跟着重新对准
  useEffect(() => {
    if (!selectedLaidOutNode) return
    if (selectedLaidOutNode.cluster !== coreCluster) return
    focusOn(selectedLaidOutNode.x, selectedLaidOutNode.y, 1.08)
  }, [coreCluster, focusOn, selectedLaidOutNode])

  useEffect(() => {
    const element = graphViewportRef.current
    if (!element) return

    const handleNativeWheel = (event: globalThis.WheelEvent) => {
      if (!element.contains(event.target as Node)) return
      event.preventDefault()
      event.stopPropagation()
      markInteracting()
      if (event.ctrlKey || event.metaKey) {
        updateZoom(getZoom() - event.deltaY * 0.004)
        return
      }
      const deltaX = event.shiftKey && Math.abs(event.deltaX) < 1 ? event.deltaY : event.deltaX
      applyPanDelta(-deltaX * 0.85, -event.deltaY * 0.85)
    }

    const preventGesture = (event: Event) => {
      event.preventDefault()
    }

    element.addEventListener('wheel', handleNativeWheel, { passive: false })
    element.addEventListener('gesturestart', preventGesture)
    element.addEventListener('gesturechange', preventGesture)
    element.addEventListener('gestureend', preventGesture)
    return () => {
      element.removeEventListener('wheel', handleNativeWheel)
      element.removeEventListener('gesturestart', preventGesture)
      element.removeEventListener('gesturechange', preventGesture)
      element.removeEventListener('gestureend', preventGesture)
    }
  }, [applyPanDelta, getZoom, markInteracting, updateZoom])

  useEffect(() => {
    applySceneTransform()
  }, [applySceneTransform])

  const handlePointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    if (selectedNodeId) {
      onSelect(null)
    }
    dragRef.current = { active: true, x: event.clientX, y: event.clientY }
    setIsDragging(true)
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current.active) return
    const deltaX = event.clientX - dragRef.current.x
    const deltaY = event.clientY - dragRef.current.y
    dragRef.current = { active: true, x: event.clientX, y: event.clientY }
    markInteracting()
    applyPanDelta(deltaX, deltaY)
  }

  const handlePointerUp = (event: PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current.active) return
    dragRef.current.active = false
    setIsDragging(false)
    syncViewportState()
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }

  const startHoverPreview = (nodeId: string) => {
    if (hoverTimerRef.current !== null) window.clearTimeout(hoverTimerRef.current)
    hoverTimerRef.current = window.setTimeout(() => {
      hoverTimerRef.current = null
      setHoverPreviewId(nodeId)
    }, HOVER_PREVIEW_DELAY)
  }

  const stopHoverPreview = () => {
    if (hoverTimerRef.current !== null) {
      window.clearTimeout(hoverTimerRef.current)
      hoverTimerRef.current = null
    }
    setHoverPreviewId(null)
  }

  const worldX = (x: number) => WORLD_BOUNDS.width / 2 + x
  const worldY = (y: number) => WORLD_BOUNDS.height / 2 + y
  const hoverAccent = hoverPreviewId ? nodeById.get(hoverPreviewId)?.accent : undefined
  const flagIconSx = { fontSize: 12, flexShrink: 0 }

  return (
    <Box
      ref={graphViewportRef}
      sx={{
        position: 'absolute',
        inset: 0,
        top,
        overflow: 'hidden',
        overscrollBehavior: 'contain',
        overscrollBehaviorX: 'contain',
        touchAction: 'none',
        cursor: isDragging ? 'grabbing' : 'grab',
        userSelect: 'none',
        WebkitUserSelect: 'none',
        WebkitUserDrag: 'none',
      }}
      onDragStart={event => event.preventDefault()}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
      onPointerLeave={handlePointerUp}
    >
      {loading ? (
        <Box sx={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <CircularProgress size={28} />
        </Box>
      ) : !layout.nodes.length ? (
        <Box sx={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', p: 2, cursor: 'default' }}>
          {empty}
        </Box>
      ) : (
        <Box
          ref={sceneRef}
          sx={{
            position: 'absolute',
            inset: 0,
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
            transformOrigin: 'center center',
          }}
        >
          <Box
            sx={{
              position: 'absolute',
              left: '50%',
              top: '50%',
              width: WORLD_BOUNDS.width,
              height: WORLD_BOUNDS.height,
              transform: 'translate(-50%, -50%)',
            }}
          >
            {config.showAxes && (
              <>
                <Box
                  sx={{
                    position: 'absolute',
                    inset: 0,
                    borderRadius: 5,
                    background: `
                      linear-gradient(90deg, transparent calc(50% - 1px), ${alpha(theme.palette.primary.main, 0.18)} calc(50% - 1px), ${alpha(theme.palette.primary.main, 0.18)} calc(50% + 1px), transparent calc(50% + 1px)),
                      linear-gradient(180deg, transparent calc(50% - 1px), ${alpha(theme.palette.primary.main, 0.18)} calc(50% - 1px), ${alpha(theme.palette.primary.main, 0.18)} calc(50% + 1px), transparent calc(50% + 1px)),
                      linear-gradient(90deg, ${alpha(theme.palette.text.secondary, 0.04)} 1px, transparent 1px),
                      linear-gradient(180deg, ${alpha(theme.palette.text.secondary, 0.04)} 1px, transparent 1px)
                    `,
                    backgroundSize: '100% 100%, 100% 100%, 64px 64px, 64px 64px',
                    boxShadow: `inset 0 0 0 1px ${alpha(theme.palette.primary.main, 0.08)}`,
                  }}
                />
                <Typography
                  variant="caption"
                  sx={{
                    position: 'absolute',
                    right: 16,
                    top: 'calc(50% + 10px)',
                    color: alpha(theme.palette.text.secondary, 0.55),
                    letterSpacing: 2,
                  }}
                >
                  X
                </Typography>
                <Typography
                  variant="caption"
                  sx={{
                    position: 'absolute',
                    left: 'calc(50% + 10px)',
                    top: 16,
                    color: alpha(theme.palette.text.secondary, 0.55),
                    letterSpacing: 2,
                  }}
                >
                  Y
                </Typography>
              </>
            )}
            <svg
              width={WORLD_BOUNDS.width}
              height={WORLD_BOUNDS.height}
              viewBox={`0 0 ${WORLD_BOUNDS.width} ${WORLD_BOUNDS.height}`}
              style={{ position: 'absolute', inset: 0, pointerEvents: 'none', overflow: 'visible' }}
            >
              <defs>
                {/* 上游原样：放在 defs 里的环境连线不会显示；打开 showAmbientEdges 的配置改在下面画 */}
                {!config.showAmbientEdges && ambientEdges.map(edge => {
                  const source = nodeById.get(edge.source)
                  const target = nodeById.get(edge.target)
                  if (!source || !target) return null
                  const style = config.ambientEdgeStyle(edge)
                  const levelFactor = zoomDetailLevel === 'high' ? 1 : zoomDetailLevel === 'medium' ? 0.8 : 0.65
                  return (
                    <line
                      key={`ambient-${edge.id}`}
                      x1={worldX(source.x)}
                      y1={worldY(source.y)}
                      x2={worldX(target.x)}
                      y2={worldY(target.y)}
                      stroke={alpha(coreAccent, zoomDetailLevel === 'high' ? 0.22 : zoomDetailLevel === 'medium' ? 0.14 : 0.08)}
                      strokeWidth={style.width * levelFactor}
                      strokeDasharray={style.dash}
                    />
                  )
                })}
                {highlightedEdges.map(edge => {
                  const source = nodeById.get(edge.source)
                  const target = nodeById.get(edge.target)
                  if (!source || !target) return null
                  return (
                    <linearGradient
                      key={`gradient-${edge.id}`}
                      id={`memory-edge-gradient-${edge.id}`}
                      gradientUnits="userSpaceOnUse"
                      x1={worldX(source.x)}
                      y1={worldY(source.y)}
                      x2={worldX(target.x)}
                      y2={worldY(target.y)}
                    >
                      <stop offset="0%" stopColor={source.glow} stopOpacity={0.2 + detailIntensity * 0.3} />
                      <stop offset="100%" stopColor={target.glow} stopOpacity={0.2 + detailIntensity * 0.3} />
                    </linearGradient>
                  )
                })}
              </defs>
              {config.showAmbientEdges &&
                ambientEdges.map(edge => {
                  const source = nodeById.get(edge.source)
                  const target = nodeById.get(edge.target)
                  if (!source || !target) return null
                  const style = config.ambientEdgeStyle(edge)
                  const levelFactor = zoomDetailLevel === 'high' ? 1 : zoomDetailLevel === 'medium' ? 0.85 : 0.7
                  const opacity = zoomDetailLevel === 'high' ? 0.62 : zoomDetailLevel === 'medium' ? 0.5 : 0.4
                  return (
                    <line
                      key={`ambient-${edge.id}`}
                      x1={worldX(source.x)}
                      y1={worldY(source.y)}
                      x2={worldX(target.x)}
                      y2={worldY(target.y)}
                      stroke={alpha(style.color ?? coreAccent, opacity)}
                      strokeWidth={style.width * levelFactor}
                      strokeDasharray={style.dash}
                      strokeLinecap="round"
                    />
                  )
                })}
              {hoverAccent &&
                previewEdges.map(edge => {
                  const source = nodeById.get(edge.source)
                  const target = nodeById.get(edge.target)
                  if (!source || !target) return null
                  const style = config.edgeStyle(edge)
                  return (
                    <line
                      key={`preview-${edge.id}`}
                      x1={worldX(source.x)}
                      y1={worldY(source.y)}
                      x2={worldX(target.x)}
                      y2={worldY(target.y)}
                      stroke={alpha(hoverAccent, 0.35)}
                      strokeWidth={style.width}
                      strokeDasharray={style.dash}
                      strokeLinecap="round"
                    />
                  )
                })}
              {highlightedEdges.map(edge => {
                const source = nodeById.get(edge.source)
                const target = nodeById.get(edge.target)
                if (!source || !target) return null
                const style = config.edgeStyle(edge)
                return (
                  <line
                    key={edge.id}
                    x1={worldX(source.x)}
                    y1={worldY(source.y)}
                    x2={worldX(target.x)}
                    y2={worldY(target.y)}
                    stroke={`url(#memory-edge-gradient-${edge.id})`}
                    strokeWidth={style.width * detailIntensity}
                    strokeDasharray={style.dash}
                  />
                )
              })}
            </svg>
            {config.atomicOrder.map(cluster => {
              if (viewMode === 'episode') return null
              const meta = clusterTitleAndCenter(config, cluster, viewMode)
              if (!layout.nodes.some(node => node.cluster === cluster)) return null
              return (
                <Typography
                  key={`${cluster}-label`}
                  variant="overline"
                  sx={{
                    position: 'absolute',
                    left: `calc(50% + ${meta.center.x}px)`,
                    top: `calc(50% + ${meta.center.y}px)`,
                    transform: 'translate(-50%, -50%)',
                    letterSpacing: 2,
                    color: alpha(meta.accent, zoomDetailLevel === 'high' ? 0.84 : zoomDetailLevel === 'medium' ? 0.76 : 0.68),
                    fontWeight: 800,
                    pointerEvents: 'none',
                  }}
                >
                  {meta.title}
                </Typography>
              )
            })}
            {layout.nodes.map(node => {
              const selected = node.id === selectedNodeId
              const related = highlightedNodeIds.has(node.id)
              const coreScoped =
                viewMode === 'episode' && selectedIsCore ? selectedCoreRelatedNodeIds.has(node.id) : true
              const isCore = node.cluster === coreCluster
              const isPerson = !!config.personCluster && node.cluster === config.personCluster
              const flags = nodeFlags(node)
              const muted = flags.inactive || flags.quarantined
              const visualScale = 1 / zoom
              const showTitle = node.labelVisible || selected || zoomDetailLevel !== 'low'
              const showSubtitle =
                !isInteracting && (selected || (zoomDetailLevel === 'high' && (isCore || node.memory_type !== 'entity')))
              const baseOpacity = muted ? Math.min(node.opacity, 0.55) : node.opacity
              const dotSize = Math.max(isCore ? 14 : 10, node.size)
              const borderColor = selected
                ? node.accent
                : muted
                  ? alpha(theme.palette.text.secondary, 0.42)
                  : related
                    ? alpha(node.glow, 0.42)
                    : alpha(node.glow, isCore ? 0.3 : 0.18)
              return (
                <Box
                  key={node.id}
                  sx={{
                    position: 'absolute',
                    left: `calc(50% + ${node.x}px)`,
                    top: `calc(50% + ${node.y}px)`,
                    width: 0,
                    height: 0,
                    transform: 'translate(-50%, -50%)',
                    zIndex: selected ? 3 : 1,
                    opacity: selectedNodeId
                      ? related
                        ? baseOpacity
                        : Math.max(
                            viewMode === 'episode' && selectedIsCore && !coreScoped ? 0.06 : 0.1,
                            baseOpacity *
                              (viewMode === 'episode' && selectedIsCore && !coreScoped
                                ? 0.12
                                : 0.18 + detailIntensity * 0.14),
                          )
                      : baseOpacity,
                  }}
                >
                  <Box
                    onPointerDown={event => {
                      event.preventDefault()
                      event.stopPropagation()
                    }}
                    onPointerEnter={() => startHoverPreview(node.id)}
                    onPointerLeave={stopHoverPreview}
                    onClick={event => {
                      event.stopPropagation()
                      onSelect(node)
                    }}
                    sx={{
                      position: 'relative',
                      width: isCore ? Math.max(104, node.size * 8.2) : Math.max(78, node.size * 9.2),
                      px: isCore ? 0.8 : 0.9,
                      py: isCore ? 0.72 : 0.78,
                      borderRadius: isCore ? 3 : 999,
                      cursor: 'pointer',
                      userSelect: 'none',
                      WebkitUserSelect: 'none',
                      WebkitUserDrag: 'none',
                      transform: `translate(-50%, -50%) scale(${visualScale})`,
                      transformOrigin: 'center center',
                      bgcolor: muted
                        ? alpha(theme.palette.text.disabled, 0.08)
                        : isCore
                          ? alpha(theme.palette.background.paper, 0.92)
                          : node.fill,
                      background:
                        isCore && !muted
                          ? `linear-gradient(135deg, ${alpha(node.glow, 0.18)}, ${alpha(theme.palette.background.paper, 0.96)})`
                          : undefined,
                      border: muted && !selected ? '1px dashed' : '1px solid',
                      borderColor,
                      boxShadow: selected
                        ? `0 0 0 2px ${alpha(theme.palette.background.paper, 0.9)}, 0 0 0 4px ${alpha(theme.palette.primary.main, 0.32)}, 0 12px 24px ${alpha(node.glow, 0.2)}`
                        : related
                          ? `0 6px 16px ${alpha(node.glow, 0.12)}`
                          : muted
                            ? 'none'
                            : `0 3px 10px ${alpha(node.glow, isCore ? 0.1 : 0.05)}`,
                      transition: 'transform 0.14s ease, box-shadow 0.14s ease, border-color 0.14s ease, opacity 0.14s ease',
                      WebkitFontSmoothing: 'antialiased',
                      MozOsxFontSmoothing: 'grayscale',
                      '&:hover': {
                        transform: `translate(-50%, -50%) scale(${visualScale}) translateY(-2px)`,
                        boxShadow: selected
                          ? `0 0 0 2px ${alpha(theme.palette.background.paper, 0.9)}, 0 0 0 4px ${alpha(theme.palette.primary.main, 0.32)}, 0 14px 26px ${alpha(node.glow, 0.24)}`
                          : `0 8px 18px ${alpha(node.glow, isCore ? 0.18 : 0.12)}`,
                      },
                    }}
                  >
                    {isCore && !muted && (
                      <>
                        <Box
                          sx={{
                            position: 'absolute',
                            inset: -9,
                            borderRadius: 5,
                            border: '1px solid',
                            borderColor: alpha(node.glow, selected ? 0.28 : 0.14),
                            pointerEvents: 'none',
                          }}
                        />
                        <Box
                          sx={{
                            position: 'absolute',
                            inset: -18,
                            borderRadius: 6,
                            background: `radial-gradient(circle, ${alpha(node.glow, 0.16)} 0%, transparent 72%)`,
                            pointerEvents: 'none',
                          }}
                        />
                      </>
                    )}
                    <Stack direction="row" spacing={0.9} alignItems="center" sx={{ position: 'relative' }}>
                      {isPerson ? (
                        <Box
                          sx={{
                            width: Math.max(18, dotSize + 4),
                            height: Math.max(18, dotSize + 4),
                            borderRadius: '50%',
                            flexShrink: 0,
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            bgcolor: alpha(muted ? theme.palette.text.disabled : node.accent, 0.18),
                            color: muted ? theme.palette.text.disabled : node.accent,
                            fontSize: '0.62rem',
                            fontWeight: 800,
                            lineHeight: 1,
                          }}
                        >
                          {firstGlyph(node.label)}
                        </Box>
                      ) : (
                        <Box
                          sx={{
                            width: dotSize,
                            height: dotSize,
                            borderRadius: '50%',
                            flexShrink: 0,
                            bgcolor: muted ? theme.palette.text.disabled : node.glow,
                            boxShadow: muted ? 'none' : `0 0 8px ${alpha(node.glow, 0.18)}`,
                          }}
                        />
                      )}
                      {showTitle && (
                        <Stack spacing={isCore ? 0.2 : 0} sx={{ minWidth: 0 }}>
                          <Stack direction="row" spacing={0.5} alignItems="center">
                            <Typography
                              variant="caption"
                              sx={{
                                color: muted ? 'text.secondary' : 'text.primary',
                                fontWeight: selected ? 800 : 700,
                                fontSize: isCore ? '0.84rem' : node.size > 12 ? '0.78rem' : '0.72rem',
                                lineHeight: 1.1,
                              }}
                            >
                              {truncateLabel(node.label, isCore ? (zoomDetailLevel === 'high' ? 24 : 18) : node.size > 12 ? 14 : 10)}
                            </Typography>
                            {flags.quarantined && (
                              <BlockRoundedIcon titleAccess={t('node.quarantined')} sx={{ ...flagIconSx, color: theme.palette.warning.main }} />
                            )}
                            {flags.frozen && (
                              <AcUnitRoundedIcon titleAccess={t('node.frozen')} sx={{ ...flagIconSx, color: theme.palette.info.main }} />
                            )}
                            {flags.protected && (
                              <ShieldRoundedIcon titleAccess={t('node.protected')} sx={{ ...flagIconSx, color: theme.palette.primary.main }} />
                            )}
                            {renderNodeBadge?.(node)}
                          </Stack>
                          {showSubtitle && (
                            <Typography variant="caption" sx={{ color: 'text.secondary', fontSize: isCore ? '0.67rem' : '0.64rem', lineHeight: 1.1 }}>
                              {truncateLabel(node.subtitle || fallbackSubtitle(node), isCore ? 28 : 18)}
                            </Typography>
                          )}
                        </Stack>
                      )}
                    </Stack>
                  </Box>
                </Box>
              )
            })}
          </Box>
        </Box>
      )}
    </Box>
  )
}
