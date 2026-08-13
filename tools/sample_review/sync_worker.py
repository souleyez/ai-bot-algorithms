#!/usr/bin/env python3
"""Incrementally copy device captures into the private sample review queue."""

from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError

try:
    from . import oss_backend
    from .retention_policy import archive_item, ensure_retention_schema
except ImportError:
    import oss_backend
    from retention_policy import archive_item, ensure_retention_schema

try:
    import paramiko
except ModuleNotFoundError:
    for site_packages in glob.glob("/srv/ai-bot-algorithm-platform/.venv/lib/python*/site-packages"):
        sys.path.insert(0, site_packages)
    import paramiko


ROOT = Path(os.environ.get("SAMPLE_REVIEW_ROOT", "/srv/ai-bot-sample-review"))
DATA_ROOT = ROOT / "data"
IMAGE_ROOT = DATA_ROOT / "images"
DATABASE = DATA_ROOT / "review.sqlite3"
LOCK_PATH = DATA_ROOT / "sync.lock"
VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_credentials() -> tuple[str, str]:
    names = ("AI_BOT_DEVICE_SSH_USER", "AI_BOT_DEVICE_SSH_PASSWORD")
    direct = tuple(os.environ.get(name) for name in names)
    if all(direct):
        return direct[0], direct[1]
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            raw = (process / "environ").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        values: dict[str, str] = {}
        for item in raw.split(b"\0"):
            if b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            decoded = key.decode("ascii", "ignore")
            if decoded in names:
                values[decoded] = value.decode("utf-8")
        if all(values.get(name) for name in names):
            return values[names[0]], values[names[1]]
    raise RuntimeError("platform device credentials were not found")


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
    migrations = {
        "storage_backend": "TEXT NOT NULL DEFAULT 'local'",
        "object_key": "TEXT NOT NULL DEFAULT ''",
        "object_sha256": "TEXT NOT NULL DEFAULT ''",
        "migrated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE items ADD COLUMN {column} {definition}")
    ensure_retention_schema(connection)
    return connection


def matches_source(filename: str, source: dict) -> bool:
    lowered = filename.lower()
    if lowered.startswith("s_") or Path(lowered).suffix not in VALID_EXTENSIONS:
        return False
    model = source["model"].lower()
    if model not in lowered:
        return False
    channel = source.get("channel")
    if channel is not None and not lowered.startswith(f"ch{int(channel)}_{model}_"):
        return False
    return True


def storage_allowed(config: dict, connection: sqlite3.Connection) -> tuple[bool, str]:
    usage = shutil.disk_usage(ROOT)
    if usage.free < int(config["min_free_bytes"]):
        return False, f"free space below floor: {usage.free}"
    if not oss_backend.configured():
        total = sum(path.stat().st_size for path in IMAGE_ROOT.rglob("*") if path.is_file())
        if total >= int(config["max_review_bytes"]):
            return False, f"review image limit reached: {total}"
    count = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    if count >= int(config["max_items"]):
        return False, f"review item limit reached: {count}"
    return True, "ok"


def normalized_image(source: Path, destination: Path, max_dimension: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.load()
        image = image.convert("RGB")
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        image.save(destination, format="JPEG", quality=87, optimize=True)


def source_group(source: dict) -> str:
    return f"自动_{source['label']}_{source['device']}_{source['model']}"


def purge_expired_pending(config: dict, dry_run: bool) -> dict:
    retention_days = int(config.get("auto_pending_retention_days", 0))
    if retention_days <= 0 or not DATABASE.exists():
        return {"enabled": False, "candidates": 0, "deleted": 0, "bytes": 0}

    cutoff = int((datetime.now(timezone.utc) - timedelta(days=retention_days)).timestamp())
    auto_kinds = sorted({source["kind"] for source in config["sources"]})
    placeholders = ",".join("?" for _ in auto_kinds)
    with connect_database() as connection:
        rows = connection.execute(
            f"""
            SELECT id, image_path, ingest_key, sha256, source_image, file_size,
                   source_mtime, object_key, object_sha256, storage_backend
            FROM items
            WHERE decision = 'pending'
              AND source_kind IN ({placeholders})
              AND source_mtime > 0
              AND source_mtime < ?
            ORDER BY source_mtime, id
            """,
            (*auto_kinds, cutoff),
        ).fetchall()
        summary = {
            "enabled": True,
            "retention_days": retention_days,
            "cutoff": datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds"),
            "candidates": len(rows),
            "deleted": 0,
            "bytes": sum(int(row["file_size"] or 0) for row in rows),
        }
        if dry_run:
            return summary

        image_root = IMAGE_ROOT.resolve()
        raw_retention_days = int(config.get("oss_raw_retention_days", 90))
        for row in rows:
            image = (IMAGE_ROOT / row["image_path"]).resolve()
            if image != image_root and image_root not in image.parents:
                summary.setdefault("errors", []).append(row["id"])
                continue
            try:
                image.unlink(missing_ok=True)
            except OSError:
                summary.setdefault("errors", []).append(row["id"])
                continue
            connection.execute(
                """
                INSERT OR IGNORE INTO deleted_items
                    (ingest_key, item_id, sha256, source_image, deleted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (row["ingest_key"], row["id"], row["sha256"], row["source_image"], utc_now()),
            )
            archive_item(
                connection,
                row,
                "review-queue-expired",
                retention_days=raw_retention_days,
            )
            connection.execute("DELETE FROM items WHERE id = ?", (row["id"],))
            connection.commit()
            summary["deleted"] += 1
        return summary


def release_migrated_local_images(dry_run: bool) -> dict:
    with connect_database() as connection:
        rows = connection.execute(
            "SELECT image_path FROM items WHERE storage_backend = 'oss' AND object_key != ''"
        ).fetchall()
    image_root = IMAGE_ROOT.resolve()
    candidates: list[Path] = []
    for row in rows:
        image = (IMAGE_ROOT / row["image_path"]).resolve()
        if image != image_root and image_root in image.parents and image.is_file():
            candidates.append(image)
    result = {
        "candidates": len(candidates),
        "bytes": sum(path.stat().st_size for path in candidates),
        "deleted": 0,
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    for path in candidates:
        path.unlink(missing_ok=True)
        result["deleted"] += 1
    for directory in sorted((path for path in IMAGE_ROOT.rglob("*") if path.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return result


def sync_source(client: paramiko.SSHClient, source: dict, config: dict, dry_run: bool) -> dict:
    remote_dir = source.get("remote_dir", "/userdata/mpp/disk")
    sftp = client.open_sftp()
    try:
        entries = [entry for entry in sftp.listdir_attr(remote_dir) if matches_source(entry.filename, source)]
        entries.sort(key=lambda entry: (entry.st_mtime, entry.filename), reverse=True)
        remote_total = len(entries)
        entries = entries[: int(config["remote_scan_limit"])]
        cutoff = float(source.get("not_before_mtime", 0))
        lookback_days = float(source.get("lookback_days", 0))
        if lookback_days > 0:
            cutoff = max(cutoff, time.time() - lookback_days * 24 * 60 * 60)
        if cutoff > 0:
            entries = [entry for entry in entries if entry.st_mtime >= cutoff]
        summary = {
            "source": source_group(source),
            "remote": len(entries),
            "remote_total": remote_total,
            "new": 0,
            "skipped": 0,
            "latest_remote_mtime": int(entries[0].st_mtime) if entries else 0,
            "latest_remote_file": entries[0].filename if entries else "",
        }
        if dry_run:
            return summary

        with connect_database() as connection:
            known_keys = {row[0] for row in connection.execute("SELECT ingest_key FROM items WHERE ingest_key != ''")}
            deleted_keys = {row[0] for row in connection.execute("SELECT ingest_key FROM deleted_items")}
            known_hashes = {row[0] for row in connection.execute("SELECT sha256 FROM items WHERE sha256 != ''")}
            deleted_hashes = {row[0] for row in connection.execute("SELECT sha256 FROM deleted_items WHERE sha256 != ''")}
            group = source_group(source)
            next_index = connection.execute(
                "SELECT COALESCE(MAX(display_index), 0) + 1 FROM items WHERE group_name = ?", (group,)
            ).fetchone()[0]

            for entry in entries:
                if summary["new"] >= int(config["max_new_per_source_per_run"]):
                    break
                remote_path = f"{remote_dir}/{entry.filename}"
                ingest_key = f"{source['device']}|{remote_path}|{int(entry.st_mtime)}|{entry.st_size}"
                if ingest_key in known_keys or ingest_key in deleted_keys:
                    summary["skipped"] += 1
                    continue
                allowed, reason = storage_allowed(config, connection)
                if not allowed:
                    summary["stopped"] = reason
                    break
                with tempfile.NamedTemporaryFile(dir=DATA_ROOT, suffix=Path(entry.filename).suffix, delete=False) as handle:
                    temporary = Path(handle.name)
                try:
                    sftp.get(remote_path, str(temporary))
                    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
                    if digest in known_hashes or digest in deleted_hashes:
                        summary["skipped"] += 1
                        deleted_keys.add(ingest_key)
                        connection.execute(
                            "INSERT OR IGNORE INTO deleted_items (ingest_key, item_id, sha256, source_image, deleted_at) VALUES (?, '', ?, ?, ?)",
                            (ingest_key, digest, f"{source['device']}:{remote_path}", utc_now()),
                        )
                        connection.commit()
                        continue
                    item_id = hashlib.sha1(ingest_key.encode("utf-8")).hexdigest()[:20]
                    relative = Path("auto") / source["kind"] / source["device"] / f"{item_id}.jpg"
                    destination = IMAGE_ROOT / relative
                    normalized_image(temporary, destination, int(config["max_image_dimension"]))
                    size = destination.stat().st_size
                    object_sha256 = ""
                    object_key = ""
                    storage_backend = "local"
                    migrated_at = ""
                    if oss_backend.configured():
                        object_sha256 = oss_backend.sha256_file(destination)
                        object_key = oss_backend.object_key_for_sha256(object_sha256, destination.suffix)
                        oss_backend.upload(destination, object_key)
                        storage_backend = "oss"
                        migrated_at = utc_now()
                    connection.execute(
                        """
                        INSERT INTO items (
                            id, group_name, display_index, filename, image_path, source_image,
                            split_name, sha256, decision, notes, updated_at, ingest_key,
                            source_kind, source_device, source_mtime, file_size,
                            storage_backend, object_key, object_sha256, migrated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, '', ?, 'pending', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item_id, group, next_index, entry.filename, relative.as_posix(),
                            f"{source['device']}:{remote_path}", digest, utc_now(), ingest_key,
                            source["kind"], source["device"], int(entry.st_mtime), size,
                            storage_backend, object_key, object_sha256, migrated_at,
                        ),
                    )
                    known_keys.add(ingest_key)
                    known_hashes.add(digest)
                    next_index += 1
                    summary["new"] += 1
                    # Release the SQLite writer lock after every object. Network
                    # upload and the next device download must never extend it.
                    connection.commit()
                except (OSError, UnidentifiedImageError) as exc:
                    summary.setdefault("errors", []).append(f"{entry.filename}: {type(exc).__name__}")
                finally:
                    temporary.unlink(missing_ok=True)
        return summary
    finally:
        sftp.close()


def sync(config: dict, dry_run: bool) -> dict:
    retention = purge_expired_pending(config, dry_run)
    username, password = load_credentials()
    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for source in config["sources"]:
        grouped[(source["host"], int(source["port"]))].append(source)
    results = []
    for (host, port), sources in grouped.items():
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                host, port=port, username=username, password=password,
                timeout=12, banner_timeout=12, auth_timeout=12,
                allow_agent=False, look_for_keys=False,
            )
            for source in sources:
                try:
                    results.append(sync_source(client, source, config, dry_run))
                except Exception as exc:  # keep other sources moving
                    results.append({"source": source_group(source), "error": type(exc).__name__})
        except Exception as exc:
            for source in sources:
                results.append({"source": source_group(source), "error": type(exc).__name__})
        finally:
            client.close()
    released = release_migrated_local_images(dry_run)
    return {
        "checked_at": utc_now(),
        "dry_run": dry_run,
        "retention": retention,
        "released_local": released,
        "sources": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "sync-config.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"skipped": "sync already running"}))
            return
        print(json.dumps(sync(config, args.dry_run), ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
