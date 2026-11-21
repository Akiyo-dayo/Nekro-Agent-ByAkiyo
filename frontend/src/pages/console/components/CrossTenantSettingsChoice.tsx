import { Box, Checkbox, FormControlLabel, Typography } from '@mui/material'
import { useTranslation } from 'react-i18next'

interface CrossTenantSettingsChoiceProps {
  /** 源实例名下有设置的插件个数（预览给出） */
  pluginSettings: number
  /** 源实例名下有生效覆盖的配置文件个数（预览给出） */
  configOverrides: number
  deletePluginSettings: boolean
  deleteConfigOverrides: boolean
  onDeletePluginSettingsChange: (value: boolean) => void
  onDeleteConfigOverridesChange: (value: boolean) => void
  disabled?: boolean
}

/**
 * 跨租户迁移 / 并入时「源实例的插件设置与配置覆盖要不要一并删除」两个勾选框
 *
 * 只在调用方判定跨租户时渲染；某一样的个数为 0 时不问那一句（请求体仍按默认删除发送）。
 * 历史迁移对话框与待认领协议端的并入对话框共用这一份，两处的说法与默认值不会分叉。
 */
export default function CrossTenantSettingsChoice({
  pluginSettings,
  configOverrides,
  deletePluginSettings,
  deleteConfigOverrides,
  onDeletePluginSettingsChange,
  onDeleteConfigOverridesChange,
  disabled = false,
}: CrossTenantSettingsChoiceProps) {
  const { t } = useTranslation('console')

  return (
    <>
      {pluginSettings > 0 && (
        <Box>
          <FormControlLabel
            control={
              <Checkbox
                checked={deletePluginSettings}
                disabled={disabled}
                onChange={event => onDeletePluginSettingsChange(event.target.checked)}
              />
            }
            label={
              <Typography variant="body2">
                {t('migrationDialog.deletePluginSettingsLabel', { count: pluginSettings })}
              </Typography>
            }
          />
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', ml: 4 }}>
            {t('migrationDialog.deletePluginSettingsHint')}
          </Typography>
        </Box>
      )}
      {configOverrides > 0 && (
        <Box>
          <FormControlLabel
            control={
              <Checkbox
                checked={deleteConfigOverrides}
                disabled={disabled}
                onChange={event => onDeleteConfigOverridesChange(event.target.checked)}
              />
            }
            label={
              <Typography variant="body2">
                {t('migrationDialog.deleteConfigOverridesLabel', { count: configOverrides })}
              </Typography>
            }
          />
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', ml: 4 }}>
            {t('migrationDialog.deleteConfigOverridesHint')}
          </Typography>
        </Box>
      )}
    </>
  )
}
