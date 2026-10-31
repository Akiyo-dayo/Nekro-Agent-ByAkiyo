/**
 * 实例作用域状态
 *
 * WebUI 的所有管理页面都在一个「作用域」下工作：
 * - `currentInstanceKey === null` 表示全局视图（平台方视角，看到并管理全部数据）
 * - `currentInstanceKey === 'xxx'` 表示实例视图（该实例的完整管理后台）
 *
 * 作用域会持久化到 localStorage，刷新页面后保持不变。
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { QueryClient } from '@tanstack/react-query'

/**
 * 切换作用域时需要失效的查询客户端
 *
 * 作用域是所有列表请求的隐式参数，切换后旧作用域的缓存全部作废；
 * 不失效的话会先闪一帧上一个实例的数据。QueryClient 由 `MainLayout` 在挂载时注册，
 * 避免 store 反向依赖 React 组件树。
 */
let scopedQueryClient: QueryClient | null = null

export function registerInstanceScopeQueryClient(client: QueryClient): void {
  scopedQueryClient = client
}

/**
 * 最近使用过的实例保留几个
 *
 * 这个数字是切换器里「常用」分区的容量。取 6 是因为它要同时满足两件事：多到足以覆盖
 * 一个管理员日常真正来回切的那几个 Bot，又少到能在不滚动的首屏里全部看见 ——
 * 一旦需要滚动，「常用」就不再是快捷方式，和从一长串列表里翻没有区别。
 */
export const RECENT_INSTANCE_LIMIT = 6

interface InstanceScopeState {
  /** 当前作用域实例标识；null 表示全局视图 */
  currentInstanceKey: string | null
  /**
   * 最近进入过的实例，最近的在前
   *
   * 排序依据里唯一「来自管理员自己」的信号：大量实例的部署上，真正会被反复切进去的
   * 只有手上正在处理的那几个，而这件事只有他自己的行为知道，任何服务端指标都推不出来。
   */
  recentInstanceKeys: string[]
  /**
   * 切换作用域，并失效全部查询缓存
   *
   * @param instanceKey 目标实例；null 表示切回全局视图
   * @param options.remember 是否计入「最近使用」。默认 true（人点的）；
   *   `useInstanceScope` 里那种「持久化的实例已不存在，自动回落」的**自动**改写传 false ——
   *   把系统的兜底动作记成使用记录会让常用区显示一个用户从没主动去过的实例。
   */
  setCurrentInstance: (instanceKey: string | null, options?: { remember?: boolean }) => void
}

export const useInstanceScopeStore = create<InstanceScopeState>()(
  persist(
    (set, get) => ({
      currentInstanceKey: null,
      recentInstanceKeys: [],
      setCurrentInstance: (instanceKey, options) => {
        const next = instanceKey?.trim() ? instanceKey.trim() : null
        const remember = options?.remember !== false

        // 使用记录要在「重复选中当前实例」时也更新：用户再点一次同一个实例是一次明确的使用，
        // 而下面的早退是为了避免无谓的缓存失效，两件事不能共用一个判断。
        if (next && remember) {
          const recent = [next, ...get().recentInstanceKeys.filter(key => key !== next)]
          set({ recentInstanceKeys: recent.slice(0, RECENT_INSTANCE_LIMIT) })
        }

        if (get().currentInstanceKey === next) {
          return
        }
        set({ currentInstanceKey: next })
        // 不带过滤条件的 invalidateQueries 会标记所有查询为过期，
        // 挂载中的查询立即重新拉取，未挂载的下次访问时重新拉取。
        void scopedQueryClient?.invalidateQueries()
      },
    }),
    {
      name: 'instance-scope-storage',
    }
  )
)
