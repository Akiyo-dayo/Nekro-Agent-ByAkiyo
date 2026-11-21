#!/usr/bin/env python3
"""把 CC 工作区的 home 搬回旧位置：退回 0.4.7 之前的版本时用

0.4.7 起，CC 工作区挂进容器当 `~/.claude` 的目录（会话历史、技能、`.claude.json`、SSH 网关钥匙）
从工作区目录里的 `.claude_home` 挪到了工作区目录之外：

    旧：{数据目录}/workspaces/<id>/.claude_home
    新：{数据目录}/workspaces/.nk-homes/<id>/claude

迁移是在建容器（启动、重建）时自动做的。旧版本只认旧位置，直接退回去的话，迁过的工作区会
拿到一个空的 home（会话历史与技能都还在新位置，只是旧版本看不见）。本脚本把新位置的 home
逐个改名回旧位置。

用法（默认只列出要做什么，加 `--apply` 才真的搬；`.nk-homes` 是 0700，要用 root 跑）::

    # 1. 先停掉 NA；CC 工作区容器也都停掉（NA 停了之后在宿主上 `docker ps` 看一眼）
    # 2. 看一遍要搬哪些
    sudo python3 scripts/workspace_homes_rollback.py --data-dir <数据目录>
    # 3. 真的搬
    sudo python3 scripts/workspace_homes_rollback.py --data-dir <数据目录> --apply
    # 4. 换回旧版本启动，在控制台对每个 CC 工作区点一次「重建」。0.4.7 建的容器挂的是新位置：
    #    0.4.6 启动时会把它们标成「挂载与预期不一致」；更早的版本不提示，直接启动的话容器拿到的
    #    是空 home，同样要重建

`<数据目录>` 是部署时的 `NEKRO_DATA_DIR`（不给 `--data-dir` 时读这个环境变量）。只用标准库，
不需要 NA 的运行环境。

逐个工作区判断，下面几种情况跳过这一个、最后以非零退出（别的照搬）：

- 旧位置已经有东西（`.claude_home` 存在，哪怕是空目录或链接）：不覆盖，留给人看；
- `.nk-homes/<id>`、新位置的 home 或工作区目录不是普通目录（是符号链接或别的东西）：不跟着链接搬；
- 工作区目录不存在（工作区已经删了，home 是残留）：不搬，可以手动删掉。

`--apply` 时先确认这套部署停干净了，没停干净就整个拒绝：

- NA 进程：在 `/proc` 里找 `bot` / `run_bot.py` 进程，`NEKRO_DATA_DIR`（没设时是它工作目录下的
  `./data/nekro_agent`）指向这个数据目录的算（Docker 部署里 NA 容器的进程也看得到）。查不了
  （不是 Linux）时也拒绝，确认 NA 停了之后加 `--skip-process-check`；
- 容器：在跑的容器里，以读写方式挂着这个数据目录或它下面任何目录的算（NA 自己、CC 工作区容器、
  按旧挂法挂着整个数据目录的 NapCat）；只读挂 `uploads` 的 NapCat 与别的部署的容器不算。查不了
  Docker（没装 docker 命令、没有权限）时也拒绝，确认都停了之后加 `--skip-docker-check`。

`.nk-homes/<id>` 里的 `legacy-<时间>` 是 NA 从工作区目录里挪出来的旧 home（退回旧版本又升级回来
时会有），本脚本不动它们，搬完之后提示一句。

搬回去的 home 照 NA 的口径交给容器里的用户（cc-sandbox 镜像的 `appuser`，uid / gid 1000）：home 本身
0777、属 1000；NA 写的 `settings.json`、`skills/` 整棵、`nk-ssh/id_gateway` 属 1000（私钥权限不动，
`nk-ssh` 目录与两个配置文件留给 root）。链接一律不碰。修复之前的 0.4.7 建出来的 home 里这些东西
属 root，旧版本的入口脚本也不一定会再 `chown` 一遍。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

HOMES_DIRNAME = ".nk-homes"
HOME_SUBDIR = "claude"
LEGACY_DIRNAME = ".claude_home"
# NA 的启动入口：`bot --env=prod`（镜像与 `poe start`）、`run_bot.py`
NA_ENTRY_NAMES = frozenset({"bot", "run_bot.py"})
NA_DEFAULT_DATA_DIR = "./data/nekro_agent"
# 容器里跑 CC 的用户（cc-sandbox 镜像的 appuser），与 NA 里的 `CONTAINER_USER_UID` / `GID` 一致
CONTAINER_UID = 1000
CONTAINER_GID = 1000


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _spellings(path: str) -> Set[str]:
    """一个路径的字面写法与解析过链接的写法（Docker 记的是建容器时给的字面路径）"""
    return {os.path.normpath(os.path.abspath(path)), os.path.normpath(os.path.realpath(path))}


def _inside(path: str, roots: Set[str]) -> bool:
    candidate = os.path.normpath(path)
    return any(candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


def running_deployment_containers(data_dir: Path) -> Optional[List[str]]:
    """在跑、以读写方式挂着这个数据目录（或它下面）的容器名；查不了 Docker 返回 None"""
    docker = shutil.which("docker")
    if docker is None:
        return None
    try:
        listed = subprocess.run(  # noqa: S603 - 参数是常量与 docker 给的容器 ID
            [docker, "ps", "-q", "--no-trunc"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if listed.returncode != 0:
            return None
        ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if not ids:
            return []
        inspected = subprocess.run(  # noqa: S603
            [docker, "inspect", *ids],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        containers = json.loads(inspected.stdout or "null")
    except ValueError:
        return None
    if not isinstance(containers, list):
        return None  # 列完之后有容器退出了也会让 inspect 失败，但输出照样是 JSON 列表
    roots = _spellings(str(data_dir))
    hits: List[str] = []
    for container in containers:
        for mount in container.get("Mounts") or []:
            if mount.get("RW") and _inside(str(mount.get("Source") or ""), roots):
                hits.append(str(container.get("Name") or container.get("Id") or "?").lstrip("/"))
                break
    return hits


def _read_environ(pid_dir: Path) -> Dict[str, str]:
    raw = (pid_dir / "environ").read_bytes()
    result: Dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            result[key.decode(errors="replace")] = value.decode(errors="replace")
    return result


def running_na_processes(data_dir: Path, proc: Path = Path("/proc")) -> Optional[List[str]]:
    """用着这个数据目录的 NA 进程（`pid 命令行`）；查不了（没有 /proc）返回 None"""
    if not proc.is_dir():
        return None
    roots = _spellings(str(data_dir))
    hits: List[str] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = [part.decode(errors="replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            continue  # 进程刚退出，或者读不到
        if not any(os.path.basename(arg) in NA_ENTRY_NAMES for arg in args[:3]):
            continue
        label = f"{entry.name} {' '.join(args)[:120]}"
        try:
            environ = _read_environ(entry)
            data = environ.get("NEKRO_DATA_DIR") or NA_DEFAULT_DATA_DIR
            if not os.path.isabs(data):
                data = os.path.join(os.readlink(entry / "cwd"), data)
        except PermissionError:
            hits.append(f"{label}（读不到它的环境变量，可能就是这套 NA）")
            continue
        except OSError:
            continue
        if _spellings(data) & roots:
            hits.append(label)
    return hits


def plan(workspaces_root: Path, only: Sequence[int] = ()) -> Tuple[List[Tuple[int, Path, Path]], List[str]]:
    """要搬的 `(编号, 新位置, 旧位置)` 与跳过的原因"""
    moves: List[Tuple[int, Path, Path]] = []
    skipped: List[str] = []
    homes_root = workspaces_root / HOMES_DIRNAME
    if not os.path.lexists(homes_root):
        return moves, skipped
    if not _is_real_dir(homes_root):
        skipped.append(f"{homes_root} 不是普通目录，整个跳过")
        return moves, skipped
    for entry in sorted(homes_root.iterdir(), key=lambda p: p.name):
        if not entry.name.isdigit():
            continue
        workspace_id = int(entry.name)
        if only and workspace_id not in only:
            continue
        if not _is_real_dir(entry):
            skipped.append(f"工作区 {workspace_id}：{entry} 不是普通目录（可能是符号链接），不搬")
            continue
        source = entry / HOME_SUBDIR
        workspace_dir = workspaces_root / entry.name
        target = workspace_dir / LEGACY_DIRNAME
        if not os.path.lexists(source):
            continue  # 迁移没成功留下的空目录、只有挪出来的旧 home，或者本来就没有 home
        if not _is_real_dir(source):
            skipped.append(f"工作区 {workspace_id}：{source} 不是普通目录（可能是符号链接），不搬")
            continue
        if not os.path.lexists(workspace_dir):
            skipped.append(f"工作区 {workspace_id}：工作区目录 {workspace_dir} 不存在（工作区可能已删除），不搬")
            continue
        if not _is_real_dir(workspace_dir):
            skipped.append(f"工作区 {workspace_id}：{workspace_dir} 不是普通目录（可能是符号链接），不搬")
            continue
        if os.path.lexists(target):
            skipped.append(f"工作区 {workspace_id}：旧位置 {target} 已经有东西，不覆盖；看过之后挪开再跑一次")
            continue
        moves.append((workspace_id, source, target))
    return moves, skipped


def _chown_entry(path: Path) -> None:
    """不跟随链接地交给容器用户；是链接就不碰"""
    if not os.path.islink(path):
        os.lchown(path, CONTAINER_UID, CONTAINER_GID)


def hand_to_container(home: Path) -> List[str]:
    """搬回去的 home 交给容器用户（见模块说明）；返回没做成的说明。不支持改属主的平台什么都不做"""
    if not hasattr(os, "lchown"):
        return []
    problems: List[str] = []
    try:
        if not _is_real_dir(home):
            return [f"{home} 不是普通目录，没有改属主"]
        os.chmod(home, 0o777)
        _chown_entry(home)
        settings = home / "settings.json"
        if os.path.lexists(settings) and stat.S_ISREG(os.lstat(settings).st_mode):
            _chown_entry(settings)
        skills = home / "skills"
        if _is_real_dir(skills):
            _chown_entry(skills)
            for root, dirs, files in os.walk(skills, followlinks=False):
                for name in dirs + files:
                    _chown_entry(Path(root) / name)
        key = home / "nk-ssh" / "id_gateway"
        if _is_real_dir(home / "nk-ssh") and os.path.lexists(key) and stat.S_ISREG(os.lstat(key).st_mode):
            _chown_entry(key)
    except OSError as exc:
        problems.append(f"{home} 没能交给 uid {CONTAINER_UID}: {type(exc).__name__}: {exc}")
    return problems


def apply(moves: Sequence[Tuple[int, Path, Path]]) -> List[str]:
    """逐个改名，搬回去的 home 交给容器用户；返回失败的说明"""
    failures: List[str] = []
    for workspace_id, source, target in moves:
        try:
            if os.path.lexists(target):
                raise FileExistsError(str(target))
            os.rename(source, target)  # 同一个文件系统上的改名：原子、不复制
        except OSError as exc:
            failures.append(f"工作区 {workspace_id}：{source} → {target} 失败: {type(exc).__name__}: {exc}")
            continue
        print(f"已搬回：工作区 {workspace_id}  {source} → {target}")
        for problem in hand_to_container(target):
            # 搬已经成了，不算失败；没交出去的话旧版本里容器可能写不进 skills/，提示人手动处理
            print(
                f"  注意：{problem}；请用 root 把这个目录本身、里面的 settings.json 与 skills/ 整棵改成 "
                f"{CONTAINER_UID}:{CONTAINER_GID}",
                file=sys.stderr,
            )
        try:
            source.parent.rmdir()  # `.nk-homes/<id>` 空了就删
        except OSError:
            print(f"  {source.parent} 里还有 NA 从工作区目录挪出来的旧 home（legacy-*），旧版本用不到，确认不要了再删")
    return failures


def _permission_hint(exc: OSError) -> str:
    return (
        f"权限不够（{exc}）：.nk-homes 与其中每个工作区的目录是 0700，属主是 NA 的运行用户"
        "（Docker 部署里是 root）。用 root 运行（sudo python3 ...）"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="把 CC 工作区的 home 搬回 0.4.7 之前的位置（退回旧版本前用）")
    parser.add_argument("--data-dir", default=os.environ.get("NEKRO_DATA_DIR", ""), help="NA 数据目录（默认读 NEKRO_DATA_DIR）")
    parser.add_argument("--apply", action="store_true", help="真的搬；不加只列出要做什么")
    parser.add_argument("--workspace", type=int, action="append", default=[], help="只处理这个编号（可以给多次）")
    parser.add_argument("--skip-docker-check", action="store_true", help="不查 Docker（确认这套部署的容器都停了再用）")
    parser.add_argument("--skip-process-check", action="store_true", help="不查 NA 进程（确认 NA 停了再用）")
    args = parser.parse_args(argv)

    if not args.data_dir:
        print("没有给数据目录：用 --data-dir 指定，或者设置 NEKRO_DATA_DIR", file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir)
    workspaces_root = data_dir / "workspaces"
    if not _is_real_dir(workspaces_root):
        print(f"{workspaces_root} 不存在或不是普通目录，数据目录对吗？", file=sys.stderr)
        return 2

    try:
        moves, skipped = plan(workspaces_root, args.workspace)
    except PermissionError as exc:
        print(_permission_hint(exc), file=sys.stderr)
        return 2
    for reason in skipped:
        print(f"跳过：{reason}")
    if not moves:
        print("没有要搬回的 home。")
        return 1 if skipped else 0

    if not args.apply:
        for workspace_id, source, target in moves:
            print(f"将搬回：工作区 {workspace_id}  {source} → {target}")
        print(f"共 {len(moves)} 个。确认 NA 与 CC 工作区容器都停了之后加 --apply 再跑一次。")
        return 1 if skipped else 0

    if not args.skip_process_check:
        processes = running_na_processes(data_dir)
        if processes is None:
            print("查不了进程（没有 /proc）。确认 NA 停了之后加 --skip-process-check", file=sys.stderr)
            return 2
        if processes:
            print("NA 还在跑，先停掉它：\n  " + "\n  ".join(processes), file=sys.stderr)
            return 2
    if not args.skip_docker_check:
        running = running_deployment_containers(data_dir)
        if running is None:
            print("查不了 Docker（没有 docker 命令或没有权限）。确认这套部署的容器都停了之后加 --skip-docker-check", file=sys.stderr)
            return 2
        if running:
            print(f"这套部署还有容器在跑（读写挂着数据目录），先停掉: {', '.join(running)}", file=sys.stderr)
            return 2

    failures = apply(moves)
    for failure in failures:
        print(f"失败：{failure}", file=sys.stderr)
    print(f"搬回 {len(moves) - len(failures)} 个，跳过 {len(skipped)} 个，失败 {len(failures)} 个。")
    if any("PermissionError" in failure for failure in failures):
        print("有的失败是权限不够：用 root 运行（sudo python3 ...）", file=sys.stderr)
    if len(moves) > len(failures):
        print("换回旧版本启动后，在控制台对每个 CC 工作区点一次「重建」（0.4.7 建的容器挂的是新位置）。")
    return 1 if (skipped or failures) else 0


if __name__ == "__main__":
    sys.exit(main())
