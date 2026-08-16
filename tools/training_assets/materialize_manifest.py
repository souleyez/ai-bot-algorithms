#!/usr/bin/env python3
"""Materialize one approved DataMax manifest without reading mutable review state."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


MAX_MEMBERS = 100_000
MAX_JSON = 32 * 1024 * 1024
MAX_IMAGE = 50 * 1024 * 1024


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class MaterializationError(RuntimeError):
    pass


class Client(Protocol):
    def json(self, base: str, path: str, token: str) -> tuple[dict[str, Any], dict[str, str]]: ...
    def binary(self, base: str, path: str, token: str) -> tuple[bytes, dict[str, str]]: ...


class HTTPClient:
    def _get(self, base: str, path: str, token: str) -> tuple[bytes, dict[str, str]]:
        url = base.rstrip("/") + path
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(MAX_IMAGE + 1)
            if len(body) > MAX_IMAGE:
                raise MaterializationError("response exceeds bound")
            return body, {key.lower(): value for key, value in response.headers.items()}

    def json(self, base: str, path: str, token: str) -> tuple[dict[str, Any], dict[str, str]]:
        body, headers = self._get(base, path, token)
        if len(body) > MAX_JSON or "application/json" not in headers.get("content-type", ""):
            raise MaterializationError("invalid JSON response")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise MaterializationError("JSON object required")
        return value, headers

    def binary(self, base: str, path: str, token: str) -> tuple[bytes, dict[str, str]]:
        return self._get(base, path, token)


@dataclass(frozen=True)
class Request:
    platform_endpoint: str
    review_endpoint: str
    proposal_id: str
    proposal_revision: int
    manifest_digest: str
    platform_token: str
    review_token: str


def _query(request: Request, suffix: str, extra: dict[str, str] | None = None) -> str:
    values = {"proposal_revision": str(request.proposal_revision), "manifest_digest": request.manifest_digest}
    values.update(extra or {})
    proposal = urllib.parse.quote(request.proposal_id, safe="")
    return f"/internal/ai-bot/training-proposals/{proposal}/{suffix}?{urllib.parse.urlencode(values)}"


def _members(client: Client, request: Request) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata, _ = client.json(request.platform_endpoint, _query(request, "manifest"), request.platform_token)
    members: list[dict[str, Any]] = []
    cursor = ""
    while True:
        extra = {"limit": "1000"}
        if cursor:
            extra["cursor"] = cursor
        page, _ = client.json(request.platform_endpoint, _query(request, "members", extra), request.platform_token)
        if page.get("manifest_digest") != request.manifest_digest or not isinstance(page.get("items"), list):
            raise MaterializationError("member page is not bound to manifest")
        for member in page["items"]:
            if not isinstance(member, dict) or member.get("ordinal") != len(members):
                raise MaterializationError("manifest ordinals are not contiguous")
            members.append(member)
            if len(members) > MAX_MEMBERS:
                raise MaterializationError("manifest member bound exceeded")
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            raise MaterializationError("invalid member cursor")
        cursor = next_cursor
    document = dict(metadata)
    document["members"] = members
    without_digest = canonical_json(document)
    if sha256(without_digest) != request.manifest_digest:
        raise MaterializationError("manifest digest mismatch")
    document["content_sha256"] = request.manifest_digest
    return document, members


def _validate_mapping(manifest: dict[str, Any], mapping: dict[str, int]) -> None:
    if not mapping or any(not isinstance(key, str) or not key or not isinstance(value, int) or value < 0 for key, value in mapping.items()):
        raise MaterializationError("invalid class mapping")
    if len(set(mapping.values())) != len(mapping):
        raise MaterializationError("class mapping indices must be unique")
    if sha256(canonical_json(mapping)) != manifest.get("class_mapping_digest"):
        raise MaterializationError("class mapping digest mismatch")


def _review_paths(request: Request, algorithm: str, member: dict[str, Any]) -> tuple[str, str]:
    quoted = [urllib.parse.quote(str(value), safe="") for value in (algorithm, member["item_id"])]
    prefix = f"/api/internal/training-assets/v1/algorithms/{quoted[0]}/items/{quoted[1]}/revisions/{member['review_revision']}"
    return prefix + "/review-fact", prefix + "/original"


def _label_bytes(manifest: dict[str, Any], fact: dict[str, Any], mapping: dict[str, int]) -> bytes:
    truth = fact.get("human_truth")
    if not isinstance(truth, dict) or truth.get("decision") not in {"positive", "negative"}:
        raise MaterializationError("only final human truth is trainable")
    labels = truth.get("label_keys")
    boxes = truth.get("boxes")
    if not isinstance(labels, list) or not isinstance(boxes, list):
        raise MaterializationError("invalid human truth")
    if any(label not in mapping for label in labels):
        raise MaterializationError("human label is absent from class mapping")
    if manifest.get("annotation_contract") == "classification.v1":
        if boxes or (truth["decision"] == "positive" and not labels):
            raise MaterializationError("invalid classification truth")
        return (" ".join(str(mapping[label]) for label in sorted(labels)) + "\n").encode("ascii") if labels else b""
    if manifest.get("annotation_contract") != "bbox.v1":
        raise MaterializationError("unsupported annotation contract")
    lines: list[str] = []
    for box in boxes:
        if not isinstance(box, dict) or box.get("label_key") not in mapping:
            raise MaterializationError("invalid human box")
        values = [box.get(axis) for axis in ("x", "y", "w", "h")]
        if any(not isinstance(value, (int, float)) for value in values):
            raise MaterializationError("invalid human box geometry")
        x, y, width, height = (float(value) for value in values)
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise MaterializationError("human box is outside normalized bounds")
        lines.append(f"{mapping[box['label_key']]} {x + width / 2:.8f} {y + height / 2:.8f} {width:.8f} {height:.8f}")
    if truth["decision"] == "positive" and not lines:
        raise MaterializationError("positive detection has no human box")
    return (("\n".join(lines) + "\n") if lines else "").encode("ascii")


def materialize(client: Client, request: Request, output: Path, mapping: dict[str, int]) -> dict[str, Any]:
    if output.exists() or request.proposal_revision < 1 or len(request.manifest_digest) != 64:
        raise MaterializationError("invalid or existing output")
    manifest, members = _members(client, request)
    if manifest.get("manifest_state") != "frozen" or manifest.get("machine_observation_policy") != "human_truth_only":
        raise MaterializationError("manifest is not human-truth-only frozen input")
    _validate_mapping(manifest, mapping)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    seen_groups: dict[str, str] = {}
    try:
        records: list[dict[str, Any]] = []
        for member in members:
            split = member.get("split")
            group = member.get("capture_group_id")
            if split not in {"train", "validation", "test", "regression"} or not isinstance(group, str):
                raise MaterializationError("invalid split membership")
            if group in seen_groups and seen_groups[group] != split:
                raise MaterializationError("capture group crosses splits")
            seen_groups[group] = split
            fact_path, image_path = _review_paths(request, str(manifest["algorithm_key"]), member)
            fact, fact_headers = client.json(request.review_endpoint, fact_path, request.review_token)
            fact_digest = fact_headers.get("x-review-fact-sha256", "")
            # Canonical review-fact digest excludes its own content_sha256 field.
            fact_without_digest = dict(fact)
            declared_digest = fact_without_digest.pop("content_sha256", None)
            if fact_digest != member.get("review_fact_digest") or declared_digest != fact_digest or sha256(canonical_json(fact_without_digest)) != fact_digest:
                raise MaterializationError("review fact digest mismatch")
            if fact.get("algorithm_key") != manifest.get("algorithm_key") or fact.get("item_id") != member.get("item_id") or fact.get("review_revision") != member.get("review_revision"):
                raise MaterializationError("review fact locator mismatch")
            image, image_headers = client.binary(request.review_endpoint, image_path, request.review_token)
            if len(image) < 1 or len(image) > MAX_IMAGE or sha256(image) != member.get("image_sha256") or image_headers.get("x-content-sha256") != member.get("image_sha256"):
                raise MaterializationError("image digest mismatch")
            mime = image_headers.get("content-type", "").split(";", 1)[0]
            extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime)
            if extension is None:
                raise MaterializationError("unsupported image MIME")
            labels = _label_bytes(manifest, fact, mapping)
            base = f"{member['ordinal']:06d}-{member['item_id']}"
            image_rel = Path("images") / member["split"] / f"{base}{extension}"
            label_rel = Path("labels") / member["split"] / f"{base}.txt"
            (temporary / image_rel).parent.mkdir(parents=True, exist_ok=True)
            (temporary / label_rel).parent.mkdir(parents=True, exist_ok=True)
            (temporary / image_rel).write_bytes(image)
            (temporary / label_rel).write_bytes(labels)
            records.append({"ordinal": member["ordinal"], "item_id": member["item_id"], "review_revision": member["review_revision"], "review_fact_digest": fact_digest, "split": member["split"], "image": image_rel.as_posix(), "image_sha256": sha256(image), "labels": label_rel.as_posix(), "labels_sha256": sha256(labels)})
        index_bytes = canonical_json({"manifest_digest": request.manifest_digest, "members": records}) + b"\n"
        (temporary / "index.json").write_bytes(index_bytes)
        receipt = {"schema_version": "ai-bot-training-materialization-receipt.v1", "proposal_id": request.proposal_id, "proposal_revision": request.proposal_revision, "manifest_digest": request.manifest_digest, "class_mapping_digest": manifest["class_mapping_digest"], "index_sha256": sha256(index_bytes), "member_count": len(records)}
        (temporary / "receipt.json").write_bytes(canonical_json(receipt) + b"\n")
        os.replace(temporary, output)
        return receipt
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _token(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32 or "\n" in value or "\r" in value:
        raise MaterializationError("invalid token file")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-endpoint", required=True)
    parser.add_argument("--review-endpoint", required=True)
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--proposal-revision", type=int, required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--platform-token-file", type=Path, required=True)
    parser.add_argument("--review-token-file", type=Path, required=True)
    parser.add_argument("--class-mapping", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    mapping = json.loads(args.class_mapping.read_text(encoding="utf-8"))
    materialize(HTTPClient(), Request(args.platform_endpoint, args.review_endpoint, args.proposal_id, args.proposal_revision, args.manifest_digest, _token(args.platform_token_file), _token(args.review_token_file)), args.output, mapping)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
