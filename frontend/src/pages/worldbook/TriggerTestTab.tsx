import { useState } from 'react'
import {
  Alert,
  Box,
  Chip,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
  useTheme,
  type SxProps,
  type Theme,
} from '@mui/material'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import QueryErrorState from '../../components/common/QueryErrorState'
import { WorldbookScopeChip, WorldbookTrustChip } from '../../components/worldbook/chips'
import BudgetExplain from '../../components/worldbook/BudgetExplain'
import {
  worldbookDropReasonText,
  worldbookPositionText,
} from '../../components/worldbook/display'
import { useWorldbookScopeOptions } from '../../components/worldbook/useWorldbookScopeOptions'
import WorldbookNotFoundState from '../../components/worldbook/WorldbookNotFoundState'
import { isNotFoundError } from '../../services/api/axios'
import {
  WORLDBOOK_TRIGGER_TEXT_MAX_LENGTH,
  worldbookApi,
  type WorldbookScopeParams,
  type WorldbookTriggerHitItem,
  type WorldbookTriggerTestResponse,
} from '../../services/api/worldbook'
import { CHIP_VARIANTS, SCROLL_COLUMN_STYLES, UNIFIED_TABLE_STYLES } from '../../theme/variants'

/**
 * 「触发测试」标签
 *
 * 给管理员排查关键词用：不调模型、不写明细、不推进 sticky / cooldown。表单状态是
 * 本地 state，不进 URL —— 它是一次性试验，不是刷新后应当恢复的筛选。
 */

// 两张表的最小宽度：窄屏下放不下时横向滚动，而不是把每列挤成一字一行
const BOOKS_TABLE_MIN_WIDTH = 480
const HITS_TABLE_MIN_WIDTH = 760
// 「前面占掉预算的」最多列几个标题，多的写「等 N 条」
const AHEAD_SHOWN = 5

interface TriggerTestTabProps {
  scope: WorldbookScopeParams
  isScopeReady: boolean
}

export default function TriggerTestTab({ scope, isScopeReady }: TriggerTestTabProps) {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')
  const { data: options } = useWorldbookScopeOptions(scope)

  const [chatKey, setChatKey] = useState('')
  const [presetId, setPresetId] = useState('')
  const [text, setText] = useState('')

  const mutation = useMutation({
    mutationFn: () =>
      worldbookApi.testTrigger({
        chat_key: chatKey,
        text: text.trim() || undefined,
        use_recent: !text.trim(),
        preset_id: presetId ? Number(presetId) : undefined,
      }),
  })

  const channelOptions = [
    { value: '', label: t('test.channelPlaceholder') },
    ...(options?.channels ?? []).map(channel => ({
      value: channel.chat_key,
      label: channel.channel_name || channel.chat_key,
    })),
  ]

  const presetOptions = [
    { value: '', label: t('test.presetCurrent') },
    ...(options?.presets ?? [])
      .filter(preset => preset.preset_id > 0)
      .map(preset => ({ value: String(preset.preset_id), label: preset.name })),
  ]

  const result = mutation.data
  const error = mutation.error

  const depthOf = (data: WorldbookTriggerTestResponse, entryId: number): string | null => {
    for (const [depth, ids] of Object.entries(data.groups.at_depth)) {
      if (ids.includes(entryId)) return depth
    }
    return null
  }

  const renderPosition = (data: WorldbookTriggerTestResponse, hit: WorldbookTriggerHitItem) => {
    if (hit.position !== 'at_depth') {
      return worldbookPositionText(hit.position, 0, t)
    }
    const depth = depthOf(data, hit.entry_id)
    return depth
      ? t('positions.atDepth', { depth })
      : t('entryDialog.positionOptions.at_depth')
  }

  /** 因预算被跳过时，前面入选、占掉预算的条目标题；标题从同一次响应的命中列表里找 */
  const renderAhead = (data: WorldbookTriggerTestResponse, ahead: number[]) => {
    if (ahead.length === 0) return null
    const shown = ahead.slice(0, AHEAD_SHOWN)
    const titles = shown
      // 没有标题就写编号，与本表标题列、书详情里的写法一致
      .map(entryId => data.hits.find(hit => hit.entry_id === entryId)?.comment || `#${entryId}`)
      .join('、')
    // 「等 N 条」的 N 是总数，不是没列出的条数
    return (
      <Typography variant="caption" color="text.secondary" display="block">
        {ahead.length > shown.length
          ? t('test.aheadMore', { titles, count: ahead.length })
          : t('test.ahead', { titles })}
      </Typography>
    )
  }

  const renderBlock = (title: string, body: string) => (
    <Box>
      <Typography variant="caption" fontWeight={600} display="block" sx={{ mb: 0.5 }}>
        {title}
      </Typography>
      <Box
        component="pre"
        sx={{
          m: 0,
          p: 1.5,
          borderRadius: 1,
          bgcolor: theme => theme.palette.action.hover,
          fontFamily: 'monospace',
          fontSize: '0.8rem',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          overflowX: 'auto',
          maxHeight: 360,
          overflowY: 'auto',
        }}
      >
        {body || t('common.empty')}
      </Box>
    </Box>
  )

  // 各块放进与「书」「隔离区」两个标签同一种表格容器（纸面底、细边框、轻阴影），不再直接
  // 铺在页面底色上。容器原本是给撑满剩余高度的单张表格用的，这里几块上下排，按内容定高。
  const sectionSx: SxProps<Theme> = [
    UNIFIED_TABLE_STYLES.tableContentContainer,
    {
      flex: '0 0 auto',
      overflow: 'visible',
      p: { xs: 1.5, sm: 2 },
      '@media (max-width: 899.95px)': { flex: '0 0 auto', height: 'auto', maxHeight: 'none' },
    },
  ] as SxProps<Theme>

  const sectionTitle = (title: string) => (
    <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
      {title}
    </Typography>
  )

  const tableHeader = (labels: string[]) => (
    <TableHead>
      <TableRow>
        {labels.map(label => (
          <TableCell key={label} sx={UNIFIED_TABLE_STYLES.header}>
            {label}
          </TableCell>
        ))}
      </TableRow>
    </TableHead>
  )

  return (
    <Box sx={[SCROLL_COLUMN_STYLES, { gap: 2, pb: 1 }] as SxProps<Theme>}>
      <Box sx={sectionSx}>
        <Stack spacing={2}>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
            <FilterSelect
              label={t('test.channel')}
              value={chatKey}
              options={channelOptions}
              onChange={setChatKey}
            />
            <FilterSelect
              label={t('test.preset')}
              value={presetId}
              options={presetOptions}
              onChange={setPresetId}
            />
          </Stack>

          <TextField
            label={t('test.text')}
            value={text}
            onChange={event => setText(event.target.value.slice(0, WORLDBOOK_TRIGGER_TEXT_MAX_LENGTH))}
            placeholder={t('test.textPlaceholder')}
            helperText={t('test.textHelper')}
            multiline
            minRows={3}
            fullWidth
          />

          <Box>
            <ActionButton
              tone="primary"
              onClick={() => mutation.mutate()}
              disabled={!chatKey || !isScopeReady || mutation.isPending}
            >
              {t('test.run')}
            </ActionButton>
          </Box>
        </Stack>
      </Box>

      {error ? (
        isNotFoundError(error) ? (
          <WorldbookNotFoundState compact />
        ) : (
          <QueryErrorState error={error} compact />
        )
      ) : null}

      {result && (
        <Stack spacing={2}>
          {result.budget && (
            <Box sx={sectionSx}>
              <BudgetExplain budget={result.budget} totalTokens={result.total_tokens} />
            </Box>
          )}

          {!result.gate_enabled && <Alert severity="warning">{t('test.gateOff')}</Alert>}

          <Box>
            <Typography variant="caption" color="text.secondary" display="block">
              {t('test.summary', { count: result.window_size })}
            </Typography>
            {text.trim() && (
              <Typography variant="caption" color="text.secondary" display="block">
                {t('test.synthetic')}
              </Typography>
            )}
          </Box>

          <Box sx={sectionSx}>
            {sectionTitle(t('test.booksTitle'))}
            {result.books.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                {t('test.booksEmpty')}
              </Typography>
            ) : (
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small" sx={{ minWidth: BOOKS_TABLE_MIN_WIDTH }}>
                  {tableHeader([
                    t('test.bookColumns.name'),
                    t('test.bookColumns.scope'),
                    t('test.bookColumns.trusted'),
                    t('test.bookColumns.untrusted'),
                  ])}
                  <TableBody>
                    {result.books.map(book => (
                      <TableRow key={book.id} sx={UNIFIED_TABLE_STYLES.row}>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>{book.name}</TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                          <WorldbookScopeChip scopeKind={book.scope_kind} />
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                          {book.trusted_entries}
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                          {book.untrusted_entries}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            )}
          </Box>

          <Box sx={sectionSx}>
            {sectionTitle(t('test.hitsTitle'))}
            {result.hits.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                {t('test.hitsEmpty')}
              </Typography>
            ) : (
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small" sx={{ minWidth: HITS_TABLE_MIN_WIDTH }}>
                  {tableHeader([
                    t('test.hitColumns.title'),
                    t('test.hitColumns.book'),
                    t('test.hitColumns.reason'),
                    t('test.hitColumns.position'),
                    t('test.hitColumns.order'),
                    t('test.hitColumns.tokens'),
                    t('test.hitColumns.trust'),
                    t('test.hitColumns.result'),
                  ])}
                  <TableBody>
                    {result.hits.map(hit => {
                      const book = result.books.find(item => item.id === hit.book_id)
                      const dropped = result.dropped.find(item => item.entry_id === hit.entry_id)
                      const selected = result.selected.includes(hit.entry_id)
                      return (
                        <TableRow key={hit.entry_id} sx={UNIFIED_TABLE_STYLES.row}>
                          <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexWrap: 'wrap' }}>
                              <span>{hit.comment || `#${hit.entry_id}`}</span>
                              {hit.ignored_budget && (
                                <Chip
                                  size="small"
                                  label={t('test.ignoredBudget')}
                                  sx={CHIP_VARIANTS.getCustomColorChip(
                                    theme.palette.info.main,
                                    true
                                  )}
                                />
                              )}
                            </Box>
                          </TableCell>
                          <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                            {book?.name ?? `#${hit.book_id}`}
                          </TableCell>
                          <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                            {hit.reason_kind === 'constant'
                              ? t('test.reasonConstant')
                              : hit.reason_kind === 'semantic'
                                ? t('test.reasonSemantic', {
                                    score: hit.score === null ? '—' : hit.score.toFixed(2),
                                  })
                                : t('test.reasonKey', { key: hit.matched_key })}
                          </TableCell>
                          <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                            {renderPosition(result, hit)}
                          </TableCell>
                          <TableCell sx={UNIFIED_TABLE_STYLES.cell}>{hit.order}</TableCell>
                          <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                            {t('test.tokenWithChars', {
                              tokens: hit.tokens ?? '—',
                              chars: hit.chars,
                            })}
                          </TableCell>
                          <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                            <WorldbookTrustChip trusted={hit.trusted} />
                          </TableCell>
                          <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                            {selected ? (
                              <Chip
                                size="small"
                                label={t('test.selected')}
                                sx={CHIP_VARIANTS.getCustomColorChip(
                                  theme.palette.success.main,
                                  true
                                )}
                              />
                            ) : dropped ? (
                              <>
                                <Typography variant="caption" color="text.secondary">
                                  {t('test.dropped', {
                                    reason: worldbookDropReasonText(
                                      dropped.reason,
                                      'test.dropReason',
                                      t
                                    ),
                                  })}
                                </Typography>
                                {renderAhead(result, dropped.ahead)}
                              </>
                            ) : (
                              <Typography variant="caption" color="text.secondary">
                                {t('test.notSelected')}
                              </Typography>
                            )}
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              </Box>
            )}
          </Box>

          <Box sx={sectionSx}>
            {sectionTitle(t('test.renderedTitle'))}
            <Stack spacing={1.5}>
              {renderBlock(t('test.renderedAfterPersona'), result.rendered.after_persona)}
              {renderBlock(t('test.renderedBeforeHistory'), result.rendered.before_history)}
              {Object.entries(result.rendered.at_depth)
                .sort(([left], [right]) => Number(left) - Number(right))
                .map(([depth, body]) => (
                  <Box key={depth}>
                    {renderBlock(t('test.renderedAtDepth', { depth }), body)}
                  </Box>
                ))}
            </Stack>
            <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
              {t('test.renderedCaption')}
            </Typography>
          </Box>
        </Stack>
      )}
    </Box>
  )
}
