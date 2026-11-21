import { useMemo, useState } from 'react'
import { Box } from '@mui/material'
import { useTranslation } from 'react-i18next'
import SegmentedControl from '../common/SegmentedControl'
import PromptRoundsPanel from './PromptRoundsPanel'
import SummariesPanel from './SummariesPanel'
import type { MemoryScopeParams } from '../../services/api/memory'

/**
 * 频道详情里的「记忆」标签
 *
 * 作用域用**频道自己的**实例（频道详情响应里的 `adapter_key` / `instance_key`），而不是
 * 顶栏的实例切换器：频道属于哪个实例是确定的，用顶栏会在「平台方从一个实例的频道列表点进
 * 另一个实例的频道」时看错数据；租户也必须带实例，否则后端直接 400。
 *
 * 不从会话标识里解析实例：它的格式随适配器而定，解析错了就是拿另一个实例的作用域去查。
 * 存量的单实例频道实例段为空，这时不带实例——平台方按全量看，租户本来就看不到这类频道。
 */

interface ChannelMemoryPanelProps {
  chatKey: string
  adapterKey: string
  instanceKey: string
}

type MemoryView = 'promptRounds' | 'summaries'

export default function ChannelMemoryPanel({
  chatKey,
  adapterKey,
  instanceKey,
}: ChannelMemoryPanelProps) {
  const { t } = useTranslation('memory')
  const [view, setView] = useState<MemoryView>('promptRounds')

  const scope = useMemo<MemoryScopeParams>(
    () =>
      instanceKey
        ? { instance_key: instanceKey, adapter_key: adapterKey || undefined }
        : {},
    [instanceKey, adapterKey]
  )

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        gap: 1.5,
        minWidth: 0,
        p: { xs: 1.5, sm: 2 },
      }}
    >
      <Box sx={{ flexShrink: 0 }}>
        <SegmentedControl
          value={view}
          options={[
            { value: 'promptRounds', label: t('tabs.promptRounds') },
            { value: 'summaries', label: t('tabs.summaries') },
          ]}
          onChange={setView}
        />
      </Box>

      {view === 'promptRounds' ? (
        <PromptRoundsPanel scope={scope} chatKey={chatKey} />
      ) : (
        <SummariesPanel scope={scope} fixedChatKey={chatKey} />
      )}
    </Box>
  )
}
