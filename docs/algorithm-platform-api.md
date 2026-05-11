# Algorithm Platform API

Updated: 2026-05-11

The algorithm platform API accepts authenticated publish instructions for AI-BOT boxes and records every release as an idempotent job.

Current control host:

- Runtime: `/srv/ai-bot-algorithm-platform` on `1服务器`
- Listen address: `127.0.0.1:8791`
- Public exposure: not enabled yet; use an internal proxy or SSH tunnel until HTTPS and allowlisting are added.

## Authentication

All endpoints except `GET /health` require:

```http
Authorization: Bearer <token>
```

The token is stored in local/server secret files only. Do not put it in Git, request bodies, or logs.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Health check |
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
- The API is currently bound to localhost on `1服务器`; third-party external calls need an HTTPS reverse proxy, IP allowlist, or internal gateway.
