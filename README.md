# NekroAgent 二次开发版

[English](./README_en.md)

> **这是一个非官方的二次开发版本。** 它基于 [KroMiose/nekro-agent](https://github.com/KroMiose/nekro-agent)（NekroAgent）修改而来，
> 与 NekroAgent 项目维护方没有任何隶属或背书关系。**基于 NekroAgent 项目构建**——上游项目地址：<https://github.com/KroMiose/nekro-agent>。
>
> - 当前版本：**0.2.0**（本版本自己的版本号；所基于的上游版本是 `2.3.3`）
> - 许可证：沿用上游的 [Nekro Agent 开源协议 V1.1](./LICENSE)（基于 Apache License 2.0 修改），详见下方「许可证」与 [`NOTICE`](./NOTICE)
> - 变更记录：[`CHANGELOG.md`](./CHANGELOG.md)

## 这是什么

NekroAgent 是一个 AI 聊天与代理执行 Bot：通过提示词引导大模型生成代码，在容器沙盒里执行，并经 RPC 与真实环境交互；
支持 OneBot v11 (QQ)、Discord、Telegram、Minecraft、B 站直播、微信、邮件、SSE+SDK 等多种平台，带插件系统和 WebUI。
上游项目的完整介绍、文档与社区见 <https://github.com/KroMiose/nekro-agent>。

本二次开发版在上游基础上增加了面向「一个进程运行多个 Bot、并交给多个客户使用」的能力。

## 与上游的主要差异

- **多实例**：一个进程管理多个逻辑 Bot 实例（一个实例 = 一个 Bot 账号），共享重资源；聊天标识、配置、沙盒和记忆都带实例维度。
- **总后台（Console）与多租户**：平台方 / 租户管理员 / 只读成员三级权限；实例、容器、公告、掉线告警、配额与并发限制统一管理。
- **隔离与身份**：好感度与记忆按「记忆域 + 人 + 人设」隔离，可配置为跨实例共享；身份别名、合并、入域并入带审批流。
- **用量计量**：每次上游 LLM 请求记一行流水，按实例 / 租户 / 模型组聚合，在 `/console/usage` 查看。
- **安全加固**：模型组 API Key 静态加密；JWT 密钥与管理员口令的启动审计、改密后旧令牌失效；日志与上游报错脱敏；沙盒与 CC 工作区网络隔离；可选的 `docker.sock` 硬化（[`docs/docker-guard.md`](./docs/docker-guard.md)）。
- **其他**：好感度软回复门、记忆与知识库中文分词、NapCat 容器归属与孤儿回收、带真实 PostgreSQL 的回归测试与 CI。

## 快速部署（Docker）

要求：Linux 主机、Docker（含 Compose v2）、`bash`、`curl`。

```bash
git clone https://github.com/Akiyo-dayo/Nekro-Agent-ByAkiyo.git nekro-agent
cd nekro-agent/docker
sudo -E bash install.sh --with-napcat
```

**请在本仓库的 `docker/` 目录里运行脚本。** 脚本发现身边已有本仓库的 compose 文件时会优先使用它；
否则它会从**本仓库**的 `main` 分支下载 compose 文件与 `.env.example`（地址是 `https://raw.githubusercontent.com/Akiyo-dayo/Nekro-Agent-ByAkiyo/main/docker`）。
也可以直接用一键命令（同样取自本仓库）：

```bash
sudo -E bash -c "$(curl -fsSL https://raw.githubusercontent.com/Akiyo-dayo/Nekro-Agent-ByAkiyo/main/docker/install.sh)" - --with-napcat
```

**不要使用上游 README 里的一键部署 `curl` 命令**：它拉的是上游的 compose 与镜像，不是本版本的。

安装脚本会生成随机的访问令牌、后台 admin 口令和 JWT 签名密钥并写入数据目录里的 `.env`（默认 `~/srv/nekro_agent`）。
**上线前请读 [`docs/operations.md`](./docs/operations.md)**：它说明各项凭据的要求、模型组 Key 的加密密钥与备份注意事项、日志与诊断开关。

### 镜像

镜像发布在 GitHub Container Registry（GHCR），由仓库自己的 CI 用内置 `GITHUB_TOKEN` 构建并推送：

| 镜像 | 用途 |
|---|---|
| `ghcr.io/akiyo-dayo/nekro-agent-byakiyo` | 主应用 |
| `ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox` | 沙盒（安装脚本会在宿主上拉取它） |

标签：

- **版本号**（如 `0.2.0`）与 `latest`：打 `v*.*.*` 标签时构建，适用于生产；
- `preview` / `preview-<提交短 SHA>`：`main` 分支更新时构建，包含最新改动，适用于测试。

```bash
docker pull ghcr.io/akiyo-dayo/nekro-agent-byakiyo:latest
docker pull ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox:latest
```

沙盒容器由应用通过 Docker API 创建，**缺镜像时自动拉取不带凭据**，所以沙盒镜像要先在宿主上手动拉取，之后更新镜像也要手动拉取。

### 其他部署方式

- 同一台主机跑多套部署：[`docs/multi-deploy-same-host.md`](./docs/multi-deploy-same-host.md)
- 需要更严的 `docker.sock` 边界：[`docs/docker-guard.md`](./docs/docker-guard.md)
- 裸机 / 开发环境、插件与适配器开发：见 [`CONTRIBUTING.md`](./CONTRIBUTING.md)、[`docs/`](./docs/) 和上游的[文档中心](https://doc.nekro.ai)（上游文档里的镜像名与脚本地址以本页为准）

## 遥测与云服务（默认开启）

沿用上游的行为，**本版本没有改动**：配置项 `ENABLE_NEKRO_CLOUD`（界面里叫“启用 NekroAI 云服务”）**默认是 `true`**。开启时：

- 应用会**每小时**向 NekroAI 云服务 `https://cloud.nekro.ai` 上报一次使用统计。该服务由上游项目运营，不是本二次开发版运营的；后台的“检查更新”也会请求同一个域名；
- 上报的内容（见 `nekro_agent/systems/cloud/` 与 `nekro_agent/tools/telemetry_util.py`）：实例 ID（由主机名、平台、机器 ID、CPU 数、内存总量、环境变量名清单等做 SHA-256 得到）、上游版本号、是否在 Docker 里运行、系统信息（含**主机名**、平台、Python 版本、CPU 架构）、以及数据库**全库合计**（多实例 / 多租户部署下是所有实例的总和）与上一小时的统计数字：用户数、频道数、消息数（含群聊 / 私聊）、沙盒执行次数与停止类型分布、执行耗时的平均与最大值、活跃用户 / 频道数。**不包含**聊天内容、代码内容和凭据；
- 填了 NekroAI 云服务 API Key 后，还可以使用云端的插件、人设共享等能力。

**如何关闭：**

1. 把 `ENABLE_NEKRO_CLOUD` 设为 `false`：Web 界面“设置 → 基础设置 → 启用 NekroAI 云服务”，或首次部署向导里的同名开关，或 `<数据目录>/configs/nekro-agent.yaml`。
2. ⚠️ **已知限制**：当前实现里，**没有填 NekroAI 云服务 API Key 时，遥测上报并不检查上面这个开关**（`nekro_agent/systems/cloud/api/telemetry.py`）。要确定地停止上报，请另外把环境变量 `NEKRO_NEKRO_CLOUD_API_BASE_URL` 设为一个不可达的地址（例如 `http://127.0.0.1:9`；注意名字里有两个 `NEKRO_`：应用会给环境变量名再加一次 `NEKRO_` 前缀），或者在出口防火墙上屏蔽 `cloud.nekro.ai`。这样做之后依赖云服务的功能（云端插件、人设共享等）也不可用。

## 许可证

本项目沿用上游的 **Nekro Agent 开源协议 V1.1**（基于 Apache License 2.0 修改，冲突时以该协议为准）。**使用、修改和再分发前请完整阅读 [`LICENSE`](./LICENSE)。** 与再分发直接相关的义务（以 `LICENSE` 原文为准，此处只是提示）：

- 再分发（含商业分发）时必须附带 `LICENSE` 的**完整副本**，并保留版权声明、商标信息和归属标识，不得移除或修改；
- 必须明确标明**基于 NekroAgent 项目构建**，并提供上游项目的原始链接；在用户界面中保留「基于 NekroAgent 构建」标识及指向上游项目的超链接，不得淡化原项目的贡献；
- 面向公众提供商业化内容创作或实时互动服务（直播、虚拟主播、公开聊天机器人等）时，须在显著位置标明「由 NekroAgent 驱动 / Powered by NekroAgent」并附上游开源地址；
- **大规模商业使用**（你及关联实体运营的所有实例，频道总数累计超过 1000，或注册用户总数累计超过 50000）须事先获得上游维护方的书面授权，且不得用多实例、多实体等方式拆分规避；授权联系方式见 `LICENSE`；
- 提供技术支持、搭建部署等服务时，须向服务对象说明所支持的项目是 NekroAgent 及其官方开源地址，不得暗示该项目为服务提供者自主开发，也不得暗示与项目维护方存在关联；
- 本项目基于大模型生成并执行代码，使用者应自行评估并告知最终用户相关风险（见 `LICENSE` 第一节第 3 条）；
- 本项目**对原有文件的修改**在 [`NOTICE`](./NOTICE) 的 "Statement of changes" 与 [`CHANGELOG.md`](./CHANGELOG.md) 中说明。本仓库没有逐提交的历史（它由维护者的开发仓库整体导出），所以要精确识别哪些文件被改过，请把本仓库的树与上游 `2.3.3` 发布版做比对。

这份提示不是法律意见，也不替代 `LICENSE`。

## 致谢

感谢 NekroAgent 的作者 KroMiose 与全体上游贡献者。上游项目的贡献者名单见 <https://github.com/KroMiose/nekro-agent/graphs/contributors>。
