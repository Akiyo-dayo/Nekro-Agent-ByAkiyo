import { useTranslation } from 'react-i18next'
import StatusChip from '../common/StatusChip'

/**
 * 预设归属
 *
 * 全局预设是平台方维护、各实例共用的一份；实例预设只属于当前实例。颜色只表达分类：
 * 本实例用「生效」的绿色，全局用中性的次要文字色，不把全局当成「已停用」。
 * 全局预设对租户只读，锁图标单独跟在名称后面（见列表），不在这里再叠一个状态标签。
 */
export default function PresetOwnerChip({ global }: { global: boolean }) {
  const { t } = useTranslation('tavern')
  return (
    <StatusChip
      tone={global ? 'inactive' : 'active'}
      label={global ? t('lab.presets.owner.global') : t('lab.presets.owner.instance')}
    />
  )
}
