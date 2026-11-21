import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Box, Chip, Stack, Typography, useTheme } from '@mui/material'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import IconActionButton from '../../components/common/IconActionButton'
import SearchField from '../../components/common/SearchField'
import ServerDataTable, { type ServerDataTableColumn } from '../../components/common/ServerDataTable'
import { SimpleTooltip } from '../../components/common/ThemedTooltip'
import PersonCardDrawer from '../../components/person/PersonCardDrawer'
import { isNotFoundError } from '../../services/api/axios'
import {
  stateTableApi,
  type StatePersonItem,
  type StateScope,
  type StateTemplateSummary,
} from '../../services/api/stateTable'
import { CHIP_VARIANTS } from '../../theme/variants'
import { formatFullTime, formatListTime } from '../../utils/listTime'
import StateDetailDialog from './StateDetailDialog'

const PAGE_SIZE = 20

/**
 * 「个人档案」标签
 *
 * 人设名不在列表接口里，只能拿 `preset_id` 去模板列表换；模板列表本来就要给模板标签用，
 * 这里复用同一份查询键，换标签时不会重复拉。找不到映射时退回 `#3` 这样的编号，
 * 至少还能看出是哪一个。
 */
export default function PeopleTab({ scope }: { scope: StateScope }) {
  const theme = useTheme()
  const { t } = useTranslation('state')
  const { t: tCommon } = useTranslation('common')
  const { t: tPerson } = useTranslation('person')
  const scopeKey = `${scope.adapter_key}|${scope.instance_key}`
  const [searchParams, setSearchParams] = useSearchParams()

  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [detail, setDetail] = useState<StatePersonItem | null>(null)
  const [cardAccount, setCardAccount] = useState<string | null>(null)
  // 状态页本来就必须有实例；三样齐全才给人物卡入口
  const canOpenCard = Boolean(scope.adapter_key && scope.instance_key)

  // 从好感页跳过来时带着 `?person=<id>`：挂载后直接打开那一份
  const personParam = searchParams.get('person')
  const personFromUrl = useMemo(() => {
    if (!personParam) return null
    const parsed = Number(personParam)
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null
  }, [personParam])

  const personQuery = useQuery({
    queryKey: ['state-table', 'person', scopeKey, personFromUrl],
    queryFn: () => stateTableApi.getPerson(scope, personFromUrl as number),
    enabled: personFromUrl !== null,
    retry: false,
  })

  const personTarget = personQuery.data

  useEffect(() => {
    if (!personTarget) return
    setDetail(personTarget)
  }, [personTarget])

  const closeDetail = () => {
    setDetail(null)
    if (personFromUrl === null) return
    const next = new URLSearchParams(searchParams)
    next.delete('person')
    setSearchParams(next, { replace: true })
  }

  const query = useQuery({
    queryKey: ['state-table', 'people', scopeKey, page, search],
    queryFn: () =>
      stateTableApi.listPeople(scope, {
        uid: search.trim() || undefined,
        page,
        size: PAGE_SIZE,
      }),
    retry: false,
    placeholderData: keepPreviousData,
  })

  const templatesQuery = useQuery({
    queryKey: ['state-table', 'templates', scopeKey, ''],
    queryFn: () => stateTableApi.listTemplates(scope, ''),
    retry: false,
  })

  const presetNames = useMemo(() => {
    const map = new Map<number, string>()
    ;(templatesQuery.data?.items ?? []).forEach((item: StateTemplateSummary) => {
      map.set(item.preset_id, item.preset_name)
    })
    return map
  }, [templatesQuery.data])

  // 人设名查不到时退回编号，至少还能看出是哪一个人设
  const presetLabel = useCallback(
    (presetId: number) => presetNames.get(presetId) ?? `#${presetId}`,
    [presetNames]
  )

  const columns: ServerDataTableColumn<StatePersonItem>[] = useMemo(
    () => [
      {
        key: 'person',
        header: t('people.columns.person'),
        width: 240,
        render: row => (
          <Stack direction="row" spacing={0.5} alignItems="center" sx={{ minWidth: 0 }}>
            <Box sx={{ minWidth: 0, flex: 1 }}>
              <SimpleTooltip title={row.platform_userid}>
                <span>
                  <Typography variant="body2" fontWeight={600} noWrap sx={{ display: 'block' }}>
                    {row.nickname || row.platform_userid}
                  </Typography>
                </span>
              </SimpleTooltip>
            </Box>
            {canOpenCard && (
              <SimpleTooltip title={tPerson('entry')}>
                <span>
                  <IconActionButton
                    size="small"
                    aria-label={tPerson('entry')}
                    onClick={event => {
                      // 整行点击会打开档案详情；这里只开人物卡
                      event.stopPropagation()
                      setCardAccount(row.platform_userid)
                    }}
                    onMouseDown={event => event.stopPropagation()}
                  >
                    <PersonOutlineIcon fontSize="small" />
                  </IconActionButton>
                </span>
              </SimpleTooltip>
            )}
          </Stack>
        ),
      },
      {
        key: 'preset',
        header: t('people.columns.preset'),
        width: 150,
        render: row => <Typography variant="body2">{presetLabel(row.preset_id)}</Typography>,
      },
      {
        key: 'domain',
        header: t('people.columns.domain'),
        width: 120,
        render: row => (
          <Chip
            size="small"
            label={
              row.domain_group_id > 0 ? t('people.instanceGroup') : t('people.localGroup')
            }
            sx={CHIP_VARIANTS.getCustomColorChip(
              row.domain_group_id > 0
                ? theme.palette.info.main
                : theme.palette.text.secondary,
              true
            )}
          />
        ),
      },
      {
        key: 'updatedAt',
        header: t('people.columns.updatedAt'),
        width: 180,
        hideOnMobile: true,
        render: row => (
          <SimpleTooltip title={formatFullTime(row.update_time)}>
            <span>
              <Typography variant="caption" color="text.secondary">
                {formatListTime(row.update_time)}
              </Typography>
            </span>
          </SimpleTooltip>
        ),
      },
    ],
    // 人设名来自另一份查询：它到了之后人设列必须重画，否则整列都停在编号上
    [t, theme, presetLabel, canOpenCard, tPerson]
  )

  if (isNotFoundError(query.error)) {
    return <Alert severity="info">{t('notEnabled')}</Alert>
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minHeight: 0, flex: 1 }}>
      {/* 跳过来的那一份看不到（不归这个实例、已删）时说一句，别让人对着列表找 */}
      {personFromUrl !== null && personQuery.isError && (
        <Alert severity="info">{t('people.personNotFound')}</Alert>
      )}
      <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', flexWrap: 'wrap' }}>
        <SearchField
          value={search}
          onChange={value => {
            setSearch(value)
            setPage(1)
          }}
          onClear={() => {
            setSearch('')
            setPage(1)
          }}
          clearAriaLabel={tCommon('actions.clear')}
          placeholder={t('people.uidPlaceholder')}
          sx={{ flex: 1, minWidth: 200, maxWidth: 320 }}
        />
      </Box>

      <Stack spacing={1} sx={{ minHeight: 0, flex: 1 }}>
        <ServerDataTable
          columns={columns}
          rows={query.data?.items ?? []}
          getRowId={row => row.id}
          total={query.data?.total ?? 0}
          page={page}
          pageSize={PAGE_SIZE}
          onPageChange={setPage}
          loading={query.isPending}
          fetching={query.isFetching}
          error={query.error}
          onRetry={() => void query.refetch()}
          emptyTitle={t('people.emptyTitle')}
          emptyHint={t('people.emptyHint')}
          onRowClick={row => setDetail(row)}
        />
      </Stack>

      {detail && (
        <StateDetailDialog
          scope={scope}
          kind="person"
          target={detail}
          onClose={closeDetail}
        />
      )}

      {canOpenCard && (
        <PersonCardDrawer
          open={cardAccount !== null}
          onClose={() => setCardAccount(null)}
          adapterKey={scope.adapter_key}
          instanceKey={scope.instance_key}
          account={cardAccount ?? ''}
        />
      )}
    </Box>
  )
}
