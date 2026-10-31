import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Box, CircularProgress, Stack, Typography, alpha, useTheme } from '@mui/material'
import { QRCodeSVG } from 'qrcode.react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import RefreshIcon from '@mui/icons-material/Refresh'
import BoltIcon from '@mui/icons-material/Bolt'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import NekroDialog from '../../../components/common/NekroDialog'
import ActionButton from '../../../components/common/ActionButton'
import { useNotification } from '../../../hooks/useNotification'
import { BORDER_RADIUS } from '../../../theme/variants'
import { consoleContainerApi, toQrCodeDataUri } from '../../../services/api/consoleContainer'

interface QrCodeLoginDialogProps {
  open: boolean
  instanceKey: string
  onClose: () => void
  /** 登录成功后回调，供外部刷新实例/容器状态 */
  onLoggedIn?: () => void
}

/** 二维码轮询间隔：NapCat 的二维码有效期较短，3s 足够跟上刷新且不会打满后端 */
const POLL_INTERVAL_MS = 3000
/** 轮询上限，超过后停止自动刷新，改为由用户手动继续，避免弹窗被遗忘后长期占用连接 */
const MAX_POLL_MS = 5 * 60 * 1000

export default function QrCodeLoginDialog({
  open,
  instanceKey,
  onClose,
  onLoggedIn,
}: QrCodeLoginDialogProps) {
  const { t } = useTranslation('console-setup')
  const theme = useTheme()
  const notification = useNotification()

  const [loggedIn, setLoggedIn] = useState(false)
  const [pollingStopped, setPollingStopped] = useState(false)
  /** 后端在本轮把哪个账号回写给了本实例；空串表示没有回写发生 */
  const [boundAccountId, setBoundAccountId] = useState('')
  const sawQrCodeRef = useRef(false)
  const pollStartedAtRef = useRef(Date.now())
  /**
   * `onLoggedIn` 本轮是否已经通知过。
   *
   * 它是**每次都新建的内联箭头函数**（两个调用点都是），所以只要它触发的刷新让父组件重渲染，
   * 依赖数组里的 `onLoggedIn` 就换了身份、本 effect 再跑一遍、于是再通知一次——
   * 「通知 → 失效查询 → 重渲染 → 再通知」形成自激循环，一直打请求。
   * 登录成功是**一次性事件**，用 ref 钉住只发一次。
   */
  const notifiedRef = useRef(false)

  const enabled = open && Boolean(instanceKey) && !loggedIn && !pollingStopped

  const { data, isFetching, refetch } = useQuery({
    queryKey: ['console-container-qrcode', instanceKey],
    queryFn: () => consoleContainerApi.getQrCode(instanceKey),
    enabled,
    refetchInterval: enabled ? POLL_INTERVAL_MS : false,
    refetchOnWindowFocus: false,
    gcTime: 0,
  })

  const resetPolling = useCallback(() => {
    sawQrCodeRef.current = false
    notifiedRef.current = false
    pollStartedAtRef.current = Date.now()
    setLoggedIn(false)
    setPollingStopped(false)
    setBoundAccountId('')
  }, [])

  /** 登录成功只通知一次外部，见 `notifiedRef` */
  const notifyLoggedIn = useCallback(() => {
    if (notifiedRef.current) return
    notifiedRef.current = true
    onLoggedIn?.()
  }, [onLoggedIn])

  useEffect(() => {
    if (open) resetPolling()
  }, [open, resetPolling])

  useEffect(() => {
    if (!open || !data) return

    // 后端回写成功即为「登录成功」的权威判据：它是直接问容器要到的账号号码，
    // 比「二维码消失了」这条间接推断可靠得多（二维码也可能只是过期）
    if (data.bound_account_id) {
      setBoundAccountId(data.bound_account_id)
      setLoggedIn(true)
      notifyLoggedIn()
      return
    }

    if (data.available) {
      sawQrCodeRef.current = true
      return
    }

    // 曾经拿到过二维码，随后二维码消失 —— 判定为扫码完成
    if (sawQrCodeRef.current) {
      setLoggedIn(true)
      notifyLoggedIn()
      return
    }

    if (Date.now() - pollStartedAtRef.current > MAX_POLL_MS) {
      setPollingStopped(true)
    }
  }, [data, open, notifyLoggedIn])

  const quickLoginMutation = useMutation({
    mutationFn: () => consoleContainerApi.quickLogin(instanceKey),
    onSuccess: result => {
      notification.success(result.message || t('login.quickLoginRequested'))
      void refetch()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const qrImage = useMemo(() => toQrCodeDataUri(data?.qrcode_base64 ?? ''), [data?.qrcode_base64])
  const qrUrl = data?.qrcode_url ?? ''
  const hasQr = Boolean(data?.available) && (Boolean(qrImage) || Boolean(qrUrl))

  const handleManualRefresh = () => {
    resetPolling()
    void refetch()
  }

  return (
    <NekroDialog
      open={open}
      onClose={onClose}
      maxWidth="xs"
      title={t('login.title')}
      dividers
      actions={
        <Stack direction="row" spacing={1}>
          <ActionButton
            tone="ghost"
            startIcon={<BoltIcon />}
            disabled={quickLoginMutation.isPending || loggedIn}
            onClick={() => quickLoginMutation.mutate()}
          >
            {t('login.quickLogin')}
          </ActionButton>
          <ActionButton tone="secondary" startIcon={<RefreshIcon />} onClick={handleManualRefresh}>
            {t('login.refresh')}
          </ActionButton>
          <ActionButton tone="primary" onClick={onClose}>
            {t('common.close')}
          </ActionButton>
        </Stack>
      }
    >
      <Stack spacing={2} alignItems="center" sx={{ py: 1 }}>
        {loggedIn ? (
          <Stack spacing={1} alignItems="center" sx={{ py: 4 }}>
            <CheckCircleIcon sx={{ fontSize: 56, color: theme.palette.success.main }} />
            <Typography variant="subtitle1">{t('login.success')}</Typography>
            <Typography variant="body2" color="text.secondary" textAlign="center">
              {boundAccountId
                ? t('login.boundHint', { account: boundAccountId })
                : t('login.successHint')}
            </Typography>
          </Stack>
        ) : (
          <>
            <Box
              sx={{
                width: 232,
                height: 232,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                borderRadius: BORDER_RADIUS.DEFAULT,
                border: `1px solid ${alpha(theme.palette.divider, 0.8)}`,
                backgroundColor: theme.palette.background.paper,
                p: 1.5,
                boxSizing: 'border-box',
              }}
            >
              {hasQr ? (
                qrImage ? (
                  <Box
                    component="img"
                    src={qrImage}
                    alt={t('login.qrAlt')}
                    sx={{ width: '100%', height: '100%', objectFit: 'contain' }}
                  />
                ) : (
                  <QRCodeSVG
                    value={qrUrl}
                    size={200}
                    level="M"
                    bgColor={theme.palette.background.paper}
                    fgColor={theme.palette.text.primary}
                  />
                )
              ) : (
                <Stack spacing={1.5} alignItems="center">
                  {isFetching && !pollingStopped ? <CircularProgress size={28} /> : null}
                  <Typography variant="body2" color="text.secondary" textAlign="center">
                    {data?.message || t('login.waiting')}
                  </Typography>
                </Stack>
              )}
            </Box>

            <Typography variant="body2" color="text.secondary" textAlign="center">
              {t('login.scanHint')}
            </Typography>

            {pollingStopped ? (
              <Alert severity="info" sx={{ width: '100%' }}>
                {t('login.pollingStopped')}
              </Alert>
            ) : null}

            {data && !data.available && data.message ? (
              <Alert severity="warning" sx={{ width: '100%' }}>
                {data.message}
              </Alert>
            ) : null}
          </>
        )}
      </Stack>
    </NekroDialog>
  )
}
