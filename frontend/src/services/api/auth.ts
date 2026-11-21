import type { SupportedLocale } from '../../config/i18n'

import axios from './axios'

export interface LoginParams {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  refresh_token: string
  token_type: string
}

export interface UserInfo {
  username: string
  userId: number
  perm_level: number
  perm_role: string
}

/**
 * 自助改密请求
 *
 * 只改**自己**的口令，所以没有 `user_id`（曾经有一个，但后端从来没读过它：写入对象恒为
 * 当前登录账号）。重置他人口令走 `user-manager.ts` 的 `resetPassword`。
 * `old_password` 是当前口令，后端必校验。
 */
export interface UpdatePasswordParams {
  old_password: string
  password: string
}

export interface ActionResponse {
  ok: boolean
}

/**
 * 自己的界面语言
 *
 * `locale` 为 `null` 表示这个账号没设置过偏好，由前端按本地存储 / 浏览器语言决定。
 * 后端只会在两种受支持的语言里返回，其余取值一律当作"没设置过"。
 */
export interface MyLocaleResponse {
  locale: SupportedLocale | null
}

export const authApi = {
  login: async (params: LoginParams) => {
    const response = await axios.post<LoginResponse>('/user/login', params)
    return response.data
  },

  getUserInfo: async () => {
    const response = await axios.get<UserInfo>('/user/me')
    return response.data
  },

  updatePassword: async (params: UpdatePasswordParams) => {
    const response = await axios.put<ActionResponse>('/user/password', params)
    return response.data
  },

  /**
   * 读取**自己**的界面语言
   *
   * 这条端点与全局 `SYSTEM_LANG` 是两件事：后者是"这个部署默认说什么话"（平台方配置），
   * 前者是"这个账号自己想看什么话"。租户账号改全局配置会 403，所以它的偏好只能走这里。
   */
  getMyLocale: async (): Promise<string | null> => {
    const response = await axios.get<MyLocaleResponse>('/user/me/locale')
    return response.data.locale
  },

  /**
   * 保存**自己**的界面语言
   *
   * 请求体里没有 `user_id`：后端写死的目标就是当前登录账号。
   */
  setMyLocale: async (locale: SupportedLocale): Promise<void> => {
    await axios.put<ActionResponse>('/user/me/locale', { locale })
  },
}
