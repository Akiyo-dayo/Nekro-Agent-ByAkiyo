/**
 * 实例作用域切换器
 *
 * 挂在顶栏，用于在「全局视图」与各实例的完整后台之间切换。
 * 单实例（或零实例）部署下整个组件不渲染，用户完全感知不到多了这一层。
 *
 * ## 为什么不是一个下拉菜单
 *
 * 本项目面向的是实例数很多（几十个量级）的部署。把几十条一次性铺进 `Menu`
 * 有两个问题，而且是不同性质的两个：
 *
 * 1. **找不到**。几十条无序列表里翻一个 Bot，靠的是记住它排第几 —— 而管理员记得住的
 *    通常是 QQ 号。所以搜索必须同时命中实例标识、显示名和账号号码。
 * 2. **渲染贵**。MUI 的每个 `MenuItem` 都是带 TouchRipple 的 `ButtonBase`，几十条一次性
 *    挂载（并在每次打开时重新挂载）。这里改成 `react-window` 虚拟滚动：
 *    无论多少实例，同时存在的行数只与可视高度有关。
 *
 * ## 排序依据
 *
 * 一条稳定的全序，从上到下四组（同组内再按各自的键，最后统一按显示名兜底，保证确定性）：
 *
 * | 组 | 内容 | 为什么排这里 |
 * |---|---|---|
 * | 0 | 当前作用域 | 「我现在在哪」必须第一眼看到，否则切换器自己就成了误操作的来源 |
 * | 1 | 最近进入过的（`recentInstanceKeys`） | 唯一来自管理员自身行为的信号：几十个实例里他这几天真正在处理的就那几个 |
 * | 2 | 需处理：`ATTENTION_STATUSES` | 这些**要人动手**才会好（去认领、去扫码、去绑账号），排前面才不会被一长串列表埋掉 |
 * | 3 | 其余在跑的 | 按显示名，稳定可预期 |
 * | 4 | 已停用 | 停用是管理员自己的决定，不占前排 |
 *
 * **`offline` 有意不算「需处理」**：掉线通常是暂时的、会自愈，而一个大部分实例都离线的
 * 部署（开发环境就是）会让这一组吞掉整个列表，等于没有分组。真正的掉线告警有专门的
 * 健康监测页，不该由切换器兼职。
 *
 * 「有没有绑账号」也**不**在这里另判一次：那件事后端已经算成 `unbound` 这个状态了，
 * 而且它的判据里有前端拿不到的东西（原生适配器的账号列恒为空，不算未绑定）。
 * 详见 `needsAttention`。
 *
 * 搜索时**匹配质量优先于上述分组**（完全相等 > 前缀 > 包含）：输入一个完整 QQ 号时，
 * 那个实例必须排第一，哪怕它既不是常用也没出问题。
 *
 * ## 打开期间顺序冻结
 *
 * 排序输入里有会自己变的部分（实例状态来自轮询）。列表在打开的一瞬间快照一次，
 * 之后只有搜索词会改变可见集合 —— 否则一次后台刷新就能让行在指针底下换位置，
 * 而这个组件的每一次点击都会切走整个后台的作用域。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Box,
  Chip,
  CircularProgress,
  Divider,
  ListItemButton,
  Popover,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import {
  Check as CheckIcon,
  ExpandMore as ExpandMoreIcon,
  Public as PublicIcon,
  SmartToy as SmartToyIcon,
} from '@mui/icons-material'
import { alpha, type Theme } from '@mui/material/styles'
import { FixedSizeList, type ListChildComponentProps } from 'react-window'
import { useTranslation } from 'react-i18next'
import ActionButton from './ActionButton'
import SearchField from './SearchField'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { type InstanceOption } from '../../services/api/console'

/** 一行的高度（px）。虚拟滚动要求定高，两行文字 + 上下留白实测 54 刚好不裁切 */
const ROW_HEIGHT = 54
/** 列表可视区最大高度：6 行多一点，露出半行提示「下面还有」 */
const LIST_MAX_HEIGHT = ROW_HEIGHT * 6.5
const POPOVER_WIDTH = 340
/**
 * 弹层里列表**之外**那部分吃掉的垂直空间（含顶栏锚点与 Popover 自己的 16px 边距）
 *
 * 弹层的 Paper 被显式设成 `overflow: hidden`（为了裁掉圆角外的涟漪），也就是说它
 * **不会**像 MUI 默认那样自己滚动；而 MUI 只给它 `maxHeight: calc(100% - 32px)`。
 * 于是列表高度写死 351px 时，视口一矮（笔记本开着调试面板、移动端横屏）整个弹层
 * 底部就被静默裁掉且**无法滚到** —— 摘要行连同列表可视区的最后两行一起消失。
 * 改造前的 `Menu` 没这个问题（MUI 默认 `overflowY: auto`），所以这是一处回退。
 * 实测：视口高 460 时 Paper `scrollHeight=541 / clientHeight=426`，115px 够不着。
 */
const POPOVER_CHROME_HEIGHT = 300
/** 再挤也要留下的行数：少于两行就不是列表了 */
const MIN_VISIBLE_ROWS = 2

/**
 * 需要人动手才会恢复的状态
 *
 * 用 `Set<string>` 而不是联合类型字面量比较：`InstanceStatus` 是随后端演进的
 * （`unbound` 就是后加的），这里多认一个不存在的取值无害，漏认一个新增取值则会
 * 让它悄悄掉进「其余」组。
 */
const ATTENTION_STATUSES: ReadonlySet<string> = new Set([
  'error', // 运行异常
  'login_required', // 等人去扫码
  'unclaimed', // 协议端自己连进来了，等人认领
  'unbound', // 建了实例但从没绑过账号
])

type StatusTone = 'ok' | 'warn' | 'down' | 'idle'

const STATUS_TONES: Readonly<Record<string, StatusTone>> = {
  online: 'ok',
  connecting: 'warn',
  offline: 'down',
  error: 'down',
  login_required: 'warn',
  unclaimed: 'warn',
  unbound: 'warn',
  disabled: 'idle',
  archived: 'idle',
}

/** 状态点的颜色。未知状态按「要看一眼」处理，不装作一切正常 */
function statusTone(instance: InstanceOption): StatusTone {
  if (!instance.enabled) return 'idle'
  return STATUS_TONES[instance.status] ?? 'warn'
}

function toneColor(tone: StatusTone, theme: Theme): string {
  if (tone === 'ok') return theme.palette.success.main
  if (tone === 'down') return theme.palette.error.main
  if (tone === 'warn') return theme.palette.warning.main
  return theme.palette.text.disabled
}

/**
 * 该实例是否需要人动手处理
 *
 * **判据只有 `status` 一个来源。** 一度还叠了一条「启用着却没绑账号」（`enabled &&
 * !provider_account_id`），那是在前端重写一份后端已经做过的判断，而且做错了：
 * 「账号为空是不是异常」取决于协议端形态，后端 `instance_manager.account_binding_required()`
 * 明确规定**原生适配器（email / minecraft / sse）的 `provider_account_id` 恒为空**，
 * 判它们「未绑定账号」是"完全不适用的引导"。而 `InstanceOption` 刻意不带 `provider`，
 * 前端根本分辨不了 —— 结果是这类实例被永久钉进「需处理」组并计进角标，永远消不掉。
 *
 * 后端已经把这件事算成一个取值了：真正「建好了却从没绑过账号」的实例，
 * `resolve_instance_status()` 会给出 `unbound`，它就在下面这张表里。
 */
function needsAttention(instance: InstanceOption): boolean {
  return ATTENTION_STATUSES.has(instance.status)
}

/**
 * 一条搜索词与一个实例的匹配质量：0 完全相等 > 1 前缀 > 2 包含 > -1 不匹配
 *
 * 三个字段一视同仁（标识 / 显示名 / 账号），因为管理员记得住哪个是不确定的 ——
 * 但**完整命中优先于顺序**，输入一个完整 QQ 号时那个实例要排第一。
 */
function matchQuality(instance: InstanceOption, query: string): number {
  if (!query) return 0
  const fields = [instance.instance_key, instance.display_name, instance.provider_account_id]
  let best = -1
  for (const field of fields) {
    const value = field.toLowerCase()
    if (!value) continue
    if (value === query) return 0
    if (value.startsWith(query)) best = best < 0 ? 1 : Math.min(best, 1)
    else if (value.includes(query)) best = best < 0 ? 2 : best
  }
  return best
}

/** 排序分组，数字越小越靠前；含义见文件头的表 */
function rankGroup(
  instance: InstanceOption,
  currentInstanceKey: string | null,
  recentIndex: number
): number {
  if (instance.instance_key === currentInstanceKey) return 0
  if (recentIndex >= 0) return 1
  if (needsAttention(instance)) return 2
  if (!instance.enabled) return 4
  return 3
}

interface RankInstancesInput {
  instances: InstanceOption[]
  currentInstanceKey: string | null
  recentInstanceKeys: string[]
  /** 已 trim + 转小写的搜索词；空串表示不搜索 */
  query: string
  /** 只保留「需处理」的实例 */
  onlyAttention: boolean
}

/**
 * 过滤 + 排序，纯函数
 *
 * 抽出来是为了让「排序依据」是一段可以单独读、单独验的代码，而不是散在 JSX 里的比较符。
 */
function rankInstances({
  instances,
  currentInstanceKey,
  recentInstanceKeys,
  query,
  onlyAttention,
}: RankInstancesInput): InstanceOption[] {
  const recentRank = new Map(recentInstanceKeys.map((key, index) => [key, index]))

  return instances
    .map(instance => {
      const recentIndex = recentRank.get(instance.instance_key) ?? -1
      return {
        instance,
        quality: matchQuality(instance, query),
        group: rankGroup(instance, currentInstanceKey, recentIndex),
        recentIndex: recentIndex < 0 ? Number.MAX_SAFE_INTEGER : recentIndex,
      }
    })
    .filter(row => row.quality >= 0 && (!onlyAttention || needsAttention(row.instance)))
    .sort(
      (a, b) =>
        a.quality - b.quality ||
        a.group - b.group ||
        a.recentIndex - b.recentIndex ||
        (a.instance.display_name || a.instance.instance_key).localeCompare(
          b.instance.display_name || b.instance.instance_key,
          'zh-Hans-CN'
        ) ||
        a.instance.instance_key.localeCompare(b.instance.instance_key)
    )
    .map(row => row.instance)
}

interface InstanceRowData {
  rows: InstanceOption[]
  currentInstanceKey: string | null
  recentKeys: Set<string>
  activeIndex: number
  /** 全局视图行占掉的键盘序号，实例行的序号要往后挪 */
  indexOffset: number
  onSelect: (instanceKey: string) => void
  statusLabel: (instance: InstanceOption) => string
  recentLabel: string
  unboundLabel: string
}

function InstanceRow({ index, style, data }: ListChildComponentProps<InstanceRowData>) {
  const theme = useTheme()
  const instance = data.rows[index]
  const selected = data.currentInstanceKey === instance.instance_key
  const isActive = data.activeIndex === index + data.indexOffset
  const tone = statusTone(instance)
  // 「未绑定账号」同样只认后端算好的 `unbound`，不看 `provider_account_id` 是不是空串：
  // 原生适配器（email / minecraft / sse）的账号列**本来就恒为空**，
  // 按空串下这个结论会给它们贴一句永远不适用的告警文案（见 `needsAttention` 的注释）。
  const unbound = instance.status === 'unbound'
  const account = instance.provider_account_id

  return (
    <ListItemButton
      style={style}
      data-instance-row={instance.instance_key}
      role="option"
      aria-selected={selected}
      selected={selected}
      onClick={() => data.onSelect(instance.instance_key)}
      sx={{
        px: 1.5,
        gap: 1.25,
        alignItems: 'center',
        // 键盘高亮与 selected 是两件事：selected 表示"这是当前作用域"，
        // active 表示"回车会选中它"，同时出现时必须还能分辨
        ...(isActive ? { backgroundColor: alpha(theme.palette.primary.main, 0.14) } : {}),
      }}
    >
      <Box
        sx={{
          width: 10,
          height: 10,
          flexShrink: 0,
          borderRadius: '50%',
          bgcolor: toneColor(tone, theme),
        }}
      />
      <Box sx={{ minWidth: 0, flex: 1 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
          <Typography variant="body2" fontWeight={600} noWrap sx={{ minWidth: 0 }}>
            {instance.display_name || instance.instance_key}
          </Typography>
          {data.recentKeys.has(instance.instance_key) && (
            <Chip
              size="small"
              label={data.recentLabel}
              sx={{
                height: 16,
                flexShrink: 0,
                fontSize: '0.6rem',
                '& .MuiChip-label': { px: 0.5 },
                backgroundColor: alpha(theme.palette.primary.main, 0.12),
                color: theme.palette.primary.main,
              }}
            />
          )}
        </Box>
        <Typography
          variant="caption"
          noWrap
          component="div"
          sx={{ fontSize: '0.7rem', color: 'text.secondary' }}
        >
          {instance.instance_key}
          {/* 账号段没有内容时连分隔点一起省掉，否则原生适配器那一行会拖一个空的 " · " */}
          {(unbound || account) && ' · '}
          <Box
            component="span"
            sx={{ color: unbound ? 'warning.main' : 'text.secondary', fontWeight: unbound ? 600 : 400 }}
          >
            {unbound ? data.unboundLabel : account}
          </Box>
        </Typography>
      </Box>
      <Typography
        variant="caption"
        sx={{ flexShrink: 0, fontSize: '0.68rem', color: toneColor(tone, theme) }}
      >
        {data.statusLabel(instance)}
      </Typography>
      {selected && <CheckIcon fontSize="small" color="primary" sx={{ flexShrink: 0 }} />}
    </ListItemButton>
  )
}

export default function InstanceScopeSwitcher() {
  const theme = useTheme()
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  const { t } = useTranslation('instance-scope')
  const { t: tConsole } = useTranslation('console')
  const {
    instances,
    currentInstance,
    currentInstanceKey,
    isScopeSwitchable,
    isGlobalScopeAvailable,
    recentInstanceKeys,
    isLoading,
    setCurrentInstance,
  } = useInstanceScope()

  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null)
  const [search, setSearch] = useState('')
  const [onlyAttention, setOnlyAttention] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)
  // 打开瞬间的快照，见文件头「打开期间顺序冻结」
  const [frozen, setFrozen] = useState<{ instances: InstanceOption[]; recent: string[] } | null>(null)
  // 打开瞬间的视口高度，用来把列表压进弹层能真正显示出来的范围（见 POPOVER_CHROME_HEIGHT）
  const [viewportHeight, setViewportHeight] = useState(() =>
    typeof window === 'undefined' ? 900 : window.innerHeight
  )
  const listRef = useRef<FixedSizeList>(null)

  const open = Boolean(anchorEl)

  useEffect(() => {
    if (!open) {
      setFrozen(null)
      setSearch('')
      setOnlyAttention(false)
      setActiveIndex(0)
      return
    }
    setViewportHeight(window.innerHeight)
    // 打开时列表可能还在加载，此时先不冻结空列表，等数据到了再定格
    setFrozen(prev =>
      prev && prev.instances.length > 0 ? prev : { instances, recent: recentInstanceKeys }
    )
  }, [open, instances, recentInstanceKeys])

  const sourceInstances = frozen?.instances ?? instances
  const sourceRecent = frozen?.recent ?? recentInstanceKeys
  const recentKeys = useMemo(() => new Set(sourceRecent), [sourceRecent])

  const attentionCount = useMemo(
    () => sourceInstances.filter(needsAttention).length,
    [sourceInstances]
  )

  const rows = useMemo(
    () =>
      rankInstances({
        instances: sourceInstances,
        currentInstanceKey,
        recentInstanceKeys: sourceRecent,
        query: search.trim().toLowerCase(),
        onlyAttention,
      }),
    [sourceInstances, currentInstanceKey, sourceRecent, search, onlyAttention]
  )

  // 全局视图那一行只在平台方存在，并且搜索时要跟着一起被过滤掉，
  // 否则搜 "acme" 的结果里挂着一条与搜索词无关的行
  const globalLabel = t('scope.global')
  const showGlobalRow =
    isGlobalScopeAvailable &&
    !onlyAttention &&
    (!search.trim() || globalLabel.toLowerCase().includes(search.trim().toLowerCase()))
  const indexOffset = showGlobalRow ? 1 : 0
  const optionCount = rows.length + indexOffset

  // 列表高度：够用就按行数收缩，行多就用 6.5 行；但都不能超过弹层在当前视口里
  // 真正显示得出来的高度，否则底部被 `overflow: hidden` 静默裁掉且滚不到
  const listHeight = Math.max(
    ROW_HEIGHT * MIN_VISIBLE_ROWS,
    Math.min(
      rows.length * ROW_HEIGHT,
      LIST_MAX_HEIGHT,
      viewportHeight - POPOVER_CHROME_HEIGHT - indexOffset * ROW_HEIGHT
    )
  )

  const handleSelect = useCallback(
    (instanceKey: string | null) => {
      setCurrentInstance(instanceKey)
      setAnchorEl(null)
    },
    [setCurrentInstance]
  )

  // 搜索词变化会改变可见集合，高亮必须回到第一条，否则回车会选中一个看不见的行
  useEffect(() => {
    setActiveIndex(0)
  }, [search, onlyAttention])

  useEffect(() => {
    const listIndex = activeIndex - indexOffset
    if (listIndex >= 0) {
      listRef.current?.scrollToItem(listIndex, 'smart')
    }
  }, [activeIndex, indexOffset])

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (optionCount === 0) return
    // 输入法组词途中的方向键与回车是**输入法的**：中文拼音下 ↑↓ 翻候选、回车上屏。
    // 不挡的话，管理员只是想按显示名搜一个 Bot，敲完候选按下回车，
    // 整个后台的作用域就被切到当时高亮的那一行上了（组件抬头写着"每一次点击都会切走
    // 整个后台的作用域"，误触的代价正是这个）。搜索框本身已经按同一条理由处理了
    // composition（见 `SearchField`），键盘导航必须跟上，否则只补了一半。
    // `keyCode === 229` 是 Safari / 部分旧内核不设 `isComposing` 时的等价信号。
    const native = event.nativeEvent
    if (native.isComposing || native.keyCode === 229) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex(index => (index + 1) % optionCount)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex(index => (index - 1 + optionCount) % optionCount)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      if (showGlobalRow && activeIndex === 0) {
        handleSelect(null)
        return
      }
      const target = rows[activeIndex - indexOffset]
      if (target) {
        handleSelect(target.instance_key)
      }
    }
  }

  // 状态文案复用总后台实例列表那一套（`console:status.*`），不另起一份译名。
  // `defaultValue` 是后端加了新状态、译名还没跟上时的兜底：宁可显示原始取值，
  // 也不要在切换器里显示一行 "status.xxx"。
  const statusLabel = useCallback(
    (instance: InstanceOption) =>
      instance.enabled
        ? tConsole(`status.${instance.status}`, { defaultValue: instance.status })
        : tConsole('status.disabled', { defaultValue: 'disabled' }),
    [tConsole]
  )

  const rowData: InstanceRowData = {
    rows,
    currentInstanceKey,
    recentKeys,
    activeIndex,
    indexOffset,
    onSelect: handleSelect,
    statusLabel,
    recentLabel: t('switcher.recentBadge'),
    unboundLabel: t('switcher.unboundAccount'),
  }

  // 单实例部署：整个切换器不渲染，各页面行为与改造前一致
  if (!isScopeSwitchable) {
    return null
  }

  const scopeLabel = currentInstance
    ? currentInstance.display_name || currentInstance.instance_key
    : globalLabel

  return (
    <>
      <ActionButton
        tone="ghost"
        variant="text"
        size={isSmall ? 'small' : 'medium'}
        onClick={event => setAnchorEl(event.currentTarget)}
        endIcon={<ExpandMoreIcon />}
        aria-label={t('switcher.ariaLabel')}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="normal-case"
        sx={{
          mr: { xs: 0, sm: 0.5 },
          minWidth: 0,
          px: { xs: 0.75, sm: 1 },
          color: 'white',
          border: 'none',
          boxShadow: 'none',
          backgroundColor: 'transparent',
          '&:hover': {
            backgroundColor: alpha(theme.palette.common.white, 0.12),
          },
        }}
      >
        {currentInstanceKey ? (
          // 处于实例作用域时给出强视觉标识，避免误以为在全局视图下操作
          <Chip
            size="small"
            icon={<SmartToyIcon sx={{ fontSize: 14 }} />}
            label={scopeLabel}
            sx={{
              maxWidth: { xs: 96, sm: 180 },
              height: 22,
              fontWeight: 700,
              color: theme.palette.common.white,
              backgroundColor: alpha(theme.palette.secondary.main, 0.9),
              '& .MuiChip-icon': { color: theme.palette.common.white },
              cursor: 'pointer',
            }}
          />
        ) : (
          <Box className="flex items-center gap-1">
            <PublicIcon sx={{ fontSize: 18 }} />
            <Box sx={{ display: { xs: 'none', sm: 'block' } }}>{scopeLabel}</Box>
          </Box>
        )}
      </ActionButton>

      <Popover
        open={open}
        anchorEl={anchorEl}
        onClose={() => setAnchorEl(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
        slotProps={{ paper: { sx: { width: POPOVER_WIDTH, maxWidth: '96vw', overflow: 'hidden' } } }}
      >
        <Box onKeyDown={handleKeyDown} sx={{ display: 'flex', flexDirection: 'column' }}>
          <Box sx={{ px: 1.5, pt: 1.25, pb: 1 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.75 }}>
              {t('switcher.title')}
            </Typography>
            <SearchField
              autoFocus
              fullWidth
              value={search}
              onChange={setSearch}
              onClear={() => setSearch('')}
              clearAriaLabel={t('switcher.clearSearch')}
              placeholder={t('switcher.searchPlaceholder')}
              inputProps={{
                'aria-label': t('switcher.searchPlaceholder'),
                'aria-controls': 'instance-scope-listbox',
              }}
            />
            {attentionCount > 0 && (
              <Box sx={{ display: 'flex', gap: 0.75, mt: 0.75 }}>
                <Chip
                  size="small"
                  clickable
                  variant={onlyAttention ? 'filled' : 'outlined'}
                  onClick={() => setOnlyAttention(value => !value)}
                  label={t('switcher.attentionFilter', { attention: attentionCount })}
                  sx={{
                    height: 20,
                    fontSize: '0.65rem',
                    ...(onlyAttention
                      ? {
                          backgroundColor: alpha(theme.palette.warning.main, 0.18),
                          color: theme.palette.warning.main,
                        }
                      : { borderColor: alpha(theme.palette.warning.main, 0.4), color: 'text.secondary' }),
                  }}
                />
              </Box>
            )}
          </Box>

          <Divider />

          <Box id="instance-scope-listbox" role="listbox" aria-label={t('switcher.listAriaLabel')}>
            {showGlobalRow && (
              <ListItemButton
                role="option"
                aria-selected={!currentInstanceKey}
                selected={!currentInstanceKey}
                onClick={() => handleSelect(null)}
                data-global-row="1"
                sx={{
                  px: 1.5,
                  gap: 1.25,
                  height: ROW_HEIGHT,
                  ...(activeIndex === 0 && showGlobalRow
                    ? { backgroundColor: alpha(theme.palette.primary.main, 0.14) }
                    : {}),
                }}
              >
                <PublicIcon fontSize="small" sx={{ color: 'text.secondary' }} />
                <Box sx={{ minWidth: 0, flex: 1 }}>
                  <Typography variant="body2" fontWeight={600} noWrap>
                    {globalLabel}
                  </Typography>
                  <Typography
                    variant="caption"
                    component="div"
                    noWrap
                    sx={{ fontSize: '0.7rem', color: 'text.secondary' }}
                  >
                    {t('scope.globalHint')}
                  </Typography>
                </Box>
                {!currentInstanceKey && <CheckIcon fontSize="small" color="primary" />}
              </ListItemButton>
            )}

            {isLoading && sourceInstances.length === 0 ? (
              <Box className="flex justify-center py-3">
                <CircularProgress size={20} />
              </Box>
            ) : rows.length === 0 ? (
              <Box sx={{ px: 2, py: 2.5, textAlign: 'center' }}>
                <Typography variant="body2" fontWeight={600}>
                  {t('switcher.noResults')}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {t('switcher.noResultsHint')}
                </Typography>
              </Box>
            ) : (
              <FixedSizeList
                ref={listRef}
                height={listHeight}
                width="100%"
                itemCount={rows.length}
                itemSize={ROW_HEIGHT}
                itemKey={index => rows[index].instance_key}
                itemData={rowData}
                overscanCount={2}
              >
                {InstanceRow}
              </FixedSizeList>
            )}
          </Box>

          <Divider />
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{ px: 1.5, py: 0.75, display: 'block' }}
          >
            {t('switcher.summary', { shown: rows.length, total: sourceInstances.length })}
          </Typography>
        </Box>
      </Popover>
    </>
  )
}
