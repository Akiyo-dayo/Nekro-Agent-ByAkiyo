"""
# 技能加载器 (skill_loader)

把全局 Skill 库（内置 / 本地 / 订阅仓库）以「索引 + 按需加载」的方式开放给 NekroAgent 主 Agent。

Skill 原本是写给沙盒内 Claude Code 的 Markdown 工作流，主 Agent 无法感知。本插件用与
Claude Code 自身发现机制一致的语义把它桥接过来：

- **提示词只注入索引**：每个可用技能一行 `标识: 用途描述`，不注入正文，避免撑爆上下文。
- **正文按需加载**：AI 判断某条描述命中当前任务时，调用 `load_skill("<id>")` 读取 SKILL.md 全文。
- **CC 专有语义降级**：返回正文时附带适配说明，并剥离 `allowed-tools` 等 Claude Code 专有
  frontmatter 字段，避免主 Agent 误以为自己拥有 Bash / Read 等工具。

## 沙盒方法

- `list_skills()`: 列出全部可加载技能（索引被截断时使用）
- `load_skill(name)`: 按标识加载技能文档正文

## 配置

- `ENABLED_SKILLS`: 白名单，留空表示开放全部
- `DISABLED_SKILLS`: 黑名单，优先级高于白名单
- `MAX_INDEX_ITEMS` / `MAX_INDEX_DESCRIPTION_LENGTH`: 索引规模控制
- `MAX_SKILL_CONTENT_LENGTH`: 单次加载正文的长度上限

以上配置均标记为 `overridable`，可按适配器 / 频道实例独立覆盖，从而实现「同一份全局技能库，
不同实例开放不同子集」。
"""

from . import main
from .plugin import plugin

__all__ = ["main", "plugin"]
