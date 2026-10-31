import axios, { AxiosError } from 'axios'
import i18n from '../../config/i18n'
import { useAuthStore } from '../../stores/auth'
import { config } from '../../config/env'
import {
  isApiErrorResponse,
  isProtocolApiErrorResponse,
  type ApiErrorResponse,
  type ProtocolApiErrorResponse,
} from './types'
import { getCurrentAppPath, loginPath, sanitizeRedirectTarget } from '../../router/routes'

/**
 * 自定义 API 错误类
 *
 * 封装后端返回的错误信息，提供统一的错误处理接口
 */
export class ApiError extends Error {
  /** 错误类型（后端错误类名） */
  public readonly type: string
  /**
   * HTTP 状态码；网络错误 / 请求根本没发出去时为 null
   *
   * **`type` 不足以判断"是不是被拒绝了"。** 后端的拒绝有两种形态：
   * `AppError` 一族（`PermissionDeniedError` 等，`type` 就是类名），以及 `require_role`
   * 抛的原生 `HTTPException` —— 后者经 `http_exception_handler` 归一化之后 `type` 恒为
   * `"HTTPException"`，403 与 400、409、502 在这个字段上**完全一样**。全部 console 端点
   * 的守卫都是 `require_role`，所以不带状态码就没有任何办法把「你无权查看」与
   * 「服务器出错了」分开说。
   */
  public readonly status: number | null
  /** 技术细节（可选） */
  public readonly detail?: string | null
  /** 附加数据（可选） */
  public readonly data?: unknown

  constructor(
    type: string,
    message: string,
    detail?: string | null,
    data?: unknown,
    status: number | null = null
  ) {
    super(message)
    this.name = 'ApiError'
    this.type = type
    this.detail = detail
    this.data = data
    this.status = status
  }

  /**
   * 检查是否为特定类型的错误
   */
  isType(errorType: string): boolean {
    return this.type === errorType
  }
}

/**
 * 这次失败是不是「不让你看」（HTTP 403）
 *
 * 与「没有数据」严格分开：页面把 403 渲染成 `0` / 空列表是本项目实测过的真实缺陷
 * （只读租户成员打开总后台首页看到「实例总数 0」，而他名下明明有实例）。
 * 「零」是一个有效答案，用户会当真；「你无权查看」才是事实。
 *
 * 401 不在此列：它由 axios 拦截器直接踢去登录页，页面拿不到。
 */
export function isForbiddenError(error: unknown): boolean {
  return httpStatusOf(error) === 403
}

/**
 * 这次失败是不是 404
 *
 * **404 在本项目里是有意的越权语义**（PROJECT_STATE §1.4：越权一律 404 而非 403，
 * 403 会泄露"该资源存在"）。所以页面拿到 404 时**必须**继续说「不存在」，
 * 不能改口说「你无权查看」—— 那等于把后端刻意藏起来的那条信息又讲了出来。
 * 这个判据存在的唯一理由就是把它从「其它失败」里摘出来，别被通用错误视图接管。
 */
export function isNotFoundError(error: unknown): boolean {
  return httpStatusOf(error) === 404
}

function httpStatusOf(error: unknown): number | null {
  if (error instanceof ApiError) {
    return error.status
  }
  // 没经过本模块拦截器的原始 axios 错误（例如 EventSource / fetch 旁路）也认
  if (typeof error === 'object' && error !== null && 'response' in error) {
    return (error as { response?: { status?: number } }).response?.status ?? null
  }
  return null
}

// 创建 axios 实例
const axiosInstance = axios.create({
  baseURL: config.apiBaseUrl,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  },
})

// 请求拦截器
axiosInstance.interceptors.request.use(
  requestConfig => {
    // 添加认证 Token
    const token = useAuthStore.getState().token
    if (token) {
      requestConfig.headers.Authorization = `Bearer ${token}`
    }

    // 添加语言偏好头，后端根据此头返回对应语言的错误消息
    requestConfig.headers['Accept-Language'] = i18n.language

    return requestConfig
  },
  error => {
    return Promise.reject(error)
  }
)

// 响应拦截器
axiosInstance.interceptors.response.use(
  // 成功响应：直接返回
  response => response,

  // 错误响应：统一处理
  async (error: AxiosError<ApiErrorResponse | ProtocolApiErrorResponse>) => {
    // 网络错误
    if (!error.response) {
      if (error.message === 'Network Error') {
        throw new ApiError(
          'NetworkError',
          i18n.t('networkError', { ns: 'errors', defaultValue: '网络连接失败，请检查网络设置' })
        )
      }
      throw new ApiError('UnknownError', error.message)
    }

    const { status, data } = error.response

    // 401 未授权
    if (status === 401) {
      const authError = isApiErrorResponse(data) ? data : null
      // 非登录接口的 401，执行登出
      if (!error.config?.url?.includes('/user/login')) {
        const sessionMessage =
          authError?.message ||
          i18n.t('sessionExpired', { ns: 'errors', defaultValue: '登录已过期，请重新登录' })
        try {
          sessionStorage.setItem('auth_error', sessionMessage)
        } catch {
          // ignore storage errors
        }
        useAuthStore.getState().logout()
        const redirectTarget = sanitizeRedirectTarget(getCurrentAppPath())
        window.location.replace(`/#${loginPath(redirectTarget)}`)
      }
      // 使用后端返回的本地化消息，或使用前端翻译
      throw new ApiError(
        authError?.error || 'UnauthorizedError',
        authError?.message || i18n.t('unauthorized', { ns: 'errors', defaultValue: '未授权访问' }),
        authError?.detail,
        undefined,
        status
      )
    }

    // 后端返回了标准错误格式
    if (isApiErrorResponse(data)) {
      throw new ApiError(data.error, data.message, data.detail, data.data, status)
    }

    if (isProtocolApiErrorResponse(data)) {
      throw new ApiError(data.error.code, data.error.message, null, data.error.details, status)
    }

    // 兜底处理
    throw new ApiError(
      'UnknownError',
      i18n.t('serverError', { ns: 'errors', defaultValue: '服务器内部错误' }),
      error.message,
      undefined,
      status
    )
  }
)

export default axiosInstance
