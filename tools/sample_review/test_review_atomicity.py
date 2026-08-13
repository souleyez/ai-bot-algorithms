#!/usr/bin/env python3
"""Step 2 failing tests: atomicity, idempotency, trigger, isolation, snapshot."""

from __future__ import annotations

import sqlite3
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from PIL import Image

from tools.sample_review import review_revisions


BOX = [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3, "label": "takeaway"}]


CREATE_ITEMS_SQL = """
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL,
    display_index INTEGER NOT NULL,
    filename TEXT NOT NULL,
    image_path TEXT NOT NULL,
    source_image TEXT NOT NULL DEFAULT '',
    split_name TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT 'pending',
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    ingest_key TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_device TEXT NOT NULL DEFAULT '',
    source_mtime INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0,
    annotations TEXT NOT NULL DEFAULT '[]',
    storage_backend TEXT NOT NULL DEFAULT 'local',
    object_key TEXT NOT NULL DEFAULT '',
    object_sha256 TEXT NOT NULL DEFAULT '',
    migrated_at TEXT NOT NULL DEFAULT '',
    ai_decision TEXT NOT NULL DEFAULT '',
    ai_notes TEXT NOT NULL DEFAULT '',
    ai_model TEXT NOT NULL DEFAULT '',
    ai_confidence REAL NOT NULL DEFAULT 0,
    ai_annotations TEXT NOT NULL DEFAULT '[]',
    ai_labeled_at TEXT NOT NULL DEFAULT '',
    ai_attempted_at TEXT NOT NULL DEFAULT '',
    ai_error TEXT NOT NULL DEFAULT '',
    human_reviewed INTEGER NOT NULL DEFAULT 0,
    human_reviewed_at TEXT NOT NULL DEFAULT ''
)
"""


@contextmanager
def _temp_db():
    directory = tempfile.mkdtemp(prefix="review-txn-test-")
    previous_root = os.environ.get("SAMPLE_REVIEW_ROOT")
    os.environ["SAMPLE_REVIEW_ROOT"] = directory
    try:
        yield Path(directory) / "review.sqlite3"
    finally:
        if previous_root is None:
            os.environ.pop("SAMPLE_REVIEW_ROOT", None)
        else:
            os.environ["SAMPLE_REVIEW_ROOT"] = previous_root
        for child in sorted(Path(directory).rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                child.unlink() if child.is_file() else child.rmdir()
            except OSError:
                pass
        try:
            Path(directory).rmdir()
        except OSError:
            pass


def _seed_one_item(connection: sqlite3.Connection, item_id: str, source_kind: str = "takeaway") -> None:
    connection.executescript(CREATE_ITEMS_SQL)
    now = "2026-08-12T00:00:00+00:00"
    connection.execute(
        """
        INSERT INTO items (
            id, group_name, display_index, filename, image_path, source_image, split_name,
            sha256, decision, notes, updated_at, ingest_key, source_kind, source_device,
            source_mtime, file_size, annotations, storage_backend, object_key, object_sha256,
            migrated_at, ai_decision, ai_notes, ai_model, ai_confidence, ai_annotations,
            ai_labeled_at, ai_attempted_at, ai_error, human_reviewed, human_reviewed_at
        ) VALUES (?, ?, ?, ?, ?, '', '', ?, 'pending', '', ?, ?, ?, 'dev1', 0, 1000, '[]', 'local', '', '', '', '', '', '', 0, '[]', '', '', '', 0, '')
        """,
        (
            item_id,
            source_kind,
            1,
            f"{item_id}.jpg",
            f"{source_kind}/{item_id}.jpg",
            item_id * 8,
            now,
            item_id,
            source_kind,
        ),
    )
    image_path = Path(os.environ["SAMPLE_REVIEW_ROOT"]) / "data" / "images" / source_kind / f"{item_id}.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 120), "white").save(image_path)
    connection.commit()
    review_revisions.migrate(connection)
    connection.commit()


class ReviewTransactionTests(unittest.TestCase):
    def test_trigger_aborts_direct_update_on_fact_revisions(self) -> None:
        with _temp_db() as db_path:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            _seed_one_item(connection, "trigger-update")
            # Insert one fact row manually
            connection.execute(
                """
                INSERT INTO review_fact_revisions (
                    item_id, algorithm_key, review_revision,
                    canonical_fact_json, fact_digest, image_sha256,
                    resolver_metadata_json, created_at
                ) VALUES ('trigger-update', 'takeaway_uniform', 1, '{}', 'a' * 64, 'a' * 64, '{}', '2026-08-12T00:00:00+00:00')
                """
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                connection.execute(
                    "UPDATE review_fact_revisions SET canonical_fact_json = '{\"tampered\":true}' WHERE item_id = 'trigger-update'"
                )
            self.assertIn("immutable", str(ctx.exception).lower())
            connection.close()

    def test_trigger_aborts_direct_delete_on_fact_revisions(self) -> None:
        with _temp_db() as db_path:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            _seed_one_item(connection, "trigger-delete")
            connection.execute(
                """
                INSERT INTO review_fact_revisions (
                    item_id, algorithm_key, review_revision,
                    canonical_fact_json, fact_digest, image_sha256,
                    resolver_metadata_json, created_at
                ) VALUES ('trigger-delete', 'takeaway_uniform', 1, '{}', 'b' * 64, 'b' * 64, '{}', '2026-08-12T00:00:00+00:00')
                """
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                connection.execute("DELETE FROM review_fact_revisions WHERE item_id = 'trigger-delete'")
            self.assertIn("immutable", str(ctx.exception).lower())
            connection.close()

    def test_trigger_aborts_direct_update_on_regression_selections(self) -> None:
        with _temp_db() as db_path:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            _seed_one_item(connection, "trigger-reg")
            connection.execute(
                """
                INSERT INTO regression_selections (
                    selection_id, algorithm_key, selection_revision,
                    canonical_selection_json, selection_digest, status,
                    idempotency_key, request_fingerprint, created_at
                ) VALUES ('sel-1', 'takeaway_uniform', 1, '{}', 'c' * 64, 'active', 'idem-1', 'fp-1', '2026-08-12T00:00:00+00:00')
                """
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                connection.execute(
                    "UPDATE regression_selections SET canonical_selection_json = '{\"x\":1}' WHERE selection_id = 'sel-1'"
                )
            self.assertIn("immutable", str(ctx.exception).lower())
            connection.close()

    def test_trigger_aborts_direct_delete_on_regression_selections(self) -> None:
        with _temp_db() as db_path:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            _seed_one_item(connection, "trigger-reg-del")
            connection.execute(
                """
                INSERT INTO regression_selections (
                    selection_id, algorithm_key, selection_revision,
                    canonical_selection_json, selection_digest, status,
                    idempotency_key, request_fingerprint, created_at
                ) VALUES ('sel-2', 'takeaway_uniform', 1, '{}', 'd' * 64, 'active', 'idem-2', 'fp-2', '2026-08-12T00:00:00+00:00')
                """
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                connection.execute("DELETE FROM regression_selections WHERE selection_id = 'sel-2'")
            self.assertIn("immutable", str(ctx.exception).lower())
            connection.close()

    def test_idempotency_receipt_replay_same_fingerprint(self) -> None:
        with _temp_db() as db_path:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            _seed_one_item(connection, "idem-replay")
            review_revisions.migrate(connection)
            connection.commit()
            review_revisions.record_review_command(
                connection,
                item_id="idem-replay",
                decision="positive",
                notes="",
                annotations=BOX,
                idempotency_key="key-1",
            )
            connection.commit()
            receipt = connection.execute(
                "SELECT idempotency_key, result_revision FROM review_command_receipts WHERE idempotency_key = 'key-1'"
            ).fetchone()
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["result_revision"], 1)
            # Replay with same payload returns same revision, no second receipt row
            review_revisions.record_review_command(
                connection,
                item_id="idem-replay",
                decision="positive",
                notes="",
                annotations=BOX,
                idempotency_key="key-1",
            )
            connection.commit()
            count = connection.execute("SELECT COUNT(*) FROM review_command_receipts").fetchone()[0]
            self.assertEqual(count, 1)
            connection.close()

    def test_idempotency_conflict_on_different_fingerprint(self) -> None:
        with _temp_db() as db_path:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            _seed_one_item(connection, "idem-conflict")
            review_revisions.migrate(connection)
            connection.commit()
            review_revisions.record_review_command(
                connection,
                item_id="idem-conflict",
                decision="positive",
                notes="",
                annotations=BOX,
                idempotency_key="key-2",
            )
            connection.commit()
            with self.assertRaises(review_revisions.IdempotencyConflict):
                review_revisions.record_review_command(
                    connection,
                    item_id="idem-conflict",
                    decision="negative",  # different fingerprint
                    notes="",
                    annotations=[],
                    idempotency_key="key-2",
                )
            connection.close()

    def test_rollback_produces_no_receipt_no_outbox_no_ledger(self) -> None:
        with _temp_db() as db_path:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            _seed_one_item(connection, "rollback-item")
            review_revisions.migrate(connection)
            connection.commit()
            with self.assertRaises(ValueError):
                review_revisions.record_review_command(
                    connection,
                    item_id="rollback-item",
                    decision="positive",
                    notes="",
                    annotations=[{"x": 1.5, "y": 0.1, "w": 0.2, "h": 0.2, "label": "courier"}],  # out of bounds
                    idempotency_key="key-rollback",
                )
            connection.rollback()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_command_receipts").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_publication_outbox").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_fact_revisions").fetchone()[0],
                0,
            )
            connection.close()

    def test_notes_only_edit_does_not_publish_outbox(self) -> None:
        with _temp_db() as db_path:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            _seed_one_item(connection, "notes-only")
            review_revisions.migrate(connection)
            connection.commit()
            # First human decision publishes
            review_revisions.record_review_command(
                connection,
                item_id="notes-only",
                decision="positive",
                notes="initial",
                annotations=BOX,
                idempotency_key="key-3",
            )
            connection.commit()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_publication_outbox").fetchone()[0],
                1,
            )
            # Notes-only update does NOT enqueue another outbox row
            review_revisions.record_review_command(
                connection,
                item_id="notes-only",
                decision=None,  # no decision change
                notes="typo fix",
                annotations=None,
                idempotency_key="key-4",
            )
            connection.commit()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_publication_outbox").fetchone()[0],
                1,
            )
            connection.close()

    def test_algorithm_isolation_triplet(self) -> None:
        """Two items with the same item_id under different algorithms must not
        collide in ledger, outbox, or receipts."""
        with _temp_db() as db_path:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            _seed_one_item(connection, "shared-id", source_kind="takeaway")
            review_revisions.migrate(connection)
            connection.commit()
            review_revisions.record_review_command(
                connection,
                item_id="shared-id",
                decision="positive",
                notes="",
                annotations=BOX,
                idempotency_key="key-shared",
                algorithm_key="takeaway_uniform",
            )
            review_revisions.record_review_command(
                connection,
                item_id="shared-id",
                decision="negative",
                notes="",
                annotations=[],
                idempotency_key="key-shared-2",
                algorithm_key="new_world_workwear",
            )
            connection.commit()
            # Both ledger rows exist, keyed by (algorithm_key, item_id, review_revision)
            rows = connection.execute(
                "SELECT algorithm_key, item_id, review_revision FROM review_fact_revisions WHERE item_id = 'shared-id' ORDER BY algorithm_key"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                {(row["algorithm_key"], row["item_id"], row["review_revision"]) for row in rows},
                {("takeaway_uniform", "shared-id", 1), ("new_world_workwear", "shared-id", 1)},
            )
            connection.close()

    def test_snapshot_includes_latest_fact_only_membership_has_both(self) -> None:
        """If r2 and r3 are pending for one item, the snapshot item table holds
        only the canonical r3 fact while membership holds both outbox ids."""
        with _temp_db() as db_path:
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            _seed_one_item(connection, "snapshot-item")
            review_revisions.migrate(connection)
            connection.commit()
            # Two sequential decisions -> r1, r2
            review_revisions.record_review_command(
                connection, item_id="snapshot-item", decision="positive", notes="", annotations=BOX, idempotency_key="snap-1"
            )
            review_revisions.record_review_command(
                connection, item_id="snapshot-item", decision="negative", notes="", annotations=[], idempotency_key="snap-2"
            )
            connection.commit()
            snapshot_id = review_revisions.create_snapshot(
                connection, algorithm_key="takeaway_uniform", snapshot_watermark=2
            )
            connection.commit()
            items = connection.execute(
                "SELECT review_revision, fact_digest FROM review_publication_snapshot_items WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["review_revision"], 2)
            members = connection.execute(
                "SELECT outbox_id, review_revision FROM review_publication_snapshot_outbox_members WHERE snapshot_id = ? ORDER BY review_revision",
                (snapshot_id,),
            ).fetchall()
            self.assertEqual(len(members), 2)
            self.assertEqual([m["review_revision"] for m in members], [1, 2])
            connection.close()


if __name__ == "__main__":
    unittest.main()
