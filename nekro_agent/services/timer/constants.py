"""定时服务与 `timer` 插件共享的常量。

核心代码不能 import 插件模块（插件加载依赖核心），所以这里只放插件键的字面量。
`tests/test_plugin_background_scope.py` 会断言它与真正加载出来的 `timer` 插件 `.key`
一致，防止插件改名后核心里的判据悄悄失效。
"""

# 与 `plugins/builtin/timer.py` 的 `NekroPlugin(module_name="timer", author="KroMiose")` 对齐
TIMER_PLUGIN_KEY = "KroMiose.timer"
