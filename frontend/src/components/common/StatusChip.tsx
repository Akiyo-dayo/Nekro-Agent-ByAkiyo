import type { ReactElement, ReactNode } from 'react'
import { Chip, type SxProps, type Theme } from '@mui/material'
import { STATUS_TONES, type StatusTone } from '../../theme/themeConfig'
import { CHIP_VARIANTS } from '../../theme/variants'

/**
 * 状态标签：语气决定颜色，全站同一套
 *
 * - active：启用 / 生效 / 正常 / 成功
 * - inactive：停用 / 已删除（可恢复）
 * - disabled：已清理 / 不可用
 * - quarantined：隔离（含自动拦截）
 * - paused：暂停
 * - failed：失败 / 连续失败 / 硬删除
 * - frozen：冻结
 * - protected：保护 / 只读
 *
 * 一个格子只放一个状态标签；冻结、保护、被拦这类附加标记用小图标跟在后面，不再叠第二个标签。
 */

export interface StatusChipProps {
  tone: StatusTone
  label: ReactNode
  /** 默认是表格里用的小号 */
  small?: boolean
  icon?: ReactElement
  sx?: SxProps<Theme>
}

export default function StatusChip({ tone, label, small = true, icon, sx }: StatusChipProps) {
  const base = CHIP_VARIANTS.getCustomColorChip(STATUS_TONES[tone], small)
  return (
    <Chip
      size="small"
      label={label}
      icon={icon}
      sx={(sx ? [base, sx] : base) as SxProps<Theme>}
    />
  )
}
