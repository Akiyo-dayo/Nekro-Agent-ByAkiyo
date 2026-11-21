import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  ListItemText,
  MenuItem,
  Menu,
  Select,
  Slider,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
  useMediaQuery,
} from '@mui/material'
import { alpha, useTheme, type Theme } from '@mui/material/styles'
import {
  AcUnit as FreezeIcon,
  ChevronLeft as CollapseIcon,
  Close as CloseIcon,
  Delete as DeleteIcon,
  Edit as EditIcon,
  GppGood as ProtectIcon,
  InfoOutlined as InfoIcon,
  Inventory2 as LibraryIcon,
  MoreHoriz as MoreIcon,
  Refresh as RefreshIcon,
  Remove as ZoomOutIcon,
  Restore as UnfreezeIcon,
  Search as SearchIcon,
  TrendingDown as DemoteIcon,
  TrendingUp as ReinforceIcon,
  Add as ZoomInIcon,
} from '@mui/icons-material'
import { motion, AnimatePresence } from 'framer-motion'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import {
  memoryApi,
  MemoryDataResponse,
  MemoryDetailResponse,
  MemoryGraphResponse,
  MemoryListResponse,
  MemoryRebuildStatus,
  MemoryTraceResponse,
  WorkspaceDetail,
} from '../../../services/api/workspace'
import { useNotification } from '../../../hooks/useNotification'
import { CARD_VARIANTS, CHIP_VARIANTS, SCROLLBAR_VARIANTS } from '../../../theme/variants'
import ActionButton from '../../../components/common/ActionButton'
import IconActionButton from '../../../components/common/IconActionButton'
import AtlasGraph from '../../../components/memory/atlas/AtlasGraph'
import { buildAtlasLayout } from '../../../components/memory/atlas/layout'
import { createMemoryAtlasConfig } from '../../../components/memory/atlas/memoryAtlasConfig'
import { ATLAS_COLORS } from '../../../theme/themeConfig'
import type { AtlasViewMode, InformationDensity } from '../../../components/memory/atlas/types'
import { useAtlasViewport } from '../../../components/memory/atlas/useAtlasViewport'

// 画布与布局在 components/memory/atlas/ 里（后台记忆页的图谱也用它）；这份配置画出来与拆分前一样
const MEMORY_ATLAS_CONFIG = createMemoryAtlasConfig({ showAxes: true })

type MemoryTypeFilter = 'all' | 'paragraph' | 'entity' | 'relation' | 'episode'
type MemoryStatusFilter = 'all' | 'active' | 'inactive'
type MemoryCognitiveTypeFilter = 'all' | 'episodic' | 'semantic'
type MemorySortBy = 'event_time' | 'update_time' | 'create_time' | 'effective_weight'
type MemorySortOrder = 'asc' | 'desc'

type MemorySelection = {
  memory_type: 'paragraph' | 'entity' | 'relation' | 'episode'
  id: number
  title: string
  subtitle?: string
  cognitive_type?: string | null
  status?: string
}

type EpisodeParticipant = {
  id: number | string
  name?: string
  entity_type?: string
}

type EpisodeParagraph = {
  id: number | string
  summary?: string
  episode_phase?: string
  knowledge_type?: string
  event_time?: string | null
}

function formatDateTime(value: string | number | null | undefined): string {
  if (!value) return '-'
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', { hour12: false })
}

function formatFieldValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return Number.isFinite(value) ? value.toFixed(4).replace(/\.?0+$/, '') : String(value)
  if (typeof value === 'object') return JSON.stringify(value, null, 2)
  return String(value)
}

const ENUM_LABELS: Record<string, Record<string, string>> = {
  memory_type: { paragraph: '段落', entity: '实体', relation: '关系', episode: '事件' },
  cognitive_type: { semantic: '语义', episodic: '情景' },
  status: { active: '活跃', inactive: '失活' },
  memory_source: { chat: '聊天', cc: '委托结果', manual: '手动', system: '系统' },
  knowledge_type: { fact: '事实', preference: '偏好', experience: '经验', profile: '档案', skill: '技能', task: '任务' },
  entity_type: { person: '人物', place: '地点', organization: '组织', object: '对象', event: '事件', concept: '概念', tag: '标签' },
  origin_kind: { chat_message: '聊天消息', cc_task: '委托任务', manual: '手动创建', system: '系统生成' },
  last_manual_action: {
    reinforce: '强化',
    demote: '降权',
    freeze: '冻结',
    unfreeze: '解冻',
    protect: '保护',
    unprotect: '取消保护',
    edit: '编辑',
    delete: '删除',
  },
  episode_phase: {
    opening: '开端',
    development: '发展',
    climax: '高潮',
    resolution: '收束',
  },
}

function formatEnumValue(fieldKey: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'string' && ENUM_LABELS[fieldKey]) {
    return ENUM_LABELS[fieldKey][value] ?? value
  }
  return formatFieldValue(value)
}

function truncateLabel(label: string, maxLength: number): string {
  if (label.length <= maxLength) return label
  return `${label.slice(0, maxLength - 1)}…`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isEpisodeParticipant(value: unknown): value is EpisodeParticipant {
  return isRecord(value) && (typeof value.id === 'number' || typeof value.id === 'string')
}

function isEpisodeParagraph(value: unknown): value is EpisodeParagraph {
  return isRecord(value) && (typeof value.id === 'number' || typeof value.id === 'string')
}

function getMemoryTypeAccent(theme: Theme, memoryType: 'paragraph' | 'entity' | 'relation' | 'episode', cognitiveType?: string | null) {
  if (memoryType === 'episode') return ATLAS_COLORS.core
  if (memoryType === 'relation') return ATLAS_COLORS.relation
  if (memoryType === 'entity') return ATLAS_COLORS.entity
  if (cognitiveType === 'semantic') return ATLAS_COLORS.semantic
  if (cognitiveType === 'episodic') return ATLAS_COLORS.episodic
  return theme.palette.primary.main
}

function getDetailFields(data: Record<string, unknown>): Array<{ key: string; label: string; wide?: boolean }> {
  const fields: Array<{ key: string; label: string; wide?: boolean }> = [
    { key: 'summary', label: '摘要', wide: true },
    { key: 'title', label: '标题', wide: true },
    { key: 'narrative_summary', label: '事件摘要', wide: true },
    { key: 'content', label: '内容', wide: true },
    { key: 'canonical_name', label: '规范名称' },
    { key: 'entity_type', label: '实体类型' },
    { key: 'subject_name', label: '主语' },
    { key: 'predicate', label: '关系' },
    { key: 'object_name', label: '宾语' },
    { key: 'memory_source', label: '来源' },
    { key: 'cognitive_type', label: '认知类型' },
    { key: 'knowledge_type', label: '知识类型' },
    { key: 'appearance_count', label: '出现次数' },
    { key: 'is_frozen', label: '已冻结' },
    { key: 'is_protected', label: '已保护' },
    { key: 'origin_kind', label: '来源类型' },
    { key: 'origin_ref', label: '来源引用', wide: true },
    { key: 'origin_chat_key', label: '来源频道', wide: true },
    { key: 'event_time', label: '事件时间' },
    { key: 'time_start', label: '开始时间' },
    { key: 'time_end', label: '结束时间' },
    { key: 'create_time', label: '创建时间' },
    { key: 'update_time', label: '更新时间' },
    { key: 'last_manual_action', label: '最近手动操作' },
    { key: 'last_manual_action_at', label: '最近手动时间' },
  ]
  return fields.filter(field => field.key in data)
}

function StatChip({ label, color }: { label: string; color: string }) {
  return <Chip size="small" label={label} sx={CHIP_VARIANTS.getCustomColorChip(color, true)} />
}

export default function MemoryTab({ workspace }: { workspace: WorkspaceDetail }) {
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))
  const { t } = useTranslation('workspace')
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const notification = useNotification()
  const memorySystemDisabled = !workspace.memory_system_enabled
  const mobileOverlayTop = memorySystemDisabled ? 260 : 178
  const [viewMode, setViewMode] = useState<AtlasViewMode>('episode')

  const [graphIncludeInactive, setGraphIncludeInactive] = useState(false)
  const viewport = useAtlasViewport()
  const { zoom, updateZoom } = viewport
  const [browserOpen, setBrowserOpen] = useState(false)
  const [selection, setSelection] = useState<MemorySelection | null>(null)
  const [memoryType, setMemoryType] = useState<MemoryTypeFilter>('all')
  const [memoryStatus, setMemoryStatus] = useState<MemoryStatusFilter>('active')
  const [memoryCognitiveType, setMemoryCognitiveType] = useState<MemoryCognitiveTypeFilter>('all')
  const [sortBy, setSortBy] = useState<MemorySortBy>('event_time')
  const [sortOrder, setSortOrder] = useState<MemorySortOrder>('desc')
  const [timeFrom, setTimeFrom] = useState('')
  const [timeTo, setTimeTo] = useState('')
  const [informationDensity, setInformationDensity] = useState<InformationDensity>('medium')
  const [actionsAnchor, setActionsAnchor] = useState<null | HTMLElement>(null)
  const [confirmAction, setConfirmAction] = useState<null | 'prune' | 'reset' | 'rebuild'>(null)
  const [searchText, setSearchText] = useState('')
  const [editSummary, setEditSummary] = useState('')
  const [editContent, setEditContent] = useState('')
  const [dismissedRebuildJobId, setDismissedRebuildJobId] = useState<string | null>(null)

  const {
    data: memoryData,
    isLoading: memoryDataLoading,
    refetch: refetchMemoryData,
  } = useQuery<MemoryDataResponse>({
    queryKey: ['memory-data', workspace.id],
    queryFn: () => memoryApi.getData(workspace.id, 24),
  })

  const {
    data: memoryGraph,
    isLoading: memoryGraphLoading,
    refetch: refetchMemoryGraph,
  } = useQuery<MemoryGraphResponse>({
    queryKey: ['memory-graph', workspace.id, graphIncludeInactive, timeFrom, timeTo],
    queryFn: () => memoryApi.getGraph(workspace.id, 320, graphIncludeInactive, timeFrom || undefined, timeTo || undefined),
  })

  const {
    data: memoryList,
    isLoading: memoryListLoading,
    refetch: refetchMemoryList,
  } = useQuery<MemoryListResponse>({
    queryKey: ['memory-list', workspace.id, memoryType, memoryStatus, memoryCognitiveType, timeFrom, timeTo, sortBy, sortOrder],
    queryFn: () =>
      memoryApi.list(workspace.id, {
        memory_type: memoryType === 'all' ? undefined : memoryType,
        status: memoryStatus === 'all' ? undefined : memoryStatus,
        cognitive_type: memoryCognitiveType === 'all' ? undefined : memoryCognitiveType,
        time_from: timeFrom || undefined,
        time_to: timeTo || undefined,
        sort_by: sortBy,
        order: sortOrder,
        limit: 180,
      }),
  })

  const {
    data: memoryDetail,
    isLoading: memoryDetailLoading,
  } = useQuery<MemoryDetailResponse>({
    queryKey: ['memory-detail', workspace.id, selection?.memory_type, selection?.id],
    queryFn: () => memoryApi.detail(workspace.id, selection!.memory_type, selection!.id),
    enabled: !!selection && selection.id > 0,
  })

  const {
    data: memoryTrace,
    isLoading: memoryTraceLoading,
  } = useQuery<MemoryTraceResponse>({
    queryKey: ['memory-trace', workspace.id, selection?.id],
    queryFn: () => memoryApi.trace(workspace.id, selection!.id),
    enabled: selection?.memory_type === 'paragraph' && (selection?.id ?? 0) > 0,
  })
  const {
    data: rebuildStatus,
    refetch: refetchRebuildStatus,
  } = useQuery<MemoryRebuildStatus>({
    queryKey: ['memory-rebuild-status', workspace.id],
    queryFn: () => memoryApi.getRebuildStatus(workspace.id),
    refetchInterval: query => {
      const data = query.state.data as MemoryRebuildStatus | undefined
      if (data?.is_running) return 2000
      if (data?.status === 'failed' || data?.status === 'completed') return 10000
      return false
    },
  })

  useEffect(() => {
    if (selection?.memory_type === 'paragraph') {
      setEditSummary(String(memoryDetail?.data?.summary ?? ''))
      setEditContent(String(memoryDetail?.data?.content ?? ''))
    } else {
      setEditSummary('')
      setEditContent('')
    }
  }, [memoryDetail, selection])

  const refetchAll = () => {
    refetchMemoryData()
    refetchMemoryGraph()
    refetchMemoryList()
    refetchRebuildStatus()
    if (selection && selection.id > 0) {
      queryClient.invalidateQueries({ queryKey: ['memory-detail', workspace.id, selection.memory_type, selection.id] })
      if (selection.memory_type === 'paragraph') {
        queryClient.invalidateQueries({ queryKey: ['memory-trace', workspace.id, selection.id] })
      }
    }
  }

  const resetMutation = useMutation({
    mutationFn: () => memoryApi.resetMemory(workspace.id),
    onSuccess: () => {
      notification.success('结构化记忆已清空')
      setSelection(null)
      refetchAll()
    },
    onError: () => notification.error('清空结构化记忆失败'),
  })

  const rebuildMutation = useMutation({
    mutationFn: () => memoryApi.rebuildMemory(workspace.id),
    onSuccess: result => {
      notification.success(result.reused ? '已有重建任务在运行，已切换到当前任务' : '记忆库重建任务已创建')
      setSelection(null)
      refetchAll()
    },
    onError: () => notification.error('重建记忆库失败'),
  })

  const cancelRebuildMutation = useMutation({
    mutationFn: () => memoryApi.cancelRebuildMemory(workspace.id),
    onSuccess: result => {
      notification.success(result.message || '已请求取消记忆重建任务')
      refetchAll()
    },
    onError: () => notification.error('取消记忆重建任务失败'),
  })

  const pruneMutation = useMutation({
    mutationFn: () => memoryApi.prune(workspace.id),
    onSuccess: result => {
      notification.success(`清理完成：段落 ${result.paragraphs_pruned}，关系 ${result.relations_pruned}，实体 ${result.entities_pruned}`)
      refetchAll()
    },
    onError: () => notification.error('清理低价值记忆失败'),
  })

  const reinforceMutation = useMutation({
    mutationFn: ({ type, id }: { type: 'paragraph' | 'entity' | 'relation' | 'episode'; id: number }) =>
      memoryApi.reinforce(workspace.id, type, id, 0.3),
    onSuccess: () => {
      notification.success('记忆已强化')
      refetchAll()
    },
    onError: () => notification.error('强化记忆失败'),
  })

  const demoteMutation = useMutation({
    mutationFn: ({ type, id }: { type: 'paragraph' | 'entity' | 'relation' | 'episode'; id: number }) =>
      memoryApi.demote(workspace.id, type, id, 0.2),
    onSuccess: () => {
      notification.success('记忆权重已降低')
      refetchAll()
    },
    onError: () => notification.error('降低记忆权重失败'),
  })

  const freezeMutation = useMutation({
    mutationFn: ({ type, id }: { type: 'paragraph' | 'entity' | 'relation' | 'episode'; id: number }) =>
      memoryApi.freeze(workspace.id, type, id),
    onSuccess: () => {
      notification.success('记忆已冻结')
      refetchAll()
    },
    onError: () => notification.error('冻结记忆失败'),
  })

  const unfreezeMutation = useMutation({
    mutationFn: ({ type, id }: { type: 'paragraph' | 'entity' | 'relation' | 'episode'; id: number }) =>
      memoryApi.unfreeze(workspace.id, type, id),
    onSuccess: () => {
      notification.success('记忆已解冻')
      refetchAll()
    },
    onError: () => notification.error('解冻记忆失败'),
  })

  const protectMutation = useMutation({
    mutationFn: ({ type, id, protectedFlag }: { type: 'paragraph' | 'entity' | 'relation' | 'episode'; id: number; protectedFlag: boolean }) =>
      memoryApi.protect(workspace.id, type, id, protectedFlag),
    onSuccess: () => {
      notification.success('记忆保护状态已更新')
      refetchAll()
    },
    onError: () => notification.error('更新保护状态失败'),
  })

  const editMutation = useMutation({
    mutationFn: ({ type, id, summary, content }: { type: 'paragraph' | 'entity' | 'relation' | 'episode'; id: number; summary?: string; content?: string }) =>
      memoryApi.edit(workspace.id, type, id, { summary, content }),
    onSuccess: () => {
      notification.success('记忆已更新')
      refetchAll()
    },
    onError: () => notification.error('编辑记忆失败'),
  })

  const deleteMutation = useMutation({
    mutationFn: ({ type, id }: { type: 'paragraph' | 'entity' | 'relation' | 'episode'; id: number }) =>
      memoryApi.remove(workspace.id, type, id),
    onSuccess: () => {
      notification.success('记忆已删除')
      setSelection(null)
      refetchAll()
    },
    onError: () => notification.error('删除记忆失败'),
  })

  const atlas = useMemo(
    () => buildAtlasLayout(theme, memoryGraph, zoom, informationDensity, viewMode, MEMORY_ATLAS_CONFIG),
    [theme, memoryGraph, zoom, informationDensity, viewMode],
  )
  const detailFields = useMemo(() => getDetailFields((memoryDetail?.data ?? {}) as Record<string, unknown>), [memoryDetail])
  const selectedNodeId = selection ? `${selection.memory_type}-${selection.id}` : null

  const visibleListItems = useMemo(() => {
    const q = searchText.trim().toLowerCase()
    const items = memoryList?.items ?? []
    if (!q) return items
    return items.filter(item =>
      item.title.toLowerCase().includes(q) ||
      item.subtitle.toLowerCase().includes(q) ||
      (item.cognitive_type ?? '').toLowerCase().includes(q),
    )
  }, [memoryList, searchText])

  const selectedActions = useMemo(() => {
    if (!selection || selection.id <= 0) return null
    return {
      canReinforce: selection.memory_type === 'paragraph' || selection.memory_type === 'relation',
      canFreeze: selection.memory_type === 'paragraph',
      canProtect: selection.memory_type === 'paragraph',
      canEdit: selection.memory_type === 'paragraph',
    }
  }, [selection])

  const selectedAtlasNode = useMemo(
    () => atlas.nodes.find(node => selection && node.id === `${selection.memory_type}-${selection.id}`),
    [atlas.nodes, selection],
  )
  const episodeParticipants = useMemo(() => {
    const raw = memoryDetail?.data?.participant_entities
    return Array.isArray(raw) ? raw.filter(isEpisodeParticipant) : []
  }, [memoryDetail])
  const episodeParagraphs = useMemo(() => {
    const raw = memoryDetail?.data?.paragraphs
    return Array.isArray(raw) ? raw.filter(isEpisodeParagraph) : []
  }, [memoryDetail])
  const episodeCount = useMemo(
    () => memoryGraph?.nodes.filter(node => node.memory_type === 'episode').length ?? 0,
    [memoryGraph?.nodes],
  )
  const rebuildStatusTone =
    rebuildStatus?.status === 'failed'
      ? 'error'
      : rebuildStatus?.status === 'cancel_requested'
        ? 'warning'
        : rebuildStatus?.status === 'stalled'
          ? 'error'
      : rebuildStatus?.is_running
        ? 'warning'
        : rebuildStatus?.status === 'completed'
          ? 'success'
          : 'default'
  const rebuildStatusLabel =
    rebuildStatus?.status === 'failed'
      ? '重建失败'
      : rebuildStatus?.status === 'cancel_requested'
        ? '正在取消'
        : rebuildStatus?.status === 'stalled'
          ? '重建中断'
      : rebuildStatus?.is_running
        ? '重建进行中'
        : rebuildStatus?.status === 'completed'
        ? '重建完成'
          : '空闲'
  const shouldShowRebuildCard = Boolean(
    rebuildStatus &&
      rebuildStatus.job_id !== dismissedRebuildJobId &&
      (
        rebuildStatus.is_running ||
        rebuildStatus.status === 'failed' ||
        rebuildStatus.status === 'cancel_requested' ||
        rebuildStatus.status === 'stalled'
      ),
  )
  const visibleRebuildChannels = useMemo(
    () =>
      (rebuildStatus?.channels ?? [])
        .slice()
        .sort((a, b) => Number(a.completed) - Number(b.completed) || b.progress_ratio - a.progress_ratio)
        .slice(0, 4),
    [rebuildStatus],
  )

  useEffect(() => {
    if (!rebuildStatus?.job_id) {
      setDismissedRebuildJobId(null)
      return
    }
    if (rebuildStatus.is_running && rebuildStatus.job_id === dismissedRebuildJobId) {
      setDismissedRebuildJobId(null)
      return
    }
    if (dismissedRebuildJobId && rebuildStatus.job_id !== dismissedRebuildJobId) {
      setDismissedRebuildJobId(null)
    }
  }, [dismissedRebuildJobId, rebuildStatus?.is_running, rebuildStatus?.job_id])

  return (
    <Box
      sx={{
        position: 'relative',
        minHeight: { xs: 'calc(100vh - 150px)', sm: 'calc(100vh - 210px)' },
        borderRadius: { xs: 2, sm: 3 },
        overflow: 'hidden',
        border: '1px solid',
        borderColor: alpha(theme.palette.primary.main, 0.12),
        background: `
          linear-gradient(180deg, ${alpha(theme.palette.background.paper, 0.96)}, ${alpha(theme.palette.background.default, 0.99)})
        `,
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

      <Box sx={{ position: 'relative', height: '100%', minHeight: { xs: 'calc(100vh - 150px)', sm: 'calc(100vh - 210px)' } }}>
        <Stack
          direction="row"
          spacing={1}
          sx={{
            position: 'absolute',
            top: { xs: 10, sm: 16 },
            left: { xs: 10, sm: 16 },
            right: { xs: 10, sm: 'auto' },
            zIndex: 5,
            flexWrap: 'wrap',
            maxWidth: { xs: 'none', sm: 'min(72vw, 720px)' },
          }}
        >
          <ActionButton
            tone={browserOpen ? 'primary' : 'secondary'}
            size="small"
            startIcon={<LibraryIcon />}
            onClick={() => setBrowserOpen(value => !value)}
          >
            记忆图谱
          </ActionButton>
          <ActionButton tone="secondary" size="small" startIcon={<RefreshIcon />} onClick={refetchAll}>
            刷新
          </ActionButton>
          <ActionButton
            tone="secondary"
            size="small"
            startIcon={<MoreIcon />}
            onClick={event => setActionsAnchor(event.currentTarget)}
          >
            其他操作
          </ActionButton>
        </Stack>

        {shouldShowRebuildCard && !!rebuildStatus && (
          <Card
            sx={{
              position: 'absolute',
              top: { xs: mobileOverlayTop, sm: memorySystemDisabled ? 152 : 68 },
              left: { xs: 10, sm: 16 },
              right: { xs: 10, sm: 'auto' },
              zIndex: 5,
              minWidth: { xs: 0, sm: 300 },
              maxWidth: { xs: 'none', sm: 420 },
              borderRadius: 2.5,
              border: '1px solid',
              borderColor:
                rebuildStatusTone === 'error'
                  ? alpha(theme.palette.error.main, 0.2)
                  : rebuildStatusTone === 'success'
                    ? alpha(theme.palette.success.main, 0.2)
                    : alpha(theme.palette.warning.main, 0.2),
              backgroundColor: alpha(theme.palette.background.paper, 0.86),
              backdropFilter: 'blur(14px)',
              boxShadow: `0 16px 48px ${alpha(theme.palette.common.black, 0.1)}`,
            }}
          >
            <CardContent sx={{ p: 1.5, '&:last-child': { pb: 1.5 } }}>
              <Stack spacing={1}>
                <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Chip
                      size="small"
                      color={rebuildStatusTone === 'default' ? 'default' : rebuildStatusTone}
                      label={rebuildStatusLabel}
                    />
                    {rebuildStatus.is_running && <CircularProgress size={16} thickness={4} />}
                  </Stack>
                  <Stack direction="row" spacing={0.5} alignItems="center">
                    <Typography variant="caption" color="text.secondary">
                      {rebuildStatus.completed_channels}/{rebuildStatus.total_channels} 频道
                    </Typography>
                    {!rebuildStatus.is_running && (
                      <Tooltip title={t('skills.drawer.close')}>
                        <IconActionButton
                          size="small"
                          onClick={() => setDismissedRebuildJobId(rebuildStatus.job_id ?? null)}
                          sx={{ ml: 0.25 }}
                        >
                          <CloseIcon sx={{ fontSize: 16 }} />
                        </IconActionButton>
                      </Tooltip>
                    )}
                  </Stack>
                </Stack>

                <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap>
                  <Typography variant="caption" color="text.secondary">
                    总进度 {Math.round(rebuildStatus.overall_progress_percent || 0)}%
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    已处理 {rebuildStatus.total_messages_processed} 条
                  </Typography>
                  {rebuildStatus.phase && (
                    <Typography variant="caption" color="text.secondary">
                      阶段 {rebuildStatus.phase}
                    </Typography>
                  )}
                  {rebuildStatus.current_chat_key && (
                    <Typography variant="caption" color="text.secondary">
                      当前频道 {rebuildStatus.current_chat_key}
                    </Typography>
                  )}
                  {rebuildStatus.cutoff && (
                    <Typography variant="caption" color="text.secondary">
                      起点 {formatDateTime(rebuildStatus.cutoff)}
                    </Typography>
                  )}
                  {rebuildStatus.started_at && (
                    <Typography variant="caption" color="text.secondary">
                      开始于 {formatDateTime(rebuildStatus.started_at)}
                    </Typography>
                  )}
                </Stack>

                {rebuildStatus.failure_reason && (
                  <Alert severity={rebuildStatus.status === 'failed' ? 'error' : 'warning'} sx={{ py: 0 }}>
                    {rebuildStatus.failure_reason}
                  </Alert>
                )}

                {visibleRebuildChannels.length > 0 && (
                  <Stack spacing={0.8}>
                    {visibleRebuildChannels.map(channel => (
                      <Box key={channel.chat_key}>
                        <Stack direction="row" justifyContent="space-between" spacing={1}>
                          <Typography variant="caption" sx={{ fontFamily: 'monospace' }}>
                            {channel.chat_key}
                          </Typography>
                          <Typography variant="caption" color="text.secondary">
                            {channel.completed ? '已完成' : `${Math.round(channel.progress_ratio * 100)}%`}
                          </Typography>
                        </Stack>
                        <Box
                          sx={{
                            mt: 0.4,
                            height: 5,
                            borderRadius: 999,
                            overflow: 'hidden',
                            backgroundColor: alpha(theme.palette.text.primary, 0.08),
                          }}
                        >
                          <Box
                            sx={{
                              width: `${Math.max(4, Math.round(channel.progress_ratio * 100))}%`,
                              height: '100%',
                              borderRadius: 999,
                              background: channel.completed
                                ? `linear-gradient(90deg, ${theme.palette.success.light}, ${theme.palette.success.main})`
                                : `linear-gradient(90deg, ${theme.palette.warning.light}, ${theme.palette.warning.main})`,
                              transition: 'width 240ms ease',
                            }}
                          />
                        </Box>
                      </Box>
                    ))}
                  </Stack>
                )}
              </Stack>
            </CardContent>
          </Card>
        )}

        {memorySystemDisabled && (
          <Alert
            severity="warning"
            sx={{
              position: 'absolute',
              top: { xs: 124, sm: 80 },
              left: { xs: 10, sm: 16 },
              right: { xs: 10, lg: 360 },
              zIndex: 5,
              maxWidth: { xs: 'none', lg: 680 },
              alignItems: 'flex-start',
              borderRadius: 2,
              boxShadow: `0 12px 36px ${alpha(theme.palette.common.black, 0.08)}`,
              '& .MuiAlert-message': {
                minWidth: 0,
              },
            }}
            action={
              <ActionButton
                tone="secondary"
                size="small"
                onClick={() => navigate('/settings/system?category=记忆系统&search=MEMORY_ENABLE_SYSTEM')}
                sx={{ whiteSpace: 'nowrap', minWidth: 88, alignSelf: 'center' }}
              >
                前往启用
              </ActionButton>
            }
          >
            记忆系统未启用。现有记忆仍可查看，但不会继续沉淀或注入。
          </Alert>
        )}

        <Menu
          anchorEl={actionsAnchor}
          open={!!actionsAnchor}
          onClose={() => setActionsAnchor(null)}
          anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
          transformOrigin={{ vertical: 'top', horizontal: 'left' }}
        >
          <MenuItem
            onClick={() => {
              setActionsAnchor(null)
              setConfirmAction('prune')
            }}
          >
            <ListItemText
              primary="清理低价值记忆"
              secondary="删除低价值段落、关系和部分孤立实体，用于瘦身和去噪。"
            />
          </MenuItem>
          <MenuItem
            onClick={() => {
              setActionsAnchor(null)
              setConfirmAction('rebuild')
            }}
            disabled={Boolean(rebuildStatus?.is_running)}
          >
            <ListItemText
              primary="清空并重新沉淀"
              secondary="清空结构化记忆、重置沉淀游标，并从历史聊天与委托日志重新构建记忆库。"
            />
          </MenuItem>
          <MenuItem
            onClick={() => {
              setActionsAnchor(null)
              cancelRebuildMutation.mutate()
            }}
            disabled={!rebuildStatus?.is_running || cancelRebuildMutation.isPending}
          >
            <ListItemText
              primary="取消当前重建"
              secondary="请求安全中止当前记忆重建任务，已完成的部分不会回滚。"
            />
          </MenuItem>
          <MenuItem
            onClick={() => {
              setActionsAnchor(null)
              setConfirmAction('reset')
            }}
          >
            <ListItemText
              primary="清空结构化记忆"
              secondary="删除当前工作区的全部段落、实体、关系、强化日志和向量索引。"
            />
          </MenuItem>
        </Menu>

        <Stack
          direction="row"
          spacing={1.2}
          alignItems="center"
          sx={{
            position: 'absolute',
            top: { xs: 58, sm: 16 },
            left: { xs: 10, sm: 'auto' },
            right: { xs: 10, sm: 16 },
            zIndex: 5,
            flexWrap: 'wrap',
            justifyContent: { xs: 'flex-start', sm: 'flex-end' },
            rowGap: 0.75,
            columnGap: 0.9,
            maxWidth: { xs: 'calc(100% - 32px)', md: 520 },
          }}
        >
          <Stack
            direction="row"
            spacing={0.5}
            alignItems="center"
            sx={{
              px: 1,
              py: 0.5,
              borderRadius: 999,
              bgcolor: alpha(theme.palette.background.paper, 0.72),
              backdropFilter: 'blur(12px)',
            }}
          >
            <Tooltip title="缩小">
              <span>
                <IconActionButton size="small" onClick={() => updateZoom(zoom - 0.1)}>
                  <ZoomOutIcon fontSize="small" />
                </IconActionButton>
              </span>
            </Tooltip>
            <Slider
              size="small"
              min={0.8}
              max={1.6}
              step={0.05}
              value={zoom}
              onChange={(_, value) => updateZoom(value as number)}
              sx={{ width: 104, mx: 0.25 }}
            />
            <Tooltip title="放大">
              <span>
                <IconActionButton size="small" onClick={() => updateZoom(zoom + 0.1)}>
                  <ZoomInIcon fontSize="small" />
                </IconActionButton>
              </span>
            </Tooltip>
          </Stack>
          <Select
            size="small"
            value={viewMode}
            onChange={event => setViewMode(event.target.value as AtlasViewMode)}
            sx={{
              minWidth: { xs: 104, sm: 92 },
              bgcolor: alpha(theme.palette.background.paper, 0.72),
              backdropFilter: 'blur(12px)',
            }}
          >
            <MenuItem value="episode">事件视图</MenuItem>
            <MenuItem value="atomic">原子视图</MenuItem>
          </Select>
          <Select
            size="small"
            value={informationDensity}
            onChange={event => setInformationDensity(event.target.value as InformationDensity)}
            sx={{
              minWidth: { xs: 112, sm: 92 },
              bgcolor: alpha(theme.palette.background.paper, 0.72),
              backdropFilter: 'blur(12px)',
            }}
          >
            <MenuItem value="low">低密度</MenuItem>
            <MenuItem value="medium">信息适中</MenuItem>
            <MenuItem value="high">高密度</MenuItem>
          </Select>
          <Stack
            direction="row"
            spacing={0.75}
            alignItems="center"
            sx={{
              px: 1,
              py: 0.4,
              borderRadius: 999,
              bgcolor: alpha(theme.palette.background.paper, 0.72),
              backdropFilter: 'blur(12px)',
            }}
          >
            <Switch checked={graphIncludeInactive} size="small" onChange={(_, checked) => setGraphIncludeInactive(checked)} />
            <Typography variant="caption" color="text.secondary">
              显示失活
            </Typography>
          </Stack>
        </Stack>

        <AnimatePresence>
          {browserOpen && (
            <motion.div
              initial={{ x: -28, opacity: 0 }}
              animate={{ x: 0, opacity: 1 }}
              exit={{ x: -28, opacity: 0 }}
              transition={{ duration: 0.22, ease: 'easeOut' }}
              style={{
                position: 'absolute',
                left: isMobile ? 10 : 16,
                top: isMobile ? mobileOverlayTop : 74,
                bottom: isMobile ? 10 : 16,
                width: isMobile ? 'calc(100% - 20px)' : 320,
                zIndex: 6,
              }}
            >
              <Card sx={{ ...CARD_VARIANTS.glassmorphism.styles, height: '100%' }}>
                <CardContent sx={{ p: 1.25, display: 'flex', flexDirection: 'column', gap: 1.1, height: '100%', '&:last-child': { pb: 1.25 } }}>
                  <Stack direction="row" justifyContent="space-between" alignItems="center">
                    <Typography variant="subtitle1" sx={{ fontWeight: 800 }}>
                      记忆图谱
                    </Typography>
                    <IconActionButton size="small" onClick={() => setBrowserOpen(false)} title={t('skills.drawer.close')}>
                      <CollapseIcon fontSize="small" />
                    </IconActionButton>
                  </Stack>
                  <TextField
                    size="small"
                    value={searchText}
                    onChange={event => setSearchText(event.target.value)}
                    placeholder="搜索标题、类型、认知"
                    InputProps={{
                      startAdornment: <SearchIcon sx={{ fontSize: 18, color: 'text.secondary', mr: 0.75 }} />,
                    }}
                  />
                  <Stack direction="row" spacing={1} flexWrap="wrap">
                    <Select size="small" value={memoryType} onChange={event => setMemoryType(event.target.value as MemoryTypeFilter)}>
                      <MenuItem value="all">全部</MenuItem>
                      <MenuItem value="paragraph">段落</MenuItem>
                      <MenuItem value="episode">事件</MenuItem>
                      <MenuItem value="entity">实体</MenuItem>
                      <MenuItem value="relation">关系</MenuItem>
                    </Select>
                    <Select size="small" value={memoryStatus} onChange={event => setMemoryStatus(event.target.value as MemoryStatusFilter)}>
                      <MenuItem value="all">全部状态</MenuItem>
                      <MenuItem value="active">活跃</MenuItem>
                      <MenuItem value="inactive">失活</MenuItem>
                    </Select>
                    <Select size="small" value={memoryCognitiveType} onChange={event => setMemoryCognitiveType(event.target.value as MemoryCognitiveTypeFilter)}>
                      <MenuItem value="all">全部认知</MenuItem>
                      <MenuItem value="episodic">情景</MenuItem>
                      <MenuItem value="semantic">语义</MenuItem>
                    </Select>
                    <Select size="small" value={sortBy} onChange={event => setSortBy(event.target.value as MemorySortBy)}>
                      <MenuItem value="event_time">事件时间</MenuItem>
                      <MenuItem value="update_time">更新时间</MenuItem>
                      <MenuItem value="create_time">创建时间</MenuItem>
                      <MenuItem value="effective_weight">有效权重</MenuItem>
                    </Select>
                    <Select size="small" value={sortOrder} onChange={event => setSortOrder(event.target.value as MemorySortOrder)}>
                      <MenuItem value="desc">倒序</MenuItem>
                      <MenuItem value="asc">正序</MenuItem>
                    </Select>
                  </Stack>
                  <Stack direction="row" spacing={1} flexWrap="wrap">
                    <TextField
                      size="small"
                      type="datetime-local"
                      label="起始时间"
                      value={timeFrom}
                      onChange={event => setTimeFrom(event.target.value)}
                      InputLabelProps={{ shrink: true }}
                    />
                    <TextField
                      size="small"
                      type="datetime-local"
                      label="结束时间"
                      value={timeTo}
                      onChange={event => setTimeTo(event.target.value)}
                      InputLabelProps={{ shrink: true }}
                    />
                  </Stack>
                  <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', pr: 0.5, ...SCROLLBAR_VARIANTS.thin.styles }}>
                    {memoryListLoading ? (
                      <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
                        <CircularProgress size={24} />
                      </Box>
                    ) : visibleListItems.length ? (
                      <Stack spacing={0.9}>
                        {visibleListItems.map(item => {
                          const selected = selection?.id === item.id && selection?.memory_type === item.memory_type
                          return (
                            <Box
                              key={`${item.memory_type}-${item.id}`}
                              onClick={() =>
                                setSelection({
                                  memory_type: item.memory_type,
                                  id: item.id,
                                  title: item.title,
                                  subtitle: item.subtitle,
                                  cognitive_type: item.cognitive_type,
                                  status: item.status,
                                })
                              }
                              sx={{
                                p: 1,
                                borderRadius: 2,
                                cursor: 'pointer',
                                border: '1px solid',
                                borderColor: selected ? 'primary.main' : 'divider',
                                bgcolor: selected ? alpha(theme.palette.primary.main, 0.08) : alpha(theme.palette.background.paper, 0.76),
                              }}
                            >
                              <Stack direction="row" spacing={0.6} flexWrap="wrap" sx={{ mb: 0.5 }}>
                                <Chip size="small" label={formatEnumValue('memory_type', item.memory_type)} sx={CHIP_VARIANTS.base(true)} />
                                {item.cognitive_type && <Chip size="small" label={formatEnumValue('cognitive_type', item.cognitive_type)} sx={CHIP_VARIANTS.base(true)} />}
                                <Chip size="small" label={formatEnumValue('status', item.status)} sx={CHIP_VARIANTS.base(true)} />
                              </Stack>
                              <Typography variant="body2" sx={{ fontWeight: 700 }}>
                                {truncateLabel(item.title, 34)}
                              </Typography>
                              <Typography variant="caption" color="text.secondary">
                                {truncateLabel(item.subtitle || '-', 34)}
                              </Typography>
                            </Box>
                          )
                        })}
                      </Stack>
                    ) : (
                      <Alert severity="info">没有匹配结果。</Alert>
                    )}
                  </Box>
                </CardContent>
              </Card>
            </motion.div>
          )}
        </AnimatePresence>

        <AtlasGraph
          viewport={viewport}
          layout={atlas}
          edges={memoryGraph?.edges ?? []}
          config={MEMORY_ATLAS_CONFIG}
          viewMode={viewMode}
          informationDensity={informationDensity}
          selectedNodeId={selectedNodeId}
          selectedIsCore={selection?.memory_type === 'episode'}
          onSelect={node =>
            setSelection(
              node
                ? {
                    memory_type: node.memory_type,
                    id: node.ref_id,
                    title: node.label,
                    subtitle: node.subtitle,
                    cognitive_type: node.cognitive_type,
                    status: node.status,
                  }
                : null,
            )
          }
          loading={memoryGraphLoading}
          empty={<Alert severity="info">当前还没有足够的图谱数据可供展示。</Alert>}
          top={{ xs: `${mobileOverlayTop}px`, sm: 0 }}
          fallbackSubtitle={node => (node.memory_type === 'episode' ? '叙事事件' : formatEnumValue('memory_type', node.memory_type))}
        />

        <Card
          sx={{
            ...CARD_VARIANTS.glassmorphism.styles,
            position: 'absolute',
            left: { xs: 10, sm: 16 },
            top: { xs: `${mobileOverlayTop}px`, sm: 74 },
            zIndex: 4,
            width: browserOpen ? 0 : 'fit-content',
            minWidth: browserOpen ? 0 : 0,
            maxWidth: browserOpen ? 0 : { xs: 'calc(100% - 32px)', sm: 420 },
            opacity: browserOpen ? 0 : 1,
            pointerEvents: browserOpen ? 'none' : 'auto',
            transition: 'opacity 0.18s ease, width 0.18s ease',
          }}
        >
          <CardContent sx={{ p: 1.2, '&:last-child': { pb: 1.2 } }}>
            <Stack direction="row" spacing={0.6} alignItems="center">
              <Typography variant="subtitle2" sx={{ fontWeight: 800 }}>
                当前焦点
              </Typography>
              <Tooltip title="触控板双指或鼠标滚轮可平移视图，按住 Ctrl 或 Command 再滚动可缩放。信息密度会同时影响标签、节点数量和关联细节。" placement="bottom-start">
                <InfoIcon sx={{ fontSize: 16, color: 'text.secondary', cursor: 'help' }} />
              </Tooltip>
            </Stack>
            {selectedAtlasNode ? (
              <Stack spacing={0.6} sx={{ mt: 0.8 }}>
                <Stack direction="row" spacing={0.75} flexWrap="wrap">
                  <StatChip label={formatEnumValue('memory_type', selectedAtlasNode.memory_type)} color={selectedAtlasNode.glow} />
                  {selectedAtlasNode.cognitive_type && <StatChip label={formatEnumValue('cognitive_type', selectedAtlasNode.cognitive_type)} color={selectedAtlasNode.glow} />}
                  {selection?.status && <StatChip label={formatEnumValue('status', selection.status)} color={theme.palette.text.secondary} />}
                </Stack>
                <Typography variant="body2" sx={{ fontWeight: 800 }}>
                  {truncateLabel(selectedAtlasNode.label, 42)}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {truncateLabel(selectedAtlasNode.subtitle || '-', 90)}
                </Typography>
              </Stack>
            ) : null}
          </CardContent>
        </Card>

        <Stack
          direction="row"
          spacing={0.8}
          sx={{
            position: 'absolute',
            left: { xs: 10, sm: 16 },
            right: { xs: 10, sm: 'auto' },
            bottom: { xs: 10, sm: 16 },
            zIndex: 4,
            flexWrap: 'wrap',
          }}
        >
          <StatChip label={`段落 ${memoryData?.stats.paragraph_count ?? 0}`} color={theme.palette.warning.main} />
          <StatChip label={`事件 ${episodeCount}`} color={ATLAS_COLORS.core} />
          <StatChip label={`关系 ${memoryData?.stats.relation_count ?? 0}`} color={ATLAS_COLORS.relation} />
          <StatChip label={`实体 ${memoryData?.stats.entity_count ?? 0}`} color={ATLAS_COLORS.entity} />
        </Stack>

        <AnimatePresence>
          {selection && selection.id > 0 && (
            <motion.div
              initial={{ x: 28, opacity: 0 }}
              animate={{ x: 0, opacity: 1 }}
              exit={{ x: 28, opacity: 0 }}
              transition={{ duration: 0.22, ease: 'easeOut' }}
              style={{
                position: 'absolute',
                top: isMobile ? mobileOverlayTop : 74,
                right: isMobile ? 10 : 16,
                bottom: isMobile ? 10 : 16,
                width: isMobile ? 'calc(100% - 20px)' : 360,
                zIndex: 7,
              }}
            >
              <Card sx={{ ...CARD_VARIANTS.glassmorphism.styles, height: '100%' }}>
                <CardContent sx={{ p: 1.25, display: 'flex', flexDirection: 'column', gap: 1.1, height: '100%', '&:last-child': { pb: 1.25 } }}>
                  <Stack direction="row" justifyContent="space-between" alignItems="center">
                    <Typography variant="subtitle1" sx={{ fontWeight: 800 }}>
                      详情与干预
                    </Typography>
                    <IconActionButton size="small" onClick={() => setSelection(null)} title={t('actions.close', { ns: 'common', defaultValue: '关闭' })}>
                      <CollapseIcon fontSize="small" sx={{ transform: 'rotate(180deg)' }} />
                    </IconActionButton>
                  </Stack>
                  {memoryDetailLoading ? (
                    <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
                      <CircularProgress size={24} />
                    </Box>
                  ) : (
                    <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', pr: 0.5, ...SCROLLBAR_VARIANTS.thin.styles }}>
                      <Stack spacing={1.2}>
                        <Stack direction="row" spacing={0.75} flexWrap="wrap">
                          <StatChip
                            label={formatEnumValue('memory_type', selection.memory_type)}
                            color={getMemoryTypeAccent(theme, selection.memory_type, selection.cognitive_type)}
                          />
                          {selection.cognitive_type && <StatChip label={formatEnumValue('cognitive_type', selection.cognitive_type)} color={theme.palette.success.main} />}
                          {selection.status && <StatChip label={formatEnumValue('status', selection.status)} color={theme.palette.text.secondary} />}
                          {typeof memoryDetail?.data?.is_protected === 'boolean' && (
                            <StatChip label={(memoryDetail.data.is_protected as boolean) ? '已保护' : '未保护'} color={theme.palette.warning.main} />
                          )}
                          {typeof memoryDetail?.data?.is_frozen === 'boolean' && (
                            <StatChip label={(memoryDetail.data.is_frozen as boolean) ? '已冻结' : '未冻结'} color={theme.palette.info.main} />
                          )}
                        </Stack>
                        <Divider />
                        <Box
                          sx={{
                            display: 'grid',
                            gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
                            gap: 1,
                          }}
                        >
                        {detailFields.map(field => (
                          <Box
                            key={field.key}
                            sx={{
                              minWidth: 0,
                              gridColumn: field.wide ? '1 / -1' : 'auto',
                              px: 1,
                              py: 0.85,
                              borderRadius: 1.5,
                              bgcolor: alpha(theme.palette.background.default, 0.5),
                            }}
                          >
                            <Typography variant="caption" color="text.secondary">
                              {field.label}
                            </Typography>
                            <Typography variant="body2" sx={{ whiteSpace: field.wide ? 'pre-wrap' : 'normal', wordBreak: 'break-word' }}>
                              {formatEnumValue(field.key, memoryDetail?.data?.[field.key])}
                            </Typography>
                          </Box>
                        ))}
                        </Box>

                        {selection.memory_type === 'episode' && (
                          <>
                            <Divider />
                            <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                              事件结构
                            </Typography>
                            <Stack spacing={1}>
                              {episodeParticipants.length > 0 && (
                                <Box>
                                  <Typography variant="caption" color="text.secondary">
                                    参与者
                                  </Typography>
                                  <Stack direction="row" spacing={0.75} flexWrap="wrap" sx={{ mt: 0.6 }}>
                                    {episodeParticipants.map(participant => (
                                      <Chip
                                        key={`episode-participant-${String(participant.id)}`}
                                        size="small"
                                        label={`${String(participant.name ?? participant.id)} · ${formatEnumValue('entity_type', participant.entity_type)}`}
                                        sx={CHIP_VARIANTS.base(true)}
                                      />
                                    ))}
                                  </Stack>
                                </Box>
                              )}
                              {episodeParagraphs.length > 0 && (
                                <Stack spacing={0.8}>
                                  <Typography variant="caption" color="text.secondary">
                                    事件时间轴
                                  </Typography>
                                  {episodeParagraphs.map((paragraph, index) => (
                                    <Box
                                      key={`episode-paragraph-${String(paragraph.id)}`}
                                      sx={{
                                        display: 'grid',
                                        gridTemplateColumns: '24px 1fr',
                                        gap: 1,
                                        alignItems: 'start',
                                      }}
                                    >
                                      <Stack alignItems="center" spacing={0.35} sx={{ pt: 0.2 }}>
                                        <Box
                                          sx={{
                                            width: 10,
                                            height: 10,
                                            borderRadius: '50%',
                                            bgcolor: alpha(theme.palette.info.main, 0.9),
                                            boxShadow: `0 0 0 3px ${alpha(theme.palette.info.main, 0.16)}`,
                                          }}
                                        />
                                        {index < episodeParagraphs.length - 1 && (
                                          <Box sx={{ width: 2, minHeight: 28, borderRadius: 999, bgcolor: alpha(theme.palette.info.main, 0.22) }} />
                                        )}
                                      </Stack>
                                      <Box
                                        sx={{
                                          px: 1,
                                          py: 0.85,
                                          borderRadius: 1.5,
                                          bgcolor: alpha(theme.palette.background.default, 0.5),
                                        }}
                                      >
                                        <Stack direction="row" spacing={0.75} flexWrap="wrap" sx={{ mb: 0.5 }}>
                                          {paragraph.episode_phase && (
                                            <Chip size="small" label={formatEnumValue('episode_phase', paragraph.episode_phase)} sx={CHIP_VARIANTS.base(true)} />
                                          )}
                                          {paragraph.knowledge_type && (
                                            <Chip size="small" label={formatEnumValue('knowledge_type', paragraph.knowledge_type)} sx={CHIP_VARIANTS.base(true)} />
                                          )}
                                          {paragraph.event_time && (
                                            <Chip size="small" label={formatDateTime(String(paragraph.event_time))} sx={CHIP_VARIANTS.base(true)} />
                                          )}
                                        </Stack>
                                        <Typography variant="body2" sx={{ fontWeight: 700 }}>
                                          {String(paragraph.summary ?? paragraph.id)}
                                        </Typography>
                                      </Box>
                                    </Box>
                                  ))}
                                </Stack>
                              )}
                            </Stack>
                          </>
                        )}

                        {selection.memory_type === 'paragraph' && (
                          <>
                            <Divider />
                            <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                              编辑
                            </Typography>
                            <Stack spacing={1}>
                              <TextField size="small" label="摘要" value={editSummary} onChange={event => setEditSummary(event.target.value)} />
                              <TextField size="small" label="内容" multiline minRows={4} value={editContent} onChange={event => setEditContent(event.target.value)} />
                            </Stack>
                            <Divider />
                            <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                              追溯
                            </Typography>
                            {memoryTraceLoading ? (
                              <CircularProgress size={20} />
                            ) : (
                              <Stack spacing={1}>
                                {memoryTrace?.messages.length ? (
                                  memoryTrace.messages.map(message => (
                                    <Card key={message.id} variant="outlined">
                                      <CardContent sx={{ p: 1.1, '&:last-child': { pb: 1.1 } }}>
                                        <Stack direction="row" justifyContent="space-between" spacing={1}>
                                          <Typography variant="body2" sx={{ fontWeight: 700 }}>
                                            {message.sender_nickname}
                                          </Typography>
                                          <Typography variant="caption" color="text.secondary">
                                            {formatDateTime(message.send_timestamp)}
                                          </Typography>
                                        </Stack>
                                        <Typography variant="caption" color="text.secondary">
                                          消息 ID: {message.message_id}
                                        </Typography>
                                        <Typography variant="body2" sx={{ mt: 0.75, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                                          {message.content_text}
                                        </Typography>
                                      </CardContent>
                                    </Card>
                                  ))
                                ) : (
                                  <Alert severity="info">当前没有可追溯的原始消息。</Alert>
                                )}
                              </Stack>
                            )}
                          </>
                        )}

                        <Divider />
                        <Stack direction="row" spacing={1} flexWrap="wrap">
                          <ActionButton tone="primary" size="small" startIcon={<ReinforceIcon />} disabled={!selectedActions?.canReinforce || reinforceMutation.isPending} onClick={() => selection && reinforceMutation.mutate({ type: selection.memory_type, id: selection.id })}>
                            强化
                          </ActionButton>
                          <ActionButton tone="secondary" size="small" startIcon={<DemoteIcon />} disabled={!selectedActions?.canReinforce || demoteMutation.isPending} onClick={() => selection && demoteMutation.mutate({ type: selection.memory_type, id: selection.id })}>
                            降权
                          </ActionButton>
                          <ActionButton tone="secondary" size="small" startIcon={<FreezeIcon />} disabled={!selectedActions?.canFreeze || freezeMutation.isPending} onClick={() => selection && freezeMutation.mutate({ type: selection.memory_type, id: selection.id })}>
                            冻结
                          </ActionButton>
                          <ActionButton tone="secondary" size="small" startIcon={<UnfreezeIcon />} disabled={!selectedActions?.canFreeze || unfreezeMutation.isPending} onClick={() => selection && unfreezeMutation.mutate({ type: selection.memory_type, id: selection.id })}>
                            解冻
                          </ActionButton>
                          <ActionButton
                            tone="secondary"
                            size="small"
                            startIcon={<ProtectIcon />}
                            disabled={!selectedActions?.canProtect || protectMutation.isPending}
                            onClick={() =>
                              selection &&
                              protectMutation.mutate({
                                type: selection.memory_type,
                                id: selection.id,
                                protectedFlag: !(memoryDetail?.data?.is_protected as boolean | undefined),
                              })
                            }
                          >
                            {(memoryDetail?.data?.is_protected as boolean | undefined) ? '取消保护' : '保护'}
                          </ActionButton>
                          <ActionButton tone="secondary" size="small" startIcon={<EditIcon />} disabled={!selectedActions?.canEdit || editMutation.isPending} onClick={() => selection && editMutation.mutate({ type: selection.memory_type, id: selection.id, summary: editSummary, content: editContent })}>
                            保存编辑
                          </ActionButton>
                          <ActionButton tone="danger" size="small" startIcon={<DeleteIcon />} disabled={deleteMutation.isPending} onClick={() => selection && deleteMutation.mutate({ type: selection.memory_type, id: selection.id })}>
                            删除
                          </ActionButton>
                        </Stack>
                      </Stack>
                    </Box>
                  )}
                </CardContent>
              </Card>
            </motion.div>
          )}
        </AnimatePresence>

        {memoryDataLoading && (
          <Box sx={{ position: 'absolute', left: '50%', top: 20, transform: 'translateX(-50%)', zIndex: 10 }}>
            <CircularProgress size={18} />
          </Box>
        )}

        <Dialog open={!!confirmAction} onClose={() => setConfirmAction(null)} maxWidth="xs" fullWidth>
          <DialogTitle>
            {confirmAction === 'reset'
              ? '确认清空结构化记忆'
              : confirmAction === 'rebuild'
                ? '确认清空并重新沉淀'
                : '确认清理低价值记忆'}
          </DialogTitle>
          <DialogContent>
            <Stack spacing={1.25}>
              {confirmAction === 'reset' ? (
                <>
                  <Typography variant="body2">
                    这会删除当前工作区的全部结构化记忆数据，包括段落、实体、关系、强化日志，以及向量索引。
                  </Typography>
                  <Typography variant="body2" color="warning.main">
                    风险高。它不是重置视图，而是接近清库操作。原始聊天消息不会被删，但现有结构化记忆会被整体清空。
                  </Typography>
                </>
              ) : confirmAction === 'rebuild' ? (
                <>
                  <Typography variant="body2">
                    这会先清空当前工作区的结构化记忆，再重置所有已绑定频道的沉淀游标，并从历史聊天消息与 CC 通讯日志重新构建记忆。
                  </Typography>
                  <Typography variant="body2" color="warning.main">
                    风险高且耗时较长。原始聊天消息和委托日志不会被删，但整个记忆库会被完全重算，结果可能与当前版本不同。
                  </Typography>
                </>
              ) : (
                <>
                  <Typography variant="body2">
                    这会按阈值删除低价值段落、关系和部分孤立实体，用于减少噪声和控制记忆库膨胀。
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    风险中等。它不会清空全部记忆，但会移除一部分弱信号内容。
                  </Typography>
                </>
              )}
            </Stack>
          </DialogContent>
        <DialogActions>
            <ActionButton tone="secondary" onClick={() => setConfirmAction(null)}>取消</ActionButton>
            <ActionButton
              tone={confirmAction === 'reset' ? 'danger' : 'secondary'}
              onClick={() => {
                const action = confirmAction
                setConfirmAction(null)
                if (action === 'reset') {
                  resetMutation.mutate()
                } else if (action === 'rebuild') {
                  rebuildMutation.mutate()
                } else if (action === 'prune') {
                  pruneMutation.mutate()
                }
              }}
              disabled={
                resetMutation.isPending ||
                pruneMutation.isPending ||
                rebuildMutation.isPending ||
                (confirmAction === 'rebuild' && Boolean(rebuildStatus?.is_running))
              }
            >
              确认执行
            </ActionButton>
          </DialogActions>
        </Dialog>
      </Box>
    </Box>
  )
}
