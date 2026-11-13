# Changelog

## v0.4.10

- 绑定冲突时明确说明已有绑定不会被替换，提示切换未绑定的站点账号后重新授权。
- 将绑定冲突、确认未通过和服务故障分别提示，不再把全部接口失败归因于确认码。
- 深链接和手动确认共用同一错误处理，不回显服务端正文、凭据或确认码。

## v0.4.9

- 修复深链接正则未匹配 AstrBot 去掉 `/` 唤醒前缀后的 `start xybind_<token>`，保留完整令牌并调用原有确认接口。
- 删除裸 `/start` 触发最近授权记录确认的逻辑，绑定确认必须携带对应令牌。
- 遮盖日志中的 `xybind_` 令牌；正常 `/start` 和邀请参数不由绑定处理器消费。
- 补充规范化输入、平台/trace/开关限制和日志令牌处理的回归检查。

## v0.4.8

此版本对“Telegram 丢失参数”的判断后来被运行日志证伪；该兜底逻辑在 v0.4.9 删除。

- Handle a Telegram `/start` without a preserved deep-link parameter by confirming the newest authorized pending binding for the authenticated sender.
- Keep ordinary `/start` messages untouched when no pending binding exists.

## v0.4.5

- Deep-link confirmation respects the plugin enabled switch before handling `/start` payloads.

## v0.4.4

- 支持 Telegram `start` 深链接自动提交绑定确认，保留手动确认码备用。
- 绑定确认继续使用真实 Telegram 发送者身份、站点一次性确认码和原有服务端冲突保护。

## v0.4.3-dev — 未部署

- 将已有开发副本纳入独立仓库，保留开发与线上差异。
- 校验站点可选的经验值字段，并在非零时展示；当前生产首轮 TG 签到仍采用只奖励积分的站点策略。
- 补充仓库地址、Telegram 平台声明、目标 AstrBot 版本、忽略规则和离线 CI。

## v0.4.2-live — 线上源码基线

2026-11-11 从运行目录采集源码并建立标签。包含身份查询、积分查询、OAuth 绑定、真实签到客户端及本地模拟命令。

该标签不包含私有运行配置、用户数据或生产数据库。线上与开发副本采用不同提交；完整 Telegram 验收状态由私有集成工程记录。
