"""人设正文的拼装模板

酒馆卡把角色拆成描述、性格、场景、示例对话与系统提示词几个字段；NA 的人设只有
一段正文。导入时按这个模板把非空字段拼成正文，拼一次写进人设，之后管理员照常
改正文——改模板不会悄悄改变已导入的人设。

模板只认五个占位，规则只有两条：按空行分段；一段里引用的字段全部为空时整段丢掉。
其余 `{{…}}` 原样保留，运行时再按宏展开。刻意不引入 Handlebars：那会多一套语法、
一套转义规则和一个新的失败面，而这里需要的只是“空字段不要留下孤零零的标题”。
"""

from __future__ import annotations

import hashlib
import re
from typing import Mapping

# 卡片字段占位；顺序也是拼装模板里默认出现的顺序。
CARD_FIELDS = ("system", "description", "personality", "scenario", "mes_example")

DEFAULT_CARD_TEMPLATE = """{{system}}

{{description}}

{{char}}'s personality: {{personality}}

Scenario: {{scenario}}

Example dialogue:
{{mes_example}}"""

# 空行分段：一行里只有空白也算空行，连续多个空行只切一次。
_BLANK_LINE = re.compile(r"(?:\n[ \t]*\n)+")
# 五个占位；大小写不敏感，两侧允许空白，方便管理员写 `{{ System }}`。
_CARD_PLACEHOLDER = re.compile(r"\{\{\s*(system|description|personality|scenario|mes_example)\s*\}\}", re.IGNORECASE)


def assemble_content(template: str, fields: Mapping[str, str]) -> str:
    """按模板把卡片字段拼成人设正文

    一段里只要引用到的字段有一个非空，这一段就保留；保留下来的占位换成字段原文
    （不 strip，作者自己写的缩进与换行要留着）。没引用占位的段落原样保留。
    """
    segments = _BLANK_LINE.split(template)
    kept: list[str] = []
    for segment in segments:
        referenced = [match.group(1).lower() for match in _CARD_PLACEHOLDER.finditer(segment)]
        if referenced and all(not (fields.get(name) or "").strip() for name in referenced):
            continue
        kept.append(
            _CARD_PLACEHOLDER.sub(lambda match: fields.get(match.group(1).lower(), ""), segment),
        )
    return "\n\n".join(kept).strip()


def content_sha256(text: str) -> str:
    """拼装结果的摘要，用来判断正文有没有被手改过"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
