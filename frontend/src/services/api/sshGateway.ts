/**
 * SSH 服务器凭据与网关 API 客户端
 *
 * 契约对齐 `nekro_agent/routers/ssh_gateway.py` 与 `nekro_agent/schemas/ssh_gateway.py`。
 * 列表与详情里的三个凭据字段永远是掩码或空串，明文只走 `getSecret` 这一条单独路径；
 * 调用方拿到明文后只能放进当次对话框，不能写进列表、查询缓存或 localStorage。
 *
 * 权限边界由后端裁决：工作区子资源要求 `Role.Admin` 且工作区可见；全局列表、状态与
 * 导入是平台方专属；审计按可见性矩阵过滤。前端只用后端下发的
 * `can_reveal` / `can_edit_connection` / `owner_whitelisted`，不自己推权限。
 */

import axios from './axios'

export type SshOwnerKind = 'platform' | 'user'

/** 平台方服务器的值守模式：空串表示跟随全局默认 */
export type SshDutyMode = '' | 'strict' | 'duty'

export interface SshHostKeyView {
  /** 类型与 base64 两段 */
  entry: string
  /** SHA256 指纹，与 ssh-keygen -lf 一致 */
  fingerprint: string
}

/** 新建一行凭据 */
export interface SshCredentialCreate {
  owner_kind: SshOwnerKind
  /** user 行的凭据主人；platform 行留空 */
  owner_account: string
  /** user 行归属实例的适配器 */
  adapter_key: string
  /** user 行归属实例标识 */
  instance_key: string
  alias: string
  host: string
  port: number
  username: string
  password: string
  private_key: string
  passphrase: string
  host_keys: string[]
  note: string
}

/**
 * 修改一行凭据
 *
 * 三个凭据字段的取值约定与工作区资源页一致：不提交或提交 `CREDENTIAL_MASK`
 * 表示保持不变，提交空串表示清空，其它值表示新值并重新加密。
 */
export interface SshCredentialUpdate {
  alias?: string
  host?: string
  port?: number
  username?: string
  password?: string
  private_key?: string
  passphrase?: string
  host_keys?: string[]
  note?: string
  status?: string
}

/** 列表与详情的对外形态 */
export interface SshCredentialView {
  id: number
  workspace_id: number
  workspace_name: string
  owner_kind: string
  owner_account: string
  adapter_key: string
  instance_key: string
  created_via: string
  import_ref: string
  alias: string
  host: string
  port: number
  username: string
  password: string
  private_key: string
  passphrase: string
  host_keys: SshHostKeyView[]
  status: string
  status_reason: string
  note: string
  created_by: string
  updated_by: string
  last_used_at: string | null
  last_error: string
  create_time: string
  update_time: string
  has_password: boolean
  has_private_key: boolean
  has_passphrase: boolean
  secret_state: string
  owner_whitelisted: boolean | null
  /** 后端判定的「能不能看明文」 */
  can_reveal: boolean
  /** 后端判定的「能不能改连接信息」；聊天登记的行对租户恒为假 */
  can_edit_connection: boolean
  /** '' = 跟随默认, 'strict', 'duty' */
  duty_mode: string
  /** '' 或 'strict' / 'duty'：此刻生效的模式 */
  effective_duty_mode: string
}

/** Bot 在任务结束后追加的一条运维经验 */
export interface SshRunbookEntryView {
  id: number
  content: string
  task_ref: string
  created_at: string | null
}

/** 一台平台方服务器的运维手册与 Bot 追加的经验（新的在前） */
export interface SshRunbookView {
  credential_id: number
  runbook: string
  entries: SshRunbookEntryView[]
}

/** 绑在工作区上的一个实例对（新建用户行时选归属实例用） */
export interface SshBoundInstance {
  adapter_key: string
  instance_key: string
}

export interface SshCredentialListResponse {
  items: SshCredentialView[]
  total: number
  /** 工作区页用：绑定的频道数（S3 提醒）；全局列表为 0 */
  bound_channel_count?: number
  /** 工作区页用：能选的归属实例，租户只看到自己的；全局列表为空 */
  bound_instances?: SshBoundInstance[]
}

/** 单独查看接口的响应：只在这一条路径上出现明文 */
export interface SshCredentialSecretResponse {
  password: string
  private_key: string
  passphrase: string
}

/** 一条审计记录 */
export interface SshAuditView {
  id: number
  create_time: string | null
  workspace_id: number
  credential_id: number | null
  owner_kind: string
  owner_account: string
  adapter_key: string
  instance_key: string
  event: string
  allowed: boolean
  reason: string
  task_ref: string
  lease_id: string
  conn_id: string
  triggered_by: string[]
  channel_kind: string
  detail: string
  input_head: string
  exit_status: number | null
  bytes_up: number | null
  bytes_down: number | null
  duration_ms: number | null
  host_key_fp: string
  actor: string
}

export interface SshAuditListResponse {
  items: SshAuditView[]
  next_before_id: number | null
}

export interface SshAuditQuery {
  workspace_id?: number
  credential_id?: number
  task_ref?: string
  limit?: number
  before_id?: number
}

export interface SshActionOkResponse {
  ok: boolean
  message: string
}

/** 后台「测试连接」的结果 */
export interface SshCredentialTestResponse {
  ok: boolean
  reason: string
  message: string
  /** 这次服务器出示的主机钥匙 SHA256 指纹 */
  fingerprint: string
  /** 这次是否首次记下了主机钥匙 */
  pinned: boolean
  /** 对不上时服务器这次出示的钥匙 */
  presented_entry: string
  /** 对不上时服务器这次出示的指纹 */
  presented_fingerprint: string
  /** 行上当前记着的钥匙 */
  host_keys: SshHostKeyView[]
}

/** 接受测试里看到的那把新主机钥匙 */
export interface SshHostKeyAcceptRequest {
  entry: string
  fingerprint: string
}

/** 网关状态：只有运行数字，不含凭据、主机或目标地址 */
export interface SshGatewayStatusResponse {
  /** off / starting / running / unavailable / tripped */
  state: string
  reason: string
  listen_addresses: string[]
  port: number
  connections: number
  channels: number
  running_leases: number
  default_duty_mode: 'strict' | 'duty'
  /** 能触发值守的平台方实例，写法 "适配器:实例" */
  duty_instances: string[]
  /** true = 名单已被清空，值守对所有实例关闭 */
  duty_instances_closed: boolean
}

/** 导入成功的一条：工作区、别名与来源资源名 */
export interface ImportReportEntry {
  workspace_id: number
  alias: string
  resource_name: string
}

/** 没有导入的一条：点名资源、工作区与原因 */
export interface ImportReportSkip {
  resource_name: string
  workspace_id: number
  reason: string
}

/** 一次存量资源导入的结果 */
export interface ImportReport {
  imported: ImportReportEntry[]
  already: number
  skipped: ImportReportSkip[]
}

const WORKSPACE_BASE = (workspaceId: number) => `/workspaces/${workspaceId}/ssh-credentials`
const GATEWAY_BASE = '/ssh-gateway'

export const SSH_GATEWAY_QUERY_KEYS = {
  workspaceCredentials: (workspaceId: number) =>
    ['ssh-gateway', 'workspace-credentials', workspaceId] as const,
  credential: (workspaceId: number, credentialId: number) =>
    ['ssh-gateway', 'credential', workspaceId, credentialId] as const,
  runbook: (workspaceId: number, credentialId: number) =>
    ['ssh-gateway', 'runbook', workspaceId, credentialId] as const,
  allCredentials: () => ['ssh-gateway', 'all-credentials'] as const,
  status: () => ['ssh-gateway', 'status'] as const,
  audit: (workspaceId?: number) =>
    ['ssh-gateway', 'audit', workspaceId ?? 'all'] as const,
}

export const sshGatewayApi = {
  /** 列出工作区的 SSH 服务器凭据 */
  async listCredentials(workspaceId: number): Promise<SshCredentialListResponse> {
    const response = await axios.get<SshCredentialListResponse>(WORKSPACE_BASE(workspaceId))
    return response.data
  },

  /** 查看单行详情 */
  async getCredential(workspaceId: number, credentialId: number): Promise<SshCredentialView> {
    const response = await axios.get<SshCredentialView>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}`
    )
    return response.data
  },

  /** 新建一行 */
  async createCredential(
    workspaceId: number,
    body: SshCredentialCreate
  ): Promise<SshCredentialView> {
    const response = await axios.post<SshCredentialView>(WORKSPACE_BASE(workspaceId), body)
    return response.data
  },

  /** 修改一行；掩码原样提交表示保持不变 */
  async updateCredential(
    workspaceId: number,
    credentialId: number,
    body: SshCredentialUpdate
  ): Promise<SshCredentialView> {
    const response = await axios.put<SshCredentialView>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}`,
      body
    )
    return response.data
  },

  /** 删除一行 */
  async deleteCredential(workspaceId: number, credentialId: number): Promise<SshActionOkResponse> {
    const response = await axios.delete<SshActionOkResponse>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}`
    )
    return response.data
  },

  /** 启用或停用 */
  async setCredentialEnabled(
    workspaceId: number,
    credentialId: number,
    enabled: boolean
  ): Promise<SshCredentialView> {
    const action = enabled ? 'enable' : 'disable'
    const response = await axios.post<SshCredentialView>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}/${action}`
    )
    return response.data
  },

  /** 平台方：设置一行平台方服务器的值守模式；空串表示跟随默认 */
  async setDutyMode(
    workspaceId: number,
    credentialId: number,
    dutyMode: SshDutyMode
  ): Promise<SshCredentialView> {
    const response = await axios.put<SshCredentialView>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}/duty-mode`,
      { duty_mode: dutyMode }
    )
    return response.data
  },

  /** 平台方：读一台平台方服务器的运维手册与 Bot 追加的经验 */
  async getRunbook(
    workspaceId: number,
    credentialId: number
  ): Promise<SshRunbookView> {
    const response = await axios.get<SshRunbookView>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}/runbook`
    )
    return response.data
  },

  /** 平台方：保存运维手册正文 */
  async updateRunbook(
    workspaceId: number,
    credentialId: number,
    runbook: string
  ): Promise<SshRunbookView> {
    const response = await axios.put<SshRunbookView>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}/runbook`,
      { runbook }
    )
    return response.data
  },

  /** 平台方：删除一条 Bot 追加的经验 */
  async deleteRunbookEntry(
    workspaceId: number,
    credentialId: number,
    entryId: number
  ): Promise<SshRunbookView> {
    const response = await axios.delete<SshRunbookView>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}/runbook/entries/${entryId}`
    )
    return response.data
  },

  /**
   * 查看明文
   *
   * 只允许在一次性交互里调用；调用方不得把它写进 React Query 缓存。
   */
  async getCredentialSecret(
    workspaceId: number,
    credentialId: number
  ): Promise<SshCredentialSecretResponse> {
    const response = await axios.get<SshCredentialSecretResponse>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}/secret`
    )
    return response.data
  },

  /** 登录一次、只认证、不开会话 */
  async testCredential(
    workspaceId: number,
    credentialId: number
  ): Promise<SshCredentialTestResponse> {
    const response = await axios.post<SshCredentialTestResponse>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}/test`
    )
    return response.data
  },

  /** 接受测试里看到的新主机指纹 */
  async acceptHostKey(
    workspaceId: number,
    credentialId: number,
    body: SshHostKeyAcceptRequest
  ): Promise<SshActionOkResponse> {
    const response = await axios.post<SshActionOkResponse>(
      `${WORKSPACE_BASE(workspaceId)}/${credentialId}/host-keys/accept`,
      body
    )
    return response.data
  },

  /** 平台方：列出全部工作区的凭据 */
  async listAllCredentials(): Promise<SshCredentialListResponse> {
    const response = await axios.get<SshCredentialListResponse>(`${GATEWAY_BASE}/credentials`)
    return response.data
  },

  /** 审计；按记录号倒序，`before_id` 翻页 */
  async listAudit(query: SshAuditQuery): Promise<SshAuditListResponse> {
    const response = await axios.get<SshAuditListResponse>(`${GATEWAY_BASE}/audit`, {
      params: query,
    })
    return response.data
  },

  /** 平台方：网关状态 */
  async getStatus(): Promise<SshGatewayStatusResponse> {
    const response = await axios.get<SshGatewayStatusResponse>(`${GATEWAY_BASE}/status`)
    return response.data
  },

  /** 平台方：导入存量工作区资源里的 SSH 模板 */
  async importResources(): Promise<ImportReport> {
    const response = await axios.post<ImportReport>(`${GATEWAY_BASE}/import-resources`)
    return response.data
  },
}
