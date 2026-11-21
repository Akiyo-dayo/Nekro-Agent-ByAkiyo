import {
  Box,
  Chip,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
  useTheme,
  type SxProps,
  type Theme,
} from '@mui/material'
import { useTranslation } from 'react-i18next'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  hasTavernItems,
  layerText,
  readDepthChars,
  readGate,
  readGreeting,
  readMacros,
  readSlot,
  SLOT_KEYS,
  SLOT_LABEL_KEYS,
  stateText,
} from './tavernItems'

/**
 * 提示词明细里的「人设卡与槽位」一节
 *
 * 三行槽位共用一张小表：状态、字数、来自哪几层。状态取的是**发送之后**的结果
 * （`sent` / `pending` / `gated` / `unsupported` / `retried_without`），不是解析时的
 * 初始值，因此这一节能回答「预填到底发没发」。
 *
 * `tavern_items` 为空表示这一轮没有记录，整块不显示；这里的「空」与「记了但槽位
 * 都是空」是两件事，后者仍要显示，否则管理员分不清是没记录还是记了全是空。
 */

function StateChip({ state }: { state: string }) {
  const theme = useTheme()
  const { t } = useTranslation('tavern')

  const color =
    state === 'sent'
      ? theme.palette.success.main
      : state === 'retried_without' || state === 'pending'
        ? theme.palette.warning.main
        : state === 'unsupported'
          ? theme.palette.warning.main
          : state === 'gated'
            ? theme.palette.text.secondary
            : theme.palette.text.disabled

  return (
    <Chip
      size="small"
      label={stateText(state, t)}
      sx={CHIP_VARIANTS.getCustomColorChip(color, true)}
    />
  )
}

export default function PromptRoundTavernItems({
  items,
}: {
  items: Record<string, unknown> | null | undefined
}) {
  const { t } = useTranslation('tavern')

  if (!hasTavernItems(items)) return null

  const gate = readGate(items)
  const depthChars = readDepthChars(items)
  const macros = readMacros(items)
  const greeted = readGreeting(items)

  return (
    <Box>
      <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
        {t('rounds.title')}
      </Typography>
      <Box sx={{ overflowX: 'auto' }}>
        <Table size="small" sx={{ minWidth: 420 }}>
          <TableHead>
            <TableRow>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('rounds.table.slot')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('rounds.table.status')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('rounds.table.chars')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('rounds.table.layers')}
              </TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {SLOT_KEYS.map(key => {
              const slot = readSlot(items, key)
              return (
                <TableRow key={key}>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                    <Typography variant="body2">{t(SLOT_LABEL_KEYS[key])}</Typography>
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                    {slot && slot.state ? <StateChip state={slot.state} /> : '—'}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                    <Typography variant="body2">{slot?.chars ?? 0}</Typography>
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                    <Typography variant="body2" color="text.secondary">
                      {slot && slot.layers.length > 0
                        ? slot.layers.map(layer => layerText(layer, t)).join(' / ')
                        : '—'}
                    </Typography>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </Box>

      <Box sx={{ mt: 1.5, display: 'flex', flexDirection: 'column', gap: 0.5 }}>
        <Typography variant="body2" color="text.secondary">
          {t('rounds.gate')}：
          {gate === null ? '—' : gate ? t('rounds.gateOn') : t('rounds.gateOff')}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {t('rounds.depth', { chars: depthChars })}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {t('rounds.greeting')}：{greeted ? t('rounds.greetingYes') : t('rounds.greetingNo')}
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {t('rounds.macros', { count: macros.expanded })}
          {macros.guarded ? ` · ${t('rounds.macrosGuarded')}` : ''}
        </Typography>
      </Box>
    </Box>
  )
}
