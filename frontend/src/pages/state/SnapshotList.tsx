import { Fragment, useState } from 'react'
import {
  Alert,
  Box,
  CircularProgress,
  Collapse,
  SxProps,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Theme,
  Typography,
} from '@mui/material'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import StatusChip from '../../components/common/StatusChip'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import { useNotification } from '../../hooks/useNotification'
import type { StateSnapshot, StateSnapshotDetail, StateSnapshotOp } from '../../services/api/stateTable'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'
import { formatFullTime, formatListTime } from '../../utils/listTime'
import JsonEditorField from './JsonEditorField'
import { enumText, isConflictError, opPathText, resultTone } from './stateUtils'

/**
 * 快照与更新记录
 *
 * 频道与档案共用：两者给的是同一份形状，展示方式分两套只会让「同一件事在两个地方说法不同」。
 * 路径与原因都是模型或管理员写进来的文字，一律按纯文本渲染，不做任何富文本加工。
 */
export interface SnapshotListProps {
  snapshots: StateSnapshot[]
  /** 打开详情时那一版的版本号，回滚要带着它，对不上就是别人先改过了 */
  version: number
  fetchSnapshot: (snapshotId: number) => Promise<StateSnapshotDetail>
  rollback: (snapshotId: number, version: number) => Promise<unknown>
  /** 回滚成功或遇到版本冲突后重新拉详情 */
  onChanged: () => void
}

function OpLine({ text }: { text: string }) {
  return (
    <Typography variant="body2" sx={{ wordBreak: 'break-word' }}>
      {text}
    </Typography>
  )
}

export default function SnapshotList({
  snapshots,
  version,
  fetchSnapshot,
  rollback,
  onChanged,
}: SnapshotListProps) {
  const notification = useNotification()
  const { t } = useTranslation('state')
  const { t: tCommon } = useTranslation('common')

  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [viewing, setViewing] = useState<StateSnapshotDetail | null>(null)
  const [viewLoadingId, setViewLoadingId] = useState<number | null>(null)
  const [viewError, setViewError] = useState('')
  const [rollbackTarget, setRollbackTarget] = useState<StateSnapshot | null>(null)
  const [rollingBack, setRollingBack] = useState(false)

  const openSnapshot = async (snapshot: StateSnapshot) => {
    setViewError('')
    setViewLoadingId(snapshot.id)
    try {
      setViewing(await fetchSnapshot(snapshot.id))
    } catch (error) {
      setViewError(error instanceof Error ? error.message : String(error))
    } finally {
      setViewLoadingId(null)
    }
  }

  const confirmRollback = async () => {
    if (!rollbackTarget) return
    setRollingBack(true)
    try {
      await rollback(rollbackTarget.id, version)
      notification.success(t('snapshot.rollbackSuccess'))
      setRollbackTarget(null)
      onChanged()
    } catch (error) {
      if (isConflictError(error)) {
        notification.warning(t('detail.conflict'))
        setRollbackTarget(null)
        onChanged()
      } else {
        notification.error(error instanceof Error ? error.message : String(error))
      }
    } finally {
      setRollingBack(false)
    }
  }

  const appliedLines = (ops: StateSnapshotOp[]) => (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25 }}>
      <Typography variant="caption" color="text.secondary">
        {t('snapshot.applied')}
      </Typography>
      {ops.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          —
        </Typography>
      ) : (
        ops.map((op, index) => (
          <OpLine
            key={`applied-${index}`}
            text={t('snapshot.appliedOp', { op: op.op, path: opPathText(op) })}
          />
        ))
      )}
    </Box>
  )

  const rejectedLines = (ops: StateSnapshotOp[]) => (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25 }}>
      <Typography variant="caption" color="text.secondary">
        {t('snapshot.rejected')}
      </Typography>
      {ops.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          —
        </Typography>
      ) : (
        ops.map((op, index) => (
          <OpLine
            key={`rejected-${index}`}
            text={t('snapshot.rejectedOp', {
              path: opPathText(op),
              reason: op.reason || t('snapshot.noReason'),
            })}
          />
        ))
      )}
    </Box>
  )

  if (snapshots.length === 0) {
    return (
      <Box sx={{ py: 2 }}>
        <Typography variant="body2" color="text.secondary">
          {t('snapshot.emptyTitle')}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {t('snapshot.emptyHint')}
        </Typography>
      </Box>
    )
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {viewError && <Alert severity="error">{viewError}</Alert>}
      <Box sx={{ overflowX: 'auto' }}>
        <Table size="small" sx={{ minWidth: 640 }}>
          <TableHead>
            <TableRow>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {tCommon('common.createdAt')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('snapshot.source')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {t('snapshot.result')}
              </TableCell>
              <TableCell sx={UNIFIED_TABLE_STYLES.header as SxProps<Theme>}>
                {tCommon('common.action')}
              </TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {snapshots.map(snapshot => (
              <Fragment key={snapshot.id}>
                <TableRow
                  hover
                  sx={{ cursor: 'pointer' }}
                  onClick={() => setExpandedId(current => (current === snapshot.id ? null : snapshot.id))}
                >
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                    <SimpleTooltip title={formatFullTime(snapshot.create_time)}>
                      <span>
                        <Typography variant="caption" color="text.secondary">
                          {formatListTime(snapshot.create_time)}
                        </Typography>
                      </span>
                    </SimpleTooltip>
                    {/* 编辑人与模型组为空时整行不显示：空标签只是噪音 */}
                    {snapshot.editor && (
                      <Typography variant="caption" color="text.secondary" display="block">
                        {`${t('snapshot.editor')}：${snapshot.editor}`}
                      </Typography>
                    )}
                    {snapshot.model_group && (
                      <Typography variant="caption" color="text.secondary" display="block">
                        {`${t('snapshot.modelGroup')}：${snapshot.model_group}`}
                      </Typography>
                    )}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                    <Typography variant="body2">
                      {enumText(t, 'enums.source', snapshot.source)}
                    </Typography>
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                    <StatusChip
                      tone={resultTone(snapshot.result)}
                      label={enumText(t, 'enums.result', snapshot.result)}
                    />
                  </TableCell>
                  <TableCell
                    sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}
                    onClick={event => event.stopPropagation()}
                  >
                    {snapshot.has_data ? (
                      <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
                        <ActionButton
                          tone="ghost"
                          size="small"
                          disabled={viewLoadingId === snapshot.id}
                          onClick={() => void openSnapshot(snapshot)}
                        >
                          {viewLoadingId === snapshot.id ? (
                            <CircularProgress size={16} />
                          ) : (
                            t('snapshot.view')
                          )}
                        </ActionButton>
                        <ActionButton
                          tone="ghost"
                          size="small"
                          onClick={() => setRollbackTarget(snapshot)}
                        >
                          {t('snapshot.rollback')}
                        </ActionButton>
                      </Box>
                    ) : (
                      <Typography variant="caption" color="text.disabled">
                        —
                      </Typography>
                    )}
                  </TableCell>
                </TableRow>
                {expandedId === snapshot.id && (
                  <TableRow>
                    <TableCell colSpan={4} sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Collapse in timeout="auto">
                        <Box sx={{ display: 'flex', gap: 3, flexWrap: 'wrap', py: 0.5 }}>
                          {appliedLines(snapshot.applied ?? [])}
                          {rejectedLines(snapshot.rejected ?? [])}
                        </Box>
                      </Collapse>
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            ))}
          </TableBody>
        </Table>
      </Box>

      <NekroDialog
        open={viewing !== null}
        onClose={() => setViewing(null)}
        title={t('snapshot.viewTitle')}
        maxWidth="sm"
        actions={
          <ActionButton tone="ghost" onClick={() => setViewing(null)}>
            {tCommon('actions.close')}
          </ActionButton>
        }
      >
        <JsonEditorField
          value={viewing?.data ? JSON.stringify(viewing.data, null, 2) : '—'}
          readOnly
          minRows={8}
        />
      </NekroDialog>

      <NekroDialog
        open={rollbackTarget !== null}
        onClose={() => (rollingBack ? undefined : setRollbackTarget(null))}
        title={t('snapshot.rollbackConfirmTitle')}
        maxWidth="xs"
        actions={
          <>
            <ActionButton
              tone="ghost"
              onClick={() => setRollbackTarget(null)}
              disabled={rollingBack}
            >
              {tCommon('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={rollingBack}
              onClick={() => void confirmRollback()}
            >
              {t('snapshot.rollback')}
            </ActionButton>
          </>
        }
      >
        <Typography variant="body2" sx={{ pt: 1 }}>
          {t('snapshot.rollbackConfirmBody')}
        </Typography>
      </NekroDialog>
    </Box>
  )
}
