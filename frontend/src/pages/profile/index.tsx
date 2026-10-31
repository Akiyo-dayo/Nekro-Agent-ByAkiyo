import { useState } from 'react'
import {
  Box,
  TextField,
  Typography,
  Stack,
  Card,
  CardContent,
  Divider,
  Alert,
} from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { authApi } from '../../services/api/auth'
import { CARD_VARIANTS, INPUT_VARIANTS, BORDER_RADIUS } from '../../theme/variants'
import { useNotification } from '../../hooks/useNotification'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'

/**
 * 登录者自己的账号页
 *
 * 主体是**当前登录的这个账号**，与身份无关，人人可见 —— 租户成员的口令由平台方在建号时
 * 一次性设定，这里是他们唯一的自助更换入口。本部署那一个 NekroAI 社区账号的主页是另一页
 * （`pages/cloud/profile.tsx` → `/cloud/profile`，平台方专属）。
 *
 * 这一页在 `router/index.tsx` 里曾经**没有任何引用**：`/profile` 指向的是社区账号页，
 * 于是整个组件是死代码，改密卡片从来没被渲染过。
 */
export default function ProfilePage() {
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const notification = useNotification()
  const { t } = useTranslation('profile')

  // 获取用户信息
  const { data: userInfo } = useQuery({
    queryKey: ['userInfo'],
    queryFn: () => authApi.getUserInfo(),
  })

  // 平台方 admin 的口令来自部署环境变量 ADMIN_PASSWORD，写进数据库不影响登录。
  // 后端现在也会明确拒绝（EnvManagedPasswordError / 403），这里只是提前把按钮禁掉。
  const isAdminUser = userInfo?.username === 'admin'

  const canSubmit = Boolean(oldPassword && newPassword && confirmPassword) && !isAdminUser

  // 处理修改密码
  const handleChangePassword = async () => {
    if (isAdminUser) {
      notification.info(t('notifications.adminCannotChangePassword'))
      return
    }
    if (!oldPassword || !newPassword || !confirmPassword) {
      notification.error(t('notifications.fillAllFields'))
      return
    }

    if (newPassword !== confirmPassword) {
      notification.error(t('notifications.passwordMismatch'))
      return
    }

    if (newPassword === oldPassword) {
      notification.error(t('notifications.passwordUnchanged'))
      return
    }

    try {
      setLoading(true)
      await authApi.updatePassword({
        old_password: oldPassword,
        password: newPassword,
      })
      notification.success(t('notifications.passwordChangeSuccess'))
      // 清空输入框
      setOldPassword('')
      setNewPassword('')
      setConfirmPassword('')
    } catch (error) {
      if (error instanceof Error) {
        notification.error(error.message)
      } else {
        notification.error(t('notifications.passwordChangeFailed'))
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <Box sx={{ maxWidth: 800, mx: 'auto', p: { xs: 1.5, sm: 2, md: 3 }, boxSizing: 'border-box' }}>
      {/* 用户信息卡片 */}
      <Card sx={{ ...CARD_VARIANTS.default.styles, borderRadius: { xs: BORDER_RADIUS.MEDIUM, sm: BORDER_RADIUS.LARGE }, mb: { xs: 2, md: 4 } }}>
        <CardContent sx={{ p: { xs: 2, sm: 3 } }}>
          <Typography variant="h6" gutterBottom>
            {t('personalInfo.title')}
          </Typography>
          <Divider sx={{ my: 2 }} />
          <Stack spacing={2}>
            <Box>
              <Typography variant="subtitle2" color="text.secondary">
                {t('personalInfo.username')}
              </Typography>
              <Typography variant="body1" sx={{ wordBreak: 'break-word' }}>{userInfo?.username}</Typography>
            </Box>
            <Box>
              <Typography variant="subtitle2" color="text.secondary">
                {t('personalInfo.userId')}
              </Typography>
              <Typography variant="body1" sx={{ wordBreak: 'break-word' }}>{userInfo?.userId}</Typography>
            </Box>
            <Box>
              <Typography variant="subtitle2" color="text.secondary">
                {t('personalInfo.permissionLevel')}
              </Typography>
              <Typography variant="body1">{userInfo?.perm_role}</Typography>
            </Box>
          </Stack>
        </CardContent>
      </Card>

      {/* 修改密码卡片 */}
      <Card sx={{ ...CARD_VARIANTS.default.styles, borderRadius: { xs: BORDER_RADIUS.MEDIUM, sm: BORDER_RADIUS.LARGE } }}>
        <CardContent sx={{ p: { xs: 2, sm: 3 } }}>
          <Typography variant="h6" gutterBottom>
            {t('changePassword.title')}
          </Typography>
          <Divider sx={{ my: 2 }} />
          <Stack spacing={3}>
            {isAdminUser && (
              <Typography variant="body2" color="text.secondary">
                {t('changePassword.adminNotice')}
              </Typography>
            )}
            <TextField
              fullWidth
              type="password"
              label={t('changePassword.currentPassword')}
              value={oldPassword}
              onChange={e => setOldPassword(e.target.value)}
              disabled={isAdminUser}
              autoComplete="current-password"
              sx={{ ...INPUT_VARIANTS.default.styles, minWidth: 0 }}
            />
            <TextField
              fullWidth
              type="password"
              label={t('changePassword.newPassword')}
              value={newPassword}
              onChange={e => setNewPassword(e.target.value)}
              disabled={isAdminUser}
              autoComplete="new-password"
              sx={{ ...INPUT_VARIANTS.default.styles, minWidth: 0 }}
            />
            <TextField
              fullWidth
              type="password"
              label={t('changePassword.confirmPassword')}
              value={confirmPassword}
              onChange={e => setConfirmPassword(e.target.value)}
              disabled={isAdminUser}
              autoComplete="new-password"
              sx={{ ...INPUT_VARIANTS.default.styles, minWidth: 0 }}
            />
            {/* 改密现在会吊销已签发的令牌（后端口令世代方案，`PUT /user/password` 的 docstring）：
                改完之后所有会话（含当前）都要用新密码重新登录，提前说清免得以为被踢是故障 */}
            {!isAdminUser && (
              <Alert severity="info" sx={{ '& .MuiAlert-message': { minWidth: 0 } }}>
                {t('changePassword.sessionNotice')}
              </Alert>
            )}
            <ActionButton
              tone="primary"
              onClick={handleChangePassword}
              disabled={!canSubmit || loading}
              sx={{ width: { xs: '100%', sm: 'fit-content' } }}
            >
              {loading ? t('changePassword.submitting') : t('changePassword.submit')}
            </ActionButton>
          </Stack>
        </CardContent>
      </Card>
    </Box>
  )
}
