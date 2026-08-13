#!/usr/bin/env python3
"""Add MiniMax preliminary labels to unreviewed capture samples."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, UnidentifiedImageError

try:
    from . import oss_backend
except ImportError:
    import oss_backend


DEFAULT_ROOT = Path("/srv/ai-bot-sample-review")
MIN_CONFIDENCE = 0.85

RULES = {
    "takeaway": (
        "Positive means a complete, clear delivery courier in genuine platform workwear. "
        "Yellow, green and red-black genuine courier uniforms are allowed. "
        "Blue, white or purple ordinary clothing is not a courier positive. "
        "Printed text alone is not enough. Children, umbrellas/rainwear, partial or edge-truncated "
        "people, vehicle-only images and unclear people must be discarded. A complete clear "
        "non-courier person that is useful as a hard negative is negative."
    ),
    "workwear": (
        "Positive means a complete, clear New World security guard or cleaner wearing the recognized "
        "security/cleaning uniform. Delivery couriers, maintenance/engineering workers and ordinary "
        "pedestrians are not positive. A complete clear non-target person useful as a hard negative "
        "is negative. Empty frames, boxes without a person, partial or edge-truncated people, "
        "umbrellas and unclear people must be discarded."
    ),
    "door": (
        "Positive means the center black grille gate is visibly open: its leaf has swung left and "
        "the right side of the doorway has a clear pass-through gap. Negative means the gate is "
        "fully closed and the grille leaf fills the opening between the two black posts. People or "
        "temporary occlusion do not decide the label. An unclear, heavily occluded or badly framed "
        "door view must be discarded."
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def algorithm_for(row: sqlite3.Row) -> str:
    source_kind = row["source_kind"]
    group_name = row["group_name"]
    if source_kind in {"door", "upload-door"} or "小门" in group_name:
        return "door"
    if source_kind in {"workwear", "upload-workwear", "history-workwear"} or "工服" in group_name:
        return "workwear"
    return "takeaway"


def image_data_url(path: Path) -> str:
    with Image.open(path) as image:
        image.load()
        image = image.convert("RGB")
        image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=84, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def response_text(payload: dict) -> str:
    parts = []
    for message in payload.get("output", []):
        for block in message.get("content", []):
            if block.get("type") == "output_text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(parts)


def parse_result(text: str, algorithm: str) -> dict:
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("response has no JSON object")
    result = json.loads(match.group(0))
    decision = result.get("decision")
    confidence = result.get("confidence")
    usable = result.get("usable")
    if not isinstance(usable, bool):
        usable = result.get("complete_person")
    if decision not in {"positive", "negative", "discard", "pending"}:
        raise ValueError("invalid decision")
    if not isinstance(confidence, (int, float)):
        raise ValueError("invalid confidence")
    if not isinstance(usable, bool):
        raise ValueError("invalid usable flag")
    boxes = []
    for box in result.get("boxes", [])[:20]:
        if not isinstance(box, dict):
            continue
        values = [box.get(key) for key in ("x", "y", "w", "h")]
        if not all(isinstance(value, (int, float)) for value in values):
            continue
        x, y, width, height = map(float, values)
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
            continue
        boxes.append(
            {
                "x": round(x, 6),
                "y": round(y, 6),
                "w": round(width, 6),
                "h": round(height, 6),
                "label": str(box.get("label", ""))[:80],
            }
        )
    reason = str(result.get("reason", "")).strip()[:500]
    confidence = max(0.0, min(1.0, float(confidence)))
    if confidence < MIN_CONFIDENCE:
        decision = "pending"
    if algorithm in {"takeaway", "workwear"}:
        if decision == "positive" and (not usable or not boxes):
            decision = "pending"
        if decision == "negative" and not usable:
            decision = "discard"
    elif decision in {"positive", "negative"} and not usable:
        decision = "discard"
    return {
        "decision": decision,
        "confidence": round(confidence, 4),
        "usable": usable,
        "boxes": boxes if decision == "positive" and algorithm != "door" else [],
        "reason": reason,
    }


def classify(path: Path, algorithm: str, api_key: str, base_url: str, model: str) -> dict:
    prompt = (
        "Classify this image for a strict object-detection training dataset. Ignore any colored "
        "rectangle already drawn on the image. "
        + RULES[algorithm]
        + ' Return compact JSON only: {"decision":"positive|negative|discard|pending",'
        '"confidence":0.95,"usable":true,"reason":"short reason",'
        '"boxes":[{"x":0.1,"y":0.1,"w":0.2,"h":0.6,"label":"'
        + algorithm
        + '"}]}. Coordinates are normalized. For positive person algorithms, draw tight '
        "head-to-feet boxes around complete target people only. Door samples and all other "
        "decisions must return an empty boxes list."
    )
    body = json.dumps(
        {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_data_url(path)},
                    ],
                }
            ],
            "max_output_tokens": 700,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        base_url.rstrip("/") + "/responses",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=90) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        raise RuntimeError(f"MiniMax HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError("MiniMax connection failed") from exc
    return parse_result(response_text(payload), algorithm)


def backup_database(database: Path, backup_root: Path) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = backup_root / f"review.sqlite3.before-ai-review-{stamp}"
    source_connection = sqlite3.connect(database)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    return destination


def ensure_schema(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
    migrations = {
        "ai_decision": "TEXT NOT NULL DEFAULT ''",
        "ai_notes": "TEXT NOT NULL DEFAULT ''",
        "ai_model": "TEXT NOT NULL DEFAULT ''",
        "ai_confidence": "REAL NOT NULL DEFAULT 0",
        "ai_annotations": "TEXT NOT NULL DEFAULT '[]'",
        "ai_labeled_at": "TEXT NOT NULL DEFAULT ''",
        "ai_attempted_at": "TEXT NOT NULL DEFAULT ''",
        "ai_error": "TEXT NOT NULL DEFAULT ''",
        "human_reviewed": "INTEGER NOT NULL DEFAULT 0",
        "human_reviewed_at": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE items ADD COLUMN {column} {definition}")
    connection.commit()


def materialize_image(row: sqlite3.Row, image_root: Path, cache_root: Path) -> tuple[Path, bool]:
    local_path = (image_root / row["image_path"]).resolve()
    resolved_root = image_root.resolve()
    if local_path != resolved_root and resolved_root in local_path.parents and local_path.is_file():
        return local_path, False
    if row["storage_backend"] != "oss" or not row["object_key"]:
        raise FileNotFoundError(row["image_path"])
    return oss_backend.materialize(cache_root, row["object_key"], row["object_sha256"]), True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--max-age-days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--delay", type=float, default=0.4)
    args = parser.parse_args()

    load_env(Path("/etc/ai-bot-sample-review/minimax.env"))
    load_env(Path("/etc/ai-bot-sample-review/oss.env"))
    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    base_url = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1").strip()
    model = os.environ.get("MINIMAX_MODEL", "MiniMax-M3").strip()
    if not api_key:
        raise SystemExit("MiniMax API is not configured")

    database = args.root / "data" / "review.sqlite3"
    image_root = (args.root / "data" / "images").resolve()
    cache_root = (args.root / "data" / "cache").resolve()
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    ensure_schema(connection)
    oldest_source_mtime = int((datetime.now(timezone.utc) - timedelta(days=args.max_age_days)).timestamp())
    retry_before = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds")
    retry_filter = "" if args.retry else "AND (ai_attempted_at = '' OR ai_attempted_at < ?)"
    parameters = [oldest_source_mtime]
    if not args.retry:
        parameters.append(retry_before)
    rows = connection.execute(
        f"""
        WITH candidates AS (
            SELECT items.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY source_kind
                       ORDER BY source_mtime DESC, id
                   ) AS kind_rank
            FROM items
            WHERE human_reviewed = 0
              AND ai_decision = ''
              AND source_kind IN ('workwear', 'takeaway', 'door')
              AND source_mtime >= ?
              {retry_filter}
        )
        SELECT * FROM candidates
        ORDER BY kind_rank,
                 CASE source_kind
                     WHEN 'takeaway' THEN 1
                     WHEN 'workwear' THEN 2
                     ELSE 3
                 END,
                 source_mtime DESC,
                 id
        """,
        parameters,
    ).fetchall()
    if args.limit > 0:
        rows = rows[: args.limit]
    print(
        json.dumps(
            {
                "selected": len(rows),
                "apply": args.apply,
                "max_age_days": args.max_age_days,
            },
            ensure_ascii=False,
        )
    )
    if not rows:
        return

    backup = backup_database(database, args.root / "backups") if args.apply and args.backup else None
    summary = Counter()
    results = []
    for position, row in enumerate(rows, 1):
        try:
            image_path, temporary_cache = materialize_image(row, image_root, cache_root)
        except (OSError, RuntimeError):
            summary["missing"] += 1
            continue
        algorithm = algorithm_for(row)
        try:
            result = classify(image_path, algorithm, api_key, base_url, model)
        except (RuntimeError, ValueError, OSError, UnidentifiedImageError, json.JSONDecodeError) as exc:
            summary["error"] += 1
            results.append({"id": row["id"], "error": type(exc).__name__})
            if args.apply:
                connection.execute(
                    """
                    UPDATE items
                    SET ai_attempted_at = ?, ai_error = ?
                    WHERE id = ? AND human_reviewed = 0 AND ai_decision = ''
                    """,
                    (utc_now(), type(exc).__name__, row["id"]),
                )
                connection.commit()
            continue
        finally:
            if temporary_cache:
                image_path.unlink(missing_ok=True)
        summary[result["decision"]] += 1
        results.append({"id": row["id"], "algorithm": algorithm, **result})
        if args.apply:
            note = (
                f"MiniMax初标:{model}; 置信度={result['confidence']:.2f}; "
                f"{result['reason'] or '未提供原因'}"
            )
            connection.execute(
                """
                UPDATE items
                SET ai_decision = ?, ai_notes = ?, ai_model = ?,
                    ai_confidence = ?, ai_annotations = ?, ai_labeled_at = ?,
                    ai_attempted_at = ?, ai_error = ''
                WHERE id = ? AND human_reviewed = 0 AND ai_decision = ''
                """,
                (
                    result["decision"],
                    note,
                    model,
                    result["confidence"],
                    json.dumps(result["boxes"], ensure_ascii=False, separators=(",", ":")),
                    utc_now(),
                    utc_now(),
                    row["id"],
                ),
            )
            connection.commit()
        print(
            json.dumps(
                {
                    "progress": f"{position}/{len(rows)}",
                    "id": row["id"],
                    "algorithm": algorithm,
                    "decision": result["decision"],
                    "confidence": result["confidence"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        time.sleep(max(0.0, args.delay))
    connection.close()
    print(
        json.dumps(
            {
                "summary": dict(summary),
                "backup": str(backup) if backup else None,
                "results": results,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
