#!/usr/bin/env python3
"""AI-BOT algorithm release worker.

This module implements the platform release primitive used by both CLI and
HTTP API entrypoints. It supports real deployment for approved RKNN `.ai`
artifacts and read-only dry-runs for service packages.

Secrets are read only from environment variables:

- AI_BOT_DEVICE_SSH_USER, default: root
- AI_BOT_DEVICE_SSH_PASSWORD
"""

from __future__ import annotations

import argparse
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
DEFAULT_AUTO_ALLOWED_TAGS = {"validation", "lab"}


class PlatformError(RuntimeError):
    """Expected platform error with a user-safe message."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def job_path(runtime: Path, request_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in request_id)
    return runtime / "release-jobs" / f"{safe}.json"


def validate_request_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformError("request_id is required")
    request_id = value.strip()
    if len(request_id) > 120 or not re.fullmatch(r"[A-Za-z0-9._-]+", request_id):
        raise PlatformError("request_id must be 1-120 characters: A-Z, a-z, 0-9, dot, underscore, or hyphen")
    return request_id


def load_catalog(runtime: Path) -> dict[str, Any]:
    path = runtime / "catalog.json"
    if not path.exists():
        raise PlatformError(f"Catalog not found: {path}")
    return read_json(path)


def resolve_devices(catalog: dict[str, Any], requested: list[str]) -> list[dict[str, Any]]:
    if not requested:
        raise PlatformError("target_devices is required")
    devices = catalog.get("devices", [])
    resolved: dict[str, dict[str, Any]] = {}
    for item in requested:
        matches = [
            d
            for d in devices
            if item in {str(d.get("id")), str(d.get("display_id")), str(d.get("web_port")), str(d.get("machine_code"))}
        ]
        if not matches:
            raise PlatformError(f"Unknown device: {item}")
        for match in matches:
            resolved[match["id"]] = match
    return list(resolved.values())


def resolve_artifact(catalog: dict[str, Any], algorithm_key: str, version_label: str | None) -> dict[str, Any]:
    candidates = [
        item
        for item in catalog.get("artifacts", [])
        if item.get("algorithm_key") == algorithm_key and item.get("status") == "approved"
    ]
    if version_label:
        candidates = [item for item in candidates if item.get("version_label") == version_label]
    if not candidates:
        raise PlatformError(f"No approved artifact found for {algorithm_key} {version_label or ''}".strip())
    if len(candidates) > 1:
        labels = ", ".join(sorted(str(item.get("version_label")) for item in candidates))
        raise PlatformError(f"Multiple approved artifacts match {algorithm_key}; specify version_label. Candidates: {labels}")
    return candidates[0]


def artifact_local_path(runtime: Path, artifact: dict[str, Any]) -> Path:
    rel = artifact.get("storage_relative_path")
    if not rel:
        raise PlatformError(f"Artifact has no copied storage path: {artifact.get('id')}")
    path = runtime / rel
    if not path.exists():
        raise PlatformError(f"Artifact file missing from runtime storage: {path}")
    return path


def get_credentials() -> tuple[str, str]:
    username = os.environ.get("AI_BOT_DEVICE_SSH_USER", "root").strip()
    password = os.environ.get("AI_BOT_DEVICE_SSH_PASSWORD")
    password = password.strip() if password else password
    if not password:
        raise PlatformError("AI_BOT_DEVICE_SSH_PASSWORD is required in the environment")
    return username, password


def require_paramiko():
    try:
        import paramiko  # type: ignore
    except ModuleNotFoundError as exc:
        raise PlatformError("paramiko is required in this runtime") from exc
    return paramiko


@dataclass
class SshResult:
    rc: int
    stdout: str
    stderr: str


class DeviceSession:
    def __init__(self, device: dict[str, Any], timeout: int = 25):
        self.device = device
        self.timeout = timeout
        self.client = None

    def __enter__(self) -> "DeviceSession":
        paramiko = require_paramiko()
        username, password = get_credentials()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.device["ssh_host"],
            port=int(self.device["ssh_port"]),
            username=username,
            password=password,
            timeout=self.timeout,
            banner_timeout=self.timeout,
            auth_timeout=self.timeout,
        )
        self.client = client
        return self

    def __exit__(self, *_exc) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def exec(self, command: str, timeout: int = 60) -> SshResult:
        assert self.client is not None
        _, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return SshResult(rc=rc, stdout=out, stderr=err)

    def put(self, local: Path, remote: str) -> None:
        assert self.client is not None
        sftp = self.client.open_sftp()
        try:
            sftp.put(str(local), remote)
        finally:
            sftp.close()


def remote_json(session: DeviceSession, python_code: str, timeout: int = 60) -> dict[str, Any]:
    command = "PYTHONIOENCODING=utf-8 python3 - <<'PY'\n" + python_code + "\nPY"
    result = session.exec(command, timeout=timeout)
    if result.rc != 0:
        raise PlatformError(f"Remote command failed rc={result.rc}: {result.stderr.strip()[:1200]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PlatformError(f"Remote command returned non-JSON: {result.stdout[:1200]}") from exc


def remote_preflight(session: DeviceSession, artifact: dict[str, Any], channels: list[int], threshold: float | None) -> dict[str, Any]:
    model_path = artifact.get("remote_model_path")
    slot = artifact.get("slot")
    code = f"""
import glob, hashlib, json, os, urllib.request

slot = {json.dumps(slot)}
model_path = {json.dumps(model_path)}
channels = {json.dumps(channels)}
threshold = {json.dumps(threshold)}

def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception as exc:
        return {{'error': type(exc).__name__, 'message': str(exc)}}

def api(path):
    try:
        with urllib.request.urlopen('http://127.0.0.1' + path, timeout=4) as resp:
            body = resp.read(65536).decode('utf-8', 'replace')
        try:
            return json.loads(body)
        except Exception:
            return body[:1000]
    except Exception as exc:
        return {{'error': type(exc).__name__, 'message': str(exc)}}

def procs_for_slot(slot):
    procs = []
    prefix = '/models/' + slot
    for pid in sorted([p for p in os.listdir('/proc') if p.isdigit()], key=lambda x: int(x)):
        try:
            cwd = os.readlink('/proc/' + pid + '/cwd')
        except Exception:
            cwd = ''
        if cwd.startswith(prefix):
            try:
                with open('/proc/' + pid + '/cmdline', 'rb') as fh:
                    cmdline = fh.read().replace(b'\\x00', b' ').decode('utf-8', 'replace').strip()
            except Exception:
                cmdline = ''
            procs.append({{'pid': int(pid), 'cwd': cwd, 'cmdline': cmdline}})
    return procs

existing = None
if model_path and os.path.exists(model_path):
    st = os.stat(model_path)
    existing = {{'path': model_path, 'md5': md5(model_path), 'size_bytes': st.st_size, 'mtime': int(st.st_mtime)}}

freq = []
for path in sorted(glob.glob('/oem/smart-gw/chma/' + slot + '/ch*/freq.json')):
    ch = None
    try:
        ch = int(os.path.basename(os.path.dirname(path)).replace('ch', ''))
    except Exception:
        pass
    freq.append({{'channel': ch, 'path': path, 'freq': read_json(path)}})

extend_path = '/models/' + slot + '/nn.extend.json'
print(json.dumps({{
    'slot': slot,
    'model_path': model_path,
    'existing_model': existing,
    'nn_extend': read_json(extend_path) if os.path.exists(extend_path) else None,
    'channel_bindings': freq,
    'requested_channels': channels,
    'requested_threshold': threshold,
    'processes': procs_for_slot(slot),
    'modelN': api('/api/v1/system/modelN'),
    'algorithm_engine': api('/api/v1/algorithm/engine'),
}}, ensure_ascii=False))
"""
    return remote_json(session, code, timeout=60)


def build_plan(
    request_id: str,
    device: dict[str, Any],
    artifact: dict[str, Any],
    local_path: Path,
    preflight: dict[str, Any],
    channels: list[int],
    threshold: float | None,
) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slot = artifact["slot"]
    remote_model_path = artifact.get("remote_model_path")
    existing = preflight.get("existing_model")
    existing_md5 = existing.get("md5") if isinstance(existing, dict) else None
    model_action = "skip_same_md5" if existing_md5 == artifact.get("md5") else "replace_model"
    bound_channels = {
        item.get("channel")
        for item in preflight.get("channel_bindings", [])
        if isinstance(item.get("channel"), int)
    }
    channels_to_add = sorted([ch for ch in channels if ch not in bound_channels])
    backup_path = f"{remote_model_path}.bak-platform-{request_id}-{stamp}" if remote_model_path else None
    upload_tmp = f"/tmp/{request_id}-{Path(remote_model_path or local_path.name).name}.upload"
    return {
        "device": {"id": device["id"], "display_id": device["display_id"], "ssh": f"{device['ssh_host']}:{device['ssh_port']}"},
        "artifact": {
            "id": artifact["id"],
            "algorithm_key": artifact["algorithm_key"],
            "display_name": artifact["display_name"],
            "version_label": artifact["version_label"],
            "artifact_kind": artifact["artifact_kind"],
            "slot": slot,
            "geid": artifact.get("geid"),
            "md5": artifact.get("md5"),
        },
        "local_artifact_path": str(local_path),
        "remote_model_path": remote_model_path,
        "existing_remote_md5": existing_md5,
        "model_action": model_action,
        "backup_path": backup_path,
        "upload_tmp": upload_tmp,
        "threshold_action": {"requested": threshold, "current": ((preflight.get("nn_extend") or {}).get("conf_thresh") or {}).get("value")},
        "channel_action": {"requested": channels, "already_bound": sorted(bound_channels), "channels_to_add": channels_to_add},
        "restart_slot": slot,
        "warnings": [],
    }


def validate_channels(channels: list[Any]) -> list[int]:
    result = []
    for ch in channels:
        if not isinstance(ch, int) or ch < 1 or ch > 64:
            raise PlatformError(f"Invalid channel: {ch}")
        result.append(ch)
    return sorted(set(result))


def validate_threshold(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise PlatformError("threshold must be a number")
    threshold = float(value)
    if threshold < 0 or threshold > 1:
        raise PlatformError("threshold must be between 0 and 1")
    return threshold


def deploy_ai_model(session: DeviceSession, plan: dict[str, Any], artifact: dict[str, Any], local_path: Path, threshold: float | None, channels: list[int]) -> dict[str, Any]:
    if artifact.get("artifact_kind") != "rknn_ai_model":
        raise PlatformError(f"Automatic deployment is not enabled for artifact kind: {artifact.get('artifact_kind')}")

    slot = artifact["slot"]
    remote_model_path = plan["remote_model_path"]
    backup_paths: list[str] = []
    upload_result = None

    if plan["model_action"] == "replace_model":
        session.put(local_path, plan["upload_tmp"])
        code = f"""
import hashlib, json, os, shutil
tmp = {json.dumps(plan["upload_tmp"])}
model_path = {json.dumps(remote_model_path)}
backup_path = {json.dumps(plan["backup_path"])}
expected_md5 = {json.dumps(artifact["md5"])}

def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

actual = md5(tmp)
if actual != expected_md5:
    raise SystemExit('uploaded md5 mismatch: ' + actual)
os.makedirs(os.path.dirname(model_path), exist_ok=True)
if os.path.exists(model_path):
    shutil.copy2(model_path, backup_path)
os.replace(tmp, model_path)
print(json.dumps({{'uploaded_md5': actual, 'model_path': model_path, 'backup_path': backup_path if os.path.exists(backup_path) else None}}, ensure_ascii=False))
"""
        upload_result = remote_json(session, code, timeout=120)
        if upload_result.get("backup_path"):
            backup_paths.append(upload_result["backup_path"])

    config_code = f"""
import json, os, shutil
slot = {json.dumps(slot)}
threshold = {json.dumps(threshold)}
channels = {json.dumps(channels)}
stamp = {json.dumps(datetime.now().strftime("%Y%m%d-%H%M%S"))}
backups = []

extend_path = '/models/' + slot + '/nn.extend.json'
if threshold is not None and os.path.exists(extend_path):
    backup = extend_path + '.bak-platform-' + stamp
    shutil.copy2(extend_path, backup)
    backups.append(backup)
    with open(extend_path, encoding='utf-8') as fh:
        data = json.load(fh)
    data.setdefault('conf_thresh', {{'name': '置信度阈值'}})['value'] = threshold
    with open(extend_path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False)

created_freq = []
for ch in channels:
    ch_dir = f'/oem/smart-gw/chma/{{slot}}/ch{{ch}}'
    os.makedirs(ch_dir, exist_ok=True)
    freq_path = os.path.join(ch_dir, 'freq.json')
    if not os.path.exists(freq_path):
        with open(freq_path, 'w', encoding='utf-8') as fh:
            json.dump({{'detectFreq': 1000, 'filterType': 0, 'pubFreq': 5, 'start_time': '00:00:00', 'end_time': '00:00:00'}}, fh)
        created_freq.append(freq_path)

print(json.dumps({{'config_backups': backups, 'created_freq': created_freq}}, ensure_ascii=False))
"""
    config_result = remote_json(session, config_code, timeout=60)
    backup_paths.extend(config_result.get("config_backups", []))

    restart_result = restart_slot_processes(session, slot)

    verify = remote_preflight(session, artifact, channels, threshold)
    remote_md5 = (verify.get("existing_model") or {}).get("md5")
    if remote_md5 != artifact.get("md5"):
        raise PlatformError(f"Post-deploy MD5 mismatch: remote={remote_md5} expected={artifact.get('md5')}")
    if not verify.get("processes"):
        raise PlatformError("Post-deploy verification found no running processes for slot")

    return {
        "upload": upload_result or {"skipped": True, "reason": plan["model_action"]},
        "config": config_result,
        "restart": restart_result,
        "verify": verify,
        "backup_paths": backup_paths,
    }


def restart_slot_processes(session: DeviceSession, slot: str) -> dict[str, Any]:
    restart_code = f"""
import json, os, signal, subprocess, time
slot = {json.dumps(slot)}
prefix = '/models/' + slot
killed = []
for pid in sorted([p for p in os.listdir('/proc') if p.isdigit()], key=lambda x: int(x)):
    try:
        cwd = os.readlink('/proc/' + pid + '/cwd')
    except Exception:
        continue
    if cwd.startswith(prefix):
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed.append(int(pid))
        except Exception:
            pass
time.sleep(1.0)
for pid in killed:
    if os.path.exists('/proc/' + str(pid)):
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
stamp = time.strftime('%Y%m%d-%H%M%S')
nn_log = f'/tmp/nn_server_{{slot}}_platform_{{stamp}}.log'
dp_log = f'/tmp/dposter_{{slot}}_platform_{{stamp}}.log'
started = []
nn_dir = prefix + '/nn_server'
if os.path.isdir(nn_dir):
    subprocess.Popen(
        f'cd {{nn_dir}} && LD_LIBRARY_PATH=/oem/usr/lib/ nohup /oem/smart-gw/service/nn_server/bin/nn_server -c nn_server.conf > {{nn_log}} 2>&1 &',
        shell=True,
    )
    started.append({{'component': 'nn_server', 'log': nn_log}})
dp_dir = prefix + '/dposter'
if os.path.isdir(dp_dir):
    arg = 'args.json' if os.path.exists(dp_dir + '/args.json') else ''
    subprocess.Popen(
        f'cd {{dp_dir}} && nohup /usr/bin/python3 main.py {{arg}} > {{dp_log}} 2>&1 &',
        shell=True,
    )
    started.append({{'component': 'dposter', 'log': dp_log}})
time.sleep(2.0)
procs = []
for pid in sorted([p for p in os.listdir('/proc') if p.isdigit()], key=lambda x: int(x)):
    try:
        cwd = os.readlink('/proc/' + pid + '/cwd')
    except Exception:
        cwd = ''
    if cwd.startswith(prefix):
        try:
            with open('/proc/' + pid + '/cmdline', 'rb') as fh:
                cmdline = fh.read().replace(b'\\x00', b' ').decode('utf-8', 'replace').strip()
        except Exception:
            cmdline = ''
        procs.append({{'pid': int(pid), 'cwd': cwd, 'cmdline': cmdline}})
print(json.dumps({{'killed': killed, 'started': started, 'processes': procs}}, ensure_ascii=False))
"""
    return remote_json(session, restart_code, timeout=90)


def result_for_device(job: dict[str, Any], display_id: str) -> dict[str, Any] | None:
    for item in job.get("results", []):
        if str(item.get("device")) == str(display_id):
            return item
    return None


def rollback_plan_for_job(job: dict[str, Any]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for plan in job.get("plans", []):
        device = plan.get("device", {})
        display_id = str(device.get("display_id", ""))
        result_item = result_for_device(job, display_id) or {}
        result = result_item.get("result") or {}
        upload = result.get("upload") or {}
        config = result.get("config") or {}
        model_backup = upload.get("backup_path")
        config_backups = list(config.get("config_backups") or [])
        created_freq = list(config.get("created_freq") or [])
        plans.append({
            "device": device,
            "slot": plan.get("restart_slot") or (plan.get("artifact") or {}).get("slot"),
            "remote_model_path": plan.get("remote_model_path"),
            "model_backup_path": model_backup,
            "config_backup_paths": config_backups,
            "created_freq_paths": created_freq,
            "has_actions": bool(model_backup or config_backups or created_freq),
        })
    return plans


def rollback_device(session: DeviceSession, plan: dict[str, Any]) -> dict[str, Any]:
    slot = plan.get("slot")
    if not isinstance(slot, str) or not slot:
        raise PlatformError("Rollback plan is missing slot")
    model_path = plan.get("remote_model_path")
    model_backup = plan.get("model_backup_path")
    config_backups = plan.get("config_backup_paths") or []
    created_freq = plan.get("created_freq_paths") or []
    code = f"""
import hashlib, json, os, shutil
slot = {json.dumps(slot)}
model_path = {json.dumps(model_path)}
model_backup = {json.dumps(model_backup)}
config_backups = {json.dumps(config_backups)}
created_freq = {json.dumps(created_freq)}
restored = []
removed = []
missing = []

def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def restore_file(src, dst):
    if not src:
        return
    if not os.path.exists(src):
        missing.append(src)
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    before = md5(src)
    shutil.copy2(src, dst)
    after = md5(dst)
    restored.append({{'from': src, 'to': dst, 'backup_md5': before, 'restored_md5': after, 'md5_match': before == after}})

if model_backup and model_path:
    restore_file(model_backup, model_path)

for src in config_backups:
    if not isinstance(src, str):
        continue
    if src.endswith('/nn.extend.json') or '.bak-platform-' not in src:
        missing.append(src)
        continue
    dst = src.split('.bak-platform-', 1)[0]
    restore_file(src, dst)

for path in created_freq:
    if not isinstance(path, str):
        continue
    expected_prefix = '/oem/smart-gw/chma/' + slot + '/ch'
    if path.startswith(expected_prefix) and path.endswith('/freq.json') and os.path.exists(path):
        os.remove(path)
        removed.append(path)

print(json.dumps({{'restored': restored, 'removed_created_freq': removed, 'missing': missing}}, ensure_ascii=False))
"""
    restore_result = remote_json(session, code, timeout=60)
    restart_result = restart_slot_processes(session, slot)
    return {"restore": restore_result, "restart": restart_result}


def auto_allowed(device: dict[str, Any], artifact: dict[str, Any]) -> bool:
    allowed = set(os.environ.get("AI_BOT_AUTO_ALLOWED_TAGS", ",".join(DEFAULT_AUTO_ALLOWED_TAGS)).strip().split(","))
    allowed = {item.strip() for item in allowed if item.strip()}
    if artifact.get("status") != "approved":
        return False
    tags = set(device.get("tags", []))
    return bool(tags & allowed)


def build_job(runtime: Path, payload: dict[str, Any]) -> dict[str, Any]:
    request_id = validate_request_id(payload.get("request_id"))
    existing_path = job_path(runtime, request_id)
    if existing_path.exists():
        return read_json(existing_path)

    catalog = load_catalog(runtime)
    devices = resolve_devices(catalog, [str(item) for item in payload.get("target_devices", [])])
    artifact = resolve_artifact(catalog, str(payload.get("algorithm_key", "")), payload.get("version_label"))
    channels = validate_channels(payload.get("channels", []))
    threshold = validate_threshold(payload.get("threshold", artifact.get("default_threshold")))
    local_path = artifact_local_path(runtime, artifact)

    mode = payload.get("mode", "semi_auto")
    if mode not in {"semi_auto", "auto"}:
        raise PlatformError("mode must be semi_auto or auto")
    dry_run = bool(payload.get("dry_run", False))

    job = {
        "schema_version": 1,
        "request_id": request_id,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "mode": mode,
        "dry_run": dry_run,
        "status": "preflight",
        "request": {
            "target_devices": [d["display_id"] for d in devices],
            "algorithm_key": artifact["algorithm_key"],
            "version_label": artifact["version_label"],
            "channels": channels,
            "threshold": threshold,
            "reason": payload.get("reason", ""),
        },
        "plans": [],
        "results": [],
        "errors": [],
    }
    write_json(existing_path, job)

    for device in devices:
        try:
            with DeviceSession(device) as session:
                preflight = remote_preflight(session, artifact, channels, threshold)
            plan = build_plan(request_id, device, artifact, local_path, preflight, channels, threshold)
            if artifact.get("artifact_kind") != "rknn_ai_model":
                plan["warnings"].append("Automatic deployment is not enabled for service packages in this MVP.")
            job["plans"].append(plan)
        except Exception as exc:
            job["errors"].append({"device": device.get("display_id"), "error": type(exc).__name__, "message": str(exc)})

    if job["errors"]:
        job["status"] = "failed"
    elif dry_run:
        job["status"] = "dry_run_complete"
    elif mode == "semi_auto":
        job["status"] = "waiting_approval"
    elif mode == "auto":
        disallowed = [plan["device"]["display_id"] for plan in job["plans"] if not auto_allowed(next(d for d in devices if d["display_id"] == plan["device"]["display_id"]), artifact)]
        if disallowed:
            job["status"] = "blocked"
            job["errors"].append({"error": "PolicyBlocked", "message": f"Auto mode is not allowed for devices: {', '.join(disallowed)}"})
        else:
            execute_job(runtime, job)
    job["updated_at"] = utc_now()
    write_json(existing_path, job)
    return job


def execute_job(runtime: Path, job: dict[str, Any]) -> dict[str, Any]:
    catalog = load_catalog(runtime)
    artifact = resolve_artifact(catalog, job["request"]["algorithm_key"], job["request"]["version_label"])
    local_path = artifact_local_path(runtime, artifact)
    channels = validate_channels(job["request"].get("channels", []))
    threshold = validate_threshold(job["request"].get("threshold"))
    devices_by_display = {str(d["display_id"]): d for d in catalog.get("devices", [])}

    if artifact.get("artifact_kind") != "rknn_ai_model":
        job["status"] = "blocked"
        job["errors"].append({"error": "UnsupportedArtifactKind", "message": "Service package deployment is not enabled for automatic execution yet."})
        return job

    job["status"] = "deploying"
    job["updated_at"] = utc_now()
    write_json(job_path(runtime, job["request_id"]), job)

    for plan in job.get("plans", []):
        device = devices_by_display.get(str(plan["device"]["display_id"]))
        if not device:
            job["errors"].append({"error": "UnknownDevice", "message": str(plan["device"])})
            continue
        try:
            with DeviceSession(device) as session:
                result = deploy_ai_model(session, plan, artifact, local_path, threshold, channels)
            job["results"].append({"device": device["display_id"], "status": "succeeded", "result": result})
        except Exception as exc:
            job["results"].append({"device": device["display_id"], "status": "failed", "error": type(exc).__name__, "message": str(exc)})
            job["errors"].append({"device": device["display_id"], "error": type(exc).__name__, "message": str(exc)})

    job["status"] = "failed" if job["errors"] else "succeeded"
    job["updated_at"] = utc_now()
    write_json(job_path(runtime, job["request_id"]), job)
    return job


def approve_job(runtime: Path, request_id: str) -> dict[str, Any]:
    request_id = validate_request_id(request_id)
    path = job_path(runtime, request_id)
    if not path.exists():
        raise PlatformError(f"Release job not found: {request_id}")
    job = read_json(path)
    if job.get("status") != "waiting_approval":
        raise PlatformError(f"Release job is not waiting for approval: {job.get('status')}")
    return execute_job(runtime, job)


def cancel_job(runtime: Path, request_id: str, reason: str = "") -> dict[str, Any]:
    request_id = validate_request_id(request_id)
    path = job_path(runtime, request_id)
    if not path.exists():
        raise PlatformError(f"Release job not found: {request_id}")
    job = read_json(path)
    if job.get("status") not in {"waiting_approval", "dry_run_complete", "blocked"}:
        raise PlatformError(f"Release job cannot be cancelled from status: {job.get('status')}")
    job["status"] = "cancelled"
    job["cancelled_at"] = utc_now()
    job["cancel_reason"] = reason
    job["updated_at"] = utc_now()
    write_json(path, job)
    return job


def rollback_job(runtime: Path, request_id: str, dry_run: bool = False, reason: str = "") -> dict[str, Any]:
    request_id = validate_request_id(request_id)
    path = job_path(runtime, request_id)
    if not path.exists():
        raise PlatformError(f"Release job not found: {request_id}")
    job = read_json(path)
    if job.get("status") == "rolled_back":
        raise PlatformError("Release job has already been rolled back")
    if job.get("status") not in {"succeeded", "failed", "rollback_failed"}:
        raise PlatformError(f"Release job cannot be rolled back from status: {job.get('status')}")

    rollback_plans = rollback_plan_for_job(job)
    if not any(plan.get("has_actions") for plan in rollback_plans):
        raise PlatformError("Release job has no recorded rollback actions")

    if dry_run:
        preview = dict(job)
        preview["rollback_status"] = "dry_run_complete"
        preview["rollback_plan"] = rollback_plans
        return preview

    catalog = load_catalog(runtime)
    devices_by_display = {str(d["display_id"]): d for d in catalog.get("devices", [])}
    rollback_results = []
    errors = []
    for plan in rollback_plans:
        display_id = str((plan.get("device") or {}).get("display_id", ""))
        if not plan.get("has_actions"):
            rollback_results.append({"device": display_id, "status": "skipped", "reason": "no recorded rollback action"})
            continue
        device = devices_by_display.get(display_id)
        if not device:
            err = {"device": display_id, "error": "UnknownDevice", "message": "Device is missing from catalog"}
            rollback_results.append({"device": display_id, "status": "failed", **err})
            errors.append(err)
            continue
        try:
            with DeviceSession(device) as session:
                result = rollback_device(session, plan)
            rollback_results.append({"device": display_id, "status": "rolled_back", "result": result})
        except Exception as exc:
            err = {"device": display_id, "error": type(exc).__name__, "message": str(exc)}
            rollback_results.append({"device": display_id, "status": "failed", **err})
            errors.append(err)

    job["rollback_reason"] = reason
    job["rollback_results"] = rollback_results
    job["rollback_errors"] = errors
    job["rolled_back_at"] = utc_now()
    job["updated_at"] = utc_now()
    job["status"] = "rollback_failed" if errors else "rolled_back"
    write_json(path, job)
    return job


def list_jobs(runtime: Path) -> list[dict[str, Any]]:
    jobs_dir = runtime / "release-jobs"
    if not jobs_dir.exists():
        return []
    jobs = [read_json(path) for path in sorted(jobs_dir.glob("*.json"))]
    return sorted(jobs, key=lambda item: item.get("created_at", ""), reverse=True)


def parse_request_json(args: argparse.Namespace) -> dict[str, Any]:
    if args.request_file:
        return read_json(Path(args.request_file))
    if args.request_json:
        return json.loads(args.request_json)
    return {
        "request_id": args.request_id,
        "mode": args.mode,
        "target_devices": args.target_device,
        "algorithm_key": args.algorithm_key,
        "version_label": args.version_label,
        "channels": args.channel,
        "threshold": args.threshold,
        "dry_run": args.dry_run,
        "reason": args.reason or "",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME))
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--request-file")
    create.add_argument("--request-json")
    create.add_argument("--request-id")
    create.add_argument("--mode", default="semi_auto", choices=["semi_auto", "auto"])
    create.add_argument("--target-device", action="append", default=[])
    create.add_argument("--algorithm-key")
    create.add_argument("--version-label")
    create.add_argument("--channel", action="append", type=int, default=[])
    create.add_argument("--threshold", type=float)
    create.add_argument("--dry-run", action="store_true")
    create.add_argument("--reason")

    approve = sub.add_parser("approve")
    approve.add_argument("request_id")

    cancel = sub.add_parser("cancel")
    cancel.add_argument("request_id")
    cancel.add_argument("--reason", default="")

    rollback = sub.add_parser("rollback")
    rollback.add_argument("request_id")
    rollback.add_argument("--dry-run", action="store_true")
    rollback.add_argument("--reason", default="")

    sub.add_parser("list")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime = Path(args.runtime).expanduser().resolve()
    try:
        if args.command == "create":
            job = build_job(runtime, parse_request_json(args))
        elif args.command == "approve":
            job = approve_job(runtime, args.request_id)
        elif args.command == "cancel":
            job = cancel_job(runtime, args.request_id, args.reason)
        elif args.command == "rollback":
            job = rollback_job(runtime, args.request_id, args.dry_run, args.reason)
        elif args.command == "list":
            job = {"jobs": list_jobs(runtime)}
        else:
            raise PlatformError(f"Unknown command: {args.command}")
    except PlatformError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "job": job}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
