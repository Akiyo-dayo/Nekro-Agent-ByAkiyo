/**
 * 模型组新建 / 编辑 / 复制对话框
 *
 * 系统配置下的「模型管理 → 基础模型组」与总后台的「模型组」共用这一个对话框，
 * 两处的表单字段、校验、连通性测试完全一致，不存在"另一处又实现了一遍"的分叉。
 *
 * ## 凭据隔离
 *
 * 平台方维护的模型组对租户只下发掩码串（见 `services/api/tenant.ts` 的契约）。本组件因此坚持三件事：
 *
 * - 凭据不可见时输入框禁用并显式标注「这是掩码不是密钥」，用户不可能把它误当真值改掉；
 * - 提交时未改动的凭据原样回传，后端据此判定「保持不变」，绝不提交空串或掩码的变体；
 * - 「拉取模型列表」与「连接测试」这两个把表单内容原样交给服务端的动作，在凭据是掩码时一律禁用。
 *   后端也不会替调用方把掩码回落成真实值：那正是一条完整的凭据外带通道（提交掩码 + 自己的地址，
 *   服务端一回落就把平台方的 Key 发到调用方指定的服务器上）。要测这类模型组请用列表行上的测速按钮，
 *   那条路径不接受任何调用方参数。
 *
 * 复制模式下凭据不可见时会把地址与密钥清空：复制出来的是一个新组，没有"保持不变"可言，
 * 把掩码串照抄过去就会真的把 `********` 存成密钥。
 */

import { useEffect, useState } from 'react'
import {
  Alert,
  Autocomplete,
  Box,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  InputAdornment,
  MenuItem,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import {
  Brush as BrushIcon,
  Chat as ChatIcon,
  Code as CodeIcon,
  EmojiObjects as EmojiObjectsIcon,
  Lock as LockIcon,
  NetworkCheck as NetworkCheckIcon,
  Visibility as VisibilityIcon,
  VisibilityOff as VisibilityOffIcon,
} from '@mui/icons-material'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import type { ModelGroupConfig } from '../common/ConfigTable'
import { unifiedConfigApi, ModelGroupTestItem } from '../../services/api/unified-config'
import {
  isCredentialMask,
  isMaskLikeCredential,
  resolveCredentialForSubmit,
  type ModelGroupInfo,
  type ModelGroupUpsertRequest,
  type ModelGroupVisibility,
} from '../../services/api/tenant'
import { getLocalizedText, OPENAI_COMPAT_PROVIDERS } from '../../config/model-presets'
import { CHIP_VARIANTS } from '../../theme/variants'
import { useNotification } from '../../hooks/useNotification'
import ActionButton from '../common/ActionButton'
import IconActionButton from '../common/IconActionButton'
import { describeTestFailure } from './testOutcome'

export type ModelGroupFormMode = 'create' | 'edit' | 'copy'

const EMPTY_CONFIG: ModelGroupConfig = {
  CHAT_MODEL: '',
  CHAT_PROXY: '',
  BASE_URL: '',
  API_KEY: '',
  MODEL_TYPE: 'chat',
  TEMPERATURE: null,
  TOP_P: null,
  TOP_K: null,
  PRESENCE_PENALTY: null,
  FREQUENCY_PENALTY: null,
  EXTRA_BODY: null,
  // 与后端 / DB 默认（均为 false）对齐。这里曾经默认 true，而模型能不能看图前端根本不知道：
  // 新建模型组时带着这个默认值落盘，就会让每一轮对话都先发几 MB 的图、再吃一个
  // 「当前模型不支持该能力：vision」400。未声明一律当作不支持，代价是少一路信息；
  // 反过来则是整轮对话（连同纯文字消息）一起失败。
  ENABLE_VISION: false,
  ENABLE_COT: false,
}

/**
 * 新建模型组时「对租户的开放程度」的默认值。
 *
 * ⚠️ **必须与后端默认一致**（`nekro_agent/models/db_model_group.py` 的 `visibility` 字段默认
 * 与 `services/model_group_service.create_group()` 的形参默认，两处都是 `private`）。
 * 表单默认与后端默认相反踩过一次：`ENABLE_VISION` 前端默认 true、后端默认 false，
 * 运维一建组就落了个错的值（见上面 `EMPTY_CONFIG` 里那条注释）。
 * 这里的方向更贵：默认 shared = 新建的平台组开箱即对所有租户开放，等于让新租户直接烧平台方的账单。
 * `tests/test_model_group_default_visibility.py` 直接读本文件，把两侧默认钉在一起。
 */
const DEFAULT_VISIBILITY: ModelGroupVisibility = 'private'

/**
 * 组名规则：中日韩文字、半角字母数字，文字之间可用空格与常见标点，首字符须是文字或左括号，
 * 不许连续空格，最长 64 字符。
 *
 * ⚠️ **与后端 `services/model_group_service._NAME_RE` 是同一条规则**，以后端为准；这里只是让错误
 * 在输入时就出现，而不是提交后弹一条 toast。中日韩文字用显式码段而不是 `\p{L}`：后者把西里尔同形字、
 * 全角字母和不可见的韩文填充符都算作字母；显式码段也不受浏览器与后端 Unicode 版本差异影响。
 * 防止平台方选错组的主手段是配置下拉框里标出租户自建的组（`ConfigTable.tsx`），字符集只挡最廉价的几类。
 * `tests/test_model_group_name_and_batch_delete.py` 读本文件逐码点比对两侧。
 */
const GROUP_NAME_LETTERS =
  'A-Za-z0-9' +
  '\\u3400-\\u4DBF\\u4E00-\\u9FFF' + // 汉字：扩展 A、基本区
  '\\u{20000}-\\u{2A6DF}\\u{2A700}-\\u{2EE5F}\\u{30000}-\\u{323AF}' + // 汉字：扩展 B–I
  '\\u3041-\\u3096\\u309D-\\u309F' + // 平假名
  '\\u30A1-\\u30FA\\u30FC-\\u30FF' + // 片假名（含长音符 ー，不含中点 ・）
  '\\uAC00-\\uD7A3' // 韩文音节（不含填充符与单独字母）
/** 可出现在文字之间的标点（含空格）：_ . - ( ) + : @ · （ ） 【 】 、 ： */
const GROUP_NAME_PUNCT = '_.\\-()+:@ \\u00B7\\uFF08\\uFF09\\u3010\\u3011\\u3001\\uFF1A'
/** 可以打头的标点只有左括号：( （ 【 */
const GROUP_NAME_OPENERS = '(\\uFF08\\u3010'
const GROUP_NAME_PATTERN = new RegExp(
  `^(?!.*  )[${GROUP_NAME_LETTERS}${GROUP_NAME_OPENERS}][${GROUP_NAME_LETTERS}${GROUP_NAME_PUNCT}]{0,63}$`,
  'u'
)

const MODEL_TYPE_ICONS: Record<string, React.ReactElement> = {
  Chat: <ChatIcon fontSize="small" />,
  Code: <CodeIcon fontSize="small" />,
  Brush: <BrushIcon fontSize="small" />,
  EmojiObjects: <EmojiObjectsIcon fontSize="small" />,
}

export interface ModelGroupFormDialogProps {
  open: boolean
  mode: ModelGroupFormMode
  /** 编辑 / 复制的源模型组；新建为 null */
  source: ModelGroupInfo | null
  /** 已存在的组名，用于重名校验 */
  existingNames: string[]
  /** 当前身份是否为平台方：决定能否设置平台组对租户的开放程度 */
  isPlatformOwner: boolean
  onClose: () => void
  onSubmit: (name: string, payload: ModelGroupUpsertRequest, isCreate: boolean) => Promise<void>
}

export default function ModelGroupFormDialog({
  open,
  mode,
  source,
  existingNames,
  isPlatformOwner,
  onClose,
  onSubmit,
}: ModelGroupFormDialogProps) {
  const theme = useTheme()
  const isMobile = useMediaQuery(theme.breakpoints.down('md'))
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  const notification = useNotification()
  const { t, i18n } = useTranslation('settings')

  const isCreate = mode !== 'edit'
  // 复制出来的是一个新组，凭据不可见时不能把掩码串照抄过去
  const inheritsCredentials = mode !== 'copy' || Boolean(source?.credential_visible)
  const initialConfig: ModelGroupConfig = source
    ? { ...source.config, ...(inheritsCredentials ? {} : { BASE_URL: '', API_KEY: '', CHAT_PROXY: '' }) }
    : EMPTY_CONFIG

  const [groupName, setGroupName] = useState('')
  const [config, setConfig] = useState<ModelGroupConfig>(EMPTY_CONFIG)
  const [visibility, setVisibility] = useState<ModelGroupVisibility>(DEFAULT_VISIBILITY)
  const [groupNameError, setGroupNameError] = useState('')
  const [error, setError] = useState('')
  const [showApiKey, setShowApiKey] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [modelOptions, setModelOptions] = useState<string[]>([])
  const [fetchingModels, setFetchingModels] = useState(false)
  const [testingModel, setTestingModel] = useState(false)
  const [testResult, setTestResult] = useState<ModelGroupTestItem | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // 统一输入尺寸与高度，确保按钮与输入框一致
  const inputSize: 'small' | 'medium' = isSmall ? 'small' : 'medium'
  const inputHeight = isSmall ? 40 : 56

  useEffect(() => {
    if (!open) return
    setGroupName(mode === 'edit' ? (source?.name ?? '') : '')
    setConfig({
      ...initialConfig,
      MODEL_TYPE: initialConfig.MODEL_TYPE || 'chat',
      ENABLE_VISION: initialConfig.ENABLE_VISION ?? false,
      ENABLE_COT: initialConfig.ENABLE_COT ?? false,
    })
    // 编辑时回显该组当下的可见性（存量 shared 的组不能被表单默认值偷偷改掉）；
    // 新建 / 复制一律回到 `DEFAULT_VISIBILITY`
    setVisibility(mode === 'edit' ? (source?.visibility ?? DEFAULT_VISIBILITY) : DEFAULT_VISIBILITY)
    setGroupNameError('')
    setError('')
    setShowApiKey(false)
    setShowAdvanced(false)
    setModelOptions([])
    setTestResult(null)
    // 打开对话框时按当前源重置整张表单；`initialConfig` 由 source/mode 推导，不额外进依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode, source])

  const { data: modelTypes = [] } = useQuery({
    queryKey: ['model-types'],
    queryFn: () => unifiedConfigApi.getModelTypes(),
  })

  const getModelTypeIcon = (type: string | undefined) => {
    if (!type) return <ChatIcon fontSize="small" />
    const iconName = modelTypes.find(item => item.value === type)?.icon || 'EmojiObjects'
    return MODEL_TYPE_ICONS[iconName] || <EmojiObjectsIcon fontSize="small" />
  }

  /** 当前身份能否看到这个模型组的真实凭据；新建的组凭据本就属于自己 */
  const credentialVisible = mode === 'edit' ? (source?.credential_visible ?? true) : true
  /** 表单里的凭据仍是掩码串：此时任何"把表单原样交给服务端"的动作都拿不到可用凭据 */
  const credentialsMasked =
    isCredentialMask(config.API_KEY) ||
    isCredentialMask(config.BASE_URL) ||
    isCredentialMask(config.CHAT_PROXY || '')

  const canFetchModels =
    Boolean(config.BASE_URL && config.API_KEY && !fetchingModels) && !credentialsMasked
  const fetchTooltipTitle = canFetchModels
    ? ''
    : credentialsMasked
      ? t('modelGroup.tooltips.testPlatformOwned')
      : t('modelGroup.helpers.fetchPrecondition')

  const canTestInline =
    Boolean(config.CHAT_MODEL && config.BASE_URL && config.API_KEY) && !credentialsMasked

  const providerOptions = OPENAI_COMPAT_PROVIDERS.map(p => p.url)
  const providerMetaByUrl = new Map(
    OPENAI_COMPAT_PROVIDERS.map(p => [p.url, getLocalizedText(p.label, i18n.resolvedLanguage)])
  )

  // 拉取模型列表一律走后端代理：浏览器直连第三方 /models 会把 API Key 从用户浏览器
  // 直接发往上游，多租户下等于把平台方的凭据交到客户手上，且绕过后端的出站地址校验。
  const fetchAvailableModels = async () => {
    if (!canFetchModels) return
    setFetchingModels(true)
    try {
      const models = await unifiedConfigApi.fetchModels({
        base_url: config.BASE_URL,
        api_key: config.API_KEY,
        proxy_url: config.CHAT_PROXY || undefined,
      })
      setModelOptions(models)
      if (models.length > 0) {
        notification.success(t('modelGroup.validation.fetchSuccess', { count: models.length }))
      } else {
        notification.info(t('modelGroup.validation.fetchEmpty'))
      }
    } catch (err) {
      const message =
        err instanceof Error ? err.message : t('common.unknownError', { ns: 'common' })
      notification.error(t('modelGroup.validation.fetchError', { error: message }))
    } finally {
      setFetchingModels(false)
    }
  }

  const handleTestModel = async () => {
    if (!canTestInline) return
    setTestingModel(true)
    setTestResult(null)
    try {
      const item = await unifiedConfigApi.testModelGroupInline({
        group_name: groupName || 'inline-test',
        chat_model: config.CHAT_MODEL,
        base_url: config.BASE_URL,
        api_key: config.API_KEY,
        model_type: config.MODEL_TYPE || 'chat',
        chat_proxy: config.CHAT_PROXY || undefined,
        temperature: config.TEMPERATURE ?? undefined,
        top_p: config.TOP_P ?? undefined,
        top_k: config.TOP_K ?? undefined,
        presence_penalty: config.PRESENCE_PENALTY ?? undefined,
        frequency_penalty: config.FREQUENCY_PENALTY ?? undefined,
        extra_body: config.EXTRA_BODY ?? undefined,
      })
      setTestResult(item)
      if (item.success) {
        notification.success(t('modelGroup.notifications.testPassed'))
      } else {
        notification.warning(t('modelGroup.notifications.testFailed'))
      }
    } catch (err) {
      notification.error(
        err instanceof Error ? err.message : t('modelGroup.notifications.testError')
      )
    } finally {
      setTestingModel(false)
    }
  }

  /**
   * 能否设置「对租户的开放程度」
   *
   * 只有平台方维护的组才有这个概念：租户自建组承载的是租户自己的凭据，后端强制私有。
   * 平台方新建的组一定是平台组，所以创建态只看身份。
   */
  const canSetVisibility =
    isPlatformOwner && (mode === 'edit' ? source?.ownership === 'platform' : true)

  const isValidGroupName = (name: string) => GROUP_NAME_PATTERN.test(name.trim())

  const handleGroupNameChange = (name: string) => {
    setGroupName(name)
    if (!name) {
      setGroupNameError('')
      return
    }
    if (!isValidGroupName(name)) {
      setGroupNameError(t('modelGroup.validation.nameInvalid'))
      return
    }
    if (isCreate && existingNames.includes(name.trim())) {
      setGroupNameError(t('modelGroup.validation.nameExists', { name }))
      return
    }
    setGroupNameError('')
  }

  const handleSubmit = async () => {
    const trimmedName = groupName.trim()
    if (!trimmedName) {
      setGroupNameError(t('modelGroup.validation.nameRequired'))
      return
    }
    // 只校验新建 / 复制：编辑时组名不可改，而存量组名可能来自上游更宽松的规则
    // （配置文件导入不走组名校验，`DeepSeek V3`、`qwen2.5:7b` 都可能存在），按新规则挡住
    // 就再也保存不了——连轮换 API Key 都做不到。后端更新接口本来也不校验组名
    if (isCreate && !isValidGroupName(trimmedName)) {
      setGroupNameError(t('modelGroup.validation.nameInvalid'))
      return
    }
    if (isCreate && existingNames.includes(trimmedName)) {
      setGroupNameError(t('modelGroup.validation.nameExists', { name: trimmedName }))
      return
    }

    const baseUrl = (config.BASE_URL || '').trim()
    const apiKey = (config.API_KEY || '').trim()
    const chatProxy = (config.CHAT_PROXY || '').trim()

    // 新建没有"保持不变"可言：掩码串一旦提交就会被当成真实凭据存下来
    if (isCreate && [baseUrl, apiKey, chatProxy].some(isMaskLikeCredential)) {
      setError(t('modelGroup.validation.credentialMaskLike'))
      return
    }
    if (!baseUrl) {
      setError(t('modelGroup.validation.baseUrlRequired'))
      return
    }

    const payload: ModelGroupUpsertRequest = {
      base_url: resolveCredentialForSubmit(
        baseUrl,
        initialConfig.BASE_URL,
        baseUrl !== initialConfig.BASE_URL
      ),
      api_key: resolveCredentialForSubmit(
        apiKey,
        initialConfig.API_KEY,
        apiKey !== initialConfig.API_KEY
      ),
      chat_proxy: resolveCredentialForSubmit(
        chatProxy,
        initialConfig.CHAT_PROXY || '',
        chatProxy !== (initialConfig.CHAT_PROXY || '')
      ),
      chat_model: (config.CHAT_MODEL || '').trim(),
      model_type: (config.MODEL_TYPE || 'chat').trim(),
      enable_vision: config.ENABLE_VISION,
      enable_cot: config.ENABLE_COT,
      temperature: config.TEMPERATURE ?? null,
      top_p: config.TOP_P ?? null,
      top_k: config.TOP_K ?? null,
      presence_penalty: config.PRESENCE_PENALTY ?? null,
      frequency_penalty: config.FREQUENCY_PENALTY ?? null,
      extra_body: config.EXTRA_BODY ? config.EXTRA_BODY.trim() || null : null,
    }
    if (canSetVisibility) {
      payload.visibility = visibility
    }

    setSubmitting(true)
    setError('')
    try {
      // 编辑时用源组名原样作路径：存量组名可能带首尾空白，trim 之后就找不到那一行了
      await onSubmit(isCreate ? trimmedName : (source?.name ?? trimmedName), payload, isCreate)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('modelGroup.actions.saveFailed'))
    } finally {
      setSubmitting(false)
    }
  }

  const ownershipLabel =
    source && mode === 'edit'
      ? t(`modelGroups.ownership.${source.ownership}`, { ns: 'tenant' })
      : isPlatformOwner
        ? t('modelGroups.ownership.platform', { ns: 'tenant' })
        : t('modelGroups.ownership.tenant', { ns: 'tenant' })

  const dialogTitle =
    mode === 'copy'
      ? t('modelGroup.dialog.copyTitle')
      : mode === 'edit'
        ? t('modelGroup.dialog.editTitle')
        : t('modelGroup.dialog.createTitle')

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>
        <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap">
          <span>{dialogTitle}</span>
          <Chip
            size="small"
            label={ownershipLabel}
            sx={CHIP_VARIANTS.getCustomColorChip(
              (mode === 'edit' ? source?.ownership === 'platform' : isPlatformOwner)
                ? theme.palette.info.main
                : theme.palette.primary.main,
              true
            )}
          />
        </Stack>
      </DialogTitle>
      <DialogContent>
        <Stack spacing={2} className="mt-4">
          <TextField
            label={t('modelGroup.form.groupName')}
            value={groupName}
            onChange={e => handleGroupNameChange(e.target.value)}
            disabled={mode === 'edit'}
            fullWidth
            autoComplete="off"
            required
            size={inputSize}
            error={!!groupNameError}
            helperText={
              groupNameError ||
              (groupName
                ? ''
                : mode === 'copy'
                  ? t('modelGroup.helpers.nameCopy')
                  : t('modelGroup.helpers.nameChar'))
            }
            inputProps={{
              autoComplete: 'new-password',
              form: { autoComplete: 'off' },
            }}
          />

          {!credentialVisible && (
            <Alert severity="info" variant="outlined" icon={<LockIcon fontSize="small" />}>
              {t('modelGroup.helpers.credentialHidden')}
            </Alert>
          )}

          <Autocomplete
            freeSolo
            disabled={!credentialVisible}
            options={providerOptions}
            value={config.BASE_URL}
            onChange={(_, newValue) => {
              if (typeof newValue === 'string') setConfig({ ...config, BASE_URL: newValue })
            }}
            onInputChange={(_, newInputValue) => {
              setConfig({ ...config, BASE_URL: newInputValue })
            }}
            renderOption={(props, option) => (
              <li {...props} key={option}>
                <Box sx={{ display: 'flex', flexDirection: 'column' }}>
                  <Typography variant="body2">
                    {providerMetaByUrl.get(option) || t('common.custom', { ns: 'common' })}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {option}
                  </Typography>
                </Box>
              </li>
            )}
            renderInput={params => (
              <TextField
                {...params}
                label={t('modelGroup.form.apiAddress')}
                placeholder={t('modelGroup.placeholders.apiAddress')}
                autoComplete="off"
                size={inputSize}
                inputProps={{
                  ...params.inputProps,
                  autoComplete: 'new-password',
                  form: { autoComplete: 'off' },
                }}
                helperText={t('modelGroup.helpers.apiAddress')}
              />
            )}
          />

          <TextField
            label={t('modelGroup.form.apiKey')}
            value={config.API_KEY}
            onChange={e => setConfig({ ...config, API_KEY: e.target.value })}
            type="text"
            fullWidth
            autoComplete="off"
            disabled={!credentialVisible}
            size={inputSize}
            helperText={
              credentialVisible
                ? mode === 'edit'
                  ? t('modelGroup.helpers.apiKeyKeep')
                  : ' '
                : t('modelGroup.helpers.credentialHidden')
            }
            name={`apikey_${Math.random().toString(36).slice(2)}`}
            inputProps={{
              autoComplete: 'new-password',
              form: { autoComplete: 'off' },
              style: !showApiKey
                ? ({ WebkitTextSecurity: 'disc' } as React.CSSProperties)
                : undefined,
            }}
            InputProps={{
              endAdornment: (
                <InputAdornment position="end">
                  <IconActionButton
                    onClick={() => setShowApiKey(!showApiKey)}
                    edge="end"
                    size={inputSize}
                    disabled={!credentialVisible}
                    title={
                      showApiKey
                        ? t('actions.hide', { ns: 'common' })
                        : t('actions.show', { ns: 'common' })
                    }
                  >
                    {showApiKey ? <VisibilityOffIcon /> : <VisibilityIcon />}
                  </IconActionButton>
                </InputAdornment>
              ),
            }}
          />

          <TextField
            label={t('modelGroup.form.proxyAddress')}
            value={config.CHAT_PROXY}
            onChange={e => setConfig({ ...config, CHAT_PROXY: e.target.value })}
            fullWidth
            autoComplete="off"
            disabled={!credentialVisible}
            size={inputSize}
            inputProps={{
              autoComplete: 'new-password',
              form: { autoComplete: 'off' },
            }}
          />

          <Stack direction={isMobile ? 'column' : 'row'} spacing={1} alignItems="flex-start">
            <Box sx={{ flex: 1, width: '100%' }}>
              <Autocomplete
                freeSolo
                options={modelOptions}
                value={config.CHAT_MODEL}
                onChange={(_, newValue) => {
                  if (typeof newValue === 'string') setConfig({ ...config, CHAT_MODEL: newValue })
                }}
                onInputChange={(_, newInputValue) => {
                  setConfig({ ...config, CHAT_MODEL: newInputValue })
                }}
                renderInput={params => (
                  <TextField
                    {...params}
                    label={t('modelGroup.form.modelName')}
                    autoComplete="off"
                    helperText={t('modelGroup.helpers.modelName')}
                    inputProps={{
                      ...params.inputProps,
                      autoComplete: 'new-password',
                      form: { autoComplete: 'off' },
                    }}
                    size={inputSize}
                  />
                )}
              />
            </Box>
            <Tooltip title={fetchTooltipTitle}>
              <span>
                <ActionButton
                  tone="secondary"
                  onClick={fetchAvailableModels}
                  disabled={!canFetchModels}
                  size={inputSize}
                  sx={{ whiteSpace: 'nowrap', height: inputHeight }}
                >
                  {fetchingModels ? (
                    <>
                      <CircularProgress size={16} sx={{ mr: 1 }} />{' '}
                      {t('modelGroup.actions.fetching')}
                    </>
                  ) : (
                    t('modelGroup.actions.fetchModels')
                  )}
                </ActionButton>
              </span>
            </Tooltip>
          </Stack>

          <TextField
            select
            label={t('modelGroup.form.modelType')}
            value={config.MODEL_TYPE || 'chat'}
            onChange={e => setConfig({ ...config, MODEL_TYPE: e.target.value })}
            fullWidth
            size={inputSize}
          >
            {modelTypes.map(type => (
              <MenuItem key={type.value} value={type.value}>
                <Box sx={{ display: 'flex', alignItems: 'center' }}>
                  {getModelTypeIcon(type.value)}
                  <Box sx={{ ml: 1 }}>
                    <Typography variant="body2" sx={{ fontSize: isSmall ? '0.8rem' : 'inherit' }}>
                      {t(`modelGroup.types.${type.value as 'chat' | 'embedding' | 'draw'}`, {
                        defaultValue: type.label,
                      })}
                    </Typography>
                    {type.description && (
                      <Typography
                        variant="caption"
                        color="text.secondary"
                        sx={{ fontSize: isSmall ? '0.7rem' : 'inherit' }}
                      >
                        {t(
                          `modelGroup.typeDescriptions.${type.value as 'chat' | 'embedding' | 'draw'}`,
                          { defaultValue: type.description }
                        )}
                      </Typography>
                    )}
                  </Box>
                </Box>
              </MenuItem>
            ))}
          </TextField>

          {canSetVisibility && (
            <TextField
              select
              label={t('modelGroup.form.visibility')}
              value={visibility}
              onChange={e => setVisibility(e.target.value as ModelGroupVisibility)}
              fullWidth
              size={inputSize}
              helperText={
                visibility === 'shared'
                  ? t('modelGroup.helpers.visibilityShared')
                  : t('modelGroup.helpers.visibilityPrivate')
              }
            >
              <MenuItem value="shared">{t('modelGroup.visibility.shared')}</MenuItem>
              <MenuItem value="private">{t('modelGroup.visibility.private')}</MenuItem>
            </TextField>
          )}

          {/* 模型功能选项 */}
          <Box className="border-t pt-2 mt-2">
            <Typography
              variant="subtitle2"
              className="mb-2"
              sx={{ fontSize: isSmall ? '0.8rem' : 'inherit' }}
            >
              {t('modelGroup.helpers.modelCapabilities')}
            </Typography>
            <Stack direction={isMobile ? 'column' : 'row'} spacing={isMobile ? 1 : 4}>
              <Tooltip title={t('modelGroup.helpers.vision')}>
                <div>
                  <FormControlLabel
                    control={
                      <Switch
                        checked={config.ENABLE_VISION}
                        onChange={e => setConfig({ ...config, ENABLE_VISION: e.target.checked })}
                        color="primary"
                        size={isSmall ? 'small' : 'medium'}
                      />
                    }
                    label={
                      <Typography sx={{ fontSize: isSmall ? '0.8rem' : 'inherit' }}>
                        {t('modelGroup.form.vision')}
                      </Typography>
                    }
                  />
                </div>
              </Tooltip>
              <Tooltip title={t('modelGroup.helpers.cot')}>
                <div>
                  <FormControlLabel
                    control={
                      <Switch
                        checked={config.ENABLE_COT}
                        onChange={e => setConfig({ ...config, ENABLE_COT: e.target.checked })}
                        color="primary"
                        size={isSmall ? 'small' : 'medium'}
                      />
                    }
                    label={
                      <Typography sx={{ fontSize: isSmall ? '0.8rem' : 'inherit' }}>
                        {t('modelGroup.form.cot')}
                      </Typography>
                    }
                  />
                </div>
              </Tooltip>
            </Stack>
          </Box>

          <ActionButton
            tone="ghost"
            onClick={() => setShowAdvanced(!showAdvanced)}
            className="self-start"
            size={isSmall ? 'small' : 'medium'}
          >
            {showAdvanced
              ? t('modelGroup.helpers.collapseAdvanced')
              : t('modelGroup.helpers.expandAdvanced')}
          </ActionButton>

          {showAdvanced && (
            <Stack spacing={2} className="pl-4 border-l-2 border-gray-200">
              <TextField
                label={t('modelGroup.form.temperature')}
                type="number"
                value={config.TEMPERATURE ?? ''}
                onChange={e =>
                  setConfig({
                    ...config,
                    TEMPERATURE: e.target.value ? parseFloat(e.target.value) : null,
                  })
                }
                fullWidth
                size={inputSize}
                inputProps={{ step: 0.1, min: 0, max: 2 }}
                helperText={t('modelGroup.helpers.temperature')}
              />
              <TextField
                label={t('modelGroup.form.topP')}
                type="number"
                value={config.TOP_P ?? ''}
                onChange={e =>
                  setConfig({ ...config, TOP_P: e.target.value ? parseFloat(e.target.value) : null })
                }
                fullWidth
                size={inputSize}
                inputProps={{ step: 0.1, min: 0, max: 1 }}
                helperText={t('modelGroup.helpers.topP')}
              />
              <TextField
                label={t('modelGroup.form.presencePenalty')}
                type="number"
                value={config.PRESENCE_PENALTY ?? ''}
                onChange={e =>
                  setConfig({
                    ...config,
                    PRESENCE_PENALTY: e.target.value ? parseFloat(e.target.value) : null,
                  })
                }
                fullWidth
                size={inputSize}
                inputProps={{ step: 0.1, min: -2, max: 2 }}
                helperText={t('modelGroup.helpers.presencePenalty')}
              />
              <TextField
                label={t('modelGroup.form.frequencyPenalty')}
                type="number"
                value={config.FREQUENCY_PENALTY ?? ''}
                onChange={e =>
                  setConfig({
                    ...config,
                    FREQUENCY_PENALTY: e.target.value ? parseFloat(e.target.value) : null,
                  })
                }
                fullWidth
                size={inputSize}
                inputProps={{ step: 0.1, min: -2, max: 2 }}
                helperText={t('modelGroup.helpers.frequencyPenalty')}
              />
              <TextField
                label={t('modelGroup.form.extraBody')}
                value={config.EXTRA_BODY ?? ''}
                onChange={e => setConfig({ ...config, EXTRA_BODY: e.target.value || null })}
                fullWidth
                size={inputSize}
                multiline
                rows={3}
                helperText={t('modelGroup.helpers.extraBody')}
              />
            </Stack>
          )}

          {error && <Alert severity="error">{error}</Alert>}

          {testResult && (
            <Alert severity={testResult.success ? 'success' : 'error'} sx={{ mt: 1 }}>
              {testResult.success ? (
                <Stack spacing={0.25}>
                  <Typography variant="caption" component="div">
                    {t('modelGroup.health.tooltipPassed')} · {testResult.latency_ms}ms
                  </Typography>
                  {testResult.used_model && (
                    <Typography variant="caption" component="div" color="text.secondary">
                      {t('modelGroup.health.tooltipModel', { model: testResult.used_model })}
                    </Typography>
                  )}
                  {((testResult.input_tokens ?? 0) > 0 || (testResult.output_tokens ?? 0) > 0) && (
                    <Typography variant="caption" component="div" color="text.secondary">
                      {t('modelGroup.health.tooltipTokens', {
                        input: testResult.input_tokens ?? 0,
                        output: testResult.output_tokens ?? 0,
                      })}
                    </Typography>
                  )}
                  {testResult.response_text && (
                    <Typography
                      variant="caption"
                      component="div"
                      color="text.secondary"
                      sx={{
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                        maxHeight: 60,
                        overflow: 'auto',
                      }}
                    >
                      {t('modelGroup.health.tooltipReply', {
                        text: testResult.response_text.trim(),
                      })}
                    </Typography>
                  )}
                </Stack>
              ) : (
                <Typography variant="caption" component="div">
                  {describeTestFailure(testResult, t, t('modelGroup.health.failed'))}
                </Typography>
              )}
            </Alert>
          )}
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2, gap: 1 }}>
        {(config.MODEL_TYPE === 'chat' || config.MODEL_TYPE === 'embedding') && (
          <Tooltip
            title={
              credentialsMasked
                ? t('modelGroup.tooltips.testPlatformOwned')
                : canTestInline
                  ? ''
                  : t('modelGroup.tooltips.testNeedsConfig')
            }
            arrow
          >
            <span>
              <ActionButton
                tone="secondary"
                onClick={handleTestModel}
                size="small"
                startIcon={testingModel ? <CircularProgress size={14} /> : <NetworkCheckIcon />}
                disabled={testingModel || !canTestInline}
                sx={{ mr: 'auto' }}
              >
                {t('modelGroup.actions.test')}
              </ActionButton>
            </span>
          </Tooltip>
        )}
        <ActionButton
          tone="secondary"
          onClick={onClose}
          sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
        >
          {t('actions.cancel', { ns: 'common' })}
        </ActionButton>
        <ActionButton
          tone="primary"
          onClick={handleSubmit}
          disabled={!!groupNameError || !groupName || submitting}
          sx={{ minWidth: { xs: 64, sm: 80 }, minHeight: { xs: 36, sm: 40 } }}
        >
          {mode === 'copy'
            ? t('modelGroup.actions.createCopy')
            : t('actions.save', { ns: 'common' })}
        </ActionButton>
      </DialogActions>
    </Dialog>
  )
}
