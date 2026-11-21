/**
 * 平台方「SSH 网关」页
 *
 * 三块内容：网关状态（每 10 秒刷新）、全部工作区的凭据（只读，改动去工作区页）、
 * 全局审计。存量资源导入是这一页唯一的写操作，幂等由后端保证。
 *
 * 权限边界：整条路由只给平台方，租户身份在 `MainLayout` 就被拦住；页面里的请求
 * 仍然按「加载失败 / 无权查看」分开呈现，不把 403 渲染成空表。
 */

import { Fragment, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material'
import {
  Refresh as RefreshIcon,
  SystemUpdateAlt as ImportIcon,
} from '@mui/icons-material'
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import { useNotification } from '../../hooks/useNotification'
import { workspaceApi, type WorkspaceSummary } from '../../services/api/workspace'
import {
  SSH_GATEWAY_QUERY_KEYS,
  sshGatewayApi,
  type ImportReport,
  type SshAuditView,
  type SshCredentialView,
} from '../../services/api/sshGateway'
import { workspaceDetailPath } from '../../router/routes'
import { CARD_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'
import { formatLastActiveTimeFromInput } from '../../utils/time'

const STATUS_COLORS: Record<string, 'success' | 'default' | 'warning' | 'error'> = {
  active: 'success',
  disabled: 'default',
  suspended: 'warning',
  auth_failed: 'error',
}

const GATEWAY_STATE_COLORS: Record<string, 'success' | 'default' | 'warning' | 'error'> = {
  running: 'success',
  off: 'default',
  starting: 'warning',
  unavailable: 'error',
  tripped: 'error',
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return String(error ?? '')
}

export default function SshGatewayPage() {
  const { t } = useTranslation('console-ssh-gateway')
  const navigate = useNavigate()
  const notification = useNotification()
  const queryClient = useQueryClient()

  const [importConfirmOpen, setImportConfirmOpen] = useState(false)
  const [expandedAudit, setExpandedAudit] = useState<Set<number>>(new Set())

  const statusQuery = useQuery({
    queryKey: SSH_GATEWAY_QUERY_KEYS.status(),
    queryFn: sshGatewayApi.getStatus,
    refetchInterval: 10_000,
    retry: false,
  })

  const credentialsQuery = useQuery({
    queryKey: SSH_GATEWAY_QUERY_KEYS.allCredentials(),
    queryFn: sshGatewayApi.listAllCredentials,
    staleTime: 15_000,
    retry: false,
  })

  const workspacesQuery = useQuery({
    queryKey: ['workspaces', 'list'],
    queryFn: workspaceApi.getList,
    staleTime: 60_000,
    retry: false,
  })

  const auditQuery = useInfiniteQuery({
    queryKey: [...SSH_GATEWAY_QUERY_KEYS.audit(), 'infinite'],
    queryFn: ({ pageParam }) =>
      sshGatewayApi.listAudit({ limit: 50, before_id: pageParam }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: lastPage => lastPage.next_before_id ?? undefined,
    retry: false,
  })

  const importMutation = useMutation({
    mutationFn: sshGatewayApi.importResources,
    onSuccess: () => {
      setImportConfirmOpen(false)
      void queryClient.invalidateQueries({
        queryKey: SSH_GATEWAY_QUERY_KEYS.allCredentials(),
      })
      void queryClient.invalidateQueries({
        queryKey: SSH_GATEWAY_QUERY_KEYS.audit(),
      })
    },
    onError: error => {
      notification.error(
        t('import.failed', { message: errorMessage(error) })
      )
    },
  })

  const credentials = useMemo(
    () => credentialsQuery.data?.items ?? [],
    [credentialsQuery.data]
  )
  const auditItems = useMemo(
    () => auditQuery.data?.pages.flatMap(page => page.items) ?? [],
    [auditQuery.data]
  )

  const workspaceNameById = useMemo(() => {
    const map = new Map<number, string>()
    for (const workspace of workspacesQuery.data ?? []) {
      map.set(workspace.id, workspace.name)
    }
    for (const credential of credentials) {
      if (credential.workspace_name) {
        map.set(credential.workspace_id, credential.workspace_name)
      }
    }
    return map
  }, [credentials, workspacesQuery.data])

  const credentialAliases = useMemo(() => {
    const map = new Map<number, string>()
    for (const credential of credentials) {
      map.set(credential.id, credential.alias)
    }
    return map
  }, [credentials])

  const workspaceName = (workspaceId: number): string =>
    workspaceNameById.get(workspaceId) ?? t('import.workspaceValue', { id: workspaceId })

  /** status_reason 目前只会落认证失败；未知值原样显示，避免把自由文本吞掉 */
  const statusReasonLabel = (reason: string): string => {
    if (reason === 'auth_failed') {
      return t('credentials.statusReasons.auth_failed')
    }
    return t(`audit.reasons.${reason}`, { defaultValue: reason })
  }

  const statusTooltip = (credential: SshCredentialView): string => {
    const lines: string[] = []
    if (credential.status_reason) {
      lines.push(
        t('credentials.statusReason', {
          reason: statusReasonLabel(credential.status_reason),
        })
      )
    }
    if (credential.last_error) {
      lines.push(t('credentials.lastError', { message: credential.last_error }))
    }
    return lines.join('\n')
  }

  const auditWorkspace = (item: SshAuditView): string =>
    workspaceNameById.get(item.workspace_id) ??
    t('import.workspaceValue', { id: item.workspace_id })

  const toggleAudit = (id: number) => {
    setExpandedAudit(previous => {
      const next = new Set(previous)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  const settingsPath = `/settings/system?category=${encodeURIComponent(
    t('settingsCategory')
  )}&search=SSH_GATEWAY_ENABLED`

  const report: ImportReport | undefined = importMutation.data

  const workspacesById = useMemo(() => {
    const map = new Map<number, WorkspaceSummary>()
    for (const workspace of workspacesQuery.data ?? []) {
      map.set(workspace.id, workspace)
    }
    return map
  }, [workspacesQuery.data])

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <Stack
        direction={{ xs: 'column', sm: 'row' }}
        alignItems={{ xs: 'flex-start', sm: 'center' }}
        justifyContent="space-between"
        spacing={1.5}
      >
        <Box>
          <Typography variant="h5" fontWeight={700}>
            {t('title')}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {t('subtitle')}
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          <IconActionButton
            aria-label={t('actions.refresh')}
            onClick={() => {
              void statusQuery.refetch()
              void credentialsQuery.refetch()
              void auditQuery.refetch()
            }}
            disabled={
              statusQuery.isFetching ||
              credentialsQuery.isFetching ||
              auditQuery.isFetching
            }
          >
            <RefreshIcon />
          </IconActionButton>
          <ActionButton
            variant="contained"
            startIcon={<ImportIcon />}
            onClick={() => setImportConfirmOpen(true)}
            disabled={importMutation.isPending}
          >
            {t('actions.import')}
          </ActionButton>
        </Stack>
      </Stack>

      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent>
          <Typography variant="h6" fontWeight={700} sx={{ mb: 1.5 }}>
            {t('status.title')}
          </Typography>
          {statusQuery.isError ? (
            <QueryErrorState
              error={statusQuery.error}
              onRetry={() => void statusQuery.refetch()}
              compact
            />
          ) : statusQuery.isLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
              <CircularProgress size={24} />
            </Box>
          ) : (
            statusQuery.data && (
              <Stack spacing={1.5}>
                <Box
                  sx={{
                    display: 'grid',
                    gridTemplateColumns: {
                      xs: 'repeat(2, minmax(0, 1fr))',
                      md: 'repeat(4, minmax(0, 1fr))',
                    },
                    gap: 2,
                  }}
                >
                  <Box>
                    <Typography variant="caption" color="text.secondary">
                      {t('status.state')}
                    </Typography>
                    <Box sx={{ mt: 0.5 }}>
                      <Chip
                        size="small"
                        color={
                          GATEWAY_STATE_COLORS[statusQuery.data.state] ?? 'default'
                        }
                        label={t(`status.states.${statusQuery.data.state}`, {
                          defaultValue: statusQuery.data.state,
                        })}
                      />
                    </Box>
                  </Box>
                  <Box>
                    <Typography variant="caption" color="text.secondary">
                      {t('status.reason')}
                    </Typography>
                    <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>
                      {statusQuery.data.reason || t('status.emptyValue')}
                    </Typography>
                  </Box>
                  <Box>
                    <Typography variant="caption" color="text.secondary">
                      {t('status.listenAddresses')}
                    </Typography>
                    <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                      {statusQuery.data.listen_addresses.length > 0
                        ? statusQuery.data.listen_addresses.join(', ')
                        : t('status.emptyValue')}
                    </Typography>
                  </Box>
                  <Box>
                    <Typography variant="caption" color="text.secondary">
                      {t('status.port')}
                    </Typography>
                    <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                      {statusQuery.data.port || t('status.emptyValue')}
                    </Typography>
                  </Box>
                  <Box>
                    <Typography variant="caption" color="text.secondary">
                      {t('status.connections')}
                    </Typography>
                    <Typography variant="body2">
                      {statusQuery.data.connections}
                    </Typography>
                  </Box>
                  <Box>
                    <Typography variant="caption" color="text.secondary">
                      {t('status.channels')}
                    </Typography>
                    <Typography variant="body2">
                      {statusQuery.data.channels}
                    </Typography>
                  </Box>
                  <Box>
                    <Typography variant="caption" color="text.secondary">
                      {t('status.runningLeases')}
                    </Typography>
                    <Typography variant="body2">
                      {statusQuery.data.running_leases}
                    </Typography>
                  </Box>
                </Box>
                <Divider />
                <Stack
                  direction={{ xs: 'column', sm: 'row' }}
                  spacing={1.5}
                  alignItems={{ xs: 'flex-start', sm: 'center' }}
                  justifyContent="space-between"
                >
                  <Typography variant="caption" color="text.secondary">
                    {t('status.configHint')}
                  </Typography>
                  <ActionButton
                    size="small"
                    onClick={() => navigate(settingsPath)}
                  >
                    {t('actions.openSettings')}
                  </ActionButton>
                </Stack>
              </Stack>
            )
          )}
        </CardContent>
      </Card>

      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent>
          <Typography variant="h6" fontWeight={700}>
            {t('credentials.title')}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            {t('credentials.subtitle')}
          </Typography>

          {credentialsQuery.isError ? (
            <QueryErrorState
              error={credentialsQuery.error}
              onRetry={() => void credentialsQuery.refetch()}
              compact
            />
          ) : credentialsQuery.isLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
              <CircularProgress size={26} />
            </Box>
          ) : credentials.length === 0 ? (
            <Box sx={{ py: 4, textAlign: 'center' }}>
              <Typography variant="body2" color="text.secondary">
                {t('credentials.empty')}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {t('credentials.emptyHint')}
              </Typography>
            </Box>
          ) : (
            <Box sx={{ overflowX: 'auto' }}>
              <Table
                size="small"
                sx={UNIFIED_TABLE_STYLES.getTableBase(false, false)}
              >
                <TableHead>
                  <TableRow>
                    <TableCell>{t('credentials.columns.alias')}</TableCell>
                    <TableCell>{t('credentials.columns.workspace')}</TableCell>
                    <TableCell>{t('credentials.columns.owner')}</TableCell>
                    <TableCell>{t('credentials.columns.address')}</TableCell>
                    <TableCell>{t('credentials.columns.username')}</TableCell>
                    <TableCell>{t('credentials.columns.status')}</TableCell>
                    <TableCell>{t('credentials.columns.secret')}</TableCell>
                    <TableCell>{t('credentials.columns.hostKeys')}</TableCell>
                    <TableCell>{t('credentials.columns.lastUsed')}</TableCell>
                    <TableCell>{t('credentials.columns.source')}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {credentials.map(credential => {
                    const tooltip = statusTooltip(credential)
                    return (
                      <TableRow key={credential.id} hover>
                        <TableCell>
                          <Typography variant="body2" fontWeight={600}>
                            {credential.alias}
                          </Typography>
                          {credential.note && (
                            <Typography variant="caption" color="text.secondary">
                              {credential.note}
                            </Typography>
                          )}
                        </TableCell>
                        <TableCell>
                          <Tooltip title={t('actions.openWorkspace')}>
                            <Button
                              size="small"
                              onClick={() =>
                                navigate(
                                  workspaceDetailPath(
                                    credential.workspace_id,
                                    'servers'
                                  )
                                )
                              }
                              sx={{ textTransform: 'none', px: 0.5 }}
                            >
                              {credential.workspace_name ||
                                workspaceName(credential.workspace_id)}
                            </Button>
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          <Stack spacing={0.5} alignItems="flex-start">
                            <Typography variant="body2">
                              {credential.owner_kind === 'platform'
                                ? t('credentials.owner.platform')
                                : t('credentials.owner.user', {
                                    account: credential.owner_account,
                                  })}
                            </Typography>
                            {credential.owner_kind === 'user' &&
                              credential.owner_whitelisted === false && (
                                <Chip
                                  size="small"
                                  color="warning"
                                  label={t('credentials.ownerNotWhitelisted')}
                                  sx={{
                                    height: 'auto',
                                    '& .MuiChip-label': {
                                      whiteSpace: 'normal',
                                      py: 0.25,
                                    },
                                  }}
                                />
                              )}
                          </Stack>
                        </TableCell>
                        <TableCell
                          sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}
                        >
                          {credential.host}:{credential.port}
                        </TableCell>
                        <TableCell sx={{ fontFamily: 'monospace' }}>
                          {credential.username}
                        </TableCell>
                        <TableCell>
                          <Tooltip title={tooltip} disableHoverListener={!tooltip}>
                            <Chip
                              size="small"
                              color={STATUS_COLORS[credential.status] ?? 'default'}
                              label={t(
                                `credentials.status.${credential.status}`,
                                { defaultValue: credential.status }
                              )}
                            />
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          {credential.secret_state === 'undecryptable' ? (
                            <Chip
                              size="small"
                              color="error"
                              label={t('credentials.secret.undecryptable')}
                              sx={{
                                height: 'auto',
                                maxWidth: 220,
                                '& .MuiChip-label': {
                                  whiteSpace: 'normal',
                                  py: 0.25,
                                },
                              }}
                            />
                          ) : (
                            <Typography variant="caption" color="text.secondary">
                              {t('credentials.secret.ok')}
                            </Typography>
                          )}
                        </TableCell>
                        <TableCell>
                          {credential.host_keys.length > 0 ? (
                            <Tooltip
                              title={credential.host_keys
                                .map(item => item.entry)
                                .join('\n')}
                            >
                              <Typography
                                variant="caption"
                                component="div"
                                sx={{
                                  fontFamily: 'monospace',
                                  wordBreak: 'break-all',
                                  whiteSpace: 'pre-line',
                                }}
                              >
                                {credential.host_keys
                                  .map(item => item.fingerprint || item.entry)
                                  .join('\n')}
                              </Typography>
                            </Tooltip>
                          ) : (
                            <Typography variant="caption" color="text.secondary">
                              {t('credentials.hostKeys.empty')}
                            </Typography>
                          )}
                        </TableCell>
                        <TableCell sx={{ whiteSpace: 'nowrap' }}>
                          {formatLastActiveTimeFromInput(
                            credential.last_used_at,
                            t('credentials.neverUsed')
                          )}
                        </TableCell>
                        <TableCell>
                          {t(`credentials.source.${credential.created_via}`, {
                            defaultValue: credential.created_via,
                          })}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </Box>
          )}
        </CardContent>
      </Card>

      {report && (
        <Card sx={CARD_VARIANTS.default.styles}>
          <CardContent>
            <Typography variant="h6" fontWeight={700} sx={{ mb: 1.5 }}>
              {t('import.resultTitle')}
            </Typography>
            <Box
              sx={{
                display: 'grid',
                gridTemplateColumns: { xs: '1fr', md: 'repeat(3, minmax(0, 1fr))' },
                gap: 2,
              }}
            >
              <Box>
                <Typography variant="subtitle2" fontWeight={700}>
                  {t('import.imported')}
                </Typography>
                {report.imported.length === 0 ? (
                  <Typography variant="body2" color="text.secondary">
                    {t('import.noneImported')}
                  </Typography>
                ) : (
                  <Stack spacing={0.75} sx={{ mt: 0.75 }}>
                    {report.imported.map(entry => (
                      <Box
                        key={`${entry.workspace_id}:${entry.alias}`}
                        sx={{ wordBreak: 'break-word' }}
                      >
                        <Typography variant="body2" fontWeight={600}>
                          {entry.alias}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                          {workspacesById.get(entry.workspace_id)?.name ??
                            workspaceName(entry.workspace_id)}{' '}
                          · {entry.resource_name}
                        </Typography>
                      </Box>
                    ))}
                  </Stack>
                )}
              </Box>
              <Box>
                <Typography variant="subtitle2" fontWeight={700}>
                  {t('import.already')}
                </Typography>
                <Typography variant="h5" fontWeight={700} sx={{ mt: 0.5 }}>
                  {t('import.alreadyCount', { count: report.already })}
                </Typography>
              </Box>
              <Box>
                <Typography variant="subtitle2" fontWeight={700}>
                  {t('import.skipped')}
                </Typography>
                {report.skipped.length === 0 ? (
                  <Typography variant="body2" color="text.secondary">
                    {t('import.noneSkipped')}
                  </Typography>
                ) : (
                  <Stack spacing={0.75} sx={{ mt: 0.75 }}>
                    {report.skipped.map((entry, index) => (
                      <Box
                        key={`${entry.workspace_id}:${entry.resource_name}:${index}`}
                        sx={{ wordBreak: 'break-word' }}
                      >
                        <Typography variant="body2" fontWeight={600}>
                          {entry.resource_name}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                          {workspacesById.get(entry.workspace_id)?.name ??
                            workspaceName(entry.workspace_id)}{' '}
                          · {entry.reason}
                        </Typography>
                      </Box>
                    ))}
                  </Stack>
                )}
              </Box>
            </Box>
            <Alert severity="warning" sx={{ mt: 2 }}>
              {t('import.securityHint')}
            </Alert>
          </CardContent>
        </Card>
      )}

      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent>
          <Typography variant="h6" fontWeight={700}>
            {t('audit.title')}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            {t('audit.subtitle')}
          </Typography>

          {auditQuery.isError ? (
            <QueryErrorState
              error={auditQuery.error}
              onRetry={() => void auditQuery.refetch()}
              compact
            />
          ) : auditQuery.isLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
              <CircularProgress size={24} />
            </Box>
          ) : auditItems.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              {t('audit.empty')}
            </Typography>
          ) : (
            <>
              <Box sx={{ overflowX: 'auto' }}>
                <Table
                  size="small"
                  sx={UNIFIED_TABLE_STYLES.getTableBase(false, false)}
                >
                  <TableHead>
                    <TableRow>
                      <TableCell>{t('audit.columns.time')}</TableCell>
                      <TableCell>{t('audit.columns.workspace')}</TableCell>
                      <TableCell>{t('audit.columns.event')}</TableCell>
                      <TableCell>{t('audit.columns.decision')}</TableCell>
                      <TableCell>{t('audit.columns.reason')}</TableCell>
                      <TableCell>{t('audit.columns.credential')}</TableCell>
                      <TableCell>{t('audit.columns.task')}</TableCell>
                      <TableCell>{t('audit.columns.actor')}</TableCell>
                      <TableCell>{t('audit.columns.detail')}</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {auditItems.map(item => (
                      <Fragment key={item.id}>
                        <TableRow hover>
                          <TableCell sx={{ whiteSpace: 'nowrap' }}>
                            {item.create_time
                              ? new Date(item.create_time).toLocaleString()
                              : '—'}
                          </TableCell>
                          <TableCell>{auditWorkspace(item)}</TableCell>
                          <TableCell>
                            {t(`audit.events.${item.event}`, {
                              defaultValue: item.event,
                            })}
                          </TableCell>
                          <TableCell>
                            <Chip
                              size="small"
                              color={item.allowed ? 'success' : 'error'}
                              label={t(
                                item.allowed
                                  ? 'audit.decision.allowed'
                                  : 'audit.decision.denied'
                              )}
                            />
                          </TableCell>
                          <TableCell>
                            {item.reason
                              ? t(`audit.reasons.${item.reason}`, {
                                  defaultValue: item.reason,
                                })
                              : '—'}
                          </TableCell>
                          <TableCell>
                            {item.credential_id
                              ? credentialAliases.get(item.credential_id) ??
                                t('audit.credentialFallback', {
                                  id: item.credential_id,
                                })
                              : '—'}
                          </TableCell>
                          <TableCell sx={{ wordBreak: 'break-all' }}>
                            {item.task_ref || '—'}
                          </TableCell>
                          <TableCell sx={{ wordBreak: 'break-all' }}>
                            {item.actor || '—'}
                          </TableCell>
                          <TableCell sx={{ wordBreak: 'break-word' }}>
                            <Stack spacing={0.5} alignItems="flex-start">
                              <Typography variant="body2">
                                {item.detail || '—'}
                              </Typography>
                              {item.input_head && (
                                <ActionButton
                                  size="small"
                                  tone="ghost"
                                  onClick={() => toggleAudit(item.id)}
                                >
                                  {expandedAudit.has(item.id)
                                    ? t('actions.hideInput')
                                    : t('actions.showInput')}
                                </ActionButton>
                              )}
                            </Stack>
                          </TableCell>
                        </TableRow>
                        {expandedAudit.has(item.id) && item.input_head && (
                          <TableRow>
                            <TableCell colSpan={9} sx={{ bgcolor: 'action.hover' }}>
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
                                {item.input_head}
                              </Box>
                            </TableCell>
                          </TableRow>
                        )}
                      </Fragment>
                    ))}
                  </TableBody>
                </Table>
              </Box>
              {auditQuery.hasNextPage && (
                <Box sx={{ display: 'flex', justifyContent: 'center', mt: 1.5 }}>
                  <ActionButton
                    size="small"
                    onClick={() => void auditQuery.fetchNextPage()}
                    disabled={auditQuery.isFetchingNextPage}
                  >
                    {t('actions.loadMore')}
                  </ActionButton>
                </Box>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <NekroDialog
        open={importConfirmOpen}
        onClose={() => !importMutation.isPending && setImportConfirmOpen(false)}
        title={t('import.confirmTitle')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton
              onClick={() => setImportConfirmOpen(false)}
              disabled={importMutation.isPending}
            >
              {t('import.cancel')}
            </ActionButton>
            <ActionButton
              variant="contained"
              onClick={() => importMutation.mutate()}
              disabled={importMutation.isPending}
            >
              {importMutation.isPending ? t('import.running') : t('import.confirm')}
            </ActionButton>
          </>
        }
      >
        <Typography variant="body2">{t('import.confirmContent')}</Typography>
      </NekroDialog>
    </Box>
  )
}
