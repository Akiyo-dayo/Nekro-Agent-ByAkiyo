import { useEffect, useState, type ReactNode } from 'react'
import {
  Alert,
  Autocomplete,
  Box,
  CircularProgress,
  Divider,
  List,
  ListItem,
  ListItemText,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import AcUnitIcon from '@mui/icons-material/AcUnit'
import BlockIcon from '@mui/icons-material/Block'
import DeleteForeverIcon from '@mui/icons-material/DeleteForever'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import FavoriteRoundedIcon from '@mui/icons-material/FavoriteRounded'
import GppBadIcon from '@mui/icons-material/GppBad'
import GppGoodIcon from '@mui/icons-material/GppGood'
import HistoryIcon from '@mui/icons-material/History'
import RestoreIcon from '@mui/icons-material/Restore'
import TrendingDownIcon from '@mui/icons-material/TrendingDown'
import TrendingUpIcon from '@mui/icons-material/TrendingUp'
import ActionButton from '../common/ActionButton'
import IconActionButton from '../common/IconActionButton'
import NekroDialog from '../common/NekroDialog'
import QueryErrorState from '../common/QueryErrorState'
import { SimpleTooltip } from '../common/ThemedTooltip'
import { isNotFoundError } from '../../services/api/axios'
import { affectionApi } from '../../services/api/affection'
import {
  memoryApi,
  type MemoryItemAction,
  type MemoryScopeParams,
  type MemoryType,
} from '../../services/api/memory'
import { withDeepLinkInstance } from '../../hooks/useDeepLinkInstance'
import { useNotification } from '../../hooks/useNotification'
import DeleteConfirmDialog from './DeleteConfirmDialog'
import MemoryNotFoundState from './MemoryNotFoundState'
import {
  MemoryFlagChips,
  MemorySensitiveChip,
  MemoryStatusChip,
} from './MemoryStatusChips'
import { memoryErrorMessage } from './errorMessage'
import { formatDateTime, formatUnixSeconds } from './format'

/**
 * 单条记忆详情
 *
 * 放在 `components/memory` 而不是页面目录：频道详情的「记忆」标签也要打开同一个对话框，
 * 两处各写一份会让「哪个动作该在什么状态下出现」慢慢长成两套。
 *
 * 删除确认也复用同一个组件（`DeleteConfirmDialog`），彻底删除前的连带条数由它去取。
 *
 * 状态是 `deleted` / `pruned` 时只读，操作区只剩「彻底删除」：这两种状态下编辑或
 * 冻结都不会改变它是否被注入，摆出来只会让人以为还能救回来。
 */

interface MemoryItemDialogProps {
  memoryType: MemoryType
  itemId: number | null
  /** 彻底删除前预览连带条数用的作用域，见 `DeleteConfirmDialog` */
  scope: MemoryScopeParams
  onClose: () => void
  onChanged: () => void
}

type ConfirmMode = 'soft' | 'hard'

const READ_ONLY_STATUSES = ['deleted', 'pruned', 'hard_deleted']

const QUARANTINE_REASON_KEYS: Record<string, string> = {
  manual: 'quarantineReason.manual',
  auto_blocked: 'quarantineReason.auto_blocked',
  dedup: 'quarantineReason.dedup',
}

function InfoRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
      <Typography variant="body2" component="div" sx={{ wordBreak: 'break-word' }}>
        {children}
      </Typography>
    </>
  )
}

export default function MemoryItemDialog({
  memoryType,
  itemId,
  scope,
  onClose,
  onChanged,
}: MemoryItemDialogProps) {
  const notification = useNotification()
  const { t } = useTranslation('memory')
  const { t: tCommon } = useTranslation('common')
  const navigate = useNavigate()

  const isEpisode = memoryType === 'episode'

  const [content, setContent] = useState('')
  const [summary, setSummary] = useState('')
  const [personAccounts, setPersonAccounts] = useState<string[]>([])
  const [title, setTitle] = useState('')
  const [narrativeSummary, setNarrativeSummary] = useState('')
  const [confirmMode, setConfirmMode] = useState<ConfirmMode | null>(null)
  const [traceOpen, setTraceOpen] = useState(false)

  const detailQuery = useQuery({
    queryKey: ['memory', 'item', memoryType, itemId],
    queryFn: async () => {
      if (itemId === null) throw new Error('memory item id is missing')
      return memoryApi.getItem(memoryType, itemId)
    },
    enabled: itemId !== null,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const detail = detailQuery.data

  useEffect(() => {
    if (!detail) return
    setContent(detail.content ?? '')
    setSummary(detail.summary ?? '')
    setPersonAccounts((detail.persons ?? []).map(person => person.account))
    setTitle(detail.title ?? '')
    setNarrativeSummary(detail.narrative_summary ?? '')
  }, [detail])

  useEffect(() => {
    setConfirmMode(null)
    setTraceOpen(false)
  }, [itemId, memoryType])

  const traceQuery = useQuery({
    queryKey: ['memory', 'trace', itemId],
    queryFn: async () => {
      if (itemId === null) throw new Error('memory item id is missing')
      return memoryApi.traceParagraph(itemId)
    },
    enabled: traceOpen && !isEpisode && itemId !== null,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (itemId === null) throw new Error('memory item id is missing')
      if (isEpisode) {
        return memoryApi.updateItem(memoryType, itemId, {
          title,
          narrative_summary: narrativeSummary,
        })
      }
      return memoryApi.updateItem(memoryType, itemId, {
        content,
        summary,
        person_accounts: personAccounts,
      })
    },
    onSuccess: () => {
      notification.success(t('actions.saveSuccess'))
      onChanged()
      void detailQuery.refetch()
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const actionMutation = useMutation({
    mutationFn: async (action: MemoryItemAction) => {
      if (itemId === null) throw new Error('memory item id is missing')
      return memoryApi.actOnItem(memoryType, itemId, action)
    },
    onSuccess: (_data, action) => {
      notification.success(t(`actions.${action}Success`))
      onChanged()
      void detailQuery.refetch()
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const basePersons = (detail?.persons ?? []).map(person => person.account)
  const isMilestone = detail?.origin_kind === 'affection'

  /**
   * 从记忆条目跳回它所属的好感详情
   *
   * 服务端只认段落编号，由它给出属于这份好感的记录编号 —— 前端不猜：
   * 里程碑与好感记录是两张表，猜一个 id 可能正好是别人的号。
   * 找不到就是「看不到或已不存在」，不做第二种解释（越权在服务端本来就是 404）。
   *
   * 链接带上这一段所属的实例（`useDeepLinkInstance`）：作用域是顶栏切换器的本地状态，
   * 不带的话链接一旦被复制到别处、或在别的实例视图下打开，就会显示「看不到」。
   */
  const openRecordMutation = useMutation({
    mutationFn: async () => {
      if (itemId === null) throw new Error('memory item id is missing')
      return affectionApi.recordOfMilestone(itemId, scope.instance_key)
    },
    onSuccess: data => {
      onClose()
      navigate(withDeepLinkInstance(`/affection?record=${data.record_id}`, detail?.instance_key))
    },
    onError: (error: unknown) =>
      notification.error(
        isNotFoundError(error)
          ? t('detail.milestoneRecordNotFound')
          : memoryErrorMessage(error, {
              notFound: t('detail.milestoneRecordNotFound'),
              fallback: tCommon('messages.operationFailed'),
            })
      ),
  })

  const personsDirty =
    personAccounts.length !== basePersons.length ||
    personAccounts.some((account, index) => account !== basePersons[index])
  const isDirty = detail
    ? isEpisode
      ? title !== (detail.title ?? '') || narrativeSummary !== (detail.narrative_summary ?? '')
      : content !== (detail.content ?? '') || summary !== (detail.summary ?? '') || personsDirty
    : false

  const isReadOnly = detail ? READ_ONLY_STATUSES.includes(detail.status) : false
  const isActive = detail?.status === 'active'
  const isQuarantined = detail?.status === 'quarantined'
  const isFrozen = Boolean(detail?.is_frozen)
  const isProtected = Boolean(detail?.is_protected)

  const quarantineReason = detail?.quarantine_reason
    ? t(QUARANTINE_REASON_KEYS[detail.quarantine_reason] ?? 'quarantineReason.unknown')
    : '—'

  const partitionLabel =
    detail?.workspace_id === undefined
      ? '—'
      : detail.workspace_id === 0
        ? t('partition.domain')
        : t('partition.workspaceWithId', { id: detail.workspace_id })

  const instanceLabel = detail?.instance_key || detail?.adapter_key || '—'

  const cognitiveLabel = detail?.cognitive_type
    ? t(`cognitive.${detail.cognitive_type}`, { defaultValue: detail.cognitive_type })
    : '—'
  const knowledgeLabel = detail?.knowledge_type
    ? t(`knowledge.${detail.knowledge_type}`, { defaultValue: detail.knowledge_type })
    : '—'

  const renderTrace = () => {
    if (traceQuery.isPending) {
      return (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}>
          <CircularProgress size={20} />
        </Box>
      )
    }
    if (traceQuery.isError) {
      return isNotFoundError(traceQuery.error) ? (
        <MemoryNotFoundState compact />
      ) : (
        <QueryErrorState
          error={traceQuery.error}
          onRetry={() => void traceQuery.refetch()}
          compact
        />
      )
    }
    const trace = traceQuery.data
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        {trace?.cross_channel && (
          <Alert severity="info" sx={{ py: 0 }}>
            {t('trace.crossChannel')}
          </Alert>
        )}
        {!trace || trace.messages.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            {t('trace.empty')}
          </Typography>
        ) : (
          <List dense disablePadding>
            {trace.messages.map(message => (
              <ListItem key={message.db_id} disableGutters divider>
                <ListItemText
                  primary={message.content_preview}
                  secondary={`${message.sender_name || '—'} · ${formatUnixSeconds(message.send_timestamp)}`}
                />
              </ListItem>
            ))}
          </List>
        )}
      </Box>
    )
  }

  const renderParagraphBody = () => {
    if (!detail) return null
    return (
      <>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <TextField
            label={t('detail.content')}
            value={content}
            onChange={event => setContent(event.target.value)}
            multiline
            minRows={4}
            fullWidth
            size="small"
            disabled={isReadOnly}
          />
          <TextField
            label={t('detail.summary')}
            value={summary}
            onChange={event => setSummary(event.target.value)}
            fullWidth
            size="small"
            disabled={isReadOnly}
          />
          <Autocomplete
            multiple
            freeSolo
            size="small"
            options={basePersons}
            value={personAccounts}
            disabled={isReadOnly}
            onChange={(_event, next) => setPersonAccounts(next)}
            getOptionLabel={option => {
              const person = detail.persons?.find(item => item.account === option)
              return person?.display_name || option
            }}
            renderInput={params => (
              <TextField
                {...params}
                label={t('detail.persons')}
                helperText={t('detail.personsHelper')}
                size="small"
              />
            )}
          />
        </Box>
        <Divider />
        <Box
          sx={{
            display: 'grid',
            gridTemplateColumns: { xs: '1fr', sm: '160px 1fr' },
            columnGap: 2,
            rowGap: 0.75,
            alignItems: 'baseline',
          }}
        >
          <InfoRow label={t('detail.channel')}>
            {detail.origin_channel_name || detail.origin_chat_key || '—'}
          </InfoRow>
          <InfoRow label={t('detail.episode')}>
            {!detail.episode
              ? '—'
              : detail.episode.visible === false
                ? t('groups.hiddenEpisode', { id: detail.episode.id })
                : detail.episode.title || t('groups.untitledEpisode', { id: detail.episode.id })}
          </InfoRow>
          <InfoRow label={t('detail.cognitive')}>{cognitiveLabel}</InfoRow>
          <InfoRow label={t('detail.knowledge')}>{knowledgeLabel}</InfoRow>
          <InfoRow label={t('detail.baseWeight')}>
            {detail.base_weight === undefined ? '—' : String(detail.base_weight)}
          </InfoRow>
          <InfoRow label={t('detail.reinforceCount')}>
            {detail.reinforcement_count === undefined ? '—' : String(detail.reinforcement_count)}
          </InfoRow>
          <InfoRow label={t('detail.blockedHits')}>
            {detail.blocked_round_hits === undefined ? '—' : String(detail.blocked_round_hits)}
          </InfoRow>
          <InfoRow label={t('detail.quarantineReason')}>{quarantineReason}</InfoRow>
          <InfoRow label={t('detail.quarantinedAt')}>
            {formatDateTime(detail.quarantined_at)}
          </InfoRow>
          <InfoRow label={t('detail.partition')}>{partitionLabel}</InfoRow>
          <InfoRow label={t('detail.instance')}>{instanceLabel}</InfoRow>
        </Box>
        {/* 里程碑软删之后这条记忆是只读的，但「跳回好感页」仍要给 ——
            找回关系里程碑的入口就在这里，藏在只读判断后面等于找不回来。 */}
        {(isMilestone || !isReadOnly) && (
          <>
            <Divider />
            <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
              {isMilestone && (
                <ActionButton
                  tone="ghost"
                  startIcon={<FavoriteRoundedIcon />}
                  disabled={openRecordMutation.isPending}
                  onClick={() => openRecordMutation.mutate()}
                >
                  {t('detail.openAffection')}
                </ActionButton>
              )}
              {!isReadOnly && (
                <ActionButton
                  tone="ghost"
                  startIcon={<HistoryIcon />}
                  onClick={() => setTraceOpen(previous => !previous)}
                >
                  {t('trace.button')}
                </ActionButton>
              )}
            </Box>
            {!isReadOnly && traceOpen && <Box sx={{ mt: 1 }}>{renderTrace()}</Box>}
          </>
        )}
      </>
    )
  }

  const renderEpisodeBody = () => {
    if (!detail) return null
    return (
      <>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <TextField
            label={t('detail.titleField')}
            value={title}
            onChange={event => setTitle(event.target.value)}
            fullWidth
            size="small"
            disabled={isReadOnly}
          />
          <TextField
            label={t('detail.narrativeSummary')}
            value={narrativeSummary}
            onChange={event => setNarrativeSummary(event.target.value)}
            multiline
            minRows={4}
            fullWidth
            size="small"
            disabled={isReadOnly}
          />
        </Box>
        <Divider />
        <Box
          sx={{
            display: 'grid',
            gridTemplateColumns: { xs: '1fr', sm: '160px 1fr' },
            columnGap: 2,
            rowGap: 0.75,
            alignItems: 'baseline',
          }}
        >
          <InfoRow label={t('detail.timeRange')}>
            {formatDateTime(detail.time_start)} – {formatDateTime(detail.time_end)}
          </InfoRow>
          <InfoRow label={t('detail.channel')}>
            {detail.origin_chat_key || '—'}
          </InfoRow>
          <InfoRow label={t('detail.blockedHits')}>
            {detail.blocked_round_hits === undefined ? '—' : String(detail.blocked_round_hits)}
          </InfoRow>
          <InfoRow label={t('detail.quarantineReason')}>{quarantineReason}</InfoRow>
          <InfoRow label={t('detail.quarantinedAt')}>
            {formatDateTime(detail.quarantined_at)}
          </InfoRow>
          <InfoRow label={t('detail.partition')}>{partitionLabel}</InfoRow>
          <InfoRow label={t('detail.instance')}>{instanceLabel}</InfoRow>
        </Box>
      </>
    )
  }

  const renderActions = () => {
    if (!detail) return undefined

    if (isReadOnly) {
      return (
        <Box
          sx={{ display: 'flex', width: '100%', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}
        >
          <ActionButton
            tone="danger"
            startIcon={<DeleteForeverIcon />}
            onClick={() => setConfirmMode('hard')}
          >
            {t('actions.hardDelete')}
          </ActionButton>
          <Box sx={{ flex: 1 }} />
          <ActionButton tone="ghost" onClick={onClose}>
            {tCommon('actions.close')}
          </ActionButton>
        </Box>
      )
    }

    return (
      <Box sx={{ display: 'flex', width: '100%', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexWrap: 'wrap' }}>
          {!isEpisode && (
            <>
              <SimpleTooltip title={t(isFrozen ? 'actions.unfreeze' : 'actions.freeze')}>
                <IconActionButton
                  size="small"
                  disabled={actionMutation.isPending}
                  onClick={() => actionMutation.mutate(isFrozen ? 'unfreeze' : 'freeze')}
                  aria-label={t(isFrozen ? 'actions.unfreeze' : 'actions.freeze')}
                >
                  {isFrozen ? <RestoreIcon fontSize="small" /> : <AcUnitIcon fontSize="small" />}
                </IconActionButton>
              </SimpleTooltip>
              <SimpleTooltip title={t(isProtected ? 'actions.unprotect' : 'actions.protect')}>
                <IconActionButton
                  size="small"
                  disabled={actionMutation.isPending}
                  onClick={() => actionMutation.mutate(isProtected ? 'unprotect' : 'protect')}
                  aria-label={t(isProtected ? 'actions.unprotect' : 'actions.protect')}
                >
                  {isProtected ? (
                    <GppBadIcon fontSize="small" />
                  ) : (
                    <GppGoodIcon fontSize="small" />
                  )}
                </IconActionButton>
              </SimpleTooltip>
              <SimpleTooltip title={t('actions.reinforce')}>
                <IconActionButton
                  size="small"
                  disabled={actionMutation.isPending}
                  onClick={() => actionMutation.mutate('reinforce')}
                  aria-label={t('actions.reinforce')}
                >
                  <TrendingUpIcon fontSize="small" />
                </IconActionButton>
              </SimpleTooltip>
              <SimpleTooltip title={t('actions.demote')}>
                <IconActionButton
                  size="small"
                  disabled={actionMutation.isPending}
                  onClick={() => actionMutation.mutate('demote')}
                  aria-label={t('actions.demote')}
                >
                  <TrendingDownIcon fontSize="small" />
                </IconActionButton>
              </SimpleTooltip>
            </>
          )}
          {isActive && (
            <ActionButton
              tone="ghost"
              size="small"
              startIcon={<BlockIcon />}
              disabled={actionMutation.isPending}
              onClick={() => actionMutation.mutate('quarantine')}
            >
              {t('actions.quarantine')}
            </ActionButton>
          )}
          {isQuarantined && (
            <ActionButton
              tone="ghost"
              size="small"
              startIcon={<RestoreIcon />}
              disabled={actionMutation.isPending}
              onClick={() => actionMutation.mutate('restore')}
            >
              {t('actions.restore')}
            </ActionButton>
          )}
          {(isActive || isQuarantined) && (
            <ActionButton
              tone="ghost"
              size="small"
              startIcon={<DeleteOutlineIcon />}
              onClick={() => setConfirmMode('soft')}
            >
              {t('actions.delete')}
            </ActionButton>
          )}
          <ActionButton
            tone="ghost"
            size="small"
            startIcon={<DeleteForeverIcon />}
            onClick={() => setConfirmMode('hard')}
          >
            {t('actions.hardDelete')}
          </ActionButton>
        </Box>

        <Box sx={{ flex: 1 }} />
        <ActionButton tone="ghost" onClick={onClose} disabled={saveMutation.isPending}>
          {tCommon('actions.cancel')}
        </ActionButton>
        <ActionButton
          tone="primary"
          disabled={!isDirty || saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {t('actions.save')}
        </ActionButton>
      </Box>
    )
  }

  const renderContent = () => {
    if (detailQuery.isPending) {
      return (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={24} />
        </Box>
      )
    }
    if (isNotFoundError(detailQuery.error)) {
      return <MemoryNotFoundState />
    }
    if (detailQuery.isError) {
      return (
        <QueryErrorState
          error={detailQuery.error}
          onRetry={() => void detailQuery.refetch()}
        />
      )
    }
    if (!detail) {
      return <MemoryNotFoundState />
    }

    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ flexWrap: 'wrap' }}>
          <MemoryStatusChip status={detail.status} />
          <MemoryFlagChips isFrozen={detail.is_frozen} isProtected={detail.is_protected} />
          {detail.is_sensitive && <MemorySensitiveChip />}
        </Stack>
        {isEpisode ? renderEpisodeBody() : renderParagraphBody()}
      </Box>
    )
  }

  return (
    <NekroDialog
      open={itemId !== null}
      onClose={onClose}
      title={isEpisode ? t('detail.episodeTitle') : t('detail.title')}
      maxWidth="md"
      actions={renderActions()}
    >
      {renderContent()}
      {confirmMode && itemId !== null && (
        <DeleteConfirmDialog
          open
          memoryType={memoryType}
          itemId={itemId}
          hard={confirmMode === 'hard'}
          scope={scope}
          onClose={() => setConfirmMode(null)}
          onDeleted={() => {
            setConfirmMode(null)
            onChanged()
            onClose()
          }}
        />
      )}
    </NekroDialog>
  )
}
