import { useState } from 'react'
import { useParams, useOutletContext } from 'react-router-dom'
import { Box, Typography, Alert } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import ConfigTable from '../../components/common/ConfigTable'
import QueryErrorState from '../../components/common/QueryErrorState'
import { createConfigService } from '../../services/api/unified-config'
import { AdapterDetailInfo } from '../../services/api/adapters'
import { isNotFoundError } from '../../services/api/axios'

interface AdapterContextType {
  adapterInfo: AdapterDetailInfo
}

export default function AdapterConfigPage() {
  const { adapterKey } = useParams<{ adapterKey: string }>()
  const { adapterInfo } = useOutletContext<AdapterContextType>()
  const [searchText, setSearchText] = useState<string>('')
  const { t } = useTranslation('adapter')

  // 创建适配器配置服务，使用 adapter_${adapterKey} 作为配置键
  const configKey = `adapter_${adapterKey}`
  const configService = createConfigService(configKey)

  // 获取适配器配置列表
  const {
    data: configs = [],
    refetch,
    isLoading,
    isLoadingError,
    error,
  } = useQuery({
    queryKey: ['adapter-configs', adapterKey],
    queryFn: () => configService.getConfigList(configKey),
    enabled: adapterInfo.has_config, // 只有当适配器支持配置时才执行查询
  })

  // 如果适配器不支持配置，显示提示信息
  if (!adapterInfo.has_config) {
    return (
      <Box sx={{ p: 3 }}>
        <Alert severity="info">
          <Typography variant="h6" gutterBottom>
            {t('config.notSupportedTitle')}
          </Typography>
          {t('config.notSupportedMessage')}
        </Alert>
      </Box>
    )
  }

  const handleSearchChange = (text: string) => {
    setSearchText(text)
  }

  const handleRefresh = () => {
    refetch()
  }

  return (
    <Box
      sx={{
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        p: { xs: 1, sm: 2 },
      }}
    >
      {/* 失败分支必须排在表格前面：`GET /config/list/adapter_*` 是 `Role.Admin`，只读租户成员得 403，
          没有这一支时页面落成「空配置表 + 『暂无 XX 配置项』」——与 #52c 要消灭的「把 403 渲染成 0 / 空表」
          同一形态（#52e ①）。做法与那一批一致：403 与其它失败分开说且 403 不摆重试按钮（组件内部保证）；
          404 仍说「不存在」，不能交给通用失败态——后端用 404 刻意藏存在性，改口说「无权查看」等于
          把它藏起来的信息又讲了出来。
          闸门是 `isLoadingError`（失败且手里没有数据）而不是 `isError`：后者在「已有数据、之后某次
          refetch 失败」时也为真，会把整张表卸载，用户未保存的编辑随组件一起丢掉。 */}
      {isLoadingError ? (
        isNotFoundError(error) ? (
          <Alert severity="error" variant="outlined">
            {t('config.notFound')}
          </Alert>
        ) : (
          <QueryErrorState error={error} onRetry={() => void refetch()} />
        )
      ) : (
        <ConfigTable
          configKey={configKey}
          configService={configService}
          configs={configs}
          loading={isLoading}
          searchText={searchText}
          onSearchChange={handleSearchChange}
          onRefresh={handleRefresh}
          showSearchBar={true}
          showToolbar={true}
          emptyMessage={t('config.emptyMessage', { name: adapterInfo.name })}
        />
      )}
    </Box>
  )
}
