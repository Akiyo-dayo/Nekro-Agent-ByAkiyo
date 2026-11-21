/**
 * 频道详情顶部的「今日额度」一行
 *
 * 这是**辅助信息**，不是这一页的主体：功能没开、无权查看（403）、频道不属于当前身份（404）
 * 或后端出错时，整行不渲染。这里刻意不做错误提示——把一条辅助信息渲染成红色报错条，
 * 会让只读成员在打开频道详情时先看到一条他什么也做不了的错误。
 *
 * 用量与列表页同源（后端同一个模型），所以这里只做展示，不在前端复算百分比。
 */

import { Box, Chip, LinearProgress, Typography, useTheme } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { CHIP_VARIANTS } from '../../../../theme/variants'
import {
  CONSOLE_CHANNEL_QUOTA_QUERY_KEYS,
  consoleChannelQuotaApi,
} from '../../../../services/api/consoleChannelQuota'
import {
  channelQuotaProgressColor,
  channelQuotaStateColor,
  channelQuotaUnitLabel,
  formatQuotaInt,
  formatQuotaLimit,
} from '../../../console/components/channelQuotaUi'

export default function ChannelQuotaStatusRow({ chatKey }: { chatKey: string }) {
  const theme = useTheme()
  const { t } = useTranslation('console-channel-quota')

  const statusQuery = useQuery({
    queryKey: CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.status(chatKey),
    queryFn: () => consoleChannelQuotaApi.getStatus(chatKey),
    enabled: Boolean(chatKey),
    staleTime: 30_000,
    retry: false,
  })

  const status = statusQuery.data
  // 失败一律静默：403 / 404 / 5xx 都不该在这条辅助信息上冒出一个报错条
  if (statusQuery.isError || !status || status.state === 'disabled') {
    return null
  }

  const unit = channelQuotaUnitLabel(t, status.unit)

  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'center',
        gap: 1.5,
        flexWrap: 'wrap',
        px: 2,
        py: 1,
        borderRadius: 1,
        border: '1px solid',
        borderColor: 'divider',
      }}
    >
      <Typography variant="body2" fontWeight={600}>
        {status.limit > 0
          ? t('statusRow.label', {
              used: formatQuotaInt(status.used),
              limit: formatQuotaLimit(t, status.limit),
              unit,
            })
          : t('statusRow.unlimitedLabel', { used: formatQuotaInt(status.used) })}
      </Typography>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, minWidth: 160, flex: 1 }}>
        <LinearProgress
          variant="determinate"
          value={Math.min(status.percent, 100)}
          color={channelQuotaProgressColor(status.state)}
          sx={{ flex: 1, height: 6, borderRadius: 999 }}
        />
        <Typography variant="caption" color="text.secondary">
          {status.percent}%
        </Typography>
      </Box>
      <Chip
        size="small"
        label={t(`state.${status.state}`)}
        sx={CHIP_VARIANTS.getCustomColorChip(channelQuotaStateColor(theme, status.state), true)}
      />
    </Box>
  )
}
