#!/usr/bin/env python3
"""Read-only collection of AI box metadata needed to replay reviewed reports."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any

import paramiko


DEFAULT_PLATFORM_ROOT = Path("/srv/ai-bot-algorithm-platform")
TARGET_IDS = ("61672", "61863")


REMOTE_SCRIPT = r'''
import json
import sqlite3
import urllib.request


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except Exception as exc:
        return {"error": type(exc).__name__}


def channel_metadata():
    with urllib.request.urlopen(
        "http://127.0.0.1/api/v1/system/channels/mag", timeout=10
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = []
    for item in payload.get("result") or []:
        rows.append(
            {
                "chNo": item.get("chNo"),
                "location": item.get("location") or "",
                "desc": item.get("desc") or "",
                "ip": item.get("ip") or item.get("ipAddr") or "",
                "sn": item.get("sn") or "",
                "sn32": item.get("sn32") or "",
                "status": item.get("status"),
                "switch": item.get("switch"),
            }
        )
    return rows


def database_schema(path):
    result = {"path": path, "tables": []}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        for table in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' ORDER BY name"
        ):
            columns = [
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(" + json.dumps(table["name"]) + ")"
                )
            ]
            result["tables"].append(
                {"name": table["name"], "columns": columns, "sql": table["sql"]}
            )
        connection.close()
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    return result


def select_rows(path, query, parameters=()):
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(query, parameters)]
        connection.close()
        return rows
    except Exception as exc:
        return [{"error": type(exc).__name__ + ": " + str(exc)}]


print(
    json.dumps(
        {
            "channels": channel_metadata(),
            "models": {
                "m103": {
                    "nn": read_json("/models/m103/nn.json"),
                    "extend": read_json("/models/m103/nn.extend.json"),
                },
                "m104": {
                    "nn": read_json("/models/m104/nn.json"),
                    "extend": read_json("/models/m104/nn.extend.json"),
                },
            },
            "databases": {
                "dmg": database_schema("/oem/smart-gw/db/dmg.db"),
                "snap": database_schema("/oem/smart-gw/db/snap.db"),
            },
            "algorithm_registry": select_rows(
                "/oem/smart-gw/db/dmg.db",
                "SELECT id, name, version, status, modelId, confidenceThreshold, "
                "modelParam, modelFnTypes FROM ai_models "
                "WHERE modelId IN (103, 104) ORDER BY modelId, id",
            ),
            "algorithm_classes": select_rows(
                "/oem/smart-gw/db/dmg.db",
                "SELECT id, chNo, modelId, channelAiModelId, classId "
                "FROM channel_ai_model_classes "
                "WHERE modelId IN (103, 104) ORDER BY modelId, chNo, id",
            ),
            "capture_rows": select_rows(
                "/oem/smart-gw/db/snap.db",
                "SELECT id, chNo, flagID, geid, picName, spicName, cid, cname, "
                "detects, timeStamp, timeStampStr FROM ch_g_imgs "
                "WHERE geid IN (103, 104) ORDER BY timeStamp, id",
            ),
        },
        ensure_ascii=False,
    )
)
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-root", type=Path, default=DEFAULT_PLATFORM_ROOT)
    parser.add_argument("--device", action="append", choices=TARGET_IDS)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def credentials() -> tuple[str, str]:
    user = os.environ.get("AI_BOT_DEVICE_SSH_USER", "").strip()
    password = os.environ.get("AI_BOT_DEVICE_SSH_PASSWORD", "").strip()
    if not user or not password:
        raise RuntimeError("device credentials are missing from the process environment")
    return user, password


def collect(device: dict[str, Any], user: str, password: str) -> dict[str, Any]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=device["ssh_host"],
            port=int(device["ssh_port"]),
            username=user,
            password=password,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        encoded = base64.b64encode(REMOTE_SCRIPT.encode("utf-8")).decode("ascii")
        command = f"printf '%s' '{encoded}' | base64 -d | python3"
        _, stdout, stderr = client.exec_command(command, timeout=60)
        raw_output = stdout.read()
        raw_error = stderr.read()
        exit_code = stdout.channel.recv_exit_status()
        error = raw_error.decode("utf-8", "replace").strip()
        if exit_code != 0:
            raise RuntimeError(f"remote probe failed with exit {exit_code}: {error[-300:]}")
        result = json.loads(raw_output.decode("utf-8"))
        result["device"] = device["display_id"]
        result["configured_machine_code"] = device.get("machine_code", "")
        return result
    finally:
        client.close()


def main() -> None:
    args = parse_args()
    if args.output.exists():
        raise RuntimeError(f"refusing to overwrite {args.output}")
    inventory = json.loads(
        (args.platform_root / "devices.json").read_text(encoding="utf-8")
    )["devices"]
    wanted = set(args.device or TARGET_IDS)
    devices = [item for item in inventory if str(item.get("display_id")) in wanted]
    if {str(item.get("display_id")) for item in devices} != wanted:
        raise RuntimeError("one or more requested devices are missing from the platform inventory")
    user, password = credentials()
    payload = {"devices": [collect(device, user, password) for device in devices]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"ok": True, "devices": sorted(wanted)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
