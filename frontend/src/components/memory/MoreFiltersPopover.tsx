import type { ReactNode } from 'react'
import { Box, Popover, Stack } from '@mui/material'
import { useTranslation } from 'react-i18next'
import ClearAllIcon from '@mui/icons-material/ClearAll'
import ActionButton from '../common/ActionButton'

/**
 * 「更多筛选」浮层
 *
 * 主行只留最常用的搜索、频道、人与状态；其余条件按两列网格收在这里，底部固定
 * 「清空」与「完成」。清空只清这里的条件，主行的关键词与选择不受影响。
 */

interface MoreFiltersPopoverProps {
  open: boolean
  anchorEl: HTMLElement | null
  onClose: () => void
  onClear: () => void
  children: ReactNode
}

export default function MoreFiltersPopover({
  open,
  anchorEl,
  onClose,
  onClear,
  children,
}: MoreFiltersPopoverProps) {
  const { t } = useTranslation('memory')

  return (
    <Popover
      open={open}
      anchorEl={anchorEl}
      onClose={onClose}
      anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
      transformOrigin={{ vertical: 'top', horizontal: 'left' }}
    >
      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          gap: 1.5,
          width: { xs: 'min(92vw, 340px)', sm: 540 },
          maxWidth: '100%',
          p: 1.5,
        }}
      >
        <Box
          sx={{
            display: 'grid',
            gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))' },
            gap: 1.5,
            alignItems: 'center',
          }}
        >
          {children}
        </Box>
        <Stack direction="row" spacing={1} justifyContent="flex-end">
          <ActionButton tone="ghost" size="small" startIcon={<ClearAllIcon />} onClick={onClear}>
            {t('filters.clear')}
          </ActionButton>
          <ActionButton tone="secondary" size="small" onClick={onClose}>
            {t('filters.done')}
          </ActionButton>
        </Stack>
      </Box>
    </Popover>
  )
}
