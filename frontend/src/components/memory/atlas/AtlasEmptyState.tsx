import type { ReactNode } from 'react'
import { Box, Typography } from '@mui/material'
import { CARD_VARIANTS } from '../../../theme/variants'

/**
 * 画布中央的空态 / 错误小卡：一行标题、一行原因，可选一个动作
 *
 * 不用 `Alert`：空不是警告，是「换个范围看看」。
 */
export default function AtlasEmptyState({
  title,
  description,
  action,
  children,
}: {
  title?: string
  description?: string
  action?: ReactNode
  /** 错误态时放 `QueryErrorState` 之类，替代标题与说明 */
  children?: ReactNode
}) {
  return (
    <Box
      sx={{
        ...(CARD_VARIANTS.default.styles as object),
        maxWidth: 360,
        width: '100%',
        px: 2.5,
        py: 2,
        textAlign: 'center',
      }}
      onPointerDown={event => event.stopPropagation()}
    >
      {children ?? (
        <>
          {title && (
            <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>
              {title}
            </Typography>
          )}
          {description && (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              {description}
            </Typography>
          )}
          {action && <Box sx={{ mt: 1.5 }}>{action}</Box>}
        </>
      )}
    </Box>
  )
}
