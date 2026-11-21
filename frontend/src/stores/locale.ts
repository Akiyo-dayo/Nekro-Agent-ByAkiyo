import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import i18next from '../config/i18n'
import type { SupportedLocale } from '../config/i18n'
import { authApi } from '../services/api/auth'
import { unifiedConfigApi } from '../services/api/unified-config'
import { useAuthStore } from './auth'
import { useIdentityStore } from './identity'

interface LocaleState {
  currentLocale: SupportedLocale
  /** 切换语言（同时同步到后端） */
  setLocale: (locale: SupportedLocale) => void
  /** 仅更新前端语言（不写后端，用于从后端同步到前端） */
  setLocaleLocal: (locale: SupportedLocale) => void
  /** 从后端同步到前端（平台方读全局配置，租户读自己的偏好） */
  syncFromBackend: () => Promise<void>
}

/** 谁能写哪个字段 */
type LocaleBackend = 'platform' | 'tenant' | 'unknown'

/**
 * 判断当前账号的界面语言该写到哪里
 *
 * 平台方的语言是**全局配置**（`SYSTEM_LANG`，`POST /config/batch/system`），租户账号改这个
 * 会 403 —— `system` 是跨租户共享的作用域。租户账号的语言存在自己那一行的
 * `ext_data.ui_locale`，走 `/user/me/locale`。
 *
 * 三条分支里最要紧的是 `unknown`：身份还没确认时**两边都不写**。猜成平台方会在租户
 * 账号上打出一个必然 403 的请求；猜成租户则会替平台方把全局默认语言改掉。
 * 还没登录（登录页、首次引导页上也有语言按钮）时同样是 `unknown`：没有令牌就不去拉身份，
 * 也不发任何写请求，语言只在本地切换。
 *
 * `unavailable` 指后端还没有身份接口，按"平台方、单租户"处理（与 `FALLBACK_ROLE` 同一口径）。
 *
 * 注意这里用 `useIdentityStore.getState()` 而**不是** `useIdentity()`：这个函数在事件
 * 回调里跑，不在 React 渲染期，拿不到 hook 的返回值。
 */
async function resolveLocaleBackend(): Promise<LocaleBackend> {
  if (!useAuthStore.getState().token) {
    return 'unknown'
  }
  const identityStore = useIdentityStore.getState()
  // 复用 identity store 的在途请求去重；已经 ready 时它就是一次同步返回
  await identityStore.loadIdentity()
  const { identity, status } = useIdentityStore.getState()

  if (status === 'unavailable') {
    return 'platform'
  }
  if (status !== 'ready' || !identity) {
    return 'unknown'
  }
  return identity.role === 'tenant_admin' || identity.role === 'tenant_user' ? 'tenant' : 'platform'
}

export const useLocaleStore = create<LocaleState>()(
  persist(
    (set, get) => ({
      currentLocale: 'zh-CN',
      setLocale: (locale: SupportedLocale) => {
        // 本地立即生效：语言是纯展示层的偏好，等后端返回再切会让点击像没响应
        i18next.changeLanguage(locale)
        set({ currentLocale: locale })

        void (async () => {
          const backend = await resolveLocaleBackend()
          if (backend === 'tenant') {
            // 租户账号写自己的偏好；写全局配置会 403（见 resolveLocaleBackend 的说明）
            await authApi.setMyLocale(locale)
            return
          }
          if (backend === 'platform') {
            // 平台方沿用原路径：全局 `SYSTEM_LANG`（batchUpdateConfig 内部同时更新并保存到文件）
            await unifiedConfigApi.batchUpdateConfig('system', { SYSTEM_LANG: locale })
          }
          // 'unknown'：身份未确认，什么都不写。本地切换已经生效，下次登录会重新同步
        })().catch((err) => {
          console.error('[locale] Failed to persist locale to backend:', err)
        })
      },
      setLocaleLocal: (locale: SupportedLocale) => {
        i18next.changeLanguage(locale)
        set({ currentLocale: locale })
      },
      syncFromBackend: async () => {
        try {
          const backend = await resolveLocaleBackend()
          if (backend === 'tenant') {
            const backendLang = await authApi.getMyLocale()
            // 只认两种受支持的语言：后端已经过滤过一遍，这里再挡一次，避免把认不出的值
            // 塞给 i18next 之后界面回退成中文而设置页仍显示"已选英文"
            if (backendLang === 'zh-CN' || backendLang === 'en-US') {
              applyLocaleFromBackend(backendLang, set, get)
            }
            return
          }
          if (backend === 'platform') {
            const item = await unifiedConfigApi.getConfigItem('system', 'SYSTEM_LANG')
            const backendLang = item.value as string
            if (backendLang && (backendLang === 'zh-CN' || backendLang === 'en-US')) {
              applyLocaleFromBackend(backendLang as SupportedLocale, set, get)
            }
          }
          // 'unknown'：身份还没确认，这一次不同步。本地存储的值继续生效
        } catch {
          // 获取失败使用本地存储的值
        }
      },
    }),
    {
      name: 'nekro-locale',
      partialize: state => ({
        currentLocale: state.currentLocale,
      }),
    }
  )
)

/** 只在真的不同时才切换，避免每次进首页都触发一次无谓的 i18next 重渲染 */
function applyLocaleFromBackend(
  backendLocale: SupportedLocale,
  set: (partial: Partial<LocaleState>) => void,
  get: () => LocaleState
): void {
  if (get().currentLocale === backendLocale) return
  i18next.changeLanguage(backendLocale)
  set({ currentLocale: backendLocale })
}
