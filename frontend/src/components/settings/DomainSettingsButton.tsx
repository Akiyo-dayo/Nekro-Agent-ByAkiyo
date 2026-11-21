/**
 * 功能页页头的「设置」按钮 + 右侧抽屉
 *
 * 六个功能域共用这一个组件：按钮只传 `domain`，具体哪些键算常用、高级项怎么分组
 * 由 `config/domainSettings.ts` 声明。抽屉里仍然用 `ConfigTable` 渲染每一行，
 * 只是换成紧凑版（隐藏「属性」「类型」两列），所以行内编辑、覆盖开关、保存校验
 * 与系统设置页完全同一套实现。
 *
 * 编辑层：
 * - 平台方的「全局」层直连 `createConfigService('system')`；
 * - 「实例」层与系统设置页一致，复用 `createInstanceConfigService` 的实例覆盖接口。
 *   租户身份没有全局层，只显示「实例」。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  CircularProgress,
  Divider,
  Drawer,
  Stack,
  type SxProps,
  TextField,
  type Theme,
  Typography,
} from '@mui/material'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import SearchIcon from '@mui/icons-material/Search'
import SettingsRoundedIcon from '@mui/icons-material/SettingsRounded'
import {
  Link as RouterLink,
  unstable_usePrompt as usePrompt,
  useSearchParams,
  type BlockerFunction,
} from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import ConfigTable, {
  type ConfigItem,
  type ConfigTableEditorState,
  type ConfigTableProps,
} from '../common/ConfigTable'
import IconActionButton from '../common/IconActionButton'
import SegmentedControl from '../common/SegmentedControl'
import { buildDomainSections, getDomainSettings, type DomainKey } from '../../config/domainSettings'
import { useIdentity } from '../../hooks/useIdentity'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { createInstanceConfigService } from '../../services/api/instance-scope'
import { createConfigService } from '../../services/api/unified-config'
import { getLocalizedText } from '../../services/api/types'
import { SCROLL_COLUMN_STYLES } from '../../theme/variants'
import { pluginsManagementPath } from '../../router/routes'

type EditLayer = 'global' | 'instance'

const EMPTY_EDITOR_STATE: ConfigTableEditorState = {
  dirtyKeys: [],
  save: async () => {},
}

const noopSearchChange = () => {}

interface EditorTableProps extends Omit<ConfigTableProps, 'onEditorStateChange'> {
  id: string
  onState: (id: string, state: ConfigTableEditorState) => void
}

/** 让每张表的未保存键走同一条上报通道；卸载时清掉自己的那一份 */
function EditorTable({ id, onState, ...props }: EditorTableProps) {
  const handleStateChange = useCallback(
    (state: ConfigTableEditorState) => onState(id, state),
    [id, onState]
  )

  useEffect(() => () => onState(id, EMPTY_EDITOR_STATE), [id, onState])

  return <ConfigTable {...props} onEditorStateChange={handleStateChange} />
}

const sameKeys = (a: string[], b: string[]) =>
  a.length === b.length && a.every((key, index) => key === b[index])

export interface DomainSettingsButtonProps {
  domain: DomainKey
}

export default function DomainSettingsButton({ domain }: DomainSettingsButtonProps) {
  const { t, i18n } = useTranslation('domain-settings')
  const definition = getDomainSettings(domain)
  const { currentInstance, currentInstanceKey, isScopeReady } = useInstanceScope()
  const { isTenantScoped, isLoading: isIdentityLoading } = useIdentity()
  const [searchParams, setSearchParams] = useSearchParams()

  const [open, setOpen] = useState(false)
  const [layer, setLayer] = useState<EditLayer>(() =>
    isTenantScoped || currentInstanceKey ? 'instance' : 'global'
  )
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [dirtyByTable, setDirtyByTable] = useState<Record<string, string[]>>({})
  const [editorVersion, setEditorVersion] = useState(0)
  const [saving, setSaving] = useState(false)
  const saveFnsRef = useRef<Record<string, () => Promise<void>>>({})
  const openRef = useRef(false)

  // 顶栏切换实例后回到实例层；身份确认完成前先按平台方处理，这是全站一致的兜底
  useEffect(() => {
    if (isIdentityLoading) {
      return
    }
    setLayer(isTenantScoped || currentInstanceKey ? 'instance' : 'global')
  }, [isIdentityLoading, isTenantScoped, currentInstanceKey])

  // 地址带 `?settings=open` 时自动打开；参数被浏览器后退掉时也跟着关掉
  useEffect(() => {
    const shouldOpen = searchParams.get('settings') === 'open'
    openRef.current = shouldOpen
    setOpen(shouldOpen)
    if (!shouldOpen) {
      setDirtyByTable({})
      saveFnsRef.current = {}
    }
  }, [searchParams])

  const syncSettingsParam = useCallback(
    (nextOpen: boolean) => {
      setSearchParams(
        prev => {
          const next = new URLSearchParams(prev)
          if (nextOpen) {
            next.set('settings', 'open')
          } else {
            next.delete('settings')
          }
          return next
        },
        { replace: true }
      )
    },
    [setSearchParams]
  )

  const handleOpen = () => {
    openRef.current = true
    setOpen(true)
    syncSettingsParam(true)
  }

  const handleClose = () => {
    openRef.current = false
    setOpen(false)
    setAdvancedOpen(false)
    setSearch('')
    setDirtyByTable({})
    saveFnsRef.current = {}
    syncSettingsParam(false)
  }

  const configService = useMemo(() => {
    if (layer === 'instance') {
      if (!currentInstance) {
        return null
      }
      return createInstanceConfigService(currentInstance.adapter_key, currentInstance.instance_key)
    }
    if (isTenantScoped) {
      return null
    }
    return createConfigService('system')
  }, [layer, currentInstance, isTenantScoped])

  const {
    data: configs = [],
    isLoading,
    refetch,
  } = useQuery({
    queryKey: [
      'domain-settings',
      domain,
      layer,
      layer === 'instance'
        ? `${currentInstance?.adapter_key ?? ''}:${currentInstance?.instance_key ?? ''}`
        : 'global',
    ],
    queryFn: () => (configService as NonNullable<typeof configService>).getConfigList('system'),
    enabled: configService !== null && isScopeReady,
    retry: false,
  })

  const configByKey = useMemo(
    () => new Map<string, ConfigItem>(configs.map(item => [item.key, item])),
    [configs]
  )

  const sections = useMemo(() => buildDomainSections(definition, configs), [definition, configs])

  const query = advancedOpen ? search.trim().toLowerCase() : ''
  const matchesQuery = useCallback(
    (key: string) => {
      if (!query) {
        return true
      }
      const item = configByKey.get(key)
      if (!item) {
        return false
      }
      const title = getLocalizedText(item.i18n_title, item.title, i18n.language)
      const description = getLocalizedText(
        item.i18n_description,
        item.description ?? '',
        i18n.language
      )
      return `${key} ${title} ${description}`.toLowerCase().includes(query)
    },
    [configByKey, i18n.language, query]
  )

  const advancedCount = useMemo(
    () => sections.advancedGroups.reduce((total, group) => total + group.keys.length, 0),
    [sections.advancedGroups]
  )

  const dirtyKeys = useMemo(
    () =>
      Array.from(
        new Set(
          Object.values(dirtyByTable)
            .flat()
            .filter(Boolean)
        )
      ),
    [dirtyByTable]
  )

  // 抽屉里同时挂多张表，各自注册 blocker 会互相顶掉；统一在这里拦一次。
  // 只拦 pathname 变化：开关抽屉本身只改 `?settings=`，不应弹确认。
  const shouldBlockNavigation = useCallback<BlockerFunction>(
    ({ currentLocation, nextLocation }) =>
      dirtyKeys.length > 0 && currentLocation.pathname !== nextLocation.pathname,
    [dirtyKeys.length]
  )
  usePrompt({ when: shouldBlockNavigation, message: t('unsavedLeaveConfirm') })

  const needsRestart = useMemo(
    () =>
      dirtyKeys.some(key => {
        const item = configByKey.get(key)
        return Boolean(item?.is_need_restart || item?.field_schema?.[key]?.is_need_restart)
      }),
    [configByKey, dirtyKeys]
  )

  const worldbookBudgetConfigKey =
    layer === 'instance' && currentInstance
      ? `instance_override_${currentInstance.adapter_key}__${currentInstance.instance_key}`
      : 'system'

  const handleEditorState = useCallback((id: string, state: ConfigTableEditorState) => {
    // 抽屉关闭动画期间表格还没卸载，别把它们上一次的未保存键再报回来
    if (!openRef.current) {
      return
    }
    saveFnsRef.current[id] = state.save
    setDirtyByTable(prev => {
      if (sameKeys(prev[id] ?? [], state.dirtyKeys)) {
        return prev
      }
      return { ...prev, [id]: state.dirtyKeys }
    })
  }, [])

  const handleRefresh = useCallback(() => {
    void refetch()
  }, [refetch])

  const resetTables = () => {
    setDirtyByTable({})
    saveFnsRef.current = {}
    // React Query 会做结构共享，refetch 回来的数据可能还是同一个引用；
    // 换 key 强制重挂载，才能保证「放弃」真的把行内编辑值丢掉。
    setEditorVersion(version => version + 1)
  }

  const handleLayerChange = (nextLayer: EditLayer) => {
    setLayer(nextLayer)
    resetTables()
  }

  const handleDiscard = () => {
    resetTables()
    void refetch()
  }

  const handleSave = async () => {
    const ids = Object.entries(dirtyByTable)
      .filter(([, keys]) => keys.length > 0)
      .map(([id]) => id)
    if (ids.length === 0) {
      return
    }
    setSaving(true)
    try {
      await Promise.all(ids.map(id => saveFnsRef.current[id]?.() ?? Promise.resolve()))
    } finally {
      setSaving(false)
    }
  }

  const layerOptions = useMemo(() => {
    const options: { value: EditLayer; label: string; disabled?: boolean }[] = []
    // 身份没确认前不画层选择器：租户不该看到「全局」闪一下
    if (isIdentityLoading) {
      return options
    }
    if (!isTenantScoped) {
      options.push({ value: 'global', label: t('layer.global') })
    }
    if (currentInstance) {
      options.push({
        value: 'instance',
        label: t('layer.instance', { name: currentInstance.display_name || currentInstance.instance_key }),
      })
    } else if (isTenantScoped) {
      options.push({ value: 'instance', label: t('layer.instanceOnly'), disabled: true })
    }
    return options
  }, [currentInstance, isIdentityLoading, isTenantScoped, t])

  const commonHasMatch = sections.commonKeys.some(matchesQuery)
  const advancedHasMatch = sections.advancedGroups.some(group => group.keys.some(matchesQuery))
  const noSearchResults = Boolean(query) && !commonHasMatch && !advancedHasMatch

  const tableProps = {
    configKey: 'system',
    configService: configService ?? createConfigService('system'),
    configs,
    loading: isLoading,
    showSearchBar: false,
    showToolbar: false,
    showCategoryColumn: false,
    isOverridePage: layer === 'instance',
    compact: true,
    navigationPrompt: false,
    searchText: query,
    onSearchChange: noopSearchChange,
    onRefresh: handleRefresh,
    emptyMessage: t('empty'),
    worldbookBudgetConfigKey,
  } satisfies Omit<ConfigTableProps, 'visibleKeys'>

  return (
    <>
      <ActionButton
        tone="secondary"
        size="small"
        startIcon={<SettingsRoundedIcon />}
        onClick={handleOpen}
      >
        {t('button')}
      </ActionButton>

      <Drawer
        anchor="right"
        open={open}
        onClose={handleClose}
        PaperProps={{
          sx: {
            width: { xs: '100%', md: 720 },
            maxWidth: '100%',
            bgcolor: 'background.paper',
          },
        }}
      >
        <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <Box
            sx={{
              position: 'sticky',
              top: 0,
              zIndex: 2,
              px: 2,
              py: 1.5,
              bgcolor: 'background.paper',
              borderBottom: '1px solid',
              borderColor: 'divider',
            }}
          >
            <Stack direction="row" alignItems="center" spacing={1.5} flexWrap="wrap">
              <Typography variant="h6" fontWeight={700} sx={{ minWidth: 0 }}>
                {t(definition.titleKey)}
              </Typography>
              <Box sx={{ flex: 1 }} />
              <IconActionButton onClick={handleClose} aria-label={t('close')}>
                <CloseRoundedIcon />
              </IconActionButton>
            </Stack>
            {layerOptions.length > 0 && (
              <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mt: 1 }}>
                <Typography variant="caption" color="text.secondary">
                  {t('layer.label')}
                </Typography>
                <SegmentedControl
                  value={layer}
                  options={layerOptions}
                  onChange={handleLayerChange}
                  density="compact"
                  disabled={layerOptions.length === 1}
                />
              </Stack>
            )}
          </Box>

          <Box
            sx={[
              SCROLL_COLUMN_STYLES,
              { flex: 1, px: 2, py: 2, gap: 1.5 },
            ] as SxProps<Theme>}
          >
            {!isScopeReady || isLoading ? (
              <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
                <CircularProgress size={28} />
              </Box>
            ) : configService === null ? (
              <Typography variant="body2" color="text.secondary">
                {t('layer.instanceMissing')}
              </Typography>
            ) : (
              <>
                <Typography variant="overline" color="text.secondary">
                  {t('common')}
                </Typography>
                <Box sx={{ display: commonHasMatch ? 'block' : 'none' }}>
                  <EditorTable
                    key={`common:${currentInstanceKey ?? 'global'}:${layer}:${editorVersion}`}
                    id="common"
                    onState={handleEditorState}
                    {...tableProps}
                    visibleKeys={sections.commonKeys}
                  />
                </Box>

                <Accordion
                  expanded={advancedOpen}
                  onChange={(_event, next) => setAdvancedOpen(next)}
                  disableGutters
                  elevation={0}
                  sx={{
                    border: '1px solid',
                    borderColor: 'divider',
                    borderRadius: 1,
                    '&:before': { display: 'none' },
                  }}
                >
                  <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                    <Typography variant="body2" fontWeight={600}>
                      {t('advancedSection', { count: advancedCount })}
                    </Typography>
                  </AccordionSummary>
                  <AccordionDetails sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                    {advancedOpen && (
                      <TextField
                        size="small"
                        value={search}
                        onChange={event => setSearch(event.target.value)}
                        placeholder={t('searchPlaceholder')}
                        InputProps={{
                          startAdornment: (
                            <Box sx={{ display: 'flex', alignItems: 'center', pr: 1 }}>
                              <SearchIcon fontSize="small" />
                            </Box>
                          ),
                        }}
                      />
                    )}
                    {noSearchResults && (
                      <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
                        {t('empty')}
                      </Typography>
                    )}
                    {sections.advancedGroups.map(group => {
                      const hasMatch = group.keys.some(matchesQuery)
                      return (
                        <Box
                          key={group.title}
                          sx={{ display: hasMatch ? 'flex' : 'none', flexDirection: 'column', gap: 1 }}
                        >
                          <Typography variant="overline" color="text.secondary">
                            {t(group.title)}
                          </Typography>
                          <EditorTable
                            key={`${group.title}:${currentInstanceKey ?? 'global'}:${layer}:${editorVersion}`}
                            id={`advanced:${group.title}`}
                            onState={handleEditorState}
                            {...tableProps}
                            visibleKeys={group.keys}
                          />
                        </Box>
                      )
                    })}
                  </AccordionDetails>
                </Accordion>

                {domain === 'state' && (
                  <>
                    <Divider />
                    <Stack
                      direction={{ xs: 'column', sm: 'row' }}
                      spacing={1}
                      alignItems={{ xs: 'flex-start', sm: 'center' }}
                      justifyContent="space-between"
                      sx={{ p: 1.5, border: '1px solid', borderColor: 'divider', borderRadius: 1 }}
                    >
                      <Typography variant="body2" color="text.secondary">
                        {t('statePlugin.hint')} →
                      </Typography>
                      <ActionButton
                        tone="ghost"
                        size="small"
                        component={RouterLink}
                        to={`${pluginsManagementPath('Akiyo_dayo.state_table')}?tab=config`}
                      >
                        {t('statePlugin.action')}
                      </ActionButton>
                    </Stack>
                  </>
                )}
              </>
            )}
          </Box>

          <Box
            sx={{
              position: 'sticky',
              bottom: 0,
              zIndex: 2,
              px: 2,
              py: 1.5,
              bgcolor: 'background.paper',
              borderTop: '1px solid',
              borderColor: 'divider',
            }}
          >
            {needsRestart && (
              <Typography variant="caption" color="warning.main" sx={{ display: 'block', mb: 0.5 }}>
                {t('restartHint')}
              </Typography>
            )}
            <Stack direction="row" alignItems="center" spacing={1}>
              <Typography variant="body2" color="text.secondary">
                {t('unsaved', { count: dirtyKeys.length })}
              </Typography>
              <Box sx={{ flex: 1 }} />
              <ActionButton
                tone="ghost"
                size="small"
                onClick={handleDiscard}
                disabled={dirtyKeys.length === 0 || saving}
              >
                {t('discard')}
              </ActionButton>
              <ActionButton
                tone="primary"
                size="small"
                onClick={() => void handleSave()}
                disabled={dirtyKeys.length === 0 || saving}
              >
                {saving ? <CircularProgress size={18} /> : t('save')}
              </ActionButton>
            </Stack>
          </Box>
        </Box>
      </Drawer>
    </>
  )
}
