#!/usr/bin/env python3

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if sys.platform == "win32":
    sys.modules.setdefault(
        "fcntl",
        SimpleNamespace(flock=lambda *_args: None, LOCK_EX=1, LOCK_NB=2),
    )

from tools.sample_review import retention_worker, sync_worker
from tools.sample_review.retention_policy import archive_item, ensure_retention_schema


ITEM_COLUMNS = """
    id TEXT PRIMARY KEY, group_name TEXT, display_index INTEGER, filename TEXT,
    image_path TEXT, source_image TEXT, split_name TEXT, sha256 TEXT,
    decision TEXT, notes TEXT, updated_at TEXT, ingest_key TEXT,
    source_kind TEXT, source_device TEXT, source_mtime INTEGER, file_size INTEGER,
    annotations TEXT, storage_backend TEXT, object_key TEXT, object_sha256 TEXT,
    migrated_at TEXT, ai_decision TEXT, ai_notes TEXT, ai_model TEXT,
    ai_confidence REAL, ai_annotations TEXT, ai_labeled_at TEXT,
    ai_attempted_at TEXT, ai_error TEXT, human_reviewed INTEGER,
    human_reviewed_at TEXT
"""


def create_database(root: Path) -> sqlite3.Connection:
    data = root / "data"
    data.mkdir(parents=True)
    connection = sqlite3.connect(data / "review.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.execute(f"CREATE TABLE items ({ITEM_COLUMNS})")
    connection.execute(
        """
        CREATE TABLE deleted_items (
            ingest_key TEXT PRIMARY KEY, item_id TEXT, sha256 TEXT,
            source_image TEXT, deleted_at TEXT
        )
        """
    )
    ensure_retention_schema(connection)
    connection.commit()
    return connection


def insert_item(
    connection: sqlite3.Connection,
    item_id: str,
    key: str,
    source_mtime: int,
    *,
    human_reviewed: int = 0,
    decision: str = "pending",
) -> None:
    values = {
        "id": item_id,
        "group_name": "auto",
        "display_index": 1,
        "filename": item_id + ".jpg",
        "image_path": "auto/" + item_id + ".jpg",
        "source_image": "device:/" + item_id + ".jpg",
        "split_name": "",
        "sha256": item_id + "-sha",
        "decision": decision,
        "notes": "",
        "updated_at": "now",
        "ingest_key": "ingest:" + item_id,
        "source_kind": "takeaway",
        "source_device": "61672",
        "source_mtime": source_mtime,
        "file_size": 123,
        "annotations": "[]",
        "storage_backend": "oss",
        "object_key": key,
        "object_sha256": item_id + "-object-sha",
        "migrated_at": "now",
        "ai_decision": "positive",
        "ai_notes": "",
        "ai_model": "MiniMax-M3",
        "ai_confidence": 0.9,
        "ai_annotations": "[]",
        "ai_labeled_at": "now",
        "ai_attempted_at": "now",
        "ai_error": "",
        "human_reviewed": human_reviewed,
        "human_reviewed_at": "now" if human_reviewed else "",
    }
    names = list(values)
    connection.execute(
        f"INSERT INTO items ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
        [values[name] for name in names],
    )
    connection.commit()


class FakeOss:
    OSS_PREFIX = "ai-bot-samples"

    def __init__(self, objects=()):
        self.objects = list(objects)
        self.deleted = []

    def iter_objects(self, _prefix):
        yield from self.objects

    def delete(self, key):
        self.deleted.append(key)


class RetentionTests(unittest.TestCase):
    def test_archive_uses_capture_age_and_preserves_longer_existing_deadline(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        ensure_retention_schema(connection)
        row = {
            "id": "one", "object_key": "prefix/one.jpg", "source_mtime": 100,
            "file_size": 50, "object_sha256": "sha",
        }
        archive_item(connection, row, "first", retention_days=90, now_epoch=500)
        archive_item(connection, row, "second", retention_days=30, now_epoch=500)
        stored = connection.execute(
            "SELECT keep_until, archive_reason FROM oss_retention_items"
        ).fetchone()
        self.assertEqual(stored[0], 100 + 90 * 86400)
        self.assertEqual(stored[1], "second")

    def test_apply_deletes_expired_raw_but_keeps_human_confirmed_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = create_database(root)
            old = int(time.time()) - 100 * 86400
            insert_item(connection, "raw", "ai-bot-samples/objects/raw.jpg", old)
            insert_item(
                connection,
                "confirmed",
                "ai-bot-samples/objects/confirmed.jpg",
                old,
                human_reviewed=1,
                decision="positive",
            )
            connection.close()
            fake = FakeOss()
            args = SimpleNamespace(root=root, retention_days=90, limit=100, mode="apply")
            with patch.object(retention_worker, "oss_backend", fake):
                result = retention_worker.run(args)
            self.assertEqual(result["activeExpired"]["archived"], 1)
            self.assertEqual(fake.deleted, ["ai-bot-samples/objects/raw.jpg"])
            connection = sqlite3.connect(root / "data" / "review.sqlite3")
            self.assertEqual(
                [row[0] for row in connection.execute("SELECT id FROM items ORDER BY id")],
                ["confirmed"],
            )
            self.assertEqual(
                connection.execute("SELECT object_deleted_at != '' FROM oss_retention_items").fetchone()[0],
                1,
            )
            connection.close()

    def test_shared_object_is_not_deleted_while_confirmed_row_references_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = create_database(root)
            old = int(time.time()) - 100 * 86400
            shared = "ai-bot-samples/objects/shared.jpg"
            insert_item(connection, "raw", shared, old)
            insert_item(connection, "confirmed", shared, old, human_reviewed=1, decision="negative")
            connection.close()
            fake = FakeOss()
            args = SimpleNamespace(root=root, retention_days=90, limit=100, mode="apply")
            with patch.object(retention_worker, "oss_backend", fake):
                result = retention_worker.run(args)
            self.assertEqual(result["objects"]["protected"], 1)
            self.assertEqual(fake.deleted, [])

    def test_old_unreferenced_object_is_discovered_and_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_database(root).close()
            old = int(time.time()) - 100 * 86400
            key = "ai-bot-samples/objects/orphan.jpg"
            fake = FakeOss([{"key": key, "size": 321, "last_modified": old}])
            args = SimpleNamespace(root=root, retention_days=90, limit=100, mode="apply")
            with patch.object(retention_worker, "oss_backend", fake):
                result = retention_worker.run(args)
            self.assertEqual(result["orphans"]["candidates"], 1)
            self.assertEqual(result["objects"]["bytesDeleted"], 321)
            self.assertEqual(fake.deleted, [key])

    def test_review_queue_expiry_archives_oss_metadata_for_day_90(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = create_database(root)
            old = int(time.time()) - 20 * 86400
            key = "ai-bot-samples/objects/queued.jpg"
            insert_item(connection, "queued", key, old)
            connection.close()
            image_root = root / "data" / "images"
            image_root.mkdir(parents=True)
            config = {
                "auto_pending_retention_days": 14,
                "oss_raw_retention_days": 90,
                "sources": [{"kind": "takeaway"}],
            }
            with patch.object(sync_worker, "DATABASE", root / "data" / "review.sqlite3"), patch.object(
                sync_worker, "IMAGE_ROOT", image_root
            ):
                result = sync_worker.purge_expired_pending(config, dry_run=False)
            self.assertEqual(result["deleted"], 1)
            connection = sqlite3.connect(root / "data" / "review.sqlite3")
            retained = connection.execute(
                "SELECT object_key, keep_until FROM oss_retention_items"
            ).fetchone()
            self.assertEqual(retained[0], key)
            self.assertEqual(retained[1], old + 90 * 86400)
            connection.close()


if __name__ == "__main__":
    unittest.main()
