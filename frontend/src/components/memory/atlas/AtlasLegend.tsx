import { useEffect, useState } from 'react'
import { Box, ButtonBase, Stack, Tooltip, Typography } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useTranslation } from 'react-i18next'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import LegendToggleRoundedIcon from '@mui/icons-material/LegendToggleRounded'
import IconActionButton from '../../common/IconActionButton'
import { ATLAS_OVERLAY } from '../../../theme/variants'

/**
 * 图谱图例（画布左上）
 *
 * 每簇一行「色点 + 簇名 + 显示 n / 共 m」：被密度档位截掉了（n < m）的那行数字用警告色、行尾注「按重要度」，
 * 这就是档位的反馈。点一行只看这一簇，再点恢复——这是图上唯一的簇筛选入口。下面列当前图里出现过的线型。
 * 可以收成一颗按钮；窄屏默认收起。
 */

export interface AtlasLegendCluster {
  key: string
  label: string
  color: string
  /** 还在加载时为 null，显示「—」 */
  shown: number | null
  total: number | null
}

export interface AtlasLegendEdge {
  key: string
  label: string
  dash?: string
  /** 缺省次要文字色 */
  color?: string
}

export interface AtlasLegendProps {
  clusters: AtlasLegendCluster[]
  edges: AtlasLegendEdge[]
  focusCluster: string | null
  onFocusCluster: (cluster: string | null) => void
  /** 顶部一行警告小字（数据很多、只画了一部分时） */
  notice?: string | null
  defaultCollapsed?: boolean
}

export default function AtlasLegend({
  clusters,
  edges,
  focusCluster,
  onFocusCluster,
  notice,
  defaultCollapsed = false,
}: AtlasLegendProps) {
  const theme = useTheme()
  const { t } = useTranslation('atlas')
  const [collapsed, setCollapsed] = useState(defaultCollapsed)

  // 从宽屏缩到窄屏时跟着收起；反过来不自动展开，免得盖住用户正在看的地方
  useEffect(() => {
    if (defaultCollapsed) setCollapsed(true)
  }, [defaultCollapsed])

  const position = {
    position: 'absolute',
    left: { xs: 10, sm: 16 },
    top: { xs: 10, sm: 16 },
    zIndex: 5,
  } as const

  if (collapsed) {
    return (
      <Box sx={{ ...position }} onPointerDown={event => event.stopPropagation()}>
        <Tooltip title={t('legend.show')} placement="right">
          <IconActionButton
            size="small"
            aria-label={t('legend.show')}
            onClick={() => setCollapsed(false)}
            sx={{ ...(ATLAS_OVERLAY.styles as object), width: 32, height: 32 }}
          >
            <LegendToggleRoundedIcon fontSize="small" />
          </IconActionButton>
        </Tooltip>
      </Box>
    )
  }

  return (
    <Box
      sx={{
        ...(ATLAS_OVERLAY.styles as object),
        ...position,
        width: 264,
        maxWidth: 'calc(100% - 20px)',
        p: 1,
      }}
      onPointerDown={event => event.stopPropagation()}
    >
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 0.5, pl: 0.5 }}>
        <Typography variant="overline" sx={{ lineHeight: 1.6, color: 'text.secondary', fontWeight: 700 }}>
          {t('legend.title')}
        </Typography>
        <IconActionButton size="small" aria-label={t('legend.hide')} title={t('legend.hide')} onClick={() => setCollapsed(true)}>
          <CloseRoundedIcon sx={{ fontSize: 16 }} />
        </IconActionButton>
      </Stack>

      {notice && (
        <Typography variant="caption" sx={{ display: 'block', px: 0.5, mb: 0.5, color: theme.palette.warning.main, lineHeight: 1.4 }}>
          {notice}
        </Typography>
      )}

      <Stack spacing={0.25}>
        {clusters.map(cluster => {
          const active = focusCluster === cluster.key
          const dimmed = focusCluster !== null && !active
          const truncated = cluster.shown !== null && cluster.total !== null && cluster.shown < cluster.total
          return (
            <Tooltip key={cluster.key} title={active ? t('legend.showAll') : t('legend.onlyThis')} placement="right">
              <ButtonBase
                onClick={() => onFocusCluster(active ? null : cluster.key)}
                aria-pressed={active}
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 0.75,
                  width: '100%',
                  px: 0.5,
                  py: 0.4,
                  borderRadius: 1,
                  textAlign: 'left',
                  opacity: dimmed ? 0.5 : 1,
                  bgcolor: active ? alpha(theme.palette.primary.main, 0.08) : 'transparent',
                  transition: 'background-color 0.14s ease, opacity 0.14s ease',
                  '&:hover': { bgcolor: alpha(theme.palette.primary.main, active ? 0.12 : 0.05), opacity: 1 },
                }}
              >
                <Box sx={{ width: 10, height: 10, borderRadius: '50%', bgcolor: cluster.color, flexShrink: 0 }} />
                <Typography variant="caption" noWrap sx={{ flex: 1, minWidth: 0, fontWeight: 600, color: 'text.primary' }}>
                  {cluster.label}
                </Typography>
                <Typography
                  variant="caption"
                  noWrap
                  sx={{
                    fontVariantNumeric: 'tabular-nums',
                    color: truncated ? theme.palette.warning.main : 'text.secondary',
                    fontWeight: truncated ? 700 : 400,
                  }}
                >
                  {cluster.shown === null || cluster.total === null
                    ? t('legend.countPending')
                    : t('legend.count', { shown: cluster.shown.toLocaleString(), total: cluster.total.toLocaleString() })}
                </Typography>
                {truncated && (
                  <Typography variant="caption" noWrap sx={{ fontSize: '0.62rem', color: 'text.disabled' }}>
                    {t('legend.byImportance')}
                  </Typography>
                )}
              </ButtonBase>
            </Tooltip>
          )
        })}
      </Stack>

      {edges.length > 0 && (
        <Box sx={{ mt: 0.75, pt: 0.75, px: 0.5, borderTop: `1px solid ${alpha(theme.palette.text.secondary, 0.12)}` }}>
          <Typography variant="caption" sx={{ display: 'block', color: 'text.disabled', fontSize: '0.62rem', mb: 0.25 }}>
            {t('legend.edges')}
          </Typography>
          <Stack spacing={0.25}>
            {edges.map(edge => (
              <Stack key={edge.key} direction="row" spacing={0.75} alignItems="center">
                <svg width={24} height={8} aria-hidden style={{ flexShrink: 0 }}>
                  <line
                    x1={1}
                    y1={4}
                    x2={23}
                    y2={4}
                    stroke={edge.color ?? theme.palette.text.secondary}
                    strokeWidth={1.6}
                    strokeDasharray={edge.dash}
                    strokeLinecap="round"
                  />
                </svg>
                <Typography variant="caption" noWrap sx={{ color: 'text.secondary', fontSize: '0.68rem' }}>
                  {edge.label}
                </Typography>
              </Stack>
            ))}
          </Stack>
        </Box>
      )}
    </Box>
  )
}
