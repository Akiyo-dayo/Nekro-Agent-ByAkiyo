/**
 * 好感度管理页
 *
 * 呈现顺序刻意与数据结构相反：先看到的是 Bot 自己写的那句话（`state_text`），
 * 分数只作为一个小小的副标题跟在关系阶段后面。好感度是给人玩的，
 * 做成一张按分数排序的审计表就没意思了——数值该在的地方是排序键和调整入口，不是主角。
 *
 * 作用域跟随顶栏的实例切换器：好感度按 (域, 人, 人设) 隔离，
 * 全局视图看到全部实例的数据，实例视图只看该实例的。租户身份的越权访问由服务端按 404 处理。
 *
 * 一行是「某人在人设 X 下的那一份」：同一个人换过人设就有好几行。列表因此标出人设与
 * 「在用」（本域里有几个频道当前正用着这个人设），同一个人的多行折叠在一起，
 * 每个改动对话框都写明改的是哪个人设那一份——否则重置很容易落在一行没人用的旧人设上。
 */

import { Fragment, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Card,
  Chip,
  CircularProgress,
  Divider,
  Link,
  Stack,
  SxProps,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Theme,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'
import AutoFixHighRoundedIcon from '@mui/icons-material/AutoFixHighRounded'
import CleaningServicesRoundedIcon from '@mui/icons-material/CleaningServicesRounded'
import DeleteOutlineRoundedIcon from '@mui/icons-material/DeleteOutlineRounded'
import EditNoteRoundedIcon from '@mui/icons-material/EditNoteRounded'
import FavoriteRoundedIcon from '@mui/icons-material/FavoriteRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import RestartAltRoundedIcon from '@mui/icons-material/RestartAltRounded'
import TimelineRoundedIcon from '@mui/icons-material/TimelineRounded'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import IconActionButton from '../../components/common/IconActionButton'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import NekroDialog from '../../components/common/NekroDialog'
import PaginationStyled from '../../components/common/PaginationStyled'
import QueryErrorState from '../../components/common/QueryErrorState'
import SearchField from '../../components/common/SearchField'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import PersonNotesSection from '../../components/worldbook/PersonNotesSection'
import { useDeepLinkInstance, withDeepLinkInstance } from '../../hooks/useDeepLinkInstance'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { useNotification } from '../../hooks/useNotification'
import { CHIP_VARIANTS, CARD_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  affectionApi,
  type AffectionItem,
  type AffectionMilestoneItem,
  type AffectionMilestoneStatus,
  type AffectionSortBy,
  type AffectionStageValue,
} from '../../services/api/affection'
import { isNotFoundError } from '../../services/api/axios'

const PAGE_SIZE = 20

const SORT_OPTIONS: AffectionSortBy[] = ['score_desc', 'score_asc', 'recent', 'interaction']

// 两段描述的长度上限，只用于输入框提示；真正的截断在服务端（`store.MAX_*_LENGTH`）
const STATE_TEXT_MAX = 300
const EVENT_TEXT_MAX = 160
// 称呼的上限同样由服务端定（`store.MAX_CALL_NAME_LENGTH`），这里只做即时提示
const CALL_NAME_MAX = 24

/**
 * 里程碑状态标签
 *
 * `active` 不画标签：一屏里绝大多数都是正常的，给每一条都挂一个「正常」只是噪音。
 * 其余几种都是「这条已经不在检索里了」，隔离给警告色，其余给灰或红。
 */
function MilestoneStatusChip({ status }: { status: AffectionMilestoneStatus }) {
  const theme = useTheme()
  const { t } = useTranslation('affection')
  if (status === 'active') return null
  const color =
    status === 'quarantined'
      ? theme.palette.warning.main
      : status === 'deleted'
        ? theme.palette.error.main
        : theme.palette.text.disabled
  return (
    <Chip
      size="small"
      label={t(`detail.milestonesStatus.${status}`)}
      sx={CHIP_VARIANTS.getCustomColorChip(color, true)}
    />
  )
}

function formatDateTime(value: string | null, fallback: string): string {
  if (!value) return fallback
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return fallback
  return new Date(timestamp).toLocaleString()
}

/**
 * 把一页里同一个人的几行（不同人设）折到一起
 *
 * 「同一个人」= 同域 + 同一个人标识。组内在用的那一份排在最前面（运营最常要动的就是它），
 * 其余保持服务端给的顺序；组与组之间也保持服务端顺序，排序选项因此照常生效。
 * 分页在服务端，所以只折得到同一页里的行。
 */
function groupByPerson(items: AffectionItem[]): AffectionItem[][] {
  const groups = new Map<string, AffectionItem[]>()
  for (const item of items) {
    const key = `${item.domain_id}|${item.platform_userid}`
    const group = groups.get(key)
    if (group) group.push(item)
    else groups.set(key, [item])
  }
  return Array.from(groups.values()).map(group =>
    group
      .map((item, index) => ({ item, index }))
      .sort(
        (a, b) =>
          Number(b.item.in_use_channels > 0) - Number(a.item.in_use_channels > 0) ||
          a.index - b.index
      )
      .map(entry => entry.item)
  )
}

export default function AffectionPage() {
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { t } = useTranslation('affection')
  const { t: tCommon } = useTranslation('common')
  const { currentInstanceKey, isScopeReady } = useInstanceScope()
  // 跳进来的链接带着 `instance` 时先切到那一份所在的实例（规则见 hook 的注释）
  useDeepLinkInstance()

  // `?record=<编号>`：从记忆页「打开好感详情」跳过来时直接展开那一份
  const recordParam = searchParams.get('record')
  const recordFromUrl = useMemo(() => {
    if (!recordParam) return null
    const parsed = Number(recordParam)
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null
  }, [recordParam])

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [stageFilter, setStageFilter] = useState<AffectionStageValue | 'all'>('all')
  const [sortBy, setSortBy] = useState<AffectionSortBy>('score_desc')
  const [detailId, setDetailId] = useState<number | null>(null)
  const [resetTarget, setResetTarget] = useState<AffectionItem | null>(null)
  const [adjustTarget, setAdjustTarget] = useState<AffectionItem | null>(null)
  const [adjustScore, setAdjustScore] = useState('0')
  const [adjustReason, setAdjustReason] = useState('')
  const [clearTarget, setClearTarget] = useState<AffectionItem | null>(null)
  const [editTarget, setEditTarget] = useState<AffectionItem | null>(null)
  const [editStateText, setEditStateText] = useState('')
  const [editEventText, setEditEventText] = useState('')
  const [editCallName, setEditCallName] = useState('')
  const [deleteReasonEventId, setDeleteReasonEventId] = useState<number | null>(null)
  const [deleteMilestoneTarget, setDeleteMilestoneTarget] =
    useState<AffectionMilestoneItem | null>(null)
  // 展开了「其他人设」的人，键与 `groupByPerson` 的分组键相同
  const [expandedPeople, setExpandedPeople] = useState<Set<string>>(() => new Set())

  // 地址栏里的 `record` 就是「这一份正开着」：进页面、刷新、前进后退都靠它恢复
  useEffect(() => {
    if (recordFromUrl === null) return
    setDetailId(recordFromUrl)
  }, [recordFromUrl])

  /** 关掉详情时把 `record` 一起去掉，其它参数（比如筛选）原样留着 */
  const closeDetail = () => {
    setDetailId(null)
    if (recordFromUrl === null) return
    const next = new URLSearchParams(searchParams)
    next.delete('record')
    setSearchParams(next, { replace: true })
  }

  /**
   * 从列表打开某一份的详情
   *
   * 地址里带着 `record` 时（从记忆页跳进来的）要把它换成眼前这一份：留着旧编号，
   * 刷新回来看到的还是跳进来那一份，而不是手上这一份。没有这个参数就不写 ——
   * 列表里点开详情不该无端改动地址栏。
   */
  const openDetail = (id: number) => {
    setDetailId(id)
    if (recordFromUrl === null || recordFromUrl === id) return
    const next = new URLSearchParams(searchParams)
    next.set('record', String(id))
    setSearchParams(next, { replace: true })
  }

  /**
   * 跳去记忆页看这条里程碑
   *
   * 带上这份好感所属的实例：作用域是顶栏切换器的本地状态，链接被复制到别处、
   * 或当前停在别的实例视图里时，不带就会显示「看不到」（见 `useDeepLinkInstance`）。
   * 实例组的好感行没有单一实例，不带，沿用当前作用域。
   */
  const openMilestoneInMemory = (milestone: AffectionMilestoneItem, instanceKey: string) => {
    navigate(withDeepLinkInstance(`/memory/items?item=${milestone.paragraph_id}`, instanceKey))
  }

  // 作用域是这些接口的必填参数（租户身份下服务端直接 400），因此必须等它确定后再发请求
  const scopeParam = currentInstanceKey ?? undefined

  /**
   * 关系阶段的配色
   *
   * 阶段的语义由服务端的阈值表决定，前端只负责把"越亲近越暖"这件事画出来。
   */
  const stageColor = (stage: AffectionStageValue): string => {
    if (stage === 'trusted' || stage === 'close') return theme.palette.error.main
    if (stage === 'friendly' || stage === 'acquainted') return theme.palette.success.main
    if (stage === 'wary') return theme.palette.warning.main
    if (stage === 'hostile') return theme.palette.text.disabled
    return theme.palette.info.main
  }

  const listQuery = useQuery({
    queryKey: ['affection', 'list', scopeParam, page, search, stageFilter, sortBy],
    queryFn: () =>
      affectionApi.getList({
        page,
        page_size: PAGE_SIZE,
        search: search.trim() || undefined,
        stage: stageFilter === 'all' ? undefined : stageFilter,
        sort_by: sortBy,
        instance_key: scopeParam,
      }),
    enabled: isScopeReady,
    retry: false,
  })

  const summaryQuery = useQuery({
    queryKey: ['affection', 'summary', scopeParam],
    queryFn: () => affectionApi.getSummary(scopeParam),
    enabled: isScopeReady,
    retry: false,
  })

  const detailQuery = useQuery({
    queryKey: ['affection', 'detail', scopeParam, detailId],
    queryFn: () => affectionApi.getDetail(detailId as number, scopeParam),
    enabled: isScopeReady && detailId !== null,
    retry: false,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['affection'] })
  }

  const adjustMutation = useMutation({
    mutationFn: (payload: { id: number; score: number; reason: string }) =>
      affectionApi.adjust(payload.id, { score: payload.score, reason: payload.reason }, scopeParam),
    onSuccess: () => {
      notification.success(tCommon('messages.updateSuccess'))
      setAdjustTarget(null)
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const resetMutation = useMutation({
    mutationFn: (id: number) => affectionApi.reset(id, scopeParam),
    onSuccess: () => {
      notification.success(t('messages.resetDone'))
      setResetTarget(null)
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const clearMutation = useMutation({
    mutationFn: (id: number) => affectionApi.clearNarrative(id, scopeParam),
    onSuccess: () => {
      notification.success(t('messages.clearDone'))
      setClearTarget(null)
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const editMutation = useMutation({
    mutationFn: (payload: {
      id: number
      state_text: string
      last_event_text: string
      call_name?: string
    }) =>
      affectionApi.editNarrative(
        payload.id,
        {
          state_text: payload.state_text,
          last_event_text: payload.last_event_text,
          call_name: payload.call_name,
        },
        scopeParam
      ),
    onSuccess: () => {
      notification.success(t('messages.editDone'))
      setEditTarget(null)
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const deleteMilestoneMutation = useMutation({
    mutationFn: (payload: { recordId: number; paragraphId: number }) =>
      affectionApi.deleteMilestone(payload.recordId, payload.paragraphId, scopeParam),
    onSuccess: () => {
      notification.success(t('messages.milestoneDeleted'))
      setDeleteMilestoneTarget(null)
      // 详情里的里程碑列表跟着变，列表页的「互动」不受影响但一并刷新更省心
      void queryClient.invalidateQueries({ queryKey: ['affection', 'detail'] })
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const deleteReasonMutation = useMutation({
    mutationFn: (payload: { recordId: number; eventId: number }) =>
      affectionApi.deleteEventReason(payload.recordId, payload.eventId, scopeParam),
    onSuccess: () => {
      notification.success(t('messages.reasonDeleted'))
      setDeleteReasonEventId(null)
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  /** 「某人在人设 X 下的那一份」——每个改动对话框都要写明改的是哪一份 */
  const shareOf = (item: AffectionItem | null): string =>
    item
      ? t('scope.thisShare', {
          name: item.nickname || item.platform_userid,
          preset: item.preset_name,
        })
      : ''

  /** 改的不是任何频道在用的那一份时，提醒一句 */
  const notInUseWarning = (item: AffectionItem | null) =>
    item && item.in_use_channels === 0 ? (
      <Alert severity="warning" variant="outlined">
        {t('scope.notInUseWarning')}
      </Alert>
    ) : null

  const togglePerson = (key: string) => {
    setExpandedPeople(previous => {
      const next = new Set(previous)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const openEdit = (item: AffectionItem) => {
    setEditTarget(item)
    setEditStateText(item.state_text)
    setEditEventText(item.last_event_text)
    setEditCallName(item.call_name ?? '')
  }

  // 按码点数，与后端 `len()` 一致：`.length` 数的是 UTF-16 单元，带表情时两边对不上
  const isCallNameTooLong = Array.from(editCallName).length > CALL_NAME_MAX

  const listItems = listQuery.data?.items
  const items = listItems ?? []
  const groups = useMemo(() => groupByPerson(listItems ?? []), [listItems])
  const total = listQuery.data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  // 阶段清单由服务端下发（阈值表是服务端的唯一事实来源），前端不复制一份枚举
  const availableStages = listQuery.data?.stages
  const stageOptions = useMemo(
    () => [
      { value: 'all', label: t('filters.allStages') },
      ...(availableStages ?? []).map(stage => ({ value: stage.value, label: stage.label })),
    ],
    [availableStages, t]
  )

  const sortOptions = useMemo(
    () => SORT_OPTIONS.map(value => ({ value, label: t(`sort.${value}`) })),
    [t]
  )

  const openAdjust = (item: AffectionItem) => {
    setAdjustTarget(item)
    setAdjustScore(String(item.score))
    setAdjustReason('')
  }

  const parsedAdjustScore = Number(adjustScore)
  const isAdjustScoreValid = Number.isInteger(parsedAdjustScore)

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 3 },
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        overflow: 'hidden',
        boxSizing: 'border-box',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
        <Box sx={{ minWidth: 0, flexShrink: 0 }}>
          <Typography variant="h6" fontWeight={700}>
            {t('title')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('subtitle')}
          </Typography>
        </Box>
        <SearchField
          value={search}
          onChange={value => {
            setSearch(value)
            setPage(1)
          }}
          onClear={() => {
            setSearch('')
            setPage(1)
          }}
          clearAriaLabel={t('filters.clearSearch')}
          placeholder={t('filters.searchPlaceholder')}
          sx={{ flex: 1, minWidth: 200, maxWidth: 320 }}
        />
        <FilterSelect
          label={t('filters.stage')}
          value={stageFilter}
          options={stageOptions}
          onChange={value => {
            setStageFilter(value as AffectionStageValue | 'all')
            setPage(1)
          }}
          fullWidth={false}
          sx={{ minWidth: 150 }}
        />
        <FilterSelect
          label={t('filters.sort')}
          value={sortBy}
          options={sortOptions}
          onChange={value => setSortBy(value as AffectionSortBy)}
          fullWidth={false}
          sx={{ minWidth: 160 }}
        />
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<RefreshRoundedIcon />}
          onClick={() => void listQuery.refetch()}
          sx={{ ml: 'auto' }}
        >
          {tCommon('actions.refresh')}
        </ActionButton>
      </Box>

      <InstanceScopeBanner description={t('scopeBanner')} />

      {summaryQuery.data && summaryQuery.data.total > 0 && (
        <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 1 }}>
          {summaryQuery.data.stage_counts
            .filter(stage => stage.count > 0)
            .map(stage => (
              <Chip
                key={stage.value}
                size="small"
                label={`${stage.label} · ${stage.count}`}
                sx={CHIP_VARIANTS.getCustomColorChip(stageColor(stage.value), true)}
              />
            ))}
        </Stack>
      )}

      {listQuery.isError && (
        <Alert
          severity="error"
          variant="outlined"
          action={
            <ActionButton tone="ghost" size="small" onClick={() => void listQuery.refetch()}>
              {tCommon('actions.refresh')}
            </ActionButton>
          }
        >
          {t('messages.loadFailed')}
        </Alert>
      )}

      <Box sx={UNIFIED_TABLE_STYLES.tableContentContainer as SxProps<Theme>}>
        <Box sx={UNIFIED_TABLE_STYLES.tableViewport as SxProps<Theme>}>
          {/* isPending 而不是 isLoading：作用域未就绪时查询被 enabled 关着，
              isLoading 为 false 会让页面先闪一帧「暂无数据」 */}
          {listQuery.isPending ? (
            <Box
              sx={{
                minHeight: 240,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <CircularProgress />
            </Box>
          ) : items.length === 0 ? (
            <Box
              sx={{
                minHeight: 240,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 1,
              }}
            >
              <Typography variant="subtitle1">{t('empty.title')}</Typography>
              <Typography variant="body2" color="text.secondary">
                {t('empty.hint')}
              </Typography>
            </Box>
          ) : (
            <Table stickyHeader sx={UNIFIED_TABLE_STYLES.getTableBase(false, false)}>
              <TableHead>
                <TableRow>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('columns.person')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('columns.preset')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('columns.relation')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('columns.feeling')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="right">
                    {t('columns.interactions')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="center">
                    {t('columns.actions')}
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {groups.map(group => {
                  const [primary, ...others] = group
                  const groupKey = `${primary.domain_id}|${primary.platform_userid}`
                  const expanded = expandedPeople.has(groupKey)
                  return (
                    <Fragment key={groupKey}>
                      {[primary, ...(expanded ? others : [])].map((item, index) => {
                        const nested = index > 0
                        return (
                          <TableRow
                            key={item.id}
                            hover
                            sx={
                              [
                                UNIFIED_TABLE_STYLES.row,
                                nested ? { bgcolor: theme.palette.action.hover } : {},
                              ] as SxProps<Theme>
                            }
                          >
                            <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                              <Box sx={{ pl: nested ? 2 : 0 }}>
                                <Typography variant="body2" fontWeight={600} noWrap>
                                  {item.nickname || item.platform_userid}
                                </Typography>
                                <Typography variant="caption" color="text.secondary" noWrap>
                                  {item.platform_userid}
                                  {item.instance_key ? ` · ${item.instance_key}` : ''}
                                </Typography>
                                {/* 被合并掉的副号那一行是**休眠**数据：Bot 读的是主账号那一行，
                                    在这里调分数不会有任何效果。不标出来的话管理员会对着一个
                                    「改了也不动」的行反复调整。 */}
                                {item.merged_into && (
                                  <Typography
                                    variant="caption"
                                    color="warning.main"
                                    noWrap
                                    display="block"
                                  >
                                    {t('columns.mergedInto', { person: item.merged_into })}
                                  </Typography>
                                )}
                                {!nested && others.length > 0 && (
                                  <Link
                                    component="button"
                                    type="button"
                                    variant="caption"
                                    underline="hover"
                                    onClick={() => togglePerson(groupKey)}
                                    sx={{ display: 'block', mt: 0.25 }}
                                  >
                                    {expanded
                                      ? t('columns.hideOtherPresets')
                                      : t('columns.showOtherPresets', { count: others.length })}
                                  </Link>
                                )}
                              </Box>
                            </TableCell>
                            <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                              <Typography variant="body2" noWrap>
                                {item.preset_name}
                              </Typography>
                              {item.in_use_channels > 0 ? (
                                <Chip
                                  size="small"
                                  label={t('columns.inUse', { count: item.in_use_channels })}
                                  sx={CHIP_VARIANTS.getCustomColorChip(
                                    theme.palette.success.main,
                                    true
                                  )}
                                />
                              ) : (
                                <Typography variant="caption" color="text.secondary">
                                  {t('columns.notInUse')}
                                </Typography>
                              )}
                            </TableCell>
                            <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                              <Chip
                                size="small"
                                label={item.stage_label}
                                sx={CHIP_VARIANTS.getCustomColorChip(stageColor(item.stage), true)}
                              />
                              <Typography
                                variant="caption"
                                color="text.secondary"
                                sx={{ display: 'block', mt: 0.25 }}
                              >
                                {item.score}
                              </Typography>
                            </TableCell>
                            <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                              <Typography variant="body2">
                                {item.state_text || t('columns.feelingEmpty')}
                              </Typography>
                              {item.last_event_text && (
                                <Typography variant="caption" color="text.secondary">
                                  {item.last_event_text}
                                </Typography>
                              )}
                            </TableCell>
                            <TableCell
                              sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}
                              align="right"
                            >
                              {item.interaction_count}
                            </TableCell>
                            <TableCell
                              sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}
                              align="center"
                            >
                              <Box sx={{ display: 'flex', gap: 0.5, justifyContent: 'center' }}>
                                <SimpleTooltip title={t('actions.detail')}>
                                  <span>
                                    <IconActionButton
                                      size="small"
                                      tone="primary"
                                      onClick={() => openDetail(item.id)}
                                    >
                                      <TimelineRoundedIcon fontSize="small" />
                                    </IconActionButton>
                                  </span>
                                </SimpleTooltip>
                                <SimpleTooltip title={t('actions.adjust')}>
                                  <span>
                                    <IconActionButton
                                      size="small"
                                      tone="subtle"
                                      onClick={() => openAdjust(item)}
                                    >
                                      <AutoFixHighRoundedIcon fontSize="small" />
                                    </IconActionButton>
                                  </span>
                                </SimpleTooltip>
                                <SimpleTooltip title={t('actions.editNarrative')}>
                                  <span>
                                    <IconActionButton
                                      size="small"
                                      tone="subtle"
                                      onClick={() => openEdit(item)}
                                    >
                                      <EditNoteRoundedIcon fontSize="small" />
                                    </IconActionButton>
                                  </span>
                                </SimpleTooltip>
                                <SimpleTooltip title={t('actions.clearNarrative')}>
                                  <span>
                                    <IconActionButton
                                      size="small"
                                      tone="subtle"
                                      disabled={!item.state_text && !item.last_event_text}
                                      onClick={() => setClearTarget(item)}
                                    >
                                      <CleaningServicesRoundedIcon fontSize="small" />
                                    </IconActionButton>
                                  </span>
                                </SimpleTooltip>
                                <SimpleTooltip title={t('actions.reset')}>
                                  <span>
                                    <IconActionButton
                                      size="small"
                                      tone="danger"
                                      onClick={() => setResetTarget(item)}
                                    >
                                      <RestartAltRoundedIcon fontSize="small" />
                                    </IconActionButton>
                                  </span>
                                </SimpleTooltip>
                              </Box>
                            </TableCell>
                          </TableRow>
                        )
                      })}
                    </Fragment>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </Box>
      </Box>

      <PaginationStyled
        totalPages={totalPages}
        currentPage={page}
        onPageChange={(_event, value) => setPage(value)}
        loading={listQuery.isFetching}
        alwaysShow={false}
      />

      <NekroDialog
        open={detailId !== null}
        onClose={closeDetail}
        title={t('detail.title')}
        titleStartIcon={<FavoriteRoundedIcon fontSize="small" />}
        // 里程碑与备注两张卡要并排放，`sm`（600px）里每列只剩二百来像素，按钮会折成好几行
        maxWidth="md"
        actions={
          <ActionButton tone="ghost" onClick={closeDetail}>
            {tCommon('actions.close')}
          </ActionButton>
        }
      >
        {detailQuery.isError ? (
          isNotFoundError(detailQuery.error) ? (
            // 深链接（`?record=`）指向别人实例的那一份、或已被删掉时是 404：
            // 一直转圈会让人以为只是慢，这里直接说清「看不到」
            <Alert severity="warning" variant="outlined" sx={{ mt: 1 }}>
              {t('detail.loadFailed')}
            </Alert>
          ) : (
            // 别的错误（服务端出错、断网）不能说成「看不到」，给原因和重试
            <QueryErrorState
              error={detailQuery.error}
              onRetry={() => void detailQuery.refetch()}
              compact
            />
          )
        ) : detailQuery.isLoading || !detailQuery.data ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
            <CircularProgress size={24} />
          </Box>
        ) : (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
            <Box>
              <Typography variant="subtitle1" fontWeight={700}>
                {detailQuery.data.item.nickname || detailQuery.data.item.platform_userid}
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block">
                {detailQuery.data.item.platform_userid}
                {detailQuery.data.item.instance_key
                  ? ` · ${detailQuery.data.item.instance_key}`
                  : ''}
                {' · '}
                {detailQuery.data.item.stage_label} ({detailQuery.data.item.score})
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block">
                {t('detail.callNameLine', {
                  name: detailQuery.data.item.call_name || '—',
                })}
              </Typography>
            </Box>
            <Typography variant="body2">
              {detailQuery.data.item.state_text || t('columns.feelingEmpty')}
            </Typography>

            {/* 「这份好感属于：域 X / 人设 甲」
                没有这一段，「为什么他在这个群 80 分那个群 0 分」没人解释得清 ——
                两个群绑着不同人设时那就是**两份**好感，不是数据丢了。
                标签由后端算（`identity_admin.domain_label`），前端不复算一份措辞。 */}
            <Alert severity="info" variant="outlined" icon={false}>
              <Typography variant="caption" component="div">
                {t('detail.scopeLine', {
                  domain: detailQuery.data.scope.domain_label,
                  preset: detailQuery.data.scope.preset_name,
                })}
              </Typography>
              {!detailQuery.data.scope.preset_exists && (
                <Typography variant="caption" component="div" color="warning.main">
                  {t('detail.presetDeleted')}
                </Typography>
              )}
              {detailQuery.data.scope.alias_accounts.length > 0 && (
                <Typography variant="caption" component="div" color="text.secondary">
                  {t('detail.mergedAccounts', {
                    accounts: detailQuery.data.scope.alias_accounts.join('、'),
                  })}
                </Typography>
              )}
            </Alert>

            {/* 这两块说的是同一件事的两面，所以并排放，各自第一句先交代「它是什么」：
                里程碑是 Bot 自己写进长期记忆的，备注是管理员写进世界书的 ——
                不写明的话，两边看起来都只是「关于这个人的一段字」。 */}
            <Box
              sx={{
                display: 'grid',
                gridTemplateColumns: { xs: '1fr', md: '1fr 1fr' },
                gap: 1.5,
                alignItems: 'start',
              }}
            >
              <Card
                sx={{
                  ...(CARD_VARIANTS.default.styles as object),
                  p: 1.5,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 1,
                }}
              >
                <Typography variant="subtitle2" fontWeight={700}>
                  {t('detail.milestones')}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {t('detail.milestonesHint')}
                </Typography>
                {detailQuery.data.milestones.length === 0 ? (
                  <Typography variant="caption" color="text.secondary">
                    {t('detail.milestonesEmpty')}
                  </Typography>
                ) : (
                  <Stack spacing={1}>
                    {detailQuery.data.milestones.map(milestone => {
                      // 已经删掉的（含彻底删除）不再给删除按钮：那里没有可删的东西了
                      // 段落行已被彻底删除，或者已随实例划给了别的租户：没有正文可显示，
                      // 记忆页那边也打不开（划走的那段已经是别人的数据），更没有可删的东西
                      const isGone = milestone.status === 'hard_deleted' || milestone.status === 'moved'
                      const canDelete =
                        !isGone && milestone.status !== 'deleted' && milestone.status !== 'pruned'
                      return (
                        // 键用流水编号：历史数据里可能有两条流水指向同一段，段落编号会撞
                        <Box
                          key={milestone.event_id}
                          sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}
                        >
                          <Typography
                            variant="body2"
                            color={isGone ? 'text.secondary' : undefined}
                            sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}
                          >
                            {milestone.status === 'moved'
                              ? t('detail.milestonesMoved')
                              : isGone
                                ? t('detail.milestonesGone')
                                : milestone.content}
                          </Typography>
                          <Stack
                            direction="row"
                            spacing={0.75}
                            sx={{ alignItems: 'center', flexWrap: 'wrap' }}
                          >
                            <MilestoneStatusChip status={milestone.status} />
                            <Typography variant="caption" color="text.secondary">
                              {formatDateTime(milestone.event_time, '—')}
                            </Typography>
                            {!isGone && (
                              <ActionButton
                                tone="ghost"
                                size="small"
                                onClick={() =>
                                  openMilestoneInMemory(milestone, detailQuery.data?.item.instance_key ?? '')
                                }
                              >
                                {t('detail.milestonesViewInMemory')}
                              </ActionButton>
                            )}
                            {canDelete && (
                              <ActionButton
                                tone="ghost"
                                size="small"
                                onClick={() => setDeleteMilestoneTarget(milestone)}
                              >
                                {t('detail.milestonesDelete')}
                              </ActionButton>
                            )}
                          </Stack>
                        </Box>
                      )
                    })}
                  </Stack>
                )}
              </Card>

              <Card
                sx={{
                  ...(CARD_VARIANTS.default.styles as object),
                  p: 1.5,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 1,
                }}
              >
                <PersonNotesSection
                  personAccount={detailQuery.data.item.platform_userid}
                  displayName={
                    detailQuery.data.item.nickname ||
                    detailQuery.data.item.platform_userid
                  }
                  onCloseDetail={closeDetail}
                  description={t('detail.notesHint')}
                />
              </Card>
            </Box>

            <Divider />
            <Typography variant="subtitle2" fontWeight={700}>
              {t('detail.timeline')}
            </Typography>
            {detailQuery.data.events.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                {t('detail.timelineEmpty')}
              </Typography>
            ) : (
              <Stack spacing={1}>
                {detailQuery.data.events.map(event => (
                  <Box key={event.id} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
                    <Chip
                      size="small"
                      label={event.delta >= 0 ? `+${event.delta}` : String(event.delta)}
                      sx={CHIP_VARIANTS.getCustomColorChip(
                        event.delta >= 0 ? theme.palette.success.main : theme.palette.warning.main,
                        true
                      )}
                    />
                    <Box sx={{ minWidth: 0, flex: 1 }}>
                      {/* 删过理由的条目只给删除痕迹，不给理由（服务端也不再返回它） */}
                      {event.deleted ? (
                        <Typography variant="body2" color="text.secondary" fontStyle="italic">
                          {t('detail.reasonDeleted', {
                            operator: event.deleted_by || '—',
                            time: formatDateTime(event.deleted_at, '—'),
                          })}
                        </Typography>
                      ) : (
                        <Typography variant="body2">
                          {event.reason || t(`detail.source.${event.source}`)}
                        </Typography>
                      )}
                      <Typography variant="caption" color="text.secondary">
                        {formatDateTime(event.create_time, '—')}
                        {' · '}
                        {t(`detail.source.${event.source}`)}
                        {/* 时间线是按「人」取的并集，合并过的人身上会同时出现好几个号干的事。
                            只在来源账号与这一行的人不同时才标出来，否则每一行都会多一句废话。 */}
                        {event.source_platform_userid &&
                        event.source_platform_userid !== detailQuery.data.item.platform_userid
                          ? ` · ${t('detail.sourceAccount', {
                              account: event.source_platform_userid,
                            })}`
                          : ''}
                        {event.operator ? ` · ${event.operator}` : ''}
                      </Typography>
                    </Box>
                    {!event.deleted && event.reason && (
                      <SimpleTooltip title={t('actions.deleteReason')}>
                        <span>
                          <IconActionButton
                            size="small"
                            tone="danger"
                            onClick={() => setDeleteReasonEventId(event.id)}
                          >
                            <DeleteOutlineRoundedIcon fontSize="small" />
                          </IconActionButton>
                        </span>
                      </SimpleTooltip>
                    )}
                  </Box>
                ))}
              </Stack>
            )}
          </Box>
        )}
      </NekroDialog>

      <NekroDialog
        open={adjustTarget !== null}
        onClose={() => setAdjustTarget(null)}
        title={t('adjustDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setAdjustTarget(null)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="primary"
              disabled={!isAdjustScoreValid || adjustMutation.isPending}
              onClick={() => {
                if (!adjustTarget || !isAdjustScoreValid) return
                adjustMutation.mutate({
                  id: adjustTarget.id,
                  score: parsedAdjustScore,
                  reason: adjustReason.trim(),
                })
              }}
            >
              {tCommon('actions.save')}
            </ActionButton>
          </>
        }
      >
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            {t('adjustDialog.description', { share: shareOf(adjustTarget) })}
          </Typography>
          {notInUseWarning(adjustTarget)}
          <TextField
            size="small"
            fullWidth
            type="number"
            label={t('adjustDialog.score')}
            value={adjustScore}
            error={!isAdjustScoreValid}
            helperText={isAdjustScoreValid ? t('adjustDialog.scoreHelper') : t('adjustDialog.scoreInvalid')}
            onChange={event => setAdjustScore(event.target.value)}
          />
          <TextField
            size="small"
            fullWidth
            label={t('adjustDialog.reason')}
            helperText={t('adjustDialog.reasonHelper')}
            value={adjustReason}
            onChange={event => setAdjustReason(event.target.value)}
          />
        </Box>
      </NekroDialog>

      <NekroDialog
        open={resetTarget !== null}
        onClose={() => setResetTarget(null)}
        title={t('resetDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setResetTarget(null)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={resetMutation.isPending}
              onClick={() => {
                if (!resetTarget) return
                resetMutation.mutate(resetTarget.id)
              }}
            >
              {t('resetDialog.submit')}
            </ActionButton>
          </>
        }
      >
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          <Typography variant="body2">
            {t('resetDialog.description', { share: shareOf(resetTarget) })}
          </Typography>
          <Typography variant="body2" color="warning.main">
            {t('resetDialog.alsoMilestones')}
          </Typography>
          {notInUseWarning(resetTarget)}
        </Box>
      </NekroDialog>

      <NekroDialog
        open={clearTarget !== null}
        onClose={() => setClearTarget(null)}
        title={t('clearDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setClearTarget(null)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={clearMutation.isPending}
              onClick={() => {
                if (!clearTarget) return
                clearMutation.mutate(clearTarget.id)
              }}
            >
              {t('clearDialog.submit')}
            </ActionButton>
          </>
        }
      >
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          <Typography variant="body2">
            {t('clearDialog.description', { share: shareOf(clearTarget) })}
          </Typography>
          {notInUseWarning(clearTarget)}
        </Box>
      </NekroDialog>

      <NekroDialog
        open={editTarget !== null}
        onClose={() => setEditTarget(null)}
        title={t('editDialog.title')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setEditTarget(null)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="primary"
              disabled={editMutation.isPending || isCallNameTooLong}
              onClick={() => {
                if (!editTarget) return
                editMutation.mutate({
                  id: editTarget.id,
                  state_text: editStateText,
                  last_event_text: editEventText,
                  // 只在改过时才带：对话框开着的这段时间里模型可能刚用 `set_call_name`
                  // 改了称呼，总是带上打开时的旧值就会把它改回去。清空也算改过（空串照样提交）
                  call_name: editCallName !== (editTarget.call_name ?? '') ? editCallName : undefined,
                })
              }}
            >
              {t('editDialog.submit')}
            </ActionButton>
          </>
        }
      >
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            {t('editDialog.description', { share: shareOf(editTarget) })}
          </Typography>
          {notInUseWarning(editTarget)}
          <TextField
            size="small"
            fullWidth
            label={t('editDialog.callName')}
            value={editCallName}
            error={isCallNameTooLong}
            helperText={
              isCallNameTooLong
                ? t('editDialog.callNameTooLong', { max: CALL_NAME_MAX })
                : t('editDialog.callNameHelper', { max: CALL_NAME_MAX })
            }
            onChange={event => setEditCallName(event.target.value)}
          />
          <TextField
            size="small"
            fullWidth
            multiline
            minRows={3}
            label={t('editDialog.stateText')}
            value={editStateText}
            error={editStateText.length > STATE_TEXT_MAX}
            helperText={`${editStateText.length} / ${STATE_TEXT_MAX} · ${t('editDialog.lengthHelper', { max: STATE_TEXT_MAX })}`}
            onChange={event => setEditStateText(event.target.value)}
          />
          <TextField
            size="small"
            fullWidth
            label={t('editDialog.eventText')}
            value={editEventText}
            error={editEventText.length > EVENT_TEXT_MAX}
            helperText={`${editEventText.length} / ${EVENT_TEXT_MAX} · ${t('editDialog.lengthHelper', { max: EVENT_TEXT_MAX })}`}
            onChange={event => setEditEventText(event.target.value)}
          />
        </Box>
      </NekroDialog>

      <NekroDialog
        open={deleteReasonEventId !== null}
        onClose={() => setDeleteReasonEventId(null)}
        title={t('deleteReasonDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setDeleteReasonEventId(null)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={deleteReasonMutation.isPending}
              onClick={() => {
                if (detailId === null || deleteReasonEventId === null) return
                deleteReasonMutation.mutate({ recordId: detailId, eventId: deleteReasonEventId })
              }}
            >
              {t('deleteReasonDialog.submit')}
            </ActionButton>
          </>
        }
      >
        <Typography variant="body2" sx={{ pt: 1 }}>
          {t('deleteReasonDialog.description')}
        </Typography>
      </NekroDialog>

      <NekroDialog
        open={deleteMilestoneTarget !== null}
        onClose={() => setDeleteMilestoneTarget(null)}
        title={t('deleteMilestoneDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setDeleteMilestoneTarget(null)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={deleteMilestoneMutation.isPending}
              onClick={() => {
                if (detailId === null || !deleteMilestoneTarget) return
                deleteMilestoneMutation.mutate({
                  recordId: detailId,
                  paragraphId: deleteMilestoneTarget.paragraph_id,
                })
              }}
            >
              {t('deleteMilestoneDialog.submit')}
            </ActionButton>
          </>
        }
      >
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, pt: 1 }}>
          <Typography variant="body2">{t('deleteMilestoneDialog.description')}</Typography>
          {deleteMilestoneTarget && (
            <Typography variant="body2" color="text.secondary">
              {deleteMilestoneTarget.content}
            </Typography>
          )}
        </Box>
      </NekroDialog>
    </Box>
  )
}
