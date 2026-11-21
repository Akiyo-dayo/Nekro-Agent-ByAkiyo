/**
 * 频道额度详情（点列表一行打开）
 *
 * 三块内容各自回答一个问题：
 * - 逐天的小表：这个群这几天到底回了多少次、烧了多少 token；
 * - 按场景的 token 合计：**后台调用烧掉的部分**，与「用量」页同源（账本），不是估算；
 * - 生效策略：此刻是哪一层的额度、超额会怎样。
 *
 * 操作按身份分：平台方能改频道层策略、能临时加额；租户管理员只能调低「我的额度」。
 * 这两组按钮**不是**同一个接口加一层前端判断——后端的写接口本来就是两组模型，
 * 租户那一组请求体里根本没有平台方字段。
 */

import { useMemo, useState } from 'react'
import {
  Box,
  CircularProgress,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../../components/common/ActionButton'
import NekroDialog from '../../../components/common/NekroDialog'
import QueryErrorState from '../../../components/common/QueryErrorState'
import { useIdentity } from '../../../hooks/useIdentity'
import { useNotification } from '../../../hooks/useNotification'
import { UNIFIED_TABLE_STYLES } from '../../../theme/variants'
import {
  CONSOLE_CHANNEL_QUOTA_QUERY_KEYS,
  consoleChannelQuotaApi,
  type ChannelQuotaDailyItem,
} from '../../../services/api/consoleChannelQuota'
import ChannelQuotaPolicyDialog from './ChannelQuotaPolicyDialog'
import TenantQuotaDialog from './TenantQuotaDialog'
import { channelQuotaActionLabel, channelQuotaUnitLabel, formatQuotaInt } from './channelQuotaUi'

/** 场景名是账本里的固定集合；未知场景原样显示，不编一个「其他」把它吞掉 */
const KNOWN_SCENES = new Set([
  'chat',
  'history_plan',
  'affection_eval',
  'memory_consolidate',
  'embedding',
  'vision_describe',
  'plugin',
  'command',
])

export default function ChannelQuotaDetailDialog({
  item,
  onClose,
}: {
  /** null 表示没打开 */
  item: ChannelQuotaDailyItem | null
  onClose: () => void
}) {
  const { t } = useTranslation('console-channel-quota')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { isPlatformOwner, isTenantAdmin } = useIdentity()

  const [days, setDays] = useState(7)
  const [policyOpen, setPolicyOpen] = useState(false)
  const [tenantOpen, setTenantOpen] = useState(false)
  const [bonusAmount, setBonusAmount] = useState('')
  const [bonusError, setBonusError] = useState('')

  const chatKey = item?.chat_key ?? ''
  const open = item !== null

  const historyQuery = useQuery({
    queryKey: CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.history(chatKey, days),
    queryFn: () => consoleChannelQuotaApi.getHistory(chatKey, days),
    enabled: open && Boolean(chatKey),
    staleTime: 15_000,
    retry: false,
  })

  const bonusMutation = useMutation({
    mutationFn: (amount: number) => consoleChannelQuotaApi.addBonus(chatKey, amount),
    onSuccess: response => {
      notification.success(t('bonusDialog.success', { value: formatQuotaInt(response.bonus) }))
      setBonusAmount('')
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-daily'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-status'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-history'] })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const recountMutation = useMutation({
    mutationFn: () => consoleChannelQuotaApi.recountChannel(chatKey),
    onSuccess: () => {
      notification.success(t('recount.channelSuccess'))
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-daily'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-status'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-history'] })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const sceneRows = useMemo(() => historyQuery.data?.by_scene ?? [], [historyQuery.data])

  const submitBonus = () => {
    const parsed = Number(bonusAmount.trim())
    if (!bonusAmount.trim() || !Number.isInteger(parsed) || parsed < 1) {
      setBonusError(t('bonusDialog.invalid'))
      return
    }
    setBonusError('')
    bonusMutation.mutate(parsed)
  }

  const policy = historyQuery.data?.policy

  return (
    <>
      <NekroDialog
        open={open}
        onClose={onClose}
        title={item ? `${t('detail.title')} · ${item.channel_name || item.chat_key}` : t('detail.title')}
        maxWidth="md"
        actions={
          <>
            {/* 平台方：改频道层策略；加额要填数字，输入框在正文里。
                租户管理员：只能调低「我的额度」。两组写接口本来就是两组模型 */}
            {isPlatformOwner && (
              <ActionButton tone="secondary" onClick={() => setPolicyOpen(true)}>
                {t('actions.editPolicy')}
              </ActionButton>
            )}
            {!isPlatformOwner && isTenantAdmin && (
              <ActionButton tone="secondary" onClick={() => setTenantOpen(true)}>
                {t('actions.myLimit')}
              </ActionButton>
            )}
            <ActionButton tone="ghost" onClick={onClose}>
              {t('actions.close')}
            </ActionButton>
          </>
        }
      >
        {historyQuery.isError ? (
          <QueryErrorState
            compact
            error={historyQuery.error}
            onRetry={() => void historyQuery.refetch()}
          />
        ) : historyQuery.isLoading ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
            <CircularProgress />
          </Box>
        ) : (
          <Stack spacing={2.5}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
              <ToggleButtonGroup
                size="small"
                exclusive
                value={days}
                onChange={(_event, value: number | null) => {
                  if (value !== null) setDays(value)
                }}
              >
                <ToggleButton value={7}>{t('detail.days7')}</ToggleButton>
                <ToggleButton value={30}>{t('detail.days30')}</ToggleButton>
              </ToggleButtonGroup>
              <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-all' }}>
                {chatKey}
              </Typography>
            </Box>

            <Box>
              <Typography variant="subtitle2" fontWeight={700} gutterBottom>
                {t('detail.dailyTitle')}
              </Typography>
              <Table size="small" sx={UNIFIED_TABLE_STYLES.getTableBase(false, true)}>
                <TableHead>
                  <TableRow>
                    <TableCell>{t('detail.columns.day')}</TableCell>
                    <TableCell align="right">{t('detail.columns.replies')}</TableCell>
                    <TableCell align="right">{t('detail.columns.tokensAll')}</TableCell>
                    <TableCell align="right">{t('detail.columns.tokensReply')}</TableCell>
                    <TableCell align="right">{t('detail.columns.bonus')}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {(historyQuery.data?.days ?? []).map(day => (
                    <TableRow key={day.day}>
                      <TableCell>{day.day}</TableCell>
                      <TableCell align="right">{formatQuotaInt(day.replies)}</TableCell>
                      <TableCell align="right">{formatQuotaInt(day.tokens_all)}</TableCell>
                      <TableCell align="right">{formatQuotaInt(day.tokens_reply)}</TableCell>
                      <TableCell align="right">
                        {day.bonus > 0 ? t('value.bonus', { value: formatQuotaInt(day.bonus) }) : '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>

            <Box>
              <Typography variant="subtitle2" fontWeight={700} gutterBottom>
                {t('detail.sceneTitle')}
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
                {t('detail.sceneHint')}
              </Typography>
              {sceneRows.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  {t('detail.noScene')}
                </Typography>
              ) : (
                <Table size="small" sx={UNIFIED_TABLE_STYLES.getTableBase(false, true)}>
                  <TableHead>
                    <TableRow>
                      <TableCell>{t('detail.columns.scene')}</TableCell>
                      <TableCell align="right">Token</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {sceneRows.map(row => (
                      <TableRow key={row.scene || '__empty__'}>
                        <TableCell>
                          {KNOWN_SCENES.has(row.scene)
                            ? t(`detail.scene.${row.scene}`)
                            : row.scene || t('detail.scene.other')}
                        </TableCell>
                        <TableCell align="right">{formatQuotaInt(row.token_total)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </Box>

            <Box>
              <Typography variant="subtitle2" fontWeight={700} gutterBottom>
                {t('detail.policyTitle')}
              </Typography>
              {policy && (
                <Stack spacing={0.5}>
                  <Typography variant="body2">
                    {t('detail.policy.unit')}：{channelQuotaUnitLabel(t, policy.unit)}
                  </Typography>
                  <Typography variant="body2">
                    {t('detail.policy.platformLimit')}：{formatQuotaInt(policy.platform_limit)}
                  </Typography>
                  <Typography variant="body2">
                    {t('detail.policy.tenantLimit')}：
                    {policy.tenant_limit === null
                      ? t('detail.notSet')
                      : formatQuotaInt(policy.tenant_limit)}
                  </Typography>
                  <Typography variant="body2">
                    {t('detail.policy.effectiveLimit')}：{formatQuotaInt(policy.effective_limit)}
                  </Typography>
                  <Typography variant="body2">
                    {t('detail.policy.action')}：{channelQuotaActionLabel(t, policy.over_limit_action)}
                  </Typography>
                  <Typography variant="body2">
                    {t('detail.policy.pauseMessage')}：{policy.pause_message}
                  </Typography>
                </Stack>
              )}
            </Box>

            {isPlatformOwner && (
              <Box>
                <Typography variant="subtitle2" fontWeight={700} gutterBottom>
                  {t('actions.bonus')}
                </Typography>
                <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
                  {t('bonusDialog.hint')}
                </Typography>
                <Stack direction="row" spacing={1.5} alignItems="flex-start" flexWrap="wrap" useFlexGap>
                  <TextField
                    size="small"
                    type="number"
                    label={t('bonusDialog.amount')}
                    value={bonusAmount}
                    error={Boolean(bonusError)}
                    helperText={bonusError}
                    slotProps={{ htmlInput: { min: 1, step: 1 } }}
                    onChange={event => setBonusAmount(event.target.value)}
                    sx={{ maxWidth: 220 }}
                  />
                  <ActionButton
                    tone="primary"
                    onClick={submitBonus}
                    disabled={bonusMutation.isPending}
                  >
                    {bonusMutation.isPending ? <CircularProgress size={18} /> : t('actions.bonus')}
                  </ActionButton>
                </Stack>

                <Typography variant="subtitle2" fontWeight={700} sx={{ mt: 2 }} gutterBottom>
                  {t('recount.channelTitle')}
                </Typography>
                <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
                  {t('recount.hint')}
                </Typography>
                <ActionButton
                  tone="ghost"
                  disabled={recountMutation.isPending}
                  onClick={() => recountMutation.mutate()}
                >
                  {recountMutation.isPending ? <CircularProgress size={18} /> : t('recount.channel')}
                </ActionButton>
              </Box>
            )}
          </Stack>
        )}
      </NekroDialog>

      <ChannelQuotaPolicyDialog
        open={policyOpen}
        onClose={() => setPolicyOpen(false)}
        scope="channel"
        chatKey={chatKey}
      />
      <TenantQuotaDialog
        open={tenantOpen}
        onClose={() => setTenantOpen(false)}
        scope="channel"
        chatKey={chatKey}
      />
    </>
  )
}
