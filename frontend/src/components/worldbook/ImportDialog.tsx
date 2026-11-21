import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Box,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import NekroDialog from '../common/NekroDialog'
import { useNotification } from '../../hooks/useNotification'
import {
  WORLDBOOK_IMPORT_MAX_BYTES,
  worldbookApi,
  type WorldbookImportReport,
  type WorldbookScopeParams,
} from '../../services/api/worldbook'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import BookFields from './BookFields'
import {
  DEFAULT_WORLDBOOK_BOOK_FORM,
  bookFormToCreateRequest,
  validateBookForm,
  type WorldbookBookFormState,
} from './bookForm'
import { useWorldbookScopeOptions } from './useWorldbookScopeOptions'
import { useWorldbookWriteScope } from './useWorldbookWriteScope'

interface ImportDialogProps {
  open: boolean
  mode: 'new' | 'append'
  scope: WorldbookScopeParams
  bookId?: number
  onClose: () => void
  onViewBook?: (bookId: number) => void
}

const IMPORT_MAX_MIB = WORLDBOOK_IMPORT_MAX_BYTES / (1024 * 1024)

function filenameWithoutExtension(filename: string): string {
  return filename.replace(/\.[^.]+$/, '')
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

/**
 * 导入对话框
 *
 * 新书模式在文件之外再带一组建书字段；追加模式只收文件。成功后切到报告视图，
 * 报告里列出条数、非零的变动类别与逐条明细。
 */
export default function ImportDialog({
  open,
  mode,
  scope: viewScope,
  bookId,
  onClose,
  onViewBook,
}: ImportDialogProps) {
  // 导入成新书同建书：要落在某个实例上，单实例部署下用唯一的那个实例
  const scope = useWorldbookWriteScope(viewScope)
  const { t } = useTranslation('worldbook')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const { data: options } = useWorldbookScopeOptions(scope)

  const inputRef = useRef<HTMLInputElement | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [form, setForm] = useState<WorldbookBookFormState>({ ...DEFAULT_WORLDBOOK_BOOK_FORM })
  const [report, setReport] = useState<WorldbookImportReport | null>(null)

  useEffect(() => {
    if (open) {
      setFile(null)
      setForm({ ...DEFAULT_WORLDBOOK_BOOK_FORM })
      setReport(null)
    }
  }, [open])

  const fallbackName = file ? filenameWithoutExtension(file.name) : ''
  const errors = useMemo(
    () =>
      mode === 'new'
        ? validateBookForm(form, {
            hasInstance: Boolean(scope.instance_key),
            fallbackName,
          })
        : {},
    [mode, form, scope.instance_key, fallbackName]
  )
  const hasErrors = Object.keys(errors).length > 0

  const mutation = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error(t('import.noFile'))
      if (mode === 'new') {
        return worldbookApi.importNewBook(
          file,
          bookFormToCreateRequest(form, fallbackName),
          scope
        )
      }
      if (!bookId) throw new Error(tCommon('messages.operationFailed'))
      return worldbookApi.importIntoBook(bookId, file)
    },
    onSuccess: result => {
      setReport(result)
      void queryClient.invalidateQueries({ queryKey: ['worldbook'] })
      notification.success(t('import.success'))
    },
    onError: (error: unknown) => {
      notification.error(
        error instanceof Error && error.message
          ? error.message
          : tCommon('messages.operationFailed')
      )
    },
  })

  const pickFile = () => inputRef.current?.click()

  const handleFileChange = (list: FileList | null) => {
    const next = list?.[0] ?? null
    if (next && next.size > WORLDBOOK_IMPORT_MAX_BYTES) {
      notification.error(t('import.fileTooLarge', { max: IMPORT_MAX_MIB }))
      setFile(null)
      return
    }
    setFile(next)
  }

  const noteEntries = report
    ? Object.entries(report.notes).filter(([, count]) => count !== 0)
    : []

  const renderReport = () => {
    if (!report) return null
    return (
      <Stack spacing={2}>
        <Typography variant="subtitle1" fontWeight={700}>
          {t('import.reportTitle')}
        </Typography>
        <Typography variant="body2">{t('import.created', { count: report.created })}</Typography>

        {noteEntries.length === 0 ? (
          <Typography variant="caption" color="text.secondary">
            {t('import.notesEmpty')}
          </Typography>
        ) : (
          <Box>
            <Typography variant="body2" fontWeight={600} sx={{ mb: 0.5 }}>
              {t('import.notesTitle')}
            </Typography>
            <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 0.5 }}>
              {noteEntries.map(([kind, count]) => (
                <Typography key={kind} variant="body2">
                  {t(`import.noteKinds.${kind}`, { defaultValue: kind })}: {count}
                </Typography>
              ))}
            </Stack>
          </Box>
        )}

        {report.details.length > 0 && (
          <Box>
            <Typography variant="body2" fontWeight={600} sx={{ mb: 0.5 }}>
              {t('import.detailsTitle')}
            </Typography>
            <Box sx={{ maxHeight: 240, overflow: 'auto' }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.detailColumns.entry')}</TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.detailColumns.field')}</TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.detailColumns.kind')}</TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.header}>{t('import.detailColumns.detail')}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {report.details.map((detail, index) => (
                    <TableRow key={`${detail.entry_key}-${detail.field}-${index}`}>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell}>{detail.entry_key || '—'}</TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell}>{detail.field}</TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                        {t(`import.noteKinds.${detail.kind}`, { defaultValue: detail.kind })}
                      </TableCell>
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell}>{detail.detail}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          </Box>
        )}
      </Stack>
    )
  }

  const reportActions = report ? (
    <>
      {mode === 'new' && onViewBook ? (
        <ActionButton tone="primary" onClick={() => onViewBook(report.book_id)}>
          {t('import.viewBook')}
        </ActionButton>
      ) : null}
      <ActionButton tone="secondary" onClick={onClose}>
        {t('import.done')}
      </ActionButton>
    </>
  ) : (
    <>
      <ActionButton tone="ghost" onClick={onClose} disabled={mutation.isPending}>
        {tCommon('actions.cancel')}
      </ActionButton>
      <ActionButton
        tone="primary"
        onClick={() => mutation.mutate()}
        disabled={!file || mutation.isPending || (mode === 'new' && hasErrors)}
      >
        {tCommon('actions.import')}
      </ActionButton>
    </>
  )

  return (
    <NekroDialog
      open={open}
      onClose={onClose}
      title={mode === 'new' ? t('import.title') : t('import.appendTitle')}
      maxWidth="md"
      actions={reportActions}
    >
      {report ? (
        renderReport()
      ) : (
        <Stack spacing={2}>
          <Box>
            <input
              ref={inputRef}
              type="file"
              accept=".json,application/json"
              style={{ display: 'none' }}
              onChange={event => {
                handleFileChange(event.target.files)
                event.target.value = ''
              }}
            />
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
              <ActionButton tone="secondary" onClick={pickFile}>
                {t('import.chooseFile')}
              </ActionButton>
              {file ? (
                <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
                  {t('import.fileSummary', {
                    name: file.name,
                    size: formatFileSize(file.size),
                  })}
                </Typography>
              ) : (
                <Typography variant="caption" color="text.secondary">
                  {t('import.fileHint', { max: IMPORT_MAX_MIB })}
                </Typography>
              )}
            </Stack>
          </Box>

          <Typography variant="caption" color="text.secondary">
            {t('import.macroHint', { example: '{{char}}' })}
          </Typography>

          {mode === 'new' && (
            <BookFields
              value={form}
              onChange={setForm}
              errors={errors}
              mode="create"
              scope={scope}
              presets={options?.presets ?? []}
              channels={options?.channels ?? []}
            />
          )}
        </Stack>
      )}
    </NekroDialog>
  )
}
