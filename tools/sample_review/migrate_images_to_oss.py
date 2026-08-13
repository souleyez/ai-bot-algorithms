#!/usr/bin/env python3
"""Upload review images to OSS and record verified object metadata."""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import oss_backend


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_columns(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
    migrations = {
        "storage_backend": "TEXT NOT NULL DEFAULT 'local'",
        "object_key": "TEXT NOT NULL DEFAULT ''",
        "object_sha256": "TEXT NOT NULL DEFAULT ''",
        "migrated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE items ADD COLUMN {column} {definition}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/srv/ai-bot-sample-review"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--verify-samples", type=int, default=30)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not oss_backend.configured():
        raise SystemExit("OSS storage is not configured")
    database = args.root / "data" / "review.sqlite3"
    image_root = args.root / "data" / "images"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    ensure_columns(connection)
    connection.commit()
    rows = connection.execute(
        "SELECT id, image_path, object_key, object_sha256, storage_backend FROM items ORDER BY id"
    ).fetchall()
    if args.limit > 0:
        rows = rows[: args.limit]

    summary = {"total": len(rows), "uploaded": 0, "already_migrated": 0, "missing": 0, "bytes": 0}
    verified: list[tuple[str, str]] = []
    pending: list[tuple[sqlite3.Row, Path]] = []
    for row in rows:
        source = image_root / row["image_path"]
        if row["storage_backend"] == "oss" and row["object_key"] and row["object_sha256"]:
            summary["already_migrated"] += 1
            verified.append((row["object_key"], row["object_sha256"]))
            continue
        if not source.is_file():
            summary["missing"] += 1
            continue
        pending.append((row, source))

    def migrate(entry: tuple[sqlite3.Row, Path]) -> tuple[str, str, str, int]:
        row, source = entry
        digest = oss_backend.sha256_file(source)
        key = oss_backend.object_key_for_sha256(digest, source.suffix)
        size = source.stat().st_size
        if not args.dry_run:
            oss_backend.upload(source, key)
        return row["id"], key, digest, size

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 16))) as executor:
        futures = [executor.submit(migrate, entry) for entry in pending]
        for future in as_completed(futures):
            item_id, key, digest, size = future.result()
            summary["bytes"] += size
            if not args.dry_run:
                connection.execute(
                    "UPDATE items SET storage_backend='oss', object_key=?, object_sha256=?, migrated_at=? WHERE id=?",
                    (key, digest, utc_now(), item_id),
                )
                connection.commit()
            summary["uploaded"] += 1
            verified.append((key, digest))
            completed = summary["uploaded"] + summary["already_migrated"]
            if completed % 100 == 0:
                print(json.dumps({"progress": completed, **summary}, separators=(",", ":")), flush=True)

    if not args.dry_run and verified:
        random.seed(3576)
        for key, digest in random.sample(verified, min(args.verify_samples, len(verified))):
            oss_backend.wait_until_readable(key, digest)
        summary["verified_samples"] = min(args.verify_samples, len(verified))
    print(json.dumps(summary, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
