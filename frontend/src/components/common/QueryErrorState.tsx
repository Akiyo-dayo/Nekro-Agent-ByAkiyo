/**
 * 查询失败的统一呈现：「没有」与「不让看」在界面上必须能分辨
 *
 * 这个组件存在的理由是一条实测过的缺陷，不是抽象的整洁：总后台首页把
 * `GET /console/overview` 的 403 渲染成 **「实例总数 0」**，实例列表把 403 渲染成
 * 一张空表。**「零」是一个有效答案，用户会当真** —— 定制版客户拉第二个人进来看数据，
 * 看到的是「你什么都没有」而不是「你无权查看」，第一周就会以为系统坏了并开工单。
 *
 * 三条硬性约定：
 *
 * 1. **403 与其它失败分开说。** 403 是「你无权查看」，其余（500 / 网络 / 超时）是
 *    「加载失败」。判据是 HTTP 状态码而不是错误类名 —— console 全族守卫都是
 *    `require_role`，它抛的是原生 `HTTPException`，归一化之后 `type` 恒为
 *    `"HTTPException"`，403 与 502 在那个字段上一模一样（见 `ApiError.status`）。
 * 2. **403 不给重试按钮。** 重试一次仍然是 403，那是一个点了不会有任何变化的按钮
 *    —— 本项目在「点不动的按钮」上栽过两次，不要再摆第三个。
 * 3. **数字类的展示位在失败时不能落成 0。** 本组件只负责把失败说清楚；调用页要自己
 *    保证失败分支下不再渲染 `data?.x ?? 0` 那一排卡片（`?? 0` 正是缺陷的源头）。
 */

import { Box, SxProps, Theme, Typography } from '@mui/material'
import LockOutlinedIcon from '@mui/icons-material/LockOutlined'
import ErrorOutlineRoundedIcon from '@mui/icons-material/ErrorOutlineRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import { useTranslation } from 'react-i18next'
import ActionButton from './ActionButton'
import { isForbiddenError } from '../../services/api/axios'

export interface QueryErrorStateProps {
  /** react-query 给出的错误对象 */
  error: unknown
  /** 重试；**403 分支下刻意不使用它** */
  onRetry?: () => void
  /** 覆盖 403 分支的说明文案（例如某一页能给出更具体的下一步） */
  forbiddenHint?: string
  /** 紧凑形态：嵌在卡片/表格里时不撑高度 */
  compact?: boolean
  sx?: SxProps<Theme>
}

export default function QueryErrorState({
  error,
  onRetry,
  forbiddenHint,
  compact = false,
  sx,
}: QueryErrorStateProps) {
  const { t } = useTranslation('errors')
  const forbidden = isForbiddenError(error)
  const message = error instanceof Error ? error.message : String(error ?? '')

  return (
    <Box
      data-testid={forbidden ? 'query-error-forbidden' : 'query-error-failed'}
      sx={[
        {
          minHeight: compact ? 0 : 240,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          textAlign: 'center',
          gap: 1,
          p: compact ? 1.5 : 3,
        },
        ...(Array.isArray(sx) ? sx : [sx ?? {}]),
      ]}
    >
      {forbidden ? (
        <LockOutlinedIcon color="disabled" fontSize={compact ? 'medium' : 'large'} />
      ) : (
        <ErrorOutlineRoundedIcon color="error" fontSize={compact ? 'medium' : 'large'} />
      )}
      <Typography variant="subtitle1" fontWeight={700}>
        {forbidden ? t('accessDenied.title') : t('loadFailed.title')}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 460 }}>
        {forbidden ? (forbiddenHint ?? t('accessDenied.hint')) : message || t('serverError')}
      </Typography>
      {/* 403 下没有重试按钮：再点一次还是 403 */}
      {!forbidden && onRetry && (
        <ActionButton
          tone="secondary"
          size="small"
          startIcon={<RefreshRoundedIcon />}
          onClick={onRetry}
        >
          {t('loadFailed.retry')}
        </ActionButton>
      )}
    </Box>
  )
}
