import { useRef, useState, type ReactNode } from 'react'
import { Box, Chip, TextField } from '@mui/material'
import { useTranslation } from 'react-i18next'
import FilterListIcon from '@mui/icons-material/FilterList'
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
  type MemorySensitiveFilter,
} from './filters'
import MoreFiltersPopover from './MoreFiltersPopover'
import PersonFilterField from './PersonFilterField'
import { useMemoryScopeOptions } from './useMemoryScopeOptions'

/**
 * 记忆页筛选栏
 *
 * 频道、人设与人的选项只来自 `GET /memory/scope-options`：那份响应已经按当前身份裁剪，
 * 从别的列表拼一份出来就等于多了一条会飘的授权判据。
 *
 * 组件是受控的：每个标签页只用 `fields` 声明自己支持哪几项。episode 没有「某个人 /
 * 认知类型 / 知识类型」，后端会直接 400，所以在这里就不画。
 *
 * 主行只放最常用的搜索、频道、人与状态，其余条件收进「更多筛选」浮层；已经生效的
 * 高级条件会在主行下方以可删除的 Chip 列出，关掉浮层也看得见当前筛了什么。
 */

// 「涉及的人」不在这里：按人筛选是记忆页最常用的一项，放在主行（候选带昵称与称呼）
const ADVANCED_FIELDS: Array<keyof MemoryFilterState> = [
  'presetId',
  'originKind',
  'partition',
  'domain',
  'cognitiveType',
  'sensitive',
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
  /** 与筛选同一行、排在右端的控件（段落列表的平铺 / 按人 / 按事件） */
  trailing?: ReactNode
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
    case 'sensitive':
      return value.sensitive !== ''
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

/**
 * 控件在窄屏整行、宽屏固定宽度；视觉上与 FilterSelect 同高
 *
 * 宽度按 1280 屏、侧栏展开时一行放下「段落|事件 · 搜索 · 频道 · 人 · 状态 · 更多筛选 ·
 * 平铺|按人|按主题」来定，改宽之前先算一遍总宽，免得主行折成两行。
 */
function FilterSlot({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <Box sx={{ width: { xs: '100%', sm: wide ? 160 : 120 }, flexShrink: 0 }}>{children}</Box>
  )
}

/** 浮层里的控件占满一格，不跟随主行的固定宽度 */
function AdvancedSlot({ children }: { children: ReactNode }) {
  return <Box sx={{ minWidth: 0, width: '100%' }}>{children}</Box>
}

export default function ScopeFilterBar({
  value,
  onChange,
  fields,
  scope,
  leading,
  trailing,
}: ScopeFilterBarProps) {
  const notification = useNotification()
  const { t } = useTranslation('memory')
  const { data: options } = useMemoryScopeOptions(scope)

  const [moreAnchor, setMoreAnchor] = useState<HTMLElement | null>(null)
  const searchLimitNotified = useRef(false)

  const update = (patch: Partial<MemoryFilterState>) => {
    // 任何筛选变化都把页码置回 1，否则会停在一个已经不存在的页上
    onChange({ ...value, ...patch, page: 1 })
  }

  const showField = (field: keyof MemoryFilterState) =>
    fields.includes(field) && (field !== 'domain' || Boolean(scope.instance_key))

  const visibleAdvancedFields = ADVANCED_FIELDS.filter(showField)
  const activeAdvancedFields = visibleAdvancedFields.filter(field => isFieldActive(value, field))
  const activeAdvancedCount = activeAdvancedFields.length

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

  const sensitiveOptions = [
    { value: '', label: t('filters.sensitiveAll') },
    { value: 'only', label: t('filters.sensitiveOnly') },
    { value: 'exclude', label: t('filters.sensitiveExclude') },
  ]

  // 来源类型：空串是「全部」，与后端 `origin_kind=` 留空等价
  const originOptions = [
    { value: '', label: t('filters.allOrigins') },
    ...ORIGIN_KINDS.map(origin => ({ value: origin, label: t(`origin.${origin}`) })),
  ]

  const sortOptions = SORTS.map(sort => ({ value: sort, label: t(`sort.${sort}`) }))
  const orderOptions = ORDERS.map(order => ({ value: order, label: t(`order.${order}`) }))

  const chipValue = (label: string, text: string) => t('filters.chipValue', { label, value: text })

  /** 已生效的高级条件怎么读；不生效时返回 null */
  const advancedChipLabel = (field: keyof MemoryFilterState): string | null => {
    switch (field) {
      case 'presetId':
        return value.presetId === null
          ? null
          : chipValue(
              t('filters.preset'),
              presetOptions.find(option => option.value === String(value.presetId))?.label ??
                String(value.presetId)
            )
      case 'originKind':
        return value.originKind
          ? chipValue(t('filters.origin'), t(`origin.${value.originKind}`))
          : null
      case 'partition':
        return value.partition !== DEFAULT_MEMORY_FILTERS.partition
          ? chipValue(t('filters.partition'), t(`partition.${value.partition}`))
          : null
      case 'domain':
        return value.domain !== DEFAULT_MEMORY_FILTERS.domain
          ? chipValue(t('filters.domain'), t(`domain.${value.domain}`))
          : null
      case 'cognitiveType':
        return value.cognitiveType
          ? chipValue(t('filters.cognitive'), t(`cognitive.${value.cognitiveType}`))
          : null
      case 'sensitive':
        if (!value.sensitive) return null
        return chipValue(
          t('filters.sensitive'),
          t(value.sensitive === 'only' ? 'filters.sensitiveOnly' : 'filters.sensitiveExclude')
        )
      case 'knowledgeType':
        return value.knowledgeType
          ? chipValue(t('filters.knowledge'), t(`knowledge.${value.knowledgeType}`))
          : null
      case 'timeFrom':
        return value.timeFrom ? chipValue(t('filters.timeFrom'), value.timeFrom) : null
      case 'timeTo':
        return value.timeTo ? chipValue(t('filters.timeTo'), value.timeTo) : null
      case 'sort':
        return value.sort !== DEFAULT_MEMORY_FILTERS.sort
          ? chipValue(t('filters.sort'), t(`sort.${value.sort}`))
          : null
      case 'order':
        return value.order !== DEFAULT_MEMORY_FILTERS.order
          ? chipValue(t('filters.order'), t(`order.${value.order}`))
          : null
      default:
        return null
    }
  }

  /** 删掉某一个高级条件 */
  const clearAdvancedField = (field: keyof MemoryFilterState) => {
    switch (field) {
      case 'presetId':
        update({ presetId: null })
        break
      case 'originKind':
        update({ originKind: '' })
        break
      case 'partition':
        update({ partition: DEFAULT_MEMORY_FILTERS.partition })
        break
      case 'domain':
        update({ domain: DEFAULT_MEMORY_FILTERS.domain })
        break
      case 'cognitiveType':
        update({ cognitiveType: '' })
        break
      case 'sensitive':
        update({ sensitive: DEFAULT_MEMORY_FILTERS.sensitive })
        break
      case 'knowledgeType':
        update({ knowledgeType: '' })
        break
      case 'timeFrom':
        update({ timeFrom: '' })
        break
      case 'timeTo':
        update({ timeTo: '' })
        break
      case 'sort':
        update({ sort: DEFAULT_MEMORY_FILTERS.sort })
        break
      case 'order':
        update({ order: DEFAULT_MEMORY_FILTERS.order })
        break
      default:
        break
    }
  }

  /** 浮层底部的「清空」只清高级条件，主行的关键词与选择保留 */
  const clearAdvanced = () => {
    onChange({
      ...value,
      presetId: DEFAULT_MEMORY_FILTERS.presetId,
      originKind: DEFAULT_MEMORY_FILTERS.originKind,
      partition: DEFAULT_MEMORY_FILTERS.partition,
      domain: DEFAULT_MEMORY_FILTERS.domain,
      cognitiveType: DEFAULT_MEMORY_FILTERS.cognitiveType,
      sensitive: DEFAULT_MEMORY_FILTERS.sensitive,
      knowledgeType: DEFAULT_MEMORY_FILTERS.knowledgeType,
      timeFrom: DEFAULT_MEMORY_FILTERS.timeFrom,
      timeTo: DEFAULT_MEMORY_FILTERS.timeTo,
      sort: DEFAULT_MEMORY_FILTERS.sort,
      order: DEFAULT_MEMORY_FILTERS.order,
      page: 1,
    })
  }

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
        <Box
          sx={{
            flex: { sm: '1 1 140px' },
            minWidth: { sm: 140 },
            width: { xs: '100%', sm: 'auto' },
          }}
        >
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
      {showField('person') && (
        <FilterSlot wide>
          <PersonFilterField
            value={value.person}
            onChange={next => update({ person: next })}
            scope={scope}
            filters={value}
          />
        </FilterSlot>
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
      {visibleAdvancedFields.length > 0 && (
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<FilterListIcon />}
          onClick={event => setMoreAnchor(event.currentTarget)}
          sx={{ flexShrink: 0, whiteSpace: 'nowrap' }}
        >
          {activeAdvancedCount > 0
            ? t('filters.moreWithCount', { count: activeAdvancedCount })
            : t('filters.more')}
        </ActionButton>
      )}
      {trailing && (
        <Box sx={{ flexShrink: 0, width: { xs: '100%', sm: 'auto' }, ml: { sm: 'auto' } }}>
          {trailing}
        </Box>
      )}
    </Box>
  )

  const activeChips = activeAdvancedFields.length > 0 && (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75 }}>
      {activeAdvancedFields.map(field => {
        const label = advancedChipLabel(field)
        if (!label) return null
        return (
          <Chip
            key={field}
            size="small"
            label={label}
            onDelete={() => clearAdvancedField(field)}
          />
        )
      })}
    </Box>
  )

  const advancedFields = (
    <>
      {showField('presetId') && (
        <AdvancedSlot>
          <FilterSelect
            label={t('filters.preset')}
            value={value.presetId === null ? '' : String(value.presetId)}
            options={presetOptions}
            onChange={next => update({ presetId: next === '' ? null : Number(next) })}
          />
        </AdvancedSlot>
      )}
      {showField('originKind') && (
        <AdvancedSlot>
          <FilterSelect
            label={t('filters.origin')}
            value={value.originKind}
            options={originOptions}
            onChange={next => update({ originKind: next as MemoryFilterState['originKind'] })}
          />
        </AdvancedSlot>
      )}
      {showField('partition') && (
        <AdvancedSlot>
          <FilterSelect
            label={t('filters.partition')}
            value={value.partition}
            options={partitionOptions}
            onChange={next => update({ partition: next as MemoryFilterState['partition'] })}
          />
        </AdvancedSlot>
      )}
      {showField('domain') && (
        <AdvancedSlot>
          <FilterSelect
            label={t('filters.domain')}
            value={value.domain}
            options={domainOptions}
            onChange={next => update({ domain: next as MemoryFilterState['domain'] })}
          />
        </AdvancedSlot>
      )}
      {showField('cognitiveType') && (
        <AdvancedSlot>
          <FilterSelect
            label={t('filters.cognitive')}
            value={value.cognitiveType}
            options={cognitiveOptions}
            onChange={next =>
              update({ cognitiveType: next as MemoryFilterState['cognitiveType'] })
            }
          />
        </AdvancedSlot>
      )}
      {showField('sensitive') && (
        <AdvancedSlot>
          <FilterSelect
            label={t('filters.sensitive')}
            value={value.sensitive}
            options={sensitiveOptions}
            onChange={next => update({ sensitive: next as MemorySensitiveFilter })}
          />
        </AdvancedSlot>
      )}
      {showField('knowledgeType') && (
        <AdvancedSlot>
          <FilterSelect
            label={t('filters.knowledge')}
            value={value.knowledgeType}
            options={knowledgeOptions}
            onChange={next =>
              update({ knowledgeType: next as MemoryFilterState['knowledgeType'] })
            }
          />
        </AdvancedSlot>
      )}
      {showField('timeFrom') && (
        <AdvancedSlot>
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
        </AdvancedSlot>
      )}
      {showField('timeTo') && (
        <AdvancedSlot>
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
        </AdvancedSlot>
      )}
      {showField('sort') && (
        <AdvancedSlot>
          <FilterSelect
            label={t('filters.sort')}
            value={value.sort}
            options={sortOptions}
            onChange={next => update({ sort: next as MemoryFilterState['sort'] })}
          />
        </AdvancedSlot>
      )}
      {showField('order') && (
        <AdvancedSlot>
          <FilterSelect
            label={t('filters.order')}
            value={value.order}
            options={orderOptions}
            onChange={next => update({ order: next as MemoryFilterState['order'] })}
          />
        </AdvancedSlot>
      )}
    </>
  )

  // 窄屏没有更多空间时把各行纵向排开；宽屏下也是上下两行，展开不会挤掉主行
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, width: '100%' }}>
      {basicRow}
      {activeChips}
      <MoreFiltersPopover
        open={Boolean(moreAnchor)}
        anchorEl={moreAnchor}
        onClose={() => setMoreAnchor(null)}
        onClear={clearAdvanced}
      >
        {advancedFields}
      </MoreFiltersPopover>
    </Box>
  )
}
