import { Fragment, useMemo, useState } from 'react'
import {
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  MenuItem,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import IconActionButton from '../../components/common/IconActionButton'
import QueryErrorState from '../../components/common/QueryErrorState'
import TablePaginationStyled from '../../components/common/TablePaginationStyled'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  CONSOLE_OPS_AUDIT_QUERY_KEYS,
  consoleOpsAuditApi,
  type OpsAuditItem,
  type OpsAuditQuery,
} from '../../services/api/consoleOpsAudit'

const PAGE_SIZE = 20
const OUTCOMES = ['pending', 'executed', 'refused', 'failed']
const SOURCES = ['agent', 'na_ops']
const COLUMN_COUNT = 11

/** 结果用的颜色：与频道额度页的状态色同一套语义 */
function outcomeColor(outcome: string): 'success' | 'warning' | 'error' | 'default' {
  if (outcome === 'executed') return 'success'
  if (outcome === 'refused') return 'warning'
  if (outcome === 'failed') return 'error'
  return 'default'
}

export default function OpsAuditPage() {
  const { t } = useTranslation('console-ops-audit')

  const [page, setPage] = useState(0)
  const [outcome, setOutcome] = useState('')
  const [source, setSource] = useState('')
  const [tool, setTool] = useState('')
  const [operator, setOperator] = useState('')
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

  const query = useMemo<OpsAuditQuery>(
    () => ({
      page: page + 1,
      page_size: PAGE_SIZE,
      outcome: outcome || undefined,
      source: source || undefined,
      tool: tool.trim() || undefined,
      operator: operator.trim() || undefined,
    }),
    [operator, outcome, page, source, tool]
  )

  const auditQuery = useQuery({
    queryKey: CONSOLE_OPS_AUDIT_QUERY_KEYS.list(query),
    queryFn: () => consoleOpsAuditApi.listEvents(query),
    staleTime: 15_000,
    retry: false,
  })

  const items = auditQuery.data?.items ?? []

  /** 改筛选就回到第一页：留在第 5 页会看到一页空数据 */
  const resetPage = () => setPage(0)

  const toggleArgs = (id: number) => {
    setExpanded(previous => {
      const next = new Set(previous)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  const locationOf = (item: OpsAuditItem) => {
    const instance = item.adapter_key
      ? `${item.adapter_key}/${item.instance_key}`
      : item.instance_key
    return [instance, item.chat_key].filter(Boolean).join(' · ') || t('value.noValue')
  }

  return (
    <Box>
      <Stack
        direction="row"
        alignItems="flex-start"
        justifyContent="space-between"
        spacing={2}
        sx={{ mb: 2 }}
      >
        <Box>
          <Typography variant="h5" fontWeight={700}>
            {t('title')}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {t('subtitle')}
          </Typography>
        </Box>
        <IconActionButton
          aria-label={t('actions.refresh')}
          onClick={() => void auditQuery.refetch()}
          disabled={auditQuery.isFetching}
        >
          <RefreshRoundedIcon />
        </IconActionButton>
      </Stack>

      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Box
            sx={{
              display: 'grid',
              gridTemplateColumns: {
                xs: '1fr',
                sm: 'repeat(2, minmax(0, 1fr))',
                md: 'repeat(4, minmax(0, 1fr))',
              },
              gap: 2,
            }}
          >
            <TextField
              select
              size="small"
              label={t('filters.outcome')}
              value={outcome}
              onChange={event => {
                setOutcome(event.target.value)
                resetPage()
              }}
            >
              <MenuItem value="">{t('filters.all')}</MenuItem>
              {OUTCOMES.map(value => (
                <MenuItem key={value} value={value}>
                  {t(`outcome.${value}`)}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              select
              size="small"
              label={t('filters.source')}
              value={source}
              onChange={event => {
                setSource(event.target.value)
                resetPage()
              }}
            >
              <MenuItem value="">{t('filters.all')}</MenuItem>
              {SOURCES.map(value => (
                <MenuItem key={value} value={value}>
                  {t(`source.${value}`)}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              size="small"
              label={t('filters.tool')}
              placeholder={t('filters.toolPlaceholder')}
              value={tool}
              onChange={event => {
                setTool(event.target.value)
                resetPage()
              }}
            />
            <TextField
              size="small"
              label={t('filters.operator')}
              placeholder={t('filters.operatorPlaceholder')}
              value={operator}
              onChange={event => {
                setOperator(event.target.value)
                resetPage()
              }}
            />
          </Box>
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          {auditQuery.isError ? (
            <QueryErrorState
              error={auditQuery.error}
              onRetry={() => void auditQuery.refetch()}
            />
          ) : auditQuery.isLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
              <CircularProgress size={28} />
            </Box>
          ) : (
            <>
              <Box sx={{ overflowX: 'auto' }}>
                <Table size="small" sx={UNIFIED_TABLE_STYLES.getTableBase(false, false)}>
                  <TableHead>
                    <TableRow>
                      <TableCell>{t('columns.id')}</TableCell>
                      <TableCell>{t('columns.time')}</TableCell>
                      <TableCell>{t('columns.source')}</TableCell>
                      <TableCell>{t('columns.tool')}</TableCell>
                      <TableCell>{t('columns.outcome')}</TableCell>
                      <TableCell>{t('columns.reason')}</TableCell>
                      <TableCell>{t('columns.operator')}</TableCell>
                      <TableCell>{t('columns.location')}</TableCell>
                      <TableCell>{t('columns.summary')}</TableCell>
                      <TableCell>{t('columns.errorType')}</TableCell>
                      <TableCell>{t('columns.args')}</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {items.map(item => (
                      <Fragment key={item.id}>
                        <TableRow hover>
                          <TableCell>#{item.id}</TableCell>
                          <TableCell sx={{ whiteSpace: 'nowrap' }}>
                            {new Date(item.created_at).toLocaleString()}
                          </TableCell>
                          <TableCell>
                            {t(`source.${item.source}`, { defaultValue: item.source })}
                          </TableCell>
                          <TableCell sx={{ fontFamily: 'monospace' }}>{item.tool}</TableCell>
                          <TableCell>
                            <Chip
                              size="small"
                              color={outcomeColor(item.outcome)}
                              label={t(`outcome.${item.outcome}`, {
                                defaultValue: item.outcome,
                              })}
                            />
                          </TableCell>
                          <TableCell>{item.refuse_reason || t('value.noValue')}</TableCell>
                          <TableCell sx={{ fontFamily: 'monospace' }}>
                            {item.operator || t('value.noValue')}
                          </TableCell>
                          <TableCell sx={{ wordBreak: 'break-all' }}>
                            {locationOf(item)}
                          </TableCell>
                          <TableCell sx={{ wordBreak: 'break-word' }}>
                            {item.result_summary || t('value.noValue')}
                          </TableCell>
                          <TableCell sx={{ fontFamily: 'monospace' }}>
                            {item.error_type || t('value.noValue')}
                          </TableCell>
                          <TableCell>
                            <Button size="small" onClick={() => toggleArgs(item.id)}>
                              {expanded.has(item.id)
                                ? t('actions.hideArgs')
                                : t('actions.showArgs')}
                            </Button>
                          </TableCell>
                        </TableRow>
                        {expanded.has(item.id) && (
                          <TableRow>
                            <TableCell colSpan={COLUMN_COUNT} sx={{ bgcolor: 'action.hover' }}>
                              <Box
                                component="pre"
                                sx={{
                                  m: 0,
                                  whiteSpace: 'pre-wrap',
                                  wordBreak: 'break-word',
                                  fontFamily: 'monospace',
                                  fontSize: '0.75rem',
                                }}
                              >
                                {JSON.stringify(item.args_json ?? {}, null, 2)}
                              </Box>
                            </TableCell>
                          </TableRow>
                        )}
                      </Fragment>
                    ))}
                  </TableBody>
                </Table>
              </Box>
              {items.length === 0 && (
                <Box sx={{ py: 3, textAlign: 'center' }}>
                  <Typography variant="body2" color="text.secondary">
                    {t('value.empty')}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {t('value.emptyHint')}
                  </Typography>
                </Box>
              )}
              <TablePaginationStyled
                component="div"
                count={auditQuery.data?.total ?? 0}
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
    </Box>
  )
}
