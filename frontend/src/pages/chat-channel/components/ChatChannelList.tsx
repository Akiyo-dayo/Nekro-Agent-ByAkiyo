import {
  List,
  ListItem,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Typography,
  Skeleton,
  Stack,
  Box,
} from '@mui/material'
import { Group as GroupIcon, Person as PersonIcon, Circle as CircleIcon } from '@mui/icons-material'
import type { ReactNode } from 'react'
import { getChannelDisplayName, type ChatChannel } from '../../../services/api/chat-channel'
import { useTranslation } from 'react-i18next'
import { formatLastActiveTimeFromInput } from '../../../utils/time'

interface ChatChannelListProps {
  channels: ChatChannel[]
  selectedChatKey: string | null
  onSelectChannel: (chatKey: string) => void
  isLoading: boolean
  /**
   * 空列表时的补充说明
   *
   * 「切到这个实例，频道管理是空的」有两种截然不同的成因：真的还没有人说过话，
   * 或者会话全挂在另一个（往往是没人认领的）实例名下。只给一个"暂无数据"等于把后者
   * 也说成前者，而后者正是本轮那个故障的第二个症状。
   */
  emptyHint?: ReactNode
}

export default function ChatChannelList({
  channels,
  selectedChatKey,
  onSelectChannel,
  isLoading,
  emptyHint,
}: ChatChannelListProps) {
  const { t } = useTranslation('chat-channel')
  if (isLoading) {
    return (
      <List disablePadding sx={{ width: '100%', minWidth: 0 }}>
        {[...Array(5)].map((_, index) => (
          <ListItem key={index} disablePadding divider>
            <ListItemButton>
              <ListItemIcon>
                <Skeleton variant="circular" width={24} height={24} />
              </ListItemIcon>
              <ListItemText
                primary={<Skeleton width="60%" />}
                secondary={<Skeleton width="40%" />}
              />
            </ListItemButton>
          </ListItem>
        ))}
      </List>
    )
  }

  if (channels.length === 0) {
    return (
      <Box
        sx={{
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 1.5,
          px: 2,
          textAlign: 'center',
        }}
      >
        <Typography color="textSecondary">{t('list.empty')}</Typography>
        {emptyHint}
      </Box>
    )
  }

  return (
    <List disablePadding sx={{ width: '100%', minWidth: 0, overflowX: 'hidden' }}>
      {channels.map(channel => (
        <ListItem key={channel.chat_key} disablePadding divider sx={{ width: '100%', minWidth: 0 }}>
          <ListItemButton
            selected={channel.chat_key === selectedChatKey}
            onClick={() => onSelectChannel(channel.chat_key)}
            sx={{
              width: '100%',
              minWidth: 0,
              py: 1.5,
              px: 2,
            }}
          >
            <ListItemIcon className="min-w-[32px]">
              {channel.chat_type === 'group' ? (
                <GroupIcon color="primary" fontSize="small" />
              ) : (
                <PersonIcon color="info" fontSize="small" />
              )}
            </ListItemIcon>
            <Box className="min-w-0 flex-1 overflow-hidden">
              <Stack direction="row" spacing={1} alignItems="center" className="min-w-0">
                <Typography variant="body2" className="font-medium truncate flex-1">
                  {getChannelDisplayName(channel)}
                </Typography>
                <CircleIcon
                  sx={{
                    fontSize: 8,
                    color: channel.status === 'active'
                      ? 'success.main'
                      : channel.status === 'observe'
                        ? 'warning.main'
                        : 'text.disabled',
                  }}
                  className="flex-shrink-0"
                />
              </Stack>
              <Typography
                variant="caption"
                color="textSecondary"
                sx={{
                  lineHeight: 1.35,
                  display: 'block',
                  minWidth: 0,
                  overflowWrap: 'anywhere',
                  wordBreak: 'break-word',
                }}
              >
                {channel.chat_key}
              </Typography>
              <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between" className="min-w-0">
                <Typography variant="caption" color="textSecondary" sx={{ lineHeight: 1.2 }}>
                  {t('list.messageCount', { count: channel.message_count })}
                </Typography>
                <Typography variant="caption" color="textSecondary" sx={{ lineHeight: 1.2 }} className="truncate">
                  {channel.last_message_time
                    ? formatLastActiveTimeFromInput(channel.last_message_time)
                    : t('list.noActiveRecord')}
                </Typography>
              </Stack>
            </Box>
          </ListItemButton>
        </ListItem>
      ))}
    </List>
  )
}
