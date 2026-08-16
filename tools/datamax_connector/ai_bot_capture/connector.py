#!/usr/bin/env python3
"""Managed-process connector for immutable raw AI-BOT capture records."""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPHandler
from urllib.error import HTTPError, URLError

PROTOCOL = "managed_connector_process/v1"
KEY = "ai_bot_capture"
VERSION = "1.0.0"


class ConnectorError(RuntimeError):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code); self.code = code; self.retryable = retryable


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def validate_base_url(value: object) -> str:
    if not isinstance(value, str): raise ConnectorError("INVALID_CONFIGURATION")
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"} or parsed.port != 8792 or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ConnectorError("INVALID_CONFIGURATION")
    return value.rstrip("/")


class Transport:
    def __init__(self, base_url: str, auth_value: str):
        self.base_url = validate_base_url(base_url)
        if len(auth_value) < 24: raise ConnectorError("AUTHENTICATION_FAILED")
        self.auth_value = auth_value; self.opener = build_opener(HTTPHandler())

    def request(self, method: str, path: str, body=None, headers=None):
        request = Request(self.base_url + path, data=canonical(body) if body is not None else None, method=method, headers={"Authorization": f"Bearer {self.auth_value}", "Content-Type": "application/json", **dict(headers or {})})
        try:
            with self.opener.open(request, timeout=30) as response: return json.loads(response.read())
        except HTTPError as exc:
            if exc.code in {401, 403}: raise ConnectorError("AUTHENTICATION_FAILED") from exc
            if exc.code == 429: raise ConnectorError("RATE_LIMITED", True) from exc
            raise ConnectorError("REMOTE_UNAVAILABLE", exc.code >= 500) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc: raise ConnectorError("REMOTE_UNAVAILABLE", True) from exc


def execute(request: Mapping[str, Any], credentials: Mapping[str, str], transport=None) -> list[dict[str, Any]]:
    if request.get("protocol") != PROTOCOL or request.get("connector_key") != KEY or request.get("connector_version") != VERSION or request.get("operation") not in {"validate", "discover", "sample", "sync"}:
        raise ConnectorError("INVALID_CONFIGURATION")
    settings = request.get("settings")
    if not isinstance(settings, dict) or set(settings) != {"api_base_url"}: raise ConnectorError("INVALID_CONFIGURATION")
    client = transport or Transport(validate_base_url(settings["api_base_url"]), str(credentials.get("api_token", "")))
    request_id = str(request.get("request_id", "")); operation = request["operation"]
    if operation == "validate": return [{"protocol": PROTOCOL, "request_id": request_id, "seq": 1, "type": "complete", "complete": {"resources_emitted": 0, "items_emitted": 0}}]
    if operation == "discover": return [
        {"protocol": PROTOCOL, "request_id": request_id, "seq": 1, "type": "resource", "resource": {"id": "captures", "name": "AI-BOT 盒子图片", "type": "image_collection", "selectable": True}},
        {"protocol": PROTOCOL, "request_id": request_id, "seq": 2, "type": "complete", "complete": {"resources_emitted": 1, "items_emitted": 0}},
    ]
    if request.get("resource_id") != "captures" or not isinstance(request.get("limit"), int) or not 1 <= request["limit"] <= 500: raise ConnectorError("INVALID_CONFIGURATION")
    cursor = request.get("cursor") or {}
    if cursor and (not isinstance(cursor, dict) or set(cursor) != {"snapshot_id", "membership_digest", "lease_owner", "api_cursor", "total", "emitted"}): raise ConnectorError("INVALID_CONFIGURATION")
    if not cursor:
        lease_owner = f"managed:{request_id}"[:128]
        made = client.request("POST", "/api/internal/datamax/v1/captures/publication-snapshots", {"lease_owner": lease_owner})
        cursor = {"snapshot_id": made["snapshot_id"], "membership_digest": made["ordered_membership_digest"], "lease_owner": lease_owner, "api_cursor": "", "total": made["total"], "emitted": 0}
    query = urlencode({"limit": request["limit"], **({"cursor": cursor["api_cursor"]} if cursor["api_cursor"] else {})})
    page = client.request("GET", f"/api/internal/datamax/v1/captures/publication-snapshots/{cursor['snapshot_id']}/items?{query}", headers={"X-Lease-Owner": cursor["lease_owner"]})
    if page.get("ordered_membership_digest") != cursor["membership_digest"]: raise ConnectorError("REMOTE_UNAVAILABLE")
    events=[]; seen=set(); seq=1
    forbidden = {"human_truth", "eligibility", "ai_original", "secondary_observations", "decision"}
    for item in page.get("items", []):
        if not isinstance(item, dict) or forbidden.intersection(item): raise ConnectorError("REMOTE_UNAVAILABLE")
        item_id=item.get("item_id"); revision=item.get("capture_revision")
        if not isinstance(item_id, str) or not isinstance(revision, int): raise ConnectorError("REMOTE_UNAVAILABLE")
        external_id=f"capture:{item_id}:{revision}"
        if external_id in seen: raise ConnectorError("REMOTE_UNAVAILABLE")
        seen.add(external_id)
        events.append({"protocol": PROTOCOL, "request_id": request_id, "seq": seq, "type": "item", "item": {"external_id": external_id, "title": f"capture {item_id} r{revision}", "content_type": "application/json", "content_base64": base64.b64encode(canonical(item)).decode(), "metadata": {"source_locator": f"aibot-capture://{item_id}/{revision}"}}}); seq += 1
    emitted=int(cursor["emitted"])+len(events); next_api=page.get("next_cursor") or ""
    complete={"resources_emitted": 0, "items_emitted": len(events)}
    if next_api: complete["next_cursor"]={**cursor, "api_cursor": next_api, "emitted": emitted}
    elif emitted != int(cursor["total"]): raise ConnectorError("REMOTE_UNAVAILABLE")
    events.append({"protocol": PROTOCOL, "request_id": request_id, "seq": seq, "type": "complete", "complete": complete}); return events


def main() -> int:
    request_id="invalid"
    try:
        request=json.load(sys.stdin); request_id=str(request.get("request_id", "invalid"))
        with os.fdopen(3, "r", encoding="utf-8", closefd=False) as handle: credentials=json.load(handle)
        for event in execute(request, credentials): print(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        return 0
    except ConnectorError as exc:
        print(json.dumps({"protocol": PROTOCOL, "request_id": request_id, "seq": 1, "type": "error", "error": {"code": exc.code, "retryable": exc.retryable}}, separators=(",", ":"))); return 1


if __name__ == "__main__": raise SystemExit(main())
