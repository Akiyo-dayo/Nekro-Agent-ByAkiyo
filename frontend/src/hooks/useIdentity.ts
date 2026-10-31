/**
 * 当前身份 Hook
 *
 * 页面统一通过它读取「我是谁、能看到什么、能改什么」。身份状态本身在
 * `stores/identity.ts`，这里只负责登录后触发拉取，并把角色翻译成页面直接可用的能力位。
 *
 * 两条硬性约定：
 * - 后端未就绪或尚未加载完成时一律按平台方处理，界面与多租户改造前完全一致；
 * - 模型组的可编辑性与凭据可见性不在这里推断，一律以后端返回的
 *   `editable` / `credential_visible` 为准。
 */

import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useAuthStore } from '../stores/auth'
import { FALLBACK_ROLE, useIdentityStore, type IdentityStatus } from '../stores/identity'
import {
  TENANT_QUERY_KEYS,
  tenantApi,
  type CurrentIdentity,
  type TenantRole,
} from '../services/api/tenant'

export interface Identity {
  /** 后端返回的原始身份；未就绪时为 null */
  identity: CurrentIdentity | null
  status: IdentityStatus
  /** 身份仍在确认中 */
  isLoading: boolean
  /** 后端身份接口尚未上线 */
  isUnavailable: boolean

  role: TenantRole
  /** 平台方：全局可见可管 */
  isPlatformOwner: boolean
  /** 租户管理员：只能管理自己名下的实例与资源 */
  isTenantAdmin: boolean
  /** 租户普通用户：只读 */
  isTenantUser: boolean
  /** 处于租户作用域（管理员或普通用户） */
  isTenantScoped: boolean
  /** 当前身份对业务数据只有读权限 */
  isReadOnly: boolean

  tenantId: number | null
  tenantName: string
  /** 可访问的实例；平台方为空数组表示不受限 */
  accessibleInstanceKeys: string[]

  /** 能否进入租户管理（创建租户、管理成员、划归实例） */
  canManageTenants: boolean
  /**
   * 能否做协议端容器的**宿主级**动作：创建 / 删除 / 起停 / 改网络配置
   *
   * 这些动作要么在宿主上分配资源，要么会回显托管连接的 OneBot 访问令牌，
   * 后端一律 `Role.Super`（见 `routers/console/containers.py` 的两档划分）。
   */
  canManageContainers: boolean
  /**
   * 能否触达**自己名下**实例的协议端运维面：重启 / 扫码 / 快速登录，以及健康流水
   *
   * R19 明写过租户最多能「重启我的 NapCat」。这一族后端统一是 `Role.Admin` + 归属校验
   * （`/console/instances/{key}/container*` 的自助运维档与 `/console/health/*`），
   * 所以只读的租户普通用户同样不该看到这些入口 —— 他点了会收到 403。
   * 界面隐藏只是避免点了才发现无权，真正的边界在服务端。
   */
  canOperateProtocol: boolean
  /** 能否访问平台级系统设置 */
  canAccessPlatformSettings: boolean
}

export function useIdentity(): Identity {
  const token = useAuthStore(state => state.token)
  const identity = useIdentityStore(state => state.identity)
  const status = useIdentityStore(state => state.status)
  const loadIdentity = useIdentityStore(state => state.loadIdentity)

  useEffect(() => {
    if (!token) {
      return
    }
    void loadIdentity()
  }, [token, loadIdentity])

  const role = identity?.role ?? FALLBACK_ROLE
  const isPlatformOwner = role === 'platform_owner'
  const isTenantAdmin = role === 'tenant_admin'
  const isTenantUser = role === 'tenant_user'
  const isTenantScoped = isTenantAdmin || isTenantUser

  return {
    identity,
    status,
    isLoading: status === 'loading' || status === 'idle',
    isUnavailable: status === 'unavailable',

    role,
    isPlatformOwner,
    isTenantAdmin,
    isTenantUser,
    isTenantScoped,
    isReadOnly: isTenantUser,

    tenantId: identity?.tenant_id ?? null,
    tenantName: identity?.tenant_name ?? '',
    accessibleInstanceKeys: identity?.accessible_instance_keys ?? [],

    canManageTenants: isPlatformOwner,
    canManageContainers: isPlatformOwner,
    canOperateProtocol: isPlatformOwner || isTenantAdmin,
    canAccessPlatformSettings: isPlatformOwner,
  }
}

export interface TenancyVisibility extends Identity {
  /**
   * 是否需要向用户暴露「租户」这一层概念
   *
   * 平台方名下一个租户都没有时，租户管理入口与顶栏身份标识都不出现，
   * 使用体验与改造前完全一致；租户身份下则始终需要标明自己在哪个作用域。
   */
  isTenancyVisible: boolean
  /** 平台方名下已有租户 */
  hasTenants: boolean
  /** 租户列表仍在确认中 */
  isTenancyLoading: boolean
}

/**
 * 顶栏与菜单据此决定是否显示多租户相关入口
 *
 * 单租户（含零租户）部署下整层概念对用户不可见，这是改造的硬性要求。
 */
export function useTenancyVisibility(): TenancyVisibility {
  const identity = useIdentity()

  const tenantsQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.tenants,
    queryFn: () => tenantApi.listTenants(),
    // 仅平台方有权读取租户列表；租户身份下不发这个请求，避免必然的 403。
    // 身份未确认前一律不发：此时 `role` 只是 `FALLBACK_ROLE`（按平台方处理），
    // 租户在每次冷启动时都会先打出一个注定 403 的请求。
    enabled: identity.canManageTenants && !identity.isLoading,
    staleTime: 60_000,
    retry: false,
    // 后端未就绪时这个请求必然 404，不要在每次窗口聚焦时重复发起
    refetchOnWindowFocus: false,
  })

  const hasTenants = (tenantsQuery.data?.items.length ?? 0) > 0

  return {
    ...identity,
    hasTenants,
    isTenancyLoading: identity.canManageTenants && tenantsQuery.isLoading,
    isTenancyVisible: identity.isTenantScoped || hasTenants,
  }
}
