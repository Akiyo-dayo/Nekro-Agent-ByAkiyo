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
   差异只有三处：新增 guard 边车（唯一持 `docker.sock:ro`）、NA **移除** socket 直挂改挂共享卷、
   NA 增加 `DOCKER_HOST=unix:///run/nekro-guard/guard.sock`。

3. **回滚**：`docker compose -f docker/docker-compose-x-hardened.yml down` 后用标准 compose 起回来。

**裸机 / 不用 compose**：NA 直连宿主 `docker.sock` 时**行为分毫不变**——guard 是一个独立进程，
不启动它就等于没有这层。要在裸机上加这层：先 `uv run python -m nekro_agent.services.docker_guard`
起 guard（默认监听 `/run/nekro-guard/guard.sock`），再给 NA 设 `DOCKER_HOST` 指向它。
**解释器要用项目虚拟环境里的那个**（镜像里是 `/app/.venv/bin/python`，裸机上用 `uv run`）：
系统 `python` 没有 aiohttp / loguru，裸 `python -m …` 会直接 ModuleNotFoundError 退出。

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
| `NEKRO_DATA_DIR` | 合法 bind source 的根：沙盒/CC/NapCat 的挂载都必须在其下，越界即拒 | — |
| `NEKRO_DOCKER_GUARD_EXTRA_BIND_ROOTS` | 额外允许的 bind 根（`:` 分隔），少数部署把数据分散在多目录时用 | 空 |
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

