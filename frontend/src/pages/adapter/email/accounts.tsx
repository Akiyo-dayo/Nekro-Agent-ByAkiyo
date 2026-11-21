import { useEffect, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  FormHelperText,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Typography,
  useTheme,
} from '@mui/material'
import {
  Add as AddIcon,
  CloudDownload as PullIcon,
  Delete as DeleteIcon,
  DriveFileMove as TransferIcon,
  Edit as EditIcon,
  OpenInNew as OpenInNewIcon,
  Refresh as RefreshIcon,
  Science as TestIcon,
} from '@mui/icons-material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { CARD_VARIANTS, CHIP_VARIANTS } from '../../../theme/variants'
import { useNotification } from '../../../hooks/useNotification'
import { ApiError } from '../../../services/api/axios'
import { consoleApi } from '../../../services/api/console'
import {
  EmailAccount,
  EmailProvider,
  EmailPullResult,
  EmailSendShare,
  EmailTransportType,
  emailApi,
} from '../../../services/api/email'
import { mailboxApi } from '../../../services/api/mailbox'
import { tenantApi } from '../../../services/api/tenant'

// 服务端的预设表取不到时的兜底（与改造前的五项相同）
const FALLBACK_PROVIDERS: EmailProvider[] = ['QQ邮箱', '163邮箱', 'Gmail', 'Outlook', '自定义']
const SEND_SHARE_OPTIONS: EmailSendShare[] = ['all_instances', 'listed', 'none']

/** 各提供商支持的收发信方式；未列出的提供商仅支持 IMAP/SMTP */
const providerTransportOptions: Partial<Record<EmailProvider, EmailTransportType[]>> = {
  Gmail: ['imap_smtp', 'gmail_api'],
  Outlook: ['imap_smtp', 'microsoft_graph'],
}

/** 保留仍然合法的连接方式，否则回落到该提供商的首选方式 */
function resolveTransportType(provider: EmailProvider, current: EmailTransportType): EmailTransportType {
  const allowed = providerTransportOptions[provider]
  if (!allowed) return 'imap_smtp'
  return allowed.includes(current) ? current : allowed[0]
}

const providerLinks: Partial<Record<EmailProvider, Array<{ labelKey: string; url: string }>>> = {
  Gmail: [
    { labelKey: 'emailAccounts.providerLinks.googleCloud', url: 'https://console.cloud.google.com/apis/credentials' },
  ],
  Outlook: [
    { labelKey: 'emailAccounts.providerLinks.microsoftAzure', url: 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade' },
  ],
}

function getOAuthRedirectUri() {
  const { protocol, hostname, origin, pathname } = window.location
  const isLocalhost = hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]'
  if (protocol === 'https:' || isLocalhost) {
    return `${origin}${pathname}`
  }
  return 'http://localhost'
}

const defaultAccount: EmailAccount = {
  DISPLAY_NAME: '',
  EMAIL_ACCOUNT: 'QQ邮箱',
  AUTH_TYPE: 'password',
  TRANSPORT_TYPE: 'imap_smtp',
  OAUTH_PROVIDER: '',
  CLIENT_ID: '',
  CLIENT_SECRET: '',
  TENANT_ID: 'common',
  ACCESS_TOKEN: '',
  REFRESH_TOKEN: '',
  TOKEN_EXPIRES_AT: 0,
  CUSTOM_IMAP_HOST: '',
  CUSTOM_IMAP_PORT: 993,
  CUSTOM_SMTP_HOST: '',
  CUSTOM_SMTP_PORT: 587,
  CUSTOM_SMTP_SSL_PORT: 465,
  CUSTOM_SMTP_USE_SSL: true,
  SKIP_TLS_VERIFY: false,
  ENABLED: true,
  RECEIVE_ENABLED: false,
  USE_PROXY: false,
  USERNAME: '',
  PASSWORD: '',
  SEND_ENABLED: false,
  IS_DEFAULT_SENDER: false,
  SEND_SHARE: 'all_instances',
  SEND_SHARE_INSTANCES: [],
}

function buildPayload(form: EmailAccount, editing: boolean): Partial<EmailAccount> {
  const payload: Partial<EmailAccount> = { ...form }
  delete payload.index
  delete payload.DISPLAY_LABEL
  delete payload.HAS_PASSWORD
  if (editing && !payload.PASSWORD?.trim()) {
    delete payload.PASSWORD
  }
  if (editing && !payload.CLIENT_SECRET?.trim()) {
    delete payload.CLIENT_SECRET
  }
  if (editing && !payload.ACCESS_TOKEN?.trim()) {
    delete payload.ACCESS_TOKEN
  }
  if (editing && !payload.REFRESH_TOKEN?.trim()) {
    delete payload.REFRESH_TOKEN
  }
  return payload
}

export default function EmailAccountsPage() {
  const theme = useTheme()
  const { t } = useTranslation('adapter')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [form, setForm] = useState<EmailAccount>(defaultAccount)
  const [deleteIndex, setDeleteIndex] = useState<number | null>(null)
  const [callbackDialogOpen, setCallbackDialogOpen] = useState(false)
  const [callbackText, setCallbackText] = useState('')
  const [pullResult, setPullResult] = useState<EmailPullResult | null>(null)
  const [transferAccount, setTransferAccount] = useState<EmailAccount | null>(null)
  const [transferTarget, setTransferTarget] = useState('')

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['email-accounts'],
    queryFn: emailApi.getAccounts,
  })
  // 服务商下拉以服务端的预设表为准（含 126、腾讯企业邮、阿里云等），页面不再写死
  const { data: providerPresets } = useQuery({
    queryKey: ['mailbox', 'providers'],
    queryFn: mailboxApi.listProviders,
    staleTime: 10 * 60_000,
  })
  // 共享发信的「指定实例」与「转为实例自有」都要实例清单。实例切换器候选接口不带租户，
  // 这里改用总后台的实例列表（带 `tenant_id`），再配租户列表把 id 翻成名字。
  const { data: instanceListData } = useQuery({
    queryKey: ['email-accounts', 'instances'],
    queryFn: consoleApi.listInstances,
    staleTime: 60_000,
  })
  const { data: tenantListData } = useQuery({
    queryKey: ['email-accounts', 'tenants'],
    queryFn: tenantApi.listTenants,
    staleTime: 60_000,
  })
  const providers: EmailProvider[] = providerPresets?.length
    ? providerPresets.map(preset => preset.key)
    : FALLBACK_PROVIDERS
  const activeInstances = (instanceListData?.items ?? []).filter(instance => instance.status !== 'archived')
  const instanceOptions = activeInstances.map(instance => ({
    value: `${instance.adapter_key}:${instance.instance_key}`,
    label: `${instance.display_name || instance.instance_key} (${instance.adapter_key}:${instance.instance_key})`,
    tenantId: instance.tenant_id ?? null,
  }))
  const tenants = tenantListData?.items ?? []
  const selectedShareInstances = form.SEND_SHARE_INSTANCES ?? []
  const selectedShareInstanceSet = new Set(selectedShareInstances)
  const knownTenantIds = new Set(tenants.map(tenant => tenant.id))
  const platformOptions = instanceOptions.filter(option => option.tenantId === null)
  const tenantGroups = tenants
    .map(tenant => ({
      key: `tenant-${tenant.id}`,
      label: tenant.display_name || tenant.name,
      options: instanceOptions.filter(option => option.tenantId === tenant.id),
    }))
    .filter(group => group.options.length > 0)
  const unknownTenantGroups = Array.from(
    new Set(
      instanceOptions
        .map(option => option.tenantId)
        .filter((tenantId): tenantId is number => tenantId !== null && !knownTenantIds.has(tenantId))
    )
  ).map(tenantId => ({
    key: `tenant-${tenantId}`,
    label: t('emailAccounts.share.tenantGroupFallback', { id: tenantId }),
    options: instanceOptions.filter(option => option.tenantId === tenantId),
  }))
  // 已经不在实例清单里、却还留在共享名单上的旧值也要能看到并取消，不能让它们只存在于保存载荷里
  const knownInstanceValues = new Set(instanceOptions.map(option => option.value))
  const unavailableSelected = selectedShareInstances.filter(value => !knownInstanceValues.has(value))
  const unavailableGroups = unavailableSelected.length > 0
    ? [{
        key: 'unavailable',
        label: t('emailAccounts.share.unavailableGroup'),
        options: unavailableSelected.map(value => ({ value, label: value, tenantId: null })),
      }]
    : []
  const instanceGroups = [
    ...(platformOptions.length > 0
      ? [{ key: 'platform', label: t('emailAccounts.share.platformGroup'), options: platformOptions }]
      : []),
    ...tenantGroups,
    ...unknownTenantGroups,
    ...unavailableGroups,
  ]

  const invalidateAccounts = async () => {
    await queryClient.invalidateQueries({ queryKey: ['email-accounts'] })
  }

  const createMutation = useMutation({
    mutationFn: emailApi.createAccount,
    onSuccess: async () => {
      notification.success(t('emailAccounts.saveSuccess'))
      setDialogOpen(false)
      await invalidateAccounts()
    },
    onError: (err: Error) => notification.error(err.message || t('emailAccounts.saveFailed')),
  })

  const updateMutation = useMutation({
    mutationFn: ({ index, account }: { index: number; account: Partial<EmailAccount> }) =>
      emailApi.updateAccount(index, account),
    onSuccess: async () => {
      notification.success(t('emailAccounts.saveSuccess'))
      setDialogOpen(false)
      await invalidateAccounts()
    },
    onError: (err: Error) => notification.error(err.message || t('emailAccounts.saveFailed')),
  })

  const deleteMutation = useMutation({
    mutationFn: emailApi.deleteAccount,
    onSuccess: async () => {
      notification.success(t('emailAccounts.deleteSuccess'))
      setDeleteIndex(null)
      await invalidateAccounts()
    },
    onError: (err: Error) => notification.error(err.message || t('emailAccounts.deleteFailed')),
  })

  const testMutation = useMutation({
    mutationFn: emailApi.testAccount,
    onSuccess: async result => {
      if (result.success) {
        notification.success(t('emailAccounts.testSuccess'))
      } else {
        notification.error(result.message || t('emailAccounts.testFailed'))
      }
      await invalidateAccounts()
    },
    onError: (err: Error) => notification.error(err.message || t('emailAccounts.testFailed')),
  })

  const transferMutation = useMutation({
    mutationFn: ({ index, username, target }: { index: number; username: string; target: string }) => {
      const [adapterKey, ...rest] = target.split(':')
      return emailApi.transferToInstance(index, username, { adapter_key: adapterKey, instance_key: rest.join(':') })
    },
    onSuccess: async () => {
      notification.success(t('emailAccounts.transfer.done'))
      setTransferAccount(null)
      setTransferTarget('')
      await invalidateAccounts()
    },
    onError: async (err: Error) => {
      notification.error(err.message || t('emailAccounts.saveFailed'))
      // 409 = 列表在别处变过：刷新列表，免得再点一次还是对不上
      if (err instanceof ApiError && err.status === 409) {
        setTransferAccount(null)
        await invalidateAccounts()
      }
    },
  })

  const pullMutation = useMutation({
    mutationFn: (index: number) => emailApi.pullAccountInbox(index, { unseen_only: false }),
    onSuccess: async result => {
      setPullResult(result)
      const message = t('emailAccounts.pullSummary', {
        mailbox: result.mailbox || '-',
        found: result.found_count,
        processed: result.processed_count,
        failed: result.failed_count,
        skipped: result.skipped_count,
      })
      if (result.success) {
        notification.success(message)
      } else {
        const firstError = result.errors[0]?.message
        notification.error(firstError ? `${message}: ${firstError}` : message)
      }
      await queryClient.invalidateQueries({ queryKey: ['emails'] })
    },
    onError: (err: Error) => notification.error(err.message || t('emailAccounts.pullFailed')),
  })

  const openCreateDialog = () => {
    setEditingIndex(null)
    setForm(defaultAccount)
    setDialogOpen(true)
  }

  const openEditDialog = (account: EmailAccount) => {
    setEditingIndex(account.index ?? null)
    // 收信开关显示账户实际的值：默认账户里它是关的（新建时默认只发信）
    setForm({ ...defaultAccount, ...account, RECEIVE_ENABLED: account.RECEIVE_ENABLED ?? true, PASSWORD: '' })
    setDialogOpen(true)
  }

  const buildAccountPayload = (editing: boolean) => {
    const payload = buildPayload(form, editing)
    payload.TRANSPORT_TYPE = resolveTransportType(form.EMAIL_ACCOUNT, form.TRANSPORT_TYPE)
    if (form.EMAIL_ACCOUNT === 'Gmail') {
      payload.AUTH_TYPE = 'oauth2'
      payload.OAUTH_PROVIDER = 'google'
      payload.USERNAME = form.USERNAME.trim() || `pending-google-${Date.now()}`
    } else if (form.EMAIL_ACCOUNT === 'Outlook') {
      payload.AUTH_TYPE = 'oauth2'
      payload.OAUTH_PROVIDER = 'microsoft'
      payload.USERNAME = form.USERNAME.trim() || `pending-microsoft-${Date.now()}`
    }
    return payload
  }

  const handleSave = () => {
    const editing = editingIndex !== null
    const payload = buildAccountPayload(editing)
    if (editing) {
      updateMutation.mutate({ index: editingIndex, account: payload })
    } else {
      createMutation.mutate(payload as EmailAccount)
    }
  }

  const updateForm = <K extends keyof EmailAccount>(key: K, value: EmailAccount[K]) => {
    setForm(prev => ({ ...prev, [key]: value }))
  }

  const toggleShareInstance = (value: string, checked: boolean) => {
    updateForm(
      'SEND_SHARE_INSTANCES',
      checked
        ? Array.from(new Set([...selectedShareInstances, value]))
        : selectedShareInstances.filter(item => item !== value)
    )
  }

  const toggleShareTenant = (options: Array<{ value: string }>, checked: boolean) => {
    const values = new Set(options.map(option => option.value))
    updateForm(
      'SEND_SHARE_INSTANCES',
      checked
        ? Array.from(new Set([...selectedShareInstances, ...values]))
        : selectedShareInstances.filter(item => !values.has(item))
    )
  }

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const code = params.get('code')
    const state = params.get('state')
    if (!code || !state?.startsWith('email:')) return

    const index = Number(state.replace('email:', ''))
    if (!Number.isInteger(index)) return

    const redirectUri = getOAuthRedirectUri()
    emailApi.handleOAuthCallback(index, code, redirectUri)
      .then(async () => {
        notification.success(t('emailAccounts.authorizeSuccess'))
        window.history.replaceState({}, document.title, `${window.location.pathname}#/adapters/email/accounts`)
        await queryClient.invalidateQueries({ queryKey: ['email-accounts'] })
      })
      .catch((err: Error) => notification.error(err.message || t('emailAccounts.authorizeFailed')))
  }, [notification, queryClient, t])

  const accounts = data?.accounts ?? []
  const isSaving = createMutation.isPending || updateMutation.isPending
  const isGmail = form.EMAIL_ACCOUNT === 'Gmail'
  const isOutlook = form.EMAIL_ACCOUNT === 'Outlook'
  const isOauthProvider = isGmail || isOutlook
  const currentProviderLinks = providerLinks[form.EMAIL_ACCOUNT] ?? []
  const currentTransportOptions = providerTransportOptions[form.EMAIL_ACCOUNT] ?? []

  const handleOpenAuthorizeUrl = async () => {
    try {
      const editing = editingIndex !== null
      const payload = buildAccountPayload(editing)
      const savedIndex = editing
        ? editingIndex
        : (await emailApi.createAccount(payload as EmailAccount)).index
      if (editing) {
        await emailApi.updateAccount(editingIndex, payload)
      }
      setEditingIndex(savedIndex)
      const redirectUri = getOAuthRedirectUri()
      const response = await emailApi.createAuthorizeUrl(savedIndex, redirectUri, `email:${savedIndex}`)
      if (redirectUri === 'http://localhost') {
        setCallbackDialogOpen(true)
        setCallbackText('')
        window.open(response.authorize_url, '_blank', 'noopener,noreferrer')
        return
      }
      window.location.href = response.authorize_url
    } catch (err) {
      notification.error(err instanceof Error ? err.message : t('emailAccounts.authorizeUrlFailed'))
    }
  }

  const getConnectionStatus = (account: EmailAccount) => {
    if (account.LAST_TEST_SUCCESS === true) {
      return { color: 'success' as const, label: t('emailAccounts.testConnected') }
    }
    if (account.LAST_TEST_SUCCESS === false) {
      return { color: 'error' as const, label: t('emailAccounts.testFailedStatus') }
    }
    return { color: 'default' as const, label: t('emailAccounts.testNotRun') }
  }

  const handleManualOAuthCallback = async () => {
    let callbackUrl: URL
    try {
      callbackUrl = new URL(callbackText.trim())
    } catch {
      notification.error(t('emailAccounts.invalidCallbackUrl'))
      return
    }

    const code = callbackUrl.searchParams.get('code')
    const state = callbackUrl.searchParams.get('state')
    if (!code || !state?.startsWith('email:')) {
      notification.error(t('emailAccounts.invalidCallbackUrl'))
      return
    }
    const index = Number(state.replace('email:', ''))
    if (!Number.isInteger(index)) {
      notification.error(t('emailAccounts.invalidCallbackUrl'))
      return
    }

    try {
      await emailApi.handleOAuthCallback(index, code, 'http://localhost')
      notification.success(t('emailAccounts.authorizeSuccess'))
      setCallbackDialogOpen(false)
      setCallbackText('')
      setDialogOpen(false)
      await queryClient.invalidateQueries({ queryKey: ['email-accounts'] })
    } catch (err) {
      notification.error(err instanceof Error ? err.message : t('emailAccounts.authorizeFailed'))
    }
  }

  return (
    <Box sx={{ p: 2 }}>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start" sx={{ mb: 2 }} spacing={2}>
        <Box>
          <Typography variant="h6">{t('emailAccounts.title')}</Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          <Button startIcon={<RefreshIcon />} onClick={() => refetch()} disabled={isLoading}>
            {t('emailAccounts.refresh')}
          </Button>
          <Button variant="contained" startIcon={<AddIcon />} onClick={openCreateDialog}>
            {t('emailAccounts.add')}
          </Button>
        </Stack>
      </Stack>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{t('emailAccounts.loadError')}</Alert>}

      <TableContainer component={Paper} sx={{ ...CARD_VARIANTS.default.styles }}>
        <Table>
          <TableHead>
            <TableRow>
              <TableCell>{t('emailAccounts.enabled')}</TableCell>
              <TableCell>{t('emailAccounts.provider')}</TableCell>
              <TableCell>{t('emailAccounts.displayName')}</TableCell>
              <TableCell>{t('emailAccounts.username')}</TableCell>
              <TableCell>{t('emailAccounts.authType')}</TableCell>
              <TableCell>{t('emailAccounts.transportType')}</TableCell>
              <TableCell>{t('emailAccounts.useProxy')}</TableCell>
              <TableCell>{t('emailAccounts.connectionStatus')}</TableCell>
              <TableCell>{t('emailAccounts.sendEnabled')}</TableCell>
              <TableCell>{t('emailAccounts.defaultSender')}</TableCell>
              <TableCell>{t('emailAccounts.share.column')}</TableCell>
              <TableCell align="right">{t('emailAccounts.actions')}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {accounts.length > 0 ? accounts.map(account => {
              const connectionStatus = getConnectionStatus(account)
              return (
              <TableRow key={account.index} hover>
                <TableCell>
                  <Chip
                    size="small"
                    color={account.ENABLED ? 'success' : 'default'}
                    label={account.ENABLED ? t('emailAccounts.enabledYes') : t('emailAccounts.enabledNo')}
                  />
                </TableCell>
                <TableCell>{account.EMAIL_ACCOUNT}</TableCell>
                <TableCell>{account.DISPLAY_NAME || '-'}</TableCell>
                <TableCell>{account.USERNAME || '-'}</TableCell>
                <TableCell>{t(`emailAccounts.authTypes.${account.AUTH_TYPE || 'password'}`)}</TableCell>
                <TableCell>{t(`emailAccounts.transportTypes.${account.TRANSPORT_TYPE || 'imap_smtp'}`)}</TableCell>
                <TableCell>{account.USE_PROXY ? t('emailAccounts.yes') : t('emailAccounts.no')}</TableCell>
                <TableCell>
                  <Chip
                    size="small"
                    color={connectionStatus.color}
                    label={connectionStatus.label}
                    title={account.LAST_TEST_MESSAGE || undefined}
                  />
                </TableCell>
                <TableCell>
                  <Stack direction="row" spacing={0.5} alignItems="center" flexWrap="wrap" useFlexGap>
                    <span>{account.SEND_ENABLED ? t('emailAccounts.yes') : t('emailAccounts.no')}</span>
                    {account.RECEIVE_ENABLED === false && (
                      <Chip
                        size="small"
                        label={t('emailAccounts.sendOnly')}
                        sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.text.secondary, true)}
                      />
                    )}
                  </Stack>
                </TableCell>
                <TableCell>{account.IS_DEFAULT_SENDER ? t('emailAccounts.yes') : t('emailAccounts.no')}</TableCell>
                <TableCell>
                  {account.SEND_SHARE === 'listed'
                    ? t('emailAccounts.share.listedCount', { count: account.SEND_SHARE_INSTANCES?.length ?? 0 })
                    : t(`emailAccounts.share.options.${account.SEND_SHARE ?? 'all_instances'}`)}
                </TableCell>
                <TableCell align="right">
                  <IconButton
                    size="small"
                    onClick={() => account.index !== undefined && testMutation.mutate(account.index)}
                    disabled={account.index === undefined || testMutation.isPending}
                    title={t('emailAccounts.testConnection')}
                  >
                    <TestIcon fontSize="small" />
                  </IconButton>
                  <IconButton
                    size="small"
                    onClick={() => account.index !== undefined && pullMutation.mutate(account.index)}
                    disabled={
                      account.index === undefined
                      || pullMutation.isPending
                      || !account.ENABLED
                      || account.RECEIVE_ENABLED === false
                    }
                    title={t('emailAccounts.pullInbox')}
                  >
                    <PullIcon fontSize="small" />
                  </IconButton>
                  <IconButton size="small" onClick={() => openEditDialog(account)}>
                    <EditIcon fontSize="small" />
                  </IconButton>
                  <IconButton
                    size="small"
                    onClick={() => {
                      setTransferAccount(account)
                      setTransferTarget('')
                    }}
                    disabled={account.index === undefined}
                    title={t('emailAccounts.transfer.action')}
                  >
                    <TransferIcon fontSize="small" />
                  </IconButton>
                  <IconButton size="small" color="error" onClick={() => setDeleteIndex(account.index ?? null)}>
                    <DeleteIcon fontSize="small" />
                  </IconButton>
                </TableCell>
              </TableRow>
              )
            }) : (
              <TableRow>
                <TableCell colSpan={12} align="center">
                  <Typography color="text.secondary">{t('emailAccounts.empty')}</Typography>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </TableContainer>

      {pullResult && (
        <Alert
          severity={pullResult.success ? 'success' : 'warning'}
          sx={{ mt: 2 }}
          onClose={() => setPullResult(null)}
        >
          <Stack spacing={0.5}>
            <Typography variant="body2">
              {t('emailAccounts.pullResultSummary', {
                account: pullResult.account_username,
                mailbox: pullResult.mailbox || '-',
                found: pullResult.found_count,
                processed: pullResult.processed_count,
                failed: pullResult.failed_count,
                marked: pullResult.marked_seen_count,
                skipped: pullResult.skipped_count,
                duration: pullResult.duration_ms,
              })}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {`unseen_only=${pullResult.search_unseen_only}, limit=${pullResult.effective_limit}, mark_seen=${pullResult.mark_as_seen_after_fetch}`}
            </Typography>
            {pullResult.reconnect_attempted && (
              <Typography variant="body2">
                {t('emailAccounts.pullReconnect', {
                  status: pullResult.reconnect_success ? t('emailAccounts.yes') : t('emailAccounts.no'),
                })}
              </Typography>
            )}
            {pullResult.debug_steps.length > 0 && (
              <Box component="ul" sx={{ m: 0, pl: 2 }}>
                {pullResult.debug_steps.map((step, idx) => (
                  <li key={`debug-${step.stage}-${idx}`}>
                    <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
                      [debug:{step.stage}] {JSON.stringify(step)}
                    </Typography>
                  </li>
                ))}
              </Box>
            )}
            {pullResult.errors.length > 0 && (
              <Box component="ul" sx={{ m: 0, pl: 2 }}>
                {pullResult.errors.slice(0, 5).map((pullError, idx) => (
                  <li key={`${pullError.stage}-${idx}`}>
                    <Typography variant="body2">
                      [{pullError.stage}] {pullError.email_id ? `${pullError.email_id}: ` : ''}{pullError.message}
                    </Typography>
                  </li>
                ))}
              </Box>
            )}
          </Stack>
        </Alert>
      )}

      <Dialog open={dialogOpen} onClose={() => setDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>
          {editingIndex === null ? t('emailAccounts.add') : t('emailAccounts.edit')}
        </DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <FormControl fullWidth>
              <InputLabel>{t('emailAccounts.provider')}</InputLabel>
              <Select
                label={t('emailAccounts.provider')}
                value={form.EMAIL_ACCOUNT}
                onChange={event => {
                  const provider = event.target.value as EmailProvider
                  setForm(prev => ({
                    ...prev,
                    EMAIL_ACCOUNT: provider,
                    AUTH_TYPE: provider === 'Gmail' || provider === 'Outlook' ? 'oauth2' : 'password',
                    TRANSPORT_TYPE: resolveTransportType(provider, prev.TRANSPORT_TYPE),
                    OAUTH_PROVIDER: provider === 'Gmail' ? 'google' : provider === 'Outlook' ? 'microsoft' : '',
                  }))
                }}
              >
                {providers.map(provider => (
                  <MenuItem key={provider} value={provider}>{provider}</MenuItem>
                ))}
              </Select>
              {providerPresets?.find(preset => preset.key === form.EMAIL_ACCOUNT)?.note && (
                <FormHelperText>
                  {providerPresets.find(preset => preset.key === form.EMAIL_ACCOUNT)?.note}
                </FormHelperText>
              )}
            </FormControl>
            <TextField
              label={t('emailAccounts.displayName')}
              value={form.DISPLAY_NAME}
              onChange={event => updateForm('DISPLAY_NAME', event.target.value)}
              fullWidth
              helperText={t('emailAccounts.displayNameHelp')}
            />
            {currentProviderLinks.length > 0 && (
              <Alert severity="info">
                <Stack direction="row" spacing={1} flexWrap="wrap" alignItems="center">
                  <Typography variant="body2">{t('emailAccounts.providerLinks.description')}</Typography>
                  {currentProviderLinks.map(link => (
                    <Button
                      key={link.url}
                      component="a"
                      href={link.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      size="small"
                      endIcon={<OpenInNewIcon fontSize="small" />}
                    >
                      {t(link.labelKey)}
                    </Button>
                  ))}
                </Stack>
              </Alert>
            )}
            {!isOauthProvider && (
              <TextField
                label={t('emailAccounts.username')}
                value={form.USERNAME}
                onChange={event => updateForm('USERNAME', event.target.value)}
                fullWidth
                required
              />
            )}
            {isOauthProvider && (
              <Alert severity="info">{t(`emailAccounts.oauthHints.${form.EMAIL_ACCOUNT}`)}</Alert>
            )}
            {isOauthProvider ? (
              <Stack spacing={2}>
                {currentTransportOptions.length > 1 && (
                  <FormControl fullWidth>
                    <InputLabel>{t('emailAccounts.transportType')}</InputLabel>
                    <Select
                      label={t('emailAccounts.transportType')}
                      value={resolveTransportType(form.EMAIL_ACCOUNT, form.TRANSPORT_TYPE)}
                      onChange={event => updateForm('TRANSPORT_TYPE', event.target.value as EmailTransportType)}
                    >
                      {currentTransportOptions.map(option => (
                        <MenuItem key={option} value={option}>
                          {t(`emailAccounts.transportTypes.${option}`)}
                        </MenuItem>
                      ))}
                    </Select>
                    <FormHelperText>{t('emailAccounts.transportTypeHelp')}</FormHelperText>
                  </FormControl>
                )}
                <TextField
                  label={t('emailAccounts.clientId')}
                  value={form.CLIENT_ID}
                  onChange={event => updateForm('CLIENT_ID', event.target.value)}
                  fullWidth
                />
                <TextField
                  label={t('emailAccounts.clientSecret')}
                  value={form.CLIENT_SECRET ?? ''}
                  onChange={event => updateForm('CLIENT_SECRET', event.target.value)}
                  fullWidth
                  type="password"
                  helperText={editingIndex !== null ? t('emailAccounts.clientSecretHelp') : undefined}
                />
                {isOutlook && (
                  <TextField
                    label={t('emailAccounts.tenantId')}
                    value={form.TENANT_ID}
                    onChange={event => updateForm('TENANT_ID', event.target.value)}
                    fullWidth
                    helperText={t('emailAccounts.tenantIdHelp')}
                  />
                )}
                <Stack direction="row" spacing={1}>
                  <Button variant="outlined" onClick={handleOpenAuthorizeUrl} disabled={!form.CLIENT_ID.trim()}>
                    {isGmail ? t('emailAccounts.connectGoogle') : t('emailAccounts.connectMicrosoft')}
                  </Button>
                </Stack>
                <Alert severity="warning">{t('emailAccounts.oauthPending')}</Alert>
              </Stack>
            ) : (
              <TextField
                label={t('emailAccounts.password')}
                value={form.PASSWORD ?? ''}
                onChange={event => updateForm('PASSWORD', event.target.value)}
                fullWidth
                type="password"
                placeholder={editingIndex !== null && form.HAS_PASSWORD ? t('emailAccounts.passwordPlaceholder') : undefined}
                helperText={editingIndex !== null && form.HAS_PASSWORD ? t('emailAccounts.passwordHelp') : undefined}
              />
            )}
            <Box
              sx={{
                display: 'grid',
                gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))' },
                columnGap: 2,
                rowGap: 1,
              }}
            >
              <FormControlLabel
                control={<Switch checked={form.ENABLED} onChange={event => updateForm('ENABLED', event.target.checked)} />}
                label={t('emailAccounts.enabled')}
                sx={{ m: 0 }}
              />
              <FormControlLabel
                control={<Switch checked={form.SEND_ENABLED} onChange={event => updateForm('SEND_ENABLED', event.target.checked)} />}
                label={t('emailAccounts.sendEnabled')}
                sx={{ m: 0 }}
              />
              <Box>
                <FormControlLabel
                  control={
                    <Switch
                      checked={form.RECEIVE_ENABLED ?? true}
                      onChange={event => updateForm('RECEIVE_ENABLED', event.target.checked)}
                    />
                  }
                  label={t('emailAccounts.receiveEnabled')}
                  sx={{ m: 0 }}
                />
                <FormHelperText sx={{ mt: 0, mx: 0 }}>{t('emailAccounts.receiveEnabledHelp')}</FormHelperText>
              </Box>
              <FormControlLabel
                control={<Switch checked={form.IS_DEFAULT_SENDER} onChange={event => updateForm('IS_DEFAULT_SENDER', event.target.checked)} />}
                label={t('emailAccounts.defaultSender')}
                sx={{ m: 0 }}
              />
              <FormControlLabel
                control={<Switch checked={form.USE_PROXY} onChange={event => updateForm('USE_PROXY', event.target.checked)} />}
                label={t('emailAccounts.useProxy')}
                sx={{ m: 0 }}
              />
            </Box>

            {form.EMAIL_ACCOUNT === '自定义' && (
              <Stack spacing={2}>
                <Typography variant="subtitle2">{t('emailAccounts.customServer')}</Typography>
                <TextField
                  label={t('emailAccounts.customImapHost')}
                  value={form.CUSTOM_IMAP_HOST}
                  onChange={event => updateForm('CUSTOM_IMAP_HOST', event.target.value)}
                  fullWidth
                />
                <TextField
                  label={t('emailAccounts.customImapPort')}
                  value={form.CUSTOM_IMAP_PORT}
                  onChange={event => updateForm('CUSTOM_IMAP_PORT', Number(event.target.value))}
                  type="number"
                  fullWidth
                />
                <TextField
                  label={t('emailAccounts.customSmtpHost')}
                  value={form.CUSTOM_SMTP_HOST}
                  onChange={event => updateForm('CUSTOM_SMTP_HOST', event.target.value)}
                  fullWidth
                />
                <Stack direction="row" spacing={2}>
                  <TextField
                    label={t('emailAccounts.customSmtpPort')}
                    value={form.CUSTOM_SMTP_PORT}
                    onChange={event => updateForm('CUSTOM_SMTP_PORT', Number(event.target.value))}
                    type="number"
                    fullWidth
                  />
                  <TextField
                    label={t('emailAccounts.customSmtpSslPort')}
                    value={form.CUSTOM_SMTP_SSL_PORT}
                    onChange={event => updateForm('CUSTOM_SMTP_SSL_PORT', Number(event.target.value))}
                    type="number"
                    fullWidth
                  />
                </Stack>
                <FormControlLabel
                  control={
                    <Switch
                      checked={form.CUSTOM_SMTP_USE_SSL}
                      onChange={event => updateForm('CUSTOM_SMTP_USE_SSL', event.target.checked)}
                    />
                  }
                  label={t('emailAccounts.customSmtpUseSsl')}
                />
                <Box>
                  <FormControlLabel
                    control={
                      <Switch
                        checked={form.SKIP_TLS_VERIFY ?? false}
                        onChange={event => updateForm('SKIP_TLS_VERIFY', event.target.checked)}
                      />
                    }
                    label={t('emailAccounts.skipTlsVerify')}
                  />
                  {form.SKIP_TLS_VERIFY && (
                    <Typography variant="caption" color="warning.main" display="block">
                      {t('emailAccounts.skipTlsVerifyHint')}
                    </Typography>
                  )}
                </Box>
              </Stack>
            )}

            <Stack spacing={1.5}>
              <Typography variant="subtitle2">{t('emailAccounts.share.title')}</Typography>
              <FormControl fullWidth size="small">
                <InputLabel>{t('emailAccounts.share.scope')}</InputLabel>
                <Select
                  label={t('emailAccounts.share.scope')}
                  value={form.SEND_SHARE ?? 'all_instances'}
                  onChange={event => updateForm('SEND_SHARE', event.target.value as EmailSendShare)}
                >
                  {SEND_SHARE_OPTIONS.map(option => (
                    <MenuItem key={option} value={option}>
                      {t(`emailAccounts.share.options.${option}`)}
                    </MenuItem>
                  ))}
                </Select>
                <FormHelperText>{t('emailAccounts.share.hint')}</FormHelperText>
              </FormControl>
              {form.SEND_SHARE === 'listed' && (
                <Box>
                  <Typography variant="subtitle2" sx={{ mb: 1 }}>
                    {t('emailAccounts.share.instances')}
                  </Typography>
                  <Box
                    sx={{
                      border: '1px solid',
                      borderColor: 'divider',
                      borderRadius: 2,
                      maxHeight: 320,
                      overflowY: 'auto',
                    }}
                  >
                    {instanceGroups.length === 0 ? (
                      <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
                        {t('emailAccounts.share.noInstances')}
                      </Typography>
                    ) : instanceGroups.map(group => {
                      const selectedCount = group.options.filter(option => selectedShareInstanceSet.has(option.value)).length
                      const allSelected = selectedCount === group.options.length
                      const someSelected = selectedCount > 0 && !allSelected
                      return (
                        <Box key={group.key}>
                          <Box
                            sx={{
                              position: 'sticky',
                              top: 0,
                              zIndex: 1,
                              display: 'flex',
                              alignItems: 'center',
                              gap: 0.5,
                              px: 1,
                              py: 0.25,
                              bgcolor: 'background.paper',
                              borderBottom: '1px solid',
                              borderColor: 'divider',
                            }}
                          >
                            <Checkbox
                              size="small"
                              checked={allSelected}
                              indeterminate={someSelected}
                              onChange={event => toggleShareTenant(group.options, event.target.checked)}
                              inputProps={{
                                'aria-label': t('emailAccounts.share.selectTenantAria', { tenant: group.label }),
                              }}
                            />
                            <Typography variant="body2" fontWeight={600} noWrap sx={{ minWidth: 0, flex: 1 }}>
                              {group.label}
                            </Typography>
                            <Typography variant="caption" color="text.secondary" sx={{ pr: 1 }}>
                              {`${selectedCount}/${group.options.length}`}
                            </Typography>
                          </Box>
                          {group.options.map(option => (
                            <FormControlLabel
                              key={option.value}
                              control={
                                <Checkbox
                                  size="small"
                                  checked={selectedShareInstanceSet.has(option.value)}
                                  onChange={event => toggleShareInstance(option.value, event.target.checked)}
                                />
                              }
                              label={option.label}
                              sx={{
                                display: 'flex',
                                mx: 0,
                                pl: 2,
                                pr: 1,
                                py: 0.25,
                                '& .MuiFormControlLabel-label': { minWidth: 0, wordBreak: 'break-all' },
                              }}
                            />
                          ))}
                        </Box>
                      )
                    })}
                  </Box>
                  <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                    {t('emailAccounts.share.instancesHint')}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
                    {t('emailAccounts.share.tenantGroupHint')}
                  </Typography>
                </Box>
              )}
            </Stack>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t('emailAccounts.cancel')}</Button>
          <Button onClick={handleSave} variant="contained" disabled={isSaving || (!isOauthProvider && !form.USERNAME.trim())}>
            {t('emailAccounts.save')}
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={callbackDialogOpen} onClose={() => setCallbackDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t('emailAccounts.manualCallbackTitle')}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <Alert severity="info">{t('emailAccounts.manualCallbackDescription')}</Alert>
            <TextField
              label={t('emailAccounts.manualCallbackUrl')}
              value={callbackText}
              onChange={event => setCallbackText(event.target.value)}
              fullWidth
              multiline
              minRows={3}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCallbackDialogOpen(false)}>{t('emailAccounts.cancel')}</Button>
          <Button onClick={handleManualOAuthCallback} variant="contained" disabled={!callbackText.trim()}>
            {t('emailAccounts.completeAuthorize')}
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={transferAccount !== null} onClose={() => setTransferAccount(null)} maxWidth="sm" fullWidth>
        <DialogTitle>{t('emailAccounts.transfer.title')}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ pt: 1 }}>
            {transferAccount && (transferAccount.AUTH_TYPE !== 'password' || transferAccount.TRANSPORT_TYPE !== 'imap_smtp') ? (
              <Alert severity="warning">{t('emailAccounts.transfer.oauthOnly')}</Alert>
            ) : (
              <>
                <Typography variant="body2">
                  {t('emailAccounts.transfer.description', { address: transferAccount?.USERNAME ?? '' })}
                </Typography>
                <FormControl fullWidth size="small">
                  <InputLabel>{t('emailAccounts.transfer.instance')}</InputLabel>
                  <Select
                    label={t('emailAccounts.transfer.instance')}
                    value={transferTarget}
                    onChange={event => setTransferTarget(event.target.value)}
                  >
                    {instanceOptions.map(option => (
                      <MenuItem key={option.value} value={option.value}>
                        {option.label}
                      </MenuItem>
                    ))}
                  </Select>
                </FormControl>
              </>
            )}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setTransferAccount(null)}>{t('emailAccounts.cancel')}</Button>
          <Button
            variant="contained"
            disabled={
              !transferTarget
              || transferMutation.isPending
              || transferAccount?.index === undefined
              || transferAccount.AUTH_TYPE !== 'password'
              || transferAccount.TRANSPORT_TYPE !== 'imap_smtp'
            }
            onClick={() =>
              transferAccount?.index !== undefined
              && transferMutation.mutate({
                index: transferAccount.index,
                username: transferAccount.USERNAME,
                target: transferTarget,
              })
            }
          >
            {t('emailAccounts.transfer.confirm')}
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={deleteIndex !== null} onClose={() => setDeleteIndex(null)}>
        <DialogTitle>{t('emailAccounts.delete')}</DialogTitle>
        <DialogContent>
          <Typography>{t('emailAccounts.deleteConfirm')}</Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleteIndex(null)}>{t('emailAccounts.cancel')}</Button>
          <Button
            color="error"
            variant="contained"
            disabled={deleteMutation.isPending}
            onClick={() => deleteIndex !== null && deleteMutation.mutate(deleteIndex)}
          >
            {t('emailAccounts.delete')}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
