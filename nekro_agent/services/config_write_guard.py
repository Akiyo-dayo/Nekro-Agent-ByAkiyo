"""配置页保存时的过期写回防护

配置页打开之后，后台别处可能改了某一项：删实例、划转时自动从值守名单或共享发信名单里摘掉那个实例，
或者另一个管理员先存了一次。旧页面上再点保存，提交的是在页面打开时那份值上改出来的结果，会把别处的
改动盖回去——名单里已经摘掉的实例又被写回来，同名实例以后被重新认领时就又带上了值守或发信授权。

页面只提交改过的项还不够：改过的那一项本身就可能是过期的（在旧名单上加一个实例，旧名单里还留着那个
已经摘掉的）。所以保存时页面把每个改动项「读取时的值」（`base`）一起带上，服务端按调用方读取时的
**同一个视图**取出现值比对（租户看到的掩码、覆盖层回落的继承值都一样算），任何一项对不上就整批拒绝
（`ConfigStaleWriteError`，409），页面刷新后在最新值上重新改。

不带 `base` 的调用方照旧（没有比对）；带了但没列某一项的，那一项不比对。比对要放在写入之前的最后一个
`await` 之后：同一进程里别的协程插不进「比对完、还没写」这一段。
"""

import json
from enum import Enum
from typing import Any, Iterable, List, Mapping, Optional

from pydantic_core import to_jsonable_python

from nekro_agent.schemas.errors import ConfigStaleWriteError

# 浏览器把 JSON 里的数一律读成双精度浮点，超出这个范围的整数读回来已经不是原值了
_BROWSER_SAFE_INTEGER = 2**53 - 1


def _normalize(value: Any) -> Any:
    """换成页面读到、再原样提交回来的那个形式

    服务端这边是配置对象上的原始值，`base` 是读接口的 JSON 经浏览器解析、再序列化回来的：
    两边都先落到同一个 JSON 形式上再比，否则某些类型的项永远对不上、永远存不进去。
    """
    if isinstance(value, Enum):
        return _normalize(value.value)
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, float):
        # `1.0` 经过一趟 JSON 回来是 `1`
        return _normalize(int(value)) if value.is_integer() else value
    if isinstance(value, int):
        if -_BROWSER_SAFE_INTEGER <= value <= _BROWSER_SAFE_INTEGER:
            return value
        # 按页面能表示的精度比：页面拿到的已经是舍入过的值，原样提交回来也是它
        return int(float(value))
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    # 其余类型（模型、时间、集合等）按读接口序列化给页面的方式先换成 JSON 形式
    return _normalize(to_jsonable_python(value, fallback=str))


def canonical_config_value(value: Any) -> str:
    """把一个配置值规整成可比较的文本：键的顺序、整数写成小数都不算不同"""
    return json.dumps(_normalize(value), sort_keys=True, ensure_ascii=False)


def find_stale_keys(
    submitted_keys: Iterable[str],
    base: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
) -> List[str]:
    """提交的项里，读取时的值与现值对不上的（现在已经没有这一项也算）；`base` 为空时一项都不比"""
    if not base:
        return []
    stale: List[str] = []
    for key in submitted_keys:
        if key not in base:
            continue
        if key not in current or canonical_config_value(base[key]) != canonical_config_value(current[key]):
            stale.append(key)
    return stale


def assert_config_not_stale(
    submitted_keys: Iterable[str],
    base: Optional[Mapping[str, Any]],
    current: Mapping[str, Any],
) -> None:
    """有过期的项就整批拒绝

    Raises:
        ConfigStaleWriteError: 至少一项在读取之后被别处改过；`data["keys"]` 列出是哪几项
    """
    stale = find_stale_keys(submitted_keys, base, current)
    if stale:
        raise ConfigStaleWriteError(keys=stale)


def view_values(items: Iterable[Mapping[str, Any]]) -> dict:
    """配置列表（`[{"key": ..., "value": ...}, ...]`）里每一项的值"""
    return {str(item.get("key")): item.get("value") for item in items}
