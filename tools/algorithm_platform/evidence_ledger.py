#!/usr/bin/env python3
"""Append-only, schema-bound lineage and validation evidence ledger."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "platform/contracts/ai-bot-visual-knowledge-v1.bundle.json"
SCHEMA_SQL = Path(__file__).with_name("evidence_schema.sql")
SCHEMA_FILES = {
    "lineage": "ai-bot-lineage-record-v1.schema.json",
    "validation": "ai-bot-validation-record-v1.schema.json",
}
FORBIDDEN_TEXT = re.compile(
    r"(?i)(begin (?:rsa |ec |openssh )?private key|authorization:\s*bearer|"
    r"https?://|(?:^|\s)(?:[a-z]:\\|/(?:home|root|srv|var|etc)/))"
)


class EvidenceError(ValueError):
    pass


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bundle() -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location(
        "ai_bot_visual_contract_verifier",
        BUNDLE.with_name("verify_visual_contract_bundle.py"),
    )
    if spec is None or spec.loader is None:
        raise EvidenceError("contract verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.verify_bundle(BUNDLE)
    value = json.loads(BUNDLE.read_text(encoding="utf-8"))
    return value


def load_schema(stream_kind: str) -> dict[str, Any]:
    filename = SCHEMA_FILES.get(stream_kind)
    if filename is None:
        raise EvidenceError("unsupported evidence stream")
    entry = _bundle()["files"].get(filename)
    if not isinstance(entry, dict):
        raise EvidenceError("evidence schema is absent from verified bundle")
    payload = base64.b64decode(entry["content_base64"], validate=True)
    if hashlib.sha256(payload).hexdigest() != entry["content_sha256"]:
        raise EvidenceError("evidence schema digest mismatch")
    return json.loads(payload)


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        raise EvidenceError("unsupported schema reference")
    target = root.get("$defs", {}).get(ref.rsplit("/", 1)[-1])
    if not isinstance(target, dict):
        raise EvidenceError("missing schema definition")
    return target


def _validate(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    schema = _resolve(schema, root)
    if "oneOf" in schema:
        matches = []
        for candidate in schema["oneOf"]:
            try:
                _validate(value, candidate, root, path)
                matches.append(candidate)
            except EvidenceError:
                pass
        if len(matches) != 1:
            raise EvidenceError(f"{path} does not match exactly one schema branch")
        return
    if "const" in schema and value != schema["const"]:
        raise EvidenceError(f"{path} has an invalid constant")
    if "enum" in schema and value not in schema["enum"]:
        raise EvidenceError(f"{path} has an invalid enum value")
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise EvidenceError(f"{path} must be an object")
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise EvidenceError(f"{path} is missing {missing[0]}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise EvidenceError(f"{path} contains an unsupported field")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], root, f"{path}.{key}")
    elif expected == "array":
        if not isinstance(value, list):
            raise EvidenceError(f"{path} must be an array")
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 1 << 30):
            raise EvidenceError(f"{path} has invalid item count")
        if schema.get("uniqueItems") and len({canonical_json(item) for item in value}) != len(value):
            raise EvidenceError(f"{path} contains duplicate items")
        for index, item in enumerate(value):
            _validate(item, schema.get("items", {}), root, f"{path}[{index}]")
    elif expected == "string":
        if not isinstance(value, str):
            raise EvidenceError(f"{path} must be a string")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1 << 30):
            raise EvidenceError(f"{path} has invalid length")
        if schema.get("pattern") and re.fullmatch(schema["pattern"], value) is None:
            raise EvidenceError(f"{path} has invalid format")
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise EvidenceError(f"{path} must be a date-time") from exc
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise EvidenceError(f"{path} must be an integer")
        if value < schema.get("minimum", -(1 << 63)) or value > schema.get("maximum", 1 << 63):
            raise EvidenceError(f"{path} is outside bounds")
    elif expected == "boolean" and not isinstance(value, bool):
        raise EvidenceError(f"{path} must be a boolean")


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def validate_record(stream_kind: str, record: dict[str, Any]) -> str:
    schema = load_schema(stream_kind)
    _validate(record, schema, schema, "record")
    if any(FORBIDDEN_TEXT.search(value) for value in _strings(record)):
        raise EvidenceError("evidence contains a secret, URL, command or host path")
    return canonical_json(record)


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    required = {
        "visual_evidence_records_no_update",
        "visual_evidence_records_no_delete",
        "visual_evidence_snapshot_items_no_update",
        "visual_evidence_snapshot_items_no_delete",
    }
    actual = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if not required.issubset(actual):
        raise RuntimeError("visual evidence immutability guards are unavailable")


def append_record(
    connection: sqlite3.Connection,
    stream_kind: str,
    record: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", idempotency_key):
        raise EvidenceError("invalid idempotency key")
    canonical = validate_record(stream_kind, record)
    record_digest = digest_text(canonical)
    fingerprint = digest_text(canonical_json({"stream_kind": stream_kind, "record": record}))
    existing = connection.execute(
        "SELECT request_fingerprint,record_id,record_digest FROM visual_evidence_receipts WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if existing[0] != fingerprint:
            raise EvidenceError("idempotency key conflicts with another request")
        return {"record_id": existing[1], "record_digest": existing[2], "replayed": True}
    algorithm = record["algorithm_key"]
    record_id = record["record_id"]
    prior = connection.execute(
        "SELECT record_digest FROM visual_evidence_records WHERE stream_kind=? AND algorithm_key=? AND record_id=?",
        (stream_kind, algorithm, record_id),
    ).fetchone()
    if prior is not None and prior[0] != record_digest:
        raise EvidenceError("record identity already has different immutable content")
    now = utc_now()
    connection.execute(
        "INSERT OR IGNORE INTO visual_evidence_records(stream_kind,algorithm_key,record_id,record_digest,canonical_record_json,created_at) VALUES(?,?,?,?,?,?)",
        (stream_kind, algorithm, record_id, record_digest, canonical, now),
    )
    connection.execute(
        "INSERT INTO visual_evidence_receipts VALUES(?,?,?,?,?,?,?)",
        (idempotency_key, fingerprint, stream_kind, algorithm, record_id, record_digest, now),
    )
    return {"record_id": record_id, "record_digest": record_digest, "replayed": False}


def create_snapshot(connection: sqlite3.Connection, stream_kind: str, algorithm_key: str) -> dict[str, Any]:
    if stream_kind not in SCHEMA_FILES or re.fullmatch(r"[a-z][a-z0-9_]{1,63}", algorithm_key) is None:
        raise EvidenceError("invalid snapshot scope")
    rows = connection.execute(
        "SELECT sequence_no,record_digest FROM visual_evidence_records WHERE stream_kind=? AND algorithm_key=? ORDER BY sequence_no",
        (stream_kind, algorithm_key),
    ).fetchall()
    membership = [{"sequence_no": int(row[0]), "record_digest": row[1]} for row in rows]
    membership_digest = digest_text(canonical_json(membership))
    watermark = int(rows[-1][0]) if rows else 0
    snapshot_id = f"evidence:{stream_kind}:{digest_text(canonical_json({'algorithm_key': algorithm_key, 'membership': membership}))[:40]}"
    now = utc_now()
    connection.execute(
        "INSERT OR IGNORE INTO visual_evidence_snapshots VALUES(?,?,?,?,?,?,?)",
        (snapshot_id, stream_kind, algorithm_key, watermark, membership_digest, len(rows), now),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO visual_evidence_snapshot_items VALUES(?,?,?,?)",
        [(snapshot_id, ordinal, int(row[0]), row[1]) for ordinal, row in enumerate(rows)],
    )
    return {"snapshot_id": snapshot_id, "stream_kind": stream_kind, "algorithm_key": algorithm_key, "watermark": watermark, "membership_digest": membership_digest, "total": len(rows)}


def page_snapshot(connection: sqlite3.Connection, snapshot_id: str, offset: int, limit: int) -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= 500:
        raise EvidenceError("invalid snapshot page")
    snapshot = connection.execute(
        "SELECT stream_kind,algorithm_key,watermark,membership_digest,total FROM visual_evidence_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if snapshot is None:
        raise KeyError("evidence snapshot not found")
    rows = connection.execute(
        "SELECT item.ordinal,record.canonical_record_json,item.record_digest FROM visual_evidence_snapshot_items item JOIN visual_evidence_records record ON record.sequence_no=item.record_sequence_no WHERE item.snapshot_id=? AND item.ordinal>=? ORDER BY item.ordinal LIMIT ?",
        (snapshot_id, offset, limit),
    ).fetchall()
    items = [{"ordinal": int(row[0]), "record": json.loads(row[1]), "record_digest": row[2]} for row in rows]
    next_offset = offset + len(items)
    return {
        "snapshot_id": snapshot_id,
        "stream_kind": snapshot[0],
        "algorithm_key": snapshot[1],
        "watermark": int(snapshot[2]),
        "membership_digest": snapshot[3],
        "total": int(snapshot[4]),
        "items": items,
        "next_offset": next_offset if next_offset < int(snapshot[4]) else None,
    }


def default_database() -> Path:
    root = Path(os.environ.get("SAMPLE_REVIEW_ROOT", "/srv/ai-bot-sample-review"))
    return root / "data/review.sqlite3"
