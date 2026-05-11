#!/usr/bin/env python3
"""Small HTTP API for AI-BOT algorithm release commands.

Environment:

- AI_BOT_PLATFORM_RUNTIME: runtime directory with catalog.json and artifacts/
- AI_BOT_RELEASE_API_TOKEN: bearer token for non-health endpoints
- AI_BOT_DEVICE_SSH_USER / AI_BOT_DEVICE_SSH_PASSWORD: device SSH credential
"""

from __future__ import annotations

import json
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import release_worker


RUNTIME = Path(os.environ.get("AI_BOT_PLATFORM_RUNTIME", release_worker.DEFAULT_RUNTIME)).expanduser().resolve()


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AiBotAlgorithmPlatform/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def require_auth(self) -> bool:
        token = os.environ.get("AI_BOT_RELEASE_API_TOKEN")
        token = token.strip() if token else token
        if not token:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "ServerMissingToken"})
            return False
        header = self.headers.get("Authorization", "")
        if header != f"Bearer {token}":
            json_response(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized"})
            return False
        return True

    def read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 1024 * 1024:
            raise release_worker.PlatformError("Request body too large")
        raw = self.rfile.read(length).decode("utf-8")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise release_worker.PlatformError(f"Invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise release_worker.PlatformError("JSON body must be an object")
        return data

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/health":
                json_response(self, HTTPStatus.OK, {"ok": True, "runtime": str(RUNTIME)})
                return
            if not self.require_auth():
                return
            if parsed.path == "/api/ai-bot/devices":
                catalog = release_worker.load_catalog(RUNTIME)
                json_response(self, HTTPStatus.OK, {"ok": True, "devices": catalog.get("devices", [])})
                return
            if parsed.path == "/api/ai-bot/algorithms":
                catalog = release_worker.load_catalog(RUNTIME)
                json_response(self, HTTPStatus.OK, {"ok": True, "artifacts": catalog.get("artifacts", [])})
                return
            if parsed.path == "/api/ai-bot/releases":
                json_response(self, HTTPStatus.OK, {"ok": True, "jobs": release_worker.list_jobs(RUNTIME)})
                return
            match = re.fullmatch(r"/api/ai-bot/releases/([^/]+)", parsed.path)
            if match:
                path = release_worker.job_path(RUNTIME, match.group(1))
                if not path.exists():
                    json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "NotFound"})
                    return
                json_response(self, HTTPStatus.OK, {"ok": True, "job": release_worker.read_json(path)})
                return
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "NotFound"})
        except release_worker.PlatformError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": type(exc).__name__, "message": str(exc)})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": type(exc).__name__, "message": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not self.require_auth():
                return
            if parsed.path == "/api/ai-bot/releases":
                payload = self.read_body()
                job = release_worker.build_job(RUNTIME, payload)
                json_response(self, HTTPStatus.OK, {"ok": True, "job": job})
                return
            match = re.fullmatch(r"/api/ai-bot/releases/([^/]+)/approve", parsed.path)
            if match:
                job = release_worker.approve_job(RUNTIME, match.group(1))
                json_response(self, HTTPStatus.OK, {"ok": True, "job": job})
                return
            json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "NotFound"})
        except release_worker.PlatformError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": type(exc).__name__, "message": str(exc)})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": type(exc).__name__, "message": str(exc)})


def main() -> int:
    host = os.environ.get("AI_BOT_PLATFORM_HOST", "127.0.0.1").strip()
    port = int(os.environ.get("AI_BOT_PLATFORM_PORT", "8791").strip())
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"AI-BOT algorithm platform API listening on http://{host}:{port}")
    print(f"Runtime: {RUNTIME}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
