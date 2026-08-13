# Sample Review Customer Reporting Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a safe, reusable customer-reporting tab to the algorithm sample review console for takeaway and New World workwear AI-positive samples.

**Architecture:** Keep the existing replay payload builder as the single source of truth, and add a small server-side reporting manager that owns immutable run directories, duplicate suppression, one active job, temporary authorization markers, and persisted status. Expose fixed-purpose same-origin APIs to the existing review UI; never accept a callback URL or algorithm identifier from the browser.

**Tech Stack:** Python 3, SQLite, filesystem JSON/JSONL evidence, `ThreadingHTTPServer`, vanilla JavaScript, existing CSS and Docker runtime.

---

### Task 1: Reporting manager and tests

**Files:**
- Create: `tools/sample_review/reporting_manager.py`
- Create: `tools/sample_review/test_reporting_manager.py`
- Modify: `tools/sample_review/replay_reviewed_box_reports.py`

**Steps:**

1. Write failing tests for the two-algorithm allowlist, new IDs, AI-positive filtering, prior-success exclusion, run state transitions, and temporary enable-file cleanup.
2. Run `python -m unittest tools.sample_review.test_reporting_manager -v` and verify the tests fail before implementation.
3. Implement preview/run creation around the existing replay module. A run is immutable after its manifest is written and lives under `report-replay/runs/<run-id>-<algorithm>-ai-positive`.
4. Scan earlier ledgers and exclude only successes with the same `report_geid`; old direct-box IDs must not suppress new secondary-review IDs.
5. Implement a single process-wide job lock. Canary and batch jobs create the run-specific `ENABLE_SEND` marker only inside `try/finally`, persist sanitized status, and remove the marker on every normal/error path.
6. Run the reporting-manager tests and the existing sample-review tests.

### Task 2: Same-origin reporting APIs

**Files:**
- Modify: `tools/sample_review/server.py`
- Modify: `tools/sample_review/test_server.py`

**Steps:**

1. Add tests for request parsing and validation: only `takeaway` and `workwear`; fixed server-side endpoint/IDs; typed confirmation required for send.
2. Add `GET /api/reporting/status` for configuration, current job, current/latest run, and recent runs.
3. Add `POST /api/reporting/prepare` to freeze the newest unsent AI-positive items for one algorithm.
4. Add `POST /api/reporting/canary` to synchronously send one newest item and verify HTTP plus application success.
5. Add `POST /api/reporting/send` to start one background batch job and return HTTP 202; reject duplicates or a missing successful canary.
6. Keep endpoint, report IDs, paths, and authorization marker content server-owned. Never return image data, machine codes, secrets, or raw response bodies.

### Task 3: Review-console reporting tab

**Files:**
- Modify: `tools/sample_review/static/index.html`
- Modify: `tools/sample_review/static/app.js`
- Modify: `tools/sample_review/static/style.css`
- Modify: `tools/sample_review/test_server.py`

**Steps:**

1. Add a third workspace tab named `客户上报` beside `AI 初审` and `补框复审`.
2. Add a reporting panel with algorithm cards for 外卖服 and 新世界工服, approved IDs, prepared/withheld/sent counts, device breakdown, run state, and recent run history.
3. Add actions in sequence: `生成预览` → `发送 1 条测试` → `发送剩余全部`. Disable later actions until the server reports the preceding state.
4. Require the operator to type a run-specific confirmation phrase before batch send. Poll status while a job is running and stop polling on success or failure.
5. Hide the gallery controls only while the reporting tab is selected; returning to either review queue restores the existing behavior.
6. Add static-contract tests that verify the tab, endpoint names, and confirmation UI exist.

### Task 4: Verification and server-8 deployment

**Files:**
- Modify on server 8 only after backup: `/srv/ai-bot-sample-review/{server.py,replay_reviewed_box_reports.py,reporting_manager.py,static/*}`

**Steps:**

1. Run `python -m unittest tools.sample_review.test_reporting_manager tools.sample_review.test_server -v` and `python -m py_compile` for all changed Python files.
2. Start a temporary local server with a temporary database/root and verify all reporting APIs without an external POST.
3. Back up the current server-8 files and database metadata required for rollback.
4. Upload only the tested files, restart only `ai-bot-sample-review.service`, and verify it is active and `/healthz` returns HTTP 200.
5. Verify the new reporting status endpoint and public tab through the existing authenticated `card.goods-editor.com` route. Do not create an enable marker or call canary/send during deployment verification.
6. Confirm there are no `ENABLE_SEND` files, no reporting send process, and no reporting timer after deployment.

### Task 5: Post-activation automatic reporting

**Files:**
- Create: `tools/sample_review/automatic_reporting.py`
- Create: `tools/sample_review/ai-bot-sample-review-auto-report.{service,timer}`
- Modify: `tools/sample_review/{reporting_manager.py,replay_reviewed_box_reports.py,oss_backend.py}`
- Modify: `tools/sample_review/static/{index.html,app.js}`

**Completed behavior:**

1. Freeze a per-algorithm maximum source-mtime cutoff before enabling the timer; combine it with `ai_labeled_at > enabledAt` so old inventory can never enter a future run.
2. Apply the same cutoff to both automatic cycles and manual reporting APIs.
3. Refresh the two boxes' report metadata at the start of every cycle.
4. Match the exact device, source GEID, and image filename without rejecting matches only because their timestamps differ by more than five minutes.
5. Deduplicate terminal records by both item ID and image SHA-256 within the new report GEID; treat unknown outcomes as terminal to prevent blind replay.
6. Automatically canary then batch; pause the durable automatic state on any exception, failed response, unknown response, or low-space condition.
7. Verify the activation cycle sends zero historical rows before enabling the five-minute timer.
