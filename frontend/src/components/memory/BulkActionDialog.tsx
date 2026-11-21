import { Box, CircularProgress, List, ListItem, ListItemText, Typography } from '@mui/material'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import NekroDialog from '../common/NekroDialog'
import QueryErrorState from '../common/QueryErrorState'
import { useNotification } from '../../hooks/useNotification'
import { ApiError, isNotFoundError } from '../../services/api/axios'
import {
  memoryApi,
  type BulkAction,
  type BulkSelection,
  type MemoryScopeParams,
} from '../../services/api/memory'
import MemoryNotFoundState from './MemoryNotFoundState'
import { memoryErrorMessage } from './errorMessage'

/**
 * 批量操作确认
 *
 * 先跑一次 `dry_run` 把条数（彻底删除时还有连带条数）给用户看，确认时才带
 * `expected_count` 真正执行。执行时如果条数已经变了，后端回 409；那时不关对话框，
 * 而是自动重新预览一遍，让用户对着新数字再决定一次。
 */

interface BulkActionDialogProps {
  /** null 表示对话框关着 */
  action: BulkAction | null
  selection: BulkSelection
  scope: MemoryScopeParams
  onClose: () => void
  onDone: (affected: number) => void
}

const ACTION_LABEL_KEYS: Record<BulkAction, string> = {
  delete: 'actions.delete',
  hard_delete: 'actions.hardDelete',
  quarantine: 'actions.quarantine',
  restore: 'actions.restore',
  freeze: 'actions.freeze',
  unfreeze: 'actions.unfreeze',
}

export default function BulkActionDialog({
  action,
  selection,
  scope,
  onClose,
  onDone,
}: BulkActionDialogProps) {
  const notification = useNotification()
  const { t } = useTranslation('memory')
  const { t: tCommon } = useTranslation('common')

  const previewQuery = useQuery({
    queryKey: ['memory', 'bulk-preview', action, selection, scope],
    queryFn: async () => {
      if (!action) throw new Error('bulk action is not selected')
      return memoryApi.bulkParagraphs({ selection, action, dry_run: true }, scope)
    },
    enabled: action !== null,
    retry: false,
  })

  const executeMutation = useMutation({
    mutationFn: async () => {
      if (!action) throw new Error('bulk action is not selected')
      return memoryApi.bulkParagraphs(
        {
          selection,
          action,
          expected_count: previewQuery.data?.matched ?? 0,
        },
        scope
      )
    },
    onSuccess: data => {
      if (data.failed_ids.length > 0) {
        notification.warning(
          t('bulk.partial', { affected: data.affected, failed: data.failed_ids.length })
        )
      } else {
        notification.success(t('bulk.success', { affected: data.affected }))
      }
      onDone(data.affected)
    },
    onError: error => {
      // 预览之后有人动了同一批记忆：刷新数字，对话框留着让用户再确认一次
      if (error instanceof ApiError && error.status === 409) {
        notification.warning(t('bulk.countChanged'))
        void previewQuery.refetch()
        return
      }
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      )
    },
  })

  const matched = previewQuery.data?.matched ?? 0
  const cascade = previewQuery.data?.cascade ?? null
  const isHardDelete = action === 'hard_delete'
  const confirmDisabled =
    executeMutation.isPending ||
    previewQuery.isPending ||
    previewQuery.isFetching ||
    previewQuery.isError ||
    matched === 0

  const renderBody = () => {
    if (previewQuery.isPending) {
      return (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
          <CircularProgress size={24} />
        </Box>
      )
    }
    if (previewQuery.isError) {
      return isNotFoundError(previewQuery.error) ? (
        <MemoryNotFoundState compact />
      ) : (
        <QueryErrorState
          error={previewQuery.error}
          onRetry={() => void previewQuery.refetch()}
          compact
        />
      )
    }
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        {matched === 0 && (
          <Typography variant="body2" color="text.secondary">
            {t('bulk.noMatch')}
          </Typography>
        )}
        {isHardDelete && cascade && matched > 0 && (
          <Typography variant="body2" color="text.secondary">
            {t('bulk.cascade', {
              episodes: cascade.episodes,
              relations: cascade.relations,
              persons: cascade.persons,
              reinforcementLogs: cascade.reinforcement_logs,
            })}
          </Typography>
        )}
        {isHardDelete && matched > 0 && (
          <List dense disablePadding>
            <ListItem disableGutters>
              <ListItemText primary={t('bulk.hardDeleteNotes.chatRecords')} />
            </ListItem>
            <ListItem disableGutters>
              <ListItemText primary={t('bulk.hardDeleteNotes.summary')} />
            </ListItem>
            <ListItem disableGutters>
              <ListItemText primary={t('bulk.hardDeleteNotes.promptRounds')} />
            </ListItem>
          </List>
        )}
      </Box>
    )
  }

  return (
    <NekroDialog
      open={action !== null}
      onClose={onClose}
      title={
        previewQuery.data
          ? t('bulk.previewLine', {
              matched,
              action: action ? t(ACTION_LABEL_KEYS[action]) : '',
            })
          : t('bulk.previewTitle')
      }
      maxWidth="sm"
      actions={
        <>
          <ActionButton tone="ghost" onClick={onClose} disabled={executeMutation.isPending}>
            {tCommon('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone={action === 'delete' || action === 'hard_delete' ? 'danger' : 'primary'}
            disabled={confirmDisabled}
            onClick={() => executeMutation.mutate()}
          >
            {t('bulk.confirm')}
          </ActionButton>
        </>
      }
    >
      {renderBody()}
    </NekroDialog>
  )
}
