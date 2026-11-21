import { Box, Chip, Typography, useTheme } from '@mui/material'
import type { Theme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { CHIP_VARIANTS } from '../../theme/variants'
import { formatDateTime } from './format'
import {
  hasStateItems,
  readLastUpdate,
  readState,
  readStateBoolean,
  readStateNumber,
  readTrimmed,
  type StateItems,
} from './stateItems'

/**
 * 提示词明细里的「角色状态」一节
 *
 * 只显示后端真的记了的键：老行缺哪些键就不画哪几行，摆一排「—」只是噪音。
 * 这里没有任何状态值，只有计数与名字 —— 状态树的内容在角色状态页看。
 */

function stateColor(theme: Theme, state: string): string {
  if (state === 'injected') return theme.palette.success.main
  if (state === 'error') return theme.palette.error.main
  if (state === 'paused' || state === 'suppressed') return theme.palette.warning.main
  return theme.palette.text.secondary
}

function Line({ label, value }: { label: string; value: string }) {
  return (
    <Typography variant="body2" color="text.secondary">
      {label}：{value}
    </Typography>
  )
}

export default function PromptRoundStateItems({ items }: { items: StateItems | null | undefined }) {
  const theme = useTheme()
  const { t } = useTranslation('state')

  if (!hasStateItems(items)) return null
  const raw = items as StateItems

  const state = readState(raw)
  const tokens = readStateNumber(raw, 'tokens')
  const chars = readStateNumber(raw, 'chars')
  const budget = readStateNumber(raw, 'budget')
  const position = typeof raw.position === 'string' ? raw.position : ''
  const people = readStateNumber(raw, 'people')
  const privateOmitted = readStateNumber(raw, 'private_omitted')
  const trimmed = readTrimmed(raw)
  const paused = readStateBoolean(raw, 'paused')
  const provider = readStateBoolean(raw, 'provider')
  const lastUpdate = readLastUpdate(raw)

  const tone = stateColor(theme, state)

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
      <Typography variant="subtitle2" fontWeight={700}>
        {t('promptRound.title')}
      </Typography>

      {state && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Typography variant="body2" color="text.secondary">
            {t('promptRound.status')}：
          </Typography>
          <Chip
            size="small"
            label={t(`enums.state.${state}`, { defaultValue: state })}
            sx={CHIP_VARIANTS.getCustomColorChip(tone, true)}
          />
        </Box>
      )}

      {tokens !== null && <Line label={t('promptRound.tokens')} value={String(tokens)} />}
      {chars !== null && <Line label={t('promptRound.chars')} value={String(chars)} />}
      {budget !== null && <Line label={t('promptRound.budget')} value={String(budget)} />}
      {position && <Line label={t('promptRound.position')} value={position} />}
      {people !== null && <Line label={t('promptRound.people')} value={String(people)} />}
      {privateOmitted !== null && (
        <Line label={t('promptRound.privateOmitted')} value={String(privateOmitted)} />
      )}
      {trimmed.length > 0 && (
        // 组名是模板里写进来的文字，按纯文本拼接
        <Line label={t('promptRound.trimmed')} value={trimmed.join('、')} />
      )}
      {paused !== null && (
        <Line label={t('promptRound.paused')} value={paused ? t('promptRound.yes') : t('promptRound.no')} />
      )}
      {provider !== null && (
        <Line
          label={t('promptRound.provider')}
          value={provider ? t('promptRound.yes') : t('promptRound.no')}
        />
      )}

      {lastUpdate && (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25 }}>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            {t('promptRound.lastUpdate')}
          </Typography>
          {lastUpdate.at && <Line label={t('promptRound.at')} value={formatDateTime(lastUpdate.at)} />}
          {lastUpdate.result && (
            <Line
              label={t('promptRound.result')}
              value={t(`enums.result.${lastUpdate.result}`, { defaultValue: lastUpdate.result })}
            />
          )}
          {lastUpdate.applied !== null && (
            <Line label={t('promptRound.applied')} value={String(lastUpdate.applied)} />
          )}
          {lastUpdate.rejected !== null && (
            <Line label={t('promptRound.rejected')} value={String(lastUpdate.rejected)} />
          )}
          {lastUpdate.source && (
            <Line
              label={t('promptRound.source')}
              value={t(`enums.source.${lastUpdate.source}`, { defaultValue: lastUpdate.source })}
            />
          )}
          {lastUpdate.step && (
            // `skipped:inline` 这类取值：只把前缀翻出来，后面的原因原样显示
            <Line
              label={t('promptRound.step')}
              value={lastUpdate.step.replace(/^skipped:/, `${t('promptRound.stepSkipped')} `)}
            />
          )}
        </Box>
      )}
    </Box>
  )
}
