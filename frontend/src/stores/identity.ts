/**
 * 当前身份状态
 *
 * 多租户下前端的每一个「显示 / 隐藏 / 只读」判断都来自后端返回的身份，
 * 而不是本地推断。因此这份状态刻意不做 localStorage 持久化：
 * 身份是权限判定的依据，必须每次会话向后端重新确认，避免被本地缓存篡改，
 * 也避免换账号登录后残留上一个身份。
 *
 * 后端尚未上线该接口时状态落到 `unavailable`，各页面按「平台方、单租户」处理，
 * 使用体验与多租户改造前完全一致。
 */

import { create } from 'zustand'
import {
  isTenantEndpointUnavailable,
  tenantApi,
  type CurrentIdentity,
  type TenantRole,
} from '../services/api/tenant'

export type IdentityStatus = 'idle' | 'loading' | 'ready' | 'unavailable' | 'error'

/** 后端未就绪 / 尚未加载时的兜底身份：平台方、不受实例限制 */
export const FALLBACK_ROLE: TenantRole = 'platform_owner'

interface IdentityState {
  identity: CurrentIdentity | null
  status: IdentityStatus
  error: string | null
  /** 拉取当前身份；并发调用共享同一次请求，`force` 用于登录后强制刷新 */
  loadIdentity: (force?: boolean) => Promise<void>
  /** 退出登录时清空，避免下一个账号看到上一个身份 */
  resetIdentity: () => void
}

let inflight: Promise<void> | null = null

export const useIdentityStore = create<IdentityState>()((set, get) => ({
  identity: null,
  status: 'idle',
  error: null,

  loadIdentity: (force = false) => {
    const { status } = get()
    if (!force && (status === 'loading' || status === 'ready' || status === 'unavailable')) {
      return inflight ?? Promise.resolve()
    }
    if (inflight && !force) {
      return inflight
    }

    set({ status: 'loading', error: null })
    inflight = tenantApi
      .getCurrentIdentity()
      .then(identity => {
        set({ identity, status: 'ready', error: null })
      })
      .catch((error: unknown) => {
        if (isTenantEndpointUnavailable(error)) {
          // 后端尚未上线身份接口：按单租户平台方处理，页面不出现任何多租户痕迹
          set({ identity: null, status: 'unavailable', error: null })
          return
        }
        set({
          identity: null,
          status: 'error',
          error: error instanceof Error ? error.message : String(error),
        })
      })
      .finally(() => {
        inflight = null
      })

    return inflight
  },

  resetIdentity: () => {
    inflight = null
    set({ identity: null, status: 'idle', error: null })
  },
}))
