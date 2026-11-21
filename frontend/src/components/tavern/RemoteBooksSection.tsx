import { useRef, useState } from 'react'
import {
  Alert,
  Box,
  Checkbox,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import QueryErrorState from '../common/QueryErrorState'
import { ApiError } from '../../services/api/axios'
import {
  tavernApi,
  type RemoteBookCheckResult,
  type RemoteBookImportResult,
  type RemoteBookView,
  type TavernScope,
} from '../../services/api/tavern'
import { CARD_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'

export interface RemoteBooksSectionProps {
  presetId: number
  scope: TavernScope
  /** 没有写权限时只看不操作（隐藏所有按钮） */
  readOnly?: boolean
}

/** 一次操作要做什么；重试时原样再跑一遍，所以连 `use_proxy` 一起记在调用点 */
type RowAction =
  | { kind: 'download'; replace: boolean }
  | { kind: 'check' }
  | { kind: 'upload'; file: File; replace: boolean }

interface RunVariables {
  action: RowAction
  useProxy: boolean
}

type RowResult = RemoteBookImportResult | RemoteBookCheckResult

/** 弹确认框的那个动作：更新到新版本，或已导入时的手动上传（都会覆盖旧条目） */
type ConfirmRequest = { kind: 'update' } | { kind: 'upload'; file: File }

/** 三种操作的失败兜底文案；后端 4xx 的 `message` 优先 */
const FAILED_KEYS: Record<RowAction['kind'], string> = {
  download: 'remoteBooks.downloadFailed',
  check: 'remoteBooks.checkFailed',
  upload: 'remoteBooks.uploadFailed',
}

/** 取给管理员看的错误文字：`ApiError.message` 已经是后端写好的原因（下载失败时可能有好几行） */
function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.message) return error.message
  if (error instanceof Error && error.message) return error.message
  return fallback
}

/**
 * 列表查询的键
 *
 * 同一份键在导入成功后用于刷新：作用域不同看到的世界书归属不同，必须把两个键都带上，
 * 否则切实例后会把上一份缓存当成这一份。
 */
function remoteBooksKey(presetId: number, scope: TavernScope) {
  return [
    'tavern',
    'remote-books',
    presetId,
    scope.instance_key ?? '',
    scope.adapter_key ?? '',
  ] as const
}

/** 地址栏里能看出来的主机名；解析失败就退回原串，宁可显示得难看也不要空着 */
function hostOf(url: string): string {
  try {
    return new URL(url).host || url
  } catch {
    return url
  }
}

/** ISO 时间按本地时区显示；坏值原样给出，不要变成 Invalid Date */
function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

/** 检查结果里版本号可能是空串，空位置用占位符，别让括号里缺一块 */
function versionOrDash(version: string): string {
  return version || '—'
}

interface RemoteBookRowProps {
  presetId: number
  scope: TavernScope
  source: RemoteBookView
  useProxy: boolean
  readOnly: boolean
}

/**
 * 一个来源一行
 *
 * 操作状态放在行内而不是整节：后端同一租户同时只允许一个下载，第二个会 409，但界面
 * 仍要让每个来源各自显示自己的错误与重试，不能一处失败就把整节锁住。
 */
function RemoteBookRow({ presetId, scope, source, useProxy, readOnly }: RemoteBookRowProps) {
  const { t } = useTranslation('tavern')
  const queryClient = useQueryClient()
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const lastRunRef = useRef<RunVariables | null>(null)

  const [check, setCheck] = useState<RemoteBookCheckResult | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState<{ text: string; notes: string } | null>(null)
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null)

  const imported = source.imported
  const booksKey = remoteBooksKey(presetId, scope)

  const mutation = useMutation<RowResult, unknown, RunVariables>({
    mutationFn: ({ action, useProxy: proxy }) => {
      switch (action.kind) {
        case 'check':
          return tavernApi.checkRemoteBook(presetId, source.key, { use_proxy: proxy }, scope)
        case 'upload':
          return tavernApi.uploadRemoteBook(
            presetId,
            source.key,
            action.file,
            action.replace,
            scope
          )
        default:
          return tavernApi.downloadRemoteBook(
            presetId,
            source.key,
            { use_proxy: proxy, replace: action.replace },
            scope
          )
      }
    },
    onMutate: ({ action }) => {
      setError('')
      setNotice(null)
      // 检查结果只对刚才那一次下载有效；开始别的动作就作废，免得「更新到新版本」按钮挂着
      if (action.kind !== 'check') setCheck(null)
    },
    onSuccess: (data, { action }) => {
      if (action.kind === 'check') {
        const result = data as RemoteBookCheckResult
        setCheck(result)
        // not_imported 说明列表里那份「已导入」已经过期（例如在别处删了书），刷一次对齐
        if (result.status === 'not_imported') {
          void queryClient.invalidateQueries({ queryKey: booksKey })
        }
        return
      }
      const result = data as RemoteBookImportResult
      const noteCount = Object.values(result.report.notes ?? {}).reduce(
        (sum, count) => sum + (count > 0 ? count : 0),
        0
      )
      const line = t(result.replaced ? 'remoteBooks.updatedDone' : 'remoteBooks.importedDone', {
        name: result.book_name,
        n: result.report.created,
      })
      setNotice({
        text: result.version
          ? `${line} · ${t('remoteBooks.versionLine', { version: result.version })}`
          : line,
        notes: noteCount > 0 ? t('remoteBooks.notesHint', { n: noteCount }) : '',
      })
      void queryClient.invalidateQueries({ queryKey: booksKey })
    },
    onError: (err, { action }) => {
      setError(errorText(err, t(FAILED_KEYS[action.kind])))
      // 失败也可能是列表已经过期（别处导入或删了这本书，409 / 404），重新拉一次对齐状态
      void queryClient.invalidateQueries({ queryKey: booksKey })
    },
  })

  const pendingKind = mutation.isPending ? mutation.variables?.action.kind : undefined

  const run = (action: RowAction, proxy = useProxy) => {
    lastRunRef.current = { action, useProxy: proxy }
    mutation.mutate({ action, useProxy: proxy })
  }

  const retry = () => {
    const last = lastRunRef.current
    if (!last) return
    mutation.mutate(last)
  }

  const pickUpload = () => fileInputRef.current?.click()

  const handleFileChange = (list: FileList | null) => {
    const file = list?.[0]
    if (!file) return
    // 已导入的书上传等于替换，必须先让管理员看到「会覆盖手改条目」这句话
    if (imported) {
      setConfirm({ kind: 'upload', file })
    } else {
      run({ kind: 'upload', file, replace: false })
    }
  }

  const handleConfirm = () => {
    if (!confirm) return
    const action: RowAction =
      confirm.kind === 'upload'
        ? { kind: 'upload', file: confirm.file, replace: true }
        : { kind: 'download', replace: true }
    setConfirm(null)
    run(action)
  }

  const showUpdate = Boolean(check && (check.status === 'newer' || check.status === 'changed'))

  const renderCheckLine = () => {
    if (!check) return null
    if (check.status === 'same') {
      return (
        <Typography variant="caption" color="text.secondary">
          {t('remoteBooks.checkSame')}
        </Typography>
      )
    }
    if (check.status === 'not_imported') {
      return (
        <Typography variant="caption" color="text.secondary">
          {t('remoteBooks.checkNotImported')}
        </Typography>
      )
    }
    return (
      <Typography variant="caption" color="info.main">
        {check.status === 'newer'
          ? t('remoteBooks.checkNewer', {
              remote: versionOrDash(check.remote_version),
              local: versionOrDash(check.local_version),
            })
          : t('remoteBooks.checkChanged')}
      </Typography>
    )
  }

  const renderActions = () => {
    if (readOnly) return null
    // 失败时只留「重试」与「手动上传」两个出口，避免一排按钮里再挑一次
    if (error) {
      return (
        <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 1 }}>
          <ActionButton tone="secondary" size="small" onClick={retry} disabled={mutation.isPending}>
            {mutation.isPending ? <CircularProgress size={16} sx={{ mr: 1 }} /> : null}
            {t('remoteBooks.retry')}
          </ActionButton>
          <ActionButton
            tone="ghost"
            size="small"
            onClick={pickUpload}
            disabled={mutation.isPending}
          >
            {t('remoteBooks.upload')}
          </ActionButton>
        </Stack>
      )
    }
    return (
      <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 1 }}>
        {imported ? (
          <ActionButton
            tone="secondary"
            size="small"
            onClick={() => run({ kind: 'check' })}
            disabled={mutation.isPending}
          >
            {pendingKind === 'check' ? <CircularProgress size={16} sx={{ mr: 1 }} /> : null}
            {t('remoteBooks.check')}
          </ActionButton>
        ) : (
          <ActionButton
            tone="primary"
            size="small"
            onClick={() => run({ kind: 'download', replace: false })}
            disabled={mutation.isPending}
          >
            {pendingKind === 'download' ? <CircularProgress size={16} sx={{ mr: 1 }} /> : null}
            {t('remoteBooks.download')}
          </ActionButton>
        )}
        {showUpdate ? (
          <ActionButton
            tone="primary"
            size="small"
            onClick={() => setConfirm({ kind: 'update' })}
            disabled={mutation.isPending}
          >
            {t('remoteBooks.update')}
          </ActionButton>
        ) : null}
        <ActionButton tone="ghost" size="small" onClick={pickUpload} disabled={mutation.isPending}>
          {pendingKind === 'upload' ? <CircularProgress size={16} sx={{ mr: 1 }} /> : null}
          {t('remoteBooks.upload')}
        </ActionButton>
      </Stack>
    )
  }

  return (
    <>
      <TableRow>
        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
          <Typography variant="body2" fontWeight={600} sx={{ wordBreak: 'break-all' }}>
            {source.file_name}
          </Typography>
          {source.version_hint ? (
            <Typography variant="caption" color="text.secondary" display="block">
              {t('remoteBooks.pointsTo', { hint: source.version_hint })}
            </Typography>
          ) : null}
          {source.scripts.length > 0 ? (
            <Typography
              variant="caption"
              color="text.secondary"
              display="block"
              sx={{ wordBreak: 'break-word' }}
            >
              {t('remoteBooks.scripts')}: {source.scripts.join(', ')}
            </Typography>
          ) : null}
          <Tooltip
            title={
              source.urls.length > 0 ? (
                <Box sx={{ whiteSpace: 'pre-line', wordBreak: 'break-all' }}>
                  {source.urls.join('\n')}
                </Box>
              ) : (
                ''
              )
            }
          >
            <Typography
              variant="caption"
              color="text.secondary"
              display="block"
              sx={{ wordBreak: 'break-all' }}
            >
              {source.urls.length > 0 ? hostOf(source.urls[0]) : '—'}
              {source.urls.length > 1
                ? ` · ${t('remoteBooks.mirrors', { n: source.urls.length - 1 })}`
                : ''}
            </Typography>
          </Tooltip>
        </TableCell>

        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
          {imported ? (
            <Box>
              <Typography variant="body2" fontWeight={600} sx={{ wordBreak: 'break-word' }}>
                {imported.book_name}
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block">
                {imported.version
                  ? t('remoteBooks.versionLine', { version: imported.version })
                  : t('remoteBooks.noVersion')}
                {' · '}
                {t('remoteBooks.entries', { n: imported.entries })}
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block">
                {imported.manual
                  ? t('remoteBooks.manualSource')
                  : t('remoteBooks.fromHost', { host: imported.host || '—' })}
                {imported.downloaded_at
                  ? ` · ${t('remoteBooks.downloadedAt', { time: formatTime(imported.downloaded_at) })}`
                  : ''}
              </Typography>
            </Box>
          ) : (
            <Typography variant="caption" color="text.secondary">
              {t('remoteBooks.notImported')}
            </Typography>
          )}
        </TableCell>

        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
          <Stack spacing={1} sx={{ alignItems: 'flex-start', minWidth: 200 }}>
            {renderCheckLine()}
            {notice ? (
              <Alert severity="success" sx={{ py: 0 }}>
                <Typography variant="caption" display="block">
                  {notice.text}
                </Typography>
                {notice.notes ? (
                  <Typography variant="caption" display="block" color="text.secondary">
                    {notice.notes}
                  </Typography>
                ) : null}
              </Alert>
            ) : null}
            {error ? (
              <Alert severity="error" sx={{ py: 0 }}>
                <Typography
                  variant="caption"
                  sx={{ whiteSpace: 'pre-line', wordBreak: 'break-word' }}
                >
                  {error}
                </Typography>
              </Alert>
            ) : null}
            {renderActions()}
            {/* 选择文件框藏在操作格里：只靠「手动上传」按钮代为点击，不占版面 */}
            <input
              ref={fileInputRef}
              type="file"
              accept=".json,application/json"
              style={{ display: 'none' }}
              onChange={event => {
                handleFileChange(event.target.files)
                event.target.value = ''
              }}
            />
          </Stack>
        </TableCell>
      </TableRow>

      {/* 替换会覆盖管理员在这本书里手改过的条目，先确认再调接口 */}
      <Dialog
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        maxWidth="xs"
        fullWidth
        PaperProps={{ sx: CARD_VARIANTS.default.styles }}
      >
        <DialogTitle>{t('remoteBooks.confirmTitle')}</DialogTitle>
        <DialogContent>
          <Typography variant="body2">{t('remoteBooks.confirmBody')}</Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <ActionButton
            tone="secondary"
            onClick={() => setConfirm(null)}
            disabled={mutation.isPending}
          >
            {t('remoteBooks.cancel')}
          </ActionButton>
          <ActionButton tone="primary" onClick={handleConfirm} disabled={mutation.isPending}>
            {t('remoteBooks.confirmReplace')}
          </ActionButton>
        </DialogActions>
      </Dialog>
    </>
  )
}

/**
 * 人设的「远程世界书」一节
 *
 * 卡片里的脚本在酒馆里运行时会自己下载这些世界书，NA 不执行脚本，这里只是把地址摊开，
 * 由管理员确认后由服务端代为下载并当作参考资料导入。来源为空时整节不渲染：卡片没有
 * 这项需求时不该占着面板的位置。
 */
export default function RemoteBooksSection({
  presetId,
  scope,
  readOnly = false,
}: RemoteBooksSectionProps) {
  const { t } = useTranslation('tavern')
  const [useProxy, setUseProxy] = useState(false)

  const query = useQuery({
    queryKey: remoteBooksKey(presetId, scope),
    queryFn: () => tavernApi.listRemoteBooks(presetId, scope),
    retry: false,
  })

  if (query.isPending) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}>
        <CircularProgress size={22} />
      </Box>
    )
  }

  if (query.isError) {
    return (
      <QueryErrorState
        error={query.error}
        onRetry={() => void query.refetch()}
        compact
        sx={{ minHeight: 0 }}
      />
    )
  }

  if (query.data.sources.length === 0) return null

  return (
    <Box component="section" sx={{ minWidth: 0 }}>
      <Typography variant="subtitle2" fontWeight={600}>
        {t('remoteBooks.title')}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
        {t('remoteBooks.intro')}
      </Typography>
      {/* 只有后台配了系统默认代理时才给这个开关（没配的时候勾了也不会有区别）；只读时没有可下载的操作，一并不显示 */}
      {query.data.proxy_available && !readOnly ? (
        <FormControlLabel
          control={
            <Checkbox
              size="small"
              checked={useProxy}
              onChange={event => setUseProxy(event.target.checked)}
            />
          }
          label={<Typography variant="caption">{t('remoteBooks.proxy')}</Typography>}
          sx={{ mt: 0.5 }}
        />
      ) : null}
      <Box sx={{ overflowX: 'auto', mt: 1 }}>
        <Table size="small" sx={{ minWidth: 760 }}>
          <TableHead>
            <TableRow>
              <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('remoteBooks.fileName')}</TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('remoteBooks.status')}</TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('remoteBooks.actions')}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {query.data.sources.map(source => (
              <RemoteBookRow
                key={source.key}
                presetId={presetId}
                scope={scope}
                source={source}
                useProxy={useProxy}
                readOnly={readOnly}
              />
            ))}
          </TableBody>
        </Table>
      </Box>
    </Box>
  )
}
