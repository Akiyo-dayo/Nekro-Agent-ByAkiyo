"""沙盒 pip 缓存挂载点与镜像 `PIP_CACHE_DIR` 必须指向同一处

这两者曾经长期不一致：`runner.py` 一直把宿主机目录挂到容器的 `/app/.pip_cache`，而镜像
从未设过 `PIP_CACHE_DIR`。容器以 `nobody` 运行，`nobody` 的 HOME 是 `/nonexistent`，
pip 找不到可写的默认缓存目录就把缓存整体关掉（`pip cache dir` 直接报
"cache is disabled"），挂进去的目录一个字节都没被写过。

这种不一致在运行时完全没有信号：装依赖照样成功，只是每次都重新下载。所以这里把
"挂载点 == 镜像里的 PIP_CACHE_DIR"钉成一条可断言的契约——任何一侧被改动而另一侧没跟上，
缓存都会重新变成摆设。
"""

import re
from pathlib import Path

import pytest

from nekro_agent.services.sandbox.runner import CONTAINER_PIP_CACHE_DIR

SANDBOX_DOCKERFILE = Path(__file__).resolve().parent.parent / "sandbox" / "dockerfile"


def read_dockerfile() -> str:
    """把 dockerfile 里的续行折叠掉，便于按单条指令匹配"""
    return SANDBOX_DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")


def test_image_pip_cache_dir_matches_mount_point() -> None:
    """镜像声明的 PIP_CACHE_DIR 必须正好是 runner 挂载缓存的那个容器路径"""
    matches = re.findall(r"\bPIP_CACHE_DIR=(\S+)", read_dockerfile())

    assert matches, "sandbox/dockerfile 未设置 PIP_CACHE_DIR，挂载的 pip 缓存不会被 pip 使用"
    assert set(matches) == {CONTAINER_PIP_CACHE_DIR}, (
        f"镜像里的 PIP_CACHE_DIR={matches} 与挂载点 {CONTAINER_PIP_CACHE_DIR} 不一致"
    )


def test_image_creates_writable_pip_cache_dir() -> None:
    """没有挂载时镜像自带的缓存目录也要建出来并可写

    本地跑镜像、或将来某个调用路径不挂这个卷时，`nobody` 仍然需要一个能写的缓存目录，
    否则又退回"缓存整体失效"。
    """
    text = read_dockerfile()
    mkdir_lines = [line for line in text.splitlines() if "mkdir -p" in line and CONTAINER_PIP_CACHE_DIR in line]
    chmod_lines = [line for line in text.splitlines() if "chmod" in line and CONTAINER_PIP_CACHE_DIR in line]

    assert mkdir_lines, f"镜像未创建 {CONTAINER_PIP_CACHE_DIR}"
    assert chmod_lines, f"镜像未放开 {CONTAINER_PIP_CACHE_DIR} 的写权限（容器以 nobody 运行）"


# ---------------------------------------------------------------------------
# 缓存必须**按实例**切开（#23 的取舍就建立在这一条上）
#
# #23 选的是「按实例隔离的可写缓存」而不是「跨实例共享的只读缓存」，理由正是
# **wheel 投毒面靠实例隔离来控**：pip 默认不校验哈希，共享可写缓存下 A 实例污染一个 wheel，
# B 实例装上就直接执行。而在此之前这条契约**一条断言都没有**——上面两条只钉了
# 「挂载点 == 镜像的 PIP_CACHE_DIR」，把 `_get_instance_pip_cache_dir` 改回返回共享根目录
# （它历史上就是这样），全部测试照样绿，隔离面静默消失。
# 同一个文件里的 `.packages` 目录是同一种形态（沙盒把它插到 sys.path 最前面），一并钉住。
# ---------------------------------------------------------------------------

from nekro_agent.services.sandbox.runner import (  # noqa: E402
    _get_instance_package_dir,
    _get_instance_pip_cache_dir,
)

# (chat_key, 期望的目录名)。落到 fallback 桶的两类要与正常实例分得开。
INSTANCE_DIR_CASES = [
    ("onebot_v11-ob10001-group_123456", "ob10001"),
    ("onebot_v11-ob10002-group_123456", "ob10002"),
    ("onebot_v11-ob10001-private_42", "ob10001"),  # 同实例的不同会话共用一个桶（轴是实例，不是会话）
]


@pytest.mark.parametrize("chat_key,expected", INSTANCE_DIR_CASES)
@pytest.mark.parametrize("resolve", [_get_instance_pip_cache_dir, _get_instance_package_dir])
def test_caches_are_split_per_instance(resolve, chat_key: str, expected: str) -> None:
    path = resolve(chat_key)
    assert path.name == expected, f"{resolve.__name__}({chat_key!r}) 落在 {path.name!r}，期望 {expected!r}"
    # 目录名必须是**实例的最后一段**，不能是缓存根本身——否则就是所有实例共用一个可写目录
    assert path.parent.name != expected


@pytest.mark.parametrize("resolve", [_get_instance_pip_cache_dir, _get_instance_package_dir])
def test_two_instances_never_share_a_cache_dir(resolve) -> None:
    a = resolve("onebot_v11-ob10001-group_1")
    b = resolve("onebot_v11-ob10002-group_1")
    assert a != b, f"{resolve.__name__} 让两个实例落进了同一个可写目录：{a}"


@pytest.mark.parametrize("resolve", [_get_instance_pip_cache_dir, _get_instance_package_dir])
@pytest.mark.parametrize(
    "bad_key",
    [
        "onebot_v11-../../etc-group_1",  # 路径穿越
        "onebot_v11-OB10001-group_1",    # 大写不合法（instance_key 是 ^[a-z0-9_]{1,32}$）
        "not-a-chat-key",
        "",
    ],
)
def test_invalid_instance_keys_fall_into_the_shared_bucket_not_out_of_the_root(resolve, bad_key: str) -> None:
    """解析不出合法实例段时落进共享桶，且**不得穿出缓存根**。"""
    root = resolve("onebot_v11-ob10001-group_1").parent
    path = resolve(bad_key)
    assert path.parent == root, f"{resolve.__name__}({bad_key!r}) 穿出了缓存根: {path}"
    assert ".." not in path.parts
