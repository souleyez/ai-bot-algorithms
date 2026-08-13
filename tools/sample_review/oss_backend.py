#!/usr/bin/env python3
"""Small OSS backend built around the official ossutil CLI."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


VENDOR_ROOT = Path(__file__).resolve().parent / "vendor"
if VENDOR_ROOT.is_dir():
    # Keep image-provided binary packages (notably cffi) ahead of the vendored
    # pure-Python tree while still making vendored oss2 importable.
    sys.path.append(str(VENDOR_ROOT))
try:
    import oss2
except ModuleNotFoundError:
    oss2 = None

OSSUTIL = os.environ.get("OSSUTIL_PATH", "ossutil")
OSS_BUCKET = os.environ.get("OSS_BUCKET", "").strip()
OSS_PREFIX = os.environ.get("OSS_PREFIX", "ai-bot-samples").strip("/")
OSS_CACHE_MAX_BYTES = int(os.environ.get("OSS_CACHE_MAX_BYTES", str(768 * 1024 * 1024)))


def configured() -> bool:
    required = ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_REGION", "OSS_ENDPOINT")
    use_sdk = oss2 is not None and os.environ.get("OSS_USE_SDK", "").strip() == "1"
    transport = use_sdk or shutil.which(OSSUTIL)
    return bool(OSS_BUCKET and transport and all(os.environ.get(name) for name in required))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def object_key_for_sha256(digest: str, suffix: str = ".jpg") -> str:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("invalid sha256 digest")
    extension = suffix.lower() if suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"
    return f"{OSS_PREFIX}/objects/{digest[:2]}/{digest}{extension}"


def _validate_key(key: str) -> str:
    normalized = key.strip("/")
    if not normalized or normalized.startswith(".") or ".." in Path(normalized).parts:
        raise ValueError("invalid OSS object key")
    return normalized


def object_uri(key: str) -> str:
    return f"oss://{OSS_BUCKET}/{_validate_key(key)}"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    if not configured():
        raise RuntimeError("OSS storage is not configured")
    try:
        return subprocess.run(
            [OSSUTIL, *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"OSS command failed: {type(exc).__name__}") from exc


_thread_local = threading.local()


def _bucket():
    if oss2 is None or os.environ.get("OSS_USE_SDK", "").strip() != "1":
        return None
    bucket = getattr(_thread_local, "bucket", None)
    if bucket is None:
        auth = oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"])
        bucket = oss2.Bucket(auth, os.environ["OSS_ENDPOINT"], OSS_BUCKET)
        _thread_local.bucket = bucket
    return bucket


def _required_sdk_bucket():
    if oss2 is None:
        raise RuntimeError("OSS SDK is unavailable")
    bucket = getattr(_thread_local, "required_bucket", None)
    if bucket is None:
        if not configured():
            raise RuntimeError("OSS storage is not configured")
        auth = oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"])
        bucket = oss2.Bucket(auth, os.environ["OSS_ENDPOINT"], OSS_BUCKET)
        _thread_local.required_bucket = bucket
    return bucket


def upload(path: Path, key: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    normalized = _validate_key(key)
    bucket = _bucket()
    if bucket is not None:
        try:
            bucket.put_object_from_file(normalized, str(path))
            return
        except Exception as exc:
            raise RuntimeError(f"OSS upload failed: {type(exc).__name__}") from exc
    _run("cp", str(path), object_uri(normalized), "--force")


def download(key: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".oss", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        normalized = _validate_key(key)
        bucket = _bucket()
        if bucket is not None:
            try:
                bucket.get_object_to_file(normalized, str(temporary))
            except Exception as exc:
                raise RuntimeError(f"OSS download failed: {type(exc).__name__}") from exc
        else:
            _run("cp", object_uri(normalized), str(temporary), "--force")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def delete(key: str) -> None:
    """Delete one validated object. Deleting a missing OSS object is idempotent."""
    normalized = _validate_key(key)
    bucket = _bucket()
    if bucket is not None:
        try:
            bucket.delete_object(normalized)
            return
        except Exception as exc:
            raise RuntimeError(f"OSS delete failed: {type(exc).__name__}") from exc
    _run("rm", object_uri(normalized), "--force")


def iter_objects(prefix: str):
    """Yield non-secret object metadata below the configured sample prefix."""
    normalized = _validate_key(prefix)
    bucket = _required_sdk_bucket()
    try:
        for item in oss2.ObjectIteratorV2(bucket, prefix=normalized):
            yield {
                "key": str(item.key),
                "size": int(item.size),
                "last_modified": int(item.last_modified),
            }
    except Exception as exc:
        raise RuntimeError(f"OSS listing failed: {type(exc).__name__}") from exc


_cache_locks_guard = threading.Lock()
_cache_locks: dict[str, threading.Lock] = {}


def _cache_lock(key: str) -> threading.Lock:
    with _cache_locks_guard:
        return _cache_locks.setdefault(key, threading.Lock())


def cache_path(cache_root: Path, key: str) -> Path:
    digest = hashlib.sha256(_validate_key(key).encode("utf-8")).hexdigest()
    suffix = Path(key).suffix.lower() or ".jpg"
    return cache_root / digest[:2] / f"{digest}{suffix}"


def materialize(cache_root: Path, key: str, expected_sha256: str = "") -> Path:
    destination = cache_path(cache_root, key)
    with _cache_lock(key):
        if destination.is_file():
            if not expected_sha256 or sha256_file(destination) == expected_sha256:
                os.utime(destination, None)
                return destination
            destination.unlink(missing_ok=True)
        download(key, destination)
        if expected_sha256 and sha256_file(destination) != expected_sha256:
            destination.unlink(missing_ok=True)
            raise RuntimeError("OSS object checksum mismatch")
        prune_cache(cache_root)
        return destination


def prune_cache(cache_root: Path, max_bytes: int = OSS_CACHE_MAX_BYTES) -> int:
    files = [path for path in cache_root.rglob("*") if path.is_file()] if cache_root.exists() else []
    total = sum(path.stat().st_size for path in files)
    removed = 0
    if total <= max_bytes:
        return removed
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        if total <= max_bytes:
            break
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size
        removed += 1
    return removed


def wait_until_readable(key: str, expected_sha256: str, attempts: int = 3) -> None:
    for attempt in range(attempts):
        with tempfile.NamedTemporaryFile(suffix=".verify", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            download(key, temporary)
            if sha256_file(temporary) == expected_sha256:
                return
        finally:
            temporary.unlink(missing_ok=True)
        time.sleep(1 + attempt)
    raise RuntimeError("OSS verification failed")
