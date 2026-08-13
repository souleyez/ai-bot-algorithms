#!/usr/bin/env python3
"""Verify the pinned AI-BOT visual-contract bundle without network access."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any


class BundleVerificationError(RuntimeError):
    """The vendored bundle is malformed, incomplete, or was tampered with."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _serialize_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def canonicalize(value: Any) -> str:
    """Canonical JSON for the bounded contract subset used by this bundle.

    The bundle builder emits only null/bool/string/integer/finite-number/list/
    object values.  Rejecting anything else is intentional and fail-closed.
    """

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _serialize_string(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BundleVerificationError("non-finite number in bundle")
        if value.is_integer():
            return str(int(value))
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(canonicalize(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise BundleVerificationError("bundle object contains a non-string key")
        return "{" + ",".join(
            _serialize_string(key) + ":" + canonicalize(value[key])
            for key in sorted(value)
        ) + "}"
    raise BundleVerificationError(f"unsupported bundle value: {type(value).__name__}")


def _decode_member(name: str, entry: dict[str, Any]) -> bytes:
    encoded = entry.get("content_base64")
    if not isinstance(encoded, str) or not encoded:
        raise BundleVerificationError(f"member {name}: missing content_base64")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise BundleVerificationError(f"member {name}: invalid content_base64") from exc
    expected_size = entry.get("byte_size")
    if not isinstance(expected_size, int) or expected_size < 0:
        raise BundleVerificationError(f"member {name}: invalid byte_size")
    if len(payload) != expected_size:
        raise BundleVerificationError(
            f"member {name}: byte_size {len(payload)} != {expected_size}"
        )
    expected_digest = entry.get("content_sha256")
    actual_digest = sha256_hex(payload)
    if actual_digest != expected_digest:
        raise BundleVerificationError(
            f"member {name}: sha256 {actual_digest} != {expected_digest}"
        )
    return payload


def verify_bundle(bundle_path: Path) -> dict[str, Any]:
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleVerificationError(f"cannot read bundle: {exc}") from exc
    if bundle.get("bundle_id") != "ai-bot-visual-knowledge.v1":
        raise BundleVerificationError("unexpected bundle_id")
    if bundle.get("bundle_revision") != 1:
        raise BundleVerificationError("unexpected bundle_revision")
    recorded = bundle.get("content_sha256")
    if not isinstance(recorded, str) or len(recorded) != 64:
        raise BundleVerificationError("bundle is missing a valid content_sha256")
    stripped = {key: value for key, value in bundle.items() if key != "content_sha256"}
    recomputed = sha256_hex(canonicalize(stripped).encode("utf-8"))
    if recomputed != recorded:
        raise BundleVerificationError(
            f"canonical digest mismatch: computed {recomputed} != recorded {recorded}"
        )
    files = bundle.get("files")
    if not isinstance(files, dict) or not files:
        raise BundleVerificationError("bundle.files must be a non-empty object")
    decoded: dict[str, bytes] = {}
    for name, entry in files.items():
        if not isinstance(name, str) or Path(name).name != name:
            raise BundleVerificationError(f"unsafe member name: {name!r}")
        if not isinstance(entry, dict):
            raise BundleVerificationError(f"member {name}: metadata is not an object")
        decoded[name] = _decode_member(name, entry)
    goldens = bundle.get("semantic_goldens")
    if not isinstance(goldens, list) or not goldens or not all(
        isinstance(value, str) and value for value in goldens
    ):
        raise BundleVerificationError("semantic_goldens must be a non-empty string array")
    return {
        "bundle_id": bundle["bundle_id"],
        "bundle_revision": bundle["bundle_revision"],
        "content_sha256": recorded,
        "verified_files": tuple(sorted(decoded)),
        "semantic_goldens": tuple(goldens),
        "decoded_files": decoded,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle",
        type=Path,
        default=Path(__file__).with_name("ai-bot-visual-knowledge-v1.bundle.json"),
    )
    args = parser.parse_args(argv)
    try:
        summary = verify_bundle(args.bundle)
    except BundleVerificationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        f"OK: {summary['bundle_id']} revision {summary['bundle_revision']} "
        f"digest {summary['content_sha256']} ({len(summary['verified_files'])} files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
