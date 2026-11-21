import { useMemo } from 'react'
import { Box, Stack, Typography, alpha, useTheme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { diffSentences, hasChanges } from './textDiff'

/**
 * 两版摘要的对比：新加的句子标绿、删掉的句子标红划线，没变的照常显示
 *
 * 只读、纯文本；摘要正文是模型写的，这里不渲染任何 HTML。
 */
export default function SummaryDiffView({ before, after }: { before: string; after: string }) {
  const theme = useTheme()
  const { t } = useTranslation('memory')
  const parts = useMemo(() => diffSentences(before, after), [before, after])

  if (!hasChanges(parts)) {
    return (
      <Typography variant="body2" color="text.secondary">
        {t('summaries.diff.noChanges')}
      </Typography>
    )
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <Stack direction="row" spacing={1.5} sx={{ flexWrap: 'wrap' }}>
        <Typography variant="caption" sx={{ color: theme.palette.success.main }}>
          {t('summaries.diff.legendAdded')}
        </Typography>
        <Typography variant="caption" sx={{ color: theme.palette.error.main }}>
          {t('summaries.diff.legendRemoved')}
        </Typography>
      </Stack>
      <Typography
        component="div"
        variant="body2"
        sx={{
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          border: 1,
          borderColor: 'divider',
          borderRadius: 1,
          p: 1.25,
          maxHeight: 360,
          overflowY: 'auto',
        }}
      >
        {parts.map((part, index) =>
          part.kind === 'same' ? (
            <span key={index}>{part.text}</span>
          ) : (
            <Box
              key={index}
              component={part.kind === 'added' ? 'ins' : 'del'}
              sx={{
                textDecoration: part.kind === 'added' ? 'none' : 'line-through',
                bgcolor: alpha(
                  part.kind === 'added' ? theme.palette.success.main : theme.palette.error.main,
                  0.14
                ),
                color: part.kind === 'added' ? 'text.primary' : 'text.secondary',
                borderRadius: 0.5,
              }}
            >
              {part.text}
            </Box>
          )
        )}
      </Typography>
    </Box>
  )
}
