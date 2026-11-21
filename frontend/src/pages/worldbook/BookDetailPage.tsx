import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Divider,
  Menu,
  MenuItem,
  Stack,
  Typography,
  useTheme,
} from '@mui/material'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import AddIcon from '@mui/icons-material/Add'
import ArrowBackIcon from '@mui/icons-material/ArrowBack'
import MoreHorizRoundedIcon from '@mui/icons-material/MoreHorizRounded'
import WarningAmberRoundedIcon from '@mui/icons-material/WarningAmberRounded'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import SearchField from '../../components/common/SearchField'
import StatusChip from '../../components/common/StatusChip'
import ServerDataTable, {
  type ServerDataTableColumn,
} from '../../components/common/ServerDataTable'
import { ThemedTooltip } from '../../components/common/ThemedTooltip'
import {
  WorldbookEnabledChip,
  WorldbookReadOnlyChip,
  WorldbookScopeChip,
  WorldbookStatusChip,
  WorldbookTrustIcon,
} from '../../components/worldbook/chips'
import {
  worldbookBindingMissingText,
  worldbookBindingText,
  worldbookPositionText,
} from '../../components/worldbook/display'
import BookFormDialog from '../../components/worldbook/BookFormDialog'
import EntryDialog from '../../components/worldbook/EntryDialog'
import ImportDialog from '../../components/worldbook/ImportDialog'
import WorldbookNotFoundState from '../../components/worldbook/WorldbookNotFoundState'
import { truncateText } from '../../components/memory/format'
import { memoryErrorMessage } from '../../components/memory/errorMessage'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { useNotification } from '../../hooks/useNotification'
import { worldbookPath } from '../../router/routes'
import { isNotFoundError } from '../../services/api/axios'
import {
  WORLDBOOK_STATE_ONLY_PURPOSES,
  worldbookApi,
  type BookBudgetCheck,
  type WorldbookEntryAction,
  type WorldbookEntryItem,
  type WorldbookScopeParams,
} from '../../services/api/worldbook'
import { CHIP_VARIANTS } from '../../theme/variants'

/** 一本书的条目动辄上百条，按 20 条翻页太碎；这张表每页 50 条 */
const ENTRY_PAGE_SIZE = 50

// 搜索框停手多久再发请求
const SEARCH_DEBOUNCE_MS = 400
// 有条目标着「分析中」时隔多久再取一次：后端每次请求只分析一部分，下次接着算
const PENDING_REFETCH_MS = 3000

/**
 * 书详情
 *
 * 条目分页、搜索都在服务端做：导入的卡片书可能有几百上千条大条目，一次取全连同用途分析
 * 会拖慢请求。书的增删改与条目动作改完统一 invalidate 以 `worldbook` 开头的查询。
 */

export default function BookDetailPage() {
  const theme = useTheme()
  const { t } = useTranslation('worldbook')
  const { t: tCommon } = useTranslation('common')
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const notification = useNotification()
  const { bookId: bookIdParam } = useParams<{ bookId: string }>()
  const { currentInstanceKey, currentInstance, isScopeReady } = useInstanceScope()

  const parsedBookId = Number(bookIdParam)
  const bookId =
    Number.isInteger(parsedBookId) && parsedBookId > 0 ? parsedBookId : null

  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [page, setPage] = useState(0)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [entryDialogOpen, setEntryDialogOpen] = useState(false)
  const [entryTarget, setEntryTarget] = useState<WorldbookEntryItem | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [deleteBookOpen, setDeleteBookOpen] = useState(false)
  const [deleteEntry, setDeleteEntryTarget] = useState<WorldbookEntryItem | null>(null)
  const [viewEntry, setViewEntry] = useState<WorldbookEntryItem | null>(null)
  const [headerMenuAnchor, setHeaderMenuAnchor] = useState<HTMLElement | null>(null)
  const [entryMenu, setEntryMenu] = useState<{
    anchor: HTMLElement
    entry: WorldbookEntryItem
  } | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  // 同一条地址参数只处理一次：通知对象每次渲染都是新的，不能靠依赖数组防重复
  const handledLinkedEntryRef = useRef<number | null>(null)
  const handledLinkedErrorRef = useRef<number | null>(null)

  const parsedEntryId = Number(searchParams.get('entry'))
  const linkedEntryId =
    Number.isInteger(parsedEntryId) && parsedEntryId > 0 ? parsedEntryId : null

  const closeLinkedEntry = useCallback(() => {
    if (!searchParams.has('entry')) return
    const next = new URLSearchParams(searchParams)
    next.delete('entry')
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])

  const scope: WorldbookScopeParams = useMemo(
    () => ({
      instance_key: currentInstanceKey ?? undefined,
      adapter_key: currentInstance?.adapter_key,
    }),
    [currentInstanceKey, currentInstance?.adapter_key]
  )

  const bookQuery = useQuery({
    queryKey: ['worldbook', 'book', bookId],
    queryFn: () => worldbookApi.getBook(bookId as number),
    enabled: bookId !== null && isScopeReady,
    retry: false,
  })

  // 图谱里「在书里打开这一条」带 `?entry=` 回来：先按 ID 取这一条，再打开书里原有的对话框。
  // 查询等书本身加载完再发，只读开放的平台方书也能用（服务端按书判可见性）。
  const linkedEntryQuery = useQuery({
    queryKey: ['worldbook', 'linked-entry', linkedEntryId],
    queryFn: () => worldbookApi.getEntry(linkedEntryId as number),
    enabled: linkedEntryId !== null && isScopeReady && Boolean(bookQuery.data),
    retry: false,
  })

  useEffect(() => {
    if (linkedEntryId === null) {
      handledLinkedEntryRef.current = null
      handledLinkedErrorRef.current = null
      return
    }
    if (!linkedEntryQuery.data || handledLinkedEntryRef.current === linkedEntryId) return
    const entry = linkedEntryQuery.data
    if (entry.book_id !== bookId) {
      handledLinkedEntryRef.current = linkedEntryId
      notification.error(t('graph.entryWrongBook'))
      closeLinkedEntry()
      return
    }
    handledLinkedEntryRef.current = linkedEntryId
    if (bookQuery.data?.read_only) {
      setViewEntry(entry)
    } else {
      setEntryTarget(entry)
      setEntryDialogOpen(true)
    }
  }, [bookId, bookQuery.data?.read_only, closeLinkedEntry, linkedEntryId, linkedEntryQuery.data, notification, t])

  useEffect(() => {
    if (linkedEntryId === null || !linkedEntryQuery.isError) return
    if (handledLinkedErrorRef.current === linkedEntryId) return
    handledLinkedErrorRef.current = linkedEntryId
    notification.error(
      isNotFoundError(linkedEntryQuery.error)
        ? t('graph.entryNotFound')
        : tCommon('messages.operationFailed')
    )
    closeLinkedEntry()
  }, [closeLinkedEntry, linkedEntryId, linkedEntryQuery.error, linkedEntryQuery.isError, notification, t, tCommon])

  // 预算体检单独拉：常驻条目多的书要数一阵 token，不能拖慢书详情本身；失败就当没有结果
  const budgetQuery = useQuery({
    queryKey: ['worldbook', 'budget-check', bookId],
    queryFn: () => worldbookApi.getBookBudgetCheck(bookId as number),
    enabled: bookId !== null && isScopeReady && Boolean(bookQuery.data),
    retry: false,
  })

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSearch(search.trim())
      setPage(0)
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [search])

  const entriesQuery = useQuery({
    queryKey: ['worldbook', 'entries', bookId, page, debouncedSearch],
    queryFn: () =>
      worldbookApi.listEntries(bookId as number, {
        page: page + 1,
        page_size: ENTRY_PAGE_SIZE,
        q: debouncedSearch || undefined,
      }),
    enabled: bookId !== null && isScopeReady,
    retry: false,
    placeholderData: keepPreviousData,
    refetchInterval: query =>
      query.state.data?.items.some(item => item.analysis_pending) ? PENDING_REFETCH_MS : false,
  })

  // 删掉最后一页的最后一条后，页码可能越过末页：退回到末页
  const entryTotal = entriesQuery.data?.total
  useEffect(() => {
    if (entryTotal === undefined) return
    const lastPage = Math.max(0, Math.ceil(entryTotal / ENTRY_PAGE_SIZE) - 1)
    if (page > lastPage) setPage(lastPage)
  }, [entryTotal, page])

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['worldbook'] })
  }

  const exportMutation = useMutation({
    mutationFn: () => worldbookApi.exportBook(bookId as number),
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: t('detail.exportFailed'),
        })
      ),
  })

  const deleteBookMutation = useMutation({
    mutationFn: () => worldbookApi.deleteBook(bookId as number),
    onSuccess: () => {
      notification.success(t('detail.deleteSuccess'))
      setDeleteBookOpen(false)
      invalidate()
      navigate(worldbookPath('books'))
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const actionMutation = useMutation({
    mutationFn: (vars: { entryId: number; action: WorldbookEntryAction }) =>
      worldbookApi.entryAction(vars.entryId, vars.action),
    onSuccess: (_entry, vars) => {
      notification.success(t(`detail.actionSuccess.${vars.action}`))
      invalidate()
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const deleteEntryMutation = useMutation({
    mutationFn: (entryId: number) => worldbookApi.deleteEntry(entryId),
    onSuccess: () => {
      notification.success(t('detail.entryDeleteSuccess'))
      setDeleteEntryTarget(null)
      invalidate()
    },
    onError: (error: unknown) =>
      notification.error(
        memoryErrorMessage(error, {
          notFound: t('errors.notFoundHint'),
          fallback: tCommon('messages.operationFailed'),
        })
      ),
  })

  const book = bookQuery.data

  const entries = entriesQuery.data?.items ?? []

  if (bookId === null) {
    return <Navigate to={worldbookPath()} replace />
  }

  if (bookQuery.isPending || !isScopeReady) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
        <CircularProgress size={28} />
      </Box>
    )
  }

  if (isNotFoundError(bookQuery.error)) {
    return (
      <Box sx={{ p: 2 }}>
        <WorldbookNotFoundState />
        <Box sx={{ display: 'flex', justifyContent: 'center' }}>
          <ActionButton tone="secondary" onClick={() => navigate(worldbookPath('books'))}>
            {t('detail.back')}
          </ActionButton>
        </Box>
      </Box>
    )
  }

  if (bookQuery.error) {
    return (
      <QueryErrorState
        error={bookQuery.error}
        onRetry={() => void bookQuery.refetch()}
      />
    )
  }

  if (!book) {
    return (
      <Box sx={{ p: 2 }}>
        <WorldbookNotFoundState />
        <Box sx={{ display: 'flex', justifyContent: 'center' }}>
          <ActionButton tone="secondary" onClick={() => navigate(worldbookPath('books'))}>
            {t('detail.back')}
          </ActionButton>
        </Box>
      </Box>
    )
  }

  // 平台方给全局人设建、对当前租户只读开放的书：只给查看，不给任何写操作的入口
  const readOnly = Boolean(book.read_only)

  const secondaryLogic = (entry: WorldbookEntryItem): string => {
    const keys = ['and_any', 'and_all', 'not_any', 'not_all']
    return keys.includes(entry.selective_logic)
      ? t(`entryDialog.logic.${entry.selective_logic}`)
      : entry.selective_logic
  }

  // 运行时按用途路由的结果（后端现算）：管理员要一眼看出哪些条目不会进主提示词、为什么
  const renderPurposeChips = (entry: WorldbookEntryItem) => {
    const chips: JSX.Element[] = []
    if (entry.analysis_pending) {
      chips.push(
        <ThemedTooltip key="pending" title={t('detail.analysisPendingHint')}>
          <Chip
            size="small"
            label={t('detail.analysisPending')}
            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.text.secondary, true)}
          />
        </ThemedTooltip>
      )
      return chips
    }
    if (WORLDBOOK_STATE_ONLY_PURPOSES.includes(entry.purpose)) {
      chips.push(
        <ThemedTooltip
          key="purpose"
          title={t('detail.purposeHint.stateOnly', {
            kind: t(`detail.purposeKind.${entry.purpose}`, { defaultValue: entry.purpose }),
          })}
        >
          <Chip
            size="small"
            label={t('detail.purpose.stateOnly')}
            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.text.secondary, true)}
          />
        </ThemedTooltip>
      )
    } else if (entry.purpose === 'unsupported_template') {
      chips.push(
        <ThemedTooltip
          key="purpose"
          title={t('detail.purposeHint.unsupported', { reason: entry.template_note })}
        >
          <Chip
            size="small"
            label={t('detail.purpose.unsupported')}
            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.warning.main, true)}
          />
        </ThemedTooltip>
      )
    } else if (entry.purpose === 'mvu_plot') {
      chips.push(
        <ThemedTooltip key="purpose" title={t('detail.purposeHint.plot')}>
          <Chip
            size="small"
            label={t('detail.purpose.plot')}
            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true)}
          />
        </ThemedTooltip>
      )
    }
    if (entry.suspected_trap) {
      chips.push(
        <ThemedTooltip key="trap" title={t('detail.trapHint', { reason: entry.trap_reason })}>
          <Chip
            size="small"
            label={t('detail.trap')}
            sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.error.main, true)}
          />
        </ThemedTooltip>
      )
    }
    return chips
  }

  const renderTitle = (entry: WorldbookEntryItem) => {
    const title = entry.comment || truncateText(entry.content, 40) || `#${entry.id}`
    const hasWarnings =
      entry.invalid_keys.length > 0 || entry.slow_disabled_keys.length > 0
    const chips = renderPurposeChips(entry)
    if (!hasWarnings && chips.length === 0) {
      return <Typography variant="body2">{title}</Typography>
    }
    const lines = [
      ...entry.invalid_keys.map(item =>
        t('detail.invalidKeys', { key: item.key, reason: item.reason })
      ),
      ...entry.slow_disabled_keys.map(key => t('detail.slowKeys', { key })),
    ]
    return (
      <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center', flexWrap: 'wrap', rowGap: 0.5 }}>
        <Typography variant="body2">{title}</Typography>
        {hasWarnings && (
          <ThemedTooltip title={lines.join('\n')}>
            <WarningAmberRoundedIcon color="warning" sx={{ fontSize: 16 }} />
          </ThemedTooltip>
        )}
        {chips}
      </Stack>
    )
  }

  const renderKeys = (entry: WorldbookEntryItem) => {
    if (entry.constant) {
      return (
        <Chip
          size="small"
          label={t('entryDialog.constant')}
          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, true)}
        />
      )
    }
    const shown = entry.keys.slice(0, 3).join('、')
    const more = entry.keys.length > 3 ? t('detail.keywordsMore', { count: entry.keys.length }) : ''
    return (
      <Box>
        <Typography variant="body2">
          {shown}
          {more ? ` ${more}` : ''}
        </Typography>
        {entry.secondary_keys.length > 0 && (
          <Typography variant="caption" color="text.secondary" display="block">
            {t('detail.secondary', {
              logic: secondaryLogic(entry),
              count: entry.secondary_keys.length,
            })}
          </Typography>
        )}
      </Box>
    )
  }

  /**
   * 预算体检的提醒
   *
   * 只在书上真有常驻条目、且后端给出了体检结果时出现：没有结果（预算读不到、只读书）
   * 说明不了任何事，宁可不显示。
   */
  const renderBudgetCheck = (check: BookBudgetCheck | undefined) => {
    if (check?.busy) {
      // 同一个账号下另一本书的体检还没算完：后端不排队，直接说一声
      return (
        <Alert severity="info">
          <Typography variant="body2">{check.summary}</Typography>
        </Alert>
      )
    }
    if (!check || check.budget === null || check.constant_count <= 0) return null
    // `summary` 已经分开点名了「单条就超预算」与「只有常驻条目命中时进不来」的条目，这里不再另列一遍
    return (
      <Alert severity={check.dropped.length > 0 ? 'warning' : 'info'}>
        <Typography variant="body2">{check.summary}</Typography>
        {check.truncated ? (
          <Typography variant="caption" display="block" sx={{ mt: 0.5 }}>
            {t('detail.budgetCheck.truncated')}
          </Typography>
        ) : null}
      </Alert>
    )
  }

  const renderStatus = (entry: WorldbookEntryItem) =>
    entry.status === 'quarantined' && entry.quarantine_reason === 'auto_blocked' ? (
      <ThemedTooltip title={t('detail.autoQuarantined', { count: entry.blocked_round_hits })}>
        <Box component="span">
          <WorldbookStatusChip status={entry.status} enabled={entry.enabled} />
        </Box>
      </ThemedTooltip>
    ) : (
      <WorldbookStatusChip status={entry.status} enabled={entry.enabled} />
    )

  // 与「书」标签的书目表同一套表格：纸面容器、表头底色、行斑马纹与悬停、固定列宽
  const entryColumns: ServerDataTableColumn<WorldbookEntryItem>[] = [
    // 标题列不给宽度：固定布局下由它吃掉其余列剩下的空间
    { key: 'title', header: t('detail.columns.title'), render: renderTitle },
    { key: 'keys', header: t('detail.columns.keys'), width: 220, render: renderKeys },
    {
      key: 'position',
      header: t('detail.columns.positionOrder'),
      width: 150,
      hideOnMobile: true,
      render: entry => (
        <Typography variant="body2" sx={{ whiteSpace: 'nowrap' }}>
          {`${worldbookPositionText(entry.position, entry.depth, t)} · ${entry.order}`}
        </Typography>
      ),
    },
    { key: 'status', header: t('detail.columns.status'), width: 100, render: renderStatus },
    {
      key: 'trust',
      header: t('detail.columns.trust'),
      width: 72,
      align: 'center',
      hideOnMobile: true,
      render: entry => <WorldbookTrustIcon trusted={entry.trusted} />,
    },
    {
      key: 'actions',
      header: t('detail.columns.actions'),
      width: 64,
      align: 'center',
      render: entry => (
        <ThemedTooltip title={t('detail.moreActions')}>
          <IconActionButton
            tone="subtle"
            size="small"
            aria-label={t('detail.moreActions')}
            onClick={event => {
              event.stopPropagation()
              setEntryMenu({ anchor: event.currentTarget, entry })
            }}
          >
            <MoreHorizRoundedIcon fontSize="small" />
          </IconActionButton>
        </ThemedTooltip>
      ),
    },
  ]

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 3 },
        height: '100%',
        minHeight: 0,
        overflowY: 'auto',
        boxSizing: 'border-box',
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
      }}
    >
      {/* 表格以上的部分按内容定高、不参与收缩：页面是定高的滚动列，条目一多，
          flex 子项会被压扁，预算横幅就只剩半行（横幅正文自带 overflow） */}
      <Stack spacing={2} sx={{ flexShrink: 0 }}>
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, flexWrap: 'wrap' }}>
        <ThemedTooltip title={t('detail.back')}>
          <IconActionButton
            tone="subtle"
            size="small"
            aria-label={t('detail.back')}
            onClick={() => navigate(worldbookPath('books'))}
          >
            <ArrowBackIcon fontSize="small" />
          </IconActionButton>
        </ThemedTooltip>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="h6" fontWeight={700} sx={{ wordBreak: 'break-word' }}>
            {book.name}
          </Typography>
          <Box
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 0.75,
              flexWrap: 'wrap',
              mt: 0.5,
            }}
          >
            <WorldbookScopeChip scopeKind={book.scope_kind} />
            {worldbookBindingText(book) && (
              <Typography variant="caption" color="text.secondary">
                {worldbookBindingText(book)}
              </Typography>
            )}
            <Typography variant="caption" color="text.secondary">
              {book.instance_key || t('common.globalPreset')}
            </Typography>
            <WorldbookEnabledChip enabled={book.enabled} />
            {book.vector_participating > 0 && (
              <StatusChip
                tone="active"
                label={t('detail.semanticParticipating', { count: book.vector_participating })}
              />
            )}
            {book.vector_pending > 0 && (
              <StatusChip
                tone="paused"
                label={t('detail.semanticPending', { count: book.vector_pending })}
              />
            )}
            {readOnly && <WorldbookReadOnlyChip />}
          </Box>
          {book.binding_missing && (
            <Typography variant="caption" display="block" color="warning.main" sx={{ mt: 0.5 }}>
              {worldbookBindingMissingText(book.scope_kind, t)}
            </Typography>
          )}
          {book.description && (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              {book.description}
            </Typography>
          )}
        </Box>

        {/* 窄屏占满一行再换行；不能写 flexShrink: 0，那会让按钮组按单行宽度撑开页面。
            平台方提供的只读书不给任何写操作的入口（后端照样拒，这里只是不让人点了才知道） */}
        {!readOnly && (
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', width: { xs: '100%', sm: 'auto' } }}>
          <ActionButton
            tone="primary"
            size="small"
            startIcon={<AddIcon />}
            onClick={() => {
              setEntryTarget(null)
              setEntryDialogOpen(true)
            }}
          >
            {t('detail.newEntry')}
          </ActionButton>
          <IconActionButton
            aria-label={t('detail.moreActions')}
            title={t('detail.moreActions')}
            onClick={event => setHeaderMenuAnchor(event.currentTarget)}
          >
            <MoreHorizRoundedIcon />
          </IconActionButton>
        </Box>
        )}
      </Box>

      <Menu
        anchorEl={headerMenuAnchor}
        open={headerMenuAnchor !== null}
        onClose={() => setHeaderMenuAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        <MenuItem
          onClick={() => {
            setHeaderMenuAnchor(null)
            setSettingsOpen(true)
          }}
        >
          {t('detail.settings')}
        </MenuItem>
        <MenuItem
          onClick={() => {
            setHeaderMenuAnchor(null)
            setImportOpen(true)
          }}
        >
          {t('detail.appendImport')}
        </MenuItem>
        <MenuItem
          disabled={exportMutation.isPending}
          onClick={() => {
            setHeaderMenuAnchor(null)
            exportMutation.mutate()
          }}
        >
          {t('detail.export')}
        </MenuItem>
        <Divider />
        <MenuItem
          sx={{ color: 'error.main' }}
          onClick={() => {
            setHeaderMenuAnchor(null)
            setDeleteBookOpen(true)
          }}
        >
          {t('detail.delete')}
        </MenuItem>
      </Menu>

      {readOnly && <Alert severity="info">{t('readOnly.hint')}</Alert>}

      {renderBudgetCheck(budgetQuery.data)}

      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
        <Typography variant="subtitle2" fontWeight={700}>
          {t('detail.entriesTitle')}
        </Typography>
        <Box sx={{ width: { xs: '100%', sm: 320 }, ml: { sm: 'auto' } }}>
          <SearchField
            placeholder={t('detail.searchPlaceholder')}
            value={search}
            onChange={setSearch}
            onClear={() => setSearch('')}
          />
        </Box>
      </Box>
      </Stack>

      {/* 桌面端吃满剩余高度、表内滚动（表头吸顶）；剩余高度不够时至少留一屏表格，整页滚 */}
      <Box sx={{ flex: 1, minHeight: { xs: 'auto', md: 360 }, display: 'flex', flexDirection: 'column' }}>
        <ServerDataTable
          columns={entryColumns}
          rows={entries}
          getRowId={entry => entry.id}
          total={entriesQuery.data?.total ?? 0}
          page={page + 1}
          pageSize={ENTRY_PAGE_SIZE}
          onPageChange={nextPage => setPage(nextPage - 1)}
          loading={entriesQuery.isPending}
          fetching={entriesQuery.isFetching}
          error={entriesQuery.error}
          onRetry={() => void entriesQuery.refetch()}
          renderError={error =>
            isNotFoundError(error) ? (
              <WorldbookNotFoundState compact />
            ) : (
              <QueryErrorState error={error} onRetry={() => void entriesQuery.refetch()} compact />
            )
          }
          emptyTitle={debouncedSearch ? t('detail.searchEmpty') : t('detail.entriesEmpty')}
          // 点行即打开：只读书看详情，其余直接编辑；「⋯」里放其余动作
          onRowClick={entry => {
            if (readOnly) {
              setViewEntry(entry)
              return
            }
            setEntryTarget(entry)
            setEntryDialogOpen(true)
          }}
        />
      </Box>

      <Menu
        anchorEl={entryMenu?.anchor ?? null}
        open={entryMenu !== null}
        onClose={() => setEntryMenu(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        {entryMenu && (
          <MenuItem
            onClick={() => {
              setViewEntry(entryMenu.entry)
              setEntryMenu(null)
            }}
          >
            {t('detail.actions.view')}
          </MenuItem>
        )}
        {entryMenu && !readOnly && (
          <MenuItem
            onClick={() => {
              setEntryTarget(entryMenu.entry)
              setEntryDialogOpen(true)
              setEntryMenu(null)
            }}
          >
            {t('detail.actions.edit')}
          </MenuItem>
        )}
        {entryMenu && !readOnly && (
          <MenuItem
            disabled={actionMutation.isPending}
            onClick={() => {
              const entry = entryMenu.entry
              setEntryMenu(null)
              actionMutation.mutate({
                entryId: entry.id,
                action: entry.enabled ? 'disable' : 'enable',
              })
            }}
          >
            {entryMenu.entry.enabled
              ? t('detail.actions.disable')
              : t('detail.actions.enable')}
          </MenuItem>
        )}
        {entryMenu && !readOnly && (
          <MenuItem
            disabled={actionMutation.isPending}
            onClick={() => {
              const entry = entryMenu.entry
              setEntryMenu(null)
              actionMutation.mutate({
                entryId: entry.id,
                action: entry.status === 'quarantined' ? 'restore' : 'quarantine',
              })
            }}
          >
            {entryMenu.entry.status === 'quarantined'
              ? t('detail.actions.restore')
              : t('detail.actions.quarantine')}
          </MenuItem>
        )}
        {entryMenu && !readOnly && <Divider />}
        {entryMenu && !readOnly && (
          <MenuItem
            sx={{ color: 'error.main' }}
            onClick={() => {
              setDeleteEntryTarget(entryMenu.entry)
              setEntryMenu(null)
            }}
          >
            {t('detail.actions.delete')}
          </MenuItem>
        )}
      </Menu>

      {viewEntry && (
        <NekroDialog
          open
          onClose={() => {
            setViewEntry(null)
            closeLinkedEntry()
          }}
          title={viewEntry.comment || `#${viewEntry.id}`}
          maxWidth="md"
        >
          <Stack spacing={1.5}>
            <Box>
              <Typography variant="caption" color="text.secondary" display="block">
                {t('readOnly.keys')}
              </Typography>
              <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>
                {viewEntry.constant
                  ? t('entryDialog.constant')
                  : viewEntry.keys.join('、') || t('common.empty')}
              </Typography>
            </Box>
            {viewEntry.secondary_keys.length > 0 && (
              <Box>
                <Typography variant="caption" color="text.secondary" display="block">
                  {t('readOnly.secondaryKeys', { logic: secondaryLogic(viewEntry) })}
                </Typography>
                <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>
                  {viewEntry.secondary_keys.join('、')}
                </Typography>
              </Box>
            )}
            <Box>
              <Typography variant="caption" color="text.secondary" display="block">
                {t('readOnly.content')}
              </Typography>
              {/* 纯文本显示：条目正文可能来自导入的文件，不按 HTML / Markdown 渲染 */}
              <Box
                component="pre"
                sx={{
                  m: 0,
                  p: 1.5,
                  borderRadius: 1,
                  bgcolor: 'action.hover',
                  fontFamily: 'inherit',
                  fontSize: '0.875rem',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  maxHeight: '50vh',
                  overflowY: 'auto',
                }}
              >
                {viewEntry.content || t('common.empty')}
              </Box>
            </Box>
          </Stack>
        </NekroDialog>
      )}

      {settingsOpen && (
        <BookFormDialog
          open
          mode="edit"
          scope={scope}
          book={book}
          onClose={() => setSettingsOpen(false)}
          onUpdated={() => invalidate()}
        />
      )}

      {entryDialogOpen && (
        <EntryDialog
          key={entryTarget?.id ?? 'new'}
          open
          book={book}
          entry={entryTarget}
          onClose={() => {
            setEntryDialogOpen(false)
            setEntryTarget(null)
            closeLinkedEntry()
          }}
          onSaved={invalidate}
        />
      )}

      {importOpen && (
        <ImportDialog
          open
          mode="append"
          scope={scope}
          bookId={book.id}
          onClose={() => {
            setImportOpen(false)
            invalidate()
          }}
        />
      )}

      {deleteBookOpen && (
        <NekroDialog
          open
          onClose={() => setDeleteBookOpen(false)}
          title={t('detail.deleteTitle')}
          maxWidth="sm"
          actions={
            <>
              <ActionButton
                tone="ghost"
                onClick={() => setDeleteBookOpen(false)}
                disabled={deleteBookMutation.isPending}
              >
                {tCommon('actions.cancel')}
              </ActionButton>
              <ActionButton
                tone="danger"
                onClick={() => deleteBookMutation.mutate()}
                disabled={deleteBookMutation.isPending}
              >
                {tCommon('actions.delete')}
              </ActionButton>
            </>
          }
        >
          <Typography variant="body2">
            {t('detail.deleteDescription', { count: book.entry_count })}
          </Typography>
        </NekroDialog>
      )}

      {deleteEntry && (
        <NekroDialog
          open
          onClose={() => setDeleteEntryTarget(null)}
          title={t('detail.entryDeleteTitle')}
          maxWidth="sm"
          actions={
            <>
              <ActionButton
                tone="ghost"
                onClick={() => setDeleteEntryTarget(null)}
                disabled={deleteEntryMutation.isPending}
              >
                {tCommon('actions.cancel')}
              </ActionButton>
              <ActionButton
                tone="danger"
                onClick={() => deleteEntryMutation.mutate(deleteEntry.id)}
                disabled={deleteEntryMutation.isPending}
              >
                {tCommon('actions.delete')}
              </ActionButton>
            </>
          }
        >
          <Typography variant="body2">{t('detail.entryDeleteDescription')}</Typography>
        </NekroDialog>
      )}
    </Box>
  )
}
