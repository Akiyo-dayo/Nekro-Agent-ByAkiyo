import { useState } from 'react'
import {
  Alert,
  Box,
  CircularProgress,
  Stack,
  Tab,
  Tabs,
  TextField,
  Typography,
  alpha,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { QRCodeSVG } from 'qrcode.react'
import QrCode2Icon from '@mui/icons-material/QrCode2'
import LinkIcon from '@mui/icons-material/Link'
import ActionButton from '../../../components/common/ActionButton'
import { useNotification } from '../../../hooks/useNotification'
import { BORDER_RADIUS } from '../../../theme/variants'
import { consoleQbobApi, type QbobBotInfo } from '../../../services/api/consoleQbob'

interface QbobConnectCardProps {
  instanceKey: string
  onAttached: (bot: QbobBotInfo) => void
}

/** 扫码结果轮询间隔；qbob 的绑定任务保留 10 分钟 */
const QR_POLL_INTERVAL_MS = 2000
/** 连续失败这么多次就停：qbob 不可达时每 2 秒报一次错，没必要一直刷 */
const QR_POLL_MAX_FAILURES = 5

/**
 * 把一个 QQ 官方机器人接到本实例：扫码（QQ 开放平台的「扫码连接」）或手填 AppID / AppSecret
 *
 * 两条路都由后端完成「先建 bot → 绑账号 → 再接线」；AppSecret 只在手填时出现在请求体里一次，
 * 扫码拿到的 Secret 留在后端，不回到浏览器。
 */
export default function QbobConnectCard({ instanceKey, onAttached }: QbobConnectCardProps) {
  const { t } = useTranslation('console-setup')
  const theme = useTheme()
  const notification = useNotification()

  const [mode, setMode] = useState<'scan' | 'manual'>('scan')
  const [taskId, setTaskId] = useState('')
  const [connectUrl, setConnectUrl] = useState('')
  const [scanMessage, setScanMessage] = useState('')
  const [pollFailures, setPollFailures] = useState(0)
  const [pollError, setPollError] = useState('')
  const [appid, setAppid] = useState('')
  const [secret, setSecret] = useState('')

  const startMutation = useMutation({
    mutationFn: () => consoleQbobApi.startQrConnect(instanceKey),
    onSuccess: result => {
      setTaskId(result.task_id)
      setConnectUrl(result.connect_url)
      setScanMessage('')
      setPollFailures(0)
      setPollError('')
    },
    onError: (error: Error) => notification.error(error.message),
  })

  // 轮询期间只看 status；一旦不是 pending 就停，结果由下面的 effect 式回调处理
  const pollQuery = useQuery({
    queryKey: ['console-qbob-qr', instanceKey, taskId],
    queryFn: async () => {
      try {
        const result = await consoleQbobApi.pollQrConnect(instanceKey, taskId)
        setPollFailures(0)
        setPollError('')
        if (result.status === 'attached' && result.bot) {
          setTaskId('')
          setConnectUrl('')
          onAttached(result.bot)
        } else if (result.status !== 'pending') {
          setTaskId('')
          setConnectUrl('')
          setScanMessage(result.message)
        }
        return result
      } catch (error) {
        setPollFailures(count => count + 1)
        setPollError((error as Error).message)
        throw error
      }
    },
    enabled: Boolean(taskId) && pollFailures < QR_POLL_MAX_FAILURES,
    refetchInterval: pollFailures < QR_POLL_MAX_FAILURES ? QR_POLL_INTERVAL_MS : false,
    retry: false,
  })

  const attachMutation = useMutation({
    mutationFn: () => consoleQbobApi.attach(instanceKey, appid.trim(), secret.trim()),
    onSuccess: bot => {
      setSecret('')
      onAttached(bot)
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const appidValid = /^\d+$/.test(appid.trim())

  return (
    <Stack spacing={2}>
      <Tabs
        value={mode}
        onChange={(_, value: 'scan' | 'manual') => {
          // 切走就放弃这次扫码：后台继续轮询的话，扫码结果会和手填的提交撞在一起
          setTaskId('')
          setConnectUrl('')
          setPollFailures(0)
          setPollError('')
          setMode(value)
        }}
      >
        <Tab value="scan" label={t('qbob.connect.scanTab')} />
        <Tab value="manual" label={t('qbob.connect.manualTab')} />
      </Tabs>

      {mode === 'scan' ? (
        <Stack spacing={1.5} alignItems="flex-start">
          <Typography variant="body2" color="text.secondary">
            {t('qbob.connect.scanIntro')}
          </Typography>

          {connectUrl ? (
            <Stack spacing={1} alignItems="center" sx={{ alignSelf: 'center' }}>
              <Box
                sx={{
                  p: 1.5,
                  borderRadius: BORDER_RADIUS.DEFAULT,
                  border: `1px solid ${alpha(theme.palette.divider, 0.8)}`,
                  backgroundColor: theme.palette.background.paper,
                }}
              >
                <QRCodeSVG
                  value={connectUrl}
                  size={200}
                  level="M"
                  bgColor={theme.palette.background.paper}
                  fgColor={theme.palette.text.primary}
                />
              </Box>
              <Stack direction="row" spacing={1} alignItems="center">
                {pollQuery.isFetching ? <CircularProgress size={14} /> : null}
                <Typography variant="caption" color="text.secondary">
                  {t('qbob.connect.waitingScan')}
                </Typography>
              </Stack>
            </Stack>
          ) : null}

          {scanMessage ? (
            <Alert severity="warning" sx={{ width: '100%' }}>
              {scanMessage}
            </Alert>
          ) : null}
          {pollFailures >= QR_POLL_MAX_FAILURES ? (
            <Alert severity="error" sx={{ width: '100%' }}>
              {t('qbob.connect.pollStopped')}
              {pollError ? ` ${pollError}` : ''}
            </Alert>
          ) : pollQuery.error ? (
            <Alert severity="error" sx={{ width: '100%' }}>
              {(pollQuery.error as Error).message}
            </Alert>
          ) : null}

          <ActionButton
            tone={connectUrl ? 'secondary' : 'primary'}
            startIcon={<QrCode2Icon />}
            disabled={startMutation.isPending}
            onClick={() => startMutation.mutate()}
          >
            {connectUrl ? t('qbob.connect.regenerate') : t('qbob.connect.generate')}
          </ActionButton>
        </Stack>
      ) : (
        <Stack spacing={1.5}>
          <Typography variant="body2" color="text.secondary">
            {t('qbob.connect.manualIntro')}
          </Typography>
          <TextField
            size="small"
            label={t('qbob.connect.appid')}
            value={appid}
            onChange={event => setAppid(event.target.value)}
            error={Boolean(appid) && !appidValid}
            helperText={appid && !appidValid ? t('qbob.connect.appidInvalid') : ' '}
            inputProps={{ inputMode: 'numeric' }}
          />
          <TextField
            size="small"
            type="password"
            autoComplete="new-password"
            label={t('qbob.connect.secret')}
            value={secret}
            onChange={event => setSecret(event.target.value)}
            helperText={t('qbob.connect.secretHint')}
          />
          <Box>
            <ActionButton
              tone="primary"
              startIcon={<LinkIcon />}
              disabled={!appidValid || !secret.trim() || attachMutation.isPending}
              onClick={() => attachMutation.mutate()}
            >
              {t('qbob.connect.attach')}
            </ActionButton>
          </Box>
        </Stack>
      )}
    </Stack>
  )
}
