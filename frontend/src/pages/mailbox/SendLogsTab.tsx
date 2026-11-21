import { useState } from 'react'
import { Box, Chip, Typography, useTheme } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ServerDataTable, { type ServerDataTableColumn } from '../../components/common/ServerDataTable'
import { ThemedTooltip } from '../../components/common/ThemedTooltip'
import { mailboxApi, type MailboxScope, type SendLogItem } from '../../services/api/mailbox'
import { CHIP_VARIANTS } from '../../theme/variants'

const PAGE_SIZE = 25

/**
 * 「发信记录」标签
 *
 * 每次发信（会话里的 `send_email` 与节日邮件的最终结果）一行。平台方的邮箱对租户只显示名字；
 * 失败原因对租户是归类后的一句话（平台方邮箱那几行不给原因）。理由是模型写的，原样显示。
 */
export default function SendLogsTab({ scope }: { scope: MailboxScope }) {
  const theme = useTheme()
  const { t } = useTranslation('mailbox')
  const [page, setPage] = useState(1)
  const scopeKey = `${scope.adapter_key}|${scope.instance_key}`

  const query = useQuery({
    queryKey: ['mailbox', 'send-logs', scopeKey, page],
    queryFn: () => mailboxApi.listSendLogs(scope, (page - 1) * PAGE_SIZE, PAGE_SIZE),
    retry: false,
  })

  const columns: ServerDataTableColumn<SendLogItem>[] = [
    {
      key: 'time',
      header: t('sendLogs.columns.time'),
      width: 170,
      render: row => (
        <Typography variant="caption" color="text.secondary">
          {new Date(row.create_time).toLocaleString()}
        </Typography>
      ),
    },
    {
      key: 'account',
      header: t('sendLogs.columns.account'),
      width: 170,
      render: row => <Typography variant="body2">{row.display_label}</Typography>,
    },
    {
      key: 'to',
      header: t('sendLogs.columns.to'),
      width: 200,
      render: row => (
        <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
          {row.to_address}
        </Typography>
      ),
    },
    {
      key: 'subject',
      header: t('sendLogs.columns.subject'),
      width: 220,
      render: row => (
        <Box>
          <Typography variant="body2">{row.subject || t('sendLogs.noSubject')}</Typography>
          {row.purpose === 'festival' && (
            <Typography variant="caption" color="text.secondary">
              {t('sendLogs.festival')}
            </Typography>
          )}
        </Box>
      ),
    },
    {
      key: 'reason',
      header: t('sendLogs.columns.reason'),
      width: 240,
      hideOnMobile: true,
      render: row => (
        <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
          {row.reason || '-'}
        </Typography>
      ),
    },
    {
      key: 'result',
      header: t('sendLogs.columns.result'),
      width: 110,
      render: row =>
        row.ok ? (
          <Chip size="small" label={t('sendLogs.ok')} sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.success.main, true)} />
        ) : (
          <ThemedTooltip title={row.error || t('sendLogs.failedNoDetail')}>
            <Chip size="small" label={t('sendLogs.failed')} sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.error.main, true)} />
          </ThemedTooltip>
        ),
    },
  ]

  return (
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
      emptyTitle={t('sendLogs.emptyTitle')}
      emptyHint={t('sendLogs.emptyHint')}
    />
  )
}
