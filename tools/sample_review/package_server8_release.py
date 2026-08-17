#!/usr/bin/env python3
"""Build a deterministic Server-8 sample-review release from committed bytes."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


SAMPLE_REVIEW_FILES = frozenset(
    {
        "tools/sample_review/asset_export.py",
        "tools/sample_review/capture_export.py",
        "tools/sample_review/original_resolver.py",
        "tools/sample_review/oss_backend.py",
        "tools/sample_review/preview_resolver.py",
        "tools/sample_review/regression_store.py",
        "tools/sample_review/reporting_manager.py",
        "tools/sample_review/retention_policy.py",
        "tools/sample_review/review_revisions.py",
        "tools/sample_review/secondary_recognition.py",
        "tools/sample_review/server.py",
        "tools/sample_review/visual_registry.py",
        "tools/sample_review/ai-bot-datamax-export.example.env",
        "tools/sample_review/ai-bot-sample-review-server8.service",
    }
)
ALGORITHM_PLATFORM_FILES = frozenset(
    {
        "tools/algorithm_platform/evidence_ledger.py",
        "tools/algorithm_platform/evidence_schema.sql",
    }
)
STATIC_PREFIX = "tools/sample_review/static/"
REGISTRY_PREFIX = "platform/visual-task-registry/"
MANIFEST_PATH = "release/ai-bot-sample-review-release.json"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 1_000


@dataclass(frozen=True)
class ReleaseResult:
    archive: Path
    checksum: Path
    sha256: str


def runtime_paths(paths: Iterable[str]) -> list[str]:
    selected = {
        path
        for path in paths
        if path in SAMPLE_REVIEW_FILES
        or path in ALGORITHM_PLATFORM_FILES
        or path.startswith(STATIC_PREFIX)
        or path.startswith(REGISTRY_PREFIX)
    }
    return sorted(selected)


def _manifest(files: Mapping[str, bytes], commit: str, epoch: int) -> bytes:
    payload = {
        "schema": "ai-bot.sample-review.release.v1",
        "commit": commit,
        "commit_short": commit[:12],
        "source_date_epoch": epoch,
        "files": {
            path: hashlib.sha256(content).hexdigest()
            for path, content in sorted(files.items())
        },
    }
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def build_archive_bytes(files: Mapping[str, bytes], commit: str, epoch: int) -> bytes:
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("commit must be exactly 40 lowercase hexadecimal characters")
    if epoch <= 0:
        raise ValueError("source_date_epoch must be positive")
    normalized = dict(files)
    if not normalized or any(path.startswith("/") or ".." in Path(path).parts for path in normalized):
        raise ValueError("release files must use safe relative paths")
    normalized[MANIFEST_PATH] = _manifest(files, commit, epoch)

    raw_tar = io.BytesIO()
    with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path, content in sorted(normalized.items()):
            info = tarfile.TarInfo(path)
            info.size = len(content)
            info.mtime = epoch
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(content))

    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=9) as compressed:
        compressed.write(raw_tar.getvalue())
    return output.getvalue()


def verify_archive_bytes(
    content: bytes,
    *,
    expected_commit: str,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    if not content or len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError("release archive exceeds the compressed-size limit")
    actual_archive_digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and actual_archive_digest != expected_sha256.lower():
        raise ValueError("release archive SHA-256 mismatch")

    extracted: dict[str, bytes] = {}
    names: list[str] = []
    total_size = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            for member in archive:
                if len(names) >= MAX_ENTRIES:
                    raise ValueError("release archive entry count exceeds limit")
                path = Path(member.name)
                if (
                    not member.isfile()
                    or member.name.startswith("/")
                    or "\\" in member.name
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or member.name in extracted
                ):
                    raise ValueError(f"unsafe release archive member: {member.name!r}")
                if member.size < 0 or member.size > MAX_FILE_BYTES:
                    raise ValueError(f"release archive member exceeds limit: {member.name}")
                total_size += member.size
                if total_size > MAX_EXPANDED_BYTES:
                    raise ValueError("release archive expanded size exceeds limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"cannot read release archive member: {member.name}")
                payload = stream.read(MAX_FILE_BYTES + 1)
                if len(payload) != member.size:
                    raise ValueError(f"release archive member size mismatch: {member.name}")
                names.append(member.name)
                extracted[member.name] = payload
    except (tarfile.TarError, OSError) as exc:
        raise ValueError(f"invalid release archive: {exc}") from exc

    if names != sorted(names):
        raise ValueError("release archive members are not canonically ordered")
    raw_manifest = extracted.pop(MANIFEST_PATH, None)
    if raw_manifest is None:
        raise ValueError("release archive manifest is missing")
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("release archive manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema",
        "commit",
        "commit_short",
        "source_date_epoch",
        "files",
    }:
        raise ValueError("release archive manifest shape is invalid")
    if manifest["schema"] != "ai-bot.sample-review.release.v1":
        raise ValueError("release archive manifest schema is unsupported")
    if manifest["commit"] != expected_commit or manifest["commit_short"] != expected_commit[:12]:
        raise ValueError("release archive commit mismatch")
    if not isinstance(manifest["source_date_epoch"], int) or manifest["source_date_epoch"] <= 0:
        raise ValueError("release archive source_date_epoch is invalid")
    recorded_files = manifest["files"]
    if not isinstance(recorded_files, dict) or set(recorded_files) != set(extracted):
        raise ValueError("release archive manifest file set mismatch")
    for path, payload in extracted.items():
        if recorded_files.get(path) != hashlib.sha256(payload).hexdigest():
            raise ValueError(f"release archive file digest mismatch: {path}")
    return manifest


def write_release(
    output_dir: Path,
    files: Mapping[str, bytes],
    commit: str,
    epoch: int,
) -> ReleaseResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"ai-bot-sample-review-{commit[:12]}.tar.gz"
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    content = build_archive_bytes(files, commit, epoch)
    digest = hashlib.sha256(content).hexdigest()

    with tempfile.NamedTemporaryFile(dir=output_dir, prefix=".sample-review-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii", newline="\n")
    return ReleaseResult(archive=archive, checksum=checksum, sha256=digest)


def _git(repository: Path, *arguments: str, text: bool = True) -> str | bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=text,
    )
    return completed.stdout if text else completed.stdout


def committed_release(repository: Path, output_dir: Path) -> ReleaseResult:
    repository = repository.resolve(strict=True)
    dirty = str(_git(repository, "status", "--porcelain=v1", "--untracked-files=no")).strip()
    if dirty:
        raise RuntimeError("sample-review release requires a tracked-clean worktree")
    commit = str(_git(repository, "rev-parse", "HEAD")).strip()
    epoch_text = str(_git(repository, "log", "-1", "--format=%ct", commit)).strip()
    epoch = int(epoch_text)
    tracked = str(_git(repository, "ls-tree", "-r", "--name-only", commit)).splitlines()
    selected = runtime_paths(tracked)
    missing = sorted((SAMPLE_REVIEW_FILES | ALGORITHM_PLATFORM_FILES) - set(selected))
    if missing:
        raise RuntimeError(f"committed release is missing required files: {', '.join(missing)}")
    if not any(path.startswith(STATIC_PREFIX) for path in selected):
        raise RuntimeError("committed release is missing sample-review static assets")
    if not any(path.startswith(REGISTRY_PREFIX) for path in selected):
        raise RuntimeError("committed release is missing the visual task registry")
    files = {
        path: bytes(_git(repository, "show", f"{commit}:{path}", text=False))
        for path in selected
    }
    return write_release(output_dir.resolve(), files, commit, epoch)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    result = committed_release(arguments.repository, arguments.output_dir)
    commit = str(_git(arguments.repository, "rev-parse", "HEAD")).strip()
    print(f"AI_BOT_SAMPLE_REVIEW_ARCHIVE={result.archive}")
    print(f"AI_BOT_SAMPLE_REVIEW_ARCHIVE_SHA256={result.sha256}")
    print(f"AI_BOT_SAMPLE_REVIEW_CHECKSUM={result.checksum}")
    print(f"AI_BOT_SAMPLE_REVIEW_SOURCE_COMMIT={commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
