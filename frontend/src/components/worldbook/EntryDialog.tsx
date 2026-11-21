import { useMemo, useState, type KeyboardEvent } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Autocomplete,
  Box,
  Chip,
  FormControlLabel,
  Stack,
  Switch,
  TextField,
  Typography,
  useTheme,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import FilterSelect from '../common/FilterSelect'
import NekroDialog from '../common/NekroDialog'
import { useIdentity } from '../../hooks/useIdentity'
import { useNotification } from '../../hooks/useNotification'
import {
  WORLDBOOK_COMMENT_MAX_LENGTH,
  WORLDBOOK_CONTENT_MAX_LENGTH,
  WORLDBOOK_DEPTH_MAX,
  WORLDBOOK_DEPTH_MIN,
  WORLDBOOK_KEY_MAX_LENGTH,
  WORLDBOOK_MAX_KEYS,
  WORLDBOOK_ORDER_MAX,
  WORLDBOOK_ORDER_MIN,
  WORLDBOOK_POSITIONS,
  WORLDBOOK_SCAN_DEPTH_MAX,
  WORLDBOOK_SCAN_DEPTH_MIN,
  WORLDBOOK_SELECTIVE_LOGICS,
  WORLDBOOK_STATE_ONLY_PURPOSES,
  WORLDBOOK_TIMED_MAX,
  worldbookApi,
  type WorldbookBookDetail,
  type WorldbookEntryCreateRequest,
  type WorldbookEntryItem,
  type WorldbookEntryUpdateRequest,
  type WorldbookPosition,
  type WorldbookSelectiveLogic,
} from '../../services/api/worldbook'
import { CHIP_VARIANTS } from '../../theme/variants'

type TriState = '' | 'true' | 'false'

interface EntryFormState {
  comment: string
  keys: string[]
  secondaryKeys: string[]
  selectiveLogic: WorldbookSelectiveLogic
  useRegex: boolean
  constant: boolean
  content: string
  order: string
  position: WorldbookPosition
  depth: string
  scanDepth: string
  caseSensitive: TriState
  matchWholeWords: TriState
  sticky: string
  cooldown: string
  delay: string
  enabled: boolean
  trusted: boolean
  vectorized: boolean
  ignoreBudget: boolean
}

const DEFAULT_ENTRY_FORM: EntryFormState = {
  comment: '',
  keys: [],
  secondaryKeys: [],
  selectiveLogic: 'and_any',
  useRegex: false,
  constant: false,
  content: '',
  order: '100',
  position: 'before_history',
  depth: '4',
  scanDepth: '',
  caseSensitive: '',
  matchWholeWords: '',
  sticky: '',
  cooldown: '',
  delay: '',
  enabled: true,
  trusted: false,
  vectorized: false,
  ignoreBudget: false,
}

function triStateFromNullable(value: boolean | null): TriState {
  return value === null ? '' : value ? 'true' : 'false'
}

function entryFormFromEntry(entry: WorldbookEntryItem): EntryFormState {
  return {
    comment: entry.comment,
    keys: [...entry.keys],
    secondaryKeys: [...entry.secondary_keys],
    selectiveLogic: (WORLDBOOK_SELECTIVE_LOGICS as readonly string[]).includes(
      entry.selective_logic
    )
      ? (entry.selective_logic as WorldbookSelectiveLogic)
      : 'and_any',
    useRegex: entry.use_regex,
    constant: entry.constant,
    content: entry.content,
    order: String(entry.order),
    position: (WORLDBOOK_POSITIONS as readonly string[]).includes(entry.position)
      ? (entry.position as WorldbookPosition)
      : 'before_history',
    depth: String(entry.depth),
    scanDepth: entry.scan_depth === null ? '' : String(entry.scan_depth),
    caseSensitive: triStateFromNullable(entry.case_sensitive),
    matchWholeWords: triStateFromNullable(entry.match_whole_words),
    sticky: entry.sticky === null ? '' : String(entry.sticky),
    cooldown: entry.cooldown === null ? '' : String(entry.cooldown),
    delay: entry.delay === null ? '' : String(entry.delay),
    enabled: entry.enabled,
    trusted: entry.trusted,
    vectorized: entry.vectorized,
    ignoreBudget: entry.ignore_budget,
  }
}

function parseOptionalInteger(value: string): number | null {
  const text = value.trim()
  if (!text) return null
  const parsed = Number(text)
  return Number.isInteger(parsed) ? parsed : null
}

function triStateToNullable(value: TriState): boolean | null {
  if (value === '') return null
  return value === 'true'
}

function cleanKeys(values: string[]): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  values.forEach(value => {
    const key = value.trim()
    if (!key || seen.has(key)) return
    seen.add(key)
    result.push(key)
  })
  return result
}

function arraysEqual(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index])
}

/**
 * 判断一个关键词会不会被当成正则，以及它能不能编译
 *
 * 只做提示，不拦提交：服务端还要做安全检查（反向引用、嵌套重复等），前端这层只把
 * 语法明显写错的标签标出来。`g` / `y` / `u` 对匹配语义没有影响，忽略；其余未知标志
 * 视为写错。Python 的命名组写法 `(?P<name>` 先改写成 JS 的 `(?<name>` 再试。
 */
function regexIssue(key: string, useRegex: boolean): string | null {
  let pattern = key
  let flags = ''

  if (!useRegex) {
    const match = key.match(/^\/([\s\S]*)\/([a-zA-Z]*)$/)
    if (!match) return null
    pattern = match[1]
    for (const flag of match[2]) {
      if (flag === 'i' || flag === 's' || flag === 'm') flags += flag
      else if (flag === 'g' || flag === 'y' || flag === 'u') continue
      else return 'flags'
    }
  }

  try {
    new RegExp(pattern.replace(/\(\?P</g, '(?<'), flags)
    return null
  } catch {
    return 'pattern'
  }
}

interface EntryDialogProps {
  open: boolean
  book: WorldbookBookDetail
  entry: WorldbookEntryItem | null
  onClose: () => void
  onSaved?: () => void
}

/**
 * 条目新建 / 编辑对话框
 *
 * 编辑时只发改过的字段：服务端把「没传」当「不改」，把可空字段的显式 `null` 当
 * 「清空」。这正是三态覆盖（用书的设置 / 是 / 否）能工作的前提。
 */
export default function EntryDialog({ open, book, entry, onClose, onSaved }: EntryDialogProps) {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')
  const { t: tCommon } = useTranslation('common')
  const { isPlatformOwner } = useIdentity()
  const notification = useNotification()
  const queryClient = useQueryClient()

  const [form, setForm] = useState<EntryFormState>(() =>
    entry ? entryFormFromEntry(entry) : { ...DEFAULT_ENTRY_FORM }
  )
  const [mainInput, setMainInput] = useState('')
  const [secondaryInput, setSecondaryInput] = useState('')

  const update = (patch: Partial<EntryFormState>) => setForm(current => ({ ...current, ...patch }))

  const keys = cleanKeys(form.keys)
  const secondaryKeys = cleanKeys(form.secondaryKeys)
  const invalidRegexKeys = useMemo(
    () =>
      [...keys, ...secondaryKeys].filter(key => regexIssue(key, form.useRegex) !== null),
    [keys, secondaryKeys, form.useRegex]
  )

  // 上限按主、次关键词各自算，与服务端 `_clean_keys` 对每个数组分别校验一致
  const keyListError = (list: string[]) =>
    list.length > WORLDBOOK_MAX_KEYS
      ? t('entryDialog.tooManyKeys', { max: WORLDBOOK_MAX_KEYS })
      : list.some(key => key.length > WORLDBOOK_KEY_MAX_LENGTH)
        ? t('entryDialog.keyTooLong', { max: WORLDBOOK_KEY_MAX_LENGTH })
        : ''
  const mainKeyError = keyListError(keys)
  const secondaryKeyError = keyListError(secondaryKeys)
  const keyError = mainKeyError || secondaryKeyError

  const commentError =
    form.comment.length > WORLDBOOK_COMMENT_MAX_LENGTH
      ? t('entryDialog.commentTooLong', { max: WORLDBOOK_COMMENT_MAX_LENGTH })
      : ''
  const contentError = !form.content.trim()
    ? t('entryDialog.contentRequired')
    : form.content.length > WORLDBOOK_CONTENT_MAX_LENGTH
      ? t('entryDialog.contentTooLong', { max: WORLDBOOK_CONTENT_MAX_LENGTH })
      : ''
  const keysRequired = !form.constant && keys.length === 0

  const orderValue = parseOptionalInteger(form.order)
  const orderError =
    orderValue === null || orderValue < WORLDBOOK_ORDER_MIN || orderValue > WORLDBOOK_ORDER_MAX
      ? t('entryDialog.orderInvalid')
      : ''

  const depthValue = parseOptionalInteger(form.depth)
  const depthError =
    form.position === 'at_depth' &&
    (depthValue === null || depthValue < WORLDBOOK_DEPTH_MIN || depthValue > WORLDBOOK_DEPTH_MAX)
      ? t('entryDialog.depthInvalid')
      : ''

  const scanDepthValue = parseOptionalInteger(form.scanDepth)
  const scanDepthError =
    form.scanDepth.trim() !== '' &&
    (scanDepthValue === null ||
      scanDepthValue < WORLDBOOK_SCAN_DEPTH_MIN ||
      scanDepthValue > WORLDBOOK_SCAN_DEPTH_MAX)
      ? t('entryDialog.scanDepthInvalid')
      : ''

  const timedError = (value: string) => {
    const parsed = parseOptionalInteger(value)
    return value.trim() !== '' && (parsed === null || parsed < 0 || parsed > WORLDBOOK_TIMED_MAX)
      ? t('entryDialog.numberInvalid')
      : ''
  }
  const stickyError = timedError(form.sticky)
  const cooldownError = timedError(form.cooldown)
  const delayError = timedError(form.delay)

  const hasErrors = Boolean(
    keyError ||
      commentError ||
      contentError ||
      keysRequired ||
      orderError ||
      depthError ||
      scanDepthError ||
      stickyError ||
      cooldownError ||
      delayError
  )

  // 「忽略预算」只对宿主条目生效：新建时服务端按创建者身份定（平台方建的一律是宿主内容），
  // 编辑时看这一行现在的值。租户写的条目一律不是宿主条目。
  const effectiveTrusted = entry ? form.trusted : isPlatformOwner

  const mutation = useMutation({
    mutationFn: async () => {
      if (entry) {
        const body = buildUpdateRequest()
        if (Object.keys(body).length === 0) return entry
        return worldbookApi.updateEntry(entry.id, body)
      }
      return worldbookApi.createEntry(book.id, buildCreateRequest())
    },
    onSuccess: () => {
      notification.success(t('entryDialog.saveSuccess'))
      void queryClient.invalidateQueries({ queryKey: ['worldbook'] })
      onSaved?.()
      onClose()
    },
    onError: (error: unknown) => {
      notification.error(
        error instanceof Error && error.message
          ? error.message
          : tCommon('messages.operationFailed')
      )
    },
  })

  function buildCreateRequest(): WorldbookEntryCreateRequest {
    return {
      comment: form.comment.trim(),
      keys,
      secondary_keys: secondaryKeys,
      selective_logic: form.selectiveLogic,
      use_regex: form.useRegex,
      constant: form.constant,
      content: form.content,
      order: orderValue ?? 100,
      position: form.position,
      depth: depthValue ?? 4,
      scan_depth: scanDepthValue,
      case_sensitive: triStateToNullable(form.caseSensitive),
      match_whole_words: triStateToNullable(form.matchWholeWords),
      sticky: parseOptionalInteger(form.sticky),
      cooldown: parseOptionalInteger(form.cooldown),
      delay: parseOptionalInteger(form.delay),
      enabled: form.enabled,
      vectorized: form.vectorized,
      ignore_budget: form.ignoreBudget,
      // 不带 `trusted`：新建条目的信任级别由服务端按创建者身份定（平台方 = 宿主内容），
      // 请求里带了也会被忽略
    }
  }

  function buildUpdateRequest(): WorldbookEntryUpdateRequest {
    if (!entry) return {}
    const body: WorldbookEntryUpdateRequest = {}
    const comment = form.comment.trim()

    if (comment !== entry.comment) body.comment = comment
    if (!arraysEqual(keys, entry.keys)) body.keys = keys
    if (!arraysEqual(secondaryKeys, entry.secondary_keys)) body.secondary_keys = secondaryKeys
    if (form.selectiveLogic !== entry.selective_logic) body.selective_logic = form.selectiveLogic
    if (form.useRegex !== entry.use_regex) body.use_regex = form.useRegex
    if (form.constant !== entry.constant) body.constant = form.constant
    if (form.content !== entry.content) body.content = form.content
    if ((orderValue ?? 100) !== entry.order) body.order = orderValue ?? 100
    if (form.position !== entry.position) body.position = form.position
    if (form.position === 'at_depth' && (depthValue ?? 4) !== entry.depth) {
      body.depth = depthValue ?? 4
    }

    const scanDepth = parseOptionalInteger(form.scanDepth)
    if (scanDepth !== entry.scan_depth) body.scan_depth = scanDepth

    const caseSensitive = triStateToNullable(form.caseSensitive)
    if (caseSensitive !== entry.case_sensitive) body.case_sensitive = caseSensitive

    const matchWholeWords = triStateToNullable(form.matchWholeWords)
    if (matchWholeWords !== entry.match_whole_words) body.match_whole_words = matchWholeWords

    const sticky = parseOptionalInteger(form.sticky)
    if (sticky !== entry.sticky) body.sticky = sticky
    const cooldown = parseOptionalInteger(form.cooldown)
    if (cooldown !== entry.cooldown) body.cooldown = cooldown
    const delay = parseOptionalInteger(form.delay)
    if (delay !== entry.delay) body.delay = delay

    if (form.enabled !== entry.enabled) body.enabled = form.enabled
    if (form.vectorized !== entry.vectorized) body.vectorized = form.vectorized
    if (form.ignoreBudget !== entry.ignore_budget) body.ignore_budget = form.ignoreBudget
    if (isPlatformOwner && form.trusted !== entry.trusted) body.trusted = form.trusted

    return body
  }

  const handleComma = (
    event: KeyboardEvent,
    current: string[],
    apply: (next: string[]) => void,
    input: string,
    setInput: (value: string) => void
  ) => {
    if (event.key !== ',' && event.key !== '，') return
    event.preventDefault()
    const text = input.trim()
    if (text) apply(cleanKeys([...current, text]))
    setInput('')
  }

  const selectiveLogicOptions = WORLDBOOK_SELECTIVE_LOGICS.map(logic => ({
    value: logic,
    label: t(`entryDialog.logic.${logic}`),
  }))

  const positionOptions = WORLDBOOK_POSITIONS.map(position => ({
    value: position,
    label: t(`entryDialog.positionOptions.${position}`),
  }))

  const triStateOptions = [
    { value: '', label: t('entryDialog.useBookDefault') },
    { value: 'true', label: t('entryDialog.yes') },
    { value: 'false', label: t('entryDialog.no') },
  ]

  return (
    <NekroDialog
      open={open}
      onClose={onClose}
      title={entry ? t('entryDialog.editTitle') : t('entryDialog.createTitle')}
      maxWidth="md"
      actions={
        <>
          <ActionButton tone="ghost" onClick={onClose} disabled={mutation.isPending}>
            {tCommon('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone="primary"
            onClick={() => mutation.mutate()}
            disabled={hasErrors || mutation.isPending}
          >
            {tCommon('actions.save')}
          </ActionButton>
        </>
      }
    >
      <Stack spacing={2}>
        {book.scope_kind === 'person' && (
          <Alert severity="warning">{t('entryDialog.personBookWarning')}</Alert>
        )}
        {entry && entry.invalid_keys.length > 0 && (
          <Alert severity="warning">
            {t('entryDialog.invalidKeysWarning', {
              keys: entry.invalid_keys.map(item => `${item.key}（${item.reason}）`).join('、'),
            })}
          </Alert>
        )}
        {entry && entry.slow_disabled_keys.length > 0 && (
          <Alert severity="warning">
            {t('entryDialog.slowKeysWarning', {
              keys: entry.slow_disabled_keys.join('、'),
            })}
          </Alert>
        )}
        {entry && entry.status === 'quarantined' && (
          <Alert severity="warning">{t('entryDialog.quarantinedWarning')}</Alert>
        )}
        {entry && WORLDBOOK_STATE_ONLY_PURPOSES.includes(entry.purpose) && (
          <Alert severity="info">
            {t('detail.purposeHint.stateOnly', {
              kind: t(`detail.purposeKind.${entry.purpose}`, { defaultValue: entry.purpose }),
            })}
          </Alert>
        )}
        {entry && entry.purpose === 'unsupported_template' && (
          <Alert severity="warning">
            {t('detail.purposeHint.unsupported', { reason: entry.template_note })}
          </Alert>
        )}
        {entry && entry.suspected_trap && (
          <Alert severity="error">{t('detail.trapHint', { reason: entry.trap_reason })}</Alert>
        )}

        <TextField
          label={t('entryDialog.comment')}
          value={form.comment}
          onChange={event => update({ comment: event.target.value })}
          error={Boolean(commentError)}
          helperText={commentError || t('entryDialog.commentLimit', { max: WORLDBOOK_COMMENT_MAX_LENGTH })}
          slotProps={{ htmlInput: { maxLength: WORLDBOOK_COMMENT_MAX_LENGTH } }}
          fullWidth
        />

        <Autocomplete
          multiple
          freeSolo
          options={[] as string[]}
          value={form.keys}
          inputValue={mainInput}
          onInputChange={(_event, value) => setMainInput(value)}
          onChange={(_event, value) =>
            update({ keys: cleanKeys(value.filter((item): item is string => typeof item === 'string')) })
          }
          onKeyDown={event =>
            handleComma(
              event,
              form.keys,
              next => update({ keys: next }),
              mainInput,
              setMainInput
            )
          }
          renderTags={(values, getTagProps) =>
            values.map((option, index) => {
              const { key, ...tagProps } = getTagProps({ index })
              return (
                <Chip
                  key={key}
                  size="small"
                  label={option}
                  sx={
                    regexIssue(option, form.useRegex) !== null
                      ? CHIP_VARIANTS.getCustomColorChip(theme.palette.error.main, true)
                      : CHIP_VARIANTS.getCustomColorChip(theme.palette.primary.main, true)
                  }
                  {...tagProps}
                />
              )
            })
          }
          renderInput={params => (
            <TextField
              {...params}
              label={t('entryDialog.keys')}
              placeholder={t('entryDialog.keysPlaceholder')}
              error={Boolean(mainKeyError) || keysRequired}
              helperText={mainKeyError || (keysRequired ? t('entryDialog.keysRequired') : '')}
            />
          )}
        />

        <Autocomplete
          multiple
          freeSolo
          options={[] as string[]}
          value={form.secondaryKeys}
          inputValue={secondaryInput}
          onInputChange={(_event, value) => setSecondaryInput(value)}
          onChange={(_event, value) =>
            update({
              secondaryKeys: cleanKeys(
                value.filter((item): item is string => typeof item === 'string')
              ),
            })
          }
          onKeyDown={event =>
            handleComma(
              event,
              form.secondaryKeys,
              next => update({ secondaryKeys: next }),
              secondaryInput,
              setSecondaryInput
            )
          }
          renderTags={(values, getTagProps) =>
            values.map((option, index) => {
              const { key, ...tagProps } = getTagProps({ index })
              return (
                <Chip
                  key={key}
                  size="small"
                  label={option}
                  sx={
                    regexIssue(option, form.useRegex) !== null
                      ? CHIP_VARIANTS.getCustomColorChip(theme.palette.error.main, true)
                      : CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true)
                  }
                  {...tagProps}
                />
              )
            })
          }
          renderInput={params => (
            <TextField
              {...params}
              label={t('entryDialog.secondaryKeys')}
              placeholder={t('entryDialog.secondaryKeysPlaceholder')}
              error={Boolean(secondaryKeyError)}
              helperText={secondaryKeyError}
            />
          )}
        />

        {secondaryKeys.length > 0 && (
          <FilterSelect
            label={t('entryDialog.selectiveLogic')}
            value={form.selectiveLogic}
            options={selectiveLogicOptions}
            onChange={value =>
              update({ selectiveLogic: value as WorldbookSelectiveLogic })
            }
          />
        )}

        {invalidRegexKeys.length > 0 && (
          <Typography variant="caption" color="error.main">
            {t('entryDialog.regexInvalid', { keys: invalidRegexKeys.join('、') })}
          </Typography>
        )}

        <Box>
          <FormControlLabel
            control={
              <Switch
                checked={form.useRegex}
                onChange={event => update({ useRegex: event.target.checked })}
              />
            }
            label={t('entryDialog.useRegex')}
          />
          <Typography variant="caption" color="text.secondary" display="block">
            {t('entryDialog.useRegexHelper')}
          </Typography>
        </Box>

        <Typography variant="caption" color="text.secondary">
          {t('entryDialog.regexSafety')}
        </Typography>

        <Box>
          <FormControlLabel
            control={
              <Switch
                checked={form.constant}
                onChange={event =>
                  update(
                    event.target.checked
                      ? { constant: true, vectorized: false }
                      : { constant: false }
                  )
                }
              />
            }
            label={t('entryDialog.constant')}
          />
          <Typography variant="caption" color="text.secondary" display="block">
            {t('entryDialog.constantHelper')}
          </Typography>
        </Box>

        <Box>
          <FormControlLabel
            control={
              <Switch
                checked={form.vectorized}
                disabled={form.constant}
                onChange={event => update({ vectorized: event.target.checked })}
              />
            }
            label={t('entryDialog.vectorized')}
          />
          <Typography variant="caption" color="text.secondary" display="block">
            {form.constant
              ? t('entryDialog.vectorizedConstant')
              : t('entryDialog.vectorizedHelper')}
          </Typography>
        </Box>

        <TextField
          label={t('entryDialog.content')}
          value={form.content}
          onChange={event => update({ content: event.target.value })}
          error={Boolean(contentError)}
          helperText={contentError || t('entryDialog.contentCount', { count: form.content.length })}
          slotProps={{ htmlInput: { maxLength: WORLDBOOK_CONTENT_MAX_LENGTH } }}
          multiline
          minRows={4}
          fullWidth
          required
        />

        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
          <TextField
            label={t('entryDialog.order')}
            value={form.order}
            onChange={event => update({ order: event.target.value })}
            error={Boolean(orderError)}
            helperText={orderError || t('entryDialog.orderHelper')}
            type="number"
            fullWidth
          />
          <FilterSelect
            label={t('entryDialog.position')}
            value={form.position}
            options={positionOptions}
            onChange={value => update({ position: value as WorldbookPosition })}
          />
        </Stack>

        {form.position === 'at_depth' && (
          <TextField
            label={t('entryDialog.depth')}
            value={form.depth}
            onChange={event => update({ depth: event.target.value })}
            error={Boolean(depthError)}
            helperText={depthError || t('entryDialog.depthHelper')}
            type="number"
            fullWidth
          />
        )}

        <Accordion disableGutters>
          <AccordionSummary expandIcon={<ExpandMoreIcon />}>
            <Typography variant="body2" fontWeight={600}>
              {t('entryDialog.advanced')}
            </Typography>
          </AccordionSummary>
          <AccordionDetails>
            <Stack spacing={2}>
              <TextField
                label={t('entryDialog.scanDepth')}
                value={form.scanDepth}
                onChange={event => update({ scanDepth: event.target.value })}
                error={Boolean(scanDepthError)}
                helperText={scanDepthError || t('entryDialog.scanDepthHelper')}
                type="number"
                fullWidth
              />
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                <FilterSelect
                  label={t('entryDialog.caseSensitive')}
                  value={form.caseSensitive}
                  options={triStateOptions}
                  onChange={value => update({ caseSensitive: value as TriState })}
                />
                <FilterSelect
                  label={t('entryDialog.matchWholeWords')}
                  value={form.matchWholeWords}
                  options={triStateOptions}
                  onChange={value => update({ matchWholeWords: value as TriState })}
                />
              </Stack>
              <TextField
                label={t('entryDialog.sticky')}
                value={form.sticky}
                onChange={event => update({ sticky: event.target.value })}
                error={Boolean(stickyError)}
                helperText={stickyError || t('entryDialog.stickyHelper')}
                type="number"
                fullWidth
              />
              <TextField
                label={t('entryDialog.cooldown')}
                value={form.cooldown}
                onChange={event => update({ cooldown: event.target.value })}
                error={Boolean(cooldownError)}
                helperText={cooldownError || t('entryDialog.cooldownHelper')}
                type="number"
                fullWidth
              />
              <TextField
                label={t('entryDialog.delay')}
                value={form.delay}
                onChange={event => update({ delay: event.target.value })}
                error={Boolean(delayError)}
                helperText={delayError || t('entryDialog.delayHelper')}
                type="number"
                fullWidth
              />
            </Stack>
          </AccordionDetails>
        </Accordion>

        <FormControlLabel
          control={
            <Switch
              checked={form.enabled}
              onChange={event => update({ enabled: event.target.checked })}
            />
          }
          label={t('entryDialog.enabled')}
        />

        <Box>
          <FormControlLabel
            control={
              <Switch
                checked={form.ignoreBudget}
                onChange={event => update({ ignoreBudget: event.target.checked })}
              />
            }
            label={t('entryDialog.ignoreBudget')}
          />
          <Typography variant="caption" color="text.secondary" display="block">
            {t('entryDialog.ignoreBudgetHelper')}
          </Typography>
          {form.ignoreBudget && !effectiveTrusted && (
            <Typography variant="caption" color="warning.main" display="block">
              {t('entryDialog.ignoreBudgetNotHost')}
            </Typography>
          )}
        </Box>

        {isPlatformOwner ? (
          <Box>
            <FormControlLabel
              control={
                <Switch
                  // 新建时服务端一律按宿主内容建，开关只读地显示这个结果；建好后再编辑才能改
                  checked={entry ? form.trusted : true}
                  disabled={!entry}
                  onChange={event => update({ trusted: event.target.checked })}
                />
              }
              label={t('entryDialog.trusted')}
            />
            <Typography variant="caption" color="text.secondary" display="block">
              {entry ? t('entryDialog.trustedHelper') : t('entryDialog.trustedCreateHelper')}
            </Typography>
          </Box>
        ) : (
          <Typography variant="caption" color="text.secondary">
            {t('entryDialog.tenantTrustedHint')}
          </Typography>
        )}
      </Stack>
    </NekroDialog>
  )
}
