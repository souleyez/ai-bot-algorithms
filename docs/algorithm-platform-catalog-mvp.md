# Algorithm Platform Catalog MVP

Updated: 2026-05-11

## Status

Phase 1 read-only catalog is implemented. Phase 2 read-only probing is implemented. Phase 3/4 release API MVP is implemented for controlled RKNN `.ai` rollout.

The repository now contains only catalog code and non-secret metadata:

- `platform/algorithm-catalog/devices.json`
- `platform/algorithm-catalog/recommended-artifacts.json`
- `tools/algorithm_platform/catalog_mvp.py`

Generated runtime data and copied algorithm packages stay outside Git:

- Local runtime: `C:\Users\soulzyn\Desktop\codex\ai-bot-algorithms\.runtime\algorithm-platform`
- 10 server runtime: `/home/xigma01/apps/Assistant/data/runtime/algorithm-platform`
- Release API runtime: `/srv/ai-bot-algorithm-platform` on `1服务器`

## Current Import Result

Generated at `2026-05-11T03:09:36+00:00`.

| Item | Count |
|---|---:|
| Known boxes | 16 |
| Algorithm assets | 15 |
| Approved assets | 4 |
| Deprecated discovered `.ai` assets | 11 |
| Copied files | 18 |
| `.ai` files on 10 server | 14 |
| `.zip` files on 10 server | 4 |
| 10 server storage size | 164 MB |
| Missing sources | 0 |
| Blocked runtime files | 0 |

Approved assets:

| Algorithm | Version | File | MD5 |
|---|---|---|---|
| 保洁识别 | `v5c` | `cleaner.rk3576.ai` | `c3f040828d0dea908d9d39a446360638` |
| 维修识别 | `v2-balanced` | `engineering_worker.rk3576.ai` | `11881e0df47cab454543e094df2fb4eb` |
| 画面位移 | `20260506-1423` | `画面巡检-m101-通用画面变化-服务包-20260506-1423.zip` | `e7747a6b2bc28871be932d64681930b8` |
| 保安识别 | `v3l` | `security_guard.rk3576.ai` | `9a587b8aa0c562472e5f8e8eb5d1aefa` |

## Generate Catalog

From the repository root:

```powershell
python tools\algorithm_platform\catalog_mvp.py --include-discovered --include-companions --copy-artifacts
```

This writes:

- `.runtime/algorithm-platform/catalog.json`
- `.runtime/algorithm-platform/devices.json`
- `.runtime/algorithm-platform/artifacts.json`
- `.runtime/algorithm-platform/import-report.txt`
- `.runtime/algorithm-platform/artifacts/`

## Sync To 10 Server

```powershell
ssh 10服务器 "mkdir -p /home/xigma01/apps/Assistant/data/runtime"
scp -r ".runtime\algorithm-platform" "10服务器:/home/xigma01/apps/Assistant/data/runtime/"
```

Verification command used:

```bash
cd /home/xigma01/apps/Assistant/data/runtime/algorithm-platform
python3 - <<'PY'
import json, pathlib
root=pathlib.Path('.')
cat=json.loads((root/'catalog.json').read_text(encoding='utf-8'))
print('devices', len(cat['devices']))
print('artifacts', len(cat['artifacts']))
print('approved', sum(1 for a in cat['artifacts'] if a['status']=='approved'))
print('deprecated', sum(1 for a in cat['artifacts'] if a['status']=='deprecated'))
print('missing', len(cat.get('missing_sources', [])))
print('files_ai', len(list((root/'artifacts').rglob('*.ai'))))
print('files_zip', len(list((root/'artifacts').rglob('*.zip'))))
PY
```

## Safety Notes

- `.ai` and `.zip` files are copied only into ignored runtime directories and the private 10 server runtime folder.
- GitHub still only receives scripts, manifests, and documentation.
- Customer images, captures, databases, `.rknn`, `.onnx`, and `.pt` files are blocked from the runtime copy check.
- This phase does not publish to boxes or modify any device.

## Next Phase

Phase 2 should add device inventory probing:

1. Resolve a user-facing box port such as `61672` to its SSH mapping.
2. Probe `/models`, `/oem/smart-gw/chma`, thresholds, model hashes, and process state.
3. Store the result beside the catalog as read-only `device_algorithm_state`.
4. Warn when a box has model-slot display conflicts such as the 61672 eight-model limit.

## Phase 2 Probe Result

Updated: 2026-05-11

Phase 2 read-only probing is now implemented by:

- `tools/algorithm_platform/probe_device.py`

Credentials are read only from process environment variables:

- `AI_BOT_DEVICE_SSH_USER`
- `AI_BOT_DEVICE_SSH_PASSWORD`

Generated state is ignored by Git:

- Local: `.runtime/algorithm-platform/device-state/`
- 10 server: `/home/xigma01/apps/Assistant/data/runtime/algorithm-platform/device-state/`

Current validation probe:

| Device | Custom slots | Key state |
|---|---|---|
| `61651` | `m100,m101` | `m100` channels `13,14`, threshold `0.8`, MD5 `9a587b8aa0c562472e5f8e8eb5d1aefa`; `m101` service `active/enabled` |
| `61672` | `m100,m102,m103` | `m100` channel `4`; `m102` channels `1,6`, threshold `0.5`, MD5 `c3f040828d0dea908d9d39a446360638`; `m103` channels `1,4`, MD5 `11881e0df47cab454543e094df2fb4eb` |

61672 warning:

- Device reports `modelN=8`, but `/models` has 11 model directories. Custom algorithms can run, but may not appear in the Web management algorithm list.

Verification:

- Probe script syntax check passed.
- Probe state JSON parsed successfully locally and on 10 server.
- Obvious secret-string scan on `device_algorithm_state.json` found no password/env secret strings.
- No device files were modified.

## Phase 3/4 Release API Result

Updated: 2026-05-11

The release worker and HTTP instruction API are now implemented by:

- `tools/algorithm_platform/release_worker.py`
- `tools/algorithm_platform/api_server.py`

The service is running on `1服务器`:

- Runtime: `/srv/ai-bot-algorithm-platform`
- Listen address: `127.0.0.1:8791`
- Health check: `GET /health`
- Catalog endpoints: `GET /api/ai-bot/devices`, `GET /api/ai-bot/algorithms`
- Release endpoints: `POST /api/ai-bot/releases`, `GET /api/ai-bot/releases/{request_id}`, `POST /api/ai-bot/releases/{request_id}/approve`, `POST /api/ai-bot/releases/{request_id}/cancel`, `POST /api/ai-bot/releases/{request_id}/rollback`

Authentication:

- The API requires `Authorization: Bearer ...`.
- The bearer token and device SSH credentials are stored only in server/local secret files, not in Git.

Deployment host decision:

- `10服务器` keeps the private catalog/artifact mirror, but cannot currently connect to the mapped box SSH ports.
- `120服务器` can reach the boxes, but its default Python is too old for this service without a compatibility pass.
- `1服务器` can reach the boxes and has Python 3.11, so it is the current release-control host.

Implemented release behavior:

- `request_id` is required and limited to 1-120 characters using letters, numbers, `.`, `_`, and `-`.
- Duplicate valid `request_id` returns the existing job.
- `dry_run=true` performs preflight only and does not modify the box.
- `semi_auto` creates a job that waits for `/approve`.
- Waiting, dry-run, or blocked jobs can be cancelled with `/cancel`.
- Executed jobs can generate rollback previews or run rollback with `/rollback`.
- `auto` is limited to approved artifacts and devices tagged for validation.
- RKNN `.ai` artifacts are supported for automatic release.
- RKNN rollback restores only backup files recorded by the original job, removes only `freq.json` files created by that job, then restarts the affected slot.
- `m101` service packages are cataloged and can be planned, but automatic service-package install is blocked until the installer layout is normalized.

Controlled validation:

| Target | Algorithm | Request | Result |
|---|---|---|---|
| `61672` | 保洁识别 `m102 v5c` | channel `6`, threshold `0.5`, `auto`, non-dry-run | Succeeded |

Validation details:

- Remote model MD5 already matched `c3f040828d0dea908d9d39a446360638`, so model upload was skipped.
- `nn.extend.json` was backed up before config verification.
- Channel `6` was already bound under `m102`; channels `1,6` remain configured.
- `m102` `nn_server` and `dposter` were restarted and verified by process working directory.
- Device still reports `modelN=8`; custom algorithms can run even when they do not appear in the Web management algorithm list.

Post-release management validation:

- Created a `semi_auto` smoke-test release for `61672` `m102 v5c`; it reached `waiting_approval`.
- Cancelled that smoke-test release through `/cancel`; it reached `cancelled` without changing the device.
- Ran `/rollback` with `dry_run=true` against the successful `61672` `m102 v5c` validation job; it produced one rollback plan and did not touch the device.
