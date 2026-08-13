#!/usr/bin/env python3
"""Dedicated training-only exact original resolver."""

from __future__ import annotations

import base64
import io
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, UnidentifiedImageError

try:
    from . import preview_resolver
except ImportError:
    import preview_resolver  # type: ignore


MAX_TRAINING_INPUT_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class OriginalResult:
    body: bytes
    content_type: str
    digest: str

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": self.content_type,
            "Content-Length": str(len(self.body)),
            "Digest": f"sha-256={base64.b64encode(bytes.fromhex(self.digest)).decode('ascii')}",
            "X-Content-SHA256": self.digest,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }


def resolve_review_original(
    connection: sqlite3.Connection,
    *,
    algorithm_key: str,
    item_id: str,
    review_revision: int,
    image_resolver: Callable[[sqlite3.Row], Path],
) -> OriginalResult:
    payload, mime, digest, metadata = preview_resolver.resolve_exact_source(
        connection,
        algorithm_key=algorithm_key,
        item_id=item_id,
        review_revision=review_revision,
        image_resolver=image_resolver,
        max_input_bytes=MAX_TRAINING_INPUT_BYTES,
    )
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.width * image.height > preview_resolver.MAX_PIXELS:
                raise preview_resolver.PreviewResolutionError(
                    "decoded_pixels_exceeded", "training original dimensions exceed limit"
                )
            image.verify()
            dimensions = image.size
    except (OSError, UnidentifiedImageError, AttributeError) as exc:
        raise preview_resolver.PreviewResolutionError(
            "image_decode_failed", "training original cannot be decoded"
        ) from exc
    if dimensions != (metadata.get("expected_width"), metadata.get("expected_height")):
        raise preview_resolver.PreviewResolutionError(
            "asset_dimensions_mismatch", "training original dimensions changed"
        )
    return OriginalResult(body=payload, content_type=mime, digest=digest)
