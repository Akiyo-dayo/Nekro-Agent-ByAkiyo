import { Fragment, useState, type ReactNode } from 'react'
import {
  Box,
  Checkbox,
  Collapse,
  IconButton,
  MenuItem,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
  type SxProps,
  type Theme,
} from '@mui/material'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import DeleteOutlineRoundedIcon from '@mui/icons-material/DeleteOutlineRounded'
import ExpandLessRoundedIcon from '@mui/icons-material/ExpandLessRounded'
import ExpandMoreRoundedIcon from '@mui/icons-material/ExpandMoreRounded'
import FavoriteRoundedIcon from '@mui/icons-material/FavoriteRounded'
import PersonOutlineRoundedIcon from '@mui/icons-material/PersonOutlineRounded'
import TitleRoundedIcon from '@mui/icons-material/TitleRounded'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import type { StateFieldDef, StateFieldType, StateFieldDefs } from '../../services/api/stateTable'
import { UNIFIED_TABLE_STYLES } from '../../theme/variants'

/**
 * 模板字段表格
 *
 * 一行一个字段。默认只放路径、类型、说明、标签位、优先级与动作；只读、私有、隐藏、数值约束
 * 收进每行的展开区，避免一张表横向滚出十几个列。表格只负责改自己能表达的那些键，其余键
 * （`pair`、`extensible`、`template`、`required` 等）随对象展开原样带回去。
 *
 * 路径在输入时按 `/` 切回数组，但**保留空段**：否则刚敲下的那个 `/` 会立刻被吃掉，
 * 嵌套路径根本打不出来。去掉空段是保存那一步的事（见 `TemplatesTab`）。
 */

const FIELD_TYPES: StateFieldType[] = ['string', 'number', 'boolean', 'object', 'array']

const AFFECTION_VALUES = ['', 'score', 'stage'] as const

export interface FieldDefsTableProps {
  fields: StateFieldDef[]
  onChange: (fields: StateFieldDef[]) => void
  disabled?: boolean
}

function numberText(value: number | null | undefined): string {
  return typeof value === 'number' ? String(value) : ''
}

function toNumber(value: string): number | undefined {
  const trimmed = value.trim()
  if (trimmed === '') return undefined
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : undefined
}

export default function FieldDefsTable({ fields, onChange, disabled = false }: FieldDefsTableProps) {
  const { t } = useTranslation('state')
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set())

  const patch = (index: number, changes: Partial<StateFieldDef>) => {
    onChange(fields.map((field, current) => (current === index ? { ...field, ...changes } : field)))
  }

  /** 类型换了之后，只属于旧类型的几条约束就没有意义了，留着只会被服务端拒 */
  const changeType = (index: number, type: StateFieldType) => {
    const next: StateFieldDef = { ...fields[index], type }
    if (type !== 'number') {
      delete next.minimum
      delete next.maximum
    }
    if (type !== 'string') delete next.max_length
    if (type !== 'array') delete next.max_items
    onChange(fields.map((field, current) => (current === index ? next : field)))
  }

  const remove = (index: number) => {
    onChange(fields.filter((_field, current) => current !== index))
    setExpanded(previous => {
      const next = new Set<number>()
      previous.forEach(item => {
        if (item !== index) next.add(item > index ? item - 1 : item)
      })
      return next
    })
  }

  const add = () => {
    onChange([...fields, { path: [], type: 'string' }])
  }

  const toggleExpanded = (index: number) => {
    setExpanded(previous => {
      const next = new Set(previous)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  const headerCell = (label: string, width: number) => (
    <TableCell key={label} sx={[UNIFIED_TABLE_STYLES.header, { width }] as SxProps<Theme>}>
      <Typography variant="caption" fontWeight={600}>
        {label}
      </Typography>
    </TableCell>
  )

  const flagIcon = (
    icon: ReactNode,
    label: string,
    color: 'primary' | 'secondary'
  ) => (
    <Tooltip title={label}>
      <Box component="span" sx={{ display: 'inline-flex', color: `${color}.main` }}>
        {icon}
      </Box>
    </Tooltip>
  )

  return (
    <Box>
      <Box sx={{ overflowX: 'auto' }}>
        <Table size="small" sx={{ minWidth: 820 }}>
          <TableHead>
            <TableRow>
              {headerCell(t('templates.table.path'), 220)}
              {headerCell(t('templates.table.type'), 110)}
              {headerCell(t('templates.table.description'), 200)}
              {headerCell(t('templates.table.flags'), 130)}
              {headerCell(t('templates.table.priority'), 90)}
              {headerCell(t('templates.table.actions'), 90)}
            </TableRow>
          </TableHead>
          <TableBody>
            {fields.map((field, index) => {
              const isNumber = field.type === 'number'
              const isString = field.type === 'string'
              const isArray = field.type === 'array'
              const open = expanded.has(index)
              return (
                <Fragment key={index}>
                  <TableRow>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <TextField
                        size="small"
                        fullWidth
                        disabled={disabled}
                        value={field.path.join('/')}
                        placeholder={t('templates.pathPlaceholder')}
                        onChange={event => patch(index, { path: event.target.value.split('/') })}
                        inputProps={{ 'aria-label': t('templates.table.path') }}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Select
                        size="small"
                        fullWidth
                        disabled={disabled}
                        value={field.type}
                        onChange={event => changeType(index, event.target.value as StateFieldType)}
                        inputProps={{ 'aria-label': t('templates.table.type') }}
                      >
                        {FIELD_TYPES.map(type => (
                          <MenuItem key={type} value={type}>
                            {t(`templates.fieldType.${type}`)}
                          </MenuItem>
                        ))}
                      </Select>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <TextField
                        size="small"
                        fullWidth
                        disabled={disabled}
                        value={field.description ?? ''}
                        onChange={event => patch(index, { description: event.target.value })}
                        inputProps={{ 'aria-label': t('templates.table.description') }}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <Stack direction="row" spacing={0.5} alignItems="center">
                        {field.person &&
                          flagIcon(
                            <PersonOutlineRoundedIcon fontSize="small" />,
                            t('templates.table.person'),
                            'primary'
                          )}
                        {field.headline &&
                          flagIcon(
                            <TitleRoundedIcon fontSize="small" />,
                            t('templates.table.headline'),
                            'primary'
                          )}
                        {field.affection &&
                          flagIcon(
                            <FavoriteRoundedIcon fontSize="small" />,
                            `${t('templates.table.affection')} · ${t(
                              `templates.affection.${field.affection}`
                            )}`,
                            'secondary'
                          )}
                        {!field.person && !field.headline && !field.affection && (
                          <Typography variant="caption" color="text.secondary">
                            —
                          </Typography>
                        )}
                      </Stack>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>}>
                      <TextField
                        size="small"
                        fullWidth
                        type="number"
                        disabled={disabled}
                        value={numberText(field.priority)}
                        onChange={event =>
                          patch(index, { priority: toNumber(event.target.value) })
                        }
                        inputProps={{
                          min: 0,
                          max: 9,
                          'aria-label': t('templates.table.priority'),
                        }}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell as SxProps<Theme>} align="center">
                      <Tooltip
                        title={
                          open ? t('templates.table.collapse') : t('templates.table.expand')
                        }
                      >
                        <span>
                          <IconButton
                            size="small"
                            disabled={disabled}
                            aria-label={
                              open ? t('templates.table.collapse') : t('templates.table.expand')
                            }
                            onClick={() => toggleExpanded(index)}
                          >
                            {open ? (
                              <ExpandLessRoundedIcon fontSize="small" />
                            ) : (
                              <ExpandMoreRoundedIcon fontSize="small" />
                            )}
                          </IconButton>
                        </span>
                      </Tooltip>
                      <Tooltip title={t('templates.removeField')}>
                        <span>
                          <IconButton
                            size="small"
                            disabled={disabled}
                            aria-label={t('templates.removeField')}
                            onClick={() => remove(index)}
                          >
                            <DeleteOutlineRoundedIcon fontSize="small" />
                          </IconButton>
                        </span>
                      </Tooltip>
                    </TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell colSpan={6} sx={{ p: 0, borderBottom: 'none' }}>
                      <Collapse in={open} timeout="auto" unmountOnExit>
                        <Box
                          sx={{
                            px: 2,
                            py: 1.5,
                            bgcolor: 'action.hover',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: 1.5,
                          }}
                        >
                          <Stack direction="row" spacing={1.5} sx={{ flexWrap: 'wrap' }}>
                            <Stack direction="row" spacing={0.5} alignItems="center">
                              <Checkbox
                                size="small"
                                disabled={disabled}
                                checked={field.readonly === true}
                                onChange={event =>
                                  patch(index, { readonly: event.target.checked })
                                }
                                inputProps={{ 'aria-label': t('templates.table.readonly') }}
                              />
                              <Typography variant="caption">
                                {t('templates.table.readonly')}
                              </Typography>
                            </Stack>
                            <Stack direction="row" spacing={0.5} alignItems="center">
                              <Checkbox
                                size="small"
                                disabled={disabled}
                                checked={field.private === true}
                                onChange={event =>
                                  patch(index, { private: event.target.checked })
                                }
                                inputProps={{ 'aria-label': t('templates.table.private') }}
                              />
                              <Typography variant="caption">
                                {t('templates.table.private')}
                              </Typography>
                            </Stack>
                            <Stack direction="row" spacing={0.5} alignItems="center">
                              <Checkbox
                                size="small"
                                disabled={disabled}
                                checked={field.hidden === true}
                                onChange={event =>
                                  patch(index, { hidden: event.target.checked })
                                }
                                inputProps={{ 'aria-label': t('templates.table.hidden') }}
                              />
                              <Typography variant="caption">
                                {t('templates.table.hidden')}
                              </Typography>
                            </Stack>
                            <Stack direction="row" spacing={0.5} alignItems="center">
                              <Checkbox
                                size="small"
                                disabled={disabled}
                                checked={field.person === true}
                                onChange={event =>
                                  patch(index, { person: event.target.checked })
                                }
                                inputProps={{ 'aria-label': t('templates.table.person') }}
                              />
                              <Typography variant="caption">
                                {t('templates.table.person')}
                              </Typography>
                            </Stack>
                            <Stack direction="row" spacing={0.5} alignItems="center">
                              <Checkbox
                                size="small"
                                disabled={disabled}
                                checked={field.headline === true}
                                onChange={event =>
                                  patch(index, { headline: event.target.checked })
                                }
                                inputProps={{ 'aria-label': t('templates.table.headline') }}
                              />
                              <Typography variant="caption">
                                {t('templates.table.headline')}
                              </Typography>
                            </Stack>
                          </Stack>
                          <Stack
                            direction="row"
                            spacing={1.5}
                            sx={{ flexWrap: 'wrap', alignItems: 'center' }}
                          >
                            <Box sx={{ width: 170 }}>
                              <Select
                                size="small"
                                fullWidth
                                disabled={disabled}
                                value={field.affection ?? ''}
                                onChange={event =>
                                  patch(index, {
                                    affection:
                                      event.target.value === ''
                                        ? null
                                        : (event.target.value as 'score' | 'stage'),
                                  })
                                }
                                inputProps={{ 'aria-label': t('templates.table.affection') }}
                              >
                                {AFFECTION_VALUES.map(value => (
                                  <MenuItem key={value || 'none'} value={value}>
                                    {t(`templates.affection.${value || 'none'}`)}
                                  </MenuItem>
                                ))}
                              </Select>
                            </Box>
                            <Box sx={{ width: 130 }}>
                              <TextField
                                size="small"
                                fullWidth
                                type="number"
                                label={t('templates.table.minimum')}
                                disabled={disabled || !isNumber}
                                value={numberText(field.minimum)}
                                onChange={event =>
                                  patch(index, { minimum: toNumber(event.target.value) })
                                }
                                inputProps={{ 'aria-label': t('templates.table.minimum') }}
                              />
                            </Box>
                            <Box sx={{ width: 130 }}>
                              <TextField
                                size="small"
                                fullWidth
                                type="number"
                                label={t('templates.table.maximum')}
                                disabled={disabled || !isNumber}
                                value={numberText(field.maximum)}
                                onChange={event =>
                                  patch(index, { maximum: toNumber(event.target.value) })
                                }
                                inputProps={{ 'aria-label': t('templates.table.maximum') }}
                              />
                            </Box>
                            <Box sx={{ width: 130 }}>
                              <TextField
                                size="small"
                                fullWidth
                                type="number"
                                label={t('templates.table.maxLength')}
                                disabled={disabled || !isString}
                                value={numberText(field.max_length)}
                                onChange={event =>
                                  patch(index, { max_length: toNumber(event.target.value) })
                                }
                                inputProps={{ 'aria-label': t('templates.table.maxLength') }}
                              />
                            </Box>
                            <Box sx={{ width: 130 }}>
                              <TextField
                                size="small"
                                fullWidth
                                type="number"
                                label={t('templates.table.maxItems')}
                                disabled={disabled || !isArray}
                                value={numberText(field.max_items)}
                                onChange={event =>
                                  patch(index, { max_items: toNumber(event.target.value) })
                                }
                                inputProps={{ 'aria-label': t('templates.table.maxItems') }}
                              />
                            </Box>
                          </Stack>
                        </Box>
                      </Collapse>
                    </TableCell>
                  </TableRow>
                </Fragment>
              )
            })}
          </TableBody>
        </Table>
      </Box>
      <Box sx={{ mt: 1 }}>
        <ActionButton
          tone="ghost"
          size="small"
          startIcon={<AddRoundedIcon />}
          disabled={disabled}
          onClick={add}
        >
          {t('templates.addField')}
        </ActionButton>
      </Box>
    </Box>
  )
}

/** 模板字段定义在表格外还带着几项（严格集、私密词、主题），保存时原样带回 */
export type FieldDefsDraft = Pick<StateFieldDefs, 'fields'>
