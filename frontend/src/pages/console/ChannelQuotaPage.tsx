/**
 * 频道额度（总后台）
 *
 * 回答的问题是：**平台方给每个群 / 私聊承诺的每日用量，今天用掉了多少、还剩多少**。
 * 数据来自 `nekro_agent/routers/console/channel_quota.py`，作用域由后端按当前身份收敛：
 * 平台方看全部，租户只看自己名下实例上的频道，越权一律 404。
 *
 * ## 与「实例自限」标签的关系
 *
 * 这一页是平台额度（平台方给每个群 / 私聊的承诺），`/console/quota` 是租户在它以内
 * 自己再调低的自限（按回复回合数）。两层各管各的，**两层都放行才回复**。这条说明已经
 * 挪到「额度与用量」页壳的页头副标题，本页不再单独解释一遍。
 *
 * ## 失败不画 0
 *
 * 列表失败时整块换成 `QueryErrorState`：403 与「没有频道」在界面上必须能分辨。
 * 「0」是一个有效答案，用户会当真。
 */

import { useMemo, useState } from 'react'
import {
  Box,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  FormControlLabel,
  LinearProgress,
  MenuItem,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import ActionButton from '../../components/common/ActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import TablePaginationStyled from '../../components/common/TablePaginationStyled'
import { useIdentity } from '../../hooks/useIdentity'
import { useNotification } from '../../hooks/useNotification'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  CONSOLE_CHANNEL_QUOTA_QUERY_KEYS,
  consoleChannelQuotaApi,
  type ChannelQuotaAction,
  type ChannelQuotaDailyFilters,
  type ChannelQuotaDailyItem,
  type ChannelQuotaState,
  type ChannelQuotaUnit,
  type GlobalQuotaPolicyUpdate,
} from '../../services/api/consoleChannelQuota'
import { TENANT_QUERY_KEYS, tenantApi } from '../../services/api/tenant'
import ChannelQuotaDetailDialog from './components/ChannelQuotaDetailDialog'
import {
  channelQuotaActionLabel,
  channelQuotaProgressColor,
  channelQuotaStateColor,
  channelQuotaUnitLabel,
  formatQuotaInt,
  formatQuotaLimit,
} from './components/channelQuotaUi'

const PAGE_SIZE = 50
const MAX_PAUSE_MESSAGE_LENGTH = 200

const STATE_OPTIONS: ChannelQuotaState[] = [
  'normal',
  'warning',
  'exceeded',
  'paused',
  'unlimited',
  'disabled',
]


/**
 * 全局策略卡片
 *
 * 开关是**总闸**：关着时所有频道都不受限，实例层与频道层只能在它开着时把某处关掉。
 * 这句语义必须写在卡片上，否则平台方会以为「实例层能单独打开」。
 */
function GlobalPolicyCard() {
  const { t } = useTranslation('console-channel-quota')
  const notification = useNotification()
  const queryClient = useQueryClient()

  const policyQuery = useQuery({
    queryKey: CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.globalPolicy,
    queryFn: () => consoleChannelQuotaApi.getGlobalPolicy(),
    staleTime: 15_000,
    retry: false,
  })

  const [draft, setDraft] = useState<GlobalQuotaPolicyUpdate | null>(null)
  const policy = policyQuery.data
  const form: GlobalQuotaPolicyUpdate = draft ?? {
    enabled: policy?.enabled ?? false,
    unit: policy?.unit ?? 'replies',
    group_daily_limit: policy?.group_daily_limit ?? 0,
    private_daily_limit: policy?.private_daily_limit ?? 0,
    over_limit_action: policy?.over_limit_action ?? 'alert',
    pause_message: policy?.pause_message ?? '',
  }

  const saveMutation = useMutation({
    mutationFn: (body: GlobalQuotaPolicyUpdate) => consoleChannelQuotaApi.updateGlobalPolicy(body),
    onSuccess: () => {
      notification.success(t('global.saveSuccess'))
      setDraft(null)
      void queryClient.invalidateQueries({ queryKey: CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.globalPolicy })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-daily'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-status'] })
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-history'] })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const update = (patch: Partial<GlobalQuotaPolicyUpdate>) => setDraft({ ...form, ...patch })

  return (
    <Card>
      <CardContent>
        <Typography variant="subtitle1" fontWeight={700}>
          {t('global.title')}
        </Typography>
        <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1.5 }}>
          {t('global.subtitle')}
        </Typography>
        <Divider sx={{ mb: 1.5 }} />

        {policyQuery.isError ? (
          <QueryErrorState
            compact
            error={policyQuery.error}
            onRetry={() => void policyQuery.refetch()}
          />
        ) : policyQuery.isLoading ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
            <CircularProgress size={24} />
          </Box>
        ) : (
          <Stack spacing={1.5}>
            {/* 开关是总闸，用 Switch 而不是下拉：它只有一个二元状态，下拉会让人以为还有第三档 */}
            <FormControlLabel
              control={
                <Switch
                  checked={form.enabled}
                  onChange={event => update({ enabled: event.target.checked })}
                />
              }
              label={t('global.enabled')}
            />

            <TextField
              select
              size="small"
              label={t('global.unit')}
              value={form.unit}
              onChange={event => update({ unit: event.target.value as ChannelQuotaUnit })}
              sx={{ maxWidth: 320 }}
            >
              <MenuItem value="replies">{t('unit.replies')}</MenuItem>
              <MenuItem value="tokens">{t('unit.tokens')}</MenuItem>
            </TextField>

            <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap>
              <TextField
                size="small"
                type="number"
                label={t('global.groupLimit')}
                value={form.group_daily_limit ?? 0}
                helperText={t('global.limitHint')}
                slotProps={{ htmlInput: { min: 0, step: 1 } }}
                onChange={event => update({ group_daily_limit: Number(event.target.value) })}
                sx={{ maxWidth: 220 }}
              />
              <TextField
                size="small"
                type="number"
                label={t('global.privateLimit')}
                value={form.private_daily_limit ?? 0}
                helperText={t('global.limitHint')}
                slotProps={{ htmlInput: { min: 0, step: 1 } }}
                onChange={event => update({ private_daily_limit: Number(event.target.value) })}
                sx={{ maxWidth: 220 }}
              />
              <TextField
                select
                size="small"
                label={t('global.action')}
                value={form.over_limit_action}
                onChange={event =>
                  update({ over_limit_action: event.target.value as ChannelQuotaAction })
                }
                sx={{ maxWidth: 200 }}
              >
                <MenuItem value="alert">{t('action.alert')}</MenuItem>
                <MenuItem value="pause">{t('action.pause')}</MenuItem>
              </TextField>
            </Stack>

            <TextField
              multiline
              minRows={2}
              size="small"
              label={t('global.pauseMessage')}
              value={form.pause_message ?? ''}
              helperText={t('global.pauseMessageHint', { max: MAX_PAUSE_MESSAGE_LENGTH })}
              onChange={event => update({ pause_message: event.target.value })}
            />
            <Typography variant="caption" color="text.secondary" align="right">
              {t('global.charCount', {
                value: (form.pause_message ?? '').length,
                max: MAX_PAUSE_MESSAGE_LENGTH,
              })}
            </Typography>

            <Box>
              <ActionButton
                tone="primary"
                disabled={saveMutation.isPending}
                onClick={() =>
                  saveMutation.mutate({
                    ...form,
                    pause_message: form.pause_message?.trim() ? form.pause_message : null,
                  })
                }
              >
                {saveMutation.isPending ? <CircularProgress size={18} /> : t('global.save')}
              </ActionButton>
            </Box>
          </Stack>
        )}
      </CardContent>
    </Card>
  )
}

interface ChannelQuotaPageProps {
  /** 嵌在「额度与用量」页壳里时，不再画自己的大标题与外层页面内边距 */
  embedded?: boolean
}

export default function ChannelQuotaPage({ embedded = false }: ChannelQuotaPageProps) {
  const theme = useTheme()
  const { t } = useTranslation('console-channel-quota')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { isPlatformOwner: isPlatformOwnerRole, isLoading: isIdentityLoading } = useIdentity()
  // 身份没确认之前兜底角色是平台方：不等确认就画平台方那几块，租户会先看到全局策略卡片、
  // 并打出两条必然 403 的请求（全局策略、租户列表）
  const isPlatformOwner = isPlatformOwnerRole && !isIdentityLoading

  // 留空表示「今天」，由后端按额度时区解释。浏览器所在时区可能与额度时区不同（例如平台方人在海外），
  // 前端自己算「今天」会默认看错一天；输入框显示的是后端回来的那一天
  const [day, setDay] = useState('')
  const [state, setState] = useState<ChannelQuotaState | ''>('')
  const [chatType, setChatType] = useState<'group' | 'private' | ''>('')
  const [tenantId, setTenantId] = useState<number | null>(null)
  const [page, setPage] = useState(0)
  const [detailItem, setDetailItem] = useState<ChannelQuotaDailyItem | null>(null)

  // 租户下拉只有平台方需要，也只对平台方发请求（租户身份下这一条必然 403）
  const tenantsQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.tenants,
    queryFn: () => tenantApi.listTenants(),
    enabled: isPlatformOwner,
    staleTime: 60_000,
    retry: false,
  })

  const filters: ChannelQuotaDailyFilters = useMemo(
    () => ({ day, state, chatType, tenantId, page: page + 1, pageSize: PAGE_SIZE }),
    [day, state, chatType, tenantId, page]
  )

  const dailyQuery = useQuery({
    queryKey: CONSOLE_CHANNEL_QUOTA_QUERY_KEYS.daily(filters),
    queryFn: () => consoleChannelQuotaApi.listDaily(filters),
    staleTime: 15_000,
    retry: false,
  })

  const items = useMemo(() => dailyQuery.data?.items ?? [], [dailyQuery.data])
  const total = dailyQuery.data?.total ?? 0

  // 重算的是列表当前显示的那一天（输入框里那个日期键就是后端回来的），不是浏览器算的今天
  const recountDay = day || dailyQuery.data?.day || ''

  const recountMutation = useMutation({
    mutationFn: () => consoleChannelQuotaApi.recountAll(recountDay || undefined),
    onSuccess: response => {
      notification.success(t('recount.allSuccess', { day: response.day, channels: response.channels }))
      void queryClient.invalidateQueries({ queryKey: ['console-channel-quota-daily'] })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  return (
    <Box
      sx={{
        p: embedded ? 0 : { xs: 1.5, sm: 2, md: 3 },
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
      }}
    >
      {!embedded && (
        <Box>
          <Typography variant="h6" fontWeight={700}>
            {t('title')}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t('subtitle')}
          </Typography>
        </Box>
      )}

      {isPlatformOwner && <GlobalPolicyCard />}

      <Card>
        <CardContent>
          <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap alignItems="center" sx={{ mb: 1.5 }}>
            <TextField
              size="small"
              type="date"
              label={t('filters.day')}
              value={day || dailyQuery.data?.day || ''}
              onChange={event => {
                setDay(event.target.value)
                setPage(0)
              }}
              slotProps={{ inputLabel: { shrink: true } }}
              sx={{ maxWidth: 190 }}
            />
            <TextField
              select
              size="small"
              label={t('filters.state')}
              value={state}
              onChange={event => {
                setState(event.target.value as ChannelQuotaState | '')
                setPage(0)
              }}
              sx={{ minWidth: 150 }}
            >
              <MenuItem value="">{t('filters.all')}</MenuItem>
              {STATE_OPTIONS.map(option => (
                <MenuItem key={option} value={option}>
                  {t(`state.${option}`)}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              select
              size="small"
              label={t('filters.type')}
              value={chatType}
              onChange={event => {
                setChatType(event.target.value as 'group' | 'private' | '')
                setPage(0)
              }}
              sx={{ minWidth: 130 }}
            >
              <MenuItem value="">{t('filters.all')}</MenuItem>
              <MenuItem value="group">{t('filters.group')}</MenuItem>
              <MenuItem value="private">{t('filters.private')}</MenuItem>
            </TextField>
            {isPlatformOwner && (
              <TextField
                select
                size="small"
                label={t('filters.tenant')}
                value={tenantId === null ? '' : String(tenantId)}
                onChange={event => {
                  setTenantId(event.target.value === '' ? null : Number(event.target.value))
                  setPage(0)
                }}
                sx={{ minWidth: 170 }}
              >
                <MenuItem value="">{t('filters.all')}</MenuItem>
                {(tenantsQuery.data?.items ?? []).map(tenant => (
                  <MenuItem key={tenant.id} value={String(tenant.id)}>
                    {tenant.display_name || tenant.name}
                  </MenuItem>
                ))}
              </TextField>
            )}
            <ActionButton
              tone="ghost"
              size="small"
              startIcon={<RefreshRoundedIcon />}
              onClick={() => void dailyQuery.refetch()}
            >
              {t('actions.refresh')}
            </ActionButton>
            {isPlatformOwner && (
              <>
                <ActionButton
                  tone="ghost"
                  size="small"
                  disabled={recountMutation.isPending}
                  onClick={() => recountMutation.mutate()}
                >
                  {recountMutation.isPending ? <CircularProgress size={18} /> : t('recount.all')}
                </ActionButton>
                <Typography variant="caption" color="text.secondary">
                  {t('recount.hint')}
                </Typography>
              </>
            )}
          </Stack>

          {dailyQuery.isLoading ? (
            <Box sx={{ minHeight: 200, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <CircularProgress />
            </Box>
          ) : dailyQuery.isError ? (
            /* 失败分支排在空态之前：403 之下两个条件同时成立，先命中的那个会赢 */
            <QueryErrorState error={dailyQuery.error} onRetry={() => void dailyQuery.refetch()} />
          ) : items.length === 0 ? (
            <Box
              sx={{
                minHeight: 200,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 1,
              }}
            >
              <Typography variant="subtitle1">{t('value.empty')}</Typography>
              <Typography variant="body2" color="text.secondary">
                {t('value.emptyHint')}
              </Typography>
            </Box>
          ) : (
            <>
              <Box sx={UNIFIED_TABLE_STYLES.embedded}>
                <Table size="small" stickyHeader sx={UNIFIED_TABLE_STYLES.getTableBase(false, true)}>
                  <TableHead>
                    <TableRow>
                      <TableCell>{t('columns.channel')}</TableCell>
                      <TableCell>{t('columns.type')}</TableCell>
                      <TableCell>{t('columns.instance')}</TableCell>
                      {isPlatformOwner && <TableCell>{t('columns.tenant')}</TableCell>}
                      <TableCell>{t('columns.unit')}</TableCell>
                      <TableCell align="right">{t('columns.usage')}</TableCell>
                      <TableCell align="right">{t('columns.remaining')}</TableCell>
                      <TableCell sx={{ minWidth: 160 }}>{t('columns.progress')}</TableCell>
                      <TableCell>{t('columns.state')}</TableCell>
                      <TableCell>{t('columns.action')}</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {items.map(item => (
                      <TableRow
                        key={item.chat_key}
                        hover
                        sx={{ cursor: 'pointer' }}
                        onClick={() => setDetailItem(item)}
                      >
                        <TableCell>
                          <Typography variant="body2" fontWeight={600}>
                            {item.channel_name || item.chat_key}
                          </Typography>
                          <Typography
                            variant="caption"
                            color="text.secondary"
                            component="div"
                            sx={{ wordBreak: 'break-all' }}
                          >
                            {item.chat_key}
                          </Typography>
                        </TableCell>
                        <TableCell>
                          {item.chat_type === 'group' ? t('filters.group') : t('filters.private')}
                        </TableCell>
                        <TableCell>{item.instance_key}</TableCell>
                        {isPlatformOwner && (
                          <TableCell>{item.tenant_name || t('detail.notSet')}</TableCell>
                        )}
                        <TableCell>{channelQuotaUnitLabel(t, item.unit)}</TableCell>
                        <TableCell align="right">
                          <Typography variant="body2" fontWeight={600}>
                            {formatQuotaInt(item.used)} / {formatQuotaLimit(t, item.limit)}
                          </Typography>
                          {item.bonus > 0 && (
                            <Chip
                              size="small"
                              label={t('value.bonus', { value: formatQuotaInt(item.bonus) })}
                              sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.success.main, true)}
                            />
                          )}
                        </TableCell>
                        <TableCell align="right">
                          {item.remaining === null
                            ? t('value.unlimited')
                            : formatQuotaInt(item.remaining)}
                        </TableCell>
                        <TableCell>
                          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                            {/* 超过 100% 也照实显示数字：用量确实可以超过额度（判定在回合开始前） */}
                            <LinearProgress
                              variant="determinate"
                              value={Math.min(item.percent, 100)}
                              color={channelQuotaProgressColor(item.state)}
                              sx={{ flex: 1, height: 6, borderRadius: 999 }}
                            />
                            <Typography variant="caption" color="text.secondary" sx={{ minWidth: 40 }}>
                              {item.percent}%
                            </Typography>
                          </Box>
                        </TableCell>
                        <TableCell>
                          <Chip
                            size="small"
                            label={t(`state.${item.state}`)}
                            sx={CHIP_VARIANTS.getCustomColorChip(
                              channelQuotaStateColor(theme, item.state),
                              true
                            )}
                          />
                        </TableCell>
                        <TableCell>{channelQuotaActionLabel(t, item.action)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
              <TablePaginationStyled
                component="div"
                count={total}
                page={page}
                onPageChange={(_event, next) => setPage(next)}
                rowsPerPage={PAGE_SIZE}
                rowsPerPageOptions={[PAGE_SIZE]}
                onRowsPerPageChange={() => undefined}
              />
            </>
          )}
        </CardContent>
      </Card>

      <ChannelQuotaDetailDialog item={detailItem} onClose={() => setDetailItem(null)} />
    </Box>
  )
}
