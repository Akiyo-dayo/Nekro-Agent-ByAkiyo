import { Chip, useTheme, type Theme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { CHIP_VARIANTS } from '../../theme/variants'

/**
 * 世界书状态的统一呈现
 *
 * 颜色只在主题调色板里取，页面与表格都从这里拿，避免同一状态在两处长得不一样。
 * 不认识的取值按「未知」显示，不把 i18n 键名漏到界面上。
 */

function worldbookScopeColor(theme: Theme, scopeKind: string): string {
  switch (scopeKind) {
    case 'domain':
      return theme.palette.primary.main
    case 'preset':
      return theme.palette.secondary.main
    case 'channel':
      return theme.palette.info.main
    case 'person':
      return theme.palette.warning.main
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
  const theme = useTheme()
  const { t } = useTranslation('worldbook')

  return (
    <Chip
      size="small"
      label={enabled ? t('common.enabled') : t('common.disabled')}
      sx={CHIP_VARIANTS.getCustomColorChip(
        enabled ? theme.palette.success.main : theme.palette.text.disabled,
        true
      )}
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
        trusted ? theme.palette.success.main : theme.palette.warning.main,
        true
      )}
    />
  )
}

export function WorldbookStatusChip({
  status,
  enabled,
}: {
  status: string
  enabled: boolean
}) {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')

  if (status === 'quarantined') {
    return (
      <Chip
        size="small"
        label={t('detail.status.quarantined')}
        sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.error.main, true)}
      />
    )
  }
  if (!enabled) {
    return (
      <Chip
        size="small"
        label={t('detail.status.disabled')}
        sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.text.disabled, true)}
      />
    )
  }
  return (
    <Chip
      size="small"
      label={t('detail.status.active')}
      sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.success.main, true)}
    />
  )
}

/** 平台方给全局人设建、对当前租户只读开放的书 */
export function WorldbookReadOnlyChip() {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')

  return (
    <Chip
      size="small"
      label={t('readOnly.chip')}
      sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true)}
    />
  )
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
        reason === 'auto_blocked' ? theme.palette.error.main : theme.palette.info.main,
        true
      )}
    />
  )
}
