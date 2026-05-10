# M100 保安服检测交付记录

## 当前成果

- 算法: 保安服检测
- 设备算法编号: `m100 / geid=100`
- 目标芯片: `rk3576 / rv3576`
- 当前设备部署路径: `/models/m100/security_guard.rk3576.ai`
- 设备配置: `/models/m100/nn.json`

## 本地成果包

模型包和压缩包保存在桌面算法包目录，不提交到 GitHub：

- `C:\Users\soulzyn\Desktop\算法包\保安服检测-rk3576-yolov5-冬夏两套制服14通道紧框修正-v3l-20260508-1600\security_guard.rk3576.ai`
- `C:\Users\soulzyn\Desktop\算法包\保安服检测-rk3576-yolov5-冬夏两套制服14通道紧框修正-v3l-20260508-1600-硬件AI包.zip`

## 最新训练版本

- 当前推荐版本: `v3l`，冬夏两套保安制服，补入 14 通道紧框修正样本。
- 训练工程: `/home/xigma01/apps/Assistant/backend/data/runtime/studio/manual/guard-v5-two-uniform-v3l-ch14-tighterbox-20260508-1555`
- 数据集: `/home/xigma01/apps/Assistant/backend/data/runtime/quick-start/qs-bd5fac25/dataset-curated-two-uniform-v3l-ch14-tighterbox-20260508-1555`
- RKNN 导出: `/home/xigma01/apps/Assistant/data/runtime/rknn-exports/guard-v5-two-uniform-v3l-ch14-tighterbox/rk3576-pad5-sigmoid-int8-norm255/security_guard_yolov5_two_uniform_v3l_ch14_tighterbox_int8_norm255.rknn`
- 指标: `P=0.91788`、`R=0.98`、`mAP50=0.94299`、`mAP50-95=0.92055`。

## 现场版本取舍

- `v3l` 已部署到 61651，部署前设备备份为 `/models/m100/security_guard.rk3576.ai.bak-before-v3l-tightbox-20260508-160205`。
- 设备当前阈值恢复为 `0.80`；恢复前配置备份为 `/models/m100/nn.extend.json.bak-v3l-restore080-20260508-165111`。
- 现场观察: 14 通道在 `0.80` 下由两个框降为一个框，右侧屏幕/闸机误框被压下；13 通道仍能产生有效抓拍。
- `v3m` 使用更强现场负样本后 13 通道置信度降到约 `0.3`，未采用。
- `v3n` 能保住 13 通道，但 14 通道右侧误框未明显改善，未采用。
- `v3o` 增加 kiosk 负样本裁剪后只把误框压到约 `0.74-0.76`，同时削弱部分 13 通道结果，未采用。

## 关键经验

- 目标通道需要真实正样本。
- 空场景、柱子、路锥、车辆、普通行人、反光背心等要作为负样本压误报。
- 如果某张图没有保安，必须作为空标签负样本加入训练。
- 现场效果不好时，优先补该通道截图和负样本再微调，不只靠调阈值。

## 部署验证

- 已修复抓拍输出，设备抓拍历史可生成 `ch*_m100_*.jpg` 记录。
- 当前 m100 曾在 13/14 通道验证过保安服检测输出。
- 后续如果只允许特定通道使用，需要在设备模型绑定处解绑其他通道。

## 抓拍推送

保安服检测继续复用设备原有 `dposter -> /smart_gw/cmd` 抓拍链路。算法包只保存抓拍图并发送内部 `ch_detect_rsp` 消息，不单独配置外部推送 URL。设备后台统一配置的 URL 会把抓拍记录按 `AI盒子AI摄像头通用JSON数据说明.docx` 中的 `POST` body 格式推出去。

通用格式要求见 `docs/alarm-push-json-format.md`。
