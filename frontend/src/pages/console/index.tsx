import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { Alert, Box, Chip, CircularProgress, Typography, useTheme } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import ForumOutlinedIcon from '@mui/icons-material/ForumOutlined'
import ChatBubbleOutlineRoundedIcon from '@mui/icons-material/ChatBubbleOutlineRounded'
import HubRoundedIcon from '@mui/icons-material/HubRounded'
import CheckCircleRoundedIcon from '@mui/icons-material/CheckCircleRounded'
import CloudOffRoundedIcon from '@mui/icons-material/CloudOffRounded'
import LinkOffRoundedIcon from '@mui/icons-material/LinkOffRounded'
import HelpOutlineRoundedIcon from '@mui/icons-material/HelpOutlineRounded'
import ActionButton from '../../components/common/ActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import StatCard from '../../components/common/StatCard'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import { useIdentity } from '../../hooks/useIdentity'
import { CHIP_VARIANTS } from '../../theme/variants'
import { consoleApi, consolePaths } from '../../services/api/console'
import InstanceCard from './components/InstanceCard'
import UnclaimedProtocolPanel from './components/UnclaimedProtocolPanel'

/**
 * 总后台首页
 *
 * ⚠️ 这一页此前**没有任何失败分支**：两条查询失败时 `data` 为 undefined，
 * 于是每张卡片各自 `?? 0`、实例列表 `?? []`，整页渲染成
 * 「实例总数 0 / 在线 0 / 还没有实例」。只读租户成员（`tenant_user`）打开它必然如此
 * —— `GET /console/overview` 与 `GET /console/instances` 都是 `@require_role(Role.Admin)`。
 * 客户拉第二个人进来看数据，看到的是「你什么都没有」而不是「你无权查看」。
 *
 * 现在失败就说失败：403 说「你无权查看」，其余说「加载失败」并给重试
 * （`components/common/QueryErrorState.tsx`）。**空与不让看必须能分辨。**
 */
export default function ConsoleHomePage() {
  const navigate = useNavigate()
  const theme = useTheme()
  const { t } = useTranslation('console')
  // 新建实例是 `POST /console/instances`，`Role.Super`。此前这个按钮无条件渲染，
  // 租户管理员点进向导、填完三步、最后一步收 403 —— 同页的其它平台方能力都有门，只有它漏了
  const { isPlatformOwner } = useIdentity()

  const overviewQuery = useQuery({
    queryKey: ['console-overview'],
    queryFn: () => consoleApi.getOverview(),
    staleTime: 15_000,
    // 403 不会因为再试一次变成 200，重试只是让「你无权查看」晚三秒出现
    retry: false,
  })

  const instancesQuery = useQuery({
    queryKey: ['console-instances'],
    queryFn: () => consoleApi.listInstances(),
    staleTime: 15_000,
    retry: false,
  })

  const instances = useMemo(() => instancesQuery.data?.items ?? [], [instancesQuery.data])

  /**
   * 单实例优雅降级：实例总数 ≤ 1 时隐藏实例计数类卡片与列表入口，
   * 让单实例用户不用先理解「多实例」这一层。
   */
  const isMultiInstance = instances.length > 1

  const unclaimedCount = overviewQuery.data?.instance_unclaimed ?? 0
  const unboundCount = overviewQuery.data?.instance_unbound ?? 0
  const isLoading = overviewQuery.isLoading || instancesQuery.isLoading
  // 任何一条失败都不能再画那排 `?? 0` 的卡片：`0` 是一个有效答案，用户会当真。
  // 取第一个错误对象（两条一起 403 时说法一样，取哪个都行）
  const loadError = overviewQuery.error ?? instancesQuery.error
  const isLoadFailed = overviewQuery.isError || instancesQuery.isError
  const retryAll = () => {
    void overviewQuery.refetch()
    void instancesQuery.refetch()
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
        overflow: 'auto',
        boxSizing: 'border-box',
        // 定高滚动列：子项按内容定高，不被压扁（见 SCROLL_COLUMN_STYLES）
        '& > *': { flexShrink: 0 },
      }}
    >
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 1.5,
          flexWrap: 'wrap',
        }}
      >
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography variant="h6" fontWeight={700}>
            {t('title')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('subtitle')}
          </Typography>
        </Box>
        {/* 「Docker 可用 / 不可用」是 overview 里的一个布尔位。取不到时它会稳定地显示成
            「Docker 不可用」——那是一句错话，不是一个空值。失败时整个标不画 */}
        {!isLoadFailed && (
          <SimpleTooltip
            title={
              overviewQuery.data?.docker_available
                ? t('overview.dockerAvailableHint')
                : t('overview.dockerUnavailableHint')
            }
          >
            <Chip
              size="small"
              label={
                overviewQuery.data?.docker_available
                  ? t('overview.dockerAvailable')
                  : t('overview.dockerUnavailable')
              }
              sx={CHIP_VARIANTS.getCustomColorChip(
                overviewQuery.data?.docker_available
                  ? theme.palette.success.main
                  : theme.palette.text.secondary,
                true
              )}
            />
          </SimpleTooltip>
        )}
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<RefreshRoundedIcon />}
          onClick={retryAll}
        >
          {t('actions.refresh')}
        </ActionButton>
        {isMultiInstance && (
          <ActionButton
            tone="secondary"
            size="small"
            onClick={() => navigate(consolePaths.instanceList())}
          >
            {t('actions.viewAll')}
          </ActionButton>
        )}
        {isPlatformOwner && (
          <ActionButton
            tone="primary"
            size="small"
            startIcon={<AddRoundedIcon />}
            onClick={() => navigate(consolePaths.instanceCreate())}
          >
            {t('actions.create')}
          </ActionButton>
        )}
      </Box>

      {/* 失败时**整块业务区**换成一句实话，而不是继续画那排 `?? 0` 的卡片。
          放在这里（标题栏之下、统计之上）是为了把「刷新」留在页面上：
          非 403 的失败重试一次就好了 */}
      {isLoadFailed ? (
        <QueryErrorState error={loadError} onRetry={retryAll} />
      ) : (
        <>
        {/* 待认领协议端的引导与首页共用同一个面板：认领有两条出路（认领为新实例 / 并入已有实例），
            分两处各写一套必然分叉，而这条引导恰恰是管理员唯一能看懂现场的地方 */}
        <UnclaimedProtocolPanel
          onClaimed={result => navigate(consolePaths.instanceDetail(result.instance.instance_key))}
        />

        {unboundCount > 0 && (
          <Alert severity="warning" variant="outlined">
            {t('overview.unboundBanner', { count: unboundCount })}
          </Alert>
        )}

        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: { xs: 1.25, sm: 2 } }}>
          {isMultiInstance && (
            <>
              <StatCard
                label={t('overview.instanceTotal')}
                value={overviewQuery.data?.instance_total ?? 0}
                icon={<HubRoundedIcon />}
                color={theme.palette.primary.main}
                loading={overviewQuery.isLoading}
              />
              <StatCard
                label={t('overview.instanceOnline')}
                value={overviewQuery.data?.instance_online ?? 0}
                icon={<CheckCircleRoundedIcon />}
                color={theme.palette.success.main}
                loading={overviewQuery.isLoading}
              />
              <StatCard
                label={t('overview.instanceOffline')}
                value={overviewQuery.data?.instance_offline ?? 0}
                icon={<CloudOffRoundedIcon />}
                color={theme.palette.error.main}
                loading={overviewQuery.isLoading}
              />
            </>
          )}
          {unboundCount > 0 && (
            <StatCard
              label={t('overview.instanceUnbound')}
              value={unboundCount}
              icon={<LinkOffRoundedIcon />}
              color={theme.palette.warning.main}
              loading={overviewQuery.isLoading}
            />
          )}
          {unclaimedCount > 0 && (
            <StatCard
              label={t('overview.instanceUnclaimed')}
              value={unclaimedCount}
              icon={<HelpOutlineRoundedIcon />}
              color={theme.palette.warning.main}
              loading={overviewQuery.isLoading}
            />
          )}
          <StatCard
            label={t('overview.channelTotal')}
            value={overviewQuery.data?.channel_total ?? 0}
            icon={<ForumOutlinedIcon />}
            color={theme.palette.info.main}
            loading={overviewQuery.isLoading}
          />
          <StatCard
            label={t('overview.messageToday')}
            value={overviewQuery.data?.message_count_today ?? 0}
            icon={<ChatBubbleOutlineRoundedIcon />}
            color={theme.palette.secondary.main}
            loading={overviewQuery.isLoading}
          />
        </Box>

        <Typography variant="subtitle2" fontWeight={700} sx={{ mt: 0.5 }}>
          {isMultiInstance ? t('overview.instancesTitle') : t('overview.instanceTitleSingle')}
        </Typography>

        {isLoading ? (
          <Box
            sx={{ minHeight: 200, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          >
            <CircularProgress />
          </Box>
        ) : instances.length === 0 ? (
          <Box
            sx={{
              minHeight: 200,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 1,
            }}
          >
            <Typography variant="subtitle1">{t('overview.empty')}</Typography>
            <Typography variant="body2" color="text.secondary">
              {t('overview.emptyHint')}
            </Typography>
            {isPlatformOwner && (
              <ActionButton
                tone="primary"
                size="small"
                startIcon={<AddRoundedIcon />}
                onClick={() => navigate(consolePaths.instanceCreate())}
              >
                {t('actions.create')}
              </ActionButton>
            )}
          </Box>
        ) : (
          <Box
            sx={{
              display: 'grid',
              gridTemplateColumns: isMultiInstance
                ? {
                    xs: '1fr',
                    sm: 'repeat(2, minmax(0, 1fr))',
                    lg: 'repeat(3, minmax(0, 1fr))',
                    xl: 'repeat(4, minmax(0, 1fr))',
                  }
                : { xs: '1fr', sm: 'minmax(0, 420px)' },
              gap: { xs: 1.25, sm: 2 },
              alignContent: 'start',
            }}
          >
            {/* 卡片上不再各带一个"认领"按钮：认领有两条出路，塞进卡片只放得下最不该做的那条
                （就地认领、沿用 `ob{QQ号}` 这个机器生成的标识）。引导统一在上面那个面板里 */}
            {instances.map(instance => (
              <InstanceCard
                key={instance.instance_key}
                instance={instance}
                onOpen={instanceKey => navigate(consolePaths.instanceDetail(instanceKey))}
              />
            ))}
          </Box>
        )}
        </>
      )}
    </Box>
  )
}
