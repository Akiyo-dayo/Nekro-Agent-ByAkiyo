/**
 * 工作区「服务器」标签
 *
 * 这一页把 SSH 网关的凭据、一次性操作与审计放在一起：上面是 CC 能用的服务器，
 * 下面是这些服务器上发生过的连接与命令。凭据的可见性与可编辑性全部来自后端下发的
 * `can_reveal` / `can_edit_connection` / `owner_whitelisted`，这里不按角色另推一套，
 * 否则前后端各有一套判据，迟早会在「聊天登记的行」上对不上。
 *
 * 明文只在 `revealTarget` 打开期间存在：走一次性请求，不进 React Query 缓存，
 * 关闭对话框即丢弃。查看动作本身由后端记审计，所以界面上写清楚这一点。
 */

import { Fragment, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
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
  Tooltip,
  Typography,
} from '@mui/material'
import {
  Add as AddIcon,
  ContentCopy as ContentCopyIcon,
  Delete as DeleteIcon,
  Edit as EditIcon,
  Refresh as RefreshIcon,
  VpnKey as VpnKeyIcon,
} from '@mui/icons-material'
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'

import ActionButton from '../../../components/common/ActionButton'
import IconActionButton from '../../../components/common/IconActionButton'
import NekroDialog from '../../../components/common/NekroDialog'
import QueryErrorState from '../../../components/common/QueryErrorState'
import { useNotification } from '../../../hooks/useNotification'
import { useIdentity } from '../../../hooks/useIdentity'
import { consoleApi } from '../../../services/api/console'
import {
  CREDENTIAL_MASK,
  isCredentialMask,
} from '../../../services/api/tenant'
import {
  SSH_GATEWAY_QUERY_KEYS,
  sshGatewayApi,
  type SshCredentialCreate,
  type SshCredentialSecretResponse,
  type SshCredentialTestResponse,
  type SshCredentialUpdate,
  type SshCredentialView,
  type SshDutyMode,
  type SshGatewayStatusResponse,
} from '../../../services/api/sshGateway'
import type { WorkspaceDetail } from '../../../services/api/workspace'
import { CARD_VARIANTS, UNIFIED_TABLE_STYLES } from '../../../theme/variants'
import { copyText } from '../../../utils/clipboard'
import { formatLastActiveTimeFromInput } from '../../../utils/time'

type FormMode = 'create' | 'edit'

interface CredentialFormState {
  ownerKind: 'platform' | 'user'
  ownerAccount: string
  adapterKey: string
  instanceKey: string
  alias: string
  host: string
  port: string
  username: string
  password: string
  privateKey: string
  passphrase: string
  hostKeys: string
  note: string
}

interface InstancePairOption {
  adapterKey: string
  instanceKey: string
  label: string
}

const EMPTY_FORM: CredentialFormState = {
  ownerKind: 'platform',
  ownerAccount: '',
  adapterKey: '',
  instanceKey: '',
  alias: '',
  host: '',
  port: '22',
  username: '',
  password: '',
  privateKey: '',
  passphrase: '',
  hostKeys: '',
  note: '',
}

const STATUS_COLORS: Record<string, 'success' | 'default' | 'warning' | 'error'> = {
  active: 'success',
  disabled: 'default',
  suspended: 'warning',
  auth_failed: 'error',
}

const RUNBOOK_MAX_LENGTH = 8000

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return String(error ?? '')
}

function splitHostKeys(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean)
}

function instancePairId(adapterKey: string, instanceKey: string): string {
  return `${adapterKey}:${instanceKey}`
}

/** 后端给的是自由字符串；只认三种合法值，其余按「跟随默认」处理 */
function normalizeDutyMode(value: string): SshDutyMode {
  return value === 'strict' || value === 'duty' ? value : ''
}

export default function ServersTab({ workspace }: { workspace: WorkspaceDetail }) {
  const { t } = useTranslation('workspace')
  const navigate = useNavigate()
  const notification = useNotification()
  const queryClient = useQueryClient()
  const {
    isPlatformOwner,
    isLoading: identityLoading,
  } = useIdentity()

  const workspaceId = workspace.id
  const platformViewer = isPlatformOwner && !identityLoading

  const [formOpen, setFormOpen] = useState(false)
  const [formMode, setFormMode] = useState<FormMode>('create')
  const [editing, setEditing] = useState<SshCredentialView | null>(null)
  const [form, setForm] = useState<CredentialFormState>(EMPTY_FORM)

  const [deleteTarget, setDeleteTarget] = useState<SshCredentialView | null>(null)
  const [revealTarget, setRevealTarget] = useState<SshCredentialView | null>(null)
  const [revealData, setRevealData] = useState<SshCredentialSecretResponse | null>(null)
  const [revealLoading, setRevealLoading] = useState(false)
  const [testTarget, setTestTarget] = useState<SshCredentialView | null>(null)
  const [testResult, setTestResult] = useState<SshCredentialTestResponse | null>(null)
  const [dutyTarget, setDutyTarget] = useState<SshCredentialView | null>(null)
  const [dutyMode, setDutyMode] = useState<SshDutyMode>('')
  const [runbookTarget, setRunbookTarget] = useState<SshCredentialView | null>(null)
  const [runbookDraft, setRunbookDraft] = useState<string | null>(null)
  const [entryToDelete, setEntryToDelete] = useState<number | null>(null)
  const [expandedAudit, setExpandedAudit] = useState<Set<number>>(new Set())

  const credentialsQuery = useQuery({
    queryKey: SSH_GATEWAY_QUERY_KEYS.workspaceCredentials(workspaceId),
    queryFn: () => sshGatewayApi.listCredentials(workspaceId),
    staleTime: 10_000,
    retry: false,
  })

  const statusQuery = useQuery<SshGatewayStatusResponse>({
    queryKey: SSH_GATEWAY_QUERY_KEYS.status(),
    queryFn: sshGatewayApi.getStatus,
    enabled: platformViewer,
    refetchInterval: 10_000,
    retry: false,
  })

  const runbookCredentialId = runbookTarget?.id ?? 0
  const runbookQuery = useQuery({
    queryKey: SSH_GATEWAY_QUERY_KEYS.runbook(workspaceId, runbookCredentialId),
    queryFn: () =>
      sshGatewayApi.getRunbook(workspaceId, runbookCredentialId),
    enabled: platformViewer && !!runbookTarget,
    retry: false,
  })

  // 首次拿到数据时填进可编辑的草稿；之后保存/删除引起的刷新不覆盖用户正在改的内容
  useEffect(() => {
    if (runbookDraft !== null) return
    if (!runbookQuery.data) return
    setRunbookDraft(runbookQuery.data.runbook)
  }, [runbookDraft, runbookQuery.data])

  const instanceOptionsQuery = useQuery({
    queryKey: ['console', 'instance-options'],
    queryFn: () => consoleApi.listInstanceOptions(),
    enabled: formOpen && formMode === 'create' && !identityLoading,
    staleTime: 30_000,
    retry: false,
  })

  const auditQuery = useInfiniteQuery({
    queryKey: [...SSH_GATEWAY_QUERY_KEYS.audit(workspaceId), 'infinite'],
    queryFn: ({ pageParam }) =>
      sshGatewayApi.listAudit({
        workspace_id: workspaceId,
        limit: 50,
        before_id: pageParam,
      }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: lastPage => lastPage.next_before_id ?? undefined,
    retry: false,
  })

  const credentials = useMemo(
    () => credentialsQuery.data?.items ?? [],
    [credentialsQuery.data]
  )

  /**
   * 绑在这个工作区上、当前账号能选的实例对
   *
   * 由凭据列表接口直接给出（租户只看到自己的实例）；实例候选只用来补一个好认的名字。
   * 列表为空时表单退回手填，后端仍会自己校验实例是否绑在这个工作区上。
   */
  const instancePairs = useMemo<InstancePairOption[]>(() => {
    const names = new Map<string, string>()
    for (const option of instanceOptionsQuery.data?.items ?? []) {
      names.set(instancePairId(option.adapter_key, option.instance_key), option.display_name)
    }
    return (credentialsQuery.data?.bound_instances ?? []).map(item => {
      const displayName = names.get(instancePairId(item.adapter_key, item.instance_key))
      return {
        adapterKey: item.adapter_key,
        instanceKey: item.instance_key,
        label: `${displayName || item.instance_key} (${item.adapter_key}/${item.instance_key})`,
      }
    })
  }, [instanceOptionsQuery.data, credentialsQuery.data])

  const boundChannelCount = credentialsQuery.data?.bound_channel_count ?? 0

  const credentialAliases = useMemo(() => {
    const map = new Map<number, string>()
    for (const credential of credentials) {
      map.set(credential.id, credential.alias)
    }
    return map
  }, [credentials])

  const auditItems = useMemo(
    () => auditQuery.data?.pages.flatMap(page => page.items) ?? [],
    [auditQuery.data]
  )

  const refreshCredentials = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: SSH_GATEWAY_QUERY_KEYS.workspaceCredentials(workspaceId),
      }),
      queryClient.invalidateQueries({
        queryKey: SSH_GATEWAY_QUERY_KEYS.audit(workspaceId),
      }),
    ])
  }

  const openCreate = () => {
    const firstPair = instancePairs[0]
    setFormMode('create')
    setEditing(null)
    setForm({
      ...EMPTY_FORM,
      ownerKind: platformViewer ? 'platform' : 'user',
      adapterKey: firstPair?.adapterKey ?? '',
      instanceKey: firstPair?.instanceKey ?? '',
    })
    setFormOpen(true)
  }

  const openEdit = (credential: SshCredentialView) => {
    setFormMode('edit')
    setEditing(credential)
    setForm({
      ownerKind: credential.owner_kind === 'user' ? 'user' : 'platform',
      ownerAccount: credential.owner_account,
      adapterKey: credential.adapter_key,
      instanceKey: credential.instance_key,
      alias: credential.alias,
      host: credential.host,
      port: String(credential.port),
      username: credential.username,
      password: credential.has_password ? CREDENTIAL_MASK : '',
      privateKey: credential.has_private_key ? CREDENTIAL_MASK : '',
      passphrase: credential.has_passphrase ? CREDENTIAL_MASK : '',
      hostKeys: credential.host_keys.map(item => item.entry).join('\n'),
      note: credential.note,
    })
    setFormOpen(true)
  }

  const saveMutation = useMutation({
    mutationFn: async (
      payload:
        | { mode: 'create'; body: SshCredentialCreate }
        | { mode: 'edit'; credentialId: number; body: SshCredentialUpdate }
    ) => {
      if (payload.mode === 'create') {
        return sshGatewayApi.createCredential(workspaceId, payload.body)
      }
      return sshGatewayApi.updateCredential(
        workspaceId,
        payload.credentialId,
        payload.body
      )
    },
    onSuccess: (_data, payload) => {
      notification.success(
        t(
          payload.mode === 'create'
            ? 'detail.servers.notifications.created'
            : 'detail.servers.notifications.updated'
        )
      )
      setFormOpen(false)
      setEditing(null)
      // 保存成功后立刻抹掉表单里的明文，别让它在组件 state 里留到下一次打开
      setForm(EMPTY_FORM)
      void refreshCredentials()
    },
    onError: (error, payload) => {
      notification.error(
        t(
          payload.mode === 'create'
            ? 'detail.servers.notifications.createFailed'
            : 'detail.servers.notifications.updateFailed',
          { message: errorMessage(error) }
        )
      )
    },
  })

  const toggleMutation = useMutation({
    mutationFn: (variables: { credential: SshCredentialView; enabled: boolean }) =>
      sshGatewayApi.setCredentialEnabled(
        workspaceId,
        variables.credential.id,
        variables.enabled
      ),
    onSuccess: (_data, variables) => {
      notification.success(
        t(
          variables.enabled
            ? 'detail.servers.notifications.enabled'
            : 'detail.servers.notifications.disabled'
        )
      )
      void refreshCredentials()
    },
    onError: error => {
      notification.error(
        t('detail.servers.notifications.toggleFailed', { message: errorMessage(error) })
      )
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (credential: SshCredentialView) =>
      sshGatewayApi.deleteCredential(workspaceId, credential.id),
    onSuccess: () => {
      notification.success(t('detail.servers.notifications.deleted'))
      setDeleteTarget(null)
      void refreshCredentials()
    },
    onError: error => {
      notification.error(
        t('detail.servers.notifications.deleteFailed', { message: errorMessage(error) })
      )
    },
  })

  const testMutation = useMutation({
    mutationFn: (credential: SshCredentialView) =>
      sshGatewayApi.testCredential(workspaceId, credential.id),
    onSuccess: result => {
      setTestResult(result)
      void refreshCredentials()
    },
    onError: error => {
      notification.error(
        t('detail.servers.notifications.testFailed', { message: errorMessage(error) })
      )
      setTestTarget(null)
    },
  })

  const acceptMutation = useMutation({
    mutationFn: (variables: {
      credentialId: number
      entry: string
      fingerprint: string
    }) =>
      sshGatewayApi.acceptHostKey(workspaceId, variables.credentialId, {
        entry: variables.entry,
        fingerprint: variables.fingerprint,
      }),
    onSuccess: () => {
      notification.success(t('detail.servers.test.acceptSuccess'))
      setTestTarget(null)
      setTestResult(null)
      void refreshCredentials()
    },
    onError: error => {
      notification.error(
        t('detail.servers.notifications.acceptFailed', {
          message: errorMessage(error),
        })
      )
    },
  })

  const dutyModeMutation = useMutation({
    mutationFn: (variables: { credentialId: number; dutyMode: SshDutyMode }) =>
      sshGatewayApi.setDutyMode(
        workspaceId,
        variables.credentialId,
        variables.dutyMode
      ),
    onSuccess: () => {
      notification.success(t('detail.servers.duty.saved'))
      setDutyTarget(null)
      void queryClient.invalidateQueries({
        queryKey: SSH_GATEWAY_QUERY_KEYS.workspaceCredentials(workspaceId),
      })
    },
    onError: error => {
      notification.error(
        t('detail.servers.notifications.updateFailed', {
          message: errorMessage(error),
        })
      )
    },
  })

  const runbookSaveMutation = useMutation({
    mutationFn: (variables: { credentialId: number; runbook: string }) =>
      sshGatewayApi.updateRunbook(
        workspaceId,
        variables.credentialId,
        variables.runbook
      ),
    onSuccess: (data, variables) => {
      notification.success(t('detail.servers.runbook.saved'))
      setRunbookDraft(data.runbook)
      void queryClient.invalidateQueries({
        queryKey: SSH_GATEWAY_QUERY_KEYS.runbook(
          workspaceId,
          variables.credentialId
        ),
      })
    },
    onError: error => {
      notification.error(
        t('detail.servers.notifications.updateFailed', {
          message: errorMessage(error),
        })
      )
    },
  })

  const runbookDeleteMutation = useMutation({
    mutationFn: (variables: { credentialId: number; entryId: number }) =>
      sshGatewayApi.deleteRunbookEntry(
        workspaceId,
        variables.credentialId,
        variables.entryId
      ),
    onSuccess: (_data, variables) => {
      setEntryToDelete(null)
      void queryClient.invalidateQueries({
        queryKey: SSH_GATEWAY_QUERY_KEYS.runbook(
          workspaceId,
          variables.credentialId
        ),
      })
    },
    onError: error => {
      notification.error(
        t('detail.servers.notifications.deleteFailed', {
          message: errorMessage(error),
        })
      )
    },
  })

  const closeForm = () => {
    if (saveMutation.isPending) return
    setFormOpen(false)
    setEditing(null)
    // 取消也要抹掉刚输入的密码/私钥，避免留在 state 里
    setForm(EMPTY_FORM)
  }

  const updateForm = (patch: Partial<CredentialFormState>) => {
    setForm(previous => ({ ...previous, ...patch }))
  }

  const submitForm = () => {
    const alias = form.alias.trim()
    const host = form.host.trim()
    const username = form.username.trim()
    const port = Number(form.port)
    if (!alias || !host || !username) {
      notification.warning(t('detail.servers.form.required'))
      return
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      notification.warning(t('detail.servers.form.portInvalid'))
      return
    }
    if (form.ownerKind === 'user') {
      if (!form.ownerAccount.trim() || !form.adapterKey.trim() || !form.instanceKey.trim()) {
        notification.warning(t('detail.servers.form.userOwnerRequired'))
        return
      }
    }

    if (formMode === 'create') {
      const hasSecret = form.password.length > 0 || form.privateKey.length > 0
      if (!hasSecret) {
        notification.warning(t('detail.servers.form.secretRequired'))
        return
      }
      saveMutation.mutate({
        mode: 'create',
        body: {
          owner_kind: form.ownerKind,
          owner_account: form.ownerKind === 'user' ? form.ownerAccount.trim() : '',
          adapter_key: form.ownerKind === 'user' ? form.adapterKey.trim() : '',
          instance_key: form.ownerKind === 'user' ? form.instanceKey.trim() : '',
          alias,
          host,
          port,
          username,
          password: isCredentialMask(form.password) ? '' : form.password,
          private_key: isCredentialMask(form.privateKey) ? '' : form.privateKey,
          passphrase: isCredentialMask(form.passphrase) ? '' : form.passphrase,
          host_keys: splitHostKeys(form.hostKeys),
          note: form.note.trim(),
        },
      })
      return
    }

    if (!editing) return
    /** 聊天登记的行对租户只允许改备注；其余字段不提交，避免后端按「改连接信息」拒绝 */
    const body: SshCredentialUpdate = { note: form.note.trim() }
    if (editing.can_edit_connection) {
      body.alias = alias
      body.host = host
      body.port = port
      body.username = username
      body.password = form.password
      body.private_key = form.privateKey
      body.passphrase = form.passphrase
      body.host_keys = splitHostKeys(form.hostKeys)
    }
    saveMutation.mutate({ mode: 'edit', credentialId: editing.id, body })
  }

  const openReveal = async (credential: SshCredentialView) => {
    setRevealTarget(credential)
    setRevealData(null)
    setRevealLoading(true)
    try {
      // 一次性请求：结果只放进本地 state，不进 React Query 缓存
      const secret = await sshGatewayApi.getCredentialSecret(workspaceId, credential.id)
      setRevealData(secret)
    } catch (error) {
      notification.error(
        t('detail.servers.notifications.revealFailed', {
          message: errorMessage(error),
        })
      )
      setRevealTarget(null)
    } finally {
      setRevealLoading(false)
    }
  }

  const closeReveal = () => {
    // 请求还在路上时不让关：迟到的响应会把明文写回已经换过目标的对话框
    if (revealLoading) return
    setRevealTarget(null)
    setRevealData(null)
  }

  const openTest = (credential: SshCredentialView) => {
    setTestTarget(credential)
    setTestResult(null)
    testMutation.mutate(credential)
  }

  const closeTest = () => {
    // 测试或接受还在跑时关掉，迟到的结果会落到下一次打开的对话框里
    if (testMutation.isPending || acceptMutation.isPending) return
    setTestTarget(null)
    setTestResult(null)
  }

  const openDutyMode = (credential: SshCredentialView) => {
    setDutyTarget(credential)
    setDutyMode(normalizeDutyMode(credential.duty_mode))
  }

  const closeDutyMode = () => {
    if (dutyModeMutation.isPending) return
    setDutyTarget(null)
  }

  const submitDutyMode = () => {
    if (!dutyTarget) return
    dutyModeMutation.mutate({ credentialId: dutyTarget.id, dutyMode })
  }

  const openRunbook = (credential: SshCredentialView) => {
    setRunbookTarget(credential)
    setRunbookDraft(null)
  }

  const closeRunbook = () => {
    if (runbookSaveMutation.isPending || runbookDeleteMutation.isPending) return
    setRunbookTarget(null)
    setRunbookDraft(null)
    setEntryToDelete(null)
  }

  const submitRunbook = () => {
    if (!runbookTarget || runbookDraft === null) return
    if (runbookDraft.length > RUNBOOK_MAX_LENGTH) return
    runbookSaveMutation.mutate({
      credentialId: runbookTarget.id,
      runbook: runbookDraft,
    })
  }

  const confirmDeleteRunbookEntry = () => {
    if (!runbookTarget || entryToDelete === null) return
    runbookDeleteMutation.mutate({
      credentialId: runbookTarget.id,
      entryId: entryToDelete,
    })
  }

  const copySecret = async (value: string) => {
    const copied = await copyText(value)
    if (copied) {
      notification.success(t('detail.servers.reveal.copied'))
    } else {
      notification.error(t('detail.servers.reveal.copyFailed'))
    }
  }

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

  /** status_reason 目前只会落认证失败；未知值原样显示，避免把自由文本吞掉 */
  const statusReasonLabel = (reason: string): string => {
    if (reason === 'auth_failed') {
      return t('detail.servers.statusReasons.auth_failed')
    }
    return t(`detail.servers.audit.reasons.${reason}`, { defaultValue: reason })
  }

  const statusTooltip = (credential: SshCredentialView): string => {
    const lines: string[] = []
    if (credential.status_reason) {
      lines.push(
        t('detail.servers.statusReason', {
          reason: statusReasonLabel(credential.status_reason),
        })
      )
    }
    if (credential.last_error) {
      lines.push(t('detail.servers.lastError', { message: credential.last_error }))
    }
    return lines.join('\n')
  }

  const ownerLabel = (credential: SshCredentialView): string =>
    credential.owner_kind === 'platform'
      ? t('detail.servers.owner.platform')
      : t('detail.servers.owner.user', { account: credential.owner_account })

  const sourceLabel = (credential: SshCredentialView): string =>
    t(`detail.servers.source.${credential.created_via}`, {
      defaultValue: credential.created_via,
    })

  const canEditConnection = formMode === 'create' || !!editing?.can_edit_connection
  const hostKeyMismatch =
    testResult?.reason === 'host_key_mismatch' &&
    !!testResult.presented_entry &&
    !!testResult.presented_fingerprint
  const acceptingHostKey = hostKeyMismatch && !!testTarget?.can_edit_connection

  const defaultDutyModeLabel =
    statusQuery.data?.default_duty_mode === 'duty'
      ? t('detail.servers.duty.modeDuty')
      : t('detail.servers.duty.modeStrict')
  const runbookText = runbookDraft ?? ''
  const runbookOverLimit = runbookText.length > RUNBOOK_MAX_LENGTH
  const runbookEntries = runbookQuery.data?.entries ?? []

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <Stack
        direction={{ xs: 'column', sm: 'row' }}
        alignItems={{ xs: 'flex-start', sm: 'center' }}
        justifyContent="space-between"
        spacing={1.5}
      >
        <Box>
          <Typography variant="h6" fontWeight={700}>
            {t('detail.servers.title')}
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {t('detail.servers.subtitle')}
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          <IconActionButton
            aria-label={t('detail.servers.actions.refresh')}
            onClick={() => {
              void credentialsQuery.refetch()
              void auditQuery.refetch()
              // 租户身份不碰状态接口，避免冷启动时打一个必然 403 的请求
              if (platformViewer) void statusQuery.refetch()
            }}
            disabled={
              credentialsQuery.isFetching ||
              auditQuery.isFetching ||
              (platformViewer && statusQuery.isFetching)
            }
          >
            <RefreshIcon />
          </IconActionButton>
          <ActionButton
            variant="contained"
            startIcon={<AddIcon />}
            onClick={openCreate}
          >
            {t('detail.servers.actions.create')}
          </ActionButton>
        </Stack>
      </Stack>

      {platformViewer && statusQuery.data && statusQuery.data.state !== 'running' && (
        <Alert
          severity={statusQuery.data.state === 'off' ? 'info' : 'warning'}
          action={
            <ActionButton
              tone="secondary"
              size="small"
              onClick={() =>
                navigate(
                  `/settings/system?category=${encodeURIComponent(
                    t('detail.servers.gateway.settingsCategory')
                  )}&search=SSH_GATEWAY_ENABLED`
                )
              }
            >
              {t('detail.servers.actions.openSettings')}
            </ActionButton>
          }
        >
          <Typography variant="body2" fontWeight={600}>
            {t('detail.servers.gateway.title')}
          </Typography>
          <Typography variant="body2">
            {statusQuery.data.state === 'off'
              ? t('detail.servers.gateway.off')
              : t('detail.servers.gateway.state', {
                  state: t(`detail.servers.gateway.states.${statusQuery.data.state}`, {
                    defaultValue: statusQuery.data.state,
                  }),
                })}
          </Typography>
          {statusQuery.data.reason && (
            <Typography variant="body2">
              {t('detail.servers.gateway.reason', {
                reason: statusQuery.data.reason,
              })}
            </Typography>
          )}
        </Alert>
      )}

      {platformViewer &&
        boundChannelCount > 1 &&
        credentials.some(item => item.owner_kind === 'platform') && (
          <Alert severity="warning">
            {t('detail.servers.s3Warning', { count: boundChannelCount })}
          </Alert>
        )}

      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent sx={{ p: 0 }}>
          {credentialsQuery.isError ? (
            <QueryErrorState
              error={credentialsQuery.error}
              onRetry={() => void credentialsQuery.refetch()}
            />
          ) : credentialsQuery.isLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 5 }}>
              <CircularProgress size={28} />
            </Box>
          ) : credentials.length === 0 ? (
            <Box sx={{ py: 6, px: 2, textAlign: 'center' }}>
              <Typography variant="subtitle1" fontWeight={600}>
                {t('detail.servers.empty.title')}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {t('detail.servers.empty.hint')}
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
                    <TableCell>{t('detail.servers.columns.alias')}</TableCell>
                    <TableCell>{t('detail.servers.columns.owner')}</TableCell>
                    <TableCell>{t('detail.servers.columns.address')}</TableCell>
                    <TableCell>{t('detail.servers.columns.username')}</TableCell>
                    <TableCell>{t('detail.servers.columns.status')}</TableCell>
                    <TableCell>{t('detail.servers.columns.secret')}</TableCell>
                    <TableCell>{t('detail.servers.columns.hostKeys')}</TableCell>
                    <TableCell>{t('detail.servers.columns.lastUsed')}</TableCell>
                    <TableCell>{t('detail.servers.columns.source')}</TableCell>
                    <TableCell>{t('detail.servers.columns.actions')}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {credentials.map(credential => {
                    const tooltip = statusTooltip(credential)
                    return (
                      <TableRow key={credential.id} hover>
                        <TableCell>
                          <Stack spacing={0.25}>
                            <Typography variant="body2" fontWeight={600}>
                              {credential.alias}
                            </Typography>
                            {credential.note && (
                              <Typography variant="caption" color="text.secondary">
                                {credential.note}
                              </Typography>
                            )}
                          </Stack>
                        </TableCell>
                        <TableCell>
                          <Stack spacing={0.5} alignItems="flex-start">
                            <Typography variant="body2">
                              {ownerLabel(credential)}
                            </Typography>
                            {credential.owner_kind === 'user' &&
                              credential.owner_whitelisted === false && (
                                <Chip
                                  size="small"
                                  color="warning"
                                  label={t('detail.servers.ownerNotWhitelisted')}
                                  sx={{
                                    height: 'auto',
                                    '& .MuiChip-label': {
                                      whiteSpace: 'normal',
                                      py: 0.25,
                                    },
                                  }}
                                />
                              )}
                            {platformViewer &&
                              credential.owner_kind === 'platform' &&
                              credential.effective_duty_mode === 'duty' && (
                                <Tooltip title={t('detail.servers.duty.badgeHint')}>
                                  <Chip
                                    size="small"
                                    color="warning"
                                    label={t('detail.servers.duty.badge')}
                                    sx={{
                                      height: 'auto',
                                      '& .MuiChip-label': {
                                        whiteSpace: 'normal',
                                        py: 0.25,
                                      },
                                    }}
                                  />
                                </Tooltip>
                              )}
                          </Stack>
                        </TableCell>
                        <TableCell sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>
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
                              label={t(`detail.servers.status.${credential.status}`, {
                                defaultValue: credential.status,
                              })}
                            />
                          </Tooltip>
                        </TableCell>
                        <TableCell>
                          {credential.secret_state === 'undecryptable' ? (
                            <Chip
                              size="small"
                              color="error"
                              label={t('detail.servers.secret.undecryptable')}
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
                              {t('detail.servers.secret.ok')}
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
                              {t('detail.servers.hostKeys.empty')}
                            </Typography>
                          )}
                        </TableCell>
                        <TableCell sx={{ whiteSpace: 'nowrap' }}>
                          {formatLastActiveTimeFromInput(
                            credential.last_used_at,
                            t('detail.servers.neverUsed')
                          )}
                        </TableCell>
                        <TableCell>{sourceLabel(credential)}</TableCell>
                        <TableCell>
                          <Stack
                            direction="row"
                            spacing={0.5}
                            flexWrap="wrap"
                            useFlexGap
                          >
                            {credential.can_reveal && (
                              <ActionButton
                                size="small"
                                tone="ghost"
                                startIcon={<VpnKeyIcon fontSize="small" />}
                                onClick={() => void openReveal(credential)}
                              >
                                {t('detail.servers.actions.reveal')}
                              </ActionButton>
                            )}
                            {platformViewer &&
                              credential.owner_kind === 'platform' && (
                                <>
                                  <ActionButton
                                    size="small"
                                    tone="ghost"
                                    onClick={() => openDutyMode(credential)}
                                  >
                                    {t('detail.servers.actions.dutyMode')}
                                  </ActionButton>
                                  <ActionButton
                                    size="small"
                                    tone="ghost"
                                    onClick={() => openRunbook(credential)}
                                  >
                                    {t('detail.servers.actions.runbook')}
                                  </ActionButton>
                                </>
                              )}
                            <ActionButton
                              size="small"
                              tone="ghost"
                              startIcon={<EditIcon fontSize="small" />}
                              onClick={() => openEdit(credential)}
                            >
                              {t('detail.servers.actions.edit')}
                            </ActionButton>
                            <ActionButton
                              size="small"
                              tone="ghost"
                              onClick={() => openTest(credential)}
                            >
                              {t('detail.servers.actions.test')}
                            </ActionButton>
                            {/* 聊天登记的行租户也能停用、再启用（只是看不到明文、改不了连接信息）；
                                登录失败停用的行，启用前仍要先测试通过，由后端把关 */}
                            {credential.status !== 'suspended' && (
                              <ActionButton
                                size="small"
                                tone="ghost"
                                onClick={() =>
                                  toggleMutation.mutate({
                                    credential,
                                    enabled: credential.status === 'disabled',
                                  })
                                }
                                disabled={toggleMutation.isPending}
                              >
                                {credential.status === 'disabled'
                                  ? t('detail.servers.actions.enable')
                                  : t('detail.servers.actions.disable')}
                              </ActionButton>
                            )}
                            <ActionButton
                              size="small"
                              tone="ghost"
                              startIcon={<DeleteIcon fontSize="small" />}
                              sx={{ color: 'error.main' }}
                              onClick={() => setDeleteTarget(credential)}
                            >
                              {t('detail.servers.actions.delete')}
                            </ActionButton>
                          </Stack>
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

      <Card sx={CARD_VARIANTS.default.styles}>
        <CardContent>
          <Typography variant="h6" fontWeight={700}>
            {t('detail.servers.audit.title')}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            {t('detail.servers.audit.subtitle')}
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
              {t('detail.servers.audit.empty')}
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
                      <TableCell>{t('detail.servers.audit.columns.time')}</TableCell>
                      <TableCell>{t('detail.servers.audit.columns.event')}</TableCell>
                      <TableCell>
                        {t('detail.servers.audit.columns.decision')}
                      </TableCell>
                      <TableCell>{t('detail.servers.audit.columns.reason')}</TableCell>
                      <TableCell>
                        {t('detail.servers.audit.columns.credential')}
                      </TableCell>
                      <TableCell>{t('detail.servers.audit.columns.task')}</TableCell>
                      <TableCell>{t('detail.servers.audit.columns.actor')}</TableCell>
                      <TableCell>{t('detail.servers.audit.columns.detail')}</TableCell>
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
                          <TableCell>
                            {t(`detail.servers.audit.events.${item.event}`, {
                              defaultValue: item.event,
                            })}
                          </TableCell>
                          <TableCell>
                            <Chip
                              size="small"
                              color={item.allowed ? 'success' : 'error'}
                              label={t(
                                item.allowed
                                  ? 'detail.servers.audit.decision.allowed'
                                  : 'detail.servers.audit.decision.denied'
                              )}
                            />
                          </TableCell>
                          <TableCell>
                            {item.reason
                              ? t(`detail.servers.audit.reasons.${item.reason}`, {
                                  defaultValue: item.reason,
                                })
                              : '—'}
                          </TableCell>
                          <TableCell>
                            {item.credential_id
                              ? credentialAliases.get(item.credential_id) ??
                                t('detail.servers.audit.credentialFallback', {
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
                                    ? t('detail.servers.audit.hideInput')
                                    : t('detail.servers.audit.showInput')}
                                </ActionButton>
                              )}
                            </Stack>
                          </TableCell>
                        </TableRow>
                        {expandedAudit.has(item.id) && item.input_head && (
                          <TableRow>
                            <TableCell
                              colSpan={8}
                              sx={{ bgcolor: 'action.hover' }}
                            >
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
                    {t('detail.servers.audit.loadMore')}
                  </ActionButton>
                </Box>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <NekroDialog
        open={formOpen}
        onClose={closeForm}
        title={
          formMode === 'create'
            ? t('detail.servers.form.createTitle')
            : t('detail.servers.form.editTitle')
        }
        maxWidth="md"
        actions={
          <>
            <ActionButton
              onClick={closeForm}
              disabled={saveMutation.isPending}
            >
              {t('detail.servers.form.cancel')}
            </ActionButton>
            <ActionButton
              variant="contained"
              onClick={submitForm}
              disabled={saveMutation.isPending}
            >
              {formMode === 'create'
                ? t('detail.servers.form.create')
                : t('detail.servers.form.save')}
            </ActionButton>
          </>
        }
      >
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          {formMode === 'edit' && editing && !editing.can_edit_connection && (
            <Alert severity="info">
              {t('detail.servers.form.noteOnlyHint')}
            </Alert>
          )}

          <Box
            sx={{
              display: 'grid',
              gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' },
              gap: 2,
            }}
          >
            <TextField
              select
              size="small"
              label={t('detail.servers.form.ownerKind')}
              value={form.ownerKind}
              onChange={event =>
                updateForm({ ownerKind: event.target.value as 'platform' | 'user' })
              }
              disabled={formMode === 'edit' || !canEditConnection}
            >
              {platformViewer && (
                <MenuItem value="platform">
                  {t('detail.servers.form.ownerPlatform')}
                </MenuItem>
              )}
              <MenuItem value="user">
                {t('detail.servers.form.ownerUser')}
              </MenuItem>
            </TextField>

            <TextField
              size="small"
              label={t('detail.servers.form.alias')}
              helperText={t('detail.servers.form.aliasHint')}
              value={form.alias}
              onChange={event => updateForm({ alias: event.target.value })}
              disabled={!canEditConnection}
              inputProps={{ maxLength: 32 }}
            />

            {form.ownerKind === 'user' && (
              <>
                <TextField
                  size="small"
                  label={t('detail.servers.form.ownerAccount')}
                  helperText={t('detail.servers.form.ownerAccountHint')}
                  value={form.ownerAccount}
                  onChange={event =>
                    updateForm({ ownerAccount: event.target.value })
                  }
                  disabled={formMode === 'edit' || !canEditConnection}
                />
                {instancePairs.length > 0 ? (
                  <TextField
                    select
                    size="small"
                    label={t('detail.servers.form.instance')}
                    helperText={t('detail.servers.form.instanceHint')}
                    value={
                      form.adapterKey && form.instanceKey
                        ? instancePairId(form.adapterKey, form.instanceKey)
                        : ''
                    }
                    onChange={event => {
                      const selected = instancePairs.find(
                        item =>
                          instancePairId(item.adapterKey, item.instanceKey) ===
                          event.target.value
                      )
                      updateForm({
                        adapterKey: selected?.adapterKey ?? '',
                        instanceKey: selected?.instanceKey ?? '',
                      })
                    }}
                    disabled={formMode === 'edit' || !canEditConnection}
                  >
                    <MenuItem value="" disabled>
                      —
                    </MenuItem>
                    {instancePairs.map(item => (
                      <MenuItem
                        key={instancePairId(item.adapterKey, item.instanceKey)}
                        value={instancePairId(item.adapterKey, item.instanceKey)}
                      >
                        {item.label}
                      </MenuItem>
                    ))}
                  </TextField>
                ) : (
                  <>
                    <TextField
                      size="small"
                      label={t('detail.servers.form.adapterKey')}
                      helperText={t('detail.servers.form.manualInstanceHint')}
                      value={form.adapterKey}
                      onChange={event =>
                        updateForm({ adapterKey: event.target.value })
                      }
                      disabled={formMode === 'edit' || !canEditConnection}
                    />
                    <TextField
                      size="small"
                      label={t('detail.servers.form.instanceKey')}
                      value={form.instanceKey}
                      onChange={event =>
                        updateForm({ instanceKey: event.target.value })
                      }
                      disabled={formMode === 'edit' || !canEditConnection}
                    />
                  </>
                )}
              </>
            )}

            <TextField
              size="small"
              label={t('detail.servers.form.host')}
              value={form.host}
              onChange={event => updateForm({ host: event.target.value })}
              disabled={!canEditConnection}
            />
            <TextField
              size="small"
              type="number"
              label={t('detail.servers.form.port')}
              value={form.port}
              onChange={event => updateForm({ port: event.target.value })}
              disabled={!canEditConnection}
              inputProps={{ min: 1, max: 65535 }}
            />
            <TextField
              size="small"
              label={t('detail.servers.form.username')}
              value={form.username}
              onChange={event => updateForm({ username: event.target.value })}
              disabled={!canEditConnection}
            />
          </Box>

          <Alert severity="info" icon={false}>
            {t('detail.servers.form.secretKeepHint')}
          </Alert>

          <Box
            sx={{
              display: 'grid',
              gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' },
              gap: 2,
            }}
          >
            <TextField
              size="small"
              type="password"
              label={t('detail.servers.form.password')}
              value={form.password}
              onChange={event => updateForm({ password: event.target.value })}
              disabled={!canEditConnection}
              helperText={
                formMode === 'edit' && editing?.has_password
                  ? t('detail.servers.form.passwordMaskedHint')
                  : undefined
              }
            />
            <TextField
              size="small"
              type="password"
              label={t('detail.servers.form.passphrase')}
              value={form.passphrase}
              onChange={event => updateForm({ passphrase: event.target.value })}
              disabled={!canEditConnection}
              helperText={
                formMode === 'edit' && editing?.has_passphrase
                  ? t('detail.servers.form.passphraseMaskedHint')
                  : undefined
              }
            />
          </Box>

          <TextField
            size="small"
            multiline
            minRows={4}
            label={t('detail.servers.form.privateKey')}
            value={form.privateKey}
            onChange={event => updateForm({ privateKey: event.target.value })}
            disabled={!canEditConnection}
            helperText={
              formMode === 'edit' && editing?.has_private_key
                ? t('detail.servers.form.privateKeyMaskedHint')
                : undefined
            }
          />

          <TextField
            size="small"
            multiline
            minRows={3}
            label={t('detail.servers.form.hostKeys')}
            helperText={t('detail.servers.form.hostKeysHint')}
            value={form.hostKeys}
            onChange={event => updateForm({ hostKeys: event.target.value })}
            disabled={!canEditConnection}
          />

          <TextField
            size="small"
            multiline
            minRows={2}
            label={t('detail.servers.form.note')}
            value={form.note}
            onChange={event => updateForm({ note: event.target.value })}
          />
        </Stack>
      </NekroDialog>

      <NekroDialog
        open={!!deleteTarget}
        onClose={() => !deleteMutation.isPending && setDeleteTarget(null)}
        title={t('detail.servers.delete.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton
              onClick={() => setDeleteTarget(null)}
              disabled={deleteMutation.isPending}
            >
              {t('detail.servers.form.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              variant="contained"
              onClick={() => deleteTarget && deleteMutation.mutate(deleteTarget)}
              disabled={deleteMutation.isPending}
            >
              {t('detail.servers.delete.confirm')}
            </ActionButton>
          </>
        }
      >
        <Typography variant="body2">
          {t('detail.servers.delete.content', {
            alias: deleteTarget?.alias ?? '',
          })}
        </Typography>
      </NekroDialog>

      <NekroDialog
        open={!!revealTarget}
        onClose={closeReveal}
        title={t('detail.servers.reveal.title')}
        maxWidth="sm"
        actions={
          <ActionButton onClick={closeReveal} disabled={revealLoading}>
            {t('detail.servers.reveal.close')}
          </ActionButton>
        }
      >
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          <Alert severity="warning">{t('detail.servers.reveal.warning')}</Alert>
          {revealLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
              <CircularProgress size={24} />
            </Box>
          ) : (
            revealData &&
            [
              {
                key: 'password',
                label: t('detail.servers.reveal.password'),
                value: revealData.password,
              },
              {
                key: 'privateKey',
                label: t('detail.servers.reveal.privateKey'),
                value: revealData.private_key,
              },
              {
                key: 'passphrase',
                label: t('detail.servers.reveal.passphrase'),
                value: revealData.passphrase,
              },
            ].map(item => (
              <Box key={item.key}>
                <Stack
                  direction="row"
                  alignItems="center"
                  justifyContent="space-between"
                >
                  <Typography variant="subtitle2">{item.label}</Typography>
                  {item.value && (
                    <ActionButton
                      size="small"
                      tone="ghost"
                      startIcon={<ContentCopyIcon fontSize="small" />}
                      onClick={() => void copySecret(item.value)}
                    >
                      {t('detail.servers.reveal.copy')}
                    </ActionButton>
                  )}
                </Stack>
                {item.value ? (
                  <Box
                    component="pre"
                    sx={{
                      m: 0,
                      p: 1,
                      bgcolor: 'action.hover',
                      borderRadius: 1,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-all',
                      fontFamily: 'monospace',
                      fontSize: '0.75rem',
                    }}
                  >
                    {item.value}
                  </Box>
                ) : (
                  <Typography variant="body2" color="text.secondary">
                    {t('detail.servers.reveal.empty')}
                  </Typography>
                )}
              </Box>
            ))
          )}
        </Stack>
      </NekroDialog>

      <NekroDialog
        open={!!testTarget}
        onClose={closeTest}
        title={t('detail.servers.test.title')}
        maxWidth="sm"
        actions={
          <>
            {acceptingHostKey && (
              <ActionButton
                tone="danger"
                variant="contained"
                onClick={() => {
                  if (!testTarget || !testResult) return
                  acceptMutation.mutate({
                    credentialId: testTarget.id,
                    entry: testResult.presented_entry,
                    fingerprint: testResult.presented_fingerprint,
                  })
                }}
                disabled={acceptMutation.isPending}
              >
                {t('detail.servers.test.accept')}
              </ActionButton>
            )}
            <ActionButton
              onClick={closeTest}
              disabled={testMutation.isPending || acceptMutation.isPending}
            >
              {t('detail.servers.test.close')}
            </ActionButton>
          </>
        }
      >
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          {testMutation.isPending ? (
            <Stack direction="row" spacing={1.5} alignItems="center">
              <CircularProgress size={22} />
              <Typography variant="body2">
                {t('detail.servers.test.running')}
              </Typography>
            </Stack>
          ) : (
            testResult && (
              <>
                <Alert severity={testResult.ok ? 'success' : 'error'}>
                  <Typography variant="body2" fontWeight={600}>
                    {testResult.ok
                      ? t('detail.servers.test.success')
                      : t('detail.servers.test.failed')}
                  </Typography>
                  {testResult.reason && (
                    <Typography variant="body2">
                      {t('detail.servers.test.reason', {
                        reason: t(
                          `detail.servers.audit.reasons.${testResult.reason}`,
                          { defaultValue: testResult.reason }
                        ),
                      })}
                    </Typography>
                  )}
                  {testResult.message && (
                    <Typography variant="body2">
                      {t('detail.servers.test.message', {
                        message: testResult.message,
                      })}
                    </Typography>
                  )}
                </Alert>
                {testResult.fingerprint && (
                  <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
                    {t('detail.servers.test.fingerprint', {
                      fingerprint: testResult.fingerprint,
                    })}
                  </Typography>
                )}
                {testResult.pinned && (
                  <Typography variant="body2" color="success.main">
                    {t('detail.servers.test.pinned')}
                  </Typography>
                )}
                {hostKeyMismatch && (
                  <Alert severity="warning">
                    <Stack spacing={1}>
                      <Typography variant="body2" fontWeight={600}>
                        {t('detail.servers.test.registeredKeys')}
                      </Typography>
                      {testResult.host_keys.map(item => (
                        <Typography
                          key={item.entry}
                          variant="caption"
                          component="div"
                          sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}
                        >
                          {item.fingerprint || item.entry}
                        </Typography>
                      ))}
                      <Typography variant="body2" fontWeight={600}>
                        {t('detail.servers.test.presentedKey')}
                      </Typography>
                      <Typography
                        variant="caption"
                        component="div"
                        sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}
                      >
                        {testResult.presented_fingerprint}
                      </Typography>
                      <Typography variant="body2" color="text.secondary">
                        {acceptingHostKey
                          ? t('detail.servers.test.acceptHint')
                          : t('detail.servers.form.noteOnlyHint')}
                      </Typography>
                    </Stack>
                  </Alert>
                )}
              </>
            )
          )}
        </Stack>
      </NekroDialog>

      <NekroDialog
        open={platformViewer && !!dutyTarget}
        onClose={closeDutyMode}
        title={t('detail.servers.duty.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton
              onClick={closeDutyMode}
              disabled={dutyModeMutation.isPending}
            >
              {t('detail.servers.form.cancel')}
            </ActionButton>
            <ActionButton
              variant="contained"
              onClick={submitDutyMode}
              disabled={dutyModeMutation.isPending}
            >
              {t('detail.servers.form.save')}
            </ActionButton>
          </>
        }
      >
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          <TextField
            select
            size="small"
            fullWidth
            label={t('detail.servers.duty.title')}
            value={dutyMode}
            onChange={event =>
              setDutyMode(event.target.value as SshDutyMode)
            }
          >
            <MenuItem value="">
              {t('detail.servers.duty.modeInherit', {
                mode: defaultDutyModeLabel,
              })}
            </MenuItem>
            <MenuItem value="strict">
              {t('detail.servers.duty.modeStrict')}
            </MenuItem>
            <MenuItem value="duty">
              {t('detail.servers.duty.modeDuty')}
            </MenuItem>
          </TextField>
          {dutyMode === 'duty' && (
            <Alert severity="warning">
              {t('detail.servers.duty.dutyWarning')}
            </Alert>
          )}
        </Stack>
      </NekroDialog>

      <NekroDialog
        open={platformViewer && !!runbookTarget}
        onClose={closeRunbook}
        title={t('detail.servers.runbook.title', {
          alias: runbookTarget?.alias ?? '',
        })}
        maxWidth="md"
        actions={
          <>
            <ActionButton
              onClick={closeRunbook}
              disabled={
                runbookSaveMutation.isPending ||
                runbookDeleteMutation.isPending
              }
            >
              {t('detail.servers.form.cancel')}
            </ActionButton>
            <ActionButton
              variant="contained"
              onClick={submitRunbook}
              disabled={
                runbookSaveMutation.isPending ||
                runbookDraft === null ||
                runbookOverLimit
              }
            >
              {t('detail.servers.form.save')}
            </ActionButton>
          </>
        }
      >
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          <Typography variant="body2" color="text.secondary">
            {t('detail.servers.runbook.ownerHint')}
          </Typography>
          <Box>
            <TextField
              multiline
              minRows={8}
              fullWidth
              value={runbookText}
              onChange={event => setRunbookDraft(event.target.value)}
              disabled={runbookDraft === null || runbookSaveMutation.isPending}
            />
            <Typography
              variant="caption"
              color={runbookOverLimit ? 'error' : 'text.secondary'}
              sx={{ display: 'block', mt: 0.5, textAlign: 'right' }}
            >
              {runbookText.length} / {RUNBOOK_MAX_LENGTH}
            </Typography>
          </Box>

          <Box>
            <Typography variant="subtitle2" fontWeight={700}>
              {t('detail.servers.runbook.entriesTitle', {
                count: runbookEntries.length,
              })}
            </Typography>
            <Typography
              variant="caption"
              color="text.secondary"
              component="div"
            >
              {t('detail.servers.runbook.entriesHint')}
            </Typography>

            {runbookQuery.isLoading ? (
              <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
                <CircularProgress size={24} />
              </Box>
            ) : runbookQuery.isError ? (
              <Box sx={{ mt: 1 }}>
                <QueryErrorState
                  error={runbookQuery.error}
                  onRetry={() => void runbookQuery.refetch()}
                  compact
                />
              </Box>
            ) : runbookEntries.length === 0 ? (
              <Typography
                variant="body2"
                color="text.secondary"
                sx={{ mt: 1 }}
              >
                {t('detail.servers.runbook.entriesEmpty')}
              </Typography>
            ) : (
              <Stack spacing={1} sx={{ mt: 1 }}>
                {runbookEntries.map(entry => (
                  <Box
                    key={entry.id}
                    sx={{
                      p: 1,
                      bgcolor: 'action.hover',
                      borderRadius: 1,
                    }}
                  >
                    <Stack
                      direction="row"
                      alignItems="flex-start"
                      justifyContent="space-between"
                      spacing={1}
                    >
                      <Box sx={{ minWidth: 0 }}>
                        <Typography
                          variant="caption"
                          color="text.secondary"
                          component="div"
                        >
                          {formatLastActiveTimeFromInput(entry.created_at, '—')}
                        </Typography>
                        <Typography
                          variant="body2"
                          sx={{
                            whiteSpace: 'pre-wrap',
                            wordBreak: 'break-word',
                          }}
                        >
                          {entry.content}
                        </Typography>
                      </Box>
                      <IconActionButton
                        aria-label={t('detail.servers.runbook.deleteEntry')}
                        onClick={() => setEntryToDelete(entry.id)}
                        disabled={
                          runbookDeleteMutation.isPending ||
                          runbookSaveMutation.isPending
                        }
                      >
                        <DeleteIcon fontSize="small" />
                      </IconActionButton>
                    </Stack>
                  </Box>
                ))}
              </Stack>
            )}
          </Box>
        </Stack>
      </NekroDialog>

      <NekroDialog
        open={platformViewer && !!runbookTarget && entryToDelete !== null}
        onClose={() => !runbookDeleteMutation.isPending && setEntryToDelete(null)}
        title={t('detail.servers.runbook.deleteEntry')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton
              onClick={() => setEntryToDelete(null)}
              disabled={runbookDeleteMutation.isPending}
            >
              {t('detail.servers.form.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              variant="contained"
              onClick={confirmDeleteRunbookEntry}
              disabled={runbookDeleteMutation.isPending}
            >
              {t('detail.servers.delete.confirm')}
            </ActionButton>
          </>
        }
      >
        <Typography variant="body2">
          {t('detail.servers.runbook.deleteConfirm')}
        </Typography>
      </NekroDialog>
    </Box>
  )
}
