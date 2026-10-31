import { lazy, type ComponentType, Suspense } from 'react'
import { createHashRouter, Navigate } from 'react-router-dom'
import { CircularProgress, Box } from '@mui/material'
import MainLayout from '../layouts/MainLayout'
import AdapterLayout from '../layouts/AdapterLayout'
import LoginPage from '../pages/login'
import OobePage from '../pages/oobe'
import RequireAuth from './RequireAuth'

// 创建一个包装器组件来处理懒加载和加载状态
const lazyLoad = (importFn: () => Promise<{ default: ComponentType }>) => {
  const LazyComponent = lazy(importFn)
  return (
    <Suspense 
      fallback={
        <Box 
          sx={{ 
            display: 'flex', 
            justifyContent: 'center', 
            alignItems: 'center', 
            height: '100%',
            minHeight: 200 
          }}
        >
          <CircularProgress />
        </Box>
      }
    >
      <LazyComponent />
    </Suspense>
  )
}

const router = createHashRouter([
  {
    path: '/login',
    element: <LoginPage />,
  },
  {
    path: '/oobe',
    element: (
      <RequireAuth>
        <OobePage />
      </RequireAuth>
    ),
  },
  {
    path: '/',
    element: <MainLayout />,
    errorElement: <Navigate to="/login" />,
    children: [
      {
        index: true,
        element: <Navigate to="/dashboard" />,
      },
      {
        path: 'dashboard',
        element: lazyLoad(() => import('../pages/dashboard')),
      },
      {
        path: 'console',
        children: [
          {
            index: true,
            element: lazyLoad(() => import('../pages/console')),
          },
          {
            path: 'instances',
            children: [
              {
                index: true,
                element: lazyLoad(() => import('../pages/console/InstanceListPage')),
              },
              {
                // 实例向导由实例向导路实现
                path: 'new',
                element: lazyLoad(() => import('../pages/console/wizard/InstanceWizardPage')),
              },
              {
                path: ':instanceKey',
                children: [
                  {
                    index: true,
                    element: <Navigate to="overview" replace />,
                  },
                  {
                    path: ':tab',
                    element: lazyLoad(() => import('../pages/console/InstanceDetailPage')),
                  },
                ],
              },
            ],
          },
          {
            // 租户管理仅平台方可用，租户身份的兜底拦截在 MainLayout 统一处理
            path: 'tenants',
            children: [
              {
                index: true,
                element: lazyLoad(() => import('../pages/console/TenantListPage')),
              },
              {
                path: ':tenantId',
                children: [
                  {
                    index: true,
                    element: lazyLoad(() => import('../pages/console/TenantDetailPage')),
                  },
                  {
                    // 租户详情已合并为单页，旧的 /overview /members /instances 链接
                    // 仍落到同一个页面，由页面把 URL 收敛回干净形态
                    path: ':tab',
                    element: lazyLoad(() => import('../pages/console/TenantDetailPage')),
                  },
                ],
              },
            ],
          },
          {
            path: 'model-groups',
            element: lazyLoad(() => import('../pages/console/ModelGroupOwnershipPage')),
          },
          {
            // 租户自助的「我的团队」：**不是** `/console/tenants` 的别名。
            // 那条路径整体是平台方专属（租户列表 + 客户资料编辑 + 停用 + 实例划归），
            // 本页只渲染登录者自己所属的那一个租户，且只暴露后端已经按
            // `require_tenant_admin` 开给租户的成员增删。平台方走这条会看到
            // 「这是租户自助页」的提示并被引到租户列表去。
            path: 'team',
            element: lazyLoad(() => import('../pages/console/TenantTeamPage')),
          },
          {
            // 租户也要能管自己实例的配额，这里不做平台方限定；可见范围由后端按租户作用域收敛
            path: 'quota',
            element: lazyLoad(() => import('../pages/console/InstanceQuotaPage')),
          },
          {
            // 用量与费用（token 账本 + 折算费用）。**不做平台方限定**：定制版客户要看得到
            // 自己烧了多少，可见范围由后端按租户作用域收敛（越权 404）。形态对照 `/console/quota`。
            path: 'usage',
            element: lazyLoad(() => import('../pages/console/UsagePage')),
          },
          {
            // 身份与域（记忆域配置 + 账号合并 + 人设孤儿数据）。
            // **不做平台方限定**：定制版客户要能自助配自己的域、合并自己客户的账号，
            // 可见范围由后端按租户作用域收敛（越权 404）。形态对照 `/console/quota`。
            path: 'identity',
            element: lazyLoad(() => import('../pages/console/IdentityDomainPage')),
          },
          {
            // 宿主级的 iptables 隔离规则运维，平台方专属；租户身份的兜底拦截在 MainLayout 统一处理
            path: 'network-rules',
            element: lazyLoad(() => import('../pages/console/NetworkRuleOrphansPage')),
          },
          {
            // 运维 Bot 写操作的确认落地页，链接由 Bot 在聊天频道里签发
            path: 'ops-confirm',
            element: lazyLoad(() => import('../pages/console/OpsActionConfirmPage')),
          },
        ],
      },
      {
        path: 'chat-channel',
        children: [
          {
            index: true,
            element: <Navigate to="management" replace />,
          },
          {
            path: 'management',
            element: lazyLoad(() => import('../pages/chat-channel')),
          },
          {
            path: 'management/:chatKey',
            element: lazyLoad(() => import('../pages/chat-channel')),
          },
          {
            path: 'announcement',
            element: lazyLoad(() => import('../pages/chat-channel/announcement')),
          },
        ],
      },
      {
        path: 'chat-channel/:chatKey',
        element: lazyLoad(() => import('../pages/chat-channel')),
      },
      {
        path: 'chat-channel/:chatKey/:tab',
        element: lazyLoad(() => import('../pages/chat-channel')),
      },
      {
        path: 'affection',
        element: lazyLoad(() => import('../pages/affection')),
      },
      {
        path: 'user-manager',
        element: lazyLoad(() => import('../pages/user-manager')),
      },
      {
        path: 'presets',
        element: lazyLoad(() => import('../pages/presets')),
      },
      {
        path: 'logs',
        element: lazyLoad(() => import('../pages/logs')),
      },
      {
        path: 'plugins',
        children: [
          {
            index: true,
            element: <Navigate to="management" />,
          },
          {
            path: 'management',
            element: lazyLoad(() => import('../pages/plugins/management')),
          },
          {
            path: 'management/:pluginId',
            element: lazyLoad(() => import('../pages/plugins/management')),
          },
          {
            path: 'editor',
            element: lazyLoad(() => import('../pages/plugins/editor')),
          },
        ],
      },
      {
        path: 'sandbox-logs',
        element: lazyLoad(() => import('../pages/sandbox')),
      },
      {
        path: 'adapters',
        children: [
          {
            index: true,
            element: lazyLoad(() => import('../pages/adapter')),
          },
          {
            path: ':adapterKey',
            element: <AdapterLayout />,
            children: [
              {
                index: true,
                element: lazyLoad(() => import('../pages/adapter/AdapterTabPage')),
              },
              {
                path: '*',
                element: lazyLoad(() => import('../pages/adapter/AdapterTabPage')),
              },
            ],
          },
        ],
      },
      {
        path: 'settings',
        element: lazyLoad(() => import('../pages/settings')),
        children: [
          {
            index: true,
            element: <Navigate to="system" />,
          },
          {
            path: 'models',
            element: lazyLoad(() => import('../pages/settings/models')),
          },
          {
            path: 'system',
            element: lazyLoad(() => import('../pages/settings/system')),
          },
          {
            path: 'model-groups',
            element: <Navigate to="/settings/models?tab=basic" replace />,
          },
          {
            path: 'theme',
            element: lazyLoad(() => import('../pages/settings/theme')),
          },
          {
            path: 'space-cleanup',
            element: lazyLoad(() => import('../pages/settings/space-cleanup')),
          },
          {
            path: 'deployment',
            element: lazyLoad(() => import('../pages/settings/deployment')),
          },
          {
            path: 'commands',
            element: lazyLoad(() => import('../pages/settings/commands')),
          },
        ],
      },
      {
        path: 'commands',
        children: [
          {
            index: true,
            element: <Navigate to="/settings/commands" replace />,
          },
          {
            path: 'management',
            element: <Navigate to="/settings/commands" replace />,
          },
          {
            path: 'output',
            element: <Navigate to="/settings/commands?focus=execute" replace />,
          },
        ],
      },
      {
        // 登录者**自己**的账号页：用户名 / 用户 ID / 权限等级 + 自助改口令。
        // 这条路径此前指向 `pages/cloud/profile`（本部署那一个 NekroAI 社区账号的主页），
        // 于是 `pages/profile/index.tsx` 整页成了死代码 —— 全库没有第二处引用它，
        // 租户成员因此永远换不了自己的口令。两页现在按"主体是谁"分成两条路径：
        // 本条的主体是登录者本人（人人有，无身份门槛），`/cloud/profile` 的主体是
        // 部署级的社区账号（平台方专属）。合并成一页会把一块租户看不见的区块塞进
        // 一个人人都要进的页面，还得在页内再维护一份可见性判断。
        path: 'profile',
        element: lazyLoad(() => import('../pages/profile')),
      },
      {
        path: 'cloud/telemetry',
        element: lazyLoad(() => import('../pages/cloud/telemetry')),
      },
      {
        path: 'cloud/presets-market',
        element: lazyLoad(() => import('../pages/cloud/presets_market')),
      },
      {
        path: 'cloud/plugins-market',
        element: lazyLoad(() => import('../pages/cloud/plugins_market')),
      },
      {
        // 本部署那**一个** NekroAI 社区账号的主页（它的发布物与收藏）。
        // 它不是"登录者的资料页"，与登录身份无关，后端那几条接口都是平台方专属，
        // 所以留在 `/cloud` 前缀下由 `PLATFORM_ONLY_PATHS` 的 `/cloud` 一并兜住。
        path: 'cloud/profile',
        element: lazyLoad(() => import('../pages/cloud/profile')),
      },
      {
        path: 'workspace',
        children: [
          {
            index: true,
            element: lazyLoad(() => import('../pages/workspace')),
          },
          {
            path: 'resources',
            element: lazyLoad(() => import('../pages/workspace/resources')),
          },
          {
            path: 'skills',
            element: lazyLoad(() => import('../pages/workspace/skills')),
          },
          {
            path: 'cc-models',
            element: <Navigate to="/settings/models?tab=cc" replace />,
          },
          {
            path: 'mcp-services',
            element: lazyLoad(() => import('../pages/workspace/mcp-services')),
          },
          {
            path: 'kb-library',
            element: lazyLoad(() => import('../pages/workspace/kb-library')),
          },
          {
            path: 'timers',
            element: lazyLoad(() => import('../pages/workspace/timers')),
          },
          {
            path: ':id',
            children: [
              {
                index: true,
                element: <Navigate to="overview" replace />,
              },
              {
                path: ':tab',
                element: lazyLoad(() => import('../pages/workspace/detail')),
              },
            ],
          },
        ],
      },
    ],
  },
  {
    path: '*',
    element: <Navigate to="/" />,
  },
])

export default router
