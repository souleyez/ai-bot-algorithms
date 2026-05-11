# Algorithm Platform API

Updated: 2026-05-11

The algorithm platform API accepts authenticated publish instructions for AI-BOT boxes and records every release as an idempotent job.

Current platform host:

- Runtime: `/home/xigma01/apps/Assistant/data/runtime/algorithm-platform` on `10服务器`
- Listen address: `0.0.0.0:8791`
- Internal service address: `http://10.0.121.52:8791`
- Operator UI: `http://10.0.121.52:8791/operator`
- Health check: `http://10.0.121.52:8791/health`
- Public egress IP observed from the server: `113.108.131.252`
- Public direct access to `113.108.131.252:8791` was not reachable from the Codex desktop test network on 2026-05-11. Third-party testers need network/VPN access to `10.0.121.52`, or a later HTTPS reverse proxy.
- Current installation/execution host: `1服务器`
- Installation API runtime on `1服务器`: `/srv/ai-bot-algorithm-platform`
- Installation API listen address: `127.0.0.1:8791`
- `1服务器` can reach the mapped box SSH ports `42.193.140.103:61673/61674`.
- Release execution from `10服务器` needs network access to the mapped box ports. On 2026-05-11, `10服务器` still timed out against `42.193.140.103:61673/61674`.

## Authentication

All endpoints except `GET /health` require:

```http
Authorization: Bearer <token>
```

The token is stored in local/server secret files only. Do not put it in Git, request bodies, or logs.

## Operator UI

Open:

```http
GET /operator
```

The page is served by the same Python API process. It does not embed the bearer token; the operator enters the token in the browser session.

Current UI scope:

- View known boxes, approved algorithms, and release jobs.
- Create dry-run, semi-auto, or auto release jobs.
- Approve waiting semi-auto jobs.
- Cancel waiting, dry-run, or blocked jobs.
- Preview or execute rollback for executed jobs.

The API currently listens on `10服务器` port `8791`. Operators must be able to reach `10.0.121.52:8791`, or use an SSH tunnel/internal gateway/future HTTPS reverse proxy.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/operator` | Operator web UI |
| `GET` | `/api/ai-bot/devices` | List known boxes |
| `GET` | `/api/ai-bot/algorithms` | List known algorithm artifacts |
| `GET` | `/api/ai-bot/releases` | List release jobs |
| `POST` | `/api/ai-bot/releases` | Create a dry-run, semi-auto, or auto release job |
| `GET` | `/api/ai-bot/releases/{request_id}` | Read one release job |
| `POST` | `/api/ai-bot/releases/{request_id}/approve` | Execute a waiting semi-auto job |
| `POST` | `/api/ai-bot/releases/{request_id}/cancel` | Cancel a waiting, dry-run, or blocked job |
| `POST` | `/api/ai-bot/releases/{request_id}/rollback` | Preview or execute rollback for an executed job |

## Release Request

```json
{
  "request_id": "ops-20260511-001",
  "mode": "semi_auto",
  "target_devices": ["61672"],
  "algorithm_key": "cleaner",
  "version_label": "v5c",
  "channels": [6],
  "threshold": 0.5,
  "dry_run": true,
  "reason": "61672 channel 6 cleaner validation"
}
```

Rules:

- `request_id` is required and must be 1-120 characters using only letters, numbers, `.`, `_`, and `-`.
- Reusing a valid `request_id` returns the existing job and does not start a second release.
- `target_devices` uses the human-facing device identity, such as `61672`.
- `mode` is `semi_auto` or `auto`.
- `dry_run=true` performs preflight only and never modifies the device.
- `semi_auto` waits for `/approve` before making device changes.
- `auto` is allowed only for approved artifacts and devices tagged for automatic validation.

## Rollback

Rollback uses backup paths recorded by the original job. It does not invent backup locations.

Preview first:

```json
{
  "dry_run": true,
  "reason": "preview rollback"
}
```

Execute:

```json
{
  "dry_run": false,
  "reason": "restore previous configuration"
}
```

Current rollback behavior for RKNN `.ai` jobs:

- Restore the previous model file if the release replaced a model and recorded a model backup.
- Restore backed-up `nn.extend.json` files.
- Remove only `freq.json` files created by that release job.
- Restart the affected model slot and record the result.

## Current Limits

- Automatic execution currently supports RKNN `.ai` artifacts.
- `m101` service packages are cataloged and can be planned, but service-package deployment and rollback stay blocked until their installer layout is normalized.
- The API currently listens on `10服务器` port `8791`, but public direct access was not reachable from the Codex desktop test network. Third-party external calls may need VPN, an HTTPS reverse proxy, IP allowlist, or internal gateway.
- Automatic release from `10服务器` requires a network path to the target boxes or a configured jump host.
