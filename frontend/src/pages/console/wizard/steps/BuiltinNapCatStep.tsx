import { useState } from 'react'
import {
  Alert,
  Box,
  Card,
  CardContent,
  CircularProgress,
  LinearProgress,
  Stack,
  Typography,
  alpha,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import AddCircleOutlineIcon from '@mui/icons-material/AddCircleOutline'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import QrCode2Icon from '@mui/icons-material/QrCode2'
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked'
import ActionButton from '../../../../components/common/ActionButton'
import { useNotification } from '../../../../hooks/useNotification'
import { BORDER_RADIUS, CARD_VARIANTS } from '../../../../theme/variants'
import {
  CONTAINER_PRESENT_STATES,
  consoleContainerApi,
} from '../../../../services/api/consoleContainer'
import QrCodeLoginDialog from '../../components/QrCodeLoginDialog'

interface BuiltinNapCatStepProps {
  instanceKey: string
}

/** 容器创建阶段轮询更密一些，拉镜像/启动的状态变化需要及时反馈 */
const CREATE_POLL_INTERVAL_MS = 3000

type PhaseStatus = 'pending' | 'active' | 'done'

export default function BuiltinNapCatStep({ instanceKey }: BuiltinNapCatStepProps) {
  const { t } = useTranslation('console-setup')
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()

  const [qrDialogOpen, setQrDialogOpen] = useState(false)
  const [loginDone, setLoginDone] = useState(false)

  const containerQueryKey = ['console-container', instanceKey]

  const { data: container, isLoading } = useQuery({
    queryKey: containerQueryKey,
    queryFn: () => consoleContainerApi.getContainer(instanceKey),
    refetchInterval: CREATE_POLL_INTERVAL_MS,
    enabled: Boolean(instanceKey),
  })

  const createMutation = useMutation({
    mutationFn: () => consoleContainerApi.createContainer(instanceKey),
    onSuccess: result => {
      notification.success(result.message || t('panel.createStarted'))
      void queryClient.invalidateQueries({ queryKey: containerQueryKey })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const state = container?.state ?? 'absent'
  const containerPresent = CONTAINER_PRESENT_STATES.includes(state)
  const isRunning = state === 'running'
  const creating = createMutation.isPending || (containerPresent && !isRunning)

  const phases: { key: string; status: PhaseStatus }[] = [
    {
      key: 'create',
      status: containerPresent ? 'done' : createMutation.isPending ? 'active' : 'pending',
    },
    {
      key: 'start',
      status: isRunning ? 'done' : containerPresent ? 'active' : 'pending',
    },
    {
      key: 'login',
      status: loginDone ? 'done' : isRunning ? 'active' : 'pending',
    },
  ]

  const phaseColor = (status: PhaseStatus) =>
    status === 'done'
      ? theme.palette.success.main
      : status === 'active'
        ? theme.palette.primary.main
        : theme.palette.text.disabled

  if (isLoading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
        <CircularProgress />
      </Box>
    )
  }

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t('builtin.intro')}
      </Typography>

      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent>
          <Stack spacing={1.5}>
            {phases.map(phase => {
              const color = phaseColor(phase.status)
              return (
                <Stack key={phase.key} direction="row" spacing={1.5} alignItems="center">
                  <Box sx={{ width: 24, display: 'flex', justifyContent: 'center' }}>
                    {phase.status === 'done' ? (
                      <CheckCircleIcon sx={{ fontSize: 20, color }} />
                    ) : phase.status === 'active' ? (
                      <CircularProgress size={16} />
                    ) : (
                      <RadioButtonUncheckedIcon sx={{ fontSize: 20, color }} />
                    )}
                  </Box>
                  <Stack spacing={0.25} sx={{ flex: 1, minWidth: 0 }}>
                    <Typography variant="body2" sx={{ color, fontWeight: 600 }}>
                      {t(`builtin.phase.${phase.key}.title`)}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {t(`builtin.phase.${phase.key}.hint`)}
                    </Typography>
                  </Stack>
                </Stack>
              )
            })}

            {creating ? (
              <Box
                sx={{
                  borderRadius: BORDER_RADIUS.SMALL,
                  backgroundColor: alpha(theme.palette.primary.main, 0.06),
                  p: 1.25,
                }}
              >
                <LinearProgress sx={{ borderRadius: BORDER_RADIUS.PILL, mb: 1 }} />
                <Typography variant="caption" color="text.secondary">
                  {t('builtin.creatingHint')}
                </Typography>
              </Box>
            ) : null}
          </Stack>
        </CardContent>
      </Card>

      {!containerPresent ? (
        <Stack spacing={1.5} alignItems="flex-start">
          <Alert severity="info" sx={{ width: '100%' }}>
            {t('builtin.beforeCreate')}
          </Alert>
          <ActionButton
            tone="primary"
            startIcon={<AddCircleOutlineIcon />}
            disabled={createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {t('panel.createContainer')}
          </ActionButton>
        </Stack>
      ) : null}

      {isRunning && !loginDone ? (
        <Stack spacing={1.5} alignItems="flex-start">
          <Alert severity="success" sx={{ width: '100%' }}>
            {t('builtin.runningHint')}
          </Alert>
          <ActionButton
            tone="primary"
            startIcon={<QrCode2Icon />}
            onClick={() => setQrDialogOpen(true)}
          >
            {t('panel.scanLogin')}
          </ActionButton>
        </Stack>
      ) : null}

      {loginDone ? <Alert severity="success">{t('builtin.loginDone')}</Alert> : null}

      <QrCodeLoginDialog
        open={qrDialogOpen}
        instanceKey={instanceKey}
        onClose={() => setQrDialogOpen(false)}
        onLoggedIn={() => {
          setLoginDone(true)
          void queryClient.invalidateQueries({ queryKey: containerQueryKey })
          // 后端在登录成功这一刻已经把账号回写进实例行了，实例查询也要跟着失效，
          // 否则向导后续步骤与实例列表仍显示"未绑定"（与 NapCatPanel 同一条理由）
          void queryClient.invalidateQueries({ queryKey: ['console-instance', instanceKey] })
          void queryClient.invalidateQueries({ queryKey: ['console-instances'] })
        }}
      />
    </Stack>
  )
}
