import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ANNOUNCEMENT_TERMINAL_STATUSES,
  chatChannelApi,
  type AnnouncementSummary,
} from '../../../../services/api/chat-channel'

const ACTIVE_TASK_STORAGE_KEY = 'nekro-announcement-active-task-v1'
const DELIVERY_RENDER_LIMIT = 300

export type AnnouncementConnectionState = 'idle' | 'connecting' | 'live' | 'disconnected'

export interface AnnouncementDeliveryLine {
  chatKey: string
  channelName: string
  ok: boolean
  error: string
}

const readStoredId = (): number | null => {
  try {
    const raw = localStorage.getItem(ACTIVE_TASK_STORAGE_KEY)
    if (!raw) return null
    const value = Number.parseInt(raw, 10)
    if (Number.isInteger(value) && value > 0) return value
    localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY)
  } catch {
    // Storage may be unavailable in private browsing or hardened environments.
  }
  return null
}

const persistId = (announcementId: number | null) => {
  try {
    if (announcementId === null) {
      localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY)
    } else {
      localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, String(announcementId))
    }
  } catch {
    // Ignore storage failures: tracking still works for the current page session.
  }
}

/**
 * Track a server-side announcement task through its SSE progress stream.
 *
 * The task itself lives on the backend, so the browser only observes it: closing or
 * reloading the page never interrupts the broadcast, and the persisted task id lets the
 * page re-attach to a still running task after a reload.
 */
export const useAnnouncementJob = (onFinished?: () => void) => {
  const [activeId, setActiveId] = useState<number | null>(() => readStoredId())
  const [summary, setSummary] = useState<AnnouncementSummary | null>(null)
  const [deliveries, setDeliveries] = useState<AnnouncementDeliveryLine[]>([])
  const [connectionState, setConnectionState] = useState<AnnouncementConnectionState>('idle')
  const [isCancelling, setIsCancelling] = useState(false)
  const onFinishedRef = useRef(onFinished)

  useEffect(() => {
    onFinishedRef.current = onFinished
  }, [onFinished])

  const startTracking = useCallback((announcementId: number) => {
    setSummary(null)
    setDeliveries([])
    setConnectionState('connecting')
    persistId(announcementId)
    setActiveId(announcementId)
  }, [])

  const dismissFinished = useCallback(() => {
    setSummary(null)
    setDeliveries([])
    setConnectionState('idle')
  }, [])

  const cancelActive = useCallback(async () => {
    if (activeId === null || isCancelling) return
    setIsCancelling(true)
    try {
      await chatChannelApi.cancelAnnouncement(activeId)
    } finally {
      setIsCancelling(false)
    }
  }, [activeId, isCancelling])

  useEffect(() => {
    if (activeId === null) {
      setConnectionState('idle')
      return
    }

    let disposed = false

    const finishTracking = () => {
      persistId(null)
      setActiveId(null)
      setConnectionState('idle')
      onFinishedRef.current?.()
    }

    let cleanup: (() => void) | null = null
    try {
      cleanup = chatChannelApi.subscribeAnnouncementEvents(activeId, {
        onEvent: event => {
          if (disposed) return
          setConnectionState('live')

          if (event.type === 'snapshot') {
            setSummary(event.announcement)
            if (ANNOUNCEMENT_TERMINAL_STATUSES.has(event.announcement.status)) finishTracking()
            return
          }

          if (event.type === 'delivery') {
            setDeliveries(current => {
              const next = current.filter(line => line.chatKey !== event.chat_key)
              next.push({
                chatKey: event.chat_key,
                channelName: event.channel_name,
                ok: event.ok,
                error: event.error,
              })
              return next.slice(-DELIVERY_RENDER_LIMIT)
            })
            setSummary(current =>
              current
                ? {
                    ...current,
                    total: event.total,
                    success_count: event.success_count,
                    failure_count: event.failure_count,
                  }
                : current
            )
            return
          }

          setSummary(current =>
            current
              ? {
                  ...current,
                  status: event.status,
                  total: event.total,
                  success_count: event.success_count,
                  failure_count: event.failure_count,
                  failure_reason: event.failure_reason,
                }
              : current
          )
          if (ANNOUNCEMENT_TERMINAL_STATUSES.has(event.status)) finishTracking()
        },
        onError: () => {
          if (!disposed) setConnectionState('disconnected')
        },
      })
    } catch {
      setConnectionState('disconnected')
    }

    return () => {
      disposed = true
      cleanup?.()
    }
  }, [activeId])

  return {
    activeId,
    summary,
    deliveries,
    connectionState,
    isCancelling,
    startTracking,
    cancelActive,
    dismissFinished,
  }
}
