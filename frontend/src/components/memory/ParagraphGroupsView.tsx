import { useEffect, useMemo, useState } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  Chip,
  CircularProgress,
  Pagination,
  Stack,
  Typography,
  useTheme,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import IconActionButton from '../common/IconActionButton'
import QueryErrorState from '../common/QueryErrorState'
import { SimpleTooltip } from '../common/ThemedTooltip'
import PersonCardDrawer from '../person/PersonCardDrawer'
import { isNotFoundError } from '../../services/api/axios'
import {
  memoryApi,
  type MemoryPersonItem,
  type MemoryScopeParams,
  type MemoryTopicItem,
  type ParagraphListItem,
} from '../../services/api/memory'
import { CHIP_VARIANTS } from '../../theme/variants'
import { MemorySensitiveChip, MemoryStatusChip } from './MemoryStatusChips'
import MemoryNotFoundState from './MemoryNotFoundState'
import PersonNames, { PersonName } from './PersonNames'
import { filtersToGroupQuery, filtersToParagraphQuery, type MemoryFilterState } from './filters'
import { formatListTime, truncateText } from './format'
import { labelsOrAccounts } from './personLabels'

/**
 * 记忆条目的分组视图：按人、按事件
 *
 * 平铺列表一页 20 条、按时间排，看不出谁和谁、哪件事和哪件事有关。这里把满足当前筛选的段落
 * 按「涉及的人」或「所属事件」折叠成组，组头给名字、段数与最近时间；展开一组才去取组内的
 * 前几段，想看全部就跳回平铺列表并带上这一组的筛选（那里有分页、排序与批量）。
 *
 * 组的数据来自两个只读接口（`/memory/persons`、`/memory/topics`），与段落列表同一个范围、
 * 同一套筛选；组内段落用段落列表的 `person` / `episode` 参数取，数字两边对得上。
 */

export type MemoryGroupBy = 'person' | 'topic'

const GROUP_PAGE_SIZE = 20
const PREVIEW_SIZE = 10

interface ParagraphGroupsViewProps {
  by: MemoryGroupBy
  scope: MemoryScopeParams
  filters: MemoryFilterState
  isScopeReady: boolean
  channelLabel: (chatKey: string | null) => string
  onOpenParagraph: (id: number) => void
  /** 「在列表里看这一组」：切回平铺列表并带上这一组的筛选 */
  onShowInList: (patch: Partial<MemoryFilterState>) => void
}

/** 组内的前几段：展开时才取，按创建时间倒序 */
function GroupParagraphs({
  scope,
  filters,
  patch,
  total,
  showPersons,
  channelLabel,
  onOpenParagraph,
  onShowInList,
}: {
  scope: MemoryScopeParams
  filters: MemoryFilterState
  patch: Partial<MemoryFilterState>
  total: number
  showPersons: boolean
  channelLabel: (chatKey: string | null) => string
  onOpenParagraph: (id: number) => void
  onShowInList: (patch: Partial<MemoryFilterState>) => void
}) {
  const { t } = useTranslation('memory')
  const groupFilters = useMemo<MemoryFilterState>(
    () => ({ ...filters, ...patch, page: 1, sort: 'create_time', order: 'desc' }),
    [filters, patch]
  )
  const query = useQuery({
    queryKey: ['memory', 'group-paragraphs', scope, groupFilters],
    queryFn: () =>
      memoryApi.listParagraphs({ ...filtersToParagraphQuery(groupFilters, PREVIEW_SIZE), ...scope }),
    retry: false,
  })

  if (query.isPending) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 1.5 }}>
        <CircularProgress size={20} />
      </Box>
    )
  }
  if (query.isError) {
    return isNotFoundError(query.error) ? (
      <MemoryNotFoundState compact />
    ) : (
      <QueryErrorState error={query.error} onRetry={() => void query.refetch()} compact />
    )
  }

  const rows: ParagraphListItem[] = query.data?.items ?? []
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.75 }}>
      {rows.map(row => (
        <Box
          key={row.id}
          role="button"
          tabIndex={0}
          onClick={() => onOpenParagraph(row.id)}
          onKeyDown={event => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              onOpenParagraph(row.id)
            }
          }}
          sx={{
            display: 'grid',
            gridTemplateColumns: { xs: '1fr', md: showPersons ? 'minmax(0,1fr) 180px 150px 150px' : 'minmax(0,1fr) 150px 150px' },
            gap: 1,
            alignItems: 'center',
            px: 1,
            py: 0.75,
            borderRadius: 1,
            cursor: 'pointer',
            '&:hover': { bgcolor: 'action.hover' },
          }}
        >
          <Box sx={{ minWidth: 0 }}>
            <Stack direction="row" spacing={0.75} alignItems="center" sx={{ minWidth: 0 }}>
              {row.status !== 'active' && <MemoryStatusChip status={row.status} />}
              <Typography variant="body2" noWrap title={row.summary || row.content}>
                {row.summary || truncateText(row.content, 80)}
              </Typography>
              {row.is_sensitive && <MemorySensitiveChip />}
            </Stack>
          </Box>
          {showPersons && (
            <Box sx={{ minWidth: 0, display: { xs: 'none', md: 'block' } }}>
              <PersonNames persons={labelsOrAccounts(row.persons, row.person_accounts)} max={1} />
            </Box>
          )}
          <Typography
            variant="caption"
            color="text.secondary"
            noWrap
            sx={{ display: { xs: 'none', md: 'block' } }}
          >
            {channelLabel(row.origin_chat_key)}
          </Typography>
          <Typography variant="caption" color="text.secondary" noWrap>
            {formatListTime(row.create_time)}
          </Typography>
        </Box>
      ))}
      {total > rows.length && (
        <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
          <ActionButton tone="ghost" size="small" onClick={() => onShowInList(patch)}>
            {t('groups.showAllInList', { count: total })}
          </ActionButton>
        </Box>
      )}
    </Box>
  )
}

function PersonGroupHeader({
  item,
  canOpenCard,
  onOpenCard,
}: {
  item: MemoryPersonItem
  /** 组头拿不到适配器或实例时不给入口 */
  canOpenCard: boolean
  onOpenCard: (account: string) => void
}) {
  const { t } = useTranslation('memory')
  const { t: tPerson } = useTranslation('person')
  return (
    <Stack direction="row" spacing={1} alignItems="center" sx={{ minWidth: 0, width: '100%' }}>
      <Box sx={{ minWidth: 0, flex: 1 }}>
        <Stack direction="row" spacing={0.5} alignItems="center" sx={{ minWidth: 0 }}>
          <PersonName person={item} />
          {item.accounts.length > 1 && (
            <SimpleTooltip
              title={t('groups.mergedAccounts', { accounts: item.accounts.join('、') })}
            >
              <InfoOutlinedIcon sx={{ fontSize: 14, color: 'text.secondary' }} />
            </SimpleTooltip>
          )}
        </Stack>
      </Box>
      <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
        {t('groups.paragraphCount', { count: item.paragraph_count })}
      </Typography>
      {canOpenCard && (
        <SimpleTooltip title={tPerson('entry')}>
          <span>
            <IconActionButton
              size="small"
              aria-label={tPerson('entry')}
              onClick={event => {
                // 组头也是 AccordionSummary：别让开人物卡顺手把这一组收起 / 展开
                event.stopPropagation()
                onOpenCard(item.account)
              }}
              onMouseDown={event => event.stopPropagation()}
            >
              <PersonOutlineIcon fontSize="small" />
            </IconActionButton>
          </span>
        </SimpleTooltip>
      )}
    </Stack>
  )
}

function TopicGroupHeader({ item }: { item: MemoryTopicItem }) {
  const theme = useTheme()
  const { t } = useTranslation('memory')
  const title =
    item.episode_id === null
      ? t('groups.looseParagraphs')
      : item.visible
        ? item.title || t('groups.untitledEpisode', { id: item.episode_id })
        : t('groups.hiddenEpisode', { id: item.episode_id })
  return (
    <Stack direction="row" spacing={1} alignItems="center" sx={{ minWidth: 0, width: '100%' }}>
      <Box sx={{ minWidth: 0, flex: 1 }}>
        <Stack direction="row" spacing={0.75} alignItems="center" sx={{ minWidth: 0 }}>
          {item.episode_id !== null && (
            <Chip
              size="small"
              label={t('kind.episode')}
              sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true)}
            />
          )}
          <Typography
            variant="body2"
            fontWeight={600}
            noWrap
            color={item.visible && item.episode_id !== null ? 'text.primary' : 'text.secondary'}
          >
            {title}
          </Typography>
        </Stack>
        {item.narrative_summary && (
          <Typography variant="caption" color="text.secondary" noWrap display="block" title={item.narrative_summary}>
            {truncateText(item.narrative_summary, 120)}
          </Typography>
        )}
      </Box>
      <Box sx={{ flexShrink: 0, textAlign: 'right' }}>
        <Typography variant="caption" color="text.secondary" display="block">
          {t('groups.paragraphCount', { count: item.paragraph_count })}
        </Typography>
        <Typography variant="caption" color="text.secondary" display="block">
          {formatListTime(item.last_time)}
        </Typography>
      </Box>
    </Stack>
  )
}

export default function ParagraphGroupsView({
  by,
  scope,
  filters,
  isScopeReady,
  channelLabel,
  onOpenParagraph,
  onShowInList,
}: ParagraphGroupsViewProps) {
  const { t } = useTranslation('memory')
  const [page, setPage] = useState(1)
  const [expanded, setExpanded] = useState<string | null>(null)
  // 人物卡：组头点开的是某个人，实例取页面当前作用域
  const [cardAccount, setCardAccount] = useState<string | null>(null)
  const adapterKey = scope.adapter_key ?? ''
  const instanceKey = scope.instance_key ?? ''
  const canOpenCard = Boolean(adapterKey && instanceKey)
  const groupQuery = useMemo(() => filtersToGroupQuery(filters, by), [filters, by])
  // 按内容比较：地址栏里与筛选无关的参数变了（打开详情之类），不该把展开的组收起来
  const groupKey = JSON.stringify(groupQuery)

  // 换了分组维度或筛选，旧的页码与展开项都不再对应现在的组
  useEffect(() => {
    setPage(1)
    setExpanded(null)
  }, [by, groupKey])

  const personsQuery = useQuery({
    queryKey: ['memory', 'groups', 'person', scope, groupQuery, page],
    queryFn: () =>
      memoryApi.listPersons({ ...groupQuery, page, page_size: GROUP_PAGE_SIZE, ...scope }),
    enabled: isScopeReady && by === 'person',
    retry: false,
    placeholderData: keepPreviousData,
  })
  const topicsQuery = useQuery({
    queryKey: ['memory', 'groups', 'topic', scope, groupQuery, page],
    queryFn: () =>
      memoryApi.listTopics({ ...groupQuery, page, page_size: GROUP_PAGE_SIZE, ...scope }),
    enabled: isScopeReady && by === 'topic',
    retry: false,
    placeholderData: keepPreviousData,
  })

  const activeQuery = by === 'person' ? personsQuery : topicsQuery
  const total = activeQuery.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / GROUP_PAGE_SIZE))

  const renderGroups = () => {
    if (activeQuery.isPending) {
      return (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={28} />
        </Box>
      )
    }
    if (activeQuery.isError) {
      return isNotFoundError(activeQuery.error) ? (
        <MemoryNotFoundState />
      ) : (
        <QueryErrorState error={activeQuery.error} onRetry={() => void activeQuery.refetch()} compact />
      )
    }
    if (total === 0) {
      return (
        <Box sx={{ py: 4, textAlign: 'center' }}>
          <Typography variant="subtitle1" fontWeight={700}>
            {t('empty.itemsTitle')}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {by === 'person' ? t('groups.emptyPersons') : t('empty.itemsHint')}
          </Typography>
        </Box>
      )
    }

    const groups =
      by === 'person'
        ? (personsQuery.data?.items ?? []).map(item => ({
            key: `person:${item.account}`,
            count: item.paragraph_count,
            header: (
              <PersonGroupHeader
                item={item}
                canOpenCard={canOpenCard}
                onOpenCard={setCardAccount}
              />
            ),
            patch: { person: item.account } as Partial<MemoryFilterState>,
          }))
        : (topicsQuery.data?.items ?? []).map(item => ({
            key: `topic:${item.episode_id ?? 'none'}`,
            count: item.paragraph_count,
            header: <TopicGroupHeader item={item} />,
            patch: {
              episode: item.episode_id === null ? 'none' : String(item.episode_id),
            } as Partial<MemoryFilterState>,
          }))

    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.75 }}>
        {groups.map(group => (
          <Accordion
            key={group.key}
            disableGutters
            expanded={expanded === group.key}
            onChange={(_event, isExpanded) => setExpanded(isExpanded ? group.key : null)}
            TransitionProps={{ unmountOnExit: true }}
            sx={{ '&:before': { display: 'none' }, border: 1, borderColor: 'divider', borderRadius: 1 }}
          >
            <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ minWidth: 0 }}>
              {group.header}
            </AccordionSummary>
            <AccordionDetails sx={{ pt: 0 }}>
              <GroupParagraphs
                scope={scope}
                filters={filters}
                patch={group.patch}
                total={group.count}
                showPersons={by === 'topic'}
                channelLabel={channelLabel}
                onOpenParagraph={onOpenParagraph}
                onShowInList={onShowInList}
              />
            </AccordionDetails>
          </Accordion>
        ))}
      </Box>
    )
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, flex: 1, minHeight: 0 }}>
      <Typography variant="caption" color="text.secondary">
        {by === 'person'
          ? t('groups.personHint', { total })
          : t('groups.topicHint', { total })}
      </Typography>
      <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', pr: 0.5 }}>{renderGroups()}</Box>
      {pageCount > 1 && (
        <Box sx={{ display: 'flex', justifyContent: 'center' }}>
          <Pagination
            size="small"
            count={pageCount}
            page={Math.min(page, pageCount)}
            onChange={(_event, next) => {
              setPage(next)
              setExpanded(null)
            }}
          />
        </Box>
      )}
      {canOpenCard && (
        <PersonCardDrawer
          open={cardAccount !== null}
          onClose={() => setCardAccount(null)}
          adapterKey={adapterKey}
          instanceKey={instanceKey}
          account={cardAccount ?? ''}
        />
      )}
    </Box>
  )
}
