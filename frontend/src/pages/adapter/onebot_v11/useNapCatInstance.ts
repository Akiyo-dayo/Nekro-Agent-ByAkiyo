/**
 * NapCat 页面的实例作用域
 *
 * 容器端点按实例寻址（后端按 `nekro_napcat_{实例标识}` 找容器），而顶部的实例切换器
 * 是全局的：它可能停在「全局视图」，也可能停在一个别的适配器的实例上。这里把它收敛成
 * 一个明确结论，而不是让每个页面各自猜：
 *
 * - 切换器选中的就是某个 OneBot 实例 → 用它；
 * - 否则整个部署里只有一个 OneBot 实例 → 用那个（单实例部署根本感知不到作用域这一层）；
 * - 否则不给结论，让页面提示用户先选实例。**刻意不随便挑第一个**：日志里带着 NapCat
 *   的 WebUI 令牌，把 A 实例的日志显示成"当前实例"的，等于让人拿错账号的控制台钥匙。
 *
 * 结论为 null 时请求不带 `instance_key`，后端会回退到单实例时代那个固定容器名，
 * 老部署升级后行为不变。
 */

import { useMemo } from 'react'
import { useInstanceScope } from '../../../hooks/useInstanceScope'

export interface NapCatInstanceScope {
  /** 当前该操作哪个实例的容器；null 表示不带实例标识（走后端的遗留容器回退） */
  instanceKey: string | null
  /** 实例列表与身份都已就位，可以安全发请求 */
  isReady: boolean
  /** 有多个 OneBot 实例但作用域停在全局，需要用户先选一个 */
  needsSelection: boolean
}

export function useNapCatInstance(): NapCatInstanceScope {
  const { instances, currentInstanceKey, isScopeReady } = useInstanceScope()

  const onebotInstances = useMemo(
    () => instances.filter(instance => instance.adapter_key === 'onebot_v11'),
    [instances]
  )

  const selected = onebotInstances.find(instance => instance.instance_key === currentInstanceKey)
  const soleInstance = onebotInstances.length === 1 ? onebotInstances[0] : undefined
  const instanceKey = selected?.instance_key ?? soleInstance?.instance_key ?? null

  return {
    instanceKey,
    isReady: isScopeReady,
    needsSelection: isScopeReady && instanceKey === null && onebotInstances.length > 1,
  }
}
