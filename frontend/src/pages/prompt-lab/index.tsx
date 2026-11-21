import { useMemo, type ReactNode } from 'react'
import { Box, Tab, Typography, useMediaQuery, useTheme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import { PageTabs } from '../../components/common/NekroTabs'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import {
  isPromptLabPageTab,
  promptLabPath,
  type PromptLabPageTab,
} from '../../router/routes'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import AuditTab from './AuditTab'
import PreviewTab from './PreviewTab'
import TrialTab from './TrialTab'

/**
 * 提示词预览页
 *
 * 三个标签共用一份实例作用域：预览、试发、变更记录都只针对顶栏选中的实例。
 * 作用域参数同时进查询键与标签组件的 `key`，换实例时整个组件重新挂载 —— 上一个
 * 实例的频道、模型组与结果都不能留在新实例的页面上。
 *
 * 标签进 path，与 `worldbookPath` 同一形态：刷新与分享都落回同一个标签。
 */

export default function PromptLabPage() {
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))
  const navigate = useNavigate()
  const { t } = useTranslation('tavern')
  const { tab: tabParam } = useParams<{ tab?: string }>()
  const { currentInstanceKey, currentInstance, isScopeReady } = useInstanceScope()

  const scope = useMemo(
    () => ({
      instance_key: currentInstanceKey ?? undefined,
      adapter_key: currentInstance?.adapter_key,
    }),
    [currentInstanceKey, currentInstance?.adapter_key]
  )
  const scopeKey = `${scope.adapter_key ?? ''}|${scope.instance_key ?? ''}`

  if (!isPromptLabPageTab(tabParam)) {
    return <Navigate to={promptLabPath()} replace />
  }

  const tab: PromptLabPageTab = tabParam
  const tabDefs: { key: PromptLabPageTab; labelKey: string; render: () => ReactNode }[] = [
    {
      key: 'preview',
      labelKey: 'lab.tabs.preview',
      render: () => (
        <PreviewTab key={scopeKey} scope={scope} isScopeReady={isScopeReady} />
      ),
    },
    {
      key: 'trial',
      labelKey: 'lab.tabs.trial',
      render: () => <TrialTab key={scopeKey} scope={scope} isScopeReady={isScopeReady} />,
    },
    {
      key: 'audit',
      labelKey: 'lab.tabs.audit',
      render: () => <AuditTab key={scopeKey} scope={scope} />,
    },
  ]
  const activeDefinition = tabDefs.find(definition => definition.key === tab) ?? tabDefs[0]

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
      <Box sx={{ minWidth: 0 }}>
        <Typography variant="h6" fontWeight={700}>
          {t('lab.title')}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {t('lab.subtitle')}
        </Typography>
      </Box>

      <InstanceScopeBanner description={t('lab.scopeBanner')} />

      <Box sx={{ flexShrink: 0, ...(UNIFIED_TABLE_STYLES.pageTabContainer as object) }}>
        <PageTabs
          value={tab}
          onChange={(_event, value: PromptLabPageTab) => navigate(promptLabPath(value))}
          variant={isMobile ? 'scrollable' : 'standard'}
          scrollButtons="auto"
        >
          {tabDefs.map(definition => (
            <Tab key={definition.key} value={definition.key} label={t(definition.labelKey)} />
          ))}
        </PageTabs>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {activeDefinition.render()}
      </Box>
    </Box>
  )
}
