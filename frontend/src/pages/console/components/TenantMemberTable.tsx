/**
 * 租户成员管理
 *
 * 平台方在这里为客户开后台账号：租户管理员可管理自己名下的实例与资源，
 * 租户成员只读。账号一旦创建，密码不会再次展示。
 *
 * 两个挂载点共用本组件：平台方的 `/console/tenants/{id}`，与租户自助的 `/console/team`。
 * 后端 `POST/DELETE /console/tenants/{id}/members` 用的是 `require_tenant_admin()`，
 * 因此**租户管理员本来就写得动自己的成员**，本组件不再假设调用方一定是平台方。
 *
 * 界面上有两条「后端已经会拒绝，但按钮不该摆出来」的判定
 * （判定在后端是活的，前端只负责别递一个必然失败的按钮）：
 *
 * - **只读身份**（`tenant_user`）：`require_tenant_admin()` 直接 403，添加/移除全部隐藏；
 * - **移除自己**：后端 `remove_tenant_member` 明确 `不能移除自己` → 400。
 *   租户自助场景下这一条是天天会碰到的（成员表里第一行往往就是登录者本人）。
 */

import { useState } from 'react'
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  MenuItem,
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
import PersonAddAlt1RoundedIcon from '@mui/icons-material/PersonAddAlt1Rounded'
import PersonRemoveRoundedIcon from '@mui/icons-material/PersonRemoveRounded'
import ActionButton from '../../../components/common/ActionButton'
import IconActionButton from '../../../components/common/IconActionButton'
import NekroDialog from '../../../components/common/NekroDialog'
import QueryErrorState from '../../../components/common/QueryErrorState'
import { SimpleTooltip } from '../../../components/common/ThemedTooltip'
import { useIdentity } from '../../../hooks/useIdentity'
import { useNotification } from '../../../hooks/useNotification'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../../theme/variants'
import {
  TENANT_QUERY_KEYS,
  isTenantEndpointUnavailable,
  tenantApi,
  type TenantMemberInfo,
  type TenantRole,
} from '../../../services/api/tenant'

const MIN_PASSWORD_LENGTH = 8

/** 可分配给成员的角色；平台方角色不通过租户成员接口下发 */
const ASSIGNABLE_ROLES: TenantRole[] = ['tenant_admin', 'tenant_user']

export default function TenantMemberTable({ tenantId }: { tenantId: number }) {
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('tenant')
  const { t: tCommon } = useTranslation('common')
  // 只读身份写不动成员（后端 `require_tenant_admin()` 403），登录者也移不掉自己（后端 400）。
  // 两条都以后端为准，这里只决定按钮画不画。
  const { isReadOnly, identity } = useIdentity()
  const currentUserId = identity?.user_id ?? null
  const canManageMembers = !isReadOnly

  const [createOpen, setCreateOpen] = useState(false)
  const [removeTarget, setRemoveTarget] = useState<TenantMemberInfo | null>(null)
  const [draftUsername, setDraftUsername] = useState('')
  const [draftPassword, setDraftPassword] = useState('')
  const [draftRole, setDraftRole] = useState<TenantRole>('tenant_admin')

  const membersQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.members(tenantId),
    queryFn: () => tenantApi.listMembers(tenantId),
    staleTime: 30_000,
    retry: false,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: TENANT_QUERY_KEYS.members(tenantId) })
    void queryClient.invalidateQueries({ queryKey: TENANT_QUERY_KEYS.tenant(tenantId) })
    void queryClient.invalidateQueries({ queryKey: TENANT_QUERY_KEYS.tenants })
  }

  const createMutation = useMutation({
    mutationFn: () =>
      tenantApi.createMember(tenantId, {
        username: draftUsername.trim(),
        password: draftPassword,
        role: draftRole,
      }),
    onSuccess: () => {
      notification.success(t('members.createSuccess'))
      setCreateOpen(false)
      setDraftUsername('')
      setDraftPassword('')
      setDraftRole('tenant_admin')
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const removeMutation = useMutation({
    mutationFn: (userId: number) => tenantApi.removeMember(tenantId, userId),
    onSuccess: () => {
      notification.success(t('members.removeSuccess'))
      setRemoveTarget(null)
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const isEndpointMissing = membersQuery.isError && isTenantEndpointUnavailable(membersQuery.error)
  const members = membersQuery.data ?? []
  const canSubmitCreate =
    draftUsername.trim().length > 0 && draftPassword.length >= MIN_PASSWORD_LENGTH

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.25, minHeight: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <Typography variant="subtitle2" fontWeight={700} sx={{ flexShrink: 0 }}>
          {t('members.title')}
        </Typography>
        <SimpleTooltip title={t('members.description')}>
          <Chip
            size="small"
            label={t('members.count', { count: members.length })}
            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.primary.main, true)}
          />
        </SimpleTooltip>
        <Box sx={{ flex: 1 }} />
        {canManageMembers && (
          <ActionButton
            tone="primary"
            size="small"
            startIcon={<PersonAddAlt1RoundedIcon />}
            disabled={isEndpointMissing}
            onClick={() => setCreateOpen(true)}
          >
            {t('actions.addMember')}
          </ActionButton>
        )}
      </Box>

      {isEndpointMissing && (
        <Alert severity="info" variant="outlined">
          <Typography variant="subtitle2" fontWeight={700}>
            {t('unavailable.title')}
          </Typography>
          <Typography variant="body2">{t('unavailable.members')}</Typography>
        </Alert>
      )}

      {/* ⚠️ 同 `TenantListPage`：这条旧横幅**刻意删掉**。下面表格区已经用
          `QueryErrorState` 接管了同一个条件（`isError && !isEndpointMissing`），
          两条一起画时 403 会同时显示「成员列表加载失败 + 重试」与「你无权查看」，
          前者那个重试按钮点了永远还是 403。本组件嵌在 `/console/team` 里，
          那是只读租户成员唯一够得着的总后台页 —— 说两遍且互相矛盾的代价最高。 */}

      <Box sx={UNIFIED_TABLE_STYLES.tableContentContainer as SxProps<Theme>}>
        <Box sx={{ ...(UNIFIED_TABLE_STYLES.tableViewport as object), maxHeight: 260 }}>
          {membersQuery.isLoading ? (
            <Box
              sx={{
                minHeight: 140,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <CircularProgress />
            </Box>
          ) : membersQuery.isError && !isEndpointMissing ? (
            /* 失败时不要再画「该租户还没有成员」：错误横幅与这句空态会同时出现，
               而后半句是编的。只读成员打开「我的团队」时这条最容易被撞到 */
            <QueryErrorState
              compact
              error={membersQuery.error}
              onRetry={() => void membersQuery.refetch()}
            />
          ) : members.length === 0 ? (
            <Box
              sx={{
                minHeight: 140,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 0.5,
              }}
            >
              <Typography variant="subtitle2">{t('members.empty')}</Typography>
              <Typography variant="body2" color="text.secondary">
                {t('members.emptyHint')}
              </Typography>
            </Box>
          ) : (
            <Table stickyHeader size="small">
              <TableHead>
                <TableRow>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('members.columns.username')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('members.columns.role')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('members.columns.state')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="center">
                    {t('members.columns.actions')}
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {members.map(member => (
                  <TableRow
                    key={member.user_id}
                    hover
                    sx={UNIFIED_TABLE_STYLES.row as SxProps<Theme>}
                  >
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Typography variant="body2" fontWeight={600}>
                        {member.username}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Chip
                        size="small"
                        label={t(`role.${member.role}`)}
                        sx={CHIP_VARIANTS.getCustomColorChip(
                          member.role === 'tenant_admin'
                            ? theme.palette.primary.main
                            : theme.palette.text.secondary,
                          true
                        )}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Typography
                        variant="body2"
                        color={member.is_active ? 'text.primary' : 'text.secondary'}
                      >
                        {member.is_active ? t('members.state.active') : t('members.state.inactive')}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="center">
                      {canManageMembers ? (
                        <SimpleTooltip
                          title={
                            member.user_id === currentUserId
                              ? t('members.cannotRemoveSelf')
                              : t('actions.removeMember')
                          }
                        >
                          <span>
                            <IconActionButton
                              size="small"
                              tone="danger"
                              disabled={
                                removeMutation.isPending || member.user_id === currentUserId
                              }
                              onClick={() => setRemoveTarget(member)}
                            >
                              <PersonRemoveRoundedIcon fontSize="small" />
                            </IconActionButton>
                          </span>
                        </SimpleTooltip>
                      ) : (
                        <Typography variant="caption" color="text.secondary">
                          —
                        </Typography>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Box>
      </Box>

      <NekroDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title={t('members.createDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setCreateOpen(false)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="primary"
              disabled={!canSubmitCreate || createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              {t('members.createDialog.submit')}
            </ActionButton>
          </>
        }
      >
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            {t('members.createDialog.description')}
          </Typography>
          <TextField
            size="small"
            fullWidth
            autoComplete="off"
            label={t('members.createDialog.username')}
            value={draftUsername}
            onChange={event => setDraftUsername(event.target.value)}
          />
          <TextField
            size="small"
            fullWidth
            type="password"
            autoComplete="new-password"
            label={t('members.createDialog.password')}
            value={draftPassword}
            error={draftPassword.length > 0 && draftPassword.length < MIN_PASSWORD_LENGTH}
            helperText={
              draftPassword.length > 0 && draftPassword.length < MIN_PASSWORD_LENGTH
                ? t('members.createDialog.passwordTooShort')
                : t('members.createDialog.passwordHelper')
            }
            onChange={event => setDraftPassword(event.target.value)}
          />
          <TextField
            select
            size="small"
            fullWidth
            label={t('members.createDialog.role')}
            value={draftRole}
            helperText={
              draftRole === 'tenant_admin'
                ? t('members.createDialog.roleAdminHint')
                : t('members.createDialog.roleUserHint')
            }
            onChange={event => setDraftRole(event.target.value as TenantRole)}
          >
            {ASSIGNABLE_ROLES.map(role => (
              <MenuItem key={role} value={role}>
                {t(`role.${role}`)}
              </MenuItem>
            ))}
          </TextField>
        </Box>
      </NekroDialog>

      <NekroDialog
        open={Boolean(removeTarget)}
        onClose={() => setRemoveTarget(null)}
        title={t('members.removeDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setRemoveTarget(null)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={removeMutation.isPending}
              onClick={() => {
                if (!removeTarget) return
                removeMutation.mutate(removeTarget.user_id)
              }}
            >
              {t('members.removeDialog.submit')}
            </ActionButton>
          </>
        }
      >
        {removeTarget && (
          <Typography variant="body2" sx={{ pt: 1 }}>
            {t('members.removeDialog.description', { username: removeTarget.username })}
          </Typography>
        )}
      </NekroDialog>
    </Box>
  )
}
