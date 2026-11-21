import { Box, CircularProgress, List, ListItem, ListItemText, Typography } from '@mui/material'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import NekroDialog from '../common/NekroDialog'
import QueryErrorState from '../common/QueryErrorState'
import { useNotification } from '../../hooks/useNotification'
import { isNotFoundError } from '../../services/api/axios'
import {
  memoryApi,
  type HardDeleteReport,
  type MemoryScopeParams,
  type MemoryType,
} from '../../services/api/memory'
import MemoryNotFoundState from './MemoryNotFoundState'
import { memoryErrorMessage } from './errorMessage'

/**
 * 删除确认
 *
 * 软删与彻底删除共用这一个对话框：两者只差一段说明与是否需要先看连带条数。
 * 彻底删除一个段落时先走一次批量预览（`dry_run`），把会一起消失的事件、关系、
 * 涉及的人与强化日志摊开；episode 只删自己，成员段落之后会重新聚合，不需要连带数字。
 *
 * 每个动作完成后都由调用方刷新列表，这里只负责把话说清楚。
 *
 * 预览用的作用域由调用方给：记忆页跟着顶栏的实例与域筛选，频道详情跟着频道自己的实例。
 * 在这里自己去读顶栏的话，租户在频道详情里打开别的实例的频道时预览会是 0 条。
 */

interface DeleteConfirmDialogProps {
  open: boolean
  memoryType: MemoryType
  itemId: number
  hard: boolean
  scope: MemoryScopeParams
  onClose: () => void
  onDeleted: () => void
}

function cascadeTotal(report: HardDeleteReport): number {
  return (
    report.persons + report.relations + report.episodes + report.entities + report.reinforcement_logs
  )
}

export default function DeleteConfirmDialog({
  open,
  memoryType,
  itemId,
  hard,
  scope,
  onClose,
  onDeleted,
}: DeleteConfirmDialogProps) {
  const notification = useNotification()
  const { t } = useTranslation('memory')
  const { t: tCommon } = useTranslation('common')

  const needsPreview = hard && memoryType === 'paragraph'
  const previewQuery = useQuery({
    queryKey: ['memory', 'hard-delete-preview', memoryType, itemId, scope],
    queryFn: () =>
      memoryApi.bulkParagraphs(
        { selection: { ids: [itemId] }, action: 'hard_delete', dry_run: true },
        scope
      ),
    enabled: open && needsPreview,
    retry: false,
  })

  const deleteMutation = useMutation({
    mutationFn: () => memoryApi.deleteItem(memoryType, itemId, hard),
    onSuccess: report => {
      if (!hard) {
        notification.success(t('actions.deleteSuccess'))
      } else if (memoryType === 'episode') {
        notification.success(t('hardDeleteDialog.successEpisode'))
      } else {
        notification.success(
          t('hardDeleteDialog.success', { count: cascadeTotal(report) })
        )
      }
      onDeleted()
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const matched = previewQuery.data?.matched ?? 0
  const cascade = previewQuery.data?.cascade ?? null
  const confirmDisabled =
    deleteMutation.isPending ||
    (needsPreview &&
      (previewQuery.isPending || previewQuery.isFetching || previewQuery.isError || matched === 0))

  const renderHardDeleteBody = () => {
    if (memoryType === 'episode') {
      return (
        <Typography variant="body2">{t('hardDeleteDialog.episodeDescription')}</Typography>
      )
    }
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
    if (matched === 0) {
      return <Typography variant="body2">{t('bulk.noMatch')}</Typography>
    }
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        <Typography variant="body2">
          {t('bulk.previewLine', { matched, action: t('actions.hardDelete') })}
        </Typography>
        {cascade && (
          <Typography variant="body2" color="text.secondary">
            {t('bulk.cascade', {
              episodes: cascade.episodes,
              relations: cascade.relations,
              persons: cascade.persons,
              reinforcementLogs: cascade.reinforcement_logs,
            })}
          </Typography>
        )}
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
      </Box>
    )
  }

  return (
    <NekroDialog
      open={open}
      onClose={onClose}
      title={hard ? t('hardDeleteDialog.title') : t('deleteDialog.title')}
      maxWidth="sm"
      actions={
        <>
          <ActionButton tone="ghost" onClick={onClose} disabled={deleteMutation.isPending}>
            {tCommon('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone="danger"
            disabled={confirmDisabled}
            onClick={() => deleteMutation.mutate()}
          >
            {hard ? t('hardDeleteDialog.confirm') : t('deleteDialog.confirm')}
          </ActionButton>
        </>
      }
    >
      {hard ? (
        renderHardDeleteBody()
      ) : (
        <Typography variant="body2">{t('deleteDialog.description')}</Typography>
      )}
    </NekroDialog>
  )
}
