/**
 * 租户详情页（仅平台方）
 *
 * 一个租户 = 一个定制版客户 = 一套独立的实例与资源作用域。平台方在这里处理一个客户时
 * 要同时知道「这个客户是谁」「名下有哪些实例」「谁能登录」，这三件事本就是一屏内的事，
 * 因此本页是单页而不是分 Tab：左边一列客户资料，右边把实例归属与成员并排摊开。
 *
 * 页面此前把这三件事拆成三个 Tab，代价是划一个实例要点四次、而且看不到全貌；
 * 「基本信息」那一屏更是只承载三个输入框和五行只读文本，占满整个视口却几乎没有信息量。
 * 现在只读事实（标识、创建时间、实例数、成员数）全部压进页头，腾出的空间给真正要操作的两块。
 */

import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  Box,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  TextField,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ArrowBackRoundedIcon from '@mui/icons-material/ArrowBackRounded'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import SaveRoundedIcon from '@mui/icons-material/SaveRounded'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import ActionButton from '../../components/common/ActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import { useNotification } from '../../hooks/useNotification'
import { isNotFoundError } from '../../services/api/axios'
import { CARD_VARIANTS, CHIP_VARIANTS } from '../../theme/variants'
import {
  TENANT_QUERY_KEYS,
  isTenantEndpointUnavailable,
  tenantApi,
  tenantPaths,
  type TenantStatus,
} from '../../services/api/tenant'
import TenantInstanceBinder from './components/TenantInstanceBinder'
import TenantMemberTable from './components/TenantMemberTable'

function formatDateTime(value: string | null, fallback: string): string {
  if (!value) return fallback
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return fallback
  return new Date(timestamp).toLocaleString()
}

/** 页头里的一条只读事实。这些以前各占一整行，现在挤成一行摆开 */
function HeadFact({ label, value }: { label: string; value: string }) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 0.5, minWidth: 0 }}>
      <Typography variant="caption" color="text.secondary" noWrap>
        {label}
      </Typography>
      <Typography variant="caption" fontWeight={600} noWrap>
        {value}
      </Typography>
    </Box>
  )
}

export default function TenantDetailPage() {
  const navigate = useNavigate()
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('tenant')
  const { t: tCommon } = useTranslation('common')
  const params = useParams<{ tenantId: string; tab?: string }>()
  const tenantId = Number(params.tenantId)

  const [draft, setDraft] = useState<{
    display_name: string
    contact: string
    note: string
  } | null>(null)
  const [suspendOpen, setSuspendOpen] = useState(false)
  const [confirmName, setConfirmName] = useState('')

  // 三个 Tab 合并成单页后，旧链接（/overview /members /instances）仍会落进来，
  // 收敛回干净地址，免得书签与分享链接一直带着一段已经没有意义的路径
  const legacyTab = params.tab
  useEffect(() => {
    if (legacyTab && Number.isInteger(tenantId) && tenantId > 0) {
      navigate(tenantPaths.tenantDetail(tenantId), { replace: true })
    }
  }, [legacyTab, tenantId, navigate])

  const tenantQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.tenant(tenantId),
    queryFn: () => tenantApi.getTenant(tenantId),
    enabled: Number.isInteger(tenantId) && tenantId > 0,
    staleTime: 30_000,
    retry: false,
  })

  const tenant = tenantQuery.data

  const updateMutation = useMutation({
    mutationFn: (payload: {
      display_name?: string
      contact?: string
      note?: string
      status?: TenantStatus
    }) => tenantApi.updateTenant(tenantId, payload),
    onSuccess: () => {
      notification.success(t('detail.saveSuccess'))
      setDraft(null)
      setSuspendOpen(false)
      setConfirmName('')
      void queryClient.invalidateQueries({ queryKey: TENANT_QUERY_KEYS.tenant(tenantId) })
      void queryClient.invalidateQueries({ queryKey: TENANT_QUERY_KEYS.tenants })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const statusColor = (status: TenantStatus): string => {
    if (status === 'active') return theme.palette.success.main
    if (status === 'suspended') return theme.palette.warning.main
    return theme.palette.text.secondary
  }

  const isEndpointMissing = tenantQuery.isError && isTenantEndpointUnavailable(tenantQuery.error)
  const effectiveDraft = useMemo(
    () =>
      draft ?? {
        display_name: tenant?.display_name ?? '',
        contact: tenant?.contact ?? '',
        note: tenant?.note ?? '',
      },
    [draft, tenant]
  )
  const isDirty =
    draft !== null &&
    tenant !== undefined &&
    (draft.display_name !== tenant.display_name ||
      draft.contact !== tenant.contact ||
      draft.note !== tenant.note)

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 2.5 },
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 1.5,
        overflow: 'hidden',
        boxSizing: 'border-box',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, flexWrap: 'wrap' }}>
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<ArrowBackRoundedIcon />}
          onClick={() => navigate(tenantPaths.tenantList())}
        >
          {t('detail.backToList')}
        </ActionButton>
        <Box sx={{ minWidth: 0, display: 'flex', alignItems: 'baseline', gap: 1 }}>
          <Typography variant="h6" fontWeight={700} noWrap>
            {tenant ? tenant.display_name || tenant.name : '—'}
          </Typography>
          {tenant && (
            <Typography variant="caption" color="text.secondary" noWrap>
              {tenant.name}
            </Typography>
          )}
        </Box>
        {tenant && (
          <>
            <Chip
              size="small"
              label={t(`status.${tenant.status}`)}
              sx={CHIP_VARIANTS.getCustomColorChip(statusColor(tenant.status), true)}
            />
            {/* 只读事实压进页头，不再单独占一张卡片 */}
            <Box
              sx={{
                display: 'flex',
                alignItems: 'baseline',
                gap: 1.5,
                flexWrap: 'wrap',
                minWidth: 0,
              }}
            >
              <HeadFact
                label={t('detail.fields.instanceCount')}
                value={String(tenant.instance_count)}
              />
              <HeadFact
                label={t('detail.fields.memberCount')}
                value={String(tenant.member_count)}
              />
              <HeadFact
                label={t('detail.fields.createTime')}
                value={formatDateTime(tenant.create_time, '—')}
              />
            </Box>
            <Box sx={{ flex: 1 }} />
            <ActionButton
              tone={tenant.status === 'suspended' ? 'primary' : 'danger'}
              size="small"
              startIcon={
                tenant.status === 'suspended' ? <PlayArrowRoundedIcon /> : <StopRoundedIcon />
              }
              disabled={tenant.status === 'archived' || updateMutation.isPending}
              onClick={() => {
                if (tenant.status === 'suspended') {
                  updateMutation.mutate({ status: 'active' })
                  return
                }
                setConfirmName('')
                setSuspendOpen(true)
              }}
            >
              {tenant.status === 'suspended' ? t('actions.resume') : t('actions.suspend')}
            </ActionButton>
          </>
        )}
      </Box>

      {isEndpointMissing && (
        <Alert severity="info" variant="outlined">
          <Typography variant="subtitle2" fontWeight={700}>
            {t('unavailable.title')}
          </Typography>
          <Typography variant="body2">{t('unavailable.tenantDetail')}</Typography>
        </Alert>
      )}

      {/* 与 `TenantTeamPage` 同一条判据（同一个缺陷的第二处实现）：越权/不存在是 404，
          「租户不存在或已被归档」只在 404 下成立；403 / 5xx 走统一失败态 */}
      {tenantQuery.isError &&
        !isEndpointMissing &&
        (isNotFoundError(tenantQuery.error) ? (
          <Alert severity="error" variant="outlined">
            {t('detail.notFound')}
          </Alert>
        ) : (
          <QueryErrorState
            compact
            error={tenantQuery.error}
            onRetry={() => void tenantQuery.refetch()}
          />
        ))}

      {tenant?.status === 'suspended' && (
        <Alert severity="warning" variant="outlined" sx={{ py: 0.25 }}>
          {t('detail.suspendedBanner')}
        </Alert>
      )}

      <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        {tenantQuery.isLoading ? (
          <Box
            sx={{ minHeight: 240, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          >
            <CircularProgress />
          </Box>
        ) : tenant ? (
          <Box
            sx={{
              display: 'grid',
              // 客户资料是低频编辑，固定一窄列；实例归属与成员是主战场，吃掉剩下的宽度
              gridTemplateColumns: { xs: '1fr', lg: 'minmax(280px, 340px) minmax(0, 1fr)' },
              gap: 1.5,
              alignItems: 'start',
            }}
          >
            <Card sx={CARD_VARIANTS.default.styles}>
              <CardContent sx={{ '&:last-child': { pb: 2 } }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
                  <Typography variant="subtitle2" fontWeight={700} sx={{ flex: 1 }}>
                    {t('detail.baseInfo')}
                  </Typography>
                  <ActionButton
                    tone="primary"
                    size="small"
                    startIcon={<SaveRoundedIcon />}
                    disabled={!isDirty || updateMutation.isPending}
                    onClick={() =>
                      updateMutation.mutate({
                        display_name: effectiveDraft.display_name.trim(),
                        contact: effectiveDraft.contact.trim(),
                        note: effectiveDraft.note.trim(),
                      })
                    }
                  >
                    {t('actions.save')}
                  </ActionButton>
                </Box>
                <Divider sx={{ mb: 1.5 }} />
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                  <TextField
                    size="small"
                    fullWidth
                    label={t('detail.fields.displayName')}
                    value={effectiveDraft.display_name}
                    onChange={event =>
                      setDraft({ ...effectiveDraft, display_name: event.target.value })
                    }
                  />
                  <TextField
                    size="small"
                    fullWidth
                    label={t('detail.fields.contact')}
                    value={effectiveDraft.contact}
                    onChange={event => setDraft({ ...effectiveDraft, contact: event.target.value })}
                  />
                  <TextField
                    size="small"
                    fullWidth
                    multiline
                    minRows={3}
                    label={t('detail.fields.note')}
                    value={effectiveDraft.note}
                    onChange={event => setDraft({ ...effectiveDraft, note: event.target.value })}
                  />
                  <Typography variant="caption" color="text.secondary">
                    {t('detail.nameImmutable', { name: tenant.name })}
                  </Typography>
                </Box>
              </CardContent>
            </Card>

            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 }}>
              <Card sx={CARD_VARIANTS.default.styles}>
                <CardContent sx={{ '&:last-child': { pb: 2 } }}>
                  <TenantInstanceBinder tenantId={tenantId} />
                </CardContent>
              </Card>
              <Card sx={CARD_VARIANTS.default.styles}>
                <CardContent sx={{ '&:last-child': { pb: 2 } }}>
                  <TenantMemberTable tenantId={tenantId} />
                </CardContent>
              </Card>
            </Box>
          </Box>
        ) : null}
      </Box>

      <NekroDialog
        open={suspendOpen}
        onClose={() => setSuspendOpen(false)}
        title={t('suspendDialog.title')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setSuspendOpen(false)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={confirmName.trim() !== tenant?.name || updateMutation.isPending}
              onClick={() => updateMutation.mutate({ status: 'suspended' })}
            >
              {t('suspendDialog.submit')}
            </ActionButton>
          </>
        }
      >
        {tenant && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, pt: 1 }}>
            <Typography variant="body2">
              {t('suspendDialog.description', { tenant: tenant.display_name || tenant.name })}
            </Typography>
            <Box component="ul" sx={{ pl: 2.5, m: 0, display: 'grid', gap: 0.5 }}>
              <Typography component="li" variant="body2" color="error.main">
                {t('suspendDialog.consequenceLogin')}
              </Typography>
              <Typography component="li" variant="body2" color="error.main">
                {t('suspendDialog.consequenceInstance', { instanceCount: tenant.instance_count })}
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
              placeholder={tenant.name}
              value={confirmName}
              error={Boolean(confirmName) && confirmName.trim() !== tenant.name}
              helperText={
                confirmName && confirmName.trim() !== tenant.name
                  ? t('suspendDialog.confirmMismatch')
                  : t('suspendDialog.confirmHelper', { name: tenant.name })
              }
              onChange={event => setConfirmName(event.target.value)}
            />
          </Box>
        )}
      </NekroDialog>
    </Box>
  )
}
