# M101 在 61651 设备的部署记录

- 日期: 2026-05-06
- 设备: `42.193.140.103:61651`
- SSH: `42.193.140.103:61674`
- 服务名: `m101-scene-change.service`
- 运行状态: `active`
- 开机自启: 已启用

## 部署路径

- 脚本: `/oem/smart-gw/m101_scene_change/m101_scene_change_service.py`
- 配置: `/oem/smart-gw/m101_scene_change/config.json`
- 日志: `/oem/smart-gw/m101_scene_change/m101_scene_change.log`
- 基准图: `/oem/smart-gw/m101_scene_change/baselines`
- systemd: `/etc/systemd/system/m101-scene-change.service`

## 当前策略

- 1-16 通道全量巡检。
- 每 5 分钟一轮，滚动执行。
- 每路异常后 8 秒二次确认。
- 异常默认交给设备原有抓拍链路，由设备后台原有 URL 配置统一推送。
- 本机 MQTT/网关不可用时回退写入原抓拍历史，事件名为 `画面变化`。
- 模型/算法编号使用 `m101 / geid=101`。

## 验证结果

首次 dry-run 初始化了 1-16 通道基准图。第二轮 dry-run 全部正常，无误报：

| 通道范围 | 告警 | 预警 | 抓帧失败 | 最高分 |
|----------|------|------|----------|--------|
| 1-16 | 0 | 0 | 0 | 约 0.171 |

报警阈值为 `0.62`，当前正常波动离阈值较远。

服务启动后实测：

- CPU: 启动/抓帧时约 `3.6%`
- 内存: 约 `2.5%`
- 抓拍历史中 `geid=101` 初始记录数为 `0`，没有误写告警。

## 二次部署记录

- 时间: 2026-05-06 14:15 CST
- 操作: 从本仓库重新上传 `m101_scene_change_service.py` 和 `m101-scene-change.service`。
- 备份: 远端旧文件已备份到 `/oem/smart-gw/m101_scene_change/backups/`，备份时间戳 `20260506061416`。
- 设备端语法检查: `python3 -m py_compile` 通过。
- 服务状态: `active (running)`，开机自启 `enabled`。
- 运行进程: `/usr/bin/python3 /oem/smart-gw/m101_scene_change/m101_scene_change_service.py`。
- 本地与远端 SHA256 已核对一致:
  - 脚本: `85c0ae46b9be8f909edbfdd6d22061b11f755814fcf6fa82a6a2b939be8826f2`
  - systemd: `ab398ba91cbacddd0482c45b9ededb239964eb77bc8c69a2e887b1f8fb9fbec6`

部署前服务曾在 2026-05-06 14:05:41 写入 1 条 `geid=101` 记录：

- 通道: `16`
- 抓拍: `ch16_m101_1.jpg`

二次部署 dry-run 结果：

| 通道范围 | 告警 | 预警 | 抓帧失败 | 最高分 |
|----------|------|------|----------|--------|
| 1-16 | 0 | 2 | 0 | 0.4981 |

预警通道为 4 和 16，未达到告警阈值 `0.62`，且当前配置 `write_warning_to_history=false`，不会写入抓拍历史。部署后服务重启正常，最近日志显示通道 1、2、3 均继续正常巡检。

## 三次部署记录

- 时间: 2026-05-06 19:11 CST
- 操作: 更新抓拍输出链路，默认改为复用设备本机 MQTT `/smart_gw/cmd` 的 `ch_detect_rsp`，不在算法服务里配置外部 URL。
- 设备端语法检查: `python3 -m py_compile` 通过。
- 本机依赖检查: 设备端 `paho.mqtt.publish` 可用，`127.0.0.1:1883` 可连接。
- 服务状态: `active (running)`，开机自启 `enabled`。
- dry-run: 更新后单轮 dry-run 可完成，无脚本异常。
- 本地与远端 SHA256 已核对一致:
  - 脚本: `00ae6a643f190e6623757d8c04776f107058d0ea529aed3daf48bbd7517c20c3`
  - systemd: `ab398ba91cbacddd0482c45b9ededb239964eb77bc8c69a2e887b1f8fb9fbec6`
