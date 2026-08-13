#!/usr/bin/env python3
"""Immutable human-truth revisions and publication snapshots for review UI."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from PIL import Image, UnidentifiedImageError

try:
    from . import visual_registry
except ImportError:
    import visual_registry  # type: ignore


ImageResolver = Callable[[sqlite3.Row], Path]
MAX_FACT_BYTES = 256 * 1024
SNAPSHOT_LEASE_SECONDS = 15 * 60
ACK_GRACE_SECONDS = 60 * 60


class IdempotencyConflict(RuntimeError):
    pass


class RevisionConflict(RuntimeError):
    pass


class SnapshotConflict(RuntimeError):
    pass


class NoPublishableChanges(RuntimeError):
    pass


class FactQuarantined(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(payload: str) -> str:
    return _sha256_bytes(payload.encode("utf-8"))


def _parse_json_array(raw: str, field: str) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise FactQuarantined(f"invalid_{field}", f"{field} is not valid JSON") from exc
    if not isinstance(value, list):
        raise FactQuarantined(f"invalid_{field}", f"{field} must be an array")
    return value


def _default_image_resolver(row: sqlite3.Row) -> Path:
    root = Path(os.environ.get("SAMPLE_REVIEW_ROOT", "/srv/ai-bot-sample-review"))
    image_root = (root / "data" / "images").resolve()
    candidate = (image_root / str(row["image_path"])).resolve()
    if candidate == image_root or image_root not in candidate.parents or not candidate.is_file():
        raise FileNotFoundError("review image is unavailable")
    return candidate


def _capture_group_id(row: sqlite3.Row, algorithm_key: str) -> str:
    source_event = str(row["source_image"] or "").strip()
    if source_event:
        identity = f"event\0{source_event}"
        return f"event:{_sha256_text(identity)[:40]}"
    ingest_key = str(row["ingest_key"] or "").strip()
    if ingest_key:
        identity = f"ingest\0{ingest_key}"
        return f"ingest:{_sha256_text(identity)[:40]}"
    identity = f"singleton\0{algorithm_key}\0{row['id']}"
    return f"singleton:{_sha256_text(identity)[:40]}"


def _captured_at(row: sqlite3.Row) -> str:
    source_mtime = int(row["source_mtime"] or 0)
    if source_mtime > 0:
        return datetime.fromtimestamp(source_mtime, timezone.utc).isoformat(timespec="seconds")
    fallback = str(row["updated_at"] or "").strip()
    if not fallback:
        raise FactQuarantined("missing_capture_time", "no truthful capture or ingest time")
    return fallback


def _image_metadata(
    row: sqlite3.Row,
    algorithm_key: str,
    revision: int,
    resolver: ImageResolver,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        path = Path(resolver(row)).resolve()
        payload = path.read_bytes()
        if not payload:
            raise FactQuarantined("empty_image", "image is empty")
        digest = _sha256_bytes(payload)
        with Image.open(path) as image:
            image.load()
            orientation = int(image.getexif().get(274, 1) or 1)
            if orientation != 1:
                raise FactQuarantined(
                    "image_orientation_not_normalized",
                    "image EXIF orientation must be normalized before publication",
                )
            width, height = image.size
            image_format = str(image.format or "").upper()
    except FactQuarantined:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise FactQuarantined("image_unavailable", "image cannot be resolved and decoded") from exc
    mime_by_format = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    mime = mime_by_format.get(image_format)
    if mime is None:
        raise FactQuarantined("unsupported_image_type", f"unsupported image type {image_format}")
    if not (1 <= width <= 65535 and 1 <= height <= 65535):
        raise FactQuarantined("invalid_image_dimensions", "image dimensions are out of range")
    recorded_object_digest = str(row["object_sha256"] or "").strip()
    if recorded_object_digest and recorded_object_digest != digest:
        raise FactQuarantined("image_digest_mismatch", "resolved image digest changed")
    asset_ref = f"asset:{digest[:48]}"
    image_fact = {
        "asset_ref": asset_ref,
        "sha256": digest,
        "width": int(width),
        "height": int(height),
        "coordinate_space": "orientation_normalized_original",
    }
    resolver_metadata = {
        "resolver_policy_revision": "review-ledger.v1",
        "item_locator": {
            "algorithm_key": algorithm_key,
            "item_id": str(row["id"]),
            "review_revision": revision,
        },
        "asset_ref": asset_ref,
        "expected_mime": mime,
        "expected_length": len(payload),
        "expected_width": int(width),
        "expected_height": int(height),
        "expected_sha256": digest,
    }
    return image_fact, resolver_metadata


def _normalize_boxes(
    annotations: list[Mapping[str, Any]], entry: visual_registry.AlgorithmEntry
) -> list[dict[str, Any]]:
    if len(annotations) > entry.bbox_max_count:
        raise FactQuarantined("too_many_boxes", "annotation count exceeds review policy")
    result: list[dict[str, Any]] = []
    for raw in annotations:
        if not isinstance(raw, Mapping):
            raise FactQuarantined("invalid_box", "annotation must be an object")
        values = [raw.get(key) for key in ("x", "y", "w", "h")]
        if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values):
            raise FactQuarantined("invalid_box", "annotation coordinates must be finite numbers")
        x, y, width, height = (round(float(value), 6) for value in values)
        if (
            x < 0
            or y < 0
            or width <= 0
            or height <= 0
            or x + width > 1.000001
            or y + height > 1.000001
            or width * height < entry.bbox_min_area
        ):
            raise FactQuarantined("invalid_box_geometry", "annotation violates review geometry policy")
        try:
            label_key = entry.normalize_label(str(raw.get("label") or raw.get("label_key") or ""))
        except visual_registry.RegistryValidationError as exc:
            raise FactQuarantined("unknown_taxonomy_label", str(exc)) from exc
        result.append({"x": x, "y": y, "w": width, "h": height, "label_key": label_key})
    return result


def _normalize_tags(tags: list[Any], entry: visual_registry.AlgorithmEntry) -> list[str]:
    normalized = sorted({str(tag).strip() for tag in tags if str(tag).strip()})
    unknown = [tag for tag in normalized if tag not in entry.tag_keys]
    if unknown:
        raise FactQuarantined("unknown_taxonomy_tag", f"unknown taxonomy tags: {unknown}")
    return normalized


def _build_fact(
    row: sqlite3.Row,
    *,
    algorithm_key: str,
    entry: visual_registry.AlgorithmEntry,
    revision: int,
    decision: str,
    annotations: list[Mapping[str, Any]],
    tags: list[Any],
    reviewed_at: str,
    resolver: ImageResolver,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not entry.accepted:
        raise FactQuarantined("algorithm_not_onboarded", "algorithm is catalog-only")
    boxes = _normalize_boxes(annotations, entry)
    tag_keys = _normalize_tags(tags, entry)
    if decision not in {"positive", "negative", "unusable"}:
        raise FactQuarantined("invalid_human_decision", "decision is not a final human truth")
    if entry.task_type == "object_detection":
        if decision == "positive" and not boxes:
            raise FactQuarantined("positive_without_box", "positive detection truth requires a box")
        if decision != "positive" and boxes:
            raise FactQuarantined("non_positive_with_boxes", "negative/unusable truth cannot contain boxes")
    image, resolver_metadata = _image_metadata(row, algorithm_key, revision, resolver)
    label_keys = sorted({box["label_key"] for box in boxes})
    capture_group_id = _capture_group_id(row, algorithm_key)
    trainable = decision in {"positive", "negative"}
    fact: dict[str, Any] = {
        "schema_version": "ai-bot-review-fact.v1",
        "item_id": str(row["id"]),
        "review_revision": revision,
        "algorithm_key": algorithm_key,
        "task_type": entry.task_type,
        "visual_semantics_version_ref": entry.visual_semantics_version_ref,
        "visual_semantics_content_sha256": entry.visual_semantics_content_sha256,
        "task_profile_ref": entry.task_profile_ref,
        "task_profile_content_sha256": entry.task_profile_content_sha256,
        "taxonomy_version_ref": entry.taxonomy_version_ref,
        "taxonomy_content_sha256": entry.taxonomy_content_sha256,
        "captured_at": _captured_at(row),
        "source": {
            "capture_group_id": capture_group_id,
            "source_kind": str(row["source_kind"] or "")[:64],
        },
        "image": image,
        "primary_observation_status": "absent_legacy",
        "secondary_observations": [],
        "human_truth": {
            "decision": decision,
            "boxes": boxes,
            "label_keys": label_keys,
            "tag_keys": tag_keys,
            "reviewed_at": reviewed_at,
        },
        "observation_comparisons": [],
        "correction": {"types": ["unavailable"], "reason_codes": []},
        "eligibility": {
            "trainable": trainable,
            "exclusion_reasons": [] if trainable else ["human_unusable"],
            "regression_roles": [],
        },
        "updated_at": reviewed_at,
    }
    validate_review_fact(fact, entry)
    return fact, resolver_metadata


def validate_review_fact(fact: Mapping[str, Any], entry: visual_registry.AlgorithmEntry) -> None:
    required = {
        "schema_version", "item_id", "review_revision", "algorithm_key", "task_type",
        "visual_semantics_version_ref", "visual_semantics_content_sha256", "task_profile_ref",
        "task_profile_content_sha256", "taxonomy_version_ref", "taxonomy_content_sha256",
        "captured_at", "source", "image", "primary_observation_status",
        "secondary_observations", "human_truth", "observation_comparisons", "correction",
        "eligibility", "updated_at",
    }
    if set(fact) != required:
        raise FactQuarantined("review_fact_shape", "review fact fields do not match v1")
    if fact["schema_version"] != "ai-bot-review-fact.v1" or fact["algorithm_key"] != entry.algorithm_key:
        raise FactQuarantined("review_fact_identity", "review fact identity mismatch")
    if int(fact["review_revision"]) < 1:
        raise FactQuarantined("review_fact_revision", "published revision must be positive")
    bindings = (
        ("visual_semantics_version_ref", entry.visual_semantics_version_ref),
        ("visual_semantics_content_sha256", entry.visual_semantics_content_sha256),
        ("task_profile_ref", entry.task_profile_ref),
        ("task_profile_content_sha256", entry.task_profile_content_sha256),
        ("taxonomy_version_ref", entry.taxonomy_version_ref),
        ("taxonomy_content_sha256", entry.taxonomy_content_sha256),
    )
    if any(fact[key] != expected for key, expected in bindings):
        raise FactQuarantined("semantic_binding_mismatch", "review fact semantic binding drifted")
    if fact["primary_observation_status"] == "absent_legacy":
        if "ai_original" in fact or fact["observation_comparisons"]:
            raise FactQuarantined("legacy_primary_fabricated", "absent primary cannot have comparison")
        if fact["correction"].get("types") != ["unavailable"]:
            raise FactQuarantined("legacy_correction_invalid", "absent primary must be unavailable")
    if fact["secondary_observations"]:
        raise FactQuarantined("secondary_not_enabled", "secondary observations are disabled in accepted v1 profiles")
    human = fact["human_truth"]
    boxes = human.get("boxes")
    labels = human.get("label_keys")
    if not isinstance(boxes, list) or not isinstance(labels, list):
        raise FactQuarantined("human_truth_shape", "human truth boxes/labels must be arrays")
    if sorted(set(box["label_key"] for box in boxes)) != labels:
        raise FactQuarantined("label_box_mismatch", "label keys must equal human box labels")
    canonical = _canonical_json(fact)
    if len(canonical.encode("utf-8")) > MAX_FACT_BYTES:
        raise FactQuarantined("review_fact_too_large", "review fact exceeds byte budget")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS review_fact_revisions (
  item_id TEXT NOT NULL, algorithm_key TEXT NOT NULL, review_revision INTEGER NOT NULL,
  canonical_fact_json TEXT NOT NULL, fact_digest TEXT NOT NULL, image_sha256 TEXT NOT NULL,
  resolver_metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(algorithm_key,item_id,review_revision));
CREATE TABLE IF NOT EXISTS review_publication_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, algorithm_key TEXT NOT NULL,
  review_revision INTEGER NOT NULL, change_type TEXT NOT NULL, created_at TEXT NOT NULL,
  published_at TEXT NOT NULL DEFAULT '', UNIQUE(algorithm_key,item_id,review_revision));
CREATE TABLE IF NOT EXISTS review_publication_snapshots (
  snapshot_id TEXT PRIMARY KEY, algorithm_key TEXT NOT NULL, snapshot_watermark INTEGER NOT NULL,
  ordered_membership_digest TEXT NOT NULL, semantics_policy_digest TEXT NOT NULL,
  status TEXT NOT NULL, source_version_id TEXT NOT NULL DEFAULT '', source_content_digest TEXT NOT NULL DEFAULT '',
  lease_owner TEXT NOT NULL DEFAULT '', lease_expires_at TEXT NOT NULL DEFAULT '',
  commit_idempotency_key TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  committed_at TEXT NOT NULL DEFAULT '', acknowledged_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS review_publication_snapshot_items (
  snapshot_id TEXT NOT NULL, ordinal INTEGER NOT NULL, algorithm_key TEXT NOT NULL,
  item_id TEXT NOT NULL, review_revision INTEGER NOT NULL, fact_digest TEXT NOT NULL,
  canonical_fact_json TEXT NOT NULL, PRIMARY KEY(snapshot_id,ordinal),
  UNIQUE(snapshot_id,algorithm_key,item_id));
CREATE TABLE IF NOT EXISTS review_publication_snapshot_outbox_members (
  snapshot_id TEXT NOT NULL, outbox_id INTEGER NOT NULL, algorithm_key TEXT NOT NULL,
  item_id TEXT NOT NULL, review_revision INTEGER NOT NULL, represented_by_item_id TEXT NOT NULL,
  represented_by_review_revision INTEGER NOT NULL,
  PRIMARY KEY(snapshot_id,outbox_id));
CREATE TABLE IF NOT EXISTS review_command_receipts (
  idempotency_key TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL, item_id TEXT NOT NULL,
  result_revision INTEGER NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS regression_selections (
  selection_id TEXT PRIMARY KEY, algorithm_key TEXT NOT NULL, selection_revision INTEGER NOT NULL,
  canonical_selection_json TEXT NOT NULL, selection_digest TEXT NOT NULL, status TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE, request_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_fact_quarantine (
  algorithm_key TEXT NOT NULL, item_id TEXT NOT NULL, attempted_revision INTEGER NOT NULL,
  reason_code TEXT NOT NULL, detail_digest TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(algorithm_key,item_id,attempted_revision));
CREATE TRIGGER IF NOT EXISTS review_fact_revisions_no_update BEFORE UPDATE ON review_fact_revisions
BEGIN SELECT RAISE(ABORT,'review fact revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS review_fact_revisions_no_delete BEFORE DELETE ON review_fact_revisions
BEGIN SELECT RAISE(ABORT,'review fact revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS regression_selections_no_update BEFORE UPDATE ON regression_selections
BEGIN SELECT RAISE(ABORT,'regression selection is immutable'); END;
CREATE TRIGGER IF NOT EXISTS regression_selections_no_delete BEFORE DELETE ON regression_selections
BEGIN SELECT RAISE(ABORT,'regression selection is immutable'); END;
"""


ITEM_MIGRATIONS = {
    "review_revision": "INTEGER NOT NULL DEFAULT 0",
    "capture_group_id": "TEXT NOT NULL DEFAULT ''",
    "human_label_keys_json": "TEXT NOT NULL DEFAULT '[]'",
    "human_tag_keys_json": "TEXT NOT NULL DEFAULT '[]'",
}


def _validate_immutable_guards(connection: sqlite3.Connection) -> None:
    expected = {
        "review_fact_revisions_no_update": ("review_fact_revisions", "BEFORE UPDATE"),
        "review_fact_revisions_no_delete": ("review_fact_revisions", "BEFORE DELETE"),
        "regression_selections_no_update": ("regression_selections", "BEFORE UPDATE"),
        "regression_selections_no_delete": ("regression_selections", "BEFORE DELETE"),
    }
    rows = {
        row[0]: (row[1], str(row[2] or "").upper())
        for row in connection.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"
        )
    }
    for name, (table, marker) in expected.items():
        actual = rows.get(name)
        if actual is None or actual[0] != table or marker not in actual[1] or "IMMUTABLE" not in actual[1]:
            raise RuntimeError(f"immutable trigger missing or drifted: {name}")


def migrate(connection: sqlite3.Connection, *, image_resolver: ImageResolver | None = None) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
    for column, definition in ITEM_MIGRATIONS.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE items ADD COLUMN {column} {definition}")
    connection.executescript(SCHEMA_SQL)
    snapshot_member_columns = {
        row[1] for row in connection.execute(
            "PRAGMA table_info(review_publication_snapshot_outbox_members)"
        )
    }
    if "represented_by_item_id" not in snapshot_member_columns:
        connection.execute(
            "ALTER TABLE review_publication_snapshot_outbox_members "
            "ADD COLUMN represented_by_item_id TEXT NOT NULL DEFAULT ''"
        )
    _validate_immutable_guards(connection)
    _backfill_legacy_rows(connection, image_resolver=image_resolver or _default_image_resolver)


def _quarantine(
    connection: sqlite3.Connection,
    algorithm_key: str,
    item_id: str,
    revision: int,
    exc: FactQuarantined,
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO review_fact_quarantine VALUES (?,?,?,?,?,?)",
        (
            algorithm_key,
            item_id,
            revision,
            exc.reason_code,
            _sha256_text(str(exc)),
            utc_now_iso(),
        ),
    )


def _backfill_legacy_rows(connection: sqlite3.Connection, *, image_resolver: ImageResolver) -> None:
    registry = visual_registry.accepted_algorithms()
    rows = connection.execute(
        "SELECT * FROM items WHERE human_reviewed=1 AND review_revision=0 AND decision IN ('positive','negative')"
    ).fetchall()
    for row in rows:
        algorithm_key = visual_registry.legacy_algorithm_for(row)
        entry = registry.get(algorithm_key)
        if entry is None:
            _quarantine(
                connection,
                algorithm_key or "unmapped",
                str(row["id"]),
                1,
                FactQuarantined("unmapped_algorithm", "legacy row does not map to an accepted algorithm"),
            )
            continue
        try:
            annotations = _parse_json_array(str(row["annotations"] or "[]"), "annotations")
            tags = _parse_json_array(str(row["human_tag_keys_json"] or "[]"), "human_tags")
            reviewed_at = str(row["human_reviewed_at"] or row["updated_at"] or "")
            fact, resolver_metadata = _build_fact(
                row,
                algorithm_key=algorithm_key,
                entry=entry,
                revision=1,
                decision=str(row["decision"]),
                annotations=annotations,
                tags=tags,
                reviewed_at=reviewed_at,
                resolver=image_resolver,
            )
        except FactQuarantined as exc:
            _quarantine(connection, algorithm_key, str(row["id"]), 1, exc)
            continue
        canonical = _canonical_json(fact)
        digest = _sha256_text(canonical)
        connection.execute(
            "INSERT OR IGNORE INTO review_fact_revisions VALUES (?,?,?,?,?,?,?,?)",
            (
                row["id"], algorithm_key, 1, canonical, digest, fact["image"]["sha256"],
                _canonical_json(resolver_metadata), utc_now_iso(),
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO review_publication_outbox(item_id,algorithm_key,review_revision,change_type,created_at) VALUES (?,?,?,?,?)",
            (row["id"], algorithm_key, 1, "human_review", utc_now_iso()),
        )
        connection.execute(
            "UPDATE items SET review_revision=1,capture_group_id=?,human_label_keys_json=?,human_tag_keys_json=? WHERE id=?",
            (
                fact["source"]["capture_group_id"],
                _canonical_json(fact["human_truth"]["label_keys"]),
                _canonical_json(fact["human_truth"]["tag_keys"]),
                row["id"],
            ),
        )


def _item(connection: sqlite3.Connection, item_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise ValueError("item not found")
    return row


def record_review_command(
    connection: sqlite3.Connection,
    *,
    item_id: str,
    decision: str | None,
    notes: str | None,
    annotations: list[Mapping[str, Any]] | None,
    idempotency_key: str,
    algorithm_key: str | None = None,
    label_keys: list[str] | None = None,
    tag_keys: list[str] | None = None,
    expected_revision: int | None = None,
    image_resolver: ImageResolver | None = None,
) -> dict[str, Any]:
    if not idempotency_key or len(idempotency_key) > 160:
        raise ValueError("valid idempotency key is required")
    row = _item(connection, item_id)
    resolved_algorithm = algorithm_key or visual_registry.legacy_algorithm_for(row)
    entry = visual_registry.accepted_algorithms().get(resolved_algorithm)
    if entry is None:
        raise ValueError("item is not mapped to an accepted visual algorithm")
    request = {
        "algorithm_key": resolved_algorithm,
        "item_id": item_id,
        "decision": decision,
        "notes": notes,
        "annotations": annotations,
        "label_keys": label_keys,
        "tag_keys": tag_keys,
        "expected_revision": expected_revision,
    }
    fingerprint = _sha256_text(_canonical_json(request))
    existing = connection.execute(
        "SELECT request_fingerprint,result_revision,response_json FROM review_command_receipts WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if existing["request_fingerprint"] != fingerprint:
            raise IdempotencyConflict("idempotency key was used for another review command")
        response = json.loads(existing["response_json"])
        response["replayed"] = True
        return response
    current_revision = int(
        connection.execute(
            "SELECT COALESCE(MAX(review_revision),0) FROM review_fact_revisions WHERE algorithm_key=? AND item_id=?",
            (resolved_algorithm, item_id),
        ).fetchone()[0]
    )
    if expected_revision is not None and expected_revision != current_revision:
        raise RevisionConflict(f"expected revision {expected_revision}, current {current_revision}")
    if notes is not None and (not isinstance(notes, str) or len(notes) > 1000):
        raise ValueError("invalid notes")
    final_decision = decision if decision is not None else str(row["decision"])
    truth_changing = (
        decision is not None
        or annotations is not None
        or label_keys is not None
        or tag_keys is not None
    ) and final_decision in {"positive", "negative", "unusable"}
    connection.execute("SAVEPOINT review_command")
    try:
        if not truth_changing:
            connection.execute(
                "UPDATE items SET notes=?,updated_at=? WHERE id=?",
                (notes if notes is not None else row["notes"], utc_now_iso(), item_id),
            )
            response = {"item_id": item_id, "review_revision": current_revision, "notes_only": True}
        else:
            raw_annotations = annotations
            if raw_annotations is None:
                raw_annotations = _parse_json_array(str(row["annotations"] or "[]"), "annotations")
            raw_tags: list[Any] = tag_keys if tag_keys is not None else _parse_json_array(
                str(row["human_tag_keys_json"] or "[]"), "human_tags"
            )
            if label_keys is not None:
                explicit = sorted({entry.normalize_label(label) for label in label_keys})
                box_labels = sorted({entry.normalize_label(str(box.get("label") or box.get("label_key") or "")) for box in raw_annotations})
                if explicit != box_labels:
                    raise FactQuarantined("label_box_mismatch", "explicit labels do not match boxes")
            next_revision = current_revision + 1
            now = utc_now_iso()
            prospective = dict(row)
            prospective["decision"] = final_decision
            prospective["annotations"] = _canonical_json(raw_annotations)
            prospective["human_tag_keys_json"] = _canonical_json(raw_tags)
            prospective["updated_at"] = now
            prospective["human_reviewed_at"] = now
            fact, resolver_metadata = _build_fact(
                prospective,  # type: ignore[arg-type]
                algorithm_key=resolved_algorithm,
                entry=entry,
                revision=next_revision,
                decision=final_decision,
                annotations=raw_annotations,
                tags=raw_tags,
                reviewed_at=now,
                resolver=image_resolver or _default_image_resolver,
            )
            canonical = _canonical_json(fact)
            digest = _sha256_text(canonical)
            connection.execute(
                """UPDATE items SET decision=?,notes=?,annotations=?,updated_at=?,human_reviewed=1,
                   human_reviewed_at=?,review_revision=?,capture_group_id=?,human_label_keys_json=?,human_tag_keys_json=?
                   WHERE id=?""",
                (
                    final_decision,
                    notes if notes is not None else row["notes"],
                    _canonical_json([
                        {"x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"], "label": box["label_key"]}
                        for box in fact["human_truth"]["boxes"]
                    ]),
                    now,
                    now,
                    next_revision,
                    fact["source"]["capture_group_id"],
                    _canonical_json(fact["human_truth"]["label_keys"]),
                    _canonical_json(fact["human_truth"]["tag_keys"]),
                    item_id,
                ),
            )
            connection.execute(
                "INSERT INTO review_fact_revisions VALUES (?,?,?,?,?,?,?,?)",
                (
                    item_id, resolved_algorithm, next_revision, canonical, digest,
                    fact["image"]["sha256"], _canonical_json(resolver_metadata), now,
                ),
            )
            connection.execute(
                "INSERT INTO review_publication_outbox(item_id,algorithm_key,review_revision,change_type,created_at) VALUES (?,?,?,?,?)",
                (item_id, resolved_algorithm, next_revision, "human_review", now),
            )
            response = {"item_id": item_id, "review_revision": next_revision, "fact_digest": digest}
        connection.execute(
            "INSERT INTO review_command_receipts VALUES (?,?,?,?,?,?)",
            (
                idempotency_key, fingerprint, item_id, int(response["review_revision"]),
                _canonical_json(response), utc_now_iso(),
            ),
        )
        connection.execute("RELEASE SAVEPOINT review_command")
        return response
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT review_command")
        connection.execute("RELEASE SAVEPOINT review_command")
        raise


def exact_review_fact(
    connection: sqlite3.Connection, algorithm_key: str, item_id: str, review_revision: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM review_fact_revisions WHERE algorithm_key=? AND item_id=? AND review_revision=?",
        (algorithm_key, item_id, review_revision),
    ).fetchone()
    if row is None:
        raise KeyError("review fact revision not found")
    if _sha256_text(row["canonical_fact_json"]) != row["fact_digest"]:
        raise RuntimeError("review fact digest mismatch")
    return row


def _box_iou(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    x1 = max(float(left["x"]), float(right["x"]))
    y1 = max(float(left["y"]), float(right["y"]))
    x2 = min(float(left["x"]) + float(left["w"]), float(right["x"]) + float(right["w"]))
    y2 = min(float(left["y"]) + float(left["h"]), float(right["y"]) + float(right["h"]))
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (
        float(left["w"]) * float(left["h"])
        + float(right["w"]) * float(right["h"])
        - intersection
    )
    return intersection / union if union > 0 else 0.0


def _truths_compatible(left: Mapping[str, Any], right: Mapping[str, Any], threshold: float) -> bool:
    left_truth = left["human_truth"]
    right_truth = right["human_truth"]
    for field in ("decision", "label_keys", "tag_keys"):
        if left_truth.get(field) != right_truth.get(field):
            return False
    left_boxes = list(left_truth.get("boxes") or [])
    right_boxes = list(right_truth.get("boxes") or [])
    if len(left_boxes) != len(right_boxes):
        return False
    unmatched = list(right_boxes)
    for box in left_boxes:
        candidates = [
            (index, _box_iou(box, candidate))
            for index, candidate in enumerate(unmatched)
            if candidate.get("label_key") == box.get("label_key")
        ]
        if not candidates:
            return False
        index, score = max(candidates, key=lambda value: value[1])
        if score < threshold:
            return False
        unmatched.pop(index)
    return not unmatched


def _quarantine_duplicate_conflict(
    connection: sqlite3.Connection,
    algorithm_key: str,
    candidates: list[tuple[str, int, str, str, dict[str, Any]]],
) -> None:
    conflict_digest = _sha256_text(
        _canonical_json(
            [
                {"item_id": item_id, "review_revision": revision, "fact_digest": digest}
                for item_id, revision, digest, _, _ in candidates
            ]
        )
    )
    for item_id, revision, _, _, _ in candidates:
        connection.execute(
            "INSERT OR REPLACE INTO review_fact_quarantine VALUES (?,?,?,?,?,?)",
            (
                algorithm_key, item_id, revision, "duplicate_truth_conflict",
                conflict_digest, utc_now_iso(),
            ),
        )


def create_snapshot(
    connection: sqlite3.Connection,
    *,
    algorithm_key: str,
    snapshot_watermark: int | None = None,
    lease_owner: str = "",
) -> str:
    entry = visual_registry.accepted_algorithms().get(algorithm_key)
    if entry is None:
        raise ValueError("unknown or inactive algorithm")
    pending_commit = connection.execute(
        "SELECT snapshot_id FROM review_publication_snapshots WHERE algorithm_key=? AND status='committed_pending_ack' LIMIT 1",
        (algorithm_key,),
    ).fetchone()
    if pending_commit is not None:
        raise SnapshotConflict(f"snapshot {pending_commit['snapshot_id']} requires acknowledgement")
    if snapshot_watermark is None:
        snapshot_watermark = int(
            connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM review_publication_outbox WHERE algorithm_key=?",
                (algorithm_key,),
            ).fetchone()[0]
        )
    pending = connection.execute(
        "SELECT * FROM review_publication_outbox WHERE algorithm_key=? AND id<=? AND published_at='' ORDER BY id",
        (algorithm_key, snapshot_watermark),
    ).fetchall()
    if not pending:
        raise NoPublishableChanges("no pending review facts at watermark")
    latest = connection.execute(
        """SELECT item_id,MAX(review_revision) AS review_revision
           FROM review_publication_outbox WHERE algorithm_key=? AND id<=? GROUP BY item_id ORDER BY item_id""",
        (algorithm_key, snapshot_watermark),
    ).fetchall()
    candidates_by_digest: dict[str, list[tuple[str, int, str, str, dict[str, Any]]]] = {}
    for member in latest:
        fact = exact_review_fact(connection, algorithm_key, member["item_id"], member["review_revision"])
        document = json.loads(fact["canonical_fact_json"])
        candidates_by_digest.setdefault(document["image"]["sha256"], []).append(
            (
                member["item_id"], member["review_revision"], fact["fact_digest"],
                fact["canonical_fact_json"], document,
            )
        )
    frozen: list[tuple[str, int, str, str]] = []
    represented: dict[str, tuple[str, int]] = {}
    for image_digest in sorted(candidates_by_digest):
        candidates = sorted(
            candidates_by_digest[image_digest], key=lambda value: (-value[1], value[0])
        )
        canonical = candidates[0]
        if not all(
            _truths_compatible(canonical[4], candidate[4], entry.box_match_iou_threshold)
            for candidate in candidates[1:]
        ):
            _quarantine_duplicate_conflict(connection, algorithm_key, candidates)
            continue
        frozen.append((canonical[0], canonical[1], canonical[2], canonical[3]))
        for item_id, _, _, _, _ in candidates:
            represented[item_id] = (canonical[0], canonical[1])
    frozen.sort(key=lambda value: value[0])
    if not frozen:
        raise NoPublishableChanges("all pending review facts are quarantined")
    total_bytes = sum(len(canonical.encode("utf-8")) for _, _, _, canonical in frozen)
    if total_bytes > entry.max_snapshot_bytes:
        raise SnapshotConflict("snapshot exceeds publication policy byte budget")
    membership = [
        {"ordinal": ordinal, "item_id": item, "review_revision": revision, "fact_digest": digest}
        for ordinal, (item, revision, digest, _) in enumerate(frozen)
    ]
    membership_digest = _sha256_text(_canonical_json(membership))
    snapshot_id = "review-snapshot:" + _sha256_text(
        _canonical_json(
            {"algorithm_key": algorithm_key, "watermark": snapshot_watermark, "membership": membership_digest}
        )
    )[:40]
    existing = connection.execute(
        "SELECT ordered_membership_digest FROM review_publication_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if existing is not None:
        if existing["ordered_membership_digest"] != membership_digest:
            raise SnapshotConflict("snapshot identity collision")
        return snapshot_id
    now = datetime.now(timezone.utc)
    semantics_policy_digest = _sha256_text(
        _canonical_json(
            {
                "visual_semantics": entry.visual_semantics_content_sha256,
                "publication_policy": entry.publication_policy_content_sha256,
            }
        )
    )
    connection.execute(
        """INSERT INTO review_publication_snapshots(
           snapshot_id,algorithm_key,snapshot_watermark,ordered_membership_digest,semantics_policy_digest,
           status,lease_owner,lease_expires_at,created_at,expires_at)
           VALUES (?,?,?,?,?,'frozen',?,?,?,?)""",
        (
            snapshot_id, algorithm_key, snapshot_watermark, membership_digest,
            semantics_policy_digest, lease_owner,
            (now + timedelta(seconds=SNAPSHOT_LEASE_SECONDS)).isoformat(timespec="seconds") if lease_owner else "",
            now.isoformat(timespec="seconds"),
            (now + timedelta(seconds=ACK_GRACE_SECONDS)).isoformat(timespec="seconds"),
        ),
    )
    for ordinal, (item_id, revision, digest, canonical) in enumerate(frozen):
        connection.execute(
            "INSERT INTO review_publication_snapshot_items VALUES (?,?,?,?,?,?,?)",
            (snapshot_id, ordinal, algorithm_key, item_id, revision, digest, canonical),
        )
    for outbox in pending:
        representative = represented.get(outbox["item_id"])
        if representative is None:
            continue
        connection.execute(
            """INSERT INTO review_publication_snapshot_outbox_members(
               snapshot_id,outbox_id,algorithm_key,item_id,review_revision,
               represented_by_item_id,represented_by_review_revision) VALUES (?,?,?,?,?,?,?)""",
            (
                snapshot_id, outbox["id"], algorithm_key, outbox["item_id"],
                outbox["review_revision"], representative[0], representative[1],
            ),
        )
    return snapshot_id


def record_snapshot_commit(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    source_version_id: str,
    source_content_digest: str,
    idempotency_key: str,
) -> None:
    if not source_version_id or len(source_content_digest) != 64 or not idempotency_key:
        raise ValueError("invalid immutable SourceVersion receipt")
    row = connection.execute(
        "SELECT * FROM review_publication_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise KeyError("snapshot not found")
    if row["status"] == "committed_pending_ack":
        if (
            row["source_version_id"], row["source_content_digest"], row["commit_idempotency_key"]
        ) != (source_version_id, source_content_digest, idempotency_key):
            raise SnapshotConflict("snapshot already committed with another receipt")
        return
    if row["status"] != "frozen":
        raise SnapshotConflict(f"cannot commit snapshot in state {row['status']}")
    connection.execute(
        """UPDATE review_publication_snapshots SET status='committed_pending_ack',source_version_id=?,
           source_content_digest=?,commit_idempotency_key=?,committed_at=? WHERE snapshot_id=? AND status='frozen'""",
        (source_version_id, source_content_digest, idempotency_key, utc_now_iso(), snapshot_id),
    )


def acknowledge_snapshot(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    source_version_id: str,
    source_content_digest: str,
) -> None:
    row = connection.execute(
        "SELECT * FROM review_publication_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise KeyError("snapshot not found")
    if row["status"] == "acknowledged":
        if (row["source_version_id"], row["source_content_digest"]) != (
            source_version_id,
            source_content_digest,
        ):
            raise SnapshotConflict("acknowledgement receipt mismatch")
        return
    if row["status"] != "committed_pending_ack" or (
        row["source_version_id"], row["source_content_digest"]
    ) != (source_version_id, source_content_digest):
        raise SnapshotConflict("snapshot commit receipt does not match acknowledgement")
    now = utc_now_iso()
    connection.execute(
        """UPDATE review_publication_outbox SET published_at=? WHERE id IN (
           SELECT outbox_id FROM review_publication_snapshot_outbox_members WHERE snapshot_id=?)
           AND published_at=''""",
        (now, snapshot_id),
    )
    connection.execute(
        "UPDATE review_publication_snapshots SET status='acknowledged',acknowledged_at=? WHERE snapshot_id=?",
        (now, snapshot_id),
    )
