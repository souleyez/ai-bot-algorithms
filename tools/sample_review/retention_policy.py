#!/usr/bin/env python3
"""Durable metadata for deferred OSS sample-image retention."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any


DEFAULT_RAW_RETENTION_DAYS = 90


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_retention_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS oss_retention_items (
            item_id TEXT PRIMARY KEY,
            object_key TEXT NOT NULL,
            object_sha256 TEXT NOT NULL DEFAULT '',
            file_size INTEGER NOT NULL DEFAULT 0,
            source_mtime INTEGER NOT NULL DEFAULT 0,
            keep_until INTEGER NOT NULL,
            archived_at TEXT NOT NULL,
            archive_reason TEXT NOT NULL,
            object_deleted_at TEXT NOT NULL DEFAULT '',
            delete_attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_oss_retention_due "
        "ON oss_retention_items(object_deleted_at, keep_until, object_key)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_oss_retention_key "
        "ON oss_retention_items(object_key)"
    )


def value(row: sqlite3.Row | dict[str, Any], name: str, default: Any = "") -> Any:
    try:
        result = row[name]
    except (IndexError, KeyError):
        return default
    return default if result is None else result


def keep_until_for(
    source_mtime: int,
    retention_days: int = DEFAULT_RAW_RETENTION_DAYS,
    *,
    now_epoch: int | None = None,
) -> int:
    current = int(time.time()) if now_epoch is None else int(now_epoch)
    captured = int(source_mtime or 0)
    baseline = captured if captured > 0 else current
    return baseline + int(retention_days) * 24 * 60 * 60


def archive_item(
    connection: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
    reason: str,
    *,
    retention_days: int = DEFAULT_RAW_RETENTION_DAYS,
    now_epoch: int | None = None,
) -> bool:
    object_key = str(value(row, "object_key", "") or "")
    if not object_key:
        return False
    item_id = str(value(row, "id", value(row, "item_id", "")) or "")
    if not item_id:
        raise ValueError("retention item id is required")
    keep_until = keep_until_for(
        int(value(row, "source_mtime", 0) or 0),
        retention_days,
        now_epoch=now_epoch,
    )
    connection.execute(
        """
        INSERT INTO oss_retention_items (
            item_id, object_key, object_sha256, file_size, source_mtime,
            keep_until, archived_at, archive_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            object_key = excluded.object_key,
            object_sha256 = excluded.object_sha256,
            file_size = excluded.file_size,
            source_mtime = excluded.source_mtime,
            keep_until = MAX(oss_retention_items.keep_until, excluded.keep_until),
            archive_reason = excluded.archive_reason
        """,
        (
            item_id,
            object_key,
            str(value(row, "object_sha256", "") or value(row, "sha256", "") or ""),
            int(value(row, "file_size", 0) or 0),
            int(value(row, "source_mtime", 0) or 0),
            keep_until,
            utc_now(),
            reason[:80],
        ),
    )
    return True

