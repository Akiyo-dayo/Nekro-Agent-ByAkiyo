import { useState } from 'react'
import { Box, LinearProgress, Stack, Typography } from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import ServerDataTable, { type ServerDataTableColumn } from '../../components/common/ServerDataTable'
import { useIdentity } from '../../hooks/useIdentity'
import { useNotification } from '../../hooks/useNotification'
import { mailboxApi, type MailboxScope, type OutboxBatch } from '../../services/api/mailbox'

type BatchAction = 'pause' | 'resume' | 'void'

const count = (batch: OutboxBatch, key: keyof OutboxBatch['counts']) => batch.counts[key] ?? 0

/**
 * 「节日邮件」标签
 *
 * 每一批是某个节日给本实例的一次群发：入队后由队列按发件邮箱匀速发出，每一封发出前都重新判一次
 * （退订、共享取消、插件关掉、今天已经发过）。租户只能看进度；平台方可以暂停、恢复或作废一批。
 */
export default function FestivalTab({ scope }: { scope: MailboxScope }) {
  const { t } = useTranslation('mailbox')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { isPlatformOwner } = useIdentity()
  const [confirm, setConfirm] = useState<{ batch: OutboxBatch; action: BatchAction } | null>(null)
  const scopeKey = `${scope.adapter_key}|${scope.instance_key}`

  const query = useQuery({
    queryKey: ['mailbox', 'outbox', scopeKey],
    queryFn: () => mailboxApi.listOutboxBatches(scope),
    retry: false,
    refetchInterval: 30_000,
  })

  const actionMutation = useMutation({
    mutationFn: ({ batch, action }: { batch: OutboxBatch; action: BatchAction }) =>
      mailboxApi.outboxAction(scope, action, batch.batch_key),
    onSuccess: result => {
      notification.success(t('festival.actionDone', { count: result.changed }))
      setConfirm(null)
      void queryClient.invalidateQueries({ queryKey: ['mailbox', 'outbox'] })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const rows = query.data ?? []

  const columns: ServerDataTableColumn<OutboxBatch>[] = [
    {
      key: 'festival',
      header: t('festival.columns.festival'),
      width: 180,
      render: row => (
        <Box>
          <Typography variant="body2" fontWeight={600}>
            {row.festival_name}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {row.date}
          </Typography>
        </Box>
      ),
    },
    {
      key: 'recipients',
      header: t('festival.columns.recipients'),
      width: 100,
      align: 'right',
      render: row => {
        const total = Object.values(row.counts).reduce((sum, value) => sum + (value ?? 0), 0)
        return <Typography variant="body2">{total}</Typography>
      },
    },
    {
      key: 'progress',
      header: t('festival.columns.progress'),
      width: 300,
      render: row => {
        const total = Object.values(row.counts).reduce((sum, value) => sum + (value ?? 0), 0)
        const done = count(row, 'sent') + count(row, 'skipped') + count(row, 'failed')
        return (
          <Stack spacing={0.5}>
            <LinearProgress variant="determinate" value={total ? (done / total) * 100 : 0} sx={{ height: 6, borderRadius: 3 }} />
            <Typography variant="caption" color="text.secondary">
              {t('festival.progressText', {
                sent: count(row, 'sent'),
                skipped: count(row, 'skipped'),
                failed: count(row, 'failed'),
                queued: count(row, 'queued'),
                paused: count(row, 'paused'),
              })}
            </Typography>
          </Stack>
        )
      },
    },
    {
      key: 'queuedAt',
      header: t('festival.columns.queuedAt'),
      width: 170,
      hideOnMobile: true,
      render: row => (
        <Typography variant="caption" color="text.secondary">
          {row.first_queued_at ? new Date(row.first_queued_at).toLocaleString() : '-'}
        </Typography>
      ),
    },
    ...(isPlatformOwner
      ? [
          {
            key: 'actions',
            header: t('festival.columns.actions'),
            width: 220,
            render: (row: OutboxBatch) => {
              const pending = count(row, 'queued') + count(row, 'paused')
              return (
                <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                  {count(row, 'queued') > 0 && (
                    <ActionButton tone="secondary" size="small" onClick={() => setConfirm({ batch: row, action: 'pause' })}>
                      {t('festival.pause')}
                    </ActionButton>
                  )}
                  {count(row, 'paused') > 0 && (
                    <ActionButton tone="secondary" size="small" onClick={() => setConfirm({ batch: row, action: 'resume' })}>
                      {t('festival.resume')}
                    </ActionButton>
                  )}
                  {pending > 0 && (
                    <ActionButton tone="danger" size="small" onClick={() => setConfirm({ batch: row, action: 'void' })}>
                      {t('festival.void')}
                    </ActionButton>
                  )}
                </Stack>
              )
            },
          } satisfies ServerDataTableColumn<OutboxBatch>,
        ]
      : []),
  ]

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      <Typography variant="caption" color="text.secondary">
        {isPlatformOwner ? t('festival.hintPlatform') : t('festival.hintTenant')}
      </Typography>
      <ServerDataTable
        columns={columns}
        rows={rows}
        getRowId={row => row.batch_key}
        total={rows.length}
        page={1}
        pageSize={Math.max(rows.length, 1)}
        onPageChange={() => undefined}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        onRetry={() => void query.refetch()}
        emptyTitle={t('festival.emptyTitle')}
        emptyHint={t('festival.emptyHint')}
      />
      {confirm && (
        <NekroDialog
          open
          onClose={() => setConfirm(null)}
          title={t(`festival.confirm.${confirm.action}Title`)}
          maxWidth="sm"
          actions={
            <>
              <ActionButton tone="ghost" onClick={() => setConfirm(null)} disabled={actionMutation.isPending}>
                {tCommon('actions.cancel')}
              </ActionButton>
              <ActionButton
                tone={confirm.action === 'void' ? 'danger' : 'primary'}
                onClick={() => actionMutation.mutate(confirm)}
                disabled={actionMutation.isPending}
              >
                {tCommon('actions.confirm')}
              </ActionButton>
            </>
          }
        >
          <Typography variant="body2">
            {t(`festival.confirm.${confirm.action}Description`, { festival: confirm.batch.festival_name })}
          </Typography>
        </NekroDialog>
      )}
    </Box>
  )
}
