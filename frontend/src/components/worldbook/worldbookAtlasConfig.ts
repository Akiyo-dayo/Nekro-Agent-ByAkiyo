import type { AtlasClusterConfig, AtlasClusterSpec, AtlasGraphEdgeData, AtlasGraphNodeData } from '../memory/atlas/types'
import type { AtlasColorRole } from '../../theme/themeConfig'

/**
 * 世界书图谱的簇配置（画布与记忆图谱共用 `AtlasGraph`）
 *
 * 书是核心簇，放在中间；条目在内圈，绑定对象（人设 / 频道 / 人，后两者合成「绑定」一簇）在外圈。
 * 常显的只有「书 → 条目」这一种边：一个条目属于哪本书一眼看得出来。书 → 绑定对象与
 * 条目之间的关键词引用只在选中某个节点时画出来，否则一个大书库会糊成一片。
 *
 * 颜色只给角色：书用 `core`、条目用 `semantic`、人用 `person`、绑定对象用 `context`；
 * 具体色值由 `ATLAS_COLORS` 从主题取，这里不写死。
 */

export type WorldbookClusterKey = 'book' | 'entry' | 'person' | 'binding'

export const WORLDBOOK_CLUSTER_ROLES: Record<WorldbookClusterKey, AtlasColorRole> = {
  book: 'core',
  entry: 'semantic',
  person: 'person',
  binding: 'context',
}

export interface WorldbookAtlasOptions {
  /** 簇标题（跟着界面语言走） */
  titles: Record<WorldbookClusterKey, { title: string; episodeTitle?: string }>
}

export function createWorldbookAtlasConfig(options: WorldbookAtlasOptions): AtlasClusterConfig {
  const clusters: Record<WorldbookClusterKey, AtlasClusterSpec> = {
    book: {
      ...options.titles.book,
      role: WORLDBOOK_CLUSTER_ROLES.book,
      center: { x: 0, y: 0 },
      episodeRole: 'core',
      limits: {
        episode: { low: 8, medium: 16, high: 24 },
        atomic: { low: 8, medium: 16, high: 24 },
      },
      zoomExpansionFactor: 1,
      ringBase: 24,
      ringStep: 52,
      ringSquash: 0.5,
      nodeSize: () => 13,
      labelTier: 'always',
      tint: { fill: 0.2, glow: 0.48 },
    },
    entry: {
      ...options.titles.entry,
      role: WORLDBOOK_CLUSTER_ROLES.entry,
      center: { x: 300, y: -180 },
      episodeRole: 'band',
      band: { inner: 190, outer: 520, stretchY: 0.78, biasX: -20, biasY: 10 },
      limits: {
        // 条目是这张图的主体：密档给到 200 左右，疏档只留最近注入最多的 40 条
        episode: { low: 40, medium: 100, high: 200 },
        atomic: { low: 40, medium: 100, high: 200 },
      },
      zoomExpansionFactor: 2,
      ringBase: 36,
      ringStep: 24,
      ringSquash: 0.7,
      nodeSize: importance => Math.min(12, 6 + importance * 4),
      labelTier: 'entity',
      tint: { fill: 0.16, glow: 0.44 },
    },
    person: {
      ...options.titles.person,
      role: WORLDBOOK_CLUSTER_ROLES.person,
      center: { x: -310, y: 40 },
      episodeRole: 'band',
      band: { inner: 200, outer: 360, stretchY: 0.74, biasX: 0, biasY: 0 },
      limits: {
        episode: { low: 20, medium: 40, high: 60 },
        atomic: { low: 20, medium: 40, high: 60 },
      },
      zoomExpansionFactor: 4,
      ringBase: 30,
      ringStep: 26,
      ringSquash: 0.72,
      nodeSize: importance => Math.min(14, 7 + importance * 3),
      labelTier: 'other',
    },
    binding: {
      ...options.titles.binding,
      role: WORLDBOOK_CLUSTER_ROLES.binding,
      center: { x: 320, y: 200 },
      episodeRole: 'band',
      band: { inner: 360, outer: 500, stretchY: 0.7, biasX: 30, biasY: -10 },
      limits: {
        episode: { low: 12, medium: 24, high: 40 },
        atomic: { low: 12, medium: 24, high: 40 },
      },
      zoomExpansionFactor: 2,
      ringBase: 32,
      ringStep: 26,
      ringSquash: 0.7,
      nodeSize: importance => Math.min(12, 7 + importance * 1.5),
      labelTier: 'entity',
    },
  }

  return {
    clusters,
    atomicOrder: ['book', 'entry', 'person', 'binding'],
    episodeOuterOrder: ['entry', 'person', 'binding'],
    coreCluster: 'book',
    personCluster: 'person',
    ambientEdgeTypes: ['book_entry'],
    showAmbientEdges: true,
    resolveCluster: (node: AtlasGraphNodeData) =>
      node.memory_type === 'book' || node.memory_type === 'entry' || node.memory_type === 'person'
        ? node.memory_type
        : 'binding',
    edgeStyle: (edge: AtlasGraphEdgeData) => ({
      width: edge.edge_type === 'key_reference' ? 1.2 : 1,
      // 推导出来的引用用虚线：与「包含 / 绑定」这种结构边分开
      dash: edge.edge_type === 'key_reference' ? '5 5' : undefined,
    }),
    ambientEdgeStyle: () => ({ width: 0.7 }),
  }
}
