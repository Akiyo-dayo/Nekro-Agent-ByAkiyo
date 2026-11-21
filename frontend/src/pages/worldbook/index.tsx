import { useEffect, useMemo, useRef } from 'react'
import { Box, Tab, Typography, useMediaQuery, useTheme } from '@mui/material'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import ActionButton from '../../components/common/ActionButton'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import { PageTabs } from '../../components/common/NekroTabs'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import {
  isWorldbookPageTab,
  worldbookPath,
  type WorldbookPageTab,
} from '../../router/routes'
import type { WorldbookScopeParams } from '../../services/api/worldbook'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import BooksTab from './BooksTab'
import QuarantineTab from './QuarantineTab'
import TriggerTestTab from './TriggerTestTab'
import type { WorldbookPageTabDefinition } from './types'

/**
 * 世界书管理页
 *
 * 主视图（当前在哪个标签）进 path，筛选与分页进 query；切标签时不带上一个标签的
 * 筛选，避免在隔离区里看到一条只对「书」有意义的搜索词。
 *
 * 作用域跟随顶栏的实例切换器。平台方不选实例时看全部；租户没有全局视图，越权访问
 * 由服务端按 404 处理，页面据此显示「未找到」而不是「你无权查看」。
 */

const WORLDBOOK_PAGE_TAB_DEFS: WorldbookPageTabDefinition[] = [
  {
    key: 'books',
    labelKey: 'tabs.books',
    render: context => (
      <BooksTab scope={context.scope} isScopeReady={context.isScopeReady} />
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
    key: 'test',
    labelKey: 'tabs.test',
    // 换实例就整个重新挂载：选过的频道、人设和上一次的结果都属于旧实例，留着会让
    // 「运行」拿旧频道去测、结果却显示在新实例的页面上
    render: context => (
      <TriggerTestTab
        key={`${context.scope.adapter_key ?? ''}|${context.scope.instance_key ?? ''}`}
        scope={context.scope}
        isScopeReady={context.isScopeReady}
      />
    ),
  },
]

export default function WorldbookPage() {
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { t } = useTranslation('worldbook')
  const { t: tCommon } = useTranslation('common')
  const { tab: tabParam } = useParams<{ tab?: string }>()
  const [searchParams] = useSearchParams()
  const { currentInstanceKey, currentInstance, isScopeReady } = useInstanceScope()

  const scope = useMemo<WorldbookScopeParams>(
    () => ({
      instance_key: currentInstanceKey ?? undefined,
      adapter_key: currentInstance?.adapter_key,
    }),
    [currentInstanceKey, currentInstance?.adapter_key]
  )

  // 用户在顶栏换了实例：页码与人设、频道这类筛选都属于旧实例，带过去会落在一个空页上
  // （新实例可能只有一页书），看起来像「一本书都没有」。换实例时把当前标签的查询串清掉。
  // 作用域第一次确定（冷启动时从「未确定」到「确定」）不算换实例，否则分享来的带筛选
  // 的地址一打开就被冲掉。
  const scopeKey = `${scope.adapter_key ?? ''}|${scope.instance_key ?? ''}`
  const settledScopeKey = useRef<string | null>(null)
  const hasQuery = searchParams.toString() !== ''
  useEffect(() => {
    if (!isScopeReady) return
    if (settledScopeKey.current === null) {
      settledScopeKey.current = scopeKey
      return
    }
    if (settledScopeKey.current === scopeKey) return
    settledScopeKey.current = scopeKey
    if (hasQuery && isWorldbookPageTab(tabParam)) {
      navigate(worldbookPath(tabParam), { replace: true })
    }
  }, [isScopeReady, scopeKey, hasQuery, tabParam, navigate])

  if (!isWorldbookPageTab(tabParam)) {
    return <Navigate to={worldbookPath()} replace />
  }

  const tab: WorldbookPageTab = tabParam
  const activeDefinition =
    WORLDBOOK_PAGE_TAB_DEFS.find(definition => definition.key === tab) ??
    WORLDBOOK_PAGE_TAB_DEFS[0]

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
          onClick={() => void queryClient.invalidateQueries({ queryKey: ['worldbook'] })}
        >
          {tCommon('actions.refresh')}
        </ActionButton>
      </Box>

      <InstanceScopeBanner description={t('scopeBanner')} />

      <Box sx={{ flexShrink: 0, ...(UNIFIED_TABLE_STYLES.pageTabContainer as object) }}>
        <PageTabs
          value={tab}
          onChange={(_event, value: WorldbookPageTab) => navigate(worldbookPath(value))}
          variant={isMobile ? 'scrollable' : 'standard'}
          scrollButtons="auto"
        >
          {WORLDBOOK_PAGE_TAB_DEFS.map(definition => (
            <Tab key={definition.key} value={definition.key} label={t(definition.labelKey)} />
          ))}
        </PageTabs>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {activeDefinition.render({ scope, isScopeReady })}
      </Box>
    </Box>
  )
}
