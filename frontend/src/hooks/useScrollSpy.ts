import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * 滚动位置对应的目录项
 *
 * 长页面上左侧目录要跟着滚动高亮：给每个锚点元素套一个观察器，取「当前落在视口上
 * 半部分里的第一个」作为高亮项，顺序按调用方给的 `keys`，而不是按进入视口的先后，
 * 这样两段同时可见时高亮不会来回跳。
 *
 * 点目录跳转时 `scrollIntoView` 会带一段平滑动画，途中经过的段落会依次进入视口；
 * 这段时间里观察到的变化不是用户滚动造成的，所以跳转前用 `lockTo` 把高亮钉在目标项
 * 上，默认 700ms 后再交回观察器。
 *
 * `root` 用视口本身（`root: null`）：浏览器算相交时会把祖先的裁剪一起算进去，所以
 * 即便真正的滚动容器是页面里的某一层，滚出那一层的条目也不会被当成可见；只是
 * `rootMargin` 的百分比按视口算，不是按那个容器的可见高度算。
 */
export function useScrollSpy<K extends string>(
  keys: readonly K[],
  getElement: (key: K) => HTMLElement | null,
  options?: { enabled?: boolean }
): { active: K | null; lockTo: (key: K, ms?: number) => void } {
  const enabled = options?.enabled ?? true

  const [active, setActive] = useState<K | null>(keys[0] ?? null)

  // 观察器只在需要时重建，回调里仍要读最新的取元素函数与 key 列表
  const keysRef = useRef(keys)
  keysRef.current = keys
  const getElementRef = useRef(getElement)
  getElementRef.current = getElement

  const activeRef = useRef<K | null>(active)
  const lockRef = useRef<{ key: K; until: number } | null>(null)
  const timerRef = useRef<number | null>(null)

  // 当前被观察的元素；重新挂载（换了内容、同名的段落被换掉）时据此判断要不要重连
  const observedRef = useRef<Map<K, Element>>(new Map())
  const observerRef = useRef<IntersectionObserver | null>(null)

  const signature = keys.join('\u0000')

  const attach = useCallback(() => {
    if (!enabled || typeof IntersectionObserver === 'undefined') return

    observerRef.current?.disconnect()
    observerRef.current = null
    observedRef.current = new Map()

    const currentKeys = keysRef.current
    const visible = new Set<K>()
    const keyOfElement = new Map<Element, K>()

    const observer = new IntersectionObserver(
      entries => {
        entries.forEach(entry => {
          const key = keyOfElement.get(entry.target)
          if (key === undefined) return
          if (entry.isIntersecting) visible.add(key)
          else visible.delete(key)
        })

        const lock = lockRef.current
        if (lock !== null && lock.until > Date.now()) return
        lockRef.current = null

        // 一段都没进来时保留上一次的值，避免滚到页尾时目录突然全灭
        const next = currentKeys.find(key => visible.has(key))
        if (next !== undefined && next !== activeRef.current) {
          activeRef.current = next
          setActive(next)
        }
      },
      { root: null, rootMargin: '0px 0px -60% 0px', threshold: 0 }
    )

    const observed = new Map<K, Element>()
    currentKeys.forEach(key => {
      const element = getElementRef.current(key)
      if (!element) return
      observed.set(key, element)
      keyOfElement.set(element, key)
      observer.observe(element)
    })

    observerRef.current = observer
    observedRef.current = observed

    // 目录项增减（例如预设卡片出现或消失）后，旧的高亮可能已经不在列表里
    if (activeRef.current !== null && !currentKeys.includes(activeRef.current)) {
      const fallback = currentKeys[0] ?? null
      activeRef.current = fallback
      setActive(fallback)
    }
  }, [enabled])

  useEffect(() => {
    attach()
    return () => {
      observerRef.current?.disconnect()
      observerRef.current = null
      observedRef.current = new Map()
    }
  }, [attach, signature])

  // 再预览一次时段落会整批换成新的节点（key 没变），旧观察对象已经不在文档里，
  // 这里在每次渲染后核对一遍，换了就重连。
  useEffect(() => {
    if (!enabled) return
    const changed = keysRef.current.some(
      key => (getElementRef.current(key) ?? null) !== (observedRef.current.get(key) ?? null)
    )
    if (changed) attach()
  })

  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      timerRef.current = null
    },
    []
  )

  const lockTo = useCallback((key: K, ms = 700) => {
    lockRef.current = { key, until: Date.now() + ms }
    activeRef.current = key
    setActive(key)
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => {
      lockRef.current = null
      timerRef.current = null
    }, ms)
  }, [])

  return { active, lockTo }
}