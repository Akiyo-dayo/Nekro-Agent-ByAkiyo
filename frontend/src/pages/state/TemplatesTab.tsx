import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Checkbox,
  Chip,
  CircularProgress,
  Divider,
  FormControl,
  FormControlLabel,
  InputLabel,
  List,
  ListItemButton,
  ListItemText,
  MenuItem,
  Select,
  Stack,
  Typography,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import SearchField from '../../components/common/SearchField'
import { isNotFoundError } from '../../services/api/axios'
import {
  STATE_THEMES,
  stateTableApi,
  type StateBuiltinKind,
  type StateFieldDef,
  type StateFieldDefs,
  type StateInitvarEntry,
  type StateScope,
} from '../../services/api/stateTable'
import { CHIP_VARIANTS, CARD_VARIANTS } from '../../theme/variants'
import FieldDefsTable from './FieldDefsTable'
import JsonEditorField from './JsonEditorField'
import { enumText, isConflictError, parseStateObject } from './stateUtils'

/**
 * 「模板」标签：左边人设清单，右边选中那一份的模板编辑
 *
 * 表格只管 `fields`，模板级的 `strict_set` / `private_words` 等键原样带着走；
 * 保存时每次都以服务端的 `version` 为准，冲突就整份重新拉，不做字段级合并 ——
 * 服务端不接受部分更新，前端自己合出来的结果反而说不清是谁改的。
 */
export default function TemplatesTab({ scope }: { scope: StateScope }) {
  const theme = useTheme()
  const { t } = useTranslation('state')
  const { t: tCommon } = useTranslation('common')
  const queryClient = useQueryClient()
  const scopeKey = `${scope.adapter_key}|${scope.instance_key}`

  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [fieldDefs, setFieldDefs] = useState<StateFieldDefs>({ fields: [] })
  const [initialDraft, setInitialDraft] = useState('{}')
  const [initialError, setInitialError] = useState('')
  const [saveError, setSaveError] = useState('')

  const listQuery = useQuery({
    queryKey: ['state-table', 'templates', scopeKey, search],
    queryFn: () => stateTableApi.listTemplates(scope, search.trim() || undefined),
    retry: false,
  })

  const items = useMemo(() => listQuery.data?.items ?? [], [listQuery.data])
  const selected = items.find(item => item.preset_id === selectedId) ?? null

  // 换人设时默认选第一份，省得右边一直是空的
  useEffect(() => {
    if (selectedId !== null && items.some(item => item.preset_id === selectedId)) return
    setSelectedId(items[0]?.preset_id ?? null)
  }, [items, selectedId])

  const detailQuery = useQuery({
    queryKey: ['state-table', 'template', scopeKey, selectedId],
    queryFn: () => stateTableApi.getTemplate(scope, selectedId as number),
    enabled: selectedId !== null,
    retry: false,
    // 切回窗口时不重拉：字段表与初值改到一半的内容不能被悄悄换掉
    refetchOnWindowFocus: false,
  })

  const detail = detailQuery.data
  // 只认服务端给的 `writable`：它已经算过「全局人设 + 租户身份」这一层，前端再猜一遍只会猜错
  const writable = detail?.writable ?? selected?.writable ?? false
  const version = detail?.version ?? 0

  // 换了人设、或者服务端那一份的版本变了，才把编辑态换掉；版本没变的重拉（页面上的「刷新」、
  // 别的标签改了东西顺手失效的查询）不动编辑态，免得改到一半的内容被悄悄覆盖
  const shownTemplate = useRef('')
  useEffect(() => {
    if (!detail) return
    const marker = `${detail.preset_id}|${detail.exists ? (detail.version ?? 0) : 'none'}`
    if (shownTemplate.current === marker) return
    const switched = !shownTemplate.current.startsWith(`${detail.preset_id}|`)
    shownTemplate.current = marker
    setFieldDefs(detail.field_defs ?? { fields: [] })
    setInitialDraft(JSON.stringify(detail.initial ?? {}, null, 2))
    setInitialError('')
    // 同一个人设因为版本冲突重拉时，留着「别处刚改过，已重新加载」那句；换了人设才清掉
    if (switched) setSaveError('')
  }, [detail])

  const [notice, setNotice] = useState('')

  const refreshAll = () => {
    void detailQuery.refetch()
    void queryClient.invalidateQueries({ queryKey: ['state-table'] })
  }

  const saveMutation = useMutation({
    mutationFn: (payload: { field_defs: StateFieldDefs; initial: Record<string, unknown>; version: number }) =>
      stateTableApi.updateTemplate(scope, selectedId as number, payload),
    onSuccess: () => {
      refreshAll()
      setNotice(t('templates.saveSuccess'))
    },
    onError: (error: unknown) => {
      if (isConflictError(error)) {
        setSaveError(t('detail.conflict'))
        void detailQuery.refetch()
        return
      }
      // 400 / 422 的 message 会指出哪一条字段不合法，原样显示
      setSaveError(error instanceof Error ? error.message : String(error))
    },
  })

  const initvarQuery = useQuery({
    queryKey: ['state-table', 'initvar-entries', scopeKey, selectedId],
    queryFn: () => stateTableApi.listInitvarEntries(scope, selectedId as number),
    enabled: false,
    retry: false,
  })

  const [initvarOpen, setInitvarOpen] = useState(false)
  const [initvarChoice, setInitvarChoice] = useState<number | 'all'>('all')
  const [builtinOpen, setBuiltinOpen] = useState(false)
  const [builtinKinds, setBuiltinKinds] = useState<StateBuiltinKind[]>(['scene', 'inventory'])
  const [dialogError, setDialogError] = useState('')
  const [busy, setBusy] = useState(false)

  const openInitvar = async () => {
    if (selectedId === null) return
    setDialogError('')
    setInitvarChoice('all')
    setInitvarOpen(true)
    await initvarQuery.refetch()
  }

  const runInitvar = async () => {
    if (selectedId === null) return
    setBusy(true)
    setDialogError('')
    try {
      const result = await stateTableApi.templateFromInitvar(
        scope,
        selectedId,
        version,
        initvarChoice === 'all' ? undefined : initvarChoice
      )
      setInitvarOpen(false)
      refreshAll()
      setNotice(t('templates.initvarDialog.success', { count: result.fields }))
    } catch (error) {
      if (isConflictError(error)) {
        setInitvarOpen(false)
        void detailQuery.refetch()
        return
      }
      setDialogError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  const runBuiltin = async () => {
    if (selectedId === null || builtinKinds.length === 0) return
    setBusy(true)
    setDialogError('')
    try {
      const result = await stateTableApi.templateFromBuiltin(scope, selectedId, builtinKinds, version)
      setBuiltinOpen(false)
      refreshAll()
      setNotice(
        t('templates.builtinDialog.success', {
          added: result.added.length,
          skipped: result.skipped.length,
        })
      )
    } catch (error) {
      if (isConflictError(error)) {
        setBuiltinOpen(false)
        void detailQuery.refetch()
        return
      }
      setDialogError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(false)
    }
  }

  const save = () => {
    const parsed = parseStateObject(initialDraft)
    if (!parsed.ok) {
      setInitialError(t('templates.initialInvalid'))
      return
    }
    setInitialError('')
    setSaveError('')
    // 路径按 `/` 切回数组、去掉空段放在保存这一步：输入中途的 `/` 不能被吃掉
    const fields: StateFieldDef[] = fieldDefs.fields.map(field => ({
      ...field,
      path: field.path.map(segment => segment.trim()).filter(Boolean),
    }))
    saveMutation.mutate({
      field_defs: { ...fieldDefs, fields },
      initial: parsed.value,
      version,
    })
  }

  if (isNotFoundError(listQuery.error)) {
    return <Alert severity="info">{t('notEnabled')}</Alert>
  }

  const noticeAlert = notice ? (
    <Alert severity="success" onClose={() => setNotice('')}>
      {notice}
    </Alert>
  ) : null

  return (
    <Box
      sx={{
        display: 'grid',
        gridTemplateColumns: { xs: '1fr', md: '280px minmax(0, 1fr)' },
        gap: 2,
        minHeight: 0,
        flex: 1,
      }}
    >
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, minHeight: 0 }}>
        <SearchField
          value={search}
          onChange={setSearch}
          onClear={() => setSearch('')}
          clearAriaLabel={tCommon('actions.clear')}
          placeholder={t('templates.searchPlaceholder')}
        />
        {listQuery.isPending ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
            <CircularProgress size={22} />
          </Box>
        ) : items.length === 0 ? (
          <Box sx={{ py: 2 }}>
            <Typography variant="body2" color="text.secondary">
              {t('templates.emptyTitle')}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {t('templates.emptyHint')}
            </Typography>
          </Box>
        ) : (
          <List dense sx={{ overflowY: 'auto', minHeight: 0 }}>
            {items.map(item => (
              <ListItemButton
                key={item.preset_id}
                selected={item.preset_id === selectedId}
                onClick={() => setSelectedId(item.preset_id)}
              >
                <ListItemText
                  primary={
                    <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
                      <Typography variant="body2" fontWeight={600}>
                        {item.preset_name}
                      </Typography>
                      {item.global && (
                        <Chip
                          size="small"
                          label={t('templates.global')}
                          sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.primary.main, true)}
                        />
                      )}
                    </Stack>
                  }
                  secondary={
                    <Typography variant="caption" color="text.secondary" component="span">
                      {item.exists
                        ? `${t('templates.exists')} · ${enumText(t, 'templates.source', item.source)} · ${t('templates.fieldCount', { count: item.fields })}`
                        : t('templates.missing')}
                    </Typography>
                  }
                />
              </ListItemButton>
            ))}
          </List>
        )}
      </Box>

      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0, minWidth: 0 }}>
        {noticeAlert}
        {selected === null ? (
          <Box sx={{ py: 3 }}>
            <Typography variant="subtitle1">{t('templates.selectTitle')}</Typography>
            <Typography variant="body2" color="text.secondary">
              {t('templates.selectHint')}
            </Typography>
          </Box>
        ) : (
          <>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
              <Typography variant="subtitle1" fontWeight={700}>
                {selected.preset_name}
              </Typography>
              <ActionButton tone="secondary" size="small" disabled={!writable} onClick={() => void openInitvar()}>
                {t('templates.fromInitvar')}
              </ActionButton>
              <ActionButton
                tone="secondary"
                size="small"
                disabled={!writable}
                onClick={() => {
                  setDialogError('')
                  setBuiltinOpen(true)
                }}
              >
                {t('templates.fromBuiltin')}
              </ActionButton>
            </Stack>

            {!writable && <Alert severity="info">{t('templates.readonlyAlert')}</Alert>}
            {saveError && <Alert severity="error">{saveError}</Alert>}

            {detailQuery.isPending ? (
              <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
                <CircularProgress size={22} />
              </Box>
            ) : detailQuery.isError ? (
              <Alert severity="error">
                {detailQuery.error instanceof Error ? detailQuery.error.message : String(detailQuery.error)}
              </Alert>
            ) : !detail?.exists ? (
              <Alert severity="info" icon={false}>
                <Typography variant="body2">{t('templates.noTemplate')}</Typography>
                <Typography variant="caption" color="text.secondary">
                  {t('templates.noTemplateHint')}
                </Typography>
              </Alert>
            ) : (
              <Box
                sx={{
                  ...(CARD_VARIANTS.default.styles as object),
                  p: 2,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 2,
                  overflowY: 'auto',
                  minHeight: 0,
                }}
              >
                <Box>
                  <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                    {t('templates.sections.fields')}
                  </Typography>
                  <FieldDefsTable
                    fields={fieldDefs.fields}
                    disabled={!writable}
                    onChange={fields => setFieldDefs(previous => ({ ...previous, fields }))}
                  />
                </Box>

                <Divider />

                <Box>
                  <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
                    {t('templates.sections.initial')}
                  </Typography>
                  <JsonEditorField
                    value={initialDraft}
                    onChange={setInitialDraft}
                    readOnly={!writable}
                    error={initialError}
                    minRows={6}
                    maxRows={16}
                  />
                </Box>

                <FormControl size="small" sx={{ maxWidth: 220 }}>
                  <InputLabel id="state-theme-label">{t('templates.sections.theme')}</InputLabel>
                  <Select
                    labelId="state-theme-label"
                    label={t('templates.sections.theme')}
                    disabled={!writable}
                    // 空串与 `default` 都是默认主题（接口说明里两者都收），下拉里统一按 `default` 显示
                    value={fieldDefs.theme || 'default'}
                    onChange={event =>
                      setFieldDefs(previous => ({ ...previous, theme: event.target.value }))
                    }
                  >
                    {STATE_THEMES.map(themeKey => (
                      <MenuItem key={themeKey} value={themeKey}>
                        {t(`templates.theme.${themeKey}`)}
                      </MenuItem>
                    ))}
                  </Select>
                </FormControl>

                <Box>
                  <ActionButton
                    tone="primary"
                    disabled={!writable || saveMutation.isPending}
                    onClick={save}
                  >
                    {tCommon('actions.save')}
                  </ActionButton>
                </Box>
              </Box>
            )}
          </>
        )}
      </Box>

      <NekroDialog
        open={initvarOpen}
        onClose={() => (busy ? undefined : setInitvarOpen(false))}
        title={t('templates.initvarDialog.title')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" disabled={busy} onClick={() => setInitvarOpen(false)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton tone="primary" disabled={busy} onClick={() => void runInitvar()}>
              {t('templates.initvarDialog.confirm')}
            </ActionButton>
          </>
        }
      >
        <Stack spacing={1.5} sx={{ pt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            {t('templates.initvarDialog.description')}
          </Typography>
          {dialogError && <Alert severity="error">{dialogError}</Alert>}
          {initvarQuery.isFetching ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}>
              <CircularProgress size={22} />
            </Box>
          ) : (
            <FormControl fullWidth size="small">
              <InputLabel id="state-initvar-entry-label">
                {t('templates.initvarDialog.entryLabel')}
              </InputLabel>
              <Select
                labelId="state-initvar-entry-label"
                label={t('templates.initvarDialog.entryLabel')}
                value={initvarChoice}
                onChange={event =>
                  setInitvarChoice(
                    event.target.value === 'all' ? 'all' : Number(event.target.value)
                  )
                }
              >
                <MenuItem value="all">{t('templates.initvarDialog.all')}</MenuItem>
                {(initvarQuery.data?.items ?? []).map((entry: StateInitvarEntry) => (
                  <MenuItem key={entry.entry_id} value={entry.entry_id}>
                    {`#${entry.entry_id} ${entry.title}`}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}
          {!initvarQuery.isFetching && (initvarQuery.data?.items ?? []).length === 0 && (
            <Typography variant="caption" color="text.secondary">
              {t('templates.initvarDialog.empty')}
            </Typography>
          )}
        </Stack>
      </NekroDialog>

      <NekroDialog
        open={builtinOpen}
        onClose={() => (busy ? undefined : setBuiltinOpen(false))}
        title={t('templates.builtinDialog.title')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton tone="ghost" disabled={busy} onClick={() => setBuiltinOpen(false)}>
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="primary"
              disabled={busy || builtinKinds.length === 0}
              onClick={() => void runBuiltin()}
            >
              {t('templates.builtinDialog.confirm')}
            </ActionButton>
          </>
        }
      >
        <Stack spacing={1.5} sx={{ pt: 1 }}>
          <Typography variant="body2" color="text.secondary">
            {t('templates.builtinDialog.description')}
          </Typography>
          {dialogError && <Alert severity="error">{dialogError}</Alert>}
          {(['scene', 'inventory'] as StateBuiltinKind[]).map(kind => (
            <FormControlLabel
              key={kind}
              control={
                <Checkbox
                  checked={builtinKinds.includes(kind)}
                  onChange={event =>
                    setBuiltinKinds(previous =>
                      event.target.checked
                        ? [...previous, kind]
                        : previous.filter(item => item !== kind)
                    )
                  }
                />
              }
              label={t(`templates.builtinDialog.${kind}`)}
            />
          ))}
          {builtinKinds.length === 0 && (
            <Typography variant="caption" color="warning.main">
              {t('templates.builtinDialog.needOne')}
            </Typography>
          )}
        </Stack>
      </NekroDialog>
    </Box>
  )
}
