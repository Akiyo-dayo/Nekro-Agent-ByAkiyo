/**
 * 总后台 → 模型组
 *
 * 与「系统配置 → 模型管理 → 基础模型组」共用 `components/model-group/ModelGroupManager`，
 * 不存在第二套列表或第二套新建表单。平台提供的模型组租户可以直接用，但永远读不到它的
 * API Key 与接口地址；租户自建的模型组接的是客户自己的 API，凭据归客户自己维护。
 * 可编辑性与凭据可见性一律以后端返回的 `editable` / `credential_visible` 为准。
 */

import { Box, Typography } from '@mui/material'
import { useTranslation } from 'react-i18next'
import ModelGroupManager from '../../components/model-group/ModelGroupManager'

export default function ModelGroupOwnershipPage() {
  const { t } = useTranslation('tenant')

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 3 },
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 1.5,
        overflow: 'hidden',
        boxSizing: 'border-box',
      }}
    >
      <Box sx={{ flexShrink: 0 }}>
        <Typography variant="h6" fontWeight={700}>
          {t('modelGroups.title')}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {t('modelGroups.subtitle')}
        </Typography>
      </Box>
      <Box sx={{ flex: 1, minHeight: 0 }}>
        <ModelGroupManager />
      </Box>
    </Box>
  )
}
