/**
 * 身份与域（记忆域配置 + 账号合并 + 人设孤儿数据）API 客户端
 *
 * 契约对齐 `nekro_agent/routers/console/identity.py`。
 *
 * ## 作用域用「实例」表达，不用「域」
 *
 * 域（`domain_id`）是一个没有归属的字符串，谁都能编一个。所以合并与孤儿数据这一族接口
 * 一律要求传一个**你够得着的 `instance_key`**，服务端据此解析出域；越权一律 404。
 * 前端因此永远不该把 `domain_id` 当参数发回去（除了孤儿清理那一处的**乐观锁**用途：
 * 它带上预览时看到的 `domain_id`，与服务端此刻解析出来的不一致就拒绝执行）。
 *
 * ## 判定不在前端复算
 *
 * 合并预览里的 `blockers` / `warnings` / `transfers` 全部由后端算好，页面原样显示。
 * 「好感取高的」这条规则的权威实现在 `services/affection/merge.py`，
 * 前端复算一份等于把授权语义写成三份。
 */

import axios from './axios'

const BASE = '/console/identity'

export const IDENTITY_QUERY_KEYS = {
  domains: ['console', 'identity', 'domains'] as const,
  aliases: (instanceKey: string) => ['console', 'identity', 'aliases', instanceKey] as const,
  aliasRequests: (instanceKey: string) =>
    ['console', 'identity', 'alias-requests', instanceKey] as const,
  orphans: (instanceKey: string) => ['console', 'identity', 'orphans', instanceKey] as const,
}

export interface DomainMemberInfo {
  adapter_key: string
  instance_key: string
  display_name: string
}

/** 一个域（实例组） */
export interface DomainInfo {
  group_id: number
  name: string
  note: string
  /** 归属租户；null 表示平台方。**成员实例的归属必须与它一致**，否则后端拒绝入组 */
  tenant_id: number | null
  members: DomainMemberInfo[]
  create_time: string | null
  update_time: string | null
}

/** 入域并入的实做结果；只在请求勾选 `migrate_affection` 时随响应返回 */
export interface DomainJoinMigrationInfo {
  affection_rows: number
  folded_rows: number
  score_delta: number
  event_rows: number
  alias_rows: number
  /** 目标域已有同账号指向、以目标域为准未随迁的账号 */
  alias_conflicts: string[]
  person_rows: number
  person_conflicts: string[]
}

/** 一个实例当前在哪个域；`group_id` 为 null 表示自成一域（= 不配置时的行为） */
export interface InstanceDomainItem {
  adapter_key: string
  instance_key: string
  display_name: string
  tenant_id: number | null
  group_id: number | null
  group_name: string
  domain_id: string
  migration?: DomainJoinMigrationInfo | null
}

/** 「实例入域并入存量好感」的预览 */
export interface DomainJoinPreviewResponse {
  adapter_key: string
  instance_key: string
  source_domain_id: string
  target_domain_id: string
  target_domain_label: string
  affection_rows: number
  affection_people: number
  event_rows: number
  /** 与目标域已有同（人, 人设）行重合、将按「取高」折叠的行数 */
  fold_pairs: number
  alias_rows: number
  alias_conflicts: string[]
  person_rows: number
  blockers: string[]
  notes: string[]
  can_migrate: boolean
}

export interface DomainListResponse {
  items: DomainInfo[]
  instances: InstanceDomainItem[]
  /** 平台方才有「给别的租户建组」这一档；租户身份下恒为 false，据此不渲染那个选择器 */
  can_choose_tenant: boolean
}

export interface PresetAffectionItem {
  preset_id: number
  preset_name: string
  /** false = 人设已被删除，这一份是孤儿数据 */
  preset_exists: boolean
  score: number
  stage: string
  stage_label: string
  interaction_count: number
  last_interact_at: string | null
}

export interface AccountFactsItem {
  platform_userid: string
  nickname: string
  /** 非空表示这个账号自己已经是别人的副号 */
  alias_person_uid: string
  /** 有多少个账号已经指向它 */
  incoming_alias_count: number
  best_score: number
  affection_rows: number
  event_rows: number
  interaction_total: number
  last_interact_at: string | null
  presets: PresetAffectionItem[]
}

/** 某个人设下「将采用哪个值」 */
export interface MergeTransferItem {
  preset_id: number
  preset_name: string
  preset_exists: boolean
  alias_score: number
  person_score: number
  /** 本次要补给主账号的差额；0 表示这个人设下主账号本来就不低 */
  delta: number
  /** 合并后主账号在这个人设下的分数 = max(两边) */
  score_after: number
}

export interface MergePreviewResponse {
  domain_id: string
  domain_label: string
  alias: AccountFactsItem
  person: AccountFactsItem
  transfers: MergeTransferItem[]
  total_delta: number
  /** 本域的记忆条数（记忆不按账号分行，见 memory_scope_note） */
  domain_memory_paragraphs: number
  memory_scope_note: string
  reversible_note: string
  warnings: string[]
  blockers: string[]
  can_merge: boolean
}

export interface MergeTransferResultItem {
  preset_id: number
  delta: number
  score_after: number
}

export interface MergeExecuteResponse {
  domain_id: string
  person_uid: string
  alias_platform_userid: string
  total_delta: number
  transfers: MergeTransferResultItem[]
  /** 因为「别名不成链」被一并改写的其他副号；撤销时它们不会自动跟回去 */
  rewritten_aliases: string[]
}

export interface AliasItem {
  id: number
  domain_id: string
  platform_userid: string
  person_uid: string
  source: string
  operator: string
  note: string
  create_time: string | null
}

export interface AliasListResponse {
  domain_id: string
  domain_label: string
  items: AliasItem[]
}

/** 一张自助申报单；`reporter_*` 是给管理员核对「谁报的」用的，不是解析结果 */
export interface AliasRequestItem {
  id: number
  alias_platform_userid: string
  reporter_userid: string
  reporter_person_uid: string
  note: string
  status: 'pending' | 'approved' | 'rejected' | 'canceled'
  reviewed_by: string
  review_note: string
  reviewed_at: string | null
  create_time: string | null
}

export interface AliasRequestListResponse {
  domain_id: string
  domain_label: string
  items: AliasRequestItem[]
}

/** 批准的回执：申报单终态 + 合并的实做结果（与手动合并同一个形状） */
export interface AliasRequestApproveResponse {
  request: AliasRequestItem
  merge: MergeExecuteResponse
}

export interface OrphanBucketItem {
  preset_id: number
  domain_id: string
  domain_label: string
  adapter_key: string
  instance_key: string
  domain_group_id: number
  affection_rows: number
  event_rows: number
  people: number
  last_update: string | null
}

export interface OrphanListResponse {
  items: OrphanBucketItem[]
  policy_note: string
}

export interface OrphanCleanupResponse {
  ok: boolean
  affection: number
  affection_event: number
}

export interface ActionOkResponse {
  ok: boolean
  message: string
}

export const identityApi = {
  listDomains: async (): Promise<DomainListResponse> => {
    const response = await axios.get<DomainListResponse>(`${BASE}/domains`)
    return response.data
  },

  createDomain: async (body: {
    name: string
    note?: string
    tenant_id?: number | null
  }): Promise<DomainInfo> => {
    const response = await axios.post<DomainInfo>(`${BASE}/domains`, body)
    return response.data
  },

  updateDomain: async (
    groupId: number,
    body: { name?: string; note?: string }
  ): Promise<DomainInfo> => {
    const response = await axios.patch<DomainInfo>(`${BASE}/domains/${groupId}`, body)
    return response.data
  },

  /** 删除域；成员实例回到自成一域（后端在删组前先释放绑定行） */
  deleteDomain: async (groupId: number): Promise<ActionOkResponse> => {
    const response = await axios.delete<ActionOkResponse>(`${BASE}/domains/${groupId}`)
    return response.data
  },

  /**
   * 把实例编进域；`groupId` 传 null 表示回到自成一域
   *
   * `migrateAffection` 只在「自成一域 → 组域」方向合法：勾选后把该实例自成一域时期的
   * 存量好感一并搬进目标组域（撞上的同（人, 人设）行按「取高」折叠）。
   */
  setInstanceDomain: async (
    instanceKey: string,
    groupId: number | null,
    migrateAffection = false
  ): Promise<InstanceDomainItem> => {
    const response = await axios.put<InstanceDomainItem>(
      `${BASE}/instances/${encodeURIComponent(instanceKey)}/domain`,
      { group_id: groupId, migrate_affection: migrateAffection }
    )
    return response.data
  },

  /** 入域并入的影响面：搬多少行、折叠多少、哪些别名以目标域为准 */
  previewDomainJoin: async (
    instanceKey: string,
    groupId: number
  ): Promise<DomainJoinPreviewResponse> => {
    const response = await axios.get<DomainJoinPreviewResponse>(
      `${BASE}/instances/${encodeURIComponent(instanceKey)}/domain-join-preview`,
      { params: { group_id: groupId } }
    )
    return response.data
  },

  listAliases: async (instanceKey: string): Promise<AliasListResponse> => {
    const response = await axios.get<AliasListResponse>(`${BASE}/aliases`, {
      params: { instance_key: instanceKey },
    })
    return response.data
  },

  /** 先预览：两边各自是什么、每个人设合完之后采用哪个值 */
  previewMerge: async (body: {
    instance_key: string
    alias_userid: string
    person_userid: string
    note?: string
  }): Promise<MergePreviewResponse> => {
    const response = await axios.post<MergePreviewResponse>(`${BASE}/merge/preview`, body)
    return response.data
  },

  executeMerge: async (body: {
    instance_key: string
    alias_userid: string
    person_userid: string
    note?: string
  }): Promise<MergeExecuteResponse> => {
    const response = await axios.post<MergeExecuteResponse>(`${BASE}/merge`, body)
    return response.data
  },

  /**
   * 撤销合并
   *
   * `instanceKey` 不是可选的：别名表是全局一张，服务端按 `(域, 行 ID)` 双条件定位，
   * 域里没有那一行就 404。
   */
  revokeMerge: async (aliasId: number, instanceKey: string): Promise<MergeExecuteResponse> => {
    const response = await axios.delete<MergeExecuteResponse>(`${BASE}/aliases/${aliasId}`, {
      params: { instance_key: instanceKey },
    })
    return response.data
  },

  /** 自助申报列表；`status` 默认只看待审批，传空串列全部 */
  listAliasRequests: async (
    instanceKey: string,
    status = 'pending'
  ): Promise<AliasRequestListResponse> => {
    const response = await axios.get<AliasRequestListResponse>(`${BASE}/alias-requests`, {
      params: { instance_key: instanceKey, status },
    })
    return response.data
  },

  /** 批准 = 兑现成一条 `self_report` 别名；走与手动合并完全相同的路径 */
  approveAliasRequest: async (
    requestId: number,
    body: { instance_key: string; note?: string }
  ): Promise<AliasRequestApproveResponse> => {
    const response = await axios.post<AliasRequestApproveResponse>(
      `${BASE}/alias-requests/${requestId}/approve`,
      body
    )
    return response.data
  },

  /** 拒绝 = 只留痕（状态、批语、谁批的），永远不动别名表 */
  rejectAliasRequest: async (
    requestId: number,
    body: { instance_key: string; note?: string }
  ): Promise<AliasRequestItem> => {
    const response = await axios.post<AliasRequestItem>(
      `${BASE}/alias-requests/${requestId}/reject`,
      body
    )
    return response.data
  },

  /** 人设孤儿数据；平台方可留空表示全局视图，租户必须指定自己名下的实例 */
  listOrphans: async (instanceKey: string): Promise<OrphanListResponse> => {
    const response = await axios.get<OrphanListResponse>(`${BASE}/orphans`, {
      params: { instance_key: instanceKey || undefined },
    })
    return response.data
  },

  cleanupOrphans: async (body: {
    instance_key: string
    preset_id: number
    domain_id: string
  }): Promise<OrphanCleanupResponse> => {
    const response = await axios.post<OrphanCleanupResponse>(`${BASE}/orphans/cleanup`, body)
    return response.data
  },
}
