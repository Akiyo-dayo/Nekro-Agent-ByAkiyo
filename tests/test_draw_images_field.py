"""绘图聊天模式图片字段解析。

模型可能把生成的图片放在 `images`（复数列表）里，旧接口则放在 `image`（单数）里。
这里用一份模拟的模型响应覆盖两种字段，直接验证解析函数，不发起任何网络请求。
"""

from typing import Any, Dict

from plugins.builtin.draw.handlers import _extract_first_image_data_uri

IMAGE_ONE = "aW1hZ2Utb25l"
IMAGE_TWO = "aW1hZ2UtdHdv"

# 模拟两次模型响应：流式 delta 走 `images` 复数列表，非流式 message 走旧的 `image` 单数字段
MOCK_MODEL_RESPONSE: Dict[str, Any] = {
    "choices": [
        {
            "delta": {
                "images": [
                    {"data": IMAGE_ONE},
                    {"b64_json": IMAGE_TWO},
                ],
            },
        },
        {
            "message": {
                "image": [
                    {"data": IMAGE_TWO},
                ],
            },
        },
    ],
}


def test_extracts_images_from_plural_and_legacy_singular_fields() -> None:
    """复数 `images` 与单数 `image` 都应解析出完整的 data URI。"""
    delta = MOCK_MODEL_RESPONSE["choices"][0]["delta"]
    message = MOCK_MODEL_RESPONSE["choices"][1]["message"]

    assert _extract_first_image_data_uri(delta) == f"data:image/png;base64,{IMAGE_ONE}"
    assert _extract_first_image_data_uri(message) == f"data:image/png;base64,{IMAGE_TWO}"


def test_plural_images_field_takes_precedence() -> None:
    """同一份响应里两个字段并存时，优先取 `images` 复数列表。"""
    chunk = {"images": [{"data": IMAGE_ONE}], "image": [{"data": IMAGE_TWO}]}

    assert _extract_first_image_data_uri(chunk) == f"data:image/png;base64,{IMAGE_ONE}"
