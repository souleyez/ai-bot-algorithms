#!/usr/bin/env python3
"""Managed-process connector for immutable AI-BOT human review facts."""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, build_opener, HTTPHandler

PROTOCOL = "managed_connector_process/v1"
KEY = "ai_bot_review"
VERSION = "1.0.0"
MAX_LIMIT = 500


class ConnectorError(RuntimeError):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_base_url(value: object) -> str:
    if not isinstance(value, str):
        raise ConnectorError("INVALID_CONFIGURATION")
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ConnectorError("INVALID_CONFIGURATION")
    if parsed.port != 8792 or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ConnectorError("INVALID_CONFIGURATION")
    return value.rstrip("/")


class Transport:
    def __init__(self, base_url: str, auth_value: str):
        self.base_url = validate_base_url(base_url)
        if not isinstance(auth_value, str) or len(auth_value) < 24:
            raise ConnectorError("AUTHENTICATION_FAILED")
        self.auth_value = auth_value
        self.opener = build_opener(HTTPHandler())

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> Any:
        data = canonical(body) if body is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.auth_value}",
                "Content-Type": "application/json",
                **dict(headers or {}),
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise ConnectorError("AUTHENTICATION_FAILED") from exc
            if exc.code == 404:
                raise ConnectorError("RESOURCE_NOT_FOUND") from exc
            if exc.code == 429:
                raise ConnectorError("RATE_LIMITED", True) from exc
            raise ConnectorError("REMOTE_UNAVAILABLE", exc.code >= 500) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ConnectorError("REMOTE_UNAVAILABLE", True) from exc


def validate_request(request: Mapping[str, Any]) -> tuple[str, str]:
    if (
        request.get("protocol") != PROTOCOL
        or request.get("connector_key") != KEY
        or request.get("connector_version") != VERSION
        or request.get("operation") not in {"validate", "discover", "sample", "sync"}
    ):
        raise ConnectorError("INVALID_CONFIGURATION")
    settings = request.get("settings")
    if not isinstance(settings, dict) or set(settings) != {"algorithm_key", "api_base_url"}:
        raise ConnectorError("INVALID_CONFIGURATION")
    algorithm = settings.get("algorithm_key")
    if not isinstance(algorithm, str) or not algorithm or len(algorithm) > 64:
        raise ConnectorError("INVALID_CONFIGURATION")
    return algorithm, validate_base_url(settings.get("api_base_url"))


def execute(request: Mapping[str, Any], credentials: Mapping[str, str], transport: Any | None = None) -> list[dict[str, Any]]:
    algorithm, base_url = validate_request(request)
    credential_value = credentials.get("api_token") if isinstance(credentials, Mapping) else None
    client = transport or Transport(base_url, str(credential_value or ""))
    request_id = str(request.get("request_id", ""))
    operation = request["operation"]
    seq = 1
    events: list[dict[str, Any]] = []
    algorithms = client.request("GET", "/api/internal/datamax/v1/algorithms")
    accepted = {
        item["algorithm_key"]: item
        for item in algorithms.get("algorithms", [])
        if item.get("onboarding_state") == "accepted"
    }
    if algorithm not in accepted:
        raise ConnectorError("RESOURCE_NOT_FOUND")
    if operation == "validate":
        return [{"protocol": PROTOCOL, "request_id": request_id, "seq": 1, "type": "complete", "complete": {"resources_emitted": 0, "items_emitted": 0}}]
    if operation == "discover":
        events.append({"protocol": PROTOCOL, "request_id": request_id, "seq": seq, "type": "resource", "resource": {"id": algorithm, "name": accepted[algorithm]["display_name"], "type": "review_truth", "selectable": True}})
        return events + [{"protocol": PROTOCOL, "request_id": request_id, "seq": 2, "type": "complete", "complete": {"resources_emitted": 1, "items_emitted": 0}}]
    if request.get("resource_id") != algorithm:
        raise ConnectorError("RESOURCE_NOT_FOUND")
    limit = request.get("limit")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ConnectorError("INVALID_CONFIGURATION")
    cursor = request.get("cursor") or {}
    if not isinstance(cursor, dict):
        raise ConnectorError("INVALID_CONFIGURATION")
    if cursor:
        required = {"snapshot_id", "membership_digest", "lease_owner", "api_cursor", "total", "emitted"}
        if set(cursor) != required:
            raise ConnectorError("INVALID_CONFIGURATION")
        snapshot = cursor
    else:
        lease_owner = f"managed:{request_id}"[:128]
        created = client.request(
            "POST",
            f"/api/internal/datamax/v1/algorithms/{quote(algorithm)}/publication-snapshots",
            {"lease_owner": lease_owner},
        )
        snapshot = {
            "snapshot_id": created["snapshot_id"],
            "membership_digest": created["ordered_membership_digest"],
            "lease_owner": lease_owner,
            "api_cursor": "",
            "total": created["total"],
            "emitted": 0,
        }
    query = urlencode({"limit": limit, **({"cursor": snapshot["api_cursor"]} if snapshot["api_cursor"] else {})})
    page = client.request(
        "GET",
        f"/api/internal/datamax/v1/algorithms/{quote(algorithm)}/publication-snapshots/{quote(snapshot['snapshot_id'])}/review-facts?{query}",
        headers={"X-Lease-Owner": snapshot["lease_owner"]},
    )
    if page.get("ordered_membership_digest") != snapshot["membership_digest"]:
        raise ConnectorError("REMOTE_UNAVAILABLE")
    seen: set[str] = set()
    for fact in page.get("items", []):
        if not isinstance(fact, dict) or fact.get("algorithm_key") != algorithm:
            raise ConnectorError("REMOTE_UNAVAILABLE")
        item_id = fact.get("item_id")
        revision = fact.get("review_revision")
        if not isinstance(item_id, str) or not isinstance(revision, int):
            raise ConnectorError("REMOTE_UNAVAILABLE")
        external_id = f"review:{algorithm}:{item_id}"
        if external_id in seen:
            raise ConnectorError("REMOTE_UNAVAILABLE")
        seen.add(external_id)
        content = canonical(fact)
        events.append({
            "protocol": PROTOCOL, "request_id": request_id, "seq": seq, "type": "item",
            "item": {
                "external_id": external_id,
                "title": f"{algorithm} {item_id} r{revision}",
                "content_type": "application/json",
                "content_base64": base64.b64encode(content).decode("ascii"),
                "metadata": {
                    "source_locator": f"aibot-review://{algorithm}/{item_id}/{revision}",
                    "review_revision": str(revision),
                },
            },
        })
        seq += 1
    emitted = int(snapshot["emitted"]) + len(events)
    next_api = page.get("next_cursor") or ""
    completion: dict[str, Any] = {"resources_emitted": 0, "items_emitted": len(events)}
    if next_api:
        completion["next_cursor"] = {**snapshot, "api_cursor": next_api, "emitted": emitted}
    elif emitted != int(snapshot["total"]):
        raise ConnectorError("REMOTE_UNAVAILABLE")
    events.append({"protocol": PROTOCOL, "request_id": request_id, "seq": seq, "type": "complete", "complete": completion})
    return events


def _read_credentials() -> dict[str, str]:
    try:
        with os.fdopen(3, "r", encoding="utf-8", closefd=False) as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorError("AUTHENTICATION_FAILED") from exc
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    request_id = "invalid"
    try:
        request = json.load(sys.stdin)
        request_id = str(request.get("request_id", "invalid")) if isinstance(request, dict) else "invalid"
        for event in execute(request, _read_credentials()):
            sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 0
    except ConnectorError as exc:
        event = {"protocol": PROTOCOL, "request_id": request_id, "seq": 1, "type": "error", "error": {"code": exc.code, "retryable": exc.retryable}}
        sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
