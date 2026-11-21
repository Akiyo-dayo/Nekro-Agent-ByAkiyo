import { Box, Stack, Tooltip, Typography } from '@mui/material'
import { useTranslation } from 'react-i18next'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import FitScreenRoundedIcon from '@mui/icons-material/FitScreenRounded'
import RemoveRoundedIcon from '@mui/icons-material/RemoveRounded'
import IconActionButton from '../../common/IconActionButton'
import { ATLAS_OVERLAY } from '../../../theme/variants'
import { ATLAS_MAX_ZOOM, ATLAS_MIN_ZOOM, type AtlasViewport } from './useAtlasViewport'

/**
 * 画布右下角的缩放浮层：− / 百分比 / + / 适配
 *
 * 由画布所在的容器摆放（父元素要 `position: relative`）。
 */
export default function AtlasZoomControl({ viewport }: { viewport: AtlasViewport }) {
  const { t } = useTranslation('atlas')
  const { zoom, updateZoom, resetView } = viewport

  return (
    <Box
      sx={{
        ...(ATLAS_OVERLAY.styles as object),
        position: 'absolute',
        right: { xs: 10, sm: 16 },
        bottom: { xs: 10, sm: 16 },
        zIndex: 5,
        px: 0.5,
        py: 0.25,
      }}
      onPointerDown={event => event.stopPropagation()}
    >
      <Stack direction="row" spacing={0.25} alignItems="center">
        <Tooltip title={t('zoom.zoomOut')}>
          <span>
            <IconActionButton
              size="small"
              aria-label={t('zoom.zoomOut')}
              disabled={zoom <= ATLAS_MIN_ZOOM}
              onClick={() => updateZoom(zoom - 0.1)}
            >
              <RemoveRoundedIcon fontSize="small" />
            </IconActionButton>
          </span>
        </Tooltip>
        <Tooltip title={t('zoom.hint')}>
          <Typography
            variant="caption"
            sx={{ minWidth: 40, textAlign: 'center', fontVariantNumeric: 'tabular-nums', color: 'text.secondary', cursor: 'default' }}
          >
            {Math.round(zoom * 100)}%
          </Typography>
        </Tooltip>
        <Tooltip title={t('zoom.zoomIn')}>
          <span>
            <IconActionButton
              size="small"
              aria-label={t('zoom.zoomIn')}
              disabled={zoom >= ATLAS_MAX_ZOOM}
              onClick={() => updateZoom(zoom + 0.1)}
            >
              <AddRoundedIcon fontSize="small" />
            </IconActionButton>
          </span>
        </Tooltip>
        <Tooltip title={t('zoom.fit')}>
          <span>
            <IconActionButton size="small" aria-label={t('zoom.fit')} onClick={resetView}>
              <FitScreenRoundedIcon fontSize="small" />
            </IconActionButton>
          </span>
        </Tooltip>
      </Stack>
    </Box>
  )
}
