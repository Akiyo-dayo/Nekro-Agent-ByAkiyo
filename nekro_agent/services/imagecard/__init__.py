"""图片卡片渲染

用 Pillow 直接作画，不引入任何浏览器/HTML 渲染引擎：卡片结构简单固定，
排版逻辑（折行、截断、分列）本身就是需要被单独测试的业务规则，
把它交给一个几百 MB 的无头浏览器既拖慢部署，也让这些规则变得无法断言。

- `fonts`：字体链解析与字形回退，保证任何用户可控文本都不会渲染出豆腐块
- `painter`：折行、截断、圆角矩形等绘制原语
- `inner_voice`：心声卡片
- `affection_rank`：好感度排行榜卡片
"""

from .fonts import FontRole, describe_fonts, fonts_available, load_font
from .inner_voice import (
    MAX_BODY_LENGTH,
    layout_inner_voice,
    prepare_body_text,
    render_inner_voice_card,
)

__all__ = [
    "MAX_BODY_LENGTH",
    "FontRole",
    "describe_fonts",
    "fonts_available",
    "layout_inner_voice",
    "load_font",
    "prepare_body_text",
    "render_inner_voice_card",
]
