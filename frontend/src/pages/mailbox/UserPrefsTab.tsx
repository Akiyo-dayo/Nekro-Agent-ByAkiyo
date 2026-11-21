import { useState } from 'react'
import { Box, Chip, Stack, Typography, useTheme } from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import ServerDataTable, { type ServerDataTableColumn } from '../../components/common/ServerDataTable'
import { ThemedTooltip } from '../../components/common/ThemedTooltip'
import { useNotification } from '../../hooks/useNotification'
import { mailboxApi, type MailboxScope, type UserPrefItem } from '../../services/api/mailbox'
import { CHIP_VARIANTS } from '../../theme/variants'

const PAGE_SIZE = 25

/**
 * 「用户邮件偏好」标签
 *
 * 用户在聊天里让 Bot 记下的收件地址与节日邮件的订阅 / 退订。地址只显示遮掩后的样子。
 * 删除只清地址与订阅，退订标记留着：删一行不能让这个人重新变成默认收件人。
 */
export default function UserPrefsTab({ scope }: { scope: MailboxScope }) {
  const theme = useTheme()
  const { t } = useTranslation('mailbox')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)
  const [deleteTarget, setDeleteTarget] = useState<UserPrefItem | null>(null)
  const scopeKey = `${scope.adapter_key}|${scope.instance_key}`

  const query = useQuery({
    queryKey: ['mailbox', 'user-prefs', scopeKey, page],
    queryFn: () => mailboxApi.listUserPrefs(scope, (page - 1) * PAGE_SIZE, PAGE_SIZE),
    retry: false,
  })

  const deleteMutation = useMutation({
    mutationFn: (pref: UserPrefItem) => mailboxApi.deleteUserPref(scope, pref.id),
    onSuccess: () => {
      notification.success(tCommon('messages.deleteSuccess'))
      setDeleteTarget(null)
      void queryClient.invalidateQueries({ queryKey: ['mailbox', 'user-prefs'] })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const columns: ServerDataTableColumn<UserPrefItem>[] = [
    {
      key: 'user',
      header: t('userPrefs.columns.user'),
      width: 160,
      render: row => (
        <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
          {row.platform_userid}
        </Typography>
      ),
    },
    {
      key: 'email',
      header: t('userPrefs.columns.email'),
      width: 200,
      render: row => <Typography variant="body2">{row.email || '-'}</Typography>,
    },
    {
      key: 'festival',
      header: t('userPrefs.columns.festival'),
      width: 180,
      render: row => (
        <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
          {row.unsubscribed ? (
            <Chip size="small" label={t('userPrefs.unsubscribed')} sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.warning.main, true)} />
          ) : row.festival_subscribed ? (
            <Chip size="small" label={t('userPrefs.subscribed')} sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.success.main, true)} />
          ) : (
            <Chip size="small" label={t('userPrefs.default')} sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.text.secondary, true)} />
          )}
        </Stack>
      ),
    },
    {
      key: 'changed',
      header: t('userPrefs.columns.changed'),
      width: 170,
      hideOnMobile: true,
      render: row => (
        <Typography variant="caption" color="text.secondary">
          {row.unsubscribed_at ? new Date(row.unsubscribed_at).toLocaleString() : '-'}
        </Typography>
      ),
    },
    {
      key: 'chat',
      header: t('userPrefs.columns.chat'),
      width: 220,
      hideOnMobile: true,
      render: row => (
        <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-all' }}>
          {row.set_in_chat_key || '-'}
        </Typography>
      ),
    },
    {
      key: 'actions',
      header: t('userPrefs.columns.actions'),
      width: 80,
      render: row => (
        <ThemedTooltip title={tCommon('actions.delete')}>
          <span>
            <IconActionButton
              tone="danger"
              size="small"
              aria-label={tCommon('actions.delete')}
              disabled={!row.email && !row.festival_subscribed}
              onClick={() => setDeleteTarget(row)}
            >
              <DeleteOutlineIcon fontSize="small" />
            </IconActionButton>
          </span>
        </ThemedTooltip>
      ),
    },
  ]

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      <Typography variant="caption" color="text.secondary">
        {t('userPrefs.hint')}
      </Typography>
      <ServerDataTable
        columns={columns}
        rows={query.data?.items ?? []}
        getRowId={row => row.id}
        total={query.data?.total ?? 0}
        page={page}
        pageSize={PAGE_SIZE}
        onPageChange={setPage}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        onRetry={() => void query.refetch()}
        emptyTitle={t('userPrefs.emptyTitle')}
        emptyHint={t('userPrefs.emptyHint')}
      />
      {deleteTarget && (
        <NekroDialog
          open
          onClose={() => setDeleteTarget(null)}
          title={t('userPrefs.deleteTitle')}
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
          <Typography variant="body2">{t('userPrefs.deleteDescription')}</Typography>
        </NekroDialog>
      )}
    </Box>
  )
}
