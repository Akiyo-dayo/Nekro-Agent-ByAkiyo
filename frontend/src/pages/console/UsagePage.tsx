/**
 * 用量与费用（总后台）
 *
 * 回答的问题是：**哪个租户 / 哪个实例、用哪个模型组与模型、烧了多少 token、折算多少钱、什么时候**。
 * 数据来自 `nekro_agent/routers/console/usage.py`，作用域由后端按当前身份收敛：
 * 平台方看全部，租户只看自己名下的行。
 *
 * ⚠️ **不画「哪个人」这一维**：接口出参里有 `person_uid`，但它当前没有任何生产者
 * （计费单元不是人，一次回合的 prompt 装的是整个频道的上下文）。画一列恒为空的东西
 * 与「把 403 渲染成 0」是同一类错——用户会当真。理由见后端模型的模块文档。
 *
 * ## 页面的取舍
 *
 * - **不做扣费、也不显示"剩余额度"**：本轮只有账本与视图，扣费策略是产品决策（PROJECT_STATE §8）。
 *   画一个"剩余额度"就等于先斩后奏地固化一个口径。
 * - **失败与中断的那些行照常计入合计**：它们也是真金白银（上游 2xx 但正文为空的那一类
 *   已经计过费）。「成功率」单列一格，让"钱花了但没拿到回复"这件事可见。
 * - **模型组名可能是后端替换过的占位串**（租户看不到的平台组），所以这一列不做任何跳转。
 * - 越权一律 404，与"这个实例不存在"不可区分——这是后端刻意的，页面如实呈现错误即可。
 */

import { useMemo, useState } from 'react'
import {
  Box,
  Card,
  CardContent,
  Chip,
  LinearProgress,
  MenuItem,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
  useTheme,
} from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import PaidRoundedIcon from '@mui/icons-material/PaidRounded'
import TokenRoundedIcon from '@mui/icons-material/TokenRounded'
import ForumRoundedIcon from '@mui/icons-material/ForumRounded'
import ReportProblemRoundedIcon from '@mui/icons-material/ReportProblemRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import ActionButton from '../../components/common/ActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import StatCard from '../../components/common/StatCard'
import TablePaginationStyled from '../../components/common/TablePaginationStyled'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  CONSOLE_USAGE_QUERY_KEYS,
  consoleUsageApi,
  type UsageBreakdownItem,
  type UsageFilters,
} from '../../services/api/consoleUsage'

/** 预设时间窗（天）。上限由后端的 `max_window_days` 兜底，这里只给常用几档 */
const WINDOW_PRESETS = [1, 7, 30, 90] as const

const PAGE_SIZE = 25

/** 大数字加千分位；token 数动辄七位，不分节根本读不出量级 */
const formatInt = (value: number): string => value.toLocaleString()

/**
 * 费用的显示精度
 *
 * 倍率是运营自己配的相对值（不是某个货币单位），所以这里**不加货币符号**——
 * 写个 ¥ 或 $ 就等于替用户断言了单位，而 `token_input_rate` 默认 1.0 时它其实是 token 数。
 */
const formatCost = (value: number): string =>
  value >= 100 ? value.toFixed(0) : value.toFixed(2)

function windowRange(days: number): { startTime: string; endTime: string } {
  const end = new Date()
  const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000)
  return { startTime: start.toISOString(), endTime: end.toISOString() }
}

function BreakdownTable({
  title,
  items,
  emptyText,
  keyHeader,
}: {
  title: string
  items: UsageBreakdownItem[]
  emptyText: string
  keyHeader: string
}) {
  const { t } = useTranslation('console-usage')
  return (
    <Card sx={{ flex: '1 1 320px', minWidth: 300 }}>
      <CardContent>
        <Typography variant="subtitle2" fontWeight={700} gutterBottom>
          {title}
        </Typography>
        {items.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            {emptyText}
          </Typography>
        ) : (
          <Table size="small" sx={UNIFIED_TABLE_STYLES.getTableBase(false, false)}>
            <TableHead>
              <TableRow>
                <TableCell>{keyHeader}</TableCell>
                <TableCell align="right">{t('columns.records')}</TableCell>
                <TableCell align="right">{t('columns.tokens')}</TableCell>
                <TableCell align="right">{t('columns.cost')}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {items.slice(0, 10).map(item => (
                <TableRow key={item.key || '__empty__'}>
                  <TableCell sx={{ wordBreak: 'break-all' }}>
                    {item.key || t('value.unattributed')}
                  </TableCell>
                  <TableCell align="right">{formatInt(item.records)}</TableCell>
                  <TableCell align="right">{formatInt(item.token_total)}</TableCell>
                  <TableCell align="right">{formatCost(item.cost)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

export default function UsagePage() {
  const theme = useTheme()
  const { t } = useTranslation('console-usage')

  const [windowDays, setWindowDays] = useState<number>(7)
  const [instanceKey, setInstanceKey] = useState('')
  const [scene, setScene] = useState('')
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(0)
  /**
   * 「刷新」按下的次数
   *
   * ⚠️ 少了它，刷新按钮**永远拿不到新数据**：时间窗被 `useMemo` 冻在打开页面那一刻，
   * `refetch()` 用的是同一组 `start_time` / `end_time`，于是页面开着一小时，
   * 刷新多少次也看不到这一小时里新写进来的流水。那正是本项目栽过两次的
   * 「点不动的按钮」——它有反应（转一圈）、就是不起作用，比灰掉更难发现。
   */
  const [refreshEpoch, setRefreshEpoch] = useState(0)

  /**
   * 时间窗只在筛选条件变化（或按下刷新）时重算一次
   *
   * 每次渲染都取 `new Date()` 会让 queryKey 每次都不同，于是列表无限重取——
   * 而这一页的每次请求在后端都是六条聚合查询。
   */
  const filters: UsageFilters = useMemo(
    () => ({ instanceKey: instanceKey.trim(), ...windowRange(windowDays) }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refreshEpoch 是刻意的重算信号，不是被读取的值
    [instanceKey, windowDays, refreshEpoch]
  )

  const enumsQuery = useQuery({
    queryKey: CONSOLE_USAGE_QUERY_KEYS.enums,
    queryFn: consoleUsageApi.getEnums,
    staleTime: Infinity,
    retry: false,
  })

  const summaryQuery = useQuery({
    queryKey: CONSOLE_USAGE_QUERY_KEYS.summary(filters),
    queryFn: () => consoleUsageApi.getSummary(filters),
    staleTime: 30_000,
    retry: false,
  })

  const recordsQuery = useQuery({
    queryKey: CONSOLE_USAGE_QUERY_KEYS.records(filters, scene, status, page + 1, PAGE_SIZE),
    queryFn: () =>
      consoleUsageApi.listRecords(filters, { scene, status, page: page + 1, pageSize: PAGE_SIZE }),
    staleTime: 30_000,
    retry: false,
  })

  const total = summaryQuery.data?.total
  /** `?? []` 每次渲染都会造一个新数组，直接当依赖会让下面两个 useMemo 恒失效 */
  const byStatus = useMemo(() => summaryQuery.data?.by_status ?? [], [summaryQuery.data])
  /** 成功率按**条数**算：它回答的是"打出去的请求有多少拿到了回复" */
  const successRate = useMemo(() => {
    const records = byStatus.reduce((sum, item) => sum + item.records, 0)
    if (records === 0) return null
    const ok = byStatus.find(item => item.key === 'success')?.records ?? 0
    return Math.round((ok / records) * 1000) / 10
  }, [byStatus])

  /** 失败 + 中断烧掉的钱：这一格存在的意义就是让"花了钱没拿到回复"可见 */
  const wastedCost = useMemo(
    () =>
      byStatus
        .filter(item => item.key !== 'success')
        .reduce((sum, item) => sum + item.cost, 0),
    [byStatus]
  )

  if (summaryQuery.isError) {
    return <QueryErrorState error={summaryQuery.error} onRetry={() => void summaryQuery.refetch()} />
  }

  const refreshing = summaryQuery.isFetching || recordsQuery.isFetching

  return (
    <Box sx={{ p: { xs: 2, md: 3 }, display: 'flex', flexDirection: 'column', gap: 2 }}>
      <Box>
        <Typography variant="h5" fontWeight={700}>
          {t('title')}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {t('subtitle')}
        </Typography>
      </Box>

      <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap alignItems="center">
        <TextField
          select
          size="small"
          label={t('filters.window')}
          value={windowDays}
          onChange={event => {
            setWindowDays(Number(event.target.value))
            setPage(0)
          }}
          sx={{ minWidth: 140 }}
        >
          {WINDOW_PRESETS.map(days => (
            <MenuItem key={days} value={days}>
              {t('filters.lastDays', { count: days })}
            </MenuItem>
          ))}
        </TextField>
        <TextField
          size="small"
          label={t('filters.instance')}
          value={instanceKey}
          onChange={event => {
            setInstanceKey(event.target.value)
            setPage(0)
          }}
          placeholder={t('filters.instancePlaceholder')}
          sx={{ minWidth: 200 }}
        />
        <TextField
          select
          size="small"
          label={t('filters.scene')}
          value={scene}
          onChange={event => {
            setScene(event.target.value)
            setPage(0)
          }}
          sx={{ minWidth: 170 }}
        >
          <MenuItem value="">{t('filters.all')}</MenuItem>
          {(enumsQuery.data?.scenes ?? []).map(item => (
            <MenuItem key={item} value={item}>
              {t(`scene.${item}`, { defaultValue: item })}
            </MenuItem>
          ))}
        </TextField>
        <TextField
          select
          size="small"
          label={t('filters.status')}
          value={status}
          onChange={event => {
            setStatus(event.target.value)
            setPage(0)
          }}
          sx={{ minWidth: 150 }}
        >
          <MenuItem value="">{t('filters.all')}</MenuItem>
          {(enumsQuery.data?.statuses ?? []).map(item => (
            <MenuItem key={item} value={item}>
              {t(`status.${item}`, { defaultValue: item })}
            </MenuItem>
          ))}
        </TextField>
        <ActionButton
          startIcon={<RefreshRoundedIcon />}
          onClick={() => {
            // 先把窗口右端推到「此刻」再取：只 refetch 的话查的还是打开页面那一刻的窗口，
            // 这一小时新写进来的流水一条都不会出现（见 `refreshEpoch` 的说明）。
            // `filters` 变了 → queryKey 变了 → react-query 自己会重取，不必再 refetch 一次
            setRefreshEpoch(epoch => epoch + 1)
            setPage(0)
          }}
          disabled={refreshing}
        >
          {t('actions.refresh')}
        </ActionButton>
      </Stack>

      {refreshing && <LinearProgress />}

      <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap>
        <StatCard
          label={t('stats.cost')}
          value={total ? formatCost(total.cost) : '0'}
          icon={<PaidRoundedIcon />}
          color={theme.palette.primary.main}
          loading={summaryQuery.isLoading}
        />
        <StatCard
          label={t('stats.tokens')}
          value={total ? formatInt(total.token_total) : '0'}
          icon={<TokenRoundedIcon />}
          color={theme.palette.info.main}
          loading={summaryQuery.isLoading}
        />
        <StatCard
          label={t('stats.records')}
          value={total ? formatInt(total.records) : '0'}
          icon={<ForumRoundedIcon />}
          color={theme.palette.success.main}
          loading={summaryQuery.isLoading}
        />
        <StatCard
          label={t('stats.wasted')}
          value={successRate === null ? '-' : `${formatCost(wastedCost)} / ${successRate}%`}
          icon={<ReportProblemRoundedIcon />}
          color={theme.palette.warning.main}
          loading={summaryQuery.isLoading}
        />
      </Stack>

      <Typography variant="caption" color="text.secondary">
        {t('hint.noBilling')}
      </Typography>

      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
        {/*
          「每个客户烧了多少」是平台方打开这一页要看的**第一件事**（共用版 Bot 烧的是
          平台方的 Key），后端一直在算 `by_tenant`，只是此前没有任何地方把它画出来。
          租户身份下这一档只可能有他自己一行（`UsageScope.records()` 已按 tenant_id 过滤），
          留着不碍事，也省了一条"当前身份是谁"的前端判定。
          空 key = `tenant_id IS NULL`，也就是平台方自己的花销与归不了属的那些行。
        */}
        <BreakdownTable
          title={t('breakdown.byTenant')}
          keyHeader={t('columns.tenant')}
          items={summaryQuery.data?.by_tenant ?? []}
          emptyText={t('value.empty')}
        />
        <BreakdownTable
          title={t('breakdown.byInstance')}
          keyHeader={t('columns.instance')}
          items={summaryQuery.data?.by_instance ?? []}
          emptyText={t('value.empty')}
        />
        <BreakdownTable
          title={t('breakdown.byModelGroup')}
          keyHeader={t('columns.modelGroup')}
          items={summaryQuery.data?.by_model_group ?? []}
          emptyText={t('value.empty')}
        />
        <BreakdownTable
          title={t('breakdown.byModel')}
          keyHeader={t('columns.model')}
          items={summaryQuery.data?.by_model ?? []}
          emptyText={t('value.empty')}
        />
        <BreakdownTable
          title={t('breakdown.byScene')}
          keyHeader={t('columns.scene')}
          items={(summaryQuery.data?.by_scene ?? []).map(item => ({
            ...item,
            key: t(`scene.${item.key}`, { defaultValue: item.key }),
          }))}
          emptyText={t('value.empty')}
        />
      </Stack>

      <Card>
        <CardContent>
          <Typography variant="subtitle2" fontWeight={700} gutterBottom>
            {t('records.title')}
          </Typography>
          {recordsQuery.isError ? (
            <QueryErrorState
              error={recordsQuery.error}
              onRetry={() => void recordsQuery.refetch()}
            />
          ) : (
            <>
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small" sx={UNIFIED_TABLE_STYLES.getTableBase(false, false)}>
                  <TableHead>
                    <TableRow>
                      <TableCell>{t('columns.time')}</TableCell>
                      <TableCell>{t('columns.instance')}</TableCell>
                      <TableCell>{t('columns.model')}</TableCell>
                      <TableCell>{t('columns.scene')}</TableCell>
                      <TableCell>{t('columns.status')}</TableCell>
                      <TableCell align="right">{t('columns.input')}</TableCell>
                      <TableCell align="right">{t('columns.output')}</TableCell>
                      <TableCell align="right">{t('columns.cost')}</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {(recordsQuery.data?.items ?? []).map(row => (
                      <TableRow key={row.id}>
                        <TableCell sx={{ whiteSpace: 'nowrap' }}>
                          {new Date(row.create_time).toLocaleString()}
                        </TableCell>
                        <TableCell>{row.instance_key || t('value.unattributed')}</TableCell>
                        <TableCell sx={{ wordBreak: 'break-all' }}>
                          {row.model_name}
                          {row.is_fallback && (
                            <Chip size="small" label={t('value.fallback')} sx={{ ml: 0.5 }} />
                          )}
                          {row.attempt_index > 1 && (
                            <Chip
                              size="small"
                              label={t('value.attempt', {
                                index: row.attempt_index,
                                total: row.attempt_total,
                              })}
                              sx={{ ml: 0.5 }}
                            />
                          )}
                        </TableCell>
                        <TableCell>{t(`scene.${row.scene}`, { defaultValue: row.scene })}</TableCell>
                        <TableCell>
                          {t(`status.${row.status}`, { defaultValue: row.status })}
                          {/* 只显示异常类名与归一化后的 finish_reason，后端不落任何上游原文 */}
                          {row.error_type && (
                            <Typography variant="caption" color="text.secondary" display="block">
                              {row.error_type}
                              {row.finish_reason ? ` · ${row.finish_reason}` : ''}
                            </Typography>
                          )}
                        </TableCell>
                        <TableCell align="right">{formatInt(row.token_input)}</TableCell>
                        <TableCell align="right">{formatInt(row.token_output)}</TableCell>
                        <TableCell align="right">{formatCost(row.cost)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
              {(recordsQuery.data?.items ?? []).length === 0 && (
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                  {t('value.empty')}
                </Typography>
              )}
              <TablePaginationStyled
                component="div"
                count={recordsQuery.data?.total ?? 0}
                page={page}
                onPageChange={(_event, next) => setPage(next)}
                rowsPerPage={PAGE_SIZE}
                rowsPerPageOptions={[PAGE_SIZE]}
                onRowsPerPageChange={() => undefined}
              />
            </>
          )}
        </CardContent>
      </Card>
    </Box>
  )
}
