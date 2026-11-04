import { useState } from 'react'
import {
  Alert,
  Box,
  Card,
  CardContent,
  Divider,
  InputAdornment,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import ActionButton from '../../../components/common/ActionButton'
import IconActionButton from '../../../components/common/IconActionButton'
import NekroDialog from '../../../components/common/NekroDialog'
import { useNotification } from '../../../hooks/useNotification'
import { CARD_VARIANTS } from '../../../theme/variants'
import { copyText } from '../../../utils/clipboard'
import {
  consoleApi,
  type InstanceAccessTokenRotateResponse,
  type InstanceInfo,
} from '../../../services/api/console'

interface AccessTokenCardProps {
  instance: InstanceInfo
}

/** 各形态在确认框里额外多说一句的键；原生适配器没有令牌，不会走到这里 */
const PROVIDER_NOTICE_KEYS: Record<string, string> = {
  builtin_napcat: 'accessToken.confirm.napcat',
  remote_onebot: 'accessToken.confirm.remote',
  builtin_qbob: 'accessToken.confirm.qbob',
}

/** 送达失败时按形态提示下一步 */
const FAILED_HINT_KEYS: Record<string, string> = {
  builtin_napcat: 'accessToken.failedHint.napcat',
  builtin_qbob: 'accessToken.failedHint.qbob',
  remote_onebot: 'accessToken.failedHint.remote',
}

/**
 * 实例接入令牌
 *
 * 换令牌是一次**会断开在线连接、还可能重启容器**的写操作，因此入口只给平台方
 * （后端 `POST /instances/{key}/access-token/rotate` 是 `Role.Super`），且要先过一道确认框。
 *
 * 结果视图留在同一个对话框里：远程协议端的新令牌只在这条响应里出现一次，关掉对话框
 * 就等于把「抄进协议端配置」这一步的唯一凭据弄丢了，所以成功之后不自动关闭，
 * 由人复制完再关。
 */
export default function AccessTokenCard({ instance }: AccessTokenCardProps) {
  const { t } = useTranslation('console')
  const notification = useNotification()
  const queryClient = useQueryClient()

  const [open, setOpen] = useState(false)
  const [result, setResult] = useState<InstanceAccessTokenRotateResponse | null>(null)

  const rotateMutation = useMutation({
    mutationFn: () => consoleApi.rotateAccessToken(instance.instance_key),
    onSuccess: data => {
      setResult(data)
      // 新令牌改了元数据、断开了连接、还可能重启了容器：这几份查询都得重取
      void queryClient.invalidateQueries({ queryKey: ['console-instance', instance.instance_key] })
      void queryClient.invalidateQueries({ queryKey: ['console-instances'] })
      void queryClient.invalidateQueries({ queryKey: ['console-network-config', instance.instance_key] })
      void queryClient.invalidateQueries({ queryKey: ['onebot-v11-onebot-token', instance.instance_key] })
      void queryClient.invalidateQueries({ queryKey: ['console-health-events', instance.instance_key] })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const close = () => {
    setOpen(false)
    // 关掉之后结果就没了：下一次打开必须重新走确认，不能让人误以为按钮还是「查看结果」
    setResult(null)
  }

  const handleCopy = async () => {
    if (!result?.token) return
    const ok = await copyText(result.token)
    if (ok) {
      notification.success(t('accessToken.copySuccess'))
    } else {
      notification.error(t('accessToken.copyFailed'))
    }
  }

  const providerNoticeKey = PROVIDER_NOTICE_KEYS[instance.provider]
  const failedHintKey = FAILED_HINT_KEYS[instance.provider] ?? 'accessToken.failedHint.other'

  return (
    <Card sx={CARD_VARIANTS.default.styles}>
      <CardContent>
        <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
          {t('accessToken.title')}
        </Typography>
        <Divider sx={{ mb: 1 }} />
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {t('accessToken.description')}
        </Typography>
        <ActionButton tone="secondary" size="small" onClick={() => setOpen(true)}>
          {t('accessToken.action')}
        </ActionButton>
      </CardContent>

      <NekroDialog
        open={open}
        onClose={close}
        title={result ? t('accessToken.resultTitle') : t('accessToken.confirm.title')}
        maxWidth="xs"
        actions={
          result ? (
            <ActionButton tone="primary" onClick={close}>
              {t('accessToken.close')}
            </ActionButton>
          ) : (
            <>
              <ActionButton tone="ghost" onClick={close}>
                {t('accessToken.cancel')}
              </ActionButton>
              <ActionButton
                tone="danger"
                disabled={rotateMutation.isPending}
                onClick={() => rotateMutation.mutate()}
              >
                {t('accessToken.confirm.submit')}
              </ActionButton>
            </>
          )
        }
      >
        {result ? (
          <Stack spacing={2} sx={{ pt: 1 }}>
            {result.delivery === 'manual' ? (
              <Alert severity="warning">{t('accessToken.delivery.manual')}</Alert>
            ) : result.delivery === 'failed' ? (
              <Alert severity="error">
                <Stack spacing={1}>
                  <Typography variant="body2">
                    {result.delivery_error || t('accessToken.delivery.failed')}
                  </Typography>
                  <Typography variant="body2">{t(failedHintKey)}</Typography>
                </Stack>
              </Alert>
            ) : (
              <Alert severity="success">{t(`accessToken.delivery.${result.delivery}`)}</Alert>
            )}

            <Typography variant="body2" color="text.secondary">
              {t('accessToken.disconnected', { n: result.disconnected })}
            </Typography>

            {/* 新令牌只在这条响应里出现一次：远程协议端要靠它抄进自己的配置 */}
            {result.delivery === 'manual' || result.delivery === 'failed' ? (
              <TextField
                fullWidth
                size="small"
                label={t('accessToken.tokenLabel')}
                value={result.token}
                InputProps={{
                  readOnly: true,
                  endAdornment: (
                    <InputAdornment position="end">
                      <IconActionButton
                        size="small"
                        aria-label={t('accessToken.copy')}
                        onClick={() => void handleCopy()}
                      >
                        <ContentCopyIcon fontSize="small" />
                      </IconActionButton>
                    </InputAdornment>
                  ),
                }}
              />
            ) : null}
          </Stack>
        ) : (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
            <Typography variant="body2" color="text.secondary">
              {t('accessToken.confirm.intro')}
            </Typography>
            {providerNoticeKey ? (
              <Typography variant="body2" color="text.secondary">
                {t(providerNoticeKey)}
              </Typography>
            ) : null}
          </Box>
        )}
      </NekroDialog>
    </Card>
  )
}
