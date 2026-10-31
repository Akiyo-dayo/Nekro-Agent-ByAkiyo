/**
 * 运维操作确认页
 *
 * 运维 Bot 在聊天频道里**不执行**写操作，只发一条一次性深链，落地就是这一页。
 * 本页存在的唯一理由是「让人在一个有会话认证的界面里看清楚要做什么再点」，
 * 因此有三条不能省的约定：
 *
 * 1. **先预览再执行**：进页面只调 preview（不消费令牌），确认按钮才调 confirm。
 *    合成一步的话，浏览器预取或聊天软件的链接预览就能把令牌白白消耗掉。
 * 2. **必须显示后端给的 summary**：不显示就等于让人盲签。文案来自后端的
 *    `OpsActionSpec.summary`，前端不自行改写、不做「更友好」的转述。
 * 3. **不在前端做权限判断**：`isPlatformOwner` 只用来给一句更好懂的提示，
 *    真正的拦截在后端 `require_platform_owner()`。
 */

import { useCallback, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Alert, Box, Card, CardContent, Chip, CircularProgress, Stack, Typography } from '@mui/material'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import { useIdentity } from '../../hooks/useIdentity'
import { useNotification } from '../../hooks/useNotification'
import { CONSOLE_OPS_QUERY_KEYS, consoleOpsApi } from '../../services/api/consoleOps'

/** 后端返回的 unix 秒，展示成本地时间 */
const formatExpiry = (unixSeconds: number): string => new Date(unixSeconds * 1000).toLocaleString()

export default function OpsActionConfirmPage() {
  const { t } = useTranslation('console-ops')
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const notification = useNotification()
  const { isPlatformOwner } = useIdentity()
  const [doneMessage, setDoneMessage] = useState('')

  const token = useMemo(() => searchParams.get('token') ?? '', [searchParams])

  const previewQuery = useQuery({
    queryKey: CONSOLE_OPS_QUERY_KEYS.preview(token),
    queryFn: () => consoleOpsApi.preview(token),
    enabled: !!token,
    retry: false,
    // 令牌短时效且一次性，重新聚焦窗口时再拉一次没有意义，只会多打一次后端
    refetchOnWindowFocus: false,
  })

  const confirmMutation = useMutation({
    mutationFn: () => consoleOpsApi.confirm(token),
    onSuccess: response => {
      setDoneMessage(response.message)
      notification.success(response.message)
    },
    onError: (error: Error) => {
      notification.error(error.message)
    },
  })

  const goBack = useCallback(() => navigate('/console'), [navigate])

  if (!token) {
    return (
      <Box sx={{ p: 3, maxWidth: 720 }}>
        <Alert severity="error">{t('missingToken')}</Alert>
      </Box>
    )
  }

  const preview = previewQuery.data

  return (
    <Box sx={{ p: 3, maxWidth: 720 }}>
      <Typography variant="h5" sx={{ mb: 1 }}>
        {t('title')}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {t('subtitle')}
      </Typography>

      {!isPlatformOwner && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          {t('notOwnerHint')}
        </Alert>
      )}

      {previewQuery.isLoading && (
        <Stack direction="row" spacing={1} alignItems="center">
          <CircularProgress size={18} />
          <Typography variant="body2">{t('loading')}</Typography>
        </Stack>
      )}

      {previewQuery.isError && (
        <Alert severity="error" sx={{ mb: 2 }}>
          <Typography variant="subtitle2">{t('invalidTitle')}</Typography>
          {(previewQuery.error as Error).message}
        </Alert>
      )}

      {preview && (
        <Card variant="outlined">
          <CardContent>
            <Stack spacing={1.5}>
              <Stack direction="row" spacing={1} alignItems="center">
                <Chip size="small" label={preview.action} />
                <Typography variant="caption" color="text.secondary">
                  {t('fields.expiresAt')}: {formatExpiry(preview.expires_at)}
                </Typography>
              </Stack>

              <Box>
                <Typography variant="caption" color="text.secondary">
                  {t('fields.summary')}
                </Typography>
                <Typography variant="body1">{preview.summary}</Typography>
              </Box>

              {!!preview.target && (
                <Box>
                  <Typography variant="caption" color="text.secondary">
                    {preview.target_label || t('fields.target')}
                  </Typography>
                  <Typography variant="body1">{preview.target}</Typography>
                </Box>
              )}

              <Box>
                <Typography variant="caption" color="text.secondary">
                  {t('fields.issuer')}
                </Typography>
                <Typography variant="body2">
                  {preview.issuer_user_id} @ {preview.issuer_chat_key}
                </Typography>
              </Box>

              <Typography variant="caption" color="text.secondary">
                {t('oneTimeHint')}
              </Typography>

              {!!doneMessage && (
                <Alert severity="success">
                  <Typography variant="subtitle2">{t('doneTitle')}</Typography>
                  {doneMessage}
                </Alert>
              )}

              <Stack direction="row" spacing={1}>
                <ActionButton
                  tone="danger"
                  disabled={confirmMutation.isPending || !!doneMessage}
                  onClick={() => confirmMutation.mutate()}
                >
                  {confirmMutation.isPending ? t('confirming') : t('confirm')}
                </ActionButton>
                <ActionButton tone="ghost" onClick={goBack}>
                  {t('cancel')}
                </ActionButton>
              </Stack>
            </Stack>
          </CardContent>
        </Card>
      )}
    </Box>
  )
}
