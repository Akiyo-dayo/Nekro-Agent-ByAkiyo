/**
 * 实例作用域 Hook
 *
 * 页面统一通过它读取「当前在哪个实例的后台里」，并拿到该实例的元信息
 * （显示名、适配器标识等）。作用域状态本身在 `stores/instanceScope.ts`。
 */

import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { consoleApi, type InstanceOption } from '../services/api/console'
import { useInstanceScopeStore } from '../stores/instanceScope'
import { useIdentity } from './useIdentity'

export const INSTANCE_SCOPE_QUERY_KEY = ['instance-scope', 'instances'] as const

export interface InstanceScope {
  /** 可选择的实例（已归档的不出现） */
  instances: InstanceOption[]
  /** 当前作用域实例标识；null 表示全局视图 */
  currentInstanceKey: string | null
  /** 当前作用域实例的完整信息；全局视图或尚未加载时为 null */
  currentInstance: InstanceOption | null
  /** 是否处于全局视图 */
  isGlobalScope: boolean
  /**
   * 是否需要向用户暴露作用域概念
   *
   * 单实例（含零实例）部署下切换器不渲染、作用域恒为全局，用户完全感知不到这一层。
   */
  isScopeSwitchable: boolean
  /**
   * 「全局视图」这一档是否存在
   *
   * **租户没有全局视图**：`instance_key` 为空对平台方是"看全部"，
   * 对租户则被后端 `resolve_instance_scope` 直接 400。切换器据此决定要不要渲染那一项 ——
   * 渲染出来就是一个点了必然被弹回别的实例的选项，而"弹回别的实例"比"没有这一项"危险得多。
   *
   * **身份没确认之前一律为 false。** 身份和实例列表是两个各自独立的请求：实例列表先回来时
   * `isTenantScoped` 还是兜底的"平台方"，于是租户在冷启动的那一小段里照样看得到「全局视图」，
   * 点下去就是上面那条被禁掉的路径。这一档宁可晚出现一帧，也不能早出现一帧。
   */
  isGlobalScopeAvailable: boolean
  /** 最近进入过的实例（最近的在前），已剔除当前已不存在的 */
  recentInstanceKeys: string[]
  /** 实例列表是否仍在加载 */
  isLoading: boolean
  /**
   * 作用域是否已经确定，可以安全地发起带作用域的请求
   *
   * 身份与实例列表都还没到位时 `currentInstanceKey` 只是个中间态：租户身份下它可能
   * 暂时是 null（本次是首次登录，或持久化的作用域还没被校验），而
   * `/affection/*`、`/presets/*` 这类接口对租户强制要求 `instance_key`，
   * 提前发出去就是一个必然的 400「租户身份必须指定实例作用域」。
   * 需要作用域的页面一律用它作为查询的 `enabled` 条件。
   */
  isScopeReady: boolean
  setCurrentInstance: (instanceKey: string | null) => void
}

export function useInstanceScope(): InstanceScope {
  const currentInstanceKey = useInstanceScopeStore(state => state.currentInstanceKey)
  const storedRecentKeys = useInstanceScopeStore(state => state.recentInstanceKeys)
  const setCurrentInstance = useInstanceScopeStore(state => state.setCurrentInstance)
  const { isTenantScoped, isReadOnly, isLoading: isIdentityLoading } = useIdentity()

  // 切换器要的只是「有哪些实例、叫什么、绑的哪个号、什么状态」，用轻量端点而不是
  // `GET /console/instances`：后者每个 builtin_napcat 实例要花一次 Docker inspect、
  // 外加三条跨全表聚合，而这个查询挂在 MainLayout 上，每开一个后台标签页就要付一次。
  //
  // 只读租户成员（tenant_user）读不了它：`GET /console/instance-options` 是
  // `Role.Admin`（`tenant_perm_level` 把 tenant_user 钉在 `Role.User`），对他每次
  // 都是必然的 403。与 `useTenancyVisibility` 里「无权限身份下不发这个请求，避免
  // 必然的 403」是同一条既定立场——身份未确认前也一律不发，否则只读成员每次冷启动
  // 都会先打出一个注定 403 的请求（`FALLBACK_ROLE` 按平台方处理）。被跳过时
  // `instances` 恒为空，切换器 `length > 1` 不成立、不渲染，与请求失败时的表现一致。
  const { data, isLoading, isSuccess, isError } = useQuery({
    queryKey: INSTANCE_SCOPE_QUERY_KEY,
    queryFn: () => consoleApi.listInstanceOptions(),
    staleTime: 60_000,
    retry: false,
    enabled: !isIdentityLoading && !isReadOnly,
  })

  const instances = useMemo(
    () => (data?.items ?? []).filter(instance => instance.status !== 'archived'),
    [data]
  )
  const isScopeSwitchable = instances.length > 1

  // 租户身份没有「全局视图」这一档：后端对他们直接拒绝空作用域。
  // 因此单实例租户也必须被钉在那一个实例上——否则切换器不渲染、作用域恒为 null，
  // 页面永远拿不到数据且没有任何自救入口。
  const requiresPinnedScope = isTenantScoped

  // 列表请求失败（后端未就绪等）也算「已确定」：一直挂着不给结论会让页面永远转圈，
  // 而失败时按全局视图走与改造前完全一致。被上面 `enabled` 跳过的查询同样要算
  // 「已确定」——它既不会 success 也不会 error，只读成员的终态就是「没有实例清单」，
  // 不给结论的话依赖 `isScopeReady` 的页面（`/affection`、`/presets` 等）会永远转圈。
  // `isReadOnly` 只有身份确认后才可信（此前是按平台方处理的兜底值），
  // 而 `isScopeReady` 本来就要求身份确认完毕，两者在这里汇合。
  const isInstanceListSettled = isSuccess || isError || isReadOnly
  const isScopeReady = !isIdentityLoading && isInstanceListSettled

  const isKnownInstance = instances.some(instance => instance.instance_key === currentInstanceKey)

  // 结论只在列表到位后才做，否则加载途中会把持久化的作用域误判成「已不存在」。
  // 平台方：实例被归档/删除或部署缩回单实例时回落到全局视图，
  //         否则页面会一直按一个不再可选的实例过滤，表现为「什么都没有」。
  // 租户：没有全局视图这一档，钉在自己的实例上；当前选择失效时自动换到第一个可用实例。
  let effectiveInstanceKey = currentInstanceKey
  if (isScopeReady) {
    if (requiresPinnedScope) {
      effectiveInstanceKey = isKnownInstance
        ? currentInstanceKey
        : (instances[0]?.instance_key ?? null)
    } else if (!isScopeSwitchable || !isKnownInstance) {
      effectiveInstanceKey = null
    }
  }

  // 把结论写回 store，让持久化与其他消费方（切换器、横幅）看到同一个作用域。
  // `remember: false`：这是系统的兜底改写，不是用户的一次使用，不该挤进「常用」。
  useEffect(() => {
    if (isScopeReady && effectiveInstanceKey !== currentInstanceKey) {
      setCurrentInstance(effectiveInstanceKey, { remember: false })
    }
  }, [isScopeReady, effectiveInstanceKey, currentInstanceKey, setCurrentInstance])

  const currentInstance =
    instances.find(instance => instance.instance_key === effectiveInstanceKey) ?? null

  // 已被删除/归档的实例不该继续占着「常用」名额。持久化的记录活得比实例久，
  // 不剔除的话一个删掉半年的实例会永远排在前面。
  const recentInstanceKeys = useMemo(() => {
    const alive = new Set(instances.map(instance => instance.instance_key))
    return storedRecentKeys.filter(key => alive.has(key))
  }, [storedRecentKeys, instances])

  return {
    instances,
    currentInstanceKey: effectiveInstanceKey,
    currentInstance,
    isGlobalScope: effectiveInstanceKey === null,
    isScopeSwitchable,
    // `isScopeReady` 里已经含「身份确认完毕」，见字段注释：身份没到位时不能暴露这一档
    isGlobalScopeAvailable: isScopeReady && !requiresPinnedScope,
    recentInstanceKeys,
    isLoading,
    isScopeReady,
    setCurrentInstance,
  }
}
