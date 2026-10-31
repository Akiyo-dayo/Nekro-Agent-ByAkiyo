import { useQuery } from '@tanstack/react-query'
import { getUploadAccessToken, type UploadAccessToken } from '../services/api/common'

/**
 * 服务端时间窗是 900s，令牌在窗口内逐字节不变、有效期落在 [900, 1800) 秒。
 * 每 5 分钟重取一次：既保证手里的令牌至少还有一个完整窗口可用，
 * 又因为窗口内令牌不变而不会让图片 URL 频繁变化（URL 一变浏览器缓存就作废）。
 */
const REFRESH_INTERVAL_MS = 5 * 60 * 1000

/**
 * 取某个频道上传目录的取件令牌
 *
 * 用于把 `/api/common/uploads/...` 拼成签名 URL，见 `buildSignedUploadUrl`。
 * 令牌本身就是访问凭据，所以不做跨频道复用：一个频道一份，作用域与后端的
 * `assert_channel_accessible` 对齐。
 */
export function useUploadAccessToken(chatKey: string): UploadAccessToken | undefined {
  const { data } = useQuery({
    queryKey: ['upload-access-token', chatKey],
    queryFn: () => getUploadAccessToken(chatKey),
    enabled: !!chatKey,
    staleTime: REFRESH_INTERVAL_MS,
    refetchInterval: REFRESH_INTERVAL_MS,
    refetchOnWindowFocus: true,
    // 越权/频道不存在会 404，重试没有意义
    retry: false,
  })
  return data
}
