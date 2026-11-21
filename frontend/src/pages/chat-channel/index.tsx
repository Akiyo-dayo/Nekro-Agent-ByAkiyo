import { useDeferredValue, useMemo, useState, useEffect } from 'react'
import {
  Box,
  Typography,
  Stack,
  Divider,
  useTheme,
  useMediaQuery,
  Drawer,
  Fab,
  Card,
} from '@mui/material'
import {
  List as ListIcon,
  Info as InfoIcon,
} from '@mui/icons-material'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import ChatChannelList from './components/ChatChannelList'
import ChatChannelDetail from './components/ChatChannelDetail'
import BotMembershipCheckDialog from './components/BotMembershipCheckDialog'
import BotMembershipDisableDialog from './components/BotMembershipDisableDialog'
import { CARD_VARIANTS } from '../../theme/variants'
import { useTranslation } from 'react-i18next'
import SearchField from '../../components/common/SearchField'
import FilterSelect from '../../components/common/FilterSelect'
import ActionButton from '../../components/common/ActionButton'
import {
  chatChannelPath,
  DEFAULT_CHAT_CHANNEL_DETAIL_TAB,
  isChatChannelDetailTab,
  type ChatChannelDetailTab,
} from '../../router/routes'
import {
  chatChannelApi,
  type ChatChannel,
} from '../../services/api/chat-channel'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import { useIdentity } from '../../hooks/useIdentity'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { consolePaths } from '../../services/api/console'
import { isChatKeyInScope, parseInstanceKeyFromChatKey } from '../../services/api/instance-scope'
import { CHAT_CHANNEL_LIST_KEY } from './queryKeys'

export default function ChatChannelPage() {
  const [drawerOpen, setDrawerOpen] = useState(false)

  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('md'))
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  const { t } = useTranslation('chat-channel')
  const navigate = useNavigate()
  const { chatKey, tab } = useParams<{ chatKey: string; tab: string }>()
  const [searchParams] = useSearchParams()
  const selectedChatKey = chatKey ?? null
  const selectedTab = isChatChannelDetailTab(tab) ? tab : null

  // 本地搜索 / 筛选状态（从 sessionStorage 恢复，保证路由重挂载后不丢失）
  const [search, setSearch] = useState(() => sessionStorage.getItem('chatChannel.search') ?? '')
  const [chatType, setChatType] = useState(() => sessionStorage.getItem('chatChannel.chatType') ?? '')
  const [status, setStatus] = useState(() => sessionStorage.getItem('chatChannel.status') ?? '')
  const [botMembershipFilter, setBotMembershipFilter] = useState(
    () => sessionStorage.getItem('chatChannel.botMembership') ?? '',
  )
  const [checkDialogOpen, setCheckDialogOpen] = useState(false)
  const [disableDialogOpen, setDisableDialogOpen] = useState(false)

  useEffect(() => { sessionStorage.setItem('chatChannel.search', search) }, [search])
  useEffect(() => { sessionStorage.setItem('chatChannel.chatType', chatType) }, [chatType])
  useEffect(() => { sessionStorage.setItem('chatChannel.status', status) }, [status])
  useEffect(() => {
    sessionStorage.setItem('chatChannel.botMembership', botMembershipFilter)
  }, [botMembershipFilter])

  // 延迟搜索值，避免每次按键都触发请求
  const deferredSearch = useDeferredValue(search)

  const { currentInstanceKey, currentInstance, instances } = useInstanceScope()
  const { isPlatformOwner, isReadOnly } = useIdentity()

  const onebotInstances = useMemo(
    () => instances.filter(instance => instance.adapter_key === 'onebot_v11'),
    [instances],
  )
  const canCheckMembership = onebotInstances.length > 0 && !isReadOnly
  const canDisableNotInGroup = botMembershipFilter === 'not_in_group' && !isReadOnly

  const { data, isLoading } = useQuery({
    queryKey: [CHAT_CHANNEL_LIST_KEY, deferredSearch, chatType, status, botMembershipFilter, currentInstanceKey],
    queryFn: () =>
      chatChannelApi.getList({
        page: 1,
        page_size: 1000,
        search: deferredSearch || undefined,
        chat_type: chatType || undefined,
        status: status === '' ? undefined : status as ChatChannel['status'],
        bot_membership: botMembershipFilter === 'not_in_group' ? 'not_in_group' : undefined,
      }),
    staleTime: 30_000,
  })

  // 会话标识里编码了实例段，实例作用域下只保留属于本实例的会话。
  // 列表一次性取全量（page_size 1000）后在前端渲染，因此这里的过滤是完整的。
  const filteredChannels = (data?.items ?? []).filter(channel =>
    isChatKeyInScope(channel.chat_key, currentInstanceKey)
  )

  /**
   * 「切到这个实例，频道管理是空的」要说清楚是哪一种空
   *
   * 现场形态：协议端连上时自动登记成 `ob{QQ号}` 并把频道全部归给它，管理员手工建的实例
   * 一个会话也没有。只给一句"暂无数据"，管理员会以为是自己的实例还没人说话，
   * 而实际上他要找的会话就在旁边那个没人认领的实例底下。
   *
   * 计数直接从**本次已经取回来的**列表里按 `chat_key` 的实例段分组得到，不多发任何请求：
   * 后端的 `/chat-channel/list` 本来就返回当前身份可见的全部会话（租户已按归属过滤），
   * 实例作用域是前端这一层的过滤。实例的显示名与状态取自作用域切换器的轻量列表。
   */
  const strandedByInstance = useMemo(() => {
    const counts = new Map<string, number>()
    for (const channel of data?.items ?? []) {
      if (isChatKeyInScope(channel.chat_key, currentInstanceKey)) continue
      const owner = parseInstanceKeyFromChatKey(channel.chat_key)
      if (!owner) continue
      counts.set(owner, (counts.get(owner) ?? 0) + 1)
    }
    return [...counts.entries()]
      .map(([instanceKey, count]) => {
        const meta = instances.find(instance => instance.instance_key === instanceKey)
        return {
          instanceKey,
          count,
          label: meta?.display_name || instanceKey,
          unclaimed: meta?.status === 'unclaimed',
        }
      })
      .sort((a, b) => b.count - a.count)
  }, [data, currentInstanceKey, instances])

  const strandedTotal = strandedByInstance.reduce((sum, entry) => sum + entry.count, 0)
  const strandedUnclaimed = strandedByInstance.filter(entry => entry.unclaimed)
  const strandedUnclaimedTotal = strandedUnclaimed.reduce((sum, entry) => sum + entry.count, 0)

  useEffect(() => {
    const legacyChatKey = searchParams.get('chat_key')
    if (!legacyChatKey) return

    const nextParams = new URLSearchParams(searchParams)
    nextParams.delete('chat_key')
    const nextQuery = nextParams.toString()
    navigate(`${chatChannelPath(legacyChatKey, DEFAULT_CHAT_CHANNEL_DETAIL_TAB)}${nextQuery ? `?${nextQuery}` : ''}`, { replace: true })
  }, [navigate, searchParams])

  useEffect(() => {
    if (!selectedChatKey || selectedTab) return
    const nextQuery = searchParams.toString()
    navigate(
      `${chatChannelPath(selectedChatKey, DEFAULT_CHAT_CHANNEL_DETAIL_TAB)}${nextQuery ? `?${nextQuery}` : ''}`,
      { replace: true },
    )
  }, [navigate, searchParams, selectedChatKey, selectedTab])

  const buildChannelUrl = (
    nextChatKey?: string | null,
    nextTab?: ChatChannelDetailTab | null,
  ) => {
    const basePath = chatChannelPath(nextChatKey, nextChatKey ? (nextTab ?? DEFAULT_CHAT_CHANNEL_DETAIL_TAB) : null)
    return basePath
  }

  const handleDetailTabChange = (nextTab: ChatChannelDetailTab) => {
    if (!selectedChatKey) return
    navigate(buildChannelUrl(selectedChatKey, nextTab))
  }

  // 处理选择聊天
  const handleSelectChannel = (chatKey: string) => {
    navigate(buildChannelUrl(chatKey, selectedTab ?? DEFAULT_CHAT_CHANNEL_DETAIL_TAB))
    if (isMobile) {
      setDrawerOpen(false)
    }
  }

  // 返回聊天列表（移动端）
  const handleBackToList = () => {
    navigate(buildChannelUrl())
  }

  // 渲染聊天列表组件
  const renderChannelList = () => (
    <Box
      sx={{
        height: '100%',
        minWidth: 0,
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      <Box sx={{ p: { xs: 1.25, sm: 1.5 }, flexShrink: 0, minWidth: 0, bgcolor: 'background.paper' }}>
        <Stack spacing={1.5}>
          {/* 实例作用域提示 */}
          <InstanceScopeBanner description={t('banner.channels', { ns: 'instance-scope' })} />

          {/* 搜索框 */}
          <SearchField
            fullWidth
            size={isSmall ? 'small' : 'medium'}
            placeholder={t('search.placeholder')}
            value={search}
            onChange={setSearch}
            onClear={() => setSearch('')}
          />

          {/* 筛选选项 */}
          <Stack
            direction={isSmall ? 'column' : 'row'}
            spacing={1}
            sx={{ minWidth: 0, alignItems: 'stretch' }}
          >
            <FilterSelect
              label={t('filters.type')}
              value={chatType}
              onChange={setChatType}
              sx={{ minWidth: 0, flex: 1 }}
              options={[
                { value: '', label: t('filters.all') },
                { value: 'group', label: t('filters.group') },
                { value: 'private', label: t('filters.private') },
              ]}
            />
            <FilterSelect
              label={t('filters.status')}
              value={status}
              onChange={setStatus}
              sx={{ minWidth: 0, flex: 1 }}
              options={[
                { value: '', label: t('filters.all') },
                { value: 'active', label: t('filters.active') },
                { value: 'observe', label: t('filters.observe') },
                { value: 'disabled', label: t('filters.inactive') },
              ]}
            />
            <FilterSelect
              label={t('filters.botMembership')}
              value={botMembershipFilter}
              onChange={setBotMembershipFilter}
              sx={{ minWidth: 0, flex: 1 }}
              options={[
                { value: '', label: t('filters.all') },
                { value: 'not_in_group', label: t('filters.botNotInGroup') },
              ]}
            />
          </Stack>

          {/* 核对与批量停用：窄屏时按钮换行，不把筛选区撑破 */}
          {(canCheckMembership || canDisableNotInGroup) && (
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              {canCheckMembership && (
                <ActionButton size="small" onClick={() => setCheckDialogOpen(true)}>
                  {t('botMembership.check.button')}
                </ActionButton>
              )}
              {canDisableNotInGroup && (
                <ActionButton size="small" tone="danger" onClick={() => setDisableDialogOpen(true)}>
                  {t('botMembership.disable.button')}
                </ActionButton>
              )}
            </Stack>
          )}
        </Stack>
      </Box>

      <Divider />

      {/* 聊天列表 */}
      <Box
        sx={{
          flex: 1,
          minWidth: 0,
          minHeight: 0,
          overflowY: 'auto',
          overflowX: 'hidden',
          bgcolor: 'background.paper',
        }}
      >
        <ChatChannelList
          channels={filteredChannels}
          selectedChatKey={selectedChatKey}
          onSelectChannel={handleSelectChannel}
          isLoading={isLoading}
          emptyHint={
            currentInstanceKey && strandedTotal > 0 ? (
              <Stack spacing={1} alignItems="center">
                <Typography variant="body2" color="text.secondary">
                  {strandedUnclaimed.length > 0
                    ? t('list.emptyOtherUnclaimed', {
                        count: strandedUnclaimedTotal,
                        instance: strandedUnclaimed[0].label,
                      })
                    : t('list.emptyOtherInstances', {
                        count: strandedTotal,
                        instance: strandedByInstance[0].label,
                      })}
                </Typography>
                {isPlatformOwner && strandedUnclaimed.length > 0 && (
                  <ActionButton
                    tone="primary"
                    size="small"
                    onClick={() => navigate(consolePaths.instanceList())}
                  >
                    {t('list.emptyGoClaim')}
                  </ActionButton>
                )}
              </Stack>
            ) : null
          }
        />
      </Box>
    </Box>
  )

  // 渲染聊天详情组件
  const renderChannelDetail = () => (
    <>
      {selectedChatKey ? (
        <ChatChannelDetail
          chatKey={selectedChatKey}
          currentTab={selectedTab ?? DEFAULT_CHAT_CHANNEL_DETAIL_TAB}
          onTabChange={handleDetailTabChange}
          onBack={handleBackToList}
        />
      ) : (
        <Card
          sx={{
            ...CARD_VARIANTS.default.styles,
            height: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexDirection: 'column',
          }}
        >
          <InfoIcon sx={{ fontSize: 48, color: 'text.secondary', mb: 2, opacity: 0.7 }} />
          <Typography color="textSecondary">{t('detail.selectChat')}</Typography>
          {isMobile && (
            <ActionButton
              onClick={() => setDrawerOpen(true)}
              variant="outlined"
              startIcon={<ListIcon />}
              sx={{ mt: 2 }}
            >
              {t('actions.viewList')}
            </ActionButton>
          )}
        </Card>
      )}
    </>
  )

  return (
    <Box
      className="h-full flex gap-2 overflow-hidden p-2 box-border"
      sx={{ minWidth: 0, minHeight: 0 }}
    >
      {isMobile ? (
        // 移动端布局
        <>
          {/* 主内容区 - 根据是否选择聊天，显示详情或提示 */}
          <Box className="w-full flex-1 overflow-hidden" sx={{ minWidth: 0, minHeight: 0 }}>
            {selectedChatKey ? (
              <ChatChannelDetail
                chatKey={selectedChatKey}
                currentTab={selectedTab ?? DEFAULT_CHAT_CHANNEL_DETAIL_TAB}
                onTabChange={handleDetailTabChange}
                onBack={handleBackToList}
              />
            ) : (
              <Card
                sx={{
                  ...CARD_VARIANTS.default.styles,
                  height: '100%',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexDirection: 'column',
                }}
              >
                <InfoIcon sx={{ fontSize: 48, color: 'text.secondary', mb: 2, opacity: 0.7 }} />
                <Typography color="textSecondary">{t('detail.selectChat')}</Typography>
                <ActionButton
                  onClick={() => setDrawerOpen(true)}
                  variant="outlined"
                  startIcon={<ListIcon />}
                  sx={{ mt: 2 }}
                >
                  {t('actions.viewList')}
                </ActionButton>
              </Card>
            )}
          </Box>

          {/* 抽屉式侧边栏 - 聊天列表 */}
          <Drawer
            anchor="left"
            open={drawerOpen}
            onClose={() => setDrawerOpen(false)}
            ModalProps={{
              keepMounted: true,
            }}
            PaperProps={{
              sx: {
                width: isSmall ? 'min(88vw, 360px)' : '360px',
                maxWidth: '100vw',
                backgroundColor: 'background.paper',
                backgroundImage: 'none',
                borderRight: `1px solid ${theme.palette.divider}`,
                display: 'flex',
                flexDirection: 'column',
                overflow: 'hidden',
                boxShadow: theme.shadows[12],
              },
            }}
          >
            {renderChannelList()}
          </Drawer>

          {/* 浮动按钮 - 打开聊天列表 */}
          {!drawerOpen && (
            <Fab
              color="primary"
              size={isSmall ? 'medium' : 'large'}
              onClick={() => setDrawerOpen(true)}
              sx={{
                position: 'fixed',
                bottom: 16,
                right: 16,
                zIndex: theme.zIndex.drawer - 1,
              }}
            >
              <ListIcon />
            </Fab>
          )}
        </>
      ) : (
        // 桌面端布局
        <>
          <Box className="flex-1 overflow-hidden" sx={{ minWidth: 0, minHeight: 0 }}>
            {renderChannelDetail()}
          </Box>
          <Card
            sx={{
              ...CARD_VARIANTS.default.styles,
              width: '360px',
              height: '100%',
              flexShrink: 0,
              display: 'flex',
              flexDirection: 'column',
              minHeight: 0,
              overflow: 'hidden',
            }}
          >
            {renderChannelList()}
          </Card>
        </>
      )}

      <BotMembershipCheckDialog
        open={checkDialogOpen}
        onClose={() => setCheckDialogOpen(false)}
        instances={onebotInstances}
        currentInstanceKey={currentInstanceKey}
      />
      <BotMembershipDisableDialog
        open={disableDialogOpen}
        onClose={() => setDisableDialogOpen(false)}
        currentInstance={currentInstance}
      />
    </Box>
  )
}
