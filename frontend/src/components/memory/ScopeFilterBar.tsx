import { useRef, useState, type ReactNode } from 'react'
import { Box, TextField } from '@mui/material'
import { useTranslation } from 'react-i18next'
import FilterListIcon from '@mui/icons-material/FilterList'
import ClearAllIcon from '@mui/icons-material/ClearAll'
import ActionButton from '../common/ActionButton'
import FilterSelect from '../common/FilterSelect'
import SearchField from '../common/SearchField'
import { useNotification } from '../../hooks/useNotification'
import { INPUT_VARIANTS } from '../../theme/variants'
import {
  COGNITIVE_TYPES,
  KNOWLEDGE_TYPES,
  MEMORY_STATUS_FILTERS,
  ORDERS,
  ORIGIN_KINDS,
  PARTITIONS,
  SORTS,
  type MemoryScopeParams,
  type MemoryStatusFilter,
} from '../../services/api/memory'
import {
  DEFAULT_MEMORY_FILTERS,
  MEMORY_DOMAINS,
  MEMORY_SEARCH_MAX_LENGTH,
  type MemoryFilterState,
} from './filters'
import { useMemoryScopeOptions } from './useMemoryScopeOptions'

/**
 * 记忆页筛选栏
 *
 * 频道与人设的选项只来自 `GET /memory/scope-options`：那份响应已经按当前身份裁剪，
 * 从别的列表拼一份出来就等于多了一条会飘的授权判据。
 *
 * 组件是受控的：每个标签页只用 `fields` 声明自己支持哪几项。episode 没有「某个人 /
 * 认知类型 / 知识类型」，后端会直接 400，所以在这里就不画。
 */

const ADVANCED_FIELDS: Array<keyof MemoryFilterState> = [
  'partition',
  'domain',
  'person',
  'cognitiveType',
  'knowledgeType',
  'timeFrom',
  'timeTo',
  'sort',
  'order',
]

export interface ScopeFilterBarProps {
  value: MemoryFilterState
  onChange: (next: MemoryFilterState) => void
  /** 本标签页支持哪些筛选项 */
  fields: Array<keyof MemoryFilterState>
  scope: MemoryScopeParams
  /** 与筛选同一行、排在最左的前置控件（记忆条目的段落 / 事件切换） */
  leading?: ReactNode
}

function isFieldActive(value: MemoryFilterState, field: keyof MemoryFilterState): boolean {
  switch (field) {
    case 'search':
      return value.search.trim() !== ''
    case 'status':
      return value.status !== DEFAULT_MEMORY_FILTERS.status
    case 'chatKey':
      return value.chatKey !== ''
    case 'presetId':
      return value.presetId !== null
    case 'partition':
      return value.partition !== DEFAULT_MEMORY_FILTERS.partition
    case 'domain':
      return value.domain !== DEFAULT_MEMORY_FILTERS.domain
    case 'person':
      return value.person.trim() !== ''
    case 'cognitiveType':
      return value.cognitiveType !== ''
    case 'knowledgeType':
      return value.knowledgeType !== ''
    case 'originKind':
      return value.originKind !== ''
    case 'timeFrom':
      return value.timeFrom !== ''
    case 'timeTo':
      return value.timeTo !== ''
    case 'sort':
      return value.sort !== DEFAULT_MEMORY_FILTERS.sort
    case 'order':
      return value.order !== DEFAULT_MEMORY_FILTERS.order
    case 'page':
      return value.page !== DEFAULT_MEMORY_FILTERS.page
    default:
      return false
  }
}

/** 控件在窄屏整行、宽屏固定宽度；视觉上与 FilterSelect 同高 */
function FilterSlot({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <Box sx={{ width: { xs: '100%', sm: wide ? 240 : 170 }, flexShrink: 0 }}>{children}</Box>
  )
}

export default function ScopeFilterBar({
  value,
  onChange,
  fields,
  scope,
  leading,
}: ScopeFilterBarProps) {
  const notification = useNotification()
  const { t } = useTranslation('memory')
  const { data: options } = useMemoryScopeOptions(scope)

  const [expanded, setExpanded] = useState(false)
  const searchLimitNotified = useRef(false)

  const update = (patch: Partial<MemoryFilterState>) => {
    // 任何筛选变化都把页码置回 1，否则会停在一个已经不存在的页上
    onChange({ ...value, ...patch, page: 1 })
  }

  const showField = (field: keyof MemoryFilterState) =>
    fields.includes(field) && (field !== 'domain' || Boolean(scope.instance_key))

  const visibleAdvancedFields = ADVANCED_FIELDS.filter(showField)
  const activeAdvancedCount = visibleAdvancedFields.filter(field => isFieldActive(value, field)).length

  const statusOptions = MEMORY_STATUS_FILTERS.map(status => ({
    value: status,
    label: t(`status.${status}`),
  }))

  const channelOptions = [
    { value: '', label: t('filters.allChannels') },
    ...(options?.channels ?? []).map(channel => ({
      value: channel.chat_key,
      label: channel.channel_name || channel.chat_key,
    })),
  ]

  const presetOptions = [
    { value: '', label: t('filters.allPresets') },
    ...(options?.presets ?? []).map(preset => ({
      value: String(preset.preset_id),
      label: preset.preset_id === 0 ? t('preset.none') : preset.name,
    })),
  ]

  const partitionOptions = PARTITIONS.map(partition => ({
    value: partition,
    label: t(`partition.${partition}`),
  }))

  const domainOptions = MEMORY_DOMAINS.map(domain => ({
    value: domain,
    label: t(`domain.${domain}`),
  }))

  const cognitiveOptions = [
    { value: '', label: t('filters.all') },
    ...COGNITIVE_TYPES.map(cognitive => ({
      value: cognitive,
      label: t(`cognitive.${cognitive}`),
    })),
  ]

  const knowledgeOptions = [
    { value: '', label: t('filters.all') },
    ...KNOWLEDGE_TYPES.map(knowledge => ({
      value: knowledge,
      label: t(`knowledge.${knowledge}`),
    })),
  ]

  // 来源类型：空串是「全部」，与后端 `origin_kind=` 留空等价
  const originOptions = [
    { value: '', label: t('filters.allOrigins') },
    ...ORIGIN_KINDS.map(origin => ({ value: origin, label: t(`origin.${origin}`) })),
  ]

  const sortOptions = SORTS.map(sort => ({ value: sort, label: t(`sort.${sort}`) }))
  const orderOptions = ORDERS.map(order => ({ value: order, label: t(`order.${order}`) }))

  const handleSearchChange = (next: string) => {
    if (next.length > MEMORY_SEARCH_MAX_LENGTH) {
      if (!searchLimitNotified.current) {
        searchLimitNotified.current = true
        notification.warning(
          t('filters.searchTooLong', { max: MEMORY_SEARCH_MAX_LENGTH })
        )
      }
      update({ search: next.slice(0, MEMORY_SEARCH_MAX_LENGTH) })
      return
    }
    update({ search: next })
  }

  const basicRow = (
    <Box
      sx={{
        display: 'flex',
        flexDirection: { xs: 'column', sm: 'row' },
        flexWrap: { sm: 'wrap' },
        gap: 1,
        alignItems: { xs: 'stretch', sm: 'center' },
      }}
    >
      {leading && (
        <Box sx={{ flexShrink: 0, width: { xs: '100%', sm: 'auto' } }}>{leading}</Box>
      )}
      {showField('search') && (
        <Box sx={{ flexGrow: 1, minWidth: { sm: 240 }, width: { xs: '100%', sm: 'auto' } }}>
          <SearchField
            value={value.search}
            onChange={handleSearchChange}
            onClear={() => update({ search: '' })}
            placeholder={t('filters.searchPlaceholder')}
            clearAriaLabel={t('filters.clearSearch')}
            fullWidth
          />
        </Box>
      )}
      {showField('status') && (
        <FilterSlot>
          <FilterSelect
            label={t('filters.status')}
            value={value.status}
            options={statusOptions}
            onChange={next => update({ status: next as MemoryStatusFilter })}
          />
        </FilterSlot>
      )}
      {showField('chatKey') && (
        <FilterSlot wide>
          <FilterSelect
            label={t('filters.channel')}
            value={value.chatKey}
            options={channelOptions}
            onChange={next => update({ chatKey: next })}
          />
        </FilterSlot>
      )}
      {showField('presetId') && (
        <FilterSlot wide>
          <FilterSelect
            label={t('filters.preset')}
            value={value.presetId === null ? '' : String(value.presetId)}
            options={presetOptions}
            onChange={next => update({ presetId: next === '' ? null : Number(next) })}
          />
        </FilterSlot>
      )}
      {showField('originKind') && (
        <FilterSlot wide>
          <FilterSelect
            label={t('filters.origin')}
            value={value.originKind}
            options={originOptions}
            onChange={next => update({ originKind: next as MemoryFilterState['originKind'] })}
          />
        </FilterSlot>
      )}
      {visibleAdvancedFields.length > 0 && (
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<FilterListIcon />}
          onClick={() => setExpanded(previous => !previous)}
          sx={{ flexShrink: 0, whiteSpace: 'nowrap' }}
        >
          {activeAdvancedCount > 0
            ? t('filters.moreWithCount', { count: activeAdvancedCount })
            : t('filters.more')}
        </ActionButton>
      )}
    </Box>
  )

  const advancedRow = expanded && visibleAdvancedFields.length > 0 && (
    <Box
      sx={{
        display: 'flex',
        flexDirection: { xs: 'column', sm: 'row' },
        flexWrap: { sm: 'wrap' },
        gap: 1,
        alignItems: { xs: 'stretch', sm: 'center' },
      }}
    >
      {showField('partition') && (
        <FilterSlot>
          <FilterSelect
            label={t('filters.partition')}
            value={value.partition}
            options={partitionOptions}
            onChange={next => update({ partition: next as MemoryFilterState['partition'] })}
          />
        </FilterSlot>
      )}
      {showField('domain') && (
        <FilterSlot>
          <FilterSelect
            label={t('filters.domain')}
            value={value.domain}
            options={domainOptions}
            onChange={next => update({ domain: next as MemoryFilterState['domain'] })}
          />
        </FilterSlot>
      )}
      {showField('person') && (
        <FilterSlot wide>
          <TextField
            label={t('filters.person')}
            placeholder={t('filters.personPlaceholder')}
            value={value.person}
            size="small"
            fullWidth
            sx={INPUT_VARIANTS.default.styles}
            onChange={event => update({ person: event.target.value })}
          />
        </FilterSlot>
      )}
      {showField('cognitiveType') && (
        <FilterSlot>
          <FilterSelect
            label={t('filters.cognitive')}
            value={value.cognitiveType}
            options={cognitiveOptions}
            onChange={next =>
              update({ cognitiveType: next as MemoryFilterState['cognitiveType'] })
            }
          />
        </FilterSlot>
      )}
      {showField('knowledgeType') && (
        <FilterSlot>
          <FilterSelect
            label={t('filters.knowledge')}
            value={value.knowledgeType}
            options={knowledgeOptions}
            onChange={next =>
              update({ knowledgeType: next as MemoryFilterState['knowledgeType'] })
            }
          />
        </FilterSlot>
      )}
      {showField('timeFrom') && (
        <FilterSlot>
          <TextField
            label={t('filters.timeFrom')}
            type="date"
            value={value.timeFrom}
            size="small"
            fullWidth
            InputLabelProps={{ shrink: true }}
            sx={INPUT_VARIANTS.default.styles}
            onChange={event => update({ timeFrom: event.target.value })}
          />
        </FilterSlot>
      )}
      {showField('timeTo') && (
        <FilterSlot>
          <TextField
            label={t('filters.timeTo')}
            type="date"
            value={value.timeTo}
            size="small"
            fullWidth
            InputLabelProps={{ shrink: true }}
            sx={INPUT_VARIANTS.default.styles}
            onChange={event => update({ timeTo: event.target.value })}
          />
        </FilterSlot>
      )}
      {showField('sort') && (
        <FilterSlot>
          <FilterSelect
            label={t('filters.sort')}
            value={value.sort}
            options={sortOptions}
            onChange={next => update({ sort: next as MemoryFilterState['sort'] })}
          />
        </FilterSlot>
      )}
      {showField('order') && (
        <FilterSlot>
          <FilterSelect
            label={t('filters.order')}
            value={value.order}
            options={orderOptions}
            onChange={next => update({ order: next as MemoryFilterState['order'] })}
          />
        </FilterSlot>
      )}
      <ActionButton
        tone="ghost"
        size="small"
        startIcon={<ClearAllIcon />}
        onClick={() => onChange({ ...DEFAULT_MEMORY_FILTERS })}
        sx={{ flexShrink: 0, whiteSpace: 'nowrap' }}
      >
        {t('filters.clear')}
      </ActionButton>
    </Box>
  )

  // 窄屏没有更多空间时把两行纵向排开；宽屏下也是上下两行，展开不会挤掉主行
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, width: '100%' }}>
      {basicRow}
      {advancedRow}
    </Box>
  )
}
