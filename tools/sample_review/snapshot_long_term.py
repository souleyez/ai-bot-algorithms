#!/usr/bin/env python3
"""Freeze the current review queue into a hard-linked long-term dataset baseline."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def algorithm_for(row: sqlite3.Row) -> str:
    source_kind = row["source_kind"]
    group_name = row["group_name"]
    if source_kind in {"door", "upload-door"} or "小门" in group_name:
        return "door"
    if source_kind in {"workwear", "upload-workwear"} or "工服" in group_name:
        return "workwear"
    return "takeaway"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/srv/ai-bot-sample-review"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    database = root / "data" / "review.sqlite3"
    image_root = (root / "data" / "images").resolve()
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError(f"output already exists: {output}")
    output.mkdir(parents=True)
    output_images = output / "images"
    output_images.mkdir()

    source_connection = sqlite3.connect(database)
    source_connection.row_factory = sqlite3.Row
    source_connection.execute("BEGIN")
    rows = source_connection.execute(
        "SELECT * FROM items ORDER BY group_name, display_index, id"
    ).fetchall()

    snapshot_database = output / "review.sqlite3"
    snapshot_connection = sqlite3.connect(snapshot_database)
    source_connection.backup(snapshot_connection)
    snapshot_connection.close()

    manifest_path = output / "manifest.jsonl"
    algorithm_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    linked = 0
    missing: list[str] = []

    with manifest_path.open("w", encoding="utf-8", newline="\n") as manifest:
        for row in rows:
            relative = Path(row["image_path"])
            if relative.is_absolute() or ".." in relative.parts:
                missing.append(row["id"])
                continue
            source = (image_root / relative).resolve()
            if image_root not in source.parents or not source.is_file():
                missing.append(row["id"])
                continue
            destination = output_images / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, destination)
            algorithm = algorithm_for(row)
            record = {
                "id": row["id"],
                "algorithm": algorithm,
                "decision": row["decision"],
                "group": row["group_name"],
                "index": row["display_index"],
                "filename": row["filename"],
                "image": str(Path("images") / relative).replace("\\", "/"),
                "sha256": row["sha256"],
                "annotations": json.loads(row["annotations"] or "[]"),
                "source_image": row["source_image"],
                "source_kind": row["source_kind"],
                "source_device": row["source_device"],
                "source_mtime": row["source_mtime"],
                "captured_at": (
                    datetime.fromtimestamp(row["source_mtime"], timezone.utc).isoformat()
                    if row["source_mtime"] else None
                ),
            }
            manifest.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            algorithm_counts[algorithm] += 1
            decision_counts[row["decision"]] += 1
            source_counts[f"{row['source_device'] or 'historical'}:{row['source_kind'] or 'curated'}"] += 1
            linked += 1

    source_connection.close()
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_root": str(root),
        "output": str(output),
        "items": len(rows),
        "linked_images": linked,
        "missing_images": missing,
        "algorithm_counts": dict(sorted(algorithm_counts.items())),
        "decision_counts": dict(sorted(decision_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "training_policy": {
            "include_decisions": ["positive", "negative"],
            "exclude_until_reviewed": ["pending"],
            "images_are_hard_linked": True,
        },
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
