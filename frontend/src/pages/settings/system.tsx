/**
 * 系统配置页（全局视图 = 全局配置；实例作用域 = 该实例的覆盖层）
 *
 * ## 这一页对租户开着，是有意的
 *
 * 四层配置是 `会话 > 实例 > 适配器 > 全局`。租户能写的只有**自己名下实例**那一层，
 * 而本页是这一层在整个前端里的**唯一**写入口（`createInstanceConfigService` →
 * `PUT /config/instance/{adapter}/{instance}`）。把它关进 `PLATFORM_ONLY_PATHS` 的代价是
 * 租户换个模型组只能去 `/chat-channel` 逐个频道改。
 *
 * ## 租户身份下的三条硬约束（都在本文件里落地）
 *
 * 1. **作用域恒为某个具体实例**。租户没有「全局视图」这一档：
 *    `instance_key` 为空对平台方是"看全部"，对租户是后端直接 400。因此租户身份下
 *    `configService` 宁可为 `null`（页面显示"没有可管理的实例"），也**不回落**
 *    `createConfigService('system')` —— 那会把全局配置表摆到租户面前，读得到、
 *    点保存却必然 403（后端 `_assert_config_key_accessible` 对共享作用域的写入判 403）。
 * 2. **共享层（全局 / 适配器）不可写**。这条由后端把守，前端做的是不给入口：
 *    租户看不到全局视图，也就没有"在共享层保存"这个动作。
 * 3. **凭据仍是掩码语义**。读侧 `_mask_config_items` 出掩码串，写侧 `_assert_tenant_may_write`
 *    对承载凭据的字段整体只读。前端不重复这套判定，也不做任何"本地明文回填"。
 * 4. **「租户」不是一个身份，是两个。** 这一页对**租户管理员**开着，对**只读租户成员**
 *    （`tenant_user` = `Role.User`）整页零可用：它要打的两条请求
 *    `GET /console/instance-options` 与 `GET /config/instance/{adapter}/{instance}`
 *    都带 `@require_role(Role.Admin)`，双双 403（测试部署上实测）。
 *    不分这一档的后果不是"看到一页只读表单"，而是**实例列表恒为空 → 页面说「名下暂时
 *    没有实例，请联系服务提供方」**，而同一时刻 `/user/me/identity` 明写着
 *    `accessible_instance_keys: ["inst1"]` —— 一句假话，和把越权 404 说成"接口没上线"
 *    是同一个错误。菜单里由 `visibility: 'instanceConfigOverrides'` 不画这一格，
 *    本文件兜 URL 直达。
 *
 * ## 实例作用域必须按覆盖页渲染
 *
 * 实例级列表返回的是**成对**的 `enable_X` / `X`：`enable_X` 关着时 `X` 不参与解析。
 * 不传 `isOverridePage` 的话既画不出覆盖开关、也不会把 `enable_X` 一起提交，
 * 用户改完模型组按保存 → 值落进覆盖文件、开关仍是 false → **什么都没发生**。
 * 同理 `enable_X` 自己没有分类信息，必须归到被它控制的那一项的分类下，
 * 否则它们全落进「其他」，而 `ConfigTable` 靠 `configs` 里同时存在两者才算得出开关状态。
 *
 * 全局视图完全不受影响：`/config/list/system` 的每一项 `enable_toggle` 都为空，
 * 也没有任何 `enable_` 开头的键，上面两条在那一侧退化成恒等变换。
 */

import { useEffect, useMemo, useState } from 'react'
import { Alert, AlertTitle, Box, Typography } from '@mui/material'
import RocketLaunchIcon from '@mui/icons-material/RocketLaunch'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import ConfigTable from '../../components/common/ConfigTable'
import SearchField from '../../components/common/SearchField'
import {
  DOMAIN_SETTINGS_LIST,
  findDomainByCategory,
  type DomainSettingsDefinition,
} from '../../config/domainSettings'
import {
  SETTINGS_CATEGORY_GROUPS,
  findSettingsCategoryGroup,
} from '../../config/settingsCategoryGroups'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import { useIdentity } from '../../hooks/useIdentity'
import { INSTANCE_SCOPE_QUERY_KEY, useInstanceScope } from '../../hooks/useInstanceScope'
import {
  createInstanceConfigService,
  isScopeEndpointUnavailable,
} from '../../services/api/instance-scope'
import { createConfigService, unifiedConfigApi } from '../../services/api/unified-config'
import { useTranslation } from 'react-i18next'
import { getLocalizedText } from '../../services/api/types'
import type { ConfigItem, ConfigService } from '../../components/common/ConfigTable'
import { useLocaleStore } from '../../stores/locale'
import type { SupportedLocale } from '../../config/i18n'
import ActionButton from '../../components/common/ActionButton'
import OutputFilterHitsPanel from './components/OutputFilterHitsPanel'
import SettingsCategoryNav, { type CategoryNavGroup } from './components/SettingsCategoryNav'

/** 分类在导航里的排序键：先按分组表，再是功能页那几类，再是没归组的，「其他」垫底 */
const categoryRank = (category: string, otherLabel: string, discovered: string[]) => {
  if (category === otherLabel) {
    return [3, 0]
  }
  const grouped = findSettingsCategoryGroup(category)
  if (grouped) {
    return [0, SETTINGS_CATEGORY_GROUPS.indexOf(grouped.group) * 100 + grouped.order]
  }
  const domainIndex = DOMAIN_SETTINGS_LIST.findIndex(
    domain => domain.category === category || domain.categoryEn === category
  )
  if (domainIndex >= 0) {
    return [1, domainIndex]
  }
  return [2, discovered.indexOf(category)]
}

export default function SettingsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { t } = useTranslation('settings')
  const { t: tDomainSettings } = useTranslation('domain-settings')
  const { i18n } = useTranslation()
  const { setLocaleLocal } = useLocaleStore()
  const urlSearchText = searchParams.get('search') ?? ''
  const requestedCategory = searchParams.get('category') ?? ''
  const [searchInput, setSearchInput] = useState<string>(urlSearchText)
  const [debouncedSearchInput, setDebouncedSearchInput] = useState<string>(urlSearchText)

  const { currentInstance, currentInstanceKey, setCurrentInstance, isScopeReady } =
    useInstanceScope()
  const { isTenantScoped, isReadOnly, accessibleInstanceKeys } = useIdentity()

  // 实例作用域下读写实例级覆盖；全局视图仍然直接操作全局配置，**但租户没有全局视图这一档**，
  // 拿不到实例时返回 null 而不是回落到全局服务（理由见文件头注释第 1 条）。
  const configService = useMemo<ConfigService | null>(() => {
    if (currentInstance) {
      return createInstanceConfigService(currentInstance.adapter_key, currentInstance.instance_key)
    }
    if (isTenantScoped) {
      return null
    }
    return createConfigService('system')
  }, [currentInstance, isTenantScoped])

  // 覆盖页形态只由「当前是不是实例作用域」决定，与身份无关：
  // 平台方切到某个实例时看到的也必须是能真正生效的覆盖开关。
  const isOverrideScope = Boolean(currentInstanceKey)

  // 获取系统配置列表
  const {
    data: configs = [],
    refetch,
    isLoading,
    error: configsError,
  } = useQuery({
    queryKey: ['system-configs', currentInstanceKey],
    queryFn: () => (configService as ConfigService).getConfigList('system'),
    enabled: configService !== null,
    retry: false,
  })

  // 世界书预算改成按 token 计的一次性迁移提示；租户身份取回来恒为空数组
  const { data: worldbookBudgetNotice } = useQuery({
    queryKey: ['worldbook-budget-notice'],
    queryFn: () => unifiedConfigApi.getWorldbookBudgetNotice(),
    retry: false,
  })
  const ackWorldbookBudgetNotice = useMutation({
    mutationFn: () => unifiedConfigApi.ackWorldbookBudgetNotice(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['worldbook-budget-notice'] })
    },
  })
  const worldbookBudgetNoticeLines = worldbookBudgetNotice?.lines ?? []

  // 实例作用域下配置项走实例覆盖接口，预算换算也要取实例层的键
  const worldbookBudgetConfigKey = currentInstance
    ? `instance_override_${currentInstance.adapter_key}__${currentInstance.instance_key}`
    : 'system'

  // 「后端未就绪」这个结论只对平台方成立。租户拿到的 404 可能是
  // `assert_instance_accessible` 的越权 404（实例已被划走），把它说成"接口没上线"、
  // 再递一个「切回全局视图」的按钮，两句都是错的——租户根本没有全局视图。
  const instanceConfigUnavailable =
    !!currentInstanceKey && !isTenantScoped && isScopeEndpointUnavailable(configsError)
  // 只读租户成员（`tenant_user` = `Role.User`）在这一页上**读也读不到**：
  // `GET /console/instance-options` 与 `GET /config/instance/{adapter}/{instance}`
  // 都是 `@require_role(Role.Admin)`，两条一起 403。菜单里已经不画这一格
  // （`visibility: 'instanceConfigOverrides'`），这里兜的是直接敲 URL。
  // 必须排在下面那条之前：实例列表被 403 挡掉之后列表恒为空，不分档就会被说成
  // 「名下没有实例」——而同一时刻 `/user/me/identity` 明写着他有实例。
  const tenantCannotReadInstanceConfig = isTenantScoped && isReadOnly
  // 租户身份下作用域恒为某个具体实例。一个都没有时要分清两种情形，
  // 否则就会把一种失败说成另一种。
  // 判据取自身份接口的 `accessible_instance_keys`——它与实例列表是两条独立的请求，
  // 一条挂了另一条还在。
  const tenantScopeMissing = isTenantScoped && isScopeReady && !currentInstance
  const tenantWithoutInstanceScope = tenantScopeMissing && accessibleInstanceKeys.length === 0
  // 身份说他有实例，实例列表却一个都没给出来 = 那次请求失败了，不是"名下没有实例"
  const tenantInstanceListUnavailable = tenantScopeMissing && accessibleInstanceKeys.length > 0

  // 从配置项中提取本地化的分类名称，无分类时 fallback 到"其他"
  const resolveCategory = (config: ConfigItem): string => {
    if (config.i18n_category && typeof config.i18n_category === 'object') {
      return (
        getLocalizedText(config.i18n_category, config.category || '', i18n.language) ||
        t('system.otherCategory', '其他')
      )
    }
    if (typeof config.i18n_category === 'string' && config.i18n_category) {
      return config.i18n_category
    }
    return config.category || t('system.otherCategory', '其他')
  }

  // 按分类组织配置，同时提取分类列表（按导航里的分组顺序排好）
  const { categories, configsByCategory, categoryCounts } = useMemo(() => {
    const visibleConfigs = configs.filter((config: ConfigItem) => !config.is_hidden)
    const seen = new Set<string>()
    const cats: string[] = []
    const grouped: Record<string, ConfigItem[]> = {}

    const otherLabel = t('system.otherCategory', '其他')

    // 覆盖开关（`enable_X`）自己不带分类信息。把它归到被它控制的那一项的分类下，
    // 否则它们全部落进「其他」，而 `ConfigTable` 要在同一份 `configs` 里同时看到
    // `X` 与 `enable_X` 才算得出开关状态——分家的结果是开关永远显示为关、点了也不亮。
    // 全局视图下没有任何 `enable_toggle`，这张表恒为空，整段退化成恒等变换。
    const toggleOwnerCategory = new Map<string, string>()
    visibleConfigs.forEach((config: ConfigItem) => {
      if (config.enable_toggle) {
        toggleOwnerCategory.set(config.enable_toggle, resolveCategory(config))
      }
    })

    // 导航上显示的条数只数配置项本身，不数成对的覆盖开关
    const counts: Record<string, number> = {}

    visibleConfigs.forEach((config: ConfigItem) => {
      const category = toggleOwnerCategory.get(config.key) ?? resolveCategory(config)
      if (!seen.has(category)) {
        seen.add(category)
        cats.push(category)
      }
      if (!grouped[category]) {
        grouped[category] = []
      }
      grouped[category].push(config)
      if (!toggleOwnerCategory.has(config.key)) {
        counts[category] = (counts[category] ?? 0) + 1
      }
    })

    const sortedCategories = cats.slice().sort((a, b) => {
      const [aTier, aOrder] = categoryRank(a, otherLabel, cats)
      const [bTier, bOrder] = categoryRank(b, otherLabel, cats)
      return aTier - bTier || aOrder - bOrder
    })

    return { categories: sortedCategories, configsByCategory: grouped, categoryCounts: counts }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configs, i18n.language, t])

  const activeTab = useMemo(() => {
    if (categories.length === 0) {
      return ''
    }
    if (requestedCategory && categories.includes(requestedCategory)) {
      return requestedCategory
    }
    // 默认落在第一个还在本页编辑的分类上，而不是一张跳转卡
    return categories.find(category => !findDomainByCategory(category)) ?? categories[0]
  }, [categories, requestedCategory])

  // 六个功能域的配置已经移到各自页面：导航里合成一组「功能页设置」，点了直接去那一页。
  // 旧的 `?category=记忆系统` 这类深链仍然有效：落在这里，由下面那张跳转卡接住。
  const domainForActiveTab = useMemo(() => findDomainByCategory(activeTab), [activeTab])

  const navGroups = useMemo<CategoryNavGroup[]>(() => {
    const otherLabel = t('system.otherCategory', '其他')
    const itemOf = (category: string, domain?: DomainSettingsDefinition) => ({
      category,
      count: categoryCounts[category] ?? 0,
      domain,
    })
    const groups: CategoryNavGroup[] = SETTINGS_CATEGORY_GROUPS.map(group => ({
      key: group.key,
      title: t(group.titleKey),
      items: categories
        .filter(category => findSettingsCategoryGroup(category)?.group === group)
        .map(category => itemOf(category)),
    }))
    groups.push({
      key: 'domains',
      title: t('system.groups.domains'),
      items: categories.flatMap(category => {
        const domain = findDomainByCategory(category)
        return domain ? [itemOf(category, domain)] : []
      }),
    })
    // 没归进任何一组的分类（含后端没给分类的配置项）都落「其他」
    groups.push({
      key: 'other',
      title: t('system.groups.other'),
      items: categories
        .filter(
          category =>
            category === otherLabel ||
            (!findSettingsCategoryGroup(category) && !findDomainByCategory(category))
        )
        .map(category => itemOf(category)),
    })
    return groups.filter(group => group.items.length > 0)
  }, [categories, categoryCounts, t])

  const isSearching = Boolean(searchInput.trim())

  const selectCategory = (category: string) => {
    const nextParams = new URLSearchParams(searchParams)
    nextParams.set('category', category)
    // 选分类即退出搜索：搜索结果跨全部分类，和「看某一类」是两种视图
    nextParams.delete('search')
    setSearchInput('')
    setDebouncedSearchInput('')
    setSearchParams(nextParams, { replace: true })
  }

  useEffect(() => {
    setSearchInput(urlSearchText)
    setDebouncedSearchInput(urlSearchText)
  }, [urlSearchText])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSearchInput(searchInput)
    }, 300)
    return () => window.clearTimeout(timer)
  }, [searchInput])

  useEffect(() => {
    if (categories.length === 0 || !activeTab) {
      return
    }
    if (requestedCategory === activeTab) {
      return
    }
    const nextParams = new URLSearchParams(searchParams)
    nextParams.set('category', activeTab)
    setSearchParams(nextParams, { replace: true })
  }, [activeTab, categories.length, requestedCategory, searchParams, setSearchParams])

  useEffect(() => {
    const normalizedSearch = debouncedSearchInput.trim()
    const currentSearch = searchParams.get('search') ?? ''
    const currentCategory = searchParams.get('category') ?? ''
    const normalizedCurrentSearch = currentSearch.trim()
    if (normalizedSearch === normalizedCurrentSearch && currentCategory === activeTab) {
      return
    }

    const nextParams = new URLSearchParams(searchParams)
    if (normalizedSearch) {
      nextParams.set('search', debouncedSearchInput)
    } else {
      nextParams.delete('search')
    }
    if (activeTab) {
      nextParams.set('category', activeTab)
    }
    setSearchParams(nextParams, { replace: true })
  }, [activeTab, debouncedSearchInput, searchParams, setSearchParams])

  // 当系统配置刷新后，同步 SYSTEM_LANG 到前端 locale。
  // 实例作用域下读到的是实例级覆盖，不代表后台自身的语言，因此只在全局视图同步。
  useEffect(() => {
    if (currentInstanceKey) {
      return
    }
    const langConfig = configs.find((c: ConfigItem) => c.key === 'SYSTEM_LANG')
    if (langConfig) {
      const backendLang = langConfig.value as string
      if (backendLang === 'zh-CN' || backendLang === 'en-US') {
        setLocaleLocal(backendLang as SupportedLocale)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configs])

  // 根据当前选中的分类过滤配置
  const displayConfigs = useMemo(() => {
    if (searchInput.trim()) {
      return configs
    }
    return configsByCategory[activeTab] || []
  }, [activeTab, configs, configsByCategory, searchInput])

  const showOutputFilterPanel = displayConfigs.some((c) => c.key === 'OUTPUT_FILTER_ENABLE')

  const handleSearchChange = (text: string | undefined) => {
    setSearchInput(text ?? '')
  }

  const handleRefresh = () => {
    refetch()
  }

  // 重试的对象是**实例列表**那条查询（它在 `useInstanceScope` 里），不是本页的配置查询：
  // 配置查询此刻压根没发出去（`enabled: configService !== null`），refetch 它是个空动作。
  const handleRetryInstanceList = () => {
    void queryClient.invalidateQueries({ queryKey: INSTANCE_SCOPE_QUERY_KEY })
  }

  const otherCategoryLabel = t('system.otherCategory', '其他')
  // OOBE 是**部署级**的首次配置向导（写的是全局配置，且 `/oobe` 挂在 `MainLayout` 之外、
  // 兜不到 `PLATFORM_ONLY_PATHS`）。租户点进去就是一页做不了任何事的向导，不给这个按钮。
  const guideToolbarAction = activeTab === otherCategoryLabel && !isTenantScoped
    ? (
        <ActionButton
          tone="secondary"
          size="small"
          startIcon={<RocketLaunchIcon />}
          onClick={() => {
            navigate('/oobe')
          }}
          sx={{ height: 38, flexShrink: 0, whiteSpace: 'nowrap' }}
        >
          {t('oobe.actions.openGuide')}
        </ActionButton>
      )
    : null

  // 实例级配置接口尚未就绪时，明确告知而不是把全局配置伪装成实例配置让用户误改
  if (instanceConfigUnavailable) {
    return (
      <Box sx={{ p: 2 }}>
        <Alert
          severity="warning"
          action={
            <ActionButton tone="ghost" size="small" onClick={() => setCurrentInstance(null)}>
              {t('unavailable.backToGlobal', { ns: 'instance-scope' })}
            </ActionButton>
          }
        >
          <AlertTitle>{t('unavailable.title', { ns: 'instance-scope' })}</AlertTitle>
          {t('unavailable.settings', { ns: 'instance-scope' })}
        </Alert>
      </Box>
    )
  }

  // 只读租户成员：整页对他是 403，明说需要什么权限，而不是让他看一页空表或一句假话
  if (tenantCannotReadInstanceConfig) {
    return (
      <Box sx={{ p: 2 }}>
        <Alert severity="info">
          <AlertTitle>{t('tenantScope.readOnlyTitle', { ns: 'instance-scope' })}</AlertTitle>
          {t('tenantScope.readOnlyHint', { ns: 'instance-scope' })}
        </Alert>
      </Box>
    )
  }

  // 身份接口说他有实例，实例列表却空着——这是一次失败的请求，不是"名下没有实例"
  if (tenantInstanceListUnavailable) {
    return (
      <Box sx={{ p: 2 }}>
        <Alert
          severity="warning"
          action={
            <ActionButton tone="ghost" size="small" onClick={handleRetryInstanceList}>
              {t('unavailable.retry', { ns: 'instance-scope' })}
            </ActionButton>
          }
        >
          <AlertTitle>{t('tenantScope.listFailedTitle', { ns: 'instance-scope' })}</AlertTitle>
          {t('tenantScope.listFailedHint', { ns: 'instance-scope' })}
        </Alert>
      </Box>
    )
  }

  // 租户身份 + 一个可管理的实例都没有。这里刻意不给「切回全局视图」：租户没有那一档，
  // 点了只会被 `useInstanceScope` 原地弹回来。
  if (tenantWithoutInstanceScope) {
    return (
      <Box sx={{ p: 2 }}>
        <Alert severity="info">
          <AlertTitle>{t('tenantScope.noInstanceTitle', { ns: 'instance-scope' })}</AlertTitle>
          {t('tenantScope.noInstanceHint', { ns: 'instance-scope' })}
        </Alert>
      </Box>
    )
  }

  return (
    <Box
      sx={{
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        p: 2,
      }}
    >
      {worldbookBudgetNoticeLines.length > 0 && (
        <Alert
          severity="info"
          sx={{ mb: 2, flexShrink: 0 }}
          action={
            <ActionButton
              tone="ghost"
              size="small"
              onClick={() => ackWorldbookBudgetNotice.mutate()}
              disabled={ackWorldbookBudgetNotice.isPending}
            >
              {t('worldbookBudgetNotice.ack')}
            </ActionButton>
          }
        >
          <AlertTitle>{t('worldbookBudgetNotice.title')}</AlertTitle>
          {worldbookBudgetNoticeLines.map((line, index) => (
            <Typography key={index} variant="body2">
              {line}
            </Typography>
          ))}
          <Typography variant="body2" sx={{ mt: 1 }}>
            {t('worldbookBudgetNotice.hint')}
          </Typography>
        </Alert>
      )}

      {currentInstanceKey && (
        <Box sx={{ mb: 2, flexShrink: 0 }}>
          <InstanceScopeBanner description={t('banner.settings', { ns: 'instance-scope' })} />
        </Box>
      )}

      {/* 搜索 + 分组分类导航。搜索跨全部分类出结果，此时导航上不标当前项 */}
      <Box
        sx={{
          mb: 2,
          flexShrink: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 1.5,
        }}
      >
        {/* 上方留出浮动标签的位置：页面外层是 overflow: hidden */}
        <Box sx={{ width: { xs: '100%', sm: 360 }, pt: 0.5 }}>
          <SearchField
            fullWidth
            label={t('system.searchLabel')}
            placeholder={t('system.searchPlaceholder')}
            value={searchInput}
            onChange={value => handleSearchChange(value)}
            onClear={() => handleSearchChange('')}
          />
        </Box>
        {navGroups.length > 0 && (
          <SettingsCategoryNav
            groups={navGroups}
            activeCategory={isSearching ? null : activeTab}
            onSelect={selectCategory}
            onOpenDomain={domain => navigate(`${domain.pagePath}?settings=open`)}
          />
        )}
      </Box>

      {showOutputFilterPanel && (
        <Box sx={{ mb: 2, flexShrink: 0 }}>
          <OutputFilterHitsPanel
            adapterKey={currentInstance?.adapter_key}
            instanceKey={currentInstance?.instance_key}
          />
        </Box>
      )}

      <Box sx={{ flex: 1, minHeight: 0, overflow: 'hidden' }}>
        {domainForActiveTab && !isSearching ? (
          <Box
            sx={{
              p: 2,
              display: 'flex',
              alignItems: 'center',
              gap: 2,
              flexWrap: 'wrap',
              border: '1px solid',
              borderColor: 'divider',
              borderRadius: 1,
              bgcolor: 'background.paper',
            }}
          >
            <Typography variant="body1" sx={{ minWidth: 0, flex: 1 }}>
              {tDomainSettings('system.moved', {
                page: tDomainSettings(`page.${domainForActiveTab.key}`),
              })}
            </Typography>
            <ActionButton
              tone="primary"
              onClick={() => navigate(`${domainForActiveTab.pagePath}?settings=open`)}
            >
              {tDomainSettings('system.go')}
            </ActionButton>
          </Box>
        ) : (
          configService && (
            <ConfigTable
              configKey="system"
              worldbookBudgetConfigKey={worldbookBudgetConfigKey}
              configService={configService}
              configs={displayConfigs}
              loading={isLoading}
              searchText={searchInput}
              onSearchChange={handleSearchChange}
              onRefresh={handleRefresh}
              // 搜索框提到了分类导航上方（跨分类搜索），表格自己的不再画
              showSearchBar={false}
              showToolbar={true}
              toolbarActions={guideToolbarAction}
              showCategoryColumn={Boolean(searchInput.trim())}
              emptyMessage={t('system.emptyMessage')}
              // 实例作用域返回的是成对的 `enable_X` / `X`：不按覆盖页渲染就既画不出覆盖开关、
              // 也不会把 `enable_X` 一起提交，改完保存等于什么都没发生（见文件头注释）
              isOverridePage={isOverrideScope}
            />
          )
        )}
      </Box>
    </Box>
  )
}
