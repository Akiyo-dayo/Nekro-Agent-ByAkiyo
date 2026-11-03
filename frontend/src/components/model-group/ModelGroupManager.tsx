/**
 * 模型组管理（唯一实现）
 *
 * 「系统配置 → 模型管理 → 基础模型组」与「总后台 → 模型组」渲染的是同一个组件。
 * 两处原本各有一套列表与新建表单，读写的却是同一张 `model_group` 表，
 * 差别只是一处带归属信息、一处字段更全，合并后不再有第二份实现。
 *
 * 数据源统一为 `/console/model-groups`：它是唯一带归属与凭据可见性的读接口，
 * 对平台方返回全部模型组与明文凭据，对租户只返回可见的组且平台组凭据恒为掩码。
 *
 * ## 凭据隔离
 *
 * 可编辑性与凭据可见性一律以后端返回的 `editable` / `credential_visible` 为准，前端不做任何推断。
 * 「接口地址」列同样受掩码保护——它暴露平台方接的是哪家上游，契约里与凭据同等对待。
 *
 * ## 批量删除
 *
 * 只能勾选**当前筛选结果里**可删除的行（`editable`、非只读身份、非默认组）。勾选后再改筛选，
 * 被筛掉的行不计入选中数、也不会被删除——删除是不可恢复的操作，不能删掉一个用户此刻看不见的组。
 * （逐行勾选的项在筛选改回来时会恢复勾选；表头全选 / 取消全选与「清空选择」则整体替换选中集合。）
 * 确认框里的名单在打开那一刻定格，平台方还能看到哪些是租户自建的组。
 * 前端的可勾选判定只是交互便利，后端逐个组重新判定权限并如实返回没删成的组与原因。
 */

import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Alert,
  Box,
  Checkbox,
  Chip,
  CircularProgress,
  Link,
  Paper,
  Stack,
  SxProps,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Theme,
  Tooltip,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import {
  Add as AddIcon,
  Brush as BrushIcon,
  Chat as ChatIcon,
  Code as CodeIcon,
  ContentCopy as ContentCopyIcon,
  Delete as DeleteIcon,
  DeleteSweep as DeleteSweepIcon,
  Edit as EditIcon,
  EmojiObjects as EmojiObjectsIcon,
  Image as ImageIcon,
  Launch as LaunchIcon,
  Lock as LockIcon,
  NetworkCheck as NetworkCheckIcon,
  Psychology as PsychologyIcon,
  Refresh as RefreshIcon,
} from '@mui/icons-material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import FilterSelect from '../common/FilterSelect'
import IconActionButton from '../common/IconActionButton'
import NekroDialog from '../common/NekroDialog'
import QueryErrorState from '../common/QueryErrorState'
import SearchField from '../common/SearchField'
import { useIdentity } from '../../hooks/useIdentity'
import { useNotification } from '../../hooks/useNotification'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'
import { unifiedConfigApi, ModelGroupTestItem } from '../../services/api/unified-config'
import {
  MODEL_GROUP_OWNERSHIPS,
  TENANT_QUERY_KEYS,
  isTenantEndpointUnavailable,
  tenantApi,
  type ModelGroupInfo,
  type ModelGroupOwnership,
  type ModelGroupUpsertRequest,
} from '../../services/api/tenant'
import ModelGroupFormDialog, { type ModelGroupFormMode } from './ModelGroupFormDialog'
import ModelGroupReferenceNotice from './ModelGroupReferenceNotice'
import { describeTestFailure } from './testOutcome'

/** 默认模型组被 `USE_MODEL_GROUP` 等配置项当作兜底，删掉会让全局配置指空 */
const DEFAULT_GROUP_NAME = 'default'

/** 批量删除部分失败时，通知里最多点名几个组 */
const MAX_FAILURES_IN_NOTICE = 5

const MODEL_TYPE_ICONS: Record<string, React.ReactElement> = {
  Chat: <ChatIcon fontSize="small" />,
  Code: <CodeIcon fontSize="small" />,
  Brush: <BrushIcon fontSize="small" />,
  EmojiObjects: <EmojiObjectsIcon fontSize="small" />,
}

const isOwnership = (value: string | null): value is ModelGroupOwnership =>
  !!value && MODEL_GROUP_OWNERSHIPS.includes(value as ModelGroupOwnership)

interface FormTarget {
  mode: ModelGroupFormMode
  source: ModelGroupInfo | null
}

export interface ModelGroupManagerProps {
  /** 是否显示「支持任意 OpenAI 兼容供应商」的顶部提示 */
  showProviderHint?: boolean
}

export default function ModelGroupManager({ showProviderHint = false }: ModelGroupManagerProps) {
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('md'))
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('settings')
  const { t: tTenant } = useTranslation('tenant')
  const { isReadOnly, isPlatformOwner } = useIdentity()

  const [searchParams, setSearchParams] = useSearchParams()
  const ownershipFilter = isOwnership(searchParams.get('ownership'))
    ? (searchParams.get('ownership') as ModelGroupOwnership)
    : 'all'
  const search = searchParams.get('search') ?? ''

  const [formTarget, setFormTarget] = useState<FormTarget | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ModelGroupInfo | null>(null)
  const [selectedNames, setSelectedNames] = useState<string[]>([])
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false)
  /**
   * 确认框里要删的组，打开时定格。关闭时不清空：对话框淡出动画期间正文仍在渲染，
   * 清空的话会闪出一句「确定要删除以下 0 个模型组吗」
   */
  const [batchDeleteTargets, setBatchDeleteTargets] = useState<ModelGroupInfo[]>([])
  /** 删除确认框里的引用检查是否还在进行；进行中禁用确认，免得提醒还没出来就删了 */
  const [checkingReferences, setCheckingReferences] = useState(false)
  const [testingGroups, setTestingGroups] = useState<Set<string>>(new Set())
  const [testResults, setTestResults] = useState<Record<string, ModelGroupTestItem>>({})

  const updateQuery = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams)
    if (!value || value === 'all') {
      next.delete(key)
    } else {
      next.set(key, value)
    }
    setSearchParams(next, { replace: true })
  }

  const groupsQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.modelGroups,
    queryFn: () => tenantApi.listModelGroups(),
    staleTime: 30_000,
    retry: false,
  })

  const { data: modelTypes = [] } = useQuery({
    queryKey: ['model-types'],
    queryFn: () => unifiedConfigApi.getModelTypes(),
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: TENANT_QUERY_KEYS.modelGroups })
    // 旧的 `/config/model-groups` 视图（配置项里的模型组下拉等）同样要跟着刷新
    void queryClient.invalidateQueries({ queryKey: ['model-groups'] })
  }

  const deleteMutation = useMutation({
    mutationFn: (name: string) => tenantApi.deleteModelGroup(name),
    onSuccess: (_data, name) => {
      notification.success(t('modelGroup.notifications.deleteSuccess', { name }))
      setDeleteTarget(null)
      invalidate()
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const batchDeleteMutation = useMutation({
    mutationFn: (names: string[]) => tenantApi.batchDeleteModelGroups(names),
    onSuccess: result => {
      // 后端逐个结算：有没删成的组时用告警并说明原因，避免「报了成功但其实没删完」。
      // 只点名前几个，其余折成「等 N 个」：一条通知里塞上百个组名没人读得完
      if (result.failed.length > 0) {
        const listed = result.failed.slice(0, MAX_FAILURES_IN_NOTICE)
        const rest = result.failed.length - listed.length
        const details = listed
          .map(item =>
            t('modelGroup.batchDelete.failureItem', {
              name: item.name,
              reason: t(`modelGroup.batchDelete.reasons.${item.reason}`),
            })
          )
          .join(t('modelGroup.batchDelete.failureSeparator'))
        notification.warning(
          t('modelGroup.notifications.batchDeletePartial', {
            succeeded: result.succeeded.length,
            failed: result.failed.length,
            details:
              rest > 0
                ? `${details}${t('modelGroup.batchDelete.failureMore', { count: rest })}`
                : details,
          })
        )
      } else {
        notification.success(
          t('modelGroup.notifications.batchDeleteSuccess', { count: result.succeeded.length })
        )
      }
      setSelectedNames([])
      setBatchDeleteOpen(false)
      invalidate()
    },
    // 请求失败时也刷新：服务端可能已经删掉了一部分，列表不能还显示着它们
    onError: (error: Error) => {
      notification.error(error.message)
      invalidate()
    },
  })

  const handleSubmit = async (
    name: string,
    payload: ModelGroupUpsertRequest,
    isCreate: boolean
  ) => {
    if (isCreate) {
      await tenantApi.createModelGroup({ ...payload, name })
    } else {
      await tenantApi.updateModelGroup(name, payload)
    }
    notification.success(
      isCreate
        ? tTenant('modelGroups.createSuccess')
        : tTenant('modelGroups.updateSuccess')
    )
    invalidate()
  }

  const runTest = async (groupName: string) => {
    if (testingGroups.has(groupName)) return
    setTestingGroups(prev => new Set([...prev, groupName]))
    try {
      const items = await unifiedConfigApi.testModelGroups([groupName])
      const item = items[0]
      if (item) {
        setTestResults(prev => ({ ...prev, [groupName]: item }))
        if (item.success) {
          notification.success(t('modelGroup.notifications.testPassed'))
        } else {
          notification.warning(t('modelGroup.notifications.testFailed'))
        }
      }
    } catch (err) {
      notification.error(
        err instanceof Error ? err.message : t('modelGroup.notifications.testError')
      )
    } finally {
      setTestingGroups(prev => {
        const next = new Set(prev)
        next.delete(groupName)
        return next
      })
    }
  }

  const groups = useMemo(() => {
    const items = groupsQuery.data?.items ?? []
    const keyword = search.trim().toLowerCase()
    return items.filter(group => {
      if (ownershipFilter !== 'all' && group.ownership !== ownershipFilter) return false
      if (!keyword) return true
      return [group.name, group.chat_model, group.base_url, group.model_type].some(value =>
        (value || '').toLowerCase().includes(keyword)
      )
    })
  }, [groupsQuery.data, search, ownershipFilter])

  const ownershipOptions = useMemo(
    () => [
      { value: 'all', label: tTenant('modelGroups.ownershipAll') },
      ...MODEL_GROUP_OWNERSHIPS.map(ownership => ({
        value: ownership,
        label: tTenant(`modelGroups.ownership.${ownership}`),
      })),
    ],
    [tTenant]
  )

  const existingNames = useMemo(
    () => (groupsQuery.data?.items ?? []).map(group => group.name),
    [groupsQuery.data]
  )

  /** 与单行删除按钮的可用条件一致 */
  const isDeletable = useCallback(
    (group: ModelGroupInfo) => group.editable && !isReadOnly && group.name !== DEFAULT_GROUP_NAME,
    [isReadOnly]
  )

  /** 当前筛选结果里可删除的组名；勾选只在这个范围内生效 */
  const deletableNames = useMemo(
    () => groups.filter(isDeletable).map(group => group.name),
    [groups, isDeletable]
  )

  /** 列表刷新或筛选变化后，已不在可删除范围内的组自动退出选中集合 */
  const activeSelection = useMemo(
    () => selectedNames.filter(name => deletableNames.includes(name)),
    [deletableNames, selectedNames]
  )
  const allDeletableSelected =
    deletableNames.length > 0 && activeSelection.length === deletableNames.length
  const someDeletableSelected = activeSelection.length > 0 && !allDeletableSelected

  const batchDeleteNames = useMemo(
    () => batchDeleteTargets.map(group => group.name),
    [batchDeleteTargets]
  )

  const openBatchDelete = () => {
    setBatchDeleteTargets(groups.filter(group => activeSelection.includes(group.name)))
    setBatchDeleteOpen(true)
  }

  const toggleSelected = (name: string, checked: boolean) => {
    setSelectedNames(previous => {
      if (checked) {
        return previous.includes(name) ? previous : [...previous, name]
      }
      return previous.filter(item => item !== name)
    })
  }

  const getModelTypeLabel = (type: string | undefined) =>
    t(`modelGroup.types.${(type || 'chat') as 'chat' | 'embedding' | 'draw'}`, {
      defaultValue: modelTypes.find(item => item.value === type)?.label || type || 'chat',
    })

  const getModelTypeThemeColor = (type: string | undefined): string => {
    const color = modelTypes.find(item => item.value === type)?.color
    switch (color) {
      case 'secondary':
        return theme.palette.secondary.main
      case 'error':
        return theme.palette.error.main
      case 'info':
        return theme.palette.info.main
      case 'success':
        return theme.palette.success.main
      case 'warning':
        return theme.palette.warning.main
      default:
        return theme.palette.primary.main
    }
  }

  const getModelTypeIcon = (type: string | undefined) => {
    const iconName = modelTypes.find(item => item.value === type)?.icon || 'EmojiObjects'
    return MODEL_TYPE_ICONS[iconName] || <EmojiObjectsIcon fontSize="small" />
  }

  const getProviderOrigin = (url: string) => {
    try {
      const parsed = new URL(url)
      return `${parsed.protocol}//${parsed.host}`
    } catch {
      return ''
    }
  }

  const renderTestTooltip = (result: ModelGroupTestItem) => (
    <Box sx={{ maxWidth: 320 }}>
      <Typography variant="caption" sx={{ display: 'block', fontWeight: 700, mb: 0.5 }}>
        {result.success
          ? t('modelGroup.health.tooltipPassed')
          : t('modelGroup.health.tooltipFailed')}
      </Typography>
      <Typography variant="caption" sx={{ display: 'block' }}>
        {t('modelGroup.health.tooltipModel', {
          model: result.used_model || result.model_name || '-',
        })}
      </Typography>
      <Typography variant="caption" sx={{ display: 'block' }}>
        {t('modelGroup.health.tooltipLatency', { ms: result.latency_ms })}
      </Typography>
      {result.success ? (
        <>
          {((result.input_tokens ?? 0) > 0 || (result.output_tokens ?? 0) > 0) && (
            <Typography variant="caption" sx={{ display: 'block' }}>
              {t('modelGroup.health.tooltipTokens', {
                input: result.input_tokens ?? 0,
                output: result.output_tokens ?? 0,
              })}
            </Typography>
          )}
          {result.response_text && (
            <Typography
              variant="caption"
              sx={{
                display: 'block',
                mt: 0.5,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {t('modelGroup.health.tooltipReply', { text: result.response_text.trim() })}
            </Typography>
          )}
        </>
      ) : (
        <>
          <Typography
            variant="caption"
            sx={{ display: 'block', mt: 0.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}
          >
            {t('modelGroup.health.tooltipError', {
              error: describeTestFailure(result, t, t('modelGroup.health.failed')),
            })}
          </Typography>
          {typeof result.status_code === 'number' && (
            <Typography variant="caption" sx={{ display: 'block' }}>
              {t('modelGroup.health.tooltipStatus', { status: result.status_code })}
            </Typography>
          )}
        </>
      )}
    </Box>
  )

  const isEndpointMissing = groupsQuery.isError && isTenantEndpointUnavailable(groupsQuery.error)
  const hasGroupsLoaded = (groupsQuery.data?.items.length ?? 0) > 0

  return (
    <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {showProviderHint && (
        <Alert severity="info" sx={{ mb: 1.5, flexShrink: 0 }}>
          {t('modelGroup.alert.providerSupportBefore')}
          <Link href="https://api.nekro.ai" target="_blank" rel="noopener noreferrer">
            {t('modelGroup.alert.providerSupportLink')}
          </Link>
          {t('modelGroup.alert.providerSupportAfter')}
        </Alert>
      )}

      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          mb: 1.5,
          // 留出上边距，否则筛选框浮在边框上的标签会被容器顶边裁掉
          pt: 0.5,
          flexShrink: 0,
          gap: 1,
          flexWrap: 'wrap',
        }}
      >
        <SearchField
          size="small"
          value={search}
          onChange={value => updateQuery('search', value)}
          onClear={() => updateQuery('search', '')}
          placeholder={t('modelGroup.actions.searchPlaceholder')}
          sx={{ flex: 1, minWidth: 200 }}
        />
        <FilterSelect
          label={tTenant('modelGroups.ownershipFilter')}
          value={ownershipFilter}
          options={ownershipOptions}
          onChange={value => updateQuery('ownership', value)}
          fullWidth={false}
          sx={{ minWidth: 150 }}
        />
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<RefreshIcon />}
          onClick={() => void groupsQuery.refetch()}
          sx={{ height: 40, whiteSpace: 'nowrap', flexShrink: 0 }}
        >
          {tTenant('actions.refresh')}
        </ActionButton>
        {activeSelection.length > 0 && (
          <>
            <ActionButton
              tone="ghost"
              size="small"
              onClick={() => setSelectedNames([])}
              sx={{ height: 40, whiteSpace: 'nowrap', flexShrink: 0 }}
            >
              {t('modelGroup.actions.clearSelection')}
            </ActionButton>
            <ActionButton
              tone="danger"
              size="small"
              startIcon={<DeleteSweepIcon />}
              onClick={openBatchDelete}
              sx={{ height: 40, whiteSpace: 'nowrap', flexShrink: 0 }}
            >
              {t('modelGroup.actions.batchDelete', { count: activeSelection.length })}
            </ActionButton>
          </>
        )}
        {!isReadOnly && (
          <ActionButton
            tone="primary"
            startIcon={<AddIcon />}
            size="small"
            disabled={isEndpointMissing}
            onClick={() => setFormTarget({ mode: 'create', source: null })}
            sx={{ height: 40, whiteSpace: 'nowrap', flexShrink: 0 }}
          >
            {isPlatformOwner
              ? t('modelGroup.actions.createPlatformBtn')
              : t('modelGroup.actions.createBtn')}
          </ActionButton>
        )}
      </Box>

      {isEndpointMissing && (
        <Alert severity="info" variant="outlined" sx={{ mb: 1.5, flexShrink: 0 }}>
          <Typography variant="subtitle2" fontWeight={700}>
            {tTenant('unavailable.title')}
          </Typography>
          <Typography variant="body2">{tTenant('unavailable.modelGroups')}</Typography>
        </Alert>
      )}

      {/* ⚠️ 这里**刻意不再画**一条「模型组加载失败 + 重试」的横幅（同 `TenantListPage`
          / `TenantMemberTable` 的先例）：表格区已由 `QueryErrorState` 接管同一个条件，
          两条一起画时 403 之下会同时出现「加载失败（可重试）」与「你无权查看（不给重试）」
          两种说法，而那个重试按钮点了永远是同一个 403。一个失败只说一次。 */}

      <Paper
        elevation={0}
        sx={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0,
          overflow: 'hidden',
          ...(UNIFIED_TABLE_STYLES.paper as SxProps<Theme>),
        }}
      >
        <TableContainer
          sx={{
            flex: 1,
            overflow: 'auto',
            ...(UNIFIED_TABLE_STYLES.scrollbar as SxProps<Theme>),
          }}
        >
          {groupsQuery.isLoading ? (
            <Box
              sx={{
                minHeight: 240,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <CircularProgress />
            </Box>
          ) : groupsQuery.isError && !isEndpointMissing ? (
            /* 失败时不要再画「还没有模型组」的空态：那句空态与失败会同时出现，读起来像
               「请求失败了，另外你确实一个模型组都没有」——后半句是编的。403（只读成员
               敲 `/settings/models` 或 `/console/model-groups` 的 URL）与其它失败由
               `QueryErrorState` 分开说，空态只在**取到了、真的是空**时才成立 */
            <QueryErrorState
              error={groupsQuery.error}
              onRetry={() => void groupsQuery.refetch()}
            />
          ) : groups.length === 0 ? (
            <Box
              sx={{
                minHeight: 240,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 1,
              }}
            >
              <Typography variant="subtitle1">
                {hasGroupsLoaded
                  ? tTenant('modelGroups.noResults')
                  : tTenant('modelGroups.empty')}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {hasGroupsLoaded
                  ? tTenant('modelGroups.noResultsHint')
                  : tTenant('modelGroups.emptyHint')}
              </Typography>
            </Box>
          ) : (
            <Table
              stickyHeader
              size={isSmall ? 'small' : 'medium'}
              // 固定表格布局：列宽由下面的 width 决定，长地址不会把整行撑开去压别的列
              sx={{
                minWidth: (isMobile ? 720 : 1080) + (isReadOnly ? 0 : 48),
                tableLayout: 'fixed',
              }}
            >
              <TableHead>
                <TableRow>
                  {!isReadOnly && (
                    <TableCell
                      padding="checkbox"
                      sx={{ width: 48, ...(UNIFIED_TABLE_STYLES.header as SxProps<Theme>) }}
                    >
                      <Checkbox
                        size="small"
                        checked={allDeletableSelected}
                        indeterminate={someDeletableSelected}
                        disabled={deletableNames.length === 0}
                        inputProps={{ 'aria-label': t('modelGroup.batchDelete.selectAllAria') }}
                        onChange={event =>
                          setSelectedNames(event.target.checked ? deletableNames : [])
                        }
                      />
                    </TableCell>
                  )}
                  <TableCell
                    sx={{ width: 150, ...(UNIFIED_TABLE_STYLES.header as SxProps<Theme>) }}
                  >
                    {t('modelGroup.table.groupName')}
                  </TableCell>
                  <TableCell
                    sx={{ width: 110, ...(UNIFIED_TABLE_STYLES.header as SxProps<Theme>) }}
                  >
                    {tTenant('modelGroups.columns.ownership')}
                  </TableCell>
                  <TableCell
                    sx={{ width: 180, ...(UNIFIED_TABLE_STYLES.header as SxProps<Theme>) }}
                  >
                    {t('modelGroup.table.modelName')}
                  </TableCell>
                  <TableCell
                    sx={{ width: 110, ...(UNIFIED_TABLE_STYLES.header as SxProps<Theme>) }}
                  >
                    {t('modelGroup.table.modelType')}
                  </TableCell>
                  <TableCell
                    sx={{ width: 240, ...(UNIFIED_TABLE_STYLES.header as SxProps<Theme>) }}
                  >
                    {t('modelGroup.table.apiAddress')}
                  </TableCell>
                  <TableCell
                    sx={{ width: 130, ...(UNIFIED_TABLE_STYLES.header as SxProps<Theme>) }}
                  >
                    {tTenant('modelGroups.columns.credential')}
                  </TableCell>
                  {!isMobile && (
                    <TableCell
                      sx={{ width: 170, ...(UNIFIED_TABLE_STYLES.header as SxProps<Theme>) }}
                    >
                      {t('modelGroup.table.features')}
                    </TableCell>
                  )}
                  <TableCell
                    sx={{ width: 100, ...(UNIFIED_TABLE_STYLES.header as SxProps<Theme>) }}
                  >
                    {t('modelGroup.table.health')}
                  </TableCell>
                  <TableCell
                    sx={{ width: 180, ...(UNIFIED_TABLE_STYLES.header as SxProps<Theme>) }}
                  >
                    {t('modelGroup.table.actions')}
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {groups.map(group => {
                  const canEdit = group.editable && !isReadOnly
                  const lockedTooltip = isReadOnly
                    ? tTenant('modelGroups.readOnlyRoleTooltip')
                    : tTenant('modelGroups.readOnlyTooltip')
                  const testable = group.model_type === 'chat' || group.model_type === 'embedding'
                  const providerOrigin = getProviderOrigin(group.base_url)

                  return (
                    <TableRow key={group.name} hover sx={UNIFIED_TABLE_STYLES.row as SxProps<Theme>}>
                      {!isReadOnly && (
                        <TableCell
                          padding="checkbox"
                          sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}
                        >
                          <Checkbox
                            size="small"
                            checked={activeSelection.includes(group.name)}
                            disabled={!isDeletable(group)}
                            inputProps={{
                              'aria-label': t('modelGroup.batchDelete.selectRowAria', {
                                name: group.name,
                              }),
                            }}
                            onChange={event => toggleSelected(group.name, event.target.checked)}
                          />
                        </TableCell>
                      )}
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        <Tooltip title={group.name}>
                          <Typography
                            variant="subtitle2"
                            fontWeight={group.name === DEFAULT_GROUP_NAME ? 700 : 600}
                            noWrap
                          >
                            {group.name}
                          </Typography>
                        </Tooltip>
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        <Tooltip
                          title={
                            group.ownership === 'platform'
                              ? tTenant('modelGroups.ownershipPlatformHint')
                              : tTenant('modelGroups.ownershipTenantHint')
                          }
                        >
                          <Chip
                            size="small"
                            label={tTenant(`modelGroups.ownership.${group.ownership}`)}
                            sx={CHIP_VARIANTS.getCustomColorChip(
                              group.ownership === 'platform'
                                ? theme.palette.info.main
                                : theme.palette.primary.main,
                              true
                            )}
                          />
                        </Tooltip>
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        <Tooltip title={group.chat_model || '—'}>
                          <Typography variant="body2" noWrap>
                            {group.chat_model || '—'}
                          </Typography>
                        </Tooltip>
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        <Chip
                          icon={getModelTypeIcon(group.model_type)}
                          label={getModelTypeLabel(group.model_type)}
                          size="small"
                          variant="outlined"
                          sx={CHIP_VARIANTS.getCustomColorChip(
                            getModelTypeThemeColor(group.model_type),
                            true
                          )}
                        />
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        {/* 超长地址一律截断并在悬停时给出全文，不允许溢出去压住右边的凭据列 */}
                        <Tooltip
                          title={
                            <Box sx={{ maxWidth: 420, wordBreak: 'break-all' }}>
                              {group.base_url || '—'}
                            </Box>
                          }
                        >
                          <Typography
                            variant="caption"
                            color="text.secondary"
                            sx={{
                              display: 'block',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {group.base_url || '—'}
                          </Typography>
                        </Tooltip>
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        {group.credential_visible ? (
                          <Typography variant="caption" color="text.secondary" noWrap>
                            {tTenant('modelGroups.credentialVisible')}
                          </Typography>
                        ) : (
                          <Tooltip title={tTenant('modelGroups.credentialHiddenTooltip')}>
                            <Chip
                              size="small"
                              icon={<LockIcon sx={{ fontSize: 14 }} />}
                              label={tTenant('modelGroups.credentialHidden')}
                              sx={CHIP_VARIANTS.getCustomColorChip(
                                theme.palette.text.secondary,
                                true
                              )}
                            />
                          </Tooltip>
                        )}
                      </TableCell>
                      {!isMobile && (
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                          <Stack direction="row" spacing={0.5}>
                            <Tooltip
                              title={
                                group.config.ENABLE_VISION
                                  ? t('modelGroup.chips.visionSupported')
                                  : t('modelGroup.chips.visionUnsupported')
                              }
                            >
                              <Chip
                                icon={<ImageIcon fontSize="small" />}
                                label={t('modelGroup.chips.vision')}
                                size="small"
                                variant="outlined"
                                sx={CHIP_VARIANTS.getCustomColorChip(
                                  group.config.ENABLE_VISION
                                    ? theme.palette.primary.main
                                    : theme.palette.text.secondary,
                                  true
                                )}
                              />
                            </Tooltip>
                            <Tooltip
                              title={
                                group.config.ENABLE_COT
                                  ? t('modelGroup.chips.cotEnabled')
                                  : t('modelGroup.chips.cotDisabled')
                              }
                            >
                              <Chip
                                icon={<PsychologyIcon fontSize="small" />}
                                label={t('modelGroup.chips.cot')}
                                size="small"
                                variant="outlined"
                                sx={CHIP_VARIANTS.getCustomColorChip(
                                  group.config.ENABLE_COT
                                    ? theme.palette.secondary.main
                                    : theme.palette.text.secondary,
                                  true
                                )}
                              />
                            </Tooltip>
                          </Stack>
                        </TableCell>
                      )}
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        {!testable ? (
                          <Typography variant="caption" color="text.secondary">
                            -
                          </Typography>
                        ) : testingGroups.has(group.name) ? (
                          <Chip
                            size="small"
                            variant="outlined"
                            icon={<CircularProgress size={12} />}
                            label={t('modelGroup.health.testing')}
                            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true)}
                          />
                        ) : testResults[group.name] ? (
                          <Tooltip title={renderTestTooltip(testResults[group.name])} arrow>
                            <Chip
                              size="small"
                              variant="outlined"
                              label={
                                testResults[group.name].success
                                  ? t('modelGroup.health.ok', {
                                      ms: testResults[group.name].latency_ms,
                                    })
                                  : t('modelGroup.health.failed')
                              }
                              sx={CHIP_VARIANTS.getCustomColorChip(
                                testResults[group.name].success
                                  ? theme.palette.success.main
                                  : theme.palette.error.main,
                                true
                              )}
                            />
                          </Tooltip>
                        ) : (
                          <Typography variant="caption" color="text.secondary">
                            {t('modelGroup.health.notTested')}
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        <Stack direction="row" spacing={0.5} justifyContent="flex-start">
                          <Tooltip title={t('modelGroup.tooltips.visitProvider')} arrow>
                            <span>
                              <IconActionButton
                                tone="primary"
                                onClick={() => window.open(providerOrigin, '_blank')}
                                size="small"
                                disabled={!providerOrigin}
                                sx={{ p: 0.5 }}
                              >
                                <LaunchIcon fontSize="small" />
                              </IconActionButton>
                            </span>
                          </Tooltip>
                          <Tooltip
                            title={
                              testable
                                ? t('modelGroup.tooltips.test')
                                : t('modelGroup.tooltips.testOnlyChat')
                            }
                            arrow
                          >
                            <span>
                              <IconActionButton
                                tone="primary"
                                onClick={() => void runTest(group.name)}
                                size="small"
                                disabled={
                                  !testable || isReadOnly || testingGroups.has(group.name)
                                }
                                sx={{ p: 0.5 }}
                              >
                                {testingGroups.has(group.name) ? (
                                  <CircularProgress size={16} />
                                ) : (
                                  <NetworkCheckIcon fontSize="small" />
                                )}
                              </IconActionButton>
                            </span>
                          </Tooltip>
                          <Tooltip
                            title={canEdit ? t('modelGroup.tooltips.edit') : lockedTooltip}
                            arrow
                          >
                            <span>
                              <IconActionButton
                                onClick={() => setFormTarget({ mode: 'edit', source: group })}
                                size="small"
                                disabled={!canEdit}
                                sx={{ p: 0.5 }}
                              >
                                <EditIcon fontSize="small" />
                              </IconActionButton>
                            </span>
                          </Tooltip>
                          <Tooltip title={t('modelGroup.tooltips.copy')} arrow>
                            <span>
                              <IconActionButton
                                onClick={() => setFormTarget({ mode: 'copy', source: group })}
                                size="small"
                                disabled={isReadOnly}
                                sx={{ p: 0.5 }}
                              >
                                <ContentCopyIcon fontSize="small" />
                              </IconActionButton>
                            </span>
                          </Tooltip>
                          <Tooltip
                            title={
                              group.name === DEFAULT_GROUP_NAME
                                ? t('modelGroup.tooltips.defaultDelete')
                                : canEdit
                                  ? t('modelGroup.tooltips.delete')
                                  : lockedTooltip
                            }
                            arrow
                          >
                            <span>
                              <IconActionButton
                                tone="danger"
                                onClick={() => setDeleteTarget(group)}
                                size="small"
                                disabled={!canEdit || group.name === DEFAULT_GROUP_NAME}
                                sx={{ p: 0.5 }}
                              >
                                <DeleteIcon fontSize="small" />
                              </IconActionButton>
                            </span>
                          </Tooltip>
                        </Stack>
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </TableContainer>
      </Paper>

      <ModelGroupFormDialog
        // 每次打开都基于当前目标重建，避免上一次的表单状态被复用
        key={`${formTarget?.mode ?? 'none'}-${formTarget?.source?.name ?? 'new'}`}
        open={Boolean(formTarget)}
        mode={formTarget?.mode ?? 'create'}
        source={formTarget?.source ?? null}
        existingNames={existingNames}
        isPlatformOwner={isPlatformOwner}
        onClose={() => setFormTarget(null)}
        onSubmit={handleSubmit}
      />

      <NekroDialog
        open={Boolean(deleteTarget)}
        onClose={() => setDeleteTarget(null)}
        title={t('modelGroup.deleteDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="secondary" onClick={() => setDeleteTarget(null)}>
              {t('actions.cancel', { ns: 'common' })}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={deleteMutation.isPending || checkingReferences}
              onClick={() => {
                if (deleteTarget) deleteMutation.mutate(deleteTarget.name)
              }}
            >
              {t('modelGroup.deleteDialog.confirm')}
            </ActionButton>
          </>
        }
      >
        {deleteTarget && (
          <>
            <Typography variant="body2" sx={{ pt: 1 }}>
              {t('modelGroup.deleteDialog.content', { name: deleteTarget.name })}
            </Typography>
            <ModelGroupReferenceNotice
              names={[deleteTarget.name]}
              onCheckingChange={setCheckingReferences}
            />
          </>
        )}
      </NekroDialog>

      <NekroDialog
        open={batchDeleteOpen}
        onClose={() => setBatchDeleteOpen(false)}
        title={t('modelGroup.batchDelete.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="secondary" onClick={() => setBatchDeleteOpen(false)}>
              {t('actions.cancel', { ns: 'common' })}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={
                batchDeleteMutation.isPending || batchDeleteNames.length === 0 || checkingReferences
              }
              onClick={() => batchDeleteMutation.mutate(batchDeleteNames)}
            >
              {t('modelGroup.batchDelete.confirm')}
            </ActionButton>
          </>
        }
      >
        <Typography variant="body2" sx={{ pt: 1 }}>
          {t('modelGroup.batchDelete.content', { count: batchDeleteNames.length })}
        </Typography>
        {/* 逐个列出要删的组名：不可恢复的操作，确认时要能看清删的是哪些。
            平台方「全选」会连带租户自建的组，名单里把它们标出来 */}
        <Box
          component="ul"
          sx={{
            mt: 1,
            mb: 0,
            pl: 2.5,
            maxHeight: 240,
            overflow: 'auto',
            ...(UNIFIED_TABLE_STYLES.scrollbar as SxProps<Theme>),
          }}
        >
          {batchDeleteTargets.map(group => (
            <Typography
              key={group.name}
              component="li"
              variant="body2"
              sx={{ wordBreak: 'break-all' }}
            >
              {group.name}
              {isPlatformOwner && group.ownership === 'tenant' && (
                <Typography component="span" variant="caption" color="warning.main" sx={{ ml: 0.5 }}>
                  {t('modelGroup.batchDelete.tenantOwned', { id: group.owner_tenant_id ?? '?' })}
                </Typography>
              )}
            </Typography>
          ))}
        </Box>
        {batchDeleteOpen && (
          <ModelGroupReferenceNotice
            names={batchDeleteNames}
            onCheckingChange={setCheckingReferences}
          />
        )}
      </NekroDialog>
    </Box>
  )
}
