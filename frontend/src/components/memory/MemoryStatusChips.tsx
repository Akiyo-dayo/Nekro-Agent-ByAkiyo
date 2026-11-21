import { Chip, Stack, Typography, useTheme, type Theme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import {
  MEMORY_DETAIL_STATUSES,
  type MemoryDetailStatus,
} from '../../services/api/memory'
import { CHIP_VARIANTS } from '../../theme/variants'

/**
 * 记忆状态的统一呈现
 *
 * 颜色只在主题调色板里取，页面与表格都从这里拿，避免同一状态在两处长得不一样。
 * 状态值来自后端，做一次归一：不认识的取值按「未知」显示，而不是把 i18n 键名漏到界面上。
 */

function normalizeStatus(status: string): MemoryDetailStatus {
  return (MEMORY_DETAIL_STATUSES as readonly string[]).includes(status)
    ? (status as MemoryDetailStatus)
    : 'unknown'
}

function statusColor(theme: Theme, status: MemoryDetailStatus): string {
  switch (status) {
    case 'active':
      return theme.palette.success.main
    case 'quarantined':
      return theme.palette.warning.main
    case 'deleted':
      return theme.palette.text.secondary
    case 'pruned':
      return theme.palette.text.disabled
    case 'hard_deleted':
      // 明细里引用到的条目已经不在库里：用醒目的颜色，免得被当成「还能处理」
      return theme.palette.error.main
    default:
      return theme.palette.text.secondary
  }
}

export function MemoryStatusChip({ status }: { status: string }) {
  const theme = useTheme()
  const { t } = useTranslation('memory')
  const normalized = normalizeStatus(status)

  return (
    <Chip
      size="small"
      label={t(`status.${normalized}`)}
      sx={CHIP_VARIANTS.getCustomColorChip(statusColor(theme, normalized), true)}
    />
  )
}

export function MemoryFlagChips({
  isFrozen,
  isProtected,
}: {
  isFrozen?: boolean
  isProtected?: boolean
}) {
  const theme = useTheme()
  const { t } = useTranslation('memory')

  if (!isFrozen && !isProtected) return null

  return (
    <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap' }}>
      {isFrozen && (
        <Chip
          size="small"
          label={t('flags.frozen')}
          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true)}
        />
      )}
      {isProtected && (
        <Chip
          size="small"
          label={t('flags.protected')}
          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.primary.main, true)}
        />
      )}
    </Stack>
  )
}

/** 触发拦截次数：大于 0 才用错误色，0 保持普通文本，避免整列都在喊 */
export function MemoryBlockedHitsChip({ count }: { count: number }) {
  const theme = useTheme()

  if (count <= 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        0
      </Typography>
    )
  }

  return (
    <Chip
      size="small"
      label={count}
      sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.error.main, true)}
    />
  )
}

export function MemoryKindChip({ memoryType }: { memoryType: string }) {
  const theme = useTheme()
  const { t } = useTranslation('memory')

  if (memoryType === 'episode') {
    return (
      <Chip
        size="small"
        label={t('kind.episode')}
        sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.primary.main, true)}
      />
    )
  }

  if (memoryType === 'paragraph') {
    return (
      <Chip
        size="small"
        label={t('kind.paragraph')}
        sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true)}
      />
    )
  }

  return <Chip size="small" label={memoryType} />
}
