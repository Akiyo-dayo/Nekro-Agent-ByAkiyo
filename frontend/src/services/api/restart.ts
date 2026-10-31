import axios from './axios'

export interface RestartResponse {
  ok: boolean
}

/**
 * 当前部署形态支不支持从后台重启
 *
 * 「重启」要求进程退出后有人能把它拉回来：Docker daemon，或配了 `Restart=always` 的
 * systemd 服务。裸机 `uv run bot` 两者都不是，后端会明确拒绝而不是尝试后失败 ——
 * 所以前端必须在**点之前**就问清楚，把按钮禁掉并说明原因。
 */
export interface RestartCapability {
  supported: boolean
  /** docker | systemd | unsupported */
  mode: string
  /** 不支持的原因代码，用作前端 i18n 键 */
  reason_code: string
  /** 后端给出的中文说明，前端翻不到 reason_code 时回落到它 */
  detail: string
  /** systemd 部署下所属的服务名 */
  unit: string | null
}

export const restartApi = {
    /**
     * 查询重启能力（平台方专属，租户会 403）
     */
    getCapability: async (): Promise<RestartCapability> => {
        const response = await axios.get<RestartCapability>('/restart/capability')
        return response.data
    },

    /**
     * 重启系统
     */
    restartSystem: async (): Promise<RestartResponse> => {
        const response = await axios.post<RestartResponse>('/restart')
        return response.data
    },
}
