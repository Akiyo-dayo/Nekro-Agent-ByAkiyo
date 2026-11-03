/**
 * 多租户 API 客户端
 *
 * 本文件是前端与后端 `nekro_agent/schemas/tenant.py` 的唯一契约映射，
 * 所有类型定义均对齐该 Python 模块，后端契约变更时需同步修改此处。
 *
 * 运营模型：平台方运营「共用版」（多客户共用平台方实例与凭据）与「定制版」
 * （客户拥有独立实例、可访问自己的后台）两类 Bot。租户不是另做一套后台，
 * 而是被限定在自己实例作用域里的同一套 UI，因此前端一切分支都以
 * `GET /user/me/identity` 返回的身份为依据。
 */

import axios, { ApiError } from './axios'
import type { I18nDict } from './types'
import type { ModelGroupConfig } from '../../components/common/ConfigTable'

/** 租户体系下的角色，权限从高到低 */
export const TENANT_ROLES = ['platform_owner', 'tenant_admin', 'tenant_user'] as const
export type TenantRole = (typeof TENANT_ROLES)[number]

/** 租户状态 */
export const TENANT_STATUSES = ['active', 'suspended', 'archived'] as const
export type TenantStatus = (typeof TENANT_STATUSES)[number]

/**
 * 模型组归属
 *
 * 决定凭据可见性，是整个多租户里最敏感的一个判定：
 * 平台组的 `api_key` 对租户永远是掩码，租户自建组的凭据归租户自己。
 */
export const MODEL_GROUP_OWNERSHIPS = ['platform', 'tenant'] as const
export type ModelGroupOwnership = (typeof MODEL_GROUP_OWNERSHIPS)[number]

/**
 * 平台组对租户的开放程度
 *
 * 只对平台组有意义：`shared` 表示租户可以选用（凭据仍然掩码），`private` 表示仅平台方可见可用。
 * 租户自建组由后端强制为 `private`，前端不提供这个选项。
 */
export const MODEL_GROUP_VISIBILITIES = ['shared', 'private'] as const
export type ModelGroupVisibility = (typeof MODEL_GROUP_VISIBILITIES)[number]

/** 租户概要 */
export interface TenantInfo {
  /** 租户 ID */
  id: number
  /** 租户标识（唯一，创建后不可改） */
  name: string
  /** 显示名称 */
  display_name: string
  status: TenantStatus
  /** 名下实例数 */
  instance_count: number
  /** 成员数 */
  member_count: number
  /** 联系方式 */
  contact: string
  /** 备注 */
  note: string
  create_time: string | null
}

export interface TenantListResponse {
  items: TenantInfo[]
  total: number
}

export interface TenantCreateRequest {
  /** 租户标识，仅小写字母数字下划线，创建后不可改 */
  name: string
  display_name?: string
  contact?: string
  note?: string
}

export interface TenantUpdateRequest {
  display_name?: string
  status?: TenantStatus
  contact?: string
  note?: string
}

/** 租户成员 */
export interface TenantMemberInfo {
  user_id: number
  username: string
  role: TenantRole
  is_active: boolean
}

export interface TenantMemberCreateRequest {
  /** 登录名 */
  username: string
  /** 初始密码 */
  password: string
  role: TenantRole
}

/** 把实例划归某租户；空列表表示解绑全部 */
export interface TenantInstanceBindRequest {
  instance_keys: string[]
}

/**
 * 模型组（对外形态）
 *
 * `api_key` 的取值规则是本契约最关键的部分：
 * - 平台组 + 平台方查看 → 明文
 * - 平台组 + 租户查看   → 恒为掩码串，且 `editable=false`
 * - 租户组 + 该租户查看 → 明文
 * - 租户组 + 其他租户   → 该组根本不出现在列表里
 *
 * 前端不自行推断可编辑性与凭据可见性，一律以 `editable` / `credential_visible` 为准。
 */
export interface ModelGroupInfo {
  name: string
  ownership: ModelGroupOwnership
  /** 归属租户；平台组为 null */
  owner_tenant_id: number | null
  /** 平台组对租户的开放程度；租户自建组恒为 private */
  visibility: ModelGroupVisibility
  /** 接口地址；平台组对租户展示时同样掩码 */
  base_url: string
  /** 凭据；不可见时为掩码串而非真实值 */
  api_key: string
  chat_model: string
  model_type: string
  /** 当前身份能否编辑该组 */
  editable: boolean
  /** 当前身份能否看到真实凭据 */
  credential_visible: boolean
  /**
   * 完整配置，供编辑表单回填
   *
   * 其中的 `API_KEY` / `BASE_URL` / `CHAT_PROXY` 与顶层字段走同一套掩码规则，
   * 凭据不可见时拿到的是掩码串而不是真值。
   */
  config: ModelGroupConfig
}

export interface ModelGroupListResponse {
  items: ModelGroupInfo[]
  total: number
}

/**
 * 批量删除里某个组没删成的原因（与后端 `DeleteRefusal` 一一对应）
 *
 * - `not_found`：不存在，或当前身份看不见（两者刻意不区分）
 * - `forbidden`：看得见但无权删除，如租户眼里的平台组
 * - `default_group`：默认模型组不可删除
 * - `error`：删除时出错（详情只在服务端日志里）
 */
export const MODEL_GROUP_DELETE_REFUSALS = [
  'not_found',
  'forbidden',
  'default_group',
  'error',
] as const
export type ModelGroupDeleteRefusal = (typeof MODEL_GROUP_DELETE_REFUSALS)[number]

/** 引用所在的配置层（与后端 `ModelGroupRefScope` 一一对应） */
export type ModelGroupRefScope =
  | 'system'
  | 'adapter'
  | 'instance'
  | 'channel'
  | 'plugin'
  | 'plugin_instance'

/** 一处配置引用 */
export interface ModelGroupReferenceInfo {
  scope: ModelGroupRefScope
  /** system 为空；adapter 为适配器标识；instance 为 `<adapter>/<instance>`；channel 为 chat_key；插件两层为插件键 */
  target: string
  field: string
  title: string
  /** 仅 plugin_instance：插件在哪个实例上的配置 */
  instance_key: string
  /** 字段标题的多语言版本；有值时界面按语言取 */
  i18n_title?: I18nDict | null
}

/**
 * 一个组被哪些配置引用。后端只对当前身份可删除的组作答；
 * 租户只看得到自己实例下的引用。`references` 可能被截断，`total` 是总数
 */
export interface ModelGroupReferenceItem {
  name: string
  total: number
  references: ModelGroupReferenceInfo[]
}

/** 批量删除结果；后端逐个结算，`ok` 仅在全部删除成功时为 true */
export interface ModelGroupBatchDeleteResponse {
  ok: boolean
  succeeded: string[]
  failed: { name: string; reason: ModelGroupDeleteRefusal }[]
  message: string
}

/**
 * 模型组写入载荷（新建与更新共用）
 *
 * 字段名是后端 `ModelConfigGroup` 同名字段的小写形态。`api_key` / `base_url` / `chat_proxy`
 * 等于 `CREDENTIAL_MASK` 时后端保持原值不变，因此表单未改动的凭据要原样回传。
 */
export interface ModelGroupUpsertRequest {
  /** 仅新建时需要；组名创建后不可改 */
  name?: string
  group_name?: string
  base_url: string
  api_key: string
  chat_model: string
  model_type: string
  chat_proxy?: string
  enable_vision?: boolean
  enable_cot?: boolean
  temperature?: number | null
  top_p?: number | null
  top_k?: number | null
  presence_penalty?: number | null
  frequency_penalty?: number | null
  extra_body?: string | null
  /** 仅平台方修改平台组时生效 */
  visibility?: ModelGroupVisibility
}

/**
 * 连通性测试的归一化结论
 *
 * 与后端 `nekro_agent/services/model_group_test.py` 的 `ModelTestErrorCode` 一一对应。
 * 租户测平台方的模型组时，后端只下发这个结论码与有限集合内的 `message`，
 * 上游的响应体、响应头与异常原文一律不出现在响应里，因此前端只能按结论码分支，
 * 不存在"解析上游文本"这种用法。
 */
export const MODEL_TEST_ERROR_CODES = [
  'none',
  'unauthorized',
  'forbidden',
  'model_not_found',
  'rate_limited',
  'upstream_rejected',
  'upstream_error',
  'timeout',
  'dns_failure',
  'tls_error',
  'proxy_error',
  'connection_failed',
  'invalid_response',
  'not_configured',
  'unsupported_model_type',
  'group_not_found',
  'internal_error',
] as const
export type ModelTestErrorCode = (typeof MODEL_TEST_ERROR_CODES)[number]

/**
 * 模型组连通性测试结果
 *
 * `message` 恒为后端预定义的有限文案；`response_text` / `error_detail` 里的上游原文
 * 只在 `credential_visible` 为真的模型组上才有值（即凭据本就属于当前身份）。
 */
export interface ModelGroupTestResponse {
  name: string
  success: boolean
  error_code: ModelTestErrorCode
  /** 上游 HTTP 状态码；非 HTTP 失败为 null */
  status_code: number | null
  latency_ms: number
  model_name: string
  /** 有限集合内的人类可读文案 */
  message: string
  /** 上游回复；凭据不可见时恒为空串 */
  response_text: string
  /** 上游原文（已脱敏）；凭据不可见时恒为空串 */
  error_detail: string
}

/** 当前登录身份，前端据此决定展示哪一套 UI */
export interface CurrentIdentity {
  user_id: number
  username: string
  role: TenantRole
  tenant_id: number | null
  tenant_name: string
  /** 该身份可访问的实例；平台方为空列表表示不受限 */
  accessible_instance_keys: string[]
}

/**
 * 凭据掩码的统一形态
 *
 * 后端在凭据不可见时返回这个串而不是真实值，并约定「提交上来的值等于掩码即保持不变」。
 * 前端因此必须做到两件事：展示时明确告知这不是真值；提交时未改动就原样回传，
 * 绝不能提交空串或掩码的变体，否则会把凭据清空。
 */
export const CREDENTIAL_MASK = '********'

/** 该值是否就是后端的掩码串 */
export function isCredentialMask(value: string): boolean {
  return value === CREDENTIAL_MASK
}

/**
 * 该值是否是「掩码形态」的输入
 *
 * 用户手动敲出的星号串长度往往与掩码不一致（掩码的变体），一旦当成真实凭据保存
 * 就会把密钥换成一串星号。识别出来后交由调用方回落到原值或直接拒绝提交。
 */
export function isMaskLikeCredential(value: string): boolean {
  const trimmed = value.trim()
  return trimmed.length > 0 && /^[*•·]+$/.test(trimmed)
}

/**
 * 计算提交给后端的凭据值
 *
 * `edited` 为 false（用户根本没碰这个字段）时原样回传后端给的值——它可能就是掩码串，
 * 后端据此判定「保持不变」。用户确实改过、但输入的又是一串星号时同样回落原值，
 * 避免把掩码的变体写成真实凭据。
 */
export function resolveCredentialForSubmit(
  draft: string,
  original: string,
  edited: boolean
): string {
  if (!edited) {
    return original
  }
  if (isMaskLikeCredential(draft)) {
    return original
  }
  return draft
}

const CONSOLE_BASE = '/console'

const encode = encodeURIComponent

const tenantPath = (tenantId: number) => `${CONSOLE_BASE}/tenants/${tenantId}`

export const tenantApi = {
  /** 当前登录身份，前端一切分支的依据 */
  async getCurrentIdentity(): Promise<CurrentIdentity> {
    const response = await axios.get<CurrentIdentity>('/user/me/identity')
    return response.data
  },

  /** 租户列表（仅平台方） */
  async listTenants(): Promise<TenantListResponse> {
    const response = await axios.get<TenantListResponse>(`${CONSOLE_BASE}/tenants`)
    return response.data
  },

  /** 新建租户 */
  async createTenant(data: TenantCreateRequest): Promise<TenantInfo> {
    const response = await axios.post<TenantInfo>(`${CONSOLE_BASE}/tenants`, data)
    return response.data
  },

  /** 租户详情 */
  async getTenant(tenantId: number): Promise<TenantInfo> {
    const response = await axios.get<TenantInfo>(tenantPath(tenantId))
    return response.data
  },

  /** 更新租户可变属性（name 不可改） */
  async updateTenant(tenantId: number, data: TenantUpdateRequest): Promise<TenantInfo> {
    const response = await axios.patch<TenantInfo>(tenantPath(tenantId), data)
    return response.data
  },

  /** 归档租户 */
  async archiveTenant(tenantId: number): Promise<void> {
    await axios.delete(tenantPath(tenantId))
  },

  /** 租户成员 */
  async listMembers(tenantId: number): Promise<TenantMemberInfo[]> {
    const response = await axios.get<TenantMemberInfo[]>(`${tenantPath(tenantId)}/members`)
    return response.data
  },

  /** 为租户创建成员账号 */
  async createMember(
    tenantId: number,
    data: TenantMemberCreateRequest
  ): Promise<TenantMemberInfo> {
    const response = await axios.post<TenantMemberInfo>(`${tenantPath(tenantId)}/members`, data)
    return response.data
  },

  /** 移除租户成员 */
  async removeMember(tenantId: number, userId: number): Promise<void> {
    await axios.delete(`${tenantPath(tenantId)}/members/${userId}`)
  },

  /** 划归实例（整表替换，传空列表表示解绑全部） */
  async bindInstances(tenantId: number, instanceKeys: string[]): Promise<void> {
    const body: TenantInstanceBindRequest = { instance_keys: instanceKeys }
    await axios.put(`${tenantPath(tenantId)}/instances`, body)
  },

  /** 模型组列表（后端按身份过滤并对不可见凭据掩码） */
  async listModelGroups(): Promise<ModelGroupListResponse> {
    const response = await axios.get<ModelGroupListResponse>(`${CONSOLE_BASE}/model-groups`)
    return response.data
  },

  /** 新建模型组 */
  async createModelGroup(data: ModelGroupUpsertRequest): Promise<ModelGroupInfo> {
    const response = await axios.post<ModelGroupInfo>(`${CONSOLE_BASE}/model-groups`, data)
    return response.data
  },

  /** 更新模型组 */
  async updateModelGroup(
    name: string,
    data: ModelGroupUpsertRequest
  ): Promise<ModelGroupInfo> {
    const response = await axios.patch<ModelGroupInfo>(
      `${CONSOLE_BASE}/model-groups/${encode(name)}`,
      data
    )
    return response.data
  },

  /** 删除模型组 */
  async deleteModelGroup(name: string): Promise<void> {
    await axios.delete(`${CONSOLE_BASE}/model-groups/${encode(name)}`)
  },

  /** 查询模型组被哪些配置引用（删除确认框的提醒用） */
  async getModelGroupReferences(names: string[]): Promise<ModelGroupReferenceItem[]> {
    const response = await axios.post<{ items: ModelGroupReferenceItem[] }>(
      `${CONSOLE_BASE}/model-groups/actions/references`,
      { names }
    )
    return response.data.items
  },

  /** 批量删除模型组（组名在请求体里，不进路径） */
  async batchDeleteModelGroups(names: string[]): Promise<ModelGroupBatchDeleteResponse> {
    const response = await axios.post<ModelGroupBatchDeleteResponse>(
      `${CONSOLE_BASE}/model-groups/actions/batch-delete`,
      { names }
    )
    return response.data
  },

  /** 模型组连通性测试 */
  async testModelGroup(name: string): Promise<ModelGroupTestResponse> {
    const response = await axios.post<ModelGroupTestResponse>(
      `${CONSOLE_BASE}/model-groups/${encode(name)}/test`
    )
    return response.data
  },
}

/**
 * 判断错误是否为「后端接口尚未就绪」
 *
 * 多租户各路后端并行实现，前端先行开发。命中时页面显示明确提示，
 * 而不是把用户丢进白屏或误导性的空列表。
 */
export function isTenantEndpointUnavailable(error: unknown): boolean {
  if (error instanceof ApiError) {
    // ⚠️ `NotFoundError` 是**后端自己抛的越权语义**（越权一律 404，
    // 见 `assert_tenant_accessible` / `assert_instance_accessible`），它恰恰证明接口**在**。
    // 把它算成「接口尚未就绪」就是「把越权 404 说成接口没上线」——`pages/settings/system.tsx`
    // 顶部已经把这句话点名成一条错误，这里是它的第二处实现。
    if (error.type === 'NotFoundError') {
      return false
    }
    if (error.type === 'NotImplementedError') {
      return true
    }
    // 真正「接口没上线」的形态：路由压根没匹配上，FastAPI 抛原生 HTTPException，
    // 经 `http_exception_handler` 归一化后 `type` 是 `"HTTPException"`，判据只能是状态码。
    // 旧实现在这条上是**漏的**（ApiError 分支直接 return，下面那段对 axios 调用是死代码），
    // 于是后端没上线时反而落到「加载失败」。状态码由 `ApiError.status` 提供。
    return error.status === 404 || error.status === 405 || error.status === 501
  }
  if (typeof error === 'object' && error !== null && 'response' in error) {
    const status = (error as { response?: { status?: number } }).response?.status
    return status === 404 || status === 405 || status === 501
  }
  return false
}

/** 租户标识的合法形态，与后端保持一致 */
export const TENANT_NAME_PATTERN = /^[a-z0-9_]{1,32}$/

/**
 * 多租户页面路径（跨页跳转统一使用，禁止散落硬编码拼接）
 *
 * 租户详情曾按「基本信息 / 成员 / 实例归属」拆成三个 Tab，路径里带一段 tab。
 * 平台方处理一个客户时这三件事本就要一起看，拆开只是把一屏内容摊成三次点击，
 * 因此现在是单页。旧链接仍会落到同一个页面，由页面自行把 URL 收敛回干净形态。
 */
export const tenantPaths = {
  tenantList: () => `${CONSOLE_BASE}/tenants`,
  tenantDetail: (tenantId: number) => `${CONSOLE_BASE}/tenants/${tenantId}`,
  modelGroups: () => `${CONSOLE_BASE}/model-groups`,
}

/** React Query key，跨页共享同一份缓存 */
export const TENANT_QUERY_KEYS = {
  identity: ['tenant', 'identity'] as const,
  tenants: ['tenant', 'list'] as const,
  tenant: (tenantId: number) => ['tenant', 'detail', tenantId] as const,
  members: (tenantId: number) => ['tenant', 'members', tenantId] as const,
  modelGroups: ['tenant', 'model-groups'] as const,
  /** 删除确认框的引用检查；组名排序后入键，同一组集合只查一次 */
  modelGroupReferences: (names: string[]) =>
    ['tenant', 'model-group-references', [...names].sort()] as const,
  /** 实例列表与 Console 侧共用同一份缓存，划归后两边同时失效 */
  instances: ['console-instances'] as const,
}
