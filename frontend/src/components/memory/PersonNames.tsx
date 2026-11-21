import { Box, Chip, Stack, Typography, useTheme } from '@mui/material'
import { useTranslation } from 'react-i18next'
import { SimpleTooltip } from '../common/ThemedTooltip'
import { CHIP_VARIANTS } from '../../theme/variants'
import type { PersonLabel } from '../../services/api/memory'
import { personPrimaryName, personTooltipLine } from './personLabels'

/**
 * 记忆里「涉及的人」的显示
 *
 * 名字在前、账号放进悬停提示：列表里一串号码认不出是谁，而号码在排查时又必须拿得到。
 * 称呼（好感里模型给这个人记的）是不可信内容，这里只当纯文本放进 Chip，不做任何解析。
 */

interface PersonNameProps {
  person: PersonLabel
  /** 紧凑模式只画名字与称呼；宽松模式在后面再跟一个灰色账号 */
  dense?: boolean
}

/** 单个人：名字 +（称呼标签） */
export function PersonName({ person, dense = true }: PersonNameProps) {
  const theme = useTheme()
  const { t } = useTranslation('memory')
  const callName = person.call_name.trim()
  return (
    <SimpleTooltip title={personTooltipLine(person, t('persons.callName'), t('persons.fromAffection'))}>
      <Stack direction="row" spacing={0.5} alignItems="center" sx={{ minWidth: 0 }}>
        <Typography variant="body2" noWrap sx={{ minWidth: 0 }}>
          {personPrimaryName(person)}
        </Typography>
        {callName && (
          <Chip
            size="small"
            label={callName}
            sx={{
              ...CHIP_VARIANTS.getCustomColorChip(theme.palette.secondary.main, true),
              maxWidth: 96,
            }}
          />
        )}
        {!dense && person.display_name.trim() && (
          <Typography variant="caption" color="text.secondary" noWrap>
            {person.account}
          </Typography>
        )}
      </Stack>
    </SimpleTooltip>
  )
}

interface PersonNamesProps {
  persons: PersonLabel[]
  /** 最多直接显示几个人，其余收成「+N」 */
  max?: number
}

/** 一段记忆的全部人：前几个直接显示，其余收成「+N」并放进悬停提示 */
export default function PersonNames({ persons, max = 2 }: PersonNamesProps) {
  const { t } = useTranslation('memory')
  if (persons.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        —
      </Typography>
    )
  }
  const shown = persons.slice(0, max)
  const rest = persons.slice(max)
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25, minWidth: 0 }}>
      {shown.map(person => (
        <PersonName key={person.account} person={person} />
      ))}
      {rest.length > 0 && (
        <SimpleTooltip
          title={
            <Box>
              {rest.map(person => (
                <Box key={person.account}>
                  {personTooltipLine(person, t('persons.callName'), t('persons.fromAffection'))}
                </Box>
              ))}
            </Box>
          }
        >
          <Typography variant="caption" color="text.secondary" sx={{ cursor: 'default' }}>
            {t('persons.more', { count: rest.length })}
          </Typography>
        </SimpleTooltip>
      )}
    </Box>
  )
}
