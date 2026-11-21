import { useState } from 'react'
import {
  Box,
  ButtonBase,
  ListSubheader,
  Menu,
  MenuItem,
  Typography,
  useMediaQuery,
  useTheme,
  type SxProps,
  type Theme,
} from '@mui/material'
import ExpandMoreRoundedIcon from '@mui/icons-material/ExpandMoreRounded'
import NorthEastRoundedIcon from '@mui/icons-material/NorthEastRounded'
import { useTranslation } from 'react-i18next'
import ActionButton from '../../../components/common/ActionButton'
import { ThemedTooltip } from '../../../components/common/ThemedTooltip'
import type { DomainSettingsDefinition } from '../../../config/domainSettings'
import { CATEGORY_NAV_VARIANTS } from '../../../theme/variants'

export interface CategoryNavItem {
  /** 本地化后的分类名，也是 `?category=` 里的取值 */
  category: string
  count: number
  /** 已搬到功能页「设置」抽屉的分类：点了直接去那一页 */
  domain?: DomainSettingsDefinition
}

export interface CategoryNavGroup {
  key: string
  title: string
  items: CategoryNavItem[]
}

interface SettingsCategoryNavProps {
  groups: CategoryNavGroup[]
  /** 当前分类；搜索时为 null（结果跨全部分类） */
  activeCategory: string | null
  onSelect: (category: string) => void
  onOpenDomain: (domain: DomainSettingsDefinition) => void
}

/**
 * 系统配置页的分组分类导航
 *
 * 宽屏：一块面板里按组排开，每组一行「组名 + 分类」，所有分类一屏看全、不横向滚动。
 * 窄屏：收成一个按钮，点开是按组分节的菜单，免得导航本身占掉半屏。
 */
export default function SettingsCategoryNav({
  groups,
  activeCategory,
  onSelect,
  onOpenDomain,
}: SettingsCategoryNavProps) {
  const theme = useTheme()
  const isCompact = useMediaQuery(theme.breakpoints.down('md'))
  const { t } = useTranslation('settings')
  const { t: tDomainSettings } = useTranslation('domain-settings')
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)

  const handlePick = (item: CategoryNavItem) => {
    setMenuAnchor(null)
    if (item.domain) {
      onOpenDomain(item.domain)
      return
    }
    onSelect(item.category)
  }

  const domainHint = (domain: DomainSettingsDefinition) =>
    t('system.nav.domainHint', { page: tDomainSettings(`page.${domain.key}`) })

  if (isCompact) {
    const activeGroup = groups.find(group =>
      group.items.some(item => item.category === activeCategory)
    )
    return (
      <>
        <ActionButton
          tone="secondary"
          fullWidth
          endIcon={<ExpandMoreRoundedIcon />}
          onClick={event => setMenuAnchor(event.currentTarget)}
          aria-haspopup="menu"
          sx={{ justifyContent: 'space-between' }}
        >
          <Box
            component="span"
            sx={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
          >
            {activeCategory && activeGroup
              ? `${activeGroup.title} · ${activeCategory}`
              : t('system.nav.searching')}
          </Box>
        </ActionButton>
        <Menu
          anchorEl={menuAnchor}
          open={menuAnchor !== null}
          onClose={() => setMenuAnchor(null)}
          slotProps={{
            paper: { sx: { width: menuAnchor?.clientWidth, maxWidth: '100%', maxHeight: '70vh' } },
          }}
        >
          {groups.flatMap(group => [
            <ListSubheader key={`group:${group.key}`} sx={{ lineHeight: '32px' }}>
              {group.title}
            </ListSubheader>,
            ...group.items.map(item => (
              <MenuItem
                key={item.category}
                selected={item.category === activeCategory}
                onClick={() => handlePick(item)}
                sx={{ gap: 1 }}
              >
                <Box component="span" sx={{ flex: 1, minWidth: 0 }}>
                  {item.category}
                </Box>
                {item.domain ? (
                  <NorthEastRoundedIcon fontSize="inherit" sx={{ opacity: 0.6 }} />
                ) : (
                  <Typography variant="caption" color="text.secondary">
                    {item.count}
                  </Typography>
                )}
              </MenuItem>
            )),
          ])}
        </Menu>
      </>
    )
  }

  return (
    <Box
      component="nav"
      aria-label={t('system.nav.label')}
      sx={[
        CATEGORY_NAV_VARIANTS.panel,
        {
          px: 1.5,
          py: 1,
          display: 'grid',
          // 两栏放得下就两栏；中等宽度（侧栏展开）退成一栏，各组仍各占一行
          gridTemplateColumns: 'repeat(auto-fill, minmax(380px, 1fr))',
          columnGap: 3,
          rowGap: 0.5,
        },
      ] as SxProps<Theme>}
    >
      {groups.map(group => (
        <Box key={group.key} sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, minWidth: 0 }}>
          <Typography component="div" sx={CATEGORY_NAV_VARIANTS.groupTitle}>
            {group.title}
          </Typography>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, minWidth: 0 }}>
            {group.items.map(item => {
              const button = (
                <ButtonBase
                  key={item.category}
                  aria-current={item.category === activeCategory ? 'true' : undefined}
                  onClick={() => handlePick(item)}
                  sx={CATEGORY_NAV_VARIANTS.item}
                >
                  <span>{item.category}</span>
                  {item.domain ? (
                    <NorthEastRoundedIcon className="category-nav-icon" />
                  ) : (
                    <span className="category-nav-count">{item.count}</span>
                  )}
                </ButtonBase>
              )
              return item.domain ? (
                <ThemedTooltip key={item.category} title={domainHint(item.domain)}>
                  {button}
                </ThemedTooltip>
              ) : (
                button
              )
            })}
          </Box>
        </Box>
      ))}
    </Box>
  )
}
