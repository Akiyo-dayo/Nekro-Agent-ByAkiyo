import { ReactNode } from 'react'
import { Box, Card, CardActionArea, Chip, Stack, Typography, alpha, useTheme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import ExtensionIcon from '@mui/icons-material/Extension'
import HubIcon from '@mui/icons-material/Hub'
import MemoryIcon from '@mui/icons-material/Memory'
import SmartToyIcon from '@mui/icons-material/SmartToy'
import { BORDER_RADIUS, CARD_VARIANTS, CHIP_VARIANTS } from '../../../../theme/variants'
import type { InstanceProvider } from '../../../../services/api/console'

interface ProviderStepProps {
  value: InstanceProvider
  onChange: (value: InstanceProvider) => void
}

interface ProviderOption {
  provider: InstanceProvider
  icon: ReactNode
  /** 资源开销强调色取自主题：warning 表示要额外吃内存，success 表示零开销 */
  costTone: 'warning' | 'success'
}

const PROVIDER_OPTIONS: ProviderOption[] = [
  { provider: 'builtin_napcat', icon: <SmartToyIcon />, costTone: 'warning' },
  { provider: 'remote_onebot', icon: <HubIcon />, costTone: 'success' },
  { provider: 'native', icon: <ExtensionIcon />, costTone: 'success' },
]

export default function ProviderStep({ value, onChange }: ProviderStepProps) {
  const { t } = useTranslation(['console-setup', 'console'])
  const theme = useTheme()

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t('provider.intro')}
      </Typography>

      <Stack spacing={1.5}>
        {PROVIDER_OPTIONS.map(option => {
          const selected = value === option.provider
          const costColor =
            option.costTone === 'warning' ? theme.palette.warning.main : theme.palette.success.main

          return (
            <Card
              key={option.provider}
              sx={{
                ...CARD_VARIANTS.default.styles,
                borderColor: selected ? alpha(theme.palette.primary.main, 0.5) : undefined,
                boxShadow: selected ? theme.shadows[6] : undefined,
                transform: selected ? 'translateY(-1px)' : undefined,
              }}
            >
              <CardActionArea
                onClick={() => onChange(option.provider)}
                sx={{ p: 2, alignItems: 'stretch' }}
              >
                <Stack direction="row" spacing={2} alignItems="flex-start">
                  <Box
                    sx={{
                      width: 40,
                      height: 40,
                      flexShrink: 0,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      borderRadius: BORDER_RADIUS.DEFAULT,
                      color: selected ? theme.palette.primary.main : theme.palette.text.secondary,
                      backgroundColor: alpha(
                        selected ? theme.palette.primary.main : theme.palette.text.secondary,
                        0.1
                      ),
                    }}
                  >
                    {option.icon}
                  </Box>

                  <Stack spacing={0.75} sx={{ flex: 1, minWidth: 0 }}>
                    <Stack
                      direction="row"
                      spacing={1}
                      alignItems="center"
                      flexWrap="wrap"
                      useFlexGap
                    >
                      <Typography
                        variant="subtitle1"
                        fontWeight={600}
                        color={selected ? 'primary.main' : 'text.primary'}
                      >
                        {t(`console:provider.${option.provider}`)}
                      </Typography>
                      {option.provider === 'builtin_napcat' ? (
                        <Chip
                          size="small"
                          label={t('provider.recommended')}
                          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.primary.main, true)}
                        />
                      ) : null}
                      {selected ? (
                        <CheckCircleIcon
                          fontSize="small"
                          sx={{ color: theme.palette.primary.main, ml: 'auto' }}
                        />
                      ) : null}
                    </Stack>

                    <Typography variant="body2" color="text.secondary">
                      {t(`provider.${option.provider}.description`)}
                    </Typography>

                    <Stack direction="row" spacing={0.75} alignItems="center">
                      <MemoryIcon sx={{ fontSize: 16, color: costColor }} />
                      <Typography variant="caption" sx={{ color: costColor, fontWeight: 600 }}>
                        {t(`provider.${option.provider}.cost`)}
                      </Typography>
                    </Stack>
                  </Stack>
                </Stack>
              </CardActionArea>
            </Card>
          )
        })}
      </Stack>
    </Stack>
  )
}
