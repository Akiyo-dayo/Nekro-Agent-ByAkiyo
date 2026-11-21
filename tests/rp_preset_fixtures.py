"""酒馆对话补全预设的测试样本（中性占位文字）

只用来验证解析与排布，不抄任何真实预设的正文，也没有成人或越狱内容。所有函数都返回
一份新的字典，调用方可以随意改动。
"""

from __future__ import annotations

import copy
from typing import Any


def _prompt(
    identifier: str,
    name: str,
    content: str = "",
    *,
    role: str = "system",
    marker: bool = False,
    position: int = 0,
    depth: int = 4,
    order: int = 100,
    forbid_overrides: bool = False,
) -> dict[str, Any]:
    return {
        "identifier": identifier,
        "name": name,
        "role": role,
        "content": content,
        "marker": marker,
        "system_prompt": False,
        "injection_position": position,
        "injection_depth": depth,
        "injection_order": order,
        "forbid_overrides": forbid_overrides,
    }


def _order_item(identifier: str, enabled: bool) -> dict[str, Any]:
    return {"identifier": identifier, "enabled": enabled}


def standard_preset() -> dict[str, Any]:
    """标准顺序：main、世界书前后两处、人设类占位段、两个自定义段、聊天记录、后置段

    长的那份 `prompt_order`（角色 100000）才是生效顺序；短的那份只用来验证「取最长」。
    `enhanceDefinitions` 与 `nsfw` 关闭；`orphanNote` 不在任何顺序表里，应排在最后且关闭。
    """
    prompts = [
        _prompt("main", "Main Prompt", "MAIN_BODY {{char}} {{getvar::mood}} {{// a comment}}"),
        _prompt("worldInfoBefore", "World Info Before", marker=True),
        _prompt("personaDescription", "Persona Description", marker=True),
        _prompt("charDescription", "Character Description", marker=True),
        _prompt("charPersonality", "Character Personality", marker=True),
        _prompt("scenario", "Scenario", marker=True),
        _prompt(
            "enhanceDefinitions",
            "Enhance Definitions",
            "ENHANCE_BODY {{getvar::mood}} {{// a comment}}",
        ),
        _prompt("nsfw", "Style Marker", "STYLE_MARKER"),
        _prompt("worldInfoAfter", "World Info After", marker=True),
        _prompt("dialogueExamples", "Dialogue Examples", marker=True),
        _prompt("chatHistory", "Chat History", marker=True),
        _prompt("jailbreak", "Jailbreak", "JAIL_BODY {{user}}"),
        _prompt("orphanNote", "Orphan Note", "ORPHAN_BODY"),
    ]
    long_order = [
        _order_item("main", True),
        _order_item("worldInfoBefore", True),
        _order_item("personaDescription", True),
        _order_item("charDescription", True),
        _order_item("charPersonality", True),
        _order_item("scenario", True),
        _order_item("enhanceDefinitions", False),
        _order_item("nsfw", False),
        _order_item("worldInfoAfter", True),
        _order_item("dialogueExamples", True),
        _order_item("chatHistory", True),
        _order_item("jailbreak", True),
    ]
    short_order = [
        _order_item("main", False),
        _order_item("worldInfoBefore", False),
    ]
    return {
        "name": "standard",
        "temperature": 0.7,
        "top_p": 0.9,
        "openai_max_tokens": 800,
        "prompts": prompts,
        "prompt_order": [
            {"character_id": 100001, "order": short_order},
            {"character_id": 100000, "order": long_order},
        ],
        "extensions": {
            "regex_scripts": [
                {
                    "scriptName": "清理占位",
                    "findRegex": "/PLACEHOLDER/g",
                    "replaceString": "",
                    "trimStrings": [],
                    "placement": [2],
                    "disabled": False,
                    "markdownOnly": False,
                    "promptOnly": False,
                    "substituteRegex": 0,
                },
            ],
        },
    }


def wrapped_preset() -> dict[str, Any]:
    """带包裹标签、深度段与末尾 assistant 的样本"""
    prompts = [
        _prompt("main", "Main Prompt", "WRAPPED_MAIN"),
        _prompt("worldInfoBefore", "World Info Before", marker=True),
        _prompt("openCharacters", "Open Characters", "<characters>"),
        _prompt("charDescription", "Character Description", marker=True),
        _prompt("closeCharacters", "Close Characters", "</characters>"),
        _prompt("charPersonality", "Character Personality", marker=True),
        _prompt("scenario", "Scenario", marker=True),
        _prompt("worldInfoAfter", "World Info After", marker=True),
        _prompt("dialogueExamples", "Dialogue Examples", marker=True),
        _prompt("chatHistory", "Chat History", marker=True),
        _prompt("depthInstruction", "Depth Instruction", "DEPTH_MARKER", position=1, depth=2, order=30),
        _prompt("jailbreak", "Jailbreak", "WRAPPED_JAIL", order=90),
        _prompt("tailAssistant", "Tail Assistant", "PREFILL_MARKER", role="assistant", order=110),
    ]
    order = [_order_item(prompt["identifier"], True) for prompt in prompts]
    return {
        "name": "wrapped",
        "prompts": prompts,
        "prompt_order": [{"character_id": 100000, "order": order}],
    }


def custom(name: str, content: str = "", *, role: str = "system") -> dict[str, Any]:
    """造一段普通提示词；测试里按需拼顺序表"""
    return _prompt(name, name, content, role=role)


def with_prompts(prompts: list[dict[str, Any]], order: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": "custom",
        "prompts": copy.deepcopy(prompts),
        "prompt_order": [{"character_id": 100000, "order": copy.deepcopy(order)}],
    }


__all__ = [
    "custom",
    "standard_preset",
    "with_prompts",
    "wrapped_preset",
]
