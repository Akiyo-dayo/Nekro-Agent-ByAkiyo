import { useState, type ReactNode } from 'react'
import type { TFunction } from 'i18next'
import {
  Box,
  Chip,
  CircularProgress,
  Divider,
  Link,
  ListItemIcon,
  ListItemText,
  Menu,
  MenuItem,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import AcUnitRoundedIcon from '@mui/icons-material/AcUnitRounded'
import BlockIcon from '@mui/icons-material/Block'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import FavoriteRoundedIcon from '@mui/icons-material/FavoriteRounded'
import FilterAltOutlinedIcon from '@mui/icons-material/FilterAltOutlined'
import GppBadIcon from '@mui/icons-material/GppBad'
import GppGoodIcon from '@mui/icons-material/GppGood'
import ListAltIcon from '@mui/icons-material/ListAlt'
import MoreHorizRoundedIcon from '@mui/icons-material/MoreHorizRounded'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import RestoreIcon from '@mui/icons-material/Restore'
import ShieldRoundedIcon from '@mui/icons-material/ShieldRounded'
import TrendingDownIcon from '@mui/icons-material/TrendingDown'
import TrendingUpIcon from '@mui/icons-material/TrendingUp'
import ActionButton from '../common/ActionButton'
import IconActionButton from '../common/IconActionButton'
import QueryErrorState from '../common/QueryErrorState'
import StatusChip from '../common/StatusChip'
import { useNotification } from '../../hooks/useNotification'
import { isNotFoundError } from '../../services/api/axios'
import {
  memoryApi,
  type MemoryGraphNode,
  type MemoryItemAction,
  type MemoryItemDetail,
  type MemoryScopeParams,
  type MemoryType,
} from '../../services/api/memory'
import type { StatusTone } from '../../theme/themeConfig'
import { BORDER_RADIUS, CHIP_VARIANTS, SCROLLBAR_VARIANTS } from '../../theme/variants'
import DeleteConfirmDialog from './DeleteConfirmDialog'
import MemoryNotFoundState from './MemoryNotFoundState'
import PersonNames from './PersonNames'
import PersonCardDrawer from '../person/PersonCardDrawer'
import { memoryErrorMessage } from './errorMessage'
import { formatDateTime } from './format'
import {
  graphNodeActions,
  isItemType,
  isMilestoneNode,
  metaNumber,
  metaString,
  metaStrings,
} from './graphNodes'

/**
 * 图谱的详情面板（宽屏是画布右边的第二栏，窄屏是右侧抽屉）
 *
 * 自上而下：头（类型 · 状态 · 关闭）→ 正文（摘要一行粗体 + 正文最多 6 行，可展开）→ 关键事实两列
 * → 关联（连着的事件 / 人 / 实体，点了就在图上选中）→ 底部吸附的动作区：一个主按钮，其余进「⋯」，
 * 删除在菜单最后。详情走 `/memory/items/{type}/{id}`（与详情对话框同一个缓存），动作是同一套白名单；
 * 编辑、追溯原话与彻底删除留在详情对话框里（「完整详情」打开它）。
 *
 * 「人」节点没有单条详情：昵称大字、账号一行（这里是唯一常显账号的地方）、称呼，以及两个去处。
 */

export interface GraphNodeNeighbour {
  id: string
  label: string
  memoryType: string
  color: string
}

interface GraphNodePanelProps {
  node: MemoryGraphNode
  /** 这个节点所在簇的颜色，类型标签沿用 */
  accent: string
  scope: MemoryScopeParams
  /** 连着的节点（关联区用） */
  neighbours: GraphNodeNeighbour[]
  /** 抽屉里不画外框 */
  variant?: 'column' | 'drawer'
  channelLabel: (chatKey: string | null) => string
  onClose: () => void
  onChanged: () => void
  onOpenFull: (memoryType: MemoryType, id: number) => void
  onOpenChannel: (chatKey: string) => void
  onSelectNode: (nodeId: string) => void
  onFocusPerson: (account: string) => void
  onShowPersonInItems: (account: string) => void
}

const ACTION_ICONS: Record<MemoryItemAction, ReactNode> = {
  reinforce: <TrendingUpIcon fontSize="small" />,
  demote: <TrendingDownIcon fontSize="small" />,
  freeze: <AcUnitRoundedIcon fontSize="small" />,
  unfreeze: <RestoreIcon fontSize="small" />,
  protect: <GppGoodIcon fontSize="small" />,
  unprotect: <GppBadIcon fontSize="small" />,
  quarantine: <BlockIcon fontSize="small" />,
  restore: <RestoreIcon fontSize="small" />,
}

const STATUS_TONE: Record<string, StatusTone> = {
  active: 'active',
  quarantined: 'quarantined',
  deleted: 'inactive',
  pruned: 'disabled',
  hard_deleted: 'failed',
}

const RELATED_GROUPS = ['episode', 'person', 'entity', 'other'] as const
const RELATED_LIMIT = 5

interface Fact {
  key: string
  label: string
  value: ReactNode
  wide?: boolean
}

export default function GraphNodePanel({
  node,
  accent,
  scope,
  neighbours,
  variant = 'column',
  channelLabel,
  onClose,
  onChanged,
  onOpenFull,
  onOpenChannel,
  onSelectNode,
  onFocusPerson,
  onShowPersonInItems,
}: GraphNodePanelProps) {
  const theme = useTheme()
  const [cardAccount, setCardAccount] = useState<string | null>(null)
  const adapterKey = scope.adapter_key ?? ''
  const instanceKey = scope.instance_key ?? ''
  const canOpenCard = Boolean(adapterKey && instanceKey)

  return (
    <>
      <Box
        sx={{
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0,
          bgcolor: 'background.paper',
          ...(variant === 'column'
            ? {
                borderRadius: BORDER_RADIUS.LARGE,
                border: '1px solid',
                borderColor: alpha(theme.palette.primary.main, 0.12),
                overflow: 'hidden',
              }
            : {}),
        }}
      >
        {isItemType(node.memory_type) ? (
          <ItemBody
            key={node.id}
            node={node}
            memoryType={node.memory_type}
            accent={accent}
            scope={scope}
            neighbours={neighbours}
            channelLabel={channelLabel}
            onClose={onClose}
            onChanged={onChanged}
            onOpenFull={onOpenFull}
            onOpenChannel={onOpenChannel}
            onSelectNode={onSelectNode}
          />
        ) : (
          <PersonBody
            node={node}
            accent={accent}
            neighbours={neighbours}
            onClose={onClose}
            onSelectNode={onSelectNode}
            onFocusPerson={onFocusPerson}
            onShowPersonInItems={onShowPersonInItems}
            canOpenCard={canOpenCard}
            onOpenPersonCard={setCardAccount}
          />
        )}
      </Box>
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

function PanelHeader({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  const { t } = useTranslation('atlas')
  return (
    <Stack
      direction="row"
      spacing={0.75}
      alignItems="center"
      sx={{ px: 1.5, pt: 1.25, pb: 1, flexShrink: 0 }}
    >
      <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap" useFlexGap sx={{ flex: 1, minWidth: 0 }}>
        {children}
      </Stack>
      <IconActionButton size="small" onClick={onClose} aria-label={t('panel.close')} title={t('panel.close')}>
        <CloseRoundedIcon fontSize="small" />
      </IconActionButton>
    </Stack>
  )
}

function PanelScroll({ children }: { children: ReactNode }) {
  return (
    <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', px: 1.5, pb: 1.5, ...SCROLLBAR_VARIANTS.thin.styles }}>
      <Stack spacing={1.5}>{children}</Stack>
    </Box>
  )
}

function PanelFooter({ children }: { children: ReactNode }) {
  const theme = useTheme()
  return (
    <Stack
      direction="row"
      spacing={1}
      alignItems="center"
      sx={{
        flexShrink: 0,
        px: 1.5,
        py: 1,
        borderTop: `1px solid ${alpha(theme.palette.text.secondary, 0.12)}`,
        bgcolor: 'background.paper',
      }}
    >
      {children}
    </Stack>
  )
}

function TypeChip({ label, color }: { label: string; color: string }) {
  return <Chip size="small" label={label} sx={CHIP_VARIANTS.getCustomColorChip(color, true)} />
}

function FactGrid({ facts }: { facts: Fact[] }) {
  const theme = useTheme()
  if (!facts.length) return null
  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 1 }}>
      {facts.map(fact => (
        <Box
          key={fact.key}
          sx={{
            minWidth: 0,
            gridColumn: fact.wide ? '1 / -1' : 'auto',
            px: 1,
            py: 0.85,
            borderRadius: BORDER_RADIUS.DEFAULT,
            bgcolor: alpha(theme.palette.background.default, 0.5),
          }}
        >
          <Typography variant="caption" color="text.secondary">
            {fact.label}
          </Typography>
          <Box sx={{ typography: 'body2', wordBreak: 'break-word' }}>{fact.value}</Box>
        </Box>
      ))}
    </Box>
  )
}

/** 关联：连着的事件 / 人 / 实体，各最多 5 个；点一下在图上选中 */
function RelatedSection({
  neighbours,
  onSelectNode,
}: {
  neighbours: GraphNodeNeighbour[]
  onSelectNode: (nodeId: string) => void
}) {
  const { t } = useTranslation('atlas')
  if (!neighbours.length) return null
  const groupOf = (memoryType: string): (typeof RELATED_GROUPS)[number] =>
    memoryType === 'episode' || memoryType === 'person' || memoryType === 'entity' ? memoryType : 'other'
  const labels: Record<(typeof RELATED_GROUPS)[number], string> = {
    episode: t('panel.relatedEpisode'),
    person: t('panel.relatedPerson'),
    entity: t('panel.relatedEntity'),
    other: t('panel.relatedOther'),
  }
  return (
    <Box>
      <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 700 }}>
        {t('panel.related')}
      </Typography>
      <Stack spacing={0.75} sx={{ mt: 0.5 }}>
        {RELATED_GROUPS.map(group => {
          const items = neighbours.filter(item => groupOf(item.memoryType) === group)
          if (!items.length) return null
          return (
            <Stack key={group} direction="row" spacing={0.75} alignItems="flex-start">
              <Typography variant="caption" color="text.disabled" sx={{ width: 32, flexShrink: 0, pt: 0.25 }}>
                {labels[group]}
              </Typography>
              <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap sx={{ minWidth: 0 }}>
                {items.slice(0, RELATED_LIMIT).map(item => (
                  <Chip
                    key={item.id}
                    size="small"
                    label={item.label || item.id}
                    onClick={() => onSelectNode(item.id)}
                    sx={{ ...(CHIP_VARIANTS.getCustomColorChip(item.color, true) as object), maxWidth: 140 }}
                  />
                ))}
                {items.length > RELATED_LIMIT && (
                  <Typography variant="caption" color="text.secondary" sx={{ alignSelf: 'center' }}>
                    +{items.length - RELATED_LIMIT}
                  </Typography>
                )}
              </Stack>
            </Stack>
          )
        })}
      </Stack>
    </Box>
  )
}

function PersonBody({
  node,
  accent,
  neighbours,
  onClose,
  onSelectNode,
  onFocusPerson,
  onShowPersonInItems,
  canOpenCard,
  onOpenPersonCard,
}: {
  node: MemoryGraphNode
  accent: string
  neighbours: GraphNodeNeighbour[]
  onClose: () => void
  onSelectNode: (nodeId: string) => void
  onFocusPerson: (account: string) => void
  onShowPersonInItems: (account: string) => void
  canOpenCard: boolean
  onOpenPersonCard: (account: string) => void
}) {
  const theme = useTheme()
  const { t } = useTranslation('memory')
  const { t: tPerson } = useTranslation('person')
  const account = metaString(node.metadata, 'account')
  const accounts = metaStrings(node.metadata, 'accounts')
  const callName = metaString(node.metadata, 'call_name')
  const count = metaNumber(node.metadata, 'paragraph_count') ?? 0
  // 名字来自好感记录时注明：好感域共用，可能是在别的群或私聊里看到的
  const nameFromAffection = metaString(node.metadata, 'name_source') === 'affection'
  const accountLine = (accounts.length ? accounts : [account]).filter(Boolean).join(' · ')

  return (
    <>
      <PanelHeader onClose={onClose}>
        <TypeChip label={t('graph.kind.person')} color={accent} />
        <Typography variant="caption" color="text.secondary">
          {t('graph.person.paragraphCount', { count })}
        </Typography>
      </PanelHeader>
      <PanelScroll>
        <Box>
          <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap" useFlexGap>
            <Typography variant="h6" sx={{ fontWeight: 800, wordBreak: 'break-word', lineHeight: 1.3 }}>
              {node.label}
            </Typography>
            {callName && (
              <Chip
                size="small"
                label={callName}
                sx={{ ...(CHIP_VARIANTS.getCustomColorChip(theme.palette.secondary.main, true) as object), maxWidth: 140 }}
              />
            )}
          </Stack>
          {accountLine && (
            <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>
              {accountLine}
            </Typography>
          )}
          {nameFromAffection && (
            <Typography variant="caption" color="text.disabled" display="block">
              {t('persons.fromAffection')}
            </Typography>
          )}
        </Box>
        <Typography variant="caption" color="text.secondary">
          {t('graph.person.note')}
        </Typography>
        {canOpenCard && (
          <Box>
            <ActionButton
              tone="ghost"
              size="small"
              startIcon={<PersonOutlineIcon />}
              onClick={() => onOpenPersonCard(account)}
              disabled={!account}
            >
              {tPerson('open')}
            </ActionButton>
          </Box>
        )}
        <RelatedSection neighbours={neighbours} onSelectNode={onSelectNode} />
      </PanelScroll>
      <PanelFooter>
        <ActionButton
          tone="primary"
          size="small"
          startIcon={<FilterAltOutlinedIcon />}
          onClick={() => onFocusPerson(account)}
          disabled={!account}
        >
          {t('graph.person.focus')}
        </ActionButton>
        <ActionButton
          tone="secondary"
          size="small"
          startIcon={<ListAltIcon />}
          onClick={() => onShowPersonInItems(account)}
          disabled={!account}
        >
          {t('graph.person.showInItems')}
        </ActionButton>
      </PanelFooter>
    </>
  )
}

function ItemBody({
  node,
  memoryType,
  accent,
  scope,
  neighbours,
  channelLabel,
  onClose,
  onChanged,
  onOpenFull,
  onOpenChannel,
  onSelectNode,
}: {
  node: MemoryGraphNode
  memoryType: MemoryType
  accent: string
  scope: MemoryScopeParams
  neighbours: GraphNodeNeighbour[]
  channelLabel: (chatKey: string | null) => string
  onClose: () => void
  onChanged: () => void
  onOpenFull: (memoryType: MemoryType, id: number) => void
  onOpenChannel: (chatKey: string) => void
  onSelectNode: (nodeId: string) => void
}) {
  const theme = useTheme()
  const notification = useNotification()
  const { t } = useTranslation('memory')
  const { t: tAtlas } = useTranslation('atlas')
  const { t: tCommon } = useTranslation('common')
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)

  const detailQuery = useQuery({
    queryKey: ['memory', 'item', memoryType, node.ref_id],
    queryFn: () => memoryApi.getItem(memoryType, node.ref_id),
    retry: false,
    refetchOnWindowFocus: false,
  })
  const detail = detailQuery.data

  const actionMutation = useMutation({
    mutationFn: (action: MemoryItemAction) => memoryApi.actOnItem(memoryType, node.ref_id, action),
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

  const typeChip = <TypeChip label={t(`kind.${memoryType}`)} color={accent} />

  if (detailQuery.isPending || detailQuery.isError || !detail) {
    return (
      <>
        <PanelHeader onClose={onClose}>{typeChip}</PanelHeader>
        <PanelScroll>
          {detailQuery.isPending ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
              <CircularProgress size={24} />
            </Box>
          ) : isNotFoundError(detailQuery.error) ? (
            <MemoryNotFoundState compact />
          ) : (
            <QueryErrorState error={detailQuery.error} onRetry={() => void detailQuery.refetch()} compact />
          )}
        </PanelScroll>
      </>
    )
  }

  const actions = graphNodeActions(memoryType, detail)
  const canOpenFull = memoryType === 'paragraph' || memoryType === 'episode'
  const canSoftDelete = detail.status === 'active'
  // 主按钮：段落 = 强化（隔离着的给「放回」），事件 = 打开完整，关系 = 强化
  const primaryAction: MemoryItemAction | null = actions.includes('reinforce')
    ? 'reinforce'
    : actions.includes('restore') && memoryType !== 'episode'
      ? 'restore'
      : null
  const primaryIsOpenFull = memoryType === 'episode' || (!primaryAction && canOpenFull)
  const menuActions = actions.filter(action => action !== primaryAction)
  const showOpenFullInMenu = canOpenFull && !primaryIsOpenFull
  const hasMenu = menuActions.length > 0 || showOpenFullInMenu || canSoftDelete

  // 一个格子只放一个状态标签：图上失活（检索里不再出现）但条目本身仍生效时，标「失活」
  const statusTone: StatusTone =
    detail.status === 'active' && node.status === 'inactive' ? 'inactive' : (STATUS_TONE[detail.status] ?? 'inactive')
  const statusLabel =
    detail.status === 'active' && node.status === 'inactive'
      ? tAtlas('node.inactive')
      : t(`status.${detail.status}`, { defaultValue: detail.status })

  const closeMenu = () => setMenuAnchor(null)

  return (
    <>
      <PanelHeader onClose={onClose}>
        {typeChip}
        <StatusChip tone={statusTone} label={statusLabel} />
        {detail.is_frozen && (
          <Tooltip title={t('flags.frozen')}>
            <AcUnitRoundedIcon sx={{ fontSize: 14, color: theme.palette.info.main }} />
          </Tooltip>
        )}
        {detail.is_protected && (
          <Tooltip title={t('flags.protected')}>
            <ShieldRoundedIcon sx={{ fontSize: 14, color: theme.palette.primary.main }} />
          </Tooltip>
        )}
        {isMilestoneNode(node) && (
          <Tooltip title={t('origin.affection')}>
            <FavoriteRoundedIcon sx={{ fontSize: 14, color: theme.palette.secondary.main }} />
          </Tooltip>
        )}
      </PanelHeader>
      <PanelScroll>
        <ItemText memoryType={memoryType} node={node} detail={detail} />
        <FactGrid facts={itemFacts(memoryType, node, detail, channelLabel, onOpenChannel, t, tAtlas)} />
        <RelatedSection neighbours={neighbours} onSelectNode={onSelectNode} />
      </PanelScroll>
      {(primaryAction || primaryIsOpenFull || hasMenu) ? (
        <PanelFooter>
          {primaryAction && (
            <ActionButton
              tone="primary"
              size="small"
              startIcon={ACTION_ICONS[primaryAction]}
              disabled={actionMutation.isPending}
              onClick={() => actionMutation.mutate(primaryAction)}
            >
              {t(`actions.${primaryAction}`)}
            </ActionButton>
          )}
          {primaryIsOpenFull && (
            <ActionButton
              tone="primary"
              size="small"
              startIcon={<OpenInNewIcon />}
              onClick={() => onOpenFull(memoryType, node.ref_id)}
            >
              {t('graph.panel.openFull')}
            </ActionButton>
          )}
          <Box sx={{ flex: 1 }} />
          {hasMenu && (
            <IconActionButton
              size="small"
              aria-label={tAtlas('panel.more')}
              title={tAtlas('panel.more')}
              onClick={event => setMenuAnchor(event.currentTarget)}
            >
              <MoreHorizRoundedIcon fontSize="small" />
            </IconActionButton>
          )}
          <Menu
            anchorEl={menuAnchor}
            open={!!menuAnchor}
            onClose={closeMenu}
            anchorOrigin={{ vertical: 'top', horizontal: 'right' }}
            transformOrigin={{ vertical: 'bottom', horizontal: 'right' }}
          >
            {showOpenFullInMenu && (
              <MenuItem
                onClick={() => {
                  closeMenu()
                  onOpenFull(memoryType, node.ref_id)
                }}
              >
                <ListItemIcon>
                  <OpenInNewIcon fontSize="small" />
                </ListItemIcon>
                <ListItemText>{t('graph.panel.openFull')}</ListItemText>
              </MenuItem>
            )}
            {menuActions.map(action => (
              <MenuItem
                key={action}
                disabled={actionMutation.isPending}
                onClick={() => {
                  closeMenu()
                  actionMutation.mutate(action)
                }}
              >
                <ListItemIcon>{ACTION_ICONS[action]}</ListItemIcon>
                <ListItemText>{t(`actions.${action}`)}</ListItemText>
              </MenuItem>
            ))}
            {canSoftDelete && (menuActions.length > 0 || showOpenFullInMenu) && <Divider />}
            {canSoftDelete && (
              <MenuItem
                onClick={() => {
                  closeMenu()
                  setDeleteOpen(true)
                }}
                sx={{ color: theme.palette.error.main }}
              >
                <ListItemIcon sx={{ color: 'inherit' }}>
                  <DeleteOutlineIcon fontSize="small" />
                </ListItemIcon>
                <ListItemText>{t('actions.delete')}</ListItemText>
              </MenuItem>
            )}
          </Menu>
        </PanelFooter>
      ) : (
        <PanelFooter>
          <Typography variant="caption" color="text.secondary">
            {t('graph.panel.noActions')}
          </Typography>
        </PanelFooter>
      )}
      {deleteOpen && (
        <DeleteConfirmDialog
          open
          memoryType={memoryType}
          itemId={node.ref_id}
          hard={false}
          scope={scope}
          onClose={() => setDeleteOpen(false)}
          onDeleted={() => {
            setDeleteOpen(false)
            onChanged()
          }}
        />
      )}
    </>
  )
}

function ItemText({
  memoryType,
  node,
  detail,
}: {
  memoryType: MemoryType
  node: MemoryGraphNode
  detail: MemoryItemDetail
}) {
  const { t } = useTranslation('atlas')
  const [expanded, setExpanded] = useState(false)
  const title =
    memoryType === 'paragraph'
      ? detail.summary || node.label
      : memoryType === 'episode'
        ? detail.title || node.label
        : node.label
  const body =
    memoryType === 'paragraph'
      ? detail.content && detail.content !== title
        ? detail.content
        : ''
      : memoryType === 'episode'
        ? detail.narrative_summary ?? ''
        : node.subtitle
  // 粗估：超过 6 行的长度才给「展开」
  const long = body.length > 180 || body.split('\n').length > 6
  return (
    <Box>
      <Typography variant="body1" sx={{ fontWeight: 800, wordBreak: 'break-word' }}>
        {title || '—'}
      </Typography>
      {body && (
        <>
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{
              mt: 0.5,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              ...(expanded
                ? {}
                : { display: '-webkit-box', WebkitLineClamp: 6, WebkitBoxOrient: 'vertical', overflow: 'hidden' }),
            }}
          >
            {body}
          </Typography>
          {long && (
            <Link
              component="button"
              type="button"
              variant="caption"
              underline="hover"
              onClick={() => setExpanded(value => !value)}
              sx={{ mt: 0.25 }}
            >
              {expanded ? t('panel.collapse') : t('panel.expand')}
            </Link>
          )}
        </>
      )}
    </Box>
  )
}

function itemFacts(
  memoryType: MemoryType,
  node: MemoryGraphNode,
  detail: MemoryItemDetail,
  channelLabel: (chatKey: string | null) => string,
  onOpenChannel: (chatKey: string) => void,
  t: TFunction,
  tAtlas: TFunction
): Fact[] {
  const facts: Fact[] = []
  const reinforced = detail.reinforcement_count ?? 0
  const weight: Fact = {
    key: 'weight',
    label: tAtlas('panel.weight'),
    value: reinforced > 0 ? tAtlas('panel.weightLine', { weight: node.weight.toFixed(2), count: reinforced }) : node.weight.toFixed(2),
  }
  const channel = (chatKey: string | null | undefined, name?: string): Fact => ({
    key: 'channel',
    label: tAtlas('panel.channel'),
    value: chatKey ? (
      <Link component="button" type="button" underline="hover" onClick={() => onOpenChannel(chatKey)} sx={{ textAlign: 'left' }}>
        {name || channelLabel(chatKey)}
      </Link>
    ) : (
      '—'
    ),
  })
  const blocked = detail.blocked_round_hits ?? 0
  if (memoryType === 'paragraph') {
    const persons = detail.persons ?? []
    if (persons.length > 0) {
      facts.push({
        key: 'persons',
        label: tAtlas('panel.persons'),
        wide: true,
        value: (
          <Box sx={{ mt: 0.25 }}>
            <PersonNames
              persons={persons.map(person => ({
                account: person.account,
                display_name: person.display_name,
                call_name: person.call_name ?? '',
                name_source: person.name_source,
              }))}
              max={4}
            />
          </Box>
        ),
      })
    }
    facts.push(channel(detail.origin_chat_key, detail.origin_channel_name))
    facts.push({
      key: 'time',
      label: tAtlas('panel.time'),
      value: formatDateTime(metaString(node.metadata, 'create_time') || null),
    })
    facts.push(weight)
  } else if (memoryType === 'episode') {
    facts.push(channel(detail.origin_chat_key))
    facts.push(weight)
    facts.push({
      key: 'time',
      label: tAtlas('panel.time'),
      value: `${formatDateTime(detail.time_start ?? null)} – ${formatDateTime(detail.time_end ?? null)}`,
      wide: true,
    })
  } else if (memoryType === 'relation') {
    facts.push(weight)
    if (node.paragraph_id) {
      facts.push({
        key: 'paragraph',
        label: t('graph.panel.sourceParagraph'),
        value: `#${node.paragraph_id}`,
      })
    }
  } else {
    const appearances = metaNumber(node.metadata, 'appearance_count')
    if (appearances !== null) {
      facts.push({
        key: 'appearances',
        label: t('graph.panel.appearances'),
        value: appearances.toLocaleString(),
      })
    }
  }
  if (blocked > 0) {
    facts.push({ key: 'blocked', label: tAtlas('panel.blockedHits'), value: blocked.toLocaleString() })
  }
  return facts
}
