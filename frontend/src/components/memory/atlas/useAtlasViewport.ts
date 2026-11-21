import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react'

/**
 * 图谱画布的平移与缩放
 *
 * 拖动、滚轮时不走 React 状态：直接在下一帧把变换写到画布的 style 上，停下 80 毫秒后再同步回
 * 状态（缩放滑块、标签门槛、布局的「放大多画几个」都读状态）。这是从上游 `MemoryTab` 原样搬出来的，
 * 放进一个钩子是为了让画布组件与外面的工具栏共用同一份缩放。
 */

export const ATLAS_MIN_ZOOM = 0.72
export const ATLAS_MAX_ZOOM = 1.65

export interface AtlasViewport {
  zoom: number
  pan: { x: number; y: number }
  sceneRef: MutableRefObject<HTMLDivElement | null>
  /** 此刻的缩放（拖动、滚轮中途还没同步到 `zoom` 的那个值） */
  getZoom: () => number
  /** 把缩放设成这个值（会夹在上下限之间） */
  updateZoom: (nextZoom: number) => void
  applyPanDelta: (deltaX: number, deltaY: number) => void
  /** 把视图中心移到世界坐标 (x, y) 附近，缩放至少到 minZoom */
  focusOn: (x: number, y: number, minZoom: number) => void
  /** 回到初始视图：居中、缩放 1（缩放浮层的「适配」） */
  resetView: () => void
  /** 拖动结束时立刻同步状态（不等 80 毫秒） */
  syncViewportState: () => void
  applySceneTransform: () => void
}

export function useAtlasViewport(): AtlasViewport {
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const sceneRef = useRef<HTMLDivElement | null>(null)
  const panRef = useRef(pan)
  const zoomRef = useRef(zoom)
  const frameRef = useRef<number | null>(null)
  const viewportSyncTimerRef = useRef<number | null>(null)

  useEffect(() => {
    panRef.current = pan
  }, [pan])

  useEffect(() => {
    zoomRef.current = zoom
  }, [zoom])

  useEffect(
    () => () => {
      if (frameRef.current !== null) {
        cancelAnimationFrame(frameRef.current)
      }
      if (viewportSyncTimerRef.current !== null) {
        window.clearTimeout(viewportSyncTimerRef.current)
      }
    },
    []
  )

  const syncViewportState = useCallback(() => {
    setPan({ ...panRef.current })
    setZoom(zoomRef.current)
  }, [])

  const scheduleViewportStateSync = useCallback(() => {
    if (viewportSyncTimerRef.current !== null) {
      window.clearTimeout(viewportSyncTimerRef.current)
    }
    viewportSyncTimerRef.current = window.setTimeout(() => {
      viewportSyncTimerRef.current = null
      syncViewportState()
    }, 80)
  }, [syncViewportState])

  const applySceneTransform = useCallback(() => {
    if (!sceneRef.current) return
    const pixelRatio = window.devicePixelRatio || 1
    const alignedX = Math.round(panRef.current.x * pixelRatio) / pixelRatio
    const alignedY = Math.round(panRef.current.y * pixelRatio) / pixelRatio
    sceneRef.current.style.transform = `translate(${alignedX}px, ${alignedY}px) scale(${zoomRef.current})`
  }, [])

  const commitViewport = useCallback(() => {
    if (frameRef.current !== null) return
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null
      applySceneTransform()
    })
    scheduleViewportStateSync()
  }, [applySceneTransform, scheduleViewportStateSync])

  const updateZoom = useCallback(
    (nextZoom: number) => {
      zoomRef.current = Math.max(ATLAS_MIN_ZOOM, Math.min(ATLAS_MAX_ZOOM, Number(nextZoom.toFixed(2))))
      commitViewport()
    },
    [commitViewport]
  )

  const applyPanDelta = useCallback(
    (deltaX: number, deltaY: number) => {
      panRef.current = {
        x: panRef.current.x + deltaX,
        y: panRef.current.y + deltaY,
      }
      commitViewport()
    },
    [commitViewport]
  )

  const resetView = useCallback(() => {
    panRef.current = { x: 0, y: 0 }
    zoomRef.current = 1
    commitViewport()
  }, [commitViewport])

  const getZoom = useCallback(() => zoomRef.current, [])

  const focusOn = useCallback(
    (x: number, y: number, minZoom: number) => {
      panRef.current = { x: -x * 0.32, y: -y * 0.32 }
      zoomRef.current = Math.max(zoomRef.current, minZoom)
      commitViewport()
    },
    [commitViewport]
  )

  return {
    zoom,
    pan,
    sceneRef,
    getZoom,
    updateZoom,
    applyPanDelta,
    focusOn,
    resetView,
    syncViewportState,
    applySceneTransform,
  }
}
