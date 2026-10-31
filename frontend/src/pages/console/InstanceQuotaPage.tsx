import { useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Checkbox,
  Chip,
  CircularProgress,
  FormControlLabel,
  SxProps,
  Switch,
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
import BoltRoundedIcon from '@mui/icons-material/BoltRounded'
import EditRoundedIcon from '@mui/icons-material/EditRounded'
import HourglassEmptyRoundedIcon from '@mui/icons-material/HourglassEmptyRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import SpeedRoundedIcon from '@mui/icons-material/SpeedRounded'
import TuneRoundedIcon from '@mui/icons-material/TuneRounded'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import StatCard from '../../components/common/StatCard'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import { useNotification } from '../../hooks/useNotification'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  CONSOLE_QUOTA_QUERY_KEYS,
  consoleQuotaApi,
  type InstanceQuotaInfo,
  type InstanceQuotaLimitSource,
  type InstanceQuotaUpdateRequest,
} from '../../services/api/consoleQuota'

/** 编辑对话框的两种形态；null 表示未打开 */
type QuotaDialogMode = 'single' | 'batch' | null

/**
 * 配额生效层级的配色
 *
 * 「来自哪一层」是这张表最容易看错的一列：实例层是本页能直接改的，
 * 适配器层与全局层只是继承来的显示值，配色上要能一眼分开。
 */
function limitSourceColor(theme: Theme, source: InstanceQuotaLimitSource): string {
  if (source === 'instance') return theme.palette.primary.main
  if (source === 'adapter') return theme.palette.info.main
  return theme.palette.text.secondary
}

export default function InstanceQuotaPage() {
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('console-quota')
  const { t: tCommon } = useTranslation('common')

  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [dialogMode, setDialogMode] = useState<QuotaDialogMode>(null)
  const [editTargetKey, setEditTargetKey] = useState('')
  const [dailyLimitInput, setDailyLimitInput] = useState('')
  const [clearOverride, setClearOverride] = useState(false)
  const [ceilingEnabled, setCeilingEnabled] = useState(false)
  const [ceilingInput, setCeilingInput] = useState('')

  const quotaQuery = useQuery({
    queryKey: CONSOLE_QUOTA_QUERY_KEYS.list,
    queryFn: consoleQuotaApi.listInstanceQuotas,
    staleTime: 15_000,
    retry: false,
  })

  const items = useMemo(() => quotaQuery.data?.items ?? [], [quotaQuery.data])
  /** 平台上限只有平台方能写，这个开关由后端给出，不在前端按角色推断 */
  const editableCeiling = quotaQuery.data?.editable_ceiling ?? false

  /** 列表刷新后被删掉的实例不该继续留在选中集合里 */
  const activeSelection = useMemo(
    () => selectedKeys.filter(key => items.some(item => item.instance_key === key)),
    [items, selectedKeys]
  )

  const editTarget = useMemo(
    () => items.find(item => item.instance_key === editTargetKey) ?? null,
    [editTargetKey, items]
  )

  const dialogTargets = useMemo(() => {
    if (dialogMode === 'batch') {
      return items.filter(item => activeSelection.includes(item.instance_key))
    }
    return editTarget ? [editTarget] : []
  }, [activeSelection, dialogMode, editTarget, items])

  /**
   * 本次编辑受到的平台方上限约束
   *
   * 平台方自己不受约束（他就是定上限的人）。租户批量编辑时取所选实例里最严的一档：
   * 服务端是逐个实例判定的，前端要是按最宽的算，就会让人先看到「能提交」再收到一半失败。
   */
  const lockedCeiling = useMemo(() => {
    if (editableCeiling) return null
    const ceilings = dialogTargets
      .map(item => item.ceiling)
      .filter((value): value is number => typeof value === 'number')
    return ceilings.length > 0 ? Math.min(...ceilings) : null
  }, [dialogTargets, editableCeiling])

  const dailyLimitError = useMemo(() => {
    if (clearOverride) return ''
    const raw = dailyLimitInput.trim()
    if (!raw) return ''
    const parsed = Number(raw)
    if (!Number.isInteger(parsed)) return t('dialog.errors.invalid')
    if (parsed < 0) return t('dialog.errors.negative')
    if (lockedCeiling === null) return ''
    // 0 在配额字段里表示「不限制」，有上限时它是最大的越界而不是最小的取值
    if (parsed === 0) return t('dialog.errors.unlimitedBlocked', { ceiling: lockedCeiling })
    if (parsed > lockedCeiling) return t('dialog.errors.overCeiling', { ceiling: lockedCeiling })
    return ''
  }, [clearOverride, dailyLimitInput, lockedCeiling, t])

  const ceilingError = useMemo(() => {
    if (!editableCeiling || !ceilingEnabled) return ''
    const raw = ceilingInput.trim()
    if (!raw) return ''
    const parsed = Number(raw)
    if (!Number.isInteger(parsed) || parsed < 0) return t('dialog.errors.ceilingInvalid')
    return ''
  }, [ceilingEnabled, ceilingInput, editableCeiling, t])

  /** 本次要提交的改动；没有任何字段被动过时为 null，提交按钮据此禁用 */
  const pendingUpdate = useMemo<InstanceQuotaUpdateRequest | null>(() => {
    const update: InstanceQuotaUpdateRequest = {}
    let touched = false
    if (clearOverride) {
      update.clear_override = true
      touched = true
    } else if (dailyLimitInput.trim()) {
      update.daily_limit = Number(dailyLimitInput.trim())
      touched = true
    }
    if (editableCeiling && ceilingEnabled) {
      // 空输入按「取消上限」处理，与后端 0/负数取消上限的语义一致
      update.set_ceiling = true
      update.ceiling = ceilingInput.trim() ? Number(ceilingInput.trim()) : 0
      touched = true
    }
    return touched ? update : null
  }, [ceilingEnabled, ceilingInput, clearOverride, dailyLimitInput, editableCeiling])

  const closeDialog = () => {
    setDialogMode(null)
    setEditTargetKey('')
    setDailyLimitInput('')
    setClearOverride(false)
    setCeilingEnabled(false)
    setCeilingInput('')
  }

  const updateMutation = useMutation({
    mutationFn: (payload: { instanceKey: string; update: InstanceQuotaUpdateRequest }) =>
      consoleQuotaApi.updateInstanceQuota(payload.instanceKey, payload.update),
    onSuccess: () => {
      notification.success(t('messages.updateSuccess'))
      closeDialog()
      void queryClient.invalidateQueries({ queryKey: CONSOLE_QUOTA_QUERY_KEYS.list })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const batchMutation = useMutation({
    mutationFn: (payload: { instanceKeys: string[]; update: InstanceQuotaUpdateRequest }) =>
      consoleQuotaApi.batchUpdateInstanceQuota({
        instance_keys: payload.instanceKeys,
        update: payload.update,
      }),
    onSuccess: result => {
      // 批量是逐个实例结算的，有失败项时用告警而不是成功，避免「报了成功但一半没生效」
      if (result.failed.length > 0) {
        notification.warning(result.message)
      } else {
        notification.success(result.message)
      }
      setSelectedKeys([])
      closeDialog()
      void queryClient.invalidateQueries({ queryKey: CONSOLE_QUOTA_QUERY_KEYS.list })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const isSubmitting = updateMutation.isPending || batchMutation.isPending
  const allSelected = items.length > 0 && activeSelection.length === items.length
  const someSelected = activeSelection.length > 0 && !allSelected

  const formatLimit = (value: number) => (value > 0 ? String(value) : t('value.unlimited'))

  const openSingleDialog = (item: InstanceQuotaInfo) => {
    setDialogMode('single')
    setEditTargetKey(item.instance_key)
    setDailyLimitInput(String(item.daily_limit))
    setClearOverride(false)
    setCeilingEnabled(false)
    setCeilingInput(item.ceiling === null ? '' : String(item.ceiling))
  }

  const openBatchDialog = () => {
    setDialogMode('batch')
    setEditTargetKey('')
    setDailyLimitInput('')
    setClearOverride(false)
    setCeilingEnabled(false)
    setCeilingInput('')
  }

  const handleSubmit = () => {
    if (!pendingUpdate) return
    if (dialogMode === 'batch') {
      batchMutation.mutate({ instanceKeys: activeSelection, update: pendingUpdate })
      return
    }
    if (!editTargetKey) return
    updateMutation.mutate({ instanceKey: editTargetKey, update: pendingUpdate })
  }

  const toggleRow = (instanceKey: string, checked: boolean) => {
    setSelectedKeys(previous => {
      if (checked) {
        return previous.includes(instanceKey) ? previous : [...previous, instanceKey]
      }
      return previous.filter(key => key !== instanceKey)
    })
  }

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
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography variant="h6" fontWeight={700}>
            {t('title')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('subtitle')}
          </Typography>
        </Box>
        {activeSelection.length > 0 && (
          <Typography variant="caption" color="text.secondary">
            {t('list.selected', { count: activeSelection.length })}
          </Typography>
        )}
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<RefreshRoundedIcon />}
          onClick={() => void quotaQuery.refetch()}
        >
          {t('actions.refresh')}
        </ActionButton>
        <ActionButton
          tone="primary"
          size="small"
          startIcon={<TuneRoundedIcon />}
          disabled={activeSelection.length === 0}
          onClick={openBatchDialog}
        >
          {t('actions.batchEdit')}
        </ActionButton>
      </Box>

      {/* 失败时这一排整块不画。三张卡片各自 `?? 0`，403 之下会稳定地显示
          「全局并发上限 0 / 运行中 0 / 排队 0」——那是三句假话，而且是**看起来很像真的**
          三句假话（配额确实可能是 0）。与总后台首页的「实例总数 0」是同一个缺陷 */}
      {!quotaQuery.isError && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: { xs: 1.25, sm: 2 } }}>
          <StatCard
            label={t('stats.globalCapacity')}
            value={quotaQuery.data?.global_capacity ?? 0}
            icon={<SpeedRoundedIcon />}
            color={theme.palette.primary.main}
            loading={quotaQuery.isLoading}
          />
          <StatCard
            label={t('stats.globalActive')}
            value={quotaQuery.data?.global_active ?? 0}
            icon={<BoltRoundedIcon />}
            color={theme.palette.success.main}
            loading={quotaQuery.isLoading}
          />
          <StatCard
            label={t('stats.globalQueued')}
            value={quotaQuery.data?.global_queued ?? 0}
            icon={<HourglassEmptyRoundedIcon />}
            color={theme.palette.warning.main}
            loading={quotaQuery.isLoading}
          />
        </Box>
      )}

      <Box sx={UNIFIED_TABLE_STYLES.tableContentContainer as SxProps<Theme>}>
        <Box sx={UNIFIED_TABLE_STYLES.tableViewport as SxProps<Theme>}>
          {quotaQuery.isLoading ? (
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
          ) : quotaQuery.isError ? (
            /* 原来这里只有一句「配额列表加载失败」+ 后端原文 + 重试。403 之下那句原文是
               「权限不足」，而重试按钮点了永远是同一个结果 —— 统一换成能分辨
               「你无权查看」与「加载失败」的那个组件，403 分支不再摆重试 */
            <QueryErrorState
              error={quotaQuery.error}
              onRetry={() => void quotaQuery.refetch()}
            />
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
              <Typography variant="subtitle1">{t('list.empty')}</Typography>
              <Typography variant="body2" color="text.secondary">
                {t('list.emptyHint')}
              </Typography>
            </Box>
          ) : (
            <Table stickyHeader sx={UNIFIED_TABLE_STYLES.getTableBase(false, false)}>
              <TableHead>
                <TableRow>
                  <TableCell padding="checkbox" sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    <Checkbox
                      size="small"
                      checked={allSelected}
                      indeterminate={someSelected}
                      inputProps={{ 'aria-label': t('selectAllAria') }}
                      onChange={event =>
                        setSelectedKeys(
                          event.target.checked ? items.map(item => item.instance_key) : []
                        )
                      }
                    />
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('columns.instance')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('columns.dailyLimit')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('columns.ceiling')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('columns.usedToday')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('columns.concurrency')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="center">
                    {t('columns.actions')}
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {items.map(item => (
                  <TableRow
                    key={item.instance_key}
                    hover
                    sx={UNIFIED_TABLE_STYLES.row as SxProps<Theme>}
                  >
                    <TableCell padding="checkbox" sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Checkbox
                        size="small"
                        checked={activeSelection.includes(item.instance_key)}
                        inputProps={{
                          'aria-label': t('selectRowAria', {
                            name: item.display_name || item.instance_key,
                          }),
                        }}
                        onChange={event => toggleRow(item.instance_key, event.target.checked)}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
                        <Typography variant="body2" fontWeight={600} noWrap>
                          {item.display_name || item.instance_key}
                        </Typography>
                        {!item.enabled && (
                          <Chip
                            size="small"
                            label={t('list.disabled')}
                            sx={CHIP_VARIANTS.getCustomColorChip(
                              theme.palette.text.secondary,
                              true
                            )}
                          />
                        )}
                      </Box>
                      <Typography variant="caption" color="text.secondary" noWrap>
                        {item.instance_key}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Box
                        sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}
                      >
                        <Typography variant="body2" fontWeight={600}>
                          {formatLimit(item.daily_limit)}
                        </Typography>
                        <Chip
                          size="small"
                          label={t(`source.${item.limit_source}`)}
                          sx={CHIP_VARIANTS.getCustomColorChip(
                            limitSourceColor(theme, item.limit_source),
                            true
                          )}
                        />
                      </Box>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Typography variant="body2">
                        {item.ceiling === null ? t('value.noCeiling') : item.ceiling}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Box
                        sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}
                      >
                        <Typography variant="body2" fontWeight={600}>
                          {item.replies_today}
                        </Typography>
                        {item.channels_at_limit > 0 && (
                          <Chip
                            size="small"
                            label={t('usage.channelsAtLimit', { count: item.channels_at_limit })}
                            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.warning.main, true)}
                          />
                        )}
                      </Box>
                      {/* caption 默认渲染成 inline span，noWrap 的 overflow:hidden 对它不生效，
                          文字会溢出到右边那一列上去。改成块级元素才能真正在本列内截断 */}
                      <Typography
                        variant="caption"
                        color="text.secondary"
                        noWrap
                        component="div"
                        sx={{ maxWidth: '100%' }}
                      >
                        {t('usage.activeChannels', { count: item.active_channels })} ·{' '}
                        {t('usage.peakChannel', { count: item.peak_channel_replies })}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <SimpleTooltip
                        title={t('usage.concurrencyHint', {
                          active: item.concurrency.active,
                          max:
                            item.concurrency.max_concurrent > 0
                              ? item.concurrency.max_concurrent
                              : t('value.infinite'),
                          admitted: item.concurrency.admitted_total,
                          wait: item.concurrency.max_wait_seconds,
                        })}
                      >
                        <Box sx={{ minWidth: 0 }}>
                          <Typography variant="body2" fontWeight={600}>
                            {item.concurrency.active} /{' '}
                            {item.concurrency.max_concurrent > 0
                              ? item.concurrency.max_concurrent
                              : t('value.infinite')}
                          </Typography>
                          <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 0.75, minWidth: 0 }}>
                            <Typography variant="caption" color="text.secondary" noWrap>
                              {t('usage.queued', { count: item.concurrency.queued })}
                            </Typography>
                            {item.concurrency.rejected_total > 0 && (
                              <Typography variant="caption" color="error.main" noWrap>
                                {t('usage.rejected', { count: item.concurrency.rejected_total })}
                              </Typography>
                            )}
                          </Box>
                        </Box>
                      </SimpleTooltip>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="center">
                      <SimpleTooltip title={t('actions.edit')}>
                        <span>
                          <IconActionButton
                            size="small"
                            tone="primary"
                            onClick={() => openSingleDialog(item)}
                          >
                            <EditRoundedIcon fontSize="small" />
                          </IconActionButton>
                        </span>
                      </SimpleTooltip>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Box>
      </Box>

      <NekroDialog
        open={dialogMode !== null}
        onClose={closeDialog}
        title={dialogMode === 'batch' ? t('dialog.titleBatch') : t('dialog.titleSingle')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={closeDialog}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="primary"
              disabled={
                !pendingUpdate ||
                Boolean(dailyLimitError) ||
                Boolean(ceilingError) ||
                dialogTargets.length === 0 ||
                isSubmitting
              }
              onClick={handleSubmit}
            >
              {tCommon('actions.save')}
            </ActionButton>
          </>
        }
      >
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          {dialogMode === 'batch' ? (
            <Alert severity="info" variant="outlined">
              {t('dialog.batchTargets', { count: dialogTargets.length })}
            </Alert>
          ) : (
            editTarget && (
              <Typography variant="body2" color="text.secondary">
                {editTarget.display_name || editTarget.instance_key} · {editTarget.instance_key}
              </Typography>
            )
          )}

          {lockedCeiling !== null && (
            <Alert severity="warning" variant="outlined">
              {dialogMode === 'batch'
                ? t('dialog.ceilingLockedHintBatch', { ceiling: lockedCeiling })
                : t('dialog.ceilingLockedHint', { ceiling: lockedCeiling })}
            </Alert>
          )}

          <TextField
            size="small"
            type="number"
            fullWidth
            label={t('dialog.dailyLimitLabel')}
            value={dailyLimitInput}
            disabled={clearOverride}
            error={Boolean(dailyLimitError)}
            helperText={dailyLimitError || t('dialog.dailyLimitHelper')}
            slotProps={{ htmlInput: { min: 0, step: 1 } }}
            onChange={event => setDailyLimitInput(event.target.value)}
          />

          <Box>
            <FormControlLabel
              control={
                <Checkbox
                  size="small"
                  checked={clearOverride}
                  onChange={event => setClearOverride(event.target.checked)}
                />
              }
              label={
                dialogMode === 'batch'
                  ? t('dialog.clearOverrideLabelBatch')
                  : t('dialog.clearOverrideLabel', {
                      value: formatLimit(editTarget?.inherited_limit ?? 0),
                    })
              }
            />
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              {t('dialog.clearOverrideHelper')}
            </Typography>
          </Box>

          {editableCeiling && (
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
              <FormControlLabel
                control={
                  <Switch
                    size="small"
                    checked={ceilingEnabled}
                    onChange={event => setCeilingEnabled(event.target.checked)}
                  />
                }
                label={t('dialog.setCeilingLabel')}
              />
              <TextField
                size="small"
                type="number"
                fullWidth
                label={t('dialog.ceilingLabel')}
                value={ceilingInput}
                disabled={!ceilingEnabled}
                error={Boolean(ceilingError)}
                helperText={ceilingError || t('dialog.ceilingHelper')}
                slotProps={{ htmlInput: { min: 0, step: 1 } }}
                onChange={event => setCeilingInput(event.target.value)}
              />
            </Box>
          )}
        </Box>
      </NekroDialog>
    </Box>
  )
}
