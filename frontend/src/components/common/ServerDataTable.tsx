import { Fragment, type ReactNode } from 'react'
import {
  Box,
  Checkbox,
  CircularProgress,
  SxProps,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Theme,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import { useTranslation } from 'react-i18next'
import PaginationStyled from './PaginationStyled'
import QueryErrorState from './QueryErrorState'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'

/**
 * 服务端分页表格
 *
 * 这一件刻意不碰任何业务：分页、错误态、空态、勾选与批量条都由调用方给数据与节点。
 * 记忆页与将来的状态表插件共用同一套表格骨架，避免再长出第二份「自己算页数」的实现。
 *
 * 几个容易做错的约定：
 * - **失败先于空态**：「零」是一个有效答案，把 403 / 500 渲染成空表等于告诉用户
 *   「你什么都没有」。有错误时只渲染错误分支。
 * - 表头的全选只作用于**当前页**可勾选的行。跨页全选属于业务语义（例如「当前筛选的
 *   全部」），由调用方另给按钮，不能藏在这个组件里。
 * - 勾选框的点击要阻止冒泡，否则点一下会同时触发行点击。
 */

export interface ServerDataTableColumn<Row> {
  key: string
  header: ReactNode
  render: (row: Row) => ReactNode
  align?: 'left' | 'right' | 'center'
  /**
   * 列宽
   *
   * 表格沿用 `UNIFIED_TABLE_STYLES.getTableBase` 的固定布局：其余列都给像素宽时，
   * 给百分比的那一列会在中等宽度下被挤到只剩一两个字。正文列也请给像素宽，
   * 放不下时由外层视口横向滚动。
   */
  width?: number | string
  /** 窄屏（theme.breakpoints.down('sm')）下不显示的列 */
  hideOnMobile?: boolean
}

/** 列宽样式：只写调用方给了的那一项 */
const columnSizeSx = <Row,>(column: ServerDataTableColumn<Row>) =>
  column.width === undefined ? {} : { width: column.width }

export interface ServerDataTableProps<Row> {
  columns: ServerDataTableColumn<Row>[]
  rows: Row[]
  getRowId: (row: Row) => string | number
  total: number
  /** 从 1 开始 */
  page: number
  pageSize: number
  onPageChange: (page: number) => void
  /** 首次加载（用 isPending） */
  loading?: boolean
  /** 翻页 / 刷新中 */
  fetching?: boolean
  /** 有值时渲染错误态而不是空态 */
  error?: unknown
  onRetry?: () => void
  /** 调用方可覆盖错误呈现（记忆页用它把 404 说成「未找到」） */
  renderError?: (error: unknown) => ReactNode
  emptyTitle: ReactNode
  emptyHint?: ReactNode
  onRowClick?: (row: Row) => void
  /** 传了就有勾选列；受控 */
  selection?: {
    selectedIds: ReadonlySet<string | number>
    onChange: (next: Set<string | number>) => void
    isRowSelectable?: (row: Row) => boolean
  }
  /** 勾选非空时，在表格上方出现的批量操作条（调用方给按钮） */
  renderBulkBar?: (selectedCount: number) => ReactNode
  /** 分页器旁的自定义控件（记忆页用它放每页条数选择） */
  paginationExtra?: ReactNode
  /**
   * 当前展开的行；与 `renderExpandedRow` 成对使用
   *
   * 展开状态由调用方持有：它决定展开哪一行，也决定展开内容要不要发请求。
   */
  expandedRowId?: string | number | null
  /** 在展开那一行下面插入一行占满整张表的单元格来渲染它 */
  renderExpandedRow?: (row: Row) => ReactNode
}

export default function ServerDataTable<Row>({
  columns,
  rows,
  getRowId,
  total,
  page,
  pageSize,
  onPageChange,
  loading = false,
  fetching = false,
  error,
  onRetry,
  renderError,
  emptyTitle,
  emptyHint,
  onRowClick,
  selection,
  renderBulkBar,
  paginationExtra,
  expandedRowId,
  renderExpandedRow,
}: ServerDataTableProps<Row>) {
  const theme = useTheme()
  const { t } = useTranslation('common')
  const isMobile = useMediaQuery(theme.breakpoints.down('sm'))

  const visibleColumns = columns.filter(column => !(isMobile && column.hideOnMobile))
  const totalPages = Math.max(1, Math.ceil(total / Math.max(pageSize, 1)))

  const selectableRows = selection
    ? rows.filter(row => selection.isRowSelectable?.(row) ?? true)
    : []
  const selectedOnPage = selection
    ? selectableRows.filter(row => selection.selectedIds.has(getRowId(row))).length
    : 0
  const allSelected = selectableRows.length > 0 && selectedOnPage === selectableRows.length
  const someSelected = selectedOnPage > 0 && !allSelected

  const toggleCurrentPage = () => {
    if (!selection) return
    const next = new Set(selection.selectedIds)
    if (allSelected) {
      selectableRows.forEach(row => next.delete(getRowId(row)))
    } else {
      selectableRows.forEach(row => next.add(getRowId(row)))
    }
    selection.onChange(next)
  }

  const toggleRow = (row: Row) => {
    if (!selection) return
    const next = new Set(selection.selectedIds)
    const rowId = getRowId(row)
    if (next.has(rowId)) next.delete(rowId)
    else next.add(rowId)
    selection.onChange(next)
  }

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        flex: 1,
        minHeight: 0,
        gap: 1,
        width: '100%',
      }}
    >
      {selection && selection.selectedIds.size > 0 && renderBulkBar && (
        <Box
          sx={{
            display: 'flex',
            flexWrap: 'wrap',
            alignItems: 'center',
            gap: 1,
            px: 1.5,
            py: 1,
            border: '1px solid',
            borderColor: 'divider',
            borderRadius: 1.5,
          }}
        >
          {renderBulkBar(selection.selectedIds.size)}
        </Box>
      )}

      <Box sx={UNIFIED_TABLE_STYLES.tableContentContainer as SxProps<Theme>}>
        <Box sx={UNIFIED_TABLE_STYLES.tableViewport as SxProps<Theme>}>
          {error ? (
            <Box sx={{ minHeight: 240, display: 'flex', alignItems: 'center' }}>
              <Box sx={{ width: '100%' }}>
                {renderError ? (
                  renderError(error)
                ) : (
                  <QueryErrorState error={error} onRetry={onRetry} compact />
                )}
              </Box>
            </Box>
          ) : loading ? (
            <Box
              sx={{
                minHeight: 240,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <CircularProgress />
            </Box>
          ) : rows.length === 0 ? (
            <Box
              sx={{
                minHeight: 240,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                textAlign: 'center',
                gap: 1,
                p: 2,
              }}
            >
              <Typography variant="subtitle1">{emptyTitle}</Typography>
              {emptyHint && (
                <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 460 }}>
                  {emptyHint}
                </Typography>
              )}
            </Box>
          ) : (
            <Table stickyHeader sx={UNIFIED_TABLE_STYLES.getTableBase(isMobile, false)}>
              <TableHead>
                <TableRow>
                  {selection && (
                    <TableCell
                      padding="checkbox"
                      sx={[UNIFIED_TABLE_STYLES.header, { width: 48 }] as SxProps<Theme>}
                    >
                      <Checkbox
                        size="small"
                        checked={allSelected}
                        indeterminate={someSelected}
                        disabled={selectableRows.length === 0}
                        onChange={toggleCurrentPage}
                        inputProps={{ 'aria-label': t('actions.selectAll') }}
                      />
                    </TableCell>
                  )}
                  {visibleColumns.map(column => (
                    <TableCell
                      key={column.key}
                      align={column.align}
                      sx={
                        [UNIFIED_TABLE_STYLES.header, columnSizeSx(column)] as SxProps<Theme>
                      }
                    >
                      {column.header}
                    </TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map(row => {
                  const rowId = getRowId(row)
                  const selectable = selection?.isRowSelectable?.(row) ?? true
                  const expanded = renderExpandedRow !== undefined && expandedRowId === rowId
                  return (
                    <Fragment key={rowId}>
                      <TableRow
                        hover
                        onClick={onRowClick ? () => onRowClick(row) : undefined}
                        sx={
                          [
                            UNIFIED_TABLE_STYLES.row,
                            onRowClick ? { cursor: 'pointer' } : {},
                          ] as SxProps<Theme>
                        }
                      >
                        {selection && (
                          <TableCell
                            padding="checkbox"
                            sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}
                            onClick={event => event.stopPropagation()}
                          >
                            <Checkbox
                              size="small"
                              checked={selection.selectedIds.has(rowId)}
                              disabled={!selectable}
                              onChange={() => toggleRow(row)}
                              inputProps={{ 'aria-label': t('actions.select') }}
                            />
                          </TableCell>
                        )}
                        {visibleColumns.map(column => (
                          <TableCell
                            key={column.key}
                            align={column.align}
                            sx={
                              [UNIFIED_TABLE_STYLES.cell, columnSizeSx(column)] as SxProps<Theme>
                            }
                          >
                            {column.render(row)}
                          </TableCell>
                        ))}
                      </TableRow>
                      {expanded && (
                        <TableRow>
                          <TableCell
                            colSpan={visibleColumns.length + (selection ? 1 : 0)}
                            sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}
                          >
                            {renderExpandedRow(row)}
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </Box>
      </Box>

      {(totalPages > 1 || paginationExtra) && (
        <Box
          sx={{
            display: 'flex',
            alignItems: 'flex-end',
            justifyContent: 'center',
            gap: 2,
            flexWrap: 'wrap',
          }}
        >
          {paginationExtra}
          <PaginationStyled
            totalPages={totalPages}
            currentPage={page}
            onPageChange={(_event, value) => onPageChange(value)}
            loading={fetching}
            alwaysShow={false}
          />
        </Box>
      )}
    </Box>
  )
}
