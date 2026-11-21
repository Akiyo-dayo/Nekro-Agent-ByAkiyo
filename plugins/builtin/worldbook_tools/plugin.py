from nekro_agent.api import i18n
from nekro_agent.api.plugin import NekroPlugin

# 说明与能力摘要会进系统提示词：只写「能查当前生效的世界书」，不列有哪些书。
plugin = NekroPlugin(
    name="世界书查询",
    module_name="worldbook_tools",
    description="让主 Agent 按需查询当前频道生效的世界书设定资料",
    version="0.1.0",
    author="Akiyo_dayo",
    url="https://github.com/KroMiose/nekro-agent",
    i18n_name=i18n.i18n_text(
        zh_CN="世界书查询",
        en_US="Worldbook Lookup",
    ),
    i18n_description=i18n.i18n_text(
        zh_CN="让主 Agent 按需查询当前频道生效的世界书设定资料",
        en_US="Lets the main Agent look up the worldbook settings active in the current channel",
    ),
    allow_sleep=False,
    sleep_brief="用于按需查询当前频道生效的世界书（世界观设定资料），适合回答设定里的人物、地点、组织、历史、术语与规则细节。",
)
