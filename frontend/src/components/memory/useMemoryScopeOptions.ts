import { useQuery } from '@tanstack/react-query'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { memoryApi, type MemoryScopeParams } from '../../services/api/memory'

/**
 * 记忆筛选栏的选项（频道、人设）
 *
 * 选项只来自 `GET /memory/scope-options`：那份响应本身已经按当前身份裁剪过，
 * 前端从别的列表里拼一份出来就多了一条会飘的授权判据。
 *
 * 筛选项与列表页共用同一个查询键：同一屏里既要用它渲染下拉，又要用它把 `chat_key`
 * 翻成频道名，两处各发一次请求是白花。
 */
export function useMemoryScopeOptions(scope: MemoryScopeParams) {
  const { isScopeReady } = useInstanceScope()
  return useQuery({
    queryKey: ['memory', 'scope-options', scope],
    queryFn: () => memoryApi.getScopeOptions(scope),
    enabled: isScopeReady,
    retry: false,
  })
}
