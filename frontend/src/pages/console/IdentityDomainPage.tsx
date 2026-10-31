/**
 * 总后台 → 身份与域
 *
 * 隔离键是 `(记忆域, 人, 人设)`。这一页管前两维的运营面：
 *
 * | 页签 | 管什么 |
 * |---|---|
 * | 记忆域 | 把实例编进域。默认每个实例自成一域，配置就是让几个实例互通 |
 * | 账号合并 | 换号的人不能丢：把两个账号认成同一个人，可撤销 |
 * | 孤儿数据 | 人设删掉之后挂在它下面的数据，可见、可手动清理，不级联删 |
 *
 * ## 不摆点不动的按钮
 *
 * 本项目栽过（`InstanceDetailPage` 有两处漏了身份门，租户点了就 403）。这一页的做法是
 * **可见范围完全由后端收敛**：租户拿到的实例清单、域清单里本来就只有自己的东西，
 * 所以列表上的按钮对他一律有效。唯一按身份分叉的是「归属租户」这一档 ——
 * 它由后端返回的 `can_choose_tenant` 决定，租户身份下那一格根本不渲染，
 * 而不是渲染出来再禁用。
 *
 * 只读的租户普通用户（`isReadOnly`）看到的是整页只读：后端这一族全是 `Role.Admin`，
 * 给他画出写按钮就是点了才发现 403。
 *
 * ## 判定一个字都不复算
 *
 * 合并预览里的 `blockers` / `warnings` / `transfers`、以及「合完之后采用哪个值」，
 * 全部由后端算好原样显示。「好感取高的」的权威实现在 `services/affection/merge.py`，
 * 前端复算一份等于把授权语义写成第三份。同理，后端随响应给的那几段说明
 * （`memory_scope_note` / `reversible_note` / `policy_note`）直接显示，不进 i18n 字典 ——
 * 它们描述的是**服务端此刻的行为**，抄一份到前端就会在下一次改判据时分叉。
 */

import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Checkbox,
  Chip,
  CircularProgress,
  Divider,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Typography,
} from '@mui/material'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import AddRoundedIcon from '@mui/icons-material/AddRounded'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../components/common/ActionButton'
import NekroDialog from '../../components/common/NekroDialog'
import QueryErrorState from '../../components/common/QueryErrorState'
import { useNotification } from '../../hooks/useNotification'
import { useIdentity } from '../../hooks/useIdentity'
import { useInstanceScope } from '../../hooks/useInstanceScope'
import {
  IDENTITY_QUERY_KEYS,
  identityApi,
  type AliasItem,
  type AliasRequestItem,
  type DomainInfo,
  type InstanceDomainItem,
  type MergePreviewResponse,
  type OrphanBucketItem,
} from '../../services/api/identity'

/** 「自成一域」在下拉里的取值。空串而不是 null，MUI Select 的受控值不接受 null */
const SELF_DOMAIN = ''

/** 一次合并请求。预览与执行发的必须是**同一个**对象，见 `MergeTab` 里 `preview` 的注释 */
interface MergeRequestBody {
  instance_key: string
  alias_userid: string
  person_userid: string
  note: string
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function SectionTitle({ title, hint }: { title: string; hint: string }) {
  return (
    <Box>
      <Typography variant="subtitle1" fontWeight={700}>
        {title}
      </Typography>
      <Typography variant="caption" color="text.secondary">
        {hint}
      </Typography>
    </Box>
  )
}

/** 页签一：把实例编进域 */
function DomainTab({ readOnly }: { readOnly: boolean }) {
  const { t } = useTranslation('console-identity')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [note, setNote] = useState('')
  // 建组时的归属租户。`null` = 平台方自己的组；只有 `can_choose_tenant` 为真时这一格才渲染。
  // 组的归属决定了**哪些实例能加进来**（后端 `assert_instance_joinable` 要求两者逐字相等），
  // 所以平台方没有这一档的话，他给客户建的组一个客户实例都装不进去 —— 建完就是个死组。
  const [ownerTenantId, setOwnerTenantId] = useState<number | null>(null)
  const [pendingDelete, setPendingDelete] = useState<DomainInfo | null>(null)
  // 待确认的入域操作：加入任何组域都要先过预览对话框（并入与否、并入多少由后端算好）。
  // 退回自成一域是安全方向（没有并入这一说），仍走直接生效的老路。
  const [pendingJoin, setPendingJoin] = useState<{
    instance: InstanceDomainItem
    groupId: number
  } | null>(null)
  const [migrateChecked, setMigrateChecked] = useState(false)

  const domainsQuery = useQuery({
    queryKey: IDENTITY_QUERY_KEYS.domains,
    queryFn: () => identityApi.listDomains(),
    retry: false,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: IDENTITY_QUERY_KEYS.domains })
  }

  const createMutation = useMutation({
    mutationFn: () =>
      identityApi.createDomain({
        name: name.trim(),
        note: note.trim(),
        // 租户身份下后端一律改写成自己的租户（请求体里的值被忽略），所以这里传什么都安全；
        // 不渲染那一格只是为了不摆一个点不动的按钮。
        tenant_id: ownerTenantId,
      }),
    onSuccess: () => {
      notification.success(t('domain.created', { name: name.trim() }))
      setCreateOpen(false)
      setName('')
      setNote('')
      setOwnerTenantId(null)
      invalidate()
    },
    onError: (error: unknown) => notification.error(errorMessage(error)),
  })

  const bindMutation = useMutation({
    mutationFn: ({
      instanceKey,
      groupId,
      migrateAffection,
    }: {
      instanceKey: string
      groupId: number | null
      migrateAffection: boolean
    }) => identityApi.setInstanceDomain(instanceKey, groupId, migrateAffection),
    onSuccess: result => {
      const migration = result.migration
      notification.success(
        result.group_id
          ? migration
            ? t('domain.joinedMigrated', {
                instance: result.instance_key,
                domain: result.group_name,
                rows: migration.affection_rows,
                folded: migration.folded_rows,
                aliases: migration.alias_rows,
              })
            : t('domain.joined', { instance: result.instance_key, domain: result.group_name })
          : t('domain.left', { instance: result.instance_key })
      )
      setPendingJoin(null)
      invalidate()
    },
    onError: (error: unknown) => notification.error(errorMessage(error)),
  })

  const joinPreviewMutation = useMutation({
    mutationFn: ({ instanceKey, groupId }: { instanceKey: string; groupId: number }) =>
      identityApi.previewDomainJoin(instanceKey, groupId),
    onSuccess: result => {
      // 「并入」默认勾选只在真有事可并入时成立：零数据时勾着也是空操作，
      // 反而让操作者以为漏了什么。
      setMigrateChecked(
        result.can_migrate &&
          result.affection_rows + result.alias_rows + result.person_rows > 0
      )
    },
  })

  const closeJoinDialog = () => {
    setPendingJoin(null)
    setMigrateChecked(false)
    joinPreviewMutation.reset()
  }

  const deleteMutation = useMutation({
    mutationFn: (groupId: number) => identityApi.deleteDomain(groupId),
    onSuccess: result => {
      notification.success(result.message || t('domain.deleted'))
      setPendingDelete(null)
      invalidate()
    },
    onError: (error: unknown) => notification.error(errorMessage(error)),
  })

  const data = domainsQuery.data
  // 候选归属租户 = 清单里出现过的那些租户。用实例而不是组去数：给一个"还没有任何组"的
  // 租户建第一个组正是这一格最需要成立的场景。
  const ownerTenantOptions = useMemo(
    () =>
      Array.from(
        new Set(
          (data?.instances ?? [])
            .map(item => item.tenant_id)
            .filter((tenantId): tenantId is number => tenantId !== null)
        )
      ).sort((left, right) => left - right),
    [data]
  )

  return (
    <Stack spacing={2}>
      <Alert severity="info" variant="outlined">
        {t('domain.intro')}
      </Alert>

      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
        <SectionTitle title={t('domain.listTitle')} hint={t('domain.listHint')} />
        <Box sx={{ flex: 1 }} />
        <ActionButton
          tone="secondary"
          size="small"
          startIcon={<RefreshRoundedIcon />}
          disabled={domainsQuery.isFetching}
          onClick={() => void domainsQuery.refetch()}
        >
          {t('actions.refresh')}
        </ActionButton>
        {!readOnly && (
          <ActionButton
            size="small"
            startIcon={<AddRoundedIcon />}
            onClick={() => setCreateOpen(true)}
          >
            {t('actions.create')}
          </ActionButton>
        )}
      </Box>

      {domainsQuery.isLoading && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={28} />
        </Box>
      )}
      {/* 403 与「加载失败」分开说：这一族端点全是 `Role.Admin`，只读成员拿到的
          后端原文是「权限不足」四个字，红底一条、没有下一步。换成能分辨两者的组件 */}
      {domainsQuery.isError && (
        <QueryErrorState
          compact
          error={domainsQuery.error}
          onRetry={() => void domainsQuery.refetch()}
        />
      )}

      {data && data.items.length === 0 && <Alert severity="success">{t('domain.empty')}</Alert>}

      {data && data.items.length > 0 && (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>{t('domain.columns.domain')}</TableCell>
              <TableCell>{t('domain.columns.members')}</TableCell>
              <TableCell>{t('domain.columns.note')}</TableCell>
              <TableCell align="right">{t('domain.columns.actions')}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {data.items.map(item => (
              <TableRow key={item.group_id}>
                <TableCell>
                  <Typography variant="body2" fontWeight={600}>
                    {item.name}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    g:{item.group_id} ·{' '}
                    {item.tenant_id === null
                      ? t('domain.ownerPlatform')
                      : t('domain.ownerTenant', { id: item.tenant_id })}
                  </Typography>
                </TableCell>
                <TableCell>
                  {item.members.length === 0 ? (
                    <Typography variant="caption" color="text.secondary">
                      {t('domain.emptyGroup')}
                    </Typography>
                  ) : (
                    <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                      {item.members.map(member => (
                        <Chip
                          key={`${member.adapter_key}:${member.instance_key}`}
                          size="small"
                          variant="outlined"
                          label={member.display_name || member.instance_key}
                        />
                      ))}
                    </Stack>
                  )}
                </TableCell>
                <TableCell>
                  <Typography variant="caption" color="text.secondary">
                    {item.note || '-'}
                  </Typography>
                </TableCell>
                <TableCell align="right">
                  {!readOnly && (
                    <ActionButton tone="danger" size="small" onClick={() => setPendingDelete(item)}>
                      {t('actions.delete')}
                    </ActionButton>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Divider />

      <SectionTitle title={t('domain.bindTitle')} hint={t('domain.bindHint')} />

      {data && (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>{t('domain.bindColumns.instance')}</TableCell>
              <TableCell>{t('domain.bindColumns.domainId')}</TableCell>
              <TableCell width={260}>{t('domain.bindColumns.domain')}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {data.instances.map(instance => (
              <TableRow key={`${instance.adapter_key}:${instance.instance_key}`}>
                <TableCell>
                  <Typography variant="body2" fontWeight={600}>
                    {instance.display_name || instance.instance_key}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {instance.adapter_key} / {instance.instance_key}
                  </Typography>
                </TableCell>
                <TableCell>
                  <Typography variant="caption" color="text.secondary">
                    {instance.domain_id}
                  </Typography>
                </TableCell>
                <TableCell>
                  <FormControl size="small" fullWidth disabled={readOnly || bindMutation.isPending}>
                    <InputLabel id={`domain-${instance.instance_key}`}>
                      {t('domain.bindColumns.domain')}
                    </InputLabel>
                    <Select
                      labelId={`domain-${instance.instance_key}`}
                      label={t('domain.bindColumns.domain')}
                      // `displayEmpty` 不是样式偏好，是**默认态能不能显示出来**：
                      // 「自成一域」的取值是空串（MUI 的受控 Select 不接受 null），
                      // 而 MUI 在没有 `displayEmpty` 时把空串当成「还没选」，渲染一个
                      // 零宽空格 —— 于是零配置部署里**每一行都是空白**，正好是这一页
                      // 最常见的那个状态。
                      displayEmpty
                      value={instance.group_id === null ? SELF_DOMAIN : String(instance.group_id)}
                      onChange={event => {
                        const raw = event.target.value
                        if (raw === SELF_DOMAIN) {
                          bindMutation.mutate({
                            instanceKey: instance.instance_key,
                            groupId: null,
                            migrateAffection: false,
                          })
                          return
                        }
                        // 加入组域一律先过预览对话框：并入与否、并入多少，
                        // 由后端算好摆出来再让人拍板（含组→组，预览会说明为何不搬）
                        const groupId = Number(raw)
                        setPendingJoin({ instance, groupId })
                        joinPreviewMutation.mutate({ instanceKey: instance.instance_key, groupId })
                      }}
                    >
                      <MenuItem value={SELF_DOMAIN}>{t('domain.selfDomain')}</MenuItem>
                      {/* 只列**归属与这个实例相同**的组。
                          清单本身已经按身份收敛（租户看不到别人的组），但那对平台方不够：
                          他看得见全部租户的组，而后端 `assert_instance_joinable` 要求
                          成员与组的归属逐字相等 —— 不过滤的话，平台方那一页上绝大多数
                          组合都是"选了必然失败"的选项，正是本页开头说的不摆点不动的按钮。 */}
                      {data.items
                        .filter(item => item.tenant_id === instance.tenant_id)
                        .map(item => (
                          <MenuItem key={item.group_id} value={String(item.group_id)}>
                            {t('domain.optionLabel', { name: item.name, id: item.group_id })}
                          </MenuItem>
                        ))}
                    </Select>
                  </FormControl>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <NekroDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title={t('domain.createTitle')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setCreateOpen(false)}>
              {t('actions.cancel')}
            </ActionButton>
            <ActionButton
              disabled={!name.trim() || createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              {t('actions.confirm')}
            </ActionButton>
          </>
        }
      >
        <Stack spacing={2} sx={{ pt: 1 }}>
          <Alert severity="info" variant="outlined">
            {t('domain.createHint')}
          </Alert>
          <TextField
            size="small"
            label={t('domain.nameLabel')}
            value={name}
            autoComplete="off"
            onChange={event => setName(event.target.value)}
          />
          <TextField
            size="small"
            label={t('domain.noteLabel')}
            value={note}
            multiline
            minRows={2}
            onChange={event => setNote(event.target.value)}
          />
          {/* 唯一按身份分叉的一格：租户身份下 `can_choose_tenant` 为 false，整格不渲染，
              而不是渲染出来再禁用。候选租户直接取自实例清单 —— 后端已经按身份收敛过，
              所以这里不会出现一个"选了建出来也没用"的租户。 */}
          {data?.can_choose_tenant && (
            <FormControl size="small" fullWidth>
              <InputLabel id="domain-owner-tenant">{t('domain.ownerLabel')}</InputLabel>
              <Select
                labelId="domain-owner-tenant"
                label={t('domain.ownerLabel')}
                // 同上：默认档「平台方」的取值也是空串
                displayEmpty
                value={ownerTenantId === null ? SELF_DOMAIN : String(ownerTenantId)}
                onChange={event => {
                  const raw = String(event.target.value)
                  setOwnerTenantId(raw === SELF_DOMAIN ? null : Number(raw))
                }}
              >
                <MenuItem value={SELF_DOMAIN}>{t('domain.ownerPlatform')}</MenuItem>
                {ownerTenantOptions.map(tenantId => (
                  <MenuItem key={tenantId} value={String(tenantId)}>
                    {t('domain.ownerTenant', { id: tenantId })}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          )}
          {data?.can_choose_tenant && <Alert severity="warning">{t('domain.ownerHint')}</Alert>}
        </Stack>
      </NekroDialog>

      <NekroDialog
        open={Boolean(pendingDelete)}
        onClose={() => setPendingDelete(null)}
        title={t('domain.deleteTitle')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setPendingDelete(null)}>
              {t('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={deleteMutation.isPending}
              onClick={() => pendingDelete && deleteMutation.mutate(pendingDelete.group_id)}
            >
              {t('actions.delete')}
            </ActionButton>
          </>
        }
      >
        {pendingDelete && (
          <Alert severity="warning">
            {t('domain.deleteWarning', {
              name: pendingDelete.name,
              count: pendingDelete.members.length,
            })}
          </Alert>
        )}
      </NekroDialog>

      <NekroDialog
        open={Boolean(pendingJoin)}
        onClose={closeJoinDialog}
        title={t('domain.joinTitle')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={closeJoinDialog}>
              {t('actions.cancel')}
            </ActionButton>
            <ActionButton
              disabled={
                !joinPreviewMutation.data ||
                joinPreviewMutation.isPending ||
                bindMutation.isPending
              }
              onClick={() =>
                pendingJoin &&
                bindMutation.mutate({
                  instanceKey: pendingJoin.instance.instance_key,
                  groupId: pendingJoin.groupId,
                  // 勾选态只在后端允许并入时才有意义，拦截层再放一道
                  migrateAffection:
                    migrateChecked && Boolean(joinPreviewMutation.data?.can_migrate),
                })
              }
            >
              {t('domain.joinConfirm')}
            </ActionButton>
          </>
        }
      >
        {pendingJoin && (
          <Stack spacing={2} sx={{ pt: 1 }}>
            <Typography variant="body2">
              {t('domain.joinLine', {
                instance:
                  pendingJoin.instance.display_name || pendingJoin.instance.instance_key,
                domain:
                  joinPreviewMutation.data?.target_domain_label ??
                  `#${pendingJoin.groupId}`,
              })}
            </Typography>
            {joinPreviewMutation.isPending && (
              <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}>
                <CircularProgress size={24} />
              </Box>
            )}
            {joinPreviewMutation.isError && (
              <Alert severity="error">{errorMessage(joinPreviewMutation.error)}</Alert>
            )}
            {!joinPreviewMutation.isPending && joinPreviewMutation.data && (
              <>
                {joinPreviewMutation.data.blockers.length > 0 && (
                  <Alert severity="warning">
                    <Stack spacing={0.5}>
                      {joinPreviewMutation.data.blockers.map(item => (
                        <Typography key={item} variant="body2">
                          {item}
                        </Typography>
                      ))}
                    </Stack>
                  </Alert>
                )}
                {/* notes 由后端随响应给出，原样展示不复算——与合并预览同一约定 */}
                <Alert severity="info" variant="outlined">
                  <Stack spacing={0.5}>
                    {joinPreviewMutation.data.notes.map(item => (
                      <Typography key={item} variant="body2">
                        {item}
                      </Typography>
                    ))}
                  </Stack>
                </Alert>
                {/* 并入只在「自成一域 → 组域」这一个方向上合法；已在组域时
                    后端给 blocker，这里置灰而不是藏——让人看见"有这回事、为什么不行" */}
                <FormControlLabel
                  control={
                    <Checkbox
                      checked={migrateChecked && joinPreviewMutation.data.can_migrate}
                      disabled={!joinPreviewMutation.data.can_migrate}
                      onChange={event => setMigrateChecked(event.target.checked)}
                    />
                  }
                  label={t('domain.migrateLabel')}
                />
              </>
            )}
          </Stack>
        )}
      </NekroDialog>
    </Stack>
  )
}

/** 页签二：账号合并（先预览后执行、可撤销） */
function MergeTab({ instanceKey, readOnly }: { instanceKey: string; readOnly: boolean }) {
  const { t } = useTranslation('console-identity')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const [aliasUserid, setAliasUserid] = useState('')
  const [personUserid, setPersonUserid] = useState('')
  const [note, setNote] = useState('')
  /**
   * 预览结果，**连同产生它的那一份请求一起存下来**
   *
   * 执行必须发 `preview.request`，不能重新读表单：表单在预览之后还能改，
   * 而合并是"把两个人认成同一个人"——照着 A→B 的预览把 C→D 合进去，
   * 界面上一个字都不会变，撤销回来的分数也不是原来那一份
   * （中间正常涨跌的部分留在错误的那个人身上）。
   * 顶上的实例作用域同理：它一变，域就变了，同一对账号是另一份数据。
   */
  const [preview, setPreview] = useState<{
    request: MergeRequestBody
    data: MergePreviewResponse
  } | null>(null)
  const [pendingRevoke, setPendingRevoke] = useState<AliasItem | null>(null)
  // 待审批的自助申报：点开批准/拒绝时连「要批哪张、批语是什么」一起存，
  // 与预览同理——发出去的必须是看见的那一份
  const [reviewTarget, setReviewTarget] = useState<{
    request: AliasRequestItem
    action: 'approve' | 'reject'
  } | null>(null)
  const [reviewNote, setReviewNote] = useState('')

  // 任何一格改动都让预览作废：宁可让人多点一次预览，也不能让"看到的"与"要发的"不是同一件事
  useEffect(() => {
    setPreview(null)
  }, [instanceKey, aliasUserid, personUserid, note])

  const aliasesQuery = useQuery({
    queryKey: IDENTITY_QUERY_KEYS.aliases(instanceKey),
    queryFn: () => identityApi.listAliases(instanceKey),
    enabled: Boolean(instanceKey),
    retry: false,
  })

  const requestsQuery = useQuery({
    queryKey: IDENTITY_QUERY_KEYS.aliasRequests(instanceKey),
    queryFn: () => identityApi.listAliasRequests(instanceKey),
    enabled: Boolean(instanceKey),
    retry: false,
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: IDENTITY_QUERY_KEYS.aliases(instanceKey) })
    void queryClient.invalidateQueries({ queryKey: IDENTITY_QUERY_KEYS.aliasRequests(instanceKey) })
  }

  const body = (): MergeRequestBody => ({
    instance_key: instanceKey,
    alias_userid: aliasUserid.trim(),
    person_userid: personUserid.trim(),
    note: note.trim(),
  })

  const previewMutation = useMutation({
    mutationFn: async () => {
      const request = body()
      return { request, data: await identityApi.previewMerge(request) }
    },
    onSuccess: setPreview,
    onError: (error: unknown) => notification.error(errorMessage(error)),
  })

  const mergeMutation = useMutation({
    // 入参而不是闭包：发出去的就是预览过的那一份
    mutationFn: (request: MergeRequestBody) => identityApi.executeMerge(request),
    onSuccess: result => {
      notification.success(
        t('merge.done', {
          alias: result.alias_platform_userid,
          person: result.person_uid,
          delta: result.total_delta,
        })
      )
      setPreview(null)
      setAliasUserid('')
      setPersonUserid('')
      setNote('')
      invalidate()
    },
    onError: (error: unknown) => notification.error(errorMessage(error)),
  })

  const revokeMutation = useMutation({
    mutationFn: (alias: AliasItem) => identityApi.revokeMerge(alias.id, instanceKey),
    onSuccess: result => {
      notification.success(
        t('merge.revoked', {
          alias: result.alias_platform_userid,
          delta: -result.total_delta,
        })
      )
      setPendingRevoke(null)
      invalidate()
    },
    onError: (error: unknown) => notification.error(errorMessage(error)),
  })

  const reviewMutation = useMutation({
    mutationFn: async (target: { request: AliasRequestItem; action: 'approve' | 'reject' }) => {
      const body = { instance_key: instanceKey, note: reviewNote.trim() }
      return target.action === 'approve'
        ? identityApi.approveAliasRequest(target.request.id, body)
        : identityApi.rejectAliasRequest(target.request.id, body)
    },
    onSuccess: (_result, target) => {
      notification.success(
        target.action === 'approve'
          ? t('requests.approved', { alias: target.request.alias_platform_userid })
          : t('requests.rejected', { id: target.request.id })
      )
      setReviewTarget(null)
      setReviewNote('')
      invalidate()
    },
    onError: (error: unknown) => notification.error(errorMessage(error)),
  })

  const closeReviewDialog = () => {
    setReviewTarget(null)
    setReviewNote('')
  }

  if (!instanceKey) {
    return <Alert severity="info">{t('needScope')}</Alert>
  }

  return (
    <Stack spacing={2}>
      <Alert severity="info" variant="outlined">
        {t('merge.intro', {
          domain: aliasesQuery.data ? `（${aliasesQuery.data.domain_label}）` : '',
        })}
      </Alert>

      {!readOnly && (
        <Stack spacing={1.5}>
          <SectionTitle title={t('merge.formTitle')} hint={t('merge.formHint')} />
          <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5}>
            <TextField
              size="small"
              label={t('merge.aliasLabel')}
              value={aliasUserid}
              autoComplete="off"
              onChange={event => setAliasUserid(event.target.value)}
              fullWidth
            />
            <TextField
              size="small"
              label={t('merge.personLabel')}
              value={personUserid}
              autoComplete="off"
              onChange={event => setPersonUserid(event.target.value)}
              fullWidth
            />
            <TextField
              size="small"
              label={t('merge.noteLabel')}
              value={note}
              onChange={event => setNote(event.target.value)}
              fullWidth
            />
            <ActionButton
              size="small"
              disabled={!aliasUserid.trim() || !personUserid.trim() || previewMutation.isPending}
              onClick={() => previewMutation.mutate()}
            >
              {t('actions.preview')}
            </ActionButton>
          </Stack>
        </Stack>
      )}

      {preview && (
        <Box
          sx={{
            border: theme => `1px solid ${theme.palette.divider}`,
            borderRadius: 1.5,
            p: 2,
          }}
        >
          <Stack spacing={1.5}>
            <Typography variant="subtitle2" fontWeight={700}>
              {t('merge.previewTitle', { domain: preview.data.domain_label })}
            </Typography>

            {preview.data.blockers.map(item => (
              <Alert key={item} severity="error">
                {item}
              </Alert>
            ))}
            {preview.data.warnings.map(item => (
              <Alert key={item} severity="warning">
                {item}
              </Alert>
            ))}

            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>{t('merge.columns.account')}</TableCell>
                  <TableCell align="right">{t('merge.columns.bestScore')}</TableCell>
                  <TableCell align="right">{t('merge.columns.affectionRows')}</TableCell>
                  <TableCell align="right">{t('merge.columns.eventRows')}</TableCell>
                  <TableCell align="right">{t('merge.columns.interactions')}</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {[
                  { label: t('merge.sideAlias'), facts: preview.data.alias },
                  { label: t('merge.sidePerson'), facts: preview.data.person },
                ].map(row => (
                  <TableRow key={row.label}>
                    <TableCell>
                      {row.label} · {row.facts.platform_userid}
                      {row.facts.nickname ? ` (${row.facts.nickname})` : ''}
                    </TableCell>
                    <TableCell align="right">{row.facts.best_score}</TableCell>
                    <TableCell align="right">{row.facts.affection_rows}</TableCell>
                    <TableCell align="right">{row.facts.event_rows}</TableCell>
                    <TableCell align="right">{row.facts.interaction_total}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>

            <Typography variant="caption" color="text.secondary">
              {t('merge.memoryLine', {
                count: preview.data.domain_memory_paragraphs,
                note: preview.data.memory_scope_note,
              })}
            </Typography>

            {preview.data.transfers.length > 0 && (
              <>
                <Typography variant="subtitle2" fontWeight={700}>
                  {t('merge.transferTitle')}
                </Typography>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>{t('merge.transferColumns.preset')}</TableCell>
                      <TableCell align="right">{t('merge.transferColumns.alias')}</TableCell>
                      <TableCell align="right">{t('merge.transferColumns.person')}</TableCell>
                      <TableCell align="right">{t('merge.transferColumns.delta')}</TableCell>
                      <TableCell align="right">{t('merge.transferColumns.after')}</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {preview.data.transfers.map(transfer => (
                      <TableRow key={transfer.preset_id}>
                        <TableCell>
                          {transfer.preset_name}
                          {!transfer.preset_exists && (
                            <Chip
                              size="small"
                              color="warning"
                              variant="outlined"
                              label={t('merge.presetDeleted')}
                              sx={{ ml: 1 }}
                            />
                          )}
                        </TableCell>
                        <TableCell align="right">{transfer.alias_score}</TableCell>
                        <TableCell align="right">{transfer.person_score}</TableCell>
                        <TableCell align="right">
                          {transfer.delta > 0 ? `+${transfer.delta}` : '-'}
                        </TableCell>
                        <TableCell align="right">
                          <strong>{transfer.score_after}</strong>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </>
            )}

            <Alert severity="info" variant="outlined">
              {preview.data.reversible_note}
            </Alert>

            <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1 }}>
              <ActionButton tone="ghost" size="small" onClick={() => setPreview(null)}>
                {t('actions.cancel')}
              </ActionButton>
              <ActionButton
                size="small"
                disabled={!preview.data.can_merge || mergeMutation.isPending}
                onClick={() => mergeMutation.mutate(preview.request)}
              >
                {t('actions.merge')}
              </ActionButton>
            </Box>
          </Stack>
        </Box>
      )}

      <Divider />

      <SectionTitle title={t('requests.title')} hint={t('requests.hint')} />
      {requestsQuery.isError && (
        <QueryErrorState
          compact
          error={requestsQuery.error}
          onRetry={() => void requestsQuery.refetch()}
        />
      )}
      {requestsQuery.data && requestsQuery.data.items.length === 0 && (
        <Alert severity="success" variant="outlined">
          {t('requests.empty')}
        </Alert>
      )}
      {requestsQuery.data && requestsQuery.data.items.length > 0 && (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>{t('requests.columns.reporter')}</TableCell>
              <TableCell>{t('requests.columns.claimed')}</TableCell>
              <TableCell>{t('requests.columns.time')}</TableCell>
              {!readOnly && (
                <TableCell align="right">{t('requests.columns.actions')}</TableCell>
              )}
            </TableRow>
          </TableHead>
          <TableBody>
            {requestsQuery.data.items.map(item => (
              <TableRow key={item.id}>
                <TableCell>{item.reporter_userid}</TableCell>
                <TableCell>
                  {item.alias_platform_userid}
                  {item.note && (
                    <Typography variant="caption" color="text.secondary" display="block">
                      {item.note}
                    </Typography>
                  )}
                </TableCell>
                <TableCell>
                  <Typography variant="caption" color="text.secondary">
                    {item.create_time ? item.create_time.slice(0, 19).replace('T', ' ') : '-'}
                  </Typography>
                </TableCell>
                {!readOnly && (
                  <TableCell align="right">
                    <Stack direction="row" spacing={0.5} justifyContent="flex-end">
                      <ActionButton
                        size="small"
                        onClick={() => setReviewTarget({ request: item, action: 'approve' })}
                      >
                        {t('requests.approve')}
                      </ActionButton>
                      <ActionButton
                        tone="danger"
                        size="small"
                        onClick={() => setReviewTarget({ request: item, action: 'reject' })}
                      >
                        {t('requests.reject')}
                      </ActionButton>
                    </Stack>
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Divider />

      <SectionTitle title={t('merge.aliasesTitle')} hint={t('merge.aliasesHint')} />
      {aliasesQuery.isLoading && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 3 }}>
          <CircularProgress size={24} />
        </Box>
      )}
      {aliasesQuery.isError && (
        <QueryErrorState
          compact
          error={aliasesQuery.error}
          onRetry={() => void aliasesQuery.refetch()}
        />
      )}
      {aliasesQuery.data && aliasesQuery.data.items.length === 0 && (
        <Alert severity="success">{t('merge.aliasesEmpty')}</Alert>
      )}
      {aliasesQuery.data && aliasesQuery.data.items.length > 0 && (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>{t('merge.aliasColumns.alias')}</TableCell>
              <TableCell>{t('merge.aliasColumns.person')}</TableCell>
              <TableCell>{t('merge.aliasColumns.source')}</TableCell>
              <TableCell>{t('merge.aliasColumns.operator')}</TableCell>
              <TableCell align="right">{t('merge.aliasColumns.actions')}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {aliasesQuery.data.items.map(item => (
              <TableRow key={item.id}>
                <TableCell>{item.platform_userid}</TableCell>
                <TableCell>{item.person_uid}</TableCell>
                <TableCell>
                  <Chip
                    size="small"
                    variant="outlined"
                    label={
                      item.source === 'self_report'
                        ? t('merge.sourceSelfReport')
                        : t('merge.sourceManual')
                    }
                  />
                </TableCell>
                <TableCell>{item.operator || '-'}</TableCell>
                <TableCell align="right">
                  {!readOnly && (
                    <ActionButton tone="danger" size="small" onClick={() => setPendingRevoke(item)}>
                      {t('actions.revoke')}
                    </ActionButton>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <NekroDialog
        open={Boolean(pendingRevoke)}
        onClose={() => setPendingRevoke(null)}
        title={t('merge.revokeTitle')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setPendingRevoke(null)}>
              {t('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={revokeMutation.isPending}
              onClick={() => pendingRevoke && revokeMutation.mutate(pendingRevoke)}
            >
              {t('actions.revoke')}
            </ActionButton>
          </>
        }
      >
        {pendingRevoke && (
          <Alert severity="warning">
            {t('merge.revokeWarning', {
              alias: pendingRevoke.platform_userid,
              person: pendingRevoke.person_uid,
            })}
          </Alert>
        )}
      </NekroDialog>

      <NekroDialog
        open={Boolean(reviewTarget)}
        onClose={closeReviewDialog}
        title={
          reviewTarget?.action === 'approve'
            ? t('requests.approveTitle')
            : t('requests.rejectTitle')
        }
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={closeReviewDialog}>
              {t('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone={reviewTarget?.action === 'reject' ? 'danger' : 'primary'}
              disabled={reviewMutation.isPending}
              onClick={() => reviewTarget && reviewMutation.mutate(reviewTarget)}
            >
              {reviewTarget?.action === 'approve'
                ? t('requests.approve')
                : t('requests.reject')}
            </ActionButton>
          </>
        }
      >
        {reviewTarget && (
          <Stack spacing={2} sx={{ pt: 1 }}>
            <Alert severity={reviewTarget.action === 'approve' ? 'info' : 'warning'}>
              {reviewTarget.action === 'approve'
                ? t('requests.approveLine', {
                    alias: reviewTarget.request.alias_platform_userid,
                    reporter: reviewTarget.request.reporter_userid,
                  })
                : t('requests.rejectLine', {
                    alias: reviewTarget.request.alias_platform_userid,
                    reporter: reviewTarget.request.reporter_userid,
                  })}
            </Alert>
            <TextField
              size="small"
              label={t('requests.reviewNoteLabel')}
              value={reviewNote}
              multiline
              minRows={2}
              onChange={event => setReviewNote(event.target.value)}
            />
          </Stack>
        )}
      </NekroDialog>
    </Stack>
  )
}

/** 页签三：人设孤儿数据 */
function OrphanTab({ instanceKey, readOnly }: { instanceKey: string; readOnly: boolean }) {
  const { t } = useTranslation('console-identity')
  const notification = useNotification()
  const queryClient = useQueryClient()
  const [pending, setPending] = useState<OrphanBucketItem | null>(null)

  const orphansQuery = useQuery({
    queryKey: IDENTITY_QUERY_KEYS.orphans(instanceKey),
    queryFn: () => identityApi.listOrphans(instanceKey),
    enabled: Boolean(instanceKey),
    retry: false,
  })

  const cleanupMutation = useMutation({
    mutationFn: (bucket: OrphanBucketItem) =>
      identityApi.cleanupOrphans({
        instance_key: instanceKey,
        preset_id: bucket.preset_id,
        domain_id: bucket.domain_id,
      }),
    onSuccess: result => {
      notification.success(
        t('orphan.cleaned', { rows: result.affection, events: result.affection_event })
      )
      setPending(null)
      void queryClient.invalidateQueries({ queryKey: IDENTITY_QUERY_KEYS.orphans(instanceKey) })
    },
    onError: (error: unknown) => notification.error(errorMessage(error)),
  })

  if (!instanceKey) {
    return <Alert severity="info">{t('needScope')}</Alert>
  }

  return (
    <Stack spacing={2}>
      {orphansQuery.data && (
        <Alert severity="info" variant="outlined">
          {orphansQuery.data.policy_note}
        </Alert>
      )}

      {orphansQuery.isLoading && (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={28} />
        </Box>
      )}
      {orphansQuery.isError && (
        <QueryErrorState
          compact
          error={orphansQuery.error}
          onRetry={() => void orphansQuery.refetch()}
        />
      )}
      {orphansQuery.data && orphansQuery.data.items.length === 0 && (
        <Alert severity="success">{t('orphan.empty')}</Alert>
      )}

      {orphansQuery.data && orphansQuery.data.items.length > 0 && (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>{t('orphan.columns.preset')}</TableCell>
              <TableCell>{t('orphan.columns.domain')}</TableCell>
              <TableCell align="right">{t('orphan.columns.affectionRows')}</TableCell>
              <TableCell align="right">{t('orphan.columns.people')}</TableCell>
              <TableCell align="right">{t('orphan.columns.eventRows')}</TableCell>
              <TableCell align="right">{t('orphan.columns.actions')}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {orphansQuery.data.items.map(item => (
              <TableRow key={`${item.domain_id}:${item.preset_id}`}>
                <TableCell>#{item.preset_id}</TableCell>
                <TableCell>{item.domain_label}</TableCell>
                <TableCell align="right">{item.affection_rows}</TableCell>
                <TableCell align="right">{item.people}</TableCell>
                <TableCell align="right">{item.event_rows}</TableCell>
                <TableCell align="right">
                  {!readOnly && (
                    <ActionButton tone="danger" size="small" onClick={() => setPending(item)}>
                      {t('actions.cleanup')}
                    </ActionButton>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <NekroDialog
        open={Boolean(pending)}
        onClose={() => setPending(null)}
        title={t('orphan.cleanupTitle')}
        maxWidth="sm"
        actions={
          <>
            <ActionButton tone="ghost" onClick={() => setPending(null)}>
              {t('actions.cancel')}
            </ActionButton>
            <ActionButton
              tone="danger"
              disabled={cleanupMutation.isPending}
              onClick={() => pending && cleanupMutation.mutate(pending)}
            >
              {t('actions.cleanup')}
            </ActionButton>
          </>
        }
      >
        {pending && (
          <Alert severity="warning">
            {t('orphan.cleanupWarning', {
              preset: pending.preset_id,
              rows: pending.affection_rows,
              people: pending.people,
              events: pending.event_rows,
            })}
          </Alert>
        )}
      </NekroDialog>
    </Stack>
  )
}

export default function IdentityDomainPage() {
  const { t } = useTranslation('console-identity')
  const { isReadOnly } = useIdentity()
  const { currentInstanceKey, instances, isScopeReady } = useInstanceScope()
  const [tab, setTab] = useState(0)

  // 平台方在全局视图下 `currentInstanceKey` 为 null，而合并与孤儿这两个页签必须落在
  // 一个具体的域上。这里退回第一个实例作为默认作用域，而不是把两个页签整块禁掉 ——
  // 「点不动的按钮」正是本项目栽过的那个形态。
  const fallbackInstanceKey = useMemo(() => instances[0]?.instance_key ?? '', [instances])
  const [scopeInstanceKey, setScopeInstanceKey] = useState('')

  useEffect(() => {
    if (!isScopeReady) return
    setScopeInstanceKey(previous => previous || currentInstanceKey || fallbackInstanceKey)
  }, [isScopeReady, currentInstanceKey, fallbackInstanceKey])

  useEffect(() => {
    if (currentInstanceKey) setScopeInstanceKey(currentInstanceKey)
  }, [currentInstanceKey])

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2, md: 3 },
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        overflow: 'auto',
        boxSizing: 'border-box',
      }}
    >
      <Box>
        <Typography variant="h6" fontWeight={700}>
          {t('title')}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {t('subtitle')}
        </Typography>
      </Box>

      {isReadOnly && <Alert severity="info">{t('readOnlyHint')}</Alert>}

      <Tabs value={tab} onChange={(_event, next: number) => setTab(next)} variant="scrollable">
        <Tab label={t('tabs.domains')} />
        <Tab label={t('tabs.merge')} />
        <Tab label={t('tabs.orphans')} />
      </Tabs>

      {tab !== 0 && instances.length > 1 && (
        <FormControl size="small" sx={{ maxWidth: 320 }}>
          <InputLabel id="identity-scope">{t('scopePicker')}</InputLabel>
          <Select
            labelId="identity-scope"
            label={t('scopePicker')}
            value={scopeInstanceKey}
            onChange={event => setScopeInstanceKey(String(event.target.value))}
          >
            {instances.map(instance => (
              <MenuItem key={instance.instance_key} value={instance.instance_key}>
                {instance.display_name || instance.instance_key}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      )}

      {tab === 0 && <DomainTab readOnly={isReadOnly} />}
      {tab === 1 && <MergeTab instanceKey={scopeInstanceKey} readOnly={isReadOnly} />}
      {tab === 2 && <OrphanTab instanceKey={scopeInstanceKey} readOnly={isReadOnly} />}
    </Box>
  )
}
