import type { ReactNode } from 'react'
import type { WorldbookPageTab } from '../../router/routes'
import type { WorldbookScopeParams } from '../../services/api/worldbook'

/** 每个标签页拿到的上下文：作用域与作用域是否已就绪 */
export interface WorldbookTabContext {
  scope: WorldbookScopeParams
  isScopeReady: boolean
}

/**
 * 标签页定义
 *
 * 页面由这个数组驱动：以后加一个标签只往数组里加一项，不用再改 Tabs 的渲染与
 * 标签值的分支。
 */
export interface WorldbookPageTabDefinition {
  key: WorldbookPageTab
  /** 翻译键（`worldbook` 命名空间） */
  labelKey: string
  render: (context: WorldbookTabContext) => ReactNode
}
