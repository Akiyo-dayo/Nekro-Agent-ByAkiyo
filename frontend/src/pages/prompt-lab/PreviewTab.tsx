import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Chip,
  Stack,
  type SxProps,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  type Theme,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import QueryErrorState from '../../components/common/QueryErrorState'
import SegmentedControl from '../../components/common/SegmentedControl'
import { rpWhereLabelKey } from '../../components/rp-preset/segments'
import { useWorldbookScopeOptions } from '../../components/worldbook/useWorldbookScopeOptions'
import { useScrollSpy } from '../../hooks/useScrollSpy'
import { presetsApi } from '../../services/api/presets'
import { rpPresetsApi } from '../../services/api/rpPresets'
import {
  tavernApi,
  type TavernPreviewMessage,
  type TavernPreviewResponse,
  type TavernRpPresetMeta,
} from '../../services/api/tavern'
import type { WorldbookScopeParams } from '../../services/api/worldbook'
import {
  CHIP_VARIANTS,
  FLOATING_LABEL_GUTTER,
  SCROLL_COLUMN_STYLES,
  UNIFIED_TABLE_STYLES,
} from '../../theme/variants'

/**
 * 「预览」标签
 *
 * 预览只读：不跑记忆召回、不调插件的动态注入、不调模型、不写任何记录。页面顶部那条
 * 说明是固定文案，不能因为「今天看起来没问题」而省掉 —— 管理员靠它判断哪些段落
 * 与真实一轮不同。
 *
 * 返回的文字一律按纯文本放进等宽块，不经过 Markdown / HTML 渲染。
 */

const SAMPLE_MAX_CHARS = 2000

interface PromptLabTabProps {
  scope: WorldbookScopeParams
  isScopeReady: boolean
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function slotOf(
  meta: Record<string, unknown>,
  group: 'slots' | 'slots_preview',
  key: string
): { text: string; layers: string[] } {
  const slots = asRecord(meta[group])
  const slot = slots ? asRecord(slots[key]) : null
  return {
    text: slot && typeof slot.text === 'string' ? slot.text : '',
    layers: slot && Array.isArray(slot.layers)
      ? slot.layers.filter((layer): layer is string => typeof layer === 'string')
      : [],
  }
}

/**
 * `meta.rp_preset`
 *
 * 后端只在真的按预设排布过时给这个键；形状按行取，缺哪项都由空值兜底，避免一次
 * 字段调整把整张预览卡打成异常。
 */
function rpPresetOf(meta: Record<string, unknown>): TavernRpPresetMeta | null {
  const raw = asRecord(meta.rp_preset)
  if (!raw) return null
  const used = Array.isArray(raw.used)
    ? raw.used.flatMap(item => {
        const record = asRecord(item)
        if (!record) return []
        return [
          {
            identifier: typeof record.identifier === 'string' ? record.identifier : '',
            name: typeof record.name === 'string' ? record.name : '',
            where: typeof record.where === 'string' ? record.where : '',
            chars: typeof record.chars === 'number' ? record.chars : 0,
          },
        ]
      })
    : []
  return {
    id: typeof raw.id === 'number' ? raw.id : 0,
    name: typeof raw.name === 'string' ? raw.name : '',
    used,
    prefill_text: typeof raw.prefill_text === 'string' ? raw.prefill_text : '',
    prefill_sent: raw.prefill_sent === true,
  }
}

const SLOT_KEYS = ['post_history', 'auxiliary', 'prefill'] as const

const SLOT_LABEL_KEYS: Record<(typeof SLOT_KEYS)[number], string> = {
  post_history: 'rounds.slot.postHistory',
  auxiliary: 'rounds.slot.auxiliary',
  prefill: 'rounds.slot.prefill',
}

const LAYER_LABEL_KEYS: Record<string, string> = {
  system: 'rounds.layer.system',
  adapter: 'rounds.layer.adapter',
  instance: 'rounds.layer.instance',
  channel: 'rounds.layer.channel',
  preset: 'rounds.layer.preset',
}

const PREFILL_REASON_KEYS: Record<string, string> = {
  ready: 'lab.preview.prefillReason.ready',
  sent: 'lab.preview.prefillReason.ready',
  gated: 'lab.preview.prefillReason.gated',
  empty: 'lab.preview.prefillReason.empty',
  unsupported: 'lab.preview.prefillReason.unsupported',
  model_group_off: 'lab.preview.prefillReason.modelGroupOff',
  unavailable: 'lab.preview.prefillReason.unavailable',
}

type PreviewSection = 'overview' | 'rp' | 'greeting' | 'slots' | 'messages'

/** 预览结果的锚点：宽屏左侧目录，窄屏顶部一组选项 */
const PREVIEW_SECTIONS: { key: PreviewSection; labelKey: string }[] = [
  { key: 'overview', labelKey: 'lab.preview.nav.overview' },
  { key: 'greeting', labelKey: 'lab.preview.nav.greeting' },
  { key: 'slots', labelKey: 'lab.preview.nav.slots' },
  { key: 'messages', labelKey: 'lab.preview.nav.messages' },
]

/** 按预设排布是概览区里的一张卡，只有后端真的排过时才出现在目录里 */
const RP_SECTION: { key: PreviewSection; labelKey: string } = {
  key: 'rp',
  labelKey: 'lab.preview.nav.rp',
}

function MessageBlock({
  message,
  label,
}: {
  message: TavernPreviewMessage
  label: string
}) {
  const { t } = useTranslation('tavern')
  const defaultOpen = message.source === 'history'
  const [open, setOpen] = useState(defaultOpen)

  // 新一次预览会复用同一批下标；文字换了就回到这一段的默认折叠状态，
  // 否则上一次展开的系统提示词会让新结果看上去只显示了一半。
  useEffect(() => {
    setOpen(defaultOpen)
  }, [defaultOpen, message.text])

  return (
    <Box
      sx={{
        border: '1px solid',
        borderColor: 'divider',
        borderRadius: 1,
        overflow: 'hidden',
      }}
    >
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 1,
          flexWrap: 'wrap',
          px: 1.5,
          py: 1,
          bgcolor: 'action.hover',
        }}
      >
        <Chip size="small" label={label} sx={CHIP_VARIANTS.base(false)} />
        <Typography variant="caption" color="text.secondary">
          {t('lab.preview.messageRole', { role: message.role })}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {t('lab.preview.messageChars', { count: message.text.length })}
        </Typography>
        <ActionButton
          tone="ghost"
          size="small"
          sx={{ ml: 'auto' }}
          onClick={() => setOpen(value => !value)}
        >
          {open ? t('lab.preview.collapse') : t('lab.preview.expand')}
        </ActionButton>
      </Box>
      {open && (
        <Box
          component="pre"
          sx={{
            m: 0,
            p: 1.5,
            maxHeight: 360,
            overflowY: 'auto',
            overflowX: 'auto',
            fontFamily: 'monospace',
            fontSize: '0.8rem',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {message.text || t('lab.preview.emptyText')}
        </Box>
      )}
    </Box>
  )
}

function PreviewResult({ result }: { result: TavernPreviewResponse }) {
  const theme = useTheme()
  const isWide = useMediaQuery(theme.breakpoints.up('md'))
  const { t } = useTranslation('tavern')
  const sectionRefs = useRef<Record<PreviewSection, HTMLDivElement | null>>({
    overview: null,
    rp: null,
    greeting: null,
    slots: null,
    messages: null,
  })

  const meta = result.meta
  const rpPreset = rpPresetOf(meta)
  const rpInactiveReason =
    typeof meta.rp_preset_inactive_reason === 'string' ? meta.rp_preset_inactive_reason : ''
  const sections = rpPreset
    ? [PREVIEW_SECTIONS[0], RP_SECTION, ...PREVIEW_SECTIONS.slice(1)]
    : PREVIEW_SECTIONS
  const sectionKeys = sections.map(section => section.key)
  const { active: activeSection, lockTo } = useScrollSpy(sectionKeys, key => sectionRefs.current[key])
  // 宽屏目录与窄屏分段选择共用这一个值；观察器还没跑起来时先按概览高亮
  const currentSection: PreviewSection = activeSection ?? 'overview'
  const rpSystemCount = rpPreset?.used.filter(item => item.where === 'system').length ?? 0
  const rpTailCount = rpPreset?.used.filter(item => item.where === 'tail').length ?? 0
  const rpDepthCount =
    rpPreset?.used.filter(item => item.where.startsWith('depth:')).length ?? 0
  const slots = asRecord(meta.slots)
  const slotsEnabled = slots?.enabled === true
  const personaChars = typeof meta.persona_chars === 'number' ? meta.persona_chars : 0
  const depth = asRecord(meta.depth_prompt)
  const depthText = depth && typeof depth.text === 'string' ? depth.text : ''
  const depthValue = depth && typeof depth.depth === 'number' ? depth.depth : 0
  const worldbook = asRecord(meta.worldbook)
  const afterPersona = Array.isArray(worldbook?.after_persona) ? worldbook?.after_persona : []
  const beforeHistory = Array.isArray(worldbook?.before_history) ? worldbook?.before_history : []
  const atDepth = Array.isArray(worldbook?.at_depth) ? worldbook?.at_depth : []
  const selectedCount = afterPersona.length + beforeHistory.length + atDepth.length

  const greeting = result.greeting
  const greetingWillSend = greeting.will_send === true
  const greetingText = typeof greeting.text === 'string' ? greeting.text : ''

  const sourceLabel = (message: TavernPreviewMessage, index: number) => {
    if (message.source === 'example') {
      const number =
        result.messages.slice(0, index + 1).filter(item => item.source === 'example').length
      return t('lab.preview.sourceExample', { n: number })
    }
    if (message.source === 'history') return t('lab.preview.sourceHistory')
    return t('lab.preview.sourceSystem')
  }

  const layerLabel = (layer: string) => {
    const key = LAYER_LABEL_KEYS[layer]
    return key ? t(key) : layer
  }

  const slotBlock = (key: (typeof SLOT_KEYS)[number]) => {
    const group = slotsEnabled ? 'slots' : 'slots_preview'
    const slot = slotOf(meta, group, key)
    return (
      <Box key={key} sx={{ minWidth: 0 }}>
        <Typography variant="body2" fontWeight={600}>
          {t(SLOT_LABEL_KEYS[key])}
        </Typography>
        <Typography variant="caption" color="text.secondary" display="block">
          {slot.layers.length > 0
            ? t('lab.preview.layers', { layers: slot.layers.map(layerLabel).join(' / ') })
            : t('lab.preview.noLayers')}
        </Typography>
        <Box
          component="pre"
          sx={{
            m: 0,
            mt: 0.5,
            p: 1,
            borderRadius: 1,
            bgcolor: theme.palette.action.hover,
            fontFamily: 'monospace',
            fontSize: '0.8rem',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            overflowX: 'auto',
            maxHeight: 240,
            overflowY: 'auto',
          }}
        >
          {slot.text || t('lab.preview.emptyText')}
        </Box>
      </Box>
    )
  }

  const prefillReasonKey = PREFILL_REASON_KEYS[result.prefill.reason]
  const prefillReason = prefillReasonKey
    ? t(prefillReasonKey)
    : result.prefill.reason

  const sectionLabel = (key: PreviewSection) =>
    t(sections.find(section => section.key === key)?.labelKey ?? key)

  const rpWhereLabel = (where: string) => {
    const label = rpWhereLabelKey(where)
    if (!label.key) return where
    return label.depth === null ? t(label.key) : t(label.key, { depth: label.depth })
  }

  const goToSection = (key: PreviewSection) => {
    // 平滑滚动途中会依次经过中间的段落，先把高亮钉在目标项上再滚
    lockTo(key)
    sectionRefs.current[key]?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <Box>
      {!isWide && (
        <Box sx={{ mb: 1.5 }}>
          <SegmentedControl
            value={currentSection}
            options={sections.map(section => ({
              value: section.key,
              label: t(section.labelKey),
            }))}
            onChange={value => goToSection(value as PreviewSection)}
            fullWidth
          />
        </Box>
      )}
      <Box sx={{ display: 'flex', gap: 2, alignItems: 'flex-start' }}>
        {isWide && (
          <Box
            component="nav"
            sx={{ width: 160, flexShrink: 0, position: 'sticky', top: 0 }}
          >
            <Stack spacing={0.5}>
              {sections.map(section => (
                <ActionButton
                  key={section.key}
                  tone={currentSection === section.key ? 'secondary' : 'ghost'}
                  size="small"
                  fullWidth
                  onClick={() => goToSection(section.key)}
                  sx={{ justifyContent: 'flex-start' }}
                >
                  {t(section.labelKey)}
                </ActionButton>
              ))}
            </Stack>
          </Box>
        )}
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Stack spacing={2}>
            <Box
              ref={node => {
                sectionRefs.current.overview = node as HTMLDivElement | null
              }}
              sx={{ scrollMarginTop: 8 }}
            >
              <Typography variant="subtitle2" fontWeight={700}>
                {sectionLabel('overview')}
              </Typography>
              <Stack spacing={1.5} sx={{ mt: 1 }}>
                <Alert severity="info">{t('lab.preview.notice')}</Alert>
                {/* 协议端没连上时后端用占位代替 Bot 自己的账号；不说明的话管理员会以为那就是真值 */}
                {meta.bot_offline === true ? (
                  <Alert severity="warning">{t('lab.preview.botOffline')}</Alert>
                ) : null}
                {rpInactiveReason === 'slots_off' ||
                rpInactiveReason === 'tavern_off' ||
                rpInactiveReason === 'unavailable' ? (
                  <Alert severity="warning">
                    {t(`lab.preview.rpInactive.${rpInactiveReason}`)}
                  </Alert>
                ) : null}

      <Box
        sx={{
          p: 1.5,
          borderRadius: 1,
          border: '1px solid',
          borderColor: 'divider',
          display: 'flex',
          flexDirection: 'column',
          gap: 0.5,
        }}
      >
        <Typography variant="body2">
          {t('lab.preview.summarySlots')}：
          {slotsEnabled ? t('lab.preview.slotsOn') : t('lab.preview.slotsOff')}
        </Typography>
        <Typography variant="body2">
          {t('lab.preview.summaryPersona', { chars: personaChars })}
        </Typography>
        {depthText && (
          <Typography variant="body2">
            {t('lab.preview.summaryDepth', { depth: depthValue, chars: depthText.length })}
          </Typography>
        )}
        <Typography variant="body2">
          {t('lab.preview.summaryWorldbook', {
            count: selectedCount,
            after: afterPersona.length,
            before: beforeHistory.length,
            depth: atDepth.length,
          })}
        </Typography>
      </Box>

                {rpPreset ? (
                  <Box
                    ref={node => {
                      sectionRefs.current.rp = node as HTMLDivElement | null
                    }}
                    sx={{
                      scrollMarginTop: 8,
                      p: 1.5,
                      borderRadius: 1,
                      border: '1px solid',
                      borderColor: 'divider',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: 1,
                    }}
                  >
                    <Typography variant="body2" fontWeight={700}>
                      {t('lab.preview.rpTitle', { name: rpPreset.name })}
                    </Typography>
                    <Stack
                      direction="row"
                      spacing={1.5}
                      sx={{ flexWrap: 'wrap', rowGap: 0.5 }}
                    >
                      <Typography variant="caption" color="text.secondary">
                        {t('lab.preview.rpStats.system', { count: rpSystemCount })}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        {t('lab.preview.rpStats.tail', { count: rpTailCount })}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        {t('lab.preview.rpStats.depth', { count: rpDepthCount })}
                      </Typography>
                    </Stack>

                    {rpPreset.used.length === 0 ? (
                      <Typography variant="body2" color="text.secondary">
                        {t('lab.preview.rpEmpty')}
                      </Typography>
                    ) : (
                      <Box
                        sx={{
                          ...(UNIFIED_TABLE_STYLES.embedded as object),
                          maxHeight: 260,
                          overflow: 'auto',
                        }}
                      >
                        <Table size="small" stickyHeader>
                          <TableHead>
                            <TableRow>
                              <TableCell sx={UNIFIED_TABLE_STYLES.header}>
                                {t('lab.preview.rpColumns.name')}
                              </TableCell>
                              <TableCell sx={UNIFIED_TABLE_STYLES.header}>
                                {t('lab.preview.rpColumns.where')}
                              </TableCell>
                              <TableCell
                                sx={UNIFIED_TABLE_STYLES.header}
                                align="right"
                              >
                                {t('lab.preview.rpColumns.chars')}
                              </TableCell>
                            </TableRow>
                          </TableHead>
                          <TableBody>
                            {rpPreset.used.map((item, index) => (
                              <TableRow
                                key={`${item.identifier}:${item.where}:${index}`}
                                sx={UNIFIED_TABLE_STYLES.row}
                              >
                                <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                                  {item.name || item.identifier}
                                </TableCell>
                                <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                                  {rpWhereLabel(item.where)}
                                </TableCell>
                                <TableCell
                                  sx={UNIFIED_TABLE_STYLES.cell}
                                  align="right"
                                >
                                  {item.chars.toLocaleString()}
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </Box>
                    )}

                    {rpPreset.prefill_text ? (
                      <Box>
                        <Typography
                          variant="caption"
                          color="text.secondary"
                          display="block"
                          sx={{ mb: 0.5 }}
                        >
                          {t('lab.preview.rpPrefill')}
                        </Typography>
                        <Box
                          component="pre"
                          sx={{
                            m: 0,
                            p: 1,
                            borderRadius: 1,
                            bgcolor: theme.palette.action.hover,
                            fontFamily: 'monospace',
                            fontSize: '0.8rem',
                            whiteSpace: 'pre-wrap',
                            wordBreak: 'break-word',
                            maxHeight: 200,
                            overflow: 'auto',
                          }}
                        >
                          {rpPreset.prefill_text}
                        </Box>
                      </Box>
                    ) : null}
                  </Box>
                ) : null}
              </Stack>
            </Box>

            <Box
              ref={node => {
                sectionRefs.current.greeting = node as HTMLDivElement | null
              }}
              sx={{ scrollMarginTop: 8 }}
            >
              <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                {sectionLabel('greeting')}
              </Typography>
              {greetingWillSend ? (
                <Box
                  component="pre"
                  sx={{
                    m: 0,
                    p: 1.5,
                    borderRadius: 1,
                    bgcolor: theme.palette.action.hover,
                    fontFamily: 'monospace',
                    fontSize: '0.8rem',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    maxHeight: 240,
                    overflowY: 'auto',
                  }}
                >
                  {greetingText || t('lab.preview.emptyText')}
                </Box>
              ) : (
                <Typography variant="body2" color="text.secondary">
                  {t('lab.preview.greetingNotSent')}
                </Typography>
              )}
            </Box>

            <Box
              ref={node => {
                sectionRefs.current.slots = node as HTMLDivElement | null
              }}
              sx={{ scrollMarginTop: 8 }}
            >
              <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                {slotsEnabled ? t('lab.preview.slotsTitle') : t('lab.preview.slotsGated')}
              </Typography>
              <Stack spacing={1.5}>{SLOT_KEYS.map(slotBlock)}</Stack>
            </Box>

            <Box
              ref={node => {
                sectionRefs.current.messages = node as HTMLDivElement | null
              }}
              sx={{ scrollMarginTop: 8 }}
            >
              <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                {t('lab.preview.messagesTitle')}
              </Typography>
              <Stack spacing={1.5}>
                {result.messages.map((message, index) => (
                  <MessageBlock
                    key={`${message.source}:${index}`}
                    message={message}
                    label={sourceLabel(message, index)}
                  />
                ))}
              </Stack>
            </Box>

            <Box>
              <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                {t('lab.preview.prefillTitle')}
              </Typography>
              <Stack spacing={0.5}>
                <Typography variant="body2">
                  {t('lab.preview.prefillText')}：
                  {result.prefill.text || t('lab.preview.prefillNone')}
                </Typography>
                <Typography variant="body2">
                  {t('lab.preview.prefillWillSend')}：
                  {result.prefill.would_send ? t('lab.preview.yes') : t('lab.preview.no')}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  {t('lab.preview.prefillReasonLabel')}：{prefillReason}
                </Typography>
              </Stack>
            </Box>
          </Stack>
        </Box>
      </Box>
    </Box>
  )
}

export default function PreviewTab({ scope, isScopeReady }: PromptLabTabProps) {
  const { t } = useTranslation('tavern')
  const { data: options } = useWorldbookScopeOptions(scope)
  const [chatKey, setChatKey] = useState('')
  const [presetId, setPresetId] = useState('')
  const [rpPresetId, setRpPresetId] = useState('')
  const [sampleText, setSampleText] = useState('')

  const presetsQuery = useQuery({
    queryKey: ['prompt-lab', 'presets', scope.instance_key],
    queryFn: () =>
      presetsApi.getList({
        page: 1,
        page_size: 200,
        instance_key: scope.instance_key,
      }),
    enabled: isScopeReady,
    retry: false,
  })

  const rpPresetsQuery = useQuery({
    queryKey: ['prompt-lab', 'rp-presets', scope.instance_key],
    queryFn: () => rpPresetsApi.list({ instance_key: scope.instance_key }),
    enabled: isScopeReady,
    retry: false,
  })

  const mutation = useMutation({
    mutationFn: () =>
      tavernApi.preview({
        chat_key: chatKey,
        preset_id: presetId ? Number(presetId) : null,
        // 不传就是「按频道当前设置」；后端只在显式指定时才报「预设没生效」的原因
        ...(rpPresetId ? { rp_preset_id: Number(rpPresetId) } : {}),
        sample_text: sampleText.trim() || null,
      }),
  })

  const channelOptions = useMemo(
    () => [
      { value: '', label: t('lab.preview.channelPlaceholder') },
      ...(options?.channels ?? []).map(channel => ({
        value: channel.chat_key,
        label: channel.channel_name || channel.chat_key,
      })),
    ],
    [options?.channels, t]
  )

  const presetOptions = useMemo(
    () => [
      { value: '', label: t('lab.preview.presetCurrent') },
      ...(presetsQuery.data?.items ?? []).map(preset => ({
        value: String(preset.id),
        label: preset.name,
      })),
    ],
    [presetsQuery.data?.items, t]
  )

  const rpPresetOptions = useMemo(
    () => [
      { value: '', label: t('lab.preview.rpPresetCurrent') },
      ...(rpPresetsQuery.data?.items ?? []).map(preset => ({
        value: String(preset.id),
        label: preset.name,
      })),
    ],
    [rpPresetsQuery.data?.items, t]
  )

  return (
    <Box
      sx={[
        SCROLL_COLUMN_STYLES,
        // 第一行就是带浮动标签的下拉：顶端留出标签缩起后的位置，不然被滚动容器裁掉半截
        { gap: 2, pt: FLOATING_LABEL_GUTTER, pr: { xs: 0, sm: 0.5 } },
      ] as SxProps<Theme>}
    >
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
        <FilterSelect
          label={t('lab.preview.channel')}
          value={chatKey}
          options={channelOptions}
          onChange={setChatKey}
          showEmptyOption
        />
        <FilterSelect
          label={t('lab.preview.preset')}
          value={presetId}
          options={presetOptions}
          onChange={setPresetId}
          showEmptyOption
        />
        <FilterSelect
          label={t('lab.preview.rpPreset')}
          value={rpPresetId}
          options={rpPresetOptions}
          onChange={setRpPresetId}
          showEmptyOption
        />
      </Stack>

      {/* 换人设预览只换得了跟人设走的那几段；摘要与好感跟着记忆分区，预览不切换分区 */}
      {presetId ? <Alert severity="info">{t('lab.preview.presetOverrideNote')}</Alert> : null}

      <TextField
        label={t('lab.preview.sample')}
        value={sampleText}
        onChange={event => setSampleText(event.target.value.slice(0, SAMPLE_MAX_CHARS))}
        placeholder={t('lab.preview.samplePlaceholder')}
        helperText={t('lab.preview.sampleHelper', {
          count: sampleText.length,
          max: SAMPLE_MAX_CHARS,
        })}
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
          {mutation.isPending ? t('lab.preview.running') : t('lab.preview.run')}
        </ActionButton>
      </Box>

      {mutation.error ? <QueryErrorState error={mutation.error} compact /> : null}
      {mutation.data ? <PreviewResult result={mutation.data} /> : null}
    </Box>
  )
}
