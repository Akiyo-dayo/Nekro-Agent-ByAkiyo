import { useMemo } from 'react'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import type { WorldbookScopeParams } from '../../services/api/worldbook'

/**
 * 写操作（建书、导入成新书、记一笔）要落在的实例作用域
 *
 * 单实例部署里没有顶栏切换器，平台方的作用域恒为全局视图（`useInstanceScope` 刻意如此，
 * 让单实例的用户感知不到「作用域」这一层）。可域书、个人书与「记一笔」都必须属于某个实例，
 * 后端没有实例就 400，界面上又无从选——这时只有一个可能的答案，就隐式用这个唯一的实例。
 * 多实例时仍要求先在顶栏选，不替人猜；租户本来就钉在自己的实例上，原样返回。
 *
 * 只给写操作用：列表与筛选照旧按顶栏作用域走，全局视图仍是「看全部」。
 */
export function useWorldbookWriteScope(scope: WorldbookScopeParams): WorldbookScopeParams {
  const { instances } = useInstanceScope()
  return useMemo(() => {
    if (scope.instance_key || instances.length !== 1) return scope
    const only = instances[0]
    return { instance_key: only.instance_key, adapter_key: only.adapter_key }
  }, [scope, instances])
}
