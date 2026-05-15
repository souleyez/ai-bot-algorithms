#!/usr/bin/env python3
"""AI-BOT algorithm release worker.

This module implements the platform release primitive used by both CLI and
HTTP API entrypoints. It supports real deployment for approved RKNN `.ai`
artifacts and approved device service packages such as m101 scene change.

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
RKNN_AI_MODEL = "rknn_ai_model"
DEVICE_SERVICE_PACKAGE = "device_service_package"
DEVICE_ALGORITHM_DIRECTORY = "device_algorithm_directory"
DEPLOYABLE_ARTIFACT_KINDS = {RKNN_AI_MODEL, DEVICE_SERVICE_PACKAGE, DEVICE_ALGORITHM_DIRECTORY}
PUBLIC_ALGORITHM_ALIASES = {
    "security_guard": ["保安", "保安服", "保安识别", "保安服识别", "保安检测"],
    "cleaner": ["保洁", "保洁识别", "保洁检测"],
    "engineering_worker": ["维修", "维修识别", "工程", "工程人员", "工程人员识别"],
    "scene_change": ["位移", "画面位移", "画面移动", "画面变化", "画面巡检"],
}
PUBLIC_NAME_ALIASES = {
    "简版串岗": ["串岗", "简版串岗", "脱岗串岗", "串岗算法", "脱岗串岗算法"],
    "车牌": ["车牌", "车牌识别", "车牌检测"],
}
INSTALL_ALGORITHM_FIELDS = (
    "algorithm_key",
    "algorithm",
    "algorithm_name",
    "desired_algorithm",
    "expected_algorithm",
    "model",
    "model_name",
    "geid",
)


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


def compact_slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "model"


def normalize_algorithm_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = re.sub(r"[\s_\-./\\:：,，;；()（）\[\]【】]+", "", text)
    return text or None


def normalize_chip_family(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", "").replace("_", "")
    if not text:
        return None
    if "rk3576" in text:
        return "rk3576"
    if "rk3588" in text:
        return "rk3588"
    if "rv1126" in text or "rv1109" in text:
        return "rv1126"
    return text


def requested_chip_family(payload: dict[str, Any] | None, device: dict[str, Any] | None = None) -> str | None:
    payload = payload or {}
    chip = normalize_chip_family(payload.get("chip_family", payload.get("chip_model", payload.get("chip"))))
    if chip:
        return chip
    if device:
        return normalize_chip_family(device.get("chip_family"))
    return None


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


def extracted_models_by_md5(runtime: Path) -> dict[str, list[dict[str, Any]]]:
    report_path = runtime / "device-extract" / "extraction-report.json"
    if not report_path.exists():
        return {}
    report = read_json(report_path)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for device_item in report.get("devices", []):
        device = device_item.get("device") or {}
        display_id = device.get("display_id")
        for model in device_item.get("models", []):
            md5 = model.get("md5")
            if not md5:
                continue
            item = dict(model)
            item["source_device"] = display_id
            grouped.setdefault(str(md5), []).append(item)
    return grouped


def artifact_overrides(runtime: Path) -> dict[str, Any]:
    path = runtime / "artifact-overrides.json"
    if not path.exists():
        return {}
    try:
        return read_json(path)
    except Exception:
        return {}


def hidden_artifact_md5s(runtime: Path) -> set[str]:
    overrides = artifact_overrides(runtime)
    hidden = set()
    for item in overrides.get("hidden_md5", []):
        if isinstance(item, str):
            hidden.add(item)
        elif isinstance(item, dict) and item.get("md5"):
            hidden.add(str(item["md5"]))
    return hidden


def extracted_directories_by_md5(runtime: Path) -> dict[str, list[dict[str, Any]]]:
    report_path = runtime / "device-extract" / "extraction-report.json"
    if not report_path.exists():
        return {}
    report = read_json(report_path)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for device_item in report.get("devices", []):
        device = device_item.get("device") or {}
        display_id = device.get("display_id")
        for package in device_item.get("directories", []):
            md5 = package.get("archive_md5")
            if not md5:
                continue
            item = dict(package)
            item["source_device"] = display_id
            grouped.setdefault(str(md5), []).append(item)
    return grouped


def most_common(values: list[Any]) -> Any:
    counts: dict[Any, int] = {}
    for value in values:
        if value is None or value == "":
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))[0][0]


def extracted_artifacts(runtime: Path, catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    catalog = catalog or load_catalog(runtime)
    hidden_md5 = hidden_artifact_md5s(runtime)
    approved_by_md5 = {
        item.get("md5"): item
        for item in catalog.get("artifacts", [])
        if item.get("md5") and item.get("status") == "approved"
    }
    artifacts = []
    for md5, models in sorted(extracted_models_by_md5(runtime).items()):
        if md5 in hidden_md5:
            continue
        if not models:
            continue
        representative = sorted(
            models,
            key=lambda item: (
                str((item.get("engine") or {}).get("name") or item.get("filename") or ""),
                str(item.get("slot") or ""),
                str(item.get("source_device") or ""),
            ),
        )[0]
        names = sorted({str((item.get("engine") or {}).get("name")) for item in models if (item.get("engine") or {}).get("name")})
        slots = sorted({str(item.get("slot")) for item in models if item.get("slot")})
        geids = sorted({int(item.get("geid")) for item in models if isinstance(item.get("geid"), int)})
        source_devices = sorted({str(item.get("source_device")) for item in models if item.get("source_device")})
        approved = approved_by_md5.get(md5)
        if approved:
            artifact = dict(approved)
            artifact["source"] = "catalog_and_device_extract"
            artifact["candidate_slots"] = slots
            artifact["candidate_geids"] = geids
            artifact["engine_names"] = names
            artifact["source_devices"] = source_devices
            artifacts.append(artifact)
            continue

        filename = str(representative.get("filename") or Path(str(representative.get("remote_path") or "model.ai")).name)
        slot = representative.get("slot")
        geid = representative.get("geid")
        display_name = most_common([(item.get("engine") or {}).get("name") for item in models]) or Path(filename).stem
        storage_rel = representative.get("storage_relative_path")
        if storage_rel:
            storage_rel = "device-extract/" + str(storage_rel).lstrip("/")
        key_prefix = f"extracted_{geid}" if geid is not None else f"extracted_{slot or 'model'}"
        artifacts.append(
            {
                "id": f"device-extract-{md5[:12]}",
                "algorithm_key": f"{key_prefix}_{compact_slug(str(display_name))}_{md5[:8]}",
                "display_name": str(display_name),
                "artifact_kind": "rknn_ai_model",
                "slot": slot,
                "geid": geid,
                "chip_family": "rk3576",
                "version_label": f"device-{md5[:8]}",
                "status": "approved",
                "md5": md5,
                "sha256": None,
                "size_bytes": representative.get("size_bytes"),
                "source": "device_extract",
                "source_filename": filename,
                "remote_model_path": f"/models/{slot}/{filename}" if slot else None,
                "default_threshold": representative.get("threshold"),
                "storage_relative_path": storage_rel,
                "candidate_slots": slots,
                "candidate_geids": geids,
                "engine_names": names,
                "source_devices": source_devices,
                "notes": "Extracted from existing AI-BOT boxes. Review slot/geid semantics before broad rollout.",
            }
        )
    for md5, packages in sorted(extracted_directories_by_md5(runtime).items()):
        if md5 in hidden_md5:
            continue
        if not packages:
            continue
        representative = sorted(
            packages,
            key=lambda item: (
                str((item.get("engine") or {}).get("name") or ((item.get("base") or {}).get("name") if isinstance(item.get("base"), dict) else "") or item.get("slot") or ""),
                str(item.get("chip_family") or ""),
                str(item.get("slot") or ""),
                str(item.get("source_device") or ""),
            ),
        )[0]
        base = representative.get("base") if isinstance(representative.get("base"), dict) else {}
        names = sorted(
            {
                str((item.get("engine") or {}).get("name") or ((item.get("base") or {}).get("name") if isinstance(item.get("base"), dict) else ""))
                for item in packages
                if (item.get("engine") or {}).get("name") or (isinstance(item.get("base"), dict) and item.get("base", {}).get("name"))
            }
        )
        slots = sorted({str(item.get("slot")) for item in packages if item.get("slot")})
        geids = sorted({int(item.get("geid")) for item in packages if isinstance(item.get("geid"), int)})
        source_devices = sorted({str(item.get("source_device")) for item in packages if item.get("source_device")})
        chip_family = normalize_chip_family(representative.get("chip_family")) or "unknown"
        chip_families = sorted(
            {
                chip
                for chip in (normalize_chip_family(item.get("chip_family")) for item in packages)
                if chip
            }
        )
        approved = approved_by_md5.get(md5)
        if approved:
            artifact = dict(approved)
            artifact["source"] = "catalog_and_device_extract"
            artifact["candidate_slots"] = slots
            artifact["candidate_geids"] = geids
            artifact["engine_names"] = names
            artifact["source_devices"] = source_devices
            artifact["chip_family"] = normalize_chip_family(artifact.get("chip_family")) or chip_family
            artifact["compatible_chip_families"] = sorted(set(artifact.get("compatible_chip_families") or chip_families or [artifact["chip_family"]]))
            artifacts.append(artifact)
            continue

        slot = representative.get("slot")
        geid = representative.get("geid")
        display_name = most_common(
            [
                (item.get("engine") or {}).get("name")
                or ((item.get("base") or {}).get("name") if isinstance(item.get("base"), dict) else None)
                for item in packages
            ]
        ) or str(slot or "算法目录")
        version = most_common(
            [
                (item.get("engine") or {}).get("version")
                or ((item.get("base") or {}).get("version") if isinstance(item.get("base"), dict) else None)
                for item in packages
            ]
        )
        storage_rel = representative.get("storage_relative_path")
        if storage_rel:
            storage_rel = "device-extract/" + str(storage_rel).lstrip("/")
        key_prefix = f"extracted_dir_{geid}" if geid is not None else f"extracted_dir_{slot or 'model'}"
        nested_ai = representative.get("nested_ai") or []
        artifacts.append(
            {
                "id": f"device-dir-{md5[:12]}",
                "algorithm_key": f"{key_prefix}_{compact_slug(str(display_name))}_{chip_family}_{md5[:8]}",
                "display_name": str(display_name),
                "artifact_kind": DEVICE_ALGORITHM_DIRECTORY,
                "slot": slot,
                "geid": geid,
                "chip_family": chip_family,
                "compatible_chip_families": chip_families or [chip_family],
                "version_label": f"dir-{chip_family}-{compact_slug(str(version or 'unknown'))}-{md5[:8]}",
                "status": "approved",
                "md5": md5,
                "sha256": None,
                "size_bytes": representative.get("archive_size_bytes"),
                "source": "device_directory_extract",
                "source_filename": Path(str(storage_rel or f"{slot}.tar.gz")).name,
                "remote_model_path": f"/models/{slot}" if slot else None,
                "default_threshold": representative.get("threshold"),
                "storage_relative_path": storage_rel,
                "candidate_slots": slots,
                "candidate_geids": geids,
                "engine_names": names,
                "source_devices": source_devices,
                "nested_ai": nested_ai,
                "has_nn_server": representative.get("has_nn_server"),
                "has_dposter": representative.get("has_dposter"),
                "license_required": bool(geid is not None),
                "notes": "Complete /models algorithm directory extracted from an existing box. Deploy only to matching chip family and licensed boxes.",
            }
        )
    return artifacts


def chip_matches(artifact: dict[str, Any], chip_family: str | None) -> bool:
    if not chip_family:
        return True
    family = normalize_chip_family(artifact.get("chip_family"))
    compatibles = {
        chip
        for chip in (normalize_chip_family(item) for item in artifact.get("compatible_chip_families", []) or [])
        if chip
    }
    if family:
        compatibles.add(family)
    return chip_family in compatibles


def public_algorithm_aliases(artifact: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for key in ("algorithm_key", "display_name", "slot", "source_filename"):
        value = artifact.get(key)
        if value is not None:
            aliases.append(str(value))
    if artifact.get("geid") is not None:
        aliases.append(str(artifact["geid"]))
    aliases.extend(str(item) for item in artifact.get("aliases", []) or [])
    aliases.extend(PUBLIC_ALGORITHM_ALIASES.get(str(artifact.get("algorithm_key")), []))

    display_name = str(artifact.get("display_name") or "")
    for marker, marker_aliases in PUBLIC_NAME_ALIASES.items():
        if marker in display_name:
            aliases.extend(marker_aliases)

    seen: set[str] = set()
    result: list[str] = []
    for alias in aliases:
        text = alias.strip()
        token = normalize_algorithm_text(text)
        if not text or not token or token in seen:
            continue
        seen.add(token)
        result.append(text)
    return result


def artifact_alias_tokens(artifact: dict[str, Any]) -> set[str]:
    return {
        token
        for token in (normalize_algorithm_text(alias) for alias in public_algorithm_aliases(artifact))
        if token
    }


def deployable_artifacts(runtime: Path, catalog: dict[str, Any]) -> list[dict[str, Any]]:
    extracted = extracted_artifacts(runtime, catalog)
    extracted_md5 = {item.get("md5") for item in extracted if item.get("md5")}
    artifacts = list(extracted)
    for artifact in catalog.get("artifacts", []):
        if (
            artifact.get("status") != "approved"
            or artifact.get("artifact_kind") not in DEPLOYABLE_ARTIFACT_KINDS
            or artifact.get("md5") in extracted_md5
            or artifact.get("md5") in hidden_artifact_md5s(runtime)
        ):
            continue
        artifacts.append(dict(artifact, source="catalog"))
    return artifacts


def resolve_deploy_artifact(
    runtime: Path,
    catalog: dict[str, Any],
    algorithm_key: str,
    version_label: str | None,
    chip_family: str | None = None,
) -> dict[str, Any]:
    try:
        artifact = resolve_artifact(catalog, algorithm_key, version_label)
        if chip_family and not chip_matches(artifact, chip_family):
            raise PlatformError(f"Artifact chip_family={artifact.get('chip_family')} does not match requested chip_family={chip_family}")
        return artifact
    except PlatformError as original:
        candidates = [item for item in extracted_artifacts(runtime, catalog) if item.get("algorithm_key") == algorithm_key]
        if version_label:
            candidates = [item for item in candidates if item.get("version_label") == version_label]
        if chip_family:
            candidates = [item for item in candidates if chip_matches(item, chip_family)]
        if not candidates:
            raise original
        if len(candidates) > 1:
            labels = ", ".join(sorted(str(item.get("version_label")) for item in candidates))
            chips = ", ".join(sorted({str(item.get("chip_family")) for item in candidates}))
            raise PlatformError(f"Multiple extracted artifacts match {algorithm_key}; specify version_label and chip_family. Candidates: {labels}; chips: {chips}")
        return candidates[0]


def requested_algorithm_text(payload: dict[str, Any]) -> str:
    for field in INSTALL_ALGORITHM_FIELDS:
        value = payload.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise PlatformError("algorithm is required")


def resolve_install_artifact(
    runtime: Path,
    catalog: dict[str, Any],
    algorithm_request: str,
    version_label: str | None,
    chip_family: str | None = None,
) -> dict[str, Any]:
    try:
        return resolve_deploy_artifact(runtime, catalog, algorithm_request, version_label, chip_family)
    except PlatformError as exact_error:
        query = normalize_algorithm_text(algorithm_request)
        if not query:
            raise PlatformError("algorithm is required") from exact_error

        candidates = [item for item in deployable_artifacts(runtime, catalog) if chip_matches(item, chip_family)]
        if version_label:
            candidates = [item for item in candidates if item.get("version_label") == version_label]
        matches = [item for item in candidates if query in artifact_alias_tokens(item)]
        if not matches:
            chip_note = f" for chip_family={chip_family}" if chip_family else ""
            raise PlatformError(f"No deployable artifact matched algorithm request: {algorithm_request}{chip_note}") from exact_error
        if len(matches) > 1:
            labels = ", ".join(
                sorted(
                    f"{item.get('display_name')}({item.get('algorithm_key')}, {item.get('version_label')}, {item.get('chip_family')})"
                    for item in matches
                )
            )
            raise PlatformError(f"Multiple artifacts matched algorithm request {algorithm_request}; specify algorithm_key or version_label. Candidates: {labels}")
        return matches[0]


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
    artifact_kind = artifact.get("artifact_kind")
    code = f"""
import glob, hashlib, json, os, sqlite3, subprocess, urllib.request

slot = {json.dumps(slot)}
model_path = {json.dumps(model_path)}
artifact_kind = {json.dumps(artifact_kind)}
channels = {json.dumps(channels)}
threshold = {repr(threshold)}

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

def procs_for_slot(slot, model_path, artifact_kind):
    procs = []
    prefixes = ['/models/' + slot]
    if artifact_kind == 'device_service_package' and model_path:
        prefixes.append(model_path)
    for pid in sorted([p for p in os.listdir('/proc') if p.isdigit()], key=lambda x: int(x)):
        try:
            cwd = os.readlink('/proc/' + pid + '/cwd')
        except Exception:
            cwd = ''
        try:
            with open('/proc/' + pid + '/cmdline', 'rb') as fh:
                cmdline = fh.read().replace(b'\\x00', b' ').decode('utf-8', 'replace').strip()
        except Exception:
            cmdline = ''
        if any(cwd.startswith(prefix) for prefix in prefixes) or (artifact_kind == 'device_service_package' and slot and slot in cmdline):
            procs.append({{'pid': int(pid), 'cwd': cwd, 'cmdline': cmdline}})
    return procs

def service_status(unit):
    result = {{'unit': unit}}
    for key, cmd in {{
        'active': ['systemctl', 'is-active', unit],
        'enabled': ['systemctl', 'is-enabled', unit],
    }}.items():
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=5)
            result[key] = proc.stdout.strip() or proc.stderr.strip()
            result[key + '_rc'] = proc.returncode
        except Exception as exc:
            result[key] = type(exc).__name__ + ': ' + str(exc)
            result[key + '_rc'] = None
    try:
        proc = subprocess.run(['systemctl', 'show', unit, '--property=ActiveState,SubState,FragmentPath'], text=True, capture_output=True, timeout=5)
        result['show'] = proc.stdout.strip()
        result['show_rc'] = proc.returncode
    except Exception as exc:
        result['show'] = type(exc).__name__ + ': ' + str(exc)
        result['show_rc'] = None
    return result

def dmg_bindings_for_slot(slot):
    result = {{'models': [], 'classes': [], 'error': None}}
    db_path = '/oem/smart-gw/db/dmg.db'
    if not os.path.exists(db_path):
        result['error'] = 'missing_dmg_db'
        return result
    model_id = None
    if isinstance(slot, str) and slot.startswith('m') and slot[1:].isdigit():
        model_id = int(slot[1:])
    if model_id is None:
        result['error'] = 'slot_without_numeric_model_id'
        return result
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        rows = cur.execute(
            'select id, channelId, modelId, chNo from channel_ai_models where modelId=? order by chNo',
            (model_id,),
        ).fetchall()
        class_rows = cur.execute(
            'select id, channelId, chNo, modelId, channelAiModelId, classId from channel_ai_model_classes where modelId=? order by chNo',
            (model_id,),
        ).fetchall()
        con.close()
        result['models'] = [
            {{'id': row[0], 'channel_id': row[1], 'model_id': row[2], 'channel': row[3]}}
            for row in rows
        ]
        result['classes'] = [
            {{'id': row[0], 'channel_id': row[1], 'channel': row[2], 'model_id': row[3], 'channel_ai_model_id': row[4], 'class_id': row[5]}}
            for row in class_rows
        ]
    except Exception as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
    return result

existing = None
if model_path and os.path.exists(model_path):
    st = os.stat(model_path)
    if os.path.isfile(model_path):
        existing = {{'path': model_path, 'md5': md5(model_path), 'size_bytes': st.st_size, 'mtime': int(st.st_mtime), 'kind': 'file'}}
    elif os.path.isdir(model_path):
        manifest = {{}}
        for rel in ['m101_scene_change_service.py', 'config.json', 'base.json', 'nn.json', 'nn.extend.json', 'nn_server/args.json', 'nn_server/main.py']:
            path = os.path.join(model_path, rel)
            if os.path.isfile(path):
                manifest[rel] = {{'md5': md5(path), 'size_bytes': os.stat(path).st_size, 'mtime': int(os.stat(path).st_mtime)}}
        service_file = '/etc/systemd/system/m101-scene-change.service'
        if os.path.isfile(service_file):
            manifest[service_file] = {{'md5': md5(service_file), 'size_bytes': os.stat(service_file).st_size, 'mtime': int(os.stat(service_file).st_mtime)}}
        existing = {{'path': model_path, 'kind': 'directory', 'size_bytes': st.st_size, 'mtime': int(st.st_mtime), 'manifest': manifest}}

freq = []
for path in sorted(glob.glob('/oem/smart-gw/chma/' + slot + '/ch*/freq.json')):
    ch = None
    try:
        ch = int(os.path.basename(os.path.dirname(path)).replace('ch', ''))
    except Exception:
        pass
    freq.append({{'channel': ch, 'path': path, 'freq': read_json(path)}})

extend_path = '/models/' + slot + '/nn.extend.json'
dmg_bindings = dmg_bindings_for_slot(slot)
print(json.dumps({{
    'slot': slot,
    'model_path': model_path,
    'existing_model': existing,
    'nn_extend': read_json(extend_path) if os.path.exists(extend_path) else None,
    'channel_bindings': freq,
    'dmg_channel_bindings': dmg_bindings.get('models', []),
    'dmg_class_bindings': dmg_bindings.get('classes', []),
    'dmg_binding_error': dmg_bindings.get('error'),
    'requested_channels': channels,
    'requested_threshold': threshold,
    'processes': procs_for_slot(slot, model_path, artifact_kind),
    'service': service_status('m101-scene-change.service') if artifact_kind == 'device_service_package' else None,
    'license_exists': os.path.exists('/root/.mm/' + slot + '.lic') if isinstance(slot, str) else None,
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
    if artifact.get("artifact_kind") == DEVICE_SERVICE_PACKAGE:
        model_action = "install_service_package"
    elif artifact.get("artifact_kind") == DEVICE_ALGORITHM_DIRECTORY:
        model_action = "install_algorithm_directory"
    else:
        model_action = "skip_same_md5" if existing_md5 == artifact.get("md5") else "replace_model"
    freq_bound_channels = {
        item.get("channel")
        for item in preflight.get("channel_bindings", [])
        if isinstance(item.get("channel"), int)
    }
    dmg_bound_channels = {
        item.get("channel")
        for item in preflight.get("dmg_channel_bindings", [])
        if isinstance(item.get("channel"), int)
    }
    bound_channels = freq_bound_channels | dmg_bound_channels
    channels_to_add = sorted([ch for ch in channels if ch not in bound_channels])
    if artifact.get("artifact_kind") == DEVICE_SERVICE_PACKAGE:
        backup_path = f"{remote_model_path}/backups/platform-{request_id}-{stamp}" if remote_model_path else None
        upload_tmp = f"/tmp/{request_id}-{local_path.name}"
    else:
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
        "channel_action": {
            "requested": channels,
            "already_bound": sorted(bound_channels),
            "freq_bound": sorted(freq_bound_channels),
            "dmg_bound": sorted(dmg_bound_channels),
            "channels_to_add": channels_to_add,
        },
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


def requested_threshold(payload: dict[str, Any], artifact: dict[str, Any]) -> float | None:
    value = payload.get("threshold")
    if value is None:
        value = artifact.get("default_threshold")
    return validate_threshold(value)


def make_install_request_id(device: dict[str, Any], artifact: dict[str, Any]) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"install-{device['display_id']}-{artifact['algorithm_key']}-{stamp}"


def bool_payload(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def requested_install_device(payload: dict[str, Any]) -> str:
    value = payload.get("device", payload.get("target_device", payload.get("machine")))
    if value is None and isinstance(payload.get("target_devices"), list) and len(payload["target_devices"]) == 1:
        value = payload["target_devices"][0]
    if value is None:
        raise PlatformError("device is required")
    return str(value)


def extract_engine_list(preflight: dict[str, Any]) -> list[dict[str, Any]] | None:
    engine_payload = preflight.get("algorithm_engine")
    if not isinstance(engine_payload, dict) or engine_payload.get("error"):
        return None
    result = engine_payload.get("result")
    for container in (result, engine_payload.get("data"), engine_payload):
        if isinstance(container, dict):
            for key in ("engines", "list", "models", "items"):
                if isinstance(container.get(key), list):
                    return [item for item in container[key] if isinstance(item, dict)]
    if isinstance(engine_payload.get("engines"), list):
        return [item for item in engine_payload["engines"] if isinstance(item, dict)]
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(engine_payload.get("data"), list):
        return [item for item in engine_payload["data"] if isinstance(item, dict)]
    return None


def extract_model_limit(preflight: dict[str, Any]) -> int | None:
    model_payload = preflight.get("modelN")
    if not isinstance(model_payload, dict) or model_payload.get("error"):
        return None
    for container in (model_payload, model_payload.get("result"), model_payload.get("data")):
        if not isinstance(container, dict):
            continue
        for key in ("model_n", "modelN", "model_num", "modelNum", "n"):
            value = container.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None


def same_engine(engine: dict[str, Any], artifact: dict[str, Any]) -> bool:
    target_geid = artifact.get("geid")
    target_slot = str(artifact.get("slot", ""))
    for key in ("geid", "id", "model_id", "modelId"):
        value = engine.get(key)
        if target_geid is not None and str(value) == str(target_geid):
            return True
    for key in ("slot", "model", "name", "path"):
        value = engine.get(key)
        if target_slot and value is not None and target_slot in str(value):
            return True
    return False


def box_capacity_status(preflight: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    engines = extract_engine_list(preflight)
    model_limit = extract_model_limit(preflight)
    target_present = any(same_engine(item, artifact) for item in engines or [])
    capacity = {
        "model_limit": model_limit,
        "engine_count": len(engines) if engines is not None else None,
        "target_present": target_present,
        "is_full": False,
        "is_unknown": engines is None or model_limit is None,
    }
    if not capacity["is_unknown"]:
        capacity["is_full"] = bool(model_limit is not None and len(engines or []) >= model_limit and not target_present)
    return capacity


def install_algorithm(runtime: Path, payload: dict[str, Any]) -> dict[str, Any]:
    catalog = load_catalog(runtime)
    device = resolve_devices(catalog, [requested_install_device(payload)])[0]
    algorithm_request = requested_algorithm_text(payload)
    chip_family = requested_chip_family(payload, device)
    device_chip = normalize_chip_family(device.get("chip_family"))
    if chip_family and device_chip and chip_family != device_chip:
        raise PlatformError(f"Requested chip_family={chip_family} does not match target device chip_family={device_chip}")
    artifact = resolve_install_artifact(runtime, catalog, algorithm_request, payload.get("version_label"), chip_family)
    if artifact.get("artifact_kind") not in DEPLOYABLE_ARTIFACT_KINDS:
        raise PlatformError(f"Simplified install does not support artifact kind: {artifact.get('artifact_kind')}")
    if chip_family and not chip_matches(artifact, chip_family):
        raise PlatformError(f"Artifact chip_family={artifact.get('chip_family')} does not match requested chip_family={chip_family}")
    local_path = artifact_local_path(runtime, artifact)

    request_id = validate_request_id(payload.get("request_id") or make_install_request_id(device, artifact))
    existing_path = job_path(runtime, request_id)
    if existing_path.exists():
        return read_json(existing_path)

    channels = validate_channels(payload.get("channels", []))
    threshold = requested_threshold(payload, artifact)
    dry_run = bool_payload(payload.get("dry_run"), False)
    allow_full = bool_payload(payload.get("allow_full"), False)
    require_not_full = bool_payload(payload.get("require_not_full"), True) and not allow_full

    job = {
        "schema_version": 1,
        "api": "install",
        "request_id": request_id,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "mode": "install",
        "dry_run": dry_run,
        "status": "preflight",
        "request": {
            "target_devices": [device["display_id"]],
            "algorithm_request": algorithm_request,
            "algorithm_key": artifact["algorithm_key"],
            "version_label": artifact["version_label"],
            "chip_family": chip_family,
            "channels": channels,
            "threshold": threshold,
            "require_not_full": require_not_full,
            "allow_full": allow_full,
            "reason": payload.get("reason", "simplified install"),
        },
        "plans": [],
        "results": [],
        "errors": [],
    }
    write_json(existing_path, job)

    try:
        with DeviceSession(device) as session:
            preflight = remote_preflight(session, artifact, channels, threshold)
        if artifact.get("artifact_kind") == DEVICE_SERVICE_PACKAGE:
            capacity = {
                "model_limit": None,
                "engine_count": None,
                "target_present": True,
                "is_full": False,
                "is_unknown": False,
                "not_applicable": True,
            }
        else:
            capacity = box_capacity_status(preflight, artifact)
        plan = build_plan(request_id, device, artifact, local_path, preflight, channels, threshold)
        plan["capacity"] = capacity
        if capacity["is_unknown"]:
            plan["warnings"].append("Box capacity could not be read from modelN/algorithm_engine.")
        elif capacity["is_full"]:
            plan["warnings"].append("Box algorithm slots are full.")
        job["plans"].append(plan)

        if require_not_full and capacity["is_unknown"]:
            job["status"] = "blocked"
            job["errors"].append({"error": "BoxCapacityUnknown", "message": "Cannot confirm whether the box has a free algorithm slot.", "capacity": capacity})
        elif require_not_full and capacity["is_full"]:
            job["status"] = "blocked"
            job["errors"].append({"error": "BoxFull", "message": "Box algorithm slots are full; default install policy requires a free slot.", "capacity": capacity})
        elif dry_run:
            job["status"] = "dry_run_complete"
        else:
            job = execute_job(runtime, job)
    except Exception as exc:
        job["status"] = "failed"
        job["errors"].append({"device": device.get("display_id"), "error": type(exc).__name__, "message": str(exc)})

    job["updated_at"] = utc_now()
    write_json(existing_path, job)
    return job


def list_install_algorithms(runtime: Path, chip_family: str | None = None) -> list[dict[str, Any]]:
    chip_family = normalize_chip_family(chip_family)
    catalog = load_catalog(runtime)
    artifacts = deployable_artifacts(runtime, catalog)

    algorithms = []
    for artifact in artifacts:
        if chip_family and not chip_matches(artifact, chip_family):
            continue
        algorithms.append(
            {
                "algorithm_key": artifact.get("algorithm_key"),
                "display_name": artifact.get("display_name"),
                "version_label": artifact.get("version_label"),
                "geid": artifact.get("geid"),
                "slot": artifact.get("slot"),
                "chip_family": artifact.get("chip_family"),
                "compatible_chip_families": artifact.get("compatible_chip_families"),
                "default_threshold": artifact.get("default_threshold"),
                "artifact_kind": artifact.get("artifact_kind"),
                "md5": artifact.get("md5"),
                "source": artifact.get("source", "catalog"),
                "candidate_geids": artifact.get("candidate_geids"),
                "candidate_slots": artifact.get("candidate_slots"),
                "engine_names": artifact.get("engine_names"),
                "source_devices": artifact.get("source_devices"),
                "nested_ai": artifact.get("nested_ai"),
                "public_names": public_algorithm_aliases(artifact),
            }
        )
    return sorted(algorithms, key=lambda item: (str(item.get("display_name")), str(item.get("algorithm_key")), str(item.get("version_label"))))


def deploy_ai_model(session: DeviceSession, plan: dict[str, Any], artifact: dict[str, Any], local_path: Path, threshold: float | None, channels: list[int]) -> dict[str, Any]:
    if artifact.get("artifact_kind") != RKNN_AI_MODEL:
        raise PlatformError(f"Automatic deployment is not enabled for artifact kind: {artifact.get('artifact_kind')}")

    slot = artifact["slot"]
    remote_model_path = plan["remote_model_path"]
    model_id = int(slot[1:]) if isinstance(slot, str) and slot.startswith("m") and slot[1:].isdigit() else None
    try:
        class_id = int(artifact.get("geid")) * 256 if artifact.get("geid") is not None else None
    except (TypeError, ValueError):
        class_id = None
    backup_paths: list[str] = []
    upload_result = None

    if plan["model_action"] == "replace_model":
        session.put(local_path, plan["upload_tmp"])
        code = f"""
import errno, hashlib, json, os, shutil
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
try:
    os.replace(tmp, model_path)
except OSError as exc:
    if exc.errno != errno.EXDEV:
        raise
    shutil.copy2(tmp, model_path)
    os.remove(tmp)
print(json.dumps({{'uploaded_md5': actual, 'model_path': model_path, 'backup_path': backup_path if os.path.exists(backup_path) else None}}, ensure_ascii=False))
"""
        upload_result = remote_json(session, code, timeout=120)
        if upload_result.get("backup_path"):
            backup_paths.append(upload_result["backup_path"])

    config_code = f"""
import json, os, shutil, sqlite3
slot = {json.dumps(slot)}
threshold = {repr(threshold)}
channels = {json.dumps(channels)}
model_id = {json.dumps(model_id)}
class_id = {json.dumps(class_id)}
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

dmg_db_backup = None
created_dmg_bindings = []
dmg_db_error = None
db_path = '/oem/smart-gw/db/dmg.db'
if channels and model_id is not None and class_id is not None:
    if os.path.exists(db_path):
        con = sqlite3.connect(db_path)
        try:
            cur = con.cursor()
            for ch in channels:
                row = cur.execute(
                    'select id from channel_ai_models where modelId=? and chNo=?',
                    (model_id, ch),
                ).fetchone()
                model_created = False
                if row:
                    channel_ai_model_id = int(row[0])
                else:
                    if dmg_db_backup is None:
                        dmg_db_backup = db_path + '.bak-platform-' + stamp
                        shutil.copy2(db_path, dmg_db_backup)
                    max_row = cur.execute('select coalesce(max(id), 0) from channel_ai_models').fetchone()
                    channel_ai_model_id = int(max_row[0]) + 1
                    cur.execute(
                        'insert into channel_ai_models (id, channelId, modelId, chNo) values (?, ?, ?, ?)',
                        (channel_ai_model_id, ch, model_id, ch),
                    )
                    model_created = True

                class_row = cur.execute(
                    'select id from channel_ai_model_classes where modelId=? and chNo=? and classId=?',
                    (model_id, ch, class_id),
                ).fetchone()
                class_created = False
                if not class_row:
                    if dmg_db_backup is None:
                        dmg_db_backup = db_path + '.bak-platform-' + stamp
                        shutil.copy2(db_path, dmg_db_backup)
                    max_class_row = cur.execute('select coalesce(max(id), 0) from channel_ai_model_classes').fetchone()
                    channel_ai_model_class_id = int(max_class_row[0]) + 1
                    cur.execute(
                        'insert into channel_ai_model_classes (id, channelId, chNo, modelId, channelAiModelId, classId) values (?, ?, ?, ?, ?, ?)',
                        (channel_ai_model_class_id, ch, ch, model_id, channel_ai_model_id, class_id),
                    )
                    class_created = True

                if model_created or class_created:
                    created_dmg_bindings.append({{
                        'channel': ch,
                        'channel_ai_model_id': channel_ai_model_id,
                        'model_created': model_created,
                        'class_created': class_created,
                        'class_id': class_id,
                    }})
            con.commit()
        except Exception as exc:
            con.rollback()
            dmg_db_error = type(exc).__name__ + ': ' + str(exc)
        finally:
            con.close()
    else:
        dmg_db_error = 'missing_dmg_db'
elif channels:
    dmg_db_error = 'missing_model_id_or_class_id'

print(json.dumps({{
    'config_backups': backups,
    'created_freq': created_freq,
    'dmg_db_backup': dmg_db_backup,
    'created_dmg_bindings': created_dmg_bindings,
    'dmg_db_error': dmg_db_error,
}}, ensure_ascii=False))
"""
    config_result = remote_json(session, config_code, timeout=60)
    backup_paths.extend(config_result.get("config_backups", []))
    if config_result.get("dmg_db_backup"):
        backup_paths.append(config_result["dmg_db_backup"])
    if config_result.get("dmg_db_error"):
        raise PlatformError(f"Device channel binding update failed: {config_result['dmg_db_error']}")

    restart_result = restart_slot_processes(session, slot)
    aimaster_restart_result = restart_aimaster(session)

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
        "aimaster_restart": aimaster_restart_result,
        "verify": verify,
        "backup_paths": backup_paths,
    }


def deploy_algorithm_directory(session: DeviceSession, plan: dict[str, Any], artifact: dict[str, Any], local_path: Path, threshold: float | None, channels: list[int]) -> dict[str, Any]:
    if artifact.get("artifact_kind") != DEVICE_ALGORITHM_DIRECTORY:
        raise PlatformError(f"Directory deployment is not enabled for artifact kind: {artifact.get('artifact_kind')}")

    slot = artifact["slot"]
    remote_model_path = plan["remote_model_path"]
    model_id = int(slot[1:]) if isinstance(slot, str) and slot.startswith("m") and slot[1:].isdigit() else None
    try:
        class_id = int(artifact.get("geid")) * 256 if artifact.get("geid") is not None else None
    except (TypeError, ValueError):
        class_id = None
    backup_paths: list[str] = []

    session.put(local_path, plan["upload_tmp"])
    upload_code = f"""
import errno, hashlib, json, os, shutil, tarfile, tempfile
tmp = {json.dumps(plan["upload_tmp"])}
slot = {json.dumps(slot)}
target = {json.dumps(remote_model_path)}
backup_path = {json.dumps(plan["backup_path"])}
expected_md5 = {json.dumps(artifact["md5"])}

def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def safe_member(name):
    if name.startswith('/') or '..' in name.split('/'):
        return False
    return name == slot or name.startswith(slot + '/')

actual = md5(tmp)
if actual != expected_md5:
    raise SystemExit('uploaded md5 mismatch: ' + actual)

extract_root = tempfile.mkdtemp(prefix='algorithm-dir-')
try:
    with tarfile.open(tmp, 'r:gz') as tf:
        members = tf.getmembers()
        bad = [m.name for m in members if not safe_member(m.name)]
        if bad:
            raise SystemExit('unsafe archive members: ' + ', '.join(bad[:5]))
        tf.extractall(extract_root)
    extracted = os.path.join(extract_root, slot)
    if not os.path.isdir(extracted):
        raise SystemExit('archive does not contain top-level slot directory: ' + slot)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target):
        if os.path.exists(backup_path):
            raise SystemExit('backup path already exists: ' + backup_path)
        shutil.copytree(target, backup_path)
        shutil.rmtree(target)
    try:
        shutil.move(extracted, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copytree(extracted, target)
        shutil.rmtree(extracted)
finally:
    try:
        shutil.rmtree(extract_root)
    except Exception:
        pass
    try:
        os.remove(tmp)
    except Exception:
        pass

print(json.dumps({{'uploaded_md5': actual, 'target': target, 'backup_path': backup_path if os.path.exists(backup_path) else None}}, ensure_ascii=False))
"""
    upload_result = remote_json(session, upload_code, timeout=180)
    if upload_result.get("backup_path"):
        backup_paths.append(upload_result["backup_path"])

    config_code = f"""
import json, os, shutil, sqlite3
slot = {json.dumps(slot)}
threshold = {repr(threshold)}
channels = {json.dumps(channels)}
model_id = {json.dumps(model_id)}
class_id = {json.dumps(class_id)}
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

dmg_db_backup = None
created_dmg_bindings = []
dmg_db_error = None
db_path = '/oem/smart-gw/db/dmg.db'
if channels and model_id is not None and class_id is not None:
    if os.path.exists(db_path):
        con = sqlite3.connect(db_path)
        try:
            cur = con.cursor()
            for ch in channels:
                row = cur.execute(
                    'select id from channel_ai_models where modelId=? and chNo=?',
                    (model_id, ch),
                ).fetchone()
                model_created = False
                if row:
                    channel_ai_model_id = int(row[0])
                else:
                    if dmg_db_backup is None:
                        dmg_db_backup = db_path + '.bak-platform-' + stamp
                        shutil.copy2(db_path, dmg_db_backup)
                    max_row = cur.execute('select coalesce(max(id), 0) from channel_ai_models').fetchone()
                    channel_ai_model_id = int(max_row[0]) + 1
                    cur.execute(
                        'insert into channel_ai_models (id, channelId, modelId, chNo) values (?, ?, ?, ?)',
                        (channel_ai_model_id, ch, model_id, ch),
                    )
                    model_created = True

                class_row = cur.execute(
                    'select id from channel_ai_model_classes where modelId=? and chNo=? and classId=?',
                    (model_id, ch, class_id),
                ).fetchone()
                class_created = False
                if not class_row:
                    if dmg_db_backup is None:
                        dmg_db_backup = db_path + '.bak-platform-' + stamp
                        shutil.copy2(db_path, dmg_db_backup)
                    max_class_row = cur.execute('select coalesce(max(id), 0) from channel_ai_model_classes').fetchone()
                    channel_ai_model_class_id = int(max_class_row[0]) + 1
                    cur.execute(
                        'insert into channel_ai_model_classes (id, channelId, chNo, modelId, channelAiModelId, classId) values (?, ?, ?, ?, ?, ?)',
                        (channel_ai_model_class_id, ch, ch, model_id, channel_ai_model_id, class_id),
                    )
                    class_created = True

                if model_created or class_created:
                    created_dmg_bindings.append({{
                        'channel': ch,
                        'channel_ai_model_id': channel_ai_model_id,
                        'model_created': model_created,
                        'class_created': class_created,
                        'class_id': class_id,
                    }})
            con.commit()
        except Exception as exc:
            con.rollback()
            dmg_db_error = type(exc).__name__ + ': ' + str(exc)
        finally:
            con.close()
    else:
        dmg_db_error = 'missing_dmg_db'
elif channels:
    dmg_db_error = 'missing_model_id_or_class_id'

print(json.dumps({{
    'config_backups': backups,
    'created_freq': created_freq,
    'dmg_db_backup': dmg_db_backup,
    'created_dmg_bindings': created_dmg_bindings,
    'dmg_db_error': dmg_db_error,
}}, ensure_ascii=False))
"""
    config_result = remote_json(session, config_code, timeout=60)
    backup_paths.extend(config_result.get("config_backups", []))
    if config_result.get("dmg_db_backup"):
        backup_paths.append(config_result["dmg_db_backup"])
    if config_result.get("dmg_db_error"):
        raise PlatformError(f"Device channel binding update failed: {config_result['dmg_db_error']}")

    restart_result = restart_slot_processes(session, slot)
    aimaster_restart_result = restart_aimaster(session)
    verify = remote_preflight(session, artifact, channels, threshold)
    if not verify.get("existing_model") or (verify.get("existing_model") or {}).get("kind") != "directory":
        raise PlatformError("Post-deploy verification did not find the algorithm directory")
    if not verify.get("processes"):
        raise PlatformError("Post-deploy verification found no running processes for slot")

    return {
        "upload": upload_result,
        "config": config_result,
        "restart": restart_result,
        "aimaster_restart": aimaster_restart_result,
        "verify": verify,
        "backup_paths": backup_paths,
    }


def deploy_service_package(session: DeviceSession, plan: dict[str, Any], artifact: dict[str, Any], local_path: Path, threshold: float | None, channels: list[int]) -> dict[str, Any]:
    if artifact.get("artifact_kind") != DEVICE_SERVICE_PACKAGE:
        raise PlatformError(f"Service package deployment is not enabled for artifact kind: {artifact.get('artifact_kind')}")
    if artifact.get("slot") != "m101":
        raise PlatformError(f"Service package deployment is only implemented for m101 today: {artifact.get('slot')}")

    session.put(local_path, plan["upload_tmp"])
    remote_code = f"""
import hashlib, json, os, shutil, subprocess, time, zipfile

tmp = {json.dumps(plan["upload_tmp"])}
expected_md5 = {json.dumps(artifact["md5"])}
app_dir = {json.dumps(artifact.get("remote_model_path") or "/oem/smart-gw/m101_scene_change")}
backup_dir = {json.dumps(plan.get("backup_path"))}
work_dir = {json.dumps("/tmp/" + plan["device"]["display_id"] + "-" + plan["artifact"]["algorithm_key"] + "-" + plan["artifact"]["version_label"] + "-service-work")}
channels = {json.dumps(channels)}
threshold = {repr(threshold)}
unit = 'm101-scene-change.service'
script_path = os.path.join(app_dir, 'm101_scene_change_service.py')
config_path = os.path.join(app_dir, 'config.json')
service_path = '/etc/systemd/system/' + unit

def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def run(cmd, cwd=None, timeout=120, check=True):
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    item = {{
        'cmd': cmd,
        'cwd': cwd,
        'rc': proc.returncode,
        'stdout_tail': proc.stdout[-4000:],
        'stderr_tail': proc.stderr[-4000:],
    }}
    if check and proc.returncode != 0:
        raise RuntimeError(json.dumps(item, ensure_ascii=False))
    return item

actual_md5 = md5(tmp)
if actual_md5 != expected_md5:
    raise SystemExit('uploaded package md5 mismatch: ' + actual_md5)

if os.path.exists(work_dir):
    shutil.rmtree(work_dir)
os.makedirs(work_dir, exist_ok=True)
with zipfile.ZipFile(tmp) as zf:
    zf.extractall(work_dir)

meta_path = os.path.join(work_dir, 'package.meta.json')
meta = {{}}
if os.path.exists(meta_path):
    with open(meta_path, encoding='utf-8') as fh:
        meta = json.load(fh)
if meta.get('algorithm_id') not in (None, 'm101') or meta.get('geid') not in (None, 101):
    raise RuntimeError('package metadata does not match m101/geid=101')

backed_up = []
if backup_dir:
    os.makedirs(backup_dir, exist_ok=True)
    for path in [script_path, config_path, service_path]:
        if os.path.exists(path):
            dst = os.path.join(backup_dir, os.path.basename(path))
            shutil.copy2(path, dst)
            backed_up.append({{'from': path, 'to': dst, 'md5': md5(dst)}})

install_result = run(['sh', 'install.sh'], cwd=work_dir, timeout=180)

config_update = {{'changed': False, 'path': config_path}}
if os.path.exists(config_path):
    with open(config_path, encoding='utf-8') as fh:
        config = json.load(fh)
else:
    config = {{}}
if channels:
    config['channels'] = channels
    config_update['changed'] = True
if threshold is not None:
    config['change_threshold'] = threshold
    config_update['changed'] = True
if config_update['changed']:
    with open(config_path, 'w', encoding='utf-8') as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.write('\\n')
    config_update['md5'] = md5(config_path)

compile_result = run(['/usr/bin/python3', '-m', 'py_compile', script_path], timeout=60)
dry_run_result = run(['/usr/bin/python3', script_path, '--once', '--dry-run', '--verbose'], timeout=420, check=False)
daemon_reload = run(['systemctl', 'daemon-reload'], timeout=30)
enable_result = run(['systemctl', 'enable', unit], timeout=30)
restart_result = run(['systemctl', 'restart', unit], timeout=30)
time.sleep(2.0)
active_result = run(['systemctl', 'is-active', unit], timeout=10, check=False)
enabled_result = run(['systemctl', 'is-enabled', unit], timeout=10, check=False)

procs = []
for pid in sorted([p for p in os.listdir('/proc') if p.isdigit()], key=lambda x: int(x)):
    try:
        cwd = os.readlink('/proc/' + pid + '/cwd')
    except Exception:
        cwd = ''
    try:
        with open('/proc/' + pid + '/cmdline', 'rb') as fh:
            cmdline = fh.read().replace(b'\\x00', b' ').decode('utf-8', 'replace').strip()
    except Exception:
        cmdline = ''
    if cwd.startswith(app_dir) or script_path in cmdline:
        procs.append({{'pid': int(pid), 'cwd': cwd, 'cmdline': cmdline}})

cleanup = []
for path in [work_dir, tmp]:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
        cleanup.append({{'path': path, 'removed': True}})
    except Exception as exc:
        cleanup.append({{'path': path, 'removed': False, 'error': type(exc).__name__ + ': ' + str(exc)}})

if active_result['stdout_tail'].strip() != 'active':
    raise RuntimeError('service not active after restart: ' + json.dumps(active_result, ensure_ascii=False))
if not procs:
    raise RuntimeError('service process not found after restart')
if dry_run_result['rc'] != 0:
    raise RuntimeError('service dry-run failed after restart attempt: ' + json.dumps(dry_run_result, ensure_ascii=False))

print(json.dumps({{
    'upload': {{'package_md5': actual_md5, 'tmp': tmp}},
    'package_meta': meta,
    'backup_dir': backup_dir,
    'backed_up': backed_up,
    'install': install_result,
    'config_update': config_update,
    'compile': compile_result,
    'dry_run': dry_run_result,
    'daemon_reload': daemon_reload,
    'enable': enable_result,
    'restart': restart_result,
    'active': active_result,
    'enabled': enabled_result,
    'processes': procs,
    'cleanup': cleanup,
}}, ensure_ascii=False))
"""
    deploy_result = remote_json(session, remote_code, timeout=600)
    verify = remote_preflight(session, artifact, channels, threshold)
    service = verify.get("service") or {}
    if service.get("active") != "active":
        raise PlatformError(f"Post-deploy service check failed: {service}")
    if not verify.get("processes"):
        raise PlatformError("Post-deploy verification found no running m101 service process")
    return {"deploy": deploy_result, "verify": verify, "backup_paths": [deploy_result.get("backup_dir")] if deploy_result.get("backup_dir") else []}


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
    if os.path.exists(nn_dir + '/nn_server.conf'):
        subprocess.Popen(
            f'cd {{nn_dir}} && LD_LIBRARY_PATH=/oem/usr/lib/ nohup /oem/smart-gw/service/nn_server/bin/nn_server -c nn_server.conf > {{nn_log}} 2>&1 &',
            shell=True,
        )
        started.append({{'component': 'nn_server', 'mode': 'binary', 'log': nn_log}})
    elif os.path.exists(nn_dir + '/main.py'):
        arg = 'args.json' if os.path.exists(nn_dir + '/args.json') else ''
        subprocess.Popen(
            f'cd {{nn_dir}} && LD_LIBRARY_PATH=/usr/lib/:/oem/usr/lib/ nohup /usr/bin/python3 main.py {{arg}} > {{nn_log}} 2>&1 &',
            shell=True,
        )
        started.append({{'component': 'nn_server', 'mode': 'python', 'log': nn_log}})
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


def restart_aimaster(session: DeviceSession) -> dict[str, Any]:
    restart_code = """
import json, os, signal, subprocess, time
base = '/oem/smart-gw/service/aimaster'

def find_procs():
    procs = []
    for pid in sorted([p for p in os.listdir('/proc') if p.isdigit()], key=lambda x: int(x)):
        try:
            cwd = os.readlink('/proc/' + pid + '/cwd')
        except Exception:
            cwd = ''
        try:
            with open('/proc/' + pid + '/cmdline', 'rb') as fh:
                cmdline = fh.read().replace(b'\\x00', b' ').decode('utf-8', 'replace').strip()
        except Exception:
            cmdline = ''
        if cwd.startswith(base) or base in cmdline or cmdline.endswith('/aimaster') or cmdline == 'aimaster':
            procs.append({'pid': int(pid), 'cwd': cwd, 'cmdline': cmdline})
    return procs

before = find_procs()
killed = []
for proc in before:
    try:
        os.kill(proc['pid'], signal.SIGTERM)
        killed.append(proc['pid'])
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
log = f'/tmp/aimaster_platform_{stamp}.log'
started = None
if os.path.isdir(base):
    subprocess.Popen(f'cd {base} && nohup {base}/bin/aimaster > {log} 2>&1 &', shell=True)
    started = {'component': 'aimaster', 'log': log}
time.sleep(2.0)
print(json.dumps({'before': before, 'killed': killed, 'started': started, 'after': find_procs()}, ensure_ascii=False))
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
        dmg_db_backup = config.get("dmg_db_backup")
        plans.append({
            "device": device,
            "slot": plan.get("restart_slot") or (plan.get("artifact") or {}).get("slot"),
            "remote_model_path": plan.get("remote_model_path"),
            "model_backup_path": model_backup,
            "config_backup_paths": config_backups,
            "created_freq_paths": created_freq,
            "dmg_db_backup_path": dmg_db_backup,
            "has_actions": bool(model_backup or config_backups or created_freq or dmg_db_backup),
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
    dmg_db_backup = plan.get("dmg_db_backup_path")
    code = f"""
import hashlib, json, os, shutil
slot = {json.dumps(slot)}
model_path = {json.dumps(model_path)}
model_backup = {json.dumps(model_backup)}
config_backups = {json.dumps(config_backups)}
created_freq = {json.dumps(created_freq)}
dmg_db_backup = {json.dumps(dmg_db_backup)}
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

def restore_dir(src, dst):
    if not src:
        return
    if not os.path.isdir(src):
        missing.append(src)
        return
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    restored.append({{'from': src, 'to': dst, 'kind': 'directory'}})

if model_backup and model_path:
    if os.path.isdir(model_backup):
        restore_dir(model_backup, model_path)
    else:
        restore_file(model_backup, model_path)

for src in config_backups:
    if not isinstance(src, str):
        continue
    if src.endswith('/nn.extend.json') or '.bak-platform-' not in src:
        missing.append(src)
        continue
    dst = src.split('.bak-platform-', 1)[0]
    restore_file(src, dst)

if dmg_db_backup:
    restore_file(dmg_db_backup, '/oem/smart-gw/db/dmg.db')

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
    aimaster_restart_result = restart_aimaster(session)
    return {"restore": restore_result, "restart": restart_result, "aimaster_restart": aimaster_restart_result}


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
    chip_family = requested_chip_family(payload, devices[0] if devices else None)
    device_chips = {normalize_chip_family(device.get("chip_family")) for device in devices if normalize_chip_family(device.get("chip_family"))}
    if chip_family and device_chips and any(chip != chip_family for chip in device_chips):
        raise PlatformError(f"Requested chip_family={chip_family} does not match all target devices: {sorted(device_chips)}")
    artifact = resolve_deploy_artifact(runtime, catalog, str(payload.get("algorithm_key", "")), payload.get("version_label"), chip_family)
    channels = validate_channels(payload.get("channels", []))
    threshold = requested_threshold(payload, artifact)
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
            "chip_family": chip_family,
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
            if artifact.get("artifact_kind") == DEVICE_SERVICE_PACKAGE:
                plan["warnings"].append("Service-package deployment runs install.sh, dry-run verification, then restarts the systemd service.")
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
    artifact = resolve_deploy_artifact(
        runtime,
        catalog,
        job["request"]["algorithm_key"],
        job["request"]["version_label"],
        normalize_chip_family(job["request"].get("chip_family")),
    )
    local_path = artifact_local_path(runtime, artifact)
    channels = validate_channels(job["request"].get("channels", []))
    threshold = validate_threshold(job["request"].get("threshold"))
    devices_by_display = {str(d["display_id"]): d for d in catalog.get("devices", [])}

    if artifact.get("artifact_kind") not in DEPLOYABLE_ARTIFACT_KINDS:
        job["status"] = "blocked"
        job["errors"].append({"error": "UnsupportedArtifactKind", "message": f"Deployment is not enabled for artifact kind: {artifact.get('artifact_kind')}"})
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
                if artifact.get("artifact_kind") == DEVICE_SERVICE_PACKAGE:
                    result = deploy_service_package(session, plan, artifact, local_path, threshold, channels)
                elif artifact.get("artifact_kind") == DEVICE_ALGORITHM_DIRECTORY:
                    result = deploy_algorithm_directory(session, plan, artifact, local_path, threshold, channels)
                else:
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


def parse_install_json(args: argparse.Namespace) -> dict[str, Any]:
    if args.request_file:
        return read_json(Path(args.request_file))
    if args.request_json:
        return json.loads(args.request_json)
    return {
        "request_id": args.request_id,
        "device": args.device,
        "algorithm_key": args.algorithm_key,
        "version_label": args.version_label,
        "channels": args.channel,
        "threshold": args.threshold,
        "dry_run": args.dry_run,
        "allow_full": args.allow_full,
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

    install = sub.add_parser("install")
    install.add_argument("--request-file")
    install.add_argument("--request-json")
    install.add_argument("--request-id")
    install.add_argument("--device")
    install.add_argument("--algorithm-key")
    install.add_argument("--version-label")
    install.add_argument("--channel", action="append", type=int, default=[])
    install.add_argument("--threshold", type=float)
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--allow-full", action="store_true")
    install.add_argument("--reason")

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
        elif args.command == "install":
            job = install_algorithm(runtime, parse_install_json(args))
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
