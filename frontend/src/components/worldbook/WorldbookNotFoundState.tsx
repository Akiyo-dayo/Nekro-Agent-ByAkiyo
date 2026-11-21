import { Box, Typography } from '@mui/material'
import SearchOffIcon from '@mui/icons-material/SearchOff'
import { useTranslation } from 'react-i18next'

/**
 * 世界书接口 404 的统一说法
 *
 * 后端刻意不区分「这本书 / 这条条目不存在」与「它不在你能管理的实例里」，
 * 所以这里也只给一句合并后的说明。
 */
export default function WorldbookNotFoundState({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation('worldbook')

  return (
    <Box
      sx={{
        minHeight: compact ? 0 : 240,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        textAlign: 'center',
        gap: 1,
        p: compact ? 1.5 : 3,
      }}
    >
      <SearchOffIcon color="disabled" fontSize={compact ? 'medium' : 'large'} />
      <Typography variant="subtitle1" fontWeight={700}>
        {t('errors.notFoundTitle')}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 460 }}>
        {t('errors.notFoundHint')}
      </Typography>
    </Box>
  )
}
