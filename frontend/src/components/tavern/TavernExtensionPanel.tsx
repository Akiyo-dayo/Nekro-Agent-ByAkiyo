import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  FormLabel,
  Grid,
  Radio,
  RadioGroup,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import SearchOffIcon from '@mui/icons-material/SearchOff'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSnackbar } from 'notistack'
import ActionButton from '../common/ActionButton'
import IconActionButton from '../common/IconActionButton'
import QueryErrorState from '../common/QueryErrorState'
import { ApiError, isNotFoundError } from '../../services/api/axios'
import {
  tavernApi,
  type PresetCardResponse,
  type PresetCardUpdateRequest,
  type TavernCleanupRule,
  type TavernScope,
} from '../../services/api/tavern'
import { CARD_VARIANTS, CHIP_VARIANTS, SCROLLBAR_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'

/** 与后端同一组上限：前端先给出可读的错误，真正拦下的是服务端 */
const MAX_NICKNAME_CHARS = 128
const MAX_VERSION_CHARS = 64
const MAX_GREETINGS = 50
const MAX_GREETING_CHARS = 16000
const MAX_CLEANUP_RULES = 50
const DEPTH_MIN = 0
const DEPTH_MAX = 100

const SOURCE_KEYS = ['system', 'description', 'personality', 'scenario', 'mes_example'] as const
type SourceKey = (typeof SOURCE_KEYS)[number]

const SOURCE_LABEL_KEYS: Record<SourceKey, string> = {
  system: 'extension.source.system',
  description: 'extension.source.description',
  personality: 'extension.source.personality',
  scenario: 'extension.source.scenario',
  mes_example: 'extension.source.mesExample',
}

type GreetingField = 'greetings' | 'group_greetings'
/** 开场白与规则的上限按字段区分，避免在 JSX 里再写一遍数字 */
const GREETING_LIMIT_KEYS: Record<GreetingField, string> = {
  greetings: 'extension.greetings.general',
  group_greetings: 'extension.greetings.group',
}

/**
 * 表单状态
 *
 * 只放可改的字段：卡片规格、导入人、闸状态、`slow_disabled_rules` 都是只读的，留在
 * 查询结果里，不参与「改没改」的比较。
 */
interface ExtensionForm {
  nickname: string
  macros_enabled: boolean
  source_fields: Record<string, string>
  greetings: string[]
  group_greetings: string[]
  greeting_pick: string
  post_history: string
  prefill: string
  depth_prompt: string
  depth_prompt_depth: number
  cleanup_rules: TavernCleanupRule[]
  character_version: string
}

function emptySourceFields(): Record<string, string> {
  return Object.fromEntries(SOURCE_KEYS.map(key => [key, '']))
}

function readSourceFields(fields: Record<string, string> | undefined): Record<string, string> {
  const result = emptySourceFields()
  for (const key of SOURCE_KEYS) {
    const value = fields?.[key]
    result[key] = typeof value === 'string' ? value : ''
  }
  return result
}

function readRules(rules: TavernCleanupRule[] | undefined): TavernCleanupRule[] {
  if (!Array.isArray(rules)) return []
  return rules.map(rule => ({
    name: typeof rule.name === 'string' ? rule.name : '',
    find: typeof rule.find === 'string' ? rule.find : '',
    replace: typeof rule.replace === 'string' ? rule.replace : '',
    trim: Array.isArray(rule.trim) ? rule.trim.filter((item): item is string => typeof item === 'string') : [],
    enabled: Boolean(rule.enabled),
    disabled_reason: typeof rule.disabled_reason === 'string' ? rule.disabled_reason : '',
  }))
}

function formFromResponse(data: PresetCardResponse): ExtensionForm {
  return {
    nickname: data.nickname ?? '',
    macros_enabled: Boolean(data.macros_enabled),
    source_fields: readSourceFields(data.source_fields),
    greetings: [...(data.greetings ?? [])],
    group_greetings: [...(data.group_greetings ?? [])],
    greeting_pick: data.greeting_pick === 'random' ? 'random' : 'first',
    post_history: data.post_history ?? '',
    prefill: data.prefill ?? '',
    depth_prompt: data.depth_prompt ?? '',
    depth_prompt_depth: Number.isInteger(data.depth_prompt_depth) ? data.depth_prompt_depth : 4,
    cleanup_rules: readRules(data.cleanup_rules),
    character_version: data.character_version ?? '',
  }
}

function sameStringArray(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false
  return left.every((value, index) => value === right[index])
}

function sameSourceFields(left: Record<string, string>, right: Record<string, string>): boolean {
  return SOURCE_KEYS.every(key => (left[key] ?? '') === (right[key] ?? ''))
}

/**
 * 去除串的输入框按逗号拆分，编辑中会出现空串（清空、末尾多一个逗号）
 *
 * 空串既不该存进库，也不该让表单一直显示「有未保存的改动」，比较与提交前统一去掉。
 * 输入框本身保留空串，否则用户打不出「a,」再接着打下一个。
 */
function cleanTrim(trim: string[]): string[] {
  return trim.filter(item => item !== '')
}

function sameRule(left: TavernCleanupRule, right: TavernCleanupRule): boolean {
  return (
    left.name === right.name &&
    left.find === right.find &&
    left.replace === right.replace &&
    left.enabled === right.enabled &&
    left.disabled_reason === right.disabled_reason &&
    sameStringArray(cleanTrim(left.trim), cleanTrim(right.trim))
  )
}

function sameRules(left: TavernCleanupRule[], right: TavernCleanupRule[]): boolean {
  if (left.length !== right.length) return false
  return left.every((rule, index) => sameRule(rule, right[index]))
}

function sameForm(left: ExtensionForm, right: ExtensionForm): boolean {
  return (
    left.nickname === right.nickname &&
    left.macros_enabled === right.macros_enabled &&
    left.greeting_pick === right.greeting_pick &&
    left.post_history === right.post_history &&
    left.prefill === right.prefill &&
    left.depth_prompt === right.depth_prompt &&
    left.depth_prompt_depth === right.depth_prompt_depth &&
    left.character_version === right.character_version &&
    sameSourceFields(left.source_fields, right.source_fields) &&
    sameStringArray(left.greetings, right.greetings) &&
    sameStringArray(left.group_greetings, right.group_greetings) &&
    sameRules(left.cleanup_rules, right.cleanup_rules)
  )
}

/**
 * 只把改过的字段拼进请求
 *
 * 后端按 `model_fields_set` 判断「这次改什么」；整表传回会把没改的字段也覆盖一遍，
 * 清理规则还会连带重新校验整张表。所以这里逐字段与读到的值比较。
 */
function buildPatch(form: ExtensionForm, baseline: ExtensionForm): PresetCardUpdateRequest {
  const patch: PresetCardUpdateRequest = {}
  if (form.nickname !== baseline.nickname) patch.nickname = form.nickname
  if (form.macros_enabled !== baseline.macros_enabled) patch.macros_enabled = form.macros_enabled
  if (form.greeting_pick !== baseline.greeting_pick) patch.greeting_pick = form.greeting_pick
  if (form.post_history !== baseline.post_history) patch.post_history = form.post_history
  if (form.prefill !== baseline.prefill) patch.prefill = form.prefill
  if (form.depth_prompt !== baseline.depth_prompt) patch.depth_prompt = form.depth_prompt
  if (form.depth_prompt_depth !== baseline.depth_prompt_depth) {
    patch.depth_prompt_depth = form.depth_prompt_depth
  }
  if (form.character_version !== baseline.character_version) {
    patch.character_version = form.character_version
  }
  if (!sameSourceFields(form.source_fields, baseline.source_fields)) {
    patch.source_fields = form.source_fields
  }
  if (!sameStringArray(form.greetings, baseline.greetings)) patch.greetings = form.greetings
  if (!sameStringArray(form.group_greetings, baseline.group_greetings)) {
    patch.group_greetings = form.group_greetings
  }
  if (!sameRules(form.cleanup_rules, baseline.cleanup_rules)) {
    patch.cleanup_rules = form.cleanup_rules.map(rule => ({ ...rule, trim: cleanTrim(rule.trim) }))
  }
  return patch
}

function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.message) return error.message
  if (error instanceof Error && error.message) return error.message
  return fallback
}

function formatImportedAt(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

/**
 * 在浏览器里试编译查找串
 *
 * 形状与后端一致：`/pattern/flags` 拆成两段，不是这个形状就整串当 pattern。
 * 只提示不拦：浏览器与 Python 的正则方言不同，最终由服务端判定。
 */
function compileFindError(find: string): string {
  if (!find) return ''
  const matched = find.match(/^\/(.+)\/([A-Za-z]*)$/)
  const pattern = matched ? matched[1] : find
  const flags = matched ? matched[2] : ''
  try {
    RegExp(pattern, flags)
    return ''
  } catch (error) {
    return error instanceof Error ? error.message : String(error)
  }
}

/** 分组标题；每个分组只表达一件事，窄屏下单列排 */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Box component="section" sx={{ minWidth: 0 }}>
      <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 1 }}>
        {title}
      </Typography>
      {children}
    </Box>
  )
}

/** 行内「标签：值」；值可能是外来文字，按纯文本渲染并允许换行 */
function Field({ label, value }: { label: string; value: string }) {
  return (
    <Box sx={{ display: 'flex', gap: 1, alignItems: 'baseline', flexWrap: 'wrap', minWidth: 0 }}>
      <Typography variant="caption" color="text.secondary" sx={{ minWidth: 64 }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ wordBreak: 'break-word', minWidth: 0 }}>
        {value || '—'}
      </Typography>
    </Box>
  )
}

export interface TavernExtensionPanelProps {
  presetId: number
  scope: TavernScope
  readOnly: boolean
  /** 重新拼装后把新正文交给「基本」表单，否则那边保存会用旧正文盖回去 */
  onContentReassembled?: (content: string) => void
  onDirtyChange?: (dirty: boolean) => void
}

/**
 * 人设的「酒馆扩展」面板
 *
 * 表单是读到的值的一份拷贝：所有比较都对着这份拷贝做，保存时只提交改过的字段。
 * 「有没有未保存的改动」由整份表单与初始值逐字段比较得出，`onDirtyChange` 只在结论
 * 变化时回调一次，父组件据此决定关闭前要不要确认。
 */
export default function TavernExtensionPanel({
  presetId,
  scope,
  readOnly,
  onContentReassembled,
  onDirtyChange,
}: TavernExtensionPanelProps) {
  const { t } = useTranslation('tavern')
  const { enqueueSnackbar } = useSnackbar()
  const theme = useTheme()
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  const queryClient = useQueryClient()

  const instanceKey = scope.instance_key ?? ''
  const adapterKey = scope.adapter_key ?? ''
  const formKey = `${presetId}|${instanceKey}|${adapterKey}`
  const queryKey = ['tavern', 'preset-card', presetId, instanceKey, adapterKey] as const

  const query = useQuery({
    queryKey,
    queryFn: () =>
      tavernApi.getCard(presetId, {
        instance_key: scope.instance_key,
        adapter_key: scope.adapter_key,
      }),
    retry: false,
  })

  const [form, setForm] = useState<ExtensionForm | null>(null)
  const [baseline, setBaseline] = useState<ExtensionForm | null>(null)
  const [saveError, setSaveError] = useState('')
  const [reassembleConfirmOpen, setReassembleConfirmOpen] = useState(false)
  // 用 ref 记「上次回调过什么」：父组件的回调函数每次渲染都可能是新引用，不能拿它
  // 当触发条件，否则会在没有变化时反复回调。
  const lastDirtyRef = useRef(false)
  const initializedKeyRef = useRef('')

  // 初次读到数据时建立表单与基准；之后的后台刷新不再覆盖输入框，避免编辑到一半
  // 被一次 refetch 清掉。保存成功时会显式用响应重建这两份。
  useEffect(() => {
    if (!query.data) return
    if (initializedKeyRef.current === formKey) return
    initializedKeyRef.current = formKey
    const next = formFromResponse(query.data)
    setForm(next)
    setBaseline(next)
    setSaveError('')
  }, [query.data, formKey])

  const dirty = Boolean(form && baseline && !sameForm(form, baseline))

  useEffect(() => {
    if (lastDirtyRef.current === dirty) return
    lastDirtyRef.current = dirty
    onDirtyChange?.(dirty)
  }, [dirty, onDirtyChange])

  const saveMutation = useMutation({
    mutationFn: (patch: PresetCardUpdateRequest) => tavernApi.updateCard(presetId, patch, scope),
    onMutate: () => setSaveError(''),
    onSuccess: data => {
      queryClient.setQueryData(queryKey, data)
      const next = formFromResponse(data)
      setForm(next)
      setBaseline(next)
      enqueueSnackbar(t('extension.saveSuccess'), { variant: 'success' })
    },
    onError: error => {
      // 服务端可能一次列出多条规则的问题，原样显示
      setSaveError(errorText(error, t('extension.saveFailed')))
    },
  })

  const reassembleMutation = useMutation({
    mutationFn: () => tavernApi.reassemble(presetId, scope),
    onSuccess: async data => {
      setReassembleConfirmOpen(false)
      onContentReassembled?.(data.content)
      queryClient.setQueryData(queryKey, (previous: PresetCardResponse | undefined) =>
        previous
          ? {
              ...previous,
              content_assembled: true,
              assembled_sha256: data.assembled_sha256,
            }
          : previous
      )
      // 重新拼装不动来源字段，所以这里只刷新只读状态，不重建表单
      await query.refetch()
      enqueueSnackbar(t('extension.source.reassembleDone'), { variant: 'success' })
    },
    onError: error => {
      setReassembleConfirmOpen(false)
      enqueueSnackbar(errorText(error, t('extension.source.reassembleFailed')), { variant: 'error' })
    },
  })

  const setField = <K extends keyof ExtensionForm>(field: K, value: ExtensionForm[K]) => {
    setForm(previous => (previous ? { ...previous, [field]: value } : previous))
  }

  const setSource = (key: SourceKey, value: string) => {
    setForm(previous =>
      previous
        ? {
            ...previous,
            source_fields: { ...previous.source_fields, [key]: value },
          }
        : previous
    )
  }

  const setGreeting = (field: GreetingField, index: number, value: string) => {
    setForm(previous =>
      previous
        ? {
            ...previous,
            [field]: previous[field].map((item, itemIndex) => (itemIndex === index ? value : item)),
          }
        : previous
    )
  }

  const addGreeting = (field: GreetingField) => {
    setForm(previous =>
      previous && previous[field].length < MAX_GREETINGS
        ? { ...previous, [field]: [...previous[field], ''] }
        : previous
    )
  }

  const removeGreeting = (field: GreetingField, index: number) => {
    setForm(previous =>
      previous
        ? {
            ...previous,
            [field]: previous[field].filter((_item, itemIndex) => itemIndex !== index),
          }
        : previous
    )
  }

  const updateRule = (index: number, patch: Partial<TavernCleanupRule>) => {
    setForm(previous =>
      previous
        ? {
            ...previous,
            cleanup_rules: previous.cleanup_rules.map((rule, ruleIndex) =>
              ruleIndex === index ? { ...rule, ...patch } : rule
            ),
          }
        : previous
    )
  }

  const addRule = () => {
    setForm(previous =>
      previous && previous.cleanup_rules.length < MAX_CLEANUP_RULES
        ? {
            ...previous,
            cleanup_rules: [
              ...previous.cleanup_rules,
              { name: '', find: '', replace: '', trim: [], enabled: false, disabled_reason: '' },
            ],
          }
        : previous
    )
  }

  const removeRule = (index: number) => {
    setForm(previous =>
      previous
        ? {
            ...previous,
            cleanup_rules: previous.cleanup_rules.filter((_rule, ruleIndex) => ruleIndex !== index),
          }
        : previous
    )
  }

  const handleSave = () => {
    if (!form || !baseline) return
    const patch = buildPatch(form, baseline)
    if (Object.keys(patch).length === 0) return
    saveMutation.mutate(patch)
  }

  if (query.isPending) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
        <CircularProgress size={28} />
      </Box>
    )
  }

  if (query.isError) {
    if (isNotFoundError(query.error)) {
      return (
        <Box
          sx={{
            minHeight: 180,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            textAlign: 'center',
            gap: 1,
            p: 2,
          }}
        >
          <SearchOffIcon color="disabled" fontSize="large" />
          <Typography variant="subtitle1" fontWeight={700}>
            {t('extension.notFoundTitle')}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 460 }}>
            {t('extension.notFoundHint')}
          </Typography>
        </Box>
      )
    }
    return (
      <QueryErrorState
        error={query.error}
        onRetry={() => void query.refetch()}
        compact
      />
    )
  }

  const data = query.data
  // 也守住「换了人设/作用域但 effect 还没跑」的这一帧：此时 form 还是上一份，
  // 直接渲染会短暂显示别人的扩展数据
  if (!data || !form || !baseline || initializedKeyRef.current !== formKey) {
    // 数据到了、表单还没建立的那一帧：仍是加载态，避免闪一下空表单
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
        <CircularProgress size={28} />
      </Box>
    )
  }

  const sourceDirty = !sameSourceFields(form.source_fields, baseline.source_fields)
  const depthInvalid =
    !Number.isInteger(form.depth_prompt_depth) ||
    form.depth_prompt_depth < DEPTH_MIN ||
    form.depth_prompt_depth > DEPTH_MAX

  const renderGreetingList = (field: GreetingField) => {
    const values = form[field]
    const atLimit = values.length >= MAX_GREETINGS
    return (
      <Box sx={{ minWidth: 0 }}>
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 1,
            flexWrap: 'wrap',
            mb: 1,
          }}
        >
          <Typography variant="body2" fontWeight={600}>
            {t(GREETING_LIMIT_KEYS[field])}
          </Typography>
          <ActionButton
            size="small"
            tone="ghost"
            startIcon={<AddIcon />}
            onClick={() => addGreeting(field)}
            disabled={readOnly || atLimit}
          >
            {t('extension.greetings.add')}
          </ActionButton>
        </Box>
        <Stack spacing={1.5}>
          {values.map((value, index) => {
            const tooLong = value.length > MAX_GREETING_CHARS
            return (
              <Box key={`${field}-${index}`} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start', minWidth: 0 }}>
                <TextField
                  fullWidth
                  multiline
                  minRows={2}
                  size="small"
                  label={t('extension.greetings.itemLabel', { n: index + 1 })}
                  value={value}
                  onChange={event => setGreeting(field, index, event.target.value)}
                  disabled={readOnly}
                  error={tooLong}
                  helperText={
                    tooLong ? t('extension.greetings.tooLong', { n: MAX_GREETING_CHARS }) : undefined
                  }
                  sx={{ minWidth: 0 }}
                />
                <IconActionButton
                  tone="danger"
                  size="small"
                  onClick={() => removeGreeting(field, index)}
                  disabled={readOnly}
                  aria-label={t('extension.greetings.remove')}
                >
                  <DeleteOutlineIcon fontSize="small" />
                </IconActionButton>
              </Box>
            )
          })}
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
          {t('extension.greetings.limitCount', { n: MAX_GREETINGS })}
        </Typography>
      </Box>
    )
  }

  const renderRuleTable = () => (
    <Box>
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 1,
          flexWrap: 'wrap',
          mb: 1,
        }}
      >
        <Typography variant="caption" color="text.secondary">
          {t('extension.rules.limit', { n: MAX_CLEANUP_RULES })}
        </Typography>
        <ActionButton
          size="small"
          tone="ghost"
          startIcon={<AddIcon />}
          onClick={addRule}
          disabled={readOnly || form.cleanup_rules.length >= MAX_CLEANUP_RULES}
        >
          {t('extension.rules.add')}
        </ActionButton>
      </Box>
      <Box sx={{ overflowX: 'auto', ...(SCROLLBAR_VARIANTS.thin.styles as object) }}>
        <Table size="small" sx={{ minWidth: 860 }}>
          <TableHead>
            <TableRow>
              <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('extension.rules.enabled')}</TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('extension.rules.name')}</TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('extension.rules.find')}</TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('extension.rules.replace')}</TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('extension.rules.trim')}</TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('extension.rules.status')}</TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header} />
            </TableRow>
          </TableHead>
          <TableBody>
            {form.cleanup_rules.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} sx={UNIFIED_TABLE_STYLES.cell}>
                  <Typography variant="body2" color="text.secondary">
                    {t('extension.rules.empty')}
                  </Typography>
                </TableCell>
              </TableRow>
            ) : (
              form.cleanup_rules.map((rule, index) => {
                const findError = compileFindError(rule.find)
                const slowDisabled = data.slow_disabled_rules.includes(index)
                // `manual` 是导入时管理员主动关掉的规则，105a 的 rule.reason 里没有这一条；
                // 面板自己补一句，避免状态列直接显示机读值
                const reasonText =
                  rule.disabled_reason === 'manual'
                    ? t('extension.rules.manualReason')
                    : rule.disabled_reason
                      ? t(`rule.reason.${rule.disabled_reason}`, {
                          defaultValue: rule.disabled_reason,
                        })
                      : ''
                return (
                  <TableRow key={index}>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <Switch
                        size="small"
                        checked={rule.enabled}
                        disabled={readOnly}
                        onChange={event =>
                          // 原因只描述导入时的判定；手动改过之后它就不再准确，清掉
                          updateRule(index, { enabled: event.target.checked, disabled_reason: '' })
                        }
                        inputProps={{
                          'aria-label': `${t('extension.rules.enabled')} ${t('extension.rules.item', { n: index + 1 })}`,
                        }}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <TextField
                        size="small"
                        fullWidth
                        value={rule.name}
                        onChange={event => updateRule(index, { name: event.target.value })}
                        disabled={readOnly}
                        inputProps={{ 'aria-label': `${t('extension.rules.name')} ${index + 1}` }}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <TextField
                        size="small"
                        fullWidth
                        multiline
                        maxRows={3}
                        placeholder={t('extension.rules.findPlaceholder')}
                        value={rule.find}
                        onChange={event => updateRule(index, { find: event.target.value })}
                        disabled={readOnly}
                        error={Boolean(findError)}
                        helperText={
                          findError
                            ? t('extension.rules.regexError', { error: findError })
                            : undefined
                        }
                        FormHelperTextProps={{ sx: { wordBreak: 'break-word' } }}
                        inputProps={{ 'aria-label': `${t('extension.rules.find')} ${index + 1}` }}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <TextField
                        size="small"
                        fullWidth
                        multiline
                        maxRows={3}
                        value={rule.replace}
                        onChange={event => updateRule(index, { replace: event.target.value })}
                        disabled={readOnly}
                        inputProps={{ 'aria-label': `${t('extension.rules.replace')} ${index + 1}` }}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <TextField
                        size="small"
                        fullWidth
                        value={rule.trim.join(',')}
                        onChange={event =>
                          updateRule(index, { trim: event.target.value.split(',') })
                        }
                        disabled={readOnly}
                        placeholder={t('extension.rules.trimHint')}
                        inputProps={{ 'aria-label': `${t('extension.rules.trim')} ${index + 1}` }}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      {slowDisabled ? (
                        <Tooltip title={t('extension.rules.slowDisabledHint')}>
                          <Chip
                            size="small"
                            label={t('extension.rules.slowDisabled')}
                            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.warning.main, true)}
                          />
                        </Tooltip>
                      ) : !rule.enabled && reasonText ? (
                        <Typography variant="caption" color="text.secondary">
                          {reasonText}
                        </Typography>
                      ) : (
                        <Typography variant="caption" color="text.disabled">
                          —
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <IconActionButton
                        tone="danger"
                        size="small"
                        onClick={() => removeRule(index)}
                        disabled={readOnly}
                        aria-label={t('extension.rules.remove')}
                      >
                        <DeleteOutlineIcon fontSize="small" />
                      </IconActionButton>
                    </TableCell>
                  </TableRow>
                )
              })
            )}
          </TableBody>
        </Table>
      </Box>
    </Box>
  )

  return (
    <Box sx={{ minWidth: 0 }}>
      <Stack spacing={2}>
        {readOnly ? <Alert severity="info">{t('extension.readOnly')}</Alert> : null}
        {!data.exists ? <Alert severity="info">{t('extension.empty')}</Alert> : null}

        <Section title={t('extension.section.overview')}>
          <Stack spacing={1}>
            <Field
              label={t('extension.overview.spec')}
              value={
                data.card_spec
                  ? t('extension.overview.specCard', { spec: data.card_spec.toUpperCase() })
                  : t('extension.overview.specNative')
              }
            />
            {data.imported_by ? (
              <Field label={t('extension.overview.importedBy')} value={data.imported_by} />
            ) : null}
            {data.imported_at ? (
              <Field
                label={t('extension.overview.importedAt')}
                value={formatImportedAt(data.imported_at)}
              />
            ) : null}
            <TextField
              size="small"
              fullWidth
              label={t('extension.overview.version')}
              value={form.character_version}
              onChange={event => setField('character_version', event.target.value)}
              disabled={readOnly}
              inputProps={{ maxLength: MAX_VERSION_CHARS }}
              helperText={t('extension.overview.versionHint', { n: MAX_VERSION_CHARS })}
            />
          </Stack>
        </Section>

        <Section title={t('extension.section.identity')}>
          <Stack spacing={1.5}>
            <TextField
              size="small"
              fullWidth
              label={t('extension.identity.nickname')}
              value={form.nickname}
              onChange={event => setField('nickname', event.target.value)}
              disabled={readOnly}
              inputProps={{ maxLength: MAX_NICKNAME_CHARS }}
              helperText={t('extension.identity.nicknameHint')}
            />
            <Box>
              <FormControlLabel
                control={
                  <Switch
                    checked={form.macros_enabled}
                    onChange={event => setField('macros_enabled', event.target.checked)}
                    disabled={readOnly}
                  />
                }
                label={t('extension.identity.macros')}
              />
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                {t('extension.identity.macrosHint')}
              </Typography>
            </Box>
          </Stack>
        </Section>

        <Section title={t('extension.section.source')}>
          <Grid container spacing={2}>
            {SOURCE_KEYS.map(key => (
              <Grid item xs={12} md={key === 'system' || key === 'mes_example' ? 12 : 6} key={key}>
                <TextField
                  fullWidth
                  multiline
                  minRows={3}
                  size="small"
                  label={t(SOURCE_LABEL_KEYS[key])}
                  value={form.source_fields[key] ?? ''}
                  onChange={event => setSource(key, event.target.value)}
                  disabled={readOnly}
                />
              </Grid>
            ))}
          </Grid>
          <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', flexWrap: 'wrap', mt: 1.5 }}>
            <Typography
              variant="caption"
              color={data.content_assembled ? 'text.secondary' : 'warning.main'}
            >
              {data.content_assembled
                ? t('extension.source.assembled')
                : t('extension.source.modified')}
            </Typography>
            {!readOnly ? (
              <>
                <Tooltip
                  title={sourceDirty || !data.exists ? t('extension.source.reassembleBlocked') : ''}
                >
                  <span>
                    <ActionButton
                      size="small"
                      tone="secondary"
                      onClick={() => setReassembleConfirmOpen(true)}
                      disabled={sourceDirty || !data.exists || reassembleMutation.isPending}
                    >
                      {reassembleMutation.isPending ? (
                        <CircularProgress size={18} />
                      ) : (
                        t('extension.source.reassemble')
                      )}
                    </ActionButton>
                  </span>
                </Tooltip>
                {sourceDirty || !data.exists ? (
                  <Typography variant="caption" color="text.secondary">
                    {t('extension.source.reassembleBlocked')}
                  </Typography>
                ) : null}
              </>
            ) : null}
          </Box>
        </Section>

        <Section title={t('extension.section.greetings')}>
          <Stack spacing={2}>
            {renderGreetingList('greetings')}
            {renderGreetingList('group_greetings')}
            <FormControl>
              <FormLabel sx={{ fontSize: '0.75rem' }}>{t('extension.greetings.pick')}</FormLabel>
              <RadioGroup
                row
                value={form.greeting_pick}
                onChange={event => setField('greeting_pick', event.target.value)}
              >
                <FormControlLabel
                  value="first"
                  control={<Radio size="small" disabled={readOnly} />}
                  label={t('extension.greetings.pickFirst')}
                  disabled={readOnly}
                />
                <FormControlLabel
                  value="random"
                  control={<Radio size="small" disabled={readOnly} />}
                  label={t('extension.greetings.pickRandom')}
                  disabled={readOnly}
                />
              </RadioGroup>
            </FormControl>
            <Typography variant="caption" color="text.secondary">
              {t('extension.greetings.hint')}
            </Typography>
          </Stack>
        </Section>

        <Section title={t('extension.section.slots')}>
          <Stack spacing={1.5}>
            <Typography variant="caption" color="text.secondary">
              {data.gates.advanced_slots_enabled
                ? t('extension.slots.gateOn')
                : t('extension.slots.gateOff')}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {t('extension.slots.responsibility')}
            </Typography>
            <TextField
              fullWidth
              multiline
              minRows={3}
              size="small"
              label={t('extension.slots.postHistory')}
              value={form.post_history}
              onChange={event => setField('post_history', event.target.value)}
              disabled={readOnly}
            />
            <TextField
              fullWidth
              multiline
              minRows={2}
              size="small"
              label={t('extension.slots.prefill')}
              value={form.prefill}
              onChange={event => setField('prefill', event.target.value)}
              disabled={readOnly}
              helperText={t('extension.slots.prefillHint')}
            />
          </Stack>
        </Section>

        <Section title={t('extension.section.depth')}>
          <Stack spacing={1.5}>
            <TextField
              fullWidth
              multiline
              minRows={2}
              size="small"
              label={t('extension.depth.text')}
              value={form.depth_prompt}
              onChange={event => setField('depth_prompt', event.target.value)}
              disabled={readOnly}
            />
            <TextField
              size="small"
              type="number"
              label={t('extension.depth.depth')}
              value={form.depth_prompt_depth}
              onChange={event => {
                const raw = event.target.value
                const parsed = raw === '' ? 0 : Number(raw)
                setField(
                  'depth_prompt_depth',
                  Number.isFinite(parsed) ? Math.trunc(parsed) : 0
                )
              }}
              onBlur={() =>
                setField(
                  'depth_prompt_depth',
                  Math.min(DEPTH_MAX, Math.max(DEPTH_MIN, form.depth_prompt_depth))
                )
              }
              disabled={readOnly}
              error={depthInvalid}
              helperText={
                depthInvalid
                  ? t('extension.depth.invalid', { min: DEPTH_MIN, max: DEPTH_MAX })
                  : t('extension.depth.depthHint')
              }
              inputProps={{ min: DEPTH_MIN, max: DEPTH_MAX, step: 1 }}
              sx={{ maxWidth: isSmall ? '100%' : 240 }}
            />
          </Stack>
        </Section>

        <Section title={t('extension.section.rules')}>{renderRuleTable()}</Section>

        {saveError ? <Alert severity="error">{saveError}</Alert> : null}

        {!readOnly ? (
          <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1 }}>
            <ActionButton
              tone="primary"
              onClick={handleSave}
              disabled={!dirty || depthInvalid || saveMutation.isPending}
            >
              {saveMutation.isPending ? (
                <CircularProgress size={20} />
              ) : (
                t('extension.save')
              )}
            </ActionButton>
          </Box>
        ) : null}
      </Stack>

      {/* 重新拼装会覆盖「基本」里手改过的正文，先确认再调接口 */}
      <Dialog
        open={reassembleConfirmOpen}
        onClose={() => setReassembleConfirmOpen(false)}
        maxWidth="xs"
        fullWidth
        PaperProps={{ sx: CARD_VARIANTS.default.styles }}
      >
        <DialogTitle>{t('extension.source.reassembleTitle')}</DialogTitle>
        <DialogContent>
          <Typography>{t('extension.source.reassembleConfirm')}</Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <ActionButton
            tone="secondary"
            onClick={() => setReassembleConfirmOpen(false)}
            disabled={reassembleMutation.isPending}
          >
            {t('extension.cancel')}
          </ActionButton>
          <ActionButton
            tone="primary"
            onClick={() => reassembleMutation.mutate()}
            disabled={reassembleMutation.isPending}
          >
            {t('extension.source.reassemble')}
          </ActionButton>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
