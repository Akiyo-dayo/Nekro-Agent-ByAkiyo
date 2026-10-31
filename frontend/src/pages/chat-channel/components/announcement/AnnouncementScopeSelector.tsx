import {
  Box,
  Checkbox,
  FormControlLabel,
  List,
  ListItem,
  ListItemButton,
  Radio,
  RadioGroup,
  Skeleton,
  Typography,
} from '@mui/material'
import { useTranslation } from 'react-i18next'
import type {
  AnnouncementInstanceOption,
  AnnouncementScopeType,
} from '../../../../services/api/chat-channel'
import { instanceRefKey } from './scopeUtils'

interface AnnouncementScopeSelectorProps {
  scopeType: AnnouncementScopeType
  instanceOptions: AnnouncementInstanceOption[]
  selectedInstanceKeys: Set<string>
  loadingInstances: boolean
  onScopeTypeChange: (value: AnnouncementScopeType) => void
  onToggleInstance: (instance: AnnouncementInstanceOption) => void
}

export default function AnnouncementScopeSelector({
  scopeType,
  instanceOptions,
  selectedInstanceKeys,
  loadingInstances,
  onScopeTypeChange,
  onToggleInstance,
}: AnnouncementScopeSelectorProps) {
  const { t } = useTranslation('chat-announcement')

  const totalActiveChannels = instanceOptions.reduce((sum, option) => sum + option.channel_count, 0)

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
        {t('scope.title')}
      </Typography>

      <RadioGroup
        row
        value={scopeType}
        onChange={event => onScopeTypeChange(event.target.value as AnnouncementScopeType)}
      >
        <FormControlLabel value="all" control={<Radio size="small" />} label={t('scope.all')} />
        <FormControlLabel
          value="instances"
          control={<Radio size="small" />}
          label={t('scope.instances')}
        />
        <FormControlLabel
          value="channels"
          control={<Radio size="small" />}
          label={t('scope.channels')}
        />
      </RadioGroup>

      {scopeType === 'all' && (
        <Typography variant="body2" color="text.secondary">
          {t('scope.allHint', {
            instances: instanceOptions.length,
            channels: totalActiveChannels,
          })}
        </Typography>
      )}

      {scopeType === 'instances' &&
        (loadingInstances ? (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            {Array.from({ length: 3 }).map((_, index) => (
              <Skeleton key={index} variant="rounded" height={48} />
            ))}
          </Box>
        ) : instanceOptions.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            {t('scope.instancesEmpty')}
          </Typography>
        ) : (
          <List disablePadding sx={{ maxHeight: 260, overflowY: 'auto' }}>
            {instanceOptions.map(option => {
              const key = instanceRefKey(option)
              return (
                <ListItem key={key} disablePadding>
                  <ListItemButton
                    dense
                    onClick={() => onToggleInstance(option)}
                    sx={{ borderRadius: 1.5, gap: 1 }}
                  >
                    <Checkbox
                      edge="start"
                      size="small"
                      tabIndex={-1}
                      disableRipple
                      checked={selectedInstanceKeys.has(key)}
                    />
                    <Box sx={{ minWidth: 0, flex: 1 }}>
                      <Typography variant="body2" sx={{ fontWeight: 600 }} noWrap>
                        {option.display_name}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        {t('scope.instanceChannels', { count: option.channel_count })}
                      </Typography>
                    </Box>
                  </ListItemButton>
                </ListItem>
              )
            })}
          </List>
        ))}
    </Box>
  )
}
