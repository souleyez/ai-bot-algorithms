#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tools.sample_review import asset_export, review_revisions
from tools.sample_review.test_review_atomicity import CREATE_ITEMS_SQL


class AssetExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="asset-export-")
        self.addCleanup(self.temp.cleanup)
        self.previous_root = os.environ.get("SAMPLE_REVIEW_ROOT")
        os.environ["SAMPLE_REVIEW_ROOT"] = self.temp.name
        self.addCleanup(self._restore_root)
        self.connection = sqlite3.connect(Path(self.temp.name) / "review.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(CREATE_ITEMS_SQL)
        now = "2026-08-12T00:00:00+00:00"
        for index, item_id in enumerate(("export-a", "export-b"), 1):
            self.connection.execute(
                """INSERT INTO items(id,group_name,display_index,filename,image_path,sha256,decision,updated_at,
                   ingest_key,source_kind,source_device,file_size,annotations,human_reviewed,human_reviewed_at)
                   VALUES (?,?,?,?,?,?,'pending',?,?,'takeaway','dev1',1000,'[]',0,'')""",
                (
                    item_id, "takeaway", index, f"{item_id}.jpg", f"takeaway/{item_id}.jpg",
                    "a" * 64, now, item_id,
                ),
            )
            path = Path(self.temp.name) / "data" / "images" / "takeaway" / f"{item_id}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (64, 96), "white").save(path)
        review_revisions.migrate(self.connection)
        for item_id in ("export-a", "export-b"):
            review_revisions.record_review_command(
                self.connection,
                item_id=item_id,
                decision="positive",
                notes="",
                annotations=[{"x": .1, "y": .1, "w": .3, "h": .4, "label": "takeaway"}],
                idempotency_key=f"review-{item_id}",
            )
        self.connection.commit()
        self.key = b"cursor-test-key-32-bytes-minimum!!"

    def _restore_root(self) -> None:
        if self.previous_root is None:
            os.environ.pop("SAMPLE_REVIEW_ROOT", None)
        else:
            os.environ["SAMPLE_REVIEW_ROOT"] = self.previous_root

    def test_pages_only_frozen_canonical_payloads(self) -> None:
        created = asset_export.create_review_snapshot(
            self.connection, algorithm_key="takeaway_uniform", lease_owner="connector-1"
        )
        page1 = asset_export.page_review_snapshot(
            self.connection,
            snapshot_id=created["snapshot_id"],
            signing_key=self.key,
            limit=1,
            lease_owner="connector-1",
        )
        self.assertEqual(len(page1["items"]), 1)
        self.assertTrue(page1["next_cursor"])
        self.connection.execute("UPDATE items SET notes='later mutable edit' WHERE id='export-b'")
        page2 = asset_export.page_review_snapshot(
            self.connection,
            snapshot_id=created["snapshot_id"],
            signing_key=self.key,
            limit=1,
            cursor=page1["next_cursor"],
            lease_owner="connector-1",
        )
        self.assertEqual(len(page2["items"]), 1)
        self.assertFalse(page2["next_cursor"])
        self.assertNotIn("later mutable edit", str(page2["items"]))

    def test_cursor_tamper_and_scope_mismatch_fail_closed(self) -> None:
        created = asset_export.create_review_snapshot(
            self.connection, algorithm_key="takeaway_uniform", lease_owner="connector-1"
        )
        page = asset_export.page_review_snapshot(
            self.connection,
            snapshot_id=created["snapshot_id"],
            signing_key=self.key,
            limit=1,
            lease_owner="connector-1",
        )
        body, signature = page["next_cursor"].split(".", 1)
        tampered = body + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
        with self.assertRaises(asset_export.CursorError):
            asset_export.page_review_snapshot(
                self.connection,
                snapshot_id=created["snapshot_id"],
                signing_key=self.key,
                cursor=tampered,
                lease_owner="connector-1",
            )

    def test_commit_then_exact_ack_marks_members_and_replays(self) -> None:
        created = asset_export.create_review_snapshot(
            self.connection, algorithm_key="takeaway_uniform", lease_owner="connector-1"
        )
        digest = "d" * 64
        asset_export.commit_review_snapshot(
            self.connection,
            snapshot_id=created["snapshot_id"],
            source_version_id="source-version:1",
            source_content_digest=digest,
            idempotency_key="commit-1",
        )
        asset_export.acknowledge_review_snapshot(
            self.connection,
            snapshot_id=created["snapshot_id"],
            source_version_id="source-version:1",
            source_content_digest=digest,
        )
        asset_export.acknowledge_review_snapshot(
            self.connection,
            snapshot_id=created["snapshot_id"],
            source_version_id="source-version:1",
            source_content_digest=digest,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM review_publication_outbox WHERE published_at<>''"
            ).fetchone()[0],
            2,
        )


if __name__ == "__main__":
    unittest.main()
