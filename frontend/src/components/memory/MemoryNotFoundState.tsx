import { Box, Typography } from '@mui/material'
import SearchOffIcon from '@mui/icons-material/SearchOff'
import { useTranslation } from 'react-i18next'

/**
 * 记忆接口 404 的统一说法
 *
 * 后端刻意不区分「没有这条记录」与「它不在你能管理的实例里」—— 分开说等于把
 * 「这个 ID 存在」变回一条可枚举的信息。所以这里也只给一句合并后的说明。
 */
export default function MemoryNotFoundState({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation('memory')

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
