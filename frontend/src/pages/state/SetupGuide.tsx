import { useState, type ReactNode } from 'react'
import { alpha, Box, CircularProgress, Paper, Typography, useTheme } from '@mui/material'
import CheckCircleRoundedIcon from '@mui/icons-material/CheckCircleRounded'
import CheckRoundedIcon from '@mui/icons-material/CheckRounded'
import { Link as RouterLink } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import { isNotFoundError } from '../../services/api/axios'
import type { StateSetupStatus } from '../../services/api/stateTable'
import { pluginsManagementPath, statePath } from '../../router/routes'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'

/**
 * 「角色状态」页顶部的启用引导
 *
 * 条件链是：插件在实例层启用 → 人设已有状态模板 →（可选）单独指一个更新用的模型组 →
 * 群里聊过一轮或发过 `/状态`。这四步里任何一步没做，下面的表都会是空的；所以引导要把
 * 「差哪一步」直接说出来，而不是留一张空表让人猜。
 *
 * `setup-status` 的 404 就是第 1 步没做：插件没在实例层启用时，它的全部路由都不存在。
 * 其它失败（网络、500）交给统一的错误态，不能当成「没启用」。
 */

const COLLAPSED_STORAGE_KEY = 'state.setupGuide.collapsed'

/** 插件配置页：与设置抽屉里「角色状态插件」的入口同一条链接 */
const PLUGIN_SETTINGS_PATH = `${pluginsManagementPath('Akiyo_dayo.state_table')}?tab=config`

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_STORAGE_KEY) !== '0'
  } catch {
    return true
  }
}

function writeCollapsed(collapsed: boolean) {
  try {
    localStorage.setItem(COLLAPSED_STORAGE_KEY, collapsed ? '1' : '0')
  } catch {
    // 隐私模式或存储被禁用时只是不记住，不影响页面
  }
}

interface StepRowProps {
  index: number
  done: boolean
  title: string
  description: string
  action?: ReactNode
}

function StepRow({ index, done, title, description, action }: StepRowProps) {
  const theme = useTheme()

  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'flex-start',
        flexWrap: 'wrap',
        gap: 1.5,
        py: 1.25,
        '& + &': { borderTop: '1px solid', borderColor: 'divider' },
      }}
    >
      <Box
        aria-hidden
        sx={{
          flexShrink: 0,
          width: 24,
          height: 24,
          mt: 0.25,
          borderRadius: '50%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          border: '1px solid',
          borderColor: done ? alpha(theme.palette.success.main, 0.35) : 'text.disabled',
          bgcolor: done ? alpha(theme.palette.success.main, 0.12) : 'transparent',
          color: done ? 'success.main' : 'text.disabled',
        }}
      >
        {done ? (
          <CheckRoundedIcon sx={{ fontSize: 16 }} />
        ) : (
          <Typography variant="caption" fontWeight={600} sx={{ lineHeight: 1 }}>
            {index}
          </Typography>
        )}
      </Box>

      <Box sx={{ flex: 1, minWidth: 180 }}>
        <Typography variant="body2" fontWeight={600}>
          {title}
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.25 }}>
          {description}
        </Typography>
      </Box>

      {action && <Box sx={{ flexShrink: 0, ml: 'auto' }}>{action}</Box>}
    </Box>
  )
}

export interface SetupGuideProps {
  /** `setup-status` 的返回值；404 时为 null */
  status: StateSetupStatus | null
  loading: boolean
  error: unknown
  onRetry: () => void
}

export default function SetupGuide({ status, loading, error, onRetry }: SetupGuideProps) {
  const { t } = useTranslation('state')
  const [collapsed, setCollapsed] = useState(readCollapsed)

  const notFound = isNotFoundError(error)
  const failed = Boolean(error) && !notFound
  const pluginReady = status !== null

  const templateCount = status?.template_count ?? 0
  const channelCount = status?.channel_count ?? 0
  const personCount = status?.person_count ?? 0
  const effectiveGroup = status?.update_model_group_effective?.trim() ?? ''
  const configuredGroup = status?.update_model_group?.trim() ?? ''
  const usesFallback = status?.update_uses_fallback ?? true

  const templateDone = pluginReady && templateCount > 0
  const modelDone = pluginReady && effectiveGroup.length > 0
  const firstStateDone = pluginReady && channelCount > 0
  const allDone = pluginReady && templateDone && modelDone && firstStateDone

  const toggleCollapsed = () => {
    const next = !collapsed
    setCollapsed(next)
    writeCollapsed(next)
  }

  let content: ReactNode
  if (loading) {
    content = (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, py: 0.5 }}>
        <CircularProgress size={18} />
        <Typography variant="body2" color="text.secondary">
          {t('setup.loading')}
        </Typography>
      </Box>
    )
  } else if (failed) {
    content = <QueryErrorState error={error} onRetry={onRetry} compact />
  } else {
    const modelDescription = !modelDone
      ? t('setup.steps.modelGroup.missing')
      : usesFallback
        ? t('setup.steps.modelGroup.fallback', { group: effectiveGroup })
        : t('setup.steps.modelGroup.configured', {
            group: configuredGroup || effectiveGroup,
          })

    const steps = (
      <>
        <StepRow
          index={1}
          done={pluginReady}
          title={t('setup.steps.plugin.title')}
          description={
            pluginReady
              ? t('setup.steps.plugin.done')
              : t('setup.steps.plugin.todo')
          }
          action={
            <ActionButton
              tone="secondary"
              size="small"
              component={RouterLink}
              to={PLUGIN_SETTINGS_PATH}
            >
              {t('setup.steps.plugin.action')}
            </ActionButton>
          }
        />
        <StepRow
          index={2}
          done={templateDone}
          title={t('setup.steps.template.title')}
          description={
            templateDone
              ? t('setup.steps.template.done', { count: templateCount })
              : t('setup.steps.template.todo')
          }
          action={
            pluginReady ? (
              <ActionButton
                tone="secondary"
                size="small"
                component={RouterLink}
                to={statePath('templates')}
              >
                {t('setup.steps.template.action')}
              </ActionButton>
            ) : undefined
          }
        />
        <StepRow
          index={3}
          done={modelDone}
          title={t('setup.steps.modelGroup.title')}
          description={modelDescription}
          action={
            pluginReady ? (
              <ActionButton
                tone="secondary"
                size="small"
                component={RouterLink}
                to={PLUGIN_SETTINGS_PATH}
              >
                {t('setup.steps.modelGroup.action')}
              </ActionButton>
            ) : undefined
          }
        />
        <StepRow
          index={4}
          done={firstStateDone}
          title={t('setup.steps.firstState.title')}
          description={
            firstStateDone
              ? t('setup.steps.firstState.done', { count: channelCount })
              : t('setup.steps.firstState.todo')
          }
        />
      </>
    )

    if (allDone) {
      content = (
        <>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <CheckCircleRoundedIcon sx={{ fontSize: 18, color: 'success.main' }} />
            <Typography variant="caption" color="text.secondary" sx={{ minWidth: 0 }}>
              {t('setup.summary', { channels: channelCount, people: personCount })}
            </Typography>
            <Box sx={{ flex: 1 }} />
            <ActionButton tone="ghost" size="small" onClick={toggleCollapsed}>
              {collapsed ? t('setup.expand') : t('setup.collapse')}
            </ActionButton>
          </Box>
          {!collapsed && <Box sx={{ mt: 1 }}>{steps}</Box>}
        </>
      )
    } else {
      content = steps
    }
  }

  return (
    <Paper
      elevation={0}
      sx={{
        ...(UNIFIED_TABLE_STYLES.paper as object),
        flexShrink: 0,
        p: { xs: 1.5, sm: 2 },
      }}
    >
      {content}
    </Paper>
  )
}
