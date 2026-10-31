/**
 * 租户列表页（仅平台方）
 *
 * 平台方在这里把定制版客户划成一个个独立的作用域。零租户时本页仍可直达，
 * 用于创建第一个租户；侧边栏入口则在没有任何租户时不出现，
 * 让单租户部署的使用体验与多租户改造前完全一致。
 */

import { useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Alert,
  Box,
  Chip,
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
import Inventory2OutlinedIcon from '@mui/icons-material/Inventory2Outlined'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import SearchField from '../../components/common/SearchField'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import { useNotification } from '../../hooks/useNotification'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  TENANT_NAME_PATTERN,
  TENANT_QUERY_KEYS,
  TENANT_STATUSES,
  isTenantEndpointUnavailable,
  tenantApi,
  tenantPaths,
  type TenantInfo,
  type TenantStatus,
} from '../../services/api/tenant'

type DialogMode = 'create' | 'suspend' | 'resume' | 'archive'

const isTenantStatus = (value: string | null): value is TenantStatus =>
  !!value && TENANT_STATUSES.includes(value as TenantStatus)

function formatDateTime(value: string | null, fallback: string): string {
  if (!value) return fallback
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return fallback
  return new Date(timestamp).toLocaleString()
}

export default function TenantListPage() {
  const navigate = useNavigate()
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('tenant')
  const { t: tCommon } = useTranslation('common')

  // 搜索与筛选属于「刷新后应保留」的状态，统一进 query
  const [searchParams, setSearchParams] = useSearchParams()
  const statusFilter = isTenantStatus(searchParams.get('status'))
    ? (searchParams.get('status') as TenantStatus)
    : 'all'
  const search = searchParams.get('search') ?? ''

  const [dialogMode, setDialogMode] = useState<DialogMode | null>(null)
  const [actionTarget, setActionTarget] = useState<TenantInfo | null>(null)
  const [confirmName, setConfirmName] = useState('')
  const [draftName, setDraftName] = useState('')
  const [draftDisplayName, setDraftDisplayName] = useState('')
  const [draftContact, setDraftContact] = useState('')
  const [draftNote, setDraftNote] = useState('')

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

  const tenantsQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.tenants,
    queryFn: () => tenantApi.listTenants(),
    staleTime: 30_000,
    retry: false,
  })

  const closeDialog = () => {
    setDialogMode(null)
    setActionTarget(null)
    setConfirmName('')
  }

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: TENANT_QUERY_KEYS.tenants })
  }

  const createMutation = useMutation({
    mutationFn: () =>
      tenantApi.createTenant({
        name: draftName.trim(),
        display_name: draftDisplayName.trim(),
        contact: draftContact.trim(),
        note: draftNote.trim(),
      }),
    onSuccess: tenant => {
      notification.success(tCommon('messages.createSuccess'))
      closeDialog()
      setDraftName('')
      setDraftDisplayName('')
      setDraftContact('')
      setDraftNote('')
      invalidate()
      navigate(tenantPaths.tenantDetail(tenant.id))
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const statusMutation = useMutation({
    mutationFn: (payload: { tenantId: number; status: TenantStatus }) =>
      tenantApi.updateTenant(payload.tenantId, { status: payload.status }),
    onSuccess: () => {
      notification.success(tCommon('messages.updateSuccess'))
      closeDialog()
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const archiveMutation = useMutation({
    mutationFn: (tenantId: number) => tenantApi.archiveTenant(tenantId),
    onSuccess: () => {
      notification.success(tCommon('messages.deleteSuccess'))
      closeDialog()
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const tenants = useMemo(() => {
    const items = tenantsQuery.data?.items ?? []
    const keyword = search.trim().toLowerCase()

    return items.filter(tenant => {
      if (statusFilter !== 'all' && tenant.status !== statusFilter) return false
      if (!keyword) return true
      return [tenant.name, tenant.display_name, tenant.contact].some(value =>
        value.toLowerCase().includes(keyword)
      )
    })
  }, [tenantsQuery.data, search, statusFilter])

  const statusOptions = useMemo(
    () => [
      { value: 'all', label: t('status.all') },
      ...TENANT_STATUSES.map(status => ({ value: status, label: t(`status.${status}`) })),
    ],
    [t]
  )

  const statusColor = (status: TenantStatus): string => {
    if (status === 'active') return theme.palette.success.main
    if (status === 'suspended') return theme.palette.warning.main
    return theme.palette.text.secondary
  }

  const isEndpointMissing = tenantsQuery.isError && isTenantEndpointUnavailable(tenantsQuery.error)
  const trimmedName = draftName.trim()
  const isNameValid = TENANT_NAME_PATTERN.test(trimmedName)
  const hasTenantsLoaded = (tenantsQuery.data?.items.length ?? 0) > 0

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
            {t('list.title')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('list.subtitle')}
          </Typography>
        </Box>
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
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<RefreshRoundedIcon />}
          onClick={() => void tenantsQuery.refetch()}
          sx={{ ml: 'auto' }}
        >
          {t('actions.refresh')}
        </ActionButton>
        <ActionButton
          tone="primary"
          size="small"
          startIcon={<AddRoundedIcon />}
          disabled={isEndpointMissing}
          onClick={() => {
            setDialogMode('create')
            setConfirmName('')
          }}
        >
          {t('actions.create')}
        </ActionButton>
      </Box>

      {isEndpointMissing && (
        <Alert severity="info" variant="outlined">
          <Typography variant="subtitle2" fontWeight={700}>
            {t('unavailable.title')}
          </Typography>
          <Typography variant="body2">{t('unavailable.tenants')}</Typography>
        </Alert>
      )}

      {/* ⚠️ 这里**刻意不再画**一条「租户列表加载失败 + 重试」的横幅。
          表格区已经由 `QueryErrorState` 接管同一个条件，两条一起画的后果是：
          403 之下用户同时看到「加载失败（可重试）」与「你无权查看（不给重试）」两种说法，
          而那个重试按钮点了永远是同一个 403 —— 正是本轮要消灭的「点不动的按钮」。
          一个失败只说一次，由下面那个组件统一说。 */}

      <Box sx={UNIFIED_TABLE_STYLES.tableContentContainer as SxProps<Theme>}>
        <Box sx={UNIFIED_TABLE_STYLES.tableViewport as SxProps<Theme>}>
          {tenantsQuery.isLoading ? (
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
          ) : tenantsQuery.isError ? (
            /* 失败时不要再画「还没有租户 / 创建第一个租户」：上面那条错误横幅与这句
               空态会同时出现，读起来像"请求失败了，另外你确实一个租户都没有"——
               后半句是编的。空态只在**取到了、真的是空**时才成立 */
            <QueryErrorState
              error={tenantsQuery.error}
              onRetry={() => void tenantsQuery.refetch()}
            />
          ) : tenants.length === 0 ? (
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
              <Typography variant="subtitle1">
                {hasTenantsLoaded ? t('list.noResults') : t('list.empty')}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {hasTenantsLoaded ? t('list.noResultsHint') : t('list.emptyHint')}
              </Typography>
              {!hasTenantsLoaded && !isEndpointMissing && (
                <ActionButton
                  tone="primary"
                  size="small"
                  startIcon={<AddRoundedIcon />}
                  onClick={() => setDialogMode('create')}
                >
                  {t('actions.create')}
                </ActionButton>
              )}
            </Box>
          ) : (
            <Table stickyHeader sx={UNIFIED_TABLE_STYLES.getTableBase(false, false)}>
              <TableHead>
                <TableRow>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('list.columns.tenant')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('list.columns.status')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="right">
                    {t('list.columns.instances')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="right">
                    {t('list.columns.members')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('list.columns.contact')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('list.columns.createTime')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="center">
                    {t('list.columns.actions')}
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {tenants.map(tenant => (
                  <TableRow
                    key={tenant.id}
                    hover
                    // 整行可点进详情：详情是这张表上最常做的动作，不该只有一个 24px 的箭头能命中
                    onClick={() => navigate(tenantPaths.tenantDetail(tenant.id))}
                    sx={{ ...(UNIFIED_TABLE_STYLES.row as object), cursor: 'pointer' }}
                  >
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Typography variant="body2" fontWeight={600} noWrap>
                        {tenant.display_name || tenant.name}
                      </Typography>
                      <Typography variant="caption" color="text.secondary" noWrap>
                        {tenant.name}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Chip
                        size="small"
                        label={t(`status.${tenant.status}`)}
                        sx={CHIP_VARIANTS.getCustomColorChip(statusColor(tenant.status), true)}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="right">
                      {/* 零实例的租户是「建了但还没交付」，调淡让它在长列表里自己浮出来 */}
                      <Typography
                        variant="body2"
                        color={tenant.instance_count > 0 ? 'text.primary' : 'text.disabled'}
                      >
                        {tenant.instance_count}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="right">
                      <Typography
                        variant="body2"
                        color={tenant.member_count > 0 ? 'text.primary' : 'text.disabled'}
                      >
                        {tenant.member_count}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      {tenant.contact || '—'}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Typography variant="caption" color="text.secondary">
                        {formatDateTime(tenant.create_time, '—')}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="center">
                      {/* 行级点击会进详情，操作列里的按钮各有各的语义，必须拦住冒泡 */}
                      <Box
                        sx={{ display: 'flex', gap: 0.5, justifyContent: 'center' }}
                        onClick={event => event.stopPropagation()}
                      >
                        <SimpleTooltip
                          title={
                            tenant.status === 'suspended' ? t('actions.resume') : t('actions.suspend')
                          }
                        >
                          <span>
                            <IconActionButton
                              size="small"
                              tone={tenant.status === 'suspended' ? 'primary' : 'subtle'}
                              disabled={tenant.status === 'archived' || statusMutation.isPending}
                              onClick={() => {
                                setActionTarget(tenant)
                                setConfirmName('')
                                setDialogMode(tenant.status === 'suspended' ? 'resume' : 'suspend')
                              }}
                            >
                              {tenant.status === 'suspended' ? (
                                <PlayArrowRoundedIcon fontSize="small" />
                              ) : (
                                <StopRoundedIcon fontSize="small" />
                              )}
                            </IconActionButton>
                          </span>
                        </SimpleTooltip>
                        <SimpleTooltip title={t('actions.archive')}>
                          <span>
                            <IconActionButton
                              size="small"
                              tone="danger"
                              disabled={tenant.status === 'archived'}
                              onClick={() => {
                                setActionTarget(tenant)
                                setDialogMode('archive')
                              }}
                            >
                              <Inventory2OutlinedIcon fontSize="small" />
                            </IconActionButton>
                          </span>
                        </SimpleTooltip>
                        <SimpleTooltip title={t('actions.detail')}>
                          <span>
                            <IconActionButton
                              size="small"
                              tone="primary"
                              onClick={() => navigate(tenantPaths.tenantDetail(tenant.id))}
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
        open={dialogMode === 'create'}
        onClose={closeDialog}
        title={t('createDialog.title')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={closeDialog}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="primary"
              disabled={!isNameValid || createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              {t('createDialog.submit')}
            </ActionButton>
          </>
        }
      >
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            {t('createDialog.description')}
          </Typography>
          <TextField
            size="small"
            fullWidth
            autoComplete="off"
            label={t('createDialog.name')}
            value={draftName}
            error={Boolean(trimmedName) && !isNameValid}
            helperText={
              trimmedName && !isNameValid
                ? t('createDialog.nameInvalid')
                : t('createDialog.nameHelper')
            }
            onChange={event => setDraftName(event.target.value)}
          />
          <TextField
            size="small"
            fullWidth
            label={t('createDialog.displayName')}
            helperText={t('createDialog.displayNameHelper')}
            value={draftDisplayName}
            onChange={event => setDraftDisplayName(event.target.value)}
          />
          <TextField
            size="small"
            fullWidth
            label={t('createDialog.contact')}
            value={draftContact}
            onChange={event => setDraftContact(event.target.value)}
          />
          <TextField
            size="small"
            fullWidth
            multiline
            minRows={2}
            label={t('createDialog.note')}
            value={draftNote}
            onChange={event => setDraftNote(event.target.value)}
          />
        </Box>
      </NekroDialog>

      <NekroDialog
        open={dialogMode === 'suspend'}
        onClose={closeDialog}
        title={t('suspendDialog.title')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={closeDialog}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={
                confirmName.trim() !== actionTarget?.name || statusMutation.isPending
              }
              onClick={() => {
                if (!actionTarget) return
                statusMutation.mutate({ tenantId: actionTarget.id, status: 'suspended' })
              }}
            >
              {t('suspendDialog.submit')}
            </ActionButton>
          </>
        }
      >
        {actionTarget && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, pt: 1 }}>
            <Typography variant="body2">
              {t('suspendDialog.description', {
                tenant: actionTarget.display_name || actionTarget.name,
              })}
            </Typography>
            <Box component="ul" sx={{ pl: 2.5, m: 0, display: 'grid', gap: 0.5 }}>
              <Typography component="li" variant="body2" color="error.main">
                {t('suspendDialog.consequenceLogin')}
              </Typography>
              <Typography component="li" variant="body2" color="error.main">
                {t('suspendDialog.consequenceInstance', {
                  instanceCount: actionTarget.instance_count,
                })}
              </Typography>
              <Typography component="li" variant="body2" color="text.secondary">
                {t('suspendDialog.consequenceReversible')}
              </Typography>
            </Box>
            <TextField
              size="small"
              fullWidth
              autoComplete="off"
              label={t('suspendDialog.confirmLabel')}
              placeholder={actionTarget.name}
              value={confirmName}
              error={Boolean(confirmName) && confirmName.trim() !== actionTarget.name}
              helperText={
                confirmName && confirmName.trim() !== actionTarget.name
                  ? t('suspendDialog.confirmMismatch')
                  : t('suspendDialog.confirmHelper', { name: actionTarget.name })
              }
              onChange={event => setConfirmName(event.target.value)}
            />
          </Box>
        )}
      </NekroDialog>

      <NekroDialog
        open={dialogMode === 'resume'}
        onClose={closeDialog}
        title={t('resumeDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="ghost" onClick={closeDialog}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="primary"
              disabled={statusMutation.isPending}
              onClick={() => {
                if (!actionTarget) return
                statusMutation.mutate({ tenantId: actionTarget.id, status: 'active' })
              }}
            >
              {t('resumeDialog.submit')}
            </ActionButton>
          </>
        }
      >
        {actionTarget && (
          <Typography variant="body2" sx={{ pt: 1 }}>
            {t('resumeDialog.description', {
              tenant: actionTarget.display_name || actionTarget.name,
            })}
          </Typography>
        )}
      </NekroDialog>

      <NekroDialog
        open={dialogMode === 'archive'}
        onClose={closeDialog}
        title={t('archiveDialog.title')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={closeDialog}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={archiveMutation.isPending}
              onClick={() => {
                if (!actionTarget) return
                archiveMutation.mutate(actionTarget.id)
              }}
            >
              {t('archiveDialog.submit')}
            </ActionButton>
          </>
        }
      >
        {actionTarget && (
          <Typography variant="body2" sx={{ pt: 1 }}>
            {t('archiveDialog.description', {
              tenant: actionTarget.display_name || actionTarget.name,
            })}
          </Typography>
        )}
      </NekroDialog>
    </Box>
  )
}
