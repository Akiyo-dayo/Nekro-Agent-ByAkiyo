import { useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import AddIcon from '@mui/icons-material/Add'
import ArrowBackIcon from '@mui/icons-material/ArrowBack'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import DownloadIcon from '@mui/icons-material/Download'
import EditIcon from '@mui/icons-material/Edit'
import LockOutlinedIcon from '@mui/icons-material/LockOutlined'
import PauseIcon from '@mui/icons-material/Pause'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import RestoreIcon from '@mui/icons-material/Restore'
import SettingsIcon from '@mui/icons-material/Settings'
import UploadFileIcon from '@mui/icons-material/UploadFile'
import VisibilityOutlinedIcon from '@mui/icons-material/VisibilityOutlined'
import WarningAmberRoundedIcon from '@mui/icons-material/WarningAmberRounded'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import SearchField from '../../components/common/SearchField'
import { ThemedTooltip } from '../../components/common/ThemedTooltip'
import {
  WorldbookEnabledChip,
  WorldbookReadOnlyChip,
  WorldbookScopeChip,
  WorldbookStatusChip,
  WorldbookTrustChip,
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
  worldbookApi,
  type WorldbookEntryAction,
  type WorldbookEntryItem,
  type WorldbookScopeParams,
} from '../../services/api/worldbook'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'

/**
 * 书详情
 *
 * 条目一次取全：一本书的条目通常只有几十条，本地过滤比再写一套服务端筛选更直接。
 * 只有书的增删改与条目动作走接口，改完统一 invalidate 以 `worldbook` 开头的查询。
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
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [entryDialogOpen, setEntryDialogOpen] = useState(false)
  const [entryTarget, setEntryTarget] = useState<WorldbookEntryItem | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [deleteBookOpen, setDeleteBookOpen] = useState(false)
  const [deleteEntry, setDeleteEntryTarget] = useState<WorldbookEntryItem | null>(null)
  const [viewEntry, setViewEntry] = useState<WorldbookEntryItem | null>(null)

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

  const entriesQuery = useQuery({
    queryKey: ['worldbook', 'entries', bookId],
    queryFn: () => worldbookApi.listEntries(bookId as number),
    enabled: bookId !== null && isScopeReady,
    retry: false,
  })

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

  const filteredEntries = useMemo(() => {
    const entries = entriesQuery.data?.items ?? []
    const needle = search.trim().toLowerCase()
    if (!needle) return entries
    return entries.filter(entry => {
      const haystack = [
        entry.comment,
        ...entry.keys,
        ...entry.secondary_keys,
        entry.content,
      ]
        .join('\n')
        .toLowerCase()
      return haystack.includes(needle)
    })
  }, [entriesQuery.data?.items, search])

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

  const renderTitle = (entry: WorldbookEntryItem) => {
    const title = entry.comment || truncateText(entry.content, 40) || `#${entry.id}`
    const hasWarnings =
      entry.invalid_keys.length > 0 || entry.slow_disabled_keys.length > 0
    if (!hasWarnings) {
      return <Typography variant="body2">{title}</Typography>
    }
    const lines = [
      ...entry.invalid_keys.map(item =>
        t('detail.invalidKeys', { key: item.key, reason: item.reason })
      ),
      ...entry.slow_disabled_keys.map(key => t('detail.slowKeys', { key })),
    ]
    return (
      <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center' }}>
        <Typography variant="body2">{title}</Typography>
        <ThemedTooltip title={lines.join('\n')}>
          <WarningAmberRoundedIcon color="warning" sx={{ fontSize: 16 }} />
        </ThemedTooltip>
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

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 3 },
        height: '100%',
        overflowY: 'auto',
        boxSizing: 'border-box',
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
      }}
    >
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
            tone="secondary"
            size="small"
            startIcon={<SettingsIcon />}
            onClick={() => setSettingsOpen(true)}
          >
            {t('detail.settings')}
          </ActionButton>
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
          <ActionButton
            tone="secondary"
            size="small"
            startIcon={<UploadFileIcon />}
            onClick={() => setImportOpen(true)}
          >
            {t('detail.appendImport')}
          </ActionButton>
          <ActionButton
            tone="secondary"
            size="small"
            startIcon={<DownloadIcon />}
            disabled={exportMutation.isPending}
            onClick={() => exportMutation.mutate()}
          >
            {t('detail.export')}
          </ActionButton>
          <ActionButton
            tone="danger"
            size="small"
            startIcon={<DeleteOutlineIcon />}
            onClick={() => setDeleteBookOpen(true)}
          >
            {t('detail.delete')}
          </ActionButton>
        </Box>
        )}
      </Box>

      {readOnly && <Alert severity="info">{t('readOnly.hint')}</Alert>}

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

      {entriesQuery.isPending ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={24} />
        </Box>
      ) : entriesQuery.error ? (
        isNotFoundError(entriesQuery.error) ? (
          <WorldbookNotFoundState compact />
        ) : (
          <QueryErrorState
            error={entriesQuery.error}
            onRetry={() => void entriesQuery.refetch()}
            compact
          />
        )
      ) : (
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('detail.columns.title')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('detail.columns.keys')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('detail.columns.position')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('detail.columns.order')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('detail.columns.status')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('detail.columns.trust')}</TableCell>
                <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('detail.columns.actions')}</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {filteredEntries.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={7} sx={UNIFIED_TABLE_STYLES.cell}>
                    <Typography variant="body2" color="text.secondary" sx={{ py: 3, textAlign: 'center' }}>
                      {t('detail.entriesEmpty')}
                    </Typography>
                  </TableCell>
                </TableRow>
              ) : (
                filteredEntries.map(entry => (
                  <TableRow key={entry.id}>
                    <TableCell sx={{ ...(UNIFIED_TABLE_STYLES.cell as object), minWidth: 220 }}>
                      {renderTitle(entry)}
                    </TableCell>
                    <TableCell sx={{ ...(UNIFIED_TABLE_STYLES.cell as object), minWidth: 200 }}>
                      {renderKeys(entry)}
                    </TableCell>
                    <TableCell sx={{ ...(UNIFIED_TABLE_STYLES.cell as object), whiteSpace: 'nowrap' }}>
                      {worldbookPositionText(entry.position, entry.depth, t)}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>{entry.order}</TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      {entry.status === 'quarantined' && entry.quarantine_reason === 'auto_blocked' ? (
                        <ThemedTooltip
                          title={t('detail.autoQuarantined', { count: entry.blocked_round_hits })}
                        >
                          <Box component="span">
                            <WorldbookStatusChip status={entry.status} enabled={entry.enabled} />
                          </Box>
                        </ThemedTooltip>
                      ) : (
                        <WorldbookStatusChip status={entry.status} enabled={entry.enabled} />
                      )}
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <WorldbookTrustChip trusted={entry.trusted} />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      {/* 不换行：表格本身可以横向滚动，四个图标竖着排会把每一行拉得很高 */}
                      {readOnly ? (
                        <ThemedTooltip title={t('detail.actions.view')}>
                          <IconActionButton
                            tone="subtle"
                            size="small"
                            aria-label={t('detail.actions.view')}
                            onClick={() => setViewEntry(entry)}
                          >
                            <VisibilityOutlinedIcon fontSize="small" />
                          </IconActionButton>
                        </ThemedTooltip>
                      ) : (
                      <Box sx={{ display: 'flex', gap: 0.25, flexWrap: 'nowrap' }}>
                        <ThemedTooltip title={t('detail.actions.edit')}>
                          <IconActionButton
                            tone="subtle"
                            size="small"
                            aria-label={t('detail.actions.edit')}
                            onClick={() => {
                              setEntryTarget(entry)
                              setEntryDialogOpen(true)
                            }}
                          >
                            <EditIcon fontSize="small" />
                          </IconActionButton>
                        </ThemedTooltip>
                        <ThemedTooltip
                          title={
                            entry.enabled
                              ? t('detail.actions.disable')
                              : t('detail.actions.enable')
                          }
                        >
                          <IconActionButton
                            tone="subtle"
                            size="small"
                            aria-label={
                              entry.enabled
                                ? t('detail.actions.disable')
                                : t('detail.actions.enable')
                            }
                            disabled={actionMutation.isPending}
                            onClick={() =>
                              actionMutation.mutate({
                                entryId: entry.id,
                                action: entry.enabled ? 'disable' : 'enable',
                              })
                            }
                          >
                            {entry.enabled ? (
                              <PauseIcon fontSize="small" />
                            ) : (
                              <PlayArrowIcon fontSize="small" />
                            )}
                          </IconActionButton>
                        </ThemedTooltip>
                        <ThemedTooltip
                          title={
                            entry.status === 'quarantined'
                              ? t('detail.actions.restore')
                              : t('detail.actions.quarantine')
                          }
                        >
                          <IconActionButton
                            tone={entry.status === 'quarantined' ? 'primary' : 'subtle'}
                            size="small"
                            aria-label={
                              entry.status === 'quarantined'
                                ? t('detail.actions.restore')
                                : t('detail.actions.quarantine')
                            }
                            disabled={actionMutation.isPending}
                            onClick={() =>
                              actionMutation.mutate({
                                entryId: entry.id,
                                action:
                                  entry.status === 'quarantined' ? 'restore' : 'quarantine',
                              })
                            }
                          >
                            {entry.status === 'quarantined' ? (
                              <RestoreIcon fontSize="small" />
                            ) : (
                              <LockOutlinedIcon fontSize="small" />
                            )}
                          </IconActionButton>
                        </ThemedTooltip>
                        <ThemedTooltip title={t('detail.actions.delete')}>
                          <IconActionButton
                            tone="danger"
                            size="small"
                            aria-label={t('detail.actions.delete')}
                            onClick={() => setDeleteEntryTarget(entry)}
                          >
                            <DeleteOutlineIcon fontSize="small" />
                          </IconActionButton>
                        </ThemedTooltip>
                      </Box>
                      )}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </Box>
      )}

      {viewEntry && (
        <NekroDialog
          open
          onClose={() => setViewEntry(null)}
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
