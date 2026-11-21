import { Alert, Box, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { unifiedConfigApi } from '../../services/api/unified-config'
import { computeWorldbookBudget } from './budget'

/**
 * 世界书预算的实时换算
 *
 * 取这一层生效的模型组与分词系数，但百分比和上限用**界面上正在编辑的值**：
 * 拖滑块的时候就该看到数字跟着动，而不是保存之后才知道结果。
 * 接口失败时整块不渲染，不弹错——它只是说明文字，不该挡住配置页。
 */
export default function WorldbookBudgetPreview({
  configKey,
  percent,
  capTokens,
}: {
  configKey: string
  percent: number
  capTokens: number
}) {
  const { t } = useTranslation('common')
  const { data } = useQuery({
    queryKey: ['worldbook-budget-basis', configKey],
    queryFn: () => unifiedConfigApi.getWorldbookBudgetBasis(configKey),
    retry: false,
  })

  if (!data) {
    return null
  }

  const budget = computeWorldbookBudget(
    percent,
    capTokens,
    data.context_length,
    data.output_reserve
  )
  const approxChars =
    data.tokens_per_char > 0 ? Math.round(budget / data.tokens_per_char) : 0

  const contextNote = data.context_length_set
    ? ''
    : t('worldbookBudget.contextUnset', { defaultContext: data.default_context_length })

  const inheritedPercent = data.inherited_percent
  const inheritedCap = data.inherited_cap_tokens

  return (
    <Box>
      <Typography variant="body2" color="text.secondary">
        {t('worldbookBudget.preview', {
          modelGroup: data.model_group,
          contextLength: data.context_length,
          contextNote,
          budget,
          approx: approxChars,
        })}
      </Typography>
      {data.estimator === 'chars' && (
        <Typography variant="caption" color="text.secondary" display="block">
          {t('worldbookBudget.charsEstimator')}
        </Typography>
      )}
      {typeof inheritedPercent === 'number' && (
        <Typography variant="caption" color="text.secondary" display="block">
          {t('worldbookBudget.inheritedPercent', { percent: inheritedPercent })}
        </Typography>
      )}
      {typeof inheritedCap === 'number' &&
        (inheritedCap > 0 ? (
          <Typography variant="caption" color="text.secondary" display="block">
            {t('worldbookBudget.inheritedCap', { cap: inheritedCap })}
          </Typography>
        ) : (
          <Typography variant="caption" color="text.secondary" display="block">
            {t('worldbookBudget.inheritedUnlimited')}
          </Typography>
        ))}
      {data.fallback_context_length > 0 && (
        <Alert severity="info" sx={{ mt: 1 }}>
          {t('worldbookBudget.fallback', {
            modelGroup: data.fallback_model_group,
            contextLength: data.fallback_context_length,
          })}
        </Alert>
      )}
    </Box>
  )
}
