"""
# 世界书查询插件 (worldbook_tools)

让模型按需查询本频道当前生效的世界书：查到的是世界观设定资料，按不可信内容交回。
"""

from . import main
from .plugin import plugin

__all__ = ["plugin", "main"]
