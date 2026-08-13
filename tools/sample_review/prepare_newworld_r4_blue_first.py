#!/usr/bin/env python3
"""Export, classify, and apply blue-first candidates for the New World r4 review batch."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

try:
    from .seed_box_review import extract_embedded_blue_annotations
    from .server import materialize_item_image, utc_now
except ImportError:
    from seed_box_review import extract_embedded_blue_annotations
    from server import materialize_item_image, utc_now


DEFAULT_GROUP = "新世界工服_r4问题样本复审_20260809"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("export", "build", "apply"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--candidate-map", type=Path)
    parser.add_argument("--expected", type=int, default=79)
    parser.add_argument("--commit", action="store_true")
    return parser.parse_args()


def parse_annotations(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("invalid annotation JSON") from exc
    if not isinstance(parsed, list) or any(not isinstance(box, dict) for box in parsed):
        raise ValueError("annotations must be a list of objects")
    return parsed


def export_batch(args: argparse.Namespace) -> dict[str, Any]:
    if args.database is None or args.images is None or args.manifest is None:
        raise ValueError("export requires --database, --images, and --manifest")
    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT * FROM items WHERE group_name = ? ORDER BY display_index, id",
        (args.group,),
    ).fetchall()
    if len(rows) != args.expected:
        raise RuntimeError(f"expected {args.expected} rows, found {len(rows)}")
    args.images.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for row in rows:
        if not (
            int(row["human_reviewed"] or 0) == 1
            and row["decision"] == "positive"
            and row["annotations"] in {"", "[]"}
            and row["source_kind"] == "workwear"
        ):
            raise RuntimeError(f"ineligible review row: {row['id']}")
        source = materialize_item_image(row)
        destination = args.images / f"{row['id']}.jpg"
        shutil.copyfile(source, destination)
        records.append(
            {
                "id": row["id"],
                "sha256": row["sha256"],
                "image": destination.name,
                "currentAnnotations": parse_annotations(row["ai_annotations"]),
            }
        )
    connection.close()
    args.manifest.write_text(
        json.dumps({"group": args.group, "items": records}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"mode": "export", "group": args.group, "items": len(records)}


def build_candidates(args: argparse.Namespace) -> dict[str, Any]:
    if args.images is None or args.manifest is None or args.candidate_map is None:
        raise ValueError("build requires --images, --manifest, and --candidate-map")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    records = manifest.get("items", [])
    if manifest.get("group") != args.group or len(records) != args.expected:
        raise RuntimeError("manifest group or count mismatch")
    candidates: dict[str, dict[str, Any]] = {}
    summary = {"embeddedBlue": 0, "redFallback": 0, "withoutCandidate": 0}
    for record in records:
        item_id = str(record["id"])
        blue = extract_embedded_blue_annotations(args.images / str(record["image"]))
        if blue:
            for box in blue:
                box["label"] = "workwear"
                box["source"] = "embedded-blue"
            annotations = blue
            selection = "embedded-blue"
            model = "rendered-blue-box-extractor-v1"
            summary["embeddedBlue"] += 1
        else:
            annotations = record.get("currentAnnotations", [])
            if not isinstance(annotations, list):
                raise ValueError(f"invalid current candidates: {item_id}")
            annotations = [dict(box, label="workwear", source="red-fallback") for box in annotations]
            if annotations:
                selection = "red-fallback-no-blue"
                model = "MiniMax-M3-no-blue-fallback"
                summary["redFallback"] += 1
            else:
                selection = "manual-no-blue-no-candidate"
                model = "manual-box-required"
                summary["withoutCandidate"] += 1
        candidates[item_id] = {
            "annotations": annotations,
            "selection": selection,
            "model": model,
        }
    payload = {"group": args.group, "summary": summary, "items": candidates}
    args.candidate_map.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"mode": "build", "group": args.group, "items": len(candidates), **summary}


def apply_candidates(args: argparse.Namespace) -> dict[str, Any]:
    if args.database is None or args.candidate_map is None:
        raise ValueError("apply requires --database and --candidate-map")
    payload = json.loads(args.candidate_map.read_text(encoding="utf-8"))
    candidates = payload.get("items", {})
    if payload.get("group") != args.group or len(candidates) != args.expected:
        raise RuntimeError("candidate map group or count mismatch")
    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT * FROM items WHERE group_name = ? ORDER BY display_index, id",
        (args.group,),
    ).fetchall()
    if len(rows) != args.expected or {row["id"] for row in rows} != set(candidates):
        raise RuntimeError("database and candidate IDs do not match")
    now = utc_now()
    updated = 0
    selections: dict[str, int] = {}
    for row in rows:
        if not (
            int(row["human_reviewed"] or 0) == 1
            and row["decision"] == "positive"
            and row["annotations"] in {"", "[]"}
            and row["source_kind"] == "workwear"
        ):
            raise RuntimeError(f"ineligible review row: {row['id']}")
        candidate = candidates[row["id"]]
        annotations = candidate.get("annotations", [])
        selection = str(candidate.get("selection") or "")
        model = str(candidate.get("model") or "")
        if not isinstance(annotations, list):
            raise ValueError(f"invalid candidate list: {row['id']}")
        expected_source = "embedded-blue" if selection == "embedded-blue" else "red-fallback"
        if annotations and any(box.get("source") != expected_source for box in annotations):
            raise ValueError(f"candidate source mismatch: {row['id']}")
        if not annotations and selection != "manual-no-blue-no-candidate":
            raise ValueError(f"empty candidate selection mismatch: {row['id']}")
        selections[selection] = selections.get(selection, 0) + 1
        confidence = max((float(box.get("confidence", 0)) for box in annotations), default=0.0)
        if args.commit:
            cursor = connection.execute(
                """
                UPDATE items
                SET ai_annotations = ?, ai_model = ?, ai_notes = ?, ai_confidence = ?,
                    ai_labeled_at = ?, ai_attempted_at = ?, ai_error = ''
                WHERE id = ? AND group_name = ? AND human_reviewed = 1
                  AND decision = 'positive' AND annotations IN ('', '[]')
                """,
                (
                    json.dumps(annotations, ensure_ascii=False, separators=(",", ":")),
                    model,
                    f"新世界工服r4复审预标:{selection};蓝框优先,无蓝框才用红框",
                    confidence,
                    now if annotations else "",
                    now,
                    row["id"],
                    args.group,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"update failed: {row['id']}")
            updated += 1
    if args.commit:
        connection.commit()
    else:
        connection.rollback()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    connection.close()
    return {
        "mode": "apply" if args.commit else "dry-run",
        "group": args.group,
        "validated": len(rows),
        "updated": updated,
        "selections": selections,
        "integrity": integrity,
    }


def main() -> int:
    args = parse_args()
    if args.mode == "export":
        summary = export_batch(args)
    elif args.mode == "build":
        summary = build_candidates(args)
    else:
        summary = apply_candidates(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
