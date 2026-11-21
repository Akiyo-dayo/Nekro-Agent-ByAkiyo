/**
 * 实例历史归属迁移对话框（平台方专属）
 *
 * 协议端在管理员建实例之前先连上时会被自动登记成 `ob{账号}` 并认领历史会话，管理员随后新建的
 * 实例只拿到一行空壳。两行 `instance_key` 不同，按隔离模型是**合法的两个会话**，后端不会自动
 * 合并——这里就是那个显式的迁移入口。
 *
 * 操作不可逆，因此界面强制走「选源 → 看预览 → 回填源实例标识确认 → 执行」四步：
 * 预览未加载出来、或确认输入不匹配时，执行按钮始终禁用。
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Checkbox,
  Chip,
  CircularProgress,
  Divider,
  FormControlLabel,
  SxProps,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Theme,
  Typography,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../../components/common/ActionButton'
import FilterSelect from '../../../components/common/FilterSelect'
import NekroDialog from '../../../components/common/NekroDialog'
import { useNotification } from '../../../hooks/useNotification'
import { UNIFIED_TABLE_STYLES } from '../../../theme/variants'
import { consoleApi, type InstanceInfo } from '../../../services/api/console'
import CrossTenantSettingsChoice from './CrossTenantSettingsChoice'

interface InstanceMigrationDialogProps {
  open: boolean
  onClose: () => void
  /** 接收历史的实例，即当前详情页这一个 */
  target: InstanceInfo
}

export default function InstanceMigrationDialog({
  open,
  onClose,
  target,
}: InstanceMigrationDialogProps) {
  const { t } = useTranslation('console')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()

  const [sourceKey, setSourceKey] = useState('')
  const [confirmKey, setConfirmKey] = useState('')
  // 跨租户时默认删除用户登记的服务器凭据（与租户划转同一条取舍），取消勾选才跟到目标实例
  const [deleteSshCredentials, setDeleteSshCredentials] = useState(true)
  // 插件设置与配置覆盖同一条取舍：跨租户时默认删除
  const [deletePluginSettings, setDeletePluginSettings] = useState(true)
  const [deleteConfigOverrides, setDeleteConfigOverrides] = useState(true)

  // 每次重新打开都从"还没选源实例"开始：迁移不可逆，不能让上一次的选择残留成默认值
  useEffect(() => {
    if (open) {
      setSourceKey('')
      setConfirmKey('')
      setDeleteSshCredentials(true)
      setDeletePluginSettings(true)
      setDeleteConfigOverrides(true)
    }
  }, [open])

  const instancesQuery = useQuery({
    queryKey: ['console-instances'],
    queryFn: () => consoleApi.listInstances(),
    enabled: open,
    staleTime: 15_000,
  })

  // 只有同一适配器下的其他实例可以作为源：`chat_key` 的适配器段与 channel_id 命名空间都跟着适配器走
  const sourceOptions = useMemo(() => {
    const items = instancesQuery.data?.items ?? []
    return items
      .filter(
        item => item.adapter_key === target.adapter_key && item.instance_key !== target.instance_key
      )
      .map(item => ({
        value: item.instance_key,
        label: item.display_name
          ? `${item.display_name} (${item.instance_key})`
          : item.instance_key,
      }))
  }, [instancesQuery.data, target.adapter_key, target.instance_key])

  const previewQuery = useQuery({
    queryKey: ['console-instance-migration-preview', target.instance_key, sourceKey],
    queryFn: () => consoleApi.previewInstanceMigration(target.instance_key, sourceKey),
    enabled: open && Boolean(sourceKey),
    retry: false,
    staleTime: 0,
  })

  const migrateMutation = useMutation({
    mutationFn: () =>
      consoleApi.migrateInstanceHistory(target.instance_key, {
        source_instance_key: sourceKey,
        confirm_source_instance_key: confirmKey.trim(),
        delete_ssh_credentials: deleteSshCredentials,
        delete_plugin_settings: deletePluginSettings,
        delete_config_overrides: deleteConfigOverrides,
      }),
    onSuccess: result => {
      // 部分失败也是"已经改了库"，必须刷新列表，并且用告警而不是成功提示
      if (result.ok) {
        notification.success(result.message)
      } else {
        notification.warning(result.message)
      }
      void queryClient.invalidateQueries({ queryKey: ['console-instances'] })
      void queryClient.invalidateQueries({ queryKey: ['console-instance'] })
      void queryClient.invalidateQueries({ queryKey: ['console-overview'] })
      void queryClient.invalidateQueries({ queryKey: ['console-instance-migration-preview'] })
      if (result.ok) {
        onClose()
      }
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const preview = previewQuery.data
  const canSubmit =
    Boolean(preview) && confirmKey.trim() === sourceKey && !migrateMutation.isPending

  const countEntries = Object.entries(preview?.counts ?? {}).filter(([, count]) => count > 0)
  const sshCredentialCount = preview?.ssh_credentials ?? 0
  const showSshCredentialChoice = Boolean(preview?.cross_tenant) && sshCredentialCount > 0

  return (
    <NekroDialog
      open={open}
      onClose={onClose}
      title={t('migrationDialog.title')}
      maxWidth="md"
      actions={
        <>
          <ActionButton tone="ghost" onClick={onClose}>
            {tCommon('actions.cancel')}
          </ActionButton>
          <ActionButton
            tone="danger"
            disabled={!canSubmit}
            onClick={() => migrateMutation.mutate()}
          >
            {t('migrationDialog.submit')}
          </ActionButton>
        </>
      }
    >
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
        <Typography variant="body2" color="text.secondary">
          {t('migrationDialog.description', { instanceKey: target.instance_key })}
        </Typography>

        <FilterSelect
          label={t('migrationDialog.sourceLabel')}
          value={sourceKey}
          options={sourceOptions}
          onChange={value => {
            setSourceKey(value)
            setConfirmKey('')
            setDeleteSshCredentials(true)
          }}
        />

        {sourceOptions.length === 0 && !instancesQuery.isLoading && (
          <Alert severity="info">{t('migrationDialog.noCandidates')}</Alert>
        )}

        {previewQuery.isLoading && Boolean(sourceKey) && (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
            <CircularProgress size={28} />
          </Box>
        )}

        {previewQuery.isError && (
          <Alert severity="error">{(previewQuery.error as Error).message}</Alert>
        )}

        {preview && (
          <>
            {preview.warnings.map(warning => (
              <Alert key={warning} severity="warning">
                {warning}
              </Alert>
            ))}

            <Alert severity={preview.channel_total > 0 ? 'warning' : 'info'} variant="outlined">
              {t('migrationDialog.summary', {
                channels: preview.channel_total,
                conflicts: preview.conflict_total,
              })}
            </Alert>

            {countEntries.length > 0 && (
              <Box>
                <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                  {t('migrationDialog.countsTitle')}
                </Typography>
                <Divider sx={{ mb: 1 }} />
                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
                  {countEntries.map(([resource, count]) => (
                    <Chip
                      key={resource}
                      size="small"
                      variant="outlined"
                      label={`${t(`migrationDialog.resource.${resource}`, {
                        defaultValue: resource,
                      })}: ${count}`}
                    />
                  ))}
                </Box>
              </Box>
            )}

            {preview.channels.length > 0 && (
              <Box>
                <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                  {t('migrationDialog.channelsTitle')}
                </Typography>
                <Box sx={{ maxHeight: 240, overflow: 'auto' }}>
                  <Table size="small" stickyHeader>
                    <TableHead>
                      <TableRow>
                        <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                          {t('migrationDialog.columns.channel')}
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>} align="right">
                          {t('migrationDialog.columns.messages')}
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                          {t('migrationDialog.columns.action')}
                        </TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {preview.channels.map(channel => (
                        <TableRow key={channel.channel_id} hover>
                          <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                            <Typography variant="body2" fontWeight={600}>
                              {channel.channel_name || channel.channel_id}
                            </Typography>
                            <Typography
                              variant="caption"
                              color="text.secondary"
                              sx={{ wordBreak: 'break-all' }}
                            >
                              {channel.source_chat_key} → {channel.target_chat_key}
                            </Typography>
                          </TableCell>
                          <TableCell
                            sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}
                            align="right"
                          >
                            {channel.message_count}
                          </TableCell>
                          <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                            <Chip
                              size="small"
                              color={channel.conflict ? 'warning' : 'default'}
                              variant={channel.conflict ? 'filled' : 'outlined'}
                              label={
                                channel.conflict
                                  ? t('migrationDialog.actionMerge')
                                  : t('migrationDialog.actionMove')
                              }
                            />
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </Box>
              </Box>
            )}

            {preview.residues.length > 0 && (
              <Box>
                <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                  {t('migrationDialog.residuesTitle')}
                </Typography>
                <Divider sx={{ mb: 1 }} />
                {preview.residues.map(residue => (
                  <Typography
                    key={residue.resource}
                    variant="caption"
                    color="text.secondary"
                    component="div"
                    sx={{ py: 0.25 }}
                  >
                    {t(`migrationDialog.resource.${residue.resource}`, {
                      defaultValue: residue.resource,
                    })}
                    {` × ${residue.count} — ${residue.reason}`}
                  </Typography>
                ))}
              </Box>
            )}

            {showSshCredentialChoice && (
              <Box>
                <FormControlLabel
                  control={
                    <Checkbox
                      checked={deleteSshCredentials}
                      disabled={migrateMutation.isPending}
                      onChange={event => setDeleteSshCredentials(event.target.checked)}
                    />
                  }
                  label={
                    <Typography variant="body2">
                      {t('migrationDialog.deleteSshCredentialsLabel', {
                        count: sshCredentialCount,
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

            {Boolean(preview?.cross_tenant) && (
              <CrossTenantSettingsChoice
                pluginSettings={preview?.plugin_settings ?? 0}
                configOverrides={preview?.config_overrides ?? 0}
                deletePluginSettings={deletePluginSettings}
                deleteConfigOverrides={deleteConfigOverrides}
                onDeletePluginSettingsChange={setDeletePluginSettings}
                onDeleteConfigOverridesChange={setDeleteConfigOverrides}
                disabled={migrateMutation.isPending}
              />
            )}

            <TextField
              size="small"
              fullWidth
              autoComplete="off"
              label={t('migrationDialog.confirmLabel')}
              placeholder={sourceKey}
              value={confirmKey}
              error={Boolean(confirmKey) && confirmKey.trim() !== sourceKey}
              helperText={
                confirmKey && confirmKey.trim() !== sourceKey
                  ? t('migrationDialog.confirmMismatch')
                  : t('migrationDialog.confirmHelper', { instanceKey: sourceKey })
              }
              onChange={event => setConfirmKey(event.target.value)}
            />
          </>
        )}
      </Box>
    </NekroDialog>
  )
}
