import { useEffect, useRef, useState } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  CircularProgress,
  Divider,
  FormControlLabel,
  Stack,
  Switch,
  TextField,
  Typography,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import NekroDialog from '../common/NekroDialog'
import QueryErrorState from '../common/QueryErrorState'
import { SimpleTooltip } from '../common/ThemedTooltip'
import { useNotification } from '../../hooks/useNotification'
import { ApiError, isNotFoundError } from '../../services/api/axios'
import {
  memoryApi,
  type MemoryScopeParams,
  type SummaryVersionItem,
} from '../../services/api/memory'
import MemoryNotFoundState from './MemoryNotFoundState'
import SummaryDiffView from './SummaryDiffView'
import SummarySourceChip from './SummarySourceChip'
import { memoryErrorMessage } from './errorMessage'
import { formatDateTime, formatShortDateTime, truncateText } from './format'
import { channelNameOf, presetNameOf } from './scopeLabels'
import { useMemoryScopeOptions } from './useMemoryScopeOptions'

/**
 * 频道摘要详情
 *
 * 摘要是一个「频道 + 人设」的版本序列。这里只做三件事：暂停自动更新、把编辑存成
 * 新的一版、把历史里的某一版重新写成新的一版。三者都不覆盖旧数据，所以回滚不需要
 * 额外的确认 —— 回滚本身也是一次可再回滚的写入。
 *
 * 编辑的并发保护用 `base_version`：提交时带打开这一版时的版本号，服务端发现已经
 * 不是最新版会回 409。这种情况不能悄悄丢掉运营写的内容，所以把被替换掉的原文放进
 * 一个可复制的折叠块里。
 *
 * 打开时只摊开最新一版：几版并排铺开看不出改了什么。「和上一版对比」逐句标出增删；
 * 旧版本收进默认合上的历史抽屉，回滚也在那里。每一版用生成时间称呼，版号是次要信息。
 */

interface SummaryDialogProps {
  chatKey: string
  presetId: number
  /** 用来把 chat_key / preset_id 翻成显示名；选项本身也按这个作用域裁剪 */
  scope: MemoryScopeParams
  onClose: () => void
  onChanged: () => void
}

function isConflictError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

function SummaryVersionRow({
  version,
  isLatest,
  rollbackPending,
  onRollback,
}: {
  version: SummaryVersionItem
  isLatest: boolean
  rollbackPending: boolean
  onRollback: () => void
}) {
  const { t } = useTranslation('memory')
  const [expanded, setExpanded] = useState(false)
  const needsToggle = version.content.length > 180

  return (
    <Box
      sx={{
        border: 1,
        borderColor: 'divider',
        borderRadius: 1,
        p: 1.25,
        display: 'flex',
        flexDirection: 'column',
        gap: 0.75,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        {/* 用生成时间称呼一版；版号只作次要信息（回滚说的「回滚自第 N 版」要对得上它） */}
        <Typography variant="body2" fontWeight={700}>
          {t('summaries.generatedAt', { time: formatShortDateTime(version.create_time) })}
        </Typography>
        <SummarySourceChip source={version.source} />
        <Typography variant="caption" color="text.secondary">
          {t('summaries.dialog.version', { version: version.version })}
        </Typography>
        {version.source === 'rollback' && version.source_version !== null && (
          <Typography variant="caption" color="text.secondary">
            {t('summaries.dialog.rollbackFrom', { version: version.source_version })}
          </Typography>
        )}
        {version.edited_by && (
          <Typography variant="caption" color="text.secondary">
            {t('summaries.dialog.editedBy', { user: version.edited_by })}
          </Typography>
        )}
        <Box sx={{ flex: 1 }} />
        {!isLatest && (
          <ActionButton
            tone="ghost"
            size="small"
            disabled={rollbackPending}
            onClick={onRollback}
          >
            {t('summaries.dialog.rollbackToVersion')}
          </ActionButton>
        )}
      </Box>
      <Box>
        <Typography
          variant="body2"
          sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}
        >
          {expanded ? version.content : truncateText(version.content, 180) || t('summaries.dialog.untitled')}
        </Typography>
        {needsToggle && (
          <ActionButton tone="ghost" size="small" onClick={() => setExpanded(value => !value)}>
            {expanded ? t('summaries.dialog.collapse') : t('summaries.dialog.expand')}
          </ActionButton>
        )}
      </Box>
    </Box>
  )
}

export default function SummaryDialog({
  chatKey,
  presetId,
  scope,
  onClose,
  onChanged,
}: SummaryDialogProps) {
  const notification = useNotification()
  const { t } = useTranslation('memory')
  const { t: tCommon } = useTranslation('common')
  const { data: options } = useMemoryScopeOptions(scope)

  const [content, setContent] = useState('')
  const [baseVersion, setBaseVersion] = useState<number | null>(null)
  const [discarded, setDiscarded] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const initializedRef = useRef(false)

  const versionsQuery = useQuery({
    queryKey: ['memory', 'summary-versions', chatKey, presetId],
    queryFn: () => memoryApi.getSummaryVersions(chatKey, presetId),
    retry: false,
  })

  const versions = versionsQuery.data?.versions ?? []
  const latest = versions[0] ?? null
  const previous = versions[1] ?? null
  const older = versions.slice(1)
  const paused = versionsQuery.data?.paused ?? false
  const [showDiff, setShowDiff] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)

  // 只在打开时载入一次：暂停开关会让这个查询重新取数，那不该把正在编辑的草稿冲掉
  useEffect(() => {
    if (initializedRef.current || !versionsQuery.data) return
    initializedRef.current = true
    const first = versionsQuery.data.versions[0]
    setContent(first?.content ?? '')
    setBaseVersion(first?.version ?? null)
  }, [versionsQuery.data])

  const showError = (error: unknown) =>
    notification.error(
      memoryErrorMessage(error, {
        notFound: t('errors.notFoundHint'),
        fallback: tCommon('messages.operationFailed'),
      })
    )

  const pauseMutation = useMutation({
    mutationFn: (nextPaused: boolean) => memoryApi.pauseSummary(chatKey, presetId, nextPaused),
    onSuccess: result => {
      notification.success(
        result.paused ? t('summaries.dialog.paused') : t('summaries.dialog.resumed')
      )
      void versionsQuery.refetch()
      onChanged()
    },
    onError: showError,
  })

  const saveMutation = useMutation({
    mutationFn: () =>
      memoryApi.editSummary(chatKey, presetId, {
        content,
        base_version: baseVersion ?? 0,
      }),
    onSuccess: version => {
      notification.success(t('summaries.dialog.saveSuccess'))
      setContent(version.content)
      setBaseVersion(version.version)
      setDiscarded(null)
      setConflict(false)
      void versionsQuery.refetch()
      onChanged()
    },
    onError: error => {
      if (!isConflictError(error)) {
        showError(error)
        return
      }
      // 409 是「别人刚存过一版」。先把本地这段留住，再把编辑框换成最新版
      notification.warning(t('summaries.dialog.conflict'))
      setDiscarded(content)
      setConflict(true)
      // 别人的新版本已经落库，列表上的「最新版 / 版本数」也得跟着变
      onChanged()
      void versionsQuery.refetch().then(result => {
        const first = result.data?.versions[0]
        if (!first) return
        setContent(first.content)
        setBaseVersion(first.version)
      })
    },
  })

  const rollbackMutation = useMutation({
    mutationFn: (toVersion?: number) =>
      memoryApi.rollbackSummary(chatKey, presetId, toVersion),
    onSuccess: version => {
      notification.success(
        t('summaries.dialog.rollbackSuccess', { version: version.version })
      )
      setContent(version.content)
      setBaseVersion(version.version)
      setDiscarded(null)
      setConflict(false)
      void versionsQuery.refetch()
      onChanged()
    },
    onError: showError,
  })

  const renderBody = () => {
    if (versionsQuery.isPending) {
      return (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={28} />
        </Box>
      )
    }
    if (versionsQuery.isError) {
      return isNotFoundError(versionsQuery.error) ? (
        <MemoryNotFoundState compact />
      ) : (
        <QueryErrorState
          error={versionsQuery.error}
          onRetry={() => void versionsQuery.refetch()}
          compact
        />
      )
    }
    if (!latest) {
      return (
        <Typography variant="body2" color="text.secondary">
          {t('summaries.dialog.emptyVersions')}
        </Typography>
      )
    }

    return (
      <Stack spacing={2} sx={{ pt: 1 }}>
        <Box
          sx={{
            display: 'grid',
            gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' },
            gap: 1,
          }}
        >
          <Box>
            <Typography variant="caption" color="text.secondary">
              {t('summaries.columns.channel')}
            </Typography>
            <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>
              {channelNameOf(options, chatKey)}
            </Typography>
          </Box>
          <Box>
            <Typography variant="caption" color="text.secondary">
              {t('summaries.columns.preset')}
            </Typography>
            <Typography variant="body2">
              {presetNameOf(options, presetId, t('preset.none'), t('preset.consoleOnly'))}
            </Typography>
          </Box>
        </Box>

        <Box>
          <FormControlLabel
            control={
              <Switch
                checked={paused}
                disabled={pauseMutation.isPending}
                onChange={event => pauseMutation.mutate(event.target.checked)}
              />
            }
            label={
              <Stack direction="row" spacing={0.5} alignItems="center">
                <Typography variant="body2">{t('summaries.dialog.pauseLabel')}</Typography>
                <SimpleTooltip title={t('summaries.dialog.pauseTooltip')}>
                  <span>
                    <InfoOutlinedIcon fontSize="small" color="action" />
                  </span>
                </SimpleTooltip>
              </Stack>
            }
          />
          {paused && versionsQuery.data?.paused_by && versionsQuery.data.paused_at && (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ display: 'block', ml: 6.25 }}
            >
              {t('summaries.dialog.pausedBy', {
                user: versionsQuery.data.paused_by,
                time: formatDateTime(versionsQuery.data.paused_at),
              })}
            </Typography>
          )}
        </Box>

        <Divider />

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
          <Typography variant="subtitle2" fontWeight={700}>
            {t('summaries.dialog.latestTitle')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('summaries.generatedAt', { time: formatShortDateTime(latest.create_time) })}
          </Typography>
          <SummarySourceChip source={latest.source} />
          <Box sx={{ flex: 1 }} />
          {previous && (
            <ActionButton tone="ghost" size="small" onClick={() => setShowDiff(value => !value)}>
              {showDiff ? t('summaries.dialog.hideDiff') : t('summaries.dialog.compareWithPrevious')}
            </ActionButton>
          )}
        </Box>
        {showDiff && previous && (
          <Box>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
              {t('summaries.dialog.diffCaption', {
                previous: formatShortDateTime(previous.create_time),
                latest: formatShortDateTime(latest.create_time),
              })}
            </Typography>
            <SummaryDiffView before={previous.content} after={latest.content} />
          </Box>
        )}

        {conflict && (
          <Alert severity="info">{t('summaries.dialog.conflictReplaced')}</Alert>
        )}
        {discarded !== null && (
          <Accordion disableGutters>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="caption">
                {t('summaries.dialog.discardedLabel')}
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <TextField
                fullWidth
                multiline
                minRows={4}
                value={discarded}
                InputProps={{ readOnly: true }}
              />
            </AccordionDetails>
          </Accordion>
        )}

        <Box>
          <TextField
            fullWidth
            multiline
            minRows={6}
            label={t('summaries.dialog.contentLabel')}
            value={content}
            onChange={event => setContent(event.target.value)}
          />
          <Typography variant="caption" color="text.secondary">
            {t('summaries.dialog.charCount', { count: content.length })}
          </Typography>
        </Box>
        <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
          <ActionButton
            tone="primary"
            disabled={
              saveMutation.isPending ||
              baseVersion === null ||
              content === latest.content
            }
            onClick={() => saveMutation.mutate()}
          >
            {t('summaries.dialog.save')}
          </ActionButton>
        </Box>

        {/* 旧版本收进历史抽屉，默认收着：打开详情先看到的只有最新一版 */}
        {older.length > 0 && (
          <Accordion
            disableGutters
            expanded={historyOpen}
            onChange={(_event, expanded) => setHistoryOpen(expanded)}
            sx={{ '&:before': { display: 'none' }, border: 1, borderColor: 'divider', borderRadius: 1 }}
          >
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="subtitle2" fontWeight={700}>
                {t('summaries.dialog.historyDrawer', { count: older.length })}
              </Typography>
            </AccordionSummary>
            <AccordionDetails>
              <Stack spacing={1}>
                <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <ActionButton
                    tone="ghost"
                    size="small"
                    disabled={rollbackMutation.isPending}
                    onClick={() => rollbackMutation.mutate(undefined)}
                  >
                    {t('summaries.dialog.rollbackPrevious')}
                  </ActionButton>
                </Box>
                {older.map(version => (
                  <SummaryVersionRow
                    key={version.id}
                    version={version}
                    isLatest={false}
                    rollbackPending={rollbackMutation.isPending}
                    onRollback={() => rollbackMutation.mutate(version.version)}
                  />
                ))}
              </Stack>
            </AccordionDetails>
          </Accordion>
        )}
      </Stack>
    )
  }

  return (
    <NekroDialog
      open
      onClose={onClose}
      title={t('summaries.dialog.title')}
      maxWidth="md"
      actions={
        <ActionButton tone="ghost" onClick={onClose}>
          {tCommon('actions.close')}
        </ActionButton>
      }
    >
      {renderBody()}
    </NekroDialog>
  )
}
