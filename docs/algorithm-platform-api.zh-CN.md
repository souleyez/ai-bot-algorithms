# AI-BOT 算法平台接口说明

更新日期：2026-05-11

本文档给第三方系统对接使用。当前接口目标很简单：查询平台已有算法，然后把指定算法推送到指定盒子。

## 当前地址

公网对接地址：

- 1 服务器公网 API 前缀：`http://1.12.246.48/ai-bot-algorithm`
- 健康检查：`http://1.12.246.48/ai-bot-algorithm/health`
- 算法列表：`http://1.12.246.48/ai-bot-algorithm/api/ai-bot/install/algorithms`
- 推送安装：`http://1.12.246.48/ai-bot-algorithm/api/ai-bot/install`

公网只开放上述安装相关路径。`/ai-bot-algorithm/` 根路径和 `/ai-bot-algorithm/operator` 不对外开放。

平台管理地址：

- 10 服务器内部地址：`http://10.0.121.52:8791`
- 操作员页面：`http://10.0.121.52:8791/operator`
- 健康检查：`http://10.0.121.52:8791/health`

当前实际安装执行仍通过 1 服务器完成：

- 1 服务器运行目录：`/srv/ai-bot-algorithm-platform`
- 1 服务器当前监听：`127.0.0.1:8791`

说明：如果第三方系统无法直接访问上述内网地址，需要后续增加内网网关、VPN、SSH 隧道或 HTTPS 反向代理。

## 鉴权

除 `GET /health` 外，其余接口都需要请求头：

```http
Authorization: Bearer <token>
```

`<token>` 由平台侧单独提供，不写入请求体、日志或代码仓库。

第三方 token 提供方式：

- 平台为第三方单独生成 install-only token。
- token 只允许访问算法列表和推送安装接口。
- 原始 token 文件只放在本地桌面，1 服务器只保存 SHA-256 哈希用于校验。
- 不要把 token 放到 URL 参数里。
- 不要把 token 写进接口文档或 GitHub。
- 通过私聊、电话、企业 IM 密聊、密码管理器分享等独立安全渠道发送。
- 第三方请求时统一放在 HTTP 请求头：

```http
Authorization: Bearer <token>
```

## 对接流程

第三方只需要使用两个接口：

```http
GET /api/ai-bot/install/algorithms
POST /api/ai-bot/install
```

流程：

1. 调用算法列表接口，获取可安装算法。
2. 按盒子 Web 管理页的 80 端口号指定盒子，例如 `61672`。
3. 调用安装接口。
4. 平台先检测盒子是否满格。
5. 默认只有盒子不满格时才推送算法。

满格判断不写死 8 格。平台会读取盒子自身配置里的 `modelN`，再读取当前算法引擎列表，按盒子实际配置判断是否满格。

## 1. 获取可安装算法列表

```http
GET /api/ai-bot/install/algorithms
```

示例：

```bash
curl -H "Authorization: Bearer <token>" \
  http://1.12.246.48/ai-bot-algorithm/api/ai-bot/install/algorithms
```

返回示例：

```json
{
  "ok": true,
  "algorithms": [
    {
      "algorithm_key": "cleaner",
      "display_name": "保洁识别",
      "version_label": "v5c",
      "geid": 102,
      "default_threshold": 0.5,
      "artifact_kind": "rknn_ai_model",
      "md5": "c3f040828d0dea908d9d39a446360638"
    },
    {
      "algorithm_key": "engineering_worker",
      "display_name": "维修识别",
      "version_label": "v2-balanced",
      "geid": 103,
      "default_threshold": 0.8,
      "artifact_kind": "rknn_ai_model",
      "md5": "11881e0df47cab454543e094df2fb4eb"
    },
    {
      "algorithm_key": "security_guard",
      "display_name": "保安识别",
      "version_label": "v3l",
      "geid": 100,
      "default_threshold": 0.8,
      "artifact_kind": "rknn_ai_model",
      "md5": "9a587b8aa0c562472e5f8e8eb5d1aefa"
    }
  ]
}
```

说明：

- 该接口只返回已审核、可自动推送的 `.ai` 算法。
- `m101` 画面位移这类服务包暂不在第三方自动安装列表中。
- 第三方下发时使用 `algorithm_key` 指定算法。

## 2. 推送指定算法到盒子

```http
POST /api/ai-bot/install
```

最小请求：

```json
{
  "device": "61672",
  "algorithm_key": "cleaner"
}
```

常用请求：

```json
{
  "request_id": "partner-20260511-001",
  "device": "61672",
  "algorithm_key": "cleaner",
  "channels": [6],
  "dry_run": false
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `device` | 是 | 盒子的 Web 管理页 80 端口号，例如 `61672`。也兼容 `target_device`，或只有一个元素的 `target_devices`。 |
| `algorithm_key` | 是 | 算法标识，例如 `security_guard`、`cleaner`、`engineering_worker`。 |
| `request_id` | 否 | 幂等编号。重复提交同一个 `request_id` 会返回同一条任务，不会重复安装。未传时平台自动生成。 |
| `version_label` | 否 | 算法版本。只有同一算法存在多个可安装版本时才需要传。 |
| `channels` | 否 | 要绑定算法的通道号数组，例如 `[6]`。不传时只推送或更新算法包，不新增通道绑定。 |
| `threshold` | 否 | 置信度阈值。不传时使用算法包默认值。 |
| `dry_run` | 否 | 默认 `false`。传 `true` 时只检测连通性和满格状态，不改盒子。 |
| `allow_full` | 否 | 默认 `false`。正常对接不要传。传 `true` 可绕过满格拦截，属于人工运维覆盖开关。 |

## 满格检测规则

安装前平台会读取盒子：

- `modelN`：盒子当前配置允许的算法位数量。
- 当前算法引擎列表：盒子已经安装或展示的算法。

默认规则：

- 盒子不满格：继续推送算法。
- 盒子已满格：返回 `BoxFull`，不推送。
- 无法读取容量：返回 `BoxCapacityUnknown`，不推送。
- 如果目标算法已经在盒子算法列表中，允许更新同一个算法，不视为新增占位。

## 成功返回示例

```json
{
  "ok": true,
  "job": {
    "request_id": "partner-20260511-001",
    "api": "install",
    "status": "succeeded",
    "request": {
      "target_devices": ["61672"],
      "algorithm_key": "cleaner",
      "version_label": "v5c",
      "channels": [6],
      "threshold": 0.5
    },
    "plans": [
      {
        "device": {
          "display_id": "61672"
        },
        "artifact": {
          "algorithm_key": "cleaner",
          "version_label": "v5c",
          "geid": 102
        },
        "capacity": {
          "model_limit": 8,
          "engine_count": 6,
          "target_present": false,
          "is_full": false,
          "is_unknown": false
        }
      }
    ],
    "errors": []
  }
}
```

## 满格拦截返回示例

```json
{
  "ok": true,
  "job": {
    "request_id": "partner-20260511-002",
    "api": "install",
    "status": "blocked",
    "plans": [
      {
        "device": {
          "display_id": "61672"
        },
        "capacity": {
          "model_limit": 8,
          "engine_count": 8,
          "target_present": false,
          "is_full": true,
          "is_unknown": false
        }
      }
    ],
    "errors": [
      {
        "error": "BoxFull",
        "message": "Box algorithm slots are full; default install policy requires a free slot."
      }
    ]
  }
}
```

## 只检测不安装

把 `dry_run` 设为 `true`：

```json
{
  "request_id": "check-61672-cleaner",
  "device": "61672",
  "algorithm_key": "cleaner",
  "channels": [6],
  "dry_run": true
}
```

用途：

- 检测平台能否连上盒子。
- 检测盒子是否满格。
- 预览将要推送的算法版本和通道绑定。
- 不修改盒子文件、不重启算法进程。

## 状态说明

| 状态 | 含义 |
|---|---|
| `succeeded` | 已推送并验证成功。 |
| `blocked` | 被策略拦截，常见原因是盒子满格。 |
| `failed` | 执行失败，常见原因是盒子不可达、认证失败、文件校验失败。 |
| `dry_run_complete` | 只检测成功，没有实际安装。 |

## 错误说明

| 错误 | 含义 | 建议处理 |
|---|---|---|
| `Unauthorized` | Token 不正确或缺失。 | 检查 `Authorization` 请求头。 |
| `Unknown device` | 平台不知道这个盒子编号。 | 确认传的是 Web 管理页 80 端口号。 |
| `No approved artifact found` | 找不到可安装算法。 | 先调用算法列表接口确认 `algorithm_key`。 |
| `BoxFull` | 盒子算法位已满。 | 先人工清理盒子算法位，或由运维确认是否允许覆盖。 |
| `BoxCapacityUnknown` | 无法确认盒子容量。 | 检查盒子接口、网络和设备状态。 |
| `PlatformError` | 平台侧参数或执行异常。 | 查看返回的 `message`。 |

## 接口别名

下面两个接口是别名，功能完全相同：

```http
GET /api/ai-bot/deploy/algorithms
POST /api/ai-bot/deploy
```

建议第三方统一使用：

```http
GET /api/ai-bot/install/algorithms
POST /api/ai-bot/install
```
