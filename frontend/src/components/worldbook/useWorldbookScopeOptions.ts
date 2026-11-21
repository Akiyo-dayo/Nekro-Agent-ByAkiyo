import { useQuery } from '@tanstack/react-query'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { worldbookApi, type WorldbookScopeParams } from '../../services/api/worldbook'

/**
 * 世界书筛选栏的选项（频道、人设）
 *
 * 选项只来自 `GET /worldbook/scope-options`：那份响应本身已经按当前身份裁剪过，
 * 前端从别的列表里拼一份出来就多了一条会飘的授权判据。
 */
export function useWorldbookScopeOptions(scope: WorldbookScopeParams) {
  const { isScopeReady } = useInstanceScope()
  return useQuery({
    queryKey: ['worldbook', 'scope-options', scope],
    queryFn: () => worldbookApi.getScopeOptions(scope),
    enabled: isScopeReady,
    retry: false,
  })
}
