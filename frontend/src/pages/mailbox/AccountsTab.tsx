import { useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Chip,
  Paper,
  Stack,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import DownloadRoundedIcon from '@mui/icons-material/DownloadRounded'
import EditNoteRoundedIcon from '@mui/icons-material/EditNoteRounded'
import NetworkCheckRoundedIcon from '@mui/icons-material/NetworkCheckRounded'
import UndoRoundedIcon from '@mui/icons-material/UndoRounded'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import ServerDataTable, { type ServerDataTableColumn } from '../../components/common/ServerDataTable'
import { ThemedTooltip } from '../../components/common/ThemedTooltip'
import { useIdentity } from '../../hooks/useIdentity'
import { useNotification } from '../../hooks/useNotification'
import { chatChannelApi } from '../../services/api/chat-channel'
import { parseInstanceKeyFromChatKey } from '../../services/api/instance-scope'
import {
  mailboxApi,
  type ConnectionTestResult,
  type InstanceMailAccount,
  type MailboxScope,
} from '../../services/api/mailbox'
import { CARD_VARIANTS, CHIP_VARIANTS } from '../../theme/variants'
import AccountDialog, { type ChannelOption } from './AccountDialog'

/**
 * 「发信邮箱」标签
 *
 * 上面一块是平台方共享给本实例的发信邮箱：只有显示名（租户没有它们的地址与服务器），
 * 标出谁是本实例的默认发件人、或者已经被本实例自己的默认邮箱盖过。
 * 下面是本实例自有的邮箱：添加、修改、测试连接、手动收一次、删除；平台方还能把它转回平台方。
 */

interface AccountsTabProps {
  scope: MailboxScope
}

const scopeKeyOf = (scope: MailboxScope) => `${scope.adapter_key}|${scope.instance_key}`

export default function AccountsTab({ scope }: AccountsTabProps) {
  const theme = useTheme()
  const { t } = useTranslation('mailbox')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { isPlatformOwner } = useIdentity()
  const navigate = useNavigate()
  const scopeKey = scopeKeyOf(scope)

  const [dialogAccount, setDialogAccount] = useState<InstanceMailAccount | 'new' | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<InstanceMailAccount | null>(null)
  const [transferTarget, setTransferTarget] = useState<InstanceMailAccount | null>(null)
  const [testResult, setTestResult] = useState<{ account: InstanceMailAccount; result: ConnectionTestResult } | null>(null)

  const accountsQuery = useQuery({
    queryKey: ['mailbox', 'accounts', scopeKey],
    queryFn: () => mailboxApi.listAccounts(scope),
    retry: false,
  })
  const platformQuery = useQuery({
    queryKey: ['mailbox', 'platform-accounts', scopeKey],
    queryFn: () => mailboxApi.listPlatformAccounts(scope),
    retry: false,
  })
  const providersQuery = useQuery({
    queryKey: ['mailbox', 'providers'],
    queryFn: mailboxApi.listProviders,
    staleTime: 10 * 60_000,
  })
  // 通知频道只列本实例的：频道列表接口按身份收敛、不按实例过滤，这里再按会话标识里的实例段筛一次
  const channelsQuery = useQuery({
    queryKey: ['mailbox', 'channels', scopeKey],
    queryFn: () => chatChannelApi.getList({ page: 1, page_size: 200 }),
    staleTime: 60_000,
  })

  const channelOptions = useMemo<ChannelOption[]>(
    () =>
      (channelsQuery.data?.items ?? [])
        .filter(channel => parseInstanceKeyFromChatKey(channel.chat_key) === scope.instance_key)
        .map(channel => ({
          value: channel.chat_key,
          label: channel.custom_channel_name || channel.channel_name || channel.chat_key,
        })),
    [channelsQuery.data, scope.instance_key]
  )

  const accounts = accountsQuery.data ?? []
  const ownDefault = accounts.some(account => account.is_default_sender && account.send_enabled && account.enabled)

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['mailbox'] })
  }

  const testMutation = useMutation({
    mutationFn: (account: InstanceMailAccount) => mailboxApi.testAccount(scope, account.id),
    onSuccess: (result, account) => {
      setTestResult({ account, result })
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const fetchMutation = useMutation({
    mutationFn: (account: InstanceMailAccount) => mailboxApi.fetchAccount(scope, account.id),
    onSuccess: result => {
      notification.success(t('accounts.fetchDone', { count: result.fetched }))
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (account: InstanceMailAccount) => mailboxApi.deleteAccount(scope, account.id),
    onSuccess: () => {
      notification.success(tCommon('messages.deleteSuccess'))
      setDeleteTarget(null)
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const transferMutation = useMutation({
    mutationFn: (account: InstanceMailAccount) => mailboxApi.transferToPlatform(scope, account.id),
    onSuccess: () => {
      notification.success(t('accounts.transferDone'))
      setTransferTarget(null)
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const providerLabel = (key: string) => (key === '自定义' ? t('dialog.customProvider') : key)

  const columns: ServerDataTableColumn<InstanceMailAccount>[] = [
    {
      key: 'address',
      header: t('accounts.columns.address'),
      width: 260,
      render: row => (
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="body2" fontWeight={600} sx={{ wordBreak: 'break-all' }}>
            {row.username}
          </Typography>
          {row.display_name && (
            <Typography variant="caption" color="text.secondary">
              {row.display_name}
            </Typography>
          )}
        </Box>
      ),
    },
    {
      key: 'provider',
      header: t('accounts.columns.provider'),
      width: 120,
      hideOnMobile: true,
      render: row => <Typography variant="body2">{providerLabel(row.provider)}</Typography>,
    },
    {
      key: 'status',
      header: t('accounts.columns.status'),
      width: 260,
      render: row => (
        <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
          {!row.enabled && (
            <Chip size="small" label={t('accounts.disabled')} sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.text.disabled, true)} />
          )}
          {row.receive_enabled && (
            <Chip size="small" label={t('accounts.receiving')} sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true)} />
          )}
          {row.send_enabled && (
            <Chip size="small" label={t('accounts.sending')} sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.success.main, true)} />
          )}
          {row.is_default_sender && (
            <Chip size="small" label={t('accounts.defaultSender')} sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.primary.main, true)} />
          )}
          {row.settings.last_receive_error && (
            <ThemedTooltip title={String(row.settings.last_receive_error)}>
              <Chip size="small" label={t('accounts.receiveUnavailable')} sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.warning.main, true)} />
            </ThemedTooltip>
          )}
        </Stack>
      ),
    },
    {
      key: 'lastTest',
      header: t('accounts.columns.lastTest'),
      width: 200,
      hideOnMobile: true,
      render: row => {
        const tested = Number(row.settings.last_test_time || 0)
        if (!tested) {
          return (
            <Typography variant="caption" color="text.secondary">
              {t('accounts.neverTested')}
            </Typography>
          )
        }
        const ok = row.settings.last_test_success === true
        return (
          <ThemedTooltip title={String(row.settings.last_test_message || '')}>
            <Typography variant="caption" sx={{ color: ok ? 'success.main' : 'error.main' }}>
              {ok ? t('accounts.testOk') : t('accounts.testFailed')} · {new Date(tested * 1000).toLocaleString()}
            </Typography>
          </ThemedTooltip>
        )
      },
    },
    {
      key: 'actions',
      header: t('accounts.columns.actions'),
      width: 210,
      render: row => (
        <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
          <ThemedTooltip title={t('accounts.test')}>
            <span>
              <IconActionButton
                tone="primary"
                size="small"
                aria-label={t('accounts.test')}
                disabled={testMutation.isPending}
                onClick={() => testMutation.mutate(row)}
              >
                <NetworkCheckRoundedIcon fontSize="small" />
              </IconActionButton>
            </span>
          </ThemedTooltip>
          <ThemedTooltip title={t('accounts.fetch')}>
            <span>
              <IconActionButton
                tone="primary"
                size="small"
                aria-label={t('accounts.fetch')}
                disabled={fetchMutation.isPending || !row.enabled || !row.receive_enabled}
                onClick={() => fetchMutation.mutate(row)}
              >
                <DownloadRoundedIcon fontSize="small" />
              </IconActionButton>
            </span>
          </ThemedTooltip>
          <ThemedTooltip title={tCommon('actions.edit')}>
            <IconActionButton tone="primary" size="small" aria-label={tCommon('actions.edit')} onClick={() => setDialogAccount(row)}>
              <EditNoteRoundedIcon fontSize="small" />
            </IconActionButton>
          </ThemedTooltip>
          {isPlatformOwner && (
            <ThemedTooltip title={t('accounts.transfer')}>
              <IconActionButton tone="primary" size="small" aria-label={t('accounts.transfer')} onClick={() => setTransferTarget(row)}>
                <UndoRoundedIcon fontSize="small" />
              </IconActionButton>
            </ThemedTooltip>
          )}
          <ThemedTooltip title={tCommon('actions.delete')}>
            <IconActionButton tone="danger" size="small" aria-label={tCommon('actions.delete')} onClick={() => setDeleteTarget(row)}>
              <DeleteOutlineIcon fontSize="small" />
            </IconActionButton>
          </ThemedTooltip>
        </Box>
      ),
    },
  ]

  return (
    <Stack spacing={2}>
      <Paper sx={{ ...CARD_VARIANTS.default.styles, p: 2 }}>
        <Stack direction="row" alignItems="flex-start" justifyContent="space-between" spacing={1}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="subtitle1" fontWeight={600}>
              {t('accounts.platformTitle')}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {t('accounts.platformHint')}
            </Typography>
          </Box>
          {isPlatformOwner && (
            <ActionButton
              tone="secondary"
              size="small"
              onClick={() => navigate('/adapters/email/accounts')}
            >
              {t('accounts.managePlatform')}
            </ActionButton>
          )}
        </Stack>
        <Box sx={{ mt: 1.5 }}>
          {platformQuery.error ? (
            <QueryErrorState error={platformQuery.error} onRetry={() => void platformQuery.refetch()} compact />
          ) : (platformQuery.data ?? []).length === 0 ? (
            <Stack spacing={1} alignItems="flex-start">
              <Typography variant="body2" color="text.secondary">
                {platformQuery.isPending
                  ? t('loading')
                  : isPlatformOwner
                    ? t('accounts.platformEmptyPlatform')
                    : t('accounts.platformEmpty')}
              </Typography>
              {isPlatformOwner && !platformQuery.isPending && (
                <ActionButton
                  tone="secondary"
                  size="small"
                  onClick={() => navigate('/adapters/email/accounts')}
                >
                  {t('accounts.managePlatform')}
                </ActionButton>
              )}
            </Stack>
          ) : (
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              {(platformQuery.data ?? []).map(account => (
                <Chip
                  key={account.account_ref}
                  label={
                    account.is_default_sender
                      ? `${account.display_label} · ${ownDefault ? t('accounts.overridden') : t('accounts.instanceDefault')}`
                      : account.display_label
                  }
                  variant="outlined"
                  sx={account.is_default_sender && !ownDefault ? CHIP_VARIANTS.getCustomColorChip(theme.palette.primary.main) : undefined}
                />
              ))}
            </Stack>
          )}
        </Box>
      </Paper>

      <Box>
        <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1} sx={{ mb: 1 }} flexWrap="wrap" useFlexGap>
          <Box>
            <Typography variant="subtitle1" fontWeight={600}>
              {t('accounts.ownTitle')}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {t('accounts.ownHint')}
            </Typography>
          </Box>
          <ActionButton tone="primary" startIcon={<AddRoundedIcon />} onClick={() => setDialogAccount('new')}>
            {t('accounts.add')}
          </ActionButton>
        </Stack>
        <ServerDataTable
          columns={columns}
          rows={accounts}
          getRowId={row => row.id}
          total={accounts.length}
          page={1}
          pageSize={Math.max(accounts.length, 1)}
          onPageChange={() => undefined}
          loading={accountsQuery.isPending}
          fetching={accountsQuery.isFetching}
          error={accountsQuery.error}
          onRetry={() => void accountsQuery.refetch()}
          emptyTitle={t('accounts.emptyTitle')}
          emptyHint={t('accounts.emptyHint')}
        />
      </Box>

      {dialogAccount !== null && (
        <AccountDialog
          scope={scope}
          account={dialogAccount === 'new' ? null : dialogAccount}
          providers={providersQuery.data ?? []}
          channelOptions={channelOptions}
          onClose={() => setDialogAccount(null)}
          onSaved={() => {
            setDialogAccount(null)
            invalidate()
          }}
        />
      )}

      {testResult && (
        <NekroDialog
          open
          onClose={() => setTestResult(null)}
          title={t('accounts.testTitle', { address: testResult.account.username })}
          maxWidth="sm"
          fullWidth
          actions={
            <ActionButton tone="primary" onClick={() => setTestResult(null)}>
              {tCommon('actions.close')}
            </ActionButton>
          }
        >
          <Stack spacing={1.5}>
            <Alert severity={testResult.result.smtp.success ? 'success' : 'error'}>
              <strong>SMTP</strong> · {testResult.result.smtp.message}
            </Alert>
            <Alert severity={testResult.result.imap.success ? 'success' : 'error'}>
              <strong>IMAP</strong> · {testResult.result.imap.message}
            </Alert>
          </Stack>
        </NekroDialog>
      )}

      {deleteTarget && (
        <NekroDialog
          open
          onClose={() => setDeleteTarget(null)}
          title={t('accounts.deleteTitle')}
          maxWidth="sm"
          actions={
            <>
              <ActionButton tone="ghost" onClick={() => setDeleteTarget(null)} disabled={deleteMutation.isPending}>
                {tCommon('actions.cancel')}
              </ActionButton>
              <ActionButton tone="danger" onClick={() => deleteMutation.mutate(deleteTarget)} disabled={deleteMutation.isPending}>
                {tCommon('actions.delete')}
              </ActionButton>
            </>
          }
        >
          <Typography variant="body2">{t('accounts.deleteDescription', { address: deleteTarget.username })}</Typography>
        </NekroDialog>
      )}

      {transferTarget && (
        <NekroDialog
          open
          onClose={() => setTransferTarget(null)}
          title={t('accounts.transferTitle')}
          maxWidth="sm"
          actions={
            <>
              <ActionButton tone="ghost" onClick={() => setTransferTarget(null)} disabled={transferMutation.isPending}>
                {tCommon('actions.cancel')}
              </ActionButton>
              <ActionButton tone="primary" onClick={() => transferMutation.mutate(transferTarget)} disabled={transferMutation.isPending}>
                {tCommon('actions.confirm')}
              </ActionButton>
            </>
          }
        >
          <Typography variant="body2">{t('accounts.transferDescription', { address: transferTarget.username })}</Typography>
        </NekroDialog>
      )}
    </Stack>
  )
}
