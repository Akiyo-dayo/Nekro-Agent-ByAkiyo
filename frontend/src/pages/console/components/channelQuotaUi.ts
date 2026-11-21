/**
 * 频道额度页与实例详情页共用的展示规则
 *
 * 放在 `.ts` 里而不是组件文件里：组件文件只导出组件，否则 `react-refresh/only-export-components`
 * 会对「同一个文件里既有组件又有工具函数」报警（本项目 eslint 是 `--max-warnings 0`）。
 *
 * 六种状态的配色与文案是**一个判据的第六份副本**之外唯一该存在的一份：状态字符串由后端
 * 状态机给出，前端只负责把它翻译成颜色与标签，任何一处自己推断状态都会与后端分叉。
 */

import { Theme } from '@mui/material'
import type { ChannelQuotaAction, ChannelQuotaState, ChannelQuotaUnit } from '../../../services/api/consoleChannelQuota'

/** 六种状态各一个颜色。`paused` 与 `exceeded` 都是红系，但前者更重（真的停回复了） */
export function channelQuotaStateColor(theme: Theme, state: ChannelQuotaState): string {
  switch (state) {
    case 'disabled':
      return theme.palette.text.secondary
    case 'unlimited':
      return theme.palette.info.main
    case 'normal':
      return theme.palette.success.main
    case 'warning':
      return theme.palette.warning.main
    case 'exceeded':
      return theme.palette.error.main
    case 'paused':
      return theme.palette.secondary.main
    default:
      return theme.palette.text.secondary
  }
}

/** 进度条颜色与状态标签同一套，避免「标签说暂停、进度条还是绿的」 */
export function channelQuotaProgressColor(state: ChannelQuotaState): 'success' | 'warning' | 'error' | 'info' | 'inherit' {
  switch (state) {
    case 'normal':
      return 'success'
    case 'warning':
      return 'warning'
    case 'exceeded':
    case 'paused':
      return 'error'
    case 'unlimited':
      return 'info'
    default:
      return 'inherit'
  }
}

/** 单位的人读文案；未知取值原样显示，不编一个默认单位 */
export const channelQuotaUnitLabel = (
  t: (key: string, options?: Record<string, unknown>) => string,
  unit: ChannelQuotaUnit | string
): string => t(`unit.${unit}`, { defaultValue: unit })

export const channelQuotaActionLabel = (
  t: (key: string, options?: Record<string, unknown>) => string,
  action: ChannelQuotaAction | string
): string => t(`action.${action}`, { defaultValue: action })

/** 大数字加千分位；token 动辄七位，不分节读不出量级 */
export const formatQuotaInt = (value: number): string => value.toLocaleString()

/** 额度为 0 时是「不限」，不是「零额度」——后者会让用户以为今天一条都不能回 */
export const formatQuotaLimit = (
  t: (key: string) => string,
  limit: number
): string => (limit > 0 ? formatQuotaInt(limit) : t('value.unlimited'))
