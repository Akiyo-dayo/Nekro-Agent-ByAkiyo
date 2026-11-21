import { useEffect, useMemo, useState } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Chip,
  CircularProgress,
  Drawer,
  FormControlLabel,
  Stack,
  Switch,
  type SxProps,
  TextField,
  type Theme,
  Typography,
} from '@mui/material'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import ExpandMoreRoundedIcon from '@mui/icons-material/ExpandMoreRounded'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../common/ActionButton'
import IconActionButton from '../common/IconActionButton'
import QueryErrorState from '../common/QueryErrorState'
import SearchField from '../common/SearchField'
import { useNotification } from '../../hooks/useNotification'
import {
  rpPresetsApi,
  type RpPresetDetail,
  type RpPresetScope,
  type RpPresetUpdateRequest,
} from '../../services/api/rpPresets'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../theme/variants'
import PresetOwnerChip from './PresetOwnerChip'
import {
  chatHistoryIndex,
  isChatHistory,
  markerFillKey,
  placementLabel,
  placementOf,
} from './segments'

interface PresetDetailDrawerProps {
  open: boolean
  presetId: number | null
  scope: RpPresetScope
  onClose: () => void
  /** 保存成功：列表里的名称、段数或更新时间可能变了 */
  onChanged?: () => void
}

const CONTENT_MAX_HEIGHT = '19.2rem'

function enabledMapOf(preset: RpPresetDetail): Record<string, boolean> {
  return Object.fromEntries(preset.segments.map(segment => [segment.identifier, segment.enabled]))
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

/**
 * 预设详情抽屉
 *
 * 段落顺序就是发送顺序：`chatHistory` 那一行画成一条分隔带，别再当成普通段落排进去。
 * 只改名称与启用开关；正文、顺序与深度都只读（后端这一版不支持编辑）。脚上只提交
 * 真正改过的项，避免把一个没动过的开关也写进审计。
 */
export default function PresetDetailDrawer({
  open,
  presetId,
  scope,
  onClose,
  onChanged,
}: PresetDetailDrawerProps) {
  const { t } = useTranslation('tavern')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const [draftName, setDraftName] = useState('')
  const [nameTouched, setNameTouched] = useState(false)
  const [draftEnabled, setDraftEnabled] = useState<Record<string, boolean>>({})
  const [search, setSearch] = useState('')
  const [onlyEnabled, setOnlyEnabled] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)

  const queryKey = useMemo(
    () => ['prompt-lab', 'rp-preset', scope.instance_key ?? '', presetId],
    [scope.instance_key, presetId]
  )

  const detailQuery = useQuery({
    queryKey,
    queryFn: () => rpPresetsApi.get(presetId as number, scope),
    enabled: open && presetId !== null,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const data = detailQuery.data

  // 打开另一份预设或每次重新进来都回到初始视图；未保存的草稿不跨预设保留
  useEffect(() => {
    if (!open) return
    setSearch('')
    setOnlyEnabled(false)
    setExpanded(null)
    setNameTouched(false)
  }, [open, presetId])

  // 详情到位（或保存后换了一份数据）时，以服务端的值为新的基线
  useEffect(() => {
    if (!data) return
    setDraftName(data.name)
    setDraftEnabled(enabledMapOf(data))
    setNameTouched(false)
  }, [data])

  const dirtySegments = useMemo(() => {
    if (!data) return []
    return data.segments.filter(segment => {
      if (isChatHistory(segment)) return false
      const draft = draftEnabled[segment.identifier]
      return draft !== undefined && draft !== segment.enabled
    })
  }, [data, draftEnabled])

  // 草稿还没灌进来时不把「空字符串」当成一次改名；只改首尾空白也不算
  const nameDirty =
    data !== undefined && nameTouched && draftName.trim() !== data.name
  const nameInvalid = draftName.trim() === ''
  const dirtyCount = dirtySegments.length + (nameDirty ? 1 : 0)
  const readOnly = data?.read_only === true

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (!data || presetId === null) {
        throw new Error(tCommon('messages.operationFailed'))
      }
      const enabled: Record<string, boolean> = {}
      dirtySegments.forEach(segment => {
        enabled[segment.identifier] = draftEnabled[segment.identifier]
      })
      const patch: RpPresetUpdateRequest = {}
      if (nameDirty) patch.name = draftName.trim()
      if (Object.keys(enabled).length > 0) patch.enabled = enabled
      return rpPresetsApi.update(presetId, patch, scope)
    },
    onSuccess: updated => {
      queryClient.setQueryData(queryKey, updated)
      notification.success(t('lab.presets.detail.saved'))
      onChanged?.()
    },
    onError: (error: unknown) => {
      notification.error(errorText(error, tCommon('messages.saveFailed')))
    },
  })

  const discard = () => {
    if (!data) return
    setDraftName(data.name)
    setDraftEnabled(enabledMapOf(data))
  }

  const historyIndex = data ? chatHistoryIndex(data.segments) : -1

  const visibleSegments = useMemo(() => {
    if (!data) return []
    const query = search.trim().toLowerCase()
    return data.segments
      .map((segment, index) => ({ segment, index }))
      .filter(({ segment }) => {
        // 分隔带是理解「前 / 后」的唯一坐标，过滤时始终留着
        if (isChatHistory(segment)) return true
        if (onlyEnabled && !(draftEnabled[segment.identifier] ?? segment.enabled)) {
          return false
        }
        if (!query) return true
        const name = (segment.name || segment.identifier).toLowerCase()
        return name.includes(query)
      })
  }, [data, search, onlyEnabled, draftEnabled])

  const notes = (data?.notes ?? [])
    .map(note => (typeof note.text === 'string' ? note.text : ''))
    .filter(text => text.trim() !== '')

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
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
            display: 'flex',
            alignItems: 'center',
            gap: 1,
          }}
        >
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <TextField
              label={t('lab.presets.detail.name')}
              value={draftName}
              onChange={event => {
                setNameTouched(true)
                setDraftName(event.target.value.slice(0, 128))
              }}
              size="small"
              fullWidth
              disabled={readOnly || data === undefined}
              error={nameTouched && nameInvalid}
              helperText={
                nameTouched && nameInvalid ? t('lab.presets.detail.nameRequired') : undefined
              }
            />
          </Box>
          {data ? (
            <PresetOwnerChip global={data.owner_instance_key === ''} />
          ) : null}
          <IconActionButton onClick={onClose} aria-label={tCommon('actions.close')}>
            <CloseRoundedIcon />
          </IconActionButton>
        </Box>

        {detailQuery.isPending ? (
          <Box
            sx={{
              flex: 1,
              minHeight: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <CircularProgress />
          </Box>
        ) : detailQuery.error ? (
          <Box sx={{ p: 2 }}>
            <QueryErrorState
              error={detailQuery.error}
              onRetry={() => void detailQuery.refetch()}
            />
          </Box>
        ) : data ? (
          <Box
            sx={{
              flex: 1,
              minHeight: 0,
              display: 'flex',
              flexDirection: 'column',
              gap: 1.5,
              px: 2,
              py: 2,
              overflow: 'hidden',
            }}
          >
            <Alert severity="info" sx={{ flexShrink: 0 }}>
              {t('lab.presets.detail.notice')}
            </Alert>

            <Accordion
              disableGutters
              elevation={0}
              sx={{
                flexShrink: 0,
                border: '1px solid',
                borderColor: 'divider',
                borderRadius: 1,
                '&::before': { display: 'none' },
              }}
            >
              <AccordionSummary expandIcon={<ExpandMoreRoundedIcon />}>
                <Typography variant="body2" fontWeight={600}>
                  {t('lab.presets.detail.report', { count: notes.length })}
                </Typography>
              </AccordionSummary>
              <AccordionDetails>
                {notes.length === 0 ? (
                  <Typography variant="caption" color="text.secondary">
                    {t('lab.presets.detail.reportEmpty')}
                  </Typography>
                ) : (
                  <Box sx={{ maxHeight: 200, overflow: 'auto', ...(UNIFIED_TABLE_STYLES.scrollbar as object) }}>
                    <Stack component="ul" spacing={0.5} sx={{ m: 0, pl: 2.5 }}>
                      {notes.map((text, index) => (
                        <Typography key={`${index}-${text}`} component="li" variant="body2">
                          {text}
                        </Typography>
                      ))}
                    </Stack>
                  </Box>
                )}
              </AccordionDetails>
            </Accordion>

            <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
              <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
                <Box sx={{ flex: 1, minWidth: 180 }}>
                  <SearchField
                    label={t('lab.presets.detail.search')}
                    value={search}
                    onChange={setSearch}
                    onClear={() => setSearch('')}
                    placeholder={t('lab.presets.detail.searchPlaceholder')}
                  />
                </Box>
                <FormControlLabel
                  control={
                    <Switch
                      size="small"
                      checked={onlyEnabled}
                      onChange={event => setOnlyEnabled(event.target.checked)}
                    />
                  }
                  label={t('lab.presets.detail.onlyEnabled')}
                />
                <Typography variant="caption" color="text.secondary">
                  {t('lab.presets.detail.shown', {
                    shown: visibleSegments.length,
                    total: data.segments.length,
                  })}
                </Typography>
              </Stack>

              <Box
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1,
                  px: 1,
                  py: 0.5,
                  borderBottom: '1px solid',
                  borderColor: 'divider',
                  color: 'text.secondary',
                }}
              >
                <Box sx={{ width: 50, flexShrink: 0 }} />
                <Typography variant="caption" sx={{ flex: 1, minWidth: 0 }}>
                  {t('lab.presets.detail.columns.name')}
                </Typography>
                <Typography variant="caption" sx={{ width: 64, flexShrink: 0, textAlign: 'center' }}>
                  {t('lab.presets.detail.columns.role')}
                </Typography>
                <Typography variant="caption" sx={{ width: 90, flexShrink: 0, textAlign: 'right' }}>
                  {t('lab.presets.detail.columns.position')}
                </Typography>
                <Typography variant="caption" sx={{ width: 64, flexShrink: 0, textAlign: 'right' }}>
                  {t('lab.presets.detail.columns.chars')}
                </Typography>
              </Box>

              <Box
                sx={{
                  flex: 1,
                  minHeight: 0,
                  overflow: 'auto',
                  ...(UNIFIED_TABLE_STYLES.scrollbar as object),
                }}
              >
                {visibleSegments.length === 0 ? (
                  <Typography
                    variant="body2"
                    color="text.secondary"
                    sx={{ py: 2, textAlign: 'center' }}
                  >
                    {t('lab.presets.detail.noMatch')}
                  </Typography>
                ) : (
                  visibleSegments.map(({ segment, index }) => {
                    if (isChatHistory(segment)) {
                      return (
                        <Box
                          key={`${segment.identifier}-${index}`}
                          sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 1 }}
                        >
                          <Box
                            sx={{
                              flex: 1,
                              borderTop: '1px dashed',
                              borderColor: 'divider',
                            }}
                          />
                          <Typography variant="caption" color="text.secondary" noWrap>
                            {t('lab.presets.detail.marker.chatHistory')}
                          </Typography>
                          <Box
                            sx={{
                              flex: 1,
                              borderTop: '1px dashed',
                              borderColor: 'divider',
                            }}
                          />
                        </Box>
                      )
                    }

                    const placement = placementLabel(
                      placementOf(segment, index, historyIndex)
                    )
                    const fillKey = segment.marker ? markerFillKey(segment.identifier) : null
                    const canExpand = !segment.marker
                    const isExpanded = expanded === segment.identifier
                    return (
                      <Box
                        key={`${segment.identifier}-${index}`}
                        sx={{
                          borderBottom: '1px solid',
                          borderColor: 'divider',
                        }}
                      >
                        <Box
                          onClick={() => {
                            if (!canExpand) return
                            setExpanded(isExpanded ? null : segment.identifier)
                          }}
                          sx={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 1,
                            px: 1,
                            py: 0.5,
                            cursor: canExpand ? 'pointer' : 'default',
                            bgcolor: isExpanded ? 'action.hover' : 'transparent',
                            '&:hover': canExpand ? { bgcolor: 'action.hover' } : undefined,
                          }}
                        >
                          <Box sx={{ width: 50, flexShrink: 0 }}>
                            <Switch
                              size="small"
                              checked={draftEnabled[segment.identifier] ?? segment.enabled}
                              disabled={readOnly}
                              onClick={event => event.stopPropagation()}
                              onChange={event =>
                                setDraftEnabled(current => ({
                                  ...current,
                                  [segment.identifier]: event.target.checked,
                                }))
                              }
                              inputProps={{
                                'aria-label': t('lab.presets.detail.toggleSegment', {
                                  name: segment.name || segment.identifier,
                                }),
                              }}
                            />
                          </Box>
                          <Box sx={{ flex: 1, minWidth: 0 }}>
                            <Typography variant="body2" fontWeight={600} noWrap>
                              {segment.name || segment.identifier}
                            </Typography>
                            {segment.marker ? (
                              <Typography variant="caption" color="text.secondary" display="block">
                                {t('lab.presets.detail.marker.fillPrefix')}
                                {fillKey ? `：${t(fillKey)}` : ''}
                              </Typography>
                            ) : null}
                          </Box>
                          <Chip
                            size="small"
                            label={segment.role}
                            sx={
                              [CHIP_VARIANTS.base(true), { flexShrink: 0 }] as SxProps<Theme>
                            }
                          />
                          <Typography
                            variant="caption"
                            color="text.secondary"
                            sx={{ width: 90, flexShrink: 0, textAlign: 'right' }}
                          >
                            {t(placement.key, placement.options)}
                          </Typography>
                          <Typography
                            variant="caption"
                            color="text.secondary"
                            sx={{
                              width: 64,
                              flexShrink: 0,
                              textAlign: 'right',
                              fontVariantNumeric: 'tabular-nums',
                            }}
                          >
                            {segment.marker ? '—' : segment.chars.toLocaleString()}
                          </Typography>
                        </Box>
                        {isExpanded && !segment.marker ? (
                          <Box
                            component="pre"
                            sx={{
                              m: 0,
                              mx: 1,
                              mb: 1,
                              p: 1,
                              bgcolor: 'action.hover',
                              borderRadius: 1,
                              fontFamily: 'monospace',
                              fontSize: '0.8rem',
                              lineHeight: 1.5,
                              whiteSpace: 'pre-wrap',
                              wordBreak: 'break-word',
                              maxHeight: CONTENT_MAX_HEIGHT,
                              overflow: 'auto',
                            }}
                          >
                            {segment.content || t('lab.presets.detail.emptyContent')}
                          </Box>
                        ) : null}
                      </Box>
                    )
                  })
                )}
              </Box>
            </Box>
          </Box>
        ) : null}

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
            display: 'flex',
            alignItems: 'center',
            gap: 1,
          }}
        >
          <Typography variant="body2" color="text.secondary">
            {dirtyCount > 0 ? t('lab.presets.detail.unsaved', { count: dirtyCount }) : ''}
          </Typography>
          <Box sx={{ flex: 1 }} />
          <ActionButton
            tone="ghost"
            onClick={discard}
            disabled={dirtyCount === 0 || saveMutation.isPending || readOnly}
          >
            {t('lab.presets.detail.discard')}
          </ActionButton>
          <ActionButton
            tone="primary"
            onClick={() => saveMutation.mutate()}
            disabled={
              dirtyCount === 0 ||
              nameInvalid ||
              readOnly ||
              saveMutation.isPending
            }
          >
            {saveMutation.isPending
              ? t('lab.presets.detail.saving')
              : t('lab.presets.detail.save')}
          </ActionButton>
        </Box>
      </Box>
    </Drawer>
  )
}
