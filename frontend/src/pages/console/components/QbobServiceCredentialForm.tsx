import { useState } from 'react'
import { Box, Stack, TextField, Typography } from '@mui/material'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import KeyIcon from '@mui/icons-material/Key'
import ActionButton from '../../../components/common/ActionButton'
import { useNotification } from '../../../hooks/useNotification'
import { consoleQbobApi } from '../../../services/api/consoleQbob'

/**
 * 平台方手工提供 NA 访问 qbob 用的账号（qbob 的 admin 初始口令文件已不在、NA 又没有服务账号时）
 *
 * 口令只写不读：提交后清空输入框，后端加密落盘，任何接口都不回传。
 */
export default function QbobServiceCredentialForm() {
  const { t } = useTranslation('console-setup')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  const saveMutation = useMutation({
    mutationFn: () => consoleQbobApi.setServiceCredential(username.trim(), password),
    onSuccess: result => {
      setPassword('')
      notification.success(result.message || t('qbob.credential.saved'))
      void queryClient.invalidateQueries({ queryKey: ['console-qbob-service'] })
      void queryClient.invalidateQueries({ queryKey: ['console-qbob-bot'] })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  return (
    <Stack spacing={1.5}>
      <Typography variant="subtitle2">{t('qbob.credential.title')}</Typography>
      <Typography variant="body2" color="text.secondary">
        {t('qbob.credential.intro')}
      </Typography>
      <TextField
        size="small"
        label={t('qbob.credential.username')}
        value={username}
        onChange={event => setUsername(event.target.value)}
        autoComplete="off"
      />
      <TextField
        size="small"
        type="password"
        autoComplete="new-password"
        label={t('qbob.credential.password')}
        value={password}
        onChange={event => setPassword(event.target.value)}
        helperText={t('qbob.credential.passwordHint')}
      />
      <Box>
        <ActionButton
          tone="primary"
          startIcon={<KeyIcon />}
          disabled={!username.trim() || password.length < 8 || saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {t('qbob.credential.save')}
        </ActionButton>
      </Box>
    </Stack>
  )
}
