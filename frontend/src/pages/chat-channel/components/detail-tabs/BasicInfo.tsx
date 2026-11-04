import React, { useState, useEffect } from 'react'
import {
  Box,
  Typography,
  Stack,
  Paper,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  List,
  ListItem,
  ListItemText,
  ListItemButton,
  ListItemAvatar,
  Avatar,
  CircularProgress,
  TextField,
  InputAdornment,
  Chip,
  Divider,
} from '@mui/material'
import {
  Message as MessageIcon,
  Person as PersonIcon,
  AccessTime as AccessTimeIcon,
  Update as UpdateIcon,
  Face as FaceIcon,
  Search as SearchIcon,
  CloudDownload as CloudDownloadIcon,
  ContentCopy as ContentCopyIcon,
} from '@mui/icons-material'
import { ChatChannelDetail, chatChannelApi } from '../../../../services/api/chat-channel'
import { Preset, presetsApi } from '../../../../services/api/presets'
import { useSnackbar } from 'notistack'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { copyText } from '../../../../utils/clipboard'
import ActionButton from '../../../../components/common/ActionButton'
import PresetSwitchConfirmDialog from '../../../../components/common/PresetSwitchConfirmDialog'
import type { PresetSwitchImpact } from '../../../../services/api/types'
import { useInstanceScope } from '../../../../hooks/useInstanceScope'
import { CHANNEL_DIRECTORY_KEY, CHAT_CHANNEL_DETAIL_KEY, CHAT_CHANNEL_LIST_KEY } from '../../queryKeys'

interface BasicInfoProps {
  channel: ChatChannelDetail
}

export default function BasicInfo({ channel }: BasicInfoProps) {
  const [presetDialogOpen, setPresetDialogOpen] = useState(false)
  const [customNameDialogOpen, setCustomNameDialogOpen] = useState(false)
  const [customName, setCustomName] = useState(channel.custom_channel_name ?? '')
  const [presets, setPresets] = useState<Preset[]>([])
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [currentPreset, setCurrentPreset] = useState<Preset | null>(null)
  // 换人设的二次确认：先拿影响面，再让人点一次。
  // 人设是隔离键的一维，换掉它 = 本频道整体切到另一份好感与记忆，
  // 群里所有人的好感当场看起来归零（实为切到另一份，旧的还在、换回来就回来）。
  const [pendingPreset, setPendingPreset] = useState<{ preset: Preset | null } | null>(null)
  const [presetImpact, setPresetImpact] = useState<PresetSwitchImpact | null>(null)
  const [impactLoading, setImpactLoading] = useState(false)
  const [impactError, setImpactError] = useState('')
  const { enqueueSnackbar } = useSnackbar()
  const queryClient = useQueryClient()
  const { t } = useTranslation('chat-channel')
  // 人设接口对租户身份强制要求实例作用域，且作用域确定之前发出去必然是 400
  const { currentInstanceKey, isScopeReady } = useInstanceScope()

  useEffect(() => {
    setCustomName(channel.custom_channel_name ?? '')
  }, [channel.custom_channel_name])

  // 获取当前聊天频道的人设信息
  useEffect(() => {
    const fetchCurrentPreset = async () => {
      if (!isScopeReady) {
        return
      }
      if (channel.preset_id) {
        try {
          setLoading(true)
          const preset = await presetsApi.getDetail(channel.preset_id, currentInstanceKey ?? undefined)
          setCurrentPreset(preset)
        } finally {
          setLoading(false)
        }
      } else {
        setCurrentPreset(null)
      }
    }
    fetchCurrentPreset()
  }, [channel.preset_id, currentInstanceKey, isScopeReady])

  // 打开人设选择对话框时加载人设列表
  const handleOpenPresetDialog = async () => {
    setPresetDialogOpen(true)
    await loadPresets()
  }

  // 加载人设列表
  const loadPresets = async () => {
    try {
      setLoading(true)
      const response = await presetsApi.getList({
        page: 1,
        page_size: 100,
        search: search || undefined,
        instance_key: currentInstanceKey ?? undefined,
      })
      setPresets(response.items)
    } catch (_error) {
      enqueueSnackbar(t('basicInfo.fetchPresetsFailed'), { variant: 'error' })
    } finally {
      setLoading(false)
    }
  }

  // 搜索人设
  const handleSearch = async () => {
    await loadPresets()
  }

  const handleSaveCustomName = async () => {
    try {
      setLoading(true)
      const nextName = customName.trim()
      await chatChannelApi.setCustomChannelName(channel.chat_key, nextName || null)
      enqueueSnackbar(t('basicInfo.saveCustomNameSuccess'), { variant: 'success' })
      setCustomNameDialogOpen(false)
      queryClient.invalidateQueries({ queryKey: [CHAT_CHANNEL_DETAIL_KEY, channel.chat_key] })
      queryClient.invalidateQueries({ queryKey: [CHAT_CHANNEL_LIST_KEY] })
      queryClient.invalidateQueries({ queryKey: [CHANNEL_DIRECTORY_KEY] })
    } catch (_error) {
      enqueueSnackbar(t('basicInfo.saveCustomNameFailed'), { variant: 'error' })
    } finally {
      setLoading(false)
    }
  }

  // 真正落库的那一步。**只在确认过之后调用**（或影响面为"没变"时直接调用）
  const applyPreset = async (preset: Preset | null, confirmed: boolean) => {
    try {
      setLoading(true)
      await chatChannelApi.setPreset(channel.chat_key, preset?.id || null, confirmed)
      enqueueSnackbar(
        t('basicInfo.setPresetSuccess', {
          action: preset ? t('basicInfo.set') : t('basicInfo.clear'),
          name: preset?.title || t('basicInfo.defaultPreset'),
        }),
        {
          variant: 'success',
        }
      )
      setPresetDialogOpen(false)
      setPendingPreset(null)

      // 刷新聊天频道详情
      queryClient.invalidateQueries({ queryKey: [CHAT_CHANNEL_DETAIL_KEY, channel.chat_key] })
    } catch (_error) {
      enqueueSnackbar(t('basicInfo.setPresetFailed'), { variant: 'error' })
    } finally {
      setLoading(false)
    }
  }

  // 选择人设：先问后端"这次会影响谁"，真的换了人才弹确认框
  const handleSelectPreset = async (preset: Preset | null) => {
    setPendingPreset({ preset })
    setPresetImpact(null)
    setImpactError('')
    setImpactLoading(true)
    try {
      const impact = await chatChannelApi.getPresetImpact(channel.chat_key, preset?.id ?? null)
      setImpactLoading(false)
      if (impact.is_noop) {
        // 生效人设其实没变（典型是把实例默认人设显式绑到频道上，纯运维动作）。
        // 拦它只会让运营学会一路点确认，而那正是二次确认失效的方式。
        setPendingPreset(null)
        await applyPreset(preset, false)
        return
      }
      setPresetImpact(impact)
    } catch (error) {
      setImpactLoading(false)
      setImpactError(error instanceof Error ? error.message : String(error))
    }
  }

  const handleCopyChannelId = async () => {
    const success = await copyText(channel.chat_key)
    if (success) {
      enqueueSnackbar('频道 ID 已复制到剪贴板', { variant: 'success' })
      return
    }

    enqueueSnackbar('复制失败，请稍后重试', { variant: 'error' })
  }

  const InfoItem = ({
    icon,
    label,
    value,
    action,
  }: {
    icon: React.ReactNode
    label: string
    value: React.ReactNode
    action?: React.ReactNode
  }) => (
    <Paper variant="outlined" className="p-3">
      <Stack direction="row" spacing={2} alignItems="center">
        {icon}
        <Box className="flex-1">
          <Typography variant="body2" color="textSecondary" className="mb-1">
            {label}
          </Typography>
          <Typography variant="body1" component="div">
            {value}
          </Typography>
        </Box>
        {action}
      </Stack>
    </Paper>
  )

  return (
    <Box className="p-4">
      <Stack spacing={2}>
        <InfoItem
          icon={<MessageIcon color="primary" />}
          label={t('basicInfo.displayName')}
          value={
            <Stack spacing={0.5}>
              <Typography component="div">
                {channel.custom_channel_name || channel.channel_name || channel.chat_key}
              </Typography>
              {channel.custom_channel_name && (
                <Typography variant="caption" color="textSecondary" component="div" sx={{ wordBreak: 'break-all' }}>
                  {t('basicInfo.originalName')}: {channel.channel_name || channel.chat_key}
                </Typography>
              )}
            </Stack>
          }
          action={
            <ActionButton
              size="small"
              variant="outlined"
              onClick={() => setCustomNameDialogOpen(true)}
              disabled={loading}
            >
              {t('basicInfo.editCustomName')}
            </ActionButton>
          }
        />
        <InfoItem
          icon={<FaceIcon color="secondary" />}
          label={t('basicInfo.currentPreset')}
          value={
            loading ? (
              <CircularProgress size={16} />
            ) : currentPreset ? (
              <Stack direction="row" spacing={1} alignItems="center">
                <Avatar
                  src={currentPreset.avatar}
                  alt={currentPreset.name}
                  sx={{ width: 24, height: 24, marginRight: 0.5 }}
                />
                <Typography component="div">
                  {currentPreset.title}
                  {currentPreset.is_remote && (
                    <Chip
                      icon={<CloudDownloadIcon sx={{ fontSize: '0.7rem !important' }} />}
                      label={t('basicInfo.cloud')}
                      size="small"
                      color="primary"
                      variant="outlined"
                      sx={{ ml: 1, height: 20, fontSize: '0.7rem' }}
                    />
                  )}
                </Typography>
              </Stack>
            ) : (
              t('basicInfo.defaultPreset')
            )
          }
          action={
            <ActionButton
              size="small"
              variant="outlined"
              onClick={handleOpenPresetDialog}
              disabled={loading}
            >
              {t('basicInfo.selectPreset')}
            </ActionButton>
          }
        />
        <InfoItem
          icon={<MessageIcon color="primary" />}
          label={t('basicInfo.messageCount')}
          value={t('basicInfo.messagesCount', { count: channel.message_count })}
        />
        <InfoItem
          icon={<PersonIcon color="info" />}
          label={t('basicInfo.uniqueUsers')}
          value={t('basicInfo.usersCount', { count: channel.unique_users })}
        />
        <InfoItem
          icon={<AccessTimeIcon color="success" />}
          label={t('basicInfo.startTime')}
          value={channel.conversation_start_time}
        />
        <InfoItem
          icon={<UpdateIcon color="warning" />}
          label={t('basicInfo.lastActiveTime')}
          value={channel.last_message_time || t('basicInfo.noMessages')}
        />
        <InfoItem
          icon={<PersonIcon color="action" />}
          label="频道 ID"
          value={
            <Typography component="div" sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>
              {channel.chat_key}
            </Typography>
          }
          action={
            <ActionButton
              size="small"
              variant="outlined"
              startIcon={<ContentCopyIcon />}
              onClick={handleCopyChannelId}
            >
              复制 ID
            </ActionButton>
          }
        />
      </Stack>

      <Dialog
        open={customNameDialogOpen}
        onClose={() => setCustomNameDialogOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>{t('basicInfo.editCustomName')}</DialogTitle>
        <DialogContent>
          <Box className="mt-2">
            <TextField
              fullWidth
              size="small"
              label={t('basicInfo.customName')}
              placeholder={channel.channel_name || channel.chat_key}
              value={customName}
              inputProps={{ maxLength: 64 }}
              helperText={t('basicInfo.customNamePlaceholder')}
              onChange={event => setCustomName(event.target.value)}
              onKeyDown={event => event.key === 'Enter' && handleSaveCustomName()}
            />
          </Box>
        </DialogContent>
        <DialogActions>
          <ActionButton tone="secondary" onClick={() => setCustomNameDialogOpen(false)}>
            {t('basicInfo.cancel')}
          </ActionButton>
          <ActionButton onClick={handleSaveCustomName} disabled={loading}>
            {loading ? <CircularProgress size={20} /> : t('basicInfo.save')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 人设选择对话框 */}
      <Dialog
        open={presetDialogOpen}
        onClose={() => setPresetDialogOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>{t('basicInfo.selectPreset')}</DialogTitle>
        <DialogContent>
          <Box className="mb-3 mt-2">
            <TextField
              fullWidth
              size="small"
              placeholder={t('basicInfo.searchPreset')}
              value={search}
              onChange={e => setSearch(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSearch()}
              InputProps={{
                startAdornment: (
                  <InputAdornment position="start">
                    <SearchIcon fontSize="small" />
                  </InputAdornment>
                ),
                endAdornment: (
                  <InputAdornment position="end">
                    <ActionButton size="small" onClick={handleSearch} disabled={loading}>
                      {t('basicInfo.search')}
                    </ActionButton>
                  </InputAdornment>
                ),
              }}
            />
          </Box>

          <Box className="mb-3">
            <ActionButton
              fullWidth
              variant="outlined"
              onClick={() => handleSelectPreset(null)}
              disabled={loading}
            >
              {t('basicInfo.useDefault')}
            </ActionButton>
          </Box>

          <Divider className="mb-2" />

          {loading ? (
            <Box className="flex justify-center p-4">
              <CircularProgress />
            </Box>
          ) : presets.length > 0 ? (
            <List sx={{ maxHeight: '400px', overflow: 'auto' }}>
              {presets.map(preset => (
                <ListItem key={preset.id} disablePadding>
                  <ListItemButton onClick={() => handleSelectPreset(preset)}>
                    <ListItemAvatar>
                      <Avatar src={preset.avatar} alt={preset.name} />
                    </ListItemAvatar>
                    <ListItemText
                      primary={
                        <Stack direction="row" spacing={1} alignItems="center">
                          {preset.title}
                          {preset.is_remote && (
                            <Chip
                              icon={<CloudDownloadIcon sx={{ fontSize: '0.7rem !important' }} />}
                              label={t('basicInfo.cloud')}
                              size="small"
                              color="primary"
                              variant="outlined"
                              sx={{ height: 20, fontSize: '0.7rem' }}
                            />
                          )}
                        </Stack>
                      }
                      secondary={preset.description || preset.name}
                    />
                  </ListItemButton>
                </ListItem>
              ))}
            </List>
          ) : (
            <Typography className="p-4 text-center" color="textSecondary">
              {t('search.noResults')}
            </Typography>
          )}
        </DialogContent>
        <DialogActions>
          <ActionButton onClick={() => setPresetDialogOpen(false)}>{t('basicInfo.cancel')}</ActionButton>
        </DialogActions>
      </Dialog>

      {/* 换人设的二次确认。数字全部来自后端，前端一个字都不复算 */}
      <PresetSwitchConfirmDialog
        open={Boolean(pendingPreset)}
        impact={presetImpact}
        loading={impactLoading}
        error={impactError}
        submitting={loading}
        onCancel={() => setPendingPreset(null)}
        onConfirm={() => {
          if (pendingPreset) void applyPreset(pendingPreset.preset, true)
        }}
      />
    </Box>
  )
}
