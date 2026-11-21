import { Avatar, type AvatarProps } from '@mui/material'
import { Person as PersonIcon } from '@mui/icons-material'

export interface PresetAvatarProps extends Omit<AvatarProps, 'src' | 'alt'> {
  /** 头像地址；空串表示这张卡/人设没有头像 */
  src?: string
  /** 用于 `alt` 的文字；没有头像时不渲染 `img`，也就没有 `alt` */
  name?: string
}

/**
 * 人设头像
 *
 * 统一处理「没有头像」这一档：`src` 为空串时不传给 `Avatar`，改渲染一个人形占位。
 * 直接写 `<Avatar src={preset.avatar} />` 在 `avatar === ''` 时并不会发请求（MUI 认为
 * 空串就是没图），但渲染出来的是它自带的默认底图，和「新建人设、还没传头像」那一处的
 * 占位不是同一个东西，两侧看起来会像两种状态。导入的酒馆卡经常没有可用头像（卡里既没有
 * 内嵌图片、也没有 `data:` 数据），所以这条路径要显式照顾。
 *
 * `src` 非空但加载失败时由 `Avatar` 自己回落到同一个占位——`children` 只在没有可用图片
 * 时才渲染，这正是我们要的效果。
 */
export default function PresetAvatar({ src, name, ...props }: PresetAvatarProps) {
  const usable = typeof src === 'string' && src.trim() !== ''
  return (
    <Avatar src={usable ? src : undefined} alt={usable ? name : undefined} {...props}>
      {usable ? null : <PersonIcon sx={{ width: '60%', height: '60%', opacity: 0.7 }} />}
    </Avatar>
  )
}
