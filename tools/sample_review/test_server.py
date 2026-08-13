#!/usr/bin/env python3

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.sample_review import oss_backend
from tools.sample_review.classify_recent_pending import parse_result
from tools.sample_review.seed_box_review import (
    Prediction,
    extract_embedded_blue_annotations,
    prediction_annotation,
    select_prediction,
)
from tools.sample_review.server import (
    box_review_rows,
    confirm_ai_labels,
    parse_minimax_boxes,
    validate_reporting_payload,
)


ROOT = Path(__file__).resolve().parent


class ReviewDataTests(unittest.TestCase):
    def test_review_mutations_use_revision_ledger_and_browser_idempotency(self) -> None:
        server_source = (ROOT / "server.py").read_text(encoding="utf-8")
        app_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("review_revisions.migrate(connection", server_source)
        self.assertIn("review_revisions.record_review_command(", server_source)
        self.assertNotIn("SET decision = ?, notes = ?, annotations = ?, updated_at = ?", server_source)
        self.assertEqual(app_source.count("crypto.randomUUID()"), 1)
        self.assertGreaterEqual(app_source.count('"Idempotency-Key": reviewMutationKey('), 2)
        self.assertIn("pendingReviewMutation.fingerprint", app_source)
        self.assertIn("expectedRevision: item.reviewRevision || 0", app_source)

    def test_reporting_payload_only_allows_takeaway_and_workwear(self) -> None:
        self.assertEqual(
            validate_reporting_payload("prepare", {"algorithm": "takeaway"}),
            {"algorithm": "takeaway"},
        )
        self.assertEqual(
            validate_reporting_payload("prepare", {"algorithm": "workwear"}),
            {"algorithm": "workwear"},
        )
        with self.assertRaises(ValueError):
            validate_reporting_payload("prepare", {"algorithm": "door"})

    def test_reporting_send_requires_run_and_confirmation(self) -> None:
        self.assertEqual(
            validate_reporting_payload(
                "send", {"runId": "run-1", "confirmation": "发送 外卖服 9 条"}
            ),
            {"runId": "run-1", "confirmation": "发送 外卖服 9 条"},
        )
        with self.assertRaises(ValueError):
            validate_reporting_payload("send", {"runId": "run-1"})

    def test_reporting_tab_contract_is_present(self) -> None:
        static = Path(__file__).resolve().parent / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-queue-mode="reporting"', html)
        self.assertIn('id="reportingPanel"', html)
        self.assertIn('`api/reporting/${action}`', app)
        self.assertIn('"prepare"', app)
        self.assertIn('"canary"', app)
        self.assertIn('"send"', app)
        self.assertIn('reportConfirmation', html)
        self.assertIn('历史记录永久隔离', html)
        self.assertIn('自动发送已启用', app)

    def test_box_review_card_buttons_are_actionable(self) -> None:
        app_path = Path(__file__).resolve().parent / "static" / "app.js"
        app = app_path.read_text(encoding="utf-8")
        self.assertNotIn('if (isBoxReview()) return;', app)
        self.assertIn('payload.annotations = candidateAnnotations', app)
        self.assertIn('elements.annotateHint.textContent = "当前没有候选框，请手动画框后保存"', app)
        self.assertIn('node.querySelector(".decision-control").hidden = false;', app)

    def test_box_seed_prefers_embedded_blue_rectangle(self) -> None:
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("opencv is not installed")
        with tempfile.TemporaryDirectory() as directory:
            image = np.full((200, 300, 3), 220, dtype=np.uint8)
            cv2.rectangle(image, (60, 40), (150, 180), (255, 0, 0), 3)
            cv2.putText(image, "[1]", (62, 57), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1)
            cv2.rectangle(image, (200, 50), (260, 120), (0, 0, 255), 3)
            path = Path(directory) / "sample.jpg"
            cv2.imwrite(str(path), image)
            annotations = extract_embedded_blue_annotations(path)
        self.assertEqual(len(annotations), 1)
        self.assertEqual(annotations[0]["source"], "embedded-blue")
        self.assertAlmostEqual(annotations[0]["x"], 0.2, delta=0.02)
        self.assertAlmostEqual(annotations[0]["y"], 0.2, delta=0.02)
        self.assertAlmostEqual(annotations[0]["w"], 0.31, delta=0.03)
        self.assertAlmostEqual(annotations[0]["h"], 0.72, delta=0.03)

    def test_box_seed_prefers_strict_person_and_converts_center_to_top_left(self) -> None:
        loose = Prediction(0.5, 0.5, 0.99, 0.99, 0.99)
        strict = Prediction(0.6, 0.4, 0.2, 0.4, 0.8)
        selected, reason = select_prediction([loose, strict])
        self.assertEqual(selected, strict)
        self.assertEqual(reason, "strict-person")
        self.assertEqual(
            prediction_annotation(selected),
            [{"x": 0.5, "y": 0.2, "w": 0.2, "h": 0.4, "label": "takeaway", "confidence": 0.8}],
        )

    def test_box_seed_uses_highest_confidence_fallback_for_human_review(self) -> None:
        edge = Prediction(0.1, 0.5, 0.2, 0.8, 0.91)
        selected, reason = select_prediction([edge])
        self.assertEqual(selected, edge)
        self.assertEqual(reason, "fallback-highest-confidence")

    def test_box_review_queue_selects_only_unboxed_human_positives_and_deduplicates(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE items (
                id TEXT PRIMARY KEY,
                group_name TEXT NOT NULL,
                display_index INTEGER NOT NULL,
                object_sha256 TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                source_mtime INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                human_reviewed_at TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                human_reviewed INTEGER NOT NULL,
                decision TEXT NOT NULL,
                annotations TEXT NOT NULL,
                ai_annotations TEXT NOT NULL
            )
            """
        )
        rows = [
            ("old", "takeaway", 1, "same", "", 10, "a", "a", "takeaway", 1, "positive", "[]", "[]"),
            ("new", "takeaway", 2, "same", "", 20, "b", "b", "takeaway", 1, "positive", "[]", '[{"x":0.1}]'),
            ("saved", "takeaway", 3, "saved", "", 30, "c", "c", "takeaway", 1, "positive", '[{"x":0.1}]', "[]"),
            ("negative", "takeaway", 4, "negative", "", 40, "d", "d", "takeaway", 1, "negative", "[]", "[]"),
            ("pending", "takeaway", 5, "pending", "", 50, "e", "", "takeaway", 0, "pending", "[]", "[]"),
            ("workwear", "workwear", 6, "workwear", "", 60, "f", "f", "workwear", 1, "positive", "[]", "[]"),
        ]
        connection.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.assertEqual([row["id"] for row in box_review_rows(connection)], ["workwear", "new"])
        connection.execute("UPDATE items SET annotations = '[{\"x\":0.1}]' WHERE id = 'new'")
        self.assertEqual([row["id"] for row in box_review_rows(connection)], ["workwear"])
        connection.close()

    def test_oss_object_keys_are_content_addressed(self) -> None:
        digest = "a" * 64
        self.assertEqual(
            oss_backend.object_key_for_sha256(digest),
            f"{oss_backend.OSS_PREFIX}/objects/aa/{digest}.jpg",
        )

    def test_oss_cache_path_does_not_expose_object_hierarchy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = oss_backend.cache_path(Path(directory), "ai-bot-samples/objects/aa/example.jpg")
            self.assertEqual(path.parent.parent, Path(directory))
            self.assertEqual(path.suffix, ".jpg")

    def test_manifest_has_unique_ids_and_valid_decisions(self) -> None:
        manifest_path = Path(__file__).resolve().parents[2] / ".runtime" / "sample-review-build" / "manifest.json"
        if not manifest_path.exists():
            self.skipTest("review dataset has not been built")
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(records), len({record["id"] for record in records}))
        self.assertTrue(records)
        self.assertTrue(all(record["decision"] in {"pending", "positive", "negative", "discard"} for record in records))
        self.assertTrue(all((manifest_path.parent / "images" / record["image"]).is_file() for record in records))

    def test_minimax_boxes_are_normalized_and_incomplete_targets_are_ignored(self) -> None:
        response = json.dumps(
            {
                "boxes": [
                    {
                        "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4,
                        "confidence": 0.91, "complete": True,
                    },
                    {
                        "x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2,
                        "confidence": 0.99, "complete": False,
                    },
                ]
            }
        )
        self.assertEqual(
            parse_minimax_boxes(response, "takeaway"),
            [
                {
                    "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4,
                    "label": "takeaway", "confidence": 0.91,
                }
            ],
        )

    def test_minimax_boxes_reject_out_of_bounds_coordinates(self) -> None:
        response = json.dumps(
            {
                "boxes": [
                    {
                        "x": 0.9, "y": 0.1, "w": 0.2, "h": 0.5,
                        "confidence": 0.9, "complete": True,
                    }
                ]
            }
        )
        self.assertEqual(parse_minimax_boxes(response, "workwear"), [])

    def test_preliminary_person_positive_requires_box(self) -> None:
        response = json.dumps(
            {
                "decision": "positive",
                "confidence": 0.96,
                "usable": True,
                "reason": "courier",
                "boxes": [],
            }
        )
        self.assertEqual(parse_result(response, "takeaway")["decision"], "pending")

    def test_preliminary_door_label_does_not_require_person_box(self) -> None:
        response = json.dumps(
            {
                "decision": "positive",
                "confidence": 0.96,
                "usable": True,
                "reason": "right-side gap is visible",
                "boxes": [],
            }
        )
        result = parse_result(response, "door")
        self.assertEqual(result["decision"], "positive")
        self.assertEqual(result["boxes"], [])

    def test_bulk_confirmation_accepts_only_snapshot_ai_labels(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE items (
                id TEXT PRIMARY KEY,
                decision TEXT NOT NULL,
                notes TEXT NOT NULL,
                annotations TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ai_decision TEXT NOT NULL,
                ai_notes TEXT NOT NULL,
                ai_annotations TEXT NOT NULL,
                human_reviewed INTEGER NOT NULL,
                human_reviewed_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO items VALUES (?, 'pending', '', '[]', '', ?, 'ai', ?, 0, '')",
            [
                ("positive", "positive", '[{"x":0.1,"y":0.1,"w":0.2,"h":0.6}]'),
                ("negative", "negative", "[]"),
                ("pending", "pending", "[]"),
            ],
        )
        result = confirm_ai_labels(
            connection, ["positive", "negative", "pending", "missing"], "2026-08-07T00:00:00+00:00"
        )
        self.assertEqual(result, {"reviewed": 2, "positive": 1, "negative": 1, "skipped": 2})
        positive = connection.execute("SELECT * FROM items WHERE id = 'positive'").fetchone()
        self.assertEqual(positive["decision"], "positive")
        self.assertEqual(positive["annotations"], '[{"x":0.1,"y":0.1,"w":0.2,"h":0.6}]')
        self.assertEqual(positive["human_reviewed"], 1)
        pending = connection.execute("SELECT * FROM items WHERE id = 'pending'").fetchone()
        self.assertEqual(pending["human_reviewed"], 0)
        connection.close()


if __name__ == "__main__":
    unittest.main()
