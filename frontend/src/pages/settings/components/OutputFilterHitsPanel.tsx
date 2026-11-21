/**
 * 输出过滤的命中统计与起步词表
 *
 * 只读面板，挂在「系统配置」页的过滤词配置上方：统计来自运行时命中记录，
 * 被拒规则来自当前作用于该实例（全局视图下是全局配置）的词表 / 正则编译结果。
 * 起步词表只提供查看与复制，不会自动写入任何配置。
 */

import { useState } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import RefreshIcon from '@mui/icons-material/Refresh'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { outputFilterApi } from '../../../services/api/output-filter'

interface OutputFilterHitsPanelProps {
  /** 实例作用域下的适配器标识；不传即全局视图 */
  adapterKey?: string
  /** 实例作用域下的实例标识；不传即全局视图 */
  instanceKey?: string
}

type HitRow = {
  adapter_key: string
  instance_key: string
  kind: string
  rule: string
  count: number
  last_hit_at: string | null
}

function formatHitTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString()
}

export default function OutputFilterHitsPanel({ adapterKey, instanceKey }: OutputFilterHitsPanelProps) {
  const { t } = useTranslation('settings')
  const [starterOpen, setStarterOpen] = useState(false)
  const [starterText, setStarterText] = useState('')
  const [starterLoading, setStarterLoading] = useState(false)
  const [copied, setCopied] = useState(false)
  const isGlobalView = !instanceKey

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ['output-filter-hits', adapterKey ?? '', instanceKey ?? ''],
    queryFn: () => outputFilterApi.getHits({ adapterKey, instanceKey }),
  })

  const rows: HitRow[] =
    data?.instances.flatMap((instance) =>
      instance.rules.map((rule) => ({
        adapter_key: instance.adapter_key,
        instance_key: instance.instance_key,
        kind: rule.kind,
        rule: rule.rule,
        count: rule.count,
        last_hit_at: rule.last_hit_at,
      }))
    ) ?? []

  const kindLabel = (kind: string) => {
    if (kind === 'word') return t('outputFilter.kindWord')
    if (kind === 'regex') return t('outputFilter.kindRegex')
    return t('outputFilter.kindError')
  }

  const openStarterWords = async () => {
    setStarterOpen(true)
    setCopied(false)
    setStarterLoading(true)
    try {
      const response = await outputFilterApi.getStarterWords()
      setStarterText(response.text)
    } catch {
      setStarterText('')
    } finally {
      setStarterLoading(false)
    }
  }

  const copyStarterWords = async () => {
    try {
      await navigator.clipboard.writeText(starterText)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  return (
    <>
      <Accordion
        disableGutters
        sx={{
          border: 1,
          borderColor: 'divider',
          borderRadius: 2,
          '&:before': { display: 'none' },
        }}
      >
        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ minWidth: 0 }}>
            <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
              {t('outputFilter.title')}
            </Typography>
            {isLoading && <CircularProgress size={16} />}
          </Stack>
        </AccordionSummary>
        <AccordionDetails sx={{ pt: 0 }}>
          <Stack spacing={1.5}>
            {data && data.rejected_rules.length > 0 && (
              <Alert severity="warning">
                <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
                  {t('outputFilter.rejectedTitle')}
                </Typography>
                <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
                  {data.rejected_rules.map((item, index) => (
                    <li key={`${item.rule}-${index}`}>
                      <Typography variant="body2" sx={{ wordBreak: 'break-all' }}>
                        {item.rule} — {item.reason}
                      </Typography>
                    </li>
                  ))}
                </Box>
              </Alert>
            )}

            <Stack direction="row" spacing={1} justifyContent="flex-end" alignItems="center">
              <Button size="small" variant="outlined" onClick={openStarterWords}>
                {t('outputFilter.starterButton')}
              </Button>
              <Tooltip title={t('outputFilter.refresh')}>
                <span>
                  <IconButton size="small" onClick={() => void refetch()} disabled={isFetching}>
                    {isFetching ? <CircularProgress size={18} /> : <RefreshIcon fontSize="small" />}
                  </IconButton>
                </span>
              </Tooltip>
            </Stack>

            <TableContainer sx={{ maxHeight: 320 }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    {isGlobalView && <TableCell>{t('outputFilter.instance')}</TableCell>}
                    <TableCell>{t('outputFilter.kind')}</TableCell>
                    <TableCell>{t('outputFilter.rule')}</TableCell>
                    <TableCell align="right">{t('outputFilter.count')}</TableCell>
                    <TableCell>{t('outputFilter.lastHitAt')}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {rows.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={isGlobalView ? 5 : 4} align="center">
                        <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
                          {isLoading ? t('outputFilter.loading') : t('outputFilter.empty')}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ) : (
                    rows.map((row) => (
                      <TableRow key={`${row.adapter_key}-${row.instance_key}-${row.kind}-${row.rule}`}>
                        {isGlobalView && (
                          <TableCell>
                            {row.adapter_key} / {row.instance_key}
                          </TableCell>
                        )}
                        <TableCell>{kindLabel(row.kind)}</TableCell>
                        <TableCell sx={{ wordBreak: 'break-all' }}>{row.rule}</TableCell>
                        <TableCell align="right">{row.count}</TableCell>
                        <TableCell>{formatHitTime(row.last_hit_at)}</TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </TableContainer>

            <Typography variant="caption" color="text.secondary">
              {t('outputFilter.pluginHint')}
            </Typography>
          </Stack>
        </AccordionDetails>
      </Accordion>

      <Dialog open={starterOpen} onClose={() => setStarterOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle>{t('outputFilter.starterTitle')}</DialogTitle>
        <DialogContent>
          <Stack spacing={1.5} sx={{ pt: 1 }}>
            <Typography variant="body2" color="text.secondary">
              {t('outputFilter.starterHint')}
            </Typography>
            <TextField
              value={starterLoading ? t('outputFilter.loading') : starterText}
              multiline
              minRows={12}
              fullWidth
              InputProps={{ readOnly: true }}
              sx={{ '& textarea': { fontFamily: 'monospace', fontSize: 13 } }}
            />
            <Typography variant="caption" color="text.secondary">
              {t('outputFilter.starterNote')}
            </Typography>
          </Stack>
        </DialogContent>
        <DialogActions>
          {copied && (
            <Typography variant="caption" color="success.main" sx={{ mr: 'auto' }}>
              {t('outputFilter.copied')}
            </Typography>
          )}
          <Button onClick={copyStarterWords} disabled={starterLoading || !starterText}>
            {t('outputFilter.copy')}
          </Button>
          <Button onClick={() => setStarterOpen(false)}>{t('actions.close', { ns: 'common' })}</Button>
        </DialogActions>
      </Dialog>
    </>
  )
}
