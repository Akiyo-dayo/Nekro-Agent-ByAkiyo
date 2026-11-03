"""docker-guard：NA 与宿主 docker.sock 之间的按动作放行薄代理（opt-in 部署硬化）。

- `policy`：纯函数判据核心（表驱动单测覆盖）
- `proxy`：aiohttp 转发层

背景与部署方式见 `docs/docker-guard.md`。
"""

from nekro_agent.services.docker_guard.policy import (
    Decision,
    PolicyContext,
    api_version_of,
    body_forbidden,
    body_guard_rule,
    container_ownership_target,
    evaluate,
    is_safe_container_ref,
    labels_indicate_ownership,
    network_connect_target,
    path_has_dot_segments,
)

__all__ = [
    "Decision",
    "PolicyContext",
    "api_version_of",
    "body_forbidden",
    "body_guard_rule",
    "container_ownership_target",
    "evaluate",
    "is_safe_container_ref",
    "labels_indicate_ownership",
    "network_connect_target",
    "path_has_dot_segments",
]
