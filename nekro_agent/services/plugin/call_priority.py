"""插件调用优先级

给插件标一个档位（`auto` / `high` / `medium` / `low`），渲染进提示词里该插件块的 `call_priority` 属性，
再配一段规则告诉模型：功能等价的插件里先用高档、失败再降档。档位**不改变**插件在提示词里的先后，
也不改变任何插件的可见性：被作用域关掉、或本轮没有渲染出块的插件，档位不参与。

全进程只有一份（`CoreConfig.PLUGIN_CALL_PRIORITIES`，不进实例 / 会话覆盖），与激活策略同一档：
只有平台方能改，租户只读。落盘格式与键名与上游一致，`auto` 不落盘。
"""

from typing import Literal, Optional, Tuple

from nekro_agent.core.config import CONFIG_PATH, config
from nekro_agent.services.config_service import ConfigService

PluginCallPriority = Literal["auto", "high", "medium", "low"]


def get_plugin_call_priority(module_name: str) -> PluginCallPriority:
    """获取插件调用优先级，未配置或配置异常时回退为自动。"""
    priority = (config.PLUGIN_CALL_PRIORITIES or {}).get(module_name, "auto")
    if priority not in {"auto", "high", "medium", "low"}:
        return "auto"
    return priority


def set_plugin_call_priority(module_name: str, priority: PluginCallPriority) -> Tuple[bool, Optional[str]]:
    """设置并持久化插件调用优先级；自动策略不保存覆盖项。

    落盘失败时把内存里的取值换回原来那份字典，避免「界面报错、提示词却已经按新档位渲染」。
    """
    previous_priorities = config.PLUGIN_CALL_PRIORITIES
    priorities = dict(previous_priorities or {})
    if priority == "auto":
        priorities.pop(module_name, None)
    else:
        priorities[module_name] = priority

    config.PLUGIN_CALL_PRIORITIES = priorities
    success, error = ConfigService.save_config(config, CONFIG_PATH)
    if not success:
        config.PLUGIN_CALL_PRIORITIES = previous_priorities
    return success, error


def build_plugin_call_priority_rules() -> str:
    """构建供大模型执行的插件调用优先级规则。"""
    return (
        "Plugin call priority is a preference only when multiple enabled plugins "
        "provide equivalent capabilities.\n"
        "- Explicit priorities rank equivalent plugins as high, then medium, then low. Within the same tier, "
        "choose by task fit.\n"
        "- Try the highest applicable explicit-priority plugin first. If that plugin is sleeping and its "
        "capability brief matches the task, activate it before selecting a lower-priority equivalent plugin.\n"
        "- If the selected plugin cannot complete the task, returns an error, cannot be activated, "
        "or lacks required capability or constraints, try the next applicable lower-priority plugin.\n"
        "- Do not redundantly call a lower-priority equivalent plugin after a higher-priority plugin "
        "has completed the task.\n"
        "- A plugin with call_priority=auto is selected by your own task-fit judgment and is not forced "
        "into the explicit ranking."
    )
