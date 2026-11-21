import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Drawer,
  Menu,
  MenuItem,
  Tab,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import MoreHorizRoundedIcon from '@mui/icons-material/MoreHorizRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import IconActionButton from '../../components/common/IconActionButton'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import { PageTabs } from '../../components/common/NekroTabs'
import DomainSettingsButton from '../../components/settings/DomainSettingsButton'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import {
  DEFAULT_STATE_PAGE_TAB,
  isStatePageTab,
  STATE_PAGE_TABS,
  statePath,
  type StatePageTab,
} from '../../router/routes'
import { stateTableApi, type StateScope } from '../../services/api/stateTable'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import AuditTab from './AuditTab'
import ChannelsTab from './ChannelsTab'
import PeopleTab from './PeopleTab'
import SetupGuide from './SetupGuide'
import TemplatesTab from './TemplatesTab'

/**
 * 角色状态页（`/state`）
 *
 * 一个实例下每个频道的状态树、每个人的人设档案、状态模板与改动记录。这一页回答的都是
 * 「某个实例」的问题，所以和邮箱页一样必须先有实例：租户本来就钉在自己的实例上，
 * 平台方在全局视图下只给一句「先在顶栏选一个实例」。
 *
 * 标签进 path。换实例时各标签按实例重新挂载，上一个实例的筛选、分页与对话框不会带过来。
 */

const TAB_LABEL_KEYS: Record<StatePageTab, string> = {
  channels: 'tabs.channels',
  people: 'tabs.people',
  templates: 'tabs.templates',
}

function renderTab(tab: StatePageTab, scope: StateScope) {
  switch (tab) {
    case 'channels':
      return <ChannelsTab scope={scope} />
    case 'people':
      return <PeopleTab scope={scope} />
    case 'templates':
      return <TemplatesTab scope={scope} />
  }
}

export default function StatePage() {
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { t } = useTranslation('state')
  const { t: tCommon } = useTranslation('common')
  const { tab: tabParam } = useParams<{ tab?: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const { currentInstance, instances, isScopeSwitchable, isScopeReady } = useInstanceScope()
  const [auditOpen, setAuditOpen] = useState(false)
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)

  // `/state/audit` 与分享地址里的 `?log=1` 都落到「频道状态 + 变更记录抽屉」
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

  // 只有一个实例时顶栏不出切换器、作用域停在全局视图，而这组接口一定要带实例：
  // 这时就用那一个实例，否则单实例部署永远停在「先选一个实例」、又没有地方选
  const scopeInstance =
    currentInstance ?? (!isScopeSwitchable && instances.length === 1 ? instances[0] : null)
  const scope = useMemo<StateScope | null>(
    () =>
      scopeInstance?.instance_key && scopeInstance.adapter_key
        ? { adapter_key: scopeInstance.adapter_key, instance_key: scopeInstance.instance_key }
        : null,
    [scopeInstance?.instance_key, scopeInstance?.adapter_key]
  )
  const scopeKey = scope ? `${scope.adapter_key}|${scope.instance_key}` : ''

  // 这个接口的 404 就是「插件没在实例层启用」。它成功之前不挂任何标签页内容：
  // 频道、档案、模板的接口在未启用时同样是 404，页面上不该出现一串同样的空错误。
  const setupQuery = useQuery({
    queryKey: ['state-table', 'setup-status', scopeKey],
    queryFn: () => stateTableApi.getSetupStatus(scope as StateScope),
    enabled: scope !== null && isScopeReady,
    retry: false,
  })
  // 缓存里上一个实例的「已启用」不能当作这一次的事实：等本次挂载后的那一次请求落地，
  // 再决定要不要挂标签页，否则插件刚被关掉时会先打出一串 404。
  const pluginReady = setupQuery.isSuccess && setupQuery.isFetchedAfterMount

  if (!isStatePageTab(tabParam)) {
    return <Navigate to={statePath(DEFAULT_STATE_PAGE_TAB)} replace />
  }
  const tab: StatePageTab = tabParam

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 3 },
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        overflow: 'auto',
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
        <IconActionButton
          aria-label={tCommon('actions.refresh')}
          title={tCommon('actions.refresh')}
          disabled={!scope}
          onClick={() => void queryClient.invalidateQueries({ queryKey: ['state-table'] })}
        >
          <RefreshRoundedIcon />
        </IconActionButton>
        <IconActionButton
          aria-label={tCommon('actions.more')}
          title={tCommon('actions.more')}
          disabled={!scope || !pluginReady}
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
          <MenuItem disabled={!scope || !pluginReady} onClick={openAudit}>
            {t('menu.changeLog')}
          </MenuItem>
        </Menu>
        <DomainSettingsButton domain="state" />
      </Box>

      <InstanceScopeBanner description={t('scopeBanner')} />

      {!isScopeReady ? null : !scope ? (
        <Alert severity="info">{t('selectInstance')}</Alert>
      ) : (
        <>
          <SetupGuide
            status={setupQuery.data ?? null}
            loading={setupQuery.isPending || !setupQuery.isFetchedAfterMount}
            error={setupQuery.error}
            onRetry={() => void setupQuery.refetch()}
          />

          {pluginReady && (
            <>
              <Box sx={{ flexShrink: 0, ...(UNIFIED_TABLE_STYLES.pageTabContainer as object) }}>
                <PageTabs
                  value={tab}
                  onChange={(_event, value: StatePageTab) => navigate(statePath(value))}
                  variant={isMobile ? 'scrollable' : 'standard'}
                  scrollButtons="auto"
                >
                  {STATE_PAGE_TABS.map(key => (
                    <Tab key={key} value={key} label={t(TAB_LABEL_KEYS[key])} />
                  ))}
                </PageTabs>
              </Box>

              <Box
                key={`${tab}|${scopeKey}`}
                sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
              >
                {renderTab(tab, scope)}
              </Box>
            </>
          )}
        </>
      )}

      <Drawer
        anchor="right"
        open={auditOpen && scope !== null && pluginReady}
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
              {t('menu.changeLog')}
            </Typography>
            <Box sx={{ flex: 1 }} />
            <IconActionButton onClick={closeAudit} aria-label={tCommon('actions.close')}>
              <CloseRoundedIcon />
            </IconActionButton>
          </Box>
          <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', px: 2, py: 2 }}>
            {scope && pluginReady && <AuditTab scope={scope} />}
          </Box>
        </Box>
      </Drawer>
    </Box>
  )
}
