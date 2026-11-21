import type { Theme } from '@mui/material/styles'
import type { AtlasColorRole } from '../../theme/themeConfig'
import type { AtlasClusterConfig, AtlasClusterSpec, AtlasGraphEdgeData, AtlasGraphNodeData } from '../memory/atlas/types'
import type { AffectionStageValue } from '../../services/api/affection'

/**
 * 好感关系图的簇配置（画布与记忆图谱共用 `AtlasGraph`）
 *
 * 人设是核心簇，星形视图里放在中间；人在内圈，关系里程碑与频道在外圈。常显的只有「人 → 人设」
 * 那一条（一份好感一条）：线宽按分数，颜色按阶段（`affectionStageColor`）。里程碑与频道的线
 * 只在选中时画出来。人与人之间没有边。
 */

export type AffectionClusterKey = 'preset' | 'person' | 'milestone' | 'channel'

/** 各簇的颜色角色：人设是核心、人与记忆图的人同色、里程碑本来就是情景记忆、频道是背景 */
export const AFFECTION_CLUSTER_ROLES: Record<AffectionClusterKey, AtlasColorRole> = {
  preset: 'core',
  person: 'person',
  milestone: 'episodic',
  channel: 'context',
}

export const NEGATIVE_STAGES: ReadonlySet<string> = new Set(['wary', 'hostile'])

/**
 * 关系阶段的配色（关系图的边与面板里的阶段标签）
 *
 * 越亲近越靠品牌色：亲密、信任用 `primary`（随主题预设走），友好、相识用 `success`，陌生是中性的
 * 次要文字色，提防 `warning`，厌恶才用 `error`。「亲密」不再是报错红。
 */
export function affectionStageColor(theme: Theme, stage: AffectionStageValue | string): string {
  if (stage === 'trusted' || stage === 'close') return theme.palette.primary.main
  if (stage === 'friendly' || stage === 'acquainted') return theme.palette.success.main
  if (stage === 'wary') return theme.palette.warning.main
  if (stage === 'hostile') return theme.palette.error.main
  return theme.palette.text.secondary
}

export interface AffectionAtlasOptions {
  /** 簇标题（跟着界面语言走） */
  titles: Record<AffectionClusterKey, { title: string; episodeTitle?: string }>
  /** 阶段配色（边的颜色） */
  stageColor: (stage: AffectionStageValue) => string
}

/** 边上的分数在整个区间里的位置（后端给的 `strength`，0 ~ 1） */
function strengthOf(edge: AtlasGraphEdgeData): number {
  return Number.isFinite(edge.strength) ? Math.max(0, Math.min(1, edge.strength)) : 0
}

function stageOfEdge(edge: AtlasGraphEdgeData): string {
  const stage = edge.metadata.stage
  return typeof stage === 'string' ? stage : ''
}

export function createAffectionAtlasConfig(options: AffectionAtlasOptions): AtlasClusterConfig {
  const clusters: Record<AffectionClusterKey, AtlasClusterSpec> = {
    preset: {
      ...options.titles.preset,
      role: AFFECTION_CLUSTER_ROLES.preset,
      center: { x: 0, y: 0 },
      episodeRole: 'core',
      limits: {
        episode: { low: 6, medium: 8, high: 12 },
        atomic: { low: 6, medium: 8, high: 12 },
      },
      zoomExpansionFactor: 1,
      ringBase: 22,
      ringStep: 48,
      ringSquash: 0.48,
      nodeSize: importance => Math.min(15, 9.5 + importance * 1.6),
      labelTier: 'always',
      tint: { fill: 0.18, glow: 0.46 },
    },
    person: {
      ...options.titles.person,
      role: AFFECTION_CLUSTER_ROLES.person,
      center: { x: -300, y: 20 },
      episodeRole: 'band',
      band: { inner: 190, outer: 340, stretchY: 0.74, biasX: 0, biasY: 0 },
      limits: {
        episode: { low: 24, medium: 40, high: 60 },
        atomic: { low: 30, medium: 50, high: 80 },
      },
      zoomExpansionFactor: 4,
      ringBase: 30,
      ringStep: 26,
      ringSquash: 0.72,
      nodeSize: importance => Math.min(14, 7 + importance * 3),
      labelTier: 'other',
    },
    milestone: {
      ...options.titles.milestone,
      role: AFFECTION_CLUSTER_ROLES.milestone,
      center: { x: 300, y: -170 },
      episodeRole: 'band',
      band: { inner: 370, outer: 520, stretchY: 0.78, biasX: -20, biasY: 10 },
      limits: {
        episode: { low: 10, medium: 18, high: 28 },
        atomic: { low: 12, medium: 20, high: 32 },
      },
      zoomExpansionFactor: 2,
      ringBase: 36,
      ringStep: 24,
      ringSquash: 0.68,
      nodeSize: importance => Math.min(11, 6.5 + importance * 2),
      labelTier: 'entity',
      tint: { fill: 0.17, glow: 0.46 },
    },
    channel: {
      ...options.titles.channel,
      role: AFFECTION_CLUSTER_ROLES.channel,
      center: { x: 300, y: 180 },
      episodeRole: 'band',
      band: { inner: 360, outer: 500, stretchY: 0.7, biasX: 30, biasY: -10 },
      limits: {
        episode: { low: 6, medium: 10, high: 16 },
        atomic: { low: 8, medium: 12, high: 18 },
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
    atomicOrder: ['preset', 'person', 'milestone', 'channel'],
    episodeOuterOrder: ['person', 'channel', 'milestone'],
    coreCluster: 'preset',
    personCluster: 'person',
    ambientEdgeTypes: ['affection'],
    showAmbientEdges: true,
    resolveCluster: (node: AtlasGraphNodeData) =>
      node.memory_type in clusters ? node.memory_type : 'person',
    edgeStyle: edge => ({
      width: edge.edge_type === 'affection' ? 1.2 + strengthOf(edge) * 2.2 : 1,
      dash: edge.edge_type === 'milestone' ? '3 5' : edge.edge_type === 'last_seen' ? '6 6' : undefined,
    }),
    ambientEdgeStyle: edge => {
      const stage = stageOfEdge(edge)
      return {
        width: 0.6 + strengthOf(edge) * 3.4,
        // 提防、厌恶画成虚线：一眼分得出「不喜欢」与「还不熟」
        dash: NEGATIVE_STAGES.has(stage) ? '5 5' : undefined,
        color: stage ? options.stageColor(stage as AffectionStageValue) : undefined,
      }
    },
  }
}
