# Web Chat MCP

Web Chat MCP 内置在 Nekro Agent 进程里，用于让 MCP Client 操控 Nekro Agent 的网页聊天功能：创建测试会话、发送消息、等待 Agent 回复、读取历史和清理测试会话。

## 启动方式

不需要单独启动 MCP Server。Nekro Agent 启动后会挂载 Streamable HTTP MCP：

```text
http://<nekro-agent-host>:8021/api/mcp/web-chat/mcp
```

接口由 Nekro Agent 统一鉴权，只给平台方用。**本分支没有「一枚全局内置令牌」，这一版也不给工作区的 CC 沙盒发令牌**（见下一节）。

## 鉴权

入口接受两种凭据：

| 凭据 | 谁能拿到 | 通过后的身份 |
|---|---|---|
| 外接固定密钥 | 平台方管理员在网页适配器的「MCP 密钥」页生成 | 平台方 `admin` |
| 后台登录态 JWT | 平台方账号 | 该账号 |

- 外接密钥默认关闭、只存哈希，明文只在生成时返回一次；支持 `Authorization: Bearer <token>`，
  也兼容 `?token=`（会进反向代理访问日志，不推荐）。
- 后台登录态只认平台方：租户身份 403。
- 按工作区派生的令牌（`nwc1.` 开头）这一版一律 401，工作区 `.mcp.json` 也不写网页聊天条目。
  原因：工作区归属随频道绑定、解绑来回变，写进 `.mcp.json` 的令牌会留在租户读得到的地方，
  租户再把自己的频道解绑，工作区重新算成平台方，令牌就能以平台方身份调用。等「出现过非平台方归属的
  工作区打不会自动消失的标记」做完再开。
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
- 全局 MCP 库里若有网页聊天条目（平台方手工加的、或从上游迁来的），它的 `headers` 会被清空，也不会写进任何工作区。
- 外接固定 token 默认关闭；开启后具备同等 Web Chat MCP 管理能力，应按管理员凭据保管。
- 文件上传默认禁用本地任意路径读取，必须配置 allowlist 后才可使用。
- 删除会话会清理历史、插件数据、定时任务、上传目录和沙盒目录，因此默认需要显式确认。
