import { useMemo } from 'react'
import {
  Box,
  Card,
  Chip,
  CircularProgress,
  Divider,
  Drawer,
  Link,
  Stack,
  Typography,
  useTheme,
} from '@mui/material'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import ContentCopyRoundedIcon from '@mui/icons-material/ContentCopyRounded'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import ActionButton from '../common/ActionButton'
import IconActionButton from '../common/IconActionButton'
import { SimpleTooltip } from '../common/ThemedTooltip'
import PersonNotesSection from '../worldbook/PersonNotesSection'
import { labelsOrAccounts } from '../memory/personLabels'
import { useNotification } from '../../hooks/useNotification'
import { withDeepLinkInstance } from '../../hooks/useDeepLinkInstance'
import { statePath } from '../../router/routes'
import { affectionApi, type AffectionItem } from '../../services/api/affection'
import { memoryApi } from '../../services/api/memory'
import {
  stateTableApi,
  type StatePersonDetail,
  type StateScope,
} from '../../services/api/stateTable'
import { CARD_VARIANTS, CHIP_VARIANTS, MONO_CAPTION_STYLES } from '../../theme/variants'
import { formatListTime } from '../../utils/listTime'
import { affectionStageColor } from '../affection/affectionAtlasConfig'
import PersonMemorySection from './PersonMemorySection'

/**
 * 公共的右侧「人物卡」
 *
 * 同一个人散在好感、状态档案、记忆、世界书个人备注四处，后台原来要一页页找。这里不合并数据、
 * 不新增聚合接口：四段各自调现成接口，各自加载、各自失败——一段挂了只在它自己那张卡片里
 * 给一行错误和「重试」，别的段照常显示。
 *
 * 只处理「拿得到适配器 + 实例 + 账号」的情形；入口那边三样不全就不显示。
 */

export interface PersonCardDrawerProps {
  open: boolean
  onClose: () => void
  adapterKey: string
  instanceKey: string
  account: string
}

const AFFECTION_PAGE_SIZE = 20
const MAX_STATE_PRESETS = 3
const MAX_STATE_FIELDS = 6
const MAX_FIELD_VALUE_LENGTH = 40

interface StateProfile {
  id: number
  presetName: string
  data: Record<string, unknown>
}

/** 档案字段的值：标量截断，对象 / 数组不展开；空值给破折号 */
function fieldValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'string') {
    return value.length > MAX_FIELD_VALUE_LENGTH
      ? `${value.slice(0, MAX_FIELD_VALUE_LENGTH)}…`
      : value
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    const text = String(value)
    return text.length > MAX_FIELD_VALUE_LENGTH
      ? `${text.slice(0, MAX_FIELD_VALUE_LENGTH)}…`
      : text
  }
  return '…'
}

function isErrorWithMessage(error: unknown): error is Error {
  return error instanceof Error && Boolean(error.message)
}

export default function PersonCardDrawer({
  open,
  onClose,
  adapterKey,
  instanceKey,
  account,
}: PersonCardDrawerProps) {
  const theme = useTheme()
  const navigate = useNavigate()
  const notification = useNotification()
  const { t } = useTranslation('person')
  const { t: tCommon } = useTranslation('common')

  const enabled = open && Boolean(adapterKey && instanceKey && account)

  const affectionQuery = useQuery({
    queryKey: ['person-card', 'affection', adapterKey, instanceKey, account],
    queryFn: () =>
      affectionApi.getList({
        instance_key: instanceKey,
        person: account,
        page: 1,
        page_size: AFFECTION_PAGE_SIZE,
      }),
    enabled,
    retry: false,
  })

  const memoryQuery = useQuery({
    // 与 `PersonMemorySection` 同一个查询键：显示名要用这里的名字标签，
    // 两处同时开着时 React Query 只发一次请求。
    queryKey: ['person-card', 'memory', adapterKey, instanceKey, account],
    queryFn: () =>
      memoryApi.listParagraphs({
        person: account,
        instance_key: instanceKey,
        adapter_key: adapterKey,
        page: 1,
        page_size: 5,
        sort: 'create_time',
        order: 'desc',
      }),
    enabled,
    retry: false,
  })

  const affectionRows = useMemo(() => affectionQuery.data?.items ?? [], [affectionQuery.data])
  // 状态档案按「人」存（主账号）；入口给的可能是副账号，好感那一段返回的行里已经是主账号
  const stateUid = affectionRows[0]?.platform_userid || account

  const stateTargets = useMemo(() => {
    const seen = new Set<string>()
    const targets: Array<{ presetId: number; presetName: string; domainGroupId: number }> = []
    for (const row of affectionRows) {
      const key = `${row.preset_id}:${row.domain_group_id}`
      if (seen.has(key)) continue
      seen.add(key)
      targets.push({
        presetId: row.preset_id,
        presetName: row.preset_name || `#${row.preset_id}`,
        domainGroupId: row.domain_group_id,
      })
      if (targets.length === MAX_STATE_PRESETS) break
    }
    return targets
  }, [affectionRows])

  const stateScope = useMemo<StateScope>(
    () => ({ adapter_key: adapterKey, instance_key: instanceKey }),
    [adapterKey, instanceKey]
  )

  // 一个档案都没有、插件没启用、接口报错，对人物卡来说都是同一句话：没有状态档案。
  // 因此这里把失败吞成空列表，不把错误样式带进这一段。
  const stateQuery = useQuery({
    queryKey: ['person-card', 'state', adapterKey, instanceKey, stateUid, stateTargets],
    queryFn: async (): Promise<StateProfile[]> => {
      const profiles: StateProfile[] = []
      for (const target of stateTargets) {
        try {
          const lookup = await stateTableApi.lookupPerson(stateScope, {
            preset_id: target.presetId,
            uid: stateUid,
            domain_group_id: target.domainGroupId,
          })
          const detail: StatePersonDetail = await stateTableApi.getPerson(stateScope, lookup.id)
          profiles.push({ id: detail.id, presetName: target.presetName, data: detail.data ?? {} })
        } catch {
          // 这个人设下没有档案（或插件没启用）：跳过，不做第二种解释
        }
      }
      return profiles
    },
    enabled: enabled && stateTargets.length > 0,
    retry: false,
  })

  const displayName = useMemo(() => {
    for (const row of affectionRows) {
      const nickname = row.nickname.trim()
      if (nickname) return nickname
    }
    for (const row of memoryQuery.data?.items ?? []) {
      const matched = labelsOrAccounts(row.persons, row.person_accounts ?? []).find(
        person => person.account === account && person.display_name.trim()
      )
      if (matched) return matched.display_name.trim()
    }
    return account
  }, [affectionRows, memoryQuery.data, account])

  const openRecord = (row: AffectionItem) => {
    onClose()
    navigate(withDeepLinkInstance(`/affection?record=${row.id}`, row.instance_key || instanceKey))
  }

  const copyAccount = async () => {
    try {
      await navigator.clipboard.writeText(account)
      notification.success(t('accountCopied'))
    } catch {
      notification.error(t('copyFailed'))
    }
  }

  const openStateProfile = (profile: StateProfile) => {
    onClose()
    navigate(statePath('people', { person: String(profile.id) }))
  }

  const sectionCardSx = {
    ...(CARD_VARIANTS.default.styles as object),
    p: 1.5,
    display: 'flex',
    flexDirection: 'column',
    gap: 1,
  }

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      PaperProps={{
        sx: {
          width: { xs: '100%', sm: 560 },
          maxWidth: '100%',
          bgcolor: 'background.paper',
        },
      }}
    >
      <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        <Box
          sx={{
            position: 'sticky',
            top: 0,
            zIndex: 2,
            px: 2,
            py: 1.5,
            bgcolor: 'background.paper',
            borderBottom: '1px solid',
            borderColor: 'divider',
          }}
        >
          <Stack direction="row" alignItems="center" spacing={1}>
            <PersonOutlineIcon fontSize="small" color="primary" />
            <Typography variant="h6" fontWeight={700}>
              {t('title')}
            </Typography>
            <Box sx={{ flex: 1 }} />
            <IconActionButton onClick={onClose} aria-label={tCommon('actions.close')}>
              <CloseRoundedIcon />
            </IconActionButton>
          </Stack>
          <Typography variant="subtitle1" fontWeight={700} noWrap sx={{ mt: 0.5 }}>
            {displayName}
          </Typography>
          <Stack direction="row" spacing={0.25} alignItems="center" sx={{ minWidth: 0 }}>
            <Typography variant="caption" noWrap sx={{ ...(MONO_CAPTION_STYLES as object), minWidth: 0 }}>
              {account}
            </Typography>
            <SimpleTooltip title={t('copyAccount')}>
              <span>
                <IconActionButton
                  size="small"
                  onClick={() => void copyAccount()}
                  aria-label={t('copyAccount')}
                >
                  <ContentCopyRoundedIcon sx={{ fontSize: 14 }} />
                </IconActionButton>
              </span>
            </SimpleTooltip>
          </Stack>
        </Box>

        <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', px: 2, py: 2 }}>
          <Stack spacing={1.5}>
            {/* 好感：每个人设一行 */}
            <Card sx={sectionCardSx}>
              <Typography variant="subtitle2" fontWeight={700}>
                {t('affection.title')}
              </Typography>
              {affectionQuery.isPending ? (
                <Box sx={{ display: 'flex', justifyContent: 'center', py: 1 }}>
                  <CircularProgress size={20} />
                </Box>
              ) : affectionQuery.isError ? (
                <Stack direction="row" spacing={1} alignItems="center" sx={{ minWidth: 0 }}>
                  <Typography variant="body2" color="error.main" noWrap sx={{ flex: 1, minWidth: 0 }}>
                    {isErrorWithMessage(affectionQuery.error)
                      ? affectionQuery.error.message
                      : t('error')}
                  </Typography>
                  <ActionButton
                    tone="ghost"
                    size="small"
                    onClick={() => void affectionQuery.refetch()}
                  >
                    {t('retry')}
                  </ActionButton>
                </Stack>
              ) : affectionRows.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  {t('affection.empty')}
                </Typography>
              ) : (
                <Stack spacing={0.5}>
                  {affectionRows.map(row => (
                    <Box
                      key={row.id}
                      role="button"
                      tabIndex={0}
                      onClick={() => openRecord(row)}
                      onKeyDown={event => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          openRecord(row)
                        }
                      }}
                      sx={{
                        px: 0.75,
                        py: 0.6,
                        borderRadius: 1,
                        cursor: 'pointer',
                        '&:hover': { bgcolor: 'action.hover' },
                      }}
                    >
                      <Stack direction="row" spacing={1} alignItems="center" sx={{ minWidth: 0 }}>
                        <Typography variant="body2" fontWeight={600} noWrap sx={{ flex: 1, minWidth: 0 }}>
                          {row.preset_name || `#${row.preset_id}`}
                        </Typography>
                        <Chip
                          size="small"
                          label={row.stage_label}
                          sx={CHIP_VARIANTS.getCustomColorChip(
                            affectionStageColor(theme, row.stage),
                            true
                          )}
                        />
                        <Typography
                          variant="body2"
                          fontWeight={700}
                          sx={{ fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}
                        >
                          {row.score}
                        </Typography>
                      </Stack>
                      <Typography
                        variant="caption"
                        color="text.secondary"
                        noWrap
                        sx={{ display: 'block', mt: 0.25 }}
                      >
                        {t('affection.callName', { name: row.call_name || '—' })}
                        {' · '}
                        {t('affection.lastSeen')} {formatListTime(row.last_interact_at ?? row.update_time)}
                      </Typography>
                    </Box>
                  ))}
                </Stack>
              )}
            </Card>

            {/* 状态档案：跟着好感那一段里出现的人设走，最多前 3 个 */}
            <Card sx={sectionCardSx}>
              <Typography variant="subtitle2" fontWeight={700}>
                {t('state.title')}
              </Typography>
              {affectionQuery.isPending || (stateTargets.length > 0 && stateQuery.isPending) ? (
                <Box sx={{ display: 'flex', justifyContent: 'center', py: 1 }}>
                  <CircularProgress size={20} />
                </Box>
              ) : (stateQuery.data ?? []).length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  {t('state.empty')}
                </Typography>
              ) : (
                <Stack spacing={1.25} divider={<Divider flexItem />}>
                  {(stateQuery.data ?? []).map(profile => (
                    <Box key={profile.id}>
                      <Stack direction="row" alignItems="center" spacing={1}>
                        <Typography variant="caption" color="text.secondary" sx={{ flex: 1 }}>
                          {profile.presetName}
                        </Typography>
                        <Link
                          component="button"
                          type="button"
                          variant="caption"
                          underline="hover"
                          onClick={() => openStateProfile(profile)}
                        >
                          {t('state.open')}
                        </Link>
                      </Stack>
                      <Box
                        sx={{
                          display: 'grid',
                          gridTemplateColumns: 'minmax(96px, auto) minmax(0, 1fr)',
                          columnGap: 1.5,
                          rowGap: 0.25,
                          mt: 0.5,
                        }}
                      >
                        {Object.entries(profile.data)
                          .slice(0, MAX_STATE_FIELDS)
                          .map(([key, value]) => (
                            <Box key={key} sx={{ display: 'contents' }}>
                              <Typography variant="caption" color="text.secondary" noWrap>
                                {key}
                              </Typography>
                              <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>
                                {fieldValue(value)}
                              </Typography>
                            </Box>
                          ))}
                      </Box>
                    </Box>
                  ))}
                </Stack>
              )}
            </Card>

            {enabled && (
              <PersonMemorySection
                account={account}
                adapterKey={adapterKey}
                instanceKey={instanceKey}
                onNavigate={onClose}
              />
            )}

            {enabled && (
              <Card sx={sectionCardSx}>
                <PersonNotesSection
                  personAccount={account}
                  displayName={displayName}
                  instanceKey={instanceKey}
                  adapterKey={adapterKey}
                  onCloseDetail={onClose}
                />
              </Card>
            )}
          </Stack>
        </Box>
      </Box>
    </Drawer>
  )
}
