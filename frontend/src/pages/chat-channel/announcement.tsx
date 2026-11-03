import { useCallback, useDeferredValue, useMemo, useState } from 'react'
import {
  Box,
  Card,
  CardContent,
  Divider,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import SendRoundedIcon from '@mui/icons-material/SendRounded'
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  chatChannelApi,
  type AnnouncementInstanceOption,
  type AnnouncementScope,
  type AnnouncementScopeType,
  type ChatChannel,
} from '../../services/api/chat-channel'
import { CARD_VARIANTS } from '../../theme/variants'
import TablePaginationStyled from '../../components/common/TablePaginationStyled'
import { useNotification } from '../../hooks/useNotification'
import { useTranslation } from 'react-i18next'
import ChannelSelectorToolbar from './components/announcement/ChannelSelectorToolbar'
import ChannelSelectorList from './components/announcement/ChannelSelectorList'
import SelectedChannelSummary from './components/announcement/SelectedChannelSummary'
import AnnouncementScopeSelector from './components/announcement/AnnouncementScopeSelector'
import { instanceRefKey } from './components/announcement/scopeUtils'
import AnnouncementConfirmDialog from './components/announcement/AnnouncementConfirmDialog'
import AnnouncementProgressPanel from './components/announcement/AnnouncementProgressPanel'
import AnnouncementHistoryPanel from './components/announcement/AnnouncementHistoryPanel'
import { useAnnouncementJob } from './components/announcement/useAnnouncementJob'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'

const DEFAULT_PAGE = 1
const DEFAULT_PAGE_SIZE = 20

export default function ChatAnnouncementPage() {
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('chat-announcement')

  const [scopeType, setScopeType] = useState<AnnouncementScopeType>('channels')
  const [search, setSearch] = useState('')
  const [chatType, setChatType] = useState('')
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(DEFAULT_PAGE)
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE)
  const [message, setMessage] = useState('')
  const [selectedChannels, setSelectedChannels] = useState<Record<string, ChatChannel>>({})
  const [selectedInstances, setSelectedInstances] = useState<
    Record<string, AnnouncementInstanceOption>
  >({})
  const [confirmOpen, setConfirmOpen] = useState(false)

  const deferredSearch = useDeferredValue(search)

  const handleJobFinished = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['announcement-history'] })
  }, [queryClient])

  const {
    activeId,
    summary,
    deliveries,
    connectionState,
    isCancelling,
    startTracking,
    cancelActive,
    dismissFinished,
  } = useAnnouncementJob(handleJobFinished)

  const {
    data,
    isLoading,
    isError: isChannelsError,
    error: channelsError,
    refetch: refetchChannels,
  } = useQuery({
    queryKey: ['chat-announcement-channels', deferredSearch, chatType, status, page, pageSize],
    queryFn: () =>
      chatChannelApi.getList({
        page,
        page_size: pageSize,
        search: deferredSearch || undefined,
        chat_type: chatType || undefined,
        status: status === '' ? undefined : (status as 'active' | 'observe' | 'disabled'),
      }),
    staleTime: 30_000,
    enabled: scopeType === 'channels',
    // 403 不会因为再试一次变成 200
    retry: false,
  })

  const {
    data: instanceOptions,
    isLoading: loadingInstances,
    isError: isInstancesError,
    error: instancesError,
    refetch: refetchInstances,
  } = useQuery({
    queryKey: ['announcement-instances'],
    queryFn: () => chatChannelApi.getAnnouncementInstances(),
    staleTime: 30_000,
    retry: false,
  })

  /**
   * 这一页六个端点全是 `@require_role(Role.Admin)`（`routers/chat_channel.py`），
   * 只读租户成员一条都拿不到。此前**没有任何失败分支**：实例选择器空、频道列表空、
   * 历史面板空，而正中间摆着一个「发送公告」按钮 —— 一个点了必然 403 的按钮，
   * 摆在一页看起来"你名下一个群都没有"的界面上。
   *
   * 现在任何一条读请求失败就整页说清楚：403 说「你无权查看」，其余说「加载失败」并给重试。
   * 面板不再半瘫着渲染 —— 半瘫的界面比明确的拒绝更难排查。
   */
  /**
   * ⚠️ 频道列表那条查询是 `enabled: scopeType === 'channels'`，所以判据也必须带上作用域。
   *
   * react-query 不会因为查询被 disable 就把 `isError` 抹掉：在频道范围里失败一次之后
   * 切到实例范围，那条查询已经不发了，`isChannelsError` 却仍然为真 —— 整页会被一次
   * **与当前范围无关**的失败盖住。`retryAll` 那一行已经写了 `scopeType === 'channels'`，
   * 这里是同一个判据的第二处实现，漏了一处。
   */
  const isChannelsScope = scopeType === 'channels'
  const isChannelsFailed = isChannelsError && isChannelsScope
  const loadError = instancesError ?? (isChannelsFailed ? channelsError : undefined)
  const isLoadFailed = isInstancesError || isChannelsFailed
  const retryAll = () => {
    void refetchInstances()
    if (isChannelsScope) void refetchChannels()
  }

  const visibleChannels = data?.items ?? []
  const trimmedMessage = message.trim()
  const selectedChannelList = useMemo(
    () =>
      Object.values(selectedChannels).sort((left, right) => {
        const leftName = left.channel_name || left.chat_key
        const rightName = right.channel_name || right.chat_key
        return leftName.localeCompare(rightName, 'zh-CN')
      }),
    [selectedChannels]
  )
  const selectedChatKeySet = useMemo(
    () => new Set(Object.keys(selectedChannels)),
    [selectedChannels]
  )
  const selectedInstanceKeySet = useMemo(
    () => new Set(Object.keys(selectedInstances)),
    [selectedInstances]
  )

  const scope = useMemo<AnnouncementScope>(
    () => ({
      scope_type: scopeType,
      instances:
        scopeType === 'instances'
          ? Object.values(selectedInstances).map(instance => ({
              adapter_key: instance.adapter_key,
              instance_key: instance.instance_key,
            }))
          : [],
      chat_keys: scopeType === 'channels' ? selectedChannelList.map(channel => channel.chat_key) : [],
    }),
    [scopeType, selectedInstances, selectedChannelList]
  )

  const { data: preview, isLoading: loadingPreview } = useQuery({
    queryKey: ['announcement-preview', scope],
    queryFn: () => chatChannelApi.previewAnnouncement(scope),
    enabled: confirmOpen,
  })

  const submitMutation = useMutation({
    mutationFn: () => chatChannelApi.submitAnnouncement({ content: trimmedMessage, scope }),
    onSuccess: response => {
      setConfirmOpen(false)
      setMessage('')
      startTracking(response.announcement_id)
      notification.info(t('composer.submitted', { count: response.total }))
    },
    onError: error => {
      notification.error(error instanceof Error ? error.message : String(error))
    },
  })

  const toggleChannel = (channel: ChatChannel) => {
    setSelectedChannels(previous => {
      if (previous[channel.chat_key]) {
        const { [channel.chat_key]: _removed, ...next } = previous
        return next
      }
      return {
        ...previous,
        [channel.chat_key]: channel,
      }
    })
  }

  const toggleInstance = (instance: AnnouncementInstanceOption) => {
    const key = instanceRefKey(instance)
    setSelectedInstances(previous => {
      if (previous[key]) {
        const { [key]: _removed, ...next } = previous
        return next
      }
      return { ...previous, [key]: instance }
    })
  }

  const selectVisibleChannels = () => {
    setSelectedChannels(previous => {
      const next = { ...previous }
      visibleChannels.forEach(channel => {
        next[channel.chat_key] = channel
      })
      return next
    })
  }

  const clearSelection = () => {
    setSelectedChannels({})
  }

  const removeSelectedChannel = (chatKey: string) => {
    setSelectedChannels(previous => {
      const { [chatKey]: _removed, ...next } = previous
      return next
    })
  }

  const handleSend = () => {
    if (!trimmedMessage) {
      notification.warning(t('composer.messageRequired'))
      return
    }
    if (scopeType === 'channels' && selectedChannelList.length === 0) {
      notification.warning(t('composer.selectionRequired'))
      return
    }
    if (scopeType === 'instances' && Object.keys(selectedInstances).length === 0) {
      notification.warning(t('composer.instanceRequired'))
      return
    }
    setConfirmOpen(true)
  }

  const handleCancelActive = () => {
    void cancelActive().catch((error: unknown) => {
      notification.error(error instanceof Error ? error.message : String(error))
    })
  }

  const isTracking = activeId !== null

  if (isLoadFailed) {
    return (
      <Box
        sx={{
          p: 2,
          height: '100%',
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
          boxSizing: 'border-box',
        }}
      >
        <QueryErrorState error={loadError} onRetry={retryAll} sx={{ flex: 1 }} />
      </Box>
    )
  }

  return (
    <Box
      sx={{
        p: 2,
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        overflow: 'hidden',
        boxSizing: 'border-box',
      }}
    >
      <AnnouncementConfirmDialog
        open={confirmOpen}
        message={trimmedMessage}
        preview={preview ?? null}
        loading={loadingPreview}
        submitting={submitMutation.isPending}
        onClose={() => setConfirmOpen(false)}
        onConfirm={() => submitMutation.mutate()}
      />

      <Box
        sx={{
          display: 'grid',
          gap: 2,
          flex: 1,
          minHeight: 0,
          overflow: 'hidden',
          gridTemplateColumns: {
            xs: '1fr',
            xl: 'minmax(360px, 420px) minmax(0, 1fr)',
          },
        }}
      >
        <Card
          sx={{
            ...CARD_VARIANTS.default.styles,
            display: 'flex',
            flexDirection: 'column',
            minHeight: 0,
          }}
        >
          <CardContent
            sx={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1, minHeight: 0 }}
          >
            <AnnouncementScopeSelector
              scopeType={scopeType}
              instanceOptions={instanceOptions ?? []}
              selectedInstanceKeys={selectedInstanceKeySet}
              loadingInstances={loadingInstances}
              onScopeTypeChange={setScopeType}
              onToggleInstance={toggleInstance}
            />

            {scopeType === 'channels' && (
              <>
                <Divider />

                <ChannelSelectorToolbar
                  search={search}
                  chatType={chatType}
                  status={status}
                  selectedCount={selectedChannelList.length}
                  visibleCount={visibleChannels.length}
                  onSearchChange={value => {
                    setSearch(value)
                    setPage(DEFAULT_PAGE)
                  }}
                  onChatTypeChange={value => {
                    setChatType(value)
                    setPage(DEFAULT_PAGE)
                  }}
                  onStatusChange={value => {
                    setStatus(value)
                    setPage(DEFAULT_PAGE)
                  }}
                  onSelectVisible={selectVisibleChannels}
                  onClearSelection={clearSelection}
                />

                <Divider />

                <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', pr: 0.5 }}>
                  <ChannelSelectorList
                    channels={visibleChannels}
                    selectedChatKeys={selectedChatKeySet}
                    loading={isLoading}
                    onToggle={toggleChannel}
                  />
                </Box>
              </>
            )}
          </CardContent>

          {scopeType === 'channels' && (
            <TablePaginationStyled
              component="div"
              count={data?.total ?? 0}
              page={page - 1}
              rowsPerPage={pageSize}
              rowsPerPageOptions={[10, 20, 50]}
              onPageChange={(_, nextPage) => setPage(nextPage + 1)}
              onRowsPerPageChange={event => {
                setPage(DEFAULT_PAGE)
                setPageSize(parseInt(event.target.value, 10))
              }}
              loading={isLoading}
              showFirstLastPageButtons={true}
              sx={{
                width: '100%',
                overflow: 'hidden',
                '.MuiTablePagination-toolbar': {
                  minHeight: 52,
                  px: 1,
                  gap: 0.5,
                  flexWrap: { xs: 'wrap', md: 'nowrap' },
                },
                '.MuiTablePagination-spacer': {
                  display: { xs: 'none', md: 'block' },
                },
                '.MuiTablePagination-selectLabel, .MuiTablePagination-displayedRows': {
                  margin: 0,
                  flexShrink: 0,
                },
                '.MuiTablePagination-actions': {
                  marginLeft: 'auto',
                  flexShrink: 0,
                },
              }}
            />
          )}
        </Card>

        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            gap: 2,
            minHeight: 0,
            overflowY: 'auto',
            pr: { xl: 0.5 },
          }}
        >
          <Card sx={{ ...CARD_VARIANTS.default.styles }}>
            <CardContent
              sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0 }}
            >
              <Box
                sx={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  gap: 1,
                  flexWrap: 'wrap',
                  alignItems: 'center',
                }}
              >
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                  <Typography variant="h6" sx={{ fontWeight: 700 }}>
                    {t('composer.title')}
                  </Typography>
                  <Tooltip title={t('subtitle')} arrow placement="top">
                    <span>
                      <IconActionButton size="small" aria-label={t('composer.help')}>
                        <InfoOutlinedIcon fontSize="small" />
                      </IconActionButton>
                    </span>
                  </Tooltip>
                </Box>

                {scopeType === 'channels' && (
                  <Typography variant="body2" color="text.secondary">
                    {t('composer.selectionSummary', {
                      selected: selectedChannelList.length,
                      filtered: data?.total ?? 0,
                    })}
                  </Typography>
                )}
              </Box>

              {scopeType === 'channels' && (
                <SelectedChannelSummary
                  channels={selectedChannelList}
                  onRemove={removeSelectedChannel}
                  onClear={clearSelection}
                />
              )}

              <Divider />

              <TextField
                fullWidth
                multiline
                minRows={8}
                label={t('composer.messageLabel')}
                placeholder={t('composer.messagePlaceholder')}
                value={message}
                onChange={event => setMessage(event.target.value)}
              />

              <Box
                sx={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  gap: 1,
                  flexWrap: 'wrap',
                  alignItems: 'center',
                }}
              >
                <Typography variant="caption" color="text.secondary">
                  {t('composer.helper')}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {t('composer.length', { count: trimmedMessage.length })}
                </Typography>
              </Box>

              <Divider />

              <Box
                sx={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  gap: 1.5,
                  flexWrap: 'wrap',
                  alignItems: 'center',
                }}
              >
                <Box sx={{ minWidth: 0 }}>
                  <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
                    {t('composer.actionTitle')}
                  </Typography>
                  <Typography variant="body2" color="text.secondary">
                    {t(`composer.actionHint.${scopeType}`)}
                  </Typography>
                </Box>

                <ActionButton
                  tone="primary"
                  size="medium"
                  startIcon={<SendRoundedIcon />}
                  onClick={handleSend}
                  disabled={submitMutation.isPending || isTracking}
                  sx={{
                    minWidth: { xs: '100%', sm: 220 },
                  }}
                >
                  {isTracking ? t('composer.sending') : t('composer.send')}
                </ActionButton>
              </Box>
            </CardContent>
          </Card>

          {summary && (
            <Card sx={{ ...CARD_VARIANTS.default.styles }}>
              <CardContent>
                <AnnouncementProgressPanel
                  summary={summary}
                  deliveries={deliveries}
                  connectionState={connectionState}
                  isActive={isTracking}
                  isCancelling={isCancelling}
                  onCancel={handleCancelActive}
                  onDismiss={dismissFinished}
                />
              </CardContent>
            </Card>
          )}

          <Card sx={{ ...CARD_VARIANTS.default.styles }}>
            <CardContent>
              <AnnouncementHistoryPanel />
            </CardContent>
          </Card>
        </Box>
      </Box>
    </Box>
  )
}
