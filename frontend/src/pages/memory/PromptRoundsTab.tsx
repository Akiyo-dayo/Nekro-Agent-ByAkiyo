import { useMemo } from 'react'
import { Box, Typography } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import FilterSelect from '../../components/common/FilterSelect'
import PromptRoundsPanel from '../../components/memory/PromptRoundsPanel'
import { useMemoryScopeOptions } from '../../components/memory/useMemoryScopeOptions'
import type { MemoryScopeParams } from '../../services/api/memory'

/**
 * 「提示词明细」标签
 *
 * 明细按频道记录，所以这一页先要一个频道：没有频道就没有可看的东西，此时在表格
 * 位置放一句空态，而不是发一个必然 400 的请求。频道与页码都进 URL，刷新后还在。
 */
function parsePage(value: string | null): number {
  if (value === null || value.trim() === '') return 1
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 1 ? parsed : 1
}

export default function PromptRoundsTab({ scope }: { scope: MemoryScopeParams }) {
  const { t } = useTranslation('memory')
  const [searchParams, setSearchParams] = useSearchParams()
  const { data: options } = useMemoryScopeOptions(scope)

  const chatKey = searchParams.get('chat') ?? ''
  const page = parsePage(searchParams.get('page'))

  const channelOptions = useMemo(
    () =>
      (options?.channels ?? []).map(channel => ({
        value: channel.chat_key,
        label: channel.channel_name || channel.chat_key,
      })),
    [options]
  )

  const updateQuery = (next: { chat?: string; page?: number }) => {
    const params = new URLSearchParams(searchParams)
    if (next.chat !== undefined) {
      if (next.chat) params.set('chat', next.chat)
      else params.delete('chat')
      // 换频道后旧页码不再有意义
      params.delete('page')
    }
    if (next.page !== undefined) {
      if (next.page > 1) params.set('page', String(next.page))
      else params.delete('page')
    }
    setSearchParams(params)
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0 }}>
      <Box sx={{ width: { xs: '100%', sm: 260 }, flexShrink: 0 }}>
        <FilterSelect
          label={t('filters.channel')}
          value={chatKey}
          options={channelOptions}
          onChange={value => updateQuery({ chat: value })}
        />
      </Box>

      {chatKey ? (
        <PromptRoundsPanel
          scope={scope}
          chatKey={chatKey}
          page={page}
          onPageChange={next => updateQuery({ page: next })}
        />
      ) : (
        <Box
          sx={{
            minHeight: 200,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 0.5,
            textAlign: 'center',
            p: 3,
          }}
        >
          <Typography variant="subtitle1" fontWeight={700}>
            {t('promptRounds.pickChannel')}
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 420 }}>
            {t('promptRounds.pickChannelHint')}
          </Typography>
        </Box>
      )}
    </Box>
  )
}
