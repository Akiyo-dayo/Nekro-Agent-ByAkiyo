"""新建工作区时自动注入的技能清单

## 为什么它必须跟着作用域走

改前这份清单是**全局单值**：平台方在总后台勾上一条技能，所有租户的每一个新工作区
都会被塞进这条技能。清单里出现一条平台方私有技能时，它的正文就随着工作区创建
被复制进租户的沙盒目录，由容器里的 Claude Code 逐字读走。

现在它按作用域分片：

- **平台方**（`tenant_id=None`，也是未启用多租户时的唯一分支）读写文件顶层的
  `skills` 键，行为与改造前逐字一致。
- **租户**读写 `tenants[str(tenant_id)]`，各自独立。
- 租户**尚未设过**自己的清单时，回落到平台方那份**按可见性裁剪后**的子集
  （`resolve_auto_inject_skills`）。回落而不是给空列表，是因为默认那三条全是内置技能，
  给空列表等于让每个租户的新工作区一条技能都没有 —— 那是一次没人要求的功能回退。
  裁剪则保证回落**永远带不出**平台方的私有技能。

文件结构（老文件只有 `skills` 一个键，读起来天然兼容）：

    {"skills": ["agent-browser"], "tenants": {"1": ["my-runbook"]}}
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import OsEnv

_AUTO_INJECT_PATH = Path(OsEnv.DATA_DIR) / "configs" / "auto-inject-skills.json"
_DEFAULT_BUILTIN_SKILLS = [
    "agent-browser",
    "git-github-workflow",
    "skill-creator",
]


def _get_default_skills() -> List[str]:
    """Return the built-in skills that should be enabled by default."""
    try:
        from nekro_agent.services.workspace.manager import WorkspaceService

        valid_names = {skill["name"] for skill in WorkspaceService.list_all_skills()}
        defaults = [name for name in _DEFAULT_BUILTIN_SKILLS if name in valid_names]
        return defaults or _DEFAULT_BUILTIN_SKILLS.copy()
    except Exception as e:
        logger.warning(f"获取默认自动注入技能失败，将回退到静态列表: {e}")
        return _DEFAULT_BUILTIN_SKILLS.copy()


def _ensure_file() -> None:
    _AUTO_INJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _AUTO_INJECT_PATH.exists():
        _AUTO_INJECT_PATH.write_text(
            json.dumps({"skills": _get_default_skills()}, indent=2),
            encoding="utf-8",
        )


def _read() -> Dict[str, object]:
    _ensure_file()
    try:
        data = json.loads(_AUTO_INJECT_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"读取 auto-inject-skills.json 失败: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _tenant_section(data: Dict[str, object]) -> Dict[str, List[str]]:
    raw = data.get("tenants")
    if not isinstance(raw, dict):
        return {}
    return {str(key): [str(name) for name in value] for key, value in raw.items() if isinstance(value, list)}


def get_auto_inject_skills(tenant_id: Optional[int] = None) -> List[str]:
    """读某个作用域**自己设过的**清单

    租户没设过时返回空列表 —— 这是「没设过」，不是「设成了空」。
    要拿实际生效的清单请用 `resolve_auto_inject_skills`。
    """
    data = _read()
    if tenant_id is None:
        skills = data.get("skills")
        return [str(name) for name in skills] if isinstance(skills, list) else []
    return list(_tenant_section(data).get(str(tenant_id), []))


def has_own_auto_inject_skills(tenant_id: int) -> bool:
    """某个租户是否设过自己的清单（用于区分「设成空」与「没设过」）"""
    return str(tenant_id) in _tenant_section(_read())


class AutoInjectStoreCorrupted(RuntimeError):
    """清单文件解析不出来

    单列一个异常是因为读与写这两侧对同一个失败要给出**相反**的结论，见 `_read_for_update`。
    """


def _read_for_update() -> Dict[str, object]:
    """写入前读一次；解析不出来时抛，**不**当成空表

    改前这份文件只有 `skills` 一个键：读失败按空表处理，写下去顶多丢自己那一份。
    改成按作用域分片之后它是**全部作用域共用的一份**，同一个回落就变成
    「某个租户点一次保存，把平台方清单与其余每个租户的清单一起覆盖掉」——
    而且没有任何信号，下一次新建工作区才会有人发现技能没了。
    写这一侧必须比读那一侧严格，这是同一个异常的两条出路。
    """
    _ensure_file()
    try:
        raw = _AUTO_INJECT_PATH.read_text(encoding="utf-8")
    except OSError as e:
        raise AutoInjectStoreCorrupted(f"读取失败: {e}") from e
    try:
        data = json.loads(raw)
    except Exception as e:
        raise AutoInjectStoreCorrupted(f"JSON 解析失败: {e}") from e
    if not isinstance(data, dict):
        raise AutoInjectStoreCorrupted("顶层结构不是对象")
    return data


def set_auto_inject_skills(names: List[str], tenant_id: Optional[int] = None) -> None:
    """覆盖某个作用域的清单

    租户一旦调用过（哪怕传的是空列表），就不再回落平台方那份。

    Raises:
        AutoInjectStoreCorrupted: 现有文件解析不出来 —— 这时写入会连带覆盖别的作用域那几份
    """
    data = _read_for_update()
    if tenant_id is None:
        data["skills"] = list(names)
    else:
        tenants = _tenant_section(data)
        tenants[str(tenant_id)] = list(names)
        data["tenants"] = tenants
    _AUTO_INJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _AUTO_INJECT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def resolve_auto_inject_skills(tenant_id: Optional[int] = None) -> List[str]:
    """某个作用域实际生效的自动注入清单

    平台方：文件里的 `skills`，逐字。
    租户：自己设过就用自己的；没设过就用平台方那份**按可见性裁剪后**的子集。

    两条路径最后都再过一次可见性：清单是自由字符串，且平台方随时可能把一条
    已经共享出去的技能改回私有 —— 判据必须在**每次使用时**重算，不能只在写入时校验一次。
    """
    from nekro_agent.services.workspace.manager import WorkspaceService

    if tenant_id is None:
        return get_auto_inject_skills(None)

    names = get_auto_inject_skills(tenant_id) if has_own_auto_inject_skills(tenant_id) else get_auto_inject_skills(None)
    try:
        visible = {skill["name"] for skill in WorkspaceService.list_all_skills(tenant_id)}
    except Exception as e:
        logger.warning(f"裁剪租户 {tenant_id} 的自动注入清单失败，按空清单处理: {e}")
        return []
    return [name for name in names if name in visible]
