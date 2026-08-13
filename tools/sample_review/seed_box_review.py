#!/usr/bin/env python3
"""Seed box-review candidates without changing human decisions or saved boxes.

Embedded blue detector boxes always win. Person-model candidates are used only
for images where no embedded blue box can be recovered.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .server import materialize_item_image, request_minimax_annotations, utc_now
except ImportError:
    from server import materialize_item_image, request_minimax_annotations, utc_now


TAKEAWAY_SOURCE_KINDS = {"takeaway", "history-takeaway", "upload-takeaway"}


@dataclass(frozen=True)
class Prediction:
    x: float
    y: float
    width: float
    height: float
    confidence: float


def parse_prediction_file(path: Path) -> list[Prediction]:
    predictions: list[Prediction] = []
    if not path.is_file():
        return predictions
    for raw in path.read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if len(parts) != 6 or int(float(parts[0])) != 0:
            continue
        x, y, width, height, confidence = map(float, parts[1:])
        if not (
            0 < width <= 1
            and 0 < height <= 1
            and 0 <= x - width / 2
            and x + width / 2 <= 1
            and 0 <= y - height / 2
            and y + height / 2 <= 1
            and 0 <= confidence <= 1
        ):
            continue
        predictions.append(Prediction(x, y, width, height, confidence))
    return sorted(predictions, key=lambda item: item.confidence, reverse=True)


def select_prediction(predictions: list[Prediction]) -> tuple[Prediction | None, str]:
    strict = [
        item
        for item in predictions
        if item.height >= 0.08
        and item.width * item.height >= 0.004
        and item.x - item.width / 2 >= 0.01
        and item.x + item.width / 2 <= 0.99
        and item.y - item.height / 2 >= 0.01
        and item.y + item.height / 2 <= 0.99
    ]
    if strict:
        return strict[0], "strict-person"
    if predictions:
        return predictions[0], "fallback-highest-confidence"
    return None, "no-person-detection"


def prediction_annotation(prediction: Prediction | None) -> list[dict[str, Any]]:
    if prediction is None:
        return []
    return [
        {
            "x": round(prediction.x - prediction.width / 2, 6),
            "y": round(prediction.y - prediction.height / 2, 6),
            "w": round(prediction.width, 6),
            "h": round(prediction.height, 6),
            "label": "takeaway",
            "confidence": round(prediction.confidence, 6),
        }
    ]


def prediction_annotations(predictions: list[Prediction]) -> list[dict[str, Any]]:
    return [prediction_annotation(prediction)[0] for prediction in predictions]


def _box_iou(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
    left_x, left_y, left_w, left_h = left
    right_x, right_y, right_w, right_h = right
    x1 = max(left_x, right_x)
    y1 = max(left_y, right_y)
    x2 = min(left_x + left_w, right_x + right_w)
    y2 = min(left_y + left_h, right_y + right_h)
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    union = left_w * left_h + right_w * right_h - intersection
    return intersection / union if union else 0.0


def extract_embedded_blue_annotations(image_path: Path) -> list[dict[str, Any]]:
    """Recover the rendered dark-blue detector rectangle from a source JPEG."""
    try:
        import cv2  # type: ignore[import-not-found]
        import numpy as np
    except ImportError as exc:  # pragma: no cover - operational dependency
        raise RuntimeError("opencv-python and numpy are required for blue-box extraction") from exc

    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError(f"cannot read image: {image_path}")
    image_height, image_width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(hsv, (105, 80, 55), (140, 255, 255))
    cyan = cv2.inRange(hsv, (78, 80, 100), (104, 255, 255))
    blue = cv2.morphologyEx(blue, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(blue, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    edge_width = max(4, round(min(image_width, image_height) / 150))
    candidates: list[dict[str, Any]] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        area_fraction = width * height / (image_width * image_height)
        aspect = height / width
        if (
            width < 20
            or height < 50
            or not 0.0015 <= area_fraction <= 0.30
            or not 0.65 <= aspect <= 6.0
        ):
            continue
        edge_density = sum(
            (
                float((blue[y:min(image_height, y + edge_width), x:x + width] > 0).mean()),
                float((blue[max(0, y + height - edge_width):y + height, x:x + width] > 0).mean()),
                float((blue[y:y + height, x:min(image_width, x + edge_width)] > 0).mean()),
                float((blue[y:y + height, max(0, x + width - edge_width):x + width] > 0).mean()),
            )
        ) / 4
        if edge_density < 0.12:
            continue
        label_window = max(16, round(min(image_width, image_height) / 30))
        cyan_pixels = int(
            (
                cyan[
                    y:min(image_height, y + label_window),
                    x:min(image_width, x + label_window),
                ]
                > 0
            ).sum()
        )
        candidates.append(
            {
                "box": (x, y, width, height),
                "area": width * height,
                "score": edge_density * (width * height) ** 0.5,
                "cyan_pixels": cyan_pixels,
            }
        )

    candidates.sort(key=lambda item: float(item["score"]), reverse=True)
    deduplicated: list[dict[str, Any]] = []
    for candidate in candidates:
        if any(_box_iou(candidate["box"], kept["box"]) > 0.75 for kept in deduplicated):
            continue
        deduplicated.append(candidate)
    labeled = [item for item in deduplicated if int(item["cyan_pixels"]) >= 8]
    pool = labeled or deduplicated
    if not pool:
        return []
    selected = max(pool, key=lambda item: float(item["score"]))
    x, y, width, height = selected["box"]
    return [
        {
            "x": round(x / image_width, 6),
            "y": round(y / image_height, 6),
            "w": round(width / image_width, 6),
            "h": round(height / image_height, 6),
            "label": "takeaway",
            "confidence": 1.0,
            "source": "embedded-blue",
        }
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--candidate-map", type=Path)
    parser.add_argument("--candidate-map-out", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--minimax-missing", action="store_true")
    return parser.parse_args()


def eligible(row: sqlite3.Row) -> bool:
    return (
        int(row["human_reviewed"] or 0) == 1
        and row["decision"] == "positive"
        and row["annotations"] in {"", "[]"}
        and row["source_kind"] in TAKEAWAY_SOURCE_KINDS
    )


def update_candidate(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    annotations: list[dict[str, Any]],
    model: str,
    note: str,
    now: str,
) -> bool:
    confidence = max((float(box.get("confidence", 0)) for box in annotations), default=0.0)
    cursor = connection.execute(
        """
        UPDATE items
        SET ai_annotations = ?, ai_model = ?, ai_notes = ?, ai_confidence = ?,
            ai_labeled_at = ?, ai_attempted_at = ?, ai_error = ''
        WHERE id = ?
          AND human_reviewed = 1
          AND decision = 'positive'
          AND annotations IN ('', '[]')
          AND ai_annotations IN ('', '[]')
        """,
        (
            json.dumps(annotations, ensure_ascii=False, separators=(",", ":")),
            model,
            note,
            confidence,
            now if annotations else "",
            now,
            row["id"],
        ),
    )
    return cursor.rowcount == 1


def main() -> int:
    args = parse_args()
    if args.apply and args.database is None:
        raise ValueError("--database is required with --apply")
    if args.candidate_map is None and (args.images is None or args.predictions is None):
        raise ValueError("use --candidate-map or provide both --images and --predictions")
    records = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("manifest must be a list")
    supplied_candidates: dict[str, dict[str, Any]] = {}
    if args.candidate_map:
        supplied_candidates = json.loads(args.candidate_map.read_text(encoding="utf-8"))
        if not isinstance(supplied_candidates, dict):
            raise ValueError("candidate map must be an object")
    connection = sqlite3.connect(args.database) if args.database else None
    if connection is not None:
        connection.row_factory = sqlite3.Row
    now = utc_now()
    summary: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "manifestRecords": len(records),
        "eligible": 0,
        "embeddedBlue": 0,
        "redFallbackImages": 0,
        "redFallbackBoxes": 0,
        "noDetection": 0,
        "updated": 0,
        "skipped": 0,
        "minimaxAttempted": 0,
        "minimaxBoxed": 0,
        "minimaxErrors": [],
    }
    candidate_map: dict[str, dict[str, Any]] = {}
    manifest_ids: set[str] = set()
    for record in records:
        item_id = str(record.get("id") or "")
        split = str(record.get("split") or "")
        image_name = Path(str(record.get("image") or "")).stem
        if not item_id or split not in {"train", "val", "test"} or not image_name:
            summary["skipped"] += 1
            continue
        if item_id in manifest_ids:
            raise ValueError(f"duplicate manifest id: {item_id}")
        manifest_ids.add(item_id)
        if connection is not None:
            row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None or not eligible(row):
                summary["skipped"] += 1
                continue
        else:
            row = {"id": item_id}
        summary["eligible"] += 1
        supplied = supplied_candidates.get(item_id)
        if supplied is not None:
            annotations = supplied.get("annotations", [])
            model = str(supplied.get("model") or "candidate-map")
            selection = str(supplied.get("selection") or "candidate-map")
            if not isinstance(annotations, list):
                raise ValueError(f"invalid candidate annotations: {item_id}")
            if selection == "embedded-blue":
                summary["embeddedBlue"] += 1
            elif annotations:
                summary["redFallbackImages"] += 1
                summary["redFallbackBoxes"] += len(annotations)
        else:
            image_path = args.images / split / f"{image_name}.jpg"
            annotations = extract_embedded_blue_annotations(image_path)
            if annotations:
                selection = "embedded-blue"
                model = "rendered-blue-box-extractor-v1"
                summary["embeddedBlue"] += 1
            else:
                predictions = parse_prediction_file(args.predictions / split / "labels" / f"{image_name}.txt")
                annotations = prediction_annotations(predictions)
                selection = "red-person-fallback" if annotations else "no-box-candidate"
                model = "yolov5s-coco-person-local-20260809"
                if annotations:
                    summary["redFallbackImages"] += 1
                    summary["redFallbackBoxes"] += len(annotations)
                    for annotation in annotations:
                        annotation["source"] = "red-fallback"
        candidate_map[item_id] = {
            "annotations": annotations,
            "model": model,
            "selection": selection,
        }
        if not annotations:
            summary["noDetection"] += 1
        if args.apply and update_candidate(
            connection,
            row,
            annotations,
            model,
            f"补框复审预标:{selection};蓝框优先,无蓝框才用红框;人工确认后才进入正式框",
            now,
        ):
            summary["updated"] += 1

    if args.candidate_map_out:
        args.candidate_map_out.parent.mkdir(parents=True, exist_ok=True)
        args.candidate_map_out.write_text(
            json.dumps(candidate_map, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if args.apply and args.minimax_missing and connection is not None:
        missing = connection.execute(
            """
            SELECT * FROM items
            WHERE source_kind IN ('takeaway', 'history-takeaway', 'upload-takeaway')
              AND human_reviewed = 1
              AND decision = 'positive'
              AND annotations IN ('', '[]')
              AND ai_annotations IN ('', '[]')
            ORDER BY source_mtime, id
            """
        ).fetchall()
        for row in missing:
            summary["minimaxAttempted"] += 1
            try:
                image_path = materialize_item_image(row)
                annotations = request_minimax_annotations(image_path, "takeaway")
                if annotations:
                    summary["minimaxBoxed"] += 1
                if update_candidate(
                    connection,
                    row,
                    annotations,
                    "MiniMax-M3-box-rereview",
                    "补框复审预标:MiniMax完整外卖员候选;人工确认后才进入正式框",
                    utc_now(),
                ):
                    summary["updated"] += 1
            except Exception as exc:  # keep the row visible for manual drawing
                connection.execute(
                    "UPDATE items SET ai_attempted_at = ?, ai_error = ? WHERE id = ?",
                    (utc_now(), type(exc).__name__, row["id"]),
                )
                summary["minimaxErrors"].append({"id": row["id"], "error": type(exc).__name__})

    if args.apply and connection is not None:
        connection.commit()
    elif connection is not None:
        connection.rollback()
    if connection is not None:
        connection.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
