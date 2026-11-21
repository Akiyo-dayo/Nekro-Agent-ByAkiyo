import { Alert, Box, Typography } from '@mui/material'
import { useTranslation } from 'react-i18next'
import type { WorldbookBudgetInfo } from '../../services/api/worldbook'
import { WORLDBOOK_HARD_CAP_TOKENS } from './budget'

/**
 * 世界书预算换算的一句说明
 *
 * 触发测试与提示词明细共用：同一个后端函数算出这一轮的预算，两处不能各写一套句子。
 * `totalTokens` 只有触发测试有（明细里没有「入选合计」这个数）。
 */
export default function BudgetExplain({
  budget,
  totalTokens,
}: {
  budget: WorldbookBudgetInfo
  totalTokens?: number
}) {
  const { t } = useTranslation('worldbook')

  // 上限 0 是「不另设上限」，预算仍不超过硬上限；上限本身最大就是硬上限，所以两种情况都只写一个数
  const capText =
    budget.cap_tokens > 0
      ? t('budgetExplain.cap', { cap: budget.cap_tokens })
      : t('budgetExplain.hardCap', { cap: WORLDBOOK_HARD_CAP_TOKENS })

  const source =
    budget.factor_source === 'group'
      ? t('budgetExplain.source.group')
      : budget.factor_source === 'model_name'
        ? t('budgetExplain.source.modelName', { family: budget.factor_family })
        : t('budgetExplain.source.default')

  return (
    <Box>
      <Typography variant="body2" color="text.secondary">
        {t('budgetExplain.line', {
          budget: budget.budget,
          approx: budget.approx_chars,
          percent: budget.percent,
          contextLength: budget.context_length,
          outputReserve: budget.output_reserve,
          cap: capText,
          modelGroup: budget.model_group,
          factor: budget.factor,
          source,
        })}
      </Typography>
      {totalTokens !== undefined && (
        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
          {t('budgetExplain.total', { total: totalTokens })}
        </Typography>
      )}
      {budget.fallback_context_length > 0 && (
        <Alert severity="info" sx={{ mt: 1 }}>
          {t('budgetExplain.fallback', {
            modelGroup: budget.fallback_model_group,
            contextLength: budget.fallback_context_length,
          })}
        </Alert>
      )}
    </Box>
  )
}
