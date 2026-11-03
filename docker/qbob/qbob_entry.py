"""qbob 容器入口：首启生成配置、把数据目录交给 qbob 用户、降权后启动 qbob

只用标准库，也**不 import qbob 的任何模块**（许可边界见 docs/qbob-integration.md §4）。

## 为什么要自己生成配置

qbob 的 `config.json` 不展开环境变量，而 NekroAgent 托管它需要几处与上游默认不同的取值
（表见 docs/qbob-integration.md §5.2）。这里只写这几项，其余键交给 qbob 首启时补默认值并写回。
配置文件已存在时**不覆盖**：运维在 qbob 管理台或文件里改过的东西不能被重启抹掉。

## 为什么 db / 媒体 / 日志都写绝对路径

数据目录在 qbob 与 NA 两个容器里按**同一路径**挂载。qbob 的 `get_file` 返回的是本地路径，
路径一致 NA 才能直接读；NA 读首启 admin 口令文件也靠这个。qbob 把口令文件写在
`db_path` 的上级目录，所以 `db_path` 必须落在数据目录根下。

## 权限

qbob 的库里是明文 AppSecret。数据目录收紧到 0700、qbob 以 umask 077 运行，宿主上的其他账户读不到；
NA 容器以 root 运行，不受影响。
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

QBOB_HOME = Path("/app/qbob")
QBOB_USER_UID = 10001
CONFIG_NAME = "config.json"
ADMIN_PORT = 17810
PUBLIC_PORT = 17800
HTTP_API_PORT = 17820


def build_config(data_dir: str, public_base_url: str = "") -> Dict[str, Any]:
    """NekroAgent 托管所需的配置项（只含与上游默认不同、或必须指定的键）"""
    root = Path(data_dir)
    return {
        # 公网面（webhook、转发网页）在容器内监听；compose 默认不映射到宿主
        "host": "0.0.0.0",
        "port": PUBLIC_PORT,
        # NA 经容器网络访问管理面；宿主映射由 compose 决定且只绑回环
        "admin_host": "0.0.0.0",
        "admin_port": ADMIN_PORT,
        "admin_on_public": False,
        # OneBot HTTP API 本期不用，只留容器内本机
        "http_api_host": "127.0.0.1",
        "http_api_port": HTTP_API_PORT,
        "public_base_url": public_base_url,
        "db_path": str(root / "qqbot_onebot.db"),
        "media_dir": str(root / "media"),
        "log_file": str(root / "logs" / "qqbot.log"),
        # 升级 = 改 QBOB_REF 重新构建；管理台的 git pull 自更新关掉
        "update_check_hours": 0,
        "update_repo": "",
        # 「创建bot」聊天指令的号主确认用于 qbob 自助接入；NA 托管的 bot 由 NA 后台接入
        "ask_owner": False,
    }


def ensure_config(config_path: Path, data_dir: str, public_base_url: str = "") -> bool:
    """配置文件不存在时生成，返回是否新写了文件"""
    if config_path.exists():
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_name(config_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(build_config(data_dir, public_base_url), fh, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)
    return True


def sync_public_base_url(config_path: Path, public_base_url: str) -> bool:
    """`QBOB_PUBLIC_BASE_URL` 设了就以它为准（首启之后才填也要生效）；没设不动，返回是否改了文件"""
    if not public_base_url:
        return False
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if data.get("public_base_url") == public_base_url:
        return False
    data["public_base_url"] = public_base_url
    tmp = config_path.with_name(config_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)
    return True


def hand_over(data_dir: Path, uid: int, gid: int) -> None:
    """把数据目录交给 qbob 用户；顶层已归它时视为已交接，不再逐个文件 chown"""
    if data_dir.stat().st_uid == uid:
        return
    for dirpath, dirnames, filenames in os.walk(data_dir):
        os.chown(dirpath, uid, gid)
        for name in dirnames + filenames:
            os.chown(os.path.join(dirpath, name), uid, gid, follow_symlinks=False)


def drop_privileges(uid: int, gid: int) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main() -> None:
    data_dir = os.environ.get("QBOB_DATA_DIR", "").strip()
    if not data_dir or not os.path.isabs(data_dir):
        sys.exit("qbob_entry: QBOB_DATA_DIR 必须是绝对路径（与 NA 容器里的挂载路径一致）")
    os.umask(0o077)
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / CONFIG_NAME
    # 数据目录对 qbob 用户可写：它被攻破后能把 config.json 换成指向容器内任意文件的符号链接，
    # 下次重启入口以 root 写配置 / chown 就会作用到链接目标上。直接拒绝，不跟随
    if config_path.is_symlink():
        sys.exit(f"qbob_entry: 拒绝启动：{config_path} 是符号链接（可能被篡改），请检查数据目录")
    public_base_url = os.environ.get("QBOB_PUBLIC_BASE_URL", "").strip()
    if ensure_config(config_path, data_dir, public_base_url):
        print(f"qbob_entry: 已生成 {config_path}", flush=True)
    elif sync_public_base_url(config_path, public_base_url):
        print("qbob_entry: 已按 QBOB_PUBLIC_BASE_URL 更新 public_base_url", flush=True)

    if os.geteuid() == 0:
        hand_over(root, QBOB_USER_UID, QBOB_USER_UID)
        # 上面两步以 root 写的配置文件：顶层目录早已交接时 hand_over 不会再逐个 chown，单独交一次
        os.chown(config_path, QBOB_USER_UID, QBOB_USER_UID, follow_symlinks=False)
        os.chmod(root, 0o700)
        drop_privileges(QBOB_USER_UID, QBOB_USER_UID)

    os.chdir(QBOB_HOME)
    os.execv(sys.executable, [sys.executable, "-m", "qqbot_onebot", "--config", str(config_path)])


if __name__ == "__main__":
    main()
