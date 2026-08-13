#!/usr/bin/env python3
"""Safe run manager for customer-facing AI-reviewed box reports."""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from . import replay_reviewed_box_reports as replay
except ImportError:
    import replay_reviewed_box_reports as replay


ALGORITHM_LABELS = {
    "takeaway": "外卖服",
    "workwear": "新世界工服",
}
ENABLE_CONTENT = "AUTHORIZED_BY_SOULZYN"
DEFAULT_ENDPOINT = "https://aibot.nwcl.com.cn/prod-api/third/aiboxall/report"
MIN_REPORT_FREE_BYTES = 1024 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def ledger_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def reported_fingerprints(report_root: Path, report_geid: int) -> tuple[set[str], set[str]]:
    """Return terminal item/image identities for one secondary-report algorithm.

    Unknown outcomes are terminal too: retrying them could duplicate a request that the
    customer accepted before the connection was lost.
    """
    item_ids: set[str] = set()
    image_hashes: set[str] = set()
    for path in report_root.glob("**/ledger.jsonl"):
        for row in ledger_rows(path):
            if (
                row.get("status") in {"success", "unknown"}
                and int(row.get("report_geid") or 0) == report_geid
            ):
                if isinstance(row.get("item_id"), str):
                    item_ids.add(row["item_id"])
                if isinstance(row.get("image_sha256"), str) and row["image_sha256"]:
                    image_hashes.add(row["image_sha256"])
    return item_ids, image_hashes


def successful_item_ids(report_root: Path, report_geid: int) -> set[str]:
    """Compatibility helper retained for callers that only need item identities."""
    return reported_fingerprints(report_root, report_geid)[0]


class ReportingManager:
    def __init__(
        self,
        root: Path,
        *,
        replay_module: Any = replay,
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self.root = Path(root)
        self.report_root = self.root / "report-replay"
        self.runs_root = self.report_root / "runs"
        self.database = self.root / "data" / "review.sqlite3"
        self.metadata = self.report_root / "box-metadata.json"
        self.identifiers = self.report_root / "report-identifiers.json"
        self.automatic_state = self.report_root / "automatic-reporting.json"
        self.endpoint = endpoint
        self.replay = replay_module
        self._operation_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._job_thread: threading.Thread | None = None
        self._job: dict[str, Any] | None = None

    def cleanup_stale_enable_files(self) -> int:
        removed = 0
        if not self.report_root.exists():
            return removed
        for path in self.report_root.glob("**/ENABLE_SEND"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def _identifier_map(self) -> dict[str, dict[str, Any]]:
        identifiers = self.replay.load_identifiers(self.identifiers)
        return {name: identifiers[name] for name in ALGORITHM_LABELS}

    def public_algorithms(self) -> list[dict[str, Any]]:
        identifiers = self._identifier_map()
        return [
            {
                "key": name,
                "label": ALGORITHM_LABELS[name],
                "sourceGeid": identifiers[name]["source_geid"],
                "reportGeid": identifiers[name]["report_geid"],
                "reportGcid": identifiers[name]["report_gcid"],
                "className": identifiers[name]["class_name"],
            }
            for name in ALGORITHM_LABELS
        ]

    def _run_id(self, source_kind: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]
        return f"{stamp}-{source_kind}-ai-positive"

    def _run_dir(self, run_id: str) -> Path:
        if not run_id or len(run_id) > 100 or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in run_id
        ):
            raise ValueError("invalid run id")
        path = (self.runs_root / run_id).resolve()
        root = self.runs_root.resolve()
        if root not in path.parents:
            raise ValueError("invalid run path")
        return path

    def _args(self, run_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(
            database=self.database,
            metadata=self.metadata,
            state_dir=run_dir,
            identifier_config=self.identifiers,
            enable_file=run_dir / "ENABLE_SEND",
            endpoint=self.endpoint,
            delay=0.5,
            limit=0,
            timeout=30.0,
            source_kind="all",
            selection="ai-positive",
            minimum_source_mtime=0,
            minimum_ai_labeled_at="",
        )

    def _write_state(self, run_dir: Path, state: str, **extra: Any) -> dict[str, Any]:
        current = read_json(run_dir / "run-status.json", {}) or {}
        value = {**current, **extra, "state": state, "updatedAt": utc_now()}
        atomic_json(run_dir / "run-status.json", value)
        return value

    def _ensure_capacity(self) -> None:
        if shutil.disk_usage(self.root).free < MIN_REPORT_FREE_BYTES:
            raise RuntimeError("server free space is below 1 GB")

    def _remove_run_cache(self, run_dir: Path) -> None:
        cache = run_dir / "cache"
        if cache.is_dir():
            shutil.rmtree(cache)

    def _manifest(self, run_dir: Path) -> dict[str, Any]:
        value = read_json(run_dir / "manifest.json")
        if not isinstance(value, dict):
            raise FileNotFoundError("reporting manifest is missing")
        return value

    def _public_run(self, run_dir: Path) -> dict[str, Any]:
        manifest = self._manifest(run_dir)
        run_state = read_json(run_dir / "run-status.json", {}) or {}
        rows = ledger_rows(run_dir / "ledger.jsonl")
        terminal = {
            row["item_id"]
            for row in rows
            if row.get("status") in {"success", "unknown"} and row.get("item_id")
        }
        success = sum(row.get("status") == "success" for row in rows)
        failed = sum(row.get("status") == "failed" for row in rows)
        unknown = sum(row.get("status") == "unknown" for row in rows)
        canary_success = any(
            row.get("phase") == "canary" and row.get("status") == "success"
            for row in rows
        )
        items = manifest.get("items") or []
        summary = dict(manifest.get("summary", {}))
        if "ai_boxes" not in summary:
            ai_boxes = 0
            for item in items:
                try:
                    annotations = json.loads(str(item.get("ai_annotations") or "[]"))
                except json.JSONDecodeError:
                    annotations = []
                if isinstance(annotations, list):
                    ai_boxes += len(annotations)
            summary["ai_boxes"] = ai_boxes
        source_kind = str(summary.get("source_kind_filter") or "")
        remaining = max(0, len(items) - len(terminal))
        state = str(run_state.get("state") or "")
        if not state:
            if failed or unknown:
                state = "failed"
            elif items and success == len(items) and remaining == 0:
                state = "completed"
            elif canary_success:
                state = "canary_succeeded"
            else:
                state = "prepared"
        return {
            "runId": run_dir.name,
            "algorithm": source_kind,
            "algorithmLabel": ALGORITHM_LABELS.get(source_kind, source_kind),
            "state": state,
            "createdAt": run_state.get("createdAt", summary.get("prepared_at", "")),
            "updatedAt": run_state.get("updatedAt", ""),
            "summary": summary,
            "manifestItems": len(items),
            "ledgerEntries": len(rows),
            "success": success,
            "failed": failed,
            "unknown": unknown,
            "remaining": remaining,
            "canarySuccess": canary_success,
            "confirmationPhrase": f"发送 {ALGORITHM_LABELS.get(source_kind, source_kind)} {remaining} 条",
            "lastError": str(run_state.get("lastError") or "")[:300],
        }

    def _recent_run_dirs(self) -> list[Path]:
        if not self.runs_root.is_dir():
            return []
        return sorted(
            (path for path in self.runs_root.iterdir() if (path / "manifest.json").is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    def status(self, run_id: str = "") -> dict[str, Any]:
        selected = self._run_dir(run_id) if run_id else None
        recent_dirs = self._recent_run_dirs()
        if selected is None and recent_dirs:
            selected = recent_dirs[0]
        with self._state_lock:
            active_job = dict(self._job) if self._job else None
        return {
            "algorithms": self.public_algorithms(),
            "activeJob": active_job,
            "automatic": read_json(self.automatic_state, {"enabled": False}) or {"enabled": False},
            "selectedRun": self._public_run(selected) if selected else None,
            "recentRuns": [self._public_run(path) for path in recent_dirs[:10]],
        }

    def prepare(
        self,
        source_kind: str,
        *,
        minimum_source_mtime: int = 0,
        minimum_ai_labeled_at: str = "",
    ) -> dict[str, Any]:
        if source_kind not in ALGORITHM_LABELS:
            raise ValueError("only takeaway and workwear reporting are supported")
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("another reporting operation is active")
        try:
            automatic = read_json(self.automatic_state, {}) or {}
            if automatic.get("enabled"):
                automatic_cutoff = int(
                    (automatic.get("cutoffByAlgorithm") or {}).get(source_kind, 0) or 0
                )
                minimum_source_mtime = max(minimum_source_mtime, automatic_cutoff)
                automatic_labeled_at = str(automatic.get("enabledAt") or "")
                if automatic_labeled_at > minimum_ai_labeled_at:
                    minimum_ai_labeled_at = automatic_labeled_at
            identifiers = self._identifier_map()
            report_geid = int(identifiers[source_kind]["report_geid"])
            excluded, excluded_hashes = reported_fingerprints(self.report_root, report_geid)
            run_id = self._run_id(source_kind)
            run_dir = self._run_dir(run_id)
            run_dir.mkdir(parents=True, mode=0o700)
            args = self._args(run_dir)
            args.source_kind = source_kind
            args.minimum_source_mtime = minimum_source_mtime
            args.minimum_ai_labeled_at = minimum_ai_labeled_at
            self.replay._ACTIVE_STATE_DIR = run_dir
            summary = self.replay.prepare(
                args,
                exclude_item_ids=excluded,
                exclude_image_sha256=excluded_hashes,
            )
            self._write_state(
                run_dir,
                "prepared",
                createdAt=utc_now(),
                algorithm=source_kind,
                excludedSuccessfulItems=len(excluded),
                excludedTerminalImages=len(excluded_hashes),
            )
            os.chmod(run_dir, 0o700)
            return self._public_run(run_dir)
        except Exception:
            if "run_dir" in locals() and run_dir.exists():
                self._write_state(run_dir, "failed", lastError="prepare failed")
            raise
        finally:
            self._operation_lock.release()

    def canary(self, run_id: str) -> dict[str, Any]:
        run_dir = self._run_dir(run_id)
        run = self._public_run(run_dir)
        if run["algorithm"] not in ALGORITHM_LABELS:
            raise ValueError("unsupported reporting algorithm")
        if run["manifestItems"] <= 0:
            raise RuntimeError("the prepared run has no reportable items")
        if run["canarySuccess"]:
            raise RuntimeError("this run already has a successful canary")
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("another reporting operation is active")
        enable_file = run_dir / "ENABLE_SEND"
        try:
            self._ensure_capacity()
            enable_file.write_text(ENABLE_CONTENT + "\n", encoding="utf-8")
            os.chmod(enable_file, 0o600)
            args = self._args(run_dir)
            args.source_kind = run["algorithm"]
            self.replay._ACTIVE_STATE_DIR = run_dir
            result = self.replay.canary(args)
            if result.get("status") != "success":
                self._write_state(run_dir, "failed", lastError="canary was not accepted")
                raise RuntimeError("canary was not accepted")
            self._write_state(run_dir, "canary_succeeded")
            return self._public_run(run_dir)
        except Exception as exc:
            self._write_state(run_dir, "failed", lastError=str(exc)[:300])
            raise
        finally:
            enable_file.unlink(missing_ok=True)
            self._operation_lock.release()

    def start_send(self, run_id: str, confirmation: str) -> dict[str, Any]:
        run_dir = self._run_dir(run_id)
        run = self._public_run(run_dir)
        if not run["canarySuccess"]:
            raise RuntimeError("a successful canary is required")
        if run["remaining"] <= 0:
            raise RuntimeError("this run has no remaining items")
        if confirmation != run["confirmationPhrase"]:
            raise ValueError("confirmation phrase does not match")
        self._ensure_capacity()
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("another reporting operation is active")
        with self._state_lock:
            self._job = {
                "runId": run_id,
                "algorithm": run["algorithm"],
                "state": "sending",
                "startedAt": utc_now(),
            }
        self._write_state(run_dir, "sending", lastError="")
        self._job_thread = threading.Thread(
            target=self._send_worker,
            args=(run_dir,),
            name=f"sample-review-report-{run_id}",
            daemon=True,
        )
        self._job_thread.start()
        return self._public_run(run_dir)

    def _send_worker(self, run_dir: Path) -> None:
        enable_file = run_dir / "ENABLE_SEND"
        try:
            run = self._public_run(run_dir)
            enable_file.write_text(ENABLE_CONTENT + "\n", encoding="utf-8")
            os.chmod(enable_file, 0o600)
            args = self._args(run_dir)
            args.source_kind = run["algorithm"]
            self.replay._ACTIVE_STATE_DIR = run_dir
            result = self.replay.send(args)
            if result.get("failed") or result.get("unknown"):
                self._write_state(run_dir, "failed", result=result, lastError="batch stopped")
            else:
                self._write_state(run_dir, "completed", result=result, lastError="")
        except Exception as exc:
            self._write_state(run_dir, "failed", lastError=str(exc)[:300])
        finally:
            enable_file.unlink(missing_ok=True)
            self._remove_run_cache(run_dir)
            with self._state_lock:
                if self._job and self._job.get("runId") == run_dir.name:
                    self._job = None
            self._operation_lock.release()
