import contextlib
import hashlib
import os
import re
import secrets
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, Mapping, Optional

from .core_utils import OsEnvTypes

# Postgres 口令的**出厂默认值**。
#
# 单独抽成常量而不是写在下面的 `OsEnvTypes.Str(...)` 里，是为了让「检查口令是不是还是出厂默认」
# 的那段代码（`core/logger.py:audit_factory_default_credentials`）与这里共用**同一个字面量**。
# 判据写成两份的风险不是多几行，而是只改一份（§2.10：判据与被判值的形态一起变了，只改一侧
# = 守卫从此永不成立）。
#
# ⚠️ 这个值逐字等于包名 `nekro_agent`，于是任何「扫一遍磁盘上有没有口令泄漏」的工具都会
# 得到成千上万条命中（测试部署上是 1729 个文件），**扫描在结构上无法把它与正常文本区分开**。
# 也就是说：它一旦没换又泄漏了，不会有任何告警提示你。这正是启动期要单独告警一次的理由。
FACTORY_DEFAULT_POSTGRES_PASSWORD = "nekro_agent"

# 仓库 `.env.example` 历史上写过的两个「看起来能用」的 JWT 签名密钥示例值（round-28 起示例文件已改成占位符）。
# 不少部署是手抄那份示例起家的（测试部署就是），所以启动审计仍要认得它们；判据与被判值共用这一处字面量（§2.10）。
LEGACY_EXAMPLE_JWT_SECRETS: FrozenSet[str] = frozenset(
    {"dev-secret-key-for-development", "dev-refresh-key-for-development"},
)

# 示例文件里「必须替换」的占位值统一以它开头（`.env.example` 的 JWT 两把与 ADMIN 口令）。
# 启动审计认这个前缀（不分大小写）：逐字照抄示例的部署会在启动日志里被点名。
SECRET_PLACEHOLDER_PREFIX = "CHANGE_ME"


def resolve_signing_secret(env_key: str, prefix: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """JWT 签名密钥的生效值：环境变量里有真值就用它，**没设或为空 / 纯空白都当没设**，回落每进程随机

    为什么「空串 = 没设」必须显式处理：`OsEnvTypes.Str` 对已设置的空串原样返回，而
    `.env` 里一行 `NEKRO_JWT_SECRET_KEY=`、或 compose 把 `${NEKRO_JWT_SECRET_KEY:-}` 透传成空值，
    都会让 `os.environ` 里出现一个空串——那就成了「空的 HMAC 密钥」，任何人都能自签令牌，
    比仓库示例值还糟。随机兜底的代价只是重启后全员重登，安全的一侧。

    Args:
        env_key: 不带 `NEKRO_` 前缀的键名，如 `JWT_SECRET_KEY`
        prefix: 随机兜底值的前缀（`secret` / `refresh`，沿用原来的形态，日志里一眼能看出是兜底）
        environ: 取值来源，缺省为 `os.environ`（测试注入用）
    """
    source = os.environ if environ is None else environ
    value = source.get(f"NEKRO_{env_key.upper()}", "")
    if value.strip():
        return value
    return f"{prefix}:{secrets.token_urlsafe(32)}"


class OsEnv:
    """系统变量"""

    """实例名称

    ⚠️ 这个值被**当作主机名**用过两处：`services/console/container_manager.py:build_managed_entry`
    与 `adapters/onebot_v11/routers.py:ADAPTER_TEMPLATE` 都拿它拼 `ws://{值}:{端口}/onebot/v11/ws`，
    让内置 NapCat 反连 NA。所以它不是一个可以随便改口径的自由标识 ——
    要给「宿主全局命名空间」找一个本部署专属的前缀，用下面的 `deployment_scope()`，
    不要改这里的取值；反连地址的主机名已拆成独立的 `SELF_HOSTNAME`（见下，#47）。
    """
    INSTANCE_NAME: str = OsEnvTypes.Str("INSTANCE_NAME", default="")

    """NA 自身的 DNS 主机名（#47 的显式可配项）

    内置 NapCat 反连 NA 的 `ws://{主机名}:{端口}/onebot/v11/ws` 从前直接拿上面的
    `INSTANCE_NAME` 当主机名，而 compose 里 NA 的容器名是 `${INSTANCE_NAME}nekro_agent` ——
    于是「给宿主全局命名空间加前缀（`NEKRO_INSTANCE_NAME=na_a_`）」与「反连地址在容器网络里
    解析得到 NA 容器」二选一（#47）。这里把「NA 的主机名」拆成独立可配项：`docker/*.yml`
    里 NA 服务默认传 `NEKRO_SELF_HOSTNAME=${NEKRO_SELF_HOSTNAME:-${INSTANCE_NAME:-}nekro_agent}`
    （逐字等于该文件里 NA 的容器名），两件事即可同时成立，运维零配置。compose 的
    `environment:` 是显式白名单，不写这一行时 `.env` 里的值进不了容器
    （`tests/test_compose_self_hostname.py` 钉住每份定义 NA 服务的 compose 都含它）。
    ⚠️ 不要加进 `.env.prod`：`run_bot.py` 会把其中 `NEKRO_` 前缀的键无条件覆盖进环境。

    取值一律走下面的 `resolve_self_hostname()`（SELF_HOSTNAME → INSTANCE_NAME →
    `nekro_agent` 三级回落），**不要**在写入点直接读这个字段或 `INSTANCE_NAME`：
    `routers.py:ADAPTER_TEMPLATE` 分支改造前就是裸读 `INSTANCE_NAME` 无回落，
    空值写出 `ws://:8021/...` 这种必然连不上的 URL（#47 顺带修掉的真 bug）。
    默认不配时逐级回落，`build_managed_entry` 的产出逐字不变。
    """
    SELF_HOSTNAME: str = OsEnvTypes.Str("SELF_HOSTNAME", default="")

    """数据目录"""
    DATA_DIR: str = OsEnvTypes.Str("DATA_DIR", default="./data/nekro_agent")  # `~/srv/nekro_agent` In Docker

    """Postgres 数据库配置"""
    USE_ENV_DATABASE: str = OsEnvTypes.Str("USE_ENV_DATABASE", default="false")
    POSTGRES_HOST: str = OsEnvTypes.Str("POSTGRES_HOST", default="localhost")
    POSTGRES_PORT: int = OsEnvTypes.Int("POSTGRES_PORT", default=5432)
    POSTGRES_USER: str = OsEnvTypes.Str("POSTGRES_USER", default="nekro_agent")
    POSTGRES_PASSWORD: str = OsEnvTypes.Str("POSTGRES_PASSWORD", default=FACTORY_DEFAULT_POSTGRES_PASSWORD)
    POSTGRES_DATABASE: str = OsEnvTypes.Str("POSTGRES_DATABASE", default="nekro_agent")

    """Qdrant 数据库配置"""
    USE_ENV_QDRANT: str = OsEnvTypes.Str("USE_ENV_QDRANT", default="false")
    QDRANT_URL: str = OsEnvTypes.Str("QDRANT_URL", default="http://localhost:6333")
    QDRANT_API_KEY: str = OsEnvTypes.Str("QDRANT_API_KEY", default="")

    """JWT 配置

    两把签名密钥**没设或为空都回落每进程随机**（`resolve_signing_secret`），不再是
    `OsEnvTypes.Str` 那种「已设置的空串原样返回」——空的 HMAC 密钥等于任何人都能自签令牌。
    启动审计见 `core/logger.py:audit_factory_default_credentials`。
    """
    JWT_SECRET_KEY: str = resolve_signing_secret("JWT_SECRET_KEY", "secret")
    JWT_REFRESH_SECRET_KEY: str = resolve_signing_secret("JWT_REFRESH_SECRET_KEY", "refresh")
    """部署密钥（`/register`、`/create` 的第二重证明）

    和 JWT 两把同一个口径：`NEKRO_SUPER_ACCESS_KEY` 设了真值就用它，**没设 / 空 / 纯空白都回落每进程随机**
    （没人知道值 = fail-closed；绝不能变成空串，否则 `access_key=""` 的请求就通过校验）。

    ⚠️ 以前写成 `OsEnvTypes.Str(..., default=lambda: ...)`：`OsEnvTypes.Str` 遇到可调用默认值根本不读环境变量，
    `NEKRO_SUPER_ACCESS_KEY` 设了也读不到。现在读得到了——存量部署如果早就在 `.env` 里留过这一项
    （当时不生效），升级后它就是真的第二重证明，值太弱就该换掉。
    """
    SUPER_ACCESS_KEY: str = resolve_signing_secret("SUPER_ACCESS_KEY", "super")
    ACCESS_TOKEN_EXPIRE_DAYS: int = OsEnvTypes.Int("ACCESS_TOKEN_EXPIRE_DAYS", default=7)
    ENCRYPT_ALGORITHM: str = OsEnvTypes.Str("ENCRYPT_ALGORITHM", default="HS256")

    """凭据静态加密密钥（模型组 `api_key` 在数据库里的加密密钥）

    **默认空串是有意的，且空串不等于不加密。** 没配这一项时，
    `tools/credential_cipher.py` 会在数据目录里自动生成一把密钥文件（0600）并用它加密，
    于是"什么都不配的部署"仍然落在"库 dump 读不出 Key"这一侧；配了这一项则密钥完全
    不落在数据目录里（备份整个数据目录也带不走），是推荐形态。

    ⚠️ **刻意不复用 `JWT_SECRET_KEY`**：那两把是**要求轮换**的
    （见 docs/operations.md §1.1 与 #40），而数据加密密钥一旦轮换，
    库里所有存量密文当场作废。把"该轮换的"和"轮换即丢数据的"绑在一起是个陷阱。

    ⚠️ 它挡的是「库被 dump / 备份泄漏」，**挡不住宿主被拿下**——密钥就在同一台机器的
    环境变量里。完整威胁模型见 `tools/credential_cipher.py` 的模块文档。
    """
    CREDENTIAL_SECRET_KEY: str = OsEnvTypes.Str("CREDENTIAL_SECRET_KEY", default="")

    """RPC 配置"""
    RPC_SECRET_KEY: str = OsEnvTypes.Str("RPC_SECRET_KEY", default=f"rpc:{secrets.token_urlsafe(32)}")

    """Webhook 配置"""
    WEBHOOK_SECRET_KEY: str = OsEnvTypes.Str("WEBHOOK_SECRET_KEY", default=f"webhook:{secrets.token_urlsafe(32)}")

    """loguru 的 `diagnose`（异常 traceback 里逐行渲染局部变量）开关

    **默认关闭**。loguru 自己的文档就点名这个选项"生产环境应关闭，会带出敏感数据"：
    它渲染的是**每一行源码里出现的名字连同取值**，那份文本不经过任何插值，
    因此项目里所有按值/按形态的脱敏（`services/agent/upstream_redaction.py`、
    `tools/redaction.py`）**一条都作用不到它**。实证：测试部署 `app.log` 里现存 50 处
    `ConnectionParameters(user=…, password=…)`，就是 tortoise/asyncpg 连库失败时
    被诊断行打出来的**数据库口令**。

    取值：`true/1/yes/on` 开，`false/0/no/off`（以及不设置、空串、认不出的值）关。
    **默认值刻意选在"关"这一侧**——什么都不配的部署必须落在安全的那一档，
    而开发机是一个会主动去配环境的地方（在 `.env.dev` 里写 `NEKRO_LOG_DIAGNOSE=true`）。

    刻意放环境变量、不进 WebUI 配置（与 `ALLOW_PRIVATE_NETWORK_DOWNLOAD` 同理）：
    这是平台方的安全边界，不应该由租户管理员在后台点一下就打开。

    关掉它**不会**丢掉排障能力：异常类型、异常消息、完整调用栈（含 `backtrace` 的扩展帧）
    逐字都在，少掉的只有"每个名字当时等于什么"这几行。
    """
    LOG_DIAGNOSE: str = OsEnvTypes.Str("LOG_DIAGNOSE", default="")

    """其他配置"""
    RUN_IN_DOCKER: bool = OsEnvTypes.Bool("RUN_IN_DOCKER")

    """暴露端口"""
    EXPOSE_PORT: int = OsEnvTypes.Int("EXPOSE_PORT", default=8021)

    """前端资源目录"""
    STATIC_DIR: str = OsEnvTypes.Str("STATIC_DIR", default="./static")

    """WebUI 管理员密码"""
    ADMIN_PASSWORD: str = OsEnvTypes.Str("ADMIN_PASSWORD", default="")

    """Nekro Cloud API"""
    NEKRO_CLOUD_API_BASE_URL: str = OsEnvTypes.Str("NEKRO_CLOUD_API_BASE_URL", default="https://cloud.nekro.ai")
    # NEKRO_CLOUD_API_BASE_URL: str = OsEnvTypes.Str("NEKRO_CLOUD_API_BASE_URL", default="http://localhost:8787")

    """OPENAPI 配置"""
    ENABLE_OPENAPI_DOCS: bool = OsEnvTypes.Bool("ENABLE_OPENAPI_DOCS")

    """数据库迁移"""
    AUTO_DB_MIGRATE: bool = OsEnvTypes.Bool("AUTO_DB_MIGRATE", default=True)

    """外部资源下载安全
    默认拒绝下载指向内网/回环/链路本地地址的 URL，防止聊天消息中的图片链接被用于 SSRF 探测。
    仅当部署环境确实需要从内网地址取资源时才开启，且必须理解这会把内网暴露给任意消息发送方。
    刻意不放进 WebUI 配置：这是安全边界，不应由（未来的）租户管理员自行关闭。
    """
    ALLOW_PRIVATE_NETWORK_DOWNLOAD: bool = OsEnvTypes.Bool("ALLOW_PRIVATE_NETWORK_DOWNLOAD")

    """额外视为"非内网"的网段（逗号分隔 CIDR）

    默认放行 `198.18.0.0/15`：Shadowrocket / Clash 等透明代理把域名的 DNS 结果替换成这一段里的
    Fake-IP，真实流量由代理按原域名转发，这个地址并不指向部署方网络里的任何一台主机。
    详细取舍见 `nekro_agent/tools/common_util.py` 中 `DEFAULT_EXTRA_ALLOWED_NETWORKS` 的注释。

    置空即可恢复"只认 is_global"的严格行为；也可以填自己的内网单机地址（如 `10.0.0.5/32`），
    这比直接开 ALLOW_PRIVATE_NETWORK_DOWNLOAD 放开整个内网要安全得多。
    """
    DOWNLOAD_EXTRA_ALLOWED_NETWORKS: str = OsEnvTypes.Str(
        "DOWNLOAD_EXTRA_ALLOWED_NETWORKS",
        default="198.18.0.0/15",
    )


# =============================================================================
# 部署身份：宿主全局命名空间的前缀
# =============================================================================
#
# 一台宿主上跑两套 NA 是**受支持的形态**（见 docs/multi-deploy-same-host.md）。
# 但有一批名字并不属于任何一套部署，而是属于**宿主**——iptables 规则的 `--comment` 标记、
# Docker 网络名与网桥接口名、沙盒容器的归属标签。这些名字从前只带 `INSTANCE_NAME`，
# 而它默认是空串，于是两套部署会撞在同一个名字上。
#
# 撞车的代价不对称：compose 层的名字（容器名、卷名）撞了会让 `docker compose up` 当场失败，
# 一眼就能看见；而 iptables 标记撞了是**静默**的——`install_marked_rules` 的语义是
# 「先按标记全删、再装自己那套」，后启动的一方会把先启动一方的沙盒回调放行规则整组换掉，
# 表现只有「每一次沙盒执行都恰好卡到运行超时」，NA 侧一条错误日志都没有
# （真实事故，见 PROJECT_STATE §2.6，排查花了九个多小时）。
#
# 所以这里给出一个**结构上不可能相撞**的作用域：显式设了 `NEKRO_INSTANCE_NAME` 就用它，
# 没设就从部署身份派生一个稳定后缀。

# 派生时被跳过的路径段：它们在每一套部署里都长一样，拿来当「这是哪套」毫无区分度
_GENERIC_PATH_PARTS: FrozenSet[str] = frozenset(
    {
        "",
        "data",
        ".data",
        "nekro_agent",
        "nekroagent",
        "nekro-agent",
        "srv",
        "app",
        "var",
        "opt",
        "usr",
        "home",
        "root",
        "mnt",
        "media",
    },
)

_SCOPE_SLUG_PATTERN = re.compile(r"[^a-z0-9_]")
_SCOPE_SLUG_MAX_LEN = 12


def _scope_slug(part: str) -> str:
    """把一段目录名收成可读的 slug

    首字符收敛到字母或数字（`lstrip("_")`）：这个前缀会原样拼进 **Docker 网络名**
    （`{作用域}nekro_sandbox_net`），而 Docker 的名字校验要求首字符是字母或数字。
    以前这个前缀是运维在 `NEKRO_INSTANCE_NAME` 里自己写的、错了当场看得见；
    现在是**代码自己生成**的，一个叫 `/opt/_staging/na` 的部署就能让建网失败，
    而 `best_effort` 会把建网失败当成「隔离没装上」吞掉并回落 —— 隔离静默消失。
    不赌这一把，直接把首字符掐掉。
    """
    return _SCOPE_SLUG_PATTERN.sub("", part.strip().lower()).lstrip("_")[:_SCOPE_SLUG_MAX_LEN]


@lru_cache(maxsize=32)
def derive_deployment_scope(data_dir: str) -> str:
    """从数据目录派生出本部署的稳定后缀

    **为什么锚在数据目录上**：它是两套部署唯一**不可能**共用的东西——沙盒共享目录、
    pip 缓存、插件目录、命令状态文件、工作区、日志全在它下面，共用一份等于两套部署互相踩。
    而「部署根路径」在 Docker 形态下不成立：两套部署的容器里 cwd 都是 `/app`，
    只有 bind 进来的 `NEKRO_DATA_DIR` 是各自不同的。
    默认值 `./data/nekro_agent` 是相对路径，解析后自然就带上了各自的部署根目录。

    **必须稳定**：只用路径本身，不掺 pid / 时间 / 随机数——同一套部署重启前后必须算出同一个值，
    否则每次重启都会换一套标记，旧规则一轮轮堆成孤儿。

    Args:
        data_dir: `OsEnv.DATA_DIR` 的取值（相对路径按当前工作目录解析）

    Returns:
        形如 `myfork-3f2a91c4-` 的后缀：前半段是路径里第一个有区分度的目录名（给人看），
        后半段是整条绝对路径的 sha256 前 8 位（保证不同路径必然不同），
        结尾的 `-` 与 `${INSTANCE_NAME}nekro_network` 这类拼接习惯保持一致
    """
    try:
        resolved = Path(data_dir).expanduser().resolve()
    except OSError:  # pragma: no cover - resolve 在极端权限下才会抛
        resolved = Path(data_dir)
    digest = hashlib.sha256(resolved.as_posix().encode("utf-8")).hexdigest()[:8]

    # 跳过路径锚（POSIX 的 `/`、Windows 的 `C:\`）：它不承载任何部署身份，
    # 而且两个平台上长得完全不一样，算进去会让同一份测试在两边得出不同结论
    parts = resolved.parts[1:] if resolved.anchor else resolved.parts
    slug = ""
    for part in reversed(parts):
        if part.strip().lower() in _GENERIC_PATH_PARTS:
            continue
        slug = _scope_slug(part)
        if slug:
            break
    return f"{slug or 'na'}-{digest}-"


def deployment_scope() -> str:
    """本部署在宿主全局命名空间里的身份前缀

    iptables 标记、Docker 网络名/网桥名、沙盒容器归属标签一律用它，**不要**直接用
    `OsEnv.INSTANCE_NAME`（那个值同时被当作 NA 的 DNS 主机名，见上面的警告）。

    调用时读、不在 import 期定死：测试要能 `monkeypatch.setattr(OsEnv, "INSTANCE_NAME", ...)`
    换掉它（§2.9：fake 里写死由环境变量派生的值，换台机器就在测另一件事）。

    Returns:
        显式 `NEKRO_INSTANCE_NAME` 的原值；未设置时是由数据目录派生出的稳定后缀
    """
    return OsEnv.INSTANCE_NAME or derive_deployment_scope(OsEnv.DATA_DIR)


def describe_deployment_scope() -> str:
    """一句话说清本次作用域是怎么来的，给启动日志用

    运维在 `iptables-save` 里看到 `nekro-sandbox:myfork-3f2a91c4-` 时，要能靠启动日志
    把它对回某一套部署——所以这句话必须同时给出取值与**推导来源**。
    """
    if OsEnv.INSTANCE_NAME:
        return f"{OsEnv.INSTANCE_NAME!r}（来自显式的 NEKRO_INSTANCE_NAME）"
    scope = derive_deployment_scope(OsEnv.DATA_DIR)
    try:
        source = Path(OsEnv.DATA_DIR).expanduser().resolve().as_posix()
    except OSError:  # pragma: no cover
        source = OsEnv.DATA_DIR
    return f"{scope!r}（NEKRO_INSTANCE_NAME 未设置，由数据目录 {source} 派生）"


def resolve_self_hostname() -> str:
    """NA 自身在容器网络里的 DNS 主机名 —— 内置 NapCat 反连地址的 host 部分（#47）

    三级回落：显式 `NEKRO_SELF_HOSTNAME` → `NEKRO_INSTANCE_NAME` → `nekro_agent`
    （compose 里 NA 容器名 `${INSTANCE_NAME}nekro_agent` 的默认后缀）。

    这是反连 URL 两个写入点（`services/console/container_manager.py:build_managed_entry`
    与 `adapters/onebot_v11/routers.py:ADAPTER_TEMPLATE` 分支）取主机名的**唯一**口径 ——
    写入点各自读环境变量的后果就是口径分叉：第二个写入点改造前裸读
    `OsEnv.INSTANCE_NAME` 无回落，空值写出 `ws://:8021/...` 必然连不上的 URL。

    调用时读、不在 import 期定死：测试要能 `monkeypatch.setattr(OsEnv, "SELF_HOSTNAME", ...)`
    换档（§2.9，与 `deployment_scope()` 同一条理由）。

    Returns:
        三级回落里第一个非空的值；默认不配 SELF_HOSTNAME 时逐字等于改造前的
        `OsEnv.INSTANCE_NAME or "nekro_agent"`
    """
    return OsEnv.SELF_HOSTNAME or OsEnv.INSTANCE_NAME or "nekro_agent"


APP_SYSTEM_DIR: str = OsEnv.DATA_DIR + "/system"  # 系统目录
USER_UPLOAD_DIR: str = OsEnv.DATA_DIR + "/uploads"  # 用户资源上传目录
SANDBOX_SHARED_HOST_DIR: str = OsEnv.DATA_DIR + "/sandboxes"  # 沙盒共享目录
SANDBOX_PIP_CACHE_DIR: str = OsEnv.DATA_DIR + "/sandboxes/.pip_cache"  # 沙盒动态 PIP 缓存目录
SANDBOX_PACKAGE_DIR: str = OsEnv.DATA_DIR + "/sandboxes/.packages"  # 沙盒动态包目录
PLUGIN_DYNAMIC_PACKAGE_DIR: str = OsEnv.DATA_DIR + "/plugins/.dynamic_packages"  # 插件动态包目录
PROMPT_LOG_DIR: str = OsEnv.DATA_DIR + "/logs/prompts"  # 提示词日志目录
PROMPT_ERROR_LOG_DIR: str = OsEnv.DATA_DIR + "/logs/prompts_error"  # 提示词错误日志目录
APP_LOG_DIR: str = OsEnv.DATA_DIR + "/logs/app"  # 应用日志目录
MEMORY_PARSE_LOG_DIR: str = APP_LOG_DIR + "/memory_parse_failures"  # 记忆沉淀解析失败转储目录
BUILTIN_PLUGIN_DIR: str = "plugins/builtin"  # 内置插件目录
WORKDIR_PLUGIN_DIR: str = OsEnv.DATA_DIR + "/plugins/workdir"  # 本地插件目录
PACKAGES_DIR: str = OsEnv.DATA_DIR + "/plugins/packages"  # 云端插件目录
NAPCAT_TEMPFILE_DIR: str = OsEnv.DATA_DIR + "/napcat_data/QQ/NapCat/temp"  # NapCat 临时文件目录
NAPCAT_ONEBOT_ADAPTER_DIR: str = OsEnv.DATA_DIR + "/napcat_data/napcat"  # NapCat OneBot 适配器目录
WALLPAPER_DIR: str = OsEnv.DATA_DIR + "/wallpapers"  # 壁纸目录
WORKSPACE_ROOT_DIR: str = OsEnv.DATA_DIR + "/workspaces"  # cc-sandbox 工作区根目录
SKILLS_DIR: str = OsEnv.DATA_DIR + "/skills"  # 全局 skill 资源库根目录
SKILLS_LOCAL_DIR: str = SKILLS_DIR + "/local"  # 平台方独立技能（手动创建/上传/晋升）
SKILLS_REPOS_DIR: str = SKILLS_DIR + "/repos"  # 平台方订阅仓库（git clone）
SKILLS_TENANTS_DIR: str = SKILLS_DIR + "/tenants"  # 租户自有技能库根目录（每租户一层 t{tenant_id}）
BUILTIN_SKILLS_SOURCE_DIR: str = str(Path(__file__).parent.parent / "builtin_skills")  # 内置 skill 源目录


def resolve_deployment_onebot_access_token() -> str:
    """部署级 OneBot 协议端接入令牌的**唯一**解析口径。

    这是「NA 下发给协议端的令牌」与「NA 校验协议端时期望的令牌」共用的那个值，
    两侧口径分叉的后果不是报错而是**协议端一条都连不上**，所以只留这一个入口。

    ## 为什么不能只写 `os.getenv("ONEBOT_ACCESS_TOKEN", "")`（改造前就是这么写的）

    `run_bot.py` 把 env 文件导进 `os.environ` 时**只导 `NEKRO_` 前缀的键**，
    而 `ONEBOT_ACCESS_TOKEN` 是 NoneBot 侧的键、没有这个前缀。于是：

    - **Docker 部署**：compose 里 `- ONEBOT_ACCESS_TOKEN=${ONEBOT_ACCESS_TOKEN:-}`
      把它作为真正的容器环境变量传进来，`os.getenv` 读得到；
    - **裸机部署**（`bot --env=prod` / `--env=dev`）：它只存在于 `.env.*` 文件里，
      `os.getenv` **恒为空串**，而 NoneBot 自己是直接读 env 文件的
      （`nonebot/config.py:DotEnvSettingsSource.__call__`：
      `env_vars = {**env_file_vars, **env_vars}` —— os.environ 覆盖 env 文件，缺席时用文件值）。

    也就是说在裸机上把 `.env.prod` 的 `ONEBOT_ACCESS_TOKEN` 填上之后，
    NoneBot 会开始校验令牌，而 NA 这边读到的是空串，于是它把**空令牌**写进 NapCat 的
    `onebot11.json`（`container_manager.build_managed_entry` / `routers.ADAPTER_TEMPLATE`）——
    两边都不报错，只是协议端从此握手 403。这是「按上线清单填上那一刻全部断连」的成因之一。

    ## 解析顺序

    1. **已注册的 OneBot V11 适配器实例上的那份配置**。这是 NoneBot 自己在
       `Adapter._check_access_token` 里拿来比对的**同一个对象**，因此按定义不会分叉，
       连 `ONEBOT_V11_ACCESS_TOKEN` 这个别名也一并覆盖（见 `onebot/v11/config.py`）。
    2. 适配器还没注册时（单元测试、离线建实例路径）退回 `os.environ`。

    返回空串表示「本部署没有配置部署级令牌」。
    """
    token = _driver_onebot_access_token()
    if token is not None:
        return token
    return os.getenv("ONEBOT_ACCESS_TOKEN", "").strip()


def _driver_onebot_access_token() -> Optional[str]:
    """读活着的 OneBot V11 适配器配置；适配器不可用时返回 `None`（区别于「配了空串」）"""
    try:
        import nonebot
        from nonebot.adapters.onebot.v11 import Adapter as OneBotV11NoneBotAdapter

        adapter = nonebot.get_adapter(OneBotV11NoneBotAdapter)
    except Exception:  # noqa: BLE001 - 驱动未初始化 / 适配器未注册 / 依赖缺席都按「读不到」处理
        return None
    value = getattr(getattr(adapter, "onebot_config", None), "onebot_access_token", None)
    if value is None:
        return ""
    # `nekro_log_hygiene` 在 `nonebot.init()` 期间会把凭据类键临时换成 SecretStr 并在
    # `register_adapter` 之前还原；这里对两种形态都成立，免得还原顺序一变就静默读到掩码
    secret_value = getattr(value, "get_secret_value", None)
    if callable(secret_value):
        return str(secret_value()).strip()
    return str(value).strip()

# =============================================================================
# Timer / Calendar data paths (under DATA_DIR)
# =============================================================================
TIMER_SYSTEM_DIR: str = APP_SYSTEM_DIR + "/timer"
TIMER_ONE_SHOT_PERSIST_PATH: str = TIMER_SYSTEM_DIR + "/one_shot_timers.json"

CALENDAR_SYSTEM_DIR: str = APP_SYSTEM_DIR + "/calendar"
CALENDAR_CN_HOLIDAY_DIR: str = CALENDAR_SYSTEM_DIR + "/cn_holidays"

# =============================================================================
# Command data paths (under DATA_DIR/configs)
# =============================================================================
COMMAND_STATE_DIR: str = OsEnv.DATA_DIR + "/configs/command_states"
COMMAND_SYSTEM_STATE_FILE: str = COMMAND_STATE_DIR + "/system.json"
COMMAND_CHANNEL_STATE_DIR: str = COMMAND_STATE_DIR + "/channels"
COMMAND_SYSTEM_PERMISSION_FILE: str = COMMAND_STATE_DIR + "/system_permissions.json"
COMMAND_CHANNEL_PERMISSION_DIR: str = COMMAND_STATE_DIR + "/channel_permissions"


# 设置上传目录及其子目录权限
with contextlib.suppress(Exception):
    Path(USER_UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    subprocess.run(["chmod", "-R", "755", USER_UPLOAD_DIR], check=True)
    print(f"Set permission of {USER_UPLOAD_DIR} to 755")
