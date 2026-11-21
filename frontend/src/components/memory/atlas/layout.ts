import { alpha, type Theme } from '@mui/material/styles'
import { ATLAS_COLORS } from '../../../theme/themeConfig'
import type {
  AtlasBand,
  AtlasClusterConfig,
  AtlasClusterSpec,
  AtlasClusterStat,
  AtlasGraphEdgeData,
  AtlasGraphNodeData,
  AtlasLaidOutNode,
  AtlasLayout,
  AtlasViewMode,
  InformationDensity,
} from './types'

/**
 * 记忆图谱的布局：种子定位，不是力导向
 *
 * 同一个节点在同一份数据下永远落在同一个位置（种子取自簇名与节点 ID），刷新不跳。
 * 事件视图把核心簇撒在中心云里、其余簇撒在外圈椭圆带上，后摆的节点避开先摆的；
 * 原子视图每个簇围着自己的中心按黄金角排成螺旋。
 *
 * 这份代码是从工作区记忆页（上游 `MemoryTab`）原样搬出来的，只把「段落 / 事件 / 实体 / 关系」
 * 这几个写死的簇换成了调用方给的簇配置；上游那份配置下的结果与搬之前逐个坐标相同。
 */

export const WORLD_BOUNDS = {
  width: 1200,
  height: 760,
}

export function hashSeed(input: string): number {
  let hash = 2166136261
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

export function seededUnit(seed: number): number {
  const x = Math.sin(seed * 12.9898) * 43758.5453
  return x - Math.floor(x)
}

export function positionInRandomEllipseBand(
  key: string,
  cluster: string,
  band: AtlasBand,
  occupied: Array<{ x: number; y: number }>,
) {
  const seedBase = hashSeed(`${cluster}:${key}`)

  let best = { x: 0, y: 0, score: -1 }
  for (let attempt = 0; attempt < 14; attempt += 1) {
    const a = seededUnit(seedBase + attempt * 17)
    const b = seededUnit(seedBase + attempt * 31)
    const radius = band.inner + (band.outer - band.inner) * Math.pow(a, 0.82)
    const angle = b * Math.PI * 2
    const x = Math.cos(angle) * radius + band.biasX
    const y = Math.sin(angle) * radius * band.stretchY + band.biasY

    let nearest = Infinity
    for (const point of occupied) {
      const dx = x - point.x
      const dy = y - point.y
      const dist = Math.sqrt(dx * dx + dy * dy)
      nearest = Math.min(nearest, dist)
    }

    const centerDistance = Math.sqrt(x * x + y * y)
    const score = Math.min(nearest, 140) + centerDistance * 0.08
    if (score > best.score) {
      best = { x, y, score }
    }
  }
  return { x: best.x, y: best.y }
}

export function positionInCenterCloud(
  key: string,
  cluster: string,
  occupied: Array<{ x: number; y: number }>,
) {
  const seedBase = hashSeed(`${cluster}:${key}`)
  let best = { x: 0, y: 0, score: -1 }
  for (let attempt = 0; attempt < 16; attempt += 1) {
    const a = seededUnit(seedBase + attempt * 13)
    const b = seededUnit(seedBase + attempt * 29)
    const radius = 28 + 150 * Math.pow(a, 0.9)
    const angle = b * Math.PI * 2
    const x = Math.cos(angle) * radius
    const y = Math.sin(angle) * radius * 0.76

    let nearest = Infinity
    for (const point of occupied) {
      const dx = x - point.x
      const dy = y - point.y
      const dist = Math.sqrt(dx * dx + dy * dy)
      nearest = Math.min(nearest, dist)
    }

    const centerBias = Math.max(0, 180 - Math.sqrt(x * x + y * y))
    const score = Math.min(nearest, 120) + centerBias * 0.12
    if (score > best.score) {
      best = { x, y, score }
    }
  }
  return { x: best.x, y: best.y }
}

const LABEL_THRESHOLDS: Record<InformationDensity, { entity: number; other: number; zoom: number }> = {
  low: { entity: 2, other: 3, zoom: 1.08 },
  medium: { entity: 4, other: 5, zoom: 1.0 },
  high: { entity: 8, other: 10, zoom: 0.92 },
}

/** 画布上最多画多少个节点（不论密度档位）：再多 DOM 就拖不动了 */
export const ATLAS_NODE_CAP = 240
/** 节点超过这个数时首次进入强制「事件视图 + 疏」 */
export const ATLAS_HEAVY_GRAPH = 600
/** 环境连线超过这个数时只画选中核心的那部分 */
export const AMBIENT_EDGE_CAP = 400

/** 簇的实色、节点底色与光晕：三者同源，换主题预设时一起变 */
export function clusterPalette(spec: AtlasClusterSpec): { accent: string; fill: string; glow: string } {
  const accent = ATLAS_COLORS[spec.role]
  const tint = spec.tint ?? { fill: 0.16, glow: 0.42 }
  return { accent, fill: alpha(accent, tint.fill), glow: alpha(accent, tint.glow) }
}

/** 簇的实色；没有这个簇时退到核心色 */
export function clusterAccent<C>(config: AtlasClusterConfig<C>, cluster: string): string {
  const spec = config.clusters[cluster]
  return spec ? ATLAS_COLORS[spec.role] : ATLAS_COLORS.core
}

/**
 * 先按密度档位每簇取前 N，再把总数压到 `ATLAS_NODE_CAP` 以内（按重要度全局截）
 *
 * 返回每簇最后画哪些，顺序与簇内重要度顺序一致（布局的标签门槛按这个序号算）。
 */
function pickVisible<T extends AtlasGraphNodeData>(
  candidates: Array<{ cluster: string; sorted: T[]; limit: number }>,
): { picked: Map<string, T[]>; capped: boolean } {
  const perCluster = candidates.map(item => ({ cluster: item.cluster, nodes: item.sorted.slice(0, item.limit) }))
  const all = perCluster.flatMap(item => item.nodes)
  const picked = new Map<string, T[]>()
  if (all.length <= ATLAS_NODE_CAP) {
    perCluster.forEach(item => picked.set(item.cluster, item.nodes))
    return { picked, capped: false }
  }
  const keep = new Set(
    [...all].sort((a, b) => b.importance - a.importance).slice(0, ATLAS_NODE_CAP).map(node => node.id),
  )
  perCluster.forEach(item => picked.set(item.cluster, item.nodes.filter(node => keep.has(node.id))))
  return { picked, capped: true }
}

/**
 * @param _theme 不参与计算（颜色取 `ATLAS_COLORS`），留着是让调用方把主题放进依赖里，换主题时重算
 */
export function buildAtlasLayout<T extends AtlasGraphNodeData, C>(
  _theme: Theme,
  graph: { nodes: T[]; edges: AtlasGraphEdgeData[] } | undefined,
  zoom: number,
  informationDensity: InformationDensity,
  viewMode: AtlasViewMode,
  config: AtlasClusterConfig<C>,
): AtlasLayout<T> {
  const clusterKeys = Object.keys(config.clusters)
  const emptyCounts = Object.fromEntries(clusterKeys.map(key => [key, 0])) as Record<string, number>
  if (!graph) return { nodes: [], hiddenCounts: emptyCounts, clusterStats: {}, totalNodes: 0, capped: false }

  const grouped: Record<string, T[]> = Object.fromEntries(clusterKeys.map(key => [key, [] as T[]]))
  const context = config.prepare ? config.prepare(graph) : (undefined as C)

  const dedupeSeen = new Set<string>()
  graph.nodes.forEach(node => {
    const dedupeKey = config.dedupeKey?.(node)
    if (dedupeKey !== undefined) {
      if (dedupeSeen.has(dedupeKey)) return
      dedupeSeen.add(dedupeKey)
    }
    const cluster = config.resolveCluster(node)
    const spec = config.clusters[cluster]
    if (!spec) return
    if (viewMode === 'episode' && spec.episodeRole === 'hidden') return
    if (config.isVisible && !config.isVisible(node, viewMode, context)) return
    grouped[cluster].push(node)
  })

  const zoomExpansion = zoom >= 1.35 ? 2 : zoom >= 1.12 ? 1 : 0
  const limitOf = (cluster: string) => {
    const spec = config.clusters[cluster]
    return spec.limits[viewMode][informationDensity] + zoomExpansion * spec.zoomExpansionFactor
  }

  const nodes: AtlasLaidOutNode<T>[] = []
  const hiddenCounts = { ...emptyCounts }
  const golden = Math.PI * (3 - Math.sqrt(5))
  const baseLabelThreshold = LABEL_THRESHOLDS[informationDensity]
  const labelThreshold = {
    entity: baseLabelThreshold.entity + (zoom >= 1.32 ? 4 : zoom >= 1.12 ? 2 : 0),
    other: baseLabelThreshold.other + (zoom >= 1.32 ? 5 : zoom >= 1.12 ? 2 : 0),
    zoom: baseLabelThreshold.zoom,
  }
  const labelVisibleAt = (cluster: string, index: number) => {
    const tier = config.clusters[cluster].labelTier
    if (tier === 'always') return true
    return zoom >= labelThreshold.zoom || index < (tier === 'entity' ? labelThreshold.entity : labelThreshold.other)
  }

  const sortedOf = (cluster: string) => [...grouped[cluster]].sort((a, b) => b.importance - a.importance)
  const clusterStats: Record<string, AtlasClusterStat> = {}
  const settle = (cluster: string, shown: number) => {
    const total = grouped[cluster].length
    hiddenCounts[cluster] = Math.max(0, total - shown)
    clusterStats[cluster] = { shown, total }
  }

  if (viewMode === 'episode') {
    const core = config.coreCluster
    const coreSpec = config.clusters[core]
    const bandClusters = config.episodeOuterOrder.filter(cluster => config.clusters[cluster]?.episodeRole === 'band')
    const { picked, capped } = pickVisible(
      [core, ...bandClusters].map(cluster => ({ cluster, sorted: sortedOf(cluster), limit: limitOf(cluster) })),
    )
    const coreNodes = picked.get(core) ?? []
    settle(core, coreNodes.length)

    const mixedOuterNodes = bandClusters.flatMap(cluster => {
      const visible = picked.get(cluster) ?? []
      settle(cluster, visible.length)
      return visible.map(node => ({ node, cluster }))
    })

    const occupiedCorePositions: Array<{ x: number; y: number }> = []
    const corePalette = clusterPalette(coreSpec)
    coreNodes.forEach((node, index) => {
      let x = 0
      let y = 0
      if (!(coreNodes.length === 1 && index === 0)) {
        const pos = positionInCenterCloud(String(node.id ?? `${core}-${index}`), core, occupiedCorePositions)
        x = pos.x
        y = pos.y
      }
      occupiedCorePositions.push({ x, y })
      const importance = Math.max(0.1, node.importance)
      nodes.push({
        ...node,
        cluster: core,
        x,
        y,
        size: coreSpec.nodeSize(importance),
        opacity: Math.min(0.95, 0.45 + importance * 0.25),
        ...corePalette,
        labelVisible: true,
      })
    })

    const occupiedOuterPositions: Array<{ x: number; y: number }> = [...occupiedCorePositions]
    mixedOuterNodes.forEach(({ node, cluster }, index) => {
      const spec = config.clusters[cluster]
      const pos = positionInRandomEllipseBand(
        String(node.id ?? `${cluster}-${index}`),
        cluster,
        spec.band as AtlasBand,
        occupiedOuterPositions,
      )
      occupiedOuterPositions.push(pos)
      const palette = clusterPalette(spec)
      const importance = Math.max(0.1, node.importance)
      nodes.push({
        ...node,
        cluster,
        x: pos.x,
        y: pos.y,
        size: spec.nodeSize(importance),
        opacity: Math.min(0.92, 0.42 + importance * 0.22),
        ...palette,
        labelVisible: labelVisibleAt(cluster, index),
      })
    })

    return { nodes, hiddenCounts, clusterStats, totalNodes: graph.nodes.length, capped }
  }

  const { picked, capped } = pickVisible(
    config.atomicOrder.map(cluster => ({ cluster, sorted: sortedOf(cluster), limit: limitOf(cluster) })),
  )
  config.atomicOrder.forEach(cluster => {
    const spec = config.clusters[cluster]
    const visible = picked.get(cluster) ?? []
    settle(cluster, visible.length)
    const palette = clusterPalette(spec)

    visible.forEach((node, index) => {
      const angle = index * golden
      const ring = spec.ringBase + Math.sqrt(index + 1) * spec.ringStep
      const importance = Math.max(0.1, node.importance)
      nodes.push({
        ...node,
        cluster,
        x: spec.center.x + Math.cos(angle) * ring,
        y: spec.center.y + Math.sin(angle) * ring * spec.ringSquash,
        size: spec.nodeSize(importance),
        opacity: Math.min(0.95, 0.45 + importance * 0.25),
        ...palette,
        labelVisible: labelVisibleAt(cluster, index),
      })
    })
  })

  return { nodes, hiddenCounts, clusterStats, totalNodes: graph.nodes.length, capped }
}

/** 簇标题与强调色：事件视图优先用 `episodeTitle` / `episodeCenter`；强调色与节点同源 */
export function clusterTitleAndCenter<C>(
  config: AtlasClusterConfig<C>,
  cluster: string,
  viewMode: AtlasViewMode,
): { title: string; center: { x: number; y: number }; accent: string } {
  const spec = config.clusters[cluster]
  const accent = ATLAS_COLORS[spec.role]
  if (viewMode === 'episode') {
    return {
      title: spec.episodeTitle ?? spec.title,
      center: spec.episodeCenter ?? spec.center,
      accent,
    }
  }
  return { title: spec.title, center: spec.center, accent }
}

/** 簇在当前视图里的标题（图例用） */
export function clusterTitle<C>(config: AtlasClusterConfig<C>, cluster: string, viewMode: AtlasViewMode): string {
  const spec = config.clusters[cluster]
  if (!spec) return cluster
  return viewMode === 'episode' ? spec.episodeTitle ?? spec.title : spec.title
}

/** 图里出现过的边类型（图例只列这些） */
export function presentEdgeTypes(edges: AtlasGraphEdgeData[] | undefined): Set<string> {
  return new Set((edges ?? []).map(edge => edge.edge_type))
}

/** 图例的簇顺序：事件视图核心在前、外圈按摆放顺序；原子视图按原子顺序 */
export function legendClusterOrder<C>(config: AtlasClusterConfig<C>, viewMode: AtlasViewMode): string[] {
  if (viewMode === 'atomic') return config.atomicOrder
  return [
    config.coreCluster,
    ...config.episodeOuterOrder.filter(cluster => config.clusters[cluster]?.episodeRole === 'band'),
  ]
}
