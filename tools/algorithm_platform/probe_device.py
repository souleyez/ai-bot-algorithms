#!/usr/bin/env python3
"""Read-only AI-BOT device inventory probe.

Credentials are read only from environment variables:

- AI_BOT_DEVICE_SSH_USER, default: root
- AI_BOT_DEVICE_SSH_PASSWORD

The probe never writes to the device and never stores credentials in output.
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME = ROOT / ".runtime" / "algorithm-platform"
DEFAULT_CATALOG = DEFAULT_RUNTIME / "catalog.json"
DEFAULT_STATE_DIR = DEFAULT_RUNTIME / "device-state"


REMOTE_PROBE = r'''
import glob
import hashlib
import json
import os
import platform
import socket
import sqlite3
import subprocess
import time
import urllib.request


def read_text(path, limit=262144):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


def read_json(path):
    value = read_text(path)
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc), "raw": value[:1000]}


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_info(path, include_hash=True):
    try:
        st = os.stat(path)
        item = {
            "path": path,
            "name": os.path.basename(path),
            "size_bytes": st.st_size,
            "mtime": int(st.st_mtime),
        }
        if include_hash:
            item["md5"] = file_md5(path)
        return item
    except Exception as exc:
        return {"path": path, "error": type(exc).__name__, "message": str(exc)}


def run(cmd, timeout=8):
    try:
        out = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout)
        return {"rc": out.returncode, "stdout": out.stdout.strip(), "stderr": out.stderr.strip()}
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


def fetch_local_api(path):
    url = "http://127.0.0.1" + path
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read(262144).decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = body[:2000]
        return {"ok": True, "path": path, "body": parsed}
    except Exception as exc:
        return {"ok": False, "path": path, "error": type(exc).__name__, "message": str(exc)}


def proc_inventory():
    procs = []
    for pid in sorted([p for p in os.listdir("/proc") if p.isdigit()], key=lambda x: int(x)):
        base = "/proc/" + pid
        try:
            cwd = os.readlink(base + "/cwd")
        except Exception:
            cwd = ""
        try:
            with open(base + "/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except Exception:
            cmdline = ""
        if "/models/" in cwd or "m101_scene_change" in cwd or "m101_scene_change" in cmdline or "nn_server" in cmdline or "dposter" in cmdline:
            procs.append({"pid": int(pid), "cwd": cwd, "cmdline": cmdline})
    return procs


def model_inventory():
    models = []
    for model_dir in sorted(glob.glob("/models/m*")):
        if not os.path.isdir(model_dir):
            continue
        slot = os.path.basename(model_dir)
        model_files = []
        for pattern in ("*.ai", "*.rknn"):
            for path in sorted(glob.glob(os.path.join(model_dir, pattern))):
                model_files.append(file_info(path, include_hash=True))
        configs = {}
        for name in ("base.json", "nn.json", "nn.extend.json", "dposter.yaml", "nn_server.yaml"):
            path = os.path.join(model_dir, name)
            if os.path.exists(path):
                if name.endswith(".json"):
                    configs[name] = read_json(path)
                else:
                    configs[name] = read_text(path, limit=8192)
        models.append({
            "slot": slot,
            "path": model_dir,
            "model_files": model_files,
            "configs": configs,
        })
    return models


def channel_bindings():
    bindings = []
    for freq_path in sorted(glob.glob("/oem/smart-gw/chma/m*/ch*/freq.json")):
        parts = freq_path.split("/")
        try:
            slot = parts[-3]
            channel = int(parts[-2].replace("ch", ""))
        except Exception:
            slot = None
            channel = None
        bindings.append({
            "slot": slot,
            "channel": channel,
            "freq_path": freq_path,
            "freq": read_json(freq_path),
        })
    return bindings


def service_inventory():
    services = []
    m101_dir = "/oem/smart-gw/m101_scene_change"
    m101_unit = "/etc/systemd/system/m101-scene-change.service"
    if os.path.exists(m101_dir) or os.path.exists(m101_unit):
        files = []
        for path in (
            os.path.join(m101_dir, "m101_scene_change_service.py"),
            os.path.join(m101_dir, "config.json"),
            m101_unit,
        ):
            if os.path.exists(path):
                files.append(file_info(path, include_hash=path.endswith((".py", ".service", ".json"))))
        services.append({
            "service_key": "scene_change",
            "slot": "m101",
            "geid": 101,
            "path": m101_dir,
            "unit_path": m101_unit,
            "systemd_active": run("systemctl is-active m101-scene-change.service 2>/dev/null || true"),
            "systemd_enabled": run("systemctl is-enabled m101-scene-change.service 2>/dev/null || true"),
            "files": files,
        })
    return services


def snap_counts():
    db = "/oem/smart-gw/db/snap.db"
    if not os.path.exists(db):
        return {"db": db, "exists": False}
    result = {"db": db, "exists": True, "counts_by_geid": {}, "latest_custom": []}
    try:
        conn = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=3)
        cur = conn.cursor()
        for geid in (100, 101, 102, 103):
            try:
                row = cur.execute("select count(*) from ch_g_imgs where geid=?", (geid,)).fetchone()
                result["counts_by_geid"][str(geid)] = row[0] if row else 0
            except Exception as exc:
                result["counts_by_geid"][str(geid)] = {"error": type(exc).__name__, "message": str(exc)}
        try:
            rows = cur.execute(
                "select chNo, geid, picName, spicName, timeStampStr from ch_g_imgs "
                "where geid in (100,101,102,103) order by timeStamp desc limit 20"
            ).fetchall()
            result["latest_custom"] = [
                {"chNo": r[0], "geid": r[1], "picName": r[2], "spicName": r[3], "timeStampStr": r[4]} for r in rows
            ]
        except Exception as exc:
            result["latest_custom_error"] = {"error": type(exc).__name__, "message": str(exc)}
        conn.close()
    except Exception as exc:
        result["error"] = {"error": type(exc).__name__, "message": str(exc)}
    return result


def disk_usage(path):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"path": path, "total_bytes": total, "free_bytes": free, "used_ratio": round(1 - free / total, 4) if total else None}
    except Exception as exc:
        return {"path": path, "error": type(exc).__name__, "message": str(exc)}


state = {
    "probe_version": 1,
    "probed_at_device_epoch": int(time.time()),
    "hostname": socket.gethostname(),
    "platform": {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "uname": " ".join(platform.uname()),
    },
    "disk": [disk_usage("/models"), disk_usage("/oem/smart-gw"), disk_usage("/userdata/mpp")],
    "local_api": {
        "modelN": fetch_local_api("/api/v1/system/modelN"),
        "algorithm_engine": fetch_local_api("/api/v1/algorithm/engine"),
    },
    "models": model_inventory(),
    "channel_bindings": channel_bindings(),
    "services": service_inventory(),
    "processes": proc_inventory(),
    "snap": snap_counts(),
}
print(json.dumps(state, ensure_ascii=False, sort_keys=True))
'''


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def resolve_devices(catalog: dict[str, Any], requested: list[str]) -> list[dict[str, Any]]:
    devices = catalog.get("devices", [])
    if not requested:
        return [d for d in devices if "validation" in d.get("tags", [])]
    resolved = []
    for item in requested:
        matches = [
            d
            for d in devices
            if item in {str(d.get("id")), str(d.get("display_id")), str(d.get("web_port")), str(d.get("machine_code"))}
        ]
        if not matches:
            raise SystemExit(f"Unknown device: {item}")
        resolved.extend(matches)
    unique = {}
    for device in resolved:
        unique[device["id"]] = device
    return list(unique.values())


def get_credentials() -> tuple[str, str]:
    username = os.environ.get("AI_BOT_DEVICE_SSH_USER", "root")
    password = os.environ.get("AI_BOT_DEVICE_SSH_PASSWORD")
    if not password:
        raise SystemExit("AI_BOT_DEVICE_SSH_PASSWORD is required in the environment")
    return username, password


def connect_and_probe(device: dict[str, Any], username: str, password: str, timeout: int) -> dict[str, Any]:
    try:
        import paramiko
    except ModuleNotFoundError as exc:
        raise SystemExit("paramiko is required for probing. Install it in the runtime environment.") from exc

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=device["ssh_host"],
        port=int(device["ssh_port"]),
        username=username,
        password=password,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
    try:
        command = "PYTHONIOENCODING=utf-8 python3 - <<'PY'\n" + REMOTE_PROBE + "\nPY"
        _, stdout, stderr = client.exec_command(command, timeout=max(timeout, 60))
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
    finally:
        client.close()
    if rc != 0:
        raise RuntimeError(f"Probe failed for {device['display_id']} rc={rc} stderr={err.strip()[:1000]}")
    try:
        raw_state = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Probe returned non-JSON for {device['display_id']}: {out[:1000]}") from exc
    return raw_state


def slot_to_geid(slot: str | None) -> int | None:
    if not slot or not slot.startswith("m"):
        return None
    try:
        return int(slot[1:])
    except ValueError:
        return None


def summarize_state(device: dict[str, Any], raw: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    artifacts_by_slot = {}
    for artifact in catalog.get("artifacts", []):
        slot = artifact.get("slot")
        if slot and artifact.get("status") == "approved":
            artifacts_by_slot[slot] = {
                "artifact_id": artifact.get("id"),
                "algorithm_key": artifact.get("algorithm_key"),
                "display_name": artifact.get("display_name"),
                "version_label": artifact.get("version_label"),
                "approved_md5": artifact.get("md5"),
            }

    proc_by_slot: dict[str, list[dict[str, Any]]] = {}
    for proc in raw.get("processes", []):
        cwd = proc.get("cwd", "")
        for part in cwd.split("/"):
            if part.startswith("m") and part[1:].isdigit():
                proc_by_slot.setdefault(part, []).append(proc)

    bindings_by_slot: dict[str, list[dict[str, Any]]] = {}
    for binding in raw.get("channel_bindings", []):
        if binding.get("slot"):
            bindings_by_slot.setdefault(binding["slot"], []).append(binding)

    states = []
    for model in raw.get("models", []):
        slot = model.get("slot")
        extend = model.get("configs", {}).get("nn.extend.json", {})
        threshold = None
        if isinstance(extend, dict):
            threshold = (extend.get("conf_thresh") or {}).get("value")
        model_files = model.get("model_files", [])
        primary_file = model_files[0] if model_files else None
        states.append(
            {
                "slot": slot,
                "geid": slot_to_geid(slot),
                "path": model.get("path"),
                "threshold": threshold,
                "model_files": model_files,
                "primary_md5": primary_file.get("md5") if primary_file else None,
                "channel_bindings": sorted(bindings_by_slot.get(slot, []), key=lambda b: b.get("channel") or 0),
                "processes": proc_by_slot.get(slot, []),
                "approved_artifact": artifacts_by_slot.get(slot),
            }
        )

    for service in raw.get("services", []):
        slot = service.get("slot")
        states.append(
            {
                "slot": slot,
                "geid": service.get("geid"),
                "path": service.get("path"),
                "service": service,
                "channel_bindings": sorted(bindings_by_slot.get(slot, []), key=lambda b: b.get("channel") or 0),
                "processes": proc_by_slot.get(slot, []),
                "approved_artifact": artifacts_by_slot.get(slot),
            }
        )

    model_n = None
    api_body = raw.get("local_api", {}).get("modelN", {}).get("body")
    if isinstance(api_body, dict):
        for key in ("modelN", "model_n", "data"):
            val = api_body.get(key)
            if isinstance(val, int):
                model_n = val
            elif isinstance(val, dict):
                for nested_key in ("modelN", "model_n", "value"):
                    if isinstance(val.get(nested_key), int):
                        model_n = val[nested_key]
    model_slots = [m.get("slot") for m in raw.get("models", [])]
    warnings = []
    if model_n is not None and len(model_slots) > model_n:
        warnings.append(
            {
                "type": "model_display_limit",
                "message": f"Device reports modelN={model_n}, but /models has {len(model_slots)} model directories.",
            }
        )

    custom_slots = {"m100", "m101", "m102", "m103"}
    installed_custom = sorted({s.get("slot") for s in states if s.get("slot") in custom_slots})
    return {
        "schema_version": 1,
        "probed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "device": {
            "id": device["id"],
            "display_id": device["display_id"],
            "machine_code_expected": device.get("machine_code"),
            "web": f"{device['web_host']}:{device['web_port']}",
            "ssh": f"{device['ssh_host']}:{device['ssh_port']}",
            "tags": device.get("tags", []),
        },
        "platform": raw.get("platform"),
        "disk": raw.get("disk"),
        "local_api": raw.get("local_api"),
        "device_algorithm_state": sorted(states, key=lambda item: item.get("slot") or ""),
        "installed_custom_slots": installed_custom,
        "snap": raw.get("snap"),
        "warnings": warnings,
        "raw_summary": {
            "model_dir_count": len(raw.get("models", [])),
            "channel_binding_count": len(raw.get("channel_bindings", [])),
            "process_count": len(raw.get("processes", [])),
            "service_count": len(raw.get("services", [])),
        },
    }


def write_report(path: Path, states: list[dict[str, Any]]) -> None:
    lines = ["AI-BOT device probe report", f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}", ""]
    for state in states:
        device = state["device"]
        lines.append(f"## {device['display_id']} ({device['id']})")
        lines.append(f"- Web: {device['web']}")
        lines.append(f"- SSH: {device['ssh']}")
        lines.append(f"- Custom slots: {', '.join(state['installed_custom_slots']) or 'none'}")
        lines.append(f"- Models: {state['raw_summary']['model_dir_count']}")
        lines.append(f"- Channel bindings: {state['raw_summary']['channel_binding_count']}")
        if state.get("warnings"):
            for warning in state["warnings"]:
                lines.append(f"- Warning: {warning['message']}")
        lines.append("")
        for item in state["device_algorithm_state"]:
            slot = item.get("slot")
            if slot not in {"m100", "m101", "m102", "m103"}:
                continue
            channels = [str(b.get("channel")) for b in item.get("channel_bindings", []) if b.get("channel") is not None]
            md5 = item.get("primary_md5")
            proc_count = len(item.get("processes", []))
            threshold = item.get("threshold")
            service = item.get("service") or {}
            active = (service.get("systemd_active") or {}).get("stdout")
            enabled = (service.get("systemd_enabled") or {}).get("stdout")
            service_text = f" service={active or '-'} enabled={enabled or '-'}" if service else ""
            lines.append(
                f"  - {slot}: channels={','.join(channels) or '-'} threshold={threshold} "
                f"md5={md5 or '-'} processes={proc_count}{service_text}"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="Catalog JSON path.")
    parser.add_argument("--output-dir", default=str(DEFAULT_STATE_DIR), help="Output directory for device states.")
    parser.add_argument("--device", action="append", default=[], help="Device display port/id/machine code. Defaults to validation devices.")
    parser.add_argument("--timeout", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog = load_json(Path(args.catalog))
    devices = resolve_devices(catalog, args.device)
    username, password = get_credentials()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    states = []
    for device in devices:
        print(f"Probing {device['display_id']} at {device['ssh_host']}:{device['ssh_port']}...")
        raw = connect_and_probe(device, username, password, args.timeout)
        state = summarize_state(device, raw, catalog)
        write_json(output_dir / f"{device['display_id']}.json", state)
        states.append(state)
    aggregate = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "states": states,
    }
    write_json(output_dir / "device_algorithm_state.json", aggregate)
    write_report(output_dir / "probe-report.txt", states)
    print(f"Wrote device state to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
