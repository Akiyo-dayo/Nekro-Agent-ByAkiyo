import { Box, Chip, useTheme, type Theme } from '@mui/material'
import LinkRoundedIcon from '@mui/icons-material/LinkRounded'
import VerifiedUserRoundedIcon from '@mui/icons-material/VerifiedUserRounded'
import { useTranslation } from 'react-i18next'
import StatusChip from '../common/StatusChip'
import { ThemedTooltip } from '../common/ThemedTooltip'
import { CHIP_VARIANTS } from '../../theme/variants'

/**
 * 世界书状态的统一呈现
 *
 * 颜色只在主题调色板里取，页面与表格都从这里拿，避免同一状态在两处长得不一样。
 * 不认识的取值按「未知」显示，不把 i18n 键名漏到界面上。
 *
 * 启用/停用/隔离走全站同一套 `StatusChip` 语气；作用域是分类而不是状态，仍用它自己的
 * 四色：域 info、人设 secondary、频道 context、人物 secondary（与图谱里的 person 同色）。
 */

function worldbookScopeColor(theme: Theme, scopeKind: string): string {
  switch (scopeKind) {
    case 'domain':
      return theme.palette.info.main
    case 'preset':
      return theme.palette.secondary.main
    case 'person':
      return theme.palette.secondary.main
    case 'channel':
      return theme.palette.text.secondary
    default:
      return theme.palette.text.secondary
  }
}

function scopeLabel(scopeKind: string, t: (key: string) => string): string {
  if (['domain', 'preset', 'channel', 'person'].includes(scopeKind)) {
    return t(`scope.${scopeKind}`)
  }
  return t('common.unknown')
}

export function WorldbookScopeChip({ scopeKind }: { scopeKind: string }) {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')

  return (
    <Chip
      size="small"
      label={scopeLabel(scopeKind, t)}
      sx={CHIP_VARIANTS.getCustomColorChip(worldbookScopeColor(theme, scopeKind), true)}
    />
  )
}

export function WorldbookEnabledChip({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation('worldbook')

  return (
    <StatusChip
      tone={enabled ? 'active' : 'inactive'}
      label={enabled ? t('common.enabled') : t('common.disabled')}
    />
  )
}

export function WorldbookTrustChip({ trusted }: { trusted: boolean }) {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')

  return (
    <Chip
      size="small"
      label={trusted ? t('common.hostContent') : t('common.reference')}
      sx={CHIP_VARIANTS.getCustomColorChip(
        trusted ? theme.palette.success.main : theme.palette.text.secondary,
        true
      )}
    />
  )
}

/** 条目表里的「可信」只用一个图标，含义放在 tooltip 里 */
export function WorldbookTrustIcon({ trusted }: { trusted: boolean }) {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')
  const label = trusted ? t('common.hostContent') : t('common.reference')

  return (
    <ThemedTooltip title={label}>
      <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center' }}>
        {trusted ? (
          <VerifiedUserRoundedIcon
            sx={{ fontSize: 18, color: theme.palette.success.main }}
            aria-label={label}
          />
        ) : (
          <LinkRoundedIcon
            sx={{ fontSize: 18, color: theme.palette.text.secondary }}
            aria-label={label}
          />
        )}
      </Box>
    </ThemedTooltip>
  )
}

export function WorldbookStatusChip({
  status,
  enabled,
}: {
  status: string
  enabled: boolean
}) {
  const { t } = useTranslation('worldbook')

  if (status === 'quarantined') {
    return <StatusChip tone="quarantined" label={t('detail.status.quarantined')} />
  }
  if (!enabled) {
    return <StatusChip tone="inactive" label={t('detail.status.disabled')} />
  }
  return <StatusChip tone="active" label={t('detail.status.active')} />
}

/** 平台方给全局人设建、对当前租户只读开放的书 */
export function WorldbookReadOnlyChip() {
  const { t } = useTranslation('worldbook')

  return <StatusChip tone="protected" label={t('readOnly.chip')} />
}

export function WorldbookQuarantineReasonChip({
  reason,
  blockedHits,
}: {
  reason: string
  blockedHits: number
}) {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')
  // 自动隔离把转隔离时的被拦次数写进原因：隔离中的条目不再注入，这个数就是当时的计数。
  const label =
    reason === 'auto_blocked'
      ? t('quarantine.reason.auto_blocked', { count: blockedHits })
      : reason === 'manual'
        ? t('quarantine.reason.manual')
        : t('common.unknown')

  return (
    <Chip
      size="small"
      label={label}
      sx={CHIP_VARIANTS.getCustomColorChip(
        reason === 'auto_blocked' ? theme.palette.warning.main : theme.palette.info.main,
        true
      )}
    />
  )
}
