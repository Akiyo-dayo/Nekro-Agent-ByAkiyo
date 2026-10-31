/**
 * 系统配置 → 模型管理 → 基础模型组
 *
 * 模型组只有一份实现（`components/model-group/ModelGroupManager`），本页与总后台的
 * 「模型组」渲染的是同一个组件：两处读写的本来就是同一张 `model_group` 表，
 * 分成两套列表与两套新建表单只会互相漂移。
 */

import ModelGroupManager from '../../components/model-group/ModelGroupManager'

export default function ModelGroupsPage() {
  return <ModelGroupManager showProviderHint />
}
