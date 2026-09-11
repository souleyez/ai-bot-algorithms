#!/usr/bin/env python3
"""Collect a sanitized, read-only gateway/channel/capture dashboard snapshot."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko

logging.getLogger("paramiko").setLevel(logging.CRITICAL)


DEFAULT_INVENTORY = Path("/srv/ai-bot-algorithm-platform/devices.json")
DEFAULT_OUTPUT = Path("/srv/ai-bot-sample-review/data/gateway-dashboard.json")
KNOWN_ALGORITHMS = {
    101: "画面位移/状态巡检",
    103: "新世界工服识别",
    104: "外卖服识别",
}


REMOTE_SCRIPT = r'''
import json
import sqlite3
import time


def rows(path, query, params=()):
    try:
        db = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        result = [dict(row) for row in db.execute(query, params)]
        db.close()
        return result, ""
    except Exception as exc:
        return [], type(exc).__name__


channels, channel_error = rows(
    "/oem/smart-gw/db/dmg.db",
    "SELECT chNo, location, desc, modelEnable, isEnable, status, switch "
    "FROM channels ORDER BY chNo",
)
bindings, binding_error = rows(
    "/oem/smart-gw/db/dmg.db",
    "SELECT chNo, modelId FROM channel_ai_models ORDER BY chNo, modelId",
)
models, model_error = rows(
    "/oem/smart-gw/db/dmg.db",
    "SELECT modelId, name, version, status, confidenceThreshold FROM ai_models ORDER BY modelId",
)
now = int(time.time())
captures, capture_error = rows(
    "/oem/smart-gw/db/snap.db",
    "SELECT chNo, geid, COUNT(*) AS total, "
    "SUM(CASE WHEN timeStamp >= ? THEN 1 ELSE 0 END) AS last24h, "
    "MAX(timeStamp) AS lastCapture FROM ch_g_imgs GROUP BY chNo, geid ORDER BY chNo, geid",
    (now - 86400,),
)
trend, trend_error = rows(
    "/oem/smart-gw/db/snap.db",
    "SELECT date(timeStamp, 'unixepoch', 'localtime') AS day, COUNT(*) AS count "
    "FROM ch_g_imgs WHERE timeStamp >= ? GROUP BY day ORDER BY day",
    (now - 7 * 86400,),
)
print(json.dumps({
    "channels": channels,
    "bindings": bindings,
    "models": models,
    "captures": captures,
    "trend": trend,
    "errors": {
        "channels": channel_error,
        "bindings": binding_error,
        "models": model_error,
        "captures": capture_error,
        "trend": trend_error,
    },
}, ensure_ascii=False))
'''


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def credentials() -> tuple[str, str]:
    user = os.environ.get("AI_BOT_DEVICE_SSH_USER", "").strip()
    password = os.environ.get("AI_BOT_DEVICE_SSH_PASSWORD", "").strip()
    if not user or not password:
        raise RuntimeError("device credentials are missing")
    return user, password


def algorithm_name(model_id: int, models: dict[int, dict[str, Any]]) -> str:
    model = models.get(model_id, {})
    return str(model.get("name") or KNOWN_ALGORITHMS.get(model_id) or f"算法 m{model_id}")


def normalize_device(device: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    model_rows = {
        int(row["modelId"]): row for row in raw.get("models", []) if row.get("modelId") is not None
    }
    capture_rows = {
        (int(row["chNo"]), int(row["geid"])): row
        for row in raw.get("captures", [])
        if row.get("chNo") is not None and row.get("geid") is not None
    }
    bindings: dict[int, set[int]] = {}
    for row in raw.get("bindings", []):
        if row.get("chNo") is None or row.get("modelId") is None:
            continue
        bindings.setdefault(int(row["chNo"]), set()).add(int(row["modelId"]))
    capture_data_available = not bool((raw.get("errors") or {}).get("captures"))
    channels = []
    seen_captures: set[tuple[int, int]] = set()
    for row in raw.get("channels", []):
        channel_no = int(row.get("chNo") or 0)
        algorithms = []
        for model_id in sorted(bindings.get(channel_no, set())):
            capture = capture_rows.get((channel_no, model_id), {})
            seen_captures.add((channel_no, model_id))
            algorithms.append(
                {
                    "modelId": model_id,
                    "slot": f"m{model_id}",
                    "name": algorithm_name(model_id, model_rows),
                    "version": str(model_rows.get(model_id, {}).get("version") or ""),
                    "captures": int(capture.get("total") or 0) if capture_data_available else None,
                    "captures24h": int(capture.get("last24h") or 0) if capture_data_available else None,
                    "lastCapture": int(capture.get("lastCapture") or 0),
                }
            )
        channels.append(
            {
                "channel": channel_no,
                "location": str(row.get("location") or row.get("desc") or ""),
                "reportedStatus": row.get("status"),
                "enabled": row.get("isEnable"),
                "switch": row.get("switch"),
                "algorithms": algorithms,
            }
        )
    for key, capture in capture_rows.items():
        if key in seen_captures:
            continue
        channel_no, model_id = key
        channel = next((item for item in channels if item["channel"] == channel_no), None)
        if channel is None:
            channel = {
                "channel": channel_no,
                "location": "",
                "reportedStatus": None,
                "enabled": None,
                "switch": None,
                "algorithms": [],
            }
            channels.append(channel)
        channel["algorithms"].append(
            {
                "modelId": model_id,
                "slot": f"m{model_id}",
                "name": algorithm_name(model_id, model_rows),
                "version": str(model_rows.get(model_id, {}).get("version") or ""),
                "captures": int(capture.get("total") or 0) if capture_data_available else None,
                "captures24h": int(capture.get("last24h") or 0) if capture_data_available else None,
                "lastCapture": int(capture.get("lastCapture") or 0),
            }
        )
    channels.sort(key=lambda item: item["channel"])
    algorithms = [algorithm for channel in channels for algorithm in channel["algorithms"]]
    return {
        "id": str(device.get("id") or ""),
        "displayId": str(device.get("display_id") or ""),
        "deviceFamily": str(device.get("device_family") or "AI-BOT"),
        "chipFamily": str(device.get("chip_family") or "unknown"),
        "tags": [str(tag) for tag in device.get("tags", [])],
        "reachable": True,
        "accessMode": str(raw.get("accessMode") or "ssh"),
        "collectedAt": utc_now(),
        "errors": {key: value for key, value in raw.get("errors", {}).items() if value},
        "channels": channels,
        "summary": {
            "channels": len(channels),
            "reportedOnline": sum(1 for channel in channels if channel["reportedStatus"] == 1),
            "algorithmBindings": len(algorithms),
            "captureDataAvailable": capture_data_available,
            "captures": sum(int(algorithm["captures"] or 0) for algorithm in algorithms),
            "captures24h": sum(int(algorithm["captures24h"] or 0) for algorithm in algorithms),
            "lastCapture": max((algorithm["lastCapture"] for algorithm in algorithms), default=0),
        },
        "trend": [
            {"day": str(row.get("day") or ""), "count": int(row.get("count") or 0)}
            for row in raw.get("trend", [])
            if row.get("day")
        ],
    }


def collect_web(device: dict[str, Any], ssh_error: str) -> dict[str, Any]:
    base = f"http://{device['web_host']}:{int(device['web_port'])}"

    def get(path: str) -> Any:
        request = urllib.request.Request(base + path, headers={"User-Agent": "ai-bot-dashboard/1"})
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    channels_payload = get("/api/v1/system/channels/mag")
    channel_rows = channels_payload.get("result") or []
    channels = []
    bindings = []
    model_names: dict[int, dict[str, Any]] = {}
    for row in channel_rows:
        channels.append(
            {
                "chNo": row.get("chNo"),
                "location": row.get("location") or "",
                "desc": row.get("desc") or "",
                "modelEnable": row.get("aiDetectSwitch"),
                "isEnable": row.get("switch"),
                "status": row.get("status"),
                "switch": row.get("switch"),
            }
        )
        for model in row.get("models") or []:
            if model.get("id") is None:
                continue
            model_id = int(model["id"])
            bindings.append({"chNo": row.get("chNo"), "modelId": model_id})
            model_names[model_id] = {"modelId": model_id, "name": model.get("name") or ""}
    try:
        engines_payload = get("/api/v1/algorithm/engine")
        for engine in (engines_payload.get("result") or {}).get("engines") or []:
            if engine.get("geid") is None:
                continue
            model_id = int(engine["geid"])
            model_names[model_id] = {
                "modelId": model_id,
                "name": engine.get("name") or model_names.get(model_id, {}).get("name") or "",
                "version": engine.get("version") or "",
                "status": engine.get("isRunning"),
            }
    except Exception:
        pass
    return normalize_device(
        device,
        {
            "accessMode": "web",
            "channels": channels,
            "bindings": bindings,
            "models": list(model_names.values()),
            "captures": [],
            "trend": [],
            "errors": {"captures": "ssh_unavailable", "trend": "ssh_unavailable", "ssh": ssh_error},
        },
    )


def collect(device: dict[str, Any], user: str, password: str) -> dict[str, Any]:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=str(device["ssh_host"]),
            port=int(device["ssh_port"]),
            username=user,
            password=password,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )
        encoded = base64.b64encode(REMOTE_SCRIPT.encode("utf-8")).decode("ascii")
        _, stdout, stderr = client.exec_command(
            f"printf '%s' '{encoded}' | base64 -d | python3", timeout=45
        )
        output = stdout.read()
        error = stderr.read().decode("utf-8", "replace").strip()
        status = stdout.channel.recv_exit_status()
        if status != 0:
            raise RuntimeError(f"remote probe exit {status}: {error[-160:]}")
        return normalize_device(device, json.loads(output.decode("utf-8")))
    except Exception as exc:
        ssh_error = type(exc).__name__
        try:
            return collect_web(device, ssh_error)
        except Exception:
            pass
        return {
            "id": str(device.get("id") or ""),
            "displayId": str(device.get("display_id") or ""),
            "deviceFamily": str(device.get("device_family") or "AI-BOT"),
            "chipFamily": str(device.get("chip_family") or "unknown"),
            "tags": [str(tag) for tag in device.get("tags", [])],
            "reachable": False,
            "accessMode": "none",
            "collectedAt": utc_now(),
            "error": type(exc).__name__,
            "channels": [],
            "summary": {
                "channels": 0,
                "reportedOnline": 0,
                "algorithmBindings": 0,
                "captureDataAvailable": False,
                "captures": 0,
                "captures24h": 0,
                "lastCapture": 0,
            },
            "trend": [],
        }
    finally:
        client.close()


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    devices = list(inventory.get("devices") or [])
    user, password = credentials()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as executor:
        futures = {executor.submit(collect, device, user, password): device for device in devices}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: (not item["reachable"], item["displayId"]))
    payload = {
        "schema": "ai-bot.gateway-dashboard.v1",
        "generatedAt": utc_now(),
        "catalogCount": len(devices),
        "devices": results,
    }
    write_atomic(args.output, payload)
    print(json.dumps({"ok": True, "devices": len(results)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
