/**
 * 「有协议端连上来但没人认领」的引导面板（平台方专属）
 *
 * 这块 UI 存在的理由是一个真实故障：管理员在总后台建了实例、接了 NapCat，协议端连上时
 * 把自己登记成了 `ob{QQ号}` 并拿走了全部频道，而管理员建的那一行 `provider_account_id`
 * 为空、永远拿不到那条连接。界面此前把两行平铺在同一张列表里，管理员看到的是
 * 「我的实例离线 + 一个不认识的实例」，**没有任何地方告诉他真正在线的是旁边那个**。
 *
 * 所以这里不是又一个列表，而是一条明确的引导：先说清「有 N 个协议端已连接但未认领、
 * 它们名下压着多少会话」，再给出两条出路：
 *
 * - **认领为新实例**：用管理员自己想要的标识接管它（后端建新行 + 整体迁移，见 `claimInstance`）
 * - **并入已有实例**：把账号绑到既有实例并把历史频道迁过去（复用归属迁移）
 *
 * 两条出路都会移动数据归属，因此「并入」沿用与彻底删除、历史归属迁移相同的二次确认形态。
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  AlertTitle,
  Box,
  Checkbox,
  Chip,
  CircularProgress,
  Divider,
  FormControlLabel,
  Stack,
  TextField,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import CallMergeRoundedIcon from '@mui/icons-material/CallMergeRounded'
import HubRoundedIcon from '@mui/icons-material/HubRounded'
import PersonAddAltRoundedIcon from '@mui/icons-material/PersonAddAltRounded'
import ActionButton from '../../../components/common/ActionButton'
import FilterSelect from '../../../components/common/FilterSelect'
import NekroDialog from '../../../components/common/NekroDialog'
import SegmentedControl from '../../../components/common/SegmentedControl'
import { useIdentity } from '../../../hooks/useIdentity'
import { useNotification } from '../../../hooks/useNotification'
import { CHIP_VARIANTS } from '../../../theme/variants'
import { INSTANCE_KEY_PATTERN } from '../../../services/api/consoleContainer'
import {
  consoleApi,
  type InstanceClaimResult,
  type UnclaimedInstanceItem,
} from '../../../services/api/console'
import { CHAT_CHANNEL_LIST_KEY } from '../../chat-channel/queryKeys'
import CrossTenantSettingsChoice from './CrossTenantSettingsChoice'

/** 认领的两条出路 */
type ClaimMode = 'new' | 'adopt'

// 不导出：本文件只导出组件，否则 react-refresh 会因"文件里有非组件导出"而失效
const UNCLAIMED_QUERY_KEY = ['console-unclaimed-instances'] as const

interface UnclaimedProtocolPanelProps {
  /**
   * 预置的并入目标。实例详情页传入自己，面板据此直接把「并入本实例」作为默认出路
   * ——那正是「我建的实例一直显示未绑定账号」时管理员该做的事。
   */
  preferredTargetKey?: string
  /** 认领完成后的回调（用于跳转到认领后的实例） */
  onClaimed?: (result: InstanceClaimResult) => void
}

function formatDateTime(value: string | null, fallback: string): string {
  if (!value) return fallback
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return fallback
  return new Date(timestamp).toLocaleString()
}

export default function UnclaimedProtocolPanel({
  preferredTargetKey,
  onClaimed,
}: UnclaimedProtocolPanelProps) {
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('console')
  const { t: tCommon } = useTranslation('common')
  const { isPlatformOwner, isLoading: isIdentityLoading } = useIdentity()

  const [target, setTarget] = useState<UnclaimedInstanceItem | null>(null)
  const [mode, setMode] = useState<ClaimMode>('new')
  const [displayName, setDisplayName] = useState('')
  const [newInstanceKey, setNewInstanceKey] = useState('')
  const [adoptTargetKey, setAdoptTargetKey] = useState('')
  const [confirmKey, setConfirmKey] = useState('')
  // 并入两端归属不同时默认删除源实例上用户登记的服务器凭据（与跨租户换实例对同一口径），取消勾选才跟过去
  const [deleteSshCredentials, setDeleteSshCredentials] = useState(true)
  // 源实例的插件设置与配置覆盖同一口径：两端归属不同时默认删除
  const [deletePluginSettings, setDeletePluginSettings] = useState(true)
  const [deleteConfigOverrides, setDeleteConfigOverrides] = useState(true)

  // 待认领清单只对平台方开放（后端 Role.Super）。
  //
  // **租户身份下根本不发这个请求**，而不是发出去再吞掉 403：面板挂在实例列表 / 首页 /
  // 新建向导三处，而租户管理员（`Role.Admin`）看得到实例列表页，于是每开一次页面就多一条
  // 注定失败的请求与一条服务端越权日志。判据与 `useTenancyVisibility` 里租户列表查询的
  // `enabled` 逐字同形。身份未确认前也不发：此时 `isPlatformOwner` 只是兜底的"按平台方处理"。
  // 请求真的失败时（后端未就绪等）同样当作"没有待认领的协议端"，面板整体不渲染。
  const unclaimedQuery = useQuery({
    queryKey: UNCLAIMED_QUERY_KEY,
    queryFn: () => consoleApi.listUnclaimedInstances(),
    enabled: isPlatformOwner && !isIdentityLoading,
    staleTime: 15_000,
    retry: false,
  })

  const items = useMemo(() => unclaimedQuery.data?.items ?? [], [unclaimedQuery.data])
  const adoptTargets = useMemo(
    () => unclaimedQuery.data?.adopt_targets ?? [],
    [unclaimedQuery.data]
  )

  // 只有同一适配器下、且还没绑自己账号的实例能接管这个协议端
  const eligibleTargets = useMemo(() => {
    if (!target) return []
    return adoptTargets.filter(
      option => option.adapter_key === target.adapter_key && !option.provider_account_id
    )
  }, [adoptTargets, target])

  // 每次重新打开对话框都从头开始：认领会移动数据归属，不能让上一次的选择残留成默认值
  useEffect(() => {
    if (!target) return
    const preferred = preferredTargetKey ?? ''
    const canUsePreferred = eligibleTargets.some(option => option.instance_key === preferred)
    setMode(canUsePreferred ? 'adopt' : 'new')
    setDisplayName(target.display_name)
    setNewInstanceKey('')
    setAdoptTargetKey(canUsePreferred ? preferred : '')
    setConfirmKey('')
    setDeleteSshCredentials(true)
    setDeletePluginSettings(true)
    setDeleteConfigOverrides(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target])

  // 并入走的就是归属迁移：用换实例对的预览取「两端归属是否不同」与源实例上用户登记的服务器凭据条数
  const adoptPreviewQuery = useQuery({
    queryKey: ['console-instance-migration-preview', adoptTargetKey, target?.instance_key ?? ''],
    queryFn: () => consoleApi.previewInstanceMigration(adoptTargetKey, target?.instance_key ?? ''),
    enabled: mode === 'adopt' && Boolean(target) && Boolean(adoptTargetKey),
    retry: false,
    staleTime: 0,
  })
  const adoptSshCredentialCount = adoptPreviewQuery.data?.ssh_credentials ?? 0
  const showSshCredentialChoice =
    mode === 'adopt' && Boolean(adoptPreviewQuery.data?.cross_tenant) && adoptSshCredentialCount > 0

  const claimMutation = useMutation({
    mutationFn: (): Promise<InstanceClaimResult> => {
      if (!target) throw new Error('missing target')
      if (mode === 'adopt') {
        return consoleApi.adoptInstance(adoptTargetKey, {
          source_instance_key: target.instance_key,
          confirm_source_instance_key: confirmKey.trim(),
          delete_ssh_credentials: deleteSshCredentials,
          delete_plugin_settings: deletePluginSettings,
          delete_config_overrides: deleteConfigOverrides,
        })
      }
      return consoleApi.claimInstance(target.instance_key, {
        display_name: displayName.trim(),
        instance_key: newInstanceKey.trim(),
      })
    },
    onSuccess: result => {
      // 部分失败也是"已经改了库"：必须刷新，并且用告警而不是成功提示
      if (result.ok) {
        notification.success(result.message)
        setTarget(null)
      } else {
        notification.warning(result.message)
      }
      void queryClient.invalidateQueries({ queryKey: UNCLAIMED_QUERY_KEY })
      void queryClient.invalidateQueries({ queryKey: ['console-instances'] })
      void queryClient.invalidateQueries({ queryKey: ['console-instance'] })
      void queryClient.invalidateQueries({ queryKey: ['console-overview'] })
      void queryClient.invalidateQueries({ queryKey: ['instance-scope', 'instances'] })
      void queryClient.invalidateQueries({ queryKey: [CHAT_CHANNEL_LIST_KEY] })
      void queryClient.invalidateQueries({ queryKey: ['console-instance-migration-preview'] })
      if (result.ok) onClaimed?.(result)
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const newKeyError =
    newInstanceKey && !INSTANCE_KEY_PATTERN.test(newInstanceKey)
      ? t('claimDialog.instanceKeyInvalid')
      : null

  const canSubmit =
    !claimMutation.isPending &&
    Boolean(target) &&
    (mode === 'new'
      ? !newKeyError
      : // 预览没回来之前不让提交：否则凭据删不删由默认值决定，管理员却没看到条数
        Boolean(adoptTargetKey) &&
        Boolean(adoptPreviewQuery.data) &&
        confirmKey.trim() === target?.instance_key)

  if (unclaimedQuery.isError || items.length === 0) return null

  const totalChannels = items.reduce((sum, item) => sum + item.channel_count, 0)

  return (
    <>
      <Alert
        severity="warning"
        variant="outlined"
        icon={<HubRoundedIcon fontSize="inherit" />}
        sx={{ '& .MuiAlert-message': { width: '100%' } }}
      >
        <AlertTitle sx={{ fontWeight: 700 }}>
          {t('unclaimedPanel.title', { count: items.length })}
        </AlertTitle>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          {totalChannels > 0
            ? t('unclaimedPanel.descriptionWithChannels', { count: totalChannels })
            : t('unclaimedPanel.description')}
        </Typography>

        <Stack spacing={1}>
          {items.map(item => (
            <Box
              key={item.instance_key}
              sx={{
                display: 'flex',
                flexWrap: 'wrap',
                alignItems: 'center',
                gap: 1,
                px: 1.25,
                py: 1,
                borderRadius: 1,
                border: `1px solid ${theme.palette.divider}`,
              }}
            >
              <Box sx={{ minWidth: 0, flex: 1 }}>
                <Typography variant="body2" fontWeight={600} noWrap>
                  {t('unclaimedPanel.accountLabel', { account: item.provider_account_id || '—' })}
                </Typography>
                <Typography variant="caption" color="text.secondary" noWrap component="div">
                  {item.instance_key} · {t('unclaimedPanel.firstSeen')}{' '}
                  {formatDateTime(item.first_seen_at, '—')}
                </Typography>
                {item.dispatch_paused && (
                  <Typography variant="caption" color="warning.main" component="div">
                    {t('unclaimedPanel.dispatchPaused')}
                  </Typography>
                )}
              </Box>
              <Chip
                size="small"
                sx={CHIP_VARIANTS.getPaletteChip(theme, 'default', true)}
                label={t('card.channelCount', { count: item.channel_count })}
              />
              <Chip
                size="small"
                sx={CHIP_VARIANTS.getPaletteChip(theme, 'default', true)}
                label={t('card.messageToday', { count: item.message_count_today })}
              />
              <ActionButton
                tone="primary"
                size="small"
                startIcon={<PersonAddAltRoundedIcon fontSize="small" />}
                onClick={() => setTarget(item)}
              >
                {t('actions.claim')}
              </ActionButton>
            </Box>
          ))}
        </Stack>
      </Alert>

      <NekroDialog
        open={Boolean(target)}
        onClose={() => setTarget(null)}
        title={t('claimDialog.title')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setTarget(null)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone={mode === 'adopt' ? 'danger' : 'primary'}
              disabled={!canSubmit}
              onClick={() => claimMutation.mutate()}
            >
              {mode === 'adopt' ? t('claimDialog.submitAdopt') : t('claimDialog.submitNew')}
            </ActionButton>
          </>
        }
      >
        {target && (
          <Stack spacing={2} sx={{ pt: 1 }}>
            <Alert severity="info" variant="outlined">
              {t('claimDialog.summary', {
                account: target.provider_account_id || '—',
                instanceKey: target.instance_key,
                count: target.channel_count,
              })}
            </Alert>

            <SegmentedControl<ClaimMode>
              value={mode}
              options={[
                { value: 'new', label: t('claimDialog.modeNew') },
                { value: 'adopt', label: t('claimDialog.modeAdopt') },
              ]}
              onChange={next => {
                setMode(next)
                setConfirmKey('')
              }}
            />

            {mode === 'new' ? (
              <>
                <Typography variant="body2" color="text.secondary">
                  {t('claimDialog.modeNewHint')}
                </Typography>
                <TextField
                  size="small"
                  fullWidth
                  autoComplete="off"
                  label={t('claimDialog.displayName')}
                  helperText={t('claimDialog.displayNameHelper')}
                  value={displayName}
                  onChange={event => setDisplayName(event.target.value)}
                />
                <TextField
                  size="small"
                  fullWidth
                  autoComplete="off"
                  label={t('claimDialog.newInstanceKey')}
                  placeholder={target.instance_key}
                  error={Boolean(newKeyError)}
                  helperText={newKeyError ?? t('claimDialog.newInstanceKeyHelper')}
                  value={newInstanceKey}
                  onChange={event => setNewInstanceKey(event.target.value)}
                />
              </>
            ) : (
              <>
                <Typography variant="body2" color="text.secondary">
                  {t('claimDialog.modeAdoptHint')}
                </Typography>
                {eligibleTargets.length === 0 ? (
                  <Alert severity="warning">{t('claimDialog.noAdoptTargets')}</Alert>
                ) : (
                  <>
                    <FilterSelect
                      label={t('claimDialog.adoptTargetLabel')}
                      value={adoptTargetKey}
                      options={eligibleTargets.map(option => ({
                        value: option.instance_key,
                        label: option.display_name
                          ? `${option.display_name} (${option.instance_key})`
                          : option.instance_key,
                      }))}
                      onChange={value => {
                        setAdoptTargetKey(value)
                        setConfirmKey('')
                        setDeleteSshCredentials(true)
                        setDeletePluginSettings(true)
                        setDeleteConfigOverrides(true)
                      }}
                    />
                    {showSshCredentialChoice && (
                      <Box>
                        <FormControlLabel
                          control={
                            <Checkbox
                              checked={deleteSshCredentials}
                              disabled={claimMutation.isPending}
                              onChange={event => setDeleteSshCredentials(event.target.checked)}
                            />
                          }
                          label={
                            <Typography variant="body2">
                              {t('migrationDialog.deleteSshCredentialsLabel', {
                                count: adoptSshCredentialCount,
                              })}
                            </Typography>
                          }
                        />
                        <Typography
                          variant="caption"
                          color="text.secondary"
                          sx={{ display: 'block', ml: 4 }}
                        >
                          {t('migrationDialog.deleteSshCredentialsHint')}
                        </Typography>
                      </Box>
                    )}
                    {Boolean(adoptPreviewQuery.data?.cross_tenant) && (
                      <CrossTenantSettingsChoice
                        pluginSettings={adoptPreviewQuery.data?.plugin_settings ?? 0}
                        configOverrides={adoptPreviewQuery.data?.config_overrides ?? 0}
                        deletePluginSettings={deletePluginSettings}
                        deleteConfigOverrides={deleteConfigOverrides}
                        onDeletePluginSettingsChange={setDeletePluginSettings}
                        onDeleteConfigOverridesChange={setDeleteConfigOverrides}
                        disabled={claimMutation.isPending}
                      />
                    )}
                    <Divider />
                    <TextField
                      size="small"
                      fullWidth
                      autoComplete="off"
                      label={t('claimDialog.confirmLabel')}
                      placeholder={target.instance_key}
                      error={Boolean(confirmKey) && confirmKey.trim() !== target.instance_key}
                      helperText={
                        confirmKey && confirmKey.trim() !== target.instance_key
                          ? t('claimDialog.confirmMismatch')
                          : t('claimDialog.confirmHelper', { instanceKey: target.instance_key })
                      }
                      value={confirmKey}
                      onChange={event => setConfirmKey(event.target.value)}
                    />
                  </>
                )}
              </>
            )}

            {claimMutation.isPending && (
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                <CircularProgress size={18} />
                <Typography variant="caption" color="text.secondary">
                  {t('claimDialog.running')}
                </Typography>
              </Box>
            )}

            {claimMutation.data && !claimMutation.data.ok && (
              <Alert severity="warning" icon={<CallMergeRoundedIcon fontSize="inherit" />}>
                {claimMutation.data.message}
              </Alert>
            )}
          </Stack>
        )}
      </NekroDialog>
    </>
  )
}
