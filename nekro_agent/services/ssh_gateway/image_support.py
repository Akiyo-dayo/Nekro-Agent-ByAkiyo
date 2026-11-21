"""CC 沙盒镜像是否带 SSH 网关所需的 `Include`

镜像会话在 `/etc/ssh/ssh_config.d/50-nekro.conf` 里放了一行 `Include`，指向 NA 写进工作区的
`nk-ssh/config`。老镜像没有这个文件，容器里的 `ssh web1` 会把别名当普通主机名去解析，
最终报一个与「网关没开」毫无关系的 DNS 错误。所以网关在为一个工作区服务之前先看镜像版本，
太旧就拒（理由 `image_too_old`），后台也据此标出这个工作区需要升级镜像。

## 版本形状

`/api/v1/status` 的 `version` 形如 `0.1.14+ak2`。上游镜像不带 `+ak<数字>` 后缀，一律不支持：
它们的 ssh 配置里没有那一行 `Include`。带后缀时按基础版本判断：`0.1.14` 要求 `ak >= 2`；
比它新的基础版本都带那一行（自建镜像从 ak2 起步，之后的版本沿用）。

取不到、解析不了（`unknown`、空串、乱串）一律按不支持处理：宁可短期不提供网关，
也不要把连接指向一个容器根本读不懂的配置。
"""

from __future__ import annotations

import re
from typing import Match, Optional, Tuple

_VERSION_PATTERN = re.compile(r"^(?P<base>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)\+ak(?P<ak>\d+)$")

# 第一个带 `Include` 的镜像：0.1.14 的 ak2。之后的基础版本不需要再卡 ak 号。
_FIRST_IMAGE_BASE: Tuple[int, int, int] = (0, 1, 14)
_FIRST_IMAGE_MIN_AK = 2


def _parse(version: str) -> Optional[Match[str]]:
    return _VERSION_PATTERN.fullmatch(version.strip())


def image_supports_gateway(version: str | None) -> bool:
    """这个沙盒版本是否带网关所需的 ssh 配置

    Args:
        version: `/api/v1/status` 的 `version` 字段，允许 `None`

    Returns:
        `True` 仅当版本带 `+ak<数字>` 后缀、且基础版本与 ak 号都达到门槛。
    """
    if not version:
        return False
    match = _parse(str(version))
    if match is None:
        return False
    base = (int(match.group("base")), int(match.group("minor")), int(match.group("patch")))
    if base < _FIRST_IMAGE_BASE:
        return False
    if base == _FIRST_IMAGE_BASE:
        return int(match.group("ak")) >= _FIRST_IMAGE_MIN_AK
    return True


__all__ = ["image_supports_gateway"]
