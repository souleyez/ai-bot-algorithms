#!/usr/bin/env python3
"""Step 1 failing tests: review-revision migration must add 4 new items
columns, 7 new tables and exact immutable triggers, with deterministic
legacy backfill (r1 ledger + initial outbox) for human-reviewed rows."""

from __future__ import annotations

import os
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from PIL import Image


@contextmanager
def _temp_db():
    """mkdtemp-based temp db; avoids WorkBuddy shim recursion in rmtree."""
    directory = tempfile.mkdtemp(prefix="review-revisions-test-")
    try:
        yield Path(directory) / "review.sqlite3"
    finally:
        # Best-effort cleanup; the OS will clean up the temp dir on reboot.
        for child in Path(directory).glob("*"):
            try:
                child.unlink()
            except OSError:
                pass
        try:
            os.rmdir(directory)
        except OSError:
            pass

from tools.sample_review import review_revisions


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


class ReviewRevisionMigrationTests(unittest.TestCase):
    """A fresh sqlite database is migrated by the review-revision helper."""

    def _connect(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    def _seed_items(self, connection: sqlite3.Connection) -> None:
        connection.executescript(CREATE_ITEMS_SQL)
        now = "2026-08-12T00:00:00+00:00"
        # 31 values per row matching CREATE_ITEMS_SQL column order
        # Build rows programmatically to guarantee 31 values per row
        seed_specs = [
            # (item_id, source_kind, decision, annotations, ai_decision, ai_model, ai_confidence, ai_annotations, human_reviewed, human_reviewed_at)
            ("rev-pos", "takeaway", "positive", '[{"x":0.1,"y":0.1,"w":0.2,"h":0.2,"label":"courier"}]',
             "positive", "legacy-minimax", 0.9, '[{"x":0.1,"y":0.1,"w":0.2,"h":0.2,"label":"courier","confidence":0.9}]', 1, now),
            ("rev-neg", "workwear", "negative", "[]",
             "", "", 0, "[]", 1, now),
            ("rev-pending", "takeaway", "pending", "[]",
             "positive", "legacy-minimax", 0.85, '[{"x":0.2,"y":0.2,"w":0.3,"h":0.3,"label":"courier","confidence":0.85}]', 0, ""),
        ]
        rows = []
        for idx, (item_id, source_kind, decision, annotations, ai_decision, ai_model, ai_confidence, ai_annotations, human_reviewed, human_reviewed_at) in enumerate(seed_specs):
            hash_byte = "abc"[idx]
            rows.append((
                item_id, source_kind, idx + 1, f"{item_id}.jpg", f"{source_kind}/{item_id}.jpg", "", "", hash_byte * 64,
                decision, "", now, item_id, source_kind, "dev1", 1_700_000_000 + idx * 100, 1000 + idx * 1000,
                annotations, "local", "", "", "",
                ai_decision, "", ai_model, float(ai_confidence), ai_annotations,
                now if human_reviewed else "", now if human_reviewed else "", "", int(human_reviewed), human_reviewed_at,
            ))
        placeholders = ",".join(["?"] * 31)
        connection.executemany(f"INSERT INTO items VALUES ({placeholders})", rows)

    def _migrate(self, path: Path) -> sqlite3.Connection:
        connection = self._connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        self._seed_items(connection)
        for item_id in ("rev-pos", "rev-neg", "rev-pending"):
            Image.new("RGB", (64, 96), "white").save(path.parent / f"{item_id}.jpg")
        connection.commit()
        review_revisions.migrate(
            connection,
            image_resolver=lambda row: path.parent / f"{row['id']}.jpg",
        )
        connection.commit()
        return connection

    def test_items_has_review_revision_columns(self) -> None:
        with _temp_db() as db_path:
            connection = self._migrate(db_path)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
            for required in (
                "review_revision",
                "capture_group_id",
                "human_label_keys_json",
                "human_tag_keys_json",
            ):
                self.assertIn(required, columns)
            connection.close()

    def test_new_tables_exist(self) -> None:
        with _temp_db() as db_path:
            connection = self._migrate(db_path)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for required in (
                "review_publication_outbox",
                "review_fact_revisions",
                "review_publication_snapshots",
                "review_publication_snapshot_items",
                "review_publication_snapshot_outbox_members",
                "review_command_receipts",
                "regression_selections",
            ):
                self.assertIn(required, tables)
            connection.close()

    def test_immutable_triggers_exist_with_exact_names(self) -> None:
        with _temp_db() as db_path:
            connection = self._migrate(db_path)
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            for required in (
                "review_fact_revisions_no_update",
                "review_fact_revisions_no_delete",
                "regression_selections_no_update",
                "regression_selections_no_delete",
            ):
                self.assertIn(required, triggers)
            connection.close()

    def test_outbox_unique_constraint(self) -> None:
        with _temp_db() as db_path:
            connection = self._migrate(db_path)
            keys = connection.execute(
                "PRAGMA index_list(review_publication_outbox)"
            ).fetchall()
            unique_triplets = [
                key
                for key in keys
                if key[2] == 1  # unique
            ]
            self.assertTrue(unique_triplets, "expected a UNIQUE index on outbox")
            connection.close()

    def test_fact_ledger_primary_key_triplet(self) -> None:
        with _temp_db() as db_path:
            connection = self._migrate(db_path)
            pk_columns = [
                row[1]
                for row in connection.execute("PRAGMA table_info(review_fact_revisions)")
                if row[5] > 0
            ]
            self.assertEqual(
                sorted(pk_columns),
                ["algorithm_key", "item_id", "review_revision"],
            )
            connection.close()

    def test_legacy_backfill_human_reviewed_gets_revision_one(self) -> None:
        with _temp_db() as db_path:
            connection = self._migrate(db_path)
            rows = {
                row["id"]: row["review_revision"]
                for row in connection.execute(
                    "SELECT id, review_revision FROM items ORDER BY id"
                )
            }
            self.assertEqual(rows["rev-pos"], 1)
            self.assertEqual(rows["rev-neg"], 1)
            self.assertEqual(rows["rev-pending"], 0)
            connection.close()

    def test_legacy_backfill_writes_initial_outbox_row(self) -> None:
        with _temp_db() as db_path:
            connection = self._migrate(db_path)
            outbox = connection.execute(
                "SELECT item_id, algorithm_key, review_revision, change_type, published_at FROM review_publication_outbox ORDER BY item_id"
            ).fetchall()
            self.assertEqual(len(outbox), 2)
            by_item = {row["item_id"]: row for row in outbox}
            self.assertEqual(by_item["rev-pos"]["algorithm_key"], "takeaway_uniform")
            self.assertEqual(by_item["rev-pos"]["review_revision"], 1)
            self.assertEqual(by_item["rev-pos"]["change_type"], "human_review")
            self.assertEqual(by_item["rev-pos"]["published_at"], "")
            self.assertEqual(by_item["rev-neg"]["algorithm_key"], "new_world_workwear")
            connection.close()

    def test_legacy_backfill_writes_r1_ledger_row(self) -> None:
        with _temp_db() as db_path:
            connection = self._migrate(db_path)
            ledger = connection.execute(
                "SELECT algorithm_key, item_id, review_revision, canonical_fact_json, fact_digest "
                "FROM review_fact_revisions ORDER BY item_id"
            ).fetchall()
            self.assertEqual(len(ledger), 2)
            by_item = {row["item_id"]: row for row in ledger}
            self.assertEqual(by_item["rev-pos"]["algorithm_key"], "takeaway_uniform")
            self.assertEqual(by_item["rev-pos"]["review_revision"], 1)
            self.assertTrue(by_item["rev-pos"]["canonical_fact_json"])
            self.assertRegex(by_item["rev-pos"]["fact_digest"], r"^[a-f0-9]{64}$")
            fact = json.loads(by_item["rev-pos"]["canonical_fact_json"])
            self.assertEqual(fact["primary_observation_status"], "absent_legacy")
            self.assertNotIn("ai_original", fact)
            self.assertEqual(fact["observation_comparisons"], [])
            self.assertEqual(fact["correction"]["types"], ["unavailable"])
            self.assertEqual(fact["image"]["width"], 64)
            self.assertEqual(fact["image"]["height"], 96)
            self.assertNotIn("legacy-minimax", by_item["rev-pos"]["canonical_fact_json"])
            connection.close()

    def test_missing_image_is_quarantined_without_revision_or_outbox(self) -> None:
        with _temp_db() as db_path:
            connection = self._connect(db_path)
            self._seed_items(connection)
            # Only create images for the negative and pending rows.  The reviewed
            # positive cannot truthfully publish dimensions/digest and must stop.
            for item_id in ("rev-neg", "rev-pending"):
                Image.new("RGB", (64, 96), "white").save(db_path.parent / f"{item_id}.jpg")
            review_revisions.migrate(
                connection,
                image_resolver=lambda row: db_path.parent / f"{row['id']}.jpg",
            )
            self.assertEqual(
                connection.execute("SELECT review_revision FROM items WHERE id='rev-pos'").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_publication_outbox WHERE item_id='rev-pos'").fetchone()[0],
                0,
            )
            reason = connection.execute(
                "SELECT reason_code FROM review_fact_quarantine WHERE item_id='rev-pos'"
            ).fetchone()[0]
            self.assertEqual(reason, "image_unavailable")
            connection.close()

    def test_idempotent_second_init(self) -> None:
        """initialize_database is called twice at startup; the migration must
        be safe to repeat without raising or duplicating ledger rows."""
        with _temp_db() as db_path:
            connection = self._migrate(db_path)
            review_revisions.migrate(
                connection,
                image_resolver=lambda row: db_path.parent / f"{row['id']}.jpg",
            )
            connection.commit()
            count = connection.execute(
                "SELECT COUNT(*) FROM review_fact_revisions"
            ).fetchone()[0]
            self.assertEqual(count, 2)
            connection.close()


if __name__ == "__main__":
    unittest.main()
