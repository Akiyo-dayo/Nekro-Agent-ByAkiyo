import { useEffect, useState } from 'react'
import {
  Box,
  Chip,
  CircularProgress,
  Stack,
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
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import QueryErrorState from '../common/QueryErrorState'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../common/ServerDataTable'
import DeleteConfirmDialog from './DeleteConfirmDialog'
import MemoryItemDialog from './MemoryItemDialog'
import MemoryNotFoundState from './MemoryNotFoundState'
import PromptRoundWorldbookItems from './PromptRoundWorldbookItems'
import PromptRoundTavernItems from './PromptRoundTavernItems'
import { summarizeTavernItems } from './tavernItems'
import { MemoryStatusChip } from './MemoryStatusChips'
import { memoryErrorMessage } from './errorMessage'
import { formatDateTime } from './format'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { useNotification } from '../../hooks/useNotification'
import { isNotFoundError } from '../../services/api/axios'
import {
  MEMORY_DETAIL_STATUSES,
  MEMORY_TYPES,
  memoryApi,
  type MemoryScopeParams,
  type MemoryType,
  type PromptRoundDetail,
  type PromptRoundListItem,
  type PromptRoundMemoryItem,
} from '../../services/api/memory'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'

/**
 * 提示词明细
 *
 * 每一轮对话结束后记一行：这一轮注入了哪些记忆、摘要用的是哪一版、好感是否出现。
 * 列表只放概要，点开一行才去取条目级明细 —— 明细带每个条目的**当前**状态，是排查
 * 「为什么这一轮没注入」的地方。
 *
 * 条目上的操作复用记忆页的隔离 / 删除对话框：两处各写一套动作会让「哪些状态下能做什么」
 * 慢慢分叉。
 */

interface PromptRoundsPanelProps {
  scope: MemoryScopeParams
  chatKey: string
  page?: number
  onPageChange?: (page: number) => void
}

const DEFAULT_PAGE_SIZE = 20

function asMemoryType(value: string): MemoryType | null {
  return (MEMORY_TYPES as readonly string[]).includes(value)
    ? (value as MemoryType)
    : null
}

function OutcomeChip({ outcome }: { outcome: string }) {
  const theme = useTheme()
  const { t } = useTranslation('memory')

  const color =
    outcome === 'ok'
      ? theme.palette.success.main
      : outcome === 'blocked'
        ? theme.palette.error.main
        : outcome === 'failed'
          ? theme.palette.warning.main
          : theme.palette.text.secondary

  return (
    <Chip
      size="small"
      label={t(`promptRounds.outcome.${outcome}`, { defaultValue: outcome })}
      sx={CHIP_VARIANTS.getCustomColorChip(color, true)}
    />
  )
}

/**
 * 明细里的条目状态
 *
 * 与记忆页共用同一套状态呈现，只把「未知」单独画成禁用色：明细里遇到不认识的取值，
 * 意思是这条记录连不上原表了，不该和「已删除」抢同一个灰色。
 */
function PromptRoundStatusChip({ status }: { status: string }) {
  const theme = useTheme()
  const { t } = useTranslation('memory')

  if (status === 'unknown' || !(MEMORY_DETAIL_STATUSES as readonly string[]).includes(status)) {
    return (
      <Chip
        size="small"
        label={t('status.unknown')}
        sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.text.disabled, true)}
      />
    )
  }

  return <MemoryStatusChip status={status} />
}

const SUMMARY_STATE_KEYS: Record<string, string> = {
  current: 'promptRounds.detail.summaryCurrent',
  outdated: 'promptRounds.detail.summaryOutdated',
  deleted: 'promptRounds.detail.summaryDeleted',
  none: 'promptRounds.detail.summaryNone',
}

function PromptRoundItems({
  detail,
  scope,
  onChanged,
}: {
  detail: PromptRoundDetail
  scope: MemoryScopeParams
  onChanged: () => void
}) {
  const notification = useNotification()
  const theme = useTheme()
  const { t } = useTranslation('memory')
  const { t: tCommon } = useTranslation('common')
  const queryClient = useQueryClient()

  const [itemTarget, setItemTarget] = useState<{
    memoryType: MemoryType
    id: number
  } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<{
    memoryType: MemoryType
    id: number
  } | null>(null)

  const refresh = () => {
    void queryClient.invalidateQueries({
      queryKey: ['memory', 'prompt-round', detail.id],
    })
    onChanged()
  }

  const quarantineMutation = useMutation({
    mutationFn: (item: { memoryType: MemoryType; id: number }) =>
      memoryApi.actOnItem(item.memoryType, item.id, 'quarantine'),
    onSuccess: () => {
      notification.success(t('promptRounds.detail.quarantineSuccess'))
      refresh()
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const rowActions = (item: PromptRoundMemoryItem) => {
    const memoryType = asMemoryType(item.type)
    if (!memoryType) return null

    const isParagraphOrEpisode = memoryType === 'paragraph' || memoryType === 'episode'
    const canQuarantine = isParagraphOrEpisode && item.status === 'active'
    const canDelete = isParagraphOrEpisode
      ? item.status === 'active' || item.status === 'quarantined'
      : item.status === 'active'

    if (!canQuarantine && !canDelete) {
      return (
        <Typography variant="caption" color="text.secondary">
          —
        </Typography>
      )
    }

    return (
      <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap' }}>
        {canQuarantine && (
          <ActionButton
            tone="ghost"
            size="small"
            disabled={quarantineMutation.isPending}
            onClick={() =>
              quarantineMutation.mutate({ memoryType, id: item.id })
            }
          >
            {t('actions.quarantine')}
          </ActionButton>
        )}
        {canDelete && (
          <ActionButton
            tone="danger"
            size="small"
            onClick={() => setDeleteTarget({ memoryType, id: item.id })}
          >
            {t('actions.delete')}
          </ActionButton>
        )}
      </Stack>
    )
  }

  const openItem = (item: PromptRoundMemoryItem) => {
    const memoryType = asMemoryType(item.type)
    if (!memoryType || (memoryType !== 'paragraph' && memoryType !== 'episode')) return
    setItemTarget({ memoryType, id: item.id })
  }

  /** 好感块这一轮注入的关系里程碑（段落编号）；空数组 = 那一轮没有里程碑 */
  const milestoneIds = detail.affection_milestone_ids ?? []

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, py: 1 }}>
      <Typography variant="body2" color="text.secondary">
        {t('promptRounds.detail.summaryLine', {
          cooldownSkipped: detail.cooldown_skipped,
          blockChars: detail.memory_block_chars,
        })}
        {' · '}
        {t(
          SUMMARY_STATE_KEYS[
            typeof detail.summary.state === 'string' ? detail.summary.state : 'none'
          ] ?? SUMMARY_STATE_KEYS.none
        )}
      </Typography>

      <Box sx={{ overflowX: 'auto', width: '100%' }}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('promptRounds.detail.type')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('promptRounds.detail.preview')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('promptRounds.detail.status')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('promptRounds.detail.chars')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('promptRounds.detail.renders')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('promptRounds.detail.actions')}
              </TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {detail.memory_items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6}>
                  <Typography variant="body2" color="text.secondary">
                    {t('promptRounds.detail.noItems')}
                  </Typography>
                </TableCell>
              </TableRow>
            ) : (
              detail.memory_items.map(item => {
                const clickable =
                  item.type === 'paragraph' || item.type === 'episode'
                return (
                  <TableRow key={`${item.type}:${item.id}`}>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Typography variant="body2">
                        {t(`kind.${item.type}`, { defaultValue: item.type })}
                      </Typography>
                    </TableCell>
                    <TableCell
                      sx={
                        [
                          UNIFIED_TABLE_STYLES.cell,
                          { maxWidth: 320 },
                        ] as SxProps<Theme>
                      }
                    >
                      <Typography
                        variant="body2"
                        color={clickable ? 'primary' : 'text.primary'}
                        onClick={clickable ? () => openItem(item) : undefined}
                        sx={{
                          cursor: clickable ? 'pointer' : 'default',
                          wordBreak: 'break-word',
                          '&:hover': clickable ? { textDecoration: 'underline' } : undefined,
                        }}
                      >
                        {item.preview || '—'}
                      </Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <PromptRoundStatusChip status={item.status} />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Typography variant="body2">{item.chars}</Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Typography variant="body2">{item.renders}</Typography>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      {rowActions(item)}
                    </TableCell>
                  </TableRow>
                )
              })
            )}
          </TableBody>
        </Table>
      </Box>

      <PromptRoundWorldbookItems detail={detail} />

      <PromptRoundTavernItems items={detail.tavern_items} />

      {/* 好感块这一轮注入的关系里程碑
          它们本身就是记忆段落，点开用的是与上面记忆条目同一个详情对话框：
          另做一套只读视图只会让「同一段记忆在两个地方显示两种状态」。
          没有里程碑时整块不渲染——摆一行「无」只是噪音。 */}
      {milestoneIds.length > 0 && (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
          <Typography variant="subtitle2" fontWeight={700}>
            {t('promptRounds.detail.affectionMilestones')}
          </Typography>
          <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 0.75 }}>
            {milestoneIds.map(id => (
              <Chip
                key={id}
                size="small"
                label={`#${id}`}
                onClick={() => setItemTarget({ memoryType: 'paragraph', id })}
                sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.primary.main, true)}
              />
            ))}
          </Stack>
        </Box>
      )}

      {itemTarget && (
        <MemoryItemDialog
          memoryType={itemTarget.memoryType}
          itemId={itemTarget.id}
          scope={scope}
          onClose={() => setItemTarget(null)}
          onChanged={refresh}
        />
      )}
      {deleteTarget && (
        <DeleteConfirmDialog
          open
          memoryType={deleteTarget.memoryType}
          itemId={deleteTarget.id}
          hard={false}
          scope={scope}
          onClose={() => setDeleteTarget(null)}
          onDeleted={() => {
            setDeleteTarget(null)
            refresh()
          }}
        />
      )}
    </Box>
  )
}

function PromptRoundExpanded({
  roundId,
  scope,
  onChanged,
}: {
  roundId: number
  scope: MemoryScopeParams
  onChanged: () => void
}) {
  const detailQuery = useQuery({
    queryKey: ['memory', 'prompt-round', roundId],
    queryFn: () => memoryApi.getPromptRound(roundId),
    retry: false,
  })

  if (detailQuery.isPending) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}>
        <CircularProgress size={22} />
      </Box>
    )
  }
  if (detailQuery.isError) {
    return isNotFoundError(detailQuery.error) ? (
      <MemoryNotFoundState compact />
    ) : (
      <QueryErrorState
        error={detailQuery.error}
        onRetry={() => void detailQuery.refetch()}
        compact
      />
    )
  }
  if (!detailQuery.data) return null

  return (
    <PromptRoundItems
      detail={detailQuery.data}
      scope={scope}
      onChanged={onChanged}
    />
  )
}

export default function PromptRoundsPanel({
  scope,
  chatKey,
  page: controlledPage,
  onPageChange,
}: PromptRoundsPanelProps) {
  const { t } = useTranslation('memory')
  const { t: tTavern } = useTranslation('tavern')
  const queryClient = useQueryClient()
  const { isScopeReady } = useInstanceScope()
  const [internalPage, setInternalPage] = useState(1)
  const [expandedId, setExpandedId] = useState<number | null>(null)

  const page = controlledPage ?? internalPage
  const setPage = onPageChange ?? setInternalPage

  // 换频道后旧的展开行不再属于这份列表，收起来
  useEffect(() => {
    setExpandedId(null)
  }, [chatKey])

  const listQuery = useQuery({
    queryKey: ['memory', 'prompt-rounds', scope, chatKey, page],
    queryFn: () =>
      memoryApi.listPromptRounds({
        chat_key: chatKey,
        page,
        page_size: DEFAULT_PAGE_SIZE,
        ...scope,
      }),
    enabled: isScopeReady && Boolean(chatKey),
    retry: false,
    placeholderData: keepPreviousData,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['memory'] })
    void listQuery.refetch()
  }

  const columns: ServerDataTableColumn<PromptRoundListItem>[] = [
    {
      key: 'time',
      header: t('promptRounds.columns.time'),
      render: row => (
        <Typography variant="body2">
          {formatDateTime(row.finished_at || row.started_at)}
        </Typography>
      ),
    },
    {
      key: 'outcome',
      header: t('promptRounds.columns.outcome'),
      render: row => <OutcomeChip outcome={row.outcome} />,
    },
    {
      key: 'memory',
      header: t('promptRounds.columns.memory'),
      render: row =>
        row.memory_state === 'injected' ? (
          <Typography variant="body2">
            {t('promptRounds.memoryState.injected', {
              count: row.memory_items_count,
              chars: row.memory_block_chars,
            })}
          </Typography>
        ) : (
          <Typography variant="body2" color="text.secondary">
            {t(`promptRounds.memoryState.${row.memory_state}`, {
              defaultValue: row.memory_state,
            })}
          </Typography>
        ),
    },
    {
      key: 'summary',
      header: t('promptRounds.columns.summary'),
      render: row =>
        row.summary_state === 'injected' ? (
          <Typography variant="body2">
            {t('promptRounds.summaryState.injected', {
              version: row.summary_version ?? '—',
              chars: row.summary_chars,
            })}
          </Typography>
        ) : (
          <Typography variant="body2" color="text.secondary">
            {t(`promptRounds.summaryState.${row.summary_state}`, {
              defaultValue: row.summary_state,
            })}
          </Typography>
        ),
    },
    {
      key: 'affection',
      header: t('promptRounds.columns.affection'),
      render: row =>
        row.affection_shown ? (
          <Typography variant="body2">
            {t('promptRounds.affectionShown', {
              people: row.affection_people,
              chars: row.affection_chars,
            })}
          </Typography>
        ) : (
          <Typography variant="body2" color="text.secondary">
            {t('promptRounds.affectionNone')}
          </Typography>
        ),
    },
    {
      key: 'worldbook',
      header: t('promptRounds.columns.worldbook'),
      render: row =>
        row.worldbook_state === 'injected' ? (
          <Typography variant="body2">
            {t('promptRounds.worldbookState.injected', {
              count: row.worldbook_count,
              chars: row.worldbook_chars,
            })}
          </Typography>
        ) : (
          <Typography variant="body2" color="text.secondary">
            {t(`promptRounds.worldbookState.${row.worldbook_state}`, {
              defaultValue: row.worldbook_state,
            })}
          </Typography>
        ),
    },
    {
      key: 'tavern',
      header: tTavern('rounds.columnTitle'),
      hideOnMobile: true,
      render: row => {
        const summary = summarizeTavernItems(row.tavern_items, tTavern)
        return (
          // 摘要可能拼得很长（三个槽位都有字），列里只留一行，全文放 title 里；
          // 这是纯文本，不渲染任何 HTML。
          <Typography
            variant="body2"
            title={summary}
            color={summary === tTavern('rounds.summary.none') ? 'text.secondary' : 'text.primary'}
            sx={{
              maxWidth: 280,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {summary}
          </Typography>
        )
      },
    },
    {
      key: 'renderCount',
      header: t('promptRounds.columns.renderCount'),
      hideOnMobile: true,
      render: row => <Typography variant="body2">{row.render_count}</Typography>,
    },
  ]

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0 }}>
      <ServerDataTable
        columns={columns}
        rows={listQuery.data?.items ?? []}
        getRowId={row => row.id}
        total={listQuery.data?.total ?? 0}
        page={page}
        pageSize={DEFAULT_PAGE_SIZE}
        onPageChange={next => {
          setPage(next)
          setExpandedId(null)
        }}
        loading={listQuery.isPending}
        fetching={listQuery.isFetching}
        error={listQuery.error}
        onRetry={() => void listQuery.refetch()}
        renderError={error =>
          isNotFoundError(error) ? (
            <MemoryNotFoundState compact />
          ) : (
            <QueryErrorState
              error={error}
              onRetry={() => void listQuery.refetch()}
              compact
            />
          )
        }
        emptyTitle={t('empty.promptRoundsTitle')}
        emptyHint={t('empty.promptRoundsHint')}
        onRowClick={row =>
          setExpandedId(current => (current === row.id ? null : row.id))
        }
        expandedRowId={expandedId}
        renderExpandedRow={row => (
          <PromptRoundExpanded
            roundId={row.id}
            scope={scope}
            onChanged={invalidate}
          />
        )}
      />
    </Box>
  )
}
