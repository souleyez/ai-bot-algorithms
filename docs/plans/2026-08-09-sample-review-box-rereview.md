# Sample Review Box Re-review Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a reusable box re-review queue to the server-8 sample-review console and seed all human-confirmed takeaway positives without saved boxes, preferring the blue detector rectangle already rendered into each source image.

**Architecture:** Keep semantic decisions and box approval separate. Human-confirmed positives with empty `annotations` are exposed through a dedicated read-only queue endpoint; the existing annotation PATCH promotes edited boxes into `annotations`, after which the item automatically leaves the queue. A bounded seeding utility extracts the rendered blue detector rectangle first and uses local YOLOv5 person predictions as red fallback candidates only when no blue rectangle exists. It writes `ai_annotations` only and never changes `decision`, `human_reviewed`, or source images.

**Tech Stack:** Python 3.11 standard-library HTTP/SQLite service, vanilla JavaScript/CSS, SQLite WAL, YOLOv5 text predictions, unittest.

---

### Task 1: Queue contract and tests

**Files:**
- Modify: `tools/sample_review/server.py`
- Modify: `tools/sample_review/test_server.py`

1. Add a failing test for `box_review_rows` selecting only human-reviewed positives with empty saved boxes, including rows still requiring manual drawing.
2. Add a failing test proving saved annotations remove a row from the queue.
3. Implement the query helper with SHA-256 deduplication and newest-row selection.
4. Add `boxReviewQueue` to `/healthz` and `GET /api/box-review-items`.
5. Run `python -m unittest tools.sample_review.test_server -v`; expect all tests to pass.

### Task 2: Re-review page behavior

**Files:**
- Modify: `tools/sample_review/static/index.html`
- Modify: `tools/sample_review/static/app.js`
- Modify: `tools/sample_review/static/style.css`

1. Add a top-level mode switch for `AI 初审` and `补框复审` with counts.
2. In re-review mode load `/api/box-review-items`, force positive-only display, and disable semantic decision/bulk-delete controls.
3. Open saved AI candidates in the existing canvas editor.
4. After PATCH succeeds, remove the item locally and refresh the queue count.
5. Preserve current filters, upload flow, and normal AI-review behavior.

### Task 3: AI-box seeding utility

**Files:**
- Create: `tools/sample_review/seed_box_review.py`
- Modify: `tools/sample_review/test_server.py`

1. Recover the embedded blue detector rectangle from source pixels and convert it to review-platform top-left XYWH.
2. Parse YOLO center-XYWH confidence rows only as the no-blue fallback.
3. Map prediction stems to manifest item IDs; reject missing, duplicate, out-of-range, or non-positive targets.
4. Support dry-run candidate-map generation and a transactional apply mode.
5. Update only `ai_annotations`, `ai_model`, `ai_notes`, `ai_confidence`, `ai_labeled_at`, and `ai_attempted_at` where saved annotations remain empty.

### Task 4: Local verification

1. Run the full unittest suite.
2. Run JavaScript syntax validation with `node --check`.
3. Build a disposable SQLite fixture and verify dry-run/apply counts.
4. Confirm current 301 rows remain human-reviewed positives and saved `annotations` are unchanged.

### Task 5: Server-8 deployment and data seeding

1. Verify service health, disk, database integrity, and current hashes.
2. Back up `server.py`, static assets, and a consistent SQLite snapshot under `/srv/ai-bot-sample-review/backups/box-rereview-<timestamp>/`.
3. Upload the tested files and restart only `ai-bot-sample-review.service`.
4. Verify `/healthz`, `/api/items`, and `/api/box-review-items` locally on port 8793.
5. Copy the manifest/prediction bundle privately, run the seeder dry-run, verify exact IDs/counts, then apply transactionally.
6. Generate missing predictions for newly reviewed rows with the existing configured AI annotation endpoint or leave them visible for manual drawing; do not change semantic decisions.
7. Verify the public authenticated page, queue count, image loading, and one disposable database/API round-trip without altering a production review row.
8. Remove only deployment temporary files; retain the rollback backup and evidence.

### Task 6: Acceptance

- Normal AI-review queue is unchanged.
- Re-review queue contains every human-confirmed positive lacking saved boxes, including rows with no candidate.
- Embedded blue boxes are used whenever present; red person-model boxes appear only on no-blue images.
- Saving one box causes that row to leave re-review and remain a human-confirmed positive.
- No source image, negative decision, online algorithm model, device configuration, or training artifact is changed.
