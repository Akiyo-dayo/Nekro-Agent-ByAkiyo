import { Alert, Card, CardContent, Stack, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import ActionButton from '../../../../components/common/ActionButton'
import { CARD_VARIANTS } from '../../../../theme/variants'
import { adaptersApi } from '../../../../services/api/adapters'

interface NativeAdapterStepProps {
  adapterKey: string
  onOpenAdapterConfig: (adapterKey: string) => void
}

export default function NativeAdapterStep({
  adapterKey,
  onOpenAdapterConfig,
}: NativeAdapterStepProps) {
  const { t } = useTranslation('console-setup')

  const { data: adapter } = useQuery({
    queryKey: ['adapter-info', adapterKey],
    queryFn: () => adaptersApi.getAdapterInfo(adapterKey),
    enabled: Boolean(adapterKey),
  })

  return (
    <Stack spacing={2}>
      <Alert severity="info">{t('native.intro')}</Alert>

      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent>
          <Stack spacing={1.5}>
            <Typography variant="subtitle1" fontWeight={600}>
              {adapter?.name || adapterKey}
            </Typography>
            {adapter?.description ? (
              <Typography variant="body2" color="text.secondary">
                {adapter.description}
              </Typography>
            ) : null}
            <ActionButton
              tone="primary"
              startIcon={<OpenInNewIcon />}
              disabled={!adapterKey}
              onClick={() => onOpenAdapterConfig(adapterKey)}
              sx={{ alignSelf: 'flex-start' }}
            >
              {t('native.openConfig')}
            </ActionButton>
          </Stack>
        </CardContent>
      </Card>

      <Typography variant="caption" color="text.secondary">
        {t('native.returnHint')}
      </Typography>
    </Stack>
  )
}
