#!/usr/bin/env python3
"""Apply the 90-day raw / permanent human-reviewed OSS retention policy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import oss_backend
    from .retention_policy import archive_item, ensure_retention_schema, utc_now
except ImportError:
    import oss_backend
    from retention_policy import archive_item, ensure_retention_schema, utc_now


DEFAULT_ROOT = Path(os.environ.get("SAMPLE_REVIEW_ROOT", "/app"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def connect(database: Path) -> sqlite3.Connection:
    deadline = time.monotonic() + 5 * 60
    while True:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=60000")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            ensure_retention_schema(connection)
            connection.commit()
            return connection
        except sqlite3.OperationalError as exc:
            connection.close()
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(5)


def report_protected_keys(report_root: Path) -> set[str]:
    protected: set[str] = set()
    runs = report_root / "runs"
    if not runs.is_dir():
        return protected
    for manifest_path in runs.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            status_path = manifest_path.parent / "run-status.json"
            status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
            if status.get("state") == "completed":
                continue
            terminal: set[str] = set()
            ledger = manifest_path.parent / "ledger.jsonl"
            if ledger.is_file():
                for line in ledger.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    if row.get("status") in {"success", "unknown"}:
                        terminal.add(str(row.get("item_id") or ""))
            for item in manifest.get("items") or []:
                if str(item.get("item_id") or "") not in terminal and item.get("object_key"):
                    protected.add(str(item["object_key"]))
        except (OSError, ValueError, json.JSONDecodeError):
            # An unreadable unfinished run is a stop condition, not permission to delete.
            raise RuntimeError(f"cannot validate report run: {manifest_path.parent.name}")
    return protected


def active_expired_rows(
    connection: sqlite3.Connection, cutoff: int, limit: int
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM items
        WHERE storage_backend = 'oss'
          AND object_key != ''
          AND source_mtime > 0
          AND source_mtime < ?
          AND NOT (
              human_reviewed = 1
              AND decision IN ('positive', 'negative')
          )
        ORDER BY source_mtime, id
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()


def active_object_keys(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT DISTINCT object_key FROM items WHERE object_key != ''")
    }


def pending_retention_keys(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT object_key FROM oss_retention_items WHERE object_deleted_at = ''"
        )
    }


def archive_orphans(
    connection: sqlite3.Connection,
    known_keys: set[str],
    cutoff: int,
    retention_days: int,
    *,
    apply: bool,
    limit: int,
) -> dict[str, int]:
    summary = {"scanned": 0, "candidates": 0, "bytes": 0, "archived": 0}
    candidates: list[dict[str, Any]] = []
    for item in oss_backend.iter_objects(f"{oss_backend.OSS_PREFIX}/objects/"):
        summary["scanned"] += 1
        if item["key"] in known_keys or int(item["last_modified"]) >= cutoff:
            continue
        summary["candidates"] += 1
        summary["bytes"] += int(item["size"])
        if apply and len(candidates) < limit:
            candidates.append(item)
    if apply:
        for item in candidates:
            synthetic_id = "orphan:" + hashlib.sha1(str(item["key"]).encode("utf-8")).hexdigest()
            archive_item(
                connection,
                {
                    "id": synthetic_id,
                    "object_key": item["key"],
                    "object_sha256": "",
                    "file_size": item["size"],
                    "source_mtime": item["last_modified"],
                },
                "unreferenced-oss-object",
                retention_days=retention_days,
                now_epoch=int(item["last_modified"]),
            )
            summary["archived"] += 1
    return summary


def due_object_keys(connection: sqlite3.Connection, now_epoch: int, limit: int) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            """
            SELECT object_key
            FROM oss_retention_items
            WHERE object_deleted_at = ''
            GROUP BY object_key
            HAVING MAX(keep_until) <= ?
            ORDER BY MIN(keep_until), object_key
            LIMIT ?
            """,
            (now_epoch, limit),
        )
    ]


def mark_attempt(connection: sqlite3.Connection, key: str, error: str = "") -> None:
    connection.execute(
        """
        UPDATE oss_retention_items
        SET delete_attempts = delete_attempts + 1,
            last_attempt_at = ?, last_error = ?
        WHERE object_key = ? AND object_deleted_at = ''
        """,
        (utc_now(), error[:300], key),
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    now_epoch = int(time.time())
    cutoff = now_epoch - args.retention_days * 24 * 60 * 60
    apply = args.mode == "apply"
    database = args.root / "data" / "review.sqlite3"
    report_root = args.root / "report-replay"
    protected_reports = report_protected_keys(report_root)
    summary: dict[str, Any] = {
        "schema": "ai-bot-oss-retention-v1",
        "mode": args.mode,
        "checkedAt": utc_now(),
        "retentionDays": args.retention_days,
        "cutoffEpoch": cutoff,
        "policy": {
            "raw": "delete after capture age reaches retentionDays",
            "humanReviewedPositiveNegative": "keep permanently",
        },
        "activeExpired": {},
        "orphans": {},
        "objects": {"due": 0, "deleted": 0, "protected": 0, "failed": 0, "bytesDeleted": 0},
    }
    with connect(database) as connection:
        expired = active_expired_rows(connection, cutoff, args.limit)
        summary["activeExpired"] = {
            "candidates": len(expired),
            "bytes": sum(int(row["file_size"] or 0) for row in expired),
            "archived": 0,
        }
        if apply:
            for row in expired:
                archive_item(
                    connection,
                    row,
                    "active-unconfirmed-expired",
                    retention_days=args.retention_days,
                )
                ingest_key = row["ingest_key"] or f"retention:{row['id']}"
                connection.execute(
                    """
                    INSERT OR REPLACE INTO deleted_items
                        (ingest_key, item_id, sha256, source_image, deleted_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ingest_key, row["id"], row["sha256"], row["source_image"], utc_now()),
                )
                connection.execute("DELETE FROM items WHERE id = ?", (row["id"],))
                summary["activeExpired"]["archived"] += 1
        active_keys = active_object_keys(connection)
        queued_keys = pending_retention_keys(connection)
        known_keys = active_keys | queued_keys | protected_reports
        summary["orphans"] = archive_orphans(
            connection,
            known_keys,
            cutoff,
            args.retention_days,
            apply=apply,
            limit=args.limit,
        )
        if not apply:
            summary["objects"]["due"] = len(due_object_keys(connection, now_epoch, args.limit))
            return summary
        connection.commit()
        active_keys = active_object_keys(connection)
        due = due_object_keys(connection, now_epoch, args.limit)
        summary["objects"]["due"] = len(due)
        for key in due:
            if key in active_keys or key in protected_reports:
                summary["objects"]["protected"] += 1
                continue
            size_row = connection.execute(
                "SELECT MAX(file_size) FROM oss_retention_items WHERE object_key = ?",
                (key,),
            ).fetchone()
            try:
                oss_backend.delete(key)
                connection.execute(
                    """
                    UPDATE oss_retention_items
                    SET object_deleted_at = ?, delete_attempts = delete_attempts + 1,
                        last_attempt_at = ?, last_error = ''
                    WHERE object_key = ? AND object_deleted_at = ''
                    """,
                    (utc_now(), utc_now(), key),
                )
                connection.commit()
                summary["objects"]["deleted"] += 1
                summary["objects"]["bytesDeleted"] += int(size_row[0] or 0)
            except (OSError, RuntimeError) as exc:
                mark_attempt(connection, key, type(exc).__name__)
                connection.commit()
                summary["objects"]["failed"] += 1
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("dry-run", "apply"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    if args.retention_days < 30 or args.limit <= 0:
        raise SystemExit("unsafe retention arguments")
    summary = run(args)
    atomic_json(args.root / "retention" / "last-run.json", summary)
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    if summary["objects"]["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
