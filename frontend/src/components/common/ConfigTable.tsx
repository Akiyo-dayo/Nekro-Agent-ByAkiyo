import { useState, useEffect, useCallback, useMemo, ReactNode } from 'react'
import {
  Box,
  Paper,
  TextField,
  Switch,
  FormControlLabel,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  MenuItem,
  InputAdornment,
  Typography,
  Chip,
  Avatar,
  List,
  ListItem,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  CircularProgress,
  useTheme,
  useMediaQuery,
  Stack,
  Tooltip,
  Alert,
  SxProps,
  Theme,
} from '@mui/material'
import {
  Save as SaveIcon,
  Refresh as RefreshIcon,
  Visibility as VisibilityIcon,
  VisibilityOff as VisibilityOffIcon,
  Search as SearchIcon,
  Launch as LaunchIcon,
  ExpandMore as ExpandMoreIcon,
  ExpandLess as ExpandLessIcon,
  Add as AddIcon,
  Delete as DeleteIcon,
  HelpOutline as HelpOutlineIcon,
  RestartAlt as RestartIcon,
} from '@mui/icons-material'
import { unstable_usePrompt as usePrompt, useBeforeUnload, useNavigate } from 'react-router-dom'
import type { BlockerFunction } from 'react-router-dom'
import { UNIFIED_TABLE_STYLES, CHIP_VARIANTS } from '../../theme/variants'
import ActionButton from './ActionButton'
import IconActionButton from './IconActionButton'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import { useNotification } from '../../hooks/useNotification'
import { restartApi } from '../../services/api/restart'
import { useRestartCapability } from '../../hooks/useRestartCapability'
import { unifiedConfigApi } from '../../services/api/unified-config'
import { ThemedTooltip } from './ThemedTooltip'
import { presetsApi, Preset } from '../../services/api/presets'
import { TENANT_QUERY_KEYS, tenantApi } from '../../services/api/tenant'
import { useIdentity } from '../../hooks/useIdentity'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import { I18nDict, getLocalizedText } from '../../services/api/types'

const Button = ActionButton
const IconButton = IconActionButton

const HtmlTooltip = ThemedTooltip

export interface ConfigItem {
  key: string
  value:
    | string
    | number
    | boolean
    | Array<string | number | boolean | Record<string, unknown>>
    | Record<string, unknown>
  title: string
  description?: string
  placeholder?: string
  type: string
  is_complex?: boolean
  element_type?: string
  key_type?: string
  value_type?: string
  field_schema?: Record<
    string,
    {
      type: string
      title: string
      description: string
      i18n_title?: I18nDict
      i18n_description?: I18nDict
      default?: unknown
      required: boolean
      is_secret?: boolean
      is_textarea?: boolean
      placeholder?: string
      is_complex?: boolean
      element_type?: string
      key_type?: string
      value_type?: string
      is_need_restart?: boolean
    }
  >
  enum?: string[]
  is_secret?: boolean
  is_textarea?: boolean
  ref_model_groups?: boolean
  ref_email_accounts_send_enabled?: boolean
  ref_presets?: boolean
  ref_presets_no_default?: boolean
  ref_presets_multiple?: boolean
  is_hidden?: boolean
  required?: boolean
  model_type?: string
  sub_item_name?: string
  enable_toggle?: string
  overridable?: boolean
  /** 平台方专属：租户能看、不能改（服务端在租户写实例覆盖时同样拒绝） */
  platform_only?: boolean
  /** 租户只能把这一项往低调（服务端判，页面只做提示） */
  tenant_may_only_lower?: boolean
  is_need_restart?: boolean
  category?: string
  /**
   * 布尔项从关切到开时的确认文案
   *
   * 后端 `ExtraField.enable_confirm` 原样透传到这一项；键是 `zh-CN` / `en-US`。
   * 列表 / 字典子项的布尔不走顶层开关，不会读它。
   */
  enable_confirm?: Record<string, string> | null

  // i18n 扩展字段（可选，与后端 ExtraField 对应）
  i18n_title?: I18nDict
  i18n_description?: I18nDict
  i18n_category?: I18nDict
}

export interface ModelGroupConfig {
  CHAT_MODEL: string
  CHAT_PROXY: string
  BASE_URL: string
  API_KEY: string
  MODEL_TYPE?: string
  TEMPERATURE?: number | null
  TOP_P?: number | null
  TOP_K?: number | null
  PRESENCE_PENALTY?: number | null
  FREQUENCY_PENALTY?: number | null
  EXTRA_BODY?: string | null
  ENABLE_VISION?: boolean
  ENABLE_COT?: boolean
  /** 预填方式；后端默认 off，只有声明支持的服务才该改 */
  PREFILL_MODE?: 'off' | 'assistant' | 'deepseek'
}

export interface ModelTypeOption {
  value: string
  label: string
  description?: string
  color?: string
  icon?: string
}

export interface ConfigService {
  getConfigList: (configKey: string) => Promise<ConfigItem[]>
  getModelGroups?: () => Promise<Record<string, ModelGroupConfig>>
  getModelTypes?: () => Promise<ModelTypeOption[]>
  batchUpdateConfig: (configKey: string, configs: Record<string, string>) => Promise<void>
  saveConfig: (configKey: string) => Promise<void>
  reloadConfig: (configKey: string) => Promise<void>
}

interface ExpandedRowsState {
  [configKey: string]: boolean
}

interface EmailSendAccountOption {
  username: string
  label: string
}

const getTypeColor = (type: string, isComplex: boolean = false) => {
  if (isComplex) {
    switch (type) {
      case 'list':
        return 'secondary'
      case 'dict':
        return 'error'
      case 'object':
        return 'warning'
      default:
        return 'default'
    }
  }

  switch (type) {
    case 'str':
      return 'warning'
    case 'int':
      return 'info'
    case 'float':
      return 'info'
    case 'bool':
      return 'success'
    case 'list':
      return 'primary'
    case 'dict':
      return 'primary'
    case 'preset':
      return 'secondary'
    case 'presets':
      return 'secondary'
    default:
      return 'default'
  }
}

function renderSimpleListInput(
  value: unknown,
  elementType: string,
  onChange: (value: unknown) => void,
  t: TFunction
): React.ReactNode {
  switch (elementType) {
    case 'bool':
      return (
        <FormControlLabel
          control={
            <Switch
              checked={Boolean(value)}
              onChange={e => onChange(e.target.checked)}
              size="small"
              color="primary"
            />
          }
          label={value ? t('common.yes') : t('common.no')}
        />
      )
    case 'int':
    case 'float':
      return (
        <TextField
          type="number"
          value={value || ''}
          onChange={e =>
            onChange(
              elementType === 'int'
                ? parseInt(e.target.value, 10) || 0
                : parseFloat(e.target.value) || 0
            )
          }
          size="small"
          fullWidth
          variant="outlined"
        />
      )
    default:
      return (
        <TextField
          value={String(value || '')}
          onChange={e => onChange(e.target.value)}
          size="small"
          fullWidth
          variant="outlined"
        />
      )
  }
}

function renderFieldInput(
  value: unknown,
  fieldSchema: {
    type: string
    is_secret?: boolean
    is_textarea?: boolean
    placeholder?: string
    is_complex?: boolean
    element_type?: string
  },
  onChange: (value: unknown) => void,
  t: TFunction,
  fieldKey?: string,
  expandedRows?: ExpandedRowsState,
  setExpandedRows?: React.Dispatch<React.SetStateAction<ExpandedRowsState>>
): React.ReactNode {
  switch (fieldSchema.type) {
    case 'bool':
    case 'boolean':
      return (
        <FormControlLabel
          control={
            <Switch
              checked={Boolean(value)}
              onChange={e => onChange(e.target.checked)}
              size="small"
              color="primary"
            />
          }
          label={value ? t('common.yes') : t('common.no')}
        />
      )
    case 'int':
    case 'float':
    case 'number':
      return (
        <TextField
          type="number"
          value={value || ''}
          onChange={e =>
            onChange(
              fieldSchema.type === 'int'
                ? parseInt(e.target.value, 10) || 0
                : parseFloat(e.target.value) || 0
            )
          }
          size="small"
          fullWidth
          placeholder={fieldSchema.placeholder}
          variant="outlined"
        />
      )
    case 'list': {
      if (fieldKey && expandedRows && setExpandedRows) {
        const listValue = Array.isArray(value) ? value : []
        const isExpanded = expandedRows[fieldKey] || false

        return (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, width: '100%' }}>
            <TextField
              value={t('configTable.listCount', { count: listValue.length })}
              size="small"
              fullWidth
              InputProps={{
                readOnly: true,
                sx: {
                  cursor: 'pointer',
                  bgcolor: 'transparent',
                  '&:hover': {
                    bgcolor: 'action.hover',
                  },
                },
              }}
              onClick={() => setExpandedRows(prev => ({ ...prev, [fieldKey]: !prev[fieldKey] }))}
              variant="outlined"
              sx={{
                '& .MuiOutlinedInput-root': {
                  '&:hover .MuiOutlinedInput-notchedOutline': {
                    borderColor: 'primary.main',
                  },
                },
              }}
            />
            <IconButton
              size="small"
              onClick={() => setExpandedRows(prev => ({ ...prev, [fieldKey]: !prev[fieldKey] }))}
              sx={{
                flexShrink: 0,
                color: 'text.secondary',
                '&:hover': {
                  bgcolor: 'action.hover',
                  color: 'primary.main',
                },
              }}
            >
              {isExpanded ? <ExpandLessIcon /> : <ExpandMoreIcon />}
            </IconButton>
          </Box>
        )
      }

      const listValue = Array.isArray(value) ? value : []
      return (
        <TextField
          value={t('configTable.listCount', { count: listValue.length })}
          size="small"
          fullWidth
          InputProps={{ readOnly: true }}
          variant="outlined"
        />
      )
    }
    default:
      return (
        <TextField
          type="text"
          value={String(value || '')}
          onChange={e => onChange(e.target.value)}
          size="small"
          fullWidth
          placeholder={fieldSchema.placeholder}
          variant="outlined"
          multiline={fieldSchema.is_textarea}
          minRows={fieldSchema.is_textarea ? 2 : 1}
          maxRows={fieldSchema.is_textarea ? 4 : 1}
          InputProps={{
            style: fieldSchema.is_secret
              ? ({
                  WebkitTextSecurity: 'disc',
                } as React.CSSProperties)
              : undefined,
          }}
        />
      )
  }
}

function getDefaultValueForType(type: string): unknown {
  switch (type) {
    case 'bool':
    case 'boolean':
      return false
    case 'int':
    case 'float':
    case 'number':
      return 0
    case 'list':
      return []
    case 'dict':
      return {}
    default:
      return ''
  }
}

function renderNestedConfigRows(
  config: ConfigItem,
  editingValues: Record<string, string>,
  handleConfigChange: (key: string, value: string) => void,
  isOverridePage: boolean,
  isSmall: boolean,
  expandedRows: ExpandedRowsState,
  setExpandedRows: React.Dispatch<React.SetStateAction<ExpandedRowsState>>,
  t: TFunction,
  language: string,
  level: number = 0,
  parentKey: string = ''
): React.ReactNode[] {
  const rows: React.ReactNode[] = []
  let currentValue
  try {
    currentValue =
      editingValues[config.key] !== undefined ? JSON.parse(editingValues[config.key]) : config.value
  } catch {
    currentValue = config.value
  }

  const tableCellStyle: SxProps<Theme> = {
    py: isSmall ? 0.75 : 1.5,
    pl: 2 + level * 2,
    borderLeft: level > 0 ? `2px solid` : 'none',
    borderLeftColor: level > 0 ? 'divider' : 'transparent',
  }

  if (config.type === 'list' && !config.is_complex) {
    const listValue = Array.isArray(currentValue) ? currentValue : []
    const elementType = config.element_type || 'str'

    listValue.forEach((item: unknown, index: number) => {
      const subKey = `${parentKey}${config.key}[${index}]`
      rows.push(
        <TableRow key={subKey} sx={{ ...UNIFIED_TABLE_STYLES.nestedRow }}>
          {isOverridePage && <TableCell sx={tableCellStyle} />}
          <TableCell sx={tableCellStyle}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              <Typography variant="body2" sx={{ fontSize: isSmall ? '0.75rem' : 'inherit' }}>
                {config.sub_item_name ? `${config.sub_item_name}[${index}]` : `[${index}]`}
              </Typography>
              <IconButton
                size="small"
                onClick={() => {
                  const newList = listValue.filter((_, i) => i !== index)
                  handleConfigChange(config.key, JSON.stringify(newList))
                }}
                sx={{ color: 'text.secondary', '&:hover': { color: 'error.main' } }}
              >
                <DeleteIcon fontSize="small" />
              </IconButton>
            </Box>
          </TableCell>
          <TableCell />
          <TableCell>
            <Chip
              label={elementType}
              size="small"
              color={getTypeColor(elementType)}
              variant="outlined"
              sx={CHIP_VARIANTS.base(isSmall)}
            />
          </TableCell>
          <TableCell>
            {renderSimpleListInput(
              item,
              elementType,
              newValue => {
                const newList = [...listValue]
                newList[index] = newValue
                handleConfigChange(config.key, JSON.stringify(newList))
              },
              t
            )}
          </TableCell>
        </TableRow>
      )
    })
    rows.push(
      <TableRow key={`${config.key}-add`} sx={{ ...UNIFIED_TABLE_STYLES.nestedRow }}>
        <TableCell sx={tableCellStyle} colSpan={isOverridePage ? 5 : 4}>
          <Button
            variant="text"
            size="small"
            startIcon={<AddIcon />}
            onClick={() => {
              const defaultValue = getDefaultValueForType(elementType)
              const newList = [...listValue, defaultValue]
              handleConfigChange(config.key, JSON.stringify(newList))
            }}
            sx={{ color: 'primary.main' }}
          >
            {t('configTable.addItem', {
              name: config.sub_item_name || t('common.item', { defaultValue: '项目' }),
            })}
          </Button>
        </TableCell>
      </TableRow>
    )
  }

  if (config.is_complex) {
    if (config.type === 'list') {
      const listValue = Array.isArray(currentValue) ? currentValue : []
      listValue.forEach((item: unknown, index: number) => {
        rows.push(
          <TableRow
            key={`${config.key}[${index}]-header`}
            sx={{ ...UNIFIED_TABLE_STYLES.nestedRow }}
          >
            <TableCell sx={tableCellStyle} colSpan={isOverridePage ? 5 : 4}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                <Typography
                  variant="body2"
                  sx={{
                    fontWeight: 'bold',
                    fontSize: isSmall ? '0.75rem' : 'inherit',
                  }}
                >
                  {config.sub_item_name || t('common.item')} [{index}]
                </Typography>
                <IconButton
                  size="small"
                  onClick={() => {
                    const newList = listValue.filter((_, i) => i !== index)
                    handleConfigChange(config.key, JSON.stringify(newList))
                  }}
                  sx={{ color: 'text.secondary', '&:hover': { color: 'error.main' } }}
                >
                  <DeleteIcon fontSize="small" />
                </IconButton>
              </Box>
            </TableCell>
          </TableRow>
        )
        if (item && typeof item === 'object' && config.field_schema) {
          Object.entries(config.field_schema).forEach(([fieldName, fieldSchema]) => {
            const fieldValue = (item as Record<string, unknown>)[fieldName]
            const subKey = `${parentKey}${config.key}[${index}].${fieldName}`
            const fieldTitle = getLocalizedText(
              fieldSchema.i18n_title,
              fieldSchema.title || fieldName,
              language
            )
            const fieldDescription = fieldSchema.description
              ? getLocalizedText(
                  fieldSchema.i18n_description,
                  fieldSchema.description,
                  language
                )
              : undefined
            rows.push(
              <TableRow key={subKey} sx={{ ...UNIFIED_TABLE_STYLES.nestedRow }}>
                {isOverridePage && <TableCell sx={{ ...tableCellStyle, pl: 4 + level * 2 }} />}
                <TableCell sx={{ ...tableCellStyle, pl: 4 + level * 2 }}>
                  <Box>
                    <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                      <Typography
                        variant="body2"
                        sx={{
                          fontWeight: 'normal',
                          fontSize: isSmall ? '0.75rem' : 'inherit',
                          mr: 0.5,
                        }}
                      >
                        {fieldTitle}
                      </Typography>
                      {fieldDescription && (
                        <HtmlTooltip
                          title={
                            <div dangerouslySetInnerHTML={{ __html: fieldDescription }} />
                          }
                          placement="right"
                        >
                          <IconButton
                            size="small"
                            sx={{
                              ml: 0.5,
                              p: isSmall ? 0.2 : 0.3,
                              color: 'primary.main',
                              '&:hover': {
                                color: 'primary.dark',
                                bgcolor: 'action.hover',
                              },
                            }}
                          >
                            <HelpOutlineIcon fontSize="inherit" />
                          </IconButton>
                        </HtmlTooltip>
                      )}
                    </Box>
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      sx={{
                        fontSize: isSmall ? '0.65rem' : '0.75rem',
                      }}
                    >
                      {fieldName}
                    </Typography>
                  </Box>
                </TableCell>
                <TableCell />
                <TableCell>
                  <Chip
                    label={fieldSchema.type}
                    size="small"
                    color={getTypeColor(fieldSchema.type)}
                    variant="outlined"
                    sx={CHIP_VARIANTS.base(isSmall)}
                  />
                </TableCell>
                <TableCell>
                  {renderFieldInput(
                    fieldValue,
                    fieldSchema,
                    newValue => {
                      const newList = [...listValue]
                      const newItem = { ...(newList[index] as Record<string, unknown>) }
                      newItem[fieldName] = newValue
                      newList[index] = newItem
                      handleConfigChange(config.key, JSON.stringify(newList))
                    },
                    t,
                    subKey,
                    expandedRows,
                    setExpandedRows
                  )}
                </TableCell>
              </TableRow>
            )
            if (fieldSchema.type === 'list' && expandedRows?.[subKey]) {
              const fieldListValue = Array.isArray(fieldValue) ? fieldValue : []
              const fieldElementType = fieldSchema.element_type || 'str'
              fieldListValue.forEach((listItem: unknown, listIndex: number) => {
                const listItemKey = `${subKey}[${listIndex}]`
                rows.push(
                  <TableRow key={listItemKey} sx={{ ...UNIFIED_TABLE_STYLES.nestedRow }}>
                    {isOverridePage && <TableCell sx={{ ...tableCellStyle, pl: 6 + level * 2 }} />}
                    <TableCell sx={{ ...tableCellStyle, pl: 6 + level * 2 }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <Typography
                          variant="body2"
                          sx={{ fontSize: isSmall ? '0.75rem' : 'inherit' }}
                        >
                          {fieldTitle}[{listIndex}]
                        </Typography>
                        <IconButton
                          size="small"
                          onClick={() => {
                            const newFieldList = fieldListValue.filter((_, i) => i !== listIndex)
                            const newList = [...listValue]
                            const newItem = { ...(newList[index] as Record<string, unknown>) }
                            newItem[fieldName] = newFieldList
                            newList[index] = newItem
                            handleConfigChange(config.key, JSON.stringify(newList))
                          }}
                          sx={{ color: 'text.secondary', '&:hover': { color: 'error.main' } }}
                        >
                          <DeleteIcon fontSize="small" />
                        </IconButton>
                      </Box>
                    </TableCell>
                    <TableCell />
                    <TableCell>
                      <Chip
                        label={fieldElementType}
                        size="small"
                        color={getTypeColor(fieldElementType)}
                        variant="outlined"
                        sx={CHIP_VARIANTS.base(isSmall)}
                      />
                    </TableCell>
                    <TableCell>
                      {renderSimpleListInput(
                        listItem,
                        fieldElementType,
                        newValue => {
                          const newFieldList = [...fieldListValue]
                          newFieldList[listIndex] = newValue
                          const newList = [...listValue]
                          const newItem = { ...(newList[index] as Record<string, unknown>) }
                          newItem[fieldName] = newFieldList
                          newList[index] = newItem
                          handleConfigChange(config.key, JSON.stringify(newList))
                        },
                        t
                      )}
                    </TableCell>
                  </TableRow>
                )
              })
              rows.push(
                <TableRow key={`${subKey}-add`} sx={{ ...UNIFIED_TABLE_STYLES.nestedRow }}>
                  <TableCell
                    sx={{ ...tableCellStyle, pl: 6 + level * 2 }}
                    colSpan={isOverridePage ? 5 : 4}
                  >
                    <Button
                      variant="text"
                      size="small"
                      startIcon={<AddIcon />}
                      onClick={() => {
                        const defaultValue = getDefaultValueForType(fieldElementType)
                        const newFieldList = [...fieldListValue, defaultValue]
                        const newList = [...listValue]
                        const newItem = { ...(newList[index] as Record<string, unknown>) }
                        newItem[fieldName] = newFieldList
                        newList[index] = newItem
                        handleConfigChange(config.key, JSON.stringify(newList))
                      }}
                      sx={{ color: 'primary.main' }}
                    >
                      {t('configTable.addItem', { name: fieldTitle })}
                    </Button>
                  </TableCell>
                </TableRow>
              )
            }
          })
        }
      })
      rows.push(
        <TableRow key={`${config.key}-add-complex`} sx={{ ...UNIFIED_TABLE_STYLES.nestedRow }}>
          <TableCell sx={tableCellStyle} colSpan={isOverridePage ? 5 : 4}>
            <Button
              variant="text"
              size="small"
              startIcon={<AddIcon />}
              onClick={() => {
                const newItem: Record<string, unknown> = {}
                if (config.field_schema) {
                  Object.entries(config.field_schema).forEach(([fieldName, fieldSchema]) => {
                    newItem[fieldName] =
                      fieldSchema.default ?? getDefaultValueForType(fieldSchema.type)
                  })
                }
                const newList = [...listValue, newItem]
                handleConfigChange(config.key, JSON.stringify(newList))
              }}
              sx={{ color: 'primary.main' }}
            >
              {t('configTable.addNewItem', {
                name: config.sub_item_name || t('common.item', { defaultValue: '项目' }),
              })}
            </Button>
          </TableCell>
        </TableRow>
      )
    }
  }

  if (config.type === 'dict' && !config.is_complex) {
    const dictValue =
      currentValue && typeof currentValue === 'object'
        ? (currentValue as Record<string, unknown>)
        : {}
    const valueType = config.value_type || 'str'
    Object.entries(dictValue).forEach(([key, value]) => {
      rows.push(
        <TableRow key={`${config.key}.${key}`} sx={{ ...UNIFIED_TABLE_STYLES.nestedRow }}>
          {isOverridePage && <TableCell sx={tableCellStyle} />}
          <TableCell sx={tableCellStyle}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              <Typography variant="body2" sx={{ fontSize: isSmall ? '0.75rem' : 'inherit' }}>
                {key}
              </Typography>
              <IconButton
                size="small"
                onClick={() => {
                  const newDict = { ...dictValue }
                  delete newDict[key]
                  handleConfigChange(config.key, JSON.stringify(newDict))
                }}
                sx={{ color: 'text.secondary', '&:hover': { color: 'error.main' } }}
              >
                <DeleteIcon fontSize="small" />
              </IconButton>
            </Box>
          </TableCell>
          <TableCell />
          <TableCell>
            <Chip
              label={valueType}
              size="small"
              color={getTypeColor(valueType)}
              variant="outlined"
              sx={CHIP_VARIANTS.base(isSmall)}
            />
          </TableCell>
          <TableCell>
            {renderSimpleListInput(
              value,
              valueType,
              newValue => {
                const newDict = { ...dictValue, [key]: newValue }
                handleConfigChange(config.key, JSON.stringify(newDict))
              },
              t
            )}
          </TableCell>
        </TableRow>
      )
    })
    rows.push(
      <TableRow key={`${config.key}-add-dict`} sx={{ ...UNIFIED_TABLE_STYLES.nestedRow }}>
        <TableCell sx={tableCellStyle} colSpan={isOverridePage ? 5 : 4}>
          <TextField
            size="small"
            placeholder={t('configTable.newItemPlaceholder')}
            variant="outlined"
            onKeyPress={e => {
              if (e.key === 'Enter') {
                const target = e.target as HTMLInputElement
                const newKey = target.value.trim()
                if (newKey && !dictValue[newKey]) {
                  const defaultValue = getDefaultValueForType(valueType)
                  const newDict = { ...dictValue, [newKey]: defaultValue }
                  handleConfigChange(config.key, JSON.stringify(newDict))
                  target.value = ''
                }
              }
            }}
            sx={{ width: '200px' }}
          />
        </TableCell>
      </TableRow>
    )
  }
  return rows
}

export interface ConfigTableProps {
  configKey: string
  configService: ConfigService
  configs: ConfigItem[]
  loading?: boolean
  searchText?: string
  onSearchChange?: (text: string) => void
  onRefresh?: () => void
  showSearchBar?: boolean
  showToolbar?: boolean
  title?: string
  emptyMessage?: string
  infoBox?: ReactNode
  isOverridePage?: boolean
  showCategoryColumn?: boolean
  toolbarActions?: ReactNode
}

interface ConfirmDialogProps {
  open: boolean
  onClose: () => void
  onConfirm: () => void
  title: string
  content: ReactNode
  /** 确认按钮的文案；不传沿用通用的「确认」 */
  confirmText?: string
}

const ConfirmDialog = ({
  open,
  onClose,
  onConfirm,
  title,
  content,
  confirmText,
}: ConfirmDialogProps) => {
  const { t } = useTranslation('common')
  return (
    <Dialog open={open} onClose={onClose}>
      <DialogTitle>{title}</DialogTitle>
      <DialogContent>{content}</DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t('actions.cancel')}</Button>
        <Button onClick={onConfirm} autoFocus>
          {confirmText ?? t('actions.confirm')}
        </Button>
      </DialogActions>
    </Dialog>
  )
}

export default function ConfigTable({
  configKey,
  configService,
  configs,
  loading = false,
  searchText = '',
  onSearchChange,
  onRefresh,
  showSearchBar = true,
  showToolbar = true,
  title,
  emptyMessage, // Will handle default in component body
  infoBox,
  isOverridePage = false,
  showCategoryColumn = false,
  toolbarActions,
}: ConfigTableProps) {
  const navigate = useNavigate()
  const notification = useNotification()
  const theme = useTheme()
  const { t, i18n } = useTranslation('common')
  const defaultEmptyMessage = t('messages.noData')
  const actualEmptyMessage = emptyMessage || defaultEmptyMessage
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  // `ref_presets` 字段要拉一份人设清单；人设接口对租户身份强制要求实例作用域，
  // 不带（或抢在作用域确定之前发）就是一个必然的 400，且会以红条弹到用户脸上
  const { currentInstanceKey, isScopeReady } = useInstanceScope()
  const [internalSearchText, setInternalSearchText] = useState(searchText)
  const [isSearchComposing, setIsSearchComposing] = useState(false)
  const [searchCompositionText, setSearchCompositionText] = useState(searchText)
  const effectiveSearchText = isSearchComposing ? searchCompositionText : onSearchChange ? searchText : internalSearchText

  const updateSearchText = useCallback(
    (nextValue: string) => {
      if (onSearchChange) {
        onSearchChange(nextValue)
      } else {
        setInternalSearchText(nextValue)
      }
    },
    [onSearchChange]
  )

  // i18n 辅助函数：获取本地化的配置项标题和描述
  const getConfigTitle = useCallback(
    (config: ConfigItem) => {
      return getLocalizedText(config.i18n_title, config.title, i18n.language)
    },
    [i18n.language]
  )

  const getConfigDescription = useCallback(
    (config: ConfigItem) => {
      return config.description
        ? getLocalizedText(config.i18n_description, config.description, i18n.language)
        : undefined
    },
    [i18n.language]
  )

  const getConfigCategory = useCallback(
    (config: ConfigItem) => {
      // 优先使用后端返回的 i18n_category（多语言），fallback 到 category（单语言）
      if (config.i18n_category && typeof config.i18n_category === 'object') {
        return getLocalizedText(config.i18n_category as I18nDict, config.category || '', i18n.language)
      }
      // fallback：如果 i18n_category 是字符串（兼容旧格式），直接使用
      if (typeof config.i18n_category === 'string') {
        return config.i18n_category
      }
      return config.category || ''
    },
    [i18n.language]
  )

  /**
   * 「打开前的确认」要显示的文字
   *
   * 与标题、描述同一套取语言的办法：直接用界面语言取，取不到回落到中文。后端
   * `ExtraField.enable_confirm` 只给 `zh-CN` / `en-US` 两种键，回落到中文比回落成
   * 空串更安全——空串会让确认框变成一个只有标题的空盒子。
   */
  const getEnableConfirmText = useCallback(
    (config: ConfigItem) => {
      const text = config.enable_confirm
      if (!text) return ''
      return getLocalizedText(text as I18nDict, text['zh-CN'] ?? '', i18n.language)
    },
    [i18n.language]
  )

  const [editingValues, setEditingValues] = useState<Record<string, string>>({})
  const [dirtyKeys, setDirtyKeys] = useState<Set<string>>(new Set())
  /** 等待确认的顶层布尔开关；确认之前 editingValues 不动，取消就是什么都不发生 */
  const [pendingOpenConfirm, setPendingOpenConfirm] = useState<{
    key: string
    value: string
    text: string
  } | null>(null)
  const [visibleSecrets, setVisibleSecrets] = useState<Record<string, boolean>>({})
  const [reloadConfirmOpen, setReloadConfirmOpen] = useState(false)
  const [saveWarningOpen, setSaveWarningOpen] = useState(false)
  const [emptyRequiredFields, setEmptyRequiredFields] = useState<string[]>([])
  const [modelGroups, setModelGroups] = useState<Record<string, ModelGroupConfig>>({})
  const [modelTypes, setModelTypes] = useState<ModelTypeOption[]>([])

  /**
   * 平台方在模型组下拉框里要分得清哪些是租户自建的组：组名全局唯一，但可以长得很像
   * （片假名 カ 与汉字 力、英文 I 与 l），选错一次，平台的对话就发到了租户配置的端点上。
   * `/config/model-groups` 只给组名与配置，归属取自 `/console/model-groups`（与模型组管理页共用缓存）。
   * 只对平台方查：租户只看得到自己的组与平台开放的组，不存在被别的租户冒充的问题。
   */
  const { isPlatformOwner } = useIdentity()
  const ownershipQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.modelGroups,
    queryFn: () => tenantApi.listModelGroups(),
    enabled: isPlatformOwner && Boolean(configService.getModelGroups),
    staleTime: 30_000,
    retry: false,
  })
  const tenantOwnedGroups = useMemo(
    () =>
      new Map(
        (ownershipQuery.data?.items ?? [])
          .filter(group => group.ownership === 'tenant')
          .map(group => [group.name, group.owner_tenant_id] as const)
      ),
    [ownershipQuery.data]
  )
  const [emailSendAccounts, setEmailSendAccounts] = useState<EmailSendAccountOption[]>([])

  const [presets, setPresets] = useState<Preset[]>([])
  const [expandedRows, setExpandedRows] = useState<ExpandedRowsState>({})
  const [restartDialogOpen, setRestartDialogOpen] = useState(false)
  const [isRestarting, setIsRestarting] = useState(false)
  // 保存了 `is_need_restart` 的配置项之后这个框会**自动**弹出来，所以它比仪表盘那个按钮更需要
  // 提前知道结论：租户根本无权重启（后端 `Role.Super`），裸机部署压根没有这个能力。
  // 不问就等于把一个必然失败的「确认重启」递到用户面前，再回一句「请检查网络连接」。
  // 只在框开着的时候探测——后端每次探测要起一个 `systemctl` 子进程，不该让每个配置页都付这份钱。
  const {
    canRestart,
    checking: restartChecking,
    unsupported: restartUnsupported,
    unsupportedReason: restartUnsupportedReason,
    describeRestartError,
  } = useRestartCapability({ enabled: restartDialogOpen })
  const restartAvailable = canRestart && !restartUnsupported
  const hasUnsavedChanges = dirtyKeys.size > 0

  useBeforeUnload(
    useCallback((event: BeforeUnloadEvent) => {
      if (!hasUnsavedChanges) {
        return
      }
      event.preventDefault()
      event.returnValue = ''
    }, [hasUnsavedChanges])
  )

  usePrompt({
    when: useCallback<BlockerFunction>(
      ({ currentLocation, nextLocation }) => {
        if (!hasUnsavedChanges) {
          return false
        }

        if (currentLocation.pathname !== nextLocation.pathname) {
          return true
        }

        const currentCategory = new URLSearchParams(currentLocation.search).get('category')
        const nextCategory = new URLSearchParams(nextLocation.search).get('category')

        return currentCategory !== nextCategory
      },
      [hasUnsavedChanges]
    ),
    message: t('configTable.unsavedChangesConfirm'),
  })

  useEffect(() => {
    if (!onSearchChange) {
      setInternalSearchText(searchText)
    }
  }, [onSearchChange, searchText])

  useEffect(() => {
    const loadData = async () => {
      if (!isScopeReady) {
        return
      }
      try {
        if (configService.getModelGroups) {
          const groups = await configService.getModelGroups()
          setModelGroups(groups)
        }
        if (configService.getModelTypes) {
          const types = await configService.getModelTypes()
          setModelTypes(types)
        }

        // 加载人设数据
        const presetsResponse = await presetsApi.getList({
          page: 1,
          page_size: 1000, // 加载所有人设
          instance_key: currentInstanceKey ?? undefined,
        })
        setPresets(presetsResponse.items)
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : t('messages.operationFailed')
        notification.error(errorMessage)
      }
    }
    loadData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configService, currentInstanceKey, isScopeReady])

  const modelTypeMap = useMemo(
    () => Object.fromEntries(modelTypes.map(mt => [mt.value, mt])),
    [modelTypes]
  )

  const presetMap = useMemo(
    () => Object.fromEntries(presets.map(preset => [preset.id.toString(), preset])),
    [presets]
  )

  const enableStateMap = useMemo(() => {
    const map = new Map<string, boolean>()
    if (!configs) {
      return map
    }
    configs.forEach(config => {
      const currentValue = editingValues[config.key]
      try {
        const value = currentValue !== undefined ? JSON.parse(currentValue) : config.value
        if (typeof value === 'boolean') {
          map.set(config.key, value as boolean)
        }
      } catch {
        if (typeof config.value === 'boolean') {
          map.set(config.key, config.value as boolean)
        }
      }
    })
    return map
  }, [configs, editingValues])

  const checkRequiredFields = useCallback(() => {
    const emptyFields = configs
      .filter(config => {
        if (!config.required) return false
        if (config.enable_toggle && enableStateMap.get(config.enable_toggle) === false) {
          return false
        }
        const currentValue =
          editingValues[config.key] !== undefined ? editingValues[config.key] : String(config.value)
        return !currentValue || currentValue === '' || currentValue === '[]'
      })
      .map(config => getConfigTitle(config) || config.key)

    setEmptyRequiredFields(emptyFields)
    if (emptyFields.length > 0) {
      setSaveWarningOpen(true)
      return false
    }
    return true
  }, [configs, editingValues, enableStateMap, getConfigTitle])

  const handleSaveAllChanges = useCallback(
    async (force: boolean = false) => {
      if (!force && !checkRequiredFields()) {
        return
      }
      try {
        const changedConfigs = Object.fromEntries(
          Array.from(dirtyKeys).map(key => [key, editingValues[key]])
        )
        await configService.batchUpdateConfig(configKey, changedConfigs)
        await configService.saveConfig(configKey)
        notification.success(t('configTable.saveSuccess'))
        setDirtyKeys(new Set())
        setSaveWarningOpen(false)
        onRefresh?.()

        // 检查是否有需要重启的配置项
        const needRestartConfigs = configs.filter(config => {
          if (!dirtyKeys.has(config.key)) return false

          // 首先检查配置项本身的 is_need_restart 属性
          if (config.is_need_restart === true) {
            return true
          }

          // 然后检查字段模式中的 is_need_restart 属性
          const fieldInfo = config.field_schema?.[config.key]
          if (fieldInfo && fieldInfo.is_need_restart === true) {
            return true
          }

          return false
        })

        if (needRestartConfigs.length > 0) {
          setRestartDialogOpen(true)
        }
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : t('messages.saveFailed')
        notification.error(errorMessage)
      }
    },
    [
      checkRequiredFields,
      editingValues,
      dirtyKeys,
      configService,
      configKey,
      notification,
      onRefresh,
      configs,
      t,
    ]
  )

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault()
        handleSaveAllChanges(false)
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [handleSaveAllChanges])

  const handleReloadConfig = async () => {
    try {
      await configService.reloadConfig(configKey)
      notification.success(t('configTable.reloadSuccess'))
      setReloadConfirmOpen(false)
      setEditingValues({})
      setDirtyKeys(new Set())
      onRefresh?.()
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : t('messages.operationFailed')
      notification.error(errorMessage)
    }
  }

  // 处理重启系统
  const handleRestartSystem = async () => {
    setIsRestarting(true)
    try {
      const response = await restartApi.restartSystem()
      if (response.ok) {
        notification.success(t('configTable.restartSent'))
        setRestartDialogOpen(false)
      } else {
        notification.error(t('messages.operationFailed'))
      }
    } catch (error) {
      // 与仪表盘同一条规矩：后端区分「不支持」与「失败」，按 `reason_code` 取本地化文案后
      // 原样透出。旧文案是「重启系统失败，请检查网络连接: ...」，把部署形态不支持误导成网络问题。
      notification.error(describeRestartError(error))
    } finally {
      setIsRestarting(false)
    }
  }

  const toggleSecretVisibility = (key: string) => {
    setVisibleSecrets(prev => ({ ...prev, [key]: !prev[key] }))
  }

  const handleConfigChange = (key: string, value: string) => {
    setEditingValues(prev => ({ ...prev, [key]: value }))
    setDirtyKeys(prev => new Set(prev).add(key))
  }

  const filteredConfigs = useMemo(() => {
    if (!configs) {
      return []
    }

    let processedConfigs = configs.filter(config => !config.is_hidden)

    if (isOverridePage) {
      processedConfigs = processedConfigs.filter(config => !config.key.startsWith('enable_'))
    } else {
      processedConfigs = processedConfigs.filter(config => {
        if (config.enable_toggle && enableStateMap.get(config.enable_toggle) === false) {
          return false
        }
        return true
      })
    }

    if (effectiveSearchText) {
      const lowerSearchText = effectiveSearchText.toLowerCase()
      return processedConfigs.filter(config => {
        const title = getConfigTitle(config)
        const description = getConfigDescription(config)
        return (
          title.toLowerCase().includes(lowerSearchText) ||
          config.key.toLowerCase().includes(lowerSearchText) ||
          (description && description.toLowerCase().includes(lowerSearchText))
        )
      })
    }

    return processedConfigs
  }, [configs, effectiveSearchText, isOverridePage, enableStateMap, getConfigTitle, getConfigDescription])

  useEffect(() => {
    if (configs) {
      const initialValues: Record<string, string> = {}
      configs
        .filter(c => !c.is_hidden)
        .forEach(config => {
          const value =
            typeof config.value === 'object' && config.value !== null
              ? JSON.stringify(config.value, null, 2)
              : String(config.value)
          initialValues[config.key] = value
        })
      setEditingValues(initialValues)
      setDirtyKeys(new Set())
    }
  }, [configs])

  useEffect(() => {
    unifiedConfigApi
      .getConfigList('adapter_email')
      .then(items => {
        const receiveAccountsItem = items.find(item => item.key === 'RECEIVE_ACCOUNTS')
        const value = receiveAccountsItem?.value
        if (!Array.isArray(value)) {
          setEmailSendAccounts([])
          return
        }
        const accounts = value
          .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
          .filter(item => item.SEND_ENABLED === true && typeof item.USERNAME === 'string' && item.USERNAME.trim())
          .map(item => {
            const username = String(item.USERNAME)
            const displayName = typeof item.DISPLAY_NAME === 'string' ? item.DISPLAY_NAME.trim() : ''
            const label = displayName ? `${displayName} (${username})` : username
            return { username, label }
          })
        setEmailSendAccounts(accounts)
      })
      .catch(() => {
        setEmailSendAccounts([])
      })
  }, [])

  const renderConfigInput = (config: ConfigItem, disabled: boolean = false) => {
    const isEditing = Object.prototype.hasOwnProperty.call(editingValues, config.key)
    const rawValue = isEditing ? editingValues[config.key] : String(config.value)
    const isSecret = config.is_secret

    if (config.ref_model_groups) {
      const typeOption = modelTypeMap[config.model_type as string]
      let entries = Object.entries(modelGroups)
      if (typeOption) {
        entries = entries.filter(([, group]) => group.MODEL_TYPE === typeOption.value)
      }
      const modelGroupNames = entries.map(([name]) => name)
      const isInvalidValue = !modelGroupNames.includes(rawValue)

      return (
        <Box sx={{ width: '100%', display: 'flex', alignItems: 'center', gap: 1 }}>
          <TextField
            select
            value={rawValue}
            onChange={e => handleConfigChange(config.key, e.target.value)}
            size="small"
            sx={{ flex: 1 }}
            error={isInvalidValue}
            helperText={isInvalidValue ? t('configTable.currentModelGroupMissing') : undefined}
            placeholder={config.placeholder}
            disabled={disabled}
          >
            {modelGroupNames.map(name => (
              <MenuItem key={name} value={name}>
                {name}
                {tenantOwnedGroups.has(name) && (
                  <Chip
                    size="small"
                    variant="outlined"
                    color="warning"
                    label={t('configTable.tenantOwnedModelGroup', {
                      id: tenantOwnedGroups.get(name) ?? '?',
                    })}
                    sx={{ ml: 1 }}
                  />
                )}
              </MenuItem>
            ))}
          </TextField>
          {typeOption && (
            <Button
              variant="outlined"
              size="small"
              endIcon={<LaunchIcon fontSize={isSmall ? 'small' : 'inherit'} />}
              onClick={() => navigate('/settings/models?tab=basic')}
              sx={{
                flexShrink: 0,
                borderRadius: 999,
                fontWeight: 600,
                px: isSmall ? 1 : 1.5,
                py: isSmall ? 0.1 : 0.4,
                minWidth: 'auto',
                height: isSmall ? 24 : 28,
                fontSize: isSmall ? '0.68rem' : '0.8rem',
              }}
              aria-label={t('configTable.goToModelGroup')}
              disabled={disabled}
            >
              {t('configTable.ModelGroup', {
                label: t(`configTable.modelTypes.${typeOption.value as 'chat' | 'embedding' | 'draw'}`, {
                  defaultValue: typeOption.label,
                }),
              })}
            </Button>
          )}
        </Box>
      )
    }

    if (config.ref_email_accounts_send_enabled) {
      const isInvalidValue = Boolean(rawValue && !emailSendAccounts.some(account => account.username === rawValue))

      return (
        <TextField
          select
          value={rawValue}
          onChange={e => handleConfigChange(config.key, e.target.value)}
          size="small"
          fullWidth
          error={isInvalidValue}
          helperText={isInvalidValue ? t('configTable.currentEmailAccountMissing') : undefined}
          placeholder={config.placeholder}
          disabled={disabled}
        >
          <MenuItem value="">
            <em>{t('configTable.selectEmailAccount')}</em>
          </MenuItem>
          {emailSendAccounts.map(account => (
            <MenuItem key={account.username} value={account.username}>
              {account.label}
            </MenuItem>
          ))}
        </TextField>
      )
    }

    if (config.ref_presets_multiple) {
      // 多人设选择器，rawValue 应该是 JSON 数组字符串
      let selectedIds: number[] = []
      try {
        const parsed = JSON.parse(rawValue || '[]')
        selectedIds = Array.isArray(parsed) ? parsed : []
      } catch {
        selectedIds = []
      }

      return (
        <Box sx={{ width: '100%', display: 'flex', alignItems: 'center', gap: 1 }}>
          <TextField
            select
            value={selectedIds}
            onChange={e => {
              const value = e.target.value as unknown as number[]
              handleConfigChange(config.key, JSON.stringify(value))
            }}
            size="small"
            sx={{ flex: 1 }}
            placeholder={config.placeholder || t('configTable.selectMultiple')}
            disabled={disabled}
            SelectProps={{
              multiple: true,
              displayEmpty: true,
              renderValue: selected => {
                const selectedArray = selected as number[]
                if (selectedArray.length === 0) {
                  return <em>{t('configTable.noPresetSelected')}</em>
                }
                return (
                  <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                    {selectedArray.map(id => {
                      const preset = presetMap[id.toString()]
                      return preset ? (
                        <Chip
                          key={id}
                          size="small"
                          avatar={<Avatar src={preset.avatar} sx={{ width: 16, height: 16 }} />}
                          label={preset.title}
                          sx={{ height: 20, fontSize: '0.7rem' }}
                        />
                      ) : (
                        <Chip
                          key={id}
                          size="small"
                          label={`ID:${id}`}
                          color="error"
                          sx={{ height: 20, fontSize: '0.7rem' }}
                        />
                      )
                    })}
                  </Box>
                )
              },
            }}
          >
            {presets.map(preset => (
              <MenuItem key={preset.id} value={preset.id}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, width: '100%' }}>
                  <Avatar src={preset.avatar} alt={preset.name} sx={{ width: 20, height: 20 }} />
                  <Typography variant="body2" sx={{ flex: 1 }}>
                    {preset.title}
                  </Typography>
                  {preset.is_remote && (
                    <Chip label="Cloud" sx={{ height: 16, fontSize: '0.6rem' }} />
                  )}
                </Box>
              </MenuItem>
            ))}
          </TextField>
          <Button
            variant="outlined"
            size="small"
            onClick={() => handleConfigChange(config.key, JSON.stringify([]))}
            disabled={disabled || selectedIds.length === 0}
            sx={{
              flexShrink: 0,
              borderRadius: 999,
              fontWeight: 600,
              px: isSmall ? 1 : 1.5,
              py: isSmall ? 0.1 : 0.4,
              minWidth: 'auto',
              height: isSmall ? 24 : 28,
              fontSize: isSmall ? '0.68rem' : '0.8rem',
              mr: 1,
            }}
            aria-label={t('configTable.clearSelection')}
          >
            {t('actions.clear')}
          </Button>
          <Button
            variant="outlined"
            size="small"
            endIcon={<LaunchIcon fontSize={isSmall ? 'small' : 'inherit'} />}
            onClick={() => navigate('/presets')}
            sx={{
              flexShrink: 0,
              borderRadius: 999,
              fontWeight: 600,
              px: isSmall ? 1 : 1.5,
              py: isSmall ? 0.1 : 0.4,
              minWidth: 'auto',
              height: isSmall ? 24 : 28,
              fontSize: isSmall ? '0.68rem' : '0.8rem',
            }}
            aria-label={t('configTable.jumpToPresetManager')}
            disabled={disabled}
          >
            {t('configTable.managePresets')}
          </Button>
        </Box>
      )
    }

    if (config.ref_presets) {
      // 对于人设选择器，rawValue 应该是数字字符串或 "-1"
      const noDefault = config.ref_presets_no_default
      const numericValue = rawValue === '' ? (noDefault ? '' : '-1') : rawValue
      const selectedPreset = numericValue !== '-1' && numericValue !== '' ? presetMap[numericValue] : null
      const isInvalidValue = Boolean(numericValue !== '-1' && numericValue !== '' && !selectedPreset)

      return (
        <Box sx={{ width: '100%', display: 'flex', alignItems: 'center', gap: 1 }}>
          <TextField
            select
            value={numericValue}
            onChange={e => handleConfigChange(config.key, e.target.value)}
            size="small"
            sx={{ flex: 1 }}
            error={isInvalidValue}
            helperText={isInvalidValue ? t('configTable.presetMissing') : undefined}
            placeholder={config.placeholder || t('configTable.selectPreset')}
            disabled={disabled}
            SelectProps={{
              displayEmpty: true,
            }}
          >
            {!noDefault && (
              <MenuItem value="-1">
                <em>{t('configTable.defaultPreset')}</em>
              </MenuItem>
            )}
            {presets.map(preset => (
              <MenuItem key={preset.id} value={preset.id.toString()}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <Avatar src={preset.avatar} alt={preset.name} sx={{ width: 20, height: 20 }} />
                  <Typography variant="body2">{preset.title}</Typography>
                  {preset.is_remote && (
                    <Chip
                      label={t('configTable.cloudPreset')}
                      size="small"
                      color="primary"
                      variant="outlined"
                      sx={{ height: 16, fontSize: '0.6rem', ml: 0.5 }}
                    />
                  )}
                </Box>
              </MenuItem>
            ))}
          </TextField>
          <Button
            variant="outlined"
            size="small"
            endIcon={<LaunchIcon fontSize={isSmall ? 'small' : 'inherit'} />}
            onClick={() => navigate('/presets')}
            sx={{
              flexShrink: 0,
              borderRadius: 999,
              fontWeight: 600,
              px: isSmall ? 1 : 1.5,
              py: isSmall ? 0.1 : 0.4,
              minWidth: 'auto',
              height: isSmall ? 24 : 28,
              fontSize: isSmall ? '0.68rem' : '0.8rem',
            }}
            aria-label={t('configTable.managePresets')}
            disabled={disabled}
          >
            {t('configTable.managePresets')}
          </Button>
        </Box>
      )
    }

    if (config.enum) {
      return (
        <TextField
          select
          value={rawValue}
          onChange={e => handleConfigChange(config.key, e.target.value)}
          size="small"
          fullWidth
          placeholder={config.placeholder}
          disabled={disabled}
        >
          {config.enum.map(option => (
            <MenuItem key={option} value={option}>
              {option}
            </MenuItem>
          ))}
        </TextField>
      )
    }

    if (
      config.type === 'list' ||
      config.is_complex ||
      (config.type === 'dict' && !config.is_complex)
    ) {
      const isExpanded = expandedRows[config.key] || false
      let displayValue: string
      try {
        const parsedValue = isEditing ? JSON.parse(rawValue) : config.value
        if (config.type === 'list') {
          displayValue = t('configTable.listCount', {
            count: (Array.isArray(parsedValue) ? parsedValue : []).length,
          })
        } else {
          displayValue = t('configTable.dictCount', {
            count: Object.keys(typeof parsedValue === 'object' && parsedValue ? parsedValue : {})
              .length,
          })
        }
      } catch {
        displayValue = t('configTable.invalidJson')
      }

      const canBeNested = config.type === 'list' || config.is_complex

      if (canBeNested) {
        return (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, width: '100%' }}>
            <TextField
              value={displayValue}
              size="small"
              fullWidth
              InputProps={{
                readOnly: true,
                sx: {
                  cursor: 'pointer',
                  bgcolor: 'transparent',
                  '&:hover': { bgcolor: 'action.hover' },
                },
              }}
              onClick={() =>
                !disabled && setExpandedRows(prev => ({ ...prev, [config.key]: !prev[config.key] }))
              }
              variant="outlined"
              sx={{
                '& .MuiOutlinedInput-root': {
                  '&:hover .MuiOutlinedInput-notchedOutline': {
                    borderColor: 'primary.main',
                  },
                },
              }}
              disabled={disabled}
            />
            <IconButton
              size="small"
              onClick={() =>
                setExpandedRows(prev => ({ ...prev, [config.key]: !prev[config.key] }))
              }
              disabled={disabled}
            >
              {isExpanded ? <ExpandLessIcon /> : <ExpandMoreIcon />}
            </IconButton>
          </Box>
        )
      }
    }

    switch (config.type) {
      case 'bool':
        return (
          <FormControlLabel
            control={
              <Switch
                checked={rawValue === 'true'}
                onChange={e => {
                  const nextValue = String(e.target.checked)
                  // 只拦「关 → 开」：关掉一个高风险开关不需要再确认一次，多一步只会让人
                  // 习惯性地连点确认。`rawValue` 取的是界面上当前显示的值（覆盖层里也
                  // 一样），所以先关再开同样会重新弹。
                  const confirmText =
                    e.target.checked && rawValue !== 'true'
                      ? getEnableConfirmText(config)
                      : ''
                  if (confirmText) {
                    setPendingOpenConfirm({
                      key: config.key,
                      value: nextValue,
                      text: confirmText,
                    })
                    return
                  }
                  handleConfigChange(config.key, nextValue)
                }}
                color="primary"
                disabled={disabled}
              />
            }
            label={rawValue === 'true' ? t('common.yes') : t('common.no')}
            disabled={disabled}
          />
        )
      case 'int':
      case 'float':
        return (
          <TextField
            type="number"
            value={rawValue}
            onChange={e => handleConfigChange(config.key, e.target.value)}
            size="small"
            fullWidth
            placeholder={config.placeholder}
            disabled={disabled}
          />
        )
      default:
        return (
          <TextField
            value={rawValue}
            onChange={e => handleConfigChange(config.key, e.target.value)}
            size="small"
            fullWidth
            type="text"
            placeholder={config.placeholder}
            multiline={config.is_textarea}
            minRows={config.is_textarea ? 3 : 1}
            maxRows={config.is_textarea ? 8 : 1}
            disabled={disabled}
            InputProps={{
              style:
                isSecret && !visibleSecrets[config.key]
                  ? ({
                      WebkitTextSecurity: 'disc',
                    } as React.CSSProperties)
                  : undefined,
              endAdornment: isSecret ? (
                <InputAdornment position="end">
                  <IconButton
                    onClick={() => toggleSecretVisibility(config.key)}
                    edge="end"
                    disabled={disabled}
                  >
                    {visibleSecrets[config.key] ? <VisibilityOffIcon /> : <VisibilityIcon />}
                  </IconButton>
                </InputAdornment>
              ) : undefined,
            }}
          />
        )
    }
  }

  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%' }}>
        <CircularProgress />
      </Box>
    )
  }

  return (
    <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      <Stack direction="column" spacing={2} sx={{ flexShrink: 0 }}>
        {title && (
          <Typography variant="h5" component="h2" sx={{ fontWeight: 600 }}>
            {title}
          </Typography>
        )}
        {infoBox && <Alert severity="info">{infoBox}</Alert>}
        {showToolbar && (
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={2} justifyContent="space-between">
            {showSearchBar && (
              <TextField
                size="small"
                sx={{ flexGrow: 1 }}
                placeholder={t('configTable.searchPlaceholder')}
                value={effectiveSearchText}
                onCompositionStart={e => {
                  const nextValue = e.target instanceof HTMLInputElement ? e.target.value : ''
                  setIsSearchComposing(true)
                  setSearchCompositionText(nextValue)
                }}
                onCompositionEnd={e => {
                  const nextValue = e.target instanceof HTMLInputElement ? e.target.value : ''
                  setIsSearchComposing(false)
                  setSearchCompositionText(nextValue)
                  updateSearchText(nextValue)
                }}
                onChange={e => {
                  const nextValue = e.target.value
                  if (isSearchComposing || (e.nativeEvent instanceof InputEvent && e.nativeEvent.isComposing)) {
                    setSearchCompositionText(nextValue)
                    return
                  }
                  updateSearchText(nextValue)
                }}
                InputProps={{
                  startAdornment: (
                    <InputAdornment position="start">
                      <SearchIcon />
                    </InputAdornment>
                  ),
                }}
              />
            )}
            <Stack direction="row" spacing={1} sx={{ flexShrink: 0, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              {toolbarActions}
              <Button
                variant="contained"
                size="small"
                onClick={() => handleSaveAllChanges(false)}
                startIcon={<SaveIcon />}
                disabled={!hasUnsavedChanges}
              >
                {t('configTable.saveChanges')}
                {dirtyKeys.size > 0 && ` (${dirtyKeys.size})`}
              </Button>
              <Button
                variant="outlined"
                size="small"
                onClick={() => setReloadConfirmOpen(true)}
                startIcon={<RefreshIcon />}
              >
                {t('configTable.resetConfig')}
              </Button>
            </Stack>
          </Stack>
        )}
      </Stack>

      <Paper
        elevation={3}
        sx={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0,
          overflow: 'hidden',
          ...UNIFIED_TABLE_STYLES.paper,
        }}
      >
        <TableContainer sx={{ flex: 1, overflow: 'auto', ...UNIFIED_TABLE_STYLES.scrollbar }}>
          <Table stickyHeader size={isSmall ? 'small' : 'medium'}>
            <TableHead>
              <TableRow sx={UNIFIED_TABLE_STYLES.header}>
                {isOverridePage && (
                  <TableCell sx={{ width: '10%', minWidth: 80 }}>
                    {t('configTable.headerEnableOverride')}
                  </TableCell>
                )}
                <TableCell sx={{ width: '20%', minWidth: 200 }}>
                  {t('configTable.headerConfigItem')}
                </TableCell>
                <TableCell sx={{ width: '5%', minWidth: 80 }}>
                  {t('configTable.headerAttribute')}
                </TableCell>
                {showCategoryColumn && (
                  <TableCell sx={{ width: '10%', minWidth: 120 }}>
                    {t('configTable.headerCategory')}
                  </TableCell>
                )}
                <TableCell sx={{ width: '5%', minWidth: 80 }}>
                  {t('configTable.headerType')}
                </TableCell>
                <TableCell
                  sx={{
                    width: showCategoryColumn
                      ? isOverridePage
                        ? '35%'
                        : '40%'
                      : isOverridePage
                        ? '45%'
                        : '50%',
                    minWidth: 300,
                  }}
                >
                  {t('configTable.headerValue')}
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {filteredConfigs.flatMap(config => {
                const isEnabled =
                  isOverridePage && config.enable_toggle
                    ? (enableStateMap.get(config.enable_toggle) ?? false)
                    : true
                // 平台方专属项对租户只读：服务端本来就拒，页面不给一个点了必然 403 的输入框
                const platformLocked = !isPlatformOwner && Boolean(config.platform_only)

                const mainRow = (
                  <TableRow
                    key={config.key}
                    sx={{
                      ...UNIFIED_TABLE_STYLES.row,
                      transition: 'opacity 0.2s, background-color 0.2s',
                      ...(!isEnabled &&
                        isOverridePage && {
                          opacity: 0.6,
                          '&:hover': {
                            bgcolor: 'transparent',
                          },
                        }),
                    }}
                  >
                    {isOverridePage && (
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                        {config.enable_toggle ? (
                          <Switch
                            checked={isEnabled}
                            disabled={platformLocked}
                            onChange={e => {
                              handleConfigChange(
                                config.enable_toggle as string,
                                String(e.target.checked)
                              )
                              // 打开覆盖时把界面上显示的那个值一并提交。覆盖关着的时候
                              // 服务端返回的是「当前继承到的有效值」，而文件里存的是另一回事；
                              // 只提交开关的话钉住的是文件里那份值，与用户看到的不一致。
                              if (e.target.checked) {
                                handleConfigChange(
                                  config.key,
                                  editingValues[config.key] ?? String(config.value)
                                )
                              }
                            }}
                            color="primary"
                            size="small"
                          />
                        ) : (
                          <Tooltip title={t('configTable.cannotDisable')} arrow>
                            <Box sx={{ textAlign: 'center' }}>-</Box>
                          </Tooltip>
                        )}
                      </TableCell>
                    )}
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <Stack spacing={0.5}>
                        <Box
                          sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 0.5 }}
                        >
                          {config.required && (
                            <Typography component="span" color="error" sx={{ fontWeight: 'bold' }}>
                              *
                            </Typography>
                          )}
                          <Typography
                            variant="body2"
                            sx={{ fontWeight: 600, color: 'text.primary' }}
                          >
                            {getConfigTitle(config)}
                          </Typography>
                          {getConfigDescription(config) && (
                            <HtmlTooltip
                              title={
                                <div
                                  dangerouslySetInnerHTML={{
                                    __html: getConfigDescription(config) || '',
                                  }}
                                />
                              }
                              placement="right"
                            >
                              <IconButton
                                size="small"
                                sx={{
                                  p: 0.2,
                                  color: 'text.secondary',
                                  verticalAlign: 'middle',
                                  cursor: 'help',
                                }}
                              >
                                <HelpOutlineIcon sx={{ fontSize: '1rem' }} />
                              </IconButton>
                            </HtmlTooltip>
                          )}
                        </Box>
                        <Typography
                          variant="caption"
                          color="text.secondary"
                          sx={{ fontFamily: 'monospace' }}
                        >
                          {config.key}
                        </Typography>
                      </Stack>
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <Stack spacing={1} direction="row" alignItems="center" flexWrap="wrap">
                        {config.overridable && configKey === 'system' && (
                          <Chip
                            label={t('configTable.overridable')}
                            size="small"
                            color="info"
                            variant="outlined"
                            sx={{ ...CHIP_VARIANTS.base(isSmall) }}
                          />
                        )}
                        {config.platform_only && (
                          <Chip
                            label={t('configTable.platformOnly')}
                            size="small"
                            color="warning"
                            variant="outlined"
                            sx={{ ...CHIP_VARIANTS.base(isSmall) }}
                          />
                        )}
                        {config.tenant_may_only_lower && !isPlatformOwner && (
                          <Chip
                            label={t('configTable.tenantMayOnlyLower')}
                            size="small"
                            color="default"
                            variant="outlined"
                            sx={{ ...CHIP_VARIANTS.base(isSmall) }}
                          />
                        )}
                      </Stack>
                    </TableCell>
                    {showCategoryColumn && (
                      <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                        {getConfigCategory(config) ? (
                          <Chip
                            label={getConfigCategory(config)}
                            size="small"
                            color="secondary"
                            variant="outlined"
                            sx={{ ...CHIP_VARIANTS.base(isSmall) }}
                          />
                        ) : (
                          '-'
                        )}
                      </TableCell>
                    )}
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      <Chip
                        label={
                          config.ref_presets_multiple
                            ? 'presets'
                            : config.ref_presets
                              ? 'preset'
                              : config.type
                        }
                        size="small"
                        color={getTypeColor(
                          config.ref_presets_multiple
                            ? 'presets'
                            : config.ref_presets
                              ? 'preset'
                              : config.type,
                          config.is_complex
                        )}
                        variant="outlined"
                        sx={CHIP_VARIANTS.base(isSmall)}
                      />
                    </TableCell>
                    <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                      {renderConfigInput(config, (isOverridePage ? !isEnabled : false) || platformLocked)}
                    </TableCell>
                  </TableRow>
                )

                const canBeNested = config.type === 'list' || config.is_complex
                const nestedRows =
                  expandedRows[config.key] && canBeNested
                    ? renderNestedConfigRows(
                        config,
                        editingValues,
                        handleConfigChange,
                        isOverridePage,
                        isSmall,
                        expandedRows,
                        setExpandedRows,
                        t,
                        i18n.language,
                        1
                      )
                    : []

                return [mainRow, ...nestedRows]
              })}
            </TableBody>
          </Table>
        </TableContainer>
        {filteredConfigs.length === 0 && (
          <Box
            sx={{
              p: 4,
              display: 'flex',
              justifyContent: 'center',
              alignItems: 'center',
              height: '100%',
            }}
          >
            <Typography variant="body1" color="textSecondary">
              {actualEmptyMessage}
            </Typography>
          </Box>
        )}
      </Paper>
      <ConfirmDialog
        open={reloadConfirmOpen}
        onClose={() => setReloadConfirmOpen(false)}
        title={t('configTable.resetConfirmTitle')}
        content={t('configTable.resetConfirmContent')}
        onConfirm={handleReloadConfig}
      />
      <ConfirmDialog
        open={saveWarningOpen}
        onClose={() => setSaveWarningOpen(false)}
        title={t('configTable.saveWarningTitle')}
        content={
          <Box>
            <Typography sx={{ mb: 1 }}>
              {emptyRequiredFields.length > 0
                ? t('configTable.saveWarningContent')
                : t('configTable.saveWarningContentSimple')}
            </Typography>
            {emptyRequiredFields.length > 0 && (
              <List dense>
                {emptyRequiredFields.map((field, index) => (
                  <ListItem key={index} sx={{ py: 0 }}>
                    <Typography color="error">• {field}</Typography>
                  </ListItem>
                ))}
              </List>
            )}
          </Box>
        }
        onConfirm={() => handleSaveAllChanges(true)}
      />
      <ConfirmDialog
        open={pendingOpenConfirm !== null}
        onClose={() => setPendingOpenConfirm(null)}
        title={t('configTable.confirmEnableTitle')}
        content={
          <Typography sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {pendingOpenConfirm?.text ?? ''}
          </Typography>
        }
        confirmText={t('configTable.confirmEnableButton')}
        onConfirm={() => {
          if (pendingOpenConfirm) {
            handleConfigChange(pendingOpenConfirm.key, pendingOpenConfirm.value)
          }
          setPendingOpenConfirm(null)
        }}
      />

      {/* 重启系统确认对话框 */}
      <Dialog
        open={restartDialogOpen}
        onClose={() => !isRestarting && setRestartDialogOpen(false)}
        PaperProps={{
          sx: {
            borderRadius: '12px',
            maxWidth: '500px',
          },
        }}
      >
        <DialogTitle
          sx={{
            px: 3,
            py: 2,
            background: theme =>
              theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)',
          }}
        >
          {t('configTable.restartConfirmTitle')}
        </DialogTitle>
        <DialogContent sx={{ p: 3 }}>
          {/* 能重启时才警告「连接会断」；不能重启时什么都不会断，那句警告纯属吓人 */}
          {restartAvailable && (
            <Alert severity="warning" sx={{ mb: 2 }}>
              {t('messages.connectionLost')}
            </Alert>
          )}
          {!canRestart && (
            <Alert severity="info" sx={{ mb: 2 }}>
              {t('configTable.restartNotPermitted')}
            </Alert>
          )}
          {canRestart && restartUnsupported && (
            <Alert severity="info" sx={{ mb: 2 }}>
              {restartUnsupportedReason}
            </Alert>
          )}
          <Typography sx={{ mt: 1, mb: 2 }}>
            {restartAvailable ? t('configTable.restartConfirm') : t('configTable.restartNeeded')}
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <Button onClick={() => setRestartDialogOpen(false)} disabled={isRestarting}>
            {restartAvailable ? t('actions.cancel') : t('actions.close')}
          </Button>
          {/* 做不到的时候不渲染这个按钮：点了必然拿到 403，而「改动已保存」这件事已经说清楚了 */}
          {restartAvailable && (
            <Button
              onClick={handleRestartSystem}
              variant="contained"
              disabled={isRestarting || restartChecking}
              startIcon={<RestartIcon />}
            >
              {isRestarting ? t('configTable.restarting') : t('configTable.restartBtn')}
            </Button>
          )}
        </DialogActions>
      </Dialog>
    </Box>
  )
}
