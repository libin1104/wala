# WALA

> **WechAt-cLAude** - 基于官方 `openclaw-weixin` API 的微信 Claude 代理

## 简介

当前版本不再驱动 `filehelper.weixin.qq.com` 浏览器页面，而是直接对接官方 `openclaw-weixin` 通道：

- 首次登录时向 `ilink` 后端请求二维码
- 优先把二维码以 SVG 附件发到 `SMTP_TO`，发送失败时回退到终端 ASCII 二维码
- 扫码确认后保存 `bot_token`
- 通过 `getupdates` 长轮询接收新消息
- 调用本地 Claude CLI 生成回复
- 通过 `sendmessage` 和 `getuploadurl + CDN` 发回文本、图片、视频和文件
- 全程向 `stdout` 输出 NDJSON 事件，方便外部监督或接管

这条链路和 OpenClaw 官方 `@tencent-weixin/openclaw-weixin` 插件一致，属于“扫码登录微信 + HTTP JSON API + 长轮询”方案，不是公众号 webhook，也不是浏览器自动化。

## 当前能力

- 支持官方 `openclaw-weixin` 登录和收发消息
- 支持多微信用户直聊，默认按 `profile + 对端用户` 隔离 Claude 上下文
- 支持临时会话和 `UID, message` 持久化会话
- 支持 `截屏` 指令：调用 macOS `screencapture` 后上传回当前微信会话
- 支持接收图片、普通文件、视频、语音附件
- 入站附件会先下载并完成官方 CDN 解密，再保存到本地
- 支持发送图片、普通文件、视频；Claude 可通过 `FILE: /绝对路径` 或 `FILE: https://...` 触发发送
- 收到“只有附件、没有文字”的消息时，会按当前微信用户缓存附件，并提示再发一条文字
- Claude 长任务执行期间，每隔 2 分钟自动回一条进度消息
- 支持定时邮件任务：创建、查看、修改、删除、启停
- `stdout` 输出结构化事件：
  - `status`
  - `auth`
  - `message_in`
  - `claude_request`
  - `claude_response`
  - `message_out`
  - `error`

## 环境要求

- macOS
- Python 3.8+
- Claude CLI（已安装并可直接执行 `claude`）
- 可用的 SMTP 配置
  用于优先邮件发送登录二维码和定时邮件

## 安装

```bash
pip install -r requirements.txt
```

## 快速开始

1. 启动主程序

```bash
python main.py
```

2. 程序会尝试获取微信登录二维码

3. 如果已配置 `SMTP_*`，二维码会以 SVG 附件发送到 `SMTP_TO`

4. 如果 SMTP 不可用，终端会打印 ASCII 二维码

5. 用手机微信扫码并确认登录

6. 登录成功后，凭证会保存到本地；下次启动默认复用 `default`

7. 程序会在后台持续长轮询微信消息；收到私聊消息后会自动：
   - 识别文本和附件
   - 下载并解密新收到的附件
   - 调用 Claude CLI
   - 把文本和附件回复发回当前微信用户

## 命令行参数

```bash
python main.py --help
```

可用参数：

- `--poll-interval-s`：错误退避时的额外等待间隔
- `--login-timeout-s`：首次登录等待超时
- `--profile-name`：登录态名称，默认 `default`

## 消息模式

### 临时模式

直接发送普通文本：

```text
你好，请帮我写一个正则
```

默认会话按“当前 `profile` + 当前微信用户”隔离，不会像旧版文件传输助手那样所有人共享一个 `temp`。

### UID 持久化模式

用 4 位短码绑定当前微信用户下的 Claude 会话：

```text
ABCD, 请帮我写一个排序算法
```

后续继续使用相同短码：

```text
ABCD, 这个算法的时间复杂度是多少？
```

支持分隔符：`,`、`.`、`，`、`。`、换行。

### 特殊指令

- `截屏`

## 附件收发

### 入站附件

- 支持图片、普通文件、视频、语音。
- 附件默认保存到 `~/.wclaude_sessions/runtime/inbox/`。
- 所有入站媒体都通过官方 CDN 下载，并按协议完成 AES-128-ECB 解密。
- 语音当前保存为原始文件（通常为 `.silk`），不做转写。
- 如果一条消息只有附件没有文字，程序会按“当前微信用户”缓存附件，并回复：`文件已接收，接下来想对这个文件做什么呢？`

### 出站附件

- Claude 回复中单独一行写 `FILE: /绝对路径/xxx.pdf`，程序会上传该本地文件。
- Claude 回复中单独一行写 `FILE: https://example.com/a.png`，程序会先下载再上传。
- 图片、视频和普通文件都统一使用 `FILE:` 协议，程序会自动识别类型。
- 文本会先发送，附件随后依次发送；`FILE:` 行本身不会再作为普通文本回发。

## 官方 API 说明

当前实现对齐的官方链路是：

- 登录二维码：`GET /ilink/bot/get_bot_qrcode`
- 登录确认：`GET /ilink/bot/get_qrcode_status`
- 入站消息：`POST /ilink/bot/getupdates`
- 出站文本/媒体消息：`POST /ilink/bot/sendmessage`
- 上传前签名：`POST /ilink/bot/getuploadurl`
- CDN：`https://novac2c.cdn.weixin.qq.com/c2c`

可选环境变量：

```bash
WECHAT_OPENCLAW_BASE_URL="https://ilinkai.weixin.qq.com"
WECHAT_OPENCLAW_CDN_BASE_URL="https://novac2c.cdn.weixin.qq.com/c2c"
WECHAT_OPENCLAW_ROUTE_TAG=""
WECHAT_OPENCLAW_BOT_TYPE="3"
```

## 定时任务

定时任务逻辑和旧版本保持一致：

- 任务管理：通过项目内 skill 创建、查看、修改、删除、启停
- 任务执行：主循环登录成功后自动检查并发送邮件
- 结果只发邮件，不回写微信

需要的 SMTP 环境变量：

```bash
SMTP_HOST="smtp.example.com"
SMTP_PORT="587"
SMTP_USER="you@example.com"
SMTP_PASS="your-app-password"
SMTP_FROM="you@example.com"
SMTP_TO="you@example.com"
```

可选：

- `SMTP_USE_TLS=true`
- 项目会自动读取根目录 `./.env`

## NDJSON 输出示例

```json
{"type":"status","ts":"2026-03-23T15:00:00+0800","session":"...","chat":"openclaw-weixin","ok":true,"payload":{"stage":"startup"}}
{"type":"auth","ts":"2026-03-23T15:00:03+0800","session":"...","chat":"openclaw-weixin","ok":false,"payload":{"state":"login_required"}}
{"type":"auth","ts":"2026-03-23T15:00:08+0800","session":"...","chat":"openclaw-weixin","ok":false,"payload":{"state":"login_email_sent","attempt":1}}
{"type":"message_in","ts":"2026-03-23T15:01:10+0800","session":"...","chat":"openclaw-weixin","ok":true,"payload":{"message_id":"123","text":"你好","from_user_id":"user@im.wechat"}}
{"type":"message_out","ts":"2026-03-23T15:01:12+0800","session":"...","chat":"openclaw-weixin","ok":true,"payload":{"kind":"text","text":"你好，有什么我可以帮你？","to_user_id":"user@im.wechat"}}
```

## 失败处理

Claude 调用仍沿用固定错误文案：

- 会话恢复失败：`ID检索失败，请更换ID`
- `session_id.txt` 非法：`ID无效，请更换ID`
- Claude CLI 不可用：`Claude CLI不可用，请检查安装与PATH`
- Claude 响应超时：`Claude响应超时，请稍后重试`
- Claude 限流：`Claude请求过于频繁，请稍后重试`
- Claude 鉴权异常：`Claude鉴权失败，请重新登录`
- 其他异常：`Claude调用失败，请稍后重试` 或 `执行失败：...`

## 项目结构

```text
wala/
├── main.py                     # 代理入口
├── wechat_openclaw_agent.py    # 官方 openclaw-weixin HTTP API 连接层
├── wechat_media_bridge.py      # 附件缓存、FILE 协议解析与资源准备
├── claude_io_utlities.py       # Claude 会话与 memory 管理
├── schedual_utilities.py       # 定时任务、SMTP 与邮件渲染共享逻辑
├── tests/
├── requirements.txt
└── README.md
```

## 本地状态路径

- 登录凭证：`~/.wclaude_sessions/openclaw_weixin/accounts/default.json`
- 同步游标：`~/.wclaude_sessions/openclaw_weixin/sync_state/default.json`
- 按用户隔离的 Claude 会话：`~/.wclaude_sessions/peer_sessions/openclaw_weixin/default/`
- 待处理附件：`~/.wclaude_sessions/runtime/openclaw_weixin/pending_attachments/default/`

## 注意事项

1. 当前默认按私聊用户处理消息，不做群聊适配。
2. `截屏` 依赖 macOS `screencapture`。
3. 出站附件通过官方 CDN 上传，依赖 `pycryptodome` 做 AES-128-ECB 加密。
4. 登录二维码优先走邮件；如果邮件不可用，会退回终端二维码。
5. 当前仓库仍保留旧的 `wechat_browser_agent.py` 作为历史实现参考，但主流程已切到官方 `openclaw-weixin`。

## License

MIT License
