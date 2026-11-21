import type { AtlasClusterConfig, AtlasClusterSpec, AtlasGraphData, AtlasGraphNodeData, AtlasViewMode } from './types'

/**
 * 记忆图谱的簇配置
 *
 * 五个簇（事件、语义段落、情景段落、关系、实体）的标题、位置与上限都照搬工作区记忆页
 * （上游 `MemoryTab`）原来写死在布局里的数。颜色按角色取主题（`ATLAS_COLORS`），簇标题与节点同源。
 * 后台记忆页另加一个「人」簇（记忆里涉及的人），只在它自己的配置里出现。
 */

const BASE_CLUSTERS: Record<string, AtlasClusterSpec> = {
  episode: {
    title: '事件核',
    episodeTitle: '事件核心',
    role: 'core',
    center: { x: 0, y: 0 },
    episodeCenter: { x: 0, y: 0 },
    episodeRole: 'core',
    limits: {
      episode: { low: 7, medium: 10, high: 12 },
      atomic: { low: 8, medium: 12, high: 16 },
    },
    zoomExpansionFactor: 1,
    ringBase: 22,
    ringStep: 48,
    ringSquash: 0.48,
    nodeSize: importance => Math.min(15, 8.8 + importance * 1.45),
    labelTier: 'always',
    tint: { fill: 0.18, glow: 0.42 },

  },
  semantic: {
    title: '语义区',
    episodeTitle: '语义背景',
    role: 'semantic',
    center: { x: -280, y: -180 },
    episodeCenter: { x: -310, y: -190 },
    episodeRole: 'band',
    band: { inner: 250, outer: 520, stretchY: 0.72, biasX: 0, biasY: -34 },
    limits: {
      episode: { low: 10, medium: 14, high: 16 },
      atomic: { low: 12, medium: 16, high: 22 },
    },
    zoomExpansionFactor: 1,
    ringBase: 48,
    ringStep: 28,
    ringSquash: 0.72,
    nodeSize: importance => Math.min(15, 8.5 + importance * 1.8),
    labelTier: 'other',

  },
  episodic: {
    title: '情景区',
    episodeTitle: '事件片段',
    role: 'episodic',
    center: { x: -280, y: 180 },
    episodeCenter: { x: -310, y: 190 },
    episodeRole: 'band',
    band: { inner: 180, outer: 420, stretchY: 0.86, biasX: -18, biasY: 10 },
    limits: {
      episode: { low: 12, medium: 16, high: 18 },
      atomic: { low: 14, medium: 18, high: 24 },
    },
    zoomExpansionFactor: 2,
    ringBase: 48,
    ringStep: 28,
    ringSquash: 0.72,
    nodeSize: importance => Math.min(15, 8.5 + importance * 1.8),
    labelTier: 'other',
    tint: { fill: 0.17, glow: 0.46 },

  },
  relation: {
    title: '关系区',
    episodeTitle: '关联线索',
    role: 'relation',
    center: { x: 280, y: -180 },
    episodeCenter: { x: 310, y: -190 },
    episodeRole: 'hidden',
    limits: {
      episode: { low: 12, medium: 16, high: 20 },
      atomic: { low: 16, medium: 24, high: 32 },
    },
    zoomExpansionFactor: 2,
    ringBase: 48,
    ringStep: 28,
    ringSquash: 0.72,
    nodeSize: importance => Math.min(15, 8.5 + importance * 1.8),
    labelTier: 'other',
  },
  entity: {
    title: '实体区',
    episodeTitle: '参与实体',
    role: 'entity',
    center: { x: 280, y: 180 },
    episodeCenter: { x: 310, y: 190 },
    episodeRole: 'band',
    band: { inner: 210, outer: 470, stretchY: 0.8, biasX: 22, biasY: -8 },
    limits: {
      episode: { low: 12, medium: 16, high: 20 },
      atomic: { low: 16, medium: 24, high: 32 },
    },
    zoomExpansionFactor: 2,
    ringBase: 48,
    ringStep: 30,
    ringSquash: 0.72,
    nodeSize: () => Math.min(13, 7),
    labelTier: 'entity',
    tint: { fill: 0.16, glow: 0.38 },

  },
}

/**
 * 「人」簇：记忆里涉及的人，只在后台记忆页的图谱里有
 *
 * 原子视图里放在正上方语义区与关系区之间的空当：放下方会贴着画布底边、被计数标签压住。
 */
const PERSON_CLUSTER: AtlasClusterSpec = {
  title: '人物区',
  episodeTitle: '涉及的人',
  role: 'person',
  center: { x: 0, y: -190 },
  episodeCenter: { x: 0, y: -190 },
  episodeRole: 'band',
  band: { inner: 230, outer: 500, stretchY: 0.78, biasX: -6, biasY: 18 },
  limits: {
    episode: { low: 8, medium: 12, high: 16 },
    atomic: { low: 10, medium: 14, high: 20 },
  },
  zoomExpansionFactor: 2,
  ringBase: 40,
  ringStep: 26,
  ringSquash: 0.5,
  nodeSize: importance => Math.min(13, 7.5 + importance * 1.2),
  labelTier: 'other',

}


/** 节点归哪个簇：与上游 `resolveCluster` 相同，另把「人」单独归一簇 */
export function resolveMemoryCluster(node: AtlasGraphNodeData): string {
  if (node.memory_type === 'person') return 'person'
  if (node.memory_type === 'episode') return 'episode'
  if (node.memory_type === 'paragraph') {
    return node.cognitive_type === 'semantic' ? 'semantic' : 'episodic'
  }
  if (node.memory_type === 'relation') return 'relation'
  return 'entity'
}

interface MemoryAtlasContext {
  episodeParagraphIds: Set<number>
  episodeEntityIds: Set<number>
  /** 「人」节点 ID → 它连着的段落 ID */
  personParagraphs: Map<string, number[]>
  semanticParagraphIds: Set<number>
}

function idFromNodeId(nodeId: string, prefix: string): number {
  return Number(nodeId.replace(prefix, ''))
}

function prepareMemoryAtlas(graph: AtlasGraphData): MemoryAtlasContext {
  const episodeParagraphIds = new Set<number>()
  const episodeEntityIds = new Set<number>()
  const personParagraphs = new Map<string, number[]>()
  graph.edges.forEach(edge => {
    if (edge.edge_type === 'episode_paragraph') {
      const targetId = idFromNodeId(edge.target, 'paragraph-')
      if (!Number.isNaN(targetId)) episodeParagraphIds.add(targetId)
    }
    if (edge.edge_type === 'episode_entity') {
      const targetId = idFromNodeId(edge.target, 'entity-')
      if (!Number.isNaN(targetId)) episodeEntityIds.add(targetId)
    }
    if (edge.edge_type === 'paragraph_person') {
      const paragraphId = idFromNodeId(edge.source, 'paragraph-')
      if (!Number.isNaN(paragraphId)) {
        const list = personParagraphs.get(edge.target) ?? []
        list.push(paragraphId)
        personParagraphs.set(edge.target, list)
      }
    }
  })
  const semanticParagraphIds = new Set<number>(
    graph.nodes
      .filter(node => node.memory_type === 'paragraph' && node.cognitive_type === 'semantic')
      .map(node => node.ref_id),
  )
  return { episodeParagraphIds, episodeEntityIds, personParagraphs, semanticParagraphIds }
}

/**
 * 事件视图里只画挂在事件上的东西（与上游相同）：参与事件的实体、属于事件的情景段落、全部语义段落。
 * 「人」只画连着一段在事件视图里看得见的段落的那些，免得一群人飘在没有段落的地方。
 */
function isVisibleInMemoryAtlas(node: AtlasGraphNodeData, viewMode: AtlasViewMode, context: MemoryAtlasContext): boolean {
  if (viewMode !== 'episode') return true
  if (node.memory_type === 'entity' && node.ref_id && !context.episodeEntityIds.has(node.ref_id)) {
    return false
  }
  if (
    node.memory_type === 'paragraph' &&
    node.cognitive_type !== 'semantic' &&
    node.ref_id &&
    !context.episodeParagraphIds.has(node.ref_id)
  ) {
    return false
  }
  if (node.memory_type === 'person') {
    const paragraphs = context.personParagraphs.get(node.id) ?? []
    return paragraphs.some(id => context.semanticParagraphIds.has(id) || context.episodeParagraphIds.has(id))
  }
  return true
}

export interface MemoryAtlasOptions {
  /** 加上「人」簇（后台记忆页） */
  withPersons?: boolean
  /** 换掉簇标题（后台记忆页要跟着界面语言走）；没给的沿用上面写死的中文标题 */
  titles?: Record<string, { title: string; episodeTitle?: string }>
  /** 画十字轴与 X / Y 字母（只有工作区记忆页要） */
  showAxes?: boolean
}

export function createMemoryAtlasConfig(options: MemoryAtlasOptions = {}): AtlasClusterConfig<MemoryAtlasContext> {
  const base: Record<string, AtlasClusterSpec> = options.withPersons
    ? { ...BASE_CLUSTERS, person: PERSON_CLUSTER }
    : { ...BASE_CLUSTERS }
  const titles = options.titles ?? {}
  const clusters: Record<string, AtlasClusterSpec> = Object.fromEntries(
    Object.entries(base).map(([key, spec]) => [key, titles[key] ? { ...spec, ...titles[key] } : spec]),
  )
  const extra = options.withPersons ? ['person'] : []
  return {
    clusters,
    atomicOrder: ['episode', 'semantic', 'episodic', 'relation', 'entity', ...extra],
    // 「人」排在最后：上游四簇先摆，位置与没有「人」时一样，人去避开它们
    episodeOuterOrder: ['episodic', 'entity', 'semantic', ...extra],
    coreCluster: 'episode',
    personCluster: options.withPersons ? 'person' : undefined,
    showAxes: options.showAxes,
    ambientEdgeTypes: ['episode_paragraph', 'episode_entity'],
    resolveCluster: resolveMemoryCluster,
    dedupeKey: node => (node.memory_type === 'relation' ? `${node.label}|${node.subtitle}` : undefined),
    prepare: prepareMemoryAtlas,
    isVisible: isVisibleInMemoryAtlas,
    edgeStyle: edge => ({
      width: edge.edge_type === 'relation_paragraph' ? 1.8 : 1.2,
      dash: edge.edge_type === 'paragraph_entity' ? '5 5' : edge.edge_type === 'paragraph_person' ? '2 5' : undefined,
    }),
    ambientEdgeStyle: edge => ({
      width: edge.edge_type === 'episode_paragraph' ? 1.5 : 1.15,
      dash: edge.edge_type === 'episode_entity' ? '6 7' : undefined,
    }),
  }
}
