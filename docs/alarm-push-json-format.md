# 抓拍告警推送格式

来源文档: `C:\Users\soulzyn\Desktop\AI盒子AI摄像头通用JSON数据说明.docx`

## 原则

设备外部推送 URL 复用设备后台原有配置位置，由现场统一手动配置；算法包不要保存、覆盖或重复配置外部 URL。

算法识别后只需要把抓拍结果交给设备原有抓拍链路：

- 硬件模型算法: 由 `dposter` 保存抓拍图，并向本机 MQTT `/smart_gw/cmd` 发布 `ch_detect_rsp`。
- 自研服务算法: 优先复用同一条 `/smart_gw/cmd` 链路；只有本机 MQTT/网关不可用时，才回退为直接写 `snap.db`。

设备网关收到内部抓拍消息后，再按后台已配置的 URL 发起最终 `POST`。最终 HTTP body 必须符合文档中的抓拍告警格式。

## 最终 HTTP 抓拍告警 Body

```json
{
  "chid": 1,
  "ncid": 0,
  "ip": "camera ip",
  "geid": 101,
  "sn": "16位设备序列号",
  "sn32": "32位设备序列号",
  "location": "摄像头位置",
  "width": 1920,
  "height": 1080,
  "desc": "通道描述",
  "pic_data": "base64编码后的画框抓拍图",
  "spic_data": "base64编码后的原始抓拍图",
  "timestamp": 1778061600,
  "nn_output": [
    {
      "conf": 0.88,
      "gcid": 99001,
      "aid": 0,
      "cid": 99001,
      "class_name": "画面变化",
      "x1": 0.0,
      "y1": 0.0,
      "x2": 1.0,
      "y2": 1.0
    }
  ]
}
```

坐标字段 `x1/y1/x2/y2` 使用相对比例，范围为 `0.0-1.0`。

## 算法侧内部消息

算法侧复用设备内部消息，不直接写外部 URL：

```json
{
  "cmd": "ch_detect_rsp",
  "param": {
    "chid": 1,
    "ncid": 0,
    "ip": "",
    "surl": "rtsp://...",
    "geid": 101,
    "sn": "",
    "sn32": "",
    "location": "摄像头位置",
    "width": 1920,
    "height": 1080,
    "nn_output": [],
    "desc": "通道描述",
    "seq": 1,
    "sdpath": "/userdata/mpp/sdisk/",
    "dpath": "/userdata/mpp/disk/",
    "sfname": "s_ch1_m101_1.jpg",
    "fname": "ch1_m101_1.jpg"
  }
}
```

网关会根据 `dpath/fname` 和 `sdpath/sfname` 读取图片，结合设备配置生成最终 `pic_data/spic_data` 推送。

## 算法适配要求

- 保安服检测 `m100`: 继续走 `dposter -> /smart_gw/cmd`，不要在模型包里写外部 URL。
- 画面/位置移动检测 `m101`: 默认走 `smart_gw` 输出模式，保存画框图和原图后发布内部消息。
- 后续训练算法: 打包时必须保留或生成 `dposter` 后处理链路，输出 `ch_detect_rsp`，由设备统一推送。
- 不要让每个算法单独实现外部 HTTP `POST`，除非明确要做脱离设备网关的独立部署。
