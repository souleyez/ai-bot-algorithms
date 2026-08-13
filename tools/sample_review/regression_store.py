#!/usr/bin/env python3
"""Immutable, manually curated regression selections."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

try:
    from . import review_revisions, visual_registry
except ImportError:
    import review_revisions  # type: ignore
    import visual_registry  # type: ignore


REGRESSION_ROLES = {
    "hard_positive",
    "hard_negative",
    "historical_false_positive",
    "historical_false_negative",
    "box_edge_case",
    "device_scene",
}


class RegressionSelectionConflict(RuntimeError):
    pass


def _normalize_items(
    connection: sqlite3.Connection,
    algorithm_key: str,
    items: Iterable[Mapping[str, Any]],
    *,
    validate_facts: bool = True,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    entry = visual_registry.accepted_algorithms().get(algorithm_key)
    if entry is None:
        raise ValueError("unknown or inactive algorithm")
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("regression item must be an object")
        item_id = item.get("item_id")
        revision = item.get("review_revision")
        digest = item.get("review_fact_digest")
        roles = item.get("regression_roles")
        if not isinstance(item_id, str) or not item_id or len(item_id) > 256:
            raise ValueError("invalid regression item_id")
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("invalid regression review_revision")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("invalid review_fact_digest")
        if (
            not isinstance(roles, list)
            or not roles
            or len(roles) > 6
            or not all(isinstance(role, str) and role in REGRESSION_ROLES for role in roles)
        ):
            raise ValueError("invalid regression_roles")
        role_set = sorted(set(roles))
        if len(role_set) != len(roles):
            raise ValueError("duplicate regression role")
        key = (item_id, revision)
        if key in seen:
            raise ValueError("duplicate regression item revision")
        seen.add(key)
        if validate_facts:
            fact_row = review_revisions.exact_review_fact(
                connection, algorithm_key, item_id, revision
            )
            if fact_row["fact_digest"] != digest:
                raise ValueError("regression review fact digest mismatch")
            fact = json.loads(fact_row["canonical_fact_json"])
            if (
                fact["visual_semantics_version_ref"] != entry.visual_semantics_version_ref
                or fact["visual_semantics_content_sha256"] != entry.visual_semantics_content_sha256
            ):
                raise ValueError("regression fact uses another semantics bundle")
        normalized.append(
            {
                "item_id": item_id,
                "review_revision": revision,
                "review_fact_digest": digest,
                "regression_roles": role_set,
            }
        )
    if not normalized:
        raise ValueError("at least one regression item is required")
    if len(normalized) > 10_000:
        raise ValueError("regression selection exceeds item limit")
    return sorted(normalized, key=lambda value: (value["item_id"], value["review_revision"]))


def create_selection(
    connection: sqlite3.Connection,
    *,
    algorithm_key: str,
    items: Iterable[Mapping[str, Any]],
    idempotency_key: str,
    selection_id: str | None = None,
) -> dict[str, Any]:
    if not idempotency_key or len(idempotency_key) > 160:
        raise ValueError("valid idempotency key is required")
    normalized = _normalize_items(
        connection, algorithm_key, items, validate_facts=False
    )
    fingerprint = review_revisions._sha256_text(
        review_revisions._canonical_json(
            {"algorithm_key": algorithm_key, "items": normalized, "selection_id": selection_id}
        )
    )
    existing = connection.execute(
        "SELECT request_fingerprint,canonical_selection_json FROM regression_selections WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if existing["request_fingerprint"] != fingerprint:
            raise RegressionSelectionConflict("idempotency key was used for another selection")
        result = json.loads(existing["canonical_selection_json"])
        result["replayed"] = True
        return result
    normalized = _normalize_items(connection, algorithm_key, normalized, validate_facts=True)
    revision = int(
        connection.execute(
            "SELECT COALESCE(MAX(selection_revision),0)+1 FROM regression_selections WHERE algorithm_key=?",
            (algorithm_key,),
        ).fetchone()[0]
    )
    entry = visual_registry.accepted_algorithms()[algorithm_key]
    resolved_id = selection_id or f"regression:{algorithm_key}:r{revision}"
    if len(resolved_id) > 128:
        raise ValueError("selection_id exceeds limit")
    selection: dict[str, Any] = {
        "schema_version": "ai-bot-regression-selection.v1",
        "selection_id": resolved_id,
        "selection_revision": revision,
        "algorithm_key": algorithm_key,
        "visual_semantics_version_ref": entry.visual_semantics_version_ref,
        "visual_semantics_content_sha256": entry.visual_semantics_content_sha256,
        "items": normalized,
        "created_at": review_revisions.utc_now_iso(),
    }
    selection["content_sha256"] = review_revisions._sha256_text(
        review_revisions._canonical_json(selection)
    )
    canonical = review_revisions._canonical_json(selection)
    connection.execute(
        "INSERT INTO regression_selections VALUES (?,?,?,?,?,?,?,?,?)",
        (
            resolved_id,
            algorithm_key,
            revision,
            canonical,
            selection["content_sha256"],
            "active",
            idempotency_key,
            fingerprint,
            selection["created_at"],
        ),
    )
    return selection


def get_selection(connection: sqlite3.Connection, selection_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT canonical_selection_json,selection_digest FROM regression_selections WHERE selection_id=?",
        (selection_id,),
    ).fetchone()
    if row is None:
        raise KeyError("regression selection not found")
    selection = json.loads(row["canonical_selection_json"])
    recorded = selection.pop("content_sha256", None)
    actual = review_revisions._sha256_text(review_revisions._canonical_json(selection))
    selection["content_sha256"] = recorded
    if actual != recorded or row["selection_digest"] != recorded:
        raise RuntimeError("regression selection digest mismatch")
    return selection
