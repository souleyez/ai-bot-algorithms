#!/usr/bin/env python3
"""Sanitized public snapshots; fixed read-only SSH probes, never arbitrary commands."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile


SCHEMA = "ai-bot.gpu-dashboard.v1"
NODES = (("h3", "10服务器", "root@100.96.0.3"), ("souleye", "souleye服务器", "server4090"))
ERRORS = {"ssh_failed", "probe_invalid", "gpu_unavailable", "host_unavailable", "inventory_unavailable"}
HOST_FIELDS = ("cpuPercent", "cpuCount", "load1", "memoryUsedMiB", "memoryTotalMiB", "diskUsedGiB", "diskTotalGiB", "uptimeSeconds")
GPU_FIELDS = ("index", "utilization", "memoryUsedMiB", "memoryTotalMiB", "temperatureC", "powerW", "powerLimitW")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=6",
       "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def name(value):
    if not isinstance(value, str):
        return ""
    # No paths, addresses, query strings or arbitrary process arguments in public JSON.
    return value[:160] if re.fullmatch(r"[A-Za-z0-9 _().+\-]+", value) else ""


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return value
    except ValueError:
        return ""


def normalize_node(profile, label, raw):
    raw = raw if isinstance(raw, dict) else {}
    node = {"id": profile, "name": label, "reachable": raw.get("reachable") is True,
            "observedAt": timestamp(raw.get("observedAt")), "gpus": [], "host": {}, "services": [], "models": [], "errors": []}
    node["errors"] = [code for code in raw.get("errors", []) if isinstance(code, str) and code in ERRORS] if isinstance(raw.get("errors"), list) else []
    if not node["reachable"]:
        return node
    host = raw.get("host") if isinstance(raw.get("host"), dict) else {}
    node["host"] = {key: number(host.get(key)) for key in HOST_FIELDS}
    for gpu in (raw.get("gpus") or [])[:4]:
        if isinstance(gpu, dict):
            node["gpus"].append({"name": name(gpu.get("name")), "driver": name(gpu.get("driver")),
                                 **{key: number(gpu.get(key)) for key in GPU_FIELDS}})
    known_services = {"ComfyUI / H3", "Qwen3.8 27B", "Qwen3 Embedding 4B"}
    for item in (raw.get("services") or [])[:8]:
        if not isinstance(item, dict) or item.get("name") not in known_services:
            continue
        node["services"].append({
            "name": item["name"], "state": item.get("state") if item.get("state") in {"active", "inactive", "failed", "activating"} else "unknown",
            "healthy": item.get("healthy") if isinstance(item.get("healthy"), bool) else None,
            "model": name(item.get("model")),
            **{key: number(item.get(key)) for key in ("queueRunning", "queuePending", "contextSize")},
        })
    for item in (raw.get("models") or [])[:100]:
        if isinstance(item, dict) and name(item.get("name")):
            node["models"].append({"name": name(item["name"]), "sizeMiB": number(item.get("sizeMiB"))})
    return node


def public_snapshot(raw):
    raw = raw if isinstance(raw, dict) and raw.get("schema") == SCHEMA else {}
    rows = raw.get("nodes") if isinstance(raw.get("nodes"), list) else []
    by_id = {item.get("id"): item for item in rows if isinstance(item, dict) and isinstance(item.get("id"), str)}
    return {"schema": SCHEMA, "generatedAt": timestamp(raw.get("generatedAt")),
            "nodes": [normalize_node(profile, label, by_id.get(profile)) for profile, label, _ in NODES]}


def load_snapshot(path):
    try:
        if path.stat().st_size > 256 * 1024:
            return public_snapshot({})
        return public_snapshot(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError):
        return public_snapshot({})


def collect_node(target):
    profile, label, host = target
    source = Path(__file__).with_name("gpu_probe.py").read_bytes()
    try:
        result = subprocess.run(SSH + [host, "python3 - --profile " + profile], input=source,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35, check=False)
        if result.returncode:
            return normalize_node(profile, label, {"errors": ["ssh_failed"]})
        if len(result.stdout) > 256 * 1024:
            raise ValueError("oversized")
        node = normalize_node(profile, label, json.loads(result.stdout))
        if not node["reachable"] or not node["observedAt"]:
            raise ValueError("missing probe")
        return node
    except (OSError, subprocess.SubprocessError):
        return normalize_node(profile, label, {"errors": ["ssh_failed"]})
    except (ValueError, TypeError, KeyError):
        return normalize_node(profile, label, {"errors": ["probe_invalid"]})


def collect():
    with ThreadPoolExecutor(max_workers=2) as executor:
        nodes = list(executor.map(collect_node, NODES))
    return {"schema": SCHEMA, "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "nodes": nodes}


def fetch(output):
    result = subprocess.run(SSH + ["-i", "/etc/ai-bot-gpu-dashboard/id_ed25519",
        "-o", "IdentitiesOnly=yes", "-o", "UserKnownHostsFile=/etc/ai-bot-gpu-dashboard/known_hosts",
        "-o", "HostKeyAlgorithms=ssh-ed25519", "root@1.12.246.48"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=False)
    if result.returncode or len(result.stdout) > 256 * 1024:
        raise ValueError("GPU snapshot transport failed")
    raw = json.loads(result.stdout)
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not timestamp(raw.get("generatedAt")):
        raise ValueError("Invalid GPU snapshot")
    snapshot = public_snapshot(raw)
    # Preserve the last successful transport on failure; its timestamp becomes stale in the UI.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as handle:
        json.dump(snapshot, handle, ensure_ascii=True, allow_nan=False)
        temporary = handle.name
    os.chmod(temporary, 0o644)
    os.replace(temporary, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", type=Path)
    args = parser.parse_args()
    if args.fetch:
        try:
            fetch(args.fetch)
        except Exception:
            raise SystemExit("GPU snapshot refresh failed; previous timestamp retained")
    else:
        print(json.dumps(collect(), ensure_ascii=True, allow_nan=False))
