import { Card, CardContent, Divider, Stack, Typography, useTheme } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ArrowForwardIcon from '@mui/icons-material/ArrowForward'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import ActionButton from '../../../../components/common/ActionButton'
import { CARD_VARIANTS } from '../../../../theme/variants'
import type { InstanceInfo } from '../../../../services/api/console'
import { consoleContainerApi } from '../../../../services/api/consoleContainer'
import InstanceStatusChip from '../../components/InstanceStatusChip'

interface DoneStepProps {
  instance: InstanceInfo
  onEnterInstance: (instanceKey: string) => void
}

export default function DoneStep({ instance, onEnterInstance }: DoneStepProps) {
  const { t } = useTranslation(['console-setup', 'console'])
  const theme = useTheme()

  const { data: container } = useQuery({
    queryKey: ['console-container', instance.instance_key],
    queryFn: () => consoleContainerApi.getContainer(instance.instance_key),
    enabled: instance.provider === 'builtin_napcat',
  })

  const rows = [
    { key: 'displayName', value: instance.display_name || instance.instance_key },
    { key: 'instanceKey', value: instance.instance_key },
    { key: 'provider', value: t(`console:provider.${instance.provider}`) },
    { key: 'adapter', value: instance.adapter_key },
    {
      key: 'container',
      value: container ? t(`console:container.${container.state}`) : t('done.noContainer'),
    },
  ]

  return (
    <Stack spacing={2} alignItems="stretch">
      <Stack spacing={1} alignItems="center" sx={{ py: 2 }}>
        <CheckCircleIcon sx={{ fontSize: 48, color: theme.palette.success.main }} />
        <Typography variant="h6">{t('done.title')}</Typography>
        <Typography variant="body2" color="text.secondary" textAlign="center">
          {t('done.subtitle')}
        </Typography>
      </Stack>

      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent>
          <Stack spacing={1.25} divider={<Divider flexItem />}>
            {rows.map(row => (
              <Stack
                key={row.key}
                direction="row"
                spacing={2}
                alignItems="center"
                justifyContent="space-between"
              >
                <Typography variant="body2" color="text.secondary">
                  {t(`done.field.${row.key}`)}
                </Typography>
                <Typography variant="body2" fontWeight={600} sx={{ textAlign: 'right' }}>
                  {row.value}
                </Typography>
              </Stack>
            ))}
            <Stack direction="row" spacing={2} alignItems="center" justifyContent="space-between">
              <Typography variant="body2" color="text.secondary">
                {t('done.field.status')}
              </Typography>
              <InstanceStatusChip
                status={instance.status}
                offlineSince={instance.offline_since}
                lastError={instance.last_error}
                small
              />
            </Stack>
          </Stack>
        </CardContent>
      </Card>

      <ActionButton
        tone="primary"
        endIcon={<ArrowForwardIcon />}
        onClick={() => onEnterInstance(instance.instance_key)}
        sx={{ alignSelf: 'center' }}
      >
        {t('done.enterInstance')}
      </ActionButton>
    </Stack>
  )
}
