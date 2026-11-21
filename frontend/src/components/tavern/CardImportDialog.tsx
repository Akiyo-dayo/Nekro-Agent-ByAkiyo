import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Checkbox,
  Chip,
  CircularProgress,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  FormLabel,
  Radio,
  RadioGroup,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import IconActionButton from '../common/IconActionButton'
import RemoteBooksSection from './RemoteBooksSection'
import { ApiError } from '../../services/api/axios'
import {
  tavernApi,
  type CardImportResult,
  type CardInspectReport,
  type CleanupRuleInspect,
  type SlotsOffChannel,
  type StateTemplateReport,
  type TavernCleanupHtmlKind,
  type TavernCleanupOutputMode,
  type TavernScope,
} from '../../services/api/tavern'
import { I18nDict, getLocalizedText } from '../../services/api/types'
import { CARD_VARIANTS, CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'

/**
 * 卡片自身的上限，与服务端 `limits.MAX_UPLOAD_BYTES` 一致；客户端先拦一次是为了省掉一次必然失败的上传。
 * 服务端还会与平台配置 `MAX_UPLOAD_SIZE_MB`（默认 10）取小，那一道由服务端的报错说明。
 */
const CARD_IMPORT_MAX_BYTES = 20 * 1024 * 1024

/** 处理方式与替换文字带 HTML 时的类别名；只用来显示，不参与导入选项 */
const OUTPUT_MODE_LABEL_KEYS: Record<TavernCleanupOutputMode, string> = {
  replace: 'import.ruleOutputMode.replace',
  text: 'import.ruleOutputMode.text',
  drop: 'import.ruleOutputMode.drop',
}

const HTML_KIND_LABEL_KEYS: Record<TavernCleanupHtmlKind, string> = {
  '': '',
  display: 'import.htmlKindReveal.display',
  external: 'import.htmlKindReveal.external',
  interactive: 'import.htmlKindReveal.interactive',
  remote: 'import.htmlKindReveal.remote',
}

/**
 * 允许在导入时被打开的原因
 *
 * 其余原因（语义不符、安全不过、宏、深度）即便请求里写了 `true`，后端也不会打开；
 * 这里把开关禁用掉，免得管理员点了一个注定被忽略的开关。
 */
const ENABLEABLE_REASONS = new Set(['', 'html_output', 'disabled_in_card'])

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

/** 只取主机名：报告页只需要让人认出「从哪台机器下载」，完整地址不进这张表 */
function hostOf(url: string): string {
  try {
    return new URL(url).host || url
  } catch {
    return url
  }
}

/** 取给管理员看的错误文字：`ApiError.message` 已经是后端写好的中文原因 */
function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.message) return error.message
  if (error instanceof Error && error.message) return error.message
  return fallback
}

/** 分组标题，避免每个小节各写一遍排版 */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Box component="section">
      <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.75 }}>
        {title}
      </Typography>
      {children}
    </Box>
  )
}

/** 行内「标签：值」；空值也照常显示，让管理员知道卡里确实没写 */
function Field({ label, value }: { label: string; value: string }) {
  return (
    <Box sx={{ display: 'flex', gap: 1, alignItems: 'baseline', flexWrap: 'wrap' }}>
      <Typography variant="caption" color="text.secondary" sx={{ minWidth: 64 }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>
        {value || '—'}
      </Typography>
    </Box>
  )
}

/**
 * 只读长文本块
 *
 * 一律纯文本：卡片正文与控制指令是外来内容，绝不能走 HTML 或 Markdown 渲染。
 * `whiteSpace: 'pre-wrap'` 保住原换行，等宽字体让正则和制表符能看出来。
 */
function PlainTextBlock({
  text,
  maxHeight = 200,
}: {
  text: string
  maxHeight?: number
}) {
  return (
    <Box
      sx={{
        maxHeight,
        overflow: 'auto',
        p: 1.25,
        borderRadius: 1,
        border: '1px solid',
        borderColor: 'divider',
        bgcolor: 'action.hover',
      }}
    >
      <Typography
        component="pre"
        variant="caption"
        sx={{
          m: 0,
          fontFamily: 'monospace',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          lineHeight: 1.6,
        }}
      >
        {text}
      </Typography>
    </Box>
  )
}

/** 表格里的截断文本；悬停看全文，避免长正则撑破列宽 */
function EllipsisCell({ text, maxWidth = 150 }: { text: string; maxWidth?: number }) {
  if (!text) {
    return (
      <Typography variant="caption" color="text.disabled">
        —
      </Typography>
    )
  }
  return (
    <Tooltip title={text} placement="top-start">
      <Typography
        variant="caption"
        sx={{
          display: 'block',
          maxWidth,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          fontFamily: 'monospace',
        }}
      >
        {text}
      </Typography>
    </Tooltip>
  )
}

interface CardImportDialogProps {
  open: boolean
  onClose: () => void
  /** 导入目标作用域：决定人设归属与该实例上两个闸的取值 */
  scope: TavernScope
  /** 目标实例的显示名；全局视图传空串 */
  scopeLabel: string
  /** 导入成功并关闭后回调，调用方据此刷新列表、标签并弹提示 */
  onImported: (presetName: string) => void
}

/**
 * 酒馆人设卡导入对话框
 *
 * 三步：选文件 → 看报告并逐项确认 → 看结果。中间那步是重点：卡片里的系统提示词、
 * 后置指令、深度注入都是**会跟着每轮提示词发给模型**的外来文字，所以在真正写库之前
 * 把原文逐段摊开给管理员确认，而不是导入完再让人去别处找。
 */
export default function CardImportDialog({
  open,
  onClose,
  scope,
  scopeLabel,
  onImported,
}: CardImportDialogProps) {
  const { t, i18n } = useTranslation('tavern')
  const { t: tState } = useTranslation('state')
  const { t: tCommon } = useTranslation('common')
  const theme = useTheme()
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  const inputRef = useRef<HTMLInputElement | null>(null)

  const [step, setStep] = useState<'pick' | 'report' | 'result'>('pick')
  const [file, setFile] = useState<File | null>(null)
  const [report, setReport] = useState<CardInspectReport | null>(null)
  const [result, setResult] = useState<CardImportResult | null>(null)
  const [error, setError] = useState('')
  const [inspecting, setInspecting] = useState(false)
  const [importing, setImporting] = useState(false)

  // 三个指令块默认导入：卡片的语义就在这些文字里，默认关掉等于导入了一个空壳
  const [includeSystem, setIncludeSystem] = useState(true)
  const [includePostHistory, setIncludePostHistory] = useState(true)
  const [includeDepth, setIncludeDepth] = useState(true)
  const [includeBook, setIncludeBook] = useState(true)
  // 卡内书里读名字的变量默认换成宏；只换输出名字的写法，导入说明里会列出换了哪些
  const [rewriteNameVars, setRewriteNameVars] = useState(true)
  const [greetingPick, setGreetingPick] = useState<'first' | 'random'>('first')
  // 只记与卡片默认值不同的开关；提交时也只发这些序号
  const [ruleOverrides, setRuleOverrides] = useState<Record<number, boolean>>({})
  const [openContent, setOpenContent] = useState(false)
  const [openNotes, setOpenNotes] = useState(false)

  useEffect(() => {
    if (!open) return
    setStep('pick')
    setFile(null)
    setReport(null)
    setResult(null)
    setError('')
    setInspecting(false)
    setImporting(false)
    setIncludeSystem(true)
    setIncludePostHistory(true)
    setIncludeDepth(true)
    setIncludeBook(true)
    setRewriteNameVars(true)
    setGreetingPick('first')
    setRuleOverrides({})
    setOpenContent(false)
    setOpenNotes(false)
  }, [open])

  const pickFile = () => inputRef.current?.click()

  const handleFileChange = async (list: FileList | null) => {
    const next = list?.[0] ?? null
    if (!next) return
    setError('')

    const lower = next.name.toLowerCase()
    if (!lower.endsWith('.png') && !lower.endsWith('.json')) {
      setError(t('import.fileTypeRejected'))
      setFile(null)
      return
    }
    if (next.size > CARD_IMPORT_MAX_BYTES) {
      setError(t('import.fileTooLarge', { size: formatFileSize(CARD_IMPORT_MAX_BYTES) }))
      setFile(null)
      return
    }

    setFile(next)
    setInspecting(true)
    try {
      const data = await tavernApi.inspectCard(next, scope)
      setReport(data)
      setStep('report')
    } catch (err) {
      setError(errorText(err, t('errors.inspectFailed')))
      setFile(null)
    } finally {
      setInspecting(false)
    }
  }

  // 卡里有后置指令且导入时没取消勾选、目标实例上槽位是关的：导入确认里问要不要一起打开
  const askSlots = Boolean(
    report &&
      report.post_history_instructions &&
      includePostHistory &&
      report.target.can_enable_advanced_slots
  )

  const handleImport = async (enableSlots: boolean) => {
    if (!file || !report) {
      setError(t('import.noFile'))
      return
    }
    setImporting(true)
    setError('')
    try {
      const data = await tavernApi.importCard(
        file,
        {
          include_system_prompt: includeSystem,
          include_post_history: includePostHistory,
          include_depth_prompt: includeDepth,
          include_book: includeBook,
          rewrite_name_vars: rewriteNameVars,
          greeting_pick: greetingPick,
          // 空对象也照发：后端把「没出现的键」当默认值，语义一致
          rule_enabled: ruleOverrides,
          enable_advanced_slots: enableSlots,
        },
        scope
      )
      setResult(data)
      setStep('result')
    } catch (err) {
      setError(errorText(err, t('errors.importFailed')))
    } finally {
      setImporting(false)
    }
  }

  // 结果页关掉就算导入完成：不管按的是「完成」还是右上角的叉，都要刷新列表并提示
  const handleClose = () => {
    if (importing || inspecting) return
    if (step === 'result') onImported(report?.name ?? '')
    onClose()
  }

  const toggleRule = (rule: CleanupRuleInspect, next: boolean) => {
    setRuleOverrides(prev => {
      const copy = { ...prev }
      if (next === rule.enabled) {
        delete copy[rule.index]
      } else {
        copy[rule.index] = next
      }
      return copy
    })
  }

  const ruleValue = (rule: CleanupRuleInspect) => ruleOverrides[rule.index] ?? rule.enabled
  const ruleSwitchable = (rule: CleanupRuleInspect) =>
    rule.enabled || ENABLEABLE_REASONS.has(rule.disabled_reason)

  const reasonText = (reason: string) =>
    reason ? t(`rule.reason.${reason}`, { defaultValue: reason }) : ''

  /**
   * 角色状态模板这一节
   *
   * 检查页与结果页各用一次：`status` 说这一步发生了什么，`fields` 只在真的推出字段时
   * 才有意义，`note` 是后端写好的补充说明。整个字段为空表示插件没登记或卡里没有书，
   * 这时整节不出现 —— 画一个空标题只会让人以为漏了什么。
   */
  const renderStateTemplate = (stateTemplate: StateTemplateReport | null | undefined) => {
    if (!stateTemplate) return null
    const privatePaths = stateTemplate.private_paths ?? []
    return (
      <Section title={tState('import.title')}>
        <Stack spacing={0.5}>
          <Typography variant="body2">
            {tState(`import.status.${stateTemplate.status}`, { defaultValue: stateTemplate.status })}
          </Typography>
          {stateTemplate.fields > 0 ? (
            <Typography variant="body2">
              {tState('import.fields', {
                fields: stateTemplate.fields,
                hidden: stateTemplate.hidden,
                entries: stateTemplate.entries,
              })}
            </Typography>
          ) : null}
          {privatePaths.length > 0 ? (
            <Box>
              <Typography variant="caption" color="text.secondary">
                {tState('import.privatePaths')}
              </Typography>
              <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap', mt: 0.5 }}>
                {/* 路径是模板里写进来的文字，一律纯文本 */}
                {privatePaths.map(path => (
                  <Chip key={path} size="small" label={path} sx={CHIP_VARIANTS.base(isSmall)} />
                ))}
              </Box>
            </Box>
          ) : null}
          {stateTemplate.note ? <Alert severity="info">{stateTemplate.note}</Alert> : null}
        </Stack>
      </Section>
    )
  }

  const bookNoteEntries = report
    ? Object.entries(report.book.notes).filter(([, n]) => n !== 0)
    : []

  const renderOffChannels = (channels: SlotsOffChannel[]) => (
    <Stack component="ul" spacing={0.25} sx={{ m: 0, mt: 0.5, pl: 2.5 }}>
      {channels.map(channel => (
        <Typography component="li" variant="body2" key={channel.chat_key} sx={{ wordBreak: 'break-all' }}>
          {channel.channel_name ? `${channel.channel_name}（${channel.chat_key}）` : channel.chat_key}
        </Typography>
      ))}
    </Stack>
  )

  // 与配置表单打开这一项时弹的确认文案同一份；取不到当前语言回落到中文
  const slotsConfirmText = (confirm: Record<string, string>) =>
    getLocalizedText(confirm as I18nDict, confirm['zh-CN'] ?? '', i18n.language)

  const renderSlotsAsk = () => {
    if (!report || !askSlots) return null
    const gates = report.target
    return (
      <Alert severity="warning">
        <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 0.5 }}>
          {t('import.slotsAskTitle')}
        </Typography>
        <Stack spacing={0.75}>
          <Typography variant="body2">{t('import.slotsAskBody')}</Typography>
          <Typography variant="body2">{slotsConfirmText(gates.advanced_slots_enable_confirm)}</Typography>
          <Typography variant="body2">
            {gates.advanced_slots_global_enabled
              ? t('import.slotsAskInstanceLevel')
              : t('import.slotsAskGlobalOff')}
          </Typography>
          {gates.advanced_slots_off_channels.length > 0 ? (
            <Box>
              <Typography variant="body2">{t('import.slotsAskChannelsOff')}</Typography>
              {renderOffChannels(gates.advanced_slots_off_channels)}
            </Box>
          ) : null}
        </Stack>
      </Alert>
    )
  }

  const renderSlotsOutcome = () => {
    if (!result) return null
    const channels = result.advanced_slots_off_channels
    if (result.advanced_slots === 'failed') {
      return (
        <Alert severity="warning">
          {t('import.slotsResultFailed', { reason: result.advanced_slots_error })}
        </Alert>
      )
    }
    if (result.advanced_slots !== 'enabled' && result.advanced_slots !== 'already_enabled') return null
    return (
      <Alert severity={result.advanced_slots === 'enabled' ? 'success' : 'info'}>
        <Typography variant="body2">
          {result.advanced_slots === 'enabled'
            ? t('import.slotsResultEnabled')
            : t('import.slotsResultAlready')}
        </Typography>
        {channels.length > 0 ? (
          <Box sx={{ mt: 0.5 }}>
            <Typography variant="body2">{t('import.slotsResultChannelsOff')}</Typography>
            {renderOffChannels(channels)}
          </Box>
        ) : null}
      </Alert>
    )
  }

  const renderPick = () => (
    <Stack spacing={2}>
      <input
        ref={inputRef}
        type="file"
        accept=".png,.json"
        style={{ display: 'none' }}
        onChange={event => {
          void handleFileChange(event.target.files)
          event.target.value = ''
        }}
      />
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <ActionButton tone="secondary" onClick={pickFile} disabled={inspecting}>
          {t('import.chooseFile')}
        </ActionButton>
        {file ? (
          <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
            {file.name} · {formatFileSize(file.size)}
          </Typography>
        ) : null}
        {inspecting ? (
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
            <CircularProgress size={16} />
            <Typography variant="caption" color="text.secondary">
              {t('import.inspecting')}
            </Typography>
          </Stack>
        ) : null}
      </Stack>
      <Typography variant="caption" color="text.secondary">
        {t('import.fileHint', { size: formatFileSize(CARD_IMPORT_MAX_BYTES) })}
      </Typography>
      {error ? <Alert severity="error">{error}</Alert> : null}
    </Stack>
  )

  const renderInstructions = () => {
    if (!report) return null
    const blocks: React.ReactNode[] = []

    if (report.system_prompt) {
      blocks.push(
        <Box key="system">
          <FormControlLabel
            control={
              <Checkbox checked={includeSystem} onChange={e => setIncludeSystem(e.target.checked)} />
            }
            label={t('import.includeSystemPrompt')}
          />
          <PlainTextBlock text={report.system_prompt} />
        </Box>
      )
    }
    if (report.post_history_instructions) {
      blocks.push(
        <Box key="post_history">
          <FormControlLabel
            control={
              <Checkbox
                checked={includePostHistory}
                onChange={e => setIncludePostHistory(e.target.checked)}
              />
            }
            label={t('import.includePostHistory')}
          />
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.75 }}>
            {report.target.advanced_slots_enabled
              ? t('import.postHistoryGateOn')
              : t('import.postHistoryGateOff')}
          </Typography>
          <PlainTextBlock text={report.post_history_instructions} />
        </Box>
      )
    }
    if (report.depth_prompt.text) {
      blocks.push(
        <Box key="depth">
          <FormControlLabel
            control={
              <Checkbox checked={includeDepth} onChange={e => setIncludeDepth(e.target.checked)} />
            }
            label={`${t('import.includeDepthPrompt')}（${t('import.depthValue', {
              depth: report.depth_prompt.depth,
            })}）`}
          />
          <PlainTextBlock text={report.depth_prompt.text} />
        </Box>
      )
    }

    if (blocks.length === 0) return null
    return (
      <Section title={t('import.instructions')}>
        <Alert severity="info" sx={{ mb: 1 }}>
          {t('import.instructionsNotice')}
        </Alert>
        <Stack spacing={1.5}>{blocks}</Stack>
      </Section>
    )
  }

  const renderRules = () => {
    if (!report) return null
    if (report.cleanup_rules.length === 0) {
      return (
        <Section title={t('import.rules')}>
          <Typography variant="body2" color="text.secondary">
            {t('import.rulesEmpty')}
          </Typography>
        </Section>
      )
    }
    return (
      <Section title={t('import.rules')}>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
          {t('import.rulesHint')}
        </Typography>
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small" sx={{ minWidth: 620 }}>
            <TableHead>
              <TableRow>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.ruleIndex')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.ruleName')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.ruleFind')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.ruleReplace')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.ruleEnabled')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.ruleDisabledReason')}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {report.cleanup_rules.map(rule => {
                const switchable = ruleSwitchable(rule)
                return (
                  <TableRow key={rule.index}>
                    {/* 给人看的序号从 1 起，与导入结果里「第 N 条」一致；提交仍用卡里的原序号 */}
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>{rule.index + 1}</TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <EllipsisCell text={rule.name} maxWidth={120} />
                      {rule.html_kind ? (
                        <Tooltip title={t('import.htmlKindHint')}>
                          <Chip
                            size="small"
                            label={t(HTML_KIND_LABEL_KEYS[rule.html_kind])}
                            sx={{
                              ...CHIP_VARIANTS.getCustomColorChip(theme.palette.warning.main, true),
                              mt: 0.5,
                            }}
                          />
                        </Tooltip>
                      ) : null}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <EllipsisCell text={rule.find} />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <EllipsisCell text={rule.replace} />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <Tooltip title={switchable ? '' : reasonText(rule.disabled_reason)}>
                        <span>
                          <Switch
                            size="small"
                            checked={ruleValue(rule)}
                            disabled={!switchable}
                            onChange={e => toggleRule(rule, e.target.checked)}
                            inputProps={{ 'aria-label': `${t('import.ruleEnabled')} ${rule.index + 1}` }}
                          />
                        </span>
                      </Tooltip>
                      {/* 启用后不是按替换文字的，处理方式决定输出什么，比正则本身更值得先看到 */}
                      {ruleValue(rule) && rule.output_mode !== 'replace' ? (
                        <Typography variant="caption" color="text.secondary" display="block">
                          {t(OUTPUT_MODE_LABEL_KEYS[rule.output_mode])}
                        </Typography>
                      ) : null}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <Typography variant="caption" color="text.secondary">
                        {rule.disabled_reason ? reasonText(rule.disabled_reason) : '—'}
                      </Typography>
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </Box>
      </Section>
    )
  }

  /**
   * 卡片脚本要下载的远程世界书
   *
   * 报告页只列认出的来源，不给操作：这时候人设还没建出来，导入不导入是下一步的事。
   * 导入完成后结果页才给一份带按钮的清单。
   */
  const renderRemoteBooks = () => {
    if (!report || report.remote_books.length === 0) return null
    return (
      <Section title={t('import.remoteBooks')}>
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small" sx={{ minWidth: 620 }}>
            <TableHead>
              <TableRow>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('remoteBooks.fileName')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('remoteBooks.host')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('remoteBooks.scripts')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>
                  {t('remoteBooks.versionHint')}
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {report.remote_books.map(book => (
                <TableRow key={book.key}>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                    <Typography variant="caption" sx={{ wordBreak: 'break-all' }}>
                      {book.file_name}
                    </Typography>
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                    <Typography variant="caption" sx={{ wordBreak: 'break-all' }}>
                      {book.urls.length > 0 ? hostOf(book.urls[0]) : '—'}
                      {book.urls.length > 1
                        ? ` · ${t('remoteBooks.mirrors', { n: book.urls.length - 1 })}`
                        : ''}
                    </Typography>
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                    <Typography variant="caption" sx={{ wordBreak: 'break-word' }}>
                      {book.scripts.length > 0 ? book.scripts.join(', ') : '—'}
                    </Typography>
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                    <Typography variant="caption" color="text.secondary">
                      {book.version_hint || '—'}
                    </Typography>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
          {t('import.remoteBooksHint')}
        </Typography>
      </Section>
    )
  }

  const renderReport = () => {
    if (!report) return null
    const verdict = report.verdict
    const verdictSeverity =
      verdict?.level === 'state' ? 'warning' : verdict?.level === 'partial' ? 'info' : 'success'
    return (
      <Stack spacing={2.5}>
        {verdict?.text ? <Alert severity={verdictSeverity}>{verdict.text}</Alert> : null}
        <Section title={t('import.cardInfo')}>
          <Stack spacing={0.5}>
            <Field label={t('import.name')} value={report.name} />
            <Field label={t('import.nickname')} value={report.nickname} />
            <Field label={t('import.creator')} value={report.creator} />
            <Field label={t('import.version')} value={report.character_version} />
            <Field
              label={t('import.spec')}
              value={`${report.spec.toUpperCase()} · ${report.source_format}`}
            />
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'baseline', flexWrap: 'wrap' }}>
              <Typography variant="caption" color="text.secondary" sx={{ minWidth: 64 }}>
                {t('import.tags')}
              </Typography>
              {report.tags.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  {t('import.noTags')}
                </Typography>
              ) : (
                <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
                  {report.tags.map(tag => (
                    <Chip
                      key={tag}
                      label={tag}
                      size="small"
                      sx={{ ...CHIP_VARIANTS.base(isSmall), maxWidth: 180 }}
                    />
                  ))}
                </Box>
              )}
            </Box>
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'baseline', flexWrap: 'wrap' }}>
              <Typography variant="caption" color="text.secondary" sx={{ minWidth: 64 }}>
                {t('import.avatar')}
              </Typography>
              <Typography variant="body2">
                {t(`import.avatarSource.${report.avatar_source}`, {
                  defaultValue: report.avatar_source,
                })}
                {report.avatar_note ? ` · ${report.avatar_note}` : ''}
              </Typography>
            </Box>
          </Stack>

          {report.creator_notes ? (
            <Box sx={{ mt: 1 }}>
              <ActionButton
                tone="ghost"
                size="small"
                onClick={() => setOpenNotes(v => !v)}
                sx={{ textTransform: 'none' }}
              >
                {t('import.creatorNotes')}
              </ActionButton>
              <Collapse in={openNotes} unmountOnExit>
                <Box sx={{ mt: 1 }}>
                  <PlainTextBlock text={report.creator_notes} maxHeight={160} />
                </Box>
              </Collapse>
            </Box>
          ) : null}
        </Section>

        <Section title={t('import.content')}>
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
            <Typography variant="body2">
              {t('import.contentChars', { n: report.assembled_chars })}
            </Typography>
            <ActionButton
              tone="ghost"
              size="small"
              onClick={() => setOpenContent(v => !v)}
              sx={{ textTransform: 'none' }}
            >
              {openContent ? t('import.hideContent') : t('import.showContent')}
            </ActionButton>
          </Stack>
          {report.long_content ? (
            <Alert severity="warning" sx={{ mt: 1 }}>
              {t('import.longContent')}
            </Alert>
          ) : null}
          <Collapse in={openContent} unmountOnExit>
            <Box sx={{ mt: 1 }}>
              <PlainTextBlock text={report.assembled_content} maxHeight={240} />
            </Box>
          </Collapse>
        </Section>

        {renderInstructions()}

        <Section title={t('import.greetings')}>
          <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
            <Typography variant="body2">
              {t('import.greetingsGeneral', { n: report.greetings_count })}
            </Typography>
            <Typography variant="body2">
              {t('import.greetingsGroup', { n: report.group_greetings_count })}
            </Typography>
          </Stack>
          {report.greetings_count + report.group_greetings_count === 0 ? (
            <Typography variant="caption" color="text.secondary">
              {t('import.noGreetings')}
            </Typography>
          ) : (
            <FormControl sx={{ mt: 1 }}>
              <FormLabel sx={{ fontSize: '0.75rem' }}>{t('import.greetingPick')}</FormLabel>
              <RadioGroup
                row
                value={greetingPick}
                onChange={e => setGreetingPick(e.target.value as 'first' | 'random')}
              >
                <FormControlLabel
                  value="first"
                  control={<Radio size="small" />}
                  label={t('import.greetingPickFirst')}
                />
                <FormControlLabel
                  value="random"
                  control={<Radio size="small" />}
                  label={t('import.greetingPickRandom')}
                />
              </RadioGroup>
            </FormControl>
          )}
        </Section>

        <Section title={t('import.book')}>
          {!report.book.present ? (
            <Typography variant="body2" color="text.secondary">
              {t('import.bookAbsent')}
            </Typography>
          ) : (
            <Stack spacing={1}>
              <Stack direction="row" spacing={1.5} sx={{ flexWrap: 'wrap' }}>
                <Typography variant="body2">
                  {t('import.bookEntries', { n: report.book.entries })}
                </Typography>
                {report.book.invalid_keys > 0 ? (
                  <Typography variant="body2">
                    {t('import.bookInvalidKeys', { n: report.book.invalid_keys })}
                  </Typography>
                ) : null}
                {report.book.decorated > 0 ? (
                  <Typography variant="body2">
                    {t('import.bookDecorated', { n: report.book.decorated })}
                  </Typography>
                ) : null}
              </Stack>
              {bookNoteEntries.length > 0 ? (
                <Box>
                  <Typography variant="caption" color="text.secondary">
                    {t('import.bookNotes')}
                  </Typography>
                  <Stack direction="row" spacing={1.5} sx={{ flexWrap: 'wrap' }}>
                    {bookNoteEntries.map(([kind, n]) => (
                      <Typography key={kind} variant="caption">
                        {kind}: {n}
                      </Typography>
                    ))}
                  </Stack>
                </Box>
              ) : null}
              <FormControlLabel
                control={
                  <Checkbox checked={includeBook} onChange={e => setIncludeBook(e.target.checked)} />
                }
                label={t('import.includeBook')}
              />
              <FormControlLabel
                control={
                  <Checkbox
                    checked={rewriteNameVars}
                    onChange={e => setRewriteNameVars(e.target.checked)}
                  />
                }
                label={t('import.rewriteNameVars', {
                  userMacro: '{{user}}',
                  charMacro: '{{char}}',
                })}
              />
              <Typography variant="caption" color="text.secondary">
                {t('import.rewriteNameVarsHint')}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {report.target.worldbook_enabled
                  ? t('import.bookGateOn')
                  : t('import.bookGateOff')}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {t('import.bookReferenceHint')}
              </Typography>
            </Stack>
          )}
        </Section>

        {renderRules()}

        {renderRemoteBooks()}

        {renderStateTemplate(report.state_template)}

        {report.unsupported.length > 0 ? (
          <Section title={t('import.unsupported')}>
            <Stack component="ul" spacing={0.5} sx={{ m: 0, pl: 2.5 }}>
              {report.unsupported.map((item, index) => (
                <Typography component="li" variant="body2" key={`unsupported-${index}`}>
                  {item.detail}
                </Typography>
              ))}
            </Stack>
          </Section>
        ) : null}

        {report.notes.length > 0 ? (
          <Section title={t('import.notes')}>
            <Stack component="ul" spacing={0.5} sx={{ m: 0, pl: 2.5 }}>
              {report.notes.map((item, index) => (
                <Typography component="li" variant="body2" key={`note-${index}`}>
                  {item.detail}
                </Typography>
              ))}
            </Stack>
          </Section>
        ) : null}

        <Section title={t('import.gates')}>
          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }}>
            <Chip
              size="small"
              sx={{ ...CHIP_VARIANTS.base(isSmall) }}
              label={`${t('import.gateWorldbook')}: ${
                report.target.worldbook_enabled ? t('import.gateOn') : t('import.gateOff')
              }`}
            />
            <Chip
              size="small"
              sx={{ ...CHIP_VARIANTS.base(isSmall) }}
              label={`${t('import.gateAdvancedSlots')}: ${
                report.target.advanced_slots_enabled ? t('import.gateOn') : t('import.gateOff')
              }`}
            />
          </Stack>
        </Section>

        {renderSlotsAsk()}

        {error ? <Alert severity="error">{error}</Alert> : null}
      </Stack>
    )
  }

  const renderResult = () => {
    if (!result) return null
    return (
      <Stack spacing={2}>
        <Typography variant="subtitle1" fontWeight={600}>
          {t('import.resultTitle')}
        </Typography>
        {renderSlotsOutcome()}
        {result.notes.length > 0 ? (
          <Section title={t('import.resultNotes')}>
            <Stack component="ul" spacing={0.5} sx={{ m: 0, pl: 2.5 }}>
              {result.notes.map((note, index) => (
                <Typography component="li" variant="body2" key={`result-note-${index}`}>
                  {note}
                </Typography>
              ))}
            </Stack>
          </Section>
        ) : null}
        {result.book_report ? (
          <Section title={t('import.resultBook')}>
            <Typography variant="body2">
              {t('import.resultBookCreated', { n: result.book_report.created })}
            </Typography>
            {result.book_report.details.length > 0 ? (
              <Box sx={{ overflowX: 'auto', mt: 1, maxHeight: 220 }}>
                <Table size="small" sx={{ minWidth: 520 }}>
                  <TableHead>
                    <TableRow>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header}>
                        {t('import.ruleIndex')}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.ruleName')}</TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.header}>
                        {t('import.notes')}
                      </TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {result.book_report.details.map((detail, index) => (
                      <TableRow key={`book-detail-${index}`}>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>{index + 1}</TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                          <EllipsisCell text={detail.entry_key} maxWidth={120} />
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>{detail.detail}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            ) : null}
          </Section>
        ) : null}
        {/* 模板是在导入时按卡里的书生成的，这里才第一次能看到它落到哪一步 */}
        {renderStateTemplate(result.state_template)}
        {/* 人设已经建出来，这里给的清单才能直接把书导进它名下 */}
        {result.remote_books.length > 0 ? (
          <RemoteBooksSection presetId={result.preset_id} scope={scope} />
        ) : null}
      </Stack>
    )
  }

  const actions =
    step === 'pick' ? (
      <ActionButton tone="secondary" onClick={handleClose} disabled={inspecting}>
        {tCommon('actions.cancel')}
      </ActionButton>
    ) : step === 'report' ? (
      <>
        <ActionButton tone="ghost" onClick={() => setStep('pick')} disabled={importing}>
          {t('import.back')}
        </ActionButton>
        {askSlots ? (
          <>
            <ActionButton tone="secondary" onClick={() => void handleImport(false)} disabled={importing}>
              {t('import.importOnly')}
            </ActionButton>
            <ActionButton tone="primary" onClick={() => void handleImport(true)} disabled={importing}>
              {importing ? <CircularProgress size={20} /> : t('import.enableAndImport')}
            </ActionButton>
          </>
        ) : (
          <ActionButton tone="primary" onClick={() => void handleImport(false)} disabled={importing}>
            {importing ? <CircularProgress size={20} /> : t('import.confirmImport')}
          </ActionButton>
        )}
      </>
    ) : (
      <ActionButton tone="primary" onClick={handleClose}>
        {t('import.done')}
      </ActionButton>
    )

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      maxWidth="md"
      fullWidth
      fullScreen={isSmall}
      scroll="paper"
      PaperProps={{
        sx: {
          // 全屏时也保持 NA 原生的毛玻璃卡片底色（与其它对话框一致），不另换成不透明底
          ...CARD_VARIANTS.default.styles,
          ...(isSmall ? {} : { maxHeight: '88vh' }),
        },
      }}
    >
      <DialogTitle
        sx={{
          px: 3,
          py: 2,
          display: 'flex',
          alignItems: 'center',
          gap: 1,
          borderBottom: '1px solid',
          borderColor: 'divider',
          bgcolor: 'action.hover',
        }}
      >
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="h6" component="span">
            {t('import.title')}
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
            {scope.instance_key
              ? scopeLabel
                ? t('import.scopeInstance', { instance: scopeLabel })
                : t('import.scopeUnknown')
              : t('import.scopeGlobal')}
          </Typography>
        </Box>
        <IconActionButton
          size="small"
          onClick={handleClose}
          edge="end"
          aria-label={tCommon('actions.close')}
          disabled={importing || inspecting}
        >
          <CloseIcon />
        </IconActionButton>
      </DialogTitle>
      <DialogContent dividers sx={{ p: 2.5 }}>
        {step === 'pick' ? renderPick() : step === 'report' ? renderReport() : renderResult()}
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>{actions}</DialogActions>
    </Dialog>
  )
}
