"""角色状态（变量树，MVU 兼容）：可选内置插件，默认关"""

from . import commands, import_hook, inline, main, router
from .plugin import plugin

__all__ = ["commands", "import_hook", "inline", "main", "plugin", "router"]
