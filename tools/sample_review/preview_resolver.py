#!/usr/bin/env python3
"""Identifier-only, exact-revision thumbnail resolver."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, UnidentifiedImageError

try:
    from . import review_revisions, visual_registry
except ImportError:
    import review_revisions  # type: ignore
    import visual_registry  # type: ignore


MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_EDGE = 1600
MAX_OUTPUT_BYTES = 2 * 1024 * 1024


class PreviewResolutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreviewResult:
    body: bytes
    content_type: str
    source_digest: str

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": self.content_type,
            "Content-Length": str(len(self.body)),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }


def _magic_mime(payload: bytes) -> str:
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    raise PreviewResolutionError("unsupported_image_type", "unsupported image magic")


def resolve_exact_source(
    connection: sqlite3.Connection,
    *,
    algorithm_key: str,
    item_id: str,
    review_revision: int,
    image_resolver: Callable[[sqlite3.Row], Path],
    max_input_bytes: int = MAX_INPUT_BYTES,
) -> tuple[bytes, str, str, dict]:
    if algorithm_key not in visual_registry.accepted_algorithms():
        raise PreviewResolutionError("algorithm_not_found", "algorithm is not available")
    try:
        fact_row = review_revisions.exact_review_fact(
            connection, algorithm_key, item_id, review_revision
        )
    except KeyError as exc:
        raise PreviewResolutionError("revision_not_found", "review revision not found") from exc
    try:
        metadata = json.loads(fact_row["resolver_metadata_json"])
    except json.JSONDecodeError as exc:
        raise PreviewResolutionError("resolver_metadata_invalid", "resolver metadata is invalid") from exc
    expected_locator = {
        "algorithm_key": algorithm_key,
        "item_id": item_id,
        "review_revision": review_revision,
    }
    if metadata.get("item_locator") != expected_locator:
        raise PreviewResolutionError("resolver_scope_mismatch", "resolver scope does not match revision")
    item = connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if item is None:
        raise PreviewResolutionError("asset_unavailable", "review asset is unavailable")
    try:
        path = Path(image_resolver(item)).resolve()
        size = path.stat().st_size
        if size <= 0 or size > max_input_bytes:
            raise PreviewResolutionError("input_size_exceeded", "image exceeds resolver input limit")
        payload = path.read_bytes()
    except PreviewResolutionError:
        raise
    except OSError as exc:
        raise PreviewResolutionError("asset_unavailable", "review asset is unavailable") from exc
    digest = hashlib.sha256(payload).hexdigest()
    if digest != metadata.get("expected_sha256") or len(payload) != metadata.get("expected_length"):
        raise PreviewResolutionError("asset_digest_mismatch", "review asset changed")
    mime = _magic_mime(payload)
    if mime != metadata.get("expected_mime"):
        raise PreviewResolutionError("asset_type_mismatch", "review asset type changed")
    return payload, mime, digest, metadata


def resolve_review_preview(
    connection: sqlite3.Connection,
    *,
    algorithm_key: str,
    item_id: str,
    review_revision: int,
    image_resolver: Callable[[sqlite3.Row], Path],
) -> PreviewResult:
    payload, _, digest, metadata = resolve_exact_source(
        connection,
        algorithm_key=algorithm_key,
        item_id=item_id,
        review_revision=review_revision,
        image_resolver=image_resolver,
    )
    return _render_preview(payload, digest, metadata)


def resolve_live_review_preview(
    connection: sqlite3.Connection,
    *,
    algorithm_key: str,
    item_id: str,
    expected_review_revision: int,
    image_resolver: Callable[[sqlite3.Row], Path],
) -> PreviewResult:
    """Resolve the mutable review-queue head while binding it to its exact CAS revision."""
    if algorithm_key not in visual_registry.accepted_algorithms():
        raise PreviewResolutionError("algorithm_not_found", "algorithm is not available")
    item = connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if item is None or visual_registry.legacy_algorithm_for(item) != algorithm_key:
        raise PreviewResolutionError("item_not_found", "review item is not available")
    if int(item["review_revision"]) != expected_review_revision:
        raise PreviewResolutionError("revision_conflict", "review item revision changed")
    try:
        path = Path(image_resolver(item)).resolve()
        size = path.stat().st_size
        if size <= 0 or size > MAX_INPUT_BYTES:
            raise PreviewResolutionError("input_size_exceeded", "image exceeds resolver input limit")
        payload = path.read_bytes()
    except PreviewResolutionError:
        raise
    except OSError as exc:
        raise PreviewResolutionError("asset_unavailable", "review asset is unavailable") from exc
    digest = hashlib.sha256(payload).hexdigest()
    mime = _magic_mime(payload)
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            metadata = {
                "expected_width": image.width,
                "expected_height": image.height,
            }
    except (OSError, UnidentifiedImageError) as exc:
        raise PreviewResolutionError("image_decode_failed", "image cannot be decoded") from exc
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        raise PreviewResolutionError("unsupported_image_type", "unsupported image type")
    return _render_preview(payload, digest, metadata)


def _render_preview(payload: bytes, digest: str, metadata: dict) -> PreviewResult:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise PreviewResolutionError("decoded_pixels_exceeded", "decoded image is too large")
            if (image.width, image.height) != (
                metadata.get("expected_width"),
                metadata.get("expected_height"),
            ):
                raise PreviewResolutionError("asset_dimensions_mismatch", "review asset dimensions changed")
            image.load()
            image = image.convert("RGB")
            image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            result = b""
            for quality in (88, 80, 72, 64, 56):
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
                result = buffer.getvalue()
                if len(result) <= MAX_OUTPUT_BYTES:
                    break
    except PreviewResolutionError:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise PreviewResolutionError("image_decode_failed", "image cannot be decoded") from exc
    if not result or len(result) > MAX_OUTPUT_BYTES:
        raise PreviewResolutionError("preview_size_exceeded", "preview exceeds output limit")
    return PreviewResult(body=result, content_type="image/jpeg", source_digest=digest)


def resolve_capture_preview(
    connection: sqlite3.Connection,
    *,
    algorithm_key: str,
    item_id: str,
    capture_revision: int,
    image_resolver: Callable[[sqlite3.Row], Path],
) -> PreviewResult:
    try:
        from . import capture_export
    except ImportError:
        import capture_export  # type: ignore
    if algorithm_key not in visual_registry.accepted_algorithms():
        raise PreviewResolutionError("algorithm_not_found", "algorithm is not available")
    try:
        capture = capture_export.exact_capture(
            connection, algorithm_key, item_id, capture_revision
        )
    except KeyError as exc:
        raise PreviewResolutionError("revision_not_found", "capture revision not found") from exc
    metadata = json.loads(capture["resolver_metadata_json"])
    if metadata.get("item_locator") != {
        "algorithm_key": algorithm_key,
        "item_id": item_id,
        "capture_revision": capture_revision,
    }:
        raise PreviewResolutionError("resolver_scope_mismatch", "capture scope mismatch")
    item = connection.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if item is None:
        raise PreviewResolutionError("asset_unavailable", "capture asset is unavailable")
    try:
        path = Path(image_resolver(item)).resolve()
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise PreviewResolutionError("input_size_exceeded", "image exceeds resolver input limit")
        payload = path.read_bytes()
    except PreviewResolutionError:
        raise
    except OSError as exc:
        raise PreviewResolutionError("asset_unavailable", "capture asset is unavailable") from exc
    digest = hashlib.sha256(payload).hexdigest()
    if digest != metadata.get("expected_sha256") or len(payload) != metadata.get("expected_length"):
        raise PreviewResolutionError("asset_digest_mismatch", "capture asset changed")
    if _magic_mime(payload) != metadata.get("expected_mime"):
        raise PreviewResolutionError("asset_type_mismatch", "capture asset type changed")
    return _render_preview(payload, digest, metadata)
