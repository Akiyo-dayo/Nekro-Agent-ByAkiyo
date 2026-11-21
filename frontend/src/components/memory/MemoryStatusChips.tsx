import { Box, Chip, Stack, useTheme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import AcUnitRoundedIcon from '@mui/icons-material/AcUnitRounded'
import ShieldRoundedIcon from '@mui/icons-material/ShieldRounded'
import StatusChip from '../common/StatusChip'
import { SimpleTooltip } from '../common/ThemedTooltip'
import {
  MEMORY_DETAIL_STATUSES,
  type MemoryDetailStatus,
} from '../../services/api/memory'
import { ATLAS_COLORS, type StatusTone } from '../../theme/themeConfig'
import { CHIP_VARIANTS } from '../../theme/variants'

/**
 * 记忆状态的统一呈现
 *
 * 状态 Chip 走公共 `StatusChip`，颜色只在 `STATUS_TONES` 里取；冻结与保护属于附加标记，
 * 用一个 14px 图标跟在状态后面，不再叠第二个 Chip。类型 Chip 取 `ATLAS_COLORS` 的
 * 角色色，与图谱里的节点、簇标题同源。
 */

function normalizeStatus(status: string): MemoryDetailStatus {
  return (MEMORY_DETAIL_STATUSES as readonly string[]).includes(status)
    ? (status as MemoryDetailStatus)
    : 'unknown'
}

function statusTone(status: MemoryDetailStatus): StatusTone {
  switch (status) {
    case 'active':
      return 'active'
    case 'quarantined':
      return 'quarantined'
    case 'deleted':
      return 'inactive'
    case 'pruned':
      return 'disabled'
    case 'hard_deleted':
      // 明细里引用到的条目已经不在库里：用失败色，免得被当成「还能处理」
      return 'failed'
    default:
      return 'inactive'
  }
}

export function MemoryStatusChip({ status }: { status: string }) {
  const { t } = useTranslation('memory')
  const normalized = normalizeStatus(status)

  return <StatusChip tone={statusTone(normalized)} label={t(`status.${normalized}`)} />
}

/** 冻结 / 保护：跟在状态 Chip 后面的 14px 图标，说明放在 tooltip 里 */
export function MemoryFlagIcons({
  isFrozen,
  isProtected,
}: {
  isFrozen?: boolean
  isProtected?: boolean
}) {
  const { t } = useTranslation('memory')

  if (!isFrozen && !isProtected) return null

  return (
    <Stack direction="row" spacing={0.25} alignItems="center" component="span">
      {isFrozen && (
        <SimpleTooltip title={t('flags.frozen')}>
          <Box component="span" sx={{ display: 'inline-flex', color: 'info.main' }}>
            <AcUnitRoundedIcon sx={{ fontSize: 14 }} />
          </Box>
        </SimpleTooltip>
      )}
      {isProtected && (
        <SimpleTooltip title={t('flags.protected')}>
          <Box component="span" sx={{ display: 'inline-flex', color: 'primary.main' }}>
            <ShieldRoundedIcon sx={{ fontSize: 14 }} />
          </Box>
        </SimpleTooltip>
      )}
    </Stack>
  )
}

/** 兼容详情对话框的旧用法：那里没有表格格子，仍以 Chip 形式展示 */
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

/** 触发拦截次数：只在该值大于 0 时画出来 */
export function MemoryBlockedHitsChip({ count }: { count: number }) {
  const theme = useTheme()

  if (count <= 0) return null

  return (
    <Chip
      size="small"
      label={count}
      sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.error.main, true)}
    />
  )
}

/** 敏感事：列表、分组行与详情共用的小标签，含义放在 tooltip 里 */
export function MemorySensitiveChip() {
  const theme = useTheme()
  const { t } = useTranslation('memory')

  return (
    <SimpleTooltip title={t('flags.sensitiveHint')}>
      <Chip
        size="small"
        label={t('flags.sensitive')}
        sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.warning.main, true)}
      />
    </SimpleTooltip>
  )
}

/** 类型 Chip：与图谱同色，段落取语义簇、事件取事件核 */
export function MemoryKindChip({ memoryType }: { memoryType: string }) {
  const { t } = useTranslation('memory')

  if (memoryType === 'episode') {
    return (
      <Chip
        size="small"
        label={t('kind.episode')}
        sx={CHIP_VARIANTS.getCustomColorChip(ATLAS_COLORS.core, true)}
      />
    )
  }

  if (memoryType === 'paragraph') {
    return (
      <Chip
        size="small"
        label={t('kind.paragraph')}
        sx={CHIP_VARIANTS.getCustomColorChip(ATLAS_COLORS.semantic, true)}
      />
    )
  }

  if (memoryType === 'relation') {
    return (
      <Chip
        size="small"
        label={t('kind.relation')}
        sx={CHIP_VARIANTS.getCustomColorChip(ATLAS_COLORS.relation, true)}
      />
    )
  }

  if (memoryType === 'entity') {
    return (
      <Chip
        size="small"
        label={t('kind.entity')}
        sx={CHIP_VARIANTS.getCustomColorChip(ATLAS_COLORS.entity, true)}
      />
    )
  }

  if (memoryType === 'person') {
    return (
      <Chip
        size="small"
        label={t('graph.kind.person')}
        sx={CHIP_VARIANTS.getCustomColorChip(ATLAS_COLORS.person, true)}
      />
    )
  }

  return (
    <Chip
      size="small"
      label={memoryType}
      sx={CHIP_VARIANTS.getCustomColorChip(ATLAS_COLORS.context, true)}
    />
  )
}
