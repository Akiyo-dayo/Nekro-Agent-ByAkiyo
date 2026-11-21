from typing import Any, Optional

from nekro_agent.tools.tenant_egress import is_tenant_owned_group


def build_model_test_messages(use_system: bool = False) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {
            "role": "user",
            "content": "Repeat the following text without any thinking or explanation: Test",
        }
    ]
    if use_system:
        messages.insert(
            0,
            {
                "role": "system",
                "content": "You are a helpful assistant that follows instructions precisely.",
            },
        )
    return messages


def build_openai_model_test_params(
    model_group: Any,
    stream_mode: bool = False,
    *,
    tenant_egress: Optional[bool] = None,
) -> dict[str, Any]:
    """`tenant_egress` 不给时按组判（`is_tenant_owned_group`）；inline 测试这类临时拼的组由调用方给"""
    return {
        "model": model_group.CHAT_MODEL,
        "temperature": model_group.TEMPERATURE,
        "top_p": model_group.TOP_P,
        "top_k": model_group.TOP_K,
        "frequency_penalty": model_group.FREQUENCY_PENALTY,
        "presence_penalty": model_group.PRESENCE_PENALTY,
        "extra_body": model_group.EXTRA_BODY,
        "base_url": model_group.BASE_URL,
        "api_key": model_group.API_KEY,
        "stream_mode": stream_mode,
        "proxy_url": model_group.CHAT_PROXY,
        "api_format": model_group.API_FORMAT,
        "tenant_egress": is_tenant_owned_group(model_group) if tenant_egress is None else tenant_egress,
    }


def build_openai_embedding_test_params(model_group: Any, *, tenant_egress: Optional[bool] = None) -> dict[str, Any]:
    return {
        "model": model_group.CHAT_MODEL,
        "input": "test",
        "api_key": model_group.API_KEY,
        "base_url": model_group.BASE_URL,
        "proxy_url": model_group.CHAT_PROXY,
        "timeout": 30,
        "tenant_egress": is_tenant_owned_group(model_group) if tenant_egress is None else tenant_egress,
    }
