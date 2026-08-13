#!/usr/bin/env python3
"""Import curated historical AI-BOT samples into the review queue."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError


DEFAULT_REVIEW_ROOT = Path("/srv/ai-bot-sample-review")
DEFAULT_PLATFORM_ROOT = Path("/srv/ai-bot-algorithm-platform")
VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


@dataclass(frozen=True)
class Collection:
    algorithm: str
    decision: str
    group: str
    relative_root: str
    note: str


COLLECTIONS = (
    Collection(
        "takeaway",
        "positive",
        "历史确认_外卖正样本",
        "field-samples/takeaway-inventory-v19-20260720/field-strong-positive-v1/images",
        "人工强正样本复核集",
    ),
    Collection(
        "takeaway",
        "positive",
        "历史确认_外卖正样本",
        "field-samples/takeaway-retrain-candidates-20260705-094301/"
        "curated-training-v3-capture-kept/train_positive",
        "历史训练清单确认正样本",
    ),
    Collection(
        "takeaway",
        "positive",
        "历史确认_外卖正样本",
        "field-samples/takeaway-effect-check-20260703-current/"
        "curated-by-rule-v2-full-person-no-umbrella/positive",
        "完整人形且无雨伞的人工筛选正样本",
    ),
    Collection(
        "takeaway",
        "positive",
        "历史确认_外卖正样本",
        "field-samples/retrain-curation-workwear-takeaway-20260702-193028/"
        "takeaway/positive_device_box",
        "设备误报复核后确认的外卖正样本",
    ),
    Collection(
        "takeaway",
        "positive",
        "历史确认_外卖正样本",
        "field-samples/jingdong-daojia-redblack-61672-ch16-20260618/"
        "takeaway_positive_yolo/images/train",
        "京东到家红黑外卖服确认正样本",
    ),
    Collection(
        "takeaway",
        "negative",
        "历史确认_外卖负样本",
        "field-samples/takeaway-retrain-candidates-20260705-094301/"
        "curated-training-v3-capture-kept/train_negative",
        "历史训练清单确认负样本",
    ),
    Collection(
        "takeaway",
        "negative",
        "历史确认_外卖负样本",
        "field-samples/takeaway-effect-check-20260703-current/"
        "curated-by-rule-v2-full-person-no-umbrella/hard_negative",
        "人工筛选的完整人形困难负样本",
    ),
    Collection(
        "takeaway",
        "negative",
        "历史确认_外卖负样本",
        "field-samples/retrain-curation-workwear-takeaway-20260702-193028/"
        "takeaway/negative_device_false",
        "设备误报复核后确认的外卖负样本",
    ),
    Collection(
        "takeaway",
        "pending",
        "历史候选_外卖待复核",
        "field-samples/takeaway_uniform-vnext/review-20260621-1949/"
        "curation-suggestions/negative_obvious_candidate",
        "旧规则自动归类，仅作为待复核候选",
    ),
    Collection(
        "workwear",
        "positive",
        "历史确认_工服正样本",
        "field-samples/retrain-curation-workwear-takeaway-20260702-193028/"
        "workwear/positive_device_box",
        "设备抓拍复核后确认的保安保洁工服正样本",
    ),
    Collection(
        "workwear",
        "negative",
        "历史确认_工服负样本",
        "samples/newworld-workwear-v5-fieldneg-20260618-1648/images",
        "V5 训练清单中的现场空标注困难负样本",
    ),
    Collection(
        "workwear",
        "negative",
        "历史确认_工服负样本",
        "field-samples/retrain-curation-workwear-takeaway-20260702-193028/"
        "workwear/negative_device_false",
        "设备误报复核后确认的工服负样本",
    ),
    Collection(
        "workwear",
        "negative",
        "历史确认_工服负样本",
        "field-samples/jingdong-daojia-redblack-61672-ch16-20260618/"
        "workwear_negative_yolo/images/train",
        "红黑外卖服对新世界工服属于确认负样本",
    ),
    Collection(
        "workwear",
        "pending",
        "历史候选_工服待复核",
        "field-samples/61672-cleaner-hardneg-20260512-124047/images",
        "早期保洁误报候选，未逐张确认，禁止直接作为负样本",
    ),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def item_algorithm(row: sqlite3.Row) -> str:
    kind = row["source_kind"]
    group = row["group_name"]
    if kind in {"door", "upload-door"} or "小门" in group:
        return "door"
    if kind in {"workwear", "upload-workwear", "history-workwear"} or "工服" in group:
        return "workwear"
    return "takeaway"


def image_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in VALID_EXTENSIONS
        and "thumbs" not in {part.lower() for part in path.parts}
    )


def normalized_copy(source: Path, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.load()
        image = image.convert("RGB")
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        image.save(destination, format="JPEG", quality=87, optimize=True)
    return destination.stat().st_size


def backup_database(database: Path, backup_root: Path) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = backup_root / f"review.sqlite3.before-history-import-{stamp}"
    source_connection = sqlite3.connect(database)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    parser.add_argument("--platform-root", type=Path, default=DEFAULT_PLATFORM_ROOT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    data_root = args.review_root / "data"
    image_root = data_root / "images"
    database = data_root / "review.sqlite3"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")

    existing: dict[tuple[str, str], sqlite3.Row] = {}
    for row in connection.execute("SELECT * FROM items WHERE sha256 != ''"):
        existing[(item_algorithm(row), row["sha256"])] = row
    deleted_hashes = {
        row[0]
        for row in connection.execute("SELECT sha256 FROM deleted_items WHERE sha256 != ''")
    }

    candidates: dict[tuple[str, str], dict] = {}
    collection_stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for collection in COLLECTIONS:
        root = args.platform_root / collection.relative_root
        stat_key = f"{collection.algorithm}:{collection.decision}:{collection.group}"
        files = image_files(root)
        collection_stats[stat_key]["files"] += len(files)
        for source in files:
            try:
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
            except OSError:
                collection_stats[stat_key]["unreadable"] += 1
                continue
            key = (collection.algorithm, digest)
            record = candidates.setdefault(
                key,
                {
                    "algorithm": collection.algorithm,
                    "sha256": digest,
                    "decision": collection.decision,
                    "group": collection.group,
                    "filename": source.name,
                    "source": source,
                    "sources": [],
                    "notes": [],
                },
            )
            record["sources"].append(str(source))
            record["notes"].append(collection.note)
            if record["decision"] != collection.decision:
                decisions = {record["decision"], collection.decision}
                if decisions == {"pending", "positive"}:
                    record["decision"] = "positive"
                elif decisions == {"pending", "negative"}:
                    record["decision"] = "negative"
                elif decisions == {"positive", "negative"}:
                    record["decision"] = "pending"
                    record["notes"].append("历史来源标签冲突，必须人工复核")

    summary = defaultdict(int)
    import_records = []
    for key, record in sorted(candidates.items()):
        if key in existing:
            summary["already_in_algorithm"] += 1
            continue
        if record["sha256"] in deleted_hashes:
            summary["previously_discarded"] += 1
            continue
        import_records.append(record)
        summary[f"new_{record['algorithm']}_{record['decision']}"] += 1
    summary["candidate_unique"] = len(candidates)
    summary["new_total"] = len(import_records)

    report = {
        "apply": args.apply,
        "collection_stats": {key: dict(value) for key, value in collection_stats.items()},
        "summary": dict(summary),
    }
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    backup = backup_database(database, args.review_root / "backups")
    next_indexes: dict[str, int] = {}
    imported = 0
    errors = []
    try:
        for record in import_records:
            group = record["group"]
            if group not in next_indexes:
                next_indexes[group] = connection.execute(
                    "SELECT COALESCE(MAX(display_index), 0) + 1 FROM items WHERE group_name = ?",
                    (group,),
                ).fetchone()[0]
            item_id = hashlib.sha1(
                f"history|{record['algorithm']}|{record['sha256']}".encode("utf-8")
            ).hexdigest()[:20]
            relative = Path("history") / record["algorithm"] / f"{item_id}.jpg"
            destination = image_root / relative
            try:
                file_size = normalized_copy(record["source"], destination)
            except (OSError, UnidentifiedImageError) as exc:
                errors.append({"source": str(record["source"]), "error": type(exc).__name__})
                continue
            note = "；".join(dict.fromkeys(record["notes"]))
            source_text = json.dumps(record["sources"], ensure_ascii=False, separators=(",", ":"))
            now = utc_now()
            connection.execute(
                """
                INSERT INTO items (
                    id, group_name, display_index, filename, image_path, source_image,
                    split_name, sha256, decision, notes, updated_at, ingest_key,
                    source_kind, source_device, source_mtime, file_size, annotations
                ) VALUES (?, ?, ?, ?, ?, ?, 'historical', ?, ?, ?, ?, ?, ?, 'history', 0, ?, '[]')
                """,
                (
                    item_id,
                    group,
                    next_indexes[group],
                    record["filename"],
                    relative.as_posix(),
                    source_text,
                    record["sha256"],
                    record["decision"],
                    note,
                    now,
                    f"history|{record['algorithm']}|{record['sha256']}",
                    f"history-{record['algorithm']}",
                    file_size,
                ),
            )
            next_indexes[group] += 1
            imported += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    report["backup"] = str(backup)
    report["imported"] = imported
    report["errors"] = errors
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
