import { Alert, AlertTitle, Stack, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { consoleApi, type EmbeddingHealthItem } from '../../services/api/console'
import { formatDateTime } from './format'

/** 一分钟看一次：状态在后端随每次嵌入调用更新，这里只是读 */
const REFRESH_MS = 60_000

const KNOWN_KINDS = new Set(['auth', 'quota', 'rejected', 'server', 'network'])

/**
 * 嵌入（向量化）长期失败的提示
 *
 * 有模型组处于长期失败（`failing`）时显示红条：失败期间新沉淀的记忆没有向量，召回与近重复检查
 * 用不上它们，平时只记 WARNING 的话一天多都没人发现。后端只回该身份能看的模型组（租户只看归自己的）；
 * 全平台缺向量的段数只给平台方，没在失败却还有段落缺向量时显示一条蓝条，提示可以补齐。
 * 读不到就不显示——这是一条提示，不该让记忆页报错。查询键挂在 `memory` 下，页头的刷新按钮会一起刷新。
 */
export default function EmbeddingHealthBanner() {
  const { t } = useTranslation('memory')
  const { data } = useQuery({
    queryKey: ['memory', 'embedding-health'],
    queryFn: () => consoleApi.getEmbeddingHealth(),
    refetchInterval: REFRESH_MS,
    retry: false,
  })

  const failing = (data?.items ?? []).filter(item => item.state === 'failing')
  // 只有平台方拿得到这个数；没在失败、却还有段落缺向量时（多半是之前失败期间沉淀的），提示可以补
  const missing = data?.missing_vectors ?? null
  if (failing.length === 0) {
    if (missing === null || missing <= 0) return null
    return (
      <Alert severity="info">
        <Typography variant="body2">{t('embeddingHealth.missingOnly', { count: missing })}</Typography>
      </Alert>
    )
  }

  const describe = (item: EmbeddingHealthItem): string => {
    const kind = KNOWN_KINDS.has(item.error_kind)
      ? t(`embeddingHealth.kinds.${item.error_kind}`)
      : item.error_kind
    return item.status_code !== null
      ? t('embeddingHealth.kindWithStatus', { kind, status: item.status_code })
      : kind
  }

  return (
    <Stack spacing={1}>
      {failing.map(item => (
        <Alert key={item.model_group} severity="error">
          <AlertTitle>{t('embeddingHealth.title', { group: item.model_group })}</AlertTitle>
          <Typography variant="body2">
            {t('embeddingHealth.detail', {
              kind: describe(item),
              since: formatDateTime(item.streak_started_at),
              count: item.consecutive_failures,
              lastFailure: formatDateTime(item.last_failure_at),
            })}
          </Typography>
          <Typography variant="body2">
            {item.last_success_at
              ? t('embeddingHealth.lastSuccess', { time: formatDateTime(item.last_success_at) })
              : t('embeddingHealth.noSuccess')}
          </Typography>
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            {t('embeddingHealth.impact')}
          </Typography>
          {missing !== null && (
            <Typography variant="body2">{t('embeddingHealth.missing', { count: missing })}</Typography>
          )}
        </Alert>
      ))}
    </Stack>
  )
}
