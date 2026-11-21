import { useMemo } from 'react'
import { Badge, Box, Tab, Typography, useMediaQuery, useTheme } from '@mui/material'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import IconActionButton from '../../components/common/IconActionButton'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import { PageTabs } from '../../components/common/NekroTabs'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import EmbeddingHealthBanner from '../../components/memory/EmbeddingHealthBanner'
import DomainSettingsButton from '../../components/settings/DomainSettingsButton'
import {
  DEFAULT_MEMORY_FILTERS,
  MEMORY_TAB_FILTER_FIELDS,
  filtersFromSearchParams,
  filtersToSearchParams,
  type MemoryFilterState,
} from '../../components/memory/filters'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import {
  chatChannelPath,
  isMemoryPageTab,
  memoryPath,
  type MemoryPageTab,
} from '../../router/routes'
import { memoryApi, type MemoryScopeParams } from '../../services/api/memory'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import GraphTab from './GraphTab'
import ItemsTab from './ItemsTab'
import QuarantineTab from './QuarantineTab'
import type { MemoryPageTabDefinition } from './types'

/**
 * 记忆管理页
 *
 * 主视图（当前在哪个标签）进 path，筛选、分页与排序进 query；切标签时只把新标签也认的
 * 筛选项带过去（见 `MEMORY_TAB_FILTER_FIELDS`），其余回落默认，避免在隔离区里看到一条
 * 只对「记忆条目」有意义的搜索词。
 *
 * 作用域跟随顶栏的实例切换器。平台方不选实例时看全部；租户没有全局视图，越权访问
 * 由服务端按 404 处理，页面据此显示「未找到」而不是「你无权查看」。
 */

const MEMORY_PAGE_TAB_DEFS: MemoryPageTabDefinition[] = [
  {
    key: 'graph',
    labelKey: 'tabs.graph',
    render: context => <GraphTab scope={context.scope} isScopeReady={context.isScopeReady} />,
  },
  {
    key: 'items',
    labelKey: 'tabs.list',
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

  // 标签上的计数直接取隔离列表的 total，不新增接口；列表失效时跟着 ['memory'] 一起刷新
  const quarantineCountQuery = useQuery({
    queryKey: ['memory', 'quarantine-count', scope],
    queryFn: () => memoryApi.listQuarantine({ page: 1, page_size: 1, ...scope }),
    enabled: isScopeReady,
    retry: false,
  })
  const quarantineCount = quarantineCountQuery.data?.total ?? 0

  // 旧地址：摘要 / 提示词明细已从记忆页移走，带 chatKey 时进频道详情的记忆标签
  if (tabParam === 'summaries' || tabParam === 'prompt-rounds') {
    const chatKey = searchParams.get('chatKey')
    if (chatKey) {
      const view = tabParam === 'summaries' ? 'summaries' : 'rounds'
      return (
        <Navigate to={`${chatChannelPath(chatKey, 'memory')}?view=${view}`} replace />
      )
    }
    return <Navigate to={memoryPath('graph')} replace />
  }

  if (!isMemoryPageTab(tabParam)) {
    return <Navigate to={memoryPath()} replace />
  }

  const tab: MemoryPageTab = tabParam
  const activeDefinition =
    MEMORY_PAGE_TAB_DEFS.find(definition => definition.key === tab) ?? MEMORY_PAGE_TAB_DEFS[0]

  // 切标签只带新标签认识的筛选项，其余回落默认、页码回到第一页：
  // 隔离区不认「知识类型」，把上一个标签的搜索词原样带过去只会让列表一直是空的。
  const handleTabChange = (nextTab: MemoryPageTab) => {
    const carried: MemoryFilterState = { ...DEFAULT_MEMORY_FILTERS, page: 1 }
    MEMORY_TAB_FILTER_FIELDS[nextTab].forEach(field => {
      Object.assign(carried, { [field]: filters[field] })
    })
    navigate(memoryPath(nextTab, Object.fromEntries(filtersToSearchParams(carried))))
  }

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
        <SimpleTooltip title={tCommon('actions.refresh')}>
          <span>
            <IconActionButton
              aria-label={tCommon('actions.refresh')}
              onClick={() => void queryClient.invalidateQueries({ queryKey: ['memory'] })}
            >
              <RefreshRoundedIcon />
            </IconActionButton>
          </span>
        </SimpleTooltip>
        <DomainSettingsButton domain="memory" />
      </Box>

      <InstanceScopeBanner description={t('scopeBanner')} />
      <EmbeddingHealthBanner />

      <Box sx={{ flexShrink: 0, ...(UNIFIED_TABLE_STYLES.pageTabContainer as object) }}>
        <PageTabs
          value={tab}
          onChange={(_event, value: MemoryPageTab) => handleTabChange(value)}
          variant={isMobile ? 'scrollable' : 'standard'}
          scrollButtons="auto"
        >
          {MEMORY_PAGE_TAB_DEFS.map(definition => (
            <Tab
              key={definition.key}
              value={definition.key}
              label={
                definition.key === 'quarantine' && quarantineCount > 0 ? (
                  // 计数放在标签名右侧、与文字同一行：默认的右上角角标会被 Tab 的 overflow 裁掉
                  <Badge
                    color="warning"
                    badgeContent={quarantineCount}
                    max={99}
                    sx={{
                      alignItems: 'center',
                      '& .MuiBadge-badge': {
                        position: 'static',
                        transform: 'none',
                        ml: 0.75,
                      },
                    }}
                  >
                    <span>{t(definition.labelKey)}</span>
                  </Badge>
                ) : (
                  t(definition.labelKey)
                )
              }
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
