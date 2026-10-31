import {
  Alert,
  Box,
  Card,
  CardContent,
  CircularProgress,
  InputAdornment,
  Stack,
  TextField,
  Typography,
  alpha,
  useTheme,
} from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import IconActionButton from '../../../../components/common/IconActionButton'
import { useNotification } from '../../../../hooks/useNotification'
import { BORDER_RADIUS, CARD_VARIANTS } from '../../../../theme/variants'
import { copyText } from '../../../../utils/clipboard'
import { consoleContainerApi } from '../../../../services/api/consoleContainer'

interface RemoteOneBotStepProps {
  instanceKey: string
}

const GUIDE_STEP_KEYS = ['open', 'addWs', 'fillToken', 'enable'] as const

export default function RemoteOneBotStep({ instanceKey }: RemoteOneBotStepProps) {
  const { t } = useTranslation('console-setup')
  const theme = useTheme()
  const notification = useNotification()

  const { data: config, isLoading } = useQuery({
    queryKey: ['console-network-config', instanceKey],
    queryFn: () => consoleContainerApi.getNetworkConfig(instanceKey),
    enabled: Boolean(instanceKey),
  })

  const handleCopy = async (value: string) => {
    if (!value) return
    const ok = await copyText(value)
    if (ok) {
      notification.success(t('common.copied'))
    } else {
      notification.error(t('common.copyFailed'))
    }
  }

  // 接入信息取 NekroAgent 托管的那条连接：它的地址与令牌由后端生成，正是用户要抄进协议端的值
  const managed = config?.entries.find(entry => entry.managed_by_nekro)
  const fields = [
    { key: 'wsUrl', label: t('remote.wsUrl'), value: managed?.url ?? '' },
    { key: 'accessToken', label: t('remote.accessToken'), value: managed?.token ?? '' },
  ]

  if (isLoading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
        <CircularProgress />
      </Box>
    )
  }

  return (
    <Stack spacing={2}>
      <Alert severity="success">{t('remote.noContainerNotice')}</Alert>

      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent>
          <Stack spacing={2}>
            {fields.map(field => (
              <TextField
                key={field.key}
                fullWidth
                size="small"
                label={field.label}
                value={field.value}
                InputProps={{
                  readOnly: true,
                  endAdornment: (
                    <InputAdornment position="end">
                      <IconActionButton
                        size="small"
                        aria-label={t('common.copy')}
                        onClick={() => void handleCopy(field.value)}
                      >
                        <ContentCopyIcon fontSize="small" />
                      </IconActionButton>
                    </InputAdornment>
                  ),
                }}
              />
            ))}
          </Stack>
        </CardContent>
      </Card>

      <Box
        sx={{
          borderRadius: BORDER_RADIUS.DEFAULT,
          backgroundColor: alpha(theme.palette.primary.main, 0.06),
          p: 2,
        }}
      >
        <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 1 }}>
          {t('remote.guideTitle')}
        </Typography>
        <Stack component="ol" spacing={0.75} sx={{ pl: 2.5, m: 0 }}>
          {GUIDE_STEP_KEYS.map(key => (
            <Typography key={key} component="li" variant="body2" color="text.secondary">
              {t(`remote.guide.${key}`)}
            </Typography>
          ))}
        </Stack>
      </Box>

      <Typography variant="caption" color="text.secondary">
        {t('remote.claimHint')}
      </Typography>
    </Stack>
  )
}
