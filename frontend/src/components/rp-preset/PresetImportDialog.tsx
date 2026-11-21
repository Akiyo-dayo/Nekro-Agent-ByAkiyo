import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Box,
  CircularProgress,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from '@mui/material'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import NekroDialog from '../common/NekroDialog'
import { useNotification } from '../../hooks/useNotification'
import {
  rpPresetsApi,
  type RpPresetDetail,
  type RpPresetInspectResponse,
  type RpPresetScope,
} from '../../services/api/rpPresets'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import {
  chatHistoryIndex,
  placementLabel,
  placementOf,
} from './segments'

interface PresetImportDialogProps {
  open: boolean
  scope: RpPresetScope
  onClose: () => void
  /** 导入成功：父级负责关闭对话框、刷新列表并打开详情 */
  onImported: (preset: RpPresetDetail) => void
}

const IMPORT_MAX_MIB = 2
/** 摘要表超过 12 行时表内滚动 */
const SUMMARY_MAX_HEIGHT = 440

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

/**
 * 导入角色扮演预设
 *
 * 两步：选文件并检查（不落库），再在报告上确认名称与段落摘要后导入。检查被拒时把后端
 * 给的原因原样显示在对话框里；那一句往往就是「缺 chatHistory 占位段」这类唯一线索，
 * 不能再包一层通用错误文案。
 */
export default function PresetImportDialog({
  open,
  scope,
  onClose,
  onImported,
}: PresetImportDialogProps) {
  const { t } = useTranslation('tavern')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [report, setReport] = useState<RpPresetInspectResponse | null>(null)
  const [name, setName] = useState('')
  const [inspectError, setInspectError] = useState('')
  const [importError, setImportError] = useState('')

  useEffect(() => {
    if (!open) return
    setFile(null)
    setReport(null)
    setName('')
    setInspectError('')
    setImportError('')
  }, [open])

  const inspectMutation = useMutation({
    mutationFn: (nextFile: File) => rpPresetsApi.inspect(nextFile, scope),
    onSuccess: data => {
      setReport(data)
      setName(data.name)
      setInspectError('')
    },
    onError: (error: unknown) => {
      setReport(null)
      setInspectError(errorMessage(error, tCommon('messages.operationFailed')))
    },
  })

  const importMutation = useMutation({
    mutationFn: () => {
      if (!file) throw new Error(t('lab.presets.importDialog.noFile'))
      return rpPresetsApi.import(file, name.trim() || null, scope)
    },
    onSuccess: preset => {
      notification.success(t('lab.presets.importDialog.success'))
      onImported(preset)
    },
    onError: (error: unknown) => {
      setImportError(errorMessage(error, tCommon('messages.operationFailed')))
    },
  })

  const pickFile = () => inputRef.current?.click()

  // 请求在飞的时候不让遮罩 / 右上角把对话框关掉：否则用户会以为这次导入取消了
  const requestClose = () => {
    if (inspectMutation.isPending || importMutation.isPending) return
    onClose()
  }

  const handleFileChange = (list: FileList | null) => {
    const next = list?.[0] ?? null
    if (!next) return
    setImportError('')
    if (!next.name.toLowerCase().endsWith('.json')) {
      setFile(null)
      setReport(null)
      setInspectError(t('lab.presets.importDialog.jsonOnly'))
      return
    }
    setFile(next)
    setReport(null)
    setInspectError('')
    inspectMutation.mutate(next)
  }

  const notes = (report?.notes ?? [])
    .map(note => (typeof note.text === 'string' ? note.text : ''))
    .filter(text => text.trim() !== '')

  const historyIndex = report ? chatHistoryIndex(report.segments) : -1
  const nameInvalid = report !== null && name.trim() === ''

  const actions = report ? (
    <>
      <ActionButton
        tone="ghost"
        onClick={() => {
          setReport(null)
          setName('')
          setImportError('')
        }}
        disabled={importMutation.isPending}
      >
        {t('lab.presets.importDialog.back')}
      </ActionButton>
      <ActionButton tone="secondary" onClick={requestClose} disabled={importMutation.isPending}>
        {tCommon('actions.cancel')}
      </ActionButton>
      <ActionButton
        tone="primary"
        onClick={() => importMutation.mutate()}
        disabled={!file || nameInvalid || importMutation.isPending}
      >
        {importMutation.isPending
          ? t('lab.presets.importDialog.importing')
          : t('lab.presets.importDialog.submit')}
      </ActionButton>
    </>
  ) : (
    <ActionButton tone="ghost" onClick={requestClose} disabled={inspectMutation.isPending}>
      {tCommon('actions.cancel')}
    </ActionButton>
  )

  return (
    <NekroDialog
      open={open}
      onClose={requestClose}
      title={t('lab.presets.importDialog.title')}
      maxWidth="md"
      actions={actions}
    >
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

      {!report ? (
        <Stack spacing={2}>
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
            <ActionButton
              tone="secondary"
              onClick={pickFile}
              disabled={inspectMutation.isPending}
            >
              {t('lab.presets.importDialog.chooseFile')}
            </ActionButton>
            {file ? (
              <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
                {file.name}
              </Typography>
            ) : (
              <Typography variant="caption" color="text.secondary">
                {t('lab.presets.importDialog.fileHint', { max: IMPORT_MAX_MIB })}
              </Typography>
            )}
          </Stack>

          {inspectMutation.isPending && (
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
              <CircularProgress size={18} />
              <Typography variant="body2" color="text.secondary">
                {t('lab.presets.importDialog.inspecting')}
              </Typography>
            </Stack>
          )}

          {inspectError ? <Alert severity="error">{inspectError}</Alert> : null}
        </Stack>
      ) : (
        <Stack spacing={2}>
          <TextField
            label={t('lab.presets.importDialog.name')}
            value={name}
            onChange={event => setName(event.target.value.slice(0, 128))}
            error={nameInvalid}
            helperText={nameInvalid ? t('lab.presets.importDialog.nameRequired') : undefined}
            size="small"
            fullWidth
            autoFocus
          />

          <Box>
            <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 0.5 }}>
              {t('lab.presets.importDialog.notesTitle')}
            </Typography>
            {notes.length === 0 ? (
              <Typography variant="caption" color="text.secondary">
                {t('lab.presets.importDialog.notesEmpty')}
              </Typography>
            ) : (
              <Stack component="ul" spacing={0.5} sx={{ m: 0, pl: 2.5 }}>
                {notes.map((text, index) => (
                  <Typography key={`${index}-${text}`} component="li" variant="body2">
                    {text}
                  </Typography>
                ))}
              </Stack>
            )}
          </Box>

          <Box>
            <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 0.5 }}>
              {t('lab.presets.importDialog.summaryTitle')}
            </Typography>
            <Box
              sx={{
                maxHeight: SUMMARY_MAX_HEIGHT,
                overflow: 'auto',
                ...(UNIFIED_TABLE_STYLES.scrollbar as object),
              }}
            >
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    {[
                      'lab.presets.detail.columns.name',
                      'lab.presets.detail.columns.role',
                      'lab.presets.detail.columns.position',
                      'lab.presets.detail.columns.chars',
                      'lab.presets.detail.columns.enabled',
                    ].map(key => (
                      <TableCell key={key} sx={UNIFIED_TABLE_STYLES.header}>
                        {t(key)}
                      </TableCell>
                    ))}
                  </TableRow>
                </TableHead>
                <TableBody>
                  {report.segments.map((segment, index) => {
                    const label = placementLabel(
                      placementOf(segment, index, historyIndex)
                    )
                    return (
                      <TableRow
                        key={`${segment.identifier}-${index}`}
                        sx={UNIFIED_TABLE_STYLES.row}
                      >
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                          {segment.name || segment.identifier}
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>{segment.role}</TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                          {t(label.key, label.options)}
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                          {segment.marker ? '—' : segment.chars.toLocaleString()}
                        </TableCell>
                        <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                          {segment.enabled
                            ? t('lab.presets.detail.enabledYes')
                            : t('lab.presets.detail.enabledNo')}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </Box>
          </Box>

          {importError ? <Alert severity="error">{importError}</Alert> : null}
        </Stack>
      )}
    </NekroDialog>
  )
}
