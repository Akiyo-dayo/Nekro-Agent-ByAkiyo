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
}
