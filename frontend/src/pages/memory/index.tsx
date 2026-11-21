import { useMemo } from 'react'
import { Box, Tab, Typography, useMediaQuery, useTheme } from '@mui/material'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import ActionButton from '../../components/common/ActionButton'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import { PageTabs } from '../../components/common/NekroTabs'
import { filtersFromSearchParams } from '../../components/memory/filters'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { isMemoryPageTab, memoryPath, type MemoryPageTab } from '../../router/routes'
import type { MemoryScopeParams } from '../../services/api/memory'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import ItemsTab from './ItemsTab'
import PromptRoundsTab from './PromptRoundsTab'
import QuarantineTab from './QuarantineTab'
import SummariesTab from './SummariesTab'
import type { MemoryPageTabDefinition } from './types'

/**
 * 记忆管理页
 *
 * 主视图（当前在哪个标签）进 path，筛选、分页与排序进 query；切标签时不带上一个标签的
 * 筛选，避免在隔离区里看到一条只对「记忆条目」有意义的搜索词。
 *
 * 作用域跟随顶栏的实例切换器。平台方不选实例时看全部；租户没有全局视图，越权访问
 * 由服务端按 404 处理，页面据此显示「未找到」而不是「你无权查看」。
 */

const MEMORY_PAGE_TAB_DEFS: MemoryPageTabDefinition[] = [
  {
    key: 'items',
    labelKey: 'tabs.items',
    render: context => (
      <ItemsTab scope={context.scope} isScopeReady={context.isScopeReady} />
    ),
  },
  {
    key: 'quarantine',
    labelKey: 'tabs.quarantine',
    render: context => (
      <QuarantineTab scope={context.scope} isScopeReady={context.isScopeReady} />
    ),
  },
  {
    key: 'summaries',
    labelKey: 'tabs.summaries',
    render: context => <SummariesTab scope={context.scope} />,
  },
  {
    key: 'prompt-rounds',
    labelKey: 'tabs.promptRounds',
    render: context => <PromptRoundsTab scope={context.scope} />,
  },
]

export default function MemoryPage() {
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { t } = useTranslation('memory')
  const { t: tCommon } = useTranslation('common')
  const { tab: tabParam } = useParams<{ tab?: string }>()
  const [searchParams] = useSearchParams()
  const { currentInstanceKey, currentInstance, isScopeReady } = useInstanceScope()

  const filters = useMemo(() => filtersFromSearchParams(searchParams), [searchParams])
  const scope = useMemo<MemoryScopeParams>(
    () => ({
      instance_key: currentInstanceKey ?? undefined,
      adapter_key: currentInstance?.adapter_key,
      domain: filters.domain,
    }),
    [currentInstanceKey, currentInstance?.adapter_key, filters.domain]
  )

  if (!isMemoryPageTab(tabParam)) {
    return <Navigate to={memoryPath()} replace />
  }

  const tab: MemoryPageTab = tabParam
  const activeDefinition =
    MEMORY_PAGE_TAB_DEFS.find(definition => definition.key === tab) ?? MEMORY_PAGE_TAB_DEFS[0]

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 3 },
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        overflow: 'hidden',
        boxSizing: 'border-box',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.5, flexWrap: 'wrap' }}>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography variant="h6" fontWeight={700}>
            {t('title')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('subtitle')}
          </Typography>
        </Box>
        <ActionButton
          tone="secondary"
          startIcon={<RefreshRoundedIcon />}
          onClick={() => void queryClient.invalidateQueries({ queryKey: ['memory'] })}
        >
          {tCommon('actions.refresh')}
        </ActionButton>
      </Box>

      <InstanceScopeBanner description={t('scopeBanner')} />

      <Box sx={{ flexShrink: 0, ...(UNIFIED_TABLE_STYLES.pageTabContainer as object) }}>
        <PageTabs
          value={tab}
          onChange={(_event, value: MemoryPageTab) => navigate(memoryPath(value))}
          variant={isMobile ? 'scrollable' : 'standard'}
          scrollButtons="auto"
        >
          {MEMORY_PAGE_TAB_DEFS.map(definition => (
            <Tab
              key={definition.key}
              value={definition.key}
              label={t(definition.labelKey)}
            />
          ))}
        </PageTabs>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {activeDefinition.render({ scope, isScopeReady })}
      </Box>
    </Box>
  )
}
