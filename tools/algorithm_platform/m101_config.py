#!/usr/bin/env python3
"""Runtime configuration API helpers for the m101 scene-change service."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import release_worker


CONFIG_DIR = "/oem/smart-gw/m101_scene_change"
CONFIG_PATH = f"{CONFIG_DIR}/config.json"
SERVICE_NAME = "m101-scene-change.service"
PUBLIC_CONFIG_KEYS = (
    "enabled",
    "channels",
    "interval_seconds",
    "confirm_delay_seconds",
    "consecutive_alarm_count",
    "alarm_cooldown_seconds",
    "change_threshold",
)
_DEVICE_LOCKS: dict[str, threading.Lock] = {}
_DEVICE_LOCKS_GUARD = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def device_lock(display_id: str) -> threading.Lock:
    with _DEVICE_LOCKS_GUARD:
        return _DEVICE_LOCKS.setdefault(display_id, threading.Lock())


def resolve_device(runtime: Path, identity: str) -> dict[str, Any]:
    catalog = release_worker.load_catalog(runtime)
    return release_worker.resolve_devices(catalog, [identity])[0]


def list_devices(runtime: Path) -> list[dict[str, Any]]:
    catalog = release_worker.load_catalog(runtime)
    result = []
    for device in catalog.get("devices", []):
        result.append(
            {
                "id": device.get("id"),
                "display_id": device.get("display_id"),
                "machine_code": device.get("machine_code"),
                "chip_family": device.get("chip_family"),
                "tags": device.get("tags", []),
            }
        )
    return sorted(result, key=lambda item: str(item.get("display_id") or ""))


def _number(
    value: Any,
    field: str,
    minimum: float,
    maximum: float,
    *,
    integer: bool = False,
) -> int | float:
    if isinstance(value, bool):
        raise release_worker.PlatformError(f"{field} must be a number")
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise release_worker.PlatformError(f"{field} must be a number") from exc
    if parsed < minimum or parsed > maximum:
        raise release_worker.PlatformError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def validate_update(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    allowed = set(PUBLIC_CONFIG_KEYS) | {"dry_run", "request_id"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise release_worker.PlatformError(f"Unsupported m101 config fields: {', '.join(unknown)}")

    changes: dict[str, Any] = {}
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise release_worker.PlatformError("enabled must be true or false")
        changes["enabled"] = payload["enabled"]
    if "channels" in payload:
        if not isinstance(payload["channels"], list):
            raise release_worker.PlatformError("channels must be an array")
        changes["channels"] = release_worker.validate_channels(payload["channels"])
    if "interval_seconds" in payload:
        changes["interval_seconds"] = _number(payload["interval_seconds"], "interval_seconds", 10, 86400, integer=True)
    if "confirm_delay_seconds" in payload:
        changes["confirm_delay_seconds"] = _number(payload["confirm_delay_seconds"], "confirm_delay_seconds", 0, 300)
    if "consecutive_alarm_count" in payload:
        changes["consecutive_alarm_count"] = _number(
            payload["consecutive_alarm_count"], "consecutive_alarm_count", 1, 10, integer=True
        )
    if "alarm_cooldown_seconds" in payload:
        changes["alarm_cooldown_seconds"] = _number(
            payload["alarm_cooldown_seconds"], "alarm_cooldown_seconds", 0, 86400, integer=True
        )
    if "change_threshold" in payload:
        changes["change_threshold"] = _number(payload["change_threshold"], "change_threshold", 0.1, 1.0)

    if not changes:
        raise release_worker.PlatformError("At least one m101 config field is required")
    if changes.get("enabled", True) and "channels" in changes and not changes["channels"]:
        raise release_worker.PlatformError("channels cannot be empty while m101 is enabled")
    return changes, release_worker.bool_payload(payload.get("dry_run"), False)


def validate_control(payload: dict[str, Any]) -> tuple[str, str, list[int], bool]:
    allowed = {"device", "action", "channels", "dry_run", "request_id"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise release_worker.PlatformError(f"Unsupported m101 control fields: {', '.join(unknown)}")
    device = str(payload.get("device") or "").strip()
    if not device:
        raise release_worker.PlatformError("device is required")
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"start", "stop"}:
        raise release_worker.PlatformError("action must be start or stop")
    raw_channels = payload.get("channels", [])
    if not isinstance(raw_channels, list):
        raise release_worker.PlatformError("channels must be an array")
    channels = release_worker.validate_channels(raw_channels)
    if action == "start" and not channels:
        raise release_worker.PlatformError("channels is required for start")
    return device, action, channels, release_worker.bool_payload(payload.get("dry_run"), False)


def _inspect_code() -> str:
    return f"""
import json, os, subprocess, urllib.request

config_path = {json.dumps(CONFIG_PATH)}
service_name = {json.dumps(SERVICE_NAME)}
public_keys = {json.dumps(list(PUBLIC_CONFIG_KEYS))}

def command(args):
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=8)
        return {{'rc': proc.returncode, 'value': (proc.stdout.strip() or proc.stderr.strip())}}
    except Exception as exc:
        return {{'rc': None, 'value': type(exc).__name__ + ': ' + str(exc)}}

def processes():
    result = []
    needle = '/oem/smart-gw/m101_scene_change/m101_scene_change_service.py'
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        if int(name) == os.getpid():
            continue
        try:
            with open('/proc/' + name + '/cmdline', 'rb') as fh:
                raw_args = [item for item in fh.read().split(b'\\x00') if item]
        except Exception:
            continue
        args = [item.decode('utf-8', 'replace') for item in raw_args]
        if needle in args:
            cmdline = ' '.join(args)
            result.append({{'pid': int(name), 'cmdline': cmdline}})
    return sorted(result, key=lambda item: item['pid'])

config = {{}}
config_error = None
if os.path.exists(config_path):
    try:
        with open(config_path, encoding='utf-8') as fh:
            config = json.load(fh)
    except Exception as exc:
        config_error = type(exc).__name__ + ': ' + str(exc)

inventory = []
inventory_error = None
try:
    with urllib.request.urlopen('http://127.0.0.1/api/v1/system/channels/mag', timeout=8) as resp:
        payload = json.loads(resp.read(1024 * 1024).decode('utf-8', 'replace'))
    for item in payload.get('result') or []:
        try:
            ch_no = int(item.get('chNo'))
        except Exception:
            continue
        inventory.append({{
            'ch_no': ch_no,
            'name': str(item.get('location') or item.get('name') or ('通道' + str(ch_no))),
            'switch': int(item.get('switch') or 0),
            'status': int(item.get('status') or 0),
        }})
except Exception as exc:
    inventory_error = type(exc).__name__ + ': ' + str(exc)

active = command(['systemctl', 'is-active', service_name])
enabled = command(['systemctl', 'is-enabled', service_name])
procs = processes()
installed = os.path.isfile(config_path) and os.path.isfile('/oem/smart-gw/m101_scene_change/m101_scene_change_service.py')
print(json.dumps({{
    'installed': installed,
    'config_error': config_error,
    'config': {{key: config.get(key) for key in public_keys}},
    'channel_inventory': sorted(inventory, key=lambda item: item['ch_no']),
    'channel_inventory_error': inventory_error,
    'service': {{'active': active, 'enabled': enabled}},
    'processes': procs,
    'healthy': bool(installed and not config_error and active.get('value') == 'active' and len(procs) == 1),
}}, ensure_ascii=False))
"""


def inspect_device(runtime: Path, identity: str) -> dict[str, Any]:
    device = resolve_device(runtime, identity)
    with release_worker.DeviceSession(device) as session:
        state = release_worker.remote_json(session, _inspect_code(), timeout=30)
    state["device"] = {
        "id": device.get("id"),
        "display_id": device.get("display_id"),
        "machine_code": device.get("machine_code"),
        "chip_family": device.get("chip_family"),
    }
    return state


def _apply_code(
    changes: dict[str, Any],
    dry_run: bool,
    *,
    channel_action: str | None = None,
    action_channels: list[int] | None = None,
) -> str:
    return f"""
import json, os, shutil, subprocess, time, urllib.request
from datetime import datetime

config_dir = {json.dumps(CONFIG_DIR)}
config_path = {json.dumps(CONFIG_PATH)}
service_name = {json.dumps(SERVICE_NAME)}
changes = {repr(changes)}
dry_run = {repr(dry_run)}
channel_action = {repr(channel_action)}
action_channels = {repr(action_channels or [])}
public_keys = {json.dumps(list(PUBLIC_CONFIG_KEYS))}

if not os.path.isfile(config_path):
    raise RuntimeError('m101 config is not installed')
with open(config_path, encoding='utf-8') as fh:
    old_config = json.load(fh)
new_config = dict(old_config)
new_config.update(changes)
before_channels = sorted({{int(item) for item in old_config.get('channels') or []}})
if channel_action == 'start':
    new_config['channels'] = sorted(set(before_channels) | set(action_channels))
    new_config['enabled'] = True
elif channel_action == 'stop':
    if action_channels:
        new_config['channels'] = sorted(set(before_channels) - set(action_channels))
    else:
        new_config['channels'] = []
    if not new_config['channels']:
        new_config['enabled'] = False
if new_config.get('enabled', True) and not new_config.get('channels'):
    raise RuntimeError('m101 cannot be enabled without channels')

inventory = []
inventory_warning = None
try:
    with urllib.request.urlopen('http://127.0.0.1/api/v1/system/channels/mag', timeout=8) as resp:
        payload = json.loads(resp.read(1024 * 1024).decode('utf-8', 'replace'))
    inventory = sorted({{int(item.get('chNo')) for item in payload.get('result') or [] if item.get('chNo') is not None}})
except Exception as exc:
    inventory_warning = type(exc).__name__ + ': ' + str(exc)
if inventory:
    missing = sorted(set(new_config.get('channels') or []) - set(inventory))
    if missing:
        raise RuntimeError('requested channels are not present on device: ' + ','.join(str(item) for item in missing))

changed_keys = [key for key in public_keys if old_config.get(key) != new_config.get(key)]
changed = bool(changed_keys)
result = {{
    'dry_run': dry_run,
    'action': channel_action,
    'action_channels': action_channels,
    'before_channels': before_channels,
    'changed': changed,
    'changed_keys': changed_keys,
    'requested': changes,
    'config': {{key: new_config.get(key) for key in public_keys}},
    'channel_inventory': inventory,
    'channel_inventory_warning': inventory_warning,
    'backup_path': None,
    'service': None,
    'process_count': None,
    'rolled_back': False,
    'rollback_service': None,
    'apply_error': None,
}}
if dry_run or not changed:
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0)

backup_dir = os.path.join(config_dir, 'backups')
os.makedirs(backup_dir, exist_ok=True)
stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
backup_path = os.path.join(backup_dir, 'customer-config-' + stamp + '.json')
shutil.copy2(config_path, backup_path)
tmp_path = config_path + '.tmp-' + stamp
with open(tmp_path, 'w', encoding='utf-8') as fh:
    json.dump(new_config, fh, ensure_ascii=False, indent=2)
    fh.write('\\n')
os.replace(tmp_path, config_path)
result['backup_path'] = backup_path

def process_count():
    count = 0
    needle = '/oem/smart-gw/m101_scene_change/m101_scene_change_service.py'
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        if int(name) == os.getpid():
            continue
        try:
            with open('/proc/' + name + '/cmdline', 'rb') as fh:
                raw_args = [item for item in fh.read().split(b'\\x00') if item]
        except Exception:
            continue
        args = [item.decode('utf-8', 'replace') for item in raw_args]
        if needle in args:
            count += 1
    return count

try:
    restart = subprocess.run(['systemctl', 'restart', service_name], text=True, capture_output=True, timeout=30)
    if restart.returncode != 0:
        raise RuntimeError('targeted service restart failed')
    time.sleep(2)
    active = subprocess.run(['systemctl', 'is-active', service_name], text=True, capture_output=True, timeout=10)
    count = process_count()
    result['service'] = {{'active': active.stdout.strip(), 'rc': active.returncode}}
    result['process_count'] = count
    if active.returncode != 0 or active.stdout.strip() != 'active' or count != 1:
        raise RuntimeError('m101 verification failed after targeted restart')
except Exception as exc:
    shutil.copy2(backup_path, config_path)
    rollback_restart = subprocess.run(
        ['systemctl', 'restart', service_name], text=True, capture_output=True, timeout=30
    )
    result['rolled_back'] = True
    result['rollback_service'] = {{
        'restart_rc': rollback_restart.returncode,
        'active': subprocess.run(
            ['systemctl', 'is-active', service_name], text=True, capture_output=True, timeout=10
        ).stdout.strip(),
        'process_count': process_count(),
    }}
    result['apply_error'] = type(exc).__name__ + ': ' + str(exc)

print(json.dumps(result, ensure_ascii=False))
"""


def audit_path(runtime: Path, request_id: str) -> Path:
    safe = release_worker.validate_request_id(request_id)
    return runtime / "m101-config-jobs" / f"{safe}.json"


def update_device(runtime: Path, identity: str, payload: dict[str, Any]) -> dict[str, Any]:
    device = resolve_device(runtime, identity)
    display_id = str(device["display_id"])
    changes, dry_run = validate_update(payload)
    request_id = payload.get("request_id")
    if request_id is None:
        request_id = "m101-config-" + display_id + "-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    path = audit_path(runtime, str(request_id))

    with device_lock(display_id):
        if path.exists():
            return release_worker.read_json(path)
        job: dict[str, Any] = {
            "request_id": str(request_id),
            "device": display_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "status": "validating" if dry_run else "applying",
            "dry_run": dry_run,
            "changes": changes,
        }
        release_worker.write_json(path, job)
        try:
            with release_worker.DeviceSession(device) as session:
                result = release_worker.remote_json(session, _apply_code(changes, dry_run), timeout=60)
            job["result"] = result
            if result.get("apply_error"):
                job["status"] = "failed"
                job["error"] = {
                    "type": "RemoteApplyError",
                    "message": str(result["apply_error"]),
                    "rolled_back": bool(result.get("rolled_back")),
                }
            else:
                job["status"] = "validated" if dry_run else ("unchanged" if not result.get("changed") else "succeeded")
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = {"type": type(exc).__name__, "message": str(exc)}
        job["updated_at"] = utc_now()
        release_worker.write_json(path, job)
        return job


def control_device(runtime: Path, payload: dict[str, Any]) -> dict[str, Any]:
    identity, action, channels, dry_run = validate_control(payload)
    device = resolve_device(runtime, identity)
    display_id = str(device["display_id"])
    request_id = payload.get("request_id")
    if request_id is None:
        request_id = "m101-control-" + display_id + "-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
    path = audit_path(runtime, str(request_id))

    with device_lock(display_id):
        if path.exists():
            return release_worker.read_json(path)
        job: dict[str, Any] = {
            "request_id": str(request_id),
            "device": display_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "status": "validating" if dry_run else "applying",
            "dry_run": dry_run,
            "action": action,
            "channels": channels,
        }
        release_worker.write_json(path, job)
        try:
            code = _apply_code(
                {},
                dry_run,
                channel_action=action,
                action_channels=channels,
            )
            with release_worker.DeviceSession(device) as session:
                result = release_worker.remote_json(session, code, timeout=60)
            job["result"] = result
            if result.get("apply_error"):
                job["status"] = "failed"
                job["error"] = {
                    "type": "RemoteApplyError",
                    "message": str(result["apply_error"]),
                    "rolled_back": bool(result.get("rolled_back")),
                }
            else:
                job["status"] = "validated" if dry_run else ("unchanged" if not result.get("changed") else "succeeded")
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = {"type": type(exc).__name__, "message": str(exc)}
        job["updated_at"] = utc_now()
        release_worker.write_json(path, job)
        return job
