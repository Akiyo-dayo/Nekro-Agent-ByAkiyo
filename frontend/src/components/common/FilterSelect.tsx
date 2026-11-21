import { FormControl, FormControlProps, InputLabel, MenuItem, Select } from '@mui/material'

export interface FilterSelectOption {
  value: string
  label: string
}

interface FilterSelectProps extends Omit<FormControlProps, 'onChange'> {
  label?: string
  value: string
  options: FilterSelectOption[]
  onChange: (value: string) => void
  /**
   * 值为空串时也显示对应选项的文字
   *
   * MUI 的 `Select` 在值为空串时收起状态什么都不画，于是「频道当前的主模型组」这种用空串
   * 表示的默认项看起来像没选。打开后收起状态照选项文字显示，标签固定缩到上方免得与文字
   * 重叠。默认关：别处的空串多半是「未筛选」，空着正是想要的样子。
   */
  showEmptyOption?: boolean
}

export default function FilterSelect({
  label,
  value,
  options,
  onChange,
  size = 'small',
  fullWidth = true,
  showEmptyOption = false,
  ...props
}: FilterSelectProps) {
  return (
    <FormControl {...props} size={size} fullWidth={fullWidth}>
      {label ? <InputLabel shrink={showEmptyOption ? true : undefined}>{label}</InputLabel> : null}
      <Select
        value={value}
        label={label || undefined}
        onChange={event => onChange(String(event.target.value))}
        displayEmpty={showEmptyOption}
        notched={showEmptyOption && label ? true : undefined}
        renderValue={
          showEmptyOption
            ? selected => options.find(option => option.value === selected)?.label ?? String(selected)
            : undefined
        }
      >
        {options.map(option => (
          <MenuItem key={option.value} value={option.value}>
            {option.label}
          </MenuItem>
        ))}
      </Select>
    </FormControl>
  )
}
