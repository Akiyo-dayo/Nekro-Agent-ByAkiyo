# Web Chat MCP

Web Chat MCP 内置在 Nekro Agent 进程里，用于让 MCP Client 操控 Nekro Agent 的网页聊天功能：创建测试会话、发送消息、等待 Agent 回复、读取历史和清理测试会话。

## 启动方式

不需要单独启动 MCP Server。Nekro Agent 启动后会挂载 Streamable HTTP MCP：

```text
http://<nekro-agent-host>:8021/api/mcp/web-chat/mcp
```

接口由 Nekro Agent 统一鉴权，只给平台方用。这里没有「一枚全局内置令牌」：工作区令牌按工作区现场派生，只发给合格的工作区（见下一节）。

## 鉴权

入口接受三种凭据：

| 凭据 | 谁能拿到 | 通过后的身份 |
|---|---|---|
| 外接固定密钥 | 平台方管理员在网页适配器的「MCP 密钥」页生成 | 平台方 `admin` |
| 后台登录态 JWT | 平台方账号 | 该账号 |
| 工作区令牌（`nwc1.` 开头） | 合格工作区的 `.mcp.json` | 平台方 `admin` |

- 外接密钥默认关闭、只存哈希，明文只在生成时返回一次；支持 `Authorization: Bearer <token>`，
  也兼容 `?token=`（会进反向代理访问日志，不推荐）。
- 后台登录态只认平台方：租户身份 403。
- 工作区令牌只发给**合格**的工作区：此刻归平台方，且从来没有出现过非平台方归属（租户独占或
  与平台方共用）。工作区只要出现过非平台方归属，就永远不发也不认它的令牌，标记不会自动消失；
  平台方还要用它就另建一个工作区。归属是每次调用现算的，但判定同时看这份历史标记，
  所以租户拿到令牌后再解绑自己的频道也救不回来。
- **不要把外接密钥写进任何工作区自己的配置**：工作区以后可能绑上租户的频道，配置文件随之落到租户手里。


## 环境变量

- `NEKRO_WAIT_TIMEOUT`：等待 Agent 回复的默认超时秒数，默认 `60`。
- `NEKRO_POLL_INTERVAL`：等待回复时的历史轮询间隔秒数，默认 `1.5`。
- `NEKRO_WEB_CHAT_MCP_FILE_ALLOWLIST`：允许上传文件的本地目录列表，使用系统路径分隔符分隔；未设置时 `send_web_chat_file` 会拒绝读取本地文件。

## Tools

- `check_web_chat_status`
- `list_web_chat_sessions`
- `create_web_chat_session`
- `rename_web_chat_session`
- `delete_web_chat_session`
- `send_web_chat_message`
- `send_web_chat_file`
- `get_web_chat_messages`
- `get_web_chat_channel_detail`
- `wait_for_web_chat_reply`
- `send_and_wait_web_chat_reply`

推荐测试顺序：

1. `check_web_chat_status`
2. `create_web_chat_session`
3. `send_and_wait_web_chat_reply`
4. `get_web_chat_messages`
5. 测试结束后按需调用 `delete_web_chat_session`，并显式传入 `confirm=true`

## 安全边界

- MCP 与 NA 同进程启动，不暴露独立 stdio/HTTP 进程。
- MCP HTTP 入口先通过 Nekro Agent 的鉴权，再进入工具调用；外接密钥不是通用管理员 API token。
- 全局 MCP 库里若有网页聊天条目（平台方手工加的、或从上游迁来的），它的 `headers` 会被清空；
  写进工作区 `.mcp.json` 的令牌按该工作区现场派生，只写给合格的工作区。
- 外接固定 token 默认关闭；开启后具备同等 Web Chat MCP 管理能力，应按管理员凭据保管。
- 文件上传默认禁用本地任意路径读取，必须配置 allowlist 后才可使用。
- 删除会话会清理历史、插件数据、定时任务、上传目录和沙盒目录，因此默认需要显式确认。
