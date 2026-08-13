#!/usr/bin/env python3
"""Batch-migrate review images with one ossutil sync operation."""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import oss_backend
from migrate_images_to_oss import ensure_columns


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/srv/ai-bot-sample-review"))
    parser.add_argument("--verify-samples", type=int, default=50)
    parser.add_argument("--job", type=int, default=24)
    args = parser.parse_args()
    if not oss_backend.configured():
        raise SystemExit("OSS storage is not configured")

    database = args.root / "data" / "review.sqlite3"
    image_root = args.root / "data" / "images"
    staging_root = args.root / "data" / "oss-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    ensure_columns(connection)
    connection.commit()
    rows = connection.execute(
        "SELECT id, image_path FROM items WHERE storage_backend != 'oss' OR object_key = '' ORDER BY id"
    ).fetchall()

    mappings: list[tuple[str, str, str]] = []
    missing = 0
    bytes_total = 0
    for index, row in enumerate(rows, 1):
        source = image_root / row["image_path"]
        if not source.is_file():
            missing += 1
            continue
        digest = oss_backend.sha256_file(source)
        key = oss_backend.object_key_for_sha256(digest, source.suffix)
        relative = key.removeprefix(f"{oss_backend.OSS_PREFIX}/objects/")
        destination = staging_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            os.link(source, destination)
        bytes_total += source.stat().st_size
        mappings.append((row["id"], key, digest))
        if index % 500 == 0:
            print(json.dumps({"prepared": index, "mapped": len(mappings), "missing": missing}), flush=True)

    uri = f"oss://{oss_backend.OSS_BUCKET}/{oss_backend.OSS_PREFIX}/objects/"
    subprocess.run(
        [oss_backend.OSSUTIL, "sync", f"{staging_root}/", uri, "--force", "--job", str(args.job), "--checkers", "32"],
        check=True,
        timeout=1800,
    )
    now = utc_now()
    connection.executemany(
        "UPDATE items SET storage_backend='oss', object_key=?, object_sha256=?, migrated_at=? WHERE id=?",
        [(key, digest, now, item_id) for item_id, key, digest in mappings],
    )
    connection.commit()

    random.seed(3576)
    for _, key, digest in random.sample(mappings, min(args.verify_samples, len(mappings))):
        oss_backend.wait_until_readable(key, digest)

    shutil.rmtree(staging_root)
    print(
        json.dumps(
            {
                "mapped": len(mappings),
                "missing": missing,
                "bytes": bytes_total,
                "verified_samples": min(args.verify_samples, len(mappings)),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
