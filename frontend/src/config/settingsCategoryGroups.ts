/**
 * 系统配置页的分类分组
 *
 * 后端按 `i18n_category` 给配置项分类（二十多个），平铺成一排页签既看不全也找不到。
 * 这里只在前端把这些分类归成几个大组，后端的分类取值不动。
 *
 * - `categories` 同时写中英文名：页面拿到的是按当前语言本地化后的分类名，两种都要认得。
 * - 组内顺序即页面上的顺序。
 * - 没写进任何一组的分类落进「其他」（含后端没给分类的配置项）。
 * - 已经搬到各功能页「设置」抽屉的六类不在这里，见 `domainSettings.ts`；页面把它们
 *   合成一处「功能页设置」入口。
 */

export type SettingsCategoryGroupKey =
  | 'runtime'
  | 'conversation'
  | 'quota'
  | 'extension'
  | 'ops'
  | 'data'

export interface SettingsCategoryName {
  zh: string
  en: string
}

export interface SettingsCategoryGroup {
  key: SettingsCategoryGroupKey
  /** `settings` 命名空间里的文案键 */
  titleKey: string
  categories: SettingsCategoryName[]
}

export const SETTINGS_CATEGORY_GROUPS: SettingsCategoryGroup[] = [
  {
    key: 'runtime',
    titleKey: 'system.groups.runtime',
    categories: [
      { zh: '基础设置', en: 'Basic Settings' },
      { zh: '代理配置', en: 'Proxy Configuration' },
    ],
  },
  {
    key: 'conversation',
    titleKey: 'system.groups.conversation',
    categories: [
      { zh: '模型配置', en: 'Model Configuration' },
      { zh: '聊天配置', en: 'Chat Configuration' },
    ],
  },
  {
    key: 'quota',
    titleKey: 'system.groups.quota',
    categories: [
      { zh: '额度', en: 'Quota' },
      { zh: '输出过滤', en: 'Output Filter' },
    ],
  },
  {
    key: 'extension',
    titleKey: 'system.groups.extension',
    categories: [
      { zh: '沙盒配置', en: 'Sandbox Configuration' },
      { zh: 'CC 工作区', en: 'CC Workspace' },
      { zh: '插件配置', en: 'Plugin Configuration' },
      { zh: '插件故障隔离', en: 'Plugin Fault Isolation' },
    ],
  },
  {
    key: 'ops',
    titleKey: 'system.groups.ops',
    categories: [
      { zh: '实例监控', en: 'Instance Monitoring' },
      { zh: '运维 Bot', en: 'Ops Bot' },
      { zh: '运维工具', en: 'Ops Tools' },
      { zh: '邮件通知', en: 'Email Notification' },
      { zh: '邮件', en: 'Email' },
      { zh: '公告', en: 'Announcement' },
    ],
  },
  {
    key: 'data',
    titleKey: 'system.groups.data',
    categories: [{ zh: '知识库', en: 'Knowledge Base' }],
  },
]

/** 分类名（任一语言）→ 所在组与组内序号 */
const CATEGORY_INDEX = new Map<string, { group: SettingsCategoryGroup; order: number }>()
SETTINGS_CATEGORY_GROUPS.forEach(group => {
  group.categories.forEach((category, order) => {
    CATEGORY_INDEX.set(category.zh, { group, order })
    CATEGORY_INDEX.set(category.en, { group, order })
  })
})

export const findSettingsCategoryGroup = (
  category: string
): { group: SettingsCategoryGroup; order: number } | undefined => CATEGORY_INDEX.get(category)
