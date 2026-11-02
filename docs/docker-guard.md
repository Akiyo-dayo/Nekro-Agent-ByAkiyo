# docker-guard：docker.sock 硬化的 opt-in 部署选项

> 面向运维。讲清**为什么需要**、**怎么开**、以及**它挡住什么、挡不住什么**。
> 判据代码在 `nekro_agent/services/docker_guard/`，部署模板在 `docker/docker-compose-x-hardened.yml`。

## 一句话

把宿主 `docker.sock` 直挂进 NA 容器，等于把**宿主 root** 交给 NA 进程：任何能在 NA 里跑代码的
突破口（恶意插件、SSRF、反序列化……）都能 `POST /containers/create` 起一个 `Privileged` +
挂载宿主 `/` 的容器，直接读走 `/etc/shadow`、写 `/root`。docker-guard 是一层**看请求体**的薄反向
代理，夹在 NA 与真实 `docker.sock` 之间，只放行 NA 真正需要的动作和安全形状。

**这不是完整沙箱**，也不打算是。它把「trivial 一步拿宿主 root」收敛成「几条已登记的残余通道」，
换来的是最简部署（默认仍是直挂，行为逐字不变）。

## 为什么不用现成的 tecnativa/docker-socket-proxy

风险登记原文建议的方案。它的粒度是「整类 API」（`CONTAINERS=1`），**只按 URL 路径放行、不看
请求体**。而 NA 必须能 `POST /containers/create`（沙盒 / CC 工作区 / NapCat 都要建容器），一旦
放行这个端点，攻击者就能在请求体里塞 `Privileged:true` + `Binds:["/:/host"]`。**实测**（同机独立
lab）：经 `CONTAINERS=1,POST=1` 的 tecnativa 代理，`create`+`start` 均 201/204，容器读走宿主
`/etc/shadow`、写宿主 `/root`，与直连 socket 无差别。userns-remap 也不成立：`Privileged:true` 即
绕过 remap，且会破坏 NA 管宿主 iptables 与 bind root 属主目录。所以对 NA 这个威胁模型，路径级
代理≈没有——必须看请求体。

## 怎么开（compose 部署）

1. **构建 fork 镜像**（含 `nekro_agent.services.docker_guard` 模块；上游镜像没有它）：

   ```bash
   docker build -t my-nekro-fork:latest .
   ```

2. **切到硬化 compose**：

   ```bash
   export NEKRO_DATA_DIR=/your/abs/data/dir
   export NEKRO_IMAGE=my-nekro-fork:latest      # 必填，否则 compose 直接报错拒绝启动
   docker compose -f docker/docker-compose.yml down
   docker compose -f docker/docker-compose-x-hardened.yml up -d
   ```

   命名卷（`nekro_postgres_data` / `nekro_qdrant_data`）沿用，历史数据不丢。与标准 compose 的
   差异只有三处：新增 guard 边车（唯一持 `docker.sock:ro`，并把数据目录以**同一路径、只读**挂进来，
   见下面「guard 必须看得到数据目录」）、NA **移除** socket 直挂改挂共享卷、
   NA 增加 `DOCKER_HOST=unix:///run/nekro-guard/guard.sock`。

3. **回滚**：`docker compose -f docker/docker-compose-x-hardened.yml down` 后用标准 compose 起回来。

**裸机 / 不用 compose**：NA 直连宿主 `docker.sock` 时**行为分毫不变**——guard 是一个独立进程，
不启动它就等于没有这层。要在裸机上加这层：先 `uv run python -m nekro_agent.services.docker_guard`
起 guard（默认监听 `/run/nekro-guard/guard.sock`），再给 NA 设 `DOCKER_HOST` 指向它。
**解释器要用项目虚拟环境里的那个**（镜像里是 `/app/.venv/bin/python`，裸机上用 `uv run`）：
系统 `python` 没有 aiohttp / loguru，裸 `python -m …` 会直接 ModuleNotFoundError 退出。

### guard 必须看得到数据目录

guard 判定挂载源时要在**宿主路径**上逐级解析（见下面「它挡住什么」），所以边车需要把数据目录以
**同一个路径、只读**挂进来：硬化 compose 里 `nekro_docker_guard` 的
`- ${NEKRO_DATA_DIR}:${NEKRO_DATA_DIR}:ro`。guard 只做 `lstat` / `readlink`，从不读文件内容；只读挂载保证它改不了任何东西。

- **看不到数据目录就拒绝，不会退回字符串判断**：此时凡是挂在数据目录之下的 `create` / `start` / `restart`
  （沙盒、CC 工作区、NapCat）都会被 guard 拒绝，原因是 `root_unverifiable`。guard 启动时就会在日志里报
  `看不到允许的 bind 根目录` 的错误，不用等沙盒第一次起不来才发现。
- **从旧版硬化 compose 升级**时必须同步加上这一行，否则升级后沙盒、CC 工作区、NapCat 都起不来
  （直接用新镜像、不动 compose 的部署会这样）。自己写编排文件的同理。
- 裸机上自己起 guard 进程时，guard 直接看得到宿主文件系统，不需要额外配置。
- `NEKRO_DOCKER_GUARD_EXTRA_BIND_ROOTS` 里列的额外根同样要以同一路径挂进边车。

### 必须给 NA 自己的容器打归属标签

归属门只放行带 `nekro.*` 标签的容器，而 **NA 自己这个容器不是 NA 建的**（是 compose 或运维建的），
没有任何代码会给它打标签。NA 却有三条路径要碰自己这个容器：

| 路径 | 少了标签的后果 |
|---|---|
| `tools/docker_util.py:restart_self` | 后台「重启系统」失败 |
| `services/sandbox/network_guard.py:resolve_self_container` | 认不出自身镜像 → 起不了 iptables 临时容器 → **沙盒网络隔离静默退化成告警** |
| `services/console/container_manager.py:_resolve_nekro_network` | 探测不到自身网络 → 回落到硬编码 `nekro_network` → NapCat 被接到错的网络上 |

硬化 compose 里已经给 `nekro_agent` 服务加了 `labels: nekro.owner.scope / nekro.owner.role`。
**自己写编排文件时别漏这一段**，否则一开硬化就把上面三条一起打断。

### 监听形态与端口

guard 默认监听 **unix socket**（`NEKRO_DOCKER_GUARD_LISTEN_SOCKET`，默认 `/run/nekro-guard/guard.sock`），
**不开任何 TCP 端口**。这个 socket 等于宿主 Docker 的半个控制面。若确有跨容器/跨主机需求要走 TCP
（设 `NEKRO_DOCKER_GUARD_LISTEN_HOST` + `NEKRO_DOCKER_GUARD_LISTEN_PORT`），**必须绑 `127.0.0.1`**，
绝不能对外暴露。

### 相关环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `NEKRO_DATA_DIR` | 合法 bind source 的根：沙盒/CC/NapCat 的挂载都必须在其下，越界即拒；guard 需以同一路径只读挂载它 | — |
| `NEKRO_DOCKER_GUARD_EXTRA_BIND_ROOTS` | 额外允许的 bind 根（`:` 分隔），少数部署把数据分散在多目录时用；同样要以同一路径挂进边车 | 空 |
| `NEKRO_DOCKER_GUARD_UPSTREAM` | 真实 docker.sock 路径 | 自动探测 `/var/run/docker.sock` |
| `NEKRO_DOCKER_GUARD_LISTEN_SOCKET` | guard 对 NA 暴露的 unix socket | `/run/nekro-guard/guard.sock` |
| `NEKRO_DOCKER_GUARD_LISTEN_HOST`/`_PORT` | 改走 TCP（务必 `127.0.0.1`） | 空（用 unix socket） |

## 它挡住什么

判据 `nekro_agent/services/docker_guard/policy.py`（纯函数，`tests/test_docker_guard_policy.py`
表驱动覆盖）。**白名单端点**据「NA 实际用到的 Docker 面」推导，多一个都不放：容器
list/create/inspect/logs/start/stop/restart/kill/wait/delete/exec，exec 句柄 inspect/start/resize，
镜像 inspect/pull，网络 inspect/create/connect，系统 version/info/_ping。其余（commit/update/build/
events/volumes/swarm/archive/镜像删除…）一律 deny。

- **`create` 请求体逐字段判据**：拒 `Privileged`、host `PidMode/IpcMode/UsernsMode/CgroupnsMode/UTSMode`、
  **`container:<ref>` 形态的命名空间共享**（借用别的容器的 PID/网络命名空间 → 经 `/proc/<pid>/root`
  读写对方文件系统）、`CgroupParent`、`Devices`/`DeviceCgroupRules`、`Sysctls`、`Runtime`、
  `Mounts` 与 **`VolumesFrom`**（两条绕过 Binds 的路，后者整份继承别的容器的挂载表——宿主上
  只要有一个挂了 `docker.sock` 的容器就等于宿主 root）、`SecurityOpt=*unconfined`、越界 `CapAdd`；
  bind source 必须落在 `NEKRO_DATA_DIR` 下（`/etc/localtime` 只读豁免）。
  **判定的是真实路径，不是字符串前缀**（`nekro_agent/services/docker_guard/bindpath.py`）：guard 在宿主路径语义下
  逐级解析 source——中间目录、`..`、多级与悬空的符号链接都算——解析过程中**任何一步**离开数据目录就拒绝
  （`escapes_data_dir`），解析不了（权限、链接环、根目录不可见）同样拒绝（`unresolvable` / `root_unverifiable`），
  **不回落到字符串判断**。通过后 guard 把 source **换成解析后的规范路径**再转发给 Docker，转发的是判过的那一份。
  容器 `start` / `restart` 前再对挂载源复核一次（Docker 在那一刻才真正解析它们）。
  数据目录里指向数据目录**内**的符号链接可以用（挂载时会被换成真实路径）；指向目录外的一律拒绝。
  数据目录本身是符号链接（运维常见布局）不受影响，它的真实路径也被接受。
- **请求体按「精确键白名单」校验，键名大小写变体一律拒绝**：Docker 用 Go `encoding/json` 解码，**键名大小写不敏感**
  （`hostconfig` / `HOSTCONFIG` 都解到 `HostConfig`，还认 Unicode 折叠：`ſ`(U+017F) 当 `s`、`K`(U+212A) 当 `k`），
  重复键后者覆盖前者（嵌套对象合并）；判据若只读精确键 `HostConfig`，
  `{"Image":"a","hostconfig":{"Binds":["/:/host"],"Privileged":true}}` 在判据眼里就是「没有 HostConfig」。
  所以 create 顶层、`HostConfig`、`RestartPolicy`，以及 exec 创建 / exec 启动 / 网络 connect 的请求体，
  都按精确键白名单校验：键在白名单里才过；只是大小写（含上面的折叠字符）不同的键、以及白名单之外的未知键
  一律拒绝（`*.key_case_variant` / `*.key_unknown`）。**不就地归一化**：`Labels` / `Env` 这类 map 的键是大小写敏感的数据，
  不参与校验、不被改写。通过后转发的是**判过的结构**（重新序列化，重复键由此消除），不是客户端原始字节——
  包括 exec 与网络 connect（`{"Cmd":["sh"],"privileged":true}`、`{"Container":"自己的","container":"别人的"}` 都是同一类绕过）。
  **推论**：白名单用 Docker 的规范拼写。NA 自己的请求体已按规范拼写（沙盒配置原先的 `NanoCPUs` 改成了 `NanoCpus`，
  `tests/test_docker_guard_policy.py` 会静态扫四处建容器的字面量防回退）；docker CLI / docker-py 发的本来就是规范拼写。
  真实客户端的两处不规范写法单独放行并在转发前规范化：docker-py 与 aiodocker 的 exec 请求体都带 `Container`（容器只由 URL
  决定，转发时丢掉），aiodocker 把 `DetachKeys` 写成 `detachKeys`（改写成 `DetachKeys`；两种拼写同时出现按变体拒绝）。
  请求体里含**孤立的 UTF-16 代理项**（`\ud800` 这类）一律拒绝：Python 照单全收，Go 把它换成 U+FFFD，同一个字符串两边看到的不是同一个值。
- **白名单内的字段也要有「值」的判据**：键在 Docker 的字段表里，不等于值安全。`MaskedPaths` / `ReadonlyPaths` 是
  `--security-opt systempaths=unconfined` 的 API 形式（`[]` 会把 `/proc`、`/sys` 的敏感路径露出来），**只允许 null**；
  `SecurityOpt` 里的 `label=disable` / `label:disable`（关闭 SELinux 隔离）拒绝；NA 从不使用、却有提权 / 逃逸能力的字段——
  `DeviceRequests`（GPU / 设备）、`Annotations`、`Links`、`GroupAdd`、`VolumeDriver`、`Cgroup`、`ContainerIDFile`、`Isolation`、
  `OomScoreAdj`、`LogConfig`（只许不带选项的 json-file / local / none）和顶层的 `NetworkingConfig.EndpointsConfig`——只允许空值。
  只改资源 / 网络参数的字段（`Memory`、`NanoCpus`、`PortBindings`、`ExtraHosts`、`Dns*`、`Tmpfs`、`Ulimits` 等）保持放行；
  `tests/test_docker_guard_policy.py` 要求白名单里的每个字段要么有判据、要么在测试里登记为有意放行，防止再出现「躺在白名单里没人管」的字段。
- **`POST /containers/{id}/update` 只为一件事开**：把 `RestartPolicy` 改成 `no`（`{"RestartPolicy":{"Name":"no"}}`），
  经过归属门；改资源配额、把重启策略改成别的值一律拒绝。用途见「硬化部署下 NapCat 怎么保活」里的启动迁移。
- **Docker Engine ≤26 的扁平 HostConfig 写法**：`ContainerConfigWrapper` 内嵌了 HostConfig，请求里**没有** `HostConfig` 键时，
  顶层的 `Binds` / `Privileged` 照样生效（`{"Image":"a","Binds":["/:/host"],"Privileged":true}`）。
  create 顶层只收 `container.Config` 的字段（外加 `HostConfig` / `NetworkingConfig`），出现 HostConfig 的字段名
  （含大小写变体）即拒（`create.hostconfig_flat`）。
- **API 版本前缀低于 v1.24 一律拒绝，start / restart 不许带请求体**：API <1.24 的 start 会把请求体解码成 HostConfig，
  而 guard 对 start 不看请求体却原样转发，等于 create 时的全部判据可以在 start 时被另一份 HostConfig 覆盖。
  NA 与 docker CLI 都不会给 start / restart 发体，拒绝不误伤。
- **带 Binds 的容器只允许 `RestartPolicy=no`（或不写）**：restart policy 触发的重启由 Docker 守护进程自己做，不经过 guard，
  start / restart 前的挂载源复核管不到它——创建一个 `always` 且绑 `<数据目录>/x` 的容器，再把 `x` 换成指向 `/` 的链接，
  等容器退出，守护进程重启它时就把宿主根目录挂了进去。所以 create 时拒绝 `always` / `unless-stopped` / `on-failure`
  （`create.restart_policy.with_binds`）。**需要保活的容器由 NA 经 guard 重新拉起**：见下面「硬化部署下 NapCat 怎么保活」。
- **工作量与超时有上限**：单个 create 最多 32 条 bind（NA 自己最多 4 条）；整个请求共享一份 bind 解析预算
  （按处理的路径分量计，符号链接一步另算；耗尽即拒绝 `too_complex`），单条 source 不超过 4096 字符；
  解析（`lstat` / `readlink`）放在专用小线程池里跑，**带 5 秒超时，超时 / 异常一律拒绝**（`policy.timeout` / `policy.error`），
  不会因为一次慢的文件系统调用（NFS、被卡住的挂载点）拖住整个 guard。
- **路径必须逐字节判到、逐字节转发**：含 `.`/`..` 段的路径一律 deny，转发时用
  `URL(..., encoded=True)` 且不跟随上游重定向。原因是 `yarl` 在构造 URL 时**会自己消解 `..`**：
  `GET /images/x/../../containers/<兄弟容器>/json` 曾被判成 `images.inspect`（镜像引用本就含 `/`，
  这条端点不查归属）放行，转发时却变成对**任意容器**的 inspect —— 归属门被从另一个端点整条绕过，
  `POSTGRES_PASSWORD` 直接读走。判据与转发之间只要存在任何路径改写，白名单就形同虚设。
- **exec 请求体也判**：`POST /containers/{id}/exec` 的 `Privileged`（会补齐全部 capability 并摘掉
  seccomp/AppArmor）与 create 那侧同名同义，同样拒绝。
- **host 网络只放行 netguard 形状**：`NetworkMode=host` 仅当「打了 `nekro.netguard=1` 标签 +
  能力只有 NET_ADMIN/NET_RAW + 实际执行的程序是 iptables + 不挂任何宿主目录」。**注意**判据看的是
  **实际执行的 argv0**——`Entrypoint` 非空时它覆盖 `Cmd`，所以 `Cmd=[iptables]` 配 `Entrypoint=
  [/bin/sh,-c,…]` 这种「Cmd 摆样子、Entrypoint 塞 shell」的绕过也会被拒（本轮实测发现并修复）。
- **exec / inspect / logs / start / stop / restart / kill / wait / delete 只作用于本部署的容器**
  （`proxy.py` 的归属门，`tests/test_docker_guard_proxy.py` 覆盖）。不加这道门，攻击者虽然建不出
  特权容器，却能 `exec` 进宿主上**别人**的容器——比如挂了 `docker.sock` 的兄弟容器 → 一步宿主
  root；或 `inspect` 兄弟 `postgres` 容器读走 `POSTGRES_PASSWORD`（平台方凭据）。归属判据：目标容器
  带 `nekro.*` 标签才算本部署（NA 建的沙盒/CC/NapCat/netguard 容器都带；第三方容器不带）。
  guard 亲手 create 出来的容器 ID 走快表，长命容器（NapCat/CC 工作区）跨 guard 重启走 inspect+标签兜底。
  归属门的第二个出口在**请求体**里：`POST /networks/{id}/connect` 的目标容器写在 body 的 `Container`
  字段上，同样过归属判定（引用字符集也校验——它会被拿去拼 inspect URL）。
  **归属门拒绝时回的是 404 而不是 403**：`_is_owned_container` 分不清「不是本部署的」和「压根不存在」，
  而 NA 有一批分支正靠 Docker 的 404 判断「容器还没建」（NapCat 首次创建、镜像 pull 前的 inspect、
  onebot 候选容器名遍历）。回 403 会让这些分支永远走不到；回 404 既保住这些路径，也不泄露存在性
  （与项目「越权 404」的约定一致）。

## 硬化部署下 NapCat 怎么保活

标准部署里内置 NapCat 容器建成 `RestartPolicy=unless-stopped`，崩溃 / OOM / 宿主重启后由 Docker 拉起。
**走 guard 时 NapCat 建成 `RestartPolicy=no`**（带 Binds 的容器不能交给守护进程自动重启，见上），保活改由 NA 做：

- NA 通过 `NEKRO_DOCKER_GUARD_ACTIVE=true`（硬化 compose 已写；不设时按 `DOCKER_HOST` 里的 `nekro-guard` / `guard.sock` 推断）
  知道自己在 guard 后面；**标准 compose 不走 guard，行为逐字不变**（仍是 `unless-stopped`，实例元数据也不多出任何键）。
- 走 guard 时，控制台对 NapCat 的创建 / 启动 / 重启会在实例元数据里记 `desired_state=running`，停止 / 删除记 `stopped`。
- 实例健康监测的探活周期里，NA 起一个**独立的保活任务**（不挡探活）跑 `reconcile_container_liveness`：
  对 `desired_state=running` 却已退出的 NapCat 容器，经 guard 发普通的 `POST /containers/{id}/start`
  （与控制台「启动」按钮同一条路径，过归属门与挂载源复核）。每个实例并发处理、各有 30 秒时限
  （某个实例的 Docker 调用挂住，只让它自己超时退避，不影响别的实例和探活）。
- **退避**：每次拉起（无论成败）都把下次允许拉起的时刻往后推（30 秒起翻倍、封顶 10 分钟），**拉起后跑满 2 分钟才清零**，
  所以崩溃循环会持续退避，而不是每个探活周期拉一次。
- **只拉起「期望在跑」的**：控制台点过「停止」的不会被拉回来（动作发出**之前**就先记下期望；一轮保活期间点了停止，刚拉起的容器会被停回去）；
  **没有记录的存量容器不碰**。这也意味着：在宿主上直接 `docker stop` 一个 NapCat，NA 会在下一个探活周期把它拉起来；要停请用控制台的「停止」。
- 保活的粒度是探活周期（`INSTANCE_HEALTH_CHECK_INTERVAL_SECONDS`），比 Docker 的立即重启慢；宿主重启后要等 NA 自己起来
  （NA 的容器仍是 compose 管理的 `restart: unless-stopped`，不受影响）并过一个探活周期。

### 旧硬化部署 / 从标准部署切过来：存量 NapCat 容器的自动迁移

升级前建的内置 NapCat 容器是 `unless-stopped` 且带 Binds（既可能是标准部署建的，也可能是旧版 guard 放行过的）：守护进程会自己重启它，
重启时的挂载源解析不经过 guard。**走 guard 的 NA 每次启动都会扫一遍本部署的内置 NapCat 容器**，对 `RestartPolicy` 不是 `no` 的：

- 经 guard 发 `POST /containers/{id}/update`（`{"RestartPolicy":{"Name":"no"}}`，guard 只放行这一种 update）改成 `no`；
- 若实例还没有 `desired_state` 记录，补记（正在跑 = `running`，已退出 = `stopped`），之后由上面的保活接管；
- **失败打 ERROR 并点名容器和实例**（日志前缀 `[RestartPolicy]`）：这时它仍会被守护进程自动重启、不经过 guard，
  请在控制台把该实例的容器「删除」再「创建」一次（数据目录保留，QQ 登录态不丢）。

只处理归属校验通过的内置 NapCat 容器；NA 自己的容器（带 `nekro.owner.*` 标签、`restart: unless-stopped`）不在扫描范围，也不能被 guard 的 update 之外的路径改动。

### 回滚到标准 compose

回滚后 NA 不再走 guard，也就没有保活：**在 guard 下建成 `RestartPolicy=no` 的 NapCat 容器崩溃或宿主重启后不会自己回来**。
回滚后的 NA 启动时会对每个这样的容器打 WARNING（日志前缀 `[RestartPolicy]`，点名容器）。处理：回滚后在控制台把每个内置 NapCat 实例的容器
「删除」再「创建」一次（数据目录保留），新容器是 `unless-stopped`，由 Docker 保活。标准部署本身的行为不变。

## 硬化部署下用不了的功能（开之前必须知道）

**交互式 `docker exec` 不可用。** `Detach=false` 的 exec 走 HTTP 劫持（`Connection: Upgrade` +
裸双向流），而 guard 是普通 HTTP 反向代理：它剥掉逐跳头、把上游响应重新按分块编码发回，既没有把
客户端后续写入的 stdin 送上去的通道，也会让 docker 的 raw-stream 帧和分块帧混在一起。所以 guard
**明确拒绝**这类请求（`rule=exec.start.hijack_unsupported`）而不是让它挂住或吐乱码——
§2.6 教训②：不可诊断的挂起比明确失败糟得多。

受影响的两条产品路径：

- `services/mcp/validator.py` 的 **MCP stdio 服务器校验**（`stdin=True` 的 exec 握手）
- `routers/workspaces.py` 的 **CC 工作区网页终端**（`docker exec -it`）

沙盒执行（`create`→`start`→`wait`→`logs`）、NapCat 全生命周期、网络隔离都**不**走劫持流，不受影响。
要这两个功能就先别开硬化，或者到宿主上直接 `docker exec` 进容器。补齐它需要 guard 支持 HTTP
劫持的双向裸流转发（目前尚未实现）。

**实测对照**（同机独立 lab，`127.0.0.1:19399`，不碰运行中的部署、不改防火墙）：接管前
`Privileged+Binds:/:/host` 容器成功读 `/etc/shadow`、`exec` 进外部容器拿到 `uid=0(root)`；接管后
**同一批请求**——挂宿主路径 / Privileged / pid=host / network=host+shell / CAP_SYS_ADMIN / --device /
exec 进外部容器 / build / get_archive / Entrypoint 塞 shell——**全部 403 且无容器泄漏**，而沙盒
create→start→exec→delete、netguard 合法 iptables 形状、带 `nekro.*` 标签的长命容器操作全部照常。

## 边界与建议配置

它**不是完整沙箱**：guard 只按“已知的 Docker API 调用形状”收敛请求，目的是把攻击面从“一步到宿主 root”缩小，而不是消除。建议这样用：

1. **默认部署的边界不变**：不开这层，应用仍然直接持有宿主 `docker.sock`（有意——换最简部署）。需要更严的边界才开硬化，并且仍应把“应用进程内能执行不可信代码”当作需要额外防线的前提。
2. **别让不可信内容落在会被当作挂载源的目录里**（尤其是 `NEKRO_DATA_DIR` 之下），并保持该目录的写权限尽量收窄。
3. **同宿主多套部署**：给每套设置不同的 `NEKRO_INSTANCE_NAME`（以及不同的数据目录与端口，见 [`multi-deploy-same-host.md`](./multi-deploy-same-host.md)）。
4. **guard 与应用之间的 unix socket 等价于半个 Docker 控制面**：共享卷只挂给这两个服务，不要再挂给别的容器；确有跨主机需求而改走 TCP 时，必须绑 `127.0.0.1`。
5. 需要真正的硬隔离时，请使用 rootless Docker、gVisor 或独立宿主。

