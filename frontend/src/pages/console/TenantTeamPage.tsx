/**
 * 我的团队（租户自助）
 *
 * ## 为什么单独开一页，而不是把 `/console/tenants` 放开
 *
 * 后端 `routers/console/tenants.py` 的权限是**逐端点**分层的：
 *
 * | 端点 | 守卫 | 租户 |
 * |---|---|---|
 * | `GET /tenants` | `require_platform_owner()` | 403 |
 * | `POST /tenants`、`PATCH/DELETE /tenants/{id}`、`PUT /tenants/{id}/instances` | `require_platform_owner()` | 403 |
 * | `GET /tenants/{id}`、`GET /tenants/{id}/members` | `assert_tenant_accessible` | 只看得到自己（他人 404） |
 * | `POST /tenants/{id}/members`、`DELETE /tenants/{id}/members/{uid}` | `require_tenant_admin()` | **可写** |
 *
 * 也就是说，「租户自管成员」这条链路后端早就做完并测过了，只是前端把整条
 * `/console/tenants` 路径关进了 `PLATFORM_ONLY_PATHS`，于是那两条 `require_tenant_admin()`
 * 分支在产品上 0 可达 —— 测试在守护一段永远跑不到的代码。
 *
 * 直接放开 `/console/tenants` 不行：`TenantListPage` 是**租户列表**（把平台方的客户清单
 * 端到端摊开），`TenantDetailPage` 还带着客户资料编辑、停用、实例划归三块平台方专属写操作。
 * 本页因此只取那张能开的切片：**自己这一个租户的只读事实 + 成员增删**。
 *
 * ## 这里刻意没有的东西
 *
 * - 没有租户列表、没有「新建租户」：`POST /tenants` 是平台方专属；
 * - 没有客户资料编辑、停用/恢复、实例划归：同上，全是 `require_platform_owner()`；
 * - 没有配额：配额是计费边界，租户在 `/console/quota` 里看得到自己实例的用量，
 *   平台上限（`editable_ceiling`）由后端下发、租户恒为 false；
 * - 成员角色下拉里没有「平台方」：`TENANT_MAX_PERM_LEVEL = Role.Admin`，
 *   后端 `create_tenant_member` 对 `platform_owner` 直接 400（见 `TenantMemberTable`）。
 *
 * 判定一律以后端为准，本页只负责**不摆出做不到的按钮**。
 */

import { Alert, Box, Card, CardContent, Chip, CircularProgress, Typography, useTheme } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import BusinessRoundedIcon from '@mui/icons-material/BusinessRounded'
import ActionButton from '../../components/common/ActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import { useIdentity } from '../../hooks/useIdentity'
import { isNotFoundError } from '../../services/api/axios'
import { CARD_VARIANTS, CHIP_VARIANTS } from '../../theme/variants'
import {
  TENANT_QUERY_KEYS,
  isTenantEndpointUnavailable,
  tenantApi,
  tenantPaths,
  type TenantStatus,
} from '../../services/api/tenant'
import TenantMemberTable from './components/TenantMemberTable'

function formatDateTime(value: string | null, fallback: string): string {
  if (!value) return fallback
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return fallback
  return new Date(timestamp).toLocaleString()
}

/** 页头里的一条只读事实 */
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

export default function TenantTeamPage() {
  const theme = useTheme()
  const navigate = useNavigate()
  const { t } = useTranslation('tenant')
  // 作用域**只**取自当前身份，不从 URL 参数来：本页没有 `:tenantId` 这一段，
  // 也就没有「把别人的租户号敲进地址栏」这个动作可做。后端的 `assert_tenant_accessible`
  // 仍是最终判据（越权 404），这里是把那条路径连做都做不出来。
  const { tenantId, tenantName, isTenantScoped, isReadOnly, isLoading: isIdentityLoading } = useIdentity()

  const tenantQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.tenant(tenantId ?? 0),
    queryFn: () => tenantApi.getTenant(tenantId as number),
    enabled: tenantId !== null,
    staleTime: 30_000,
    retry: false,
  })

  const tenant = tenantQuery.data
  const isEndpointMissing = tenantQuery.isError && isTenantEndpointUnavailable(tenantQuery.error)

  const statusColor = (status: TenantStatus): string => {
    if (status === 'active') return theme.palette.success.main
    if (status === 'suspended') return theme.palette.warning.main
    return theme.palette.text.secondary
  }

  // 平台方从菜单里看不到本页（`visibility: 'tenantSelfService'`），但 URL 照样敲得进来。
  // 它对平台方不是越权，只是走错门：平台方管成员的入口在租户列表里，指过去即可。
  if (!isIdentityLoading && !isTenantScoped) {
    return (
      <Box sx={{ p: 3, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1.5 }}>
        <Typography variant="subtitle1" fontWeight={700}>
          {t('team.platformTitle')}
        </Typography>
        <Typography variant="body2" color="text.secondary" textAlign="center">
          {t('team.platformHint')}
        </Typography>
        <ActionButton
          tone="primary"
          size="small"
          startIcon={<BusinessRoundedIcon />}
          onClick={() => navigate(tenantPaths.tenantList())}
        >
          {t('team.platformAction')}
        </ActionButton>
      </Box>
    )
  }

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
        <Typography variant="h6" fontWeight={700} noWrap>
          {t('team.title')}
        </Typography>
        <Typography variant="caption" color="text.secondary" noWrap>
          {tenant ? tenant.display_name || tenant.name : tenantName}
        </Typography>
        {tenant && (
          <>
            <Chip
              size="small"
              label={t(`status.${tenant.status}`)}
              sx={CHIP_VARIANTS.getCustomColorChip(statusColor(tenant.status), true)}
            />
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
              <HeadFact label={t('detail.fields.memberCount')} value={String(tenant.member_count)} />
              <HeadFact
                label={t('detail.fields.createTime')}
                value={formatDateTime(tenant.create_time, '—')}
              />
            </Box>
          </>
        )}
      </Box>

      <Typography variant="body2" color="text.secondary">
        {t('team.subtitle')}
      </Typography>

      {isEndpointMissing && (
        <Alert severity="info" variant="outlined">
          <Typography variant="subtitle2" fontWeight={700}>
            {t('unavailable.title')}
          </Typography>
          <Typography variant="body2">{t('unavailable.tenantDetail')}</Typography>
        </Alert>
      )}

      {/* 404 与其余失败必须分开说，判据形态与 `InstanceDetailPage` 同源：
          `assert_tenant_accessible` 的越权/不存在是 404（§1.4），那句「租户不存在或已被归档」
          只有在 404 下才是真话；403 / 5xx 说成「已被归档」会让客户以为自己的租户被停掉了。
          这一页是只读成员唯一够得着的总后台页，说错话的代价最高 */}
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

      {/* 只读身份（`tenant_user`）看得到成员，但写不动：把这条说在前面，
          好过让人点了按钮才收到一句 403 */}
      {isReadOnly && (
        <Alert severity="info" variant="outlined" sx={{ py: 0.25 }}>
          {t('team.readOnlyBanner')}
        </Alert>
      )}

      <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        {isIdentityLoading || (tenantId !== null && tenantQuery.isLoading) ? (
          <Box
            sx={{ minHeight: 240, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          >
            <CircularProgress />
          </Box>
        ) : tenantId === null ? (
          // 声明了租户身份却拿不到 tenant_id：后端 `tenant_role_of` 会把这种脏数据降级成
          // 最小权限，前端只需说清「现在什么都做不了」，而不是拿 0 去请求一个不存在的租户
          <Alert severity="warning" variant="outlined">
            {t('team.noTenant')}
          </Alert>
        ) : (
          <Card sx={CARD_VARIANTS.default.styles}>
            <CardContent sx={{ '&:last-child': { pb: 2 } }}>
              <TenantMemberTable tenantId={tenantId} />
            </CardContent>
          </Card>
        )}
      </Box>
    </Box>
  )
}
