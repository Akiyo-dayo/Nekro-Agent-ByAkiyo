"""酒馆卡解析的各项上限

卡片与其中的图片都按不可信输入处理：先卡住文件、块、字段、条目与规则的规模，
再进入解析。上限集中在这个模块，解析、导入与用例引用同一份数字，避免两处各写
一遍后只改了一处。
"""

from __future__ import annotations

# 上传文件本身的上限；上传接口另受平台配置 `MAX_UPLOAD_SIZE_MB`（默认 10）约束，两者取小
# （`routers/tavern.py::_max_upload_bytes`）。从 10 MiB 放到了 20 MiB：大型世界卡把同一
# 份卡片数据在 `chara` 与 `ccv3` 两块里各存一份，PNG 轻易超过 10 MiB（单块仍受下面 8 MiB 约束）。
# 平台配置的默认值没动，所以默认部署实际仍是 10 MiB，要平台方把 `MAX_UPLOAD_SIZE_MB` 调大才放开。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# PNG 块数量与单块大小的上限：防止用海量小文本块或一个超大文本块拖垮解析。
MAX_PNG_CHUNKS = 10000
MAX_PNG_CHUNK_BYTES = 8 * 1024 * 1024
# 头像宽高的上限；超过时卡片仍可导入，只是不拿这张图当头像。
MAX_IMAGE_SIDE = 4096
# 解码后的卡片 JSON 上限。
MAX_CARD_JSON_BYTES = 8 * 1024 * 1024
# JSON 嵌套层数上限，与文件解析一样用迭代方式检查。
MAX_JSON_DEPTH = 32
# 单个文字字段的上限；超过截断并记进导入报告。
MAX_TEXT_FIELD_CHARS = 64000
# 开场白条数与单条字数上限。
MAX_GREETINGS = 50
MAX_GREETING_CHARS = 16000
# 卡内知识书条目上限；超过整张卡拒绝，不静默截断。
MAX_BOOK_ENTRIES = 2000
# 卡内清理规则条数上限；超过丢掉多余条目并记报告。
MAX_CLEANUP_RULES = 50
# 标签个数上限。
MAX_TAGS = 64
# 数据 URI 头像解码后的上限；超过就不用它当头像。
MAX_DATA_URI_IMAGE_BYTES = 2 * 1024 * 1024
# 拼装后的正文超过这个字数时导入报告提醒，但不拦。
LONG_CONTENT_WARN_CHARS = 12000
