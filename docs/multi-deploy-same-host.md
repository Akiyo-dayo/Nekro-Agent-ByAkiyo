# 同一台宿主上跑多套 NekroAgent

> 这是**受支持的形态**，不是绕过去的障碍。本文说清：哪些名字是宿主全局的、
> 哪些会自动分开、哪些必须运维自己分开、以及升级时会遇到什么。
>
> 相关代码：`nekro_agent/core/os_env.py:deployment_scope()`、
> `nekro_agent/services/sandbox/network_guard.py`、`nekro_agent/services/workspace/network_guard.py`。

---

## 0. 一分钟版

给每套部署各自准备：

| 必须不同 | 怎么设 |
|---|---|
| **数据目录** | `NEKRO_DATA_DIR`（compose）/ `.env` 里的 `NEKRO_DATA_DIR`（裸机） |
| **对外端口** | `NEKRO_EXPOSE_PORT` |
| **数据库** | `NEKRO_POSTGRES_DATABASE`（或整套独立的 Postgres 实例） |
| **向量库** | `NEKRO_QDRANT_URL` 指向各自的 Qdrant —— 集合名是固定常量，共用一个 Qdrant 会串数据 |
| **compose 前缀** | `.env` 里的 `INSTANCE_NAME`（容器名/卷名/网络名都靠它前缀） |

**不必再手工设 `NEKRO_INSTANCE_NAME`**：留空时应用会从数据目录派生一个稳定后缀
（形如 `myfork-3f2a91c4-`），iptables 标记、Docker 网络名、网桥名、沙盒容器归属标签一律带上它。
想要一眼认得出的名字就显式设它，设了就以它为准。

启动日志第一时间会打出本次的作用域与推导来源：

```
沙盒网络隔离作用域 'naa-1f0c22ae-'（NEKRO_INSTANCE_NAME 未设置，由数据目录 /opt/na-a/data/nekro_agent 派生）；
规则标记 nekro-sandbox:naa-1f0c22ae-，专用网络 naa-1f0c22ae-nekro_sandbox_net（网桥 nksbx-1a2b3c4d）
```

`iptables-save` 里看到一个不认识的标记时，就靠这一行把它对回某一套部署。

---

## 1. 宿主全局命名空间盘点

「宿主全局」= 这个名字不属于某一套部署，而属于整台机器；两套部署取到同一个值就会互相影响。

### 1.1 应用自己管的（已经结构性分开）

| 命名空间 | 取值 | 撞了会怎样 |
|---|---|---|
| iptables/ip6tables 规则标记（沙盒） | `nekro-sandbox:{作用域}` | **静默致命**。安装语义是「按标记先全删再装自己那套」，后启动方会把先启动方的沙盒回调放行规则整组换掉 → 每次沙盒执行都恰好卡到运行超时，NA 侧零错误日志 |
| iptables 规则标记（CC 工作区） | `nekro-cc:{作用域}[.{分组}]` | 同上，瘫的是工作区的模型中继 |
| 沙盒专用 Docker 网络 | `{作用域}nekro_sandbox_net` | 两套的沙盒容器落在同一张网上，互相可达 |
| 沙盒网桥接口名 | `nksbx-{sha8}`（作用域为空时曾是 `nekro-sbx0`） | 规则按接口名匹配，同名等于两套规则打在同一批容器上 |
| CC 工作区网络 / 网桥 | `{作用域}nekro_cc_net[_{分组}]` / `nkcc-{sha9}` | 跨部署的工作区互通，租户隔离被穿透 |
| 沙盒容器归属标签 | `nekro.sandbox.instance={作用域}` | **静默致命**。启动时按这个标签 `kill + delete` 回收遗留容器，撞了就是「B 一重启，A 正在跑的沙盒全被杀」 |

这一组的作用域统一由 `deployment_scope()` 给出：**显式 `NEKRO_INSTANCE_NAME` 优先，
没设时由 `NEKRO_DATA_DIR` 的绝对路径派生**（`sha256` 前 8 位 + 一段可读的目录名）。
两套部署不可能共用数据目录，所以撞车在结构上不可能发生。

### 1.2 Docker 自己管的（不会撞，但共享额度）

| 命名空间 | 说明 |
|---|---|
| 子网网段 | 建网络时**不指定** subnet，由 Docker 守护进程的 `default-address-pools` 分配。同一个守护进程内保证互不重叠，两套部署并发建网也不会撞（分配在守护进程内串行完成） |
| 地址池容量 | 但**额度是共享的**：默认池 `172.17.0.0/12 → /16 ×16` + `192.168.0.0/16 → /20 ×16`，全宿主一共约 31 张 bridge 网络。两套部署各自的沙盒网 + 每个租户分组一张 CC 网，很容易逼近上限。撞上时 `docker network create` 失败，落到既有失败分支（`strict` 拒绝启动容器，`best_effort` 告警并退回改造前形态）。需要更多就放大 `/etc/docker/daemon.json` 的 `default-address-pools` |
| 沙盒容器名 | `nekro-agent-sandbox-{频道}-{随机 8 位十六进制}`，带随机段，不会撞 |
| CC 工作区容器名 | `nekro-cc-{随机 8 位十六进制}`，建之前还查一次重名，不会撞 |
| iptables 临时容器 | `nekro-netguard-{随机 12 位十六进制}`，不会撞；但标签 `nekro.netguard=1` 是共用的，`docker ps -aq -f label=nekro.netguard=1` 捞出来的残留会包含两套部署的 |

### 1.3 运维必须自己分开的（撞了会失败，多数是**响的**）

| 命名空间 | 取值 | 撞了会怎样 |
|---|---|---|
| 数据目录 | `NEKRO_DATA_DIR` | **必须不同**。沙盒共享目录、pip 缓存、动态包目录、插件目录、命令状态文件、工作区、日志全在它下面；共用等于两套部署互相覆盖。它同时是作用域的派生依据，共用还会让 1.1 那一整组重新撞回去 |
| 对外端口 | `NEKRO_EXPOSE_PORT` | 端口占用，启动失败（响） |
| Postgres 库 | `NEKRO_POSTGRES_DATABASE` / 各自的 Postgres | 共用一个库 = 两套部署的实例、频道、消息混在一起 |
| Qdrant | `NEKRO_QDRANT_URL` | **静默**。集合名是固定常量（`nekro_memory_paragraphs` / `nekro_kb_chunks` / `nekro_kb_library_chunks`），共用一个 Qdrant 就是共用集合 |
| compose 容器名/卷名/网络名 | `.env` 的 `INSTANCE_NAME` | `docker compose up` 当场报重名（响）。⚠️ 卷名例外：`{INSTANCE_NAME}nekro_postgres_data` / `nekro_guard_sock` 撞了会**共用同一个卷** |

### 1.4 仍然会撞、本轮**没有**修的（已知项）

| 命名空间 | 取值 | 撞的条件与后果 | 为什么没修 |
|---|---|---|---|
| 内置 NapCat 容器名 | `nekro_napcat_{instance_key}` —— **不带任何部署前缀** | 两套部署出现同名 `instance_key`（例如都手工建了 `inst1`）时容器名相同：先建的那套占着，后建的一方 409；更糟的是归属校验用 `INSTANCE_NAME or "default"`，两套都没设变量时**互相认为自己拥有对方的容器**，可以停/删/重启它 | 判据在 `services/console/container_manager.py`，不在本轮文件范围；且 owner 标签一改，存量容器会立刻变成「不归本部署所有」，需要一个迁移决定 |
| NapCat WebUI 宿主端口 | 36000–36999 随机，只与**自己库里**已用端口去重 | 两套部署可能选中同一个端口 → 容器启动失败（响，但报错指向 Docker 而不是这条原因） | 同上，且失败是响的 |
| CC 工作区宿主端口 | 40000–49999 随机，只与**自己库里**已用端口去重 | 同上 | 同上 |
| docker-guard 的容器归属判据（仅硬化部署） | 认标签键前缀 `nekro.`，目前不区分具体是哪套部署 | 同宿主跑多套硬化部署时，归属判断只到“是不是本系统的容器”这一级 | 细化到“按部署作用域精确匹配”需要先让各类容器的标签都带上作用域值，并处理存量容器的迁移，计划中。建议给每套部署设置不同的 `NEKRO_INSTANCE_NAME`，边界与建议配置见 `docs/docker-guard.md` |
| ~~`NEKRO_INSTANCE_NAME` 被当作 DNS 主机名~~（#47，**已修**） | `ws://{主机名}:{端口}/onebot/v11/ws` | 曾经：用 `docker-compose.yml` 且设了 `INSTANCE_NAME=foo` 时，NA 的容器名是 `foonekro_agent` 而不是 `foo`，内置 NapCat 反连不上 | round-26 把主机名拆成独立的 `NEKRO_SELF_HOSTNAME`（应用层三级回落）；round-27 补上 compose 这一半——三份 compose 都把它传进容器、默认值就是容器名，**运维零配置**，见 §2.1 |

---

## 2. 三种部署形态怎么配

### 2.1 Docker（`docker/docker-compose.yml` 与 `-x-hardened.yml`）

每套部署一份独立的目录 + `.env`：

```bash
# 第一套
export NEKRO_DATA_DIR=/srv/na-a/data
INSTANCE_NAME=na_a_          # 写进 .env；容器名会变成 na_a_nekro_agent 等
NEKRO_EXPOSE_PORT=8021

# 第二套
export NEKRO_DATA_DIR=/srv/na-b/data
INSTANCE_NAME=na_b_
NEKRO_EXPOSE_PORT=8022
```

`NEKRO_INSTANCE_NAME=${INSTANCE_NAME:-}` 与
`NEKRO_SELF_HOSTNAME=${NEKRO_SELF_HOSTNAME:-${INSTANCE_NAME:-}nekro_agent}`
这两行三份 compose（含 `-x-napcat.yml`）里都有，会把它们传到应用层。

> ✅ **内置 NapCat 的反连地址（#47，compose 下零配置）**：应用层不把
> `NEKRO_INSTANCE_NAME` 直接当 DNS 主机名——反连 host 统一走 `resolve_self_hostname()`
> 三级回落（`NEKRO_SELF_HOSTNAME` → `NEKRO_INSTANCE_NAME` → `nekro_agent`）。
> round-26 做了应用层，但**当时 compose 没把 `NEKRO_SELF_HOSTNAME` 传进容器**
> （`environment:` 是显式白名单、也没有 `env_file`），文档却让运维写进 `.env`，
> 于是那个值进不了容器、#47 在 Docker 下形同没修（round-27 发现并补上）。
> 现在每份 compose 都带这一行，**默认值就是该文件里 NA 的容器名**：设了
> `INSTANCE_NAME=na_a_` 之后容器名是 `na_a_nekro_agent`，反连主机名也是它，
> 加前缀与容器网络解析同时成立，**什么都不用再配**。想显式覆盖才在 `docker/.env` 里写
> `NEKRO_SELF_HOSTNAME=...`（空值等于没设）。
> ⚠️ **不要**把它写进仓库的 `.env.prod`：那份文件被打进镜像，`run_bot.py` 会把其中
> `NEKRO_` 前缀的键无条件覆盖进环境，抹掉 compose 传进来的值。
> 之前升级过 round-26 并手工在 `.env` 里写了 `NEKRO_SELF_HOSTNAME=${INSTANCE_NAME}nekro_agent`
> 的部署，这行写法与默认值逐字等价，可留可删。
> 钉住这件事的用例：`tests/test_compose_self_hostname.py`（每份定义 NA 服务的 compose 都含该行，
> 默认值在 Compose 插值之后逐场景等于该文件的 `container_name`）。

### 2.2 `docker/docker-compose-x-napcat.yml`

这份把 `NEKRO_INSTANCE_NAME` 设成 `${INSTANCE_NAME:-}nekro_agent` —— 与另两份**有意不同**，
因为这里的 NapCat 是 compose 服务、要按容器名反连 NA。容器名在宿主上本来就唯一，
所以作用域也天然分得开。多套部署只要给每套一个不同的 `INSTANCE_NAME` 即可。
它同样带 `NEKRO_SELF_HOSTNAME` 那一行（默认值与容器名一致，与另两份同形），
因此「每份定义 NA 服务的 compose 都传它」这条清单没有例外。

### 2.3 裸机 / 源码运行（`uv run bot --env=dev`）

作用域由 `NEKRO_DATA_DIR` 派生。默认值 `./data/nekro_agent` 是**相对路径**，按进程的当前
工作目录解析，所以两个 checkout 天然分开：

```
/opt/na-a  ->  /opt/na-a/data/nekro_agent  ->  naa-1f0c22ae-
/opt/na-b  ->  /opt/na-b/data/nekro_agent  ->  nab-9d34e771-
```

> ⚠️ 相对路径的代价：**换一个工作目录启动，作用域就变了**（systemd 的 `WorkingDirectory`
> 与手工 `cd` 不一致时最容易踩）。要稳就在 `.env` 里把 `NEKRO_DATA_DIR` 写成绝对路径，
> 或者显式设 `NEKRO_INSTANCE_NAME`。作用域变了不会坏事，只是会多留一组孤儿规则（见 §4）。

### 2.4 跑验证脚本 / 临时 checkout

同样算「第二套部署」。脚本自己在**任何 `nekro_agent` 导入之前**设
`NEKRO_INSTANCE_NAME=<自己的名字>`（`OsEnv.INSTANCE_NAME` 是导入期读一次的类属性，
设晚了没有任何效果），或者干脆 `SANDBOX_NETWORK_ISOLATION=off`。
脚本里按上面这样设置即可，不需要别的准备。

---

## 3. 一台机器能跑几套

限制不在 NA 而在 Docker 的地址池：默认约 31 张 bridge 网络，减去已有的业务网络，
每套部署至少要 1 张沙盒网 + 1 张平台方 CC 网 + 每个租户分组 1 张。
超出时 `docker network create` 失败，`best_effort` 会告警并退回改造前形态（**隔离就没了**），
`strict` 则拒绝启动容器。放大额度：

```json
// /etc/docker/daemon.json
{
  "default-address-pools": [
    {"base": "172.17.0.0/12", "size": 20},
    {"base": "10.201.0.0/16", "size": 24}
  ]
}
```

改完要重启 Docker 守护进程，已存在的网络不受影响。

---

## 4. 从旧版本升级：旧标记的孤儿规则

2026-08-12 之前，未设 `NEKRO_INSTANCE_NAME` 的部署一律用 `nekro-sandbox:default` /
`nekro-cc:default`。升级后作用域变成派生值，链上那组旧规则**不会被自动删除**，成为孤儿。

**为什么不自动删**：本进程分不清链上那组 `nekro-sandbox:default` 是「我自己升级前装的」
还是「同宿主上另一套**尚未升级**的 NA 正在用的」——两者的规则文本完全一样。
自动删除会在后一种情形下亲手复现本特性要消灭的那场事故（把邻居的沙盒回调放行规则整组删掉，
邻居从此每次沙盒执行都静默卡到超时，且没有任何日志线索）。
不删的代价很小：旧规则的匹配条件全是 `-i 旧网桥`，对挂在新网桥上的容器一律不生效，
只是占着十几行。

> ⚠️ **反过来，"删掉"有两个前提，缺一条就会出事：**
>
> 1. 同宿主上没有别的、**尚未升级**的 NA 还在用这个标记（上面那段的理由）。
> 2. **旧网桥上没有还在跑的容器。** 沙盒容器是一次性的，升级后不会再有新的挂上去；
>    但 **CC 工作区容器是长命的**，网络在**创建时**定死、重启不换网
>    （`services/workspace/container.py:create_and_start` 里的 `NetworkMode`）。
>    升级前建的工作区仍然挂在旧网桥上，而 `nekro-cc:default*` 那组规则里
>    **既有放行也有兜底 DROP**——按标记整组删掉之后，那些容器的隔离就**静默消失**了
>    （从此可以直接访问宿主内网、兄弟容器、Postgres/Qdrant），不报错、不留日志。
>    要清理就先把这些工作区**重建一遍**（重建会落到新网桥上），再删旧规则。
>
> 判断方法：`docker network inspect nekro_cc_net` 看 `Containers` 是不是空的
> （租户分组是 `nekro_cc_net_g<hash>`，`docker network ls | grep nekro_cc_net` 全列出来逐个看）。

启动时会检查并打一条 WARNING（只在真的有残留时打），里面直接给出下面这个入口。

### 清理入口：总后台 →「隔离规则」（`/console/network-rules`，平台方专属）

**不要手写 `iptables` 命令。** 这条路已经真的翻过车：`iptables -S` 输出里的
`--comment "nekro-cc:default"` **带引号**，`iptables-save | sed 's/^-A /-D /' | while read` 那种写法
经 shell 重新分词后引号会丢掉，`-D` 一条都匹配不上；而**半成功比全失败更糟** ——
被删掉的那半完全可能是还在生效的兜底 DROP（PROJECT_STATE §2.20）。

页面做三件事：

1. **盘点**：把链上每一个 `nekro-sandbox:*` / `nekro-cc:*` 标记列出来，附规则**原文**、
   涉及的链、以及规则里 `-i` 指向的网桥上**还有几个容器**（含已停止但 `NetworkMode` 仍钉在
   旧网络上的 —— 它一旦被启动就会挂回旧网桥）。
2. **判定**：给出 `本部署自用` / `已拦住` / `可清理` 三档，并把每一条拦截理由写出来。
3. **执行**：只有 `可清理` 的才给按钮，且要把标记**原样回填**作二次确认；
   后端在执行前会**重新判定一遍**（预览与确认之间完全可能有人把工作区启动起来），
   删除走产品自己的 `build_cleanup_commands` + 行号兜底，全程 argv 数组、不经过 shell。

判定成 `可清理` 需要同时满足（任一不成立即拦住，**没有强制按钮**）：

| 判据 | 拦住时怎么办 |
|---|---|
| 不属于本部署当前作用域（按作用域前缀判，覆盖全部分组） | —— 本部署自用的永远不给删 |
| 规则匹配的网桥上**没有任何容器** | 先把这些 CC 工作区**重建一遍**（重建会落到新网桥上），再回来清理 |
| 宿主上没有容器带着这个作用域的归属标签（`nekro.sandbox.instance` / `nekro.owner.scope`） | 先处理掉那些容器 |
| 同作用域的**沙盒**规则放行的 `(地址, 端口)` 上**没有进程在监听**（端口正好是本部署在**宿主上**占着的那个不算——见下面第五条注） | 那套部署可能还在跑；先把它停掉 |
| 上面任何一项读不出来 / 探测超时或不可达 / 该作用域没有沙盒规则可供判定 | 判不准就不给删，宁可少删 |

> ⚠️ 第二条为什么是硬拦：沙盒容器是一次性的，升级后不会再挂到旧网桥上；但 **CC 工作区容器是
> 长命的**，网络在**创建时**定死、重启不换网（`services/workspace/container.py:create_and_start`）。
> 升级前建的工作区**至今仍挂在旧网桥上**，而 `nekro-cc:default*` 那组规则里**既有放行也有兜底
> DROP** —— 按标记整组删掉之后，那些容器的隔离就**静默消失**了（可以直接访问宿主内网、兄弟容器、
> Postgres/Qdrant），不报错、不留日志、没有任何运行时信号。

> ⚠️ 第四条为什么必要：「不等于本部署的标记」**不足以**判定孤儿 —— 同宿主上另一套仍在运行的
> 部署，它的标记同样不等于本部署的。规则集里那些放行端点存在的唯一理由，就是让容器能回连
> 该端口上的那个 NA；所有者只要还活着就必然在监听它，端点明确 `ECONNREFUSED` 才说明它已经不在了。
> **已知限制**：所有者活着、但只 bind 了 `127.0.0.1` 而规则放行的是网桥网关地址时，
> 探测会得到 `ECONNREFUSED` 而可能误判成孤儿。这种部署的容器本来就连不上它的 NA（放行部分已是摆设），
> 且该部署的后台回读自检（`verify_marked_rules`，45 秒一轮）会在下一次起容器时重装被误删的规则。
> 清理前请确认同宿主的其他部署都已升级到带这道自检的版本。

> ⚠️ 「端口是本部署自己的就不算证据」那条括号，**只对宿主侧端口成立**。探测打的是宿主上的
> `(网桥网关, 端口)`，而 NA 绑的是 `0.0.0.0`，所以每一个网桥网关地址上都是我们在应答 ——
> 不豁免的话「清掉自己升级前那组规则」这个主用例永远拦在「还有人在听」上。
> 但**容器内**那个端口不能算：NA 在容器里时它 bind 的是另一个网络命名空间里的 8021，
> 宿主上的 8021 完全可能是同宿主上另一套**裸机**部署占着的（裸机默认端口正好也是 8021）。
> 所以豁免集合只有 `NEKRO_EXPOSE_PORT` 与 `relay_host_port()` 这两个按定义就在宿主侧的取值
> （`routers/console/network_rules.py:own_listening_ports`）。
> 顺带一条同源的：探测**自己失败**（超时、不可达、进程 fd 用尽导致建不出 socket）一律算「判不准」，
> **绝不能**折成「没人在听」—— 那等于让一次探测失败变成一次删除。

规则清掉之后，旧的专用网络可以用 `docker network rm nekro_sandbox_net nekro_cc_net` 一并删掉
（还有容器挂着时它会失败，是**响的**，别用 `|| true` 把它盖掉）。

顺带说明：**规则本来就不在 NA 退出时移除**——NA 崩溃退出不会走关闭流程，而那恰恰是最需要
防线的时刻。所以「装前先按标记清理」才是幂等的来源，退出时清理不是。

---

## 5. 排错

| 现象 | 先看什么 |
|---|---|
| 每条消息的沙盒都恰好卡到 `SANDBOX_RUNNING_TIMEOUT` | `iptables-save \| grep nekro-sandbox` 有几个不同的标记？本部署的那组还在不在？日志里有没有「沙盒隔离规则已被外部改写」 |
| 启动时本部署的沙盒容器被别人清掉 | `docker inspect <容器> \| grep nekro.sandbox.instance`，比对两套部署启动日志里的作用域 |
| `docker network create` 失败 | 地址池耗尽，见 §3 |
| 内置 NapCat 一直连不上 NA | 容器里的 `NEKRO_SELF_HOSTNAME` 是不是等于 NA 的容器名：`docker exec <NA 容器> printenv NEKRO_SELF_HOSTNAME`，与 `docker ps` 里的容器名逐字比对；见 §2.1 |
| 两套部署的记忆互相串 | `NEKRO_QDRANT_URL` 是不是指向了同一个 Qdrant（集合名是固定常量） |

查作用域最快的办法：

```bash
grep -m2 '网络隔离作用域' <本部署的启动日志>
```
