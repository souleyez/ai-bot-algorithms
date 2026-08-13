#!/usr/bin/env python3
"""Create an idempotent New World workwear r4 box re-review batch.

The source decisions stay untouched. Newer review copies intentionally have
empty saved annotations and carry the best existing candidate in
``ai_annotations``. Once a reviewer saves a box, the copy leaves the box-review
queue and its manual annotation wins in the next dataset build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_GROUP = "新世界工服_r4问题样本复审_20260809"
WORKWEAR_KINDS = {"workwear", "history-workwear", "upload-workwear"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--prepare-report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def parse_list(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def review_id(source_id: str) -> str:
    digest = hashlib.sha256(f"newworld-r4-rereview:{source_id}".encode("utf-8")).hexdigest()
    return f"nw-r4-review-{digest[:16]}"


def main() -> int:
    args = parse_args()
    report = json.loads(args.prepare_report.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    missing_box_ids = {
        str(item["id"])
        for item in report.get("exclusions", [])
        if item.get("reason") == "positive_without_complete_box" and item.get("id")
    }
    test_positive_ids = {
        str(item["id"])
        for item in manifest
        if item.get("split") == "test" and item.get("decision") == "positive" and item.get("id")
    }
    targets = [
        *(sorted((item_id, "missing-complete-box") for item_id in missing_box_ids)),
        *(sorted((item_id, "independent-test-positive") for item_id in test_positive_ids)),
    ]
    if len(targets) != len({item_id for item_id, _ in targets}):
        raise RuntimeError("review target sets overlap")

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    columns = [row[1] for row in connection.execute("PRAGMA table_info(items)")]
    placeholders = ",".join("?" for _ in columns)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "group": args.group,
        "missingCompleteBox": len(missing_box_ids),
        "independentTestPositive": len(test_positive_ids),
        "targets": len(targets),
        "inserted": 0,
        "alreadyExists": 0,
        "missingSources": [],
        "invalidSources": [],
        "candidateBoxes": 0,
        "withoutCandidate": 0,
    }

    for display_index, (source_id, category) in enumerate(targets, start=1):
        source = connection.execute("SELECT * FROM items WHERE id = ?", (source_id,)).fetchone()
        if source is None:
            summary["missingSources"].append(source_id)
            continue
        if source["source_kind"] not in WORKWEAR_KINDS or source["decision"] != "positive":
            summary["invalidSources"].append(source_id)
            continue
        item_id = review_id(source_id)
        if connection.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            summary["alreadyExists"] += 1
            continue

        candidate = parse_list(source["ai_annotations"])
        if not candidate:
            candidate = parse_list(source["annotations"])
        summary["candidateBoxes"] += len(candidate)
        if not candidate:
            summary["withoutCandidate"] += 1

        values = dict(source)
        values.update(
            {
                "id": item_id,
                "group_name": args.group,
                "display_index": display_index,
                "filename": f"r4_{category}_{source['filename']}",
                "decision": "positive",
                "notes": (
                    f"r4复审类型={category};来源条目={source_id};"
                    "请确认完整新世界工服人员框，保存框后进入下一版训练"
                ),
                "updated_at": now,
                "ingest_key": f"newworld-r4-rereview|{source_id}",
                "source_kind": "workwear",
                "annotations": "[]",
                "ai_decision": "positive",
                "ai_notes": "新世界工服r4复审候选框；人工保存框优先",
                "ai_model": "newworld-r4-rereview-candidate-copy",
                "ai_annotations": json.dumps(candidate, ensure_ascii=False, separators=(",", ":")),
                "human_reviewed": 1,
                "human_reviewed_at": now,
            }
        )
        if args.apply:
            connection.execute(
                f"INSERT INTO items ({','.join(columns)}) VALUES ({placeholders})",
                [values[column] for column in columns],
            )
        summary["inserted"] += 1

    if summary["missingSources"] or summary["invalidSources"]:
        connection.rollback()
        raise RuntimeError(json.dumps(summary, ensure_ascii=False))
    if args.apply:
        connection.commit()
    else:
        connection.rollback()
    connection.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
