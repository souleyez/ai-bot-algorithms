#!/usr/bin/env python3
"""Automatically report only post-activation AI-positive samples."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .probe_box_report_metadata import TARGET_IDS, collect, credentials
    from .reporting_manager import ALGORITHM_LABELS, ReportingManager, atomic_json, read_json
except ImportError:
    from probe_box_report_metadata import TARGET_IDS, collect, credentials
    from reporting_manager import ALGORITHM_LABELS, ReportingManager, atomic_json, read_json


DEFAULT_ROOT = Path(os.environ.get("SAMPLE_REVIEW_ROOT", "/app"))
DEFAULT_PLATFORM_ROOT = Path("/algorithm-platform")
POLL_SECONDS = 0.25
MAX_SEND_SECONDS = 30 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cutoff_by_algorithm(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = dict(
            connection.execute(
                """
                SELECT source_kind, COALESCE(MAX(source_mtime), 0)
                FROM items
                WHERE source_kind IN ('takeaway', 'workwear')
                GROUP BY source_kind
                """
            )
        )
    finally:
        connection.close()
    return {name: int(rows.get(name, 0) or 0) for name in ALGORITHM_LABELS}


def enable(manager: ReportingManager) -> dict[str, Any]:
    existing = read_json(manager.automatic_state, {}) or {}
    if existing.get("enabled") and existing.get("cutoffByAlgorithm"):
        return existing
    enabled_at = utc_now()
    state = {
        "schema": "ai-bot-automatic-reporting-v1",
        "enabled": True,
        "paused": False,
        "status": "idle",
        "enabledAt": enabled_at,
        "cutoffByAlgorithm": cutoff_by_algorithm(manager.database),
        "algorithms": list(ALGORITHM_LABELS),
        "deduplication": ["report_geid+item_id", "report_geid+image_sha256"],
        "lastCycleAt": "",
        "lastSuccessAt": "",
        "lastError": "",
    }
    atomic_json(manager.automatic_state, state)
    return state


def refresh_metadata(manager: ReportingManager, platform_root: Path) -> None:
    inventory = json.loads((platform_root / "devices.json").read_text(encoding="utf-8"))["devices"]
    devices = [item for item in inventory if str(item.get("display_id")) in TARGET_IDS]
    if {str(item.get("display_id")) for item in devices} != set(TARGET_IDS):
        raise RuntimeError("one or more automatic-reporting devices are missing")
    user, password = credentials()
    payload = {"devices": [collect(device, user, password) for device in devices]}
    atomic_json(manager.metadata, payload)


def wait_for_send(manager: ReportingManager, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + MAX_SEND_SECONDS
    while time.monotonic() < deadline:
        status = manager.status(run_id)
        if status["activeJob"] is None:
            run = status["selectedRun"]
            if run and run["state"] == "completed":
                return run
            error = (run or {}).get("lastError") or "automatic batch stopped"
            raise RuntimeError(str(error))
        time.sleep(POLL_SECONDS)
    raise RuntimeError("automatic batch timed out")


def discard_empty_run(manager: ReportingManager, run_id: str) -> None:
    run_dir = manager._run_dir(run_id)
    if run_dir.is_dir() and run_dir.parent == manager.runs_root.resolve():
        shutil.rmtree(run_dir)


def cycle(manager: ReportingManager, platform_root: Path) -> dict[str, Any]:
    state = read_json(manager.automatic_state, {}) or {}
    if not state.get("enabled"):
        raise RuntimeError("automatic reporting is not enabled")
    if state.get("paused"):
        return state
    state.update({"status": "refreshing_metadata", "lastCycleAt": utc_now(), "lastError": ""})
    atomic_json(manager.automatic_state, state)
    results: dict[str, Any] = {}
    try:
        manager._ensure_capacity()
        refresh_metadata(manager, platform_root)
        for source_kind in ALGORITHM_LABELS:
            state["status"] = f"preparing_{source_kind}"
            atomic_json(manager.automatic_state, state)
            run = manager.prepare(
                source_kind,
                minimum_source_mtime=int(state["cutoffByAlgorithm"][source_kind]),
                minimum_ai_labeled_at=str(state["enabledAt"]),
            )
            if run["manifestItems"] == 0:
                results[source_kind] = {
                    "sent": 0,
                    "waitingForMetadata": int(run["summary"].get("missing_capture", 0)),
                    "deduplicated": int(run["summary"].get("already_reported", 0)),
                }
                discard_empty_run(manager, run["runId"])
                continue
            state.update({"status": f"sending_{source_kind}", "activeRunId": run["runId"]})
            atomic_json(manager.automatic_state, state)
            canary = manager.canary(run["runId"])
            if canary["remaining"] > 0:
                manager.start_send(run["runId"], canary["confirmationPhrase"])
                completed = wait_for_send(manager, run["runId"])
            else:
                manager._write_state(manager._run_dir(run["runId"]), "completed", lastError="")
                completed = manager.status(run["runId"])["selectedRun"]
            results[source_kind] = {
                "sent": completed["success"],
                "deduplicated": int(completed["summary"].get("already_reported", 0)),
                "runId": completed["runId"],
            }
        state.update(
            {
                "status": "idle",
                "paused": False,
                "activeRunId": "",
                "lastSuccessAt": utc_now(),
                "lastResults": results,
            }
        )
        atomic_json(manager.automatic_state, state)
        return state
    except Exception as exc:
        state.update(
            {
                "status": "paused",
                "paused": True,
                "activeRunId": "",
                "lastError": f"{type(exc).__name__}: {exc}"[:500],
            }
        )
        atomic_json(manager.automatic_state, state)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("enable", "cycle", "status"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--platform-root", type=Path, default=DEFAULT_PLATFORM_ROOT)
    args = parser.parse_args()
    manager = ReportingManager(args.root)
    manager.cleanup_stale_enable_files()
    if args.mode == "enable":
        result = enable(manager)
    elif args.mode == "cycle":
        result = cycle(manager, args.platform_root)
    else:
        result = read_json(manager.automatic_state, {"enabled": False})
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
