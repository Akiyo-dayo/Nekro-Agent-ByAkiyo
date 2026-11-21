import { useMemo, useState, type ReactNode } from 'react'
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Stack,
  TextField,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import ActionButton from '../common/ActionButton'
import NekroDialog from '../common/NekroDialog'
import QueryErrorState from '../common/QueryErrorState'
import { WorldbookTrustChip } from './chips'
import WorldbookNotFoundState from './WorldbookNotFoundState'
import { useWorldbookWriteScope } from './useWorldbookWriteScope'
import { memoryErrorMessage } from '../memory/errorMessage'
import { formatDateTime, truncateText } from '../memory/format'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { useNotification } from '../../hooks/useNotification'
import { worldbookBookPath } from '../../router/routes'
import { isNotFoundError } from '../../services/api/axios'
import {
  WORLDBOOK_PERSON_NOTE_MAX_LENGTH,
  worldbookApi,
  type WorldbookPersonNoteItem,
} from '../../services/api/worldbook'
import { CHIP_VARIANTS } from '../../theme/variants'

/**
 * 好感详情里的「设定备注」
 *
 * 入口与出口都指向世界书页：这里只负责记一笔与只读列表，真正的编辑（关键词、位置、
 * 隔离 / 停用）在世界书页里做。因此每条都带一个跳转按钮，而不是在这里再长一套编辑框。
 *
 * 实例一律取顶栏选中的那个：好感行上的 `instance_key` 在组域里是空串，拿它去请求
 * 个人书会落到错误的实例上。单实例部署（没有切换器）用唯一的那个实例；多实例时
 * 没选实例只提示、不发请求。
 */

interface PersonNotesSectionProps {
  personAccount: string
  displayName: string
  /** 跳去世界书页之前先收起好感详情对话框 */
  onCloseDetail: () => void
  /**
   * 标题下的一句说明
   *
   * 由调用方给：这块备注在好感页里与「关系里程碑」并排，两边都得先说清自己是什么，
   * 否则管理员分不出哪段字是 Bot 记得的、哪段是自己写的。
   */
  description?: ReactNode
}

export default function PersonNotesSection({
  personAccount,
  displayName,
  onCloseDetail,
  description,
}: PersonNotesSectionProps) {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')
  const { t: tCommon } = useTranslation('common')
  const navigate = useNavigate()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { currentInstanceKey, currentInstance, isScopeReady } = useInstanceScope()

  const [dialogOpen, setDialogOpen] = useState(false)
  const [content, setContent] = useState('')

  const viewScope = useMemo(
    () => ({ instance_key: currentInstanceKey ?? undefined, adapter_key: currentInstance?.adapter_key }),
    [currentInstanceKey, currentInstance?.adapter_key]
  )
  const writeScope = useWorldbookWriteScope(viewScope)
  const instanceKey = writeScope.instance_key
  const adapterKey = writeScope.adapter_key
  const hasInstance = isScopeReady && Boolean(instanceKey)

  const notesQuery = useQuery({
    queryKey: [
      'worldbook',
      'person-notes',
      instanceKey,
      adapterKey,
      personAccount,
    ],
    queryFn: () =>
      worldbookApi.listPersonNotes({
        instance_key: instanceKey,
        adapter_key: adapterKey,
        person_account: personAccount,
      }),
    enabled: hasInstance && Boolean(personAccount),
    retry: false,
  })

  const trimmed = content.trim()
  const tooLong = trimmed.length > WORLDBOOK_PERSON_NOTE_MAX_LENGTH
  const canSubmit = hasInstance && trimmed.length > 0 && !tooLong

  const createMutation = useMutation({
    mutationFn: () =>
      worldbookApi.createPersonNote({
        adapter_key: adapterKey,
        instance_key: instanceKey as string,
        person_account: personAccount,
        content: trimmed,
      }),
    onSuccess: data => {
      notification.success(
        data.created_book
          ? t('personNotes.savedWithBook')
          : t('personNotes.saved')
      )
      setContent('')
      setDialogOpen(false)
      void queryClient.invalidateQueries({ queryKey: ['worldbook'] })
    },
    // 失败时不关对话框：管理员刚写的内容还在输入框里，重试不用重写
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const statusChip = (item: WorldbookPersonNoteItem) => {
    if (item.status === 'quarantined') {
      return (
        <Chip
          size="small"
          label={t('personNotes.statusQuarantined')}
          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.error.main, true)}
        />
      )
    }
    if (!item.enabled) {
      return (
        <Chip
          size="small"
          label={t('personNotes.statusDisabled')}
          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.text.disabled, true)}
        />
      )
    }
    return null
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 1,
          flexWrap: 'wrap',
        }}
      >
        <Typography variant="subtitle2" fontWeight={700}>
          {t('personNotes.title')}
        </Typography>
        {hasInstance && (
          <ActionButton
            tone="secondary"
            size="small"
            onClick={() => setDialogOpen(true)}
          >
            {t('personNotes.add')}
          </ActionButton>
        )}
      </Box>

      {description && (
        <Typography variant="caption" color="text.secondary">
          {description}
        </Typography>
      )}

      {!hasInstance ? (
        <Typography variant="caption" color="text.secondary">
          {t('personNotes.pickInstance')}
        </Typography>
      ) : notesQuery.isPending ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 1 }}>
          <CircularProgress size={20} />
        </Box>
      ) : notesQuery.isError && isNotFoundError(notesQuery.error) ? (
        // 实例在对话框打开期间被划走、或持久化的实例已失效：与世界书其它入口同一句「未找到」
        <WorldbookNotFoundState compact />
      ) : notesQuery.isError ? (
        <QueryErrorState
          error={notesQuery.error}
          onRetry={() => void notesQuery.refetch()}
          compact
        />
      ) : (notesQuery.data?.items.length ?? 0) === 0 ? (
        <Typography variant="caption" color="text.secondary">
          {t('personNotes.empty')}
        </Typography>
      ) : (
        <Stack spacing={1}>
          {(notesQuery.data?.items ?? []).map(item => (
            <Box
              key={`${item.book_id}:${item.entry_id}`}
              sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}
            >
              <Typography
                variant="body2"
                sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}
              >
                {truncateText(item.content, 200)}
              </Typography>
              <Stack
                direction="row"
                spacing={0.75}
                sx={{ alignItems: 'center', flexWrap: 'wrap' }}
              >
                {statusChip(item)}
                <WorldbookTrustChip trusted={item.trusted} />
                <Typography variant="caption" color="text.secondary">
                  {t('personNotes.updatedAt', {
                    time: formatDateTime(item.update_time),
                  })}
                </Typography>
                <ActionButton
                  tone="ghost"
                  size="small"
                  onClick={() => {
                    onCloseDetail()
                    navigate(worldbookBookPath(item.book_id))
                  }}
                >
                  {t('personNotes.editInWorldbook')}
                </ActionButton>
              </Stack>
            </Box>
          ))}
        </Stack>
      )}

      <NekroDialog
        open={dialogOpen}
        onClose={() => {
          if (createMutation.isPending) return
          setDialogOpen(false)
        }}
        title={t('personNotes.addTitleFor', { name: displayName })}
        maxWidth="sm"
        actions={
          <>
            <ActionButton
              tone="ghost"
              disabled={createMutation.isPending}
              onClick={() => setDialogOpen(false)}
            >
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="primary"
              disabled={!canSubmit || createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              {createMutation.isPending ? (
                <CircularProgress size={18} />
              ) : (
                t('personNotes.add')
              )}
            </ActionButton>
          </>
        }
      >
        <Stack spacing={2} sx={{ pt: 1 }}>
          <Alert severity="warning">{t('personNotes.warning')}</Alert>
          <TextField
            multiline
            minRows={3}
            maxRows={8}
            fullWidth
            label={t('personNotes.contentLabel')}
            placeholder={t('personNotes.contentPlaceholder')}
            value={content}
            error={tooLong}
            onChange={event => setContent(event.target.value)}
            helperText={
              tooLong
                ? t('personNotes.contentInvalid', {
                    max: WORLDBOOK_PERSON_NOTE_MAX_LENGTH,
                  })
                : t('personNotes.contentCount', {
                    count: trimmed.length,
                    max: WORLDBOOK_PERSON_NOTE_MAX_LENGTH,
                  })
            }
          />
        </Stack>
      </NekroDialog>
    </Box>
  )
}
