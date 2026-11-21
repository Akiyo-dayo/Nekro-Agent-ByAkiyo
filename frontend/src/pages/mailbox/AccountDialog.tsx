import { useMemo, useState } from 'react'
import {
  Alert,
  Box,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Switch,
  TextField,
  Typography,
} from '@mui/material'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import {
  CUSTOM_PROVIDER_KEY,
  mailboxApi,
  type CustomMailSettings,
  type InstanceMailAccount,
  type InstanceMailAccountPayload,
  type MailProviderPreset,
  type MailboxScope,
} from '../../services/api/mailbox'

export interface ChannelOption {
  value: string
  label: string
}

interface AccountDialogProps {
  scope: MailboxScope
  /** null = 新建 */
  account: InstanceMailAccount | null
  providers: MailProviderPreset[]
  channelOptions: ChannelOption[]
  onClose: () => void
  onSaved: () => void
}

interface HostFields {
  imap_host: string
  imap_port: string
  smtp_host: string
  smtp_port: string
  smtp_ssl_port: string
  smtp_use_ssl: boolean
}

const HOST_KEYS = ['imap_host', 'imap_port', 'smtp_host', 'smtp_port', 'smtp_ssl_port', 'smtp_use_ssl'] as const

function initialHosts(settings: CustomMailSettings | undefined): HostFields {
  return {
    imap_host: String(settings?.imap_host ?? ''),
    imap_port: String(settings?.imap_port ?? 993),
    smtp_host: String(settings?.smtp_host ?? ''),
    smtp_port: String(settings?.smtp_port ?? 587),
    smtp_ssl_port: String(settings?.smtp_ssl_port ?? 465),
    smtp_use_ssl: settings?.smtp_use_ssl === undefined ? true : Boolean(settings.smtp_use_ssl),
  }
}

const normalizedHost = (value: unknown) => String(value ?? '').trim().toLowerCase()

/**
 * 添加 / 修改本实例自有邮箱
 *
 * 与服务端的三条规矩对齐（判据都在服务端，这里只是提前说清楚，免得填完才被拒）：
 * - 地址不能改：要换地址就删掉这个邮箱再添加；
 * - 授权码只写不读：修改时留空表示不改，**但改了服务商或服务器地址就必须重新填**；
 * - 自定义服务器要能从公网访问（内网、回环、透明代理的 Fake-IP 段都不行），端口只能是常用那几个。
 */
export default function AccountDialog({
  scope,
  account,
  providers,
  channelOptions,
  onClose,
  onSaved,
}: AccountDialogProps) {
  const { t } = useTranslation('mailbox')
  const { t: tCommon } = useTranslation('common')
  const isNew = account === null

  const [username, setUsername] = useState(account?.username ?? '')
  const [displayName, setDisplayName] = useState(account?.display_name ?? '')
  const [password, setPassword] = useState('')
  const [provider, setProvider] = useState(account?.provider ?? providers[0]?.key ?? CUSTOM_PROVIDER_KEY)
  const [hosts, setHosts] = useState<HostFields>(() => initialHosts(account?.settings))
  const [enabled, setEnabled] = useState(account?.enabled ?? true)
  const [receiveEnabled, setReceiveEnabled] = useState(account?.receive_enabled ?? true)
  const [sendEnabled, setSendEnabled] = useState(account?.send_enabled ?? true)
  const [isDefaultSender, setIsDefaultSender] = useState(account?.is_default_sender ?? false)
  const [notifyChatKey, setNotifyChatKey] = useState(account?.notify_chat_key ?? '')

  const isCustom = provider === CUSTOM_PROVIDER_KEY
  const preset = providers.find(item => item.key === provider)

  // 与服务端 `_endpoint_changed` 同一口径：服务商变了，或自定义时任一主机变了
  const endpointChanged = useMemo(() => {
    if (!account) return false
    if (provider !== (account.provider || CUSTOM_PROVIDER_KEY)) return true
    if (!isCustom) return false
    return (
      normalizedHost(hosts.imap_host) !== normalizedHost(account.settings.imap_host) ||
      normalizedHost(hosts.smtp_host) !== normalizedHost(account.settings.smtp_host)
    )
  }, [account, provider, isCustom, hosts.imap_host, hosts.smtp_host])

  const passwordRequired = isNew || endpointChanged
  const channelChoices = useMemo(() => {
    if (!notifyChatKey || channelOptions.some(option => option.value === notifyChatKey)) {
      return channelOptions
    }
    return [{ value: notifyChatKey, label: notifyChatKey }, ...channelOptions]
  }, [channelOptions, notifyChatKey])

  const buildPayload = (): InstanceMailAccountPayload => {
    // 设置里还存着上次测试的结果等，修改时原样带回，只换掉服务器那几项（选了预设就不带服务器）
    const preserved: CustomMailSettings = Object.fromEntries(
      Object.entries(account?.settings ?? {}).filter(([key]) => !(HOST_KEYS as readonly string[]).includes(key))
    )
    const settings: CustomMailSettings = isCustom
      ? {
          ...preserved,
          imap_host: hosts.imap_host.trim(),
          imap_port: Number(hosts.imap_port) || 993,
          smtp_host: hosts.smtp_host.trim(),
          smtp_port: Number(hosts.smtp_port) || 587,
          smtp_ssl_port: Number(hosts.smtp_ssl_port) || 465,
          smtp_use_ssl: hosts.smtp_use_ssl,
        }
      : preserved
    return {
      username: username.trim(),
      password,
      display_name: displayName.trim(),
      provider,
      settings,
      enabled,
      receive_enabled: receiveEnabled,
      send_enabled: sendEnabled,
      is_default_sender: isDefaultSender,
      notify_chat_key: notifyChatKey,
    }
  }

  const saveMutation = useMutation({
    mutationFn: () =>
      account === null
        ? mailboxApi.createAccount(scope, buildPayload())
        : mailboxApi.updateAccount(scope, account.id, buildPayload()),
    onSuccess: onSaved,
  })

  const canSave =
    username.trim() !== '' &&
    (!passwordRequired || password !== '') &&
    (!isCustom || (hosts.imap_host.trim() !== '' && hosts.smtp_host.trim() !== ''))

  const hostField = (key: keyof Omit<HostFields, 'smtp_use_ssl'>, label: string, numeric = false) => (
    <TextField
      size="small"
      label={label}
      value={hosts[key]}
      onChange={event => setHosts(prev => ({ ...prev, [key]: event.target.value }))}
      inputProps={numeric ? { inputMode: 'numeric' } : undefined}
      sx={{ flex: numeric ? '0 0 110px' : 1, minWidth: 0 }}
    />
  )

  return (
    <NekroDialog
      open
      onClose={onClose}
      title={isNew ? t('dialog.createTitle') : t('dialog.editTitle')}
      maxWidth="sm"
      fullWidth
      dividers
      actions={
        <>
          <ActionButton tone="ghost" onClick={onClose} disabled={saveMutation.isPending}>
            {tCommon('actions.cancel')}
          </ActionButton>
          <ActionButton tone="primary" onClick={() => saveMutation.mutate()} disabled={!canSave || saveMutation.isPending}>
            {tCommon('actions.save')}
          </ActionButton>
        </>
      }
    >
      <Stack spacing={2}>
        {saveMutation.error && <Alert severity="error">{(saveMutation.error as Error).message}</Alert>}

        <FormControl size="small" fullWidth>
          <InputLabel id="mailbox-provider-label">{t('dialog.provider')}</InputLabel>
          <Select
            labelId="mailbox-provider-label"
            label={t('dialog.provider')}
            value={provider}
            onChange={event => setProvider(event.target.value)}
          >
            {providers.map(item => (
              <MenuItem key={item.key} value={item.key}>
                {item.is_custom ? t('dialog.customProvider') : item.key}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        {preset?.note && (
          <Typography variant="caption" color="text.secondary" sx={{ mt: -1 }}>
            {preset.note}
          </Typography>
        )}

        <TextField
          size="small"
          label={t('dialog.address')}
          value={username}
          onChange={event => setUsername(event.target.value)}
          disabled={!isNew}
          helperText={isNew ? undefined : t('dialog.addressLocked')}
          fullWidth
        />
        <TextField
          size="small"
          label={t('dialog.displayName')}
          value={displayName}
          onChange={event => setDisplayName(event.target.value)}
          helperText={t('dialog.displayNameHint')}
          fullWidth
        />
        <TextField
          size="small"
          type="password"
          autoComplete="new-password"
          label={t('dialog.password')}
          value={password}
          onChange={event => setPassword(event.target.value)}
          placeholder={!isNew && account?.HAS_PASSWORD && !endpointChanged ? t('dialog.passwordKeep') : undefined}
          helperText={endpointChanged ? t('dialog.passwordRequiredAfterChange') : t('dialog.passwordHint')}
          error={endpointChanged && password === ''}
          fullWidth
        />

        {isCustom && (
          <Box>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              {t('dialog.customServers')}
            </Typography>
            <Stack spacing={1.25}>
              <Stack direction="row" spacing={1}>
                {hostField('imap_host', t('dialog.imapHost'))}
                {hostField('imap_port', t('dialog.port'), true)}
              </Stack>
              <Stack direction="row" spacing={1}>
                {hostField('smtp_host', t('dialog.smtpHost'))}
                {hostField('smtp_port', t('dialog.port'), true)}
                {hostField('smtp_ssl_port', t('dialog.sslPort'), true)}
              </Stack>
              <FormControlLabel
                control={
                  <Switch
                    checked={hosts.smtp_use_ssl}
                    onChange={event => setHosts(prev => ({ ...prev, smtp_use_ssl: event.target.checked }))}
                  />
                }
                label={t('dialog.smtpUseSsl')}
              />
              <Typography variant="caption" color="text.secondary">
                {t('dialog.customServersHint')}
              </Typography>
            </Stack>
          </Box>
        )}

        <Box>
          <FormControlLabel control={<Switch checked={enabled} onChange={event => setEnabled(event.target.checked)} />} label={t('dialog.enabled')} />
          <FormControlLabel
            control={<Switch checked={receiveEnabled} onChange={event => setReceiveEnabled(event.target.checked)} />}
            label={t('dialog.receiveEnabled')}
          />
          <FormControlLabel
            control={<Switch checked={sendEnabled} onChange={event => setSendEnabled(event.target.checked)} />}
            label={t('dialog.sendEnabled')}
          />
          <FormControlLabel
            control={<Switch checked={isDefaultSender} onChange={event => setIsDefaultSender(event.target.checked)} />}
            label={t('dialog.defaultSender')}
          />
        </Box>

        <FormControl size="small" fullWidth>
          <InputLabel id="mailbox-notify-label" shrink>
            {t('dialog.notifyChannel')}
          </InputLabel>
          <Select
            labelId="mailbox-notify-label"
            label={t('dialog.notifyChannel')}
            value={notifyChatKey}
            displayEmpty
            notched
            onChange={event => setNotifyChatKey(event.target.value)}
          >
            <MenuItem value="">{t('dialog.notifyNone')}</MenuItem>
            {channelChoices.map(option => (
              <MenuItem key={option.value} value={option.value}>
                {option.label}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        <Typography variant="caption" color="text.secondary" sx={{ mt: -1 }}>
          {t('dialog.notifyHint')}
        </Typography>
      </Stack>
    </NekroDialog>
  )
}
