import { useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Stack,
  Switch,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Tooltip,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSnackbar } from 'notistack'
import { useTranslation } from 'react-i18next'
import { chatChannelApi } from '../../../../services/api/chat-channel'
import { CHIP_VARIANTS, UNIFIED_TABLE_STYLES } from '../../../../theme/variants'

interface CommandPermissionsProps {
  chatKey: string
}

export default function CommandPermissions({ chatKey }: CommandPermissionsProps) {
  const theme = useTheme()
  const isSmall = useMediaQuery(theme.breakpoints.down('sm'))
  const { enqueueSnackbar } = useSnackbar()
  const queryClient = useQueryClient()
  const { t } = useTranslation('chat-channel')
  const [grantId, setGrantId] = useState('')

  const queryKey = ['chat-channel-command-permissions', chatKey]

  const { data, isLoading, isError } = useQuery({
    queryKey,
    queryFn: () => chatChannelApi.getUsers(chatKey),
  })

  const notifyUpdated = () => {
    enqueueSnackbar(t('commandPermissions.messages.updated'), { variant: 'success' })
    queryClient.invalidateQueries({ queryKey })
  }

  const {
    mutate: updatePermission,
    isPending: isUpdating,
    variables: updatingVariables,
  } = useMutation({
    mutationFn: ({
      platformUserId,
      permission,
    }: {
      platformUserId: string
      permission: 'advanced' | 'user'
    }) => chatChannelApi.setUserCommandPermission(chatKey, platformUserId, permission),
    onSuccess: notifyUpdated,
    onError: () => {
      enqueueSnackbar(t('commandPermissions.messages.updateFailed'), { variant: 'error' })
    },
  })

  const { mutate: grantPermission, isPending: isGranting } = useMutation({
    mutationFn: (platformUserId: string) =>
      chatChannelApi.setUserCommandPermission(chatKey, platformUserId, 'advanced'),
    onSuccess: () => {
      setGrantId('')
      notifyUpdated()
    },
    onError: () => {
      enqueueSnackbar(t('commandPermissions.messages.updateFailed'), { variant: 'error' })
    },
  })

  const users = data?.items ?? []

  const handleGrantById = () => {
    const platformUserId = grantId.trim()
    if (!platformUserId) return
    grantPermission(platformUserId)
  }

  return (
    <Box
      sx={{
        p: { xs: 1.5, sm: 2 },
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 1.5,
      }}
    >
      <Alert severity="info" sx={{ flexShrink: 0 }}>
        {t('commandPermissions.hint')}
      </Alert>

      <Stack
        direction={{ xs: 'column', sm: 'row' }}
        spacing={1}
        alignItems={{ xs: 'stretch', sm: 'center' }}
        sx={{ flexShrink: 0 }}
      >
        <TextField
          size="small"
          placeholder={t('commandPermissions.grantById.placeholder')}
          value={grantId}
          onChange={event => setGrantId(event.target.value)}
          sx={{ width: { xs: '100%', sm: 260 } }}
        />
        <Button
          variant="contained"
          onClick={handleGrantById}
          disabled={isGranting || !grantId.trim()}
          sx={{ whiteSpace: 'nowrap' }}
        >
          {isGranting ? (
            <CircularProgress size={20} color="inherit" />
          ) : (
            t('commandPermissions.grantById.button')
          )}
        </Button>
      </Stack>

      {isLoading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={24} />
        </Box>
      ) : isError ? (
        <Alert severity="error">{t('commandPermissions.loadError')}</Alert>
      ) : users.length === 0 ? (
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            py: 4,
          }}
        >
          <Typography variant="body2" color="text.secondary">
            {t('commandPermissions.empty')}
          </Typography>
        </Box>
      ) : (
        <TableContainer sx={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
          <Table
            stickyHeader
            size={isSmall ? 'small' : 'medium'}
            sx={UNIFIED_TABLE_STYLES.getTableBase(isSmall, isSmall)}
          >
            <TableHead>
              <TableRow>
                <TableCell sx={{ ...(UNIFIED_TABLE_STYLES.header as object), width: '40%' }}>
                  {t('commandPermissions.columns.user')}
                </TableCell>
                <TableCell sx={{ ...(UNIFIED_TABLE_STYLES.header as object), width: '18%' }}>
                  {t('commandPermissions.columns.permission')}
                </TableCell>
                <TableCell sx={{ ...(UNIFIED_TABLE_STYLES.header as object), width: '20%' }}>
                  {t('commandPermissions.columns.source')}
                </TableCell>
                <TableCell sx={{ ...(UNIFIED_TABLE_STYLES.header as object), width: '22%' }}>
                  {t('commandPermissions.columns.channelGrant')}
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {users.map(user => (
                <TableRow key={user.platform_userid} hover sx={UNIFIED_TABLE_STYLES.row}>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                    <Typography variant="body2" noWrap>
                      {user.nickname?.trim() || t('commandPermissions.unknownNickname')}
                    </Typography>
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      sx={{ display: 'block', fontFamily: 'monospace' }}
                    >
                      {user.platform_userid}
                    </Typography>
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                    <Chip
                      label={t(`commandPermissions.permissions.${user.command_permission}`)}
                      size="small"
                      sx={CHIP_VARIANTS.getCommandPermissionChip(user.command_permission, isSmall)}
                    />
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                    {user.command_permission_source
                      ? t(`commandPermissions.sources.${user.command_permission_source}`)
                      : '-'}
                  </TableCell>
                  <TableCell sx={UNIFIED_TABLE_STYLES.cell}>
                    <Tooltip
                      title={
                        user.command_permission === 'super_user' && !user.channel_granted
                          ? t('commandPermissions.superUserLocked')
                          : ''
                      }
                    >
                      <span>
                        <Switch
                          size="small"
                          checked={user.channel_granted}
                          disabled={
                            (user.command_permission === 'super_user' && !user.channel_granted) ||
                            (isUpdating &&
                              updatingVariables?.platformUserId === user.platform_userid)
                          }
                          onChange={event =>
                            updatePermission({
                              platformUserId: user.platform_userid,
                              permission: event.target.checked ? 'advanced' : 'user',
                            })
                          }
                        />
                      </span>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Box>
  )
}
