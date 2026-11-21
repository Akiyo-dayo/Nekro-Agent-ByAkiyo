import type { AtlasColorRole } from '../../../theme/themeConfig'

/**
 * 记忆图谱画布（`AtlasGraph`）的数据与配置
 *
 * 画布本身不认识「段落 / 事件 / 实体 / 关系」：节点类型是字符串，节点归到哪个簇、簇在两种视图里
 * 怎么摆、什么颜色，都由调用方的簇配置决定。工作区记忆页（上游）与后台记忆页各给一份配置，
 * 后者多一个「人」簇。
 */

export type InformationDensity = 'low' | 'medium' | 'high'

/** 事件视图：核心簇放中间、其余簇撒在外圈椭圆带；原子视图：每个簇围着自己的中心排 */
export type AtlasViewMode = 'episode' | 'atomic'

/** 图谱节点的原始数据（与后端图谱接口的节点同形，节点类型放宽成字符串） */
export interface AtlasGraphNodeData {
  id: string
  memory_type: string
  ref_id: number
  label: string
  subtitle: string
  status: string
  cognitive_type: string | null
  weight: number
  size: number
  importance: number
  paragraph_id?: number | null
  metadata: Record<string, unknown>
}

export interface AtlasGraphEdgeData {
  id: string
  source: string
  target: string
  edge_type: string
  label: string
  weight: number
  strength: number
  status: string
  cognitive_type: string | null
  metadata: Record<string, unknown>
}

export interface AtlasGraphData {
  nodes: AtlasGraphNodeData[]
  edges: AtlasGraphEdgeData[]
}

/** 摆好位置的节点：坐标以世界中心为原点 */
export type AtlasLaidOutNode<T extends AtlasGraphNodeData = AtlasGraphNodeData> = T & {
  cluster: string
  x: number
  y: number
  size: number
  opacity: number
  /** 簇的实色（选中描边、标题、图例与它同源） */
  accent: string
  fill: string
  glow: string
  labelVisible: boolean
}

export interface AtlasClusterStat {
  /** 画出来的 */
  shown: number
  /** 当前视图里本该有的（密度与总上限截断之前） */
  total: number
}

export interface AtlasLayout<T extends AtlasGraphNodeData = AtlasGraphNodeData> {
  nodes: AtlasLaidOutNode<T>[]
  /** 每个簇因为密度上限没画出来的节点数 */
  hiddenCounts: Record<string, number>
  /** 每个簇「显示 n / 共 m」（图例用）；事件视图里不画的簇不在里面 */
  clusterStats: Record<string, AtlasClusterStat>
  /** 整张图的节点数（去重之前，判断「数据很多」用） */
  totalNodes: number
  /** 是不是撞上了画布的总上限 */
  capped: boolean
}

/** 事件视图外圈的椭圆带：半径在 inner ~ outer 之间，纵向压扁 stretchY，整体偏移 bias */
export interface AtlasBand {
  inner: number
  outer: number
  stretchY: number
  biasX: number
  biasY: number
}

export interface AtlasClusterSpec {
  /** 原子视图里簇的标题与中心（世界坐标） */
  title: string
  /** 簇的颜色角色：标题、节点、图例都取 `ATLAS_COLORS[role]`，换主题预设时一起变 */
  role: AtlasColorRole
  center: { x: number; y: number }
  /** 事件视图里的标题与中心；缺省同原子视图 */
  episodeTitle?: string
  episodeCenter?: { x: number; y: number }
  /** 事件视图里的角色：core 放中心云（只能有一个簇），band 放外圈椭圆带，hidden 不画 */
  episodeRole: 'core' | 'band' | 'hidden'
  band?: AtlasBand
  /** 每档密度的节点上限 */
  limits: { episode: Record<InformationDensity, number>; atomic: Record<InformationDensity, number> }
  /** 放大时上限加 zoomExpansion × 这个系数 */
  zoomExpansionFactor: number
  /** 原子视图的环：半径 = ringBase + √(序号+1) × ringStep，纵向乘 ringSquash */
  ringBase: number
  ringStep: number
  ringSquash: number
  /** 圆点大小 */
  nodeSize: (importance: number) => number
  /** 标签显示门槛：always 总显示，entity / other 用两档不同的「前 N 个」 */
  labelTier: 'always' | 'entity' | 'other'
  /** 节点底色与光晕的透明度（缺省 0.16 / 0.42） */
  tint?: { fill: number; glow: number }
}

export interface AtlasClusterConfig<TContext = unknown> {
  clusters: Record<string, AtlasClusterSpec>
  /** 原子视图的摆放顺序（也是簇标题的渲染顺序） */
  atomicOrder: string[]
  /** 事件视图外圈的摆放顺序：后摆的会避开先摆的，顺序变了布局就变了 */
  episodeOuterOrder: string[]
  /** 核心簇的键（事件视图放中间、选中它时只留相连节点亮着、画成卡片） */
  coreCluster: string
  /** 事件视图里的「环境连线」是哪几种边 */
  ambientEdgeTypes: string[]
  resolveCluster: (node: AtlasGraphNodeData) => string
  /** 去重键：相同键的节点只画第一个；返回 undefined 不参与去重 */
  dedupeKey?: (node: AtlasGraphNodeData) => string | undefined
  /** 从整张图预先算出过滤要用的东西（例如哪些段落挂在事件上），每次布局只算一次 */
  prepare?: (graph: AtlasGraphData) => TContext
  /** 这个节点在当前视图里画不画（在 `episodeRole === 'hidden'` 之外的进一步过滤） */
  isVisible?: (node: AtlasGraphNodeData, viewMode: AtlasViewMode, context: TContext) => boolean
  /** 选中节点时高亮的那几条边怎么画：线宽系数与虚线样式 */
  edgeStyle: (edge: AtlasGraphEdgeData) => { width: number; dash?: string }
  /** 事件视图的环境连线怎么画；`color` 只在 `showAmbientEdges` 打开时用得上（缺省用核心簇的颜色） */
  ambientEdgeStyle: (edge: AtlasGraphEdgeData) => { width: number; dash?: string; color?: string }
  /**
   * 把事件视图的环境连线真正画出来
   *
   * 上游记忆页把这些线放在 SVG 的 `<defs>` 里，实际上一条都不显示；记忆的两份配置不打开它，外观与
   * 上游一样。好感关系图要靠这些线（人到人设，粗细看分数、颜色看阶段）撑起星形，所以打开。
   */
  showAmbientEdges?: boolean
  /** 画布上的十字轴与 X / Y 字母：只有工作区记忆页要 */
  showAxes?: boolean
  /** 「人」所在的簇：这个簇的节点把色点换成首字头像 */
  personCluster?: string
}
