import { Stack } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useTranslation } from 'react-i18next'
import VisibilityOffOutlinedIcon from '@mui/icons-material/VisibilityOffOutlined'
import ActionButton from '../../common/ActionButton'
import SegmentedControl from '../../common/SegmentedControl'
import type { AtlasViewMode, InformationDensity } from './types'

/**
 * 图谱工具条：视图 · 密度 ·（显示失活）
 *
 * 放在画布外、画布正上方，与页面筛选同一行；缩放不在这里，在画布右下角（`AtlasZoomControl`）。
 * 记忆图与好感图共用：视图名可以换（好感图叫星形 / 分区），不需要的「显示失活」不传就不画。
 */

export interface AtlasToolbarProps {
  viewMode: AtlasViewMode
  onViewModeChange: (value: AtlasViewMode) => void
  /** 两种视图的名字；缺省「事件 / 原子」 */
  viewLabels?: { episode: string; atomic: string }
  density: InformationDensity
  onDensityChange: (value: InformationDensity) => void
  /** 「显示失活」开关；不传就不显示 */
  showInactive?: { value: boolean; onChange: (value: boolean) => void }
  /**
   * 是否显示「事件 / 原子」视图切换；缺省显示
   *
   * 世界书图谱只有一种视图，但工具条仍要复用密度与显示失活，于是关掉这一段。
   */
  showViewSwitch?: boolean
}

export default function AtlasToolbar({
  viewMode,
  onViewModeChange,
  viewLabels,
  density,
  onDensityChange,
  showInactive,
  showViewSwitch = true,
}: AtlasToolbarProps) {
  const theme = useTheme()
  const { t } = useTranslation('atlas')

  return (
    <Stack direction="row" spacing={1} alignItems="center" useFlexGap sx={{ flexWrap: 'wrap', rowGap: 1 }}>
      {showViewSwitch && (
        <SegmentedControl<AtlasViewMode>
          value={viewMode}
          onChange={onViewModeChange}
          options={[
            { value: 'episode', label: viewLabels?.episode ?? t('toolbar.viewEpisode') },
            { value: 'atomic', label: viewLabels?.atomic ?? t('toolbar.viewAtomic') },
          ]}
        />
      )}
      <SegmentedControl<InformationDensity>
        value={density}
        onChange={onDensityChange}
        options={[
          { value: 'low', label: t('toolbar.densityLow'), ariaLabel: `${t('toolbar.density')} ${t('toolbar.densityLow')}` },
          {
            value: 'medium',
            label: t('toolbar.densityMedium'),
            ariaLabel: `${t('toolbar.density')} ${t('toolbar.densityMedium')}`,
          },
          { value: 'high', label: t('toolbar.densityHigh'), ariaLabel: `${t('toolbar.density')} ${t('toolbar.densityHigh')}` },
        ]}
      />
      {showInactive && (
        <ActionButton
          tone="ghost"
          size="small"
          aria-pressed={showInactive.value}
          startIcon={<VisibilityOffOutlinedIcon fontSize="small" />}
          onClick={() => showInactive.onChange(!showInactive.value)}
          sx={
            showInactive.value
              ? {
                  color: theme.palette.primary.main,
                  backgroundColor: alpha(theme.palette.primary.main, 0.1),
                  '&:hover': { backgroundColor: alpha(theme.palette.primary.main, 0.14) },
                }
              : { color: theme.palette.text.secondary }
          }
        >
          {t('toolbar.showInactive')}
        </ActionButton>
      )}
    </Stack>
  )
}
