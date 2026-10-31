import {
  Box,
  Card,
  CardActionArea,
  CardContent,
  Divider,
  Typography,
  useTheme,
} from '@mui/material'
import { useTranslation } from 'react-i18next'
import { alpha } from '@mui/material/styles'
import ChatBubbleOutlineRoundedIcon from '@mui/icons-material/ChatBubbleOutlineRounded'
import ForumOutlinedIcon from '@mui/icons-material/ForumOutlined'
import ScheduleRoundedIcon from '@mui/icons-material/ScheduleRounded'
import StarRoundedIcon from '@mui/icons-material/StarRounded'
import { CARD_VARIANTS, CHIP_VARIANTS } from '../../../theme/variants'
import InstanceStatusChip from './InstanceStatusChip'
import { isInstanceActionable, type InstanceInfo } from '../../../services/api/console'

interface InstanceCardProps {
  instance: InstanceInfo
  onOpen: (instanceKey: string) => void
}

function formatRelativeTime(value: string | null, fallback: string): string {
  if (!value) return fallback
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return fallback
  return new Date(timestamp).toLocaleString()
}

export default function InstanceCard({ instance, onOpen }: InstanceCardProps) {
  const theme = useTheme()
  const { t } = useTranslation('console')

  // 「等着管理员做一件事」的状态共用同一条强调线与同一句可执行提示：
  // 认领入口统一在待认领面板里，卡片只负责把"这里需要你"说清楚
  const needsAction = isInstanceActionable(instance.status)
  const accentColor = needsAction ? theme.palette.warning.main : theme.palette.primary.main

  return (
    <Card
      sx={{
        ...CARD_VARIANTS.default.styles,
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        '&::before': {
          content: '""',
          position: 'absolute',
          insetInline: 0,
          top: 0,
          height: '3px',
          backgroundColor: alpha(accentColor, needsAction ? 0.9 : 0.45),
        },
      }}
    >
      <CardActionArea
        onClick={() => onOpen(instance.instance_key)}
        sx={{ flex: 1, alignItems: 'stretch', display: 'flex' }}
      >
        <CardContent sx={{ p: 2, pt: 2.25, width: '100%' }}>
          <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, mb: 0.5 }}>
            <Box sx={{ minWidth: 0, flex: 1 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                <Typography variant="subtitle1" fontWeight={700} noWrap>
                  {instance.display_name || instance.instance_key}
                </Typography>
                {instance.is_default && (
                  <StarRoundedIcon sx={{ fontSize: 16, color: theme.palette.warning.main }} />
                )}
              </Box>
              <Typography variant="caption" color="text.secondary" noWrap component="div">
                {instance.instance_key}
              </Typography>
            </Box>
            <InstanceStatusChip
              status={instance.status}
              offlineSince={instance.offline_since}
              lastError={instance.last_error}
              small
            />
          </Box>

          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, mt: 1.25 }}>
            <Box
              component="span"
              sx={{
                ...CHIP_VARIANTS.getCustomColorChip(theme.palette.text.secondary, true),
                px: 0.75,
                py: 0.25,
                borderRadius: 1,
                fontSize: '0.65rem',
                minWidth: 0,
              }}
            >
              {t(`provider.${instance.provider}`)}
            </Box>
            <Box
              component="span"
              sx={{
                ...CHIP_VARIANTS.getCustomColorChip(theme.palette.text.secondary, true),
                px: 0.75,
                py: 0.25,
                borderRadius: 1,
                fontSize: '0.65rem',
                minWidth: 0,
              }}
            >
              {instance.provider_account_id || t('card.accountUnbound')}
            </Box>
          </Box>

          <Divider sx={{ my: 1.5 }} />

          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.75 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
              <ForumOutlinedIcon sx={{ fontSize: 16, color: 'text.secondary' }} />
              <Typography variant="caption" color="text.secondary">
                {t('card.channelCount', { count: instance.stats.channel_count })}
              </Typography>
            </Box>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
              <ChatBubbleOutlineRoundedIcon sx={{ fontSize: 16, color: 'text.secondary' }} />
              <Typography variant="caption" color="text.secondary">
                {t('card.messageToday', { count: instance.stats.message_count_today })}
              </Typography>
            </Box>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
              <ScheduleRoundedIcon sx={{ fontSize: 16, color: 'text.secondary' }} />
              <Typography variant="caption" color="text.secondary" noWrap>
                {t('card.lastActive', {
                  time: formatRelativeTime(instance.stats.last_active_at, t('card.never')),
                })}
              </Typography>
            </Box>
          </Box>
        </CardContent>
      </CardActionArea>

      {needsAction && (
        <Box sx={{ px: 2, pb: 2 }}>
          <Typography variant="caption" sx={{ color: theme.palette.warning.main }}>
            {t(`card.actionHint.${instance.status}`)}
          </Typography>
        </Box>
      )}
    </Card>
  )
}
