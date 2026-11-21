import { Chip, useTheme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { CHIP_VARIANTS } from '../../theme/variants'

/**
 * 摘要版本的来源
 *
 * 三种来源的颜色在这里定一次：列表的最新版、历史版本、回滚说明都用同一个组件，
 * 免得同一个「自动」在两处长得不一样。未知来源原样显示，不把 i18n 键漏到界面上。
 */

export default function SummarySourceChip({ source }: { source: string }) {
  const theme = useTheme()
  const { t } = useTranslation('memory')

  const color =
    source === 'auto'
      ? theme.palette.success.main
      : source === 'manual'
        ? theme.palette.primary.main
        : source === 'rollback'
          ? theme.palette.info.main
          : theme.palette.text.secondary

  return (
    <Chip
      size="small"
      label={t(`summaries.source.${source}`, { defaultValue: source })}
      sx={CHIP_VARIANTS.getCustomColorChip(color, true)}
    />
  )
}
