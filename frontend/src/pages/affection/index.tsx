/**
 * 好感度管理页
 *
 * 呈现顺序刻意与数据结构相反：先看到的是 Bot 自己写的那句话（`state_text`），
 * 分数只作为一个小小的副标题跟在关系阶段后面。好感度是给人玩的，
 * 做成一张按分数排序的审计表就没意思了——数值该在的地方是排序键和调整入口，不是主角。
 *
 * 作用域跟随顶栏的实例切换器：好感度按 (实例, 用户) 隔离，
 * 全局视图看到全部实例的数据，实例视图只看该实例的。租户身份的越权访问由服务端按 404 处理。
 */

import { useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Divider,
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
import AutoFixHighRoundedIcon from '@mui/icons-material/AutoFixHighRounded'
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
import SearchField from '../../components/common/SearchField'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { useNotification } from '../../hooks/useNotification'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  affectionApi,
  type AffectionItem,
  type AffectionSortBy,
  type AffectionStageValue,
} from '../../services/api/affection'

const PAGE_SIZE = 20

const SORT_OPTIONS: AffectionSortBy[] = ['score_desc', 'score_asc', 'recent', 'interaction']

function formatDateTime(value: string | null, fallback: string): string {
  if (!value) return fallback
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return fallback
  return new Date(timestamp).toLocaleString()
}

export default function AffectionPage() {
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('affection')
  const { t: tCommon } = useTranslation('common')
  const { currentInstanceKey, isScopeReady } = useInstanceScope()

  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [stageFilter, setStageFilter] = useState<AffectionStageValue | 'all'>('all')
  const [sortBy, setSortBy] = useState<AffectionSortBy>('score_desc')
  const [detailId, setDetailId] = useState<number | null>(null)
  const [resetTarget, setResetTarget] = useState<AffectionItem | null>(null)
  const [adjustTarget, setAdjustTarget] = useState<AffectionItem | null>(null)
  const [adjustScore, setAdjustScore] = useState('0')
  const [adjustReason, setAdjustReason] = useState('')

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

  const items = listQuery.data?.items ?? []
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
              sx={{ minHeight: 240, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
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
                {items.map(item => (
                  <TableRow key={item.id} hover sx={UNIFIED_TABLE_STYLES.row as SxProps<Theme>}>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
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
                        <Typography variant="caption" color="warning.main" noWrap display="block">
                          {t('columns.mergedInto', { person: item.merged_into })}
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
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="right">
                      {item.interaction_count}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="center">
                      <Box sx={{ display: 'flex', gap: 0.5, justifyContent: 'center' }}>
                        <SimpleTooltip title={t('actions.detail')}>
                          <span>
                            <IconActionButton
                              size="small"
                              tone="primary"
                              onClick={() => setDetailId(item.id)}
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
                ))}
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
        onClose={() => setDetailId(null)}
        title={t('detail.title')}
        titleStartIcon={<FavoriteRoundedIcon fontSize="small" />}
        maxWidth="sm"
        actions={
          <ActionButton tone="ghost" onClick={() => setDetailId(null)}>
            {tCommon('actions.close')}
          </ActionButton>
        }
      >
        {detailQuery.isLoading || !detailQuery.data ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
            <CircularProgress size={24} />
          </Box>
        ) : (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
            <Box>
              <Typography variant="subtitle1" fontWeight={700}>
                {detailQuery.data.item.nickname || detailQuery.data.item.platform_userid}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {detailQuery.data.item.platform_userid}
                {detailQuery.data.item.instance_key
                  ? ` · ${detailQuery.data.item.instance_key}`
                  : ''}
                {' · '}
                {detailQuery.data.item.stage_label} ({detailQuery.data.item.score})
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
                    <Box sx={{ minWidth: 0 }}>
                      <Typography variant="body2">
                        {event.reason || t(`detail.source.${event.source}`)}
                      </Typography>
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
            {t('adjustDialog.description')}
          </Typography>
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
        <Typography variant="body2" sx={{ pt: 1 }}>
          {t('resetDialog.description', {
            name: resetTarget?.nickname || resetTarget?.platform_userid || '',
          })}
        </Typography>
      </NekroDialog>
    </Box>
  )
}
