# Algorithm Platform Tools

## Build catalog

```powershell
python tools\algorithm_platform\catalog_mvp.py --include-discovered --include-companions --copy-artifacts
```

## Probe devices

`probe_device.py` is read-only and needs device SSH credentials in environment variables.

```powershell
python tools\algorithm_platform\probe_device.py --device 61672
```

## Release API

The API server is a small standard-library HTTP service. It uses the runtime catalog and release worker.

Current platform host:

- Runtime: `/home/xigma01/apps/Assistant/data/runtime/algorithm-platform` on `10服务器`
- Listen address: `0.0.0.0:8791`
- Internal service address: `http://10.0.121.52:8791`
- Operator UI: `http://10.0.121.52:8791/operator`
- `10服务器` is the preferred platform host because training artifacts already live there.
- As of 2026-05-11, `10服务器` can serve the UI and catalog API, but still times out when connecting to mapped box SSH/Web ports such as `42.193.140.103:61673`.
- Current installation/execution still goes through `1服务器` at `/srv/ai-bot-algorithm-platform` because it can reach the boxes.

Required environment:

- `AI_BOT_PLATFORM_RUNTIME`
- `AI_BOT_RELEASE_API_TOKEN`
- `AI_BOT_DEVICE_SSH_USER`
- `AI_BOT_DEVICE_SSH_PASSWORD`

Operator UI:

- `GET /operator`
- Served by the same API process.
- Does not embed the bearer token.

Example request:

```bash
curl -sS http://127.0.0.1:8791/api/ai-bot/releases \
  -H "Authorization: Bearer $AI_BOT_RELEASE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "ops-20260511-001",
    "mode": "auto",
    "target_devices": ["61672"],
    "algorithm_key": "cleaner",
    "version_label": "v5c",
    "channels": [6],
    "threshold": 0.5,
    "dry_run": false,
    "reason": "61672 channel 6 cleaner rollout"
  }'
```

Safety defaults:

- API requires bearer-token authentication.
- `request_id` must be 1-120 characters using only letters, numbers, `.`, `_`, and `-`.
- Duplicate valid `request_id` returns the existing job.
- `dry_run=true` never modifies a device.
- `semi_auto` creates a job and waits for `/approve`.
- `auto` runs only for approved artifacts and devices tagged for auto mode.
- Current automatic execution supports RKNN `.ai` artifacts. Service packages are cataloged and can dry-run, but are blocked from automatic execution until their installer is normalized.

Endpoints:

- `GET /health`
- `GET /api/ai-bot/devices`
- `GET /api/ai-bot/algorithms`
- `GET /api/ai-bot/releases`
- `POST /api/ai-bot/releases`
- `GET /api/ai-bot/releases/{request_id}`
- `POST /api/ai-bot/releases/{request_id}/approve`
- `POST /api/ai-bot/releases/{request_id}/cancel`
- `POST /api/ai-bot/releases/{request_id}/rollback`

Rollback behavior:

- Rollback uses only backup paths recorded by the original job.
- `rollback` accepts `{"dry_run": true}` to preview without touching the device.
- RKNN rollback can restore backed-up model files, restore backed-up `nn.extend.json`, remove `freq.json` files created by the job, and restart the affected slot.
