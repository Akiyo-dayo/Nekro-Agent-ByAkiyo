import { Box, ButtonBase, Chip, Stack, Typography } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import AutoAwesomeRoundedIcon from '@mui/icons-material/AutoAwesomeRounded'
import MoneyOffRoundedIcon from '@mui/icons-material/MoneyOffRounded'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import PushPinRoundedIcon from '@mui/icons-material/PushPinRounded'
import VerifiedUserRoundedIcon from '@mui/icons-material/VerifiedUserRounded'
import WarningAmberRoundedIcon from '@mui/icons-material/WarningAmberRounded'
import ActionButton from '../common/ActionButton'
import StatusChip from '../common/StatusChip'
import { ThemedTooltip } from '../common/ThemedTooltip'
import PersonCardDrawer from '../person/PersonCardDrawer'
import { worldbookBookPath } from '../../router/routes'
import type {
  WorldbookGraphNode,
  WorldbookGraphResponse,
  WorldbookScopeParams,
} from '../../services/api/worldbook'
import { CHIP_VARIANTS } from '../../theme/variants'
import { WorldbookScopeChip, WorldbookStatusChip } from './chips'
import { worldbookBindingText } from './display'

/**
 * 世界书图谱右侧面板
 *
 * 只读：书给作用域与家底，条目给最近注入 / 被预算挤掉 / 被查询的次数与正文预览，绑定对象
 * 给绑着的书。条目的「它提到的 / 提到它的」来自 `key_reference` 推导边，点了在图上选中。
 */

interface WorldbookGraphPanelProps {
  node: WorldbookGraphNode
  graph: WorldbookGraphResponse | undefined
  nodeById: Map<string, WorldbookGraphNode>
  accent: string
  /** 页面当前作用域：人物卡要适配器 + 实例 + 账号三样齐全才给入口 */
  scope: WorldbookScopeParams
  onSelect: (nodeId: string) => void
}

function metaText(node: WorldbookGraphNode, key: string): string {
  const value = node.metadata[key]
  return typeof value === 'string' ? value : ''
}

function metaNumber(node: WorldbookGraphNode, key: string): number {
  const value = node.metadata[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function metaBool(node: WorldbookGraphNode, key: string): boolean {
  return node.metadata[key] === true
}

function StatChip({ label, color }: { label: string; color: string }) {
  return <Chip size="small" label={label} sx={CHIP_VARIANTS.getCustomColorChip(color, true)} />
}

function SectionTitle({ children }: { children: string }) {
  return (
    <Typography variant="overline" sx={{ display: 'block', color: 'text.secondary', fontWeight: 700, lineHeight: 1.6 }}>
      {children}
    </Typography>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  const theme = useTheme()
  return (
    <Box sx={{ px: 1, py: 0.85, borderRadius: 1, bgcolor: alpha(theme.palette.background.default, 0.5) }}>
      <Typography variant="caption" color="text.secondary" display="block">
        {label}
      </Typography>
      <Box sx={{ mt: 0.25, wordBreak: 'break-word' }}>{children}</Box>
    </Box>
  )
}

export default function WorldbookGraphPanel({
  node,
  graph,
  nodeById,
  accent,
  scope,
  onSelect,
}: WorldbookGraphPanelProps) {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')
  const { t: tPerson } = useTranslation('person')
  const navigate = useNavigate()
  const [cardAccount, setCardAccount] = useState<string | null>(null)

  const adapterKey = scope.adapter_key ?? ''
  const instanceKey = scope.instance_key ?? ''
  const canOpenCard = Boolean(adapterKey && instanceKey)

  const kindLabel = t(`graph.kind.${node.memory_type}`, { defaultValue: node.memory_type })
  const scopeKind = metaText(node, 'scope_kind')
  const binding = worldbookBindingText({
    scope_kind: (scopeKind || 'domain') as 'domain' | 'preset' | 'channel' | 'person',
    preset_id: metaNumber(node, 'preset_id'),
    preset_name: metaText(node, 'preset_name'),
    chat_key: metaText(node, 'chat_key'),
    channel_name: metaText(node, 'channel_name'),
    person_account: metaText(node, 'person_account'),
  })

  const bookLink = (bookId: number) => {
    const book = nodeById.get(`book-${bookId}`)
    if (!book) {
      return (
        <Typography variant="body2" color="text.secondary">
          #{bookId}
        </Typography>
      )
    }
    return (
      <ButtonBase
        onClick={() => onSelect(`book-${bookId}`)}
        sx={{ color: theme.palette.primary.main, fontSize: '0.875rem', textAlign: 'left' }}
      >
        {book.label}
      </ButtonBase>
    )
  }

  const referenceNeighbours = (direction: 'outgoing' | 'incoming') => {
    const entries = graph?.edges ?? []
    const ids = new Set<string>()
    for (const edge of entries) {
      if (edge.edge_type !== 'key_reference') continue
      if (direction === 'outgoing' && edge.source === node.id) ids.add(edge.target)
      if (direction === 'incoming' && edge.target === node.id) ids.add(edge.source)
    }
    return [...ids]
      .map(id => nodeById.get(id))
      .filter((item): item is WorldbookGraphNode => item !== undefined && item.memory_type === 'entry')
  }

  const neighbourList = (items: WorldbookGraphNode[]) =>
    items.length === 0 ? (
      <Typography variant="body2" color="text.secondary">
        {t('graph.panel.none')}
      </Typography>
    ) : (
      <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap">
        {items.map(item => (
          <Chip
            key={item.id}
            size="small"
            label={item.label}
            onClick={() => onSelect(item.id)}
            sx={CHIP_VARIANTS.getCustomColorChip(accent, false)}
          />
        ))}
      </Stack>
    )

  const renderBook = () => (
    <Stack spacing={1.25}>
      <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap" alignItems="center">
        <WorldbookScopeChip scopeKind={scopeKind || 'domain'} />
        {metaBool(node, 'read_only') && (
          <StatusChip tone="protected" label={t('readOnly.chip')} />
        )}
        {node.status === 'inactive' ? (
          <StatusChip tone="inactive" label={t('common.disabled')} />
        ) : (
          <StatusChip tone="active" label={t('common.enabled')} />
        )}
      </Stack>

      {binding && (
        <Typography variant="body2" color="text.secondary">
          {t('graph.panel.boundTo', { target: binding })}
        </Typography>
      )}

      <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap">
        <StatChip label={t('graph.panel.entryCount', { count: metaNumber(node, 'entry_count') })} color={accent} />
        <StatChip label={t('graph.panel.constantCount', { count: metaNumber(node, 'constant_count') })} color={accent} />
        <StatChip
          label={t('graph.panel.quarantinedCount', { count: metaNumber(node, 'quarantined_count') })}
          color={accent}
        />
      </Stack>

      <Stack spacing={0.75}>
        <Field label={t('graph.panel.injectMode')}>
          <Typography variant="body2">
            {t(`booksForm.injectMode.${metaText(node, 'inject_mode')}`, {
              defaultValue: metaText(node, 'inject_mode') || t('common.unknown'),
            })}
          </Typography>
        </Field>
        <Field label={t('graph.panel.activeScene')}>
          <Typography variant="body2">
            {t(`booksForm.activeScene.${metaText(node, 'active_scene')}`, {
              defaultValue: metaText(node, 'active_scene') || t('common.unknown'),
            })}
          </Typography>
        </Field>
        <Field label={t('graph.panel.vectorAll')}>
          <Typography variant="body2">
            {metaBool(node, 'vector_all') ? t('entryDialog.yes') : t('entryDialog.no')}
          </Typography>
        </Field>
      </Stack>

      <ActionButton
        tone="secondary"
        size="small"
        startIcon={<OpenInNewIcon fontSize="small" />}
        onClick={() => navigate(worldbookBookPath(node.ref_id))}
      >
        {t('graph.panel.openBook')}
      </ActionButton>
    </Stack>
  )

  const renderEntry = () => {
    const bookId = metaNumber(node, 'book_id')
    const keys = Array.isArray(node.metadata.keys) ? node.metadata.keys : []
    const flags = [
      { key: 'constant', label: t('graph.flags.constant'), icon: <PushPinRoundedIcon fontSize="small" />, on: metaBool(node, 'constant') },
      { key: 'vectorized', label: t('graph.flags.vectorized'), icon: <AutoAwesomeRoundedIcon fontSize="small" />, on: metaBool(node, 'vectorized') },
      { key: 'ignoreBudget', label: t('graph.flags.ignoreBudget'), icon: <MoneyOffRoundedIcon fontSize="small" />, on: metaBool(node, 'ignore_budget') },
      { key: 'trusted', label: t('graph.flags.trusted'), icon: <VerifiedUserRoundedIcon fontSize="small" />, on: metaBool(node, 'trusted') },
    ].filter(flag => flag.on)

    return (
      <Stack spacing={1.25}>
        <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap" alignItems="center">
          <WorldbookStatusChip status={node.status} enabled={node.status !== 'inactive'} />
          {flags.map(flag => (
            <ThemedTooltip key={flag.key} title={flag.label}>
              <Box
                component="span"
                sx={{ display: 'inline-flex', alignItems: 'center', color: theme.palette.text.secondary }}
                aria-label={flag.label}
              >
                {flag.icon}
              </Box>
            </ThemedTooltip>
          ))}
        </Stack>

        <Box>
          <Typography variant="caption" color="text.secondary" display="block">
            {t('graph.panel.book')}
          </Typography>
          {bookLink(bookId)}
        </Box>

        {keys.length > 0 && (
          <Stack direction="row" spacing={0.5} useFlexGap flexWrap="wrap">
            {keys.map(key => (
              <Chip
                key={key}
                size="small"
                label={key}
                sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.success.main, true)}
              />
            ))}
          </Stack>
        )}

        <Box
          sx={{
            px: 1,
            py: 0.9,
            borderRadius: 1,
            bgcolor: alpha(theme.palette.success.main, 0.06),
            border: '1px solid',
            borderColor: alpha(theme.palette.success.main, 0.16),
          }}
        >
          <Typography variant="body2">
            {t('graph.panel.injectionSummary', {
              rounds: graph?.rounds_scanned ?? 0,
              injected: metaNumber(node, 'injected'),
              dropped: metaNumber(node, 'dropped'),
              lookups: metaNumber(node, 'lookups'),
            })}
          </Typography>
        </Box>

        {metaBool(node, 'starved') && (
          <Stack
            direction="row"
            spacing={0.75}
            alignItems="flex-start"
            sx={{
              px: 1,
              py: 0.9,
              borderRadius: 1,
              bgcolor: alpha(theme.palette.warning.main, 0.08),
              border: '1px solid',
              borderColor: alpha(theme.palette.warning.main, 0.24),
            }}
          >
            <WarningAmberRoundedIcon fontSize="small" sx={{ color: theme.palette.warning.main, mt: 0.1 }} />
            <Typography variant="body2" sx={{ color: theme.palette.warning.main }}>
              {t('graph.panel.starved')}
            </Typography>
          </Stack>
        )}

        <Box>
          <SectionTitle>{t('graph.panel.preview')}</SectionTitle>
          <Box
            component="pre"
            sx={{
              m: 0,
              p: 1,
              borderRadius: 1,
              bgcolor: 'action.hover',
              fontFamily: 'inherit',
              fontSize: '0.8125rem',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              maxHeight: 180,
              overflowY: 'auto',
            }}
          >
            {metaText(node, 'preview') || t('common.empty')}
          </Box>
        </Box>

        <Box>
          <SectionTitle>{t('graph.panel.mentions')}</SectionTitle>
          {neighbourList(referenceNeighbours('outgoing'))}
        </Box>
        <Box>
          <SectionTitle>{t('graph.panel.mentionedBy')}</SectionTitle>
          {neighbourList(referenceNeighbours('incoming'))}
        </Box>

        <ActionButton
          tone="secondary"
          size="small"
          startIcon={<OpenInNewIcon fontSize="small" />}
          onClick={() => navigate(`${worldbookBookPath(bookId)}?entry=${node.ref_id}`)}
        >
          {t('graph.panel.openEntry')}
        </ActionButton>
      </Stack>
    )
  }

  const renderBinding = () => {
    const bookIds = Array.isArray(node.metadata.book_ids) ? node.metadata.book_ids : []
    const personAccount = metaText(node, 'person_account')
    return (
      <Stack spacing={1.25}>
        <Field label={t('graph.panel.bindingType')}>
          <Typography variant="body2">
            {node.memory_type === 'person'
              ? t('graph.bindingType.person')
              : node.memory_type === 'channel'
                ? t('graph.bindingType.channel')
                : t('graph.bindingType.preset')}
          </Typography>
        </Field>
        <Box>
          <SectionTitle>{t('graph.panel.boundBooks', { count: bookIds.length })}</SectionTitle>
          {bookIds.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              {t('graph.panel.none')}
            </Typography>
          ) : (
            <Stack spacing={0.25}>
              {bookIds.map(id => (
                <Box key={id}>{bookLink(id)}</Box>
              ))}
            </Stack>
          )}
        </Box>
        {node.memory_type === 'person' && canOpenCard && (
          <ActionButton
            tone="ghost"
            size="small"
            startIcon={<PersonOutlineIcon fontSize="small" />}
            onClick={() => setCardAccount(personAccount)}
            disabled={!personAccount}
          >
            {tPerson('open')}
          </ActionButton>
        )}
      </Stack>
    )
  }

  return (
    <>
      <Stack spacing={1.25}>
        <Stack direction="row" spacing={0.75} useFlexGap flexWrap="wrap" alignItems="center">
          <StatChip label={kindLabel} color={accent} />
          <Typography variant="subtitle2" sx={{ fontWeight: 700, minWidth: 0, wordBreak: 'break-word' }}>
            {node.label}
          </Typography>
        </Stack>
        {node.subtitle && (
          <Typography variant="caption" color="text.secondary" sx={{ mt: -0.75 }}>
            {node.subtitle}
          </Typography>
        )}
        {node.memory_type === 'book'
          ? renderBook()
          : node.memory_type === 'entry'
            ? renderEntry()
            : renderBinding()}
      </Stack>
      {canOpenCard && (
        <PersonCardDrawer
          open={cardAccount !== null}
          onClose={() => setCardAccount(null)}
          adapterKey={adapterKey}
          instanceKey={instanceKey}
          account={cardAccount ?? ''}
        />
      )}
    </>
  )
}
