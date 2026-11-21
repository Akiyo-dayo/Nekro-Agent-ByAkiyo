import { useState } from 'react'
import { Alert, Box, Menu, MenuItem, Stack, Typography } from '@mui/material'
import LockRoundedIcon from '@mui/icons-material/LockRounded'
import MoreHorizRoundedIcon from '@mui/icons-material/MoreHorizRounded'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../../components/common/ServerDataTable'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import PresetDetailDrawer from '../../components/rp-preset/PresetDetailDrawer'
import PresetImportDialog from '../../components/rp-preset/PresetImportDialog'
import PresetOwnerChip from '../../components/rp-preset/PresetOwnerChip'
import { useIdentity } from '../../hooks/useIdentity'
import { useNotification } from '../../hooks/useNotification'
import {
  rpPresetsApi,
  type RpPresetDetail,
  type RpPresetReference,
  type RpPresetSummary,
} from '../../services/api/rpPresets'
import type { WorldbookScopeParams } from '../../services/api/worldbook'
import { formatFullTime, formatListTime } from '../../utils/listTime'

interface PresetsTabProps {
  scope: WorldbookScopeParams
  isScopeReady: boolean
}

const PAGE_SIZE = 20

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

/**
 * 「预设」标签
 *
 * 列表只读归属与段数，导入、改名与开关都在对话框 / 抽屉里完成。全局预设对租户只读：
 * 名称后跟一个锁图标，行尾菜单里也没有删除。
 *
 * 后端 `list` 一次返回全部可见预设（没有分页参数），这里按页切开只为沿用项目表格的
 * 默认密度，不额外发请求。
 */
export default function PresetsTab({ scope, isScopeReady }: PresetsTabProps) {
  const { t } = useTranslation('tavern')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { isTenantScoped } = useIdentity()
  const [page, setPage] = useState(1)
  const [importOpen, setImportOpen] = useState(false)
  const [detailId, setDetailId] = useState<number | null>(null)
  const [menu, setMenu] = useState<{
    anchor: HTMLElement
    preset: RpPresetSummary
  } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<RpPresetSummary | null>(null)

  const queryKey = ['prompt-lab', 'rp-presets', scope.instance_key ?? '']

  const listQuery = useQuery({
    queryKey,
    queryFn: () => rpPresetsApi.list({ instance_key: scope.instance_key }),
    enabled: isScopeReady,
    retry: false,
  })

  const exportMutation = useMutation({
    mutationFn: (preset: RpPresetSummary) =>
      rpPresetsApi.download(preset.id, { instance_key: scope.instance_key }),
    onError: (error: unknown) => {
      notification.error(errorText(error, tCommon('messages.operationFailed')))
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (preset: RpPresetSummary) =>
      rpPresetsApi.remove(preset.id, { instance_key: scope.instance_key }),
    onSuccess: () => {
      notification.success(t('lab.presets.deleteDialog.deleted'))
      setDeleteTarget(null)
      setDetailId(null)
      setPage(1)
      void queryClient.invalidateQueries({ queryKey })
    },
    onError: (error: unknown) => {
      notification.error(errorText(error, tCommon('messages.operationFailed')))
    },
  })

  // 删除确认框要说明「还有哪些地方在用」，只在框打开时问一次。
  // 查询失败不影响删除：那只是少一句提醒，按钮照旧可用。
  const referencesQuery = useQuery({
    queryKey: ['prompt-lab', 'rp-preset-references', deleteTarget?.id ?? null, scope.instance_key ?? ''],
    queryFn: () =>
      rpPresetsApi.getReferences(deleteTarget?.id as number, {
        instance_key: scope.instance_key,
      }),
    enabled: deleteTarget !== null,
    retry: false,
  })

  /** 一处引用的人读名字：全局配置没有名字，其余带上该层的标签 */
  const referenceLabel = (reference: RpPresetReference) => {
    const key = `lab.presets.deleteDialog.referenceLayer.${reference.layer}`
    if (reference.layer === 'system') return t(key)
    return t(key, { label: reference.label || reference.chat_key || '—' })
  }

  const references = referencesQuery.data?.items ?? []
  const referencesTotal = referencesQuery.data?.total ?? 0
  const referenceNames = references
    .slice(0, 5)
    .map(referenceLabel)
    .join(t('lab.presets.deleteDialog.referenceJoin'))
  const referenceText =
    referencesTotal > 5
      ? referenceNames + t('lab.presets.deleteDialog.referenceMore')
      : referenceNames

  const items = listQuery.data?.items ?? []
  const total = items.length
  const pageRows = items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  const openDetail = (presetId: number) => {
    setDetailId(presetId)
  }

  const columns: ServerDataTableColumn<RpPresetSummary>[] = [
    {
      key: 'name',
      header: t('lab.presets.columns.name'),
      width: 260,
      render: row => (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, minWidth: 0 }}>
          <Typography variant="body2" fontWeight={600} noWrap>
            {row.name}
          </Typography>
          {isTenantScoped && row.owner_instance_key === '' ? (
            <SimpleTooltip title={t('lab.presets.globalReadOnly')}>
              <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center' }}>
                <LockRoundedIcon
                  sx={{ fontSize: 16, color: 'text.secondary' }}
                  aria-label={t('lab.presets.globalReadOnly')}
                />
              </Box>
            </SimpleTooltip>
          ) : null}
        </Box>
      ),
    },
    {
      key: 'segments',
      header: t('lab.presets.columns.segments'),
      width: 140,
      render: row => (
        <Typography
          variant="body2"
          sx={{ fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}
        >
          {t('lab.presets.segmentsCount', {
            enabled: row.enabled_count,
            total: row.segment_count,
          })}
        </Typography>
      ),
    },
    {
      key: 'owner',
      header: t('lab.presets.columns.owner'),
      width: 110,
      render: row => <PresetOwnerChip global={row.owner_instance_key === ''} />,
    },
    {
      key: 'updated',
      header: t('lab.presets.columns.updatedAt'),
      width: 180,
      hideOnMobile: true,
      render: row => (
        <SimpleTooltip title={formatFullTime(row.update_time)}>
          <span>
            <Typography variant="caption" color="text.secondary">
              {formatListTime(row.update_time)}
            </Typography>
          </span>
        </SimpleTooltip>
      ),
    },
    {
      key: 'actions',
      header: '',
      width: 56,
      align: 'right',
      render: row => (
        <IconActionButton
          size="small"
          aria-label={tCommon('actions.more')}
          title={tCommon('actions.more')}
          onClick={event => {
            event.stopPropagation()
            setMenu({ anchor: event.currentTarget, preset: row })
          }}
        >
          <MoreHorizRoundedIcon fontSize="small" />
        </IconActionButton>
      ),
    },
  ]

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, flex: 1, minHeight: 0 }}>
      <Stack direction="row" sx={{ alignItems: 'center', justifyContent: 'flex-end' }}>
        <ActionButton tone="primary" onClick={() => setImportOpen(true)}>
          {t('lab.presets.import')}
        </ActionButton>
      </Stack>

      <Box sx={{ flex: 1, minHeight: 0 }}>
        <ServerDataTable
          columns={columns}
          rows={pageRows}
          getRowId={row => row.id}
          total={total}
          page={page}
          pageSize={PAGE_SIZE}
          onPageChange={setPage}
          loading={listQuery.isPending}
          fetching={listQuery.isFetching}
          error={listQuery.error}
          onRetry={() => void listQuery.refetch()}
          emptyTitle={t('lab.presets.emptyTitle')}
          emptyHint={
            <ActionButton tone="primary" onClick={() => setImportOpen(true)}>
              {t('lab.presets.import')}
            </ActionButton>
          }
          onRowClick={row => openDetail(row.id)}
        />
      </Box>

      <Menu
        anchorEl={menu?.anchor ?? null}
        open={menu !== null}
        onClose={() => setMenu(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        {menu ? (
          <MenuItem
            disabled={exportMutation.isPending}
            onClick={() => {
              exportMutation.mutate(menu.preset)
              setMenu(null)
            }}
          >
            {t('lab.presets.actions.export')}
          </MenuItem>
        ) : null}
        {menu && !menu.preset.read_only ? (
          <MenuItem
            onClick={() => {
              setDeleteTarget(menu.preset)
              setMenu(null)
            }}
          >
            {t('lab.presets.actions.delete')}
          </MenuItem>
        ) : null}
      </Menu>

      <PresetImportDialog
        open={importOpen}
        scope={{ instance_key: scope.instance_key }}
        onClose={() => setImportOpen(false)}
        onImported={(preset: RpPresetDetail) => {
          setImportOpen(false)
          setPage(1)
          void queryClient.invalidateQueries({ queryKey })
          openDetail(preset.id)
        }}
      />

      <PresetDetailDrawer
        key={detailId ?? 'closed'}
        open={detailId !== null}
        presetId={detailId}
        scope={{ instance_key: scope.instance_key }}
        onClose={() => setDetailId(null)}
        onChanged={() => {
          void queryClient.invalidateQueries({ queryKey })
        }}
      />

      <NekroDialog
        open={deleteTarget !== null}
        onClose={() => {
          if (!deleteMutation.isPending) setDeleteTarget(null)
        }}
        title={t('lab.presets.deleteDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton
              tone="ghost"
              onClick={() => setDeleteTarget(null)}
              disabled={deleteMutation.isPending}
            >
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              onClick={() => {
                if (deleteTarget) deleteMutation.mutate(deleteTarget)
              }}
              disabled={deleteMutation.isPending}
            >
              {t('lab.presets.deleteDialog.confirm')}
            </ActionButton>
          </>
        }
      >
        <Stack spacing={1.25}>
          <Typography variant="body2">
            {t('lab.presets.deleteDialog.body', {
              name: deleteTarget?.name ?? '',
            })}
          </Typography>
          {!referencesQuery.isPending && !referencesQuery.isError && referencesTotal > 0 && (
            <Alert severity="warning">
              {t('lab.presets.deleteDialog.referencedHint', {
                count: referencesTotal,
                names: referenceText,
              })}
            </Alert>
          )}
        </Stack>
      </NekroDialog>
    </Box>
  )
}
