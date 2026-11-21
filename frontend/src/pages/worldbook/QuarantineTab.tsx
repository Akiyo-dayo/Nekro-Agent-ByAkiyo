import { useMemo, useState } from 'react'
import { Box, Typography } from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate, useSearchParams } from 'react-router-dom'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import RestoreIcon from '@mui/icons-material/Restore'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../../components/common/ServerDataTable'
import { ThemedTooltip } from '../../components/common/ThemedTooltip'
import { WorldbookQuarantineReasonChip } from '../../components/worldbook/chips'
import { formatDateTime } from '../../components/memory/format'
import { memoryErrorMessage } from '../../components/memory/errorMessage'
import {
  DEFAULT_WORLDBOOK_PAGE_SIZE,
  parseWorldbookPage,
} from '../../components/worldbook/filters'
import WorldbookNotFoundState from '../../components/worldbook/WorldbookNotFoundState'
import { useNotification } from '../../hooks/useNotification'
import { worldbookBookPath } from '../../router/routes'
import { isNotFoundError } from '../../services/api/axios'
import {
  worldbookApi,
  type WorldbookQuarantineItem,
  type WorldbookScopeParams,
} from '../../services/api/worldbook'

/**
 * 「隔离区」标签
 *
 * 隔离是「暂时不注入」，不是删除。所以每一行最显眼的动作是「放回」，删除退居其后；
 * 放回之后被拦计数归零，通知里把这一点说清楚。
 */

interface QuarantineTabProps {
  scope: WorldbookScopeParams
  isScopeReady: boolean
}

export default function QuarantineTab({ scope, isScopeReady }: QuarantineTabProps) {
  const { t } = useTranslation('worldbook')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [deleteTarget, setDeleteTarget] = useState<WorldbookQuarantineItem | null>(null)

  const page = useMemo(() => parseWorldbookPage(searchParams.get('page')), [searchParams])

  const listQuery = useQuery({
    queryKey: ['worldbook', 'quarantine', scope, page],
    queryFn: () =>
      worldbookApi.listQuarantine({
        ...scope,
        page,
        page_size: DEFAULT_WORLDBOOK_PAGE_SIZE,
      }),
    enabled: isScopeReady,
    retry: false,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['worldbook'] })
  }

  const restoreMutation = useMutation({
    mutationFn: (entryId: number) => worldbookApi.entryAction(entryId, 'restore'),
    onSuccess: () => {
      notification.success(t('quarantine.restoreSuccess'))
      invalidate()
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const deleteMutation = useMutation({
    mutationFn: (entryId: number) => worldbookApi.deleteEntry(entryId),
    onSuccess: () => {
      notification.success(t('quarantine.deleteSuccess'))
      setDeleteTarget(null)
      invalidate()
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const changePage = (nextPage: number) => {
    const params = new URLSearchParams(searchParams)
    if (nextPage > 1) params.set('page', String(nextPage))
    else params.delete('page')
    setSearchParams(params, { replace: true })
  }

  const columns: ServerDataTableColumn<WorldbookQuarantineItem>[] = [
    {
      key: 'book',
      header: t('quarantine.columns.book'),
      width: 200,
      render: row => (
        <Typography
          variant="body2"
          role="link"
          tabIndex={0}
          onClick={() => navigate(worldbookBookPath(row.book_id))}
          onKeyDown={event => {
            if (event.key === 'Enter' || event.key === ' ') {
              navigate(worldbookBookPath(row.book_id))
            }
          }}
          sx={{
            color: 'primary.main',
            cursor: 'pointer',
            '&:hover': { textDecoration: 'underline' },
          }}
        >
          {row.book_name}
        </Typography>
      ),
    },
    {
      key: 'title',
      header: t('quarantine.columns.title'),
      width: 240,
      render: row => (
        <Typography variant="body2">
          {row.comment || `#${row.entry_id}`}
        </Typography>
      ),
    },
    {
      key: 'reason',
      header: t('quarantine.columns.reason'),
      width: 180,
      render: row => (
        <WorldbookQuarantineReasonChip
          reason={row.quarantine_reason}
          blockedHits={row.blocked_round_hits}
        />
      ),
    },
    {
      key: 'blocked',
      header: t('quarantine.columns.blockedHits'),
      width: 110,
      align: 'right',
      render: row => (
        <Typography variant="body2">{row.blocked_round_hits}</Typography>
      ),
    },
    {
      key: 'quarantinedAt',
      header: t('quarantine.columns.quarantinedAt'),
      width: 180,
      hideOnMobile: true,
      render: row => (
        <Typography variant="caption" color="text.secondary">
          {formatDateTime(row.quarantined_at)}
        </Typography>
      ),
    },
    {
      key: 'actions',
      header: t('quarantine.columns.actions'),
      width: 120,
      render: row => (
        <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
          <ThemedTooltip title={t('quarantine.restore')}>
            <IconActionButton
              tone="primary"
              size="small"
              aria-label={t('quarantine.restore')}
              disabled={restoreMutation.isPending}
              onClick={() => restoreMutation.mutate(row.entry_id)}
            >
              <RestoreIcon fontSize="small" />
            </IconActionButton>
          </ThemedTooltip>
          <ThemedTooltip title={t('quarantine.delete')}>
            <IconActionButton
              tone="danger"
              size="small"
              aria-label={t('quarantine.delete')}
              onClick={() => setDeleteTarget(row)}
            >
              <DeleteOutlineIcon fontSize="small" />
            </IconActionButton>
          </ThemedTooltip>
        </Box>
      ),
    },
  ]

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, flex: 1, minHeight: 0 }}>
      <Typography variant="caption" color="text.secondary">
        {t('quarantine.hint')}
      </Typography>

      <Box sx={{ flex: 1, minHeight: 0 }}>
        <ServerDataTable
          columns={columns}
          rows={listQuery.data?.items ?? []}
          getRowId={row => row.entry_id}
          total={listQuery.data?.total ?? 0}
          page={page}
          pageSize={DEFAULT_WORLDBOOK_PAGE_SIZE}
          onPageChange={changePage}
          loading={listQuery.isPending}
          fetching={listQuery.isFetching}
          error={listQuery.error}
          onRetry={() => void listQuery.refetch()}
          renderError={error =>
            isNotFoundError(error) ? (
              <WorldbookNotFoundState compact />
            ) : (
              <QueryErrorState
                error={error}
                onRetry={() => void listQuery.refetch()}
                compact
              />
            )
          }
          emptyTitle={t('quarantine.emptyTitle')}
          emptyHint={t('quarantine.emptyHint')}
        />
      </Box>

      {deleteTarget && (
        <NekroDialog
          open
          onClose={() => setDeleteTarget(null)}
          title={t('quarantine.deleteTitle')}
          maxWidth="sm"
          actions={
            <>
              <ActionButton
                tone="ghost"
                onClick={() => setDeleteTarget(null)}
                disabled={deleteMutation.isPending}
              >
                {tCommon('actions.cancel')}
              </ActionButton>
              <ActionButton
                tone="danger"
                onClick={() => deleteMutation.mutate(deleteTarget.entry_id)}
                disabled={deleteMutation.isPending}
              >
                {tCommon('actions.delete')}
              </ActionButton>
            </>
          }
        >
          <Typography variant="body2">{t('quarantine.deleteDescription')}</Typography>
        </NekroDialog>
      )}
    </Box>
  )
}
