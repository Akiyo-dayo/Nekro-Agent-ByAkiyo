/**
 * 实例作用域提示条
 *
 * 页面在实例作用域下必须显式告诉用户「你现在改的是这个实例的东西」，
 * 否则四层配置叠加后极易误操作。全局视图下不渲染。
 */

import { Alert, Box, Chip } from '@mui/material'
import { SmartToy as SmartToyIcon } from '@mui/icons-material'
import { alpha, useTheme } from '@mui/material/styles'
import { useTranslation } from 'react-i18next'
import type { ReactNode } from 'react'
import { useInstanceScope } from '../../hooks/useInstanceScope'

interface InstanceScopeBannerProps {
  /** 补充说明，例如「插件启停写入本实例覆盖」 */
  description?: ReactNode
  /** 严重级别，后端能力缺失时页面可传 warning */
  severity?: 'info' | 'warning'
  /** 额外操作区 */
  action?: ReactNode
}

export default function InstanceScopeBanner({
  description,
  severity = 'info',
  action,
}: InstanceScopeBannerProps) {
  const theme = useTheme()
  const { t } = useTranslation('instance-scope')
  const { currentInstance, currentInstanceKey } = useInstanceScope()

  if (!currentInstanceKey) {
    return null
  }

  const displayName = currentInstance?.display_name || currentInstanceKey

  return (
    <Alert
      severity={severity}
      icon={false}
      action={action}
      sx={{
        py: 0.5,
        alignItems: 'center',
        backgroundColor: alpha(theme.palette.secondary.main, 0.12),
        border: `1px solid ${alpha(theme.palette.secondary.main, 0.28)}`,
      }}
    >
      <Box className="flex items-center gap-1.5 flex-wrap">
        <Chip
          size="small"
          icon={<SmartToyIcon sx={{ fontSize: 14 }} />}
          label={displayName}
          color="secondary"
          sx={{ height: 22, fontWeight: 700 }}
        />
        <Box component="span">{description ?? t('banner.default')}</Box>
      </Box>
    </Alert>
  )
}
