# Xingxy 站点助手

AstrBot 的 Telegram 业务插件：查询 WordPress 站点身份与积分、发起 OAuth 绑定、提交站点签到。站点决定绑定关系和奖励，插件接收命令并展示结果。

## 仓库与版本

- 本仓库只管理 Python 插件源码、配置格式、文档和插件单元测试。
- `v0.4.2-live` 标签保存 2026-11-11 整理仓库时采集的线上源码，不包含线上配置或用户数据，也不代表完整业务验收已通过。
- 当前开发版本为 `v0.4.3-dev`，包含此前尚未部署的可选经验值响应校验与展示。提交或推送本仓库不会部署到线上。
- 当前核对的 AstrBot 核心版本是 4.29.4（Python 3.12）。插件声明支持 4.29 系列；其他核心版本需另行验证。

## 安装与开发

在 AstrBot 的插件管理页面通过本仓库地址安装，或把仓库克隆到独立开发实例的 `data/plugins/astrbot_plugin_xingxy/`。通过 WebUI 配置站点地址及专用凭据，然后重载插件。直接安装不会获得 Xingxy 站点权限，需要站点管理员配置服务能力和允许名单。

```bash
git clone https://github.com/ExoticSkywing/astrbot_plugin_xingxy.git
cd astrbot_plugin_xingxy
python3 -m unittest discover -s tests -p 'test_*.py'
```

测试使用合成身份、临时目录、AstrBot stub 和模拟 HTTP，不访问 Telegram 或真实 WordPress。运行时网络库 `aiohttp` 由目标 AstrBot 核心提供。

真实配置通过 AstrBot 配置管理保存到插件源码目录之外；仓库仅提供 `_conf_schema.json`，不提交密钥、配置值或本地测试数据。

## 功能与配置

这是用于在 AstrBot 中验证 Xingxy 命令交互的插件。默认使用本地模拟数据，也可查询网站身份与积分、启动网站账号绑定流程，并在显式启用后执行网站签到。命令包括 `/xy_help`、`/xy_identity`、`/xy_site_identity`、`/xy_balance`、`/xy_bind`、`/xy_bind_confirm <code>`、`/xy_status`、`/xy_checkin [event_id]`、`/xy_flow` 和管理员用的 `/xy_reset`。

插件只通过 `StarTools.get_data_dir("astrbot_plugin_xingxy")` 在自己的插件数据目录中读写 `test_data.json`。默认以当前消息发送者 ID 作为模拟 TG UID，以 `default_test_user_id` 作为模拟用户；签到日期按 `Asia/Shanghai` 计算，默认奖励为 10 分。`event_id` 重放返回 `replay`，同一用户同一上海日期使用另一个事件签到返回 `already_checked_in`。

默认 `backend_mode=local`：`/xy_identity`、`/xy_flow` 和 `/xy_checkin` 使用插件自己的合成本地 JSON 数据。设置 `backend_mode=site` 后，`/xy_identity` 会通过 HMAC 服务接口只读解析网站身份；`/xy_checkin` 仅在 `site_checkin_enabled=true` 时向网站提交真实签到。网站查询和真实签到只接受 Telegram 私聊，群聊、其他平台和 AstrBot 全局 trace 开启时会在发起请求前被拒绝。真实站点签到不会访问本地 `test_data.json`。

网站查询使用 `POST {site_base_url}/wp-json/xingxy-ecosystem/v1/identity/resolve` 和 `service_signature.py` 的 HMAC-SHA256 V1 编码。仅接受 HTTPS 站点根地址；请求使用 10 秒超时、有限响应体大小，不跟随重定向。响应只显示身份绑定状态与网站 `user_id`，不读取积分余额。`/xy_status` 显示当前模式和配置是否缺失，不会发起联网检查，也不会显示密钥或站点地址。

网站绑定命令只在 `binding_enabled=true` 时可用（默认关闭），并且只接受 Telegram 私聊。它们使用独立的 `binding_service_key_id` 和 `binding_service_secret`（仅有 `identity:bind` 能力）；只读身份查询继续使用 `service_key_id` 和 `service_secret`（仅有 `identity:read` 能力）。绑定密钥不完整时，绑定命令不会回退使用只读密钥，也不会发起请求。`/xy_bind` 会请求网站创建短时授权会话；只有通过同源 HTTPS、固定 OAuth authorize 路径、精确回调地址、`scope=basic` 和 S256 PKCE 验证的授权链接才会显示。用户完成网站授权后，将页面上的 16 位确认码私聊发送给 `/xy_bind_confirm <code>`；确认码不会写入插件数据，插件会在 AstrBot core `LogRecord` 发给处理器前遮盖确认码和授权 URL。AstrBot 的 trace 存储不经过该日志过滤器，因此 `trace_enable=true` 时绑定命令会暂停，使用绑定功能期间必须保持 trace logging 关闭。会话、OAuth 凭证处理和真实绑定写入均由网站负责，插件只发送签名请求并验证响应。绑定命令不依赖 `backend_mode`，也不会读写本地 `test_data.json`。

`/xy_balance` 使用 `POST {site_base_url}/wp-json/xingxy-ecosystem/v1/points/balance`，根据 Telegram 发送者查询真实站点绑定和 `points` 余额，只需要专用 `points:read` 能力。它独立于 `backend_mode`，不读取或写入模拟 JSON；专用凭据缺失、群聊、其他平台、全局 trace 开启或 HTTP 请求失败时会拒绝执行，不回退使用身份或绑定密钥。普通日志中的“站点积分余额”数值也会脱敏。响应明确区分统一账本和真实 TG 签到是否已经启用，模拟 10 分不能据此当作站点资产。

`/xy_checkin [event_id]` 在 `backend_mode=site` 且 `site_checkin_enabled=true` 时调用 `POST {site_base_url}/wp-json/xingxy-ecosystem/v1/tasks/checkin`，签名凭据必须单独授予 `tasks:checkin`。首次请求会先生成 UUID；如需安全重试，可显式传回同一个 `event_id`。客户端严格验证 200 成功与 409 今日已签到响应；403 只显示通用未绑定或无权提示，不读取服务端错误正文；其他状态或响应格式错误会显示通用失败，不回退本地模拟。`/xy_help` 和 `/xy_status` 会显示真实签到开关状态及凭据是否完整，不显示密钥。

网站访问限允许名单内参与者；绑定命令会在网站授权和同 TG 私聊确认后写入真实绑定，真实签到按站点服务规则更新网站积分。旧机器人积分迁移尚未开放。请通过私有 AstrBot 运行时配置提供服务密钥，不要将密钥写入仓库。

## 配置

- `enabled`：启用或禁用命令。
- `backend_mode`：身份查询和签到后端，`local`（默认）或 `site`。
- `site_base_url`：HTTPS 站点根地址，例如 `https://example.test`；不带路径、查询参数或片段。
- `service_key_id`：服务端签名密钥 ID。
- `service_secret`：服务端签名密钥文本，UTF-8 编码后至少 32 字节。该值必须通过私有运行时配置提供。
- `binding_service_key_id`：单独的账号绑定服务密钥 ID，不复用只读身份查询密钥。
- `binding_service_secret`：单独的账号绑定服务签名密钥，至少 32 字节；该字段以密码格式显示，并通过私有运行时配置提供。
- `binding_enabled`：启用网站账号绑定命令，默认 `false`；只有在网站端完成对应功能配置后才能通过私有运行时配置显式开启。AstrBot 全局 `trace_enable` 必须保持关闭，否则命令会 fail closed。
- `points_read_enabled`：启用真实站点积分只读查询，默认 `false`；只在 Telegram 私聊和全局 trace 关闭时执行。
- `points_service_key_id`、`points_service_secret`：独立积分读取凭据，仅授予 `points:read`，不复用身份或绑定密钥。
- `site_checkin_enabled`：真实网站签到开关，默认 `false`；只有 backend 为 `site` 时生效。
- `checkin_service_key_id`、`checkin_service_secret`：独立签到凭据，仅授予 `tasks:checkin`；不复用身份、绑定或积分读取密钥。
- `reward_points`：模拟签到奖励，默认 10。
- `default_bound`：新测试身份是否默认绑定，默认开启。
- `default_test_user_id`：新测试身份使用的模拟用户 ID，默认 `10001`。
- `admin_only_reset`：默认要求 AstrBot 管理员才能运行 `/xy_reset`。

将插件目录放入 AstrBot 插件目录并在插件管理中启用后，即可运行命令。运行 `python -m unittest discover -s tests -p 'test_plugin.py'` 可执行离线插件测试；测试使用 AstrBot API stub、临时数据目录和 stubbed HTTP/signature。
