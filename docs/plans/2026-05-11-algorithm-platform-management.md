# AI-BOT Algorithm Platform Management Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a private algorithm asset and release platform that imports known AI-BOT `.ai`/service packages, manages versions and target boxes, and publishes selected algorithms to boxes through approved automated or semi-automated jobs.

**Architecture:** Use a server-side artifact registry plus release worker. The registry stores package metadata and file hashes; the worker performs preflight, backup, upload, restart, verification, and rollback over SSH while keeping customer artifacts and credentials out of GitHub.

**Tech Stack:** Existing 10 server environment, Python/Node service depending on integration target, PostgreSQL or SQLite for MVP metadata, filesystem/object storage for large artifacts, SSH/SFTP for device rollout, private GitHub repo for code and docs only.

---

## Implementation Progress

Updated: 2026-05-11

- Phase 1 read-only catalog: complete.
- Phase 2 device inventory probe: complete for 61651 and 61672.
- Phase 3 RKNN `.ai` release worker: MVP complete for approved `.ai` artifacts.
- Phase 4 authenticated release instruction API: MVP complete.
- Post-release management: cancel endpoint and RKNN rollback preview/execution are implemented.
- Phase 5 operator UI: MVP complete as `/operator`, served by the same API process.
- Current release-control host: `1服务器` at `/srv/ai-bot-algorithm-platform`, listening on `127.0.0.1:8791`.
- `10服务器` remains the private training/artifact mirror, but is not the release-control host because it cannot currently reach the mapped box SSH ports.
- `m101` service packages are cataloged, but automatic install is intentionally blocked until the installer and rollback layout are normalized.
- Controlled validation succeeded for `61672` `m102` 保洁 `v5c`, channel `6`, threshold `0.5`; model upload was skipped because remote MD5 already matched, and the worker still verified config, channel binding, restart, and process state.
- Cancel validation succeeded with a `semi_auto` smoke-test job. Rollback validation succeeded in dry-run mode against the executed `61672` `m102` job.

## Current Context

### Known Device Pool

Source: `C:\Users\soulzyn\memory\projects\ai-bot-small-devices.md`

- Current known small-device pool: 16 rows.
- Daily user-facing device identity is the Web UI port, for example `61672`.
- Deployment identity maps the same row to SSH port, for example `61672 -> 61673`.
- Public host is currently `42.193.140.103`.
- Store machine codes and ports in platform metadata; do not store passwords in GitHub or plain exports.

### Known Algorithm Artifacts

Source: memory plus `C:\Users\soulzyn\Desktop\算法包`

| Algorithm | Type | Slot | Recommended Local Package |
|---|---|---|---|
| 保安识别 | RKNN `.ai` | `m100 / geid=100` | `C:\Users\soulzyn\Desktop\算法包\保安服检测-rk3576-yolov5-冬夏两套制服14通道紧框修正-v3l-20260508-1600\security_guard.rk3576.ai` |
| 保洁识别 | RKNN `.ai` | `m102 / geid=102` | `C:\Users\soulzyn\Desktop\算法包\保洁检测-rk3576-yolov5-v5c-61672通道6历史增强-20260511-1045\cleaner.rk3576.ai` |
| 维修识别 | RKNN `.ai` | `m103 / geid=103` | `C:\Users\soulzyn\Desktop\算法包\工程人员检测-rk3576-yolov5-v2-balanced-20260507-2030\engineering_worker.rk3576.ai` |
| 画面位移/画面变化 | Python service package | `m101 / geid=101` | `C:\Users\soulzyn\Desktop\算法包\画面巡检-m101-通用画面变化-服务包-20260506-1423.zip` |

Important: `m101` is not an `.ai` file. The platform must support at least two artifact kinds: `rknn_ai_model` and `device_service_package`.

### Existing Deployment Rules To Preserve

- Device callback URL stays manually configured in the device UI.
- Algorithm packages must not hard-code external push URLs.
- Before replacing files on a box, back up the existing remote model/service/config.
- On RK3576 boxes, `nn_server` may need `LD_LIBRARY_PATH=/oem/usr/lib/`.
- 61672 showed an 8-model management-page display limit; hidden custom algorithms can still run manually, but the platform must warn about slot/display conflicts.
- Field captures decide whether a model version is usable; platform verification should include process status, logs, model MD5, channel bindings, and recent capture checks.

## Recommended Architecture

```mermaid
flowchart LR
  "Local memory and Desktop packages" --> "Artifact import tool"
  "Training outputs on 10 server" --> "Artifact import tool"
  "Artifact import tool" --> "Algorithm registry DB"
  "Artifact import tool" --> "Private artifact storage"
  "Operator UI or API" --> "Release API"
  "External instruction API" --> "Release API"
  "Release API" --> "Release jobs"
  "Release jobs" --> "Release worker"
  "Release worker" --> "AI-BOT boxes over SSH/SFTP"
  "Release worker" --> "Verification records"
  "Verification records" --> "Operator UI or API"
```

### Component Responsibilities

| Component | Responsibility |
|---|---|
| Artifact registry | Tracks algorithm name, slot, geid, package kind, version, chip family, hashes, source path, storage path, approval state, notes, and rollback lineage. |
| Device registry | Tracks machine code, Web UI address, SSH address, tags, last seen status, known installed algorithms, channel bindings, thresholds, and model-slot warnings. |
| Import tool | Reads memory records and known package directories, computes hashes, uploads/copies artifacts to server storage, and creates version records. |
| Release API | Accepts publish instructions, creates dry-run or release jobs, exposes status, requires approval by default, and supports rollback jobs. |
| Release worker | Executes SSH/SFTP deployment with preflight, backup, upload, config patch, restart, log verification, capture verification, and rollback. |
| Operator UI | Lets a human choose device, algorithm version, channels, threshold, and release mode; shows current state and job logs. |

## Data Model

### `devices`

- `id`
- `machine_code`
- `display_port`
- `web_host`
- `web_port`
- `ssh_host`
- `ssh_port`
- `device_family`
- `chip_family`
- `tags`
- `last_seen_at`
- `last_inventory_json`
- `notes`

### `algorithm_artifacts`

- `id`
- `algorithm_key`: for example `security_guard`, `cleaner`, `engineering_worker`, `scene_change`
- `display_name`
- `artifact_kind`: `rknn_ai_model` or `device_service_package`
- `slot`: for example `m100`
- `geid`
- `chip_family`: for example `rk3576`
- `version_label`: for example `v3l`, `v5c`
- `status`: `imported`, `approved`, `deprecated`, `blocked`
- `sha256`
- `md5`
- `size_bytes`
- `storage_uri`
- `source_uri`
- `manifest_json`
- `created_at`

### `device_algorithm_state`

- `device_id`
- `slot`
- `geid`
- `artifact_id`
- `remote_model_path`
- `threshold`
- `channels_json`
- `remote_md5`
- `process_status`
- `last_verified_at`
- `warnings_json`

### `release_jobs`

- `id`
- `requested_by`
- `instruction_source`: `ui`, `api`, `scheduled`, `manual`
- `mode`: `dry_run`, `semi_auto`, `auto`
- `target_device_ids`
- `artifact_id`
- `channel_plan_json`
- `threshold_plan_json`
- `approval_status`
- `status`: `queued`, `preflight`, `waiting_approval`, `deploying`, `verifying`, `succeeded`, `failed`, `rolled_back`
- `backup_manifest_json`
- `result_json`
- `created_at`
- `updated_at`

## API Draft

### Inventory

- `GET /api/ai-bot/devices`
- `POST /api/ai-bot/devices/import-from-memory`
- `POST /api/ai-bot/devices/:id/probe`
- `GET /api/ai-bot/algorithms`
- `POST /api/ai-bot/algorithms/import`

### Release

- `POST /api/ai-bot/releases`
- `POST /api/ai-bot/releases/:id/approve`
- `POST /api/ai-bot/releases/:id/cancel`
- `POST /api/ai-bot/releases/:id/rollback`
- `GET /api/ai-bot/releases/:id`
- `GET /api/ai-bot/releases/:id/events`

### External Publish Instruction Shape

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
  "reason": "61672 ch6 cleaner verification"
}
```

Default behavior:

- If `dry_run=true`, only inspect and report.
- If `mode=semi_auto`, prepare the job and wait for operator approval.
- If `mode=auto`, execute only when the artifact is `approved`, the device has an allowed tag, the operation is within policy, and rollback is available.

## Release Workflow

### Preflight

1. Resolve user-facing device port to SSH mapping.
2. Connect over SSH using configured secret source, never from API payload.
3. Confirm device family, machine code, disk space, architecture, and current model slots.
4. Read current `/models/<slot>` state and channel bindings under `/oem/smart-gw/chma/<slot>`.
5. Compute remote hashes for existing model/service files.
6. Warn if the model management page is already at the 8-model display limit.
7. Produce a dry-run report and require approval unless the job is explicitly policy-allowed.

### Deploy RKNN `.ai`

1. Create remote backup under `/models/backup` or a slot-local timestamped backup.
2. Upload the `.ai` to a temporary remote path.
3. Validate uploaded MD5/SHA256.
4. Atomically move the uploaded model into `/models/<slot>/`.
5. Patch `nn.json` and `nn.extend.json` only when requested.
6. Restart `nn_server` and `dposter` for that slot with the correct environment.
7. Verify logs: `lic check ok`, model path, model input/output count, configured threshold.
8. Verify processes by working directory, not only process name.
9. Check recent capture records for the target `geid`.

### Deploy Service Package

1. Create remote backup for service directory and systemd unit.
2. Upload service files into a staging directory.
3. Run syntax checks on device.
4. Install files into `/oem/smart-gw/<service>` and `/etc/systemd/system`.
5. Run dry-run mode where available.
6. Enable/restart the service.
7. Verify systemd status, logs, and `snap.db` behavior.

### Rollback

1. Stop or isolate affected slot/service.
2. Restore the backup listed in `backup_manifest_json`.
3. Restart.
4. Verify old hash/process/log state.
5. Mark release job as `rolled_back` with reason.

## Implementation Phases

### Phase 1: Read-Only Catalog MVP

Goal: get all known algorithms and boxes into one searchable server-side catalog without publishing yet.

Tasks:

1. Create a manifest schema for devices and artifacts.
2. Build a local import script that reads known memory files and Desktop algorithm packages.
3. Compute `sha256`, `md5`, size, and version labels for every `.ai` and service zip.
4. Upload/copy artifacts to server storage.
5. Create catalog records for 16 known boxes and current recommended algorithm versions.
6. Add a simple list API or CLI report.
7. Verify no secrets, customer images, `.rknn`, or raw training datasets are copied unintentionally.

Acceptance:

- Server has catalog entries for 61651 and 61672.
- Server has recommended entries for `m100`, `m102`, `m103`, and `m101`.
- Historical versions can be retained but marked `deprecated` unless explicitly approved.

### Phase 2: Device Inventory Probe

Goal: allow the platform to ask a box what is currently installed before any release.

Tasks:

1. Implement SSH probe for device identity and `/models` inventory.
2. Read `/api/v1/system/modelN` or equivalent where available.
3. Read `/oem/smart-gw/chma` channel bindings.
4. Record thresholds and current model hashes.
5. Detect display-slot conflicts and hidden manual algorithms.
6. Store probe result in `device_algorithm_state`.

Acceptance:

- Probe reports 61672 `m100/m102/m103` state correctly.
- Probe reports 61651 `m100/m101` state correctly.
- Probe never prints or stores SSH passwords.

### Phase 3: Semi-Automatic Release Worker

Goal: publish a selected approved artifact to one selected box after human approval.

Tasks:

1. Add `release_jobs`.
2. Implement dry-run report.
3. Implement approval gate.
4. Implement RKNN `.ai` deploy worker.
5. Implement service-package deploy worker for `m101`.
6. Implement rollback worker.
7. Store all remote backup paths and verification logs.

Acceptance:

- A dry-run to 61672 shows exact files that would change.
- A semi-auto release can redeploy `m102 v5c` to 61672 and verify hashes/processes.
- Rollback can restore the previous `m102` backup.

### Phase 4: API Instruction Intake

Goal: let another service create release instructions safely.

Tasks:

1. Add authenticated `POST /api/ai-bot/releases`.
2. Validate device, artifact, version, threshold, and channel plan.
3. Require idempotency key `request_id`.
4. Add HMAC or bearer-token authentication.
5. Add policy checks for `auto` mode.
6. Add event stream or polling status endpoint.

Acceptance:

- Duplicate `request_id` returns the existing job instead of starting a second release.
- Invalid device or unapproved artifact is rejected.
- `semi_auto` requests wait for approval.

### Phase 5: Operator UI

Goal: make day-to-day usage understandable without touching the shell.

Tasks:

1. Add device list with Web UI port, machine code, online state, and installed algorithms.
2. Add algorithm list with recommended/deprecated versions and hashes.
3. Add release wizard: device -> algorithm -> channels -> threshold -> dry run -> approve.
4. Add job detail page with preflight, backup, upload, restart, verify, and rollback sections.
5. Add warnings for display-slot limits and channel conflicts.

Acceptance:

- Operator can publish from UI with a visible dry-run preview.
- Operator can see the exact remote backup path after deployment.
- Operator can launch rollback from the failed/succeeded job page.

### Phase 6: Controlled Automation

Goal: support scheduled or external policy-based rollout without losing safety.

Tasks:

1. Add device tags: `lab`, `customer`, `validation`, `production`.
2. Add algorithm approval policies by tag.
3. Add maintenance window support.
4. Add maximum concurrent releases.
5. Add automatic post-deploy observation window.
6. Add weekly device-list sync from the Tencent sheet or a maintained export.

Acceptance:

- Auto mode works only on allowed devices and approved artifacts.
- Production devices default to semi-auto.
- Weekly sync can detect new boxes without overwriting human notes.

## Key Decisions

| Decision | Recommendation | Rationale |
|---|---|---|
| Artifact storage | Server filesystem first, object storage later | Small MVP, easy backup, no GitHub binary leakage. |
| Database | SQLite for a CLI MVP, PostgreSQL if integrating into `ai-data-platform` | SQLite is faster to start; PostgreSQL is better for UI/jobs/concurrency. |
| Release style | Server pushes to boxes over SSH/SFTP | Current devices already expose SSH mappings; devices do not need new agents. |
| Default release mode | Semi-automatic | Prevents accidental production rollout while we are still learning device quirks. |
| Version policy | Import all, approve one recommended version per algorithm | Keeps rollback history without making old versions easy to misdeploy. |
| `m101` handling | Treat as service package, not `.ai` | It has different install and verification mechanics. |

## Security And Operations

- Do not store SSH passwords in DB rows or API payloads.
- Keep credentials in server environment, secret file, or a proper vault later.
- Never publish raw customer images, training sets, `.rknn`, or `.ai` artifacts to GitHub.
- Every release job must write a backup manifest before replacing files.
- Auto mode must require approved artifacts, known devices, idempotency keys, and rollback availability.
- API publish instructions must be authenticated and logged.
- The worker should lock per device and per slot to prevent overlapping deployments.
- Every remote command should be explicit and avoid destructive wildcard operations.

## Open Questions

1. Use `10服务器` as the first platform host, or integrate into the public `120服务器` product later?
2. Should this live inside `ai-data-platform`, inside `Assistant`, or as a small standalone service first?
3. Is the first UI needed immediately, or is API/CLI plus logs enough for MVP?
4. Should historical `.ai` versions all be uploaded now, or only recommended versions plus latest rollback candidates?
5. Should production devices ever allow full auto, or should customer boxes always stay semi-auto?

## Proposed MVP Scope

Use `10服务器` first because it already hosts `算法训练` and has the relevant model/export context.

MVP includes:

- Import all known recommended artifacts.
- Import all 16 known small boxes.
- Probe 61651 and 61672.
- Support semi-auto release of one `.ai` model to one box.
- Support rollback.
- Expose a small API for publish instructions.

MVP excludes:

- Full public UI polish.
- Multi-box concurrent rollout.
- Automatic Tencent sheet sync.
- Training orchestration changes.
- Moving customer datasets or raw captures into the platform.
