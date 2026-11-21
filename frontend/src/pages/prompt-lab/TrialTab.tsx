import { useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Chip,
  CircularProgress,
  Link,
  Stack,
  TextField,
  Typography,
  useTheme,
  type SxProps,
  type Theme,
} from '@mui/material'
import { Link as RouterLink } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import FilterSelect from '../../components/common/FilterSelect'
import QueryErrorState from '../../components/common/QueryErrorState'
import { useWorldbookScopeOptions } from '../../components/worldbook/useWorldbookScopeOptions'
import { useIdentity } from '../../hooks/useIdentity'
import { TENANT_QUERY_KEYS, tenantApi } from '../../services/api/tenant'
import { tavernApi, type TavernTrialResponse } from '../../services/api/tavern'
import type { WorldbookScopeParams } from '../../services/api/worldbook'
import { stateText } from '../../components/memory/tavernItems'
import { CHIP_VARIANTS } from '../../theme/variants'

/**
 * 「试发」标签
 *
 * 租户只能用自己名下的聊天模型组：试发的用量记在所选组上，用平台方的组等于让租户
 * 烧平台方的账。平台方可以选任意可见的聊天组，也可以留空走频道当前的主模型组。
 *
 * 顶部那句「不会发进任何群或私聊」是固定文案：试发曾被理解成往频道里发内部消息，
 * 这里必须从第一眼就说清楚。
 */

const TRIAL_TEXT_MAX_CHARS = 2000

interface PromptLabTabProps {
  scope: WorldbookScopeParams
  isScopeReady: boolean
}

function ResultBlock({ result }: { result: TavernTrialResponse }) {
  const theme = useTheme()
  const { t } = useTranslation('tavern')

  const preStyle: SxProps<Theme> = {
    m: 0,
    p: 1.5,
    borderRadius: 1,
    bgcolor: theme.palette.action.hover,
    fontFamily: 'monospace',
    fontSize: '0.8rem',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    maxHeight: 420,
    overflowY: 'auto',
    overflowX: 'auto',
  }

  return (
    <Stack spacing={1.5}>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
        <Chip size="small" label={t('lab.trial.model', { model: result.model || '—' })} />
        <Chip
          size="small"
          label={t('lab.trial.modelGroup', { group: result.model_group || '—' })}
        />
        <Chip size="small" label={t('lab.trial.latency', { ms: result.latency_ms })} />
        <Chip
          size="small"
          label={t('lab.trial.prefill', { state: stateText(result.prefill_state, t) })}
          sx={CHIP_VARIANTS.getCustomColorChip(
            result.prefill_state === 'sent'
              ? theme.palette.success.main
              : theme.palette.text.secondary,
            true
          )}
        />
      </Box>

      {result.refusal && (
        <Alert severity="error">{t('lab.trial.refusal', { category: result.refusal })}</Alert>
      )}
      {result.error_kind || result.error ? (
        <Alert severity="error">
          {result.error_kind ? `${result.error_kind}: ` : ''}
          {result.error || t('lab.trial.failed')}
        </Alert>
      ) : null}

      <Box>
        <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 0.5 }}>
          {t('lab.trial.reply')}
        </Typography>
        <Box component="pre" sx={preStyle}>
          {result.reply || t('lab.trial.emptyReply')}
        </Box>
      </Box>

      <Box>
        <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 0.5 }}>
          {t('lab.trial.code')}
        </Typography>
        {result.code ? (
          <Box component="pre" sx={preStyle}>
            {result.code}
          </Box>
        ) : (
          <Alert severity="warning">{t('lab.trial.codeEmpty')}</Alert>
        )}
      </Box>

      <Typography variant="body2" color="text.secondary">
        {t('lab.trial.usage', {
          input: result.usage?.input_tokens ?? 0,
          output: result.usage?.output_tokens ?? 0,
          total: result.usage?.total_tokens ?? 0,
        })}
      </Typography>
    </Stack>
  )
}

export default function TrialTab({ scope, isScopeReady }: PromptLabTabProps) {
  const { t } = useTranslation('tavern')
  const { isTenantScoped } = useIdentity()
  const { data: options } = useWorldbookScopeOptions(scope)
  const [chatKey, setChatKey] = useState('')
  const [modelGroup, setModelGroup] = useState('')
  const [text, setText] = useState('')

  const groupsQuery = useQuery({
    queryKey: TENANT_QUERY_KEYS.modelGroups,
    queryFn: () => tenantApi.listModelGroups(),
    enabled: isScopeReady,
    retry: false,
  })

  const chatGroups = useMemo(
    () => (groupsQuery.data?.items ?? []).filter(group => group.model_type === 'chat'),
    [groupsQuery.data?.items]
  )
  const tenantGroups = useMemo(
    () => chatGroups.filter(group => group.editable),
    [chatGroups]
  )

  // 租户必须带一个自己的组：没有选择时默认第一个，不做「留空走频道」那一档。
  const selectedGroup = isTenantScoped ? modelGroup || tenantGroups[0]?.name || '' : modelGroup

  const groupOptions = useMemo(
    () => [
      ...(isTenantScoped
        ? []
        : [{ value: '', label: t('lab.trial.groupCurrent') }]),
      ...(isTenantScoped ? tenantGroups : chatGroups).map(group => ({
        value: group.name,
        label: group.name,
      })),
    ],
    [chatGroups, isTenantScoped, t, tenantGroups]
  )

  const channelOptions = useMemo(
    () => [
      { value: '', label: t('lab.preview.channelPlaceholder') },
      ...(options?.channels ?? []).map(channel => ({
        value: channel.chat_key,
        label: channel.channel_name || channel.chat_key,
      })),
    ],
    [options?.channels, t]
  )

  const mutation = useMutation({
    mutationFn: () =>
      tavernApi.trial({
        chat_key: chatKey,
        text: text.trim(),
        model_group: isTenantScoped ? selectedGroup : modelGroup || null,
      }),
  })

  const noTenantGroup = isTenantScoped && !groupsQuery.isLoading && tenantGroups.length === 0

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        overflowY: 'auto',
        minHeight: 0,
        pr: { xs: 0, sm: 0.5 },
      }}
    >
      <Alert severity="info">{t('lab.trial.notice')}</Alert>

      {groupsQuery.error ? <QueryErrorState error={groupsQuery.error} compact /> : null}

      {noTenantGroup ? (
        <Alert severity="warning">
          {t('lab.trial.tenantEmpty')}{' '}
          <Link component={RouterLink} to="/console/model-groups">
            {t('lab.trial.goModelGroups')}
          </Link>
        </Alert>
      ) : (
        <>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
            <FilterSelect
              label={t('lab.preview.channel')}
              value={chatKey}
              options={channelOptions}
              onChange={setChatKey}
              showEmptyOption
            />
            {/* 平台方的默认项「频道当前的主模型组」值是空串，收起时也要看得到它 */}
            <FilterSelect
              label={t('lab.trial.group')}
              value={selectedGroup}
              options={groupOptions}
              onChange={setModelGroup}
              showEmptyOption
            />
          </Stack>

          <TextField
            label={t('lab.trial.text')}
            value={text}
            onChange={event => setText(event.target.value.slice(0, TRIAL_TEXT_MAX_CHARS))}
            placeholder={t('lab.trial.textPlaceholder')}
            helperText={t('lab.trial.textHelper', {
              count: text.length,
              max: TRIAL_TEXT_MAX_CHARS,
            })}
            multiline
            minRows={3}
            fullWidth
          />

          <Box>
            <ActionButton
              tone="primary"
              onClick={() => mutation.mutate()}
              startIcon={
                mutation.isPending ? <CircularProgress size={14} color="inherit" /> : undefined
              }
              disabled={
                !chatKey ||
                !isScopeReady ||
                !text.trim() ||
                (isTenantScoped && !selectedGroup) ||
                mutation.isPending
              }
            >
              {mutation.isPending ? t('lab.trial.running') : t('lab.trial.run')}
            </ActionButton>
          </Box>

          {mutation.error ? (
            <Alert severity="error">
              {mutation.error instanceof Error
                ? mutation.error.message
                : t('lab.trial.failed')}
            </Alert>
          ) : null}
          {mutation.data ? <ResultBlock result={mutation.data} /> : null}
        </>
      )}
    </Box>
  )
}
