import axios from './axios'
import type { WorldbookImportReport } from './worldbook'

/**
 * 酒馆人设卡接口
 *
 * 路径都相对 `/tavern`，权限一律 `Role.Admin`。类型与 `nekro_agent/schemas/tavern.py`
 * 一一对应：那边加字段这里也要加，两处形状不一致时前端会静默拿到 `undefined`。
 *
 * 导入与检查是 `multipart/form-data`：文件名、字节与选项都走表单，作用域也一样——它们
 * 是**请求的一部分**（决定归属与闸状态），不是可选筛选条件，放 query 里让后端两处读法
 * 不同反而更容易出错。
 */

/** 频道级覆盖把高级提示词槽位单独关着的频道：实例级打开了，这里照样不发送 */
export interface SlotsOffChannel {
  chat_key: string
  channel_name: string
}

/**
 * 导入目标实例上两个闸的生效值
 *
 * `can_enable_advanced_slots` 往下几项只在导入检查里填：有目标实例、且槽位的生效值是关时，
 * 导入确认里可以问「要不要一起打开」。确认文案与配置表单打开这一项时的是同一份（键是
 * `zh-CN` / `en-US`）。
 */
export interface TargetGates {
  worldbook_enabled: boolean
  advanced_slots_enabled: boolean
  can_enable_advanced_slots: boolean
  advanced_slots_global_enabled: boolean
  advanced_slots_off_channels: SlotsOffChannel[]
  advanced_slots_enable_confirm: Record<string, string>
}

/** 一条解析说明；`kind` 是机读分类，`detail` 是给管理员看的中文说明 */
export interface CardNoteItem {
  field: string
  kind: string
  detail: string
}

/** 深度注入的两段值；空的也照样返回，前端自己决定显不显示 */
export interface DepthPromptInspect {
  text: string
  depth: number
}

/**
 * 一条清理规则的导入预览
 *
 * `disabled_reason` 是后端判定这条规则默认关着的原因（空串表示默认启用）。它决定开关
 * 能不能被打开：安全类原因即便请求里写了 `true` 也不会生效，见 `CardImportOptions`。
 */
export interface CleanupRuleInspect {
  index: number
  name: string
  find: string
  replace: string
  enabled: boolean
  disabled_reason: string
}

/** 卡内知识书的统计；不含条目正文 */
export interface CardBookInspect {
  present: boolean
  entries: number
  invalid_keys: number
  decorated: number
  notes: Record<string, number>
}

/** 上传后的检查报告；只解析，不落库 */
export interface CardInspectReport {
  spec: string
  source_format: string
  name: string
  nickname: string
  creator: string
  character_version: string
  tags: string[]
  creator_notes: string
  assembled_content: string
  assembled_chars: number
  /** 拼装后正文很长，界面要提醒「每轮提示词都会带上这么多字」 */
  long_content: boolean
  system_prompt: string
  post_history_instructions: string
  depth_prompt: DepthPromptInspect
  greetings_count: number
  group_greetings_count: number
  book: CardBookInspect
  cleanup_rules: CleanupRuleInspect[]
  avatar_source: 'png' | 'data_uri' | 'default'
  avatar_note: string
  unsupported: CardNoteItem[]
  notes: CardNoteItem[]
  target: TargetGates
}

/** 导入选项；`rule_enabled` 的键是规则在卡片里的序号 */
export interface CardImportOptions {
  include_system_prompt?: boolean
  include_post_history?: boolean
  include_depth_prompt?: boolean
  include_book?: boolean
  rule_enabled?: Record<number, boolean>
  greeting_pick?: 'first' | 'random'
  /** 「打开并导入」：导入后把目标实例的高级提示词槽位打开（实例级覆盖，写审计） */
  enable_advanced_slots?: boolean
}

/**
 * 「打开并导入」的结果
 *
 * `failed` 时人设已经导入，`advanced_slots_error` 写没打开的原因。
 */
export type AdvancedSlotsOutcome =
  | 'not_requested'
  | 'not_needed'
  | 'already_enabled'
  | 'enabled'
  | 'failed'

/** 导入结果；`notes` 是给管理员看的中文说明 */
export interface CardImportResult {
  preset_id: number
  book_id: number | null
  book_report: WorldbookImportReport | null
  notes: string[]
  advanced_slots: AdvancedSlotsOutcome
  advanced_slots_error: string
  advanced_slots_off_channels: SlotsOffChannel[]
}

/**
 * 扩展行里的一条清理规则
 *
 * 后端返回的这条不带 `original`（导出还原用的底稿），所以保存时整条回传不会丢东西；
 * 形状逐字对应后端存的四个字段加两个状态字段。
 */
export interface TavernCleanupRule {
  name: string
  find: string
  replace: string
  trim: string[]
  enabled: boolean
  disabled_reason: string
}

/** 人设的酒馆扩展行 */
export interface PresetCardResponse {
  exists: boolean
  preset_id: number
  card_spec: string
  nickname: string
  source_fields: Record<string, string>
  assembled_sha256: string
  /** 正文是否仍等于拼装结果；手改过正文之后是 false */
  content_assembled: boolean
  macros_enabled: boolean
  greetings: string[]
  group_greetings: string[]
  greeting_pick: string
  post_history: string
  prefill: string
  depth_prompt: string
  depth_prompt_depth: number
  depth_prompt_role: string
  cleanup_rules: TavernCleanupRule[]
  character_version: string
  imported_by: string
  imported_at: string | null
  /** 按请求作用域算出的两个闸：扩展页要写明「这段保存了会不会发出去」 */
  gates: TargetGates
  /**
   * 本进程因累计超时被停用的规则序号
   *
   * 这些规则在库里仍是 `enabled: true`，但实际不执行；扩展页要单独标出来，否则管理员
   * 看到的开关状态与真实行为对不上。
   */
  slow_disabled_rules: number[]
}

/** 改扩展行；只提交要改的字段，没出现的键后端不动 */
export interface PresetCardUpdateRequest {
  nickname?: string
  source_fields?: Record<string, string>
  macros_enabled?: boolean
  greetings?: string[]
  group_greetings?: string[]
  greeting_pick?: string
  post_history?: string
  prefill?: string
  depth_prompt?: string
  depth_prompt_depth?: number
  depth_prompt_role?: string
  cleanup_rules?: TavernCleanupRule[]
  character_version?: string
}

export interface CardReassembleResponse {
  content: string
  assembled_sha256: string
}

export interface TavernPreviewRequest {
  chat_key: string
  /** 留空表示用频道当前人设 */
  preset_id?: number | null
  /** 长度上限在路由里判（400 + 中文原因），不在请求模型里 */
  sample_text?: string | null
}

export interface TavernPreviewMessage {
  role: string
  text: string
  /** 这条消息来自哪一段；组装顺序固定，界面据此分组 */
  source: 'system' | 'example' | 'history'
}

export interface TavernPreviewPrefill {
  text: string
  would_send: boolean
  reason: string
}

export interface TavernPreviewResponse {
  messages: TavernPreviewMessage[]
  prefill: TavernPreviewPrefill
  /** 各段元数据（字数、槽位层名、世界书条目引用、开场白），形状随组装层演进 */
  meta: Record<string, unknown>
  greeting: Record<string, unknown>
}

export interface TavernTrialRequest {
  chat_key: string
  text: string
  /** 不传就用频道当前的主模型组；租户只能用自己名下的组 */
  model_group?: string | null
}

export interface TavernTrialUsage {
  input_tokens: number
  output_tokens: number
  total_tokens: number
}

export interface TavernTrialResponse {
  model: string
  model_group: string
  latency_ms: number
  reply: string
  code: string
  code_missing: boolean
  /** 「渠道把拒绝受理当成正文返回」时命中的类别，空串表示不是拒答 */
  refusal: string
  error_kind: string
  error: string
  usage: TavernTrialUsage
  prefill_state: string
}

export interface TavernAuditItem {
  id: number
  created_at: string
  operator: string
  operator_is_platform: boolean
  layer: string
  adapter_key: string
  instance_key: string
  chat_key: string
  preset_id: number
  field: string
  old_value: string
  new_value: string
}

export interface TavernAuditResponse {
  total: number
  items: TavernAuditItem[]
}

/**
 * 作用域参数
 *
 * 留空表示平台方全局视图；租户身份下 `instance_key` 必填，省略会被后端拒绝而不是退化成
 * 全局视图。
 */
export interface TavernScope {
  instance_key?: string
  adapter_key?: string
}

/** 去掉空值；`0`、`false` 都保留 */
function compact<T extends object>(params: T): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== '')
  )
}

/**
 * 从 `Content-Disposition` 取文件名
 *
 * 优先认 RFC 5987 的 `filename*=UTF-8''…`：中文人设名只能是这个形态，`filename=` 那一份
 * 是给老客户端看的 ASCII 回退。顺序反了会永远拿到 `preset-12.json` 这种名字。
 */
function exportFilename(contentDisposition: unknown, presetId: number, format: 'png' | 'json'): string {
  if (typeof contentDisposition === 'string') {
    const encoded = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)
    if (encoded?.[1]) {
      try {
        return decodeURIComponent(encoded[1])
      } catch {
        // 解不开就继续往下试，坏文件名不该让一次成功的导出变成失败
      }
    }
    const plain = contentDisposition.match(/filename="?([^";]+)"?/)
    if (plain?.[1]) return plain[1]
  }
  return `preset-${presetId}.${format}`
}

export const tavernApi = {
  /** 检查一张卡（只解析、不落库）；错误用 `ApiError.message` 直接给管理员看 */
  inspectCard: async (file: File, scope: TavernScope): Promise<CardInspectReport> => {
    const formData = new FormData()
    formData.append('file', file)
    // 作用域是**表单字段**不是查询参数：后端这两个端点按 `Form(...)` 收，写成 query 会被
    // 静默当成空串，于是租户的导入落到全局视图里
    formData.append('instance_key', scope.instance_key ?? '')
    formData.append('adapter_key', scope.adapter_key ?? '')
    const response = await axios.post<CardInspectReport>('/tavern/cards/inspect', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return response.data
  },

  /** 导入一张卡：建人设、扩展行与卡内知识书 */
  importCard: async (
    file: File,
    options: CardImportOptions,
    scope: TavernScope
  ): Promise<CardImportResult> => {
    const formData = new FormData()
    formData.append('file', file)
    // 选项按 JSON 字符串提交：后端要按字典校验，而表单字段只能是字符串
    formData.append('options', JSON.stringify(options))
    formData.append('instance_key', scope.instance_key ?? '')
    formData.append('adapter_key', scope.adapter_key ?? '')
    const response = await axios.post<CardImportResult>('/tavern/cards/import', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return response.data
  },

  /** 读扩展行；没有行时 `exists: false`，其余字段是新建人设时的默认值 */
  getCard: async (presetId: number, scope: TavernScope): Promise<PresetCardResponse> => {
    const response = await axios.get<PresetCardResponse>(`/tavern/presets/${presetId}/card`, {
      params: compact(scope),
    })
    return response.data
  },

  /** 改扩展行；保存规则之后 `slow_disabled_rules` 会被清空 */
  updateCard: async (
    presetId: number,
    patch: PresetCardUpdateRequest,
    scope: TavernScope
  ): Promise<PresetCardResponse> => {
    const response = await axios.put<PresetCardResponse>(
      `/tavern/presets/${presetId}/card`,
      patch,
      { params: compact(scope) }
    )
    return response.data
  },

  /** 用当前模板与来源字段重拼正文，覆盖手改 */
  reassemble: async (presetId: number, scope: TavernScope): Promise<CardReassembleResponse> => {
    const response = await axios.post<CardReassembleResponse>(
      `/tavern/presets/${presetId}/card/reassemble`,
      null,
      { params: compact(scope) }
    )
    return response.data
  },

  /**
   * 导出 PNG / JSON 并直接触发浏览器下载
   *
   * 不看导出的内容，只把它落成文件；因此这里没有返回值。下载失败（权限、模板报错）会抛
   * `ApiError`，由调用方弹 snackbar。
   */
  exportCard: async (
    presetId: number,
    format: 'png' | 'json',
    scope: TavernScope
  ): Promise<void> => {
    const response = await axios.get<Blob>(`/tavern/presets/${presetId}/export`, {
      // 导出只认作用域，不看适配器：`adapter_key` 不传
      params: compact({ instance_key: scope.instance_key, format }),
      responseType: 'blob',
    })
    const blob =
      response.data instanceof Blob
        ? response.data
        : new Blob([response.data], {
            type: format === 'png' ? 'image/png' : 'application/json',
          })
    const url = window.URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.setAttribute('download', exportFilename(response.headers['content-disposition'], presetId, format))
    document.body.appendChild(link)
    link.click()
    window.URL.revokeObjectURL(url)
    document.body.removeChild(link)
  },

  /** 预览：看某个人设在这个频道里会拼出什么；不调模型、不写库 */
  preview: async (req: TavernPreviewRequest): Promise<TavernPreviewResponse> => {
    const response = await axios.post<TavernPreviewResponse>('/tavern/preview', req)
    return response.data
  },

  /** 试发一次：用管理员自己的模型组发，不发进频道、不进记忆 */
  trial: async (req: TavernTrialRequest): Promise<TavernTrialResponse> => {
    const response = await axios.post<TavernTrialResponse>('/tavern/trial', req)
    return response.data
  },

  /** 配置变更记录；租户只看得到自己实例的行 */
  listAudit: async (params: {
    instance_key?: string
    page?: number
    page_size?: number
  }): Promise<TavernAuditResponse> => {
    const response = await axios.get<TavernAuditResponse>('/tavern/audit', {
      params: compact(params),
    })
    return response.data
  },
}
