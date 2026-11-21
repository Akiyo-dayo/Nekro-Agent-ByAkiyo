import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Chip,
  Stack,
  TextField,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import QueryErrorState from '../../components/common/QueryErrorState'
import { useWorldbookScopeOptions } from '../../components/worldbook/useWorldbookScopeOptions'
import { presetsApi } from '../../services/api/presets'
import {
  tavernApi,
  type TavernPreviewMessage,
  type TavernPreviewResponse,
} from '../../services/api/tavern'
import type { WorldbookScopeParams } from '../../services/api/worldbook'
import { CHIP_VARIANTS } from '../../theme/variants'

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
  const { t } = useTranslation('tavern')
  const [greetingOpen, setGreetingOpen] = useState(false)

  const meta = result.meta
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

  return (
    <Stack spacing={2}>
      <Alert severity="info">{t('lab.preview.notice')}</Alert>
      {/* 协议端没连上时后端用占位代替 Bot 自己的账号；不说明的话管理员会以为那就是真值 */}
      {meta.bot_offline === true ? (
        <Alert severity="warning">{t('lab.preview.botOffline')}</Alert>
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
        {greetingWillSend && (
          <Box>
            <Typography
              variant="body2"
              color="primary"
              onClick={() => setGreetingOpen(value => !value)}
              sx={{ cursor: 'pointer', '&:hover': { textDecoration: 'underline' } }}
            >
              {t('lab.preview.summaryGreeting')}
            </Typography>
            {greetingOpen && (
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
                  maxHeight: 240,
                  overflowY: 'auto',
                }}
              >
                {greetingText || t('lab.preview.emptyText')}
              </Box>
            )}
          </Box>
        )}
      </Box>

      <Box>
        <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
          {slotsEnabled ? t('lab.preview.slotsTitle') : t('lab.preview.slotsGated')}
        </Typography>
        <Stack spacing={1.5}>{SLOT_KEYS.map(slotBlock)}</Stack>
      </Box>

      <Box>
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
  )
}

export default function PreviewTab({ scope, isScopeReady }: PromptLabTabProps) {
  const { t } = useTranslation('tavern')
  const { data: options } = useWorldbookScopeOptions(scope)
  const [chatKey, setChatKey] = useState('')
  const [presetId, setPresetId] = useState('')
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

  const mutation = useMutation({
    mutationFn: () =>
      tavernApi.preview({
        chat_key: chatKey,
        preset_id: presetId ? Number(presetId) : null,
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

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        overflowY: 'auto',
        minHeight: 0,
        pr: { xs: 0, sm: 0.5 },
      }}
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
