import { useState } from 'react'
import { Alert, Box, CircularProgress, Stack, Typography } from '@mui/material'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { consoleQbobApi, type QbobBotInfo } from '../../../../services/api/consoleQbob'
import QbobConnectCard from '../../components/QbobConnectCard'
import QbobLicenseNote from '../../components/QbobLicenseNote'
import QbobServiceCredentialForm from '../../components/QbobServiceCredentialForm'

interface BuiltinQbobStepProps {
  instanceKey: string
}

/** 向导第三步（内置 qbob）：确认附带的 qbob 可用，然后扫码或手填接入一个 QQ 官方机器人 */
export default function BuiltinQbobStep({ instanceKey }: BuiltinQbobStepProps) {
  const { t } = useTranslation('console-setup')
  const queryClient = useQueryClient()
  const [attached, setAttached] = useState<QbobBotInfo | null>(null)

  const serviceQuery = useQuery({
    queryKey: ['console-qbob-service'],
    queryFn: () => consoleQbobApi.getService(),
  })

  const handleAttached = (bot: QbobBotInfo) => {
    setAttached(bot)
    // 后端在接入这一刻把 self_id 绑到了实例上，实例查询要跟着失效（与 NapCat 扫码同一条理由）
    void queryClient.invalidateQueries({ queryKey: ['console-instance', instanceKey] })
    void queryClient.invalidateQueries({ queryKey: ['console-instances'] })
    void queryClient.invalidateQueries({ queryKey: ['console-qbob-bot', instanceKey] })
  }

  if (serviceQuery.isLoading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
        <CircularProgress />
      </Box>
    )
  }

  const service = serviceQuery.data
  const ready = Boolean(service?.configured && service.reachable && service.service_account_ready)
  // 连得上 qbob、却拿不到服务账号：只有这一种情况是平台方当场能补救的
  const needsCredential = Boolean(service?.configured && service.reachable && !service.service_account_ready)

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t('qbob.intro')}
      </Typography>

      {serviceQuery.error ? <Alert severity="error">{(serviceQuery.error as Error).message}</Alert> : null}
      {service && !ready ? (
        <Alert severity="warning">{service.message || t('qbob.serviceUnavailable')}</Alert>
      ) : null}
      {needsCredential ? <QbobServiceCredentialForm /> : null}

      {attached ? (
        <Alert severity="success">
          {t('qbob.attachedHint', { appid: attached.appid, selfId: attached.self_id })}
        </Alert>
      ) : ready ? (
        <QbobConnectCard instanceKey={instanceKey} onAttached={handleAttached} />
      ) : null}

      <Typography variant="caption" color="text.secondary">
        {t('qbob.groupHint')}
      </Typography>
      <QbobLicenseNote qbobRef={service?.qbob_ref} />
    </Stack>
  )
}
