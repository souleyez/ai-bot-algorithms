# M101 通用画面变化检测

`m101` 是不依赖 YOLO/Gemma 的轻量巡检服务，用于检测摄像头画面大幅变化、遮挡、过曝、严重模糊等异常。

## 运行方式

- 服务文件: `/etc/systemd/system/m101-scene-change.service`
- 脚本路径: `/oem/smart-gw/m101_scene_change/m101_scene_change_service.py`
- 配置路径: `/oem/smart-gw/m101_scene_change/config.json`
- 日志路径: `/oem/smart-gw/m101_scene_change/m101_scene_change.log`
- 基准图路径: `/oem/smart-gw/m101_scene_change/baselines`

## 默认策略

- 1-16 通道全部巡检。
- 每 5 分钟一轮。
- 单线程滚动检测，不同时拉 16 路 RTSP。
- 异常后等待 8 秒二次确认。
- 告警冷却 30 分钟，避免同一通道重复刷屏。
- 只在异常时写抓拍历史，正常状态只写日志和状态文件。

## 抓拍历史写入

服务复用设备原有抓拍历史表：

- DB: `/oem/smart-gw/db/snap.db`
- 表: `ch_g_imgs`
- 模型编号: `geid=101`
- 抓拍图: `/userdata/mpp/disk/ch{通道}_m101_{序号}.jpg`
- 缩略图: `/userdata/mpp/disk/s_ch{通道}_m101_{序号}.jpg`
- 事件名: `画面变化`

## 部署

```bash
mkdir -p /oem/smart-gw/m101_scene_change
cp m101_scene_change_service.py /oem/smart-gw/m101_scene_change/
cp m101-scene-change.service /etc/systemd/system/
chmod 755 /oem/smart-gw/m101_scene_change/m101_scene_change_service.py
systemctl daemon-reload
systemctl enable --now m101-scene-change.service
```

## 验证

```bash
python3 /oem/smart-gw/m101_scene_change/m101_scene_change_service.py --once --dry-run --verbose
systemctl status m101-scene-change.service --no-pager -l
tail -n 50 /oem/smart-gw/m101_scene_change/m101_scene_change.log
sqlite3 /oem/smart-gw/db/snap.db "select count(*) from ch_g_imgs where geid=101;"
```

