import {
  Box,
  Chip,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
  useTheme,
  type SxProps,
  type Theme,
} from '@mui/material'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import ActionButton from '../common/ActionButton'
import { ThemedTooltip } from '../common/ThemedTooltip'
import { WorldbookTrustChip } from '../worldbook/chips'
import { memoryErrorMessage } from './errorMessage'
import { useNotification } from '../../hooks/useNotification'
import { worldbookBookPath } from '../../router/routes'
import {
  worldbookApi,
  type WorldbookEntryAction,
} from '../../services/api/worldbook'
import type {
  PromptRoundDetail,
  PromptRoundWorldbookItem,
} from '../../services/api/memory'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'

/**
 * 提示词明细里的世界书一节
 *
 * 两张表：真正注入的条目，以及被预算跳过的条目。前者带隔离 / 停用两个动作，与记忆条目
 * 一样按「当前状态」决定能做什么；后者只解释「为什么没进来」，没有操作列。
 *
 * 条目本身不带归属列，看的范围算在书上：租户看不到的书由服务端标成 `hidden` 并抹掉
 * 书 ID、书名与标题，这里只负责把它画成「不可见」，不再猜是哪本书。
 */

function RoundWorldbookStatusChip({ status }: { status: string }) {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')

  if (status === 'hidden') {
    return (
      <ThemedTooltip title={t('roundDetail.status.hiddenHint')}>
        <Chip
          size="small"
          label={t('roundDetail.status.hidden')}
          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.text.disabled, true)}
        />
      </ThemedTooltip>
    )
  }

  const colors: Record<string, string> = {
    active: theme.palette.success.main,
    disabled: theme.palette.text.disabled,
    quarantined: theme.palette.error.main,
    hard_deleted: theme.palette.text.disabled,
  }
  const labels: Record<string, string> = {
    active: 'roundDetail.status.active',
    disabled: 'roundDetail.status.disabled',
    quarantined: 'roundDetail.status.quarantined',
    hard_deleted: 'roundDetail.status.hard_deleted',
  }
  const known = status in labels
  return (
    <Chip
      size="small"
      label={known ? t(labels[status]) : t('roundDetail.status.unknown')}
      sx={CHIP_VARIANTS.getCustomColorChip(
        colors[status] ?? theme.palette.text.disabled,
        true
      )}
    />
  )
}

export default function PromptRoundWorldbookItems({
  detail,
}: {
  detail: PromptRoundDetail
}) {
  const navigate = useNavigate()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('worldbook')
  const { t: tCommon } = useTranslation('common')

  const injectedItems = detail.worldbook_items.filter(item => item.type === 'worldbook')
  const droppedItems = detail.worldbook_items.filter(
    item => item.type === 'worldbook_dropped'
  )

  const actionMutation = useMutation({
    mutationFn: (variables: { id: number; action: WorldbookEntryAction }) =>
      worldbookApi.entryAction(variables.id, variables.action),
    onSuccess: (_data, variables) => {
      notification.success(t(`detail.actionSuccess.${variables.action}`))
      void queryClient.invalidateQueries({
        queryKey: ['memory', 'prompt-round', detail.id],
      })
      void queryClient.invalidateQueries({ queryKey: ['worldbook'] })
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const positionText = (position: string | undefined) => {
    if (position === 'after_persona') return t('roundDetail.positions.afterPersona')
    if (position === 'before_history') return t('roundDetail.positions.beforeHistory')
    if (position === 'at_depth') return t('roundDetail.positions.atDepth')
    return position
  }

  const dropReasonText = (reason: string | undefined) => {
    if (!reason) return '—'
    return t(`roundDetail.dropReason.${reason}`, { defaultValue: reason })
  }

  const bookNameCell = (item: PromptRoundWorldbookItem) => {
    const bookId = item.book_id
    // 看不到的书不给链接；已删除的条目（书可能也一起删了）给链接只会落到「未找到」
    if (typeof bookId !== 'number' || item.status === 'hidden' || item.status === 'hard_deleted') {
      return (
        <Typography variant="body2">{item.book_name || '—'}</Typography>
      )
    }
    return (
      <Typography
        variant="body2"
        color="primary"
        onClick={() => navigate(worldbookBookPath(bookId))}
        sx={{
          cursor: 'pointer',
          wordBreak: 'break-word',
          '&:hover': { textDecoration: 'underline' },
        }}
      >
        {item.book_name || '—'}
      </Typography>
    )
  }

  const rowActions = (item: PromptRoundWorldbookItem) => {
    if (item.status !== 'active') {
      return (
        <Typography variant="caption" color="text.secondary">
          —
        </Typography>
      )
    }
    return (
      <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
        <ActionButton
          tone="ghost"
          size="small"
          disabled={actionMutation.isPending}
          onClick={() =>
            actionMutation.mutate({ id: item.id, action: 'quarantine' })
          }
        >
          {t('detail.actions.quarantine')}
        </ActionButton>
        <ActionButton
          tone="ghost"
          size="small"
          disabled={actionMutation.isPending}
          onClick={() => actionMutation.mutate({ id: item.id, action: 'disable' })}
        >
          {t('detail.actions.disable')}
        </ActionButton>
      </Box>
    )
  }

  const stateLabel = t(`roundDetail.state.${detail.worldbook_state}`, {
    defaultValue: t('common.unknown'),
  })

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <Typography variant="subtitle2" fontWeight={700}>
        {t('roundDetail.title')}
      </Typography>
      <Typography variant="body2" color="text.secondary">
        {stateLabel}
        {' · '}
        {t('roundDetail.chars', { chars: detail.worldbook_chars })}
      </Typography>
      {detail.worldbook_state === 'suppressed' && (
        <Typography variant="caption" color="text.secondary">
          {t('roundDetail.suppressedHint')}
        </Typography>
      )}

      {injectedItems.length === 0 && droppedItems.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t('roundDetail.empty')}
        </Typography>
      ) : (
        <>
          {injectedItems.length > 0 && (
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
              <Typography variant="subtitle2" fontWeight={700}>
                {t('roundDetail.injectedTitle')}
              </Typography>
              <Box sx={{ overflowX: 'auto', width: '100%' }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.book')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.title')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.position')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.trust')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.status')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.chars')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.renders')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.actions')}
                      </TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {injectedItems.map(item => (
                      <TableRow key={`worldbook:${item.id}`}>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          {bookNameCell(item)}
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <Typography variant="body2">
                            {item.comment || `#${item.id}`}
                          </Typography>
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <Typography variant="body2">
                            {positionText(item.position) || '—'}
                          </Typography>
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <WorldbookTrustChip trusted={Boolean(item.trusted)} />
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <RoundWorldbookStatusChip status={item.status} />
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <Typography variant="body2">{item.chars}</Typography>
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <Typography variant="body2">
                            {item.renders ?? 0}
                          </Typography>
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          {rowActions(item)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            </Box>
          )}

          {droppedItems.length > 0 && (
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
              <Typography variant="subtitle2" fontWeight={700}>
                {t('roundDetail.droppedTitle')}
              </Typography>
              <Box sx={{ overflowX: 'auto', width: '100%' }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.book')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.title')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.chars')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.reason')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                        {t('roundDetail.columns.status')}
                      </TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {droppedItems.map(item => (
                      <TableRow key={`worldbook_dropped:${item.id}`}>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <Typography variant="body2">
                            {item.book_name || '—'}
                          </Typography>
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <Typography variant="body2">
                            {item.comment || `#${item.id}`}
                          </Typography>
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <Typography variant="body2">{item.chars}</Typography>
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <Typography variant="body2" color="text.secondary">
                            {dropReasonText(item.reason)}
                          </Typography>
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <RoundWorldbookStatusChip status={item.status} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            </Box>
          )}
        </>
      )}
    </Box>
  )
}
