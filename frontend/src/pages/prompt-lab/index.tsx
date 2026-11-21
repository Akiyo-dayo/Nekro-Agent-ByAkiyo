import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Box, Drawer, Menu, MenuItem, Tab, Typography, useMediaQuery, useTheme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import MoreHorizRoundedIcon from '@mui/icons-material/MoreHorizRounded'
import IconActionButton from '../../components/common/IconActionButton'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import { PageTabs } from '../../components/common/NekroTabs'
import DomainSettingsButton from '../../components/settings/DomainSettingsButton'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import {
  isPromptLabPageTab,
  promptLabPath,
  type PromptLabPageTab,
} from '../../router/routes'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import AuditTab from './AuditTab'
import PresetsTab from './PresetsTab'
import PreviewTab from './PreviewTab'
import TrialTab from './TrialTab'

/**
 * 提示词预览页
 *
 * 三个标签共用一份实例作用域：预览、试发与预设都只针对顶栏选中的实例。
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
  const { t: tCommon } = useTranslation('common')
  const { tab: tabParam } = useParams<{ tab?: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const { currentInstanceKey, currentInstance, isScopeReady } = useInstanceScope()
  const [auditOpen, setAuditOpen] = useState(false)
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)

  // `/prompt-lab/audit` 与分享地址里的 `?log=1` 都落到「预览 + 变更记录抽屉」
  useEffect(() => {
    if (searchParams.get('log') === '1') setAuditOpen(true)
  }, [searchParams])

  const closeAudit = () => {
    setAuditOpen(false)
    if (searchParams.get('log') === null) return
    const next = new URLSearchParams(searchParams)
    next.delete('log')
    setSearchParams(next, { replace: true })
  }

  const openAudit = () => {
    setMenuAnchor(null)
    setAuditOpen(true)
    const next = new URLSearchParams(searchParams)
    next.set('log', '1')
    setSearchParams(next, { replace: true })
  }

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
      key: 'presets',
      labelKey: 'lab.tabs.presets',
      render: () => <PresetsTab key={scopeKey} scope={scope} isScopeReady={isScopeReady} />,
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
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.5, flexWrap: 'wrap' }}>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography variant="h6" fontWeight={700}>
            {t('lab.title')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('lab.subtitle')}
          </Typography>
        </Box>
        <IconActionButton
          aria-label={tCommon('actions.more')}
          title={tCommon('actions.more')}
          onClick={event => setMenuAnchor(event.currentTarget)}
        >
          <MoreHorizRoundedIcon />
        </IconActionButton>
        <Menu
          anchorEl={menuAnchor}
          open={menuAnchor !== null}
          onClose={() => setMenuAnchor(null)}
          anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
          transformOrigin={{ vertical: 'top', horizontal: 'right' }}
        >
          <MenuItem onClick={openAudit}>{t('lab.menu.changeLog')}</MenuItem>
        </Menu>
        <DomainSettingsButton domain="promptLab" />
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

      <Drawer
        anchor="right"
        open={auditOpen}
        onClose={closeAudit}
        PaperProps={{
          sx: {
            width: { xs: '100%', sm: 720 },
            maxWidth: '100%',
            bgcolor: 'background.paper',
          },
        }}
      >
        <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <Box
            sx={{
              position: 'sticky',
              top: 0,
              zIndex: 2,
              px: 2,
              py: 1.5,
              bgcolor: 'background.paper',
              borderBottom: '1px solid',
              borderColor: 'divider',
              display: 'flex',
              alignItems: 'center',
              gap: 1,
            }}
          >
            <Typography variant="h6" fontWeight={700}>
              {t('lab.menu.changeLog')}
            </Typography>
            <Box sx={{ flex: 1 }} />
            <IconActionButton onClick={closeAudit} aria-label={tCommon('actions.close')}>
              <CloseRoundedIcon />
            </IconActionButton>
          </Box>
          <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', px: 2, py: 2 }}>
            <AuditTab scope={scope} />
          </Box>
        </Box>
      </Drawer>
    </Box>
  )
}
