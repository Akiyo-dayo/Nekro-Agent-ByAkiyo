import { Alert, MenuItem, Stack, TextField, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { adaptersApi } from '../../../../services/api/adapters'
import type { InstanceProvider } from '../../../../services/api/console'

interface BasicInfoStepProps {
  provider: InstanceProvider
  displayName: string
  onDisplayNameChange: (value: string) => void
  instanceKey: string
  onInstanceKeyChange: (value: string) => void
  instanceKeyError: string | null
  adapterKey: string
  onAdapterKeyChange: (value: string) => void
}

export default function BasicInfoStep({
  provider,
  displayName,
  onDisplayNameChange,
  instanceKey,
  onInstanceKeyChange,
  instanceKeyError,
  adapterKey,
  onAdapterKeyChange,
}: BasicInfoStepProps) {
  const { t } = useTranslation('console-setup')
  const needsAdapterChoice = provider === 'native'

  const { data: adapters = [], isLoading: adaptersLoading } = useQuery({
    queryKey: ['adapters-list'],
    queryFn: () => adaptersApi.getAdaptersList(),
    enabled: needsAdapterChoice,
  })

  const nativeAdapters = adapters.filter(adapter => adapter.key !== 'onebot_v11')

  return (
    <Stack spacing={2.5}>
      <TextField
        fullWidth
        size="small"
        autoFocus
        label={t('basic.displayName')}
        placeholder={t('basic.displayNamePlaceholder')}
        value={displayName}
        onChange={event => onDisplayNameChange(event.target.value)}
      />

      <TextField
        fullWidth
        size="small"
        label={t('basic.instanceKey')}
        placeholder={t('basic.instanceKeyPlaceholder')}
        value={instanceKey}
        onChange={event => onInstanceKeyChange(event.target.value)}
        error={Boolean(instanceKeyError)}
        helperText={instanceKeyError ?? t('basic.instanceKeyHint')}
      />

      <Alert severity="info">{t('basic.immutableNotice')}</Alert>

      {needsAdapterChoice ? (
        <Stack spacing={1}>
          <TextField
            select
            fullWidth
            size="small"
            label={t('basic.adapter')}
            value={adapterKey}
            disabled={adaptersLoading}
            onChange={event => onAdapterKeyChange(event.target.value)}
            helperText={t('basic.adapterHint')}
          >
            {nativeAdapters.map(adapter => (
              <MenuItem key={adapter.key} value={adapter.key}>
                {adapter.name || adapter.key}
              </MenuItem>
            ))}
          </TextField>
          {!adaptersLoading && nativeAdapters.length === 0 ? (
            <Typography variant="caption" color="warning.main">
              {t('basic.noNativeAdapter')}
            </Typography>
          ) : null}
        </Stack>
      ) : null}
    </Stack>
  )
}
