import { useState, useEffect, useRef, useCallback } from 'react'
import {
  Box,
  Typography,
  TextField,
  Card,
  CardContent,
  CardActions,
  Grid,
  Chip,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  InputAdornment,
  Divider,
  Tooltip,
  Alert,
  CircularProgress,
  FormControlLabel,
  Checkbox,
  Link,
  Menu,
  MenuItem,
  ListItemIcon,
  ListItemText,
  Tab,
  useTheme,
  useMediaQuery,
} from '@mui/material'
import {
  Add as AddIcon,
  Search as SearchIcon,
  Sync as SyncIcon,
  Delete as DeleteIcon,
  Edit as EditIcon,
  CloudDownload as CloudDownloadIcon,
  CloudUpload as CloudUploadIcon,
  CloudOff as CloudOffIcon,
  Upload as UploadIcon,
  Tag as TagIcon,
  CloudSync as CloudSyncIcon,
  Close as CloseIcon,
  InfoOutlined as InfoIcon,
  FilterList as FilterListIcon,
  Clear as ClearIcon,
  Star as StarIcon,
  StarBorder as StarBorderIcon,
  Download as DownloadIcon,
  FilePresent as FilePresentIcon,
  Image as ImageIcon,
  DataObject as DataObjectIcon,
  MenuBook as MenuBookIcon,
} from '@mui/icons-material'
import { ApiError } from '../../services/api/axios'
import { Preset, PresetDetail, presetsApi, TagInfo } from '../../services/api/presets'
import { tavernApi, type TavernScope } from '../../services/api/tavern'
import CardImportDialog from '../../components/tavern/CardImportDialog'
import TavernExtensionPanel from '../../components/tavern/TavernExtensionPanel'
import { InlineTabs } from '../../components/common/NekroTabs'
import type { PresetSwitchImpact } from '../../services/api/types'
import { presetsMarketApi } from '../../services/api/cloud/presets_market'
import { useSnackbar } from 'notistack'
import { formatLastActiveTime } from '../../utils/time'

import { CHIP_VARIANTS, CARD_VARIANTS, SCROLLBAR_VARIANTS } from '../../theme/variants'
import { UI_STYLES } from '../../theme/themeConfig'
import { Fade } from '@mui/material'
import PaginationStyled from '../../components/common/PaginationStyled'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import ActionButton from '../../components/common/ActionButton'
import IconActionButton from '../../components/common/IconActionButton'
import InstanceScopeBanner from '../../components/common/InstanceScopeBanner'
import PresetSwitchConfirmDialog from '../../components/common/PresetSwitchConfirmDialog'
import PresetAvatar from '../../components/common/PresetAvatar'
import DomainSettingsButton from '../../components/settings/DomainSettingsButton'
import { useIdentity } from '../../hooks/useIdentity'
import { useInstanceScope } from '../../hooks/useInstanceScope'

// 定义预设编辑表单数据类型
interface PresetFormData {
  name: string
  title: string
  avatar: string
  content: string
  description: string
  tags: string
  author: string
  remove_remote: boolean
}

// 编辑对话框组件
const PresetEditDialog = ({
  open,
  onClose,
  preset,
  onSave,
  scope,
  isNew = false,
}: {
  open: boolean
  onClose: () => void
  preset?: PresetDetail
  onSave: (data: PresetFormData) => Promise<void>
  /** 作用域与页面当前实例同一份：面板读写扩展行时要用 */
  scope: TavernScope
  isNew?: boolean
}) => {
  const [loading, setLoading] = useState(false)
  const [formData, setFormData] = useState<PresetFormData>({
    name: '',
    title: '',
    avatar: '',
    content: '',
    description: '',
    tags: '',
    author: '',
    remove_remote: false,
  })
  const [confirmDialog, setConfirmDialog] = useState(false)
  const [errors, setErrors] = useState<Record<string, string>>({})
  // 编辑已有人设才分标签；新建时还没有 ID，扩展面板无从读取
  const [activeTab, setActiveTab] = useState<'basic' | 'tavern'>('basic')
  // 面板的未保存状态：关闭对话框前据此决定要不要确认
  const [tavernDirty, setTavernDirty] = useState(false)
  const [closeConfirmOpen, setCloseConfirmOpen] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const { enqueueSnackbar } = useSnackbar()
  const { t } = useTranslation('presets')
  const { t: tTavern } = useTranslation('tavern')
  const editingExisting = !isNew && Boolean(preset)
  // 新建人设没有扩展标签；其余「当前是否在酒馆扩展页」都由它收口，避免出现
  // 「标签没渲染、activeTab 却停在 tavern」这种看不见表单也看不见保存的状态
  const onTavernTab = editingExisting && activeTab === 'tavern'

  useEffect(() => {
    if (preset) {
      setFormData({
        name: preset.name,
        title: preset.title || '',
        avatar: preset.avatar,
        content: preset.content || '',
        description: preset.description || '',
        tags: preset.tags || '',
        author: preset.author || '',
        remove_remote: false,
      })
    } else {
      setFormData({
        name: '',
        title: '',
        avatar: '',
        content: '',
        description: '',
        tags: '',
        author: '',
        remove_remote: false,
      })
    }
    // 重置错误状态
    setErrors({})
    setActiveTab('basic')
    setTavernDirty(false)
    setCloseConfirmOpen(false)
  }, [preset, open])

  /**
   * 关闭前先问有没有未保存的扩展改动
   *
   * 标签切换不会丢输入（两边都保持挂载），所以只有真正关闭才需要确认；背景点击与
   * Esc 也走这一个出口。
   */
  const handleRequestClose = () => {
    if (tavernDirty) {
      setCloseConfirmOpen(true)
      return
    }
    onClose()
  }

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    const { name, value } = e.target
    setFormData(prev => ({
      ...prev,
      [name]: value,
    }))

    // 当字段值改变时，清除对应的错误信息
    if (errors[name]) {
      setErrors(prev => {
        const newErrors = { ...prev }
        delete newErrors[name]
        return newErrors
      })
    }
  }

  const handleTagKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault()
      const input = e.target as HTMLInputElement
      const value = input.value.trim()

      if (value) {
        // 检查是否已有8个标签
        const tagsArray = formData.tags.split(',').filter(tag => tag.trim())
        if (tagsArray.length >= 8) {
          setErrors(prev => ({
            ...prev,
            tags: t('form.maxTags'),
          }))
          return
        }

        // 检查是否是重复标签
        if (tagsArray.some(tag => tag.trim().toLowerCase() === value.toLowerCase())) {
          setErrors(prev => ({
            ...prev,
            tags: t('form.duplicateTag'),
          }))
          return
        }

        // 清除标签错误信息
        if (errors.tags) {
          setErrors(prev => {
            const newErrors = { ...prev }
            delete newErrors.tags
            return newErrors
          })
        }

        // 清除输入框，并将新标签添加到已有标签中
        input.value = ''
        const newTags = formData.tags ? `${formData.tags},${value}` : value
        setFormData(prev => ({
          ...prev,
          tags: newTags,
        }))
      }
    }
  }

  const handleDeleteTag = (tagToDelete: string) => {
    const tagsArray = formData.tags
      .split(',')
      .filter(tag => tag.trim() && tag.trim() !== tagToDelete.trim())
    setFormData(prev => ({
      ...prev,
      tags: tagsArray.join(','),
    }))
  }

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return

    try {
      setLoading(true)
      const response = await presetsApi.uploadAvatar(file)
      setFormData(prev => ({
        ...prev,
        avatar: response.avatar,
      }))
      enqueueSnackbar(t('form.uploadSuccess'), { variant: 'success' })
    } catch {
      setErrors(prev => ({
        ...prev,
        avatar: t('form.retryUpload'),
      }))
    } finally {
      setLoading(false)
    }
  }

  // 验证表单
  const validateForm = (): boolean => {
    const newErrors: Record<string, string> = {}

    if (!formData.name.trim()) {
      newErrors.name = t('form.nameRequired')
    }

    if (!formData.content.trim()) {
      newErrors.content = t('form.contentRequired')
    }

    setErrors(newErrors)
    return Object.keys(newErrors).length === 0
  }

  const handleSave = async () => {
    // 验证表单
    if (!validateForm()) {
      return
    }

    // 如果是云端人设且正在编辑，但不是自己上传的(on_shared为false)，需要确认是否解除云端关联
    if (preset?.remote_id && !isNew && !preset?.on_shared && !confirmDialog) {
      setConfirmDialog(true)
      return
    }

    try {
      setLoading(true)
      await onSave({
        ...formData,
        // 如果是自己上传的云端人设(on_shared为true)，则不解除关联
        remove_remote: preset?.remote_id && !preset?.on_shared ? true : false,
      })
      handleRequestClose()
    } catch (error) {
      // 显示通用错误
      setErrors(prev => ({
        ...prev,
        general: error instanceof Error ? error.message : String(error),
      }))
    } finally {
      setLoading(false)
      setConfirmDialog(false)
    }
  }

  const handleRemoteConfirm = async (confirm: boolean) => {
    setConfirmDialog(false)
    if (confirm) {
      try {
        setLoading(true)
        await onSave({
          ...formData,
          remove_remote: true,
        })
        handleRequestClose()
      } catch (error) {
        // 显示通用错误
        setErrors(prev => ({
          ...prev,
          general: error instanceof Error ? error.message : String(error),
        }))
      } finally {
        setLoading(false)
      }
    }
  }

  return (
    <>
      <Dialog
        open={open}
        onClose={handleRequestClose}
        maxWidth="md"
        fullWidth
        scroll="paper"
        PaperProps={{
          sx: {
            ...CARD_VARIANTS.default.styles,
            overflow: 'hidden',
          },
        }}
      >
        <DialogTitle
          sx={{
            px: 3,
            py: 2,
            background: theme =>
              theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)',
            borderBottom: '1px solid',
            borderColor: 'divider',
          }}
        >
          {isNew ? t('dialog.createTitle') : t('dialog.editTitle')}
        </DialogTitle>
        <DialogContent dividers sx={{ p: 3 }}>
          {/* 编辑已有人设才分标签：新建时没有 ID，扩展面板没有可读的行 */}
          {editingExisting ? (
            <InlineTabs
              value={activeTab}
              onChange={(_event, value: 'basic' | 'tavern') => setActiveTab(value)}
              sx={{ mb: 2 }}
            >
              <Tab value="basic" label={tTavern('extension.tab.basic')} />
              <Tab value="tavern" label={tTavern('extension.tab.tavern')} />
            </InlineTabs>
          ) : null}
          {/* 两个标签都保持挂载：切换标签不能丢两边各自未保存的输入 */}
          <Box
            component="form"
            noValidate
            sx={{ mt: 1, display: onTavernTab ? 'none' : 'block' }}
            className="space-y-4"
            autoComplete="off"
          >
            {preset?.remote_id && preset?.on_shared && (
              <Alert severity="info" sx={{ mb: 2 }}>
                {t('cloud.sharedInfo')}
              </Alert>
            )}

            {errors.general && (
              <Alert severity="error" sx={{ mb: 2 }}>
                {errors.general}
              </Alert>
            )}

            <Grid container spacing={2}>
              <Grid item xs={12} md={4}>
                <Box className="flex flex-col items-center">
                  <Box
                    sx={{
                      width: '100%',
                      paddingBottom: '100%',
                      position: 'relative',
                      bgcolor: 'rgba(0,0,0,0.05)',
                      borderRadius: 2,
                      mb: 2,
                    }}
                  >
                    {formData.avatar ? (
                      <img
                        src={formData.avatar}
                        alt={t('form.avatar')}
                        style={{
                          position: 'absolute',
                          top: 0,
                          left: 0,
                          width: '100%',
                          height: '100%',
                          objectFit: 'cover',
                          borderRadius: 8,
                        }}
                      />
                    ) : (
                      <Box
                        sx={{
                          position: 'absolute',
                          top: 0,
                          left: 0,
                          width: '100%',
                          height: '100%',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          color: 'text.secondary',
                        }}
                      >
                        <Typography variant="body2">{t('form.noAvatar')}</Typography>
                      </Box>
                    )}
                    {loading && (
                      <Box
                        sx={{
                          position: 'absolute',
                          top: 0,
                          left: 0,
                          width: '100%',
                          height: '100%',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          bgcolor: 'rgba(0,0,0,0.3)',
                          borderRadius: 2,
                        }}
                      >
                        <CircularProgress />
                      </Box>
                    )}
                  </Box>
                  <input
                    type="file"
                    hidden
                    ref={fileInputRef}
                    accept="image/*"
                    onChange={handleFileUpload}
                    autoComplete="off"
                  />
                  <ActionButton
                    variant="outlined"
                    onClick={() => fileInputRef.current?.click()}
                    disabled={loading}
                  >
                    {t('form.uploadAvatar')}
                  </ActionButton>
                  {errors.avatar && (
                    <Typography variant="caption" color="error" mt={1}>
                      {errors.avatar}
                    </Typography>
                  )}
                  <Typography variant="caption" color="text.secondary" mt={1}>
                    {t('form.avatarTip')}
                  </Typography>
                </Box>
              </Grid>
              <Grid item xs={12} md={8} className="space-y-4">
                <TextField
                  name="name"
                  label={t('form.name')}
                  fullWidth
                  required
                  value={formData.name}
                  onChange={handleChange}
                  autoComplete="off"
                  error={!!errors.name}
                  helperText={errors.name}
                />
                <TextField
                  name="title"
                  label={t('form.title')}
                  fullWidth
                  value={formData.title}
                  onChange={handleChange}
                  helperText={t('form.titleHelper')}
                  autoComplete="off"
                />
                <Box sx={{ width: '100%', mb: 2 }}>
                  <Typography variant="body2" color="text.secondary" gutterBottom>
                    {t('form.tags')}
                  </Typography>

                  <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mb: 1 }}>
                    {formData.tags
                      .split(',')
                      .map(
                        (tag, index) =>
                          tag.trim() && (
                            <Chip
                              key={index}
                              label={tag.trim()}
                              size="small"
                              onDelete={() => handleDeleteTag(tag)}
                              sx={{ ...CHIP_VARIANTS.base(true) }}
                            />
                          )
                      )}
                  </Box>

                  <TextField
                    fullWidth
                    placeholder={t('form.tagsPlaceholder')}
                    size="small"
                    onKeyDown={handleTagKeyDown}
                    onChange={e => {
                      // 实时过滤非法字符
                      e.target.value = e.target.value.replace(/[^a-zA-Z0-9\u4e00-\u9fa5,\s]/g, '')
                    }}
                    InputProps={{
                      startAdornment: formData.tags ? (
                        <InputAdornment position="start">
                          <TagIcon fontSize="small" sx={{ opacity: 0.6 }} />
                        </InputAdornment>
                      ) : null,
                    }}
                    autoComplete="off"
                    helperText={errors.tags || t('form.tagsHelper')}
                  />
                </Box>
                <TextField
                  name="author"
                  label={t('form.author')}
                  fullWidth
                  value={formData.author}
                  onChange={handleChange}
                  autoComplete="off"
                />
                <TextField
                  name="description"
                  label={t('form.description')}
                  fullWidth
                  multiline
                  rows={2}
                  value={formData.description}
                  onChange={handleChange}
                  autoComplete="off"
                  helperText={t('form.descriptionHelper')}
                />
                <TextField
                  name="content"
                  label={t('form.content')}
                  fullWidth
                  required
                  multiline
                  rows={8}
                  value={formData.content}
                  onChange={handleChange}
                  autoComplete="off"
                  error={!!errors.content}
                  helperText={errors.content}
                />
              </Grid>
            </Grid>
          </Box>
          {editingExisting && preset ? (
            <Box sx={{ display: onTavernTab ? 'block' : 'none', mt: 1 }}>
              <TavernExtensionPanel
                presetId={preset.id}
                scope={scope}
                readOnly={false}
                onContentReassembled={content =>
                  // 重新拼装已经改了服务端的正文；把「基本」表单的同一份也换掉，
                  // 否则回到那边点保存会用旧正文把结果盖回去
                  setFormData(previous => ({ ...previous, content }))
                }
                onDirtyChange={setTavernDirty}
              />
            </Box>
          ) : null}
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          {/* 「酒馆扩展」下底部只留关闭：保存扩展是面板自己的按钮，两个保存并排会分不清 */}
          {onTavernTab ? (
            <ActionButton tone="secondary" onClick={handleRequestClose}>
              {tTavern('extension.close')}
            </ActionButton>
          ) : (
            <>
              <ActionButton tone="secondary" onClick={handleRequestClose} disabled={loading}>
                {t('dialog.cancel')}
              </ActionButton>
              <ActionButton tone="primary" onClick={handleSave} disabled={loading}>
                {loading ? <CircularProgress size={24} /> : t('dialog.save')}
              </ActionButton>
            </>
          )}
        </DialogActions>
      </Dialog>

      {/* 关闭前的未保存确认：只针对酒馆扩展，基本表单的保存仍按原流程 */}
      <Dialog
        open={closeConfirmOpen}
        onClose={() => setCloseConfirmOpen(false)}
        PaperProps={{ sx: CARD_VARIANTS.default.styles }}
      >
        <DialogTitle
          sx={{
            px: 3,
            py: 2,
            background: theme =>
              theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)',
            borderBottom: '1px solid',
            borderColor: 'divider',
          }}
        >
          {tTavern('extension.closeConfirmTitle')}
        </DialogTitle>
        <DialogContent sx={{ p: 3 }}>
          <Typography>{tTavern('extension.closeConfirm')}</Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <ActionButton tone="secondary" onClick={() => setCloseConfirmOpen(false)}>
            {tTavern('extension.cancel')}
          </ActionButton>
          <ActionButton
            tone="danger"
            onClick={() => {
              setCloseConfirmOpen(false)
              onClose()
            }}
          >
            {tTavern('extension.close')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 确认解除云端关联的对话框 */}
      <Dialog
        open={confirmDialog}
        onClose={() => setConfirmDialog(false)}
        PaperProps={{
          sx: {
            ...CARD_VARIANTS.default.styles,
          },
        }}
      >
        <DialogTitle
          sx={{
            px: 3,
            py: 2,
            background: theme =>
              theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)',
            borderBottom: '1px solid',
            borderColor: 'divider',
          }}
        >
          {t('cloud.unlinkTitle')}
        </DialogTitle>
        <DialogContent sx={{ p: 3 }}>
          <Alert severity="warning" sx={{ mb: 2 }}>
            {t('cloud.unlinkWarning')}
          </Alert>
          <Typography>{t('cloud.unlinkConfirm')}</Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <ActionButton tone="secondary" onClick={() => handleRemoteConfirm(false)}>{t('dialog.cancel')}</ActionButton>
          <ActionButton tone="primary" onClick={() => handleRemoteConfirm(true)}>
            {t('cloud.unlinkButton')}
          </ActionButton>
        </DialogActions>
      </Dialog>
    </>
  )
}

// 人设卡片组件
const PresetCard = ({
  preset: initialPreset,
  onExpand,
  expanded,
  onDelete,
  onSync,
  onEdit,
  showError,
  showSuccess,
  onRefreshList,
  isMobile,
  isSmall,
  scopeInstanceKey,
  scopeAdapterKey,
  isInstanceDefault,
  onToggleInstanceDefault,
}: {
  preset: Preset
  onExpand: () => void
  expanded: boolean
  onDelete: () => void
  onSync: (id: number) => Promise<void>
  onEdit: () => void
  showError: (message: string) => void
  showSuccess: (message: string) => void
  onRefreshList: () => Promise<void>
  isMobile?: boolean
  isSmall?: boolean
  /** 当前实例作用域；null 表示全局视图 */
  scopeInstanceKey: string | null
  /** 当前实例的适配器标识；只用于扩展面板按实例取闸状态 */
  scopeAdapterKey?: string
  /** 是否为当前实例的默认人设 */
  isInstanceDefault: boolean
  /** 设置/取消「本实例默认人设」 */
  onToggleInstanceDefault: (presetId: number, makeDefault: boolean) => void
}) => {
  const { enqueueSnackbar } = useSnackbar()
  const { t } = useTranslation('presets')
  const [preset, setPreset] = useState(initialPreset)
  const [loading, setLoading] = useState(false)
  const [detailData, setDetailData] = useState<PresetDetail | null>(null)
  // 全局人设在实例视图下编辑按钮是禁用的；它的扩展面板仍可只读查看
  const [showExtensionDialog, setShowExtensionDialog] = useState(false)
  const [showShareDialog, setShowShareDialog] = useState(false)
  const [showUnshareDialog, setShowUnshareDialog] = useState(false)
  const [showSyncToCloudDialog, setShowSyncToCloudDialog] = useState(false)
  const [isSfw, setIsSfw] = useState(true)
  const [agreeToTerms, setAgreeToTerms] = useState(false)
  const [shareLoading, setShareLoading] = useState(false)
  const [syncLoading, setSyncLoading] = useState(false)
  // 导出菜单的锚点与进行中的导出格式：卡片上有两个导出项，共用一个按钮
  const [exportAnchor, setExportAnchor] = useState<HTMLElement | null>(null)
  const [exportingFormat, setExportingFormat] = useState<'png' | 'json' | null>(null)
  const theme = useTheme()
  const isGridLayout = useMediaQuery(theme.breakpoints.up('md'))

  // 更新 preset 当 initialPreset 变化时
  useEffect(() => {
    setPreset(initialPreset)
  }, [initialPreset])

  // 加载详情数据
  useEffect(() => {
    if (expanded && !detailData) {
      const fetchData = async () => {
        try {
          setLoading(true)
          const data = await presetsApi.getDetail(preset.id, scopeInstanceKey ?? undefined)
          setDetailData(data)
        } catch (_error) {
          showError(t('card.loadDetails'))
        } finally {
          setLoading(false)
        }
      }
      fetchData()
    }
  }, [expanded, preset.id, enqueueSnackbar, detailData, showError, t, scopeInstanceKey])

  // 共享人设到云端
  const handleShareToCloud = async () => {
    try {
      setShareLoading(true)
      // 移除过程提示
      const response = await presetsApi.shareToCloud(preset.id, isSfw, scopeInstanceKey ?? undefined)
      showSuccess(t('cloud.shareSuccess'))
      setShowShareDialog(false)
      setPreset({
        ...preset,
        on_shared: true,
        remote_id: response.remote_id || String(preset.id),
      })
      await onRefreshList()
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error)
      showError(`${t('cloud.shareFailed')}: ${errorMessage}`)
      setShowShareDialog(false)
    } finally {
      setShareLoading(false)
    }
  }

  // 撤回共享
  const handleUnshare = async () => {
    try {
      setShareLoading(true)
      // 移除过程提示
      await presetsApi.unshare(preset.id, scopeInstanceKey ?? undefined)
      showSuccess(t('cloud.revokeSuccess'))
      setShowUnshareDialog(false)
      setPreset({
        ...preset,
        on_shared: false,
        remote_id: null,
      })
      await onRefreshList()
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error)
      showError(`${t('cloud.revokeFailed')}: ${errorMessage}`)
      setShowUnshareDialog(false)
    } finally {
      setShareLoading(false)
    }
  }

  // 同步到云端
  const handleSyncToCloud = async () => {
    try {
      setShareLoading(true)
      // 移除过程提示
      await presetsApi.syncToCloud(preset.id, isSfw, scopeInstanceKey ?? undefined)
      showSuccess(t('cloud.syncSuccess'))
      setShowSyncToCloudDialog(false)
      await onRefreshList()
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error)
      showError(`${t('cloud.syncFailed')}: ${errorMessage}`)
      setShowSyncToCloudDialog(false)
    } finally {
      setShareLoading(false)
    }
  }

  const tagsArray = preset.tags.split(',').filter(tag => tag.trim())
  const { t: tTavern } = useTranslation('tavern')

  /**
   * 导出一张卡
   *
   * 谁能看到这条人设谁就能导出（后端用 `get_visible_preset`），所以租户看到的全局人设
   * 也能导；作用域取页面当前实例，导出的知识书只含导出人看得到的书。
   */
  const handleExport = async (format: 'png' | 'json') => {
    setExportAnchor(null)
    setExportingFormat(format)
    try {
      await tavernApi.exportCard(preset.id, format, {
        instance_key: scopeInstanceKey ?? undefined,
      })
    } catch (error) {
      const message = error instanceof Error && error.message ? error.message : ''
      showError(`${tTavern('errors.exportFailed')}${message ? `: ${message}` : ''}`)
    } finally {
      setExportingFormat(null)
    }
  }

  // 决定显示哪个共享按钮
  const renderShareButtons = () => {
    // 未共享 且 无远程id：显示"共享此设定"按钮
    if (!preset.on_shared && !preset.remote_id) {
      return (
        <Tooltip title={t('cloud.shareToCloud')}>
          <IconActionButton size="small" tone="primary" onClick={() => setShowShareDialog(true)}>
            <CloudUploadIcon />
          </IconActionButton>
        </Tooltip>
      )
    }
    // 已共享 且 有远程id：显示"撤回共享"和"同步设定"按钮
    else if (preset.on_shared) {
      return (
        <>
          <Tooltip title={t('cloud.revokeShare')}>
            <IconActionButton size="small" onClick={() => setShowUnshareDialog(true)} sx={{ color: 'warning.main' }}>
              <CloudOffIcon />
            </IconActionButton>
          </Tooltip>
          <Tooltip title={t('cloud.syncToCloud')}>
            <IconActionButton size="small" tone="primary" onClick={() => setShowSyncToCloudDialog(true)}>
              <UploadIcon />
            </IconActionButton>
          </Tooltip>
        </>
      )
    }
    // 未共享但有远程id：显示"从云端同步"按钮（云端下载的人设）
    else if (!preset.on_shared && preset.remote_id) {
      return (
        <Tooltip title={t('cloud.syncFromCloud')}>
          <IconActionButton
            size="small"
            onClick={async () => {
              setSyncLoading(true)
              try {
                await onSync(preset.id)
              } finally {
                setSyncLoading(false)
              }
            }}
            disabled={syncLoading}
          >
            {syncLoading ? <CircularProgress size={24} /> : <SyncIcon />}
          </IconActionButton>
        </Tooltip>
      )
    }
    return null
  }

  return (
    <Card
      sx={{
        ...CARD_VARIANTS.default.styles,
        position: 'relative',
        overflow: 'visible',
        height: isGridLayout ? '100%' : 'auto',
        display: 'flex',
        flexDirection: 'column',
        transition: 'all 0.2s ease-in-out',
        '&:hover': {
          transform: 'translateY(-2px)',
          boxShadow: theme => theme.shadows[8],
        },
        ...(preset.remote_id
          ? {
              boxShadow: theme =>
                `0 0 0 1px ${theme.palette.primary.light}30, 0 2px 8px ${theme.palette.primary.light}20`,
            }
          : {}),
      }}
    >
      {preset.remote_id && (
        <Chip
          icon={<CloudDownloadIcon />}
          label={t('filter.cloud')}
          size="small"
          color="primary"
          sx={{
            position: 'absolute',
            top: -10,
            right: 16,
            zIndex: 1,
            ...CHIP_VARIANTS.base(isSmall),
          }}
        />
      )}

      {preset.on_shared && (
        <Chip
          icon={<CloudUploadIcon />}
          label={t('filter.shared')}
          size="small"
          color="success"
          sx={{
            position: 'absolute',
            top: -10,
            right: preset.remote_id ? 110 : 16,
            zIndex: 1,
            ...CHIP_VARIANTS.base(isSmall),
          }}
        />
      )}

      <CardContent sx={{ p: 2.5, pb: 1, flex: 1, display: 'flex', flexDirection: 'column' }}>
        <Box
          sx={{
            display: 'flex',
            gap: 2.5,
            flexDirection: isGridLayout ? 'row' : isMobile && isSmall ? 'column' : 'row',
            height: '100%',
          }}
        >
          {/* 左侧头像区域 */}
          <Box
            sx={{
              width: isGridLayout
                ? 100
                : isMobile && isSmall
                  ? '100%'
                  : { xs: '25%', sm: '16.66%' },
              height: isGridLayout ? 100 : 'auto',
              flexShrink: 0,
              maxWidth: isMobile && isSmall && !isGridLayout ? '160px' : 'none',
              alignSelf: isMobile && isSmall && !isGridLayout ? 'center' : 'flex-start',
              mb: isMobile && isSmall && !isGridLayout ? 2 : 0,
            }}
          >
            <PresetAvatar
              src={preset.avatar}
              name={preset.name}
              sx={{
                width: '100%',
                height: isGridLayout ? '100%' : 'auto',
                aspectRatio: '1/1',
                borderRadius: 2,
                boxShadow: theme.shadows[2],
              }}
              variant="rounded"
            />
          </Box>

          {/* 右侧内容区域 */}
          <Box
            sx={{
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              justifyContent: 'space-between',
              minWidth: 0, // 防止内容溢出
              height: '100%',
            }}
          >
            <Box
              sx={{
                flex: '1 0 auto',
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'flex-start',
              }}
            >
              {/* 标题 */}
              <Typography
                variant={isGridLayout ? 'h6' : 'h6'}
                gutterBottom
                sx={{
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  display: '-webkit-box',
                  WebkitLineClamp: 1,
                  WebkitBoxOrient: 'vertical',
                  fontWeight: 600,
                  fontSize: '1.1rem',
                  lineHeight: 1.4,
                }}
              >
                {preset.title}
              </Typography>

              {/* 来源标识：`card_spec` 非空才是酒馆卡；NA 原生的扩展行（空串）不挂这个标 */}
              {preset.card_spec ? (
                <Box sx={{ display: 'flex', gap: 0.75, flexWrap: 'wrap', mb: 1 }}>
                  <Tooltip
                    title={tTavern('badge.cardTooltip', {
                      // 库里存的是 v1 / v2 / v3，界面上按设计稿写成 V2 这种大写形态
                      spec: preset.card_spec.toUpperCase(),
                    })}
                  >
                    <Chip
                      size="small"
                      icon={<FilePresentIcon />}
                      label={tTavern('badge.card')}
                      sx={CHIP_VARIANTS.getCustomColorChip(theme.palette.info.main, isSmall)}
                    />
                  </Tooltip>
                </Box>
              ) : null}

              {/* 归属与默认标识：实例作用域下必须一眼看出这条人设是全局共享还是本实例私有 */}
              {scopeInstanceKey && (
                <Box sx={{ display: 'flex', gap: 0.75, flexWrap: 'wrap', mb: 1 }}>
                  <Chip
                    size="small"
                    variant={preset.is_global ? 'outlined' : 'filled'}
                    color={preset.is_global ? 'default' : 'secondary'}
                    label={
                      preset.is_global
                        ? t('presets.ownerGlobal', { ns: 'instance-scope' })
                        : t('presets.ownerInstance', { ns: 'instance-scope' })
                    }
                    sx={CHIP_VARIANTS.base(true)}
                  />
                  {isInstanceDefault && (
                    <Chip
                      size="small"
                      color="primary"
                      icon={<StarIcon />}
                      label={t('presets.instanceDefault', { ns: 'instance-scope' })}
                      sx={CHIP_VARIANTS.base(true)}
                    />
                  )}
                </Box>
              )}

              {/* 描述 */}
              <Typography
                variant="body2"
                color="text.secondary"
                sx={{
                  flex: '1 0 auto',
                  flexGrow: 1,
                  mb: 1.5,
                  lineHeight: 1.4,
                  maxHeight: expanded ? 'none' : isGridLayout ? '2.8em' : '4.2em',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  display: '-webkit-box',
                  WebkitLineClamp: isGridLayout ? 2 : 3,
                  WebkitBoxOrient: 'vertical',
                }}
              >
                {preset.description || t('card.noDescription')}
              </Typography>
            </Box>

            <Box sx={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
              {/* 标签栏 */}
              {(tagsArray.length > 0 || !isGridLayout) && (
                <Box
                  sx={{
                    display: 'flex',
                    gap: 0.75,
                    flexWrap: 'wrap',
                    my: 1,
                    maxHeight: isGridLayout ? '24px' : 'auto',
                    overflow: 'hidden',
                  }}
                >
                  {tagsArray.length > 0 ? (
                    tagsArray.slice(0, isGridLayout ? 3 : undefined).map((tag, index) => (
                      <Chip
                        key={index}
                        label={tag.trim()}
                        size="small"
                        sx={{
                          ...CHIP_VARIANTS.base(false),
                          bgcolor: UI_STYLES.HOVER,
                          fontWeight: 500,
                          borderRadius: 1,
                        }}
                      />
                    ))
                  ) : !isGridLayout ? (
                    <Typography variant="caption" color="text.disabled">
                      {t('filter.noTags')}
                    </Typography>
                  ) : null}
                  {isGridLayout && tagsArray.length > 3 && (
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      sx={{ alignSelf: 'center' }}
                    >
                      +{tagsArray.length - 3}
                    </Typography>
                  )}
                </Box>
              )}

              {/* 底部信息栏 */}
              {!isGridLayout && (
                <Box
                  sx={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                  }}
                >
                  <Typography variant="caption" color="text.secondary">
                    {t('form.author')}: {preset.author || t('common.unknown', { ns: 'common' })}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    最近更新: {formatLastActiveTime(new Date(preset.update_time).getTime() / 1000)}
                  </Typography>
                </Box>
              )}
            </Box>
          </Box>
        </Box>
      </CardContent>

      <CardActions
        sx={{
          justifyContent: 'space-between',
          p: 1.5,
          mt: 'auto',
          bgcolor: UI_STYLES.SELECTED,
          borderTop: '1px solid',
          borderColor: 'divider',
        }}
      >
        <ActionButton
          size="small"
          variant="text"
          startIcon={<InfoIcon />}
          onClick={onExpand}
          sx={{
            textTransform: 'none',
            fontWeight: 'normal',
            '&:hover': {
              backgroundColor: 'transparent',
              color: theme => theme.palette.primary.main,
            },
          }}
        >
          {t('card.moreActions')}
        </ActionButton>
        <Box>
          {scopeInstanceKey && (
            <Tooltip
              title={
                isInstanceDefault
                  ? t('presets.clearInstanceDefault', { ns: 'instance-scope' })
                  : t('presets.setInstanceDefault', { ns: 'instance-scope' })
              }
            >
              <IconActionButton
                size="small"
                tone="primary"
                onClick={() => onToggleInstanceDefault(preset.id, !isInstanceDefault)}
              >
                {isInstanceDefault ? <StarIcon /> : <StarBorderIcon />}
              </IconActionButton>
            </Tooltip>
          )}
          <Tooltip
            title={
              scopeInstanceKey && preset.is_global
                ? t('presets.readOnlyGlobal', { ns: 'instance-scope' })
                : t('card.edit')
            }
          >
            <span>
              <IconActionButton
                size="small"
                onClick={onEdit}
                disabled={Boolean(scopeInstanceKey && preset.is_global)}
              >
                <EditIcon />
              </IconActionButton>
            </span>
          </Tooltip>
          {/* 全局人设在实例视图下不能编辑，但扩展面板要能只读查看 */}
          {scopeInstanceKey && preset.is_global && preset.has_card ? (
            <Tooltip title={tTavern('extension.open')}>
              <IconActionButton
                size="small"
                onClick={() => setShowExtensionDialog(true)}
                aria-label={tTavern('extension.open')}
              >
                <MenuBookIcon />
              </IconActionButton>
            </Tooltip>
          ) : null}
          {preset.remote_id && preset.on_shared && (
            <Tooltip title={t('cloud.syncFromCloud')}>
              <IconActionButton
                size="small"
                onClick={async () => {
                  setSyncLoading(true)
                try {
                  await onSync(preset.id)
                } finally {
                  setSyncLoading(false)
                }
                }}
                disabled={syncLoading}
              >
                {syncLoading ? <CircularProgress size={24} /> : <SyncIcon />}
              </IconActionButton>
            </Tooltip>
          )}
          {renderShareButtons()}
          {/* 导出对「看得到这条人设」的人开放（含租户看到的全局人设），所以按钮不做权限收窄 */}
          <Tooltip title={tTavern('export.open')}>
            <span>
              <IconActionButton
                size="small"
                onClick={event => setExportAnchor(event.currentTarget)}
                disabled={exportingFormat !== null}
                aria-label={tTavern('export.open')}
              >
                {exportingFormat ? <CircularProgress size={24} /> : <DownloadIcon />}
              </IconActionButton>
            </span>
          </Tooltip>
          <Menu
            anchorEl={exportAnchor}
            open={Boolean(exportAnchor)}
            onClose={() => setExportAnchor(null)}
            anchorOrigin={{ vertical: 'top', horizontal: 'right' }}
            transformOrigin={{ vertical: 'bottom', horizontal: 'right' }}
          >
            <MenuItem onClick={() => void handleExport('png')}>
              <ListItemIcon>
                <ImageIcon fontSize="small" />
              </ListItemIcon>
              <ListItemText>{tTavern('export.png')}</ListItemText>
            </MenuItem>
            <MenuItem onClick={() => void handleExport('json')}>
              <ListItemIcon>
                <DataObjectIcon fontSize="small" />
              </ListItemIcon>
              <ListItemText>{tTavern('export.json')}</ListItemText>
            </MenuItem>
          </Menu>
          <Tooltip title={t('card.delete')}>
            <IconActionButton size="small" onClick={onDelete}>
              <DeleteIcon />
            </IconActionButton>
          </Tooltip>
        </Box>
      </CardActions>

      {/* 全局人设的酒馆扩展：实例视图下只读查看，窄屏全屏 */}
      <Dialog
        open={showExtensionDialog}
        onClose={() => setShowExtensionDialog(false)}
        maxWidth="md"
        fullWidth
        fullScreen={isSmall}
        scroll="paper"
        PaperProps={{
          sx: {
            // 全屏时也保持 NA 原生的毛玻璃卡片底色（与其它对话框一致），不另换成不透明底
            ...CARD_VARIANTS.default.styles,
            ...(isSmall ? {} : { maxHeight: '88vh' }),
          },
        }}
      >
        <DialogTitle
          sx={{
            px: 3,
            py: 2,
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            borderBottom: '1px solid',
            borderColor: 'divider',
            bgcolor: 'action.hover',
          }}
        >
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography variant="h6" component="span">
              {tTavern('extension.title')}
            </Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              {preset.title}
            </Typography>
          </Box>
          <IconActionButton
            size="small"
            onClick={() => setShowExtensionDialog(false)}
            edge="end"
            aria-label={tTavern('extension.close')}
          >
            <CloseIcon />
          </IconActionButton>
        </DialogTitle>
        <DialogContent dividers sx={{ p: 2.5 }}>
          <TavernExtensionPanel
            presetId={preset.id}
            scope={{ instance_key: scopeInstanceKey ?? undefined, adapter_key: scopeAdapterKey }}
            readOnly
          />
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <ActionButton tone="secondary" onClick={() => setShowExtensionDialog(false)}>
            {tTavern('extension.close')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 使用MUI Dialog组件实现展开内容 */}
      <Dialog
        open={expanded}
        onClose={onExpand}
        maxWidth="md"
        fullWidth
        scroll="paper"
        TransitionComponent={Fade}
        transitionDuration={{ enter: 300, exit: 200 }}
        PaperProps={{
          elevation: 8,
          sx: {
            ...CARD_VARIANTS.default.styles,
            overflow: 'hidden',
            maxWidth: isMobile ? '95%' : '800px',
            maxHeight: '80vh',
          },
        }}
      >
        <DialogTitle
          sx={{
            px: 3,
            py: 2,
            background: theme =>
              theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)',
            borderBottom: '1px solid',
            borderColor: 'divider',
            display: 'flex',
            alignItems: 'center',
            gap: 1,
          }}
        >
          <PresetAvatar
            src={preset.avatar}
            name={preset.name}
            sx={{ width: 36, height: 36, borderRadius: 1 }}
            variant="rounded"
          />
          <Typography variant="h6" component="div" sx={{ flex: 1 }}>
            人设详情：{preset.title}
          </Typography>
          <IconActionButton size="small" onClick={onExpand} edge="end">
            <CloseIcon />
          </IconActionButton>
        </DialogTitle>
        <DialogContent dividers sx={{ p: 3 }}>
          {loading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
              <CircularProgress />
            </Box>
          ) : detailData ? (
            <Grid container spacing={3}>
              <Grid item xs={12} sm={4} md={3}>
                <Box
                  sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2.5 }}
                >
                  <PresetAvatar
                    src={preset.avatar}
                    name={preset.name}
                    variant="rounded"
                    sx={{
                      width: '100%',
                      height: 'auto',
                      aspectRatio: '1/1',
                      borderRadius: 2,
                      boxShadow: theme => theme.shadows[3],
                    }}
                  />
                  <Box sx={{ width: '100%' }}>
                    <Typography variant="subtitle2" gutterBottom fontWeight={600}>
                      标签:
                    </Typography>
                    <Box sx={{ display: 'flex', gap: 0.75, flexWrap: 'wrap' }}>
                      {tagsArray.length > 0 ? (
                        tagsArray.map((tag, index) => (
                          <Chip
                            key={index}
                            label={tag.trim()}
                            size="small"
                            sx={{
                              ...CHIP_VARIANTS.base(false),
                              margin: '2px',
                              bgcolor: theme =>
                                theme.palette.mode === 'dark'
                                  ? 'rgba(255,255,255,0.08)'
                                  : 'rgba(0,0,0,0.05)',
                            }}
                          />
                        ))
                      ) : (
                        <Typography variant="caption" color="text.disabled">
                          {t('filter.noTags')}
                        </Typography>
                      )}
                    </Box>
                  </Box>
                </Box>
              </Grid>
              <Grid item xs={12} sm={8} md={9}>
                <Typography variant="h5" gutterBottom fontWeight={600}>
                  {preset.title}
                </Typography>
                <Box sx={{ mb: 3 }}>
                  <Typography variant="subtitle1" color="text.secondary" gutterBottom>
                    {t('form.author')}: {preset.author || t('common.unknown', { ns: 'common' })}
                  </Typography>
                  <Typography
                    variant="body1"
                    paragraph
                    sx={{
                      backgroundColor: theme =>
                        theme.palette.mode === 'dark'
                          ? 'rgba(255,255,255,0.03)'
                          : 'rgba(0,0,0,0.02)',
                      p: 2,
                      borderRadius: 1,
                      borderLeft: '4px solid',
                      borderColor: 'primary.main',
                      mt: 1,
                    }}
                  >
                    {preset.description || t('card.noDescription')}
                  </Typography>
                  <Box sx={{ display: 'flex', gap: 3, flexWrap: 'wrap', mb: 2, mt: 3 }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      <Typography variant="body2" color="text.secondary" fontWeight={500}>
                        {t('card.updatedAt')}:
                      </Typography>
                      <Typography variant="body2">
                        {formatLastActiveTime(new Date(preset.update_time).getTime() / 1000)}
                      </Typography>
                    </Box>
                  </Box>
                </Box>
                <Divider sx={{ mb: 2.5 }} />
                <Typography variant="h6" gutterBottom fontWeight={600}>
                  {t('form.content')}:
                </Typography>
                <Typography
                  variant="body2"
                  sx={{
                    whiteSpace: 'pre-wrap',
                    bgcolor: theme =>
                      theme.palette.mode === 'dark' ? 'rgba(0, 0, 0, 0.2)' : 'rgba(0, 0, 0, 0.03)',
                    p: 2.5,
                    borderRadius: 2,
                    border: '1px solid',
                    borderColor: 'divider',
                    maxHeight: '350px',
                    overflow: 'auto',
                    fontFamily: 'monospace',
                    fontSize: '0.9rem',
                    lineHeight: 1.6,
                  }}
                >
                  {detailData.content || t('card.noContent')}
                </Typography>
              </Grid>
            </Grid>
          ) : (
            <Typography color="text.secondary">{t('card.loadDetails')}</Typography>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <ActionButton variant="contained" onClick={onExpand} >
            {t('actions.close')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 共享对话框 */}
      <Dialog
        open={showShareDialog}
        onClose={() => setShowShareDialog(false)}
        PaperProps={{
          sx: {
            ...CARD_VARIANTS.default.styles,
          },
        }}
      >
        <DialogTitle
          sx={{
            px: 3,
            py: 2,
            background: theme =>
              theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)',
            borderBottom: '1px solid',
            borderColor: 'divider',
          }}
        >
          {t('cloud.shareConfirmTitle')}
        </DialogTitle>
        <DialogContent sx={{ p: 3 }}>
          <Typography paragraph>{t('cloud.shareConfirmMessage')}</Typography>
          <Alert severity="info" sx={{ mb: 2 }}>
            {t('cloud.shareInfo')}
          </Alert>
          {/* 有卡片数据时多一条：共享会把卡片原文与改过的槽位一起上传，这一段必须在上传前说 */}
          {preset.has_card ? (
            <Alert severity="warning" sx={{ mb: 2 }}>
              {tTavern('share.cardWarning')}
            </Alert>
          ) : null}
          <Box sx={{ display: 'flex', flexDirection: 'column' }}>
            <FormControlLabel
              control={<Checkbox checked={isSfw} onChange={e => setIsSfw(e.target.checked)} />}
              label={t('cloud.sfwConfirm')}
            />
            <FormControlLabel
              control={
                <Checkbox
                  checked={agreeToTerms}
                  onChange={e => setAgreeToTerms(e.target.checked)}
                />
              }
              label={
                <Box component="span" sx={{ display: 'flex', alignItems: 'center' }}>
                  {t('cloud.termsConfirm')}{' '}
                  <Link
                    href="https://cloud.nekro.ai/terms"
                    target="_blank"
                    underline="hover"
                    sx={{ ml: 0.5 }}
                  >
                    {t('cloud.termsLink')}
                  </Link>
                </Box>
              }
            />
          </Box>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <ActionButton tone="secondary" onClick={() => setShowShareDialog(false)} disabled={shareLoading}>
            {t('dialog.cancel')}
          </ActionButton>
          <ActionButton
            tone="primary"
            onClick={handleShareToCloud}
            disabled={shareLoading || !isSfw || !agreeToTerms}
          >
            {shareLoading ? <CircularProgress size={24} /> : t('actions.share')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 撤回共享对话框 */}
      <Dialog
        open={showUnshareDialog}
        onClose={() => setShowUnshareDialog(false)}
        PaperProps={{
          sx: {
            ...CARD_VARIANTS.default.styles,
          },
        }}
      >
        <DialogTitle
          sx={{
            px: 3,
            py: 2,
            background: theme =>
              theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)',
            borderBottom: '1px solid',
            borderColor: 'divider',
          }}
        >
          {t('cloud.revokeConfirmTitle')}
        </DialogTitle>
        <DialogContent sx={{ p: 3 }}>
          <Alert severity="warning" sx={{ mb: 2 }}>
            {t('cloud.revokeWarning')}
          </Alert>
          <Typography>{t('cloud.revokeConfirmMessage')}</Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <ActionButton tone="secondary" onClick={() => setShowUnshareDialog(false)} disabled={shareLoading}>
            {t('dialog.cancel')}
          </ActionButton>
          <ActionButton
            tone="danger"
            onClick={handleUnshare}
            disabled={shareLoading}
          >
            {shareLoading ? <CircularProgress size={24} /> : t('cloud.revokeShare')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 同步云端对话框 */}
      <Dialog
        open={showSyncToCloudDialog}
        onClose={() => setShowSyncToCloudDialog(false)}
        PaperProps={{
          sx: {
            ...CARD_VARIANTS.default.styles,
          },
        }}
      >
        <DialogTitle
          sx={{
            px: 3,
            py: 2,
            background: theme =>
              theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)',
            borderBottom: '1px solid',
            borderColor: 'divider',
          }}
        >
          {t('cloud.syncConfirmTitle')}
        </DialogTitle>
        <DialogContent sx={{ p: 3 }}>
          <Typography paragraph>{t('cloud.syncConfirmMessage')}</Typography>
          <Alert severity="info" sx={{ mb: 2 }}>
            {t('cloud.syncWarning')}
          </Alert>
          {preset.has_card ? (
            <Alert severity="warning" sx={{ mb: 2 }}>
              {tTavern('share.cardWarning')}
            </Alert>
          ) : null}
          <Box sx={{ display: 'flex', flexDirection: 'column' }}>
            <FormControlLabel
              control={<Checkbox checked={isSfw} onChange={e => setIsSfw(e.target.checked)} />}
              label={t('cloud.sfwConfirm')}
            />
            <FormControlLabel
              control={
                <Checkbox
                  checked={agreeToTerms}
                  onChange={e => setAgreeToTerms(e.target.checked)}
                />
              }
              label={
                <Box component="span" sx={{ display: 'flex', alignItems: 'center' }}>
                  {t('cloud.termsConfirm')}{' '}
                  <Link
                    href="https://cloud.nekro.ai/terms"
                    target="_blank"
                    underline="hover"
                    sx={{ ml: 0.5 }}
                  >
                    {t('cloud.termsLink')}
                  </Link>
                </Box>
              }
            />
          </Box>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <ActionButton tone="secondary" onClick={() => setShowSyncToCloudDialog(false)} disabled={shareLoading}>
            {t('dialog.cancel')}
          </ActionButton>
          <ActionButton
            tone="primary"
            onClick={handleSyncToCloud}
            disabled={shareLoading || !isSfw || !agreeToTerms}
          >
            {shareLoading ? <CircularProgress size={24} /> : '同步'}
          </ActionButton>
        </DialogActions>
      </Dialog>
    </Card>
  )
}

// 主页面组件
export default function PresetsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [presets, setPresets] = useState<Preset[]>([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const pageSize = 25
  const [search, setSearch] = useState('')
  const [selectedTags, setSelectedTags] = useState<string[]>([])
  const [totalPages, setTotalPages] = useState(1)
  const [availableTags, setAvailableTags] = useState<TagInfo[]>([])
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [editDialog, setEditDialog] = useState(false)
  const [editingPreset, setEditingPreset] = useState<PresetDetail | undefined>(undefined)
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null)
  const { enqueueSnackbar } = useSnackbar()
  const [refreshingShared, setRefreshingShared] = useState(false)
  const [importDialogOpen, setImportDialogOpen] = useState(false)
  // 作用域是人设接口的必填参数（租户身份下服务端直接 400），必须等它确定后再发请求
  const { currentInstanceKey, currentInstance, isScopeReady } = useInstanceScope()
  const { isTenantScoped } = useIdentity()
  const [instanceDefaultPresetId, setInstanceDefaultPresetId] = useState<number | null>(null)
  // 换实例默认人设的二次确认。`undefined` = 没有待确认的改动；`null` = 待确认「清除默认」
  const [pendingDefaultPresetId, setPendingDefaultPresetId] = useState<number | null | undefined>(
    undefined
  )
  const [defaultPresetImpact, setDefaultPresetImpact] = useState<PresetSwitchImpact | null>(null)
  const [defaultImpactLoading, setDefaultImpactLoading] = useState(false)
  const [defaultImpactError, setDefaultImpactError] = useState('')
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('md'))
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  // 添加宽屏检测
  const isLargeScreen = useMediaQuery(theme.breakpoints.up('lg'))
  const isExtraLargeScreen = useMediaQuery(theme.breakpoints.up('xl'))
  const { t } = useTranslation('presets')
  const { t: tTavern } = useTranslation('tavern')

  // 统一使用 notistack 的通知系统
  const showError = useCallback(
    (message: string) => {
      enqueueSnackbar(message, { variant: 'error', autoHideDuration: 5000 })
    },
    [enqueueSnackbar]
  )

  // 统一使用 notistack 的通知系统
  const showSuccess = useCallback(
    (message: string) => {
      enqueueSnackbar(message, { variant: 'success', autoHideDuration: 3000 })
    },
    [enqueueSnackbar]
  )

  // 云端回话用的那一条：比成功提示停留更久，免得一眼没看到就滑走了
  const showWarning = useCallback(
    (message: string) => {
      enqueueSnackbar(message, { variant: 'warning', autoHideDuration: 8000 })
    },
    [enqueueSnackbar]
  )

  const fetchData = useCallback(async () => {
    if (!isScopeReady) {
      return
    }
    try {
      setLoading(true)
      const data = await presetsApi.getList({
        page,
        page_size: pageSize,
        search: search || undefined,
        tags: selectedTags.length ? selectedTags.join(',') : undefined,
        instance_key: currentInstanceKey ?? undefined,
      })
      setPresets(data.items)
      setTotalPages(Math.ceil(data.total / pageSize))

      // 如果当前页没有数据但总数大于0，说明可能是删除后的页码问题，回到第一页
      if (data.items.length === 0 && data.total > 0 && page > 1) {
        setPage(1)
      }
    } catch (_error) {
      showError(t('list.fetchFailed'))
    } finally {
      setLoading(false)
    }
  }, [page, pageSize, search, selectedTags, showError, t, currentInstanceKey, isScopeReady])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  // 获取所有可用标签
  const fetchAvailableTags = useCallback(async () => {
    if (!isScopeReady) {
      return
    }
    try {
      const tags = await presetsApi.getTags(currentInstanceKey ?? undefined)
      setAvailableTags(tags)
    } catch (_error) {
      showError(t('list.fetchTagsFailed'))
    }
  }, [showError, t, currentInstanceKey, isScopeReady])

  useEffect(() => {
    fetchAvailableTags()
  }, [fetchAvailableTags])

  // 本实例默认人设（人设回退链的第二层）
  const fetchInstanceDefaultPreset = useCallback(async () => {
    if (!currentInstanceKey) {
      setInstanceDefaultPresetId(null)
      return
    }
    try {
      const data = await presetsApi.getInstanceDefault(currentInstanceKey)
      setInstanceDefaultPresetId(data.preset_id)
    } catch (_error) {
      // 接口未就绪时不打断人设管理，仅不展示默认标识
      setInstanceDefaultPresetId(null)
    }
  }, [currentInstanceKey])

  useEffect(() => {
    fetchInstanceDefaultPreset()
  }, [fetchInstanceDefaultPreset])

  // 真正落库的那一步。**只在确认过之后调用**（或影响面为"没变"时直接调用）
  const applyInstanceDefault = useCallback(
    async (presetId: number | null, confirmed: boolean) => {
      if (!currentInstanceKey) return
      try {
        await presetsApi.setInstanceDefault(currentInstanceKey, presetId, confirmed)
        setInstanceDefaultPresetId(presetId)
        showSuccess(
          presetId !== null
            ? t('presets.setDefaultSuccess', { ns: 'instance-scope' })
            : t('presets.clearDefaultSuccess', { ns: 'instance-scope' })
        )
        setPendingDefaultPresetId(undefined)
      } catch (_error) {
        showError(t('presets.setDefaultFailed', { ns: 'instance-scope' }))
      }
    },
    [currentInstanceKey, showError, showSuccess, t]
  )

  /**
   * 改实例默认人设：先问后端"这次会影响谁"，真的换了人才弹确认框
   *
   * 这是人设回退链的**第二层**（频道 → 实例默认 → 系统默认 → 内置）：该实例下所有
   * 没绑人设的频道说话的都是它。改掉它 = 那些频道同时切到另一份好感与记忆上，
   * 影响面比逐个频道改大得多，却长期只是一个下拉框。
   */
  const handleToggleInstanceDefault = useCallback(
    async (presetId: number, makeDefault: boolean) => {
      if (!currentInstanceKey) return
      const target = makeDefault ? presetId : null
      setPendingDefaultPresetId(target)
      setDefaultPresetImpact(null)
      setDefaultImpactError('')
      setDefaultImpactLoading(true)
      try {
        const impact = await presetsApi.getInstanceDefaultImpact(currentInstanceKey, target)
        setDefaultImpactLoading(false)
        if (impact.is_noop) {
          // 生效人设其实没变，拦它只会让人学会一路点确认
          setPendingDefaultPresetId(undefined)
          await applyInstanceDefault(target, false)
          return
        }
        setDefaultPresetImpact(impact)
      } catch (error) {
        setDefaultImpactLoading(false)
        setDefaultImpactError(error instanceof Error ? error.message : String(error))
      }
    },
    [applyInstanceDefault, currentInstanceKey]
  )

  const clearEditRemoteId = useCallback(() => {
    const nextSearchParams = new URLSearchParams(searchParams)
    nextSearchParams.delete('editRemoteId')
    setSearchParams(nextSearchParams, { replace: true })
  }, [searchParams, setSearchParams])

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    setPage(1)
  }

  const handleTagFilter = (tag: string) => {
    setSelectedTags(prev => {
      const exists = prev.includes(tag)
      const next = exists ? prev.filter(t => t !== tag) : [...prev, tag]
      return next
    })
    setPage(1)
  }

  const handlePageChange = (_event: React.ChangeEvent<unknown>, newPage: number) => {
    setPage(newPage)
    // 页面变化时滚动到顶部
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const handleExpandClick = (id: number) => {
    setExpandedId(expandedId === id ? null : id)
  }

  const handleEditClick = async (preset: Preset) => {
    try {
      showSuccess(t('list.loadingDetails'))
      // 加载详细数据
      const detailData = await presetsApi.getDetail(preset.id, currentInstanceKey ?? undefined)
      setEditingPreset(detailData)
      setEditDialog(true)
    } catch (_error) {
      showError(t('card.loadDetails'))
    }
  }

  const handleEditRemotePreset = useCallback(
    async (remoteId: string) => {
      try {
        showSuccess(t('list.loadingDetails'))
        let detailData: PresetDetail

        try {
          detailData = await presetsApi.getDetailByRemoteId(remoteId, currentInstanceKey ?? undefined)
        } catch (error) {
          if (!(error instanceof ApiError) || error.type !== 'NotFoundError') {
            throw error
          }

          const downloadResult = await presetsMarketApi.downloadPreset(remoteId)
          if (downloadResult.note) showWarning(downloadResult.note)
          detailData = await presetsApi.getDetailByRemoteId(remoteId, currentInstanceKey ?? undefined)
        }

        setEditingPreset(detailData)
        setEditDialog(true)

        void fetchData()
        void fetchAvailableTags()
      } catch (_error) {
        showError(t('card.loadDetails'))
      } finally {
        clearEditRemoteId()
      }
    },
    [clearEditRemoteId, fetchAvailableTags, fetchData, showError, showSuccess, showWarning, t, currentInstanceKey]
  )

  useEffect(() => {
    const remoteId = searchParams.get('editRemoteId')
    if (!remoteId) return

    void handleEditRemotePreset(remoteId)
  }, [handleEditRemotePreset, searchParams])

  const handleSave = async (data: PresetFormData) => {
    try {
      // 实例作用域下新建的人设归本实例私有，更新也带上作用域用于权限校验
      const scopedData = { ...data, instance_key: currentInstanceKey ?? undefined }
      if (editingPreset) {
        showSuccess(t('list.updating'))
        await presetsApi.update(editingPreset.id, scopedData)
        showSuccess(t('messages.updateSuccess', { ns: 'common' }))
      } else {
        showSuccess(t('list.creating'))
        await presetsApi.create(scopedData)
        showSuccess(t('messages.createSuccess', { ns: 'common' }))
      }
      fetchData()
      fetchAvailableTags() // 刷新标签列表
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error)
      showError(`${t('messages.saveFailed', { ns: 'common' })}: ${errorMessage}`)
      throw error // 传递错误以便上层组件处理
    }
  }

  const handleDeleteClick = (id: number) => {
    setConfirmDelete(id)
  }

  const handleDeleteConfirm = async () => {
    if (confirmDelete === null) return

    try {
      showSuccess(t('list.deleting'))
      await presetsApi.delete(confirmDelete, currentInstanceKey ?? undefined)
      showSuccess(t('messages.deleteSuccess', { ns: 'common' }))
      fetchData()
      fetchAvailableTags() // 刷新标签列表
      if (expandedId === confirmDelete) {
        setExpandedId(null)
      }
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error)
      showError(`${t('messages.deleteFailed', { ns: 'common' })}: ${errorMessage}`)
    } finally {
      setConfirmDelete(null)
    }
  }

  const handleSyncClick = async (id: number): Promise<void> => {
    try {
      const response = await presetsApi.sync(id, currentInstanceKey ?? undefined)
      showSuccess(t('cloud.syncSuccess'))
      // 云端在同步时可能会改写内容并回一句说明（例如卡片字段被压平），单独用 warning 弹出来：
      // 混在成功提示里会被当成一句废话划过去
      if (response.note) {
        showWarning(response.note)
      }
      await fetchData()
      if (expandedId === id) {
        setExpandedId(null)
        setTimeout(() => setExpandedId(id), 100)
      }
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error)
      showError(`${t('cloud.syncFailed')}: ${errorMessage}`)
      throw error
    }
  }

  // 添加刷新共享状态的函数
  const handleRefreshSharedStatus = async () => {
    try {
      setRefreshingShared(true)
      showSuccess(t('cloud.refreshingStatus'))
      const response = await presetsApi.refreshSharedStatus()
      showSuccess(t('cloud.refreshStatusSuccess'))
      showSuccess(
        t('cloud.refreshStatusDetails', {
          updated: response.updated_count,
          total: response.total_cloud_presets,
        })
      )
      await fetchData()
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error)
      showError(`${t('cloud.refreshFailed')}: ${errorMessage}`)
    } finally {
      setRefreshingShared(false)
    }
  }

  // 修改PresetCard组件，传入showError函数
  const renderPresetCard = (preset: Preset) => (
    <PresetCard
      key={preset.id}
      preset={preset}
      expanded={expandedId === preset.id}
      onExpand={() => handleExpandClick(preset.id)}
      onDelete={() => handleDeleteClick(preset.id)}
      onSync={handleSyncClick}
      onEdit={() => handleEditClick(preset)}
      showError={showError}
      showSuccess={showSuccess}
      onRefreshList={fetchData}
      isMobile={isMobile}
      isSmall={isSmall}
      scopeInstanceKey={currentInstanceKey}
      scopeAdapterKey={currentInstance?.adapter_key}
      isInstanceDefault={instanceDefaultPresetId === preset.id}
      onToggleInstanceDefault={handleToggleInstanceDefault}
    />
  )

  // 根据屏幕尺寸决定网格布局的列数
  const getGridColumns = () => {
    if (isExtraLargeScreen) return 3 // 超大屏幕显示3列
    if (isLargeScreen) return 2 // 大屏幕显示2列
    return 1 // 默认显示1列
  }

  return (
    <Box sx={{ p: 3, height: '100%', overflow: 'auto' }}>
      {currentInstanceKey && (
        <Box className="mb-4">
          <InstanceScopeBanner description={t('banner.presets', { ns: 'instance-scope' })} />
        </Box>
      )}
      <Box className="flex justify-between items-center mb-4 flex-wrap gap-2">
        <Box component="form" onSubmit={handleSearch} className="flex" autoComplete="off">
          <TextField
            size="small"
            placeholder={t('search.placeholder')}
            value={search}
            onChange={e => setSearch(e.target.value)}
            autoComplete="off"
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon fontSize="small" />
              </InputAdornment>
            ),
          }}
        />
      </Box>
        <Box className="flex gap-2">
          <DomainSettingsButton domain="presets" />
          {/* 「刷新共享状态」按的是部署级社区账号的发布清单（`POST /presets/refresh-shared-status`，
              平台方专属），租户点它必然 403，所以整颗按钮对租户身份不出现。 */}
          {!isTenantScoped && (
            <ActionButton
              variant="outlined"
              startIcon={<CloudSyncIcon />}
              onClick={handleRefreshSharedStatus}
              disabled={refreshingShared}
              size={isSmall ? 'small' : 'medium'}
            >
              {refreshingShared ? (
                <CircularProgress size={isSmall ? 16 : 24} />
              ) : isMobile ? (
                t('actions.refreshShort')
              ) : (
                t('actions.refresh')
              )}
            </ActionButton>
          )}
          {/* 「导入酒馆卡」与「新建」并列：两者都是「往列表里加一条人设」的入口。
              窄屏只留图标（无障碍标签照给），否则一行放不下会横向溢出。 */}
          <ActionButton
            variant="outlined"
            startIcon={<FilePresentIcon />}
            onClick={() => setImportDialogOpen(true)}
            size={isSmall ? 'small' : 'medium'}
            aria-label={tTavern('import.open')}
            sx={
              isSmall
                ? {
                    minWidth: 0,
                    px: 1,
                    // 只剩图标时 MUI 仍给 startIcon 留右外边距，去掉它按钮才不会偏
                    '& .MuiButton-startIcon': { mr: 0 },
                  }
                : undefined
            }
          >
            {isSmall ? null : tTavern('import.open')}
          </ActionButton>
          <ActionButton
            variant="contained"
            startIcon={<AddIcon />}
            onClick={() => {
              setEditingPreset(undefined)
              setEditDialog(true)
            }}
            size={isSmall ? 'small' : 'medium'}
          >
            {t('actions.create')}
          </ActionButton>
        </Box>
      </Box>

      {/* 标签过滤栏 */}
      {availableTags.length > 0 && (
        <Box
          sx={{
            mb: 3,
            ...CARD_VARIANTS.default.styles,
            p: 2,
            borderRadius: 2,
          }}
        >
          <Box
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1,
              mb: 2,
              flexWrap: 'wrap',
            }}
          >
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              <FilterListIcon fontSize="small" color="primary" />
              <Typography variant="body2" color="text.secondary" fontWeight={500}>
                {t('filter.byTag', { count: availableTags.length })}
              </Typography>
            </Box>
            {selectedTags.length > 0 && (
              <ActionButton
                size="small"
                startIcon={<ClearIcon />}
                onClick={() => setSelectedTags([])}
                sx={{
                  fontSize: '0.75rem',
                  minHeight: 24,
                  px: 1,
                  color: 'text.secondary',
                  '&:hover': {
                    color: 'primary.main',
                  },
                }}
              >
                {t('filter.clearFilters')}
              </ActionButton>
            )}
          </Box>
          <Box
            sx={{
              display: 'flex',
              gap: 1,
              flexWrap: 'wrap',
              maxHeight: isSmall ? '140px' : '100px',
              overflow: 'auto',
              ...SCROLLBAR_VARIANTS.thin.styles,
            }}
          >
            {availableTags.map(({ tag, count }) => (
              <Chip
                key={tag}
                label={`${tag} (${count})`}
                onClick={() => handleTagFilter(tag)}
                color={selectedTags.includes(tag) ? 'primary' : 'default'}
                variant={selectedTags.includes(tag) ? 'filled' : 'outlined'}
                size={isSmall ? 'small' : 'medium'}
                sx={{
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                  fontWeight: selectedTags.includes(tag) ? 600 : 400,
                  '&:hover': {
                    transform: 'translateY(-1px)',
                    boxShadow: theme => theme.shadows[2],
                  },
                  // 使用主题系统的背景样式
                  ...(!selectedTags.includes(tag) && {
                    background: UI_STYLES.HOVER,
                    '&:hover': {
                      background: UI_STYLES.SELECTED,
                      transform: 'translateY(-1px)',
                      boxShadow: theme => theme.shadows[2],
                    },
                  }),
                }}
              />
            ))}
          </Box>
        </Box>
      )}

      {loading && presets.length === 0 ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
          <CircularProgress />
        </Box>
      ) : presets.length > 0 ? (
        <>
          <Grid container spacing={isSmall ? 1.5 : 2}>
            {presets.map(preset => (
              <Grid
                item
                xs={12}
                md={getGridColumns() === 2 ? 6 : 12}
                lg={getGridColumns() === 3 ? 4 : 6}
                key={preset.id}
              >
                {renderPresetCard(preset)}
              </Grid>
            ))}
          </Grid>

          {/* 分页组件和统计信息 */}
          <Box
            sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, mt: 3 }}
          >
            {/* 总数统计 */}
            <PaginationStyled
              totalPages={totalPages}
              currentPage={page}
              onPageChange={handlePageChange}
              loading={loading}
              showPageInfo={!isSmall}
              siblingCount={isSmall ? 0 : 1}
              boundaryCount={isSmall ? 1 : 1}
            />
          </Box>
        </>
      ) : (
        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            py: isSmall ? 4 : 8,
          }}
        >
          <Typography color="text.secondary" gutterBottom>
            {t('list.noResults')}
          </Typography>
          <ActionButton
            variant="outlined"
            startIcon={<AddIcon />}
            onClick={() => {
              setEditingPreset(undefined)
              setEditDialog(true)
            }}
            size={isSmall ? 'small' : 'medium'}
          >
            {t('actions.create')}
          </ActionButton>
        </Box>
      )}

      {/* 编辑对话框 */}
      <PresetEditDialog
        open={editDialog}
        onClose={() => setEditDialog(false)}
        preset={editingPreset}
        onSave={handleSave}
        scope={{
          instance_key: currentInstanceKey ?? undefined,
          adapter_key: currentInstance?.adapter_key,
        }}
        isNew={!editingPreset}
      />

      {/* 删除确认对话框 */}
      <Dialog
        open={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        PaperProps={{
          sx: {
            ...CARD_VARIANTS.default.styles,
          },
        }}
      >
        <DialogTitle
          sx={{
            px: 3,
            py: 2,
            background: theme =>
              theme.palette.mode === 'dark' ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.02)',
            borderBottom: '1px solid',
            borderColor: 'divider',
          }}
        >
          {t('dialog.confirmDeleteTitle')}
        </DialogTitle>
        <DialogContent sx={{ p: 3 }}>
          <Typography>{t('dialog.confirmDeleteMessage')}</Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <ActionButton tone="secondary" onClick={() => setConfirmDelete(null)}>{t('dialog.cancel')}</ActionButton>
          <ActionButton tone="danger" onClick={handleDeleteConfirm}>
            {t('dialog.confirmDeleteTitle')}
          </ActionButton>
        </DialogActions>
      </Dialog>

      {/* 换实例默认人设的二次确认。与频道那一处共用同一个组件与同一份措辞 */}
      <PresetSwitchConfirmDialog
        open={pendingDefaultPresetId !== undefined}
        impact={defaultPresetImpact}
        loading={defaultImpactLoading}
        error={defaultImpactError}
        onCancel={() => setPendingDefaultPresetId(undefined)}
        onConfirm={() => {
          if (pendingDefaultPresetId !== undefined) {
            void applyInstanceDefault(pendingDefaultPresetId, true)
          }
        }}
      />

      {/* 导入酒馆卡。作用域跟着页面当前实例走：`adapter_key` 只用来让后端定位实例、取两个闸
          的生效值，归属仍是 `instance_key`；全局视图下两者都为空，导入成人人可见的全局人设。 */}
      <CardImportDialog
        open={importDialogOpen}
        onClose={() => setImportDialogOpen(false)}
        scope={{
          instance_key: currentInstanceKey ?? undefined,
          adapter_key: currentInstance?.adapter_key ?? undefined,
        }}
        scopeLabel={currentInstance?.display_name ?? ''}
        onImported={presetName => {
          void fetchData()
          void fetchAvailableTags()
          showSuccess(
            presetName
              ? tTavern('import.importedName', { name: presetName })
              : tTavern('import.imported')
          )
        }}
      />
    </Box>
  )
}
