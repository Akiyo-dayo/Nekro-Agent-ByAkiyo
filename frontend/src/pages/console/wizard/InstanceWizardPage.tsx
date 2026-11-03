import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Box,
  Card,
  CardContent,
  Stack,
  Step,
  StepLabel,
  Stepper,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../../components/common/ActionButton'
import { useIdentity } from '../../../hooks/useIdentity'
import { useNotification } from '../../../hooks/useNotification'
import { CARD_VARIANTS, SCROLLBAR_VARIANTS } from '../../../theme/variants'
import {
  consoleApi,
  consolePaths,
  type InstanceInfo,
  type InstanceProvider,
} from '../../../services/api/console'
import { INSTANCE_KEY_PATTERN } from '../../../services/api/consoleContainer'
import UnclaimedProtocolPanel from '../components/UnclaimedProtocolPanel'
import BasicInfoStep from './steps/BasicInfoStep'
import BuiltinNapCatStep from './steps/BuiltinNapCatStep'
import BuiltinQbobStep from './steps/BuiltinQbobStep'
import DoneStep from './steps/DoneStep'
import NativeAdapterStep from './steps/NativeAdapterStep'
import ProviderStep from './steps/ProviderStep'
import RemoteOneBotStep from './steps/RemoteOneBotStep'

const STEP_KEYS = ['provider', 'basic', 'setup', 'done'] as const

const ONEBOT_ADAPTER_KEY = 'onebot_v11'

const adapterConfigPath = (adapterKey: string) => `/adapters/${encodeURIComponent(adapterKey)}`

export default function InstanceWizardPage() {
  const { t } = useTranslation('console-setup')
  const { t: tTenant } = useTranslation('tenant')
  const theme = useTheme()
  const navigate = useNavigate()
  const notification = useNotification()
  const { isPlatformOwner } = useIdentity()
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))

  const [activeStep, setActiveStep] = useState(0)
  const [provider, setProvider] = useState<InstanceProvider>('builtin_napcat')
  const [displayName, setDisplayName] = useState('')
  const [instanceKey, setInstanceKey] = useState('')
  const [adapterKey, setAdapterKey] = useState(ONEBOT_ADAPTER_KEY)
  const [instance, setInstance] = useState<InstanceInfo | null>(null)

  const instanceKeyError =
    instanceKey && !INSTANCE_KEY_PATTERN.test(instanceKey) ? t('basic.instanceKeyInvalid') : null

  const createMutation = useMutation({
    mutationFn: () =>
      consoleApi.createInstance({
        instance_key: instanceKey.trim(),
        display_name: displayName.trim(),
        adapter_key: provider === 'native' ? adapterKey : ONEBOT_ADAPTER_KEY,
        provider,
      }),
    onSuccess: created => {
      setInstance(created)
      notification.success(t('wizard.instanceCreated', { name: created.instance_key }))
      setActiveStep(2)
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const handleProviderChange = (next: InstanceProvider) => {
    setProvider(next)
    setAdapterKey(next === 'native' ? '' : ONEBOT_ADAPTER_KEY)
  }

  const handleNext = () => {
    if (activeStep === 0) {
      setActiveStep(1)
      return
    }
    if (activeStep === 1) {
      createMutation.mutate()
      return
    }
    setActiveStep(3)
  }

  const nextDisabled =
    (activeStep === 1 &&
      (Boolean(instanceKeyError) ||
        createMutation.isPending ||
        (provider === 'native' && !adapterKey))) ||
    (activeStep === 2 && !instance)

  const renderSetupStep = () => {
    if (!instance) return null
    if (instance.provider === 'builtin_napcat') {
      return <BuiltinNapCatStep instanceKey={instance.instance_key} />
    }
    if (instance.provider === 'remote_onebot') {
      return <RemoteOneBotStep instanceKey={instance.instance_key} />
    }
    if (instance.provider === 'builtin_qbob') {
      return <BuiltinQbobStep instanceKey={instance.instance_key} />
    }
    return (
      <NativeAdapterStep
        adapterKey={instance.adapter_key}
        onOpenAdapterConfig={key => navigate(adapterConfigPath(key))}
      />
    )
  }

  /**
   * 新建实例是 `POST /console/instances`，`Role.Super`
   *
   * 入口（总后台首页与实例列表的「新建实例」按钮）已经按 `isPlatformOwner` 收敛，
   * 但这条 URL 直接敲得进来。不拦的话租户管理员会走完选协议端、填标识、点创建三步，
   * 在**最后一步**收到一句 403 —— 三步白填。判据与那两个按钮同源，本页只是第二道。
   */
  if (!isPlatformOwner) {
    return (
      <Box sx={{ p: { xs: 1.5, sm: 2, md: 3 }, height: '100%', boxSizing: 'border-box' }}>
        <Stack spacing={1.5} sx={{ maxWidth: 560, mx: 'auto', textAlign: 'center', pt: 6 }}>
          <Typography variant="subtitle1" fontWeight={700}>
            {tTenant('guard.title')}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {tTenant('guard.description')}
          </Typography>
          <Box>
            <ActionButton
              tone="primary"
              size="small"
              onClick={() => navigate(consolePaths.instanceList())}
            >
              {t('wizard.backToList')}
            </ActionButton>
          </Box>
        </Stack>
      </Box>
    )
  }

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 3 },
        height: '100%',
        overflowY: 'auto',
        boxSizing: 'border-box',
        ...(SCROLLBAR_VARIANTS.thin.styles as object),
      }}
    >
      <Stack spacing={2.5} sx={{ maxWidth: 760, mx: 'auto' }}>
        <Stack spacing={0.5}>
          <Typography variant="h5" fontWeight={600}>
            {t('wizard.title')}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {t('wizard.subtitle')}
          </Typography>
        </Stack>

        {/* 「先建一个空实例」这个动作本身就是坑的一半：协议端已经连上来时，管理员真正要做的是
            认领它，而不是再建一行永远拿不到连接的空壳。所以引导排在向导第一步之前 ——
            没有待认领协议端时面板整体不渲染，向导对新部署与改造前完全一样 */}
        {activeStep < 2 && (
          <UnclaimedProtocolPanel
            onClaimed={result => navigate(consolePaths.instanceDetail(result.instance.instance_key))}
          />
        )}

        <Stepper
          activeStep={activeStep}
          alternativeLabel={!isMobile}
          orientation={isMobile ? 'vertical' : 'horizontal'}
        >
          {STEP_KEYS.map(key => (
            <Step key={key}>
              <StepLabel>{t(`wizard.steps.${key}`)}</StepLabel>
            </Step>
          ))}
        </Stepper>

        <Card sx={CARD_VARIANTS.default.styles}>
          <CardContent sx={{ p: { xs: 2, md: 3 } }}>
            {activeStep === 0 ? (
              <ProviderStep value={provider} onChange={handleProviderChange} />
            ) : null}

            {activeStep === 1 ? (
              <BasicInfoStep
                provider={provider}
                displayName={displayName}
                onDisplayNameChange={setDisplayName}
                instanceKey={instanceKey}
                onInstanceKeyChange={setInstanceKey}
                instanceKeyError={instanceKeyError}
                adapterKey={adapterKey}
                onAdapterKeyChange={setAdapterKey}
              />
            ) : null}

            {activeStep === 2 ? renderSetupStep() : null}

            {activeStep === 3 && instance ? (
              <DoneStep
                instance={instance}
                onEnterInstance={key => navigate(consolePaths.instanceDetail(key))}
              />
            ) : null}
          </CardContent>
        </Card>

        <Stack direction="row" spacing={1} justifyContent="space-between">
          <ActionButton tone="ghost" onClick={() => navigate(consolePaths.instanceList())}>
            {t('wizard.exit')}
          </ActionButton>

          <Stack direction="row" spacing={1}>
            {activeStep === 1 ? (
              <ActionButton tone="secondary" onClick={() => setActiveStep(0)}>
                {t('common.back')}
              </ActionButton>
            ) : null}

            {activeStep < 3 ? (
              <ActionButton tone="primary" disabled={nextDisabled} onClick={handleNext}>
                {activeStep === 1 ? t('wizard.createInstance') : t('common.next')}
              </ActionButton>
            ) : (
              <ActionButton
                tone="primary"
                onClick={() =>
                  instance && navigate(consolePaths.instanceDetail(instance.instance_key))
                }
              >
                {t('done.enterInstance')}
              </ActionButton>
            )}
          </Stack>
        </Stack>
      </Stack>
    </Box>
  )
}
