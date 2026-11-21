import { useMemo } from 'react'
import { Autocomplete, Box, TextField, Typography } from '@mui/material'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { INPUT_VARIANTS } from '../../theme/variants'
import { memoryApi, type MemoryPersonItem, type MemoryScopeParams } from '../../services/api/memory'
import { filtersToGroupQuery, type MemoryFilterState } from './filters'
import { personMatches, personPrimaryName, personTooltipLine } from './personLabels'

/**
 * 「按人筛选」
 *
 * 候选来自 `GET /memory/persons`：当前作用域与筛选下记忆里出现过的人，按段数倒序取前一百个，
 * 每个带昵称与称呼。输入框在本地按昵称、称呼、账号筛候选；不在候选里的账号也能直接敲进去
 * （回车或离开输入框即生效），所以候选截断不会让人筛不到。
 *
 * 值仍然是账号：URL 里的 `person` 与改造前一样，按人筛选的语义（选了实例时按别名展开）也不变。
 */

const CANDIDATE_LIMIT = 100

interface PersonFilterFieldProps {
  value: string
  onChange: (account: string) => void
  scope: MemoryScopeParams
  /** 候选按当前其他筛选取，与列表看到的那一批对得上 */
  filters: MemoryFilterState
}

export default function PersonFilterField({ value, onChange, scope, filters }: PersonFilterFieldProps) {
  const { t } = useTranslation('memory')
  const { isScopeReady } = useInstanceScope()
  const query = useMemo(() => filtersToGroupQuery(filters, 'person'), [filters])

  const candidatesQuery = useQuery({
    queryKey: ['memory', 'person-candidates', scope, query],
    queryFn: () =>
      memoryApi.listPersons({ ...query, page: 1, page_size: CANDIDATE_LIMIT, ...scope }),
    enabled: isScopeReady,
    retry: false,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  })

  const candidates = candidatesQuery.data?.items ?? []
  const selected = candidates.find(item => item.account === value || item.accounts.includes(value)) ?? null
  const callNameLabel = t('persons.callName')
  const fromAffectionLabel = t('persons.fromAffection')

  return (
    <Autocomplete<MemoryPersonItem, false, false, true>
      freeSolo
      autoSelect
      size="small"
      options={candidates}
      value={selected ?? (value || null)}
      loading={candidatesQuery.isFetching}
      onChange={(_event, next) => {
        if (next === null) onChange('')
        else if (typeof next === 'string') onChange(next.trim())
        else onChange(next.account)
      }}
      isOptionEqualToValue={(option, current) =>
        typeof current === 'string' ? option.account === current : option.account === current.account
      }
      getOptionLabel={option =>
        typeof option === 'string' ? option : personPrimaryName(option)
      }
      filterOptions={(options, state) => options.filter(option => personMatches(option, state.inputValue))}
      renderOption={(props, option) => {
        const { key, ...optionProps } = props as typeof props & { key: string }
        return (
          <Box component="li" key={key} {...optionProps}>
            <Box sx={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
              <Typography variant="body2" noWrap title={personTooltipLine(option, callNameLabel, fromAffectionLabel)}>
                {personPrimaryName(option)}
                {option.call_name.trim() ? `（${option.call_name.trim()}）` : ''}
              </Typography>
              <Typography variant="caption" color="text.secondary" noWrap>
                {t('persons.optionMeta', { account: option.account, count: option.paragraph_count })}
              </Typography>
            </Box>
          </Box>
        )
      }}
      renderInput={params => (
        <TextField
          {...params}
          label={t('filters.person')}
          placeholder={t('filters.personPlaceholder')}
          sx={INPUT_VARIANTS.default.styles}
        />
      )}
    />
  )
}
