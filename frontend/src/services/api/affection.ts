import axios from './axios'

/**
 * 实例作用域参数
 *
 * 好感度按 (实例, 用户) 隔离，所以作用域是所有查询的隐式过滤条件：
 * 留空表示平台方全局视图（看到全部实例）；带上实例标识时只看该实例的数据。
 * 租户身份必须带上自己名下的实例，服务端对越权一律返回 404。
 */
type InstanceScopeParam = { instance_key?: string }

export type AffectionStageValue =
  | 'hostile'
  | 'wary'
  | 'stranger'
  | 'acquainted'
  | 'friendly'
  | 'close'
  | 'trusted'

export type AffectionSortBy = 'score_desc' | 'score_asc' | 'recent' | 'interaction'

export interface AffectionStageInfo {
  value: AffectionStageValue
  label: string
}

export interface AffectionItem {
  id: number
  adapter_key: string
  instance_key: string
  /**
   * 隔离键里「域」与「人设」两维的原始取值
   *
   * `domain_group_id > 0` 时 `adapter_key` / `instance_key` 恒为空串 ——
   * 域是实例组，不是任何一个成员实例。可读标签在详情的 `scope` 里，
   * **不要在前端自己拼**（后端 `identity_admin.domain_label` 是唯一产生者）。
   */
  domain_group_id: number
  domain_id: string
  preset_id: number
  platform_userid: string
  nickname: string
  /** 内部数值，用于门槛判定与排序；不是产品的主要面孔 */
  score: number
  stage: AffectionStageValue
  stage_label: string
  /** 由 LLM 按当前人设口吻写的状态描述 */
  state_text: string
  /** 模型给这个人起的称呼；空串 = 没有称呼（不是「还没加载」，直接按空值展示） */
  call_name: string
  /** 最近一次变化的叙述 */
  last_event_text: string
  interaction_count: number
  last_chat_key: string
  last_interact_at: string | null
  update_time: string | null
  /**
   * 本行的账号已经被合并到哪个人名下；空串 = 没被合并过（绝大多数行）。
   *
   * 非空表示这是一份**休眠**数据：Bot 读的是 `merged_into` 那个人的行，
   * 在这一行上调分数**不会有任何效果**。不标出来的话管理员会对着一个
   * 「改了也不动」的行反复调整（删掉别名它就会立刻复活）。
   */
  merged_into: string
  /**
   * 这一行属于哪个人设（可读名字）。同一个人换过人设就有好几行，
   * 不标出来的话只差分数和描述，重置很容易点在一行没人用的旧人设上。
   */
  preset_name: string
  /** 本域里当前生效人设正是这一行人设的频道数；0 = 眼下没有频道在用这一份 */
  in_use_channels: number
}

export interface AffectionListResponse {
  total: number
  items: AffectionItem[]
  stages: AffectionStageInfo[]
}

export interface AffectionEventItem {
  id: number
  delta: number
  score_after: number
  /**
   * 变化来源。**必须与后端 `AffectionEventSource` 逐个取值对齐** ——
   * 这个值直接拼进 `t('detail.source.${source}')`，少一个取值前端就把 i18n 的键名
   * 原样显示成一行乱码（`merge` / `unmerge` 曾经就是这样漏掉的）。
   * 对齐由 `tests/test_affection_domain_and_preset_coverage.py` 钉死（语言包那一侧）。
   */
  source:
    | 'llm'
    | 'tool'
    | 'manual'
    | 'reset'
    | 'merge'
    | 'unmerge'
    | 'domain_merge'
    | 'blocked_clear'
    | 'narrative_clear'
    | 'narrative_edit'
  /** 理由已被删除时恒为空串，看 `deleted` */
  reason: string
  operator: string
  chat_key: string
  /**
   * 触发这条变化的**原始账号**。时间线是按「人」取的并集，合并过的人身上会同时出现
   * 好几个号干的事；不标出来的话「他明明没说话怎么涨分了」没法解释。
   * 后端已经把「空串 = 与本行的人相同」还原成具体账号，前端不必再写一次这条规则。
   */
  source_platform_userid: string
  create_time: string | null
  /** 这一条的理由被管理员删掉了（软删：只删文字，分数与这一行都还在） */
  deleted: boolean
  deleted_by: string
  deleted_at: string | null
}

/** 「这份好感属于：域 X / 人设 甲」 */
export interface AffectionScopeInfo {
  domain_id: string
  domain_label: string
  is_group_domain: boolean
  preset_id: number
  preset_name: string
  /** false = 人设已被删除，这一份是孤儿数据（不级联删，后台可手动清理） */
  preset_exists: boolean
  /** 这一行记的是「人」而不是「某个账号」 */
  person_uid: string
  /** 被合并进这个人的副账号 */
  alias_accounts: string[]
}

/**
 * 一条关系里程碑
 *
 * 它同时是记忆段落（`origin_kind = affection`），所以带 `status`：管理员可能已经在记忆页
 * 把它软删、隔离或清理掉了。**取值与后端 `admin_service.paragraph_status` 逐项对齐**
 * （active / quarantined / deleted / pruned），段落被真删时后端还给 `hard_deleted`，
 * 写它的实例已被单独划给别的租户（段落跟着实例走了）时给 `moved`——这两种都不带正文；
 * 文案在语言包 `detail.milestonesStatus.*` 里，少一个取值就会把键名显示到页面上。
 */
export type AffectionMilestoneStatus =
  | 'active'
  | 'quarantined'
  | 'deleted'
  | 'pruned'
  | 'hard_deleted'
  | 'moved'

export interface AffectionMilestoneItem {
  paragraph_id: number
  event_id: number
  content: string
  summary: string
  event_time: string | null
  status: AffectionMilestoneStatus
}

export interface AffectionDetail {
  item: AffectionItem
  events: AffectionEventItem[]
  scope: AffectionScopeInfo
  milestones: AffectionMilestoneItem[]
}

export interface AffectionStageCount {
  value: AffectionStageValue
  label: string
  count: number
}

export interface AffectionSummary {
  total: number
  stage_counts: AffectionStageCount[]
  top_score: number
}

/** 关系图里「人」节点身上的一份好感（这个人在某个人设下的那一行） */
export interface AffectionGraphRecord {
  record_id: number
  preset_id: number
  preset_name: string
  score: number
  stage: AffectionStageValue
  stage_label: string
  call_name: string
  state_text: string
  interaction_count: number
  last_interact_at: string | null
  last_chat_key: string
  in_use_channels: number
  milestone_count: number
}

/**
 * 关系图节点：`memory_type` 是节点种类（preset / person / milestone / channel）
 *
 * 与记忆图谱的节点同形，两张图共用一个画布。各种类的 `metadata` 内容不同，读取时逐项核对类型。
 */
export interface AffectionGraphNode {
  id: string
  memory_type: string
  ref_id: number
  label: string
  subtitle: string
  status: string
  cognitive_type: string | null
  weight: number
  size: number
  importance: number
  paragraph_id: number | null
  metadata: Record<string, unknown>
}

/** 关系图的边：affection（人 → 人设，就是那一份好感）、milestone、last_seen */
export interface AffectionGraphEdge {
  id: string
  source: string
  target: string
  edge_type: string
  label: string
  weight: number
  strength: number
  status: string
  cognitive_type: string | null
  metadata: Record<string, unknown>
}

export interface AffectionGraphResponse {
  generated_at: string
  /** 满足筛选的份数 / 画进图里的份数 / 合并后休眠、没画的份数 */
  total: number
  drawn: number
  skipped_merged: number
  node_count: number
  edge_count: number
  nodes: AffectionGraphNode[]
  edges: AffectionGraphEdge[]
  stages: AffectionStageInfo[]
}

export const affectionApi = {
  getList: async (
    params: {
      page: number
      page_size: number
      search?: string
      /** 按人精确筛；副账号由服务端解析成主账号 */
      person?: string
      stage?: AffectionStageValue
      sort_by?: AffectionSortBy
    } & InstanceScopeParam
  ): Promise<AffectionListResponse> => {
    const response = await axios.get<AffectionListResponse>('/affection/list', { params })
    return response.data
  },

  /** 关系图（只读）：人设在中间，人、关系里程碑、频道围在外面；搜索与阶段筛选同列表 */
  getGraph: async (
    params: { search?: string; stage?: AffectionStageValue; limit?: number } & InstanceScopeParam
  ): Promise<AffectionGraphResponse> => {
    const response = await axios.get<AffectionGraphResponse>('/affection/graph', { params })
    return response.data
  },

  /**
   * 概览：总数、各阶段人数与最高分
   *
   * `search` 与列表同一口径（昵称或账号里含关键词），阶段筛选不参与 —— 概览本身就要
   * 按阶段分组，按阶段再筛一次只会剩下一个非零项。
   */
  getSummary: async (instanceKey?: string, search?: string): Promise<AffectionSummary> => {
    const response = await axios.get<AffectionSummary>('/affection/summary', {
      params: {
        instance_key: instanceKey || undefined,
        search: search?.trim() || undefined,
      },
    })
    return response.data
  },

  getDetail: async (recordId: number, instanceKey?: string): Promise<AffectionDetail> => {
    const response = await axios.get<AffectionDetail>(`/affection/${recordId}`, {
      params: { instance_key: instanceKey || undefined },
    })
    return response.data
  },

  /** 人工调整：delta 为增减，score 为直接设定，二选一 */
  adjust: async (
    recordId: number,
    body: { delta?: number; score?: number; reason?: string },
    instanceKey?: string
  ): Promise<AffectionItem> => {
    const response = await axios.post<AffectionItem>(`/affection/${recordId}/adjust`, body, {
      params: { instance_key: instanceKey || undefined },
    })
    return response.data
  },

  /** 归零并清空两段描述 */
  reset: async (recordId: number, instanceKey?: string): Promise<AffectionItem> => {
    const response = await axios.post<AffectionItem>(
      `/affection/${recordId}/reset`,
      {},
      { params: { instance_key: instanceKey || undefined } }
    )
    return response.data
  },

  /** 只清两段描述，分数不动 */
  clearNarrative: async (recordId: number, instanceKey?: string): Promise<AffectionItem> => {
    const response = await axios.post<AffectionItem>(
      `/affection/${recordId}/clear-narrative`,
      {},
      { params: { instance_key: instanceKey || undefined } }
    )
    return response.data
  },

  /** 改写两段描述与称呼，分数不动；超长由服务端按上限截断 */
  editNarrative: async (
    recordId: number,
    body: {
      state_text: string
      last_event_text: string
      /**
       * 不传 = 不改称呼；空串 = 清掉称呼。
       *
       * 与后端 `AffectionNarrativeEditRequest.call_name` 同一套语义：漏传这一栏时不能
       * 顺手把模型记下的称呼清掉，所以「编辑印象」对话框一律显式带上它。
       */
      call_name?: string
    },
    instanceKey?: string
  ): Promise<AffectionItem> => {
    const response = await axios.put<AffectionItem>(`/affection/${recordId}/narrative`, body, {
      params: { instance_key: instanceKey || undefined },
    })
    return response.data
  },

  /** 删一条流水的理由（软删：只删文字、留删除痕迹，分数不动） */
  deleteEventReason: async (
    recordId: number,
    eventId: number,
    instanceKey?: string
  ): Promise<AffectionEventItem> => {
    const response = await axios.delete<AffectionEventItem>(
      `/affection/${recordId}/events/${eventId}`,
      { params: { instance_key: instanceKey || undefined } }
    )
    return response.data
  },

  /**
   * 删一条关系里程碑（软删：段落移入「已删除」，可在记忆页找回）
   *
   * 归属由服务端判：只有「这一份好感的某条流水生成过它」才允许删，否则 404。
   */
  deleteMilestone: async (
    recordId: number,
    paragraphId: number,
    instanceKey?: string
  ): Promise<{ ok: boolean }> => {
    const response = await axios.delete<{ ok: boolean }>(
      `/affection/${recordId}/milestones/${paragraphId}`,
      { params: { instance_key: instanceKey || undefined } }
    )
    return response.data
  },

  /**
   * 从段落编号反查它属于哪一份好感（记忆页「打开好感详情」用）
   *
   * 不可见或不是里程碑时服务端一律 404，调用方据此提示「看不到或已不存在」。
   */
  recordOfMilestone: async (
    paragraphId: number,
    instanceKey?: string
  ): Promise<{ record_id: number }> => {
    const response = await axios.get<{ record_id: number }>(
      `/affection/milestones/${paragraphId}/record`,
      { params: { instance_key: instanceKey || undefined } }
    )
    return response.data
  },
}
