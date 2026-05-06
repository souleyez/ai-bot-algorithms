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
- 异常写入原抓拍历史，事件名为 `画面变化`。
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

