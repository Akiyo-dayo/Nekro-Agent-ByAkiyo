/**
 * 覆盖状态可视化
 *
 * 配置分四层叠加（全局 -> 适配器 -> 实例 -> 会话），排查「为什么这个群不回复了」
 * 时最关键的信息是：当前生效值是多少、它来自哪一层、本层有没有覆盖。
 * 凡是支持覆盖的地方都用本组件把这三件事摆在一起，并提供「恢复继承」的明确出口。
 */

import { Box, Chip, Tooltip, Typography } from '@mui/material'
import { RestartAlt as RestartAltIcon } from '@mui/icons-material'
import { useTranslation } from 'react-i18next'
import type { ReactNode } from 'react'
import ActionButton from './ActionButton'
import type { ConfigLayer } from '../../services/api/instance-scope'

const LAYER_COLOR: Record<ConfigLayer, 'default' | 'info' | 'secondary' | 'warning'> = {
  global: 'default',
  system: 'default',
  adapter: 'info',
  instance: 'secondary',
  session: 'warning',
  channel: 'warning',
}

interface ScopeOverrideControlProps {
  /** 生效值来自哪一层 */
  source: ConfigLayer
  /** 当前正在编辑的层是否存在覆盖 */
  overridden: boolean
  /** 生效值的展示文本 */
  effectiveLabel: ReactNode
  /** 覆盖操作控件（开关、输入框等） */
  children?: ReactNode
  /** 清除本层覆盖、回落上层 */
  onRestoreInherit?: () => void
  /** 恢复继承是否处于进行中/不可用 */
  restoreDisabled?: boolean
  /** 紧凑模式：只显示来源徽标，用于列表行 */
  compact?: boolean
}

export default function ScopeOverrideControl({
  source,
  overridden,
  effectiveLabel,
  children,
  onRestoreInherit,
  restoreDisabled = false,
  compact = false,
}: ScopeOverrideControlProps) {
  const { t } = useTranslation('instance-scope')

  const sourceChip = (
    <Tooltip
      title={
        overridden
          ? t('override.tooltipOverridden', { layer: t(`layer.${source}`) })
          : t('override.tooltipInherited', { layer: t(`layer.${source}`) })
      }
    >
      <Chip
        size="small"
        variant={overridden ? 'filled' : 'outlined'}
        color={LAYER_COLOR[source]}
        label={
          overridden
            ? t('override.overriddenBadge', { layer: t(`layer.${source}`) })
            : t('override.inheritedBadge', { layer: t(`layer.${source}`) })
        }
        sx={{ height: 20, fontSize: '0.65rem', '& .MuiChip-label': { px: 0.75 } }}
      />
    </Tooltip>
  )

  if (compact) {
    return sourceChip
  }

  return (
    <Box className="flex items-center gap-1.5 flex-wrap">
      {children}
      <Box className="flex items-center gap-1">
        <Typography variant="caption" color="text.secondary">
          {t('override.effective')}
        </Typography>
        <Typography variant="caption" sx={{ fontWeight: 600 }}>
          {effectiveLabel}
        </Typography>
      </Box>
      {sourceChip}
      {overridden && onRestoreInherit && (
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<RestartAltIcon />}
          disabled={restoreDisabled}
          onClick={onRestoreInherit}
          sx={{ minWidth: 0, px: 1 }}
        >
          {t('override.restoreInherit')}
        </ActionButton>
      )}
    </Box>
  )
}
