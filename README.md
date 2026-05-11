# AI-BOT Algorithms

AI-BOT 边缘盒子算法交付仓库。这个仓库只保存可维护的算法服务代码、部署模板、验证流程和交付说明；客户图片、设备抓拍、模型二进制包和 SSH 密码不进 Git。

## 当前内容

| 模块 | 状态 | 说明 |
|------|------|------|
| `m101-scene-change` | 已在 61651 设备验证 | 通用画面变化/画面位移检测，1-16 通道每 5 分钟巡检一轮，异常复用设备原有抓拍推送链路 |
| `m100-security-guard` | 已交付记录 | 保安服检测算法包交付说明，模型包本体保存在桌面算法包目录 |
| `algorithm-platform-catalog` | Phase 1-5 MVP 已完成 | 算法资产库、盒子只读探测、发布 worker、下发 API 和 operator 管理页；平台主机已部署到 10 服务器，1 服务器暂作可达盒子的发布控制备选 |
| `change_detector_mvp` | 原型 | 画面变化检测的单次验证脚本 |

## 目录

- `services/m101-scene-change/`: 设备侧常驻巡检服务和 systemd 模板。
- `platform/algorithm-catalog/`: 算法平台资产库的设备清单和推荐算法清单。
- `tools/algorithm_platform/`: 生成资产 catalog、探测设备、执行发布任务和启动下发 API 的工具脚本。
- `prototypes/`: 原型验证脚本。
- `docs/`: 部署、设备访问、交付和训练方法说明。
- `docs/algorithm-platform-api.zh-CN.md`: 第三方算法安装接口中文说明。
- `docs/algorithm-platform-api.md`: 平台 API 英文说明和内部高级接口记录。

## 大文件策略

不要把 `.rknn`、`.ai`、训练集图片、现场抓拍、客户视频直接提交到 GitHub。当前模型包位置记录在 `docs/security-guard-m100.md` 和 `docs/algorithm-platform-catalog-mvp.md`。如果后续确实需要版本化模型二进制，再单独启用 Git LFS 或对象存储。

## 当前设备基线

- Web UI: `http://42.193.140.103:61651/`
- SSH 映射: `42.193.140.103:61674`
- 设备系统: Debian 12 / aarch64
- 智能盒子路径: `/oem/smart-gw`
- 抓拍库: `/oem/smart-gw/db/snap.db`
- 抓拍图目录: `/userdata/mpp/disk`

SSH 账号密码来自本地说明书和本机长期记忆，不写入仓库。
