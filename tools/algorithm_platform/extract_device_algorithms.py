#!/usr/bin/env python3
"""Extract `.ai` algorithm packages from AI-BOT boxes into platform runtime.

The extractor is read-only on devices. It lists model files under `/models`,
downloads `.ai` files through SFTP, verifies MD5, and stores one deduplicated
copy by MD5 inside the ignored platform runtime directory.

Credentials are read only from environment variables:

- AI_BOT_DEVICE_SSH_USER, default: root
- AI_BOT_DEVICE_SSH_PASSWORD
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME = Path(os.environ.get("AI_BOT_PLATFORM_RUNTIME", ROOT / ".runtime" / "algorithm-platform"))
DEFAULT_CATALOG = DEFAULT_RUNTIME / "catalog.json"
DEFAULT_OUTPUT_DIR = DEFAULT_RUNTIME / "device-extract"


REMOTE_INVENTORY = r'''
import glob
import hashlib
import json
import os
import urllib.request


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


def api(path):
    try:
        with urllib.request.urlopen("http://127.0.0.1" + path, timeout=5) as resp:
            body = resp.read(262144).decode("utf-8", "replace")
        try:
            return json.loads(body)
        except Exception:
            return body[:2000]
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


files = []
for model_dir in sorted(glob.glob("/models/m*")):
    if not os.path.isdir(model_dir):
        continue
    slot = os.path.basename(model_dir)
    extend_path = os.path.join(model_dir, "nn.extend.json")
    threshold = None
    extend = read_json(extend_path) if os.path.exists(extend_path) else None
    if isinstance(extend, dict):
        threshold = (extend.get("conf_thresh") or {}).get("value")
    for path in sorted(glob.glob(os.path.join(model_dir, "*.ai"))):
        try:
            st = os.stat(path)
            files.append({
                "slot": slot,
                "geid": int(slot[1:]) if slot.startswith("m") and slot[1:].isdigit() else None,
                "path": path,
                "filename": os.path.basename(path),
                "size_bytes": st.st_size,
                "mtime": int(st.st_mtime),
                "md5": md5(path),
                "threshold": threshold,
            })
        except Exception as exc:
            files.append({
                "slot": slot,
                "path": path,
                "filename": os.path.basename(path),
                "error": type(exc).__name__,
                "message": str(exc),
            })

print(json.dumps({
    "generated_at_device_epoch": int(os.path.getmtime("/models")) if os.path.exists("/models") else None,
    "modelN": api("/api/v1/system/modelN"),
    "algorithm_engine": api("/api/v1/algorithm/engine"),
    "files": files,
}, ensure_ascii=False, sort_keys=True))
'''


class ExtractError(RuntimeError):
    pass


@dataclass
class SshContext:
    client: Any
    sftp: Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "item"


def resolve_devices(catalog: dict[str, Any], requested: list[str], include_all: bool) -> list[dict[str, Any]]:
    devices = catalog.get("devices", [])
    if include_all:
        return list(devices)
    if not requested:
        return [d for d in devices if "validation" in d.get("tags", [])]
    resolved: dict[str, dict[str, Any]] = {}
    for item in requested:
        matches = [
            d
            for d in devices
            if item in {str(d.get("id")), str(d.get("display_id")), str(d.get("web_port")), str(d.get("machine_code"))}
        ]
        if not matches:
            raise ExtractError(f"Unknown device: {item}")
        for match in matches:
            resolved[match["id"]] = match
    return list(resolved.values())


def get_credentials() -> tuple[str, str]:
    username = os.environ.get("AI_BOT_DEVICE_SSH_USER", "root").strip()
    password = os.environ.get("AI_BOT_DEVICE_SSH_PASSWORD")
    password = password.strip() if password else password
    if not password:
        raise ExtractError("AI_BOT_DEVICE_SSH_PASSWORD is required in the environment")
    return username, password


def require_paramiko():
    try:
        import paramiko  # type: ignore
    except ModuleNotFoundError as exc:
        raise ExtractError("paramiko is required in this runtime") from exc
    return paramiko


def connect_device(device: dict[str, Any], username: str, password: str, timeout: int) -> SshContext:
    paramiko = require_paramiko()
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
    return SshContext(client=client, sftp=client.open_sftp())


def close_device(ctx: SshContext) -> None:
    try:
        ctx.sftp.close()
    finally:
        ctx.client.close()


def remote_inventory(ctx: SshContext, timeout: int) -> dict[str, Any]:
    command = "PYTHONIOENCODING=utf-8 python3 - <<'PY'\n" + REMOTE_INVENTORY + "\nPY"
    _, stdout, stderr = ctx.client.exec_command(command, timeout=max(timeout, 60))
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise ExtractError(f"Remote inventory failed rc={rc}: {err.strip()[:1000]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"Remote inventory returned non-JSON: {out[:1000]}") from exc


def engine_name_by_geid(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    payload = inventory.get("algorithm_engine")
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    engines = None
    if isinstance(result, dict) and isinstance(result.get("engines"), list):
        engines = result["engines"]
    elif isinstance(payload.get("engines"), list):
        engines = payload["engines"]
    if not isinstance(engines, list):
        return {}
    out = {}
    for item in engines:
        if not isinstance(item, dict):
            continue
        for key in ("geid", "id"):
            value = item.get(key)
            if value is not None:
                out[str(value)] = {
                    "geid": item.get("geid"),
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "version": item.get("version"),
                    "isRunning": item.get("isRunning"),
                }
    return out


def local_model_path(output_dir: Path, remote_file: dict[str, Any]) -> Path:
    md5 = str(remote_file["md5"])
    filename = safe_name(str(remote_file.get("filename") or Path(str(remote_file["path"])).name))
    return output_dir / "by-md5" / md5 / filename


def fetch_model(ctx: SshContext, remote_path: str, local_path: Path, expected_md5: str, expected_size: int | None) -> dict[str, Any]:
    if local_path.exists():
        current_md5 = file_md5(local_path)
        size_ok = expected_size is None or local_path.stat().st_size == expected_size
        if current_md5 == expected_md5 and size_ok:
            return {"action": "skip_existing", "path": str(local_path), "md5": current_md5}
    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = local_path.with_suffix(local_path.suffix + f".tmp-{int(time.time())}")
    ctx.sftp.get(remote_path, str(tmp))
    actual_md5 = file_md5(tmp)
    if actual_md5 != expected_md5:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ExtractError(f"MD5 mismatch for {remote_path}: {actual_md5} != {expected_md5}")
    os.replace(tmp, local_path)
    return {"action": "downloaded", "path": str(local_path), "md5": actual_md5}


def extract_device(device: dict[str, Any], username: str, password: str, output_dir: Path, timeout: int) -> dict[str, Any]:
    ctx = connect_device(device, username, password, timeout)
    try:
        inventory = remote_inventory(ctx, timeout)
        engines = engine_name_by_geid(inventory)
        extracted = []
        errors = []
        for item in inventory.get("files", []):
            if not isinstance(item, dict) or item.get("error"):
                errors.append(item)
                continue
            try:
                target = local_model_path(output_dir, item)
                fetch = fetch_model(ctx, str(item["path"]), target, str(item["md5"]), item.get("size_bytes"))
                engine = engines.get(str(item.get("geid")), {})
                extracted.append(
                    {
                        "slot": item.get("slot"),
                        "geid": item.get("geid"),
                        "engine": engine,
                        "remote_path": item.get("path"),
                        "filename": item.get("filename"),
                        "size_bytes": item.get("size_bytes"),
                        "mtime": item.get("mtime"),
                        "md5": item.get("md5"),
                        "threshold": item.get("threshold"),
                        "storage_relative_path": str(target.relative_to(output_dir)).replace("\\", "/"),
                        "fetch": fetch["action"],
                    }
                )
            except Exception as exc:
                errors.append({"path": item.get("path"), "error": type(exc).__name__, "message": str(exc)})
        return {
            "device": {
                "id": device.get("id"),
                "display_id": device.get("display_id"),
                "machine_code_expected": device.get("machine_code"),
                "web": f"{device.get('web_host')}:{device.get('web_port')}",
                "ssh": f"{device.get('ssh_host')}:{device.get('ssh_port')}",
            },
            "status": "failed" if errors and not extracted else "succeeded",
            "extracted_count": len(extracted),
            "error_count": len(errors),
            "modelN": inventory.get("modelN"),
            "algorithm_engine": inventory.get("algorithm_engine"),
            "models": extracted,
            "errors": errors,
        }
    finally:
        close_device(ctx)


def write_report(path: Path, aggregate: dict[str, Any]) -> None:
    lines = [
        "AI-BOT device algorithm extraction report",
        f"Generated: {aggregate['generated_at']}",
        f"Devices requested: {aggregate['requested_devices']}",
        f"Devices succeeded: {aggregate['succeeded_devices']}",
        f"Devices failed: {aggregate['failed_devices']}",
        f"Unique .ai packages: {aggregate['unique_models']}",
        "",
    ]
    for item in aggregate["devices"]:
        device = item["device"]["display_id"]
        lines.append(f"## {device}")
        lines.append(f"- Status: {item['status']}")
        lines.append(f"- Extracted files: {item.get('extracted_count', 0)}")
        if item.get("error"):
            lines.append(f"- Error: {item['error']} {item.get('message', '')}")
        for model in item.get("models", []):
            engine = model.get("engine") or {}
            name = engine.get("name") or "-"
            version = engine.get("version") or "-"
            lines.append(
                f"  - {model.get('slot')} geid={model.get('geid')} name={name} "
                f"version={version} md5={model.get('md5')} fetch={model.get('fetch')}"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def load_all_manifests(output_dir: Path) -> list[dict[str, Any]]:
    manifest_dir = output_dir / "by-device"
    if not manifest_dir.exists():
        return []
    results = []
    for path in sorted(manifest_dir.glob("*/manifest.json")):
        try:
            results.append(load_json(path))
        except Exception:
            continue
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--device", action="append", default=[], help="Device display port/id/machine code. Defaults to validation devices.")
    parser.add_argument("--all", action="store_true", help="Extract from every device in the catalog.")
    parser.add_argument("--timeout", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog = load_json(Path(args.catalog).expanduser().resolve())
    devices = resolve_devices(catalog, [str(item) for item in args.device], args.all)
    username, password = get_credentials()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for device in devices:
        display_id = str(device.get("display_id"))
        print(f"Extracting {display_id} from {device['ssh_host']}:{device['ssh_port']}...")
        try:
            result = extract_device(device, username, password, output_dir, args.timeout)
        except Exception as exc:
            result = {
                "device": {
                    "id": device.get("id"),
                    "display_id": display_id,
                    "web": f"{device.get('web_host')}:{device.get('web_port')}",
                    "ssh": f"{device.get('ssh_host')}:{device.get('ssh_port')}",
                },
                "status": "failed",
                "extracted_count": 0,
                "error": type(exc).__name__,
                "message": str(exc),
                "models": [],
                "errors": [],
            }
        write_json(output_dir / "by-device" / display_id / "manifest.json", result)
        results.append(result)

    report_results = load_all_manifests(output_dir) or results
    unique_md5 = sorted(
        {
            model["md5"]
            for result in report_results
            for model in result.get("models", [])
            if model.get("md5")
        }
    )
    aggregate = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "requested_devices": len(report_results),
        "last_run_requested_devices": len(devices),
        "succeeded_devices": sum(1 for item in report_results if item.get("status") == "succeeded"),
        "failed_devices": sum(1 for item in report_results if item.get("status") == "failed"),
        "total_extracted_refs": sum(int(item.get("extracted_count", 0)) for item in report_results),
        "unique_models": len(unique_md5),
        "unique_md5": unique_md5,
        "devices": report_results,
    }
    write_json(output_dir / "extraction-report.json", aggregate)
    write_report(output_dir / "extraction-report.txt", aggregate)
    print(f"Wrote extraction output to {output_dir}")
    print(f"Unique .ai packages: {len(unique_md5)}")
    return 0 if aggregate["failed_devices"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
