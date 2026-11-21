import { useEffect, useRef, useState } from 'react'
import { Alert, Box, CircularProgress, Divider, Drawer, Stack, Typography } from '@mui/material'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import { useNotification } from '../../hooks/useNotification'
import { isNotFoundError } from '../../services/api/axios'
import {
  stateTableApi,
  type StateChannelDetail,
  type StateChannelItem,
  type StatePersonDetail,
  type StatePersonItem,
  type StateScope,
  type StateSnapshotDetail,
} from '../../services/api/stateTable'
import JsonEditorField from './JsonEditorField'
import SnapshotList from './SnapshotList'
import { isConflictError, parseStateObject } from './stateUtils'

/**
 * 频道状态与个人档案共用的详情对话框
 *
 * 两种目标的数据形状不同（详情接口给的东西也不完全一样），但「看状态 → 改 → 看快照」
 * 这条路径是同一件事；分成两个对话框只会让快照那部分维护两遍。
 * 打开时的 `version` 由详情接口给出，保存与回滚都带着它 —— 对不上就是别人先改过了。
 */
export interface StateDetailDialogProps {
  scope: StateScope
  kind: 'channel' | 'person'
  /** 列表那一行；标题、频道标识与显示名都从这里来 */
  target: StateChannelItem | StatePersonItem
  onClose: () => void
}

export default function StateDetailDialog({ scope, kind, target, onClose }: StateDetailDialogProps) {
  const { t } = useTranslation('state')
  const { t: tCommon } = useTranslation('common')
  const notification = useNotification()
  const queryClient = useQueryClient()

  const scopeKey = `${scope.adapter_key}|${scope.instance_key}`
  const isChannel = kind === 'channel'

  const detailQuery = useQuery<StateChannelDetail | StatePersonDetail>({
    queryKey: ['state-table', kind, scopeKey, target.id],
    queryFn: () =>
      isChannel
        ? stateTableApi.getChannel(scope, target.id)
        : stateTableApi.getPerson(scope, target.id),
    retry: false,
    // 切回窗口时不重拉：编辑框里改到一半的内容不能被悄悄换掉
    refetchOnWindowFocus: false,
  })

  const detail = detailQuery.data
  const version = detail?.version ?? target.version

  const [draft, setDraft] = useState('')
  const [jsonError, setJsonError] = useState('')
  const [resetOpen, setResetOpen] = useState(false)
  const [imageUrl, setImageUrl] = useState('')
  const [imageError, setImageError] = useState('')
  const [imageLoading, setImageLoading] = useState(false)

  // 服务端那一份的版本变了才把编辑框换掉：点开对话框、保存成功、版本冲突后重拉都会换版本；
  // 版本没变的重拉（比如页面上的「刷新」）不动编辑框，免得改到一半的内容被悄悄覆盖
  const shownVersion = useRef<number | null>(null)
  useEffect(() => {
    if (!detail || shownVersion.current === detail.version) return
    shownVersion.current = detail.version
    setDraft(JSON.stringify(detail.data ?? {}, null, 2))
    setJsonError('')
  }, [detail])

  // 换图或关对话框时撤掉上一张：object URL 不撤会一直占着内存
  useEffect(() => {
    if (!imageUrl) return
    return () => URL.revokeObjectURL(imageUrl)
  }, [imageUrl])

  const refreshDetail = () => {
    void detailQuery.refetch()
    void queryClient.invalidateQueries({ queryKey: ['state-table'] })
  }

  const afterChange = (message: string) => {
    notification.success(message)
    refreshDetail()
  }

  const saveMutation = useMutation({
    mutationFn: (payload: { data: Record<string, unknown>; version: number }) =>
      isChannel
        ? stateTableApi.updateChannel(scope, target.id, payload.data, payload.version)
        : stateTableApi.updatePerson(scope, target.id, payload.data, payload.version),
    onSuccess: () => afterChange(t('detail.saveSuccess')),
    onError: (error: unknown) => {
      if (isConflictError(error)) {
        notification.warning(t('detail.conflict'))
        refreshDetail()
        return
      }
      notification.error(error instanceof Error ? error.message : String(error))
    },
  })

  const resetMutation = useMutation({
    mutationFn: () =>
      isChannel
        ? stateTableApi.resetChannel(scope, target.id)
        : stateTableApi.resetPerson(scope, target.id),
    onSuccess: () => {
      setResetOpen(false)
      afterChange(t('detail.resetSuccess'))
    },
    onError: (error: unknown) => {
      setResetOpen(false)
      if (isConflictError(error)) {
        notification.warning(t('detail.conflict'))
        refreshDetail()
        return
      }
      notification.error(error instanceof Error ? error.message : String(error))
    },
  })

  const resumeMutation = useMutation({
    mutationFn: () => stateTableApi.resumeChannel(scope, target.id),
    onSuccess: () => afterChange(t('detail.resumeSuccess')),
    onError: (error: unknown) => {
      if (isConflictError(error)) {
        notification.warning(t('detail.conflict'))
        refreshDetail()
        return
      }
      notification.error(error instanceof Error ? error.message : String(error))
    },
  })

  const handleSave = () => {
    const parsed = parseStateObject(draft)
    if (!parsed.ok) {
      setJsonError(t('detail.invalidJson'))
      return
    }
    setJsonError('')
    saveMutation.mutate({ data: parsed.value, version })
  }

  /** 预览状态图：失败时只显示后端给的原因，且不自动重试（冷却与画图位都在服务端） */
  const loadImage = async () => {
    if (!isChannel) return
    setImageError('')
    setImageLoading(true)
    try {
      const blob = await stateTableApi.getChannelImage(scope, target.id)
      setImageUrl(URL.createObjectURL(blob))
    } catch (error) {
      // 后端给的原因（「这个频道刚画过状态图，N 秒后再试」「现在画不了状态图……」）在 message 里；
      // 拿不到才用前端自己的文案。失败后不自动重试：冷却与画图位都在服务端。
      setImageError((error instanceof Error && error.message) || t('detail.previewFailed'))
    } finally {
      setImageLoading(false)
    }
  }

  const paused = isChannel && (detail as StateChannelDetail | undefined)?.paused === true
  const busy = saveMutation.isPending || resetMutation.isPending || resumeMutation.isPending

  const title = isChannel
    ? (target as StateChannelItem).channel_name || (target as StateChannelItem).chat_key
    : (target as StatePersonItem).nickname || (target as StatePersonItem).platform_userid

  const channelChatKey = isChannel ? (target as StateChannelItem).chat_key : ''

  const fetchSnapshot = async (snapshotId: number): Promise<StateSnapshotDetail> =>
    isChannel
      ? stateTableApi.getChannelSnapshot(scope, target.id, snapshotId)
      : stateTableApi.getPersonSnapshot(scope, target.id, snapshotId)

  const rollback = async (snapshotId: number, currentVersion: number) =>
    isChannel
      ? stateTableApi.rollbackChannel(scope, target.id, snapshotId, currentVersion)
      : stateTableApi.rollbackPerson(scope, target.id, snapshotId, currentVersion)

  return (
    <Drawer
      anchor="right"
      open
      onClose={onClose}
      PaperProps={{
        sx: { width: { xs: '100%', sm: 560 }, maxWidth: '100%', bgcolor: 'background.paper' },
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
          }}
        >
          <Stack direction="row" alignItems="center" spacing={1}>
            <Typography variant="h6" fontWeight={700} sx={{ minWidth: 0 }}>
              {title}
            </Typography>
            <Box sx={{ flex: 1 }} />
            {isChannel && (
              <ActionButton
                tone="secondary"
                size="small"
                disabled={imageLoading}
                onClick={() => void loadImage()}
              >
                {imageLoading ? t('detail.previewLoading') : t('detail.previewImage')}
              </ActionButton>
            )}
            <IconActionButton onClick={onClose} aria-label={tCommon('actions.close')}>
              <CloseRoundedIcon />
            </IconActionButton>
          </Stack>
          {!isChannel && (
            <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 0.5 }}>
              {(target as StatePersonItem).platform_userid}
            </Typography>
          )}
          {channelChatKey && (
            <Typography
              variant="caption"
              color="text.secondary"
              display="block"
              sx={{ mt: 0.5, wordBreak: 'break-all' }}
            >
              {channelChatKey}
            </Typography>
          )}
        </Box>
        <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', px: 2, py: 2 }}>
      {detailQuery.isError ? (
        isNotFoundError(detailQuery.error) ? (
          <Alert severity="info">{t('notEnabled')}</Alert>
        ) : (
          <QueryErrorState error={detailQuery.error} onRetry={() => void detailQuery.refetch()} compact />
        )
      ) : detailQuery.isPending || !detail ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={24} />
        </Box>
      ) : (
        <Stack spacing={2} sx={{ pt: 1 }}>
          {imageError && <Alert severity="error">{imageError}</Alert>}
          {imageUrl && (
            <Box
              component="img"
              src={imageUrl}
              alt={t('detail.previewImage')}
              sx={{ maxWidth: '100%', borderRadius: 1, border: '1px solid', borderColor: 'divider' }}
            />
          )}

          <Box>
            <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
              {t('detail.currentState')}
            </Typography>
            <JsonEditorField
              value={draft}
              onChange={setDraft}
              error={jsonError}
              label={t('detail.jsonLabel')}
            />
          </Box>

          <Divider />

          <Box>
            <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 1 }}>
              {t('detail.snapshots')}
            </Typography>
            <SnapshotList
              snapshots={detail.snapshots ?? []}
              version={version}
              fetchSnapshot={fetchSnapshot}
              rollback={rollback}
              onChanged={refreshDetail}
            />
          </Box>

          <NekroDialog
            open={resetOpen}
            onClose={() => (resetMutation.isPending ? undefined : setResetOpen(false))}
            title={t('detail.resetConfirmTitle')}
            maxWidth="xs"
            actions={
              <>
                <ActionButton
                  tone="ghost"
                  onClick={() => setResetOpen(false)}
                  disabled={resetMutation.isPending}
                >
                  {tCommon('actions.cancel')}
                </ActionButton>
                <ActionButton
                  tone="danger"
                  disabled={resetMutation.isPending}
                  onClick={() => resetMutation.mutate()}
                >
                  {tCommon('actions.reset')}
                </ActionButton>
              </>
            }
          >
            <Typography variant="body2" sx={{ pt: 1 }}>
              {t('detail.resetConfirmBody')}
            </Typography>
          </NekroDialog>
        </Stack>
      )}
        </Box>
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
            flexWrap: 'wrap',
          }}
        >
          {/* 动作吸在底部：状态树很长时编辑框会把「保存」推出视口 */}
          <ActionButton
            tone="secondary"
            disabled={busy || !detail}
            onClick={() => setResetOpen(true)}
          >
            {t('detail.reset')}
          </ActionButton>
          {paused && (
            <ActionButton tone="secondary" disabled={busy} onClick={() => resumeMutation.mutate()}>
              {t('detail.resume')}
            </ActionButton>
          )}
          <Box sx={{ flex: 1 }} />
          <ActionButton tone="ghost" onClick={onClose}>
            {tCommon('actions.close')}
          </ActionButton>
          <ActionButton tone="primary" disabled={busy || !detail} onClick={handleSave}>
            {tCommon('actions.save')}
          </ActionButton>
        </Box>
      </Box>
    </Drawer>
  )
}

/** 详情接口的两种返回都带 `snapshots`，这里给调用方一个统一的空值兜底 */
export type StateDetailData = StateChannelDetail | StatePersonDetail
