import { useState } from 'react'
import { Alert, Box, Chip, Typography } from '@mui/material'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../../components/common/ServerDataTable'
import QueryErrorState from '../../components/common/QueryErrorState'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import { formatFullTime, formatListTime } from '../../utils/listTime'
import { isNotFoundError } from '../../services/api/axios'
import { tavernApi, type TavernAuditItem } from '../../services/api/tavern'
import type { WorldbookScopeParams } from '../../services/api/worldbook'
import { CHIP_VARIANTS } from '../../theme/variants'

/**
 * 「变更记录」标签
 *
 * 只显示服务端筛好的这一页：租户看不到全局 / 适配器层的行，平台方看全部。审计里
 * 存的是摘要而不是原文（见设计稿的取舍），所以这里没有「展开看全文」，改动列照原样
 * 显示 `旧 → 新` 即可。
 */

const PAGE_SIZE = 20

const FIELD_LABEL_KEYS: Record<string, string> = {
  PROMPT_ADVANCED_SLOTS_ENABLED: 'lab.audit.field.advancedSlots',
  PROMPT_STABLE_PREFIX_ORDER: 'lab.audit.field.stablePrefixOrder',
  PROMPT_POST_HISTORY: 'lab.audit.field.postHistory',
  PROMPT_AUXILIARY: 'lab.audit.field.auxiliary',
  PROMPT_PREFILL: 'lab.audit.field.prefill',
  'preset.post_history': 'lab.audit.field.presetPostHistory',
  'preset.prefill': 'lab.audit.field.presetPrefill',
}

const LAYER_LABEL_KEYS: Record<string, string> = {
  system: 'rounds.layer.system',
  adapter: 'rounds.layer.adapter',
  instance: 'rounds.layer.instance',
  channel: 'rounds.layer.channel',
  preset: 'rounds.layer.preset',
}

export default function AuditTab({ scope }: { scope: WorldbookScopeParams }) {
  const { t } = useTranslation('tavern')
  const [page, setPage] = useState(1)

  const auditQuery = useQuery({
    queryKey: ['tavern', 'audit', scope.instance_key, page],
    queryFn: () =>
      tavernApi.listAudit({
        instance_key: scope.instance_key,
        page,
        page_size: PAGE_SIZE,
      }),
    retry: false,
    placeholderData: keepPreviousData,
  })

  const layerLabel = (layer: string) => {
    const key = LAYER_LABEL_KEYS[layer]
    return key ? t(key) : layer
  }

  const fieldLabel = (field: string) => {
    const key = FIELD_LABEL_KEYS[field]
    return key ? t(key) : field
  }

  const targetLabel = (item: TavernAuditItem) => {
    // 按层把能定位到对象的几段拼起来；空值直接省掉，不留下「适配器：」这种半句。
    const parts: string[] = []
    if (item.adapter_key) {
      parts.push(t('lab.audit.targetAdapter', { key: item.adapter_key }))
    }
    if (item.instance_key) {
      parts.push(t('lab.audit.targetInstance', { key: item.instance_key }))
    }
    if (item.chat_key) {
      parts.push(t('lab.audit.targetChannel', { chatKey: item.chat_key }))
    }
    if (item.preset_id) {
      parts.push(t('lab.audit.targetPreset', { id: item.preset_id }))
    }
    return parts.length > 0 ? parts.join(' / ') : '—'
  }

  const columns: ServerDataTableColumn<TavernAuditItem>[] = [
    {
      key: 'time',
      header: t('lab.audit.columns.time'),
      width: 170,
      render: row => (
        <SimpleTooltip title={formatFullTime(row.created_at)}>
          <span>
            <Typography variant="body2">{formatListTime(row.created_at)}</Typography>
          </span>
        </SimpleTooltip>
      ),
    },
    {
      key: 'operator',
      header: t('lab.audit.columns.operator'),
      width: 150,
      render: row => (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexWrap: 'wrap' }}>
          <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
            {row.operator || '—'}
          </Typography>
          {row.operator_is_platform && (
            <Chip
              size="small"
              label={t('lab.audit.platformOperator')}
              sx={CHIP_VARIANTS.base(true)}
            />
          )}
        </Box>
      ),
    },
    {
      key: 'layer',
      header: t('lab.audit.columns.layer'),
      width: 100,
      render: row => <Typography variant="body2">{layerLabel(row.layer)}</Typography>,
    },
    {
      key: 'target',
      header: t('lab.audit.columns.target'),
      width: 240,
      render: row => (
        <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
          {targetLabel(row)}
        </Typography>
      ),
    },
    {
      key: 'field',
      header: t('lab.audit.columns.field'),
      width: 180,
      render: row => <Typography variant="body2">{fieldLabel(row.field)}</Typography>,
    },
    {
      key: 'change',
      header: t('lab.audit.columns.change'),
      width: 280,
      render: row => (
        <Typography
          component="span"
          variant="body2"
          sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}
        >
          {row.old_value || '—'} → {row.new_value || '—'}
        </Typography>
      ),
    },
  ]

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0 }}>
      <ServerDataTable
        columns={columns}
        rows={auditQuery.data?.items ?? []}
        getRowId={row => row.id}
        total={auditQuery.data?.total ?? 0}
        page={page}
        pageSize={PAGE_SIZE}
        onPageChange={setPage}
        loading={auditQuery.isPending}
        fetching={auditQuery.isFetching && !auditQuery.isPending}
        error={auditQuery.error}
        onRetry={() => void auditQuery.refetch()}
        renderError={error =>
          isNotFoundError(error) ? (
            <Alert severity="warning">{t('lab.audit.notFound')}</Alert>
          ) : (
            <QueryErrorState
              error={error}
              onRetry={() => void auditQuery.refetch()}
              compact
            />
          )
        }
        emptyTitle={t('lab.audit.empty')}
      />
    </Box>
  )
}
