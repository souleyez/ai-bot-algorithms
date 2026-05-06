# AI-BOT 设备访问规则

## 固定来源

同类 AI-BOT 设备的 SSH 初始账号密码来自本地说明书：

- `C:\Users\soulzyn\Desktop\AI-BOT\AI-云BOT说明书.docx`
- 章节: `5.14 SSH操作`

凭据保存在本机长期记忆的 `memory/secrets`，不要写入 GitHub。

## 当前测试设备

- Web UI: `http://42.193.140.103:61651/`
- SSH: `42.193.140.103:61674`
- 用户: `root`
- 验证日期: 2026-05-06
- 主机: `linaro-alip`
- 系统: Debian 12 / aarch64
- 智能盒子路径: `/oem/smart-gw`

## 操作原则

- 以后同类设备默认先按说明书账号和当前设备 SSH 方式尝试。
- 如果端口、密码或设备批次不一致，再询问用户。
- 普通回复和仓库文档不展示密码。

