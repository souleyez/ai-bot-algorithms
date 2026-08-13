#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tools.sample_review import regression_store, review_revisions
from tools.sample_review.test_review_atomicity import CREATE_ITEMS_SQL


class RegressionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="regression-store-")
        self.addCleanup(self.temp.cleanup)
        self.previous_root = os.environ.get("SAMPLE_REVIEW_ROOT")
        os.environ["SAMPLE_REVIEW_ROOT"] = self.temp.name
        self.addCleanup(self._restore_root)
        self.connection = sqlite3.connect(Path(self.temp.name) / "review.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(CREATE_ITEMS_SQL)
        now = "2026-08-12T00:00:00+00:00"
        self.connection.execute(
            """INSERT INTO items(id,group_name,display_index,filename,image_path,sha256,decision,updated_at,
               ingest_key,source_kind,source_device,file_size,annotations,human_reviewed,human_reviewed_at)
               VALUES ('reg-a','takeaway',1,'reg-a.jpg','takeaway/reg-a.jpg',?,'pending',?,'reg-a','takeaway','dev1',1000,'[]',0,'')""",
            ("a" * 64, now),
        )
        path = Path(self.temp.name) / "data" / "images" / "takeaway" / "reg-a.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 96), "white").save(path)
        review_revisions.migrate(self.connection)
        result = review_revisions.record_review_command(
            self.connection,
            item_id="reg-a",
            decision="positive",
            notes="",
            annotations=[{"x": .1, "y": .1, "w": .3, "h": .4, "label": "takeaway"}],
            idempotency_key="review-reg-a",
        )
        self.fact_digest = result["fact_digest"]
        self.connection.commit()

    def _restore_root(self) -> None:
        if self.previous_root is None:
            os.environ.pop("SAMPLE_REVIEW_ROOT", None)
        else:
            os.environ["SAMPLE_REVIEW_ROOT"] = self.previous_root

    def _items(self, digest: str | None = None):
        return [
            {
                "item_id": "reg-a",
                "review_revision": 1,
                "review_fact_digest": digest or self.fact_digest,
                "regression_roles": ["hard_positive", "box_edge_case"],
            }
        ]

    def test_creates_content_addressed_selection_and_replays(self) -> None:
        first = regression_store.create_selection(
            self.connection,
            algorithm_key="takeaway_uniform",
            items=self._items(),
            idempotency_key="selection-1",
        )
        replay = regression_store.create_selection(
            self.connection,
            algorithm_key="takeaway_uniform",
            items=list(reversed(self._items())),
            idempotency_key="selection-1",
        )
        self.assertEqual(first["content_sha256"], replay["content_sha256"])
        self.assertTrue(replay["replayed"])
        loaded = regression_store.get_selection(self.connection, first["selection_id"])
        self.assertEqual(loaded["items"][0]["review_fact_digest"], self.fact_digest)

    def test_changed_idempotent_request_conflicts(self) -> None:
        regression_store.create_selection(
            self.connection,
            algorithm_key="takeaway_uniform",
            items=self._items(),
            idempotency_key="selection-2",
        )
        changed = self._items()
        changed[0]["regression_roles"] = ["device_scene"]
        with self.assertRaises(regression_store.RegressionSelectionConflict):
            regression_store.create_selection(
                self.connection,
                algorithm_key="takeaway_uniform",
                items=changed,
                idempotency_key="selection-2",
            )

    def test_rejects_digest_or_algorithm_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            regression_store.create_selection(
                self.connection,
                algorithm_key="takeaway_uniform",
                items=self._items("0" * 64),
                idempotency_key="selection-bad",
            )
        with self.assertRaises(KeyError):
            regression_store.create_selection(
                self.connection,
                algorithm_key="new_world_workwear",
                items=self._items(),
                idempotency_key="selection-wrong-algorithm",
            )


if __name__ == "__main__":
    unittest.main()
