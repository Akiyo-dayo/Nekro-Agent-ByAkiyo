/**
 * 租户实例归属
 *
 * 平台方把实例划归客户的地方，也是租户管理里最常做的一件事，因此整块按「搜得到、
 * 一眼看得出差异、一次能改完」来组织：搜索 + 归属筛选 + 批量勾选，保存前把改动
 * 摊开成「新增 / 移除」两组明确的清单。
 *
 * 摊开改动不是装饰。后端是**整表覆盖**语义：提交的勾选集合就是该租户之后的全部实例，
 * 没勾的既有归属会被解除。用户看到的是"勾了几个框"，实际发生的是"替换整个集合"，
 * 这中间的落差必须由界面补上，否则少勾一个就是静默解绑。
 *
 * 归属状态一律以后端下发的 `InstanceInfo.tenant_id` 为准。这里曾因该字段被后端硬编码成
 * null 而整块失效——绑定写进了库，界面却永远显示未归属，再保存一次就把归属清空了。
 */

import { useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Checkbox,
  Chip,
  CircularProgress,
  SxProps,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Theme,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import SaveRoundedIcon from '@mui/icons-material/SaveRounded'
import UndoRoundedIcon from '@mui/icons-material/UndoRounded'
import ActionButton from '../../../components/common/ActionButton'
import SearchField from '../../../components/common/SearchField'
import SegmentedControl from '../../../components/common/SegmentedControl'
import { SimpleTooltip } from '../../../components/common/ThemedTooltip'
import { useNotification } from '../../../hooks/useNotification'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../../theme/variants'
import { consoleApi, type InstanceInfo } from '../../../services/api/console'
import {
  TENANT_QUERY_KEYS,
  isTenantEndpointUnavailable,
  tenantApi,
} from '../../../services/api/tenant'

/** 归属筛选。平台方划归实例时问的就是这四个问题之一 */
const OWNERSHIP_FILTERS = ['all', 'mine', 'unassigned', 'others'] as const
type OwnershipFilter = (typeof OWNERSHIP_FILTERS)[number]

/**
 * 该实例是否已归属别的客户
 *
 * 这是本页唯一不可勾选的情形：改判等于把一个客户的 Bot 直接转交给另一个客户，
 * 必须先在原租户下显式解绑（后端同样会拒绝，这里只是把结论提前显示出来）。
 */
const isOwnedByOtherTenant = (instance: InstanceInfo, tenantId: number): boolean =>
  instance.tenant_id !== null && instance.tenant_id !== tenantId

export default function TenantInstanceBinder({ tenantId }: { tenantId: number }) {
  const theme = useTheme()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { t } = useTranslation('tenant')
  const { t: tConsole } = useTranslation('console')

  // null 表示「还没动过」，此时展示后端的实际归属；一旦勾选就切换到本地草稿
  const [draftSelection, setDraftSelection] = useState<string[] | null>(null)
  const [search, setSearch] = useState('')
  const [ownershipFilter, setOwnershipFilter] = useState<OwnershipFilter>('all')

  const instancesQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.instances,
    queryFn: () => consoleApi.listInstances(),
    staleTime: 30_000,
    retry: false,
  })

  // 别的租户在表里只有一个 tenant_id，光靠数字认不出是哪个客户。
  // 本组件只在平台方可达的租户详情页里渲染，租户列表对它一定是可读的。
  const tenantsQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.tenants,
    queryFn: () => tenantApi.listTenants(),
    staleTime: 30_000,
    retry: false,
  })

  const tenantNameById = useMemo(() => {
    const map = new Map<number, string>()
    for (const tenant of tenantsQuery.data?.items ?? []) {
      map.set(tenant.id, tenant.display_name || tenant.name)
    }
    return map
  }, [tenantsQuery.data])

  // 归档实例不参与划归：它已经是只读留存，划给谁都不会再收发消息
  const instances = useMemo(
    () => (instancesQuery.data?.items ?? []).filter(instance => instance.status !== 'archived'),
    [instancesQuery.data]
  )

  const boundKeys = useMemo(
    () =>
      instances
        .filter(instance => instance.tenant_id === tenantId)
        .map(instance => instance.instance_key),
    [instances, tenantId]
  )

  const selection = draftSelection ?? boundKeys
  const selectionSet = useMemo(() => new Set(selection), [selection])
  const boundSet = useMemo(() => new Set(boundKeys), [boundKeys])

  /** 本次保存会新增的归属 */
  const addedKeys = useMemo(
    () => selection.filter(key => !boundSet.has(key)),
    [selection, boundSet]
  )
  /** 本次保存会解除的归属——整表覆盖语义下最容易被忽略的一半 */
  const removedKeys = useMemo(
    () => boundKeys.filter(key => !selectionSet.has(key)),
    [boundKeys, selectionSet]
  )
  const hasPendingChanges = addedKeys.length > 0 || removedKeys.length > 0

  const visibleInstances = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    return instances.filter(instance => {
      if (ownershipFilter === 'mine' && instance.tenant_id !== tenantId) return false
      if (ownershipFilter === 'unassigned' && instance.tenant_id !== null) return false
      if (ownershipFilter === 'others' && !isOwnedByOtherTenant(instance, tenantId)) return false
      if (!keyword) return true
      return [instance.instance_key, instance.display_name, instance.provider_account_id].some(
        value => value.toLowerCase().includes(keyword)
      )
    })
  }, [instances, search, ownershipFilter, tenantId])

  // 全选只作用于当前筛选出来的、且可勾选的行：看不见的行不该被一次点击悄悄改掉
  const selectableVisible = useMemo(
    () => visibleInstances.filter(instance => !isOwnedByOtherTenant(instance, tenantId)),
    [visibleInstances, tenantId]
  )
  const selectedVisibleCount = selectableVisible.filter(instance =>
    selectionSet.has(instance.instance_key)
  ).length
  const allVisibleSelected =
    selectableVisible.length > 0 && selectedVisibleCount === selectableVisible.length

  const bindMutation = useMutation({
    mutationFn: () => tenantApi.bindInstances(tenantId, selection),
    onSuccess: () => {
      notification.success(t('instances.saveSuccess'))
      setDraftSelection(null)
      void queryClient.invalidateQueries({ queryKey: TENANT_QUERY_KEYS.instances })
      void queryClient.invalidateQueries({ queryKey: TENANT_QUERY_KEYS.tenant(tenantId) })
      void queryClient.invalidateQueries({ queryKey: TENANT_QUERY_KEYS.tenants })
    },
    onError: (error: Error) => notification.error(error.message),
  })

  const toggleInstance = (instanceKey: string) => {
    const next = new Set(selection)
    if (next.has(instanceKey)) {
      next.delete(instanceKey)
    } else {
      next.add(instanceKey)
    }
    setDraftSelection(Array.from(next))
  }

  const toggleAllVisible = () => {
    const next = new Set(selection)
    for (const instance of selectableVisible) {
      if (allVisibleSelected) {
        next.delete(instance.instance_key)
      } else {
        next.add(instance.instance_key)
      }
    }
    setDraftSelection(Array.from(next))
  }

  const isEndpointMissing =
    instancesQuery.isError && isTenantEndpointUnavailable(instancesQuery.error)

  const filterOptions = OWNERSHIP_FILTERS.map(value => ({
    value,
    label: t(`instances.filter.${value}`),
  }))

  const renderOwnershipCell = (instance: InstanceInfo) => {
    if (isOwnedByOtherTenant(instance, tenantId)) {
      const ownerName =
        instance.tenant_id === null
          ? ''
          : (tenantNameById.get(instance.tenant_id) ?? `#${instance.tenant_id}`)
      return (
        <Chip
          size="small"
          label={t('instances.ownedByNamed', { tenant: ownerName })}
          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.warning.main, true)}
        />
      )
    }
    if (instance.tenant_id === tenantId) {
      return (
        <Chip
          size="small"
          label={t('instances.ownedByThis')}
          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.success.main, true)}
        />
      )
    }
    return (
      <Typography variant="caption" color="text.secondary">
        {t('instances.unassigned')}
      </Typography>
    )
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.25, minHeight: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <Typography variant="subtitle2" fontWeight={700} sx={{ flexShrink: 0 }}>
          {t('instances.title')}
        </Typography>
        <Chip
          size="small"
          label={t('instances.boundCount', { count: boundKeys.length })}
          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.primary.main, true)}
        />
        <Box sx={{ flex: 1 }} />
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<UndoRoundedIcon />}
          disabled={!hasPendingChanges || bindMutation.isPending}
          onClick={() => setDraftSelection(null)}
        >
          {t('actions.resetSelection')}
        </ActionButton>
        <ActionButton
          tone="primary"
          size="small"
          startIcon={<SaveRoundedIcon />}
          disabled={!hasPendingChanges || bindMutation.isPending}
          onClick={() => bindMutation.mutate()}
        >
          {t('actions.bindInstances')}
        </ActionButton>
      </Box>

      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <SearchField
          value={search}
          onChange={setSearch}
          onClear={() => setSearch('')}
          clearAriaLabel={t('instances.clearSearch')}
          placeholder={t('instances.searchPlaceholder')}
          sx={{ flex: 1, minWidth: 180, maxWidth: 320 }}
        />
        <SegmentedControl
          value={ownershipFilter}
          options={filterOptions}
          onChange={setOwnershipFilter}
        />
      </Box>

      {isEndpointMissing && (
        <Alert severity="info" variant="outlined">
          <Typography variant="subtitle2" fontWeight={700}>
            {t('unavailable.title')}
          </Typography>
          <Typography variant="body2">{t('unavailable.instances')}</Typography>
        </Alert>
      )}

      {instancesQuery.isError && !isEndpointMissing && (
        <Alert
          severity="error"
          variant="outlined"
          action={
            <ActionButton tone="ghost" size="small" onClick={() => void instancesQuery.refetch()}>
              {t('unavailable.retry')}
            </ActionButton>
          }
        >
          {t('instances.loadFailed')}
        </Alert>
      )}

      {hasPendingChanges && (
        <Alert
          severity={removedKeys.length > 0 ? 'warning' : 'info'}
          variant="outlined"
          sx={{ py: 0.5, '& .MuiAlert-message': { py: 0.5 } }}
        >
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
            {addedKeys.length > 0 && (
              <Typography variant="body2">
                {t('instances.pendingAdded', {
                  count: addedKeys.length,
                  keys: addedKeys.join('、'),
                })}
              </Typography>
            )}
            {removedKeys.length > 0 && (
              <Typography variant="body2" color="warning.main">
                {t('instances.pendingRemoved', {
                  count: removedKeys.length,
                  keys: removedKeys.join('、'),
                })}
              </Typography>
            )}
          </Box>
        </Alert>
      )}

      <Box sx={UNIFIED_TABLE_STYLES.tableContentContainer as SxProps<Theme>}>
        <Box sx={{ ...(UNIFIED_TABLE_STYLES.tableViewport as object), maxHeight: 360 }}>
          {instancesQuery.isLoading ? (
            <Box
              sx={{
                minHeight: 160,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <CircularProgress />
            </Box>
          ) : visibleInstances.length === 0 ? (
            <Box
              sx={{
                minHeight: 160,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 0.5,
              }}
            >
              <Typography variant="subtitle2">
                {instances.length === 0 ? t('instances.empty') : t('instances.noResults')}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {instances.length === 0 ? t('instances.emptyHint') : t('instances.noResultsHint')}
              </Typography>
            </Box>
          ) : (
            <Table stickyHeader size="small">
              <TableHead>
                <TableRow>
                  <TableCell
                    sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}
                    padding="checkbox"
                  >
                    <SimpleTooltip title={t('instances.selectAllHint')}>
                      <span>
                        <Checkbox
                          size="small"
                          checked={allVisibleSelected}
                          indeterminate={selectedVisibleCount > 0 && !allVisibleSelected}
                          disabled={selectableVisible.length === 0 || bindMutation.isPending}
                          onChange={toggleAllVisible}
                          inputProps={{ 'aria-label': t('instances.selectAllHint') }}
                        />
                      </span>
                    </SimpleTooltip>
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {tConsole('list.columns.instance')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {tConsole('list.columns.status')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {tConsole('list.columns.account')}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                    {t('list.columns.tenant')}
                  </TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {visibleInstances.map(instance => {
                  const ownedByOther = isOwnedByOtherTenant(instance, tenantId)
                  const checked = selectionSet.has(instance.instance_key)
                  const isChanged =
                    checked !== boundSet.has(instance.instance_key)

                  return (
                    <TableRow
                      key={instance.instance_key}
                      hover
                      selected={checked}
                      onClick={() => {
                        if (ownedByOther || bindMutation.isPending) return
                        toggleInstance(instance.instance_key)
                      }}
                      sx={{
                        ...(UNIFIED_TABLE_STYLES.row as object),
                        cursor: ownedByOther ? 'not-allowed' : 'pointer',
                        // 未保存的行给一条左边线，改动在长表里也一眼可见
                        ...(isChanged
                          ? { boxShadow: `inset 3px 0 0 ${theme.palette.warning.main}` }
                          : {}),
                      }}
                    >
                      <TableCell
                        sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}
                        padding="checkbox"
                      >
                        <SimpleTooltip
                          title={ownedByOther ? t('instances.ownedByOtherHint') : ''}
                        >
                          <span>
                            <Checkbox
                              size="small"
                              checked={checked}
                              disabled={ownedByOther || bindMutation.isPending}
                              onChange={() => toggleInstance(instance.instance_key)}
                              onClick={event => event.stopPropagation()}
                              inputProps={{ 'aria-label': instance.instance_key }}
                            />
                          </span>
                        </SimpleTooltip>
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        <Typography variant="body2" fontWeight={600} noWrap>
                          {instance.display_name || instance.instance_key}
                        </Typography>
                        <Typography variant="caption" color="text.secondary" noWrap>
                          {instance.instance_key}
                        </Typography>
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        <Typography variant="caption">
                          {tConsole(`status.${instance.status}`)}
                        </Typography>
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        <Typography variant="caption" color="text.secondary">
                          {instance.provider_account_id || '—'}
                        </Typography>
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                        {renderOwnershipCell(instance)}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </Box>
      </Box>
    </Box>
  )
}
