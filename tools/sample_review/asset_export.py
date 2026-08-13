#!/usr/bin/env python3
"""Bounded export helpers for immutable review publication snapshots."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

try:
    from . import review_revisions
except ImportError:
    import review_revisions  # type: ignore


class CursorError(ValueError):
    pass


def _b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _b64decode(payload: str) -> bytes:
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))


def _cursor(payload: dict[str, Any], signing_key: bytes) -> str:
    if len(signing_key) < 32:
        raise ValueError("cursor signing key must be at least 32 bytes")
    body = review_revisions._canonical_json(payload).encode("utf-8")
    signature = hmac.new(signing_key, body, hashlib.sha256).digest()
    return _b64encode(body) + "." + _b64encode(signature)


def _parse_cursor(token: str, signing_key: bytes) -> dict[str, Any]:
    try:
        body_token, signature_token = token.split(".", 1)
        body = _b64decode(body_token)
        signature = _b64decode(signature_token)
        expected = hmac.new(signing_key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise CursorError("cursor signature mismatch")
        payload = json.loads(body)
    except CursorError:
        raise
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("invalid cursor") from exc
    if not isinstance(payload, dict):
        raise CursorError("invalid cursor payload")
    return payload


def create_review_snapshot(
    connection: sqlite3.Connection, *, algorithm_key: str, lease_owner: str
) -> dict[str, Any]:
    if not lease_owner or len(lease_owner) > 128:
        raise ValueError("bounded lease_owner is required")
    snapshot_id = review_revisions.create_snapshot(
        connection, algorithm_key=algorithm_key, lease_owner=lease_owner
    )
    row = connection.execute(
        "SELECT * FROM review_publication_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    return {
        "snapshot_id": snapshot_id,
        "algorithm_key": row["algorithm_key"],
        "snapshot_watermark": row["snapshot_watermark"],
        "ordered_membership_digest": row["ordered_membership_digest"],
        "semantics_policy_digest": row["semantics_policy_digest"],
        "status": row["status"],
        "expires_at": row["expires_at"],
    }


def page_review_snapshot(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    signing_key: bytes,
    limit: int = 100,
    cursor: str = "",
    lease_owner: str = "",
) -> dict[str, Any]:
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    snapshot = connection.execute(
        "SELECT * FROM review_publication_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if snapshot is None:
        raise KeyError("snapshot not found")
    if snapshot["status"] not in {"frozen", "committed_pending_ack", "acknowledged"}:
        raise review_revisions.SnapshotConflict("snapshot is not readable")
    if snapshot["lease_owner"] and snapshot["lease_owner"] != lease_owner:
        raise review_revisions.SnapshotConflict("snapshot lease owner mismatch")
    offset = 0
    if cursor:
        parsed = _parse_cursor(cursor, signing_key)
        expected_scope = {
            "snapshot_id": snapshot_id,
            "membership_digest": snapshot["ordered_membership_digest"],
        }
        if any(parsed.get(key) != value for key, value in expected_scope.items()):
            raise CursorError("cursor scope mismatch")
        offset = parsed.get("offset")
        if not isinstance(offset, int) or offset < 0:
            raise CursorError("invalid cursor offset")
    rows = connection.execute(
        """SELECT ordinal,item_id,review_revision,fact_digest,canonical_fact_json
           FROM review_publication_snapshot_items WHERE snapshot_id=? AND ordinal>=?
           ORDER BY ordinal LIMIT ?""",
        (snapshot_id, offset, limit + 1),
    ).fetchall()
    page_rows = rows[:limit]
    items = []
    for row in page_rows:
        if review_revisions._sha256_text(row["canonical_fact_json"]) != row["fact_digest"]:
            raise RuntimeError("snapshot fact digest mismatch")
        items.append(json.loads(row["canonical_fact_json"]))
    next_cursor = ""
    if len(rows) > limit:
        next_cursor = _cursor(
            {
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


def renew_snapshot_lease(
    connection: sqlite3.Connection, *, snapshot_id: str, lease_owner: str
) -> str:
    row = connection.execute(
        "SELECT status,lease_owner FROM review_publication_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise KeyError("snapshot not found")
    if row["status"] != "frozen" or row["lease_owner"] != lease_owner:
        raise review_revisions.SnapshotConflict("snapshot lease cannot be renewed")
    from datetime import timedelta

    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=review_revisions.SNAPSHOT_LEASE_SECONDS)
    ).isoformat(timespec="seconds")
    connection.execute(
        "UPDATE review_publication_snapshots SET lease_expires_at=? WHERE snapshot_id=? AND status='frozen'",
        (expires, snapshot_id),
    )
    return expires


def commit_review_snapshot(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    source_version_id: str,
    source_content_digest: str,
    idempotency_key: str,
) -> None:
    review_revisions.record_snapshot_commit(
        connection,
        snapshot_id=snapshot_id,
        source_version_id=source_version_id,
        source_content_digest=source_content_digest,
        idempotency_key=idempotency_key,
    )


def acknowledge_review_snapshot(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    source_version_id: str,
    source_content_digest: str,
) -> None:
    review_revisions.acknowledge_snapshot(
        connection,
        snapshot_id=snapshot_id,
        source_version_id=source_version_id,
        source_content_digest=source_content_digest,
    )
