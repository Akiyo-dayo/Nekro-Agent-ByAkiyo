import {
  Alert,
  Box,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Stack,
  Typography,
  useTheme,
} from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import RefreshIcon from '@mui/icons-material/Refresh'
import ActionButton from '../../../components/common/ActionButton'
import { CARD_VARIANTS, CHIP_VARIANTS } from '../../../theme/variants'
import {
  getOnebotEndpointStatus,
  type OnebotSelfReportState,
} from '../../../services/api/consoleOnebot'

interface OnebotEndpointCardProps {
  instanceKey: string
}

const SELF_REPORT_KEYS: Record<OnebotSelfReportState, string> = {
  on: 'onebotEndpoint.selfReportState.on',
  off_suspected: 'onebotEndpoint.selfReportState.off_suspected',
  unknown: 'onebotEndpoint.selfReportState.unknown',
}

/**
 * OneBot 协议端状态卡片
 *
 * 只读：挂载时取一次，不轮询。开关开没开无法直接查询，后端看的是本次连接里 NA 自己发出的
 * 消息有没有被回传，所以这里只展示「本次连接以来」的条数，并给管理员一个手动刷新的入口。
 */
export default function OnebotEndpointCard({ instanceKey }: OnebotEndpointCardProps) {
  const { t } = useTranslation('console')
  const theme = useTheme()

  const query = useQuery({
    queryKey: ['console-onebot-endpoint', instanceKey],
    queryFn: () => getOnebotEndpointStatus(instanceKey),
    enabled: Boolean(instanceKey),
    refetchOnWindowFocus: false,
  })

  const status = query.data

  const selfReportChipSx = (state: OnebotSelfReportState) => {
    if (state === 'on') return CHIP_VARIANTS.getCustomColorChip(theme.palette.success.main, true)
    if (state === 'off_suspected') return CHIP_VARIANTS.getCustomColorChip(theme.palette.warning.main, true)
    return CHIP_VARIANTS.getCustomColorChip(theme.palette.text.secondary, true)
  }

  const hintKey =
    status?.implementation === 'snowluma' || status?.implementation === 'napcat'
      ? `onebotEndpoint.hints.${status.implementation}`
      : 'onebotEndpoint.hints.other'

  return (
    <Card sx={CARD_VARIANTS.default.styles}>
      <CardContent>
        <Stack spacing={2}>
          <Stack direction="row" alignItems="center" justifyContent="space-between" gap={1}>
            <Typography variant="subtitle1" fontWeight={600}>
              {t('onebotEndpoint.title')}
            </Typography>
            <ActionButton
              tone="ghost"
              size="small"
              startIcon={<RefreshIcon />}
              disabled={query.isFetching}
              onClick={() => void query.refetch()}
            >
              {t('onebotEndpoint.refresh')}
            </ActionButton>
          </Stack>

          {query.isLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}>
              <CircularProgress size={24} />
            </Box>
          ) : query.isError ? (
            <Alert severity="error">{t('onebotEndpoint.loadFailed')}</Alert>
          ) : !status || !status.online ? (
            <Stack direction="row" spacing={1} alignItems="center">
              <Typography variant="caption" color="text.secondary">
                {t('onebotEndpoint.protocol')}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {t('onebotEndpoint.offline')}
              </Typography>
            </Stack>
          ) : (
            <Stack spacing={1.5}>
              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                <Typography variant="caption" color="text.secondary">
                  {t('onebotEndpoint.protocol')}
                </Typography>
                <Typography variant="body2">
                  {status.app_name
                    ? `${status.app_name} ${status.app_version}`.trim()
                    : t('onebotEndpoint.unknown')}
                </Typography>
              </Stack>

              <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                <Typography variant="caption" color="text.secondary">
                  {t('onebotEndpoint.selfReport')}
                </Typography>
                <Chip
                  size="small"
                  label={t(SELF_REPORT_KEYS[status.self_report])}
                  sx={selfReportChipSx(status.self_report)}
                />
              </Stack>

              {status.self_report === 'off_suspected' ? (
                <Alert severity="warning">
                  <Typography variant="body2">{t(hintKey)}</Typography>
                  <Typography variant="body2" sx={{ mt: 1 }}>
                    {t('onebotEndpoint.hints.suffix')}
                  </Typography>
                </Alert>
              ) : null}

              {status.self_report === 'unknown' ? (
                <Typography variant="caption" color="text.secondary">
                  {t('onebotEndpoint.undetermined')}
                </Typography>
              ) : null}

              <Typography variant="caption" color="text.secondary">
                {t('onebotEndpoint.counts', { ownSends: status.own_sends, echoes: status.echoes })}
              </Typography>
            </Stack>
          )}
        </Stack>
      </CardContent>
    </Card>
  )
}
