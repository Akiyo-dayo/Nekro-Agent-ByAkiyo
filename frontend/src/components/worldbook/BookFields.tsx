import {
  Box,
  FormControlLabel,
  Stack,
  Switch,
  TextField,
  Typography,
} from '@mui/material'
import { useTranslation } from 'react-i18next'
import FilterSelect from '../common/FilterSelect'
import SegmentedControl, { type SegmentedControlOption } from '../common/SegmentedControl'
import {
  WORLDBOOK_DESCRIPTION_MAX_LENGTH,
  WORLDBOOK_NAME_MAX_LENGTH,
  WORLDBOOK_SCOPE_KINDS,
  type WorldbookBookDetail,
  type WorldbookChannelOption,
  type WorldbookPresetOption,
  type WorldbookScopeKind,
  type WorldbookScopeParams,
} from '../../services/api/worldbook'
import { WorldbookScopeChip } from './chips'
import { worldbookBindingText } from './display'
import type { WorldbookBookFormErrors, WorldbookBookFormState } from './bookForm'

interface BookFieldsProps {
  value: WorldbookBookFormState
  onChange: (next: WorldbookBookFormState) => void
  errors: WorldbookBookFormErrors
  mode: 'create' | 'edit'
  scope: WorldbookScopeParams
  presets: WorldbookPresetOption[]
  channels: WorldbookChannelOption[]
  /** 书设置模式下展示只读的作用域信息 */
  book?: WorldbookBookDetail
}

/**
 * 建书 / 书设置共用的字段组
 *
 * 作用域类型与绑定对象只在建书时可改：换作用域等于换一本书，条目会跟着跑到另一个
 * 频道或人设上，所以设置对话框里只读显示。
 */
export default function BookFields({
  value,
  onChange,
  errors,
  mode,
  scope,
  presets,
  channels,
  book,
}: BookFieldsProps) {
  const { t } = useTranslation('worldbook')
  const hasInstance = Boolean(scope.instance_key)

  const update = (patch: Partial<WorldbookBookFormState>) => onChange({ ...value, ...patch })

  const scopeOptions: SegmentedControlOption<WorldbookScopeKind>[] = WORLDBOOK_SCOPE_KINDS.map(
    kind => ({
      value: kind,
      label: t(`scope.${kind}`),
      disabled: !hasInstance && (kind === 'domain' || kind === 'person'),
    })
  )

  const presetOptions = [
    { value: '', label: t('bookForm.presetPlaceholder') },
    ...presets
      .filter(preset => preset.preset_id > 0)
      .map(preset => ({ value: String(preset.preset_id), label: preset.name })),
  ]

  const channelOptions = [
    { value: '', label: t('bookForm.channelPlaceholder') },
    ...channels.map(channel => ({
      value: channel.chat_key,
      label: channel.channel_name || channel.chat_key,
    })),
  ]

  return (
    <Stack spacing={2}>
      <TextField
        label={t('bookForm.name')}
        value={value.name}
        onChange={event => update({ name: event.target.value })}
        error={Boolean(errors.name)}
        helperText={
          errors.name
            ? t(errors.name, { max: WORLDBOOK_NAME_MAX_LENGTH })
            : t('bookForm.nameLimit', { max: WORLDBOOK_NAME_MAX_LENGTH })
        }
        slotProps={{ htmlInput: { maxLength: WORLDBOOK_NAME_MAX_LENGTH } }}
        fullWidth
        required
      />

      <TextField
        label={t('bookForm.description')}
        value={value.description}
        onChange={event => update({ description: event.target.value })}
        error={Boolean(errors.description)}
        helperText={
          errors.description
            ? t(errors.description, { max: WORLDBOOK_DESCRIPTION_MAX_LENGTH })
            : t('bookForm.descriptionLimit', { max: WORLDBOOK_DESCRIPTION_MAX_LENGTH })
        }
        slotProps={{ htmlInput: { maxLength: WORLDBOOK_DESCRIPTION_MAX_LENGTH } }}
        multiline
        minRows={2}
        fullWidth
      />

      <Box>
        <Typography variant="body2" fontWeight={600} sx={{ mb: 1 }}>
          {t('bookForm.scopeKind')}
        </Typography>
        {mode === 'edit' && book ? (
          <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
            <WorldbookScopeChip scopeKind={book.scope_kind} />
            {worldbookBindingText(book) && (
              <Typography variant="body2">{worldbookBindingText(book)}</Typography>
            )}
            <Typography variant="caption" color="text.secondary">
              {book.instance_key || t('common.globalPreset')}
            </Typography>
          </Stack>
        ) : (
          <Stack spacing={1}>
            <SegmentedControl
              value={value.scopeKind}
              options={scopeOptions}
              onChange={kind => update({ scopeKind: kind })}
              fullWidth
            />
            <Typography variant="caption" color={errors.scope ? 'error' : 'text.secondary'}>
              {errors.scope
                ? t(errors.scope, { max: WORLDBOOK_NAME_MAX_LENGTH })
                : t(`bookForm.scopeHelp.${value.scopeKind}`)}
            </Typography>
            {!hasInstance && (
              <Typography variant="caption" color="warning.main">
                {t('bookForm.needInstance')}
              </Typography>
            )}

            {value.scopeKind === 'preset' && (
              <FilterSelect
                label={t('bookForm.preset')}
                value={value.presetId}
                options={presetOptions}
                onChange={presetId => update({ presetId })}
                error={Boolean(errors.scope)}
              />
            )}
            {value.scopeKind === 'channel' && (
              <FilterSelect
                label={t('bookForm.channel')}
                value={value.chatKey}
                options={channelOptions}
                onChange={chatKey => update({ chatKey })}
                error={Boolean(errors.scope)}
              />
            )}
            {value.scopeKind === 'person' && (
              <TextField
                label={t('bookForm.person')}
                value={value.personAccount}
                onChange={event => update({ personAccount: event.target.value })}
                error={Boolean(errors.scope)}
                helperText={errors.scope ? t(errors.scope) : t('bookForm.personHelper')}
                fullWidth
              />
            )}
          </Stack>
        )}
      </Box>

      <FormControlLabel
        control={
          <Switch
            checked={value.enabled}
            onChange={event => update({ enabled: event.target.checked })}
          />
        }
        label={t('bookForm.enabled')}
      />

      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        <TextField
          label={t('bookForm.scanDepth')}
          value={value.scanDepth}
          onChange={event => update({ scanDepth: event.target.value })}
          error={Boolean(errors.scanDepth)}
          helperText={
            errors.scanDepth
              ? t(errors.scanDepth)
              : mode === 'edit'
                ? t('bookForm.scanDepthHelperEdit')
                : value.scopeKind === 'person'
                ? t('bookForm.scanDepthHelperPerson')
                : t('bookForm.scanDepthHelper')
          }
          type="number"
          slotProps={{ htmlInput: { min: 1, max: 50 } }}
          fullWidth
        />
        <TextField
          label={t('bookForm.budget')}
          value={value.budgetChars}
          onChange={event => update({ budgetChars: event.target.value })}
          error={Boolean(errors.budget)}
          helperText={errors.budget ? t(errors.budget) : t('bookForm.budgetHelper')}
          type="number"
          slotProps={{ htmlInput: { min: 0, max: 100000 } }}
          fullWidth
        />
      </Stack>

      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        <FormControlLabel
          control={
            <Switch
              checked={value.caseSensitive}
              onChange={event => update({ caseSensitive: event.target.checked })}
            />
          }
          label={t('bookForm.caseSensitive')}
        />
        <FormControlLabel
          control={
            <Switch
              checked={value.matchWholeWords}
              onChange={event => update({ matchWholeWords: event.target.checked })}
            />
          }
          label={t('bookForm.matchWholeWords')}
        />
      </Stack>
      <Typography variant="caption" color="text.secondary">
        {t('bookForm.matchWholeWordsHelper')}
      </Typography>
    </Stack>
  )
}
