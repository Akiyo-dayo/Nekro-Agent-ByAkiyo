import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import SummariesPanel from '../../components/memory/SummariesPanel'
import {
  filtersFromSearchParams,
  filtersToSearchParams,
} from '../../components/memory/filters'
import type { MemoryScopeParams } from '../../services/api/memory'

/**
 * 「频道摘要」标签
 *
 * 筛选与分页属于刷新后应当还在的状态，所以走 URL：与「记忆条目」共用同一套
 * `filters` 互转，`chat` / `preset` / `page` 的键名全站一致。
 */
export default function SummariesTab({ scope }: { scope: MemoryScopeParams }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => filtersFromSearchParams(searchParams), [searchParams])

  return (
    <SummariesPanel
      scope={scope}
      filters={filters}
      onFiltersChange={next => setSearchParams(filtersToSearchParams(next))}
    />
  )
}
