import { TextField } from '@mui/material'

/**
 * JSON 编辑框
 *
 * 只做「多行、等宽、只读」这三件事：状态树的结构由模板定义，这里不引编辑器，
 * 也不做格式化以外的加工 —— 用户写进来的每一段都必须原样送去解析。
 */
export interface JsonEditorFieldProps {
  value: string
  onChange?: (value: string) => void
  readOnly?: boolean
  /** 解析失败之类的说明，显示在输入框下面 */
  error?: string
  label?: string
  minRows?: number
  maxRows?: number
}

export default function JsonEditorField({
  value,
  onChange,
  readOnly = false,
  error,
  label,
  minRows = 10,
  maxRows = 24,
}: JsonEditorFieldProps) {
  return (
    <TextField
      fullWidth
      multiline
      size="small"
      label={label}
      value={value}
      onChange={event => onChange?.(event.target.value)}
      error={Boolean(error)}
      helperText={error}
      minRows={minRows}
      maxRows={maxRows}
      InputProps={{
        readOnly,
        sx: {
          fontFamily: 'monospace',
          fontSize: '0.8rem',
          lineHeight: 1.6,
          alignItems: 'flex-start',
        },
      }}
    />
  )
}
