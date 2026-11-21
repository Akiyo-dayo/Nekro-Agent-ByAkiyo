import { Box, Tab, Typography, useMediaQuery, useTheme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { useLocation, useNavigate } from 'react-router-dom'
import { PageTabs } from '../../components/common/NekroTabs'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import ChannelQuotaPage from './ChannelQuotaPage'
import InstanceQuotaPage from './InstanceQuotaPage'
import UsagePage from './UsagePage'

/**
 * 额度与用量（总后台）
 *
 * 原来的「配额」「频道额度」「用量与费用」三页合成一页。三个标签由**路径**决定，
 * 不用 query：`/console/channel-quota` 是频道额度（默认标签）、`/console/quota` 是
 * 实例自限、`/console/usage` 是用量与费用。切标签只做一次 `navigate`，旧链接、
 * 别处的跳转与地址栏里手敲的路径全部照常落在这里。
 *
 * 页头副标题是「平台额度 / 自限」两层语义的唯一说明处：两个子页面不再各自解释一遍。
 */
type QuotaHubTab = 'channel-quota' | 'quota' | 'usage'

interface QuotaHubTabDefinition {
  key: QuotaHubTab
  path: string
  labelKey: string
}

const QUOTA_HUB_TABS: QuotaHubTabDefinition[] = [
  { key: 'channel-quota', path: '/console/channel-quota', labelKey: 'tabs.channelQuota' },
  { key: 'quota', path: '/console/quota', labelKey: 'tabs.instanceSelfLimit' },
  { key: 'usage', path: '/console/usage', labelKey: 'tabs.usage' },
]

const DEFAULT_QUOTA_HUB_TAB: QuotaHubTab = 'channel-quota'

const resolveQuotaHubTab = (pathname: string): QuotaHubTab => {
  const matched = QUOTA_HUB_TABS.find(
    definition => pathname === definition.path || pathname.startsWith(`${definition.path}/`)
  )
  return matched?.key ?? DEFAULT_QUOTA_HUB_TAB
}

export default function QuotaHubPage() {
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const { t } = useTranslation('console-quota-hub')

  const tab = resolveQuotaHubTab(pathname)

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
          {t('title')}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {t('subtitle')}
        </Typography>
      </Box>

      <Box sx={{ flexShrink: 0, ...(UNIFIED_TABLE_STYLES.pageTabContainer as object) }}>
        <PageTabs
          value={tab}
          onChange={(_event, value: QuotaHubTab) => {
            const next = QUOTA_HUB_TABS.find(definition => definition.key === value)
            if (next) navigate(next.path)
          }}
          variant={isMobile ? 'scrollable' : 'standard'}
          scrollButtons="auto"
        >
          {QUOTA_HUB_TABS.map(definition => (
            <Tab key={definition.key} value={definition.key} label={t(definition.labelKey)} />
          ))}
        </PageTabs>
      </Box>

      <Box
        sx={{
          flex: 1,
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
          overflowY: 'auto',
        }}
      >
        {tab === 'quota' ? (
          <InstanceQuotaPage embedded />
        ) : tab === 'usage' ? (
          <UsagePage embedded />
        ) : (
          <ChannelQuotaPage embedded />
        )}
      </Box>
    </Box>
  )
}
