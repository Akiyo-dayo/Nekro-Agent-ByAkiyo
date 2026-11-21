/**
 * 深链接里的实例作用域（`?instance=<实例标识>`）
 *
 * 好感页与记忆页互相跳转时，链接带上目标那一行所属的实例。作用域是顶栏切换器的全局状态、
 * 只持久化在本地：链接若不带它，在另一个实例视图下（或换个浏览器）打开，就会显示「看不到」，
 * 而那一行其实就在隔壁实例里。
 *
 * 规则：
 * - 只在作用域已确定、当前是**另一个具体实例**、并且目标实例在当前账号可选的实例里时切过去；
 * - 全局视图本来就看得到全部，不改它（平白把人从全局视图拽进某个实例反而让人困惑）；
 * - 目标实例看不到时不切，页面照常显示「看不到」，不替用户猜一个别的实例；
 * - 参数用过一次就从地址栏去掉，免得之后手动切换作用域时又被它拽回来。
 */

import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useInstanceScope } from './useInstanceScope'

export const DEEP_LINK_INSTANCE_PARAM = 'instance'

/** 给站内深链接拼上实例参数；实例标识为空（例如实例组的好感行）时不加，沿用当前作用域 */
export function withDeepLinkInstance(path: string, instanceKey: string | null | undefined): string {
  const key = instanceKey?.trim()
  if (!key) return path
  const separator = path.includes('?') ? '&' : '?'
  return `${path}${separator}${DEEP_LINK_INSTANCE_PARAM}=${encodeURIComponent(key)}`
}

export function useDeepLinkInstance(): void {
  const [searchParams, setSearchParams] = useSearchParams()
  const { currentInstanceKey, isScopeReady, instances, setCurrentInstance } = useInstanceScope()
  const target = searchParams.get(DEEP_LINK_INSTANCE_PARAM)

  useEffect(() => {
    if (target === null || !isScopeReady) return
    const wanted = target.trim()
    const reachable = instances.some(instance => instance.instance_key === wanted)
    if (wanted && reachable && currentInstanceKey !== null && currentInstanceKey !== wanted) {
      setCurrentInstance(wanted)
    }
    // 用函数式写法基于最新的参数改：同一页还有别的参数（`record` / `item` / 筛选）在被读写
    setSearchParams(
      previous => {
        const next = new URLSearchParams(previous)
        next.delete(DEEP_LINK_INSTANCE_PARAM)
        return next
      },
      { replace: true }
    )
  }, [target, isScopeReady, instances, currentInstanceKey, setCurrentInstance, setSearchParams])
}
