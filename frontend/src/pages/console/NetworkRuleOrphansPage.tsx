/**
 * 总后台 → 隔离规则（平台方专属）
 *
 * 升级换作用域之后，宿主的 iptables 链上会留下一组用旧标记的规则。新代码用新标记，
 * 因此**永远不会去清它们**；从前运维只能自己去 iptables 上敲，而那条路上一轮已经翻过车
 * （`--comment` 的引号被 shell 吃掉，`-D` 全部失败——半成功的话被删掉的就可能是还在生效的那一组）。
 *
 * 页面强制走「盘点 → 看清要删什么 → 回填标记确认 → 执行」四步，与实例历史归属迁移同一形态：
 * 判定不是 `orphan`、或确认输入不匹配时，执行按钮始终禁用。
 *
 * **判定完全由后端做，这里一个字都不复算。** 前端只负责把规则原文、旧网桥上的容器数、
 * 以及每一条拦截理由原样显示出来——判据写成两份而两份分叉时，没有任何运行时信号。
 */

import { useMemo, useState } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Chip,
  CircularProgress,
  Divider,
  Stack,
  TextField,
  Typography,
  useTheme,
} from '@mui/material'
import ExpandMoreRoundedIcon from '@mui/icons-material/ExpandMoreRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import { useNotification } from '../../hooks/useNotification'
import { CHIP_VARIANTS } from '../../theme/variants'
import {
  CONSOLE_NETWORK_RULE_QUERY_KEYS,
  consoleNetworkRulesApi,
  type OrphanRuleCandidate,
  type OrphanRuleVerdict,
} from '../../services/api/consoleNetworkRules'

const VERDICT_TONE: Record<OrphanRuleVerdict, 'success' | 'warning' | 'info'> = {
  orphan: 'success',
  blocked: 'warning',
  self: 'info',
}

function CandidateCard({
  candidate,
  onCleanup,
}: {
  candidate: OrphanRuleCandidate
  onCleanup: (candidate: OrphanRuleCandidate) => void
}) {
  const theme = useTheme()
  const { t } = useTranslation('console-network-rules')

  return (
    <Box
      sx={{
        border: theme => `1px solid ${theme.palette.divider}`,
        borderRadius: 1.5,
        p: 2,
        display: 'flex',
        flexDirection: 'column',
        gap: 1.5,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <Typography variant="subtitle2" fontWeight={700} sx={{ wordBreak: 'break-all' }}>
          {candidate.marker}
        </Typography>
        <Chip
          size="small"
          label={candidate.guard_label}
          sx={CHIP_VARIANTS.getPaletteChip(theme, 'default', true)}
        />
        <Chip
          size="small"
          label={t(`verdict.${candidate.verdict}`)}
          sx={CHIP_VARIANTS.getPaletteChip(theme, VERDICT_TONE[candidate.verdict], true)}
        />
        <Chip
          size="small"
          label={t('ruleCount', { count: candidate.rule_count })}
          sx={CHIP_VARIANTS.getPaletteChip(theme, 'default', true)}
        />
        <Box sx={{ flex: 1 }} />
        {candidate.verdict === 'orphan' && (
          <ActionButton tone="danger" size="small" onClick={() => onCleanup(candidate)}>
            {t('actions.cleanup')}
          </ActionButton>
        )}
      </Box>

      <Typography variant="caption" color="text.secondary">
        {t('chains')}: {candidate.chains.join(' / ') || '-'}
      </Typography>

      {/* 旧网络上还有几个容器 —— CC 工作区容器是长命的，删掉规则里的兜底 DROP
          之后它们能直接访问宿主内网，不报错也不留日志。这一行必须显眼 */}
      {candidate.networks.map(network => (
        <Alert
          key={network.interface}
          severity={network.container_total > 0 ? 'error' : 'info'}
          variant="outlined"
        >
          {network.container_total > 0
            ? t('network.occupied', {
                network: network.network || network.interface,
                bridge: network.interface,
                count: network.container_total,
                containers: network.container_names.join('、'),
              })
            : t('network.empty', {
                network: network.network || t('network.none'),
                bridge: network.interface,
              })}
        </Alert>
      ))}

      {candidate.blockers.map(blocker => (
        <Alert key={blocker} severity={candidate.verdict === 'self' ? 'info' : 'warning'}>
          {blocker}
        </Alert>
      ))}

      {candidate.notes.length > 0 && (
        <Box>
          {candidate.notes.map(note => (
            <Typography key={note} variant="caption" color="text.secondary" component="div">
              · {note}
            </Typography>
          ))}
        </Box>
      )}

      <Accordion disableGutters elevation={0} sx={{ '&:before': { display: 'none' } }}>
        <AccordionSummary expandIcon={<ExpandMoreRoundedIcon />} sx={{ px: 0 }}>
          <Typography variant="caption" color="text.secondary">
            {t('rulesTitle', { count: candidate.rules.length })}
          </Typography>
        </AccordionSummary>
        <AccordionDetails sx={{ px: 0 }}>
          <Box
            component="pre"
            sx={{
              m: 0,
              p: 1.5,
              fontSize: 12,
              lineHeight: 1.7,
              overflowX: 'auto',
              borderRadius: 1,
              bgcolor: theme => theme.palette.action.hover,
            }}
          >
            {candidate.rules
              .map(rule => `[${rule.family} ${rule.chain}] ${rule.text}`)
              .join('\n') || '-'}
          </Box>
        </AccordionDetails>
      </Accordion>
    </Box>
  )
}

export default function NetworkRuleOrphansPage() {
  const { t } = useTranslation('console-network-rules')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()

  const [target, setTarget] = useState<OrphanRuleCandidate | null>(null)
  const [confirmText, setConfirmText] = useState('')

  const planQuery = useQuery({
    queryKey: CONSOLE_NETWORK_RULE_QUERY_KEYS.orphans,
    queryFn: () => consoleNetworkRulesApi.listOrphans(),
    // 判定读的是宿主此刻的状态（谁挂在旧网桥上、哪个端口还在监听），缓存久了就是在看历史
    staleTime: 0,
    retry: false,
  })

  const cleanupMutation = useMutation({
    mutationFn: () => consoleNetworkRulesApi.cleanupOrphan(target?.marker ?? '', confirmText.trim()),
    onSuccess: result => {
      if (result.ok) {
        notification.success(result.message)
      } else {
        notification.warning(result.message)
      }
      void queryClient.invalidateQueries({ queryKey: CONSOLE_NETWORK_RULE_QUERY_KEYS.orphans })
      setTarget(null)
      setConfirmText('')
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const plan = planQuery.data
  const grouped = useMemo(() => {
    const candidates = plan?.candidates ?? []
    return {
      orphan: candidates.filter(item => item.verdict === 'orphan'),
      blocked: candidates.filter(item => item.verdict === 'blocked'),
      self: candidates.filter(item => item.verdict === 'self'),
    }
  }, [plan])

  const canSubmit = Boolean(target) && confirmText.trim() === target?.marker && !cleanupMutation.isPending

  const openCleanup = (candidate: OrphanRuleCandidate) => {
    setTarget(candidate)
    setConfirmText('')
  }

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 3 },
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        overflow: 'auto',
        boxSizing: 'border-box',
        // 定高滚动列：子项按内容定高，不被压扁（见 SCROLL_COLUMN_STYLES）
        '& > *': { flexShrink: 0 },
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography variant="h6" fontWeight={700}>
            {t('title')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('subtitle')}
          </Typography>
        </Box>
        <ActionButton
          tone="secondary"
          size="small"
          startIcon={<RefreshRoundedIcon />}
          disabled={planQuery.isFetching}
          onClick={() => void planQuery.refetch()}
        >
          {tCommon('actions.refresh')}
        </ActionButton>
      </Box>

      {plan && (
        <Alert severity="info" variant="outlined">
          {t('scopeLine', { scope: plan.scope, source: plan.scope_source })}
        </Alert>
      )}

      {planQuery.isLoading && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={28} />
        </Box>
      )}

      {/* 这一页是平台方专属（两个端点都是 `Role.Super`），租户被 `PLATFORM_ONLY_PATHS`
          挡在外面。但错误原文对 403 是「权限不足」四个字，与 5xx 混成同一种红条；
          换成能分辨的那个组件，理由与总后台其余各页一致 */}
      {planQuery.isError && (
        <QueryErrorState compact error={planQuery.error} onRetry={() => void planQuery.refetch()} />
      )}

      {plan && !plan.available && <Alert severity="warning">{plan.unavailable_reason}</Alert>}

      {plan?.available && plan.candidates.length === 0 && (
        <Alert severity="success">{t('empty')}</Alert>
      )}

      {(['orphan', 'blocked', 'self'] as const).map(section =>
        grouped[section].length === 0 ? null : (
          <Box key={section} sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
            <Box>
              <Typography variant="subtitle1" fontWeight={700}>
                {t(`section.${section}.title`, { count: grouped[section].length })}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {t(`section.${section}.hint`)}
              </Typography>
            </Box>
            <Divider />
            <Stack spacing={1.5}>
              {grouped[section].map(candidate => (
                <CandidateCard key={candidate.marker} candidate={candidate} onCleanup={openCleanup} />
              ))}
            </Stack>
          </Box>
        )
      )}

      <NekroDialog
        open={Boolean(target)}
        onClose={() => setTarget(null)}
        title={t('confirmDialog.title')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setTarget(null)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton tone="danger" disabled={!canSubmit} onClick={() => cleanupMutation.mutate()}>
              {t('actions.cleanup')}
            </ActionButton>
          </>
        }
      >
        {target && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
            <Alert severity="warning">
              {t('confirmDialog.warning', { marker: target.marker, count: target.rule_count })}
            </Alert>
            <Typography variant="caption" color="text.secondary">
              {t('confirmDialog.recheck')}
            </Typography>
            <TextField
              size="small"
              fullWidth
              autoComplete="off"
              label={t('confirmDialog.label')}
              placeholder={target.marker}
              value={confirmText}
              error={Boolean(confirmText) && confirmText.trim() !== target.marker}
              helperText={
                confirmText && confirmText.trim() !== target.marker
                  ? t('confirmDialog.mismatch')
                  : t('confirmDialog.helper', { marker: target.marker })
              }
              onChange={event => setConfirmText(event.target.value)}
            />
          </Box>
        )}
      </NekroDialog>
    </Box>
  )
}
