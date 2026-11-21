/**
 * 六个功能域的「设置」抽屉配置
 *
 * 后端的配置项按 `i18n_category` 分类；同一个分类在一页里既有常用项，也有几十项高级项。
 * 这里只声明「哪些键算常用、高级项怎么分小组」，具体取值与保存仍由 `ConfigTable` 负责。
 *
 * `title` 存的是 `domain-settings` 命名空间里的文案键，而不是显示文本：组件里统一 `t(title)`。
 * 未匹配任何小组的键会落进「其他」，与规范 §5.3 一致。
 */

import type { ConfigItem } from '../components/common/ConfigTable'

export type DomainKey = 'memory' | 'affection' | 'worldbook' | 'state' | 'promptLab' | 'presets'

export interface DomainAdvancedGroup {
  /** `domain-settings` 命名空间里的文案键 */
  title: string
  /** 匹配规则；数组形式同时用来指定组内顺序（字符串项按精确键匹配） */
  match: RegExp | Array<string | RegExp>
}

export interface DomainSettingsDefinition {
  key: DomainKey
  /** 后端 `i18n_category` 的中文名 */
  category: string
  /** 后端 `i18n_category` 的英文名；英文界面下配置分类会显示成它 */
  categoryEn: string
  /** 页面地址（带默认标签），用于系统设置页的「前往」 */
  pagePath: string
  /** 抽屉标题文案键 */
  titleKey: string
  common: string[]
  advancedGroups: DomainAdvancedGroup[]
}

export interface DomainConfigGroup {
  title: string
  keys: string[]
}

const MEMORY_COMMON = [
  'MEMORY_ENABLE_SYSTEM',
  'MEMORY_DOMAIN_SCOPE_ENABLED',
  'MEMORY_ENABLE_FOR_CHAT',
  'MEMORY_RECALL_GROUP_TO_GROUP',
  'MEMORY_RECALL_GROUP_TO_PRIVATE',
  'MEMORY_RECALL_PRIVATE_TO_GROUP',
  'MEMORY_CONTEXT_MAX_ITEMS',
  'MEMORY_CHANNEL_SUMMARY_ENABLED',
]

const MEMORY_RETENTION_KEYS = [
  'MEMORY_EPISODIC_HALF_LIFE_SECONDS',
  'MEMORY_RELATION_HALF_LIFE_SECONDS',
  'MEMORY_SEMANTIC_HALF_LIFE_DAYS',
  'MEMORY_RETRIEVAL_REINFORCE_BOOST',
  'MEMORY_PRUNE_ENABLED',
  'MEMORY_PRUNE_INTERVAL_HOURS',
  'MEMORY_PRUNE_PARAGRAPH_THRESHOLD',
  'MEMORY_PRUNE_RELATION_THRESHOLD',
  'MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS',
  'MEMORY_LOG_RETENTION_DAYS',
  'MEMORY_PROMPT_ROUNDS_KEEP',
  'MEMORY_PROMPT_ROUNDS_MAX_AGE_DAYS',
]

export const DOMAIN_SETTINGS: Record<DomainKey, DomainSettingsDefinition> = {
  memory: {
    key: 'memory',
    category: '记忆系统',
    categoryEn: 'Memory System',
    pagePath: '/memory/items',
    titleKey: 'title.memory',
    common: MEMORY_COMMON,
    advancedGroups: [
      { title: 'advanced.memory.retention', match: MEMORY_RETENTION_KEYS },
      {
        title: 'advanced.memory.injection',
        match: [
          /^MEMORY_CONTEXT_MAX_LENGTH$/,
          /^MEMORY_RECALL_COOLDOWN_/,
          /^MEMORY_MEMBER_ACTIVE_DAYS$/,
          /^MEMORY_CHANNEL_SUMMARY_MAX_CHARS$/,
          /^MEMORY_INSTANCE_SHARING_GROUPS$/,
        ],
      },
      {
        title: 'advanced.memory.retrieval',
        match: [/^MEMORY_RETRIEVAL_(?!REINFORCE_BOOST$)/, /^MEMORY_ENHANCED_RETRIEVAL_/],
      },
      {
        title: 'advanced.memory.consolidation',
        match: [
          /^MEMORY_CONSOLIDATION_/,
          /^MEMORY_LLM_MAX_RETRIES$/,
          /^MEMORY_SEMANTIC_/,
          /^MEMORY_EMBEDDING_/,
        ],
      },
      { title: 'advanced.memory.episode', match: [/^MEMORY_EPISODE_/] },
      {
        title: 'advanced.memory.scheduler',
        match: [
          /^MEMORY_SCHEDULER_/,
          /^MEMORY_REBUILD_LOOKBACK_DAYS$/,
          /^MEMORY_STARTUP_RECOVERY_/,
        ],
      },
    ],
  },
  affection: {
    key: 'affection',
    category: '好感度系统',
    categoryEn: 'Affection System',
    pagePath: '/affection',
    titleKey: 'title.affection',
    common: [
      'AFFECTION_ENABLE_SYSTEM',
      'AFFECTION_ENABLE_PROMPT_INJECTION',
      'AFFECTION_ENABLE_REPLY_GATE',
      'AFFECTION_MILESTONE_ENABLED',
      'AFFECTION_MODEL_GROUP',
    ],
    advancedGroups: [
      {
        title: 'advanced.affection.prompt',
        match: [
          /^AFFECTION_PROMPT_MAX_USERS$/,
          /^AFFECTION_UPDATE_MIN_INTERVAL_SECONDS$/,
          /^AFFECTION_CACHE_TTL_SECONDS$/,
          /^AFFECTION_ABSENCE_HINT_DAYS$/,
        ],
      },
      {
        title: 'advanced.affection.budget',
        match: [
          /^AFFECTION_MAX_DELTA_PER_ROUND$/,
          /^AFFECTION_DAILY_/,
          /^AFFECTION_WEEKLY_/,
          /^AFFECTION_ENABLE_DIMINISHING_RETURNS$/,
        ],
      },
      { title: 'advanced.affection.milestone', match: [/^AFFECTION_MILESTONE_/] },
    ],
  },
  worldbook: {
    key: 'worldbook',
    category: '世界书',
    categoryEn: 'Worldbook',
    pagePath: '/worldbook/books',
    titleKey: 'title.worldbook',
    common: [
      'WORLDBOOK_ENABLED',
      'WORLDBOOK_VECTOR_ENABLED',
      'WORLDBOOK_BUDGET_PERCENT',
      'WORLDBOOK_BUDGET_CAP_TOKENS',
      'WORLDBOOK_AUTO_QUARANTINE_BLOCKED_ROUNDS',
    ],
    advancedGroups: [
      { title: 'advanced.worldbook.lookup', match: [/^WORLDBOOK_LOOKUP_/] },
      {
        title: 'advanced.worldbook.maintenance',
        match: [/^WORLDBOOK_MAX_ENTRIES_PER_ROUND$/],
      },
    ],
  },
  state: {
    key: 'state',
    category: '角色状态',
    categoryEn: 'Character State',
    pagePath: '/state/channels',
    titleKey: 'title.state',
    common: ['STATE_IMAGE_SEND_MODE', 'STATE_IMAGE_EVERY_N', 'STATE_COMPAT_PARSE'],
    advancedGroups: [
      {
        title: 'advanced.state.block',
        match: [/^STATE_PROMPT_MAX_TOKENS$/, /^STATE_PRESENT_WINDOW_MINUTES$/],
      },
      { title: 'advanced.state.image', match: [/^STATE_IMAGE_MIN_N$/] },
    ],
  },
  promptLab: {
    key: 'promptLab',
    category: '高级提示词槽位',
    categoryEn: 'Advanced Prompt Slots',
    pagePath: '/prompt-lab/preview',
    titleKey: 'title.promptLab',
    common: ['PROMPT_ADVANCED_SLOTS_ENABLED', 'PROMPT_RP_PRESET_ID'],
    advancedGroups: [
      {
        title: 'advanced.promptLab.slots',
        match: [/^PROMPT_POST_HISTORY$/, /^PROMPT_AUXILIARY$/, /^PROMPT_PREFILL$/],
      },
    ],
  },
  presets: {
    key: 'presets',
    category: '酒馆兼容',
    categoryEn: 'Tavern Compatibility',
    pagePath: '/presets',
    titleKey: 'title.presets',
    common: ['TAVERN_COMPAT_ENABLED'],
    advancedGroups: [
      {
        title: 'advanced.presets.runtime',
        match: [
          /^TAVERN_CARD_TEMPLATE$/,
          /^TAVERN_TRIAL_DAILY_LIMIT$/,
          /^PERSONA_GREETING_ENABLED$/,
        ],
      },
    ],
  },
}

export const DOMAIN_SETTINGS_LIST = Object.values(DOMAIN_SETTINGS)

export const getDomainSettings = (domain: DomainKey): DomainSettingsDefinition =>
  DOMAIN_SETTINGS[domain]

/** 按域名找定义；系统设置页跳转卡用 */
export const findDomainByCategory = (category: string): DomainSettingsDefinition | undefined =>
  DOMAIN_SETTINGS_LIST.find(
    item => item.category === category || item.categoryEn === category
  )

const categoryOf = (item: ConfigItem): string => {
  const dict = item.i18n_category
  if (dict && typeof dict === 'object') {
    return dict['zh-CN'] || item.category || ''
  }
  return typeof dict === 'string' ? dict : item.category || ''
}

const matchesGroup = (group: DomainAdvancedGroup, key: string): boolean =>
  Array.isArray(group.match)
    ? group.match.some(rule => (typeof rule === 'string' ? rule === key : rule.test(key)))
    : group.match.test(key)

/**
 * 把某域名下的配置键拆成常用项与高级小组
 *
 * `configs` 应是接口返回的完整列表（实例层会额外带 `enable_X` 开关）。`enable_X` 不参与
 * 分组，它只用来让 `ConfigTable` 算出覆盖开关的状态。
 */
export function buildDomainSections(
  definition: DomainSettingsDefinition,
  configs: ConfigItem[]
): { commonKeys: string[]; advancedGroups: DomainConfigGroup[] } {
  const domainItems = configs.filter(
    item =>
      !item.is_hidden &&
      (categoryOf(item) === definition.category || categoryOf(item) === definition.categoryEn)
  )
  const allKeys = new Set(domainItems.map(item => item.key))
  const commonKeys = definition.common.filter(key => allKeys.has(key))
  const commonSet = new Set(commonKeys)
  const candidates = domainItems.filter(
    item => !commonSet.has(item.key) && !item.key.startsWith('enable_')
  )
  const remaining = new Set(candidates.map(item => item.key))
  const advancedGroups: DomainConfigGroup[] = []

  definition.advancedGroups.forEach(group => {
    const matched = candidates.filter(
      item => remaining.has(item.key) && matchesGroup(group, item.key)
    )
    if (Array.isArray(group.match)) {
      const explicitOrder = group.match.filter((rule): rule is string => typeof rule === 'string')
      matched.sort(
        (a, b) => explicitOrder.indexOf(a.key) - explicitOrder.indexOf(b.key)
      )
    }
    if (matched.length === 0) {
      return
    }
    matched.forEach(item => remaining.delete(item.key))
    advancedGroups.push({ title: group.title, keys: matched.map(item => item.key) })
  })

  if (remaining.size > 0) {
    advancedGroups.push({
      title: 'advanced.other',
      keys: candidates.filter(item => remaining.has(item.key)).map(item => item.key),
    })
  }

  return { commonKeys, advancedGroups }
}
