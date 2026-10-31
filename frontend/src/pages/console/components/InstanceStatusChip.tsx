import { Chip, useTheme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import CheckCircleRoundedIcon from '@mui/icons-material/CheckCircleRounded'
import SyncRoundedIcon from '@mui/icons-material/SyncRounded'
import CloudOffRoundedIcon from '@mui/icons-material/CloudOffRounded'
import LinkOffRoundedIcon from '@mui/icons-material/LinkOffRounded'
import QrCode2RoundedIcon from '@mui/icons-material/QrCode2Rounded'
import ErrorOutlineRoundedIcon from '@mui/icons-material/ErrorOutlineRounded'
import HelpOutlineRoundedIcon from '@mui/icons-material/HelpOutlineRounded'
import PauseCircleOutlineRoundedIcon from '@mui/icons-material/PauseCircleOutlineRounded'
import Inventory2OutlinedIcon from '@mui/icons-material/Inventory2Outlined'
import type { ReactElement } from 'react'
import { CHIP_VARIANTS } from '../../../theme/variants'
import { SimpleTooltip } from '../../../components/common/ThemedTooltip'
import { getOfflineMinutes, type InstanceStatus } from '../../../services/api/console'

type StatusTone = 'success' | 'primary' | 'warning' | 'error' | 'muted'

const STATUS_TONE: Record<InstanceStatus, StatusTone> = {
  online: 'success',
  connecting: 'primary',
  offline: 'error',
  // 未绑定账号是"还没接上"而不是"出故障"：用告警色而不是错误色，红点会让管理员
  // 跑去查协议端，而协议端一切正常
  unbound: 'warning',
  login_required: 'warning',
  error: 'error',
  unclaimed: 'warning',
  disabled: 'muted',
  archived: 'muted',
}

const STATUS_ICON: Record<InstanceStatus, ReactElement> = {
  online: <CheckCircleRoundedIcon />,
  connecting: <SyncRoundedIcon />,
  offline: <CloudOffRoundedIcon />,
  unbound: <LinkOffRoundedIcon />,
  login_required: <QrCode2RoundedIcon />,
  error: <ErrorOutlineRoundedIcon />,
  unclaimed: <HelpOutlineRoundedIcon />,
  disabled: <PauseCircleOutlineRoundedIcon />,
  archived: <Inventory2OutlinedIcon />,
}

interface InstanceStatusChipProps {
  status: InstanceStatus
  /** 掉线起始时间，用于展示「已掉线 X 分钟」 */
  offlineSince?: string | null
  /** 最近错误，作为 tooltip 的补充说明 */
  lastError?: string
  small?: boolean
}

export default function InstanceStatusChip({
  status,
  offlineSince,
  lastError,
  small = false,
}: InstanceStatusChipProps) {
  const theme = useTheme()
  const { t } = useTranslation('console')

  const toneColor: Record<StatusTone, string> = {
    success: theme.palette.success.main,
    primary: theme.palette.primary.main,
    warning: theme.palette.warning.main,
    error: theme.palette.error.main,
    muted: theme.palette.text.secondary,
  }

  // 「已掉线 X 分钟」只对真的掉过线的实例成立。未绑定 / 待认领的实例从来没上线过，
  // 给它接一个掉线时长等于把"你还没配"说成"你已经坏了 X 分钟"
  const offlineMinutes =
    status === 'unbound' || status === 'unclaimed' ? null : getOfflineMinutes(offlineSince)
  const offlineText =
    offlineMinutes === null
      ? ''
      : offlineMinutes >= 60
        ? t('status.offlineForHours', { count: Math.floor(offlineMinutes / 60) })
        : t('status.offlineForMinutes', { count: offlineMinutes })

  const label = offlineText ? `${t(`status.${status}`)} · ${offlineText}` : t(`status.${status}`)
  const tooltip = [t(`statusHint.${status}`), lastError].filter(Boolean).join(' — ')

  return (
    <SimpleTooltip title={tooltip}>
      <Chip
        size={small ? 'small' : 'medium'}
        icon={STATUS_ICON[status]}
        label={label}
        sx={CHIP_VARIANTS.getCustomColorChip(toneColor[STATUS_TONE[status]], small)}
      />
    </SimpleTooltip>
  )
}
