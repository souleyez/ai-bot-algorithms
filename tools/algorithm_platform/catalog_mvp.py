#!/usr/bin/env python3
"""Build the read-only AI-BOT algorithm platform catalog.

The script keeps GitHub clean: it reads checked-in metadata, scans the local
Desktop algorithm package folder, computes hashes, and optionally copies only
approved artifact file types into an ignored runtime directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = ROOT / "platform" / "algorithm-catalog"
DEFAULT_OUTPUT_DIR = ROOT / ".runtime" / "algorithm-platform"
ALLOWED_COPY_SUFFIXES = {".ai", ".zip"}
BLOCKED_RUNTIME_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".mp4",
    ".avi",
    ".db",
    ".sqlite",
    ".rknn",
    ".onnx",
    ".pt",
}


@dataclass(frozen=True)
class FileHashes:
    md5: str
    sha256: str


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def hash_file(path: Path) -> FileHashes:
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            md5.update(chunk)
            sha256.update(chunk)
    return FileHashes(md5=md5.hexdigest(), sha256=sha256.hexdigest())


def slug(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "artifact"


def resolve_package_root(config: dict[str, Any], explicit_root: str | None) -> Path:
    if explicit_root:
        return Path(explicit_root).expanduser().resolve()
    env_name = config.get("package_root_env", "AI_BOT_ALGORITHM_PACKAGE_ROOT")
    if os.environ.get(env_name):
        return Path(os.environ[env_name]).expanduser().resolve()
    return Path(config.get("default_package_root", "~/Desktop/算法包")).expanduser().resolve()


def normalize_rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def infer_algorithm(path: Path, package_root: Path) -> dict[str, Any]:
    filename = path.name.lower()
    parent = path.parent.name
    if filename == "security_guard.rk3576.ai":
        return {
            "algorithm_key": "security_guard",
            "display_name": "保安识别",
            "artifact_kind": "rknn_ai_model",
            "slot": "m100",
            "geid": 100,
            "chip_family": "rk3576",
            "remote_model_path": "/models/m100/security_guard.rk3576.ai",
            "default_threshold": 0.8,
        }
    if filename == "cleaner.rk3576.ai":
        return {
            "algorithm_key": "cleaner",
            "display_name": "保洁识别",
            "artifact_kind": "rknn_ai_model",
            "slot": "m102",
            "geid": 102,
            "chip_family": "rk3576",
            "remote_model_path": "/models/m102/cleaner.rk3576.ai",
            "default_threshold": 0.5,
        }
    if filename == "engineering_worker.rk3576.ai":
        return {
            "algorithm_key": "engineering_worker",
            "display_name": "维修识别",
            "artifact_kind": "rknn_ai_model",
            "slot": "m103",
            "geid": 103,
            "chip_family": "rk3576",
            "remote_model_path": "/models/m103/engineering_worker.rk3576.ai",
            "default_threshold": 0.8,
        }
    return {
        "algorithm_key": slug(path.stem),
        "display_name": path.stem,
        "artifact_kind": "unknown_binary",
        "slot": None,
        "geid": None,
        "chip_family": "rk3576" if "rk3576" in filename or "rk3576" in parent.lower() else None,
        "remote_model_path": None,
        "default_threshold": None,
    }


def infer_version_label(path: Path) -> str:
    parent = path.parent.name
    version_match = re.search(r"(?<![a-z0-9])(v\d+[a-z0-9-]*)(?![a-z0-9])", parent, re.I)
    if version_match:
        return version_match.group(1)
    date_match = re.search(r"(20\d{6}(?:-\d{4})?)", parent)
    if date_match:
        return date_match.group(1)
    return slug(parent)[:80]


def load_recommended(package_root: Path, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    recommended: dict[str, dict[str, Any]] = {}
    for item in config["artifacts"]:
        source_rel = item["source_relative_path"].replace("\\", "/")
        source_path = package_root / source_rel
        record = dict(item)
        record["source_relative_path"] = source_rel
        record["source_path"] = str(source_path)
        recommended[source_rel] = record
    return recommended


def artifact_record(
    source_path: Path,
    package_root: Path,
    metadata: dict[str, Any],
    status: str,
    include_companion: bool,
) -> dict[str, Any]:
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    hashes = hash_file(source_path)
    rel = normalize_rel(source_path, package_root)
    version_label = metadata.get("version_label") or infer_version_label(source_path)
    algorithm_key = metadata["algorithm_key"]
    artifact_id = slug(f"{algorithm_key}-{version_label}-{hashes.sha256[:12]}")
    record = {
        "id": artifact_id,
        "algorithm_key": algorithm_key,
        "display_name": metadata.get("display_name"),
        "artifact_kind": metadata.get("artifact_kind"),
        "slot": metadata.get("slot"),
        "geid": metadata.get("geid"),
        "chip_family": metadata.get("chip_family"),
        "version_label": version_label,
        "status": status,
        "md5": hashes.md5,
        "sha256": hashes.sha256,
        "size_bytes": source_path.stat().st_size,
        "source_relative_path": rel,
        "source_filename": source_path.name,
        "remote_model_path": metadata.get("remote_model_path"),
        "default_threshold": metadata.get("default_threshold"),
        "notes": metadata.get("notes", ""),
    }
    if include_companion and metadata.get("companion_relative_path"):
        companion_rel = metadata["companion_relative_path"].replace("\\", "/")
        companion_path = package_root / companion_rel
        if companion_path.exists():
            companion_hashes = hash_file(companion_path)
            record["companion"] = {
                "source_relative_path": companion_rel,
                "source_filename": companion_path.name,
                "md5": companion_hashes.md5,
                "sha256": companion_hashes.sha256,
                "size_bytes": companion_path.stat().st_size,
            }
        else:
            record["warnings"] = [f"Companion package missing: {companion_rel}"]
    return record


def discover_ai_artifacts(package_root: Path) -> list[Path]:
    if not package_root.exists():
        raise FileNotFoundError(f"Package root does not exist: {package_root}")
    return sorted(package_root.rglob("*.ai"), key=lambda p: normalize_rel(p, package_root).lower())


def build_catalog(args: argparse.Namespace) -> dict[str, Any]:
    devices_config = read_json(CATALOG_DIR / "devices.json")
    artifact_config = read_json(CATALOG_DIR / "recommended-artifacts.json")
    package_root = resolve_package_root(artifact_config, args.package_root)
    recommended_by_rel = load_recommended(package_root, artifact_config)
    recommended_algorithm_keys = {item["algorithm_key"] for item in recommended_by_rel.values()}
    source_paths: dict[str, tuple[Path, dict[str, Any], str]] = {}

    for rel, metadata in recommended_by_rel.items():
        source_paths[rel] = (package_root / rel, metadata, metadata.get("status", "approved"))

    if args.include_discovered:
        for source_path in discover_ai_artifacts(package_root):
            rel = normalize_rel(source_path, package_root)
            if rel in source_paths:
                continue
            metadata = infer_algorithm(source_path, package_root)
            if metadata.get("algorithm_key") in recommended_algorithm_keys and not args.include_deprecated_custom:
                continue
            metadata["version_label"] = infer_version_label(source_path)
            metadata["notes"] = "Discovered from local Desktop algorithm package root."
            source_paths[rel] = (source_path, metadata, "deprecated")

    artifacts = []
    missing = []
    for _, (source_path, metadata, status) in sorted(source_paths.items()):
        try:
            artifacts.append(
                artifact_record(
                    source_path=source_path,
                    package_root=package_root,
                    metadata=metadata,
                    status=status,
                    include_companion=args.include_companions,
                )
            )
        except FileNotFoundError as exc:
            missing.append(str(exc))

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    catalog = {
        "schema_version": 1,
        "generated_at": now,
        "package_root": str(package_root),
        "server_storage_root": args.server_storage_root,
        "devices": devices_config["devices"],
        "artifacts": sorted(artifacts, key=lambda item: (item["algorithm_key"], item["status"], item["version_label"])),
        "missing_sources": missing,
        "policy": {
            "no_github_binary_artifacts": True,
            "copy_suffix_allowlist": sorted(ALLOWED_COPY_SUFFIXES),
            "runtime_blocked_suffixes": sorted(BLOCKED_RUNTIME_SUFFIXES),
            "default_release_mode": "semi_auto",
        },
    }
    return catalog


def copy_catalog_artifacts(catalog: dict[str, Any], package_root: Path, artifact_dir: Path) -> list[dict[str, Any]]:
    copied = []
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for artifact in catalog["artifacts"]:
        source_path = package_root / artifact["source_relative_path"]
        if source_path.suffix.lower() not in ALLOWED_COPY_SUFFIXES:
            continue
        target_dir = artifact_dir / artifact["id"]
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / source_path.name
        shutil.copy2(source_path, target_path)
        artifact["storage_relative_path"] = target_path.relative_to(artifact_dir.parent).as_posix()
        artifact["server_storage_uri"] = f"{catalog['server_storage_root'].rstrip('/')}/{artifact['storage_relative_path']}"
        copied.append({"artifact_id": artifact["id"], "path": str(target_path), "size_bytes": target_path.stat().st_size})

        companion = artifact.get("companion")
        if companion:
            companion_path = package_root / companion["source_relative_path"]
            if companion_path.suffix.lower() in ALLOWED_COPY_SUFFIXES:
                companion_target = target_dir / companion_path.name
                shutil.copy2(companion_path, companion_target)
                companion["storage_relative_path"] = companion_target.relative_to(artifact_dir.parent).as_posix()
                companion["server_storage_uri"] = f"{catalog['server_storage_root'].rstrip('/')}/{companion['storage_relative_path']}"
                copied.append(
                    {
                        "artifact_id": artifact["id"],
                        "path": str(companion_target),
                        "size_bytes": companion_target.stat().st_size,
                        "role": "companion",
                    }
                )
    return copied


def find_blocked_runtime_files(output_dir: Path) -> list[str]:
    blocked = []
    if not output_dir.exists():
        return blocked
    for path in output_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in BLOCKED_RUNTIME_SUFFIXES:
            blocked.append(str(path.relative_to(output_dir)))
    return sorted(blocked)


def write_report(output_dir: Path, catalog: dict[str, Any], copied: list[dict[str, Any]], blocked: list[str]) -> None:
    approved = [a for a in catalog["artifacts"] if a["status"] == "approved"]
    deprecated = [a for a in catalog["artifacts"] if a["status"] == "deprecated"]
    lines = [
        "AI-BOT algorithm platform catalog MVP report",
        f"Generated: {catalog['generated_at']}",
        f"Devices: {len(catalog['devices'])}",
        f"Artifacts: {len(catalog['artifacts'])}",
        f"Approved artifacts: {len(approved)}",
        f"Deprecated discovered artifacts: {len(deprecated)}",
        f"Copied files: {len(copied)}",
        f"Missing sources: {len(catalog['missing_sources'])}",
        f"Blocked runtime files: {len(blocked)}",
        "",
        "Approved:",
    ]
    for artifact in approved:
        lines.append(
            f"- {artifact['algorithm_key']} {artifact['version_label']} {artifact['source_filename']} "
            f"md5={artifact['md5']} size={artifact['size_bytes']}"
        )
    if blocked:
        lines.append("")
        lines.append("Blocked runtime files:")
        lines.extend(f"- {path}" for path in blocked)
    (output_dir / "import-report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", help="Local Desktop algorithm package root. Defaults to config/env.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Ignored runtime output directory.")
    parser.add_argument(
        "--server-storage-root",
        default="/home/xigma01/apps/Assistant/data/runtime/algorithm-platform",
        help="Server-side storage root used to populate server_storage_uri fields.",
    )
    parser.add_argument("--copy-artifacts", action="store_true", help="Copy artifacts into output-dir/artifacts.")
    parser.add_argument("--include-discovered", action="store_true", help="Scan package root for all local .ai files.")
    parser.add_argument("--include-deprecated-custom", action="store_true", help="When scanning, include older discovered versions of custom algorithms already listed as recommended.")
    parser.add_argument("--include-companions", action="store_true", help="Include configured companion .zip packages.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_config = read_json(CATALOG_DIR / "recommended-artifacts.json")
    package_root = resolve_package_root(artifact_config, args.package_root)
    catalog = build_catalog(args)
    copied: list[dict[str, Any]] = []
    if args.copy_artifacts:
        copied = copy_catalog_artifacts(catalog, package_root, output_dir / "artifacts")
    blocked = find_blocked_runtime_files(output_dir)
    if blocked:
        raise RuntimeError(f"Blocked runtime files found: {blocked}")
    write_json(output_dir / "catalog.json", catalog)
    write_json(output_dir / "devices.json", {"devices": catalog["devices"]})
    write_json(output_dir / "artifacts.json", {"artifacts": catalog["artifacts"]})
    write_report(output_dir, catalog, copied, blocked)
    print(f"Wrote catalog to {output_dir}")
    print(f"Devices: {len(catalog['devices'])}")
    print(f"Artifacts: {len(catalog['artifacts'])}")
    print(f"Copied files: {len(copied)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
