#!/usr/bin/env python3
"""Small dependency-free HTTP service for private image sample review."""

from __future__ import annotations

import base64
import csv
import cgi
import hashlib
import io
import json
import mimetypes
import os
import re
import sqlite3
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from PIL import Image, UnidentifiedImageError

try:
    from . import oss_backend
    from .reporting_manager import ReportingManager
    from .retention_policy import archive_item, ensure_retention_schema
except ImportError:
    import oss_backend
    from reporting_manager import ReportingManager
    from retention_policy import archive_item, ensure_retention_schema


ROOT = Path(os.environ.get("SAMPLE_REVIEW_ROOT", "/srv/ai-bot-sample-review"))
STATIC_ROOT = ROOT / "static"
DATA_ROOT = ROOT / "data"
IMAGE_ROOT = DATA_ROOT / "images"
CACHE_ROOT = DATA_ROOT / "cache"
DATABASE = DATA_ROOT / "review.sqlite3"
MANIFEST = DATA_ROOT / "manifest.json"
VALID_DECISIONS = {"pending", "positive", "negative"}
UPLOAD_GROUPS = {
    "takeaway": "手动上传_外卖",
    "workwear": "手动上传_工服",
    "door": "手动上传_小门",
    "other": "手动上传_其他",
}
MAX_UPLOAD_REQUEST = 100 * 1024 * 1024
MAX_UPLOAD_FILE = 15 * 1024 * 1024
MIN_FREE_BYTES = 8 * 1024 * 1024 * 1024
MAX_REVIEW_BYTES = 3 * 1024 * 1024 * 1024
MAX_ITEMS = 20_000
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "").strip()
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1").rstrip("/")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M3").strip()
MINIMAX_TIMEOUT_SECONDS = 90
MINIMAX_MAX_IMAGE_EDGE = 1280
MINIMAX_MAX_BOXES = 20
MINIMAX_SEMAPHORE = threading.BoundedSemaphore(1)
REPORTING_MANAGER: ReportingManager | None = None
REPORTING_MANAGER_LOCK = threading.Lock()

TARGET_DESCRIPTIONS = {
    "takeaway": (
        "delivery couriers wearing recognizable delivery-platform workwear. "
        "Uniform colors may be yellow, green, red-black, or other genuine courier colors. "
        "Do not include ordinary pedestrians, children, or vehicles."
    ),
    "workwear": (
        "New World security or cleaning staff wearing their work uniforms. "
        "Do not include delivery couriers, ordinary pedestrians, or maintenance workers."
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def get_reporting_manager() -> ReportingManager:
    global REPORTING_MANAGER
    with REPORTING_MANAGER_LOCK:
        if REPORTING_MANAGER is None:
            REPORTING_MANAGER = ReportingManager(ROOT)
            REPORTING_MANAGER.cleanup_stale_enable_files()
        return REPORTING_MANAGER


def validate_reporting_payload(action: str, payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("JSON object is required")
    if action == "prepare":
        algorithm = payload.get("algorithm")
        if algorithm not in {"takeaway", "workwear"}:
            raise ValueError("only takeaway and workwear are supported")
        return {"algorithm": str(algorithm)}
    if action in {"canary", "send"}:
        run_id = payload.get("runId")
        if not isinstance(run_id, str) or not run_id or len(run_id) > 100:
            raise ValueError("valid runId is required")
        result = {"runId": run_id}
        if action == "send":
            confirmation = payload.get("confirmation")
            if not isinstance(confirmation, str) or not confirmation:
                raise ValueError("confirmation phrase is required")
            result["confirmation"] = confirmation
        return result
    raise ValueError("unsupported reporting action")


def initialize_database() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                group_name TEXT NOT NULL,
                display_index INTEGER NOT NULL,
                filename TEXT NOT NULL,
                image_path TEXT NOT NULL,
                source_image TEXT NOT NULL DEFAULT '',
                split_name TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL DEFAULT '',
                decision TEXT NOT NULL DEFAULT 'pending',
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_retention_schema(connection)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
        migrations = {
            "ingest_key": "TEXT NOT NULL DEFAULT ''",
            "source_kind": "TEXT NOT NULL DEFAULT ''",
            "source_device": "TEXT NOT NULL DEFAULT ''",
            "source_mtime": "INTEGER NOT NULL DEFAULT 0",
            "file_size": "INTEGER NOT NULL DEFAULT 0",
            "annotations": "TEXT NOT NULL DEFAULT '[]'",
            "storage_backend": "TEXT NOT NULL DEFAULT 'local'",
            "object_key": "TEXT NOT NULL DEFAULT ''",
            "object_sha256": "TEXT NOT NULL DEFAULT ''",
            "migrated_at": "TEXT NOT NULL DEFAULT ''",
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_items (
                ingest_key TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                source_image TEXT NOT NULL DEFAULT '',
                deleted_at TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_items_ingest_key ON items(ingest_key)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_items_sha256 ON items(sha256)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_review_queue "
            "ON items(human_reviewed, ai_decision, source_kind, source_mtime)"
        )
        if connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
            records = json.loads(MANIFEST.read_text(encoding="utf-8"))
            now = utc_now()
            connection.executemany(
                """
                INSERT INTO items (
                    id, group_name, display_index, filename, image_path,
                    source_image, split_name, sha256, decision, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
                """,
                [
                    (
                        record["id"],
                        record["group"],
                        record["index"],
                        record["filename"],
                        record["image"],
                        record.get("source_image", ""),
                        record.get("split", ""),
                        record.get("sha256", ""),
                        record.get("decision", "pending"),
                        now,
                    )
                    for record in records
                ],
            )
        # Older MiniMax runs wrote their preliminary verdict into the final human
        # decision. Recover those rows before marking the remaining historic
        # positive/negative rows as manually reviewed.
        connection.execute(
            """
            UPDATE items
            SET ai_decision = decision,
                ai_notes = notes,
                ai_model = 'legacy-minimax',
                ai_confidence = CASE
                    WHEN notes GLOB '*置信度=[0-9]*' THEN
                        CAST(substr(notes, instr(notes, '置信度=') + 4, 4) AS REAL)
                    ELSE 0
                END,
                ai_annotations = annotations,
                ai_labeled_at = updated_at,
                ai_attempted_at = updated_at,
                ai_error = '',
                decision = 'pending',
                annotations = '[]',
                human_reviewed = 0,
                human_reviewed_at = ''
            WHERE notes LIKE 'AI复核:%'
              AND ai_decision = ''
              AND decision IN ('positive', 'negative')
            """
        )
        connection.execute(
            """
            UPDATE items
            SET human_reviewed = 1,
                human_reviewed_at = CASE
                    WHEN human_reviewed_at = '' THEN updated_at
                    ELSE human_reviewed_at
                END
            WHERE decision IN ('positive', 'negative')
              AND human_reviewed = 0
              AND notes NOT LIKE 'AI复核:%'
            """
        )
        for row in connection.execute("SELECT * FROM items WHERE decision = 'discard'").fetchall():
            image_path = (IMAGE_ROOT / row["image_path"]).resolve()
            image_root = IMAGE_ROOT.resolve()
            if image_root not in image_path.parents:
                continue
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                continue
            ingest_key = row["ingest_key"] or f"legacy:{row['id']}"
            connection.execute(
                """
                INSERT OR REPLACE INTO deleted_items (
                    ingest_key, item_id, sha256, source_image, deleted_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (ingest_key, row["id"], row["sha256"], row["source_image"], utc_now()),
            )
            archive_item(connection, row, "legacy-discard", retention_days=90)
            connection.execute("DELETE FROM items WHERE id = ?", (row["id"],))


def item_dict(row: sqlite3.Row) -> dict[str, object]:
    source_kind = row["source_kind"]
    group_name = row["group_name"]
    if source_kind in {"door", "upload-door"} or "小门" in group_name:
        algorithm = "door"
    elif source_kind in {"workwear", "upload-workwear"} or "工服" in group_name:
        algorithm = "workwear"
    else:
        algorithm = "takeaway"
    try:
        annotations = json.loads(row["annotations"] or "[]")
    except json.JSONDecodeError:
        annotations = []
    try:
        ai_annotations = json.loads(row["ai_annotations"] or "[]")
    except json.JSONDecodeError:
        ai_annotations = []
    return {
        "id": row["id"],
        "group": row["group_name"],
        "index": row["display_index"],
        "filename": row["filename"],
        "imageUrl": f"images/{row['image_path']}",
        "sourceImage": row["source_image"],
        "split": row["split_name"],
        "sha256": row["sha256"],
        "decision": row["decision"],
        "notes": row["notes"],
        "updatedAt": row["updated_at"],
        "sourceKind": row["source_kind"],
        "sourceDevice": row["source_device"],
        "sourceMtime": row["source_mtime"],
        "fileSize": row["file_size"],
        "algorithm": algorithm,
        "annotations": annotations,
        "aiDecision": row["ai_decision"],
        "aiNotes": row["ai_notes"],
        "aiModel": row["ai_model"],
        "aiConfidence": row["ai_confidence"],
        "aiAnnotations": ai_annotations,
        "aiLabeledAt": row["ai_labeled_at"],
        "humanReviewed": bool(row["human_reviewed"]),
        "humanReviewedAt": row["human_reviewed_at"],
    }


def confirm_ai_labels(
    connection: sqlite3.Connection, item_ids: list[str], now: str
) -> dict[str, int]:
    reviewed = 0
    positive = 0
    negative = 0
    for item_id in item_ids:
        row = connection.execute(
            """
            SELECT id, ai_decision FROM items
            WHERE id = ?
              AND human_reviewed = 0
              AND ai_decision IN ('positive', 'negative')
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            continue
        cursor = connection.execute(
            """
            UPDATE items
            SET decision = ai_decision,
                notes = CASE WHEN notes = '' THEN ai_notes ELSE notes END,
                annotations = CASE
                    WHEN ai_decision = 'positive'
                     AND annotations IN ('', '[]')
                     AND ai_annotations NOT IN ('', '[]')
                    THEN ai_annotations
                    ELSE annotations
                END,
                updated_at = ?,
                human_reviewed = 1,
                human_reviewed_at = ?
            WHERE id = ?
              AND human_reviewed = 0
              AND ai_decision IN ('positive', 'negative')
            """,
            (now, now, item_id),
        )
        if cursor.rowcount != 1:
            continue
        reviewed += 1
        if row["ai_decision"] == "positive":
            positive += 1
        else:
            negative += 1
    return {
        "reviewed": reviewed,
        "positive": positive,
        "negative": negative,
        "skipped": len(item_ids) - reviewed,
    }


def box_review_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return deduplicated takeaway/workwear positives needing a saved box."""
    return connection.execute(
        """
        WITH ranked AS (
            SELECT items.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(
                           NULLIF(object_sha256, ''),
                           NULLIF(sha256, ''),
                           id
                       )
                       ORDER BY source_mtime DESC, human_reviewed_at DESC, updated_at DESC, id
                   ) AS content_rank
            FROM items
            WHERE human_reviewed = 1
              AND decision = 'positive'
              AND source_kind IN (
                  'takeaway', 'history-takeaway', 'upload-takeaway',
                  'workwear', 'history-workwear', 'upload-workwear'
              )
        )
        SELECT * FROM ranked
        WHERE content_rank = 1
          AND annotations IN ('', '[]')
        ORDER BY source_mtime DESC, group_name, display_index, id
        """
    ).fetchall()


def upload_capacity(connection: sqlite3.Connection) -> tuple[bool, str]:
    if shutil.disk_usage(ROOT).free < MIN_FREE_BYTES:
        return False, "服务器剩余空间低于 8 GB，已暂停上传"
    if not oss_backend.configured():
        total = sum(path.stat().st_size for path in IMAGE_ROOT.rglob("*") if path.is_file())
        if total >= MAX_REVIEW_BYTES:
            return False, "审核图片已达到 3 GB 上限"
    if connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] >= MAX_ITEMS:
        return False, "审核条目已达到 20000 条上限"
    return True, "ok"


def normalize_uploaded_image(source, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.load()
        image = image.convert("RGB")
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        image.save(destination, format="JPEG", quality=87, optimize=True)


def materialize_item_image(row: sqlite3.Row) -> Path:
    local_path = (IMAGE_ROOT / row["image_path"]).resolve()
    image_root = IMAGE_ROOT.resolve()
    if local_path != image_root and image_root in local_path.parents and local_path.is_file():
        return local_path
    if row["storage_backend"] != "oss" or not row["object_key"]:
        raise FileNotFoundError(row["image_path"])
    return oss_backend.materialize(CACHE_ROOT, row["object_key"], row["object_sha256"])


def image_data_url(path: Path) -> str:
    if path.stat().st_size > MAX_UPLOAD_FILE:
        raise ValueError("image exceeds 15 MB")
    with Image.open(path) as image:
        image.load()
        image = image.convert("RGB")
        image.thumbnail(
            (MINIMAX_MAX_IMAGE_EDGE, MINIMAX_MAX_IMAGE_EDGE),
            Image.Resampling.LANCZOS,
        )
        stream = io.BytesIO()
        image.save(stream, format="JPEG", quality=84, optimize=True)
    encoded = base64.b64encode(stream.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def extract_response_text(payload: dict[str, object]) -> str:
    parts: list[str] = []
    output = payload.get("output", [])
    if not isinstance(output, list):
        return ""
    for message in output:
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if block.get("type") == "output_text" and isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def parse_minimax_boxes(text: str, label: str) -> list[dict[str, object]]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        raise ValueError("MiniMax did not return JSON")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError("MiniMax returned invalid JSON") from exc
    raw_boxes = payload.get("boxes", [])
    if not isinstance(raw_boxes, list):
        raise ValueError("MiniMax returned invalid boxes")

    boxes: list[dict[str, object]] = []
    for raw in raw_boxes[:MINIMAX_MAX_BOXES]:
        if not isinstance(raw, dict) or raw.get("complete") is not True:
            continue
        values = [raw.get(key) for key in ("x", "y", "w", "h")]
        if not all(isinstance(value, (int, float)) for value in values):
            continue
        x, y, width, height = (float(value) for value in values)
        confidence = raw.get("confidence", 0)
        if not isinstance(confidence, (int, float)) or float(confidence) < 0.5:
            continue
        if (
            x < 0 or y < 0 or width < 0.01 or height < 0.01
            or x + width > 1.000001 or y + height > 1.000001
        ):
            continue
        boxes.append(
            {
                "x": round(x, 6),
                "y": round(y, 6),
                "w": round(width, 6),
                "h": round(height, 6),
                "label": label,
                "confidence": round(min(1.0, max(0.0, float(confidence))), 4),
            }
        )
    return boxes


def request_minimax_annotations(image_path: Path, algorithm: str) -> list[dict[str, object]]:
    if not MINIMAX_API_KEY:
        raise RuntimeError("MiniMax API is not configured")
    target = TARGET_DESCRIPTIONS[algorithm]
    prompt = (
        "Inspect this image for dataset annotation. Return compact JSON only, exactly in this form: "
        '{"boxes":[{"x":0.1,"y":0.1,"w":0.2,"h":0.5,'
        '"label":"person","confidence":0.9,"complete":true}]}. '
        "Coordinates are normalized to 0..1. The target is: "
        f"{target} "
        "Draw one tight box around each complete visible target person from head to feet. "
        "Ignore colored rectangles already drawn on the image, vehicles, shadows, plants, pillars, "
        "umbrellas, severely cropped people, edge-only people, and partial bodies. "
        'If no complete target person is present, return {"boxes":[]}.'
    )
    request_body = json.dumps(
        {
            "model": MINIMAX_MODEL,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_data_url(image_path)},
                    ],
                }
            ],
            "max_output_tokens": 600,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        f"{MINIMAX_BASE_URL}/responses",
        data=request_body,
        headers={
            "Authorization": f"Bearer {MINIMAX_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=MINIMAX_TIMEOUT_SECONDS) as response:
            response_payload = json.loads(response.read())
    except HTTPError as exc:
        raise RuntimeError(f"MiniMax HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("MiniMax connection failed") from exc
    except TimeoutError as exc:
        raise RuntimeError("MiniMax request timed out") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("MiniMax returned invalid response") from exc
    return parse_minimax_boxes(extract_response_text(response_payload), algorithm)


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "SampleReview/1.0"

    def log_message(self, message: str, *args: object) -> None:
        print(f"{self.address_string()} - {message % args}", flush=True)

    def send_bytes(
        self, body: bytes, content_type: str, status: int = 200, cache_control: str | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Cache-Control",
            cache_control or ("no-store" if "json" in content_type else "private, max-age=3600"),
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: object, status: int = 200) -> None:
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def read_json_body(self, max_bytes: int = 16_384) -> object:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length <= 0 or length > max_bytes:
            raise ValueError("invalid request size")
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc

    def send_file(self, path: Path, cache_control: str | None = None) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_bytes(path.read_bytes(), content_type, cache_control=cache_control)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in {"/", "/index.html"}:
            self.send_file(STATIC_ROOT / "index.html", "no-store")
            return
        if path == "/healthz":
            with connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                deleted = connection.execute("SELECT COUNT(*) FROM deleted_items").fetchone()[0]
                review_queue = connection.execute(
                    """
                    SELECT COUNT(*) FROM items
                    WHERE human_reviewed = 0
                      AND ai_decision IN ('positive', 'negative')
                    """
                ).fetchone()[0]
                awaiting_ai = connection.execute(
                    """
                    SELECT COUNT(*) FROM items
                    WHERE human_reviewed = 0
                      AND ai_decision = ''
                      AND source_kind IN ('workwear', 'takeaway', 'door')
                    """
                ).fetchone()[0]
                confirmed_positive = connection.execute(
                    "SELECT COUNT(*) FROM items WHERE human_reviewed = 1 AND decision = 'positive'"
                ).fetchone()[0]
                confirmed_negative = connection.execute(
                    "SELECT COUNT(*) FROM items WHERE human_reviewed = 1 AND decision = 'negative'"
                ).fetchone()[0]
                box_review_queue = len(box_review_rows(connection))
            self.send_json(
                {
                    "status": "ok",
                    "items": count,
                    "deleted": deleted,
                    "reviewQueue": review_queue,
                    "awaitingAi": awaiting_ai,
                    "confirmed": confirmed_positive + confirmed_negative,
                    "confirmedPositive": confirmed_positive,
                    "confirmedNegative": confirmed_negative,
                    "boxReviewQueue": box_review_queue,
                }
            )
            return
        if path == "/api/reporting/status":
            run_id = parse_qs(parsed.query).get("runId", [""])[0]
            try:
                self.send_json(get_reporting_manager().status(run_id))
            except ValueError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except FileNotFoundError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (OSError, RuntimeError, json.JSONDecodeError) as exc:
                self.log_error("reporting status failed: %s", exc)
                self.send_json({"error": "上报状态读取失败"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception as exc:
                self.log_error("unexpected reporting status failure: %s", exc)
                self.send_json({"error": "上报状态读取失败"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if path == "/api/items":
            with connect() as connection:
                rows = connection.execute(
                    """
                    WITH ranked AS (
                        SELECT items.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY COALESCE(
                                       NULLIF(object_sha256, ''),
                                       NULLIF(sha256, ''),
                                       id
                                   )
                                   ORDER BY source_mtime DESC, updated_at DESC, id
                               ) AS content_rank
                        FROM items
                        WHERE human_reviewed = 0
                          AND ai_decision IN ('positive', 'negative')
                    )
                    SELECT * FROM ranked
                    WHERE content_rank = 1
                    ORDER BY source_mtime DESC, group_name, display_index, id
                    """
                ).fetchall()
            self.send_json([item_dict(row) for row in rows])
            return
        if path == "/api/box-review-items":
            with connect() as connection:
                rows = box_review_rows(connection)
            self.send_json([item_dict(row) for row in rows])
            return
        if path == "/api/export.csv":
            with connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM items ORDER BY group_name, display_index, id"
                ).fetchall()
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(
                [
                    "group", "index", "filename", "decision", "annotations",
                    "notes", "source_image", "sha256", "updated_at",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["group_name"],
                        row["display_index"],
                        row["filename"],
                        row["decision"],
                        row["annotations"],
                        row["notes"],
                        row["source_image"],
                        row["sha256"],
                        row["updated_at"],
                    ]
                )
            body = ("\ufeff" + stream.getvalue()).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="sample-review.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/images/"):
            relative = Path(path.removeprefix("/images/"))
            if relative.is_absolute() or ".." in relative.parts:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            with connect() as connection:
                row = connection.execute(
                    "SELECT * FROM items WHERE image_path = ? LIMIT 1", (relative.as_posix(),)
                ).fetchone()
            if row is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                image_path = materialize_item_image(row)
            except (OSError, RuntimeError):
                self.send_error(HTTPStatus.BAD_GATEWAY)
                return
            self.send_file(image_path, "private, max-age=3600")
            return
        if path.startswith("/static/"):
            relative = Path(path.removeprefix("/static/"))
            if relative.is_absolute() or ".." in relative.parts:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            self.send_file(STATIC_ROOT / relative, "no-store")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/items/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        item_id = unquote(parsed.path.removeprefix("/api/items/"))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            decision = payload.get("decision")
            notes = payload.get("notes")
            annotations = payload.get("annotations")
            if decision is not None and decision not in VALID_DECISIONS:
                raise ValueError("invalid decision")
            if decision is None and notes is None and annotations is None:
                raise ValueError("no changes supplied")
            if notes is not None and (not isinstance(notes, str) or len(notes) > 1000):
                raise ValueError("invalid notes")
            if annotations is not None:
                if not isinstance(annotations, list) or len(annotations) > 100:
                    raise ValueError("invalid annotations")
                normalized = []
                for box in annotations:
                    if not isinstance(box, dict):
                        raise ValueError("invalid annotation")
                    values = [box.get(key) for key in ("x", "y", "w", "h")]
                    if not all(isinstance(value, (int, float)) for value in values):
                        raise ValueError("invalid annotation coordinates")
                    x, y, width, height = (float(value) for value in values)
                    if (
                        x < 0 or y < 0 or width <= 0 or height <= 0
                        or x + width > 1.000001 or y + height > 1.000001
                    ):
                        raise ValueError("annotation outside image")
                    label = box.get("label", "")
                    if not isinstance(label, str) or len(label) > 80:
                        raise ValueError("invalid annotation label")
                    normalized.append(
                        {
                            "x": round(x, 6), "y": round(y, 6),
                            "w": round(width, 6), "h": round(height, 6),
                            "label": label,
                        }
                    )
                annotations = normalized
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        now = utc_now()
        with connect() as connection:
            current = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if current is None:
                self.send_json({"error": "item not found"}, HTTPStatus.NOT_FOUND)
                return
            cursor = connection.execute(
                """
                UPDATE items
                SET decision = ?, notes = ?, annotations = ?, updated_at = ?,
                    human_reviewed = ?, human_reviewed_at = ?
                WHERE id = ?
                """,
                (
                    decision if decision is not None else current["decision"],
                    notes if notes is not None else current["notes"],
                    (
                        json.dumps(annotations, ensure_ascii=False, separators=(",", ":"))
                        if annotations is not None
                        else (
                            current["ai_annotations"]
                            if decision == "positive"
                            and current["annotations"] in {"", "[]"}
                            and current["ai_annotations"] not in {"", "[]"}
                            else current["annotations"]
                        )
                    ),
                    now,
                    0 if decision == "pending" else (
                        1 if decision in {"positive", "negative"} else current["human_reviewed"]
                    ),
                    "" if decision == "pending" else (
                        now if decision in {"positive", "negative"} else current["human_reviewed_at"]
                    ),
                    item_id,
                ),
            )
            if cursor.rowcount != 1:
                self.send_json({"error": "item not found"}, HTTPStatus.NOT_FOUND)
                return
            row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        self.send_json(item_dict(row))

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/items/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        item_id = unquote(parsed.path.removeprefix("/api/items/"))
        with connect() as connection:
            row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                self.send_json({"error": "item not found"}, HTTPStatus.NOT_FOUND)
                return
            image_path = (IMAGE_ROOT / row["image_path"]).resolve()
            image_root = IMAGE_ROOT.resolve()
            if image_root not in image_path.parents:
                self.send_json({"error": "invalid image path"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                image_path.unlink(missing_ok=True)
            except OSError as exc:
                self.send_json({"error": f"failed to delete image: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            if row["object_key"]:
                oss_backend.cache_path(CACHE_ROOT, row["object_key"]).unlink(missing_ok=True)
            ingest_key = row["ingest_key"] or f"legacy:{row['id']}"
            connection.execute(
                """
                INSERT OR REPLACE INTO deleted_items (
                    ingest_key, item_id, sha256, source_image, deleted_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (ingest_key, row["id"], row["sha256"], row["source_image"], utc_now()),
            )
            archive_item(connection, row, "operator-delete", retention_days=90)
            connection.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self.send_json({"deleted": True, "id": item_id})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        reporting_match = re.fullmatch(r"/api/reporting/(prepare|canary|send)", parsed.path)
        if reporting_match:
            action = reporting_match.group(1)
            try:
                payload = validate_reporting_payload(action, self.read_json_body())
                manager = get_reporting_manager()
                if action == "prepare":
                    result = manager.prepare(payload["algorithm"])
                    status = HTTPStatus.CREATED
                elif action == "canary":
                    result = manager.canary(payload["runId"])
                    status = HTTPStatus.OK
                else:
                    result = manager.start_send(
                        payload["runId"], payload["confirmation"]
                    )
                    status = HTTPStatus.ACCEPTED
                self.send_json(result, status)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except FileNotFoundError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except (OSError, json.JSONDecodeError) as exc:
                self.log_error("reporting %s failed: %s", action, exc)
                self.send_json({"error": "上报操作失败"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception as exc:
                self.log_error("unexpected reporting %s failure: %s", action, exc)
                self.send_json({"error": "上报操作失败"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if parsed.path == "/api/review-all":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 512_000:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length))
                item_ids = payload.get("ids")
                if not isinstance(item_ids, list) or not item_ids or len(item_ids) > 5_000:
                    raise ValueError("invalid item ids")
                if not all(
                    isinstance(item_id, str) and 0 < len(item_id) <= 128
                    for item_id in item_ids
                ):
                    raise ValueError("invalid item id")
                item_ids = list(dict.fromkeys(item_ids))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            now = utc_now()
            with connect() as connection:
                result = confirm_ai_labels(connection, item_ids, now)
            self.send_json(result)
            return
        ai_match = re.fullmatch(r"/api/items/([^/]+)/ai-annotations", parsed.path)
        if ai_match:
            item_id = unquote(ai_match.group(1))
            with connect() as connection:
                row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                self.send_json({"error": "item not found"}, HTTPStatus.NOT_FOUND)
                return
            item = item_dict(row)
            if item["algorithm"] not in TARGET_DESCRIPTIONS:
                self.send_json({"error": "小门识别不使用人物 AI 预标注"}, HTTPStatus.BAD_REQUEST)
                return
            if item["annotations"]:
                self.send_json({"error": "当前图片已有标注框"}, HTTPStatus.CONFLICT)
                return
            try:
                image_path = materialize_item_image(row)
            except (OSError, RuntimeError):
                self.send_json({"error": "image is unavailable"}, HTTPStatus.BAD_GATEWAY)
                return
            if not MINIMAX_API_KEY:
                self.send_json({"error": "MiniMax API 尚未配置"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if not MINIMAX_SEMAPHORE.acquire(blocking=False):
                self.send_json({"error": "AI 标注正忙，请稍后重试"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            try:
                annotations = request_minimax_annotations(image_path, str(item["algorithm"]))
            except (RuntimeError, ValueError, OSError, UnidentifiedImageError) as exc:
                self.log_error("AI annotation failed for %s: %s", item_id, exc)
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
                return
            finally:
                MINIMAX_SEMAPHORE.release()
            self.send_json(
                {
                    "annotations": annotations,
                    "provider": "minimax",
                    "model": MINIMAX_MODEL,
                    "saved": False,
                }
            )
            return
        if parsed.path != "/api/upload":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        content_type = self.headers.get("Content-Type", "")
        if length <= 0 or length > MAX_UPLOAD_REQUEST or not content_type.startswith("multipart/form-data"):
            self.send_json({"error": "上传请求无效或超过 100 MB"}, HTTPStatus.BAD_REQUEST)
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(length)},
            keep_blank_values=True,
        )
        category = form.getfirst("category", "other")
        if category not in UPLOAD_GROUPS:
            self.send_json({"error": "上传分类无效"}, HTTPStatus.BAD_REQUEST)
            return
        files = form["files"] if "files" in form else []
        if not isinstance(files, list):
            files = [files]
        files = [item for item in files if getattr(item, "filename", "") and getattr(item, "file", None)]
        if not files or len(files) > 50:
            self.send_json({"error": "请选择 1-50 张图片"}, HTTPStatus.BAD_REQUEST)
            return

        added: list[dict[str, object]] = []
        skipped: list[dict[str, str]] = []
        group = UPLOAD_GROUPS[category]
        with connect() as connection:
            allowed, reason = upload_capacity(connection)
            if not allowed:
                self.send_json({"error": reason}, HTTPStatus.INSUFFICIENT_STORAGE)
                return
            known_hashes = {row[0] for row in connection.execute("SELECT sha256 FROM items WHERE sha256 != ''")}
            deleted_hashes = {row[0] for row in connection.execute("SELECT sha256 FROM deleted_items WHERE sha256 != ''")}
            next_index = connection.execute(
                "SELECT COALESCE(MAX(display_index), 0) + 1 FROM items WHERE group_name = ?", (group,)
            ).fetchone()[0]
            for field in files:
                original_name = Path(field.filename).name[:180]
                with tempfile.NamedTemporaryFile(dir=DATA_ROOT, suffix=".upload", delete=False) as handle:
                    temporary = Path(handle.name)
                    size = 0
                    digest = hashlib.sha256()
                    while True:
                        chunk = field.file.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD_FILE:
                            break
                        digest.update(chunk)
                        handle.write(chunk)
                try:
                    if size > MAX_UPLOAD_FILE:
                        skipped.append({"filename": original_name, "reason": "超过 15 MB"})
                        continue
                    sha256 = digest.hexdigest()
                    if sha256 in known_hashes:
                        skipped.append({"filename": original_name, "reason": "图片重复"})
                        continue
                    if sha256 in deleted_hashes:
                        skipped.append({"filename": original_name, "reason": "图片已淘汰"})
                        continue
                    allowed, reason = upload_capacity(connection)
                    if not allowed:
                        skipped.append({"filename": original_name, "reason": reason})
                        break
                    item_id = hashlib.sha1(f"upload|{sha256}".encode("utf-8")).hexdigest()[:20]
                    relative = Path("uploads") / category / f"{item_id}.jpg"
                    destination = IMAGE_ROOT / relative
                    normalize_uploaded_image(temporary, destination)
                    file_size = destination.stat().st_size
                    object_sha256 = ""
                    object_key = ""
                    storage_backend = "local"
                    migrated_at = ""
                    if oss_backend.configured():
                        object_sha256 = oss_backend.sha256_file(destination)
                        object_key = oss_backend.object_key_for_sha256(object_sha256, destination.suffix)
                        oss_backend.upload(destination, object_key)
                        storage_backend = "oss"
                        migrated_at = utc_now()
                    now = utc_now()
                    connection.execute(
                        """
                        INSERT INTO items (
                            id, group_name, display_index, filename, image_path, source_image,
                            split_name, sha256, decision, notes, updated_at, ingest_key,
                            source_kind, source_device, source_mtime, file_size,
                            storage_backend, object_key, object_sha256, migrated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, '', ?, 'pending', '', ?, ?, ?, 'manual', 0, ?, ?, ?, ?, ?)
                        """,
                        (
                            item_id, group, next_index, original_name, relative.as_posix(),
                            f"manual-upload:{original_name}", sha256, now, f"upload|{sha256}",
                            f"upload-{category}", file_size, storage_backend, object_key,
                            object_sha256, migrated_at,
                        ),
                    )
                    known_hashes.add(sha256)
                    next_index += 1
                    row = connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
                    added.append(item_dict(row))
                except (OSError, RuntimeError, UnidentifiedImageError):
                    skipped.append({"filename": original_name, "reason": "不是有效图片"})
                finally:
                    temporary.unlink(missing_ok=True)
        self.send_json({"added": added, "skipped": skipped}, HTTPStatus.CREATED)


def main() -> None:
    initialize_database()
    get_reporting_manager()
    host = os.environ.get("SAMPLE_REVIEW_HOST", "127.0.0.1")
    port = int(os.environ.get("SAMPLE_REVIEW_PORT", "8792"))
    server = ThreadingHTTPServer((host, port), ReviewHandler)
    print(f"sample review listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
