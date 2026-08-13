#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tools.sample_review import capture_export, preview_resolver, review_revisions
from tools.sample_review.test_review_atomicity import CREATE_ITEMS_SQL


class CaptureExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="capture-export-")
        self.addCleanup(self.temp.cleanup)
        self.previous_root = os.environ.get("SAMPLE_REVIEW_ROOT")
        os.environ["SAMPLE_REVIEW_ROOT"] = self.temp.name
        self.addCleanup(self._restore_root)
        self.connection = sqlite3.connect(Path(self.temp.name) / "capture.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(CREATE_ITEMS_SQL)
        now = "2026-08-12T00:00:00+00:00"
        for index, item_id in enumerate(("capture-a", "capture-b"), 1):
            self.connection.execute(
                """INSERT INTO items(id,group_name,display_index,filename,image_path,sha256,decision,updated_at,
                   ingest_key,source_kind,source_device,source_mtime,file_size,annotations,human_reviewed,human_reviewed_at)
                   VALUES (?,?,?,?,?,?,'pending',?,?,'takeaway','dev1',1700000000,1000,'[]',0,'')""",
                (item_id, "takeaway", index, f"{item_id}.jpg", f"takeaway/{item_id}.jpg", "a"*64, now, item_id),
            )
            path = Path(self.temp.name) / "data" / "images" / "takeaway" / f"{item_id}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (64, 96), "white").save(path)
        review_revisions.migrate(self.connection)
        capture_export.migrate(self.connection)
        self.key = b"capture-cursor-key-32-bytes-long!!"

    def _restore_root(self) -> None:
        if self.previous_root is None:
            os.environ.pop("SAMPLE_REVIEW_ROOT", None)
        else:
            os.environ["SAMPLE_REVIEW_ROOT"] = self.previous_root

    def _resolver(self, row: sqlite3.Row) -> Path:
        return Path(self.temp.name) / "data" / "images" / row["image_path"]

    def test_raw_capture_has_no_truth_or_training_fields(self) -> None:
        created = capture_export.create_snapshot(
            self.connection, lease_owner="capture-connector", image_resolver=self._resolver
        )
        page = capture_export.page_snapshot(
            self.connection,
            snapshot_id=created["snapshot_id"],
            signing_key=self.key,
            lease_owner="capture-connector",
            limit=10,
        )
        self.assertEqual(len(page["items"]), 2)
        for item in page["items"]:
            self.assertEqual(item["schema_version"], "ai-bot-capture-item.v1")
            self.assertNotIn("human_truth", item)
            self.assertNotIn("eligibility", item)
            self.assertNotIn("ai_original", item)

    def test_continuous_arrival_waits_for_next_snapshot(self) -> None:
        created = capture_export.create_snapshot(
            self.connection, lease_owner="capture-connector", image_resolver=self._resolver
        )
        path = Path(self.temp.name) / "data" / "images" / "takeaway" / "capture-c.jpg"
        Image.new("RGB", (64, 96), "green").save(path)
        self.connection.execute(
            """INSERT INTO items(id,group_name,display_index,filename,image_path,sha256,decision,updated_at,
               ingest_key,source_kind,source_device,source_mtime,file_size,annotations,human_reviewed,human_reviewed_at)
               VALUES ('capture-c','takeaway',3,'capture-c.jpg','takeaway/capture-c.jpg',?,'pending',?,
               'capture-c','takeaway','dev1',1700000001,1000,'[]',0,'')""",
            ("c"*64, "2026-08-12T00:00:01+00:00"),
        )
        old_page = capture_export.page_snapshot(
            self.connection,
            snapshot_id=created["snapshot_id"],
            signing_key=self.key,
            lease_owner="capture-connector",
        )
        self.assertNotIn("capture-c", {item["item_id"] for item in old_page["items"]})

    def test_commit_and_ack_are_exact_and_idempotent(self) -> None:
        created = capture_export.create_snapshot(
            self.connection, lease_owner="capture-connector", image_resolver=self._resolver
        )
        digest = "e" * 64
        capture_export.record_commit(
            self.connection,
            snapshot_id=created["snapshot_id"],
            source_version_id="capture-source-version:1",
            source_content_digest=digest,
            idempotency_key="capture-commit-1",
        )
        capture_export.acknowledge(
            self.connection,
            snapshot_id=created["snapshot_id"],
            source_version_id="capture-source-version:1",
            source_content_digest=digest,
        )
        capture_export.acknowledge(
            self.connection,
            snapshot_id=created["snapshot_id"],
            source_version_id="capture-source-version:1",
            source_content_digest=digest,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM capture_publication_outbox WHERE published_at<>''").fetchone()[0],
            2,
        )

    def test_capture_preview_is_identifier_and_revision_bound(self) -> None:
        capture_export.discover_captures(self.connection, image_resolver=self._resolver)
        result = preview_resolver.resolve_capture_preview(
            self.connection,
            algorithm_key="takeaway_uniform",
            item_id="capture-a",
            capture_revision=1,
            image_resolver=self._resolver,
        )
        self.assertEqual(result.headers["Cache-Control"], "private, no-store")
        with self.assertRaises(preview_resolver.PreviewResolutionError):
            preview_resolver.resolve_capture_preview(
                self.connection,
                algorithm_key="takeaway_uniform",
                item_id="capture-a",
                capture_revision=2,
                image_resolver=self._resolver,
            )


if __name__ == "__main__":
    unittest.main()
