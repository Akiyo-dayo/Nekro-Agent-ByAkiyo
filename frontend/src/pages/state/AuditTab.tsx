import { useMemo, useState } from 'react'
import { Alert, Box, Typography } from '@mui/material'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import FilterSelect from '../../components/common/FilterSelect'
import ServerDataTable, { type ServerDataTableColumn } from '../../components/common/ServerDataTable'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import { isNotFoundError } from '../../services/api/axios'
import {
  stateTableApi,
  type StateAuditItem,
  type StateScope,
} from '../../services/api/stateTable'
import { formatFullTime, formatListTime } from '../../utils/listTime'
import { enumText, opsText } from './stateUtils'

const PAGE_SIZE = 20

/**
 * 「审计」标签
 *
 * 管理员改动、回滚与重置都会留一行。目标那一列把 `#编号` 与频道标识一起写出来：
 * 同一个人可能在好几个频道里都有档案，只写编号没法核对是哪一条。
 */
export default function AuditTab({ scope }: { scope: StateScope }) {
  const { t } = useTranslation('state')
  const scopeKey = `${scope.adapter_key}|${scope.instance_key}`

  const [kind, setKind] = useState('')
  const [source, setSource] = useState('')
  const [page, setPage] = useState(1)

  const kindOptions = useMemo(
    () => [
      { value: '', label: t('audit.kind.all') },
      { value: 'channel', label: t('audit.kind.channel') },
      { value: 'person', label: t('audit.kind.person') },
      { value: 'template', label: t('audit.kind.template') },
    ],
    [t]
  )

  const sourceOptions = useMemo(
    () => [
      { value: '', label: t('audit.source.all') },
      { value: 'admin', label: t('audit.source.admin') },
      { value: 'rollback', label: t('audit.source.rollback') },
      { value: 'reset', label: t('audit.source.reset') },
    ],
    [t]
  )

  const query = useQuery({
    queryKey: ['state-table', 'audit', scopeKey, page, kind, source],
    queryFn: () =>
      stateTableApi.listAudit(scope, {
        kind: kind || undefined,
        source: source || undefined,
        page,
        size: PAGE_SIZE,
      }),
    retry: false,
    placeholderData: keepPreviousData,
  })

  const columns: ServerDataTableColumn<StateAuditItem>[] = useMemo(
    () => [
      {
        key: 'time',
        header: t('audit.columns.time'),
        width: 180,
        render: row => (
          <SimpleTooltip title={formatFullTime(row.create_time)}>
            <span>
              <Typography variant="caption" color="text.secondary">
                {formatListTime(row.create_time)}
              </Typography>
            </span>
          </SimpleTooltip>
        ),
      },
      {
        key: 'target',
        header: t('audit.columns.target'),
        width: 260,
        render: row => (
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="body2">
              {`${enumText(t, 'audit.kind', row.target_kind)} #${row.target_id}`}
            </Typography>
            {row.chat_key && (
              <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-all' }}>
                {row.chat_key}
              </Typography>
            )}
          </Box>
        ),
      },
      {
        key: 'source',
        header: t('audit.columns.source'),
        width: 130,
        render: row => (
          <Typography variant="body2">{enumText(t, 'enums.source', row.source)}</Typography>
        ),
      },
      {
        key: 'ops',
        header: t('audit.columns.ops'),
        width: 260,
        hideOnMobile: true,
        render: row => (
          <Typography variant="caption" color="text.secondary" sx={{ wordBreak: 'break-word' }}>
            {opsText(row.ops)}
          </Typography>
        ),
      },
      {
        key: 'editor',
        header: t('audit.columns.editor'),
        width: 140,
        render: row => <Typography variant="body2">{row.editor || '—'}</Typography>,
      },
    ],
    [t]
  )

  if (isNotFoundError(query.error)) {
    return <Alert severity="info">{t('notEnabled')}</Alert>
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0, flex: 1 }}>
      <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', flexWrap: 'wrap' }}>
        <FilterSelect
          label={t('audit.filters.kind')}
          value={kind}
          options={kindOptions}
          onChange={value => {
            setKind(value)
            setPage(1)
          }}
          fullWidth={false}
          sx={{ minWidth: 150 }}
        />
        <FilterSelect
          label={t('audit.filters.source')}
          value={source}
          options={sourceOptions}
          onChange={value => {
            setSource(value)
            setPage(1)
          }}
          fullWidth={false}
          sx={{ minWidth: 160 }}
        />
      </Box>

      <ServerDataTable
        columns={columns}
        rows={query.data?.items ?? []}
        getRowId={row => row.id}
        total={query.data?.total ?? 0}
        page={page}
        pageSize={PAGE_SIZE}
        onPageChange={setPage}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        onRetry={() => void query.refetch()}
        emptyTitle={t('audit.emptyTitle')}
        emptyHint={t('audit.emptyHint')}
      />
    </Box>
  )
}
