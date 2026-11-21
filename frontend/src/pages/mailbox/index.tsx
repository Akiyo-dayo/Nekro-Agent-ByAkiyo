import { useMemo } from 'react'
import { Alert, Box, Tab, Typography, useMediaQuery, useTheme } from '@mui/material'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import ActionButton from '../../components/common/ActionButton'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import { PageTabs } from '../../components/common/NekroTabs'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { isMailboxPageTab, mailboxPath, type MailboxPageTab } from '../../router/routes'
import type { MailboxScope } from '../../services/api/mailbox'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import AccountsTab from './AccountsTab'
import FestivalTab from './FestivalTab'
import MailsTab from './MailsTab'
import SendLogsTab from './SendLogsTab'
import UserPrefsTab from './UserPrefsTab'

/**
 * 邮箱页（`/mailbox`）
 *
 * 一个实例的邮箱：本实例自有的邮箱、平台方共享给本实例的发信邮箱、收到的信、发信记录、
 * 用户记下的收件地址与节日邮件的进度。这一页回答的都是「某个实例」的问题，所以必须先有实例：
 * 租户本来就钉在自己的实例上；平台方在全局视图下只给一句「先在顶栏选一个实例」。
 *
 * 标签进 path（与世界书页同一形态）。换实例时各标签按实例重新挂载，上一个实例的选择、
 * 分页与对话框不会带到新实例上。
 */

const TAB_LABEL_KEYS: Record<MailboxPageTab, string> = {
  accounts: 'tabs.accounts',
  mails: 'tabs.mails',
  'send-logs': 'tabs.sendLogs',
  'user-prefs': 'tabs.userPrefs',
  festival: 'tabs.festival',
}

const TAB_ORDER: MailboxPageTab[] = ['accounts', 'mails', 'send-logs', 'user-prefs', 'festival']

function renderTab(tab: MailboxPageTab, scope: MailboxScope) {
  switch (tab) {
    case 'accounts':
      return <AccountsTab scope={scope} />
    case 'mails':
      return <MailsTab scope={scope} />
    case 'send-logs':
      return <SendLogsTab scope={scope} />
    case 'user-prefs':
      return <UserPrefsTab scope={scope} />
    case 'festival':
      return <FestivalTab scope={scope} />
  }
}

export default function MailboxPage() {
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { t } = useTranslation('mailbox')
  const { t: tCommon } = useTranslation('common')
  const { tab: tabParam } = useParams<{ tab?: string }>()
  const { currentInstanceKey, currentInstance, isScopeReady } = useInstanceScope()

  const scope = useMemo<MailboxScope | null>(
    () =>
      currentInstanceKey && currentInstance?.adapter_key
        ? { adapter_key: currentInstance.adapter_key, instance_key: currentInstanceKey }
        : null,
    [currentInstanceKey, currentInstance?.adapter_key]
  )

  if (!isMailboxPageTab(tabParam)) {
    return <Navigate to={mailboxPath()} replace />
  }
  const tab: MailboxPageTab = tabParam
  const scopeKey = scope ? `${scope.adapter_key}|${scope.instance_key}` : ''

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
        <ActionButton
          tone="secondary"
          startIcon={<RefreshRoundedIcon />}
          disabled={!scope}
          onClick={() => void queryClient.invalidateQueries({ queryKey: ['mailbox'] })}
        >
          {tCommon('actions.refresh')}
        </ActionButton>
      </Box>

      <InstanceScopeBanner description={t('scopeBanner')} />

      {!isScopeReady ? null : !scope ? (
        <Alert severity="info">{t('selectInstance')}</Alert>
      ) : (
        <>
          <Box sx={{ flexShrink: 0, ...(UNIFIED_TABLE_STYLES.pageTabContainer as object) }}>
            <PageTabs
              value={tab}
              onChange={(_event, value: MailboxPageTab) => navigate(mailboxPath(value))}
              variant={isMobile ? 'scrollable' : 'standard'}
              scrollButtons="auto"
            >
              {TAB_ORDER.map(key => (
                <Tab key={key} value={key} label={t(TAB_LABEL_KEYS[key])} />
              ))}
            </PageTabs>
          </Box>

          <Box key={`${tab}|${scopeKey}`} sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
            {renderTab(tab, scope)}
          </Box>
        </>
      )}
    </Box>
  )
}
