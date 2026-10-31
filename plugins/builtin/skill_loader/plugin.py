from typing import List

from pydantic import Field

from nekro_agent.api import i18n
from nekro_agent.api.plugin import ConfigBase, ExtraField, NekroPlugin

plugin = NekroPlugin(
    name="技能加载器",
    module_name="skill_loader",
    description="把全局 Skill 库以「索引 + 按需加载」的方式开放给主 Agent，使其能够复用为 Claude Code 编写的技能工作流",
    version="0.1.0",
    author="Akiyo_dayo",
    url="https://github.com/Akiyo-dayo/Nekro-Agent-ByAkiyo",
    i18n_name=i18n.i18n_text(
        zh_CN="技能加载器",
        en_US="Skill Loader",
    ),
    i18n_description=i18n.i18n_text(
        zh_CN="把全局 Skill 库以「索引 + 按需加载」的方式开放给主 Agent，使其能够复用为 Claude Code 编写的技能工作流",
        en_US="Exposes the global skill library to the main Agent as a lightweight index plus on-demand document loading",
    ),
    allow_sleep=False,
    sleep_brief="用于在需要成体系的操作流程（浏览器自动化、Git 工作流、技能编写等）时查阅并加载对应的技能文档。",
)


@plugin.mount_config()
class SkillLoaderConfig(ConfigBase):
    ENABLED_SKILLS: List[str] = Field(
        default=[],
        title="对主 Agent 开放的技能白名单",
        description="填写技能标识（内置/本地技能为目录名，仓库技能为 `仓库名/技能名`）。留空表示开放全部技能。",
        json_schema_extra=ExtraField(
            sub_item_name="技能标识",
            overridable=True,
            placeholder="agent-browser",
            i18n_title=i18n.i18n_text(
                zh_CN="对主 Agent 开放的技能白名单",
                en_US="Skill Whitelist for the Main Agent",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="填写技能标识（内置/本地技能为目录名，仓库技能为 `仓库名/技能名`）。留空表示开放全部技能。",
                en_US="Skill identifiers (directory name for builtin/local skills, `repo/skill` for repository skills). Leave empty to expose every skill.",
            ),
        ).model_dump(),
    )

    DISABLED_SKILLS: List[str] = Field(
        default=[],
        title="对主 Agent 屏蔽的技能黑名单",
        description="始终生效，优先级高于白名单。填写技能标识，用于屏蔽只适合沙盒内 Claude Code 使用的技能。",
        json_schema_extra=ExtraField(
            sub_item_name="技能标识",
            overridable=True,
            placeholder="skill-creator",
            i18n_title=i18n.i18n_text(
                zh_CN="对主 Agent 屏蔽的技能黑名单",
                en_US="Skill Blacklist for the Main Agent",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="始终生效，优先级高于白名单。填写技能标识，用于屏蔽只适合沙盒内 Claude Code 使用的技能。",
                en_US="Always applied and takes precedence over the whitelist. Use it to hide skills that only make sense inside the Claude Code sandbox.",
            ),
        ).model_dump(),
    )

    MAX_INDEX_ITEMS: int = Field(
        default=30,
        title="技能索引最大条目数",
        description="注入提示词的技能索引最多展示多少条，超出部分会被折叠为提示行。",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(
                zh_CN="技能索引最大条目数",
                en_US="Max Skill Index Entries",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="注入提示词的技能索引最多展示多少条，超出部分会被折叠为提示行。",
                en_US="Maximum number of entries shown in the injected skill index; the remainder is collapsed into a hint line.",
            ),
        ).model_dump(),
    )

    MAX_INDEX_DESCRIPTION_LENGTH: int = Field(
        default=160,
        title="索引描述最大长度",
        description="技能索引中单条描述保留的最大字符数，超出部分会被截断。",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(
                zh_CN="索引描述最大长度",
                en_US="Max Index Description Length",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="技能索引中单条描述保留的最大字符数，超出部分会被截断。",
                en_US="Maximum characters kept for a single description in the skill index; longer text is truncated.",
            ),
        ).model_dump(),
    )

    MAX_SKILL_CONTENT_LENGTH: int = Field(
        default=8000,
        title="技能正文最大长度",
        description="单次加载技能文档时保留的最大字符数，超出部分会被截断并提示。",
        json_schema_extra=ExtraField(
            overridable=True,
            i18n_title=i18n.i18n_text(
                zh_CN="技能正文最大长度",
                en_US="Max Skill Document Length",
            ),
            i18n_description=i18n.i18n_text(
                zh_CN="单次加载技能文档时保留的最大字符数，超出部分会被截断并提示。",
                en_US="Maximum characters kept when loading a single skill document; longer content is truncated with a notice.",
            ),
        ).model_dump(),
    )


skill_loader_config: SkillLoaderConfig = plugin.get_config(SkillLoaderConfig)
