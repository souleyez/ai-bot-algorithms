#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from tools.sample_review import preview_resolver, review_revisions
from tools.sample_review.test_review_atomicity import CREATE_ITEMS_SQL


class PreviewResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="preview-resolver-")
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
               VALUES ('preview-a','takeaway',1,'preview-a.jpg','takeaway/preview-a.jpg',?,'pending',?,'preview-a','takeaway','dev1',1000,'[]',0,'')""",
            ("a" * 64, now),
        )
        self.path = Path(self.temp.name) / "data" / "images" / "takeaway" / "preview-a.jpg"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2200, 1100), "navy").save(self.path, quality=90)
        review_revisions.migrate(self.connection)
        review_revisions.record_review_command(
            self.connection,
            item_id="preview-a",
            decision="positive",
            notes="",
            annotations=[{"x": .1, "y": .1, "w": .2, "h": .4, "label": "takeaway"}],
            idempotency_key="review-preview-a",
        )
        self.connection.commit()

    def _restore_root(self) -> None:
        if self.previous_root is None:
            os.environ.pop("SAMPLE_REVIEW_ROOT", None)
        else:
            os.environ["SAMPLE_REVIEW_ROOT"] = self.previous_root

    def _resolver(self, row: sqlite3.Row) -> Path:
        return Path(self.temp.name) / "data" / "images" / row["image_path"]

    def test_preview_is_bounded_reencoded_and_no_store(self) -> None:
        result = preview_resolver.resolve_review_preview(
            self.connection,
            algorithm_key="takeaway_uniform",
            item_id="preview-a",
            review_revision=1,
            image_resolver=self._resolver,
        )
        self.assertEqual(result.content_type, "image/jpeg")
        self.assertLessEqual(len(result.body), preview_resolver.MAX_OUTPUT_BYTES)
        self.assertEqual(result.headers["Cache-Control"], "private, no-store")
        with Image.open(__import__("io").BytesIO(result.body)) as image:
            self.assertLessEqual(max(image.size), 1600)
            self.assertNotIn("exif", image.info)

    def test_mutated_image_digest_fails_closed(self) -> None:
        Image.new("RGB", (2200, 1100), "red").save(self.path, quality=90)
        with self.assertRaisesRegex(preview_resolver.PreviewResolutionError, "changed") as ctx:
            preview_resolver.resolve_review_preview(
                self.connection,
                algorithm_key="takeaway_uniform",
                item_id="preview-a",
                review_revision=1,
                image_resolver=self._resolver,
            )
        self.assertEqual(ctx.exception.code, "asset_digest_mismatch")

    def test_wrong_algorithm_or_revision_does_not_fall_back_to_current(self) -> None:
        with self.assertRaises(preview_resolver.PreviewResolutionError):
            preview_resolver.resolve_review_preview(
                self.connection,
                algorithm_key="new_world_workwear",
                item_id="preview-a",
                review_revision=1,
                image_resolver=self._resolver,
            )


if __name__ == "__main__":
    unittest.main()
