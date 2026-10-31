import { Button, ButtonProps, ButtonTypeMap, SxProps, Theme } from '@mui/material'
import type { OverridableComponent } from '@mui/material/OverridableComponent'
import { ElementType, forwardRef } from 'react'
import { ACTION_BUTTON_VARIANTS } from '../../theme/variants'

export type ActionButtonTone = 'primary' | 'secondary' | 'ghost' | 'danger'

/**
 * `color` 由 `tone` 取代：tone 的 sx 会完整覆盖 MUI 的 color 变体样式，
 * 同时保留 MUI 的多态能力（`component={RouterLink} to=...` 之类）。
 */
export interface ActionButtonTypeMap<
  AdditionalProps = object,
  RootComponent extends ElementType = 'button',
> {
  props: Omit<ButtonTypeMap<AdditionalProps, RootComponent>['props'], 'color'> & {
    tone?: ActionButtonTone
  }
  defaultComponent: RootComponent
}

const DEFAULT_VARIANT: Record<ActionButtonTone, ButtonProps['variant']> = {
  primary: 'contained',
  secondary: 'outlined',
  ghost: 'text',
  danger: 'outlined',
}

const ActionButton = forwardRef<
  HTMLButtonElement,
  ActionButtonTypeMap['props'] & { component?: ElementType }
>(function ActionButton({ tone = 'secondary', variant, sx, ...props }, ref) {
  const mergedSx = (
    sx
      ? [ACTION_BUTTON_VARIANTS[tone].styles, sx]
      : ACTION_BUTTON_VARIANTS[tone].styles
  ) as SxProps<Theme>

  return (
    <Button
      {...props}
      ref={ref}
      variant={variant ?? DEFAULT_VARIANT[tone]}
      sx={mergedSx}
    />
  )
}) as OverridableComponent<ActionButtonTypeMap>

export default ActionButton
