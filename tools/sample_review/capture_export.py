#!/usr/bin/env python3
"""Independent immutable raw-capture ledger and frozen publication pages."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from . import asset_export, review_revisions, visual_registry
except ImportError:
    import asset_export  # type: ignore
    import review_revisions  # type: ignore
    import visual_registry  # type: ignore


ImageResolver = Callable[[sqlite3.Row], Path]
CAPTURE_SQL = """
CREATE TABLE IF NOT EXISTS capture_revisions(
  algorithm_key TEXT NOT NULL,item_id TEXT NOT NULL,capture_revision INTEGER NOT NULL,
  canonical_capture_json TEXT NOT NULL,capture_digest TEXT NOT NULL,image_sha256 TEXT NOT NULL,
  resolver_metadata_json TEXT NOT NULL,created_at TEXT NOT NULL,
  PRIMARY KEY(algorithm_key,item_id,capture_revision));
CREATE TABLE IF NOT EXISTS capture_publication_outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT,algorithm_key TEXT NOT NULL,item_id TEXT NOT NULL,
  capture_revision INTEGER NOT NULL,created_at TEXT NOT NULL,published_at TEXT NOT NULL DEFAULT '',
  UNIQUE(algorithm_key,item_id,capture_revision));
CREATE TABLE IF NOT EXISTS capture_publication_snapshots(
  snapshot_id TEXT PRIMARY KEY,snapshot_watermark INTEGER NOT NULL,ordered_membership_digest TEXT NOT NULL,
  policy_digest TEXT NOT NULL,status TEXT NOT NULL,source_version_id TEXT NOT NULL DEFAULT '',
  source_content_digest TEXT NOT NULL DEFAULT '',lease_owner TEXT NOT NULL DEFAULT '',
  lease_expires_at TEXT NOT NULL DEFAULT '',commit_idempotency_key TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,expires_at TEXT NOT NULL,committed_at TEXT NOT NULL DEFAULT '',
  acknowledged_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS capture_publication_snapshot_items(
  snapshot_id TEXT NOT NULL,ordinal INTEGER NOT NULL,algorithm_key TEXT NOT NULL,item_id TEXT NOT NULL,
  capture_revision INTEGER NOT NULL,capture_digest TEXT NOT NULL,canonical_capture_json TEXT NOT NULL,
  PRIMARY KEY(snapshot_id,ordinal),UNIQUE(snapshot_id,algorithm_key,item_id));
CREATE TABLE IF NOT EXISTS capture_publication_snapshot_outbox_members(
  snapshot_id TEXT NOT NULL,outbox_id INTEGER NOT NULL,algorithm_key TEXT NOT NULL,item_id TEXT NOT NULL,
  capture_revision INTEGER NOT NULL,represented_by_capture_revision INTEGER NOT NULL,
  PRIMARY KEY(snapshot_id,outbox_id));
CREATE TRIGGER IF NOT EXISTS capture_revisions_no_update BEFORE UPDATE ON capture_revisions
BEGIN SELECT RAISE(ABORT,'capture revision is immutable'); END;
CREATE TRIGGER IF NOT EXISTS capture_revisions_no_delete BEFORE DELETE ON capture_revisions
BEGIN SELECT RAISE(ABORT,'capture revision is immutable'); END;
"""


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(CAPTURE_SQL)
    guards = {
        row[0]: str(row[1] or "").upper()
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'capture_revisions_no_%'"
        )
    }
    for name, marker in {
        "capture_revisions_no_update": "BEFORE UPDATE",
        "capture_revisions_no_delete": "BEFORE DELETE",
    }.items():
        if marker not in guards.get(name, "") or "IMMUTABLE" not in guards.get(name, ""):
            raise RuntimeError(f"capture immutable trigger missing or drifted: {name}")


def _capture_policy() -> dict[str, Any]:
    path = (
        visual_registry.DEFAULT_REGISTRY_ROOT
        / "publication-policies"
        / "raw-capture-v1.json"
    )
    policy = visual_registry._load_json(path)
    if policy.get("stream_kind") != "raw_capture":
        raise RuntimeError("raw capture publication policy mismatch")
    return policy


def _build_capture(
    row: sqlite3.Row,
    *,
    algorithm_key: str,
    revision: int,
    resolver: ImageResolver,
) -> tuple[dict[str, Any], dict[str, Any]]:
    entry = visual_registry.accepted_algorithms()[algorithm_key]
    image, resolver_metadata = review_revisions._image_metadata(
        row, algorithm_key, revision, resolver
    )
    capture_group_id = review_revisions._capture_group_id(row, algorithm_key)
    created_at = review_revisions.utc_now_iso()
    ingest_identity = str(row["ingest_key"] or row["id"])
    capture: dict[str, Any] = {
        "schema_version": "ai-bot-capture-item.v1",
        "item_id": str(row["id"]),
        "algorithm_key": algorithm_key,
        "capture_group_id": capture_group_id,
        "captured_at": review_revisions._captured_at(row),
        "source": {
            "source_id": f"capture-source:{algorithm_key}",
            "source_version_id": f"capture-ledger:{algorithm_key}:r{revision}",
            "source_item_id": str(row["id"]),
            "source_locator": f"aibot-capture://{algorithm_key}/{row['id']}/{revision}",
        },
        "image": {
            "image_sha256": image["sha256"],
            "byte_size": resolver_metadata["expected_length"],
            "content_type": resolver_metadata["expected_mime"],
            "width_px": image["width"],
            "height_px": image["height"],
        },
        "ingest": {
            "ingested_at": str(row["updated_at"]),
            "ingest_run_id": f"ingest:{review_revisions._sha256_text(ingest_identity)[:40]}",
            "mapping_ref": entry.source_mapping_ref,
            "mapping_content_sha256": entry.source_mapping_content_sha256,
        },
        "created_at": created_at,
    }
    capture["content_sha256"] = review_revisions._sha256_text(
        review_revisions._canonical_json(capture)
    )
    resolver_metadata["item_locator"] = {
        "algorithm_key": algorithm_key,
        "item_id": str(row["id"]),
        "capture_revision": revision,
    }
    resolver_metadata["resolver_policy_revision"] = "capture-ledger.v1"
    return capture, resolver_metadata


def discover_captures(connection: sqlite3.Connection, *, image_resolver: ImageResolver) -> int:
    migrate(connection)
    inserted = 0
    for row in connection.execute("SELECT * FROM items ORDER BY id").fetchall():
        algorithm_key = visual_registry.legacy_algorithm_for(row)
        if not algorithm_key:
            continue
        latest = connection.execute(
            "SELECT * FROM capture_revisions WHERE algorithm_key=? AND item_id=? ORDER BY capture_revision DESC LIMIT 1",
            (algorithm_key, row["id"]),
        ).fetchone()
        revision = int(latest["capture_revision"] if latest else 0) + 1
        try:
            capture, resolver_metadata = _build_capture(
                row, algorithm_key=algorithm_key, revision=revision, resolver=image_resolver
            )
        except review_revisions.FactQuarantined:
            continue
        if latest is not None and latest["image_sha256"] == capture["image"]["image_sha256"]:
            continue
        canonical = review_revisions._canonical_json(capture)
        connection.execute(
            "INSERT INTO capture_revisions VALUES (?,?,?,?,?,?,?,?)",
            (
                algorithm_key, row["id"], revision, canonical, capture["content_sha256"],
                capture["image"]["image_sha256"], review_revisions._canonical_json(resolver_metadata),
                capture["created_at"],
            ),
        )
        connection.execute(
            "INSERT INTO capture_publication_outbox(algorithm_key,item_id,capture_revision,created_at) VALUES (?,?,?,?)",
            (algorithm_key, row["id"], revision, capture["created_at"]),
        )
        inserted += 1
    return inserted


def exact_capture(
    connection: sqlite3.Connection, algorithm_key: str, item_id: str, capture_revision: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM capture_revisions WHERE algorithm_key=? AND item_id=? AND capture_revision=?",
        (algorithm_key, item_id, capture_revision),
    ).fetchone()
    if row is None:
        raise KeyError("capture revision not found")
    document = json.loads(row["canonical_capture_json"])
    recorded = document.pop("content_sha256", None)
    if (
        review_revisions._sha256_text(review_revisions._canonical_json(document)) != recorded
        or row["capture_digest"] != recorded
    ):
        raise RuntimeError("capture digest mismatch")
    return row


def create_snapshot(
    connection: sqlite3.Connection,
    *,
    lease_owner: str,
    image_resolver: ImageResolver,
) -> dict[str, Any]:
    if not lease_owner or len(lease_owner) > 128:
        raise ValueError("bounded lease_owner is required")
    discover_captures(connection, image_resolver=image_resolver)
    pending_commit = connection.execute(
        "SELECT snapshot_id FROM capture_publication_snapshots WHERE status='committed_pending_ack' LIMIT 1"
    ).fetchone()
    if pending_commit:
        raise review_revisions.SnapshotConflict(
            f"capture snapshot {pending_commit['snapshot_id']} requires acknowledgement"
        )
    watermark = int(
        connection.execute("SELECT COALESCE(MAX(id),0) FROM capture_publication_outbox").fetchone()[0]
    )
    pending = connection.execute(
        "SELECT * FROM capture_publication_outbox WHERE id<=? AND published_at='' ORDER BY id",
        (watermark,),
    ).fetchall()
    if not pending:
        raise review_revisions.NoPublishableChanges("no pending captures")
    latest = connection.execute(
        """SELECT algorithm_key,item_id,MAX(capture_revision) AS capture_revision
           FROM capture_publication_outbox WHERE id<=? GROUP BY algorithm_key,item_id
           ORDER BY algorithm_key,item_id""",
        (watermark,),
    ).fetchall()
    policy = _capture_policy()
    total = 0
    frozen: list[tuple[str, str, int, str, str]] = []
    for member in latest:
        row = exact_capture(
            connection, member["algorithm_key"], member["item_id"], member["capture_revision"]
        )
        total += len(row["canonical_capture_json"].encode("utf-8"))
        if total > int(policy["max_snapshot_bytes"]):
            raise review_revisions.SnapshotConflict("capture snapshot exceeds byte budget")
        frozen.append(
            (
                member["algorithm_key"], member["item_id"], member["capture_revision"],
                row["capture_digest"], row["canonical_capture_json"],
            )
        )
    membership = [
        {"ordinal": i, "algorithm_key": alg, "item_id": item, "capture_revision": rev, "capture_digest": digest}
        for i, (alg, item, rev, digest, _) in enumerate(frozen)
    ]
    membership_digest = review_revisions._sha256_text(
        review_revisions._canonical_json(membership)
    )
    snapshot_id = "capture-snapshot:" + review_revisions._sha256_text(
        review_revisions._canonical_json(
            {"watermark": watermark, "membership": membership_digest, "policy": policy["content_sha256"]}
        )
    )[:40]
    existing = connection.execute(
        "SELECT * FROM capture_publication_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if existing:
        return _snapshot_response(existing, len(frozen))
    now = datetime.now(timezone.utc)
    connection.execute(
        """INSERT INTO capture_publication_snapshots(
           snapshot_id,snapshot_watermark,ordered_membership_digest,policy_digest,status,
           lease_owner,lease_expires_at,created_at,expires_at)
           VALUES (?,?,?,?,'frozen',?,?,?,?)""",
        (
            snapshot_id, watermark, membership_digest, policy["content_sha256"], lease_owner,
            (now + timedelta(seconds=review_revisions.SNAPSHOT_LEASE_SECONDS)).isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
            (now + timedelta(seconds=review_revisions.ACK_GRACE_SECONDS)).isoformat(timespec="seconds"),
        ),
    )
    for ordinal, (algorithm_key, item_id, revision, digest, canonical) in enumerate(frozen):
        connection.execute(
            "INSERT INTO capture_publication_snapshot_items VALUES (?,?,?,?,?,?,?)",
            (snapshot_id, ordinal, algorithm_key, item_id, revision, digest, canonical),
        )
    represented = {(algorithm_key, item_id): revision for algorithm_key, item_id, revision, _, _ in frozen}
    for row in pending:
        connection.execute(
            "INSERT INTO capture_publication_snapshot_outbox_members VALUES (?,?,?,?,?,?)",
            (
                snapshot_id, row["id"], row["algorithm_key"], row["item_id"],
                row["capture_revision"], represented[(row["algorithm_key"], row["item_id"])],
            ),
        )
    stored = connection.execute(
        "SELECT * FROM capture_publication_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    return _snapshot_response(stored, len(frozen))


def _snapshot_response(row: sqlite3.Row, total: int) -> dict[str, Any]:
    return {
        "snapshot_id": row["snapshot_id"],
        "snapshot_watermark": row["snapshot_watermark"],
        "ordered_membership_digest": row["ordered_membership_digest"],
        "policy_digest": row["policy_digest"],
        "status": row["status"],
        "total": total,
        "expires_at": row["expires_at"],
    }


def page_snapshot(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    signing_key: bytes,
    lease_owner: str,
    limit: int = 100,
    cursor: str = "",
) -> dict[str, Any]:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    snapshot = connection.execute(
        "SELECT * FROM capture_publication_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if snapshot is None or snapshot["lease_owner"] != lease_owner:
        raise review_revisions.SnapshotConflict("capture snapshot lease mismatch")
    offset = 0
    if cursor:
        parsed = asset_export._parse_cursor(cursor, signing_key)
        if (
            parsed.get("snapshot_id") != snapshot_id
            or parsed.get("membership_digest") != snapshot["ordered_membership_digest"]
            or parsed.get("stream") != "raw_capture"
        ):
            raise asset_export.CursorError("capture cursor scope mismatch")
        offset = parsed.get("offset")
        if not isinstance(offset, int) or offset < 0:
            raise asset_export.CursorError("invalid capture cursor offset")
    rows = connection.execute(
        """SELECT * FROM capture_publication_snapshot_items WHERE snapshot_id=? AND ordinal>=?
           ORDER BY ordinal LIMIT ?""",
        (snapshot_id, offset, limit + 1),
    ).fetchall()
    items = []
    for row in rows[:limit]:
        document = json.loads(row["canonical_capture_json"])
        recorded = document.pop("content_sha256", None)
        if (
            review_revisions._sha256_text(review_revisions._canonical_json(document)) != recorded
            or row["capture_digest"] != recorded
        ):
            raise RuntimeError("capture snapshot digest mismatch")
        items.append(json.loads(row["canonical_capture_json"]))
    next_cursor = ""
    if len(rows) > limit:
        next_cursor = asset_export._cursor(
            {
                "stream": "raw_capture",
                "snapshot_id": snapshot_id,
                "membership_digest": snapshot["ordered_membership_digest"],
                "offset": offset + limit,
            },
            signing_key,
        )
    return {
        "snapshot_id": snapshot_id,
        "ordered_membership_digest": snapshot["ordered_membership_digest"],
        "items": items,
        "next_cursor": next_cursor,
    }


def record_commit(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    source_version_id: str,
    source_content_digest: str,
    idempotency_key: str,
) -> None:
    row = connection.execute(
        "SELECT * FROM capture_publication_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise KeyError("capture snapshot not found")
    if row["status"] == "committed_pending_ack":
        if (row["source_version_id"], row["source_content_digest"], row["commit_idempotency_key"]) != (
            source_version_id, source_content_digest, idempotency_key
        ):
            raise review_revisions.SnapshotConflict("capture commit receipt mismatch")
        return
    if row["status"] != "frozen" or not source_version_id or len(source_content_digest) != 64:
        raise review_revisions.SnapshotConflict("capture snapshot cannot be committed")
    connection.execute(
        """UPDATE capture_publication_snapshots SET status='committed_pending_ack',source_version_id=?,
           source_content_digest=?,commit_idempotency_key=?,committed_at=? WHERE snapshot_id=? AND status='frozen'""",
        (source_version_id, source_content_digest, idempotency_key, review_revisions.utc_now_iso(), snapshot_id),
    )


def acknowledge(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    source_version_id: str,
    source_content_digest: str,
) -> None:
    row = connection.execute(
        "SELECT * FROM capture_publication_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise KeyError("capture snapshot not found")
    if row["status"] == "acknowledged":
        if (row["source_version_id"], row["source_content_digest"]) != (
            source_version_id, source_content_digest
        ):
            raise review_revisions.SnapshotConflict("capture acknowledgement mismatch")
        return
    if row["status"] != "committed_pending_ack" or (
        row["source_version_id"], row["source_content_digest"]
    ) != (source_version_id, source_content_digest):
        raise review_revisions.SnapshotConflict("capture commit receipt does not match")
    now = review_revisions.utc_now_iso()
    connection.execute(
        """UPDATE capture_publication_outbox SET published_at=? WHERE id IN (
           SELECT outbox_id FROM capture_publication_snapshot_outbox_members WHERE snapshot_id=?) AND published_at=''""",
        (now, snapshot_id),
    )
    connection.execute(
        "UPDATE capture_publication_snapshots SET status='acknowledged',acknowledged_at=? WHERE snapshot_id=?",
        (now, snapshot_id),
    )
