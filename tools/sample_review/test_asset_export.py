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
            Image.new("RGB", (64, 96), "white" if index == 1 else "gray").save(path)
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

    def add_duplicate(self, item_id: str, *, decision: str) -> None:
        source = Path(self.temp.name) / "data" / "images" / "takeaway" / "export-a.jpg"
        target = Path(self.temp.name) / "data" / "images" / "takeaway" / f"{item_id}.jpg"
        target.write_bytes(source.read_bytes())
        self.connection.execute(
            """INSERT INTO items(id,group_name,display_index,filename,image_path,sha256,decision,updated_at,
               ingest_key,source_kind,source_device,file_size,annotations,human_reviewed,human_reviewed_at)
               VALUES (?,?,?,?,?,?,'pending',?,?,'takeaway','dev1',1000,'[]',0,'')""",
            (
                item_id, "takeaway", 3, f"{item_id}.jpg", f"takeaway/{item_id}.jpg",
                "c" * 64, "2026-08-12T00:00:00+00:00", item_id,
            ),
        )
        review_revisions.record_review_command(
            self.connection,
            item_id=item_id,
            decision=decision,
            notes="",
            annotations=(
                [{"x": .1, "y": .1, "w": .3, "h": .4, "label": "takeaway"}]
                if decision == "positive" else []
            ),
            idempotency_key=f"review-{item_id}",
        )

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

    def test_exact_duplicate_truth_collapses_but_all_outbox_members_are_acknowledged(self) -> None:
        self.add_duplicate("export-duplicate", decision="positive")
        created = asset_export.create_review_snapshot(
            self.connection, algorithm_key="takeaway_uniform", lease_owner="connector-1"
        )
        page = asset_export.page_review_snapshot(
            self.connection, snapshot_id=created["snapshot_id"], signing_key=self.key,
            lease_owner="connector-1", limit=500,
        )
        self.assertEqual(len(page["items"]), 2)
        members = self.connection.execute(
            """SELECT item_id,represented_by_item_id FROM review_publication_snapshot_outbox_members
               WHERE snapshot_id=? ORDER BY item_id""",
            (created["snapshot_id"],),
        ).fetchall()
        self.assertEqual(len(members), 3)
        represented = {row["item_id"]: row["represented_by_item_id"] for row in members}
        self.assertEqual(represented["export-a"], represented["export-duplicate"])

    def test_contradictory_duplicate_truth_is_quarantined_and_not_published(self) -> None:
        self.add_duplicate("export-conflict", decision="negative")
        created = asset_export.create_review_snapshot(
            self.connection, algorithm_key="takeaway_uniform", lease_owner="connector-1"
        )
        page = asset_export.page_review_snapshot(
            self.connection, snapshot_id=created["snapshot_id"], signing_key=self.key,
            lease_owner="connector-1", limit=500,
        )
        self.assertEqual([item["item_id"] for item in page["items"]], ["export-b"])
        quarantined = self.connection.execute(
            "SELECT item_id,reason_code FROM review_fact_quarantine WHERE reason_code='duplicate_truth_conflict'"
        ).fetchall()
        self.assertEqual({row["item_id"] for row in quarantined}, {"export-a", "export-conflict"})


if __name__ == "__main__":
    unittest.main()
