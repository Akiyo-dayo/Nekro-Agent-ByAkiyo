/**
 * 删除确认框里的「仍被配置引用」提醒
 *
 * 删除模型组不会改写引用它的配置：主模型组引用会让对话直接失败，备用、插件等引用会回落或报错。
 * 这里只**提醒**，不拦删除——是否先改配置由操作者决定。检查失败也照样允许删除，只是说明查不到。
 *
 * 引用范围以后端为准（`services/model_group_refs.py`）：租户只看得到自己实例下的引用，
 * 且后端只对当前身份可删除的组作答。所以对租户说的是「在你能管理的配置里没有发现引用」，
 * 不说「没有引用」——平台层的配置租户看不到，绝对化的说法会误导。
 */

import { useEffect } from 'react'
import { Alert, Box, CircularProgress, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useIdentity } from '../../hooks/useIdentity'
import { getLocalizedText } from '../../services/api/types'
import {
  TENANT_QUERY_KEYS,
  tenantApi,
  type ModelGroupReferenceInfo,
} from '../../services/api/tenant'

/** 每个组在提醒里最多列几条，其余折成「等另外 N 处」 */
const MAX_LISTED_PER_GROUP = 5

export interface ModelGroupReferenceNoticeProps {
  /** 要删除的组名；为空时不发请求 */
  names: string[]
  /** 检查进行中时回报给父组件，用于在拿到结果之前禁用确认按钮 */
  onCheckingChange?: (checking: boolean) => void
}

export default function ModelGroupReferenceNotice({
  names,
  onCheckingChange,
}: ModelGroupReferenceNoticeProps) {
  const { t, i18n } = useTranslation('settings')
  const { isPlatformOwner } = useIdentity()

  const referencesQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.modelGroupReferences(names),
    queryFn: () => tenantApi.getModelGroupReferences(names),
    enabled: names.length > 0,
    retry: false,
    // 每次打开确认框都重新查：配置可能刚被改过，提醒必须反映此刻的状态。
    // 但切回窗口时不重查：每次都是一遍全量扫盘，确认框开着时没必要反复做
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false,
  })

  const checking = names.length > 0 && referencesQuery.isFetching
  useEffect(() => {
    onCheckingChange?.(checking)
    return () => onCheckingChange?.(false)
  }, [checking, onCheckingChange])

  const describe = (reference: ModelGroupReferenceInfo) => {
    const where = t(`modelGroup.references.scopes.${reference.scope}`, {
      target: reference.target,
      instance: reference.instance_key,
    })
    const title = getLocalizedText(reference.i18n_title, reference.title, i18n.language)
    const field = title
      ? t('modelGroup.references.fieldWithTitle', { title, field: reference.field })
      : reference.field
    return t('modelGroup.references.entry', { where, field })
  }

  if (names.length === 0) return null

  if (referencesQuery.isFetching && !referencesQuery.data) {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1.5 }}>
        <CircularProgress size={14} />
        <Typography variant="caption" color="text.secondary">
          {t('modelGroup.references.checking')}
        </Typography>
      </Box>
    )
  }

  if (referencesQuery.isError) {
    return (
      <Alert severity="info" variant="outlined" sx={{ mt: 1.5 }}>
        {t('modelGroup.references.failed', {
          error:
            referencesQuery.error instanceof Error ? referencesQuery.error.message : String(referencesQuery.error),
        })}
      </Alert>
    )
  }

  const referenced = (referencesQuery.data ?? []).filter(item => item.total > 0)
  if (referenced.length === 0) {
    return (
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1.5 }}>
        {isPlatformOwner ? t('modelGroup.references.none') : t('modelGroup.references.noneInScope')}
      </Typography>
    )
  }

  return (
    <Alert severity="warning" sx={{ mt: 1.5 }}>
      <Typography variant="body2" fontWeight={700}>
        {t('modelGroup.references.title')}
      </Typography>
      <Box component="ul" sx={{ mt: 0.5, mb: 0.5, pl: 2.5, maxHeight: 200, overflow: 'auto' }}>
        {referenced.map(item => {
          const listed = item.references.slice(0, MAX_LISTED_PER_GROUP)
          const hidden = item.total - listed.length
          return (
            <Box component="li" key={item.name} sx={{ wordBreak: 'break-all' }}>
              <Typography variant="body2" fontWeight={600}>
                {item.name}
              </Typography>
              {listed.map(reference => (
                <Typography
                  key={`${reference.scope}:${reference.target}:${reference.instance_key}:${reference.field}`}
                  variant="caption"
                  sx={{ display: 'block' }}
                >
                  {describe(reference)}
                </Typography>
              ))}
              {hidden > 0 && (
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                  {t('modelGroup.references.more', { count: hidden })}
                </Typography>
              )}
            </Box>
          )
        })}
      </Box>
      <Typography variant="caption" sx={{ display: 'block' }}>
        {t('modelGroup.references.hint')}
      </Typography>
    </Alert>
  )
}
