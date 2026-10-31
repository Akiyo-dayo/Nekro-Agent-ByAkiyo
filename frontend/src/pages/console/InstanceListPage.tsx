import { useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Box,
  CircularProgress,
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
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import ArrowForwardRoundedIcon from '@mui/icons-material/ArrowForwardRounded'
import DeleteOutlineRoundedIcon from '@mui/icons-material/DeleteOutlineRounded'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import SearchField from '../../components/common/SearchField'
import SegmentedControl from '../../components/common/SegmentedControl'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import { useIdentity } from '../../hooks/useIdentity'
import { useNotification } from '../../hooks/useNotification'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  INSTANCE_STATUSES,
  consoleApi,
  consolePaths,
  type InstanceInfo,
  type InstanceStatus,
} from '../../services/api/console'
import InstanceStatusChip from './components/InstanceStatusChip'
import UnclaimedProtocolPanel from './components/UnclaimedProtocolPanel'

type DeleteMode = 'archive' | 'purge'

const isInstanceStatus = (value: string | null): value is InstanceStatus =>
  !!value && INSTANCE_STATUSES.includes(value as InstanceStatus)

function formatDateTime(value: string | null, fallback: string): string {
  if (!value) return fallback
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return fallback
  return new Date(timestamp).toLocaleString()
}

export default function InstanceListPage() {
  const navigate = useNavigate()
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('console')
  const { t: tCommon } = useTranslation('common')
  // 实例的**生命周期**动作（新建 / 启停 / 归档删除）全是 `Role.Super`
  // （`routers/console/instances.py`，登记在 `PLATFORM_ONLY` 里）。协议端自助运维降到租户可达
  // 之后，租户管理员会正常走进这张列表看自己的实例 —— 于是这三个按钮变成了摆在他面前、
  // 点了必然 403 的入口。详情页顶栏那个「启用 / 停用」是同一个缺陷，只是在另一个文件里。
  const { isPlatformOwner } = useIdentity()

  // 筛选与搜索属于「刷新后应保留」的状态，统一进 query
  const [searchParams, setSearchParams] = useSearchParams()
  const statusFilter = isInstanceStatus(searchParams.get('status'))
    ? (searchParams.get('status') as InstanceStatus)
    : 'all'
  const search = searchParams.get('search') ?? ''

  const [deleteTarget, setDeleteTarget] = useState<InstanceInfo | null>(null)
  const [deleteMode, setDeleteMode] = useState<DeleteMode>('archive')
  const [confirmKey, setConfirmKey] = useState('')

  const updateQuery = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams)
    // 空值不写入 URL
    if (!value || value === 'all') {
      next.delete(key)
    } else {
      next.set(key, value)
    }
    setSearchParams(next, { replace: true })
  }

  const instancesQuery = useQuery({
    queryKey: ['console-instances'],
    queryFn: () => consoleApi.listInstances(),
    staleTime: 15_000,
    // 403 不会因为再试一次变成 200；默认重试只是让「你无权查看」晚几秒出现
    retry: false,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['console-instances'] })
    void queryClient.invalidateQueries({ queryKey: ['console-overview'] })
  }

  const toggleMutation = useMutation({
    mutationFn: (payload: { instanceKey: string; enable: boolean }) =>
      payload.enable
        ? consoleApi.enableInstance(payload.instanceKey)
        : consoleApi.disableInstance(payload.instanceKey),
    onSuccess: (_data, variables) => {
      notification.success(
        variables.enable ? t('messages.enableSuccess') : t('messages.disableSuccess')
      )
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (payload: { instanceKey: string; mode: DeleteMode }) =>
      consoleApi.deleteInstance(payload.instanceKey, {
        purge: payload.mode === 'purge',
        confirm_instance_key: payload.mode === 'purge' ? payload.instanceKey : '',
      }),
    onSuccess: (_data, variables) => {
      notification.success(
        variables.mode === 'purge' ? t('messages.purgeSuccess') : t('messages.archiveSuccess')
      )
      setDeleteTarget(null)
      setConfirmKey('')
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const filteredInstances = useMemo(() => {
    const items = instancesQuery.data?.items ?? []
    const keyword = search.trim().toLowerCase()

    return items.filter(instance => {
      if (statusFilter !== 'all' && instance.status !== statusFilter) return false
      if (!keyword) return true
      return [instance.instance_key, instance.display_name, instance.provider_account_id].some(
        value => value.toLowerCase().includes(keyword)
      )
    })
  }, [instancesQuery.data, search, statusFilter])

  const statusOptions = useMemo(
    () => [
      { value: 'all', label: t('list.statusAll') },
      ...INSTANCE_STATUSES.map(status => ({ value: status, label: t(`status.${status}`) })),
    ],
    [t]
  )

  const canSubmitDelete =
    deleteMode === 'archive' || confirmKey.trim() === deleteTarget?.instance_key

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
        <Typography variant="h6" fontWeight={700} sx={{ flexShrink: 0 }}>
          {t('list.title')}
        </Typography>
        <SearchField
          value={search}
          onChange={value => updateQuery('search', value)}
          onClear={() => updateQuery('search', '')}
          clearAriaLabel={t('list.clearSearch')}
          placeholder={t('list.searchPlaceholder')}
          sx={{ flex: 1, minWidth: 200, maxWidth: 360 }}
        />
        <FilterSelect
          label={t('list.statusFilter')}
          value={statusFilter}
          options={statusOptions}
          onChange={value => updateQuery('status', value)}
          fullWidth={false}
          sx={{ minWidth: 160 }}
        />
        {isPlatformOwner && (
          <ActionButton
            tone="primary"
            size="small"
            startIcon={<AddRoundedIcon />}
            onClick={() => navigate(consolePaths.instanceCreate())}
            sx={{ ml: 'auto' }}
          >
            {t('actions.create')}
          </ActionButton>
        )}
      </Box>

      {/* 「有协议端已连接但未认领」必须先于列表出现：混在列表里管理员根本注意不到，
          而那恰恰是他要找的那条在线连接 */}
      <UnclaimedProtocolPanel
        onClaimed={result => navigate(consolePaths.instanceDetail(result.instance.instance_key))}
      />

      <Box sx={UNIFIED_TABLE_STYLES.tableContentContainer as SxProps<Theme>}>
        <Box sx={UNIFIED_TABLE_STYLES.tableViewport as SxProps<Theme>}>
          {instancesQuery.isLoading ? (
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
          ) : instancesQuery.isError ? (
            /* 此前没有这一支：`GET /console/instances` 403 时 `data` 是 undefined，
               `?? []` 把它变成空数组，页面于是显示「没有符合条件的实例 / 调整搜索词后再试」
               —— 一句指着筛选器的假话。只读租户成员看到的就是它 */
            <QueryErrorState
              error={instancesQuery.error}
              onRetry={() => void instancesQuery.refetch()}
            />
          ) : filteredInstances.length === 0 ? (
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
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('list.columns.instance')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('list.columns.status')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('list.columns.provider')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('list.columns.account')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="right">
                    {t('list.columns.channels')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="right">
                    {t('list.columns.messagesToday')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('list.columns.lastActive')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="center">
                    {t('list.columns.actions')}
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {filteredInstances.map(instance => (
                  <TableRow
                    key={instance.instance_key}
                    hover
                    sx={UNIFIED_TABLE_STYLES.row as SxProps<Theme>}
                  >
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Typography variant="body2" fontWeight={600} noWrap>
                        {instance.display_name || instance.instance_key}
                      </Typography>
                      <Typography variant="caption" color="text.secondary" noWrap>
                        {instance.instance_key}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <InstanceStatusChip
                        status={instance.status}
                        offlineSince={instance.offline_since}
                        lastError={instance.last_error}
                        small
                      />
                      {/* 未绑定账号是一个"等着管理员做一件事"的状态，给出下一步而不是只标一个颜色 */}
                      {instance.status === 'unbound' && (
                        <Typography
                          variant="caption"
                          component="div"
                          sx={{ color: theme.palette.warning.main, mt: 0.25 }}
                        >
                          {t('list.unboundAction')}
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      {t(`provider.${instance.provider}`)}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      {instance.provider_account_id || '—'}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="right">
                      {instance.stats.channel_count}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="right">
                      {instance.stats.message_count_today}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Typography variant="caption" color="text.secondary">
                        {formatDateTime(instance.stats.last_active_at, '—')}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="center">
                      <Box sx={{ display: 'flex', gap: 0.5, justifyContent: 'center' }}>
                        {isPlatformOwner && (
                          <SimpleTooltip
                            title={instance.enabled ? t('actions.disable') : t('actions.enable')}
                          >
                            <span>
                              <IconActionButton
                                size="small"
                                tone={instance.enabled ? 'subtle' : 'primary'}
                                disabled={toggleMutation.isPending}
                                onClick={() =>
                                  toggleMutation.mutate({
                                    instanceKey: instance.instance_key,
                                    enable: !instance.enabled,
                                  })
                                }
                              >
                                {instance.enabled ? (
                                  <StopRoundedIcon fontSize="small" />
                                ) : (
                                  <PlayArrowRoundedIcon fontSize="small" />
                                )}
                              </IconActionButton>
                            </span>
                          </SimpleTooltip>
                        )}
                        {isPlatformOwner && (
                          <SimpleTooltip title={t('actions.delete')}>
                            <span>
                              <IconActionButton
                                size="small"
                                tone="danger"
                                onClick={() => {
                                  setDeleteTarget(instance)
                                  setDeleteMode('archive')
                                  setConfirmKey('')
                                }}
                              >
                                <DeleteOutlineRoundedIcon fontSize="small" />
                              </IconActionButton>
                            </span>
                          </SimpleTooltip>
                        )}
                        <SimpleTooltip title={t('actions.detail')}>
                          <span>
                            <IconActionButton
                              size="small"
                              tone="primary"
                              onClick={() =>
                                navigate(consolePaths.instanceDetail(instance.instance_key))
                              }
                            >
                              <ArrowForwardRoundedIcon fontSize="small" />
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

      <NekroDialog
        open={Boolean(deleteTarget)}
        onClose={() => setDeleteTarget(null)}
        title={t('deleteDialog.title')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setDeleteTarget(null)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={!canSubmitDelete || deleteMutation.isPending}
              onClick={() => {
                if (!deleteTarget) return
                deleteMutation.mutate({
                  instanceKey: deleteTarget.instance_key,
                  mode: deleteMode,
                })
              }}
            >
              {deleteMode === 'purge'
                ? t('deleteDialog.submitPurge')
                : t('deleteDialog.submitArchive')}
            </ActionButton>
          </>
        }
      >
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
            <Typography variant="body2" color="text.secondary">
              {t('deleteDialog.modeLabel')}
            </Typography>
            <SegmentedControl<DeleteMode>
              value={deleteMode}
              options={[
                { value: 'archive', label: t('deleteDialog.modeArchive') },
                { value: 'purge', label: t('deleteDialog.modePurge') },
              ]}
              onChange={mode => {
                setDeleteMode(mode)
                setConfirmKey('')
              }}
            />
          </Box>
          <Typography
            variant="body2"
            sx={{
              color: deleteMode === 'purge' ? theme.palette.error.main : 'text.secondary',
            }}
          >
            {deleteMode === 'purge'
              ? t('deleteDialog.purgeDescription')
              : t('deleteDialog.archiveDescription')}
          </Typography>
          {deleteMode === 'purge' && deleteTarget && (
            <TextField
              size="small"
              fullWidth
              autoComplete="off"
              label={t('deleteDialog.confirmLabel')}
              placeholder={deleteTarget.instance_key}
              helperText={
                confirmKey && !canSubmitDelete
                  ? t('deleteDialog.confirmMismatch')
                  : t('deleteDialog.confirmHelper', { instanceKey: deleteTarget.instance_key })
              }
              error={Boolean(confirmKey) && !canSubmitDelete}
              value={confirmKey}
              onChange={event => setConfirmKey(event.target.value)}
            />
          )}
        </Box>
      </NekroDialog>
    </Box>
  )
}
