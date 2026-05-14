#!/usr/bin/env python3
"""Extract algorithm packages from AI-BOT boxes into platform runtime.

The extractor is read-only on devices. It lists model files under `/models`,
downloads `.ai` files through SFTP, and also archives complete `/models/m*`
algorithm directories so service-style algorithms such as m99 vehicle-plate
recognition remain directly deployable.

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


def detect_chip():
    texts = []
    for path in [
        "/proc/device-tree/compatible",
        "/proc/device-tree/model",
        "/sys/firmware/devicetree/base/compatible",
        "/sys/firmware/devicetree/base/model",
    ]:
        try:
            with open(path, "rb") as fh:
                texts.append(fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").lower())
        except Exception:
            pass
    text = " ".join(texts)
    if "rk3576" in text:
        family = "rk3576"
    elif "rk3588" in text:
        family = "rk3588"
    elif "rv1126" in text or "rv1109" in text:
        family = "rv1126"
    else:
        family = None
    return {"chip_family": family, "raw": text.strip()[:500]}


files = []
directories = []


def include_package_file(path):
    base = os.path.basename(path)
    if base in {".git"}:
        return False
    markers = [".bak", "bak-", ".bad-", "bad-", ".pre-", "pre-", ".upload", ".tmp"]
    return not any(marker in base for marker in markers)


for model_dir in sorted(glob.glob("/models/m*")):
    if not os.path.isdir(model_dir):
        continue
    slot = os.path.basename(model_dir)
    base_path = os.path.join(model_dir, "base.json")
    nn_path = os.path.join(model_dir, "nn.json")
    extend_path = os.path.join(model_dir, "nn.extend.json")
    threshold = None
    base = read_json(base_path) if os.path.exists(base_path) else None
    nn = read_json(nn_path) if os.path.exists(nn_path) else None
    extend = read_json(extend_path) if os.path.exists(extend_path) else None
    if isinstance(extend, dict):
        threshold = (extend.get("conf_thresh") or {}).get("value")
    nested_ai = []
    total_size = 0
    file_count = 0
    for root, dirnames, filenames in os.walk(model_dir):
        dirnames[:] = [
            d for d in dirnames
            if d not in {"backups", ".git"}
            and not d.startswith(".")
        ]
        for filename in filenames:
            path = os.path.join(root, filename)
            if not include_package_file(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            total_size += st.st_size
            file_count += 1
            if filename.endswith(".ai"):
                item = {
                    "path": path,
                    "filename": os.path.basename(path),
                    "relative_path": os.path.relpath(path, model_dir),
                    "size_bytes": st.st_size,
                    "mtime": int(st.st_mtime),
                    "md5": md5(path),
                }
                nested_ai.append(item)
                if os.path.dirname(path) == model_dir:
                    files.append({
                        "slot": slot,
                        "geid": int(slot[1:]) if slot.startswith("m") and slot[1:].isdigit() else None,
                        "path": path,
                        "filename": os.path.basename(path),
                        "size_bytes": st.st_size,
                        "mtime": int(st.st_mtime),
                        "md5": item["md5"],
                        "threshold": threshold,
                    })
    geid = None
    if isinstance(base, dict):
        geid = base.get("geid")
    if geid is None and slot.startswith("m") and slot[1:].isdigit():
        try:
            geid = int(slot[1:])
        except ValueError:
            geid = None
    directories.append({
        "slot": slot,
        "geid": geid,
        "path": model_dir,
        "base": base,
        "nn": nn,
        "threshold": threshold,
        "nested_ai": nested_ai,
        "file_count": file_count,
        "size_bytes": total_size,
        "has_nn_server": os.path.isdir(os.path.join(model_dir, "nn_server")),
        "has_dposter": os.path.isdir(os.path.join(model_dir, "dposter")),
        "license_exists": os.path.exists("/root/.mm/%s.lic" % slot),
    })

print(json.dumps({
    "chip": detect_chip(),
    "generated_at_device_epoch": int(os.path.getmtime("/models")) if os.path.exists("/models") else None,
    "modelN": api("/api/v1/system/modelN"),
    "algorithm_engine": api("/api/v1/algorithm/engine"),
    "files": files,
    "directories": directories,
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


def local_directory_package_path(output_dir: Path, device: dict[str, Any], remote_dir: dict[str, Any], archive_md5: str) -> Path:
    chip = safe_name(str(device.get("chip_family") or "unknown-chip"))
    slot = safe_name(str(remote_dir.get("slot") or "slot"))
    geid = safe_name(str(remote_dir.get("geid") if remote_dir.get("geid") is not None else slot))
    base = remote_dir.get("base") if isinstance(remote_dir.get("base"), dict) else {}
    name = safe_name(str(base.get("name") or remote_dir.get("engine", {}).get("name") or slot))
    source = safe_name(str(device.get("display_id") or "device"))
    filename = f"{slot}-{name}-geid{geid}-{source}-{archive_md5[:12]}.tar.gz"
    return output_dir / "by-chip" / chip / f"geid-{geid}" / slot / filename


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


def remote_archive_directory(ctx: SshContext, remote_dir: dict[str, Any], timeout: int) -> dict[str, Any]:
    slot = str(remote_dir.get("slot") or Path(str(remote_dir["path"])).name)
    source_path = str(remote_dir["path"])
    tmp_path = f"/tmp/codex-algorithm-dir-{safe_name(slot)}-{os.getpid()}-{int(time.time())}.tar.gz"
    code = f"""
import hashlib, json, os, tarfile
source_path = {json.dumps(source_path)}
slot = {json.dumps(slot)}
tmp_path = {json.dumps(tmp_path)}

def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def include(path):
    base = os.path.basename(path)
    if base in {{'.git', 'backups'}}:
        return False
    markers = ['.bak', 'bak-', '.bad-', 'bad-', '.pre-', 'pre-', '.upload', '.tmp']
    if any(marker in base for marker in markers):
        return False
    return True

with tarfile.open(tmp_path, 'w:gz') as tf:
    for root, dirnames, filenames in os.walk(source_path):
        dirnames[:] = sorted([d for d in dirnames if include(os.path.join(root, d))])
        for filename in sorted(filenames):
            path = os.path.join(root, filename)
            if not include(path):
                continue
            arcname = os.path.join(slot, os.path.relpath(path, source_path))
            tf.add(path, arcname=arcname)
st = os.stat(tmp_path)
print(json.dumps({{'path': tmp_path, 'md5': md5(tmp_path), 'size_bytes': st.st_size}}, ensure_ascii=False))
"""
    _, stdout, stderr = ctx.client.exec_command("PYTHONIOENCODING=utf-8 python3 - <<'PY'\n" + code + "\nPY", timeout=max(timeout, 90))
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise ExtractError(f"Remote directory archive failed rc={rc}: {err.strip()[:1000]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise ExtractError(f"Remote directory archive returned non-JSON: {out[:1000]}") from exc


def fetch_directory_package(
    ctx: SshContext,
    device: dict[str, Any],
    remote_dir: dict[str, Any],
    output_dir: Path,
    timeout: int,
) -> dict[str, Any]:
    archive = remote_archive_directory(ctx, remote_dir, timeout)
    local_path = local_directory_package_path(output_dir, device, remote_dir, str(archive["md5"]))
    if local_path.exists():
        current_md5 = file_md5(local_path)
        size_ok = local_path.stat().st_size == archive.get("size_bytes")
        if current_md5 == archive["md5"] and size_ok:
            action = "skip_existing"
        else:
            action = "downloaded"
    else:
        action = "downloaded"
    if action == "downloaded":
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_suffix(local_path.suffix + f".tmp-{int(time.time())}")
        ctx.sftp.get(str(archive["path"]), str(tmp))
        actual_md5 = file_md5(tmp)
        if actual_md5 != archive["md5"]:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise ExtractError(f"MD5 mismatch for {archive['path']}: {actual_md5} != {archive['md5']}")
        os.replace(tmp, local_path)
    try:
        ctx.client.exec_command(f"rm -f {archive['path']!r}", timeout=10)
    except Exception:
        pass
    return {
        "action": action,
        "path": str(local_path),
        "md5": archive["md5"],
        "size_bytes": archive.get("size_bytes"),
        "storage_relative_path": str(local_path.relative_to(output_dir)).replace("\\", "/"),
    }


def extract_device(
    device: dict[str, Any],
    username: str,
    password: str,
    output_dir: Path,
    timeout: int,
    include_directories: bool = True,
) -> dict[str, Any]:
    ctx = connect_device(device, username, password, timeout)
    try:
        inventory = remote_inventory(ctx, timeout)
        engines = engine_name_by_geid(inventory)
        detected_chip = (inventory.get("chip") or {}).get("chip_family") if isinstance(inventory.get("chip"), dict) else None
        effective_device = dict(device)
        if detected_chip:
            effective_device["chip_family"] = detected_chip
        extracted = []
        directory_packages = []
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
        if include_directories:
            for item in inventory.get("directories", []):
                if not isinstance(item, dict) or not item.get("path") or not item.get("slot"):
                    continue
                try:
                    engine = engines.get(str(item.get("geid")), {})
                    item["engine"] = engine
                    item["chip_family"] = effective_device.get("chip_family")
                    fetch = fetch_directory_package(ctx, effective_device, item, output_dir, timeout)
                    directory_packages.append(
                        {
                            "slot": item.get("slot"),
                            "geid": item.get("geid"),
                            "engine": engine,
                            "base": item.get("base"),
                            "nn": item.get("nn"),
                            "remote_path": item.get("path"),
                            "size_bytes_uncompressed": item.get("size_bytes"),
                            "file_count": item.get("file_count"),
                            "nested_ai": item.get("nested_ai", []),
                            "has_nn_server": item.get("has_nn_server"),
                            "has_dposter": item.get("has_dposter"),
                            "license_exists": item.get("license_exists"),
                            "threshold": item.get("threshold"),
                            "chip_family": effective_device.get("chip_family"),
                            "archive_md5": fetch["md5"],
                            "archive_size_bytes": fetch.get("size_bytes"),
                            "storage_relative_path": fetch["storage_relative_path"],
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
                "chip_family": effective_device.get("chip_family"),
                "chip_detected": inventory.get("chip"),
                "web": f"{device.get('web_host')}:{device.get('web_port')}",
                "ssh": f"{device.get('ssh_host')}:{device.get('ssh_port')}",
            },
            "status": "failed" if errors and not extracted and not directory_packages else "succeeded",
            "extracted_count": len(extracted),
            "error_count": len(errors),
            "modelN": inventory.get("modelN"),
            "algorithm_engine": inventory.get("algorithm_engine"),
            "models": extracted,
            "directories": directory_packages,
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
        lines.append(f"- Extracted directories: {item.get('directory_count', 0)}")
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
        for package in item.get("directories", []):
            engine = package.get("engine") or {}
            base = package.get("base") if isinstance(package.get("base"), dict) else {}
            name = engine.get("name") or base.get("name") or "-"
            version = engine.get("version") or base.get("version") or "-"
            lines.append(
                f"  - DIR {package.get('slot')} geid={package.get('geid')} name={name} "
                f"version={version} chip={package.get('chip_family')} archive_md5={package.get('archive_md5')} "
                f"nested_ai={len(package.get('nested_ai') or [])} fetch={package.get('fetch')}"
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
    parser.add_argument("--no-directories", action="store_true", help="Only extract top-level .ai files; skip full /models/m* directory archives.")
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
            result = extract_device(device, username, password, output_dir, args.timeout, include_directories=not args.no_directories)
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
                "directory_count": 0,
                "error": type(exc).__name__,
                "message": str(exc),
                "models": [],
                "directories": [],
                "errors": [],
            }
        result["directory_count"] = len(result.get("directories", []))
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
    unique_directory_md5 = sorted(
        {
            package["archive_md5"]
            for result in report_results
            for package in result.get("directories", [])
            if package.get("archive_md5")
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
        "total_directory_refs": sum(int(item.get("directory_count", len(item.get("directories", [])))) for item in report_results),
        "unique_models": len(unique_md5),
        "unique_directory_packages": len(unique_directory_md5),
        "unique_md5": unique_md5,
        "unique_directory_md5": unique_directory_md5,
        "devices": report_results,
    }
    write_json(output_dir / "extraction-report.json", aggregate)
    write_report(output_dir / "extraction-report.txt", aggregate)
    print(f"Wrote extraction output to {output_dir}")
    print(f"Unique .ai packages: {len(unique_md5)}")
    print(f"Unique directory packages: {len(unique_directory_md5)}")
    return 0 if aggregate["failed_devices"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
