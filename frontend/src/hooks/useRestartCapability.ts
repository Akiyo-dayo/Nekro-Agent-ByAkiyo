/**
 * 「重启整个 NA 进程」在当前身份 / 当前部署形态下做不做得到
 *
 * **为什么是一个共用 hook 而不是写在页面里。** 请求重启的地方有两个：仪表盘顶部的
 * 「重启系统」按钮，以及配置页保存了 `is_need_restart` 的配置项之后**自动弹出**的重启确认框
 * （`components/common/ConfigTable.tsx`，被适配器配置 / 适配器覆盖配置 / 频道覆盖配置 /
 * 插件配置 / 系统设置五个页面共用）。判据只写在仪表盘里的话，配置页照样会把一个必然失败的
 * 按钮递到用户面前——那正是这次要修的症状本身，只是换了个入口。
 *
 * 三件事合在一处：
 *
 * 1. **谁能看到入口。** 后端 `POST /api/restart` 是 `Role.Super`（一个进程承载全部租户的
 *    实例，重启是宿主级动作），租户点了只会拿到 403。所以租户身份下不渲染入口、也不发探测
 *    请求。身份未确认时按平台方处理（`useIdentity` 的既定约定），**未启用多租户的部署
 *    因此与改造前逐字相同**。
 * 2. **这种部署形态做不做得到。** 裸机 `uv run bot` 起来的进程没有任何监管者，退出之后没人
 *    把它拉回来，后端会明确拒绝（`DeploymentCapabilityError`，403）。必须在**点之前**问清楚。
 * 3. **报错怎么念。** 后端把「不支持」与「失败」分开了，前端按 `reason_code` 取本地化文案；
 *    不能再套一句「请检查网络连接」——那句话正是把「部署形态不支持」误导成网络问题的来源。
 *
 * 探测失败（例如后端还没有这个端点）时**不禁用**任何东西：退回改造前的行为，由后端在真正
 * 调用时给结论，而不是凭一次失败的探测把功能锁死。
 */

import { useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useIdentity } from './useIdentity'
import { restartApi } from '../services/api/restart'
import { ApiError } from '../services/api/axios'

/** 后端 `DeploymentCapabilityError` 在 `data` 里带回的机器可读结论（与 capability 响应同形） */
interface DeploymentCapabilityData {
  reason_code?: string
  mode?: string
  unit?: string | null
}

const CAPABILITY_ERROR_TYPE = 'DeploymentCapabilityError'

function readCapabilityData(error: ApiError): DeploymentCapabilityData | null {
  if (error.type !== CAPABILITY_ERROR_TYPE) {
    return null
  }
  if (!error.data || typeof error.data !== 'object') {
    return null
  }
  return error.data as DeploymentCapabilityData
}

export interface RestartCapabilityState {
  /** 当前身份能否请求重启（后端 `Role.Super`）；false 时入口不该渲染 */
  canRestart: boolean
  /** 探测仍在进行中 */
  checking: boolean
  /** 探测已明确给出「这种部署形态做不到」 */
  unsupported: boolean
  /** 不支持的原因（已本地化）。支持、或探测未给出结论时为空串 */
  unsupportedReason: string
  /** 把重启接口抛出的错误翻成一句可读的本地化文案 */
  describeRestartError: (error: unknown) => string
}

export interface UseRestartCapabilityOptions {
  /**
   * 是否需要探测。默认 true。
   *
   * 常驻可见的入口（仪表盘按钮）要一开始就知道；只在弹窗里才用得上的入口
   * （配置页的重启确认框）传弹窗的开合，免得每次打开配置页都多打一个请求 ——
   * 后端每次探测要起一个 `systemctl` 子进程。
   */
  enabled?: boolean
}

export function useRestartCapability(
  options: UseRestartCapabilityOptions = {}
): RestartCapabilityState {
  const { enabled = true } = options
  const { t } = useTranslation('common')
  const { isPlatformOwner, isLoading: isIdentityLoading } = useIdentity()

  const { data: capability, isLoading } = useQuery({
    queryKey: ['restart-capability'],
    queryFn: () => restartApi.getCapability(),
    enabled: enabled && isPlatformOwner && !isIdentityLoading,
    staleTime: 60_000,
    retry: false,
    refetchOnWindowFocus: false,
  })

  /**
   * 后端的 `detail` 只有中文（它是「前端翻不到时的兜底」），所以优先按 `reason_code` 取
   * 本地化文案。五个 reason_code 在 `common.json` 里都有对应条目，缺一个就会静默回落成中文。
   */
  const describeReason = useCallback(
    (reasonCode: string, unit?: string | null, fallback?: string): string => {
      const text = t(`restartCapability.unsupported.${reasonCode}`, {
        defaultValue: fallback || t('restartCapability.unsupported.unknown'),
      })
      return unit ? `${text}${t('restartCapability.unitSuffix', { unit })}` : text
    },
    [t]
  )

  const unsupported = capability ? !capability.supported : false
  const unsupportedReason =
    capability && !capability.supported
      ? describeReason(capability.reason_code, capability.unit, capability.detail)
      : ''

  const describeRestartError = useCallback(
    (error: unknown): string => {
      if (error instanceof ApiError) {
        const data = readCapabilityData(error)
        if (data?.reason_code) {
          return describeReason(data.reason_code, data.unit, error.message)
        }
        // 其余的 `ApiError` 已经是后端按 `Accept-Language` 本地化过的结论，原样透出即可
        return error.message
      }
      return error instanceof Error ? error.message : String(error)
    },
    [describeReason]
  )

  return {
    canRestart: isPlatformOwner,
    checking: isLoading,
    unsupported,
    unsupportedReason,
    describeRestartError,
  }
}
