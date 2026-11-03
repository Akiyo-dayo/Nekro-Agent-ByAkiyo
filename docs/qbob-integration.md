# qbob 附带部署与总后台托管（设计）

- 状态：设计已确认（2026-11-02），实现中

## 1. 背景与结论

[qbob](https://github.com/Loping151/qbob)（`qqbot-onebot`）把 **QQ 官方机器人（QQ Bot API v2）** 转成 **OneBot v11 反向 WS**。
NA 现有的 `onebot_v11` 适配器不改代码就能接它：握手头、首帧 lifecycle、心跳、`echo` 原样回传都与 NapCat 一致；
陌生 `self_id` 会被登记成「未认领实例」。

与 `qqbot_openclaw` 的关系：两者用的是**同一套** QQ Bot API v2（`api.sgroup.qq.com`、`/v2/groups|users/{openid}/...`、
网关 intents `1<<25 | 1<<26`，扫码接入也是同一个 `q.qq.com/qqbot/openclaw/connect.html` 绑定流程）。
差别在覆盖面：qbob 还用了群成员事件、入群审批、撤回、禁言、群信息、`event_id` 被动凭据与被动回复名额管理、markdown @ / @全体、多 bot 管理，
并把这些翻译成 OneBot。所以它不是 openclaw 的升级，而是给「已有 OneBot 习惯、想迁到官机」的另一条路。

**为什么是「附带 + 托管」，不是把代码并进来或写原生适配器**

| 方案 | 结论 |
|---|---|
| 把 qbob 代码搬进 NA 做原生适配器 | ❌ qbob 是 AGPL-3.0；进程内 import / 改写构成组合作品，整体须 AGPL 且不得附加限制，而 NA 协议在 Apache 2.0 上加了商用规模与署名条款，两者无法同时满足。除非作者另行授权 |
| 自己按官方 API 重写原生适配器 | 可行，但工作量大（qbob 核心 `bot.py` + `sender.py` 约 160KB）；先用本方案验证需求 |
| **原样附带、独立进程，NA 后台托管** | ✅ 与 NapCat 同一模式：NA 只经 OneBot WS 与 qbob 的管理 REST API 和它说话，不 import、不收录源码、不分发镜像 |

## 2. 目标与非目标

**目标**

1. `docker compose` 一条附加文件即可把 qbob 跑在 NA 旁边，与任何基础 compose（标准 / 带 NapCat / 加固）叠加。
2. 用户只在 **NA 总后台**完成全部日常操作：新建实例 → 扫码或填 AppID/Secret → 自动接线与绑定账号 → 状态、启停、重载、解绑。
3. 不出现「未认领实例」裂行（NapCat 踩过的坑，见 `container_manager.py` 模块文档「登录成功后的账号回写」）。
4. 不扩大攻击面：qbob 管理面只绑宿主回环，NA 不在任何响应里回传 qbob 凭据。

**非目标（本期不做）**

- `onebot_v11` 侧的兼容修补（15 位虚拟号头像、戳一戳降级提示、`fetch_ptt_text` 文案、私聊自身消息归属）。那块正由另一路改动，等其合入后再做，见 §13。
- 在 NA 里重做 qbob 管理台的全部功能（插件、分组、消息记录、聊天室、BotShepherd 预设）。高级功能仍走 qbob 自己的管理台。
- NA 托管 qbob **容器**的生命周期（像内置 NapCat 那样经 docker.sock 创建/删除）。qbob 一个进程带多个 bot，与 `container_manager` 「一实例一容器」模型不合，本期由 compose 管容器。

## 3. 架构

```
QQ 开放平台 ⇄ nekro_qbob 容器 ─────────────── OneBot v11 反向 WS（每 bot 一条，Bearer=该实例令牌）──▶ nekro_agent :8021/onebot/v11/ws
                 ▲  管理 REST API :17810（仅容器网络）                                                      │
                 └──────────────── NA 后端 qbob_manager（服务账号 cookie 会话）◀── NA 总后台前端 ───────────┘
```

- 数据面：完全复用 `onebot_v11` 适配器，不新增适配器。
- 管理面：NA 后端 `services/console/qbob_manager.py` 调 qbob 的 `/api/*`；前端只和 NA 说话。

## 4. 许可边界（硬约束，改动时别破）

1. 仓库里**只有我们自写的文件**：`docker/qbob/Dockerfile`、入口脚本、compose 附加文件、本文档。不收录 qbob 任何源码。
2. 镜像在**用户机器上本地构建**，构建时从上游 git 拉固定提交（与 NapCat「用户的 Docker 去拉上游镜像」同一性质）。我们不发布 qbob 镜像；若将来要发布，须随附对应源码（AGPL §6）。
3. **不修改 qbob**。有问题提给上游。若必须打补丁，补丁须以 AGPL 公开，且因 qbob 有联网界面，须向其用户提供改后源码（AGPL §13）。
4. NA 代码**不得 import** `qqbot_onebot` 的任何模块；只能经 OneBot WS、qbob HTTP API、qbob 管理 REST API 通信。
5. 文档与界面标明「qbob 组件为 AGPL-3.0，不适用 NA 协议」，并给出上游地址与所用提交。

## 5. 部署形态

### 5.1 文件

| 文件 | 作用 |
|---|---|
| `docker/qbob/Dockerfile` | 两阶段。构建阶段：`python:3.12-slim` + `git` + `gcc` / `libc6-dev`，按提交拉上游（`QBOB_REF`，默认 0.1.5 = `9c36d57`，拉完删 `.git`），按上游带哈希的锁文件装依赖到 `/install`——锁文件里的 `pilk` 在 PyPI 上只有 Windows 轮子与源码包，Linux 上必须现场编译。运行阶段：只拷依赖与源码，装 `ffmpeg`，`chromium` 由构建参数 `WITH_CHROMIUM` 控制（长文本转图用，默认关，关时 qbob 照常发文字）；以 uid 10001 的 `qbob` 用户运行 |
| `docker/qbob/qbob_entry.py` | 入口（只用标准库，不 import qbob）：首启按环境变量生成 `config.json`（qbob 的配置文件不展开环境变量），已存在则不覆盖，只按 `QBOB_PUBLIC_BASE_URL`（设了的话）同步那一个键；把数据目录交给 `qbob` 用户、收紧到 0700、umask 077 后降权启动 |
| `docker/qbob/docker-compose.qbob.yml` | **附加文件**（`-f docker-compose.yml -f qbob/docker-compose.qbob.yml`）：新增 `nekro_qbob` 服务，镜像名 `nekro-qbob:<QBOB_REF>`（本机没有才构建；改 `QBOB_REF` 镜像名跟着变，下次 `up` 自动重建），给 `nekro_agent` 补 `NEKRO_QBOB_ADMIN_URL`。放在 `qbob/` 下而不是 `docker-compose-x-*.yml`：它不是完整的 NA 定义，不应落进「每份定义 NA 服务的 compose」那组用例 |
| `docker/install.sh` | `--with-qbob` 选项与交互询问（默认否；`.env` 的 `COMPOSE_FILE` 已带 qbob 时沿用）；从仓库的 `docker/qbob/` 复制三份文件到数据目录；`pull --ignore-buildable` 跳过本地构建的镜像；**`up` 成功后**才写 `COMPOSE_FILE`（失败时不写，免得之后每次 `up` 都去构建一个起不来的镜像；已有其他附加文件时只追加，不整行覆盖）；放开数据目录权限时跳过 `qbob_data/`、`qbob_state/` 与 `qbob/`（构建上下文） |
| `docker/.env.example` | `QBOB_REF`、`QBOB_WITH_CHROMIUM`、`QBOB_PIP_INDEX_URL`、`QBOB_ADMIN_EXPOSE_PORT`、`QBOB_PUBLIC_BASE_URL` |

**升级**：qbob 镜像在本机构建，不在任何仓库里。手工升级要先 `docker compose pull --ignore-buildable`（普通的 `pull` 会尝试拉取 `nekro-qbob` 并以非零退出），再 `docker compose up -d`。改 `QBOB_REF` 后同理：镜像名跟着变，`up` 时才会按新提交构建。

### 5.2 qbob 生成的配置（与上游默认值的差异）

| 键 | 值 | 理由 |
|---|---|---|
| `host` | `0.0.0.0` | 公网面（webhook、转发网页）在容器内监听；**默认不映射到宿主**，需要时由运维加反代 |
| `admin_host` / `admin_port` | `0.0.0.0` / `17810` | NA 经容器网络访问；compose 把它映射到宿主 `127.0.0.1:${QBOB_ADMIN_EXPOSE_PORT:-17810}`，只绑回环（远程用 SSH 隧道） |
| `admin_on_public` | `false` | 管理台不挂到公网面 `/admin` |
| `http_api_host` | `127.0.0.1` | OneBot HTTP API 本期不用，只留本机 |
| `db_path` / `media_dir` / `log_file` | `${QBOB_DATA_DIR}/...` 绝对路径 | 数据目录在两个容器里**同路径**挂载，`get_file` 返回的本地路径 NA 能直接读 |
| `update_check_hours` | `0` | 关掉管理台的 `git pull` 自更新，升级 = 改 `QBOB_REF` 重新构建 |
| `ask_owner` | `false` | 「创建bot」聊天指令的号主确认是 qbob 自助接入用的；NA 托管的 bot 由 NA 后台接入 |

### 5.3 NA 侧环境变量（`core/os_env.py`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEKRO_QBOB_ADMIN_URL` | 空 | qbob 管理面根地址，如 `http://nekro_qbob:17810`。**为空 = 本部署未附带 qbob**，向导里该选项置灰 |
| `NEKRO_QBOB_DATA_DIR` | `{DATA_DIR}/qbob_data` | 读首启 admin 口令文件用 |

## 6. NA ↔ qbob 管理通道

qbob 管理 API 只有 cookie 会话（`POST /api/login` → `qqob_session`，会话存在 qbob 进程内存里、7 天过期、qbob 重启即失效），没有 API 令牌；
角色三级 `admin > advanced > user`；登录失败按 (IP, 用户名) 与 IP 锁定。

**服务账号引导（只在第一次需要时执行，幂等）**

1. NA 已有服务账号（`{DATA_DIR}/qbob_state/service_account.json`：用户名 + `encrypt_credential(口令)`）→ 直接登录。
   不放在 `{DATA_DIR}/qbob/`：那是 install.sh 拷进来的 compose 文件与镜像构建上下文所在目录。
2. 没有 → 读 `{QBOB_DATA_DIR}/initial_admin_password.txt`（格式 `admin / <口令>`）→ 以 `admin` 登录 →
   `POST /api/users` 建 `nekro_agent`（`admin` 角色，32 字节随机口令）；已存在（409）则以 admin `PUT /api/users/nekro_agent` 重置口令 →
   写服务账号文件（0600，口令加密）→ 改用 `nekro_agent` 登录。**`admin` 的初始口令不动**，留给运维开 qbob 管理台。
3. 初始口令文件不存在（运维已改过 admin 口令）且没有服务账号 → 状态报「需要手工提供服务账号」，平台方在向导或实例的协议端页填一个 qbob 账号
   （Super 专用接口，只写不读；登录响应里的角色必须是 `admin`，否则拒绝保存——解绑要删 bot，删 bot 需要 admin）。
   为了少走这一步，install.sh 结尾提示运维：改 qbob 的 admin 口令之前先在 NA 总后台打开一次「新建实例」页，让 NA 用初始口令建好服务账号。

**会话**：内存缓存 cookie；任一请求 401 → 丢弃缓存、重登一次、重试一次；仍 401 → 报错，不循环。并发重登用 `asyncio.Lock` 串行。
**登录失败后 5 分钟内不再自动登录**：qbob 按 (IP, 用户名) 与 IP 锁定失败登录，而实例面板每 10 秒轮询一次，不退避就会把 NA 自己的 IP 锁在外面，
到那时连平台方手工提供的正确账号也会被拒。平台方手工提供账号不受这个退避限制，成功后解除。

**归属标记**：`notes = "managed-by: nekro-agent deploy=<部署 ID> instance=<实例标识>"`。部署 ID 第一次用到时随机生成，存在 `{DATA_DIR}/qbob_state/deployment_id`，
不随 compose 形态（标准版与 x-napcat 版的 `NEKRO_INSTANCE_NAME` 不同）或 `INSTANCE_NAME` 变化。
**这个文件不能删**：它一丢，NA 就认不出自己建的所有 bot——状态页报归属标记不符，重载、开关、重新接线、解绑全部被拒。
文件缺失而库里还有接入记录时，NA 不会静默换一个新 ID，而是报错要求恢复：从备份恢复 `qbob_state/deployment_id`，
或者在确实要重建时清掉这些实例的接入记录让 NA 生成新 ID，再到 qbob 管理台把各 bot 的备注改成新的归属标记。

**错误口径**：qbob 不可达 / 未配置 / 服务账号失效分别给出可读的 `message`，不 500（与 NapCat 二维码接口「要状态不要 500」同一取向）。

## 7. 实例生命周期

### 7.1 新建

沿用现有向导与 `POST /api/console/instances`，`provider = builtin_qbob`、`adapter_key = onebot_v11`。实例令牌照常生成。

### 7.2 接入（扫码或手填）

```
扫码：NA → qbob POST /api/qrconnect/start → 前端显示二维码 → 号主手机 QQ 扫码确认
      NA 轮询 GET /api/qrconnect/poll/{task_id} → ready（appid, secret）/ exists / expired / pending
手填：AppID + AppSecret
```

拿到凭据后，**先建后接线**，杜绝竞态。整个过程持有本实例的锁，并且**锁内一律重读库里的当前行**
（路由在进锁前读出的实例可能已经旧了：两次接入并发时，第二次拿着旧快照会以为还没接入，把第一个 bot 变成带本实例令牌的孤儿）：

1. `POST /api/bots`：`onebot_endpoints = []`、`group_list_mode = "black"`（群默认可收，开关交给 NA 的频道管理）、
   `report_self_message = true`、`name = 实例显示名`、`notes = 归属标记`（§6）。
   bot 已在 qbob 里且带本实例标记（重复提交、扫码回 `exists`）：提交了新 Secret 就 `PUT {secret, enabled: true}`；
   bot 被停用了就 `PUT {enabled: true}`——凭据无效时 qbob 会自动停用 bot，而 qbob 的重载不会拉起停用的 bot。
2. `GET /api/bots?q=<appid>` 读 `self_id`（bot 启动时由 idmap 按 AppID 生成，15 位）。
3. **核对凭据**：`GET /api/status?appids=<appid>`。qbob 的 `self_id` 与凭据对错无关，建 bot 时同步取 token 失败也只记日志，
   所以「读得到 self_id」证明不了接入成功；能用的信号是第一次取 token 失败时写下的 `last_error`（`凭据错误 ×N`）、
   以及被停用后从运行列表里消失。有问题 → 本次新建的 bot 回滚删除（原来就有的不删），把 `last_error` 原样带给用户。
4. 把 `self_id` 写进实例 `provider_account_id`（走既有的 `update_instance`：查重、路由缓存失效、状态对齐）。
5. 实例元数据记 `qbob_appid`。
6. `PUT /api/bots/{appid}`：`onebot_endpoints = [{url: ws://{NA主机名}:{NA监听端口}/onebot/v11/ws, access_token: 实例令牌}]`。
   主机名与令牌复用 `resolve_self_hostname()` / `resolve_access_token()`，与内置 NapCat 的托管条目同一来源；
   端口是 NA **进程实际监听**的端口（与 CC 中继同一判据 `relay_listen_port()`，容器内恒为 8021），
   不是 `NEKRO_EXPOSE_PORT`——那是发布到宿主的端口，改过它的部署（同机多部署）在容器网络里连它连不上。

为什么不一步建好：若建 bot 时就带端点，bot 启动即连 NA，而此时账号还没绑到实例，握手只认部署级令牌 → 403，
或者落成「未认领实例」。先拿 `self_id` 绑好账号再接线，第一次握手就命中本实例。

扫码回 `exists`（这个 AppID 已在 qbob 里）：若它的 `notes` 标着本实例 → 视为重复提交，从第 2 步起；否则拒绝（可能是别的实例或运维手工建的 bot），提示去 qbob 管理台处理。
扫码出结果之后接入失败（凭据被拒、冲突）：qbob 在 `ready` / `exists` 时就清掉了它那边的任务，所以失败原因记在 NA 本地的任务上，
之后再轮询同一任务得到同样的结果，而不是「任务已过期」。

建 bot、改 bot、重载在 qbob 里同步取 token 与平台身份，到 QQ 的网络只丢包时最坏要一分多钟，这几个请求的超时放宽到 90 秒；
超时时提示「操作可能已经生效，请刷新状态再决定是否重试」（重试靠归属标记能接上，不会重复建）。

### 7.3 日常运维

| 动作 | qbob 接口 |
|---|---|
| 状态 | `GET /api/bots?q=<appid>`（`running`、`enabled`、`self_id`）+ `GET /api/status?appids=<appid>`（连通性、最近错误） |
| 启用 / 停用 | `PUT /api/bots/{appid}` `enabled` |
| 重载 | `POST /api/bots/{appid}/reload` |
| markdown / 自身消息上报 | `PUT /api/bots/{appid}` |
| 重新接线（端点被人改过） | `PUT /api/bots/{appid}` 写回托管端点；状态接口发现端点与期望不一致时提示 |
| 解绑（从 qbob 删除 bot） | `DELETE /api/bots/{appid}`，清 `qbob_appid` 与账号绑定；实例本身不删，可重新接入。qbob 里的 bot 不带本实例标记时**拒绝解绑**：它多半还配着本实例令牌的端点，只清 NA 侧会让实例回到「等待绑账号」，那枚令牌被握手放行，bot 重连时就成了未认领实例 |

删除实例不自动删 qbob 里的 bot（`purge_instance` 不改，避免与并行改动撞车）；实例列表的删除确认框对已接入 bot 的 qbob 实例给出提示，引导先「解绑」。

给租户管理员的状态与错误说明不带 qbob 的内部地址，也不提只有平台方才有的设置入口；平台方看到原因。

## 8. 权限分档（对照 `routers/console/containers.py`）

| 档 | 接口 | 要求 |
|---|---|---|
| 部署级 | qbob 服务状态、手工设置服务账号 | `Role.Super` |
| 接入拓扑 | 接入（扫码开始/轮询、手填）、重新接线、解绑 | `Role.Super` |
| 自助运维 | 读状态、启用/停用、重载、markdown / 自身消息开关 | `Role.Admin` + 实例归属校验（复用 `_resolve_accessible_instance` 同一判据） |

接入归 Super：它决定「哪个 QQ 机器人接到哪个实例」，并写入带实例令牌的出站端点，属于接入拓扑，与 NapCat 的 `network-config` 同档。

## 9. NA 新增接口（挂在 `/api/console` 下）

| 方法 | 路径 | 档 |
|---|---|---|
| GET | `/qbob/service` | Super |
| PUT | `/qbob/service/credential` | Super（只写） |
| GET | `/instances/{key}/qbob` | Admin+归属 |
| POST | `/instances/{key}/qbob/qrconnect` | Super |
| GET | `/instances/{key}/qbob/qrconnect/{task_id}` | Super |
| POST | `/instances/{key}/qbob/attach` | Super（手填 AppID/Secret） |
| POST | `/instances/{key}/qbob/rewire` | Super |
| DELETE | `/instances/{key}/qbob` | Super（解绑） |
| PATCH | `/instances/{key}/qbob` | Admin+归属（`enabled` / `markdown_enabled` / `report_self_message`） |
| POST | `/instances/{key}/qbob/reload` | Admin+归属 |

扫码轮询拿到的 `secret` **只在 NA 后端内存里流转**，直接用于第 7.2 步，不回传前端。

## 10. 前端

- `ProviderStep`：新增「内置 qbob（QQ 官方机器人）」；`NEKRO_QBOB_ADMIN_URL` 未配置时置灰并说明。
- 向导新增 `BuiltinQbobStep`：扫码 / 手填两个页签（切换页签即放弃进行中的扫码），成功后显示绑定的 15 位 ID；
  qbob 连得上却没有服务账号时，给平台方一张「提供 qbob 服务账号」表单。
- 实例详情页新增 `QbobPanel`：状态、启停、重载、两项开关、重新接线、解绑；同样在需要时给平台方服务账号表单；
  「高级管理台」只给说明（端口映射或 SSH 隧道），不做反代。
- 展示 qbob 的界面都标明「qbob 是 AGPL-3.0 的独立项目」并给出上游地址（§4 第 5 条）。
- 实例列表的删除确认框：已接入 bot 的 qbob 实例提示先解绑。
- 文案进 `locales/{zh-CN,en-US}/console.json` 与 `console-setup.json`。

## 11. 数据

| 位置 | 内容 |
|---|---|
| 实例 `metadata_json.qbob_appid` | 本实例接入的 AppID |
| `{DATA_DIR}/qbob_state/service_account.json` | `{"username": ..., "password": <encrypt_credential 密文>}`，0600，目录 0700 |
| `{DATA_DIR}/qbob_state/deployment_id` | 归属标记里的部署 ID，0600 |
| qbob 数据目录 `{DATA_DIR}/qbob_data/` | qbob 自己的 SQLite（含明文 AppSecret）、媒体、日志，0700；NA 只读 `initial_admin_password.txt` 与媒体文件 |

`{DATA_DIR}/qbob_state/` 里的两样东西性质不同：`service_account.json`（NA 的 qbob 服务账号）可以删掉重来，
NA 会按 `qbob_data/initial_admin_password.txt` 重新引导（该文件也不在时，由平台方手工提供一个 admin 账号）；
`deployment_id` 则**不能删**。它丢失后现有 bot 的归属标记全部对不上，NA 会拒绝改动这些 bot，
恢复办法只能是找回备份、或按新的部署 ID 重写 qbob 里这些 bot 的备注。

不新增数据库表、不需要迁移。

## 12. 安全边界清单

- qbob 管理面在容器网络里；宿主上只绑回环（与内置 NapCat WebUI 同口径）。
- qbob 以非 root 用户运行；NA 读它写的 0600 口令文件依赖 NA 容器以 root 运行（现状如此）。NA 将来降权的话，引导会退到 §6 第 3 步「手工提供服务账号」，不会静默失败。
- qbob 的库里是明文 AppSecret：入口把数据目录收紧到 0700、以 umask 077 运行；install.sh 放开数据目录权限时跳过 `qbob_data/` 与 `qbob_state/`，已存在的收回组与其他人的权限。
- AppSecret 不进 NA 数据库、不回传前端、不进日志（日志只记 AppID）。
- 服务账号口令以 `encrypt_credential` 加密落盘；响应里永不回传。
- NA 写入 qbob 的出站端点只由后端生成（主机名 + 实例令牌），不接受调用方传入 URL。
- `notes` 归属标记防止 NA 误接管 / 误删运维手工建的 bot。
- 自更新关闭，qbob 版本由 `QBOB_REF` 固定。

## 13. 已知限制

- **平台限制**（openclaw 同样受限）：群被动回复 5 分钟内 5 条、私聊 60 分钟内 4 条；超窗且未开主动消息 → qbob 返回 1503。NA 的定时任务、跨频道发送、WebUI 主动发送会受影响。
- 群里需由群主打开「主动发言」与「接收全部消息」；qbob 默认 `require_recv_all = true`，未开全量接收的群会被自动停用。
- 用户 / 群 ID 是每 bot 不同的 15 位虚拟号；NA 里的超级用户等配置要填虚拟号（向 bot 发「获取信息」取得）。
- 待另一路 `onebot_v11` 改动合入后再做：15 位 ID 头像走 `get_additional_user_detail`、戳一戳降级、`fetch_ptt_text` 不支持时的文案、私聊自身消息（qbob 不带 `target_id`，目前按「无可归属频道」跳过）。
- 无真实 QQ 机器人凭据，端到端只能由用户验收。

## 14. 测试计划（定向，不跑全量）

- `tests/test_qbob_console.py`：用 `httpx.MockTransport` 模拟 qbob 管理 API。假实现按 qbob 源码的行为写：`self_id` 与凭据无关、
  凭据错只体现在 `last_error` 与之后的自动停用、`/api/status` 只列在运行的 bot、停用的 bot 在 `/api/bots` 里 `self_id = 0`。
  - 服务账号引导：初始口令 → 建账号 → 加密落盘（在 `qbob_state/`）；409 → 重置口令；初始口令文件缺失 → 明确报错；手工账号必须是 admin。
  - 会话：401 → 重登一次 → 重试一次；连续 401 不循环；登录失败后退避，手工提供账号解除。
  - 接入顺序：建 bot 时端点为空 → 读 `self_id` → 核对凭据 → 绑账号 → 写端点（URL 与令牌来自 NA，端口是监听端口）；凭据错时回滚删 bot 并带出原因；
    被自动停用的本实例 bot 重新提交凭据时一并启用。
  - 并发与旧快照：同一实例并发接入两个 bot 只成一个；旧快照不能接入第二个 bot、也能正常解绑。
  - `exists` 分支：本实例标记放行，他人标记拒绝；扫码后接入失败，原因在之后的轮询里仍可见。
  - 归属标记不随 `INSTANCE_NAME` 变；标记不是本实例的 bot 拒绝解绑。
  - 响应与日志不含 secret / 服务账号口令；租户看不到 qbob 的内部地址。
  - 权限：Super / Admin+归属 两档；租户访问他人实例 404（读、改、重载）；只读成员 403 且不碰 qbob。
- 既有的路由权限契约用例若枚举全部 console 路由，同步登记新路由。
- 前端：`tsc`、`eslint --max-warnings 0`。
- compose：`docker compose -f docker-compose.yml -f qbob/docker-compose.qbob.yml config` 能解析（本机无 Docker 时以 YAML 结构用例代替：附加文件只给 `nekro_agent` 加环境变量、不改镜像与容器名；`nekro_qbob` 端口只绑回环、数据目录同路径挂载、接在 `nekro_network`），以及既有 compose 用例。
- 入口脚本：`build_config` 的取值、配置已存在时不覆盖、`QBOB_PUBLIC_BASE_URL` 后填也生效、收紧权限的顺序（用例里按路径加载，不需要 Docker）。
- Dockerfile：构建阶段带编译器、运行阶段不带；三处默认提交一致。
- `install.sh`：`bash -n`；`obtain_qbob_files` / `env_has_qbob` / `open_data_dir_permissions` 用真 bash 跑；`COMPOSE_FILE` 只在 `up` 成功后写。

## 15. 风险与待定

- qbob 0.1.x，管理 API 可能变动：版本固定在 `QBOB_REF`，升级前对照本文 §7 的接口表复核。
- qbob 会话在其进程内存里，qbob 重启后第一次请求必然 401 → 重登（已覆盖）。
- 上游若发布官方镜像，可把 Dockerfile 换成直接引用上游镜像，与 NapCat 完全同构。
