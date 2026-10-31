/**
 * 换人设的二次确认
 *
 * ## 为什么这件事必须有一个专门的弹窗
 *
 * 人设是隔离键 `(记忆域, 人, 人设)` 的一维。运营在群里把人设从甲换成乙的**那一刻**，
 * 该群所有人的好感当场看起来归零 —— 实际是切到了另一份，旧的那份还在、换回来就回来。
 * 这符合设计意图（「换了个人设就不会记得以前的恩怨情仇」），但**没有这段说明的话
 * 它长得和数据丢失一模一样**，而那是必然变成工单的一件事。
 *
 * ## 两个入口共用这一个组件
 *
 * - 频道人设：`POST /chat-channel/{chat_key}/preset`
 * - 实例默认人设：`PUT /presets/instance/{key}/default`（影响该实例下所有未绑人设的频道）
 *
 * 共用而不是各写一份，理由与后端把 `PRESET_SWITCH_REVERSIBLE_NOTE` 收成一份完全相同：
 * 同一件事在两个弹窗里说成两种措辞，运营会以为是两种不同的后果。
 *
 * ## 数字全部由后端给
 *
 * 「影响 N 人」这句话必须落在一个查得出来的数上（好感表上有 `preset_id` 这一列）。
 * 前端一个字都不复算 —— 复算一份就是在授权语义之外再造一个会分叉的判据。
 * 记忆那一侧后端只能给到**域级**条数（`mem_*` 表上没有人设列），
 * 它自带的 `memory_scope_note` 会把这件事说清楚，这里原样显示。
 */

import { Alert, Box, CircularProgress, Stack, Typography } from '@mui/material'
import { useTranslation } from 'react-i18next'
import ActionButton from './ActionButton'
import NekroDialog from './NekroDialog'
import type { PresetSwitchImpact } from '../../services/api/types'

export interface PresetSwitchConfirmDialogProps {
  open: boolean
  /** 影响面；仍在加载时为 null */
  impact: PresetSwitchImpact | null
  loading?: boolean
  /** 加载影响面时出的错；非空时只显示错误并禁用确认 */
  error?: string
  submitting?: boolean
  onCancel: () => void
  onConfirm: () => void
}

export default function PresetSwitchConfirmDialog({
  open,
  impact,
  loading = false,
  error = '',
  submitting = false,
  onCancel,
  onConfirm,
}: PresetSwitchConfirmDialogProps) {
  // 文案与「身份与域」页同一个命名空间：换人设这件事的措辞跨了三个页面
  // （频道详情、人设管理、总后台），拆成三份必然分叉。
  const { t } = useTranslation('console-identity')

  return (
    <NekroDialog
      open={open}
      onClose={onCancel}
      title={t('presetSwitch.title')}
      maxWidth="sm"
      actions={
        <>
          <ActionButton tone="ghost" onClick={onCancel}>
            {t('presetSwitch.cancel')}
          </ActionButton>
          <ActionButton
            tone="danger"
            disabled={loading || submitting || Boolean(error) || !impact}
            onClick={onConfirm}
          >
            {t('presetSwitch.confirm')}
          </ActionButton>
        </>
      }
    >
      {loading && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={24} />
        </Box>
      )}

      {!loading && error && <Alert severity="error">{error}</Alert>}

      {!loading && !error && impact && (
        <Stack spacing={2} sx={{ pt: 1 }}>
          <Typography variant="body2">
            {t('presetSwitch.line', {
              scope: impact.scope_label,
              from: impact.current_preset_name,
              to: impact.target_preset_name,
            })}
          </Typography>

          {/* 这条是整个弹窗存在的理由：把「会发生什么」说成一个数 */}
          <Alert severity="warning">
            {impact.affected_channels > 1
              ? t('presetSwitch.impactChannels', {
                  people: impact.affected_people,
                  channels: impact.affected_channels,
                })
              : t('presetSwitch.impact', { people: impact.affected_people })}
          </Alert>

          {/* 「旧数据保留、换回来就回来」这句由后端给，两个入口共用同一份措辞 */}
          <Alert severity="info" variant="outlined">
            {impact.reversible_note}
            {impact.returning_people > 0 &&
              ` ${t('presetSwitch.returning', {
                to: impact.target_preset_name,
                people: impact.returning_people,
              })}`}
          </Alert>

          <Typography variant="caption" color="text.secondary">
            {t('presetSwitch.domainLine', {
              domain: impact.domain_id,
              memories: impact.domain_memory_paragraphs,
            })}
            <br />
            {impact.memory_scope_note}
          </Typography>
        </Stack>
      )}
    </NekroDialog>
  )
}
