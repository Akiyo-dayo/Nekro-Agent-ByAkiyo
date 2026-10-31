import axios from './axios'

export interface Wallpaper {
  id: string
  url: string
  filename: string
}

export interface ActionResponse {
  ok: boolean
}

/**
 * 聊天上传目录的取件令牌
 *
 * `/api/common/uploads/{chat_key}/{filename}` 要被 `<img src>` 直接加载，拿不到
 * Authorization 头，所以它改成了签名 URL：先用本接口换一个覆盖该频道上传目录的
 * 短时效令牌，再把它作为查询参数拼进图片地址。签发点带租户归属校验，越权 404。
 */
export interface UploadAccessToken {
  token: string
  /** 查询参数名，由服务端给出，前端不要写死 */
  query_param: string
  /** 过期时间（unix 秒） */
  expires_at: number
}

export const getUploadAccessToken = async (chatKey: string): Promise<UploadAccessToken> => {
  const response = await axios.get<UploadAccessToken>('/common/uploads-access-token', {
    params: { chat_key: chatKey },
  })
  return response.data
}

/**
 * 拼出带签名的聊天上传文件地址
 *
 * 拿不到令牌时返回空串而不是无签名地址：无签名地址一定 404，返回它只会让
 * `<img onError>` 把图片标成"加载失败"，不如让调用方按"还没准备好"处理。
 */
export const buildSignedUploadUrl = (
  chatKey: string,
  fileName: string,
  access: UploadAccessToken | undefined
): string => {
  if (!fileName || !access?.token) return ''
  const query = `${encodeURIComponent(access.query_param)}=${encodeURIComponent(access.token)}`
  return `/api/common/uploads/${encodeURIComponent(chatKey)}/${encodeURIComponent(fileName)}?${query}`
}

const accessibilityCache: Record<string, { valid: boolean; timestamp: number }> = {}
const CACHE_EXPIRY = 30 * 60 * 1000

export const uploadWallpaper = async (file: File): Promise<Wallpaper> => {
  const formData = new FormData()
  formData.append('file', file)

  const response = await axios.post<Wallpaper>('/common/wallpaper/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
    timeout: 60000,
  })
  return response.data
}

export const getWallpapers = async (): Promise<Wallpaper[]> => {
  const response = await axios.get<Wallpaper[]>('/common/wallpaper/list')
  return response.data
}

export const deleteWallpaper = async (wallpaperId: string): Promise<ActionResponse> => {
  const response = await axios.delete<ActionResponse>(`/common/wallpaper/${wallpaperId}`)
  return response.data
}

export const checkWallpaperAccessible = async (url: string): Promise<boolean> => {
  const cached = accessibilityCache[url]
  const now = Date.now()
  if (cached && now - cached.timestamp < CACHE_EXPIRY) {
    return cached.valid
  }

  try {
    return new Promise(resolve => {
      const img = new Image()

      img.onload = () => {
        accessibilityCache[url] = { valid: true, timestamp: now }
        resolve(true)
      }

      img.onerror = async () => {
        try {
          const response = await fetch(url, {
            method: 'HEAD',
            cache: 'force-cache',
          })
          const valid = response.ok
          accessibilityCache[url] = { valid, timestamp: now }
          resolve(valid)
        } catch (_error) {
          if (cached && cached.valid) {
            resolve(true)
          } else {
            accessibilityCache[url] = { valid: false, timestamp: now }
            resolve(false)
          }
        }
      }

      img.src = url
    })
  } catch (_error) {
    if (cached && cached.valid) {
      return true
    }
    accessibilityCache[url] = { valid: false, timestamp: now }
    return false
  }
}

export const commonApi = {
  uploadWallpaper,
  getWallpapers,
  deleteWallpaper,
  checkWallpaperAccessible,
  getUploadAccessToken,
}
