#!/usr/bin/env python3

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from tools.sample_review.reporting_manager import (
    ENABLE_CONTENT,
    ReportingManager,
    successful_item_ids,
    reported_fingerprints,
)
from tools.sample_review.automatic_reporting import cutoff_by_algorithm, enable
from tools.sample_review import replay_reviewed_box_reports as replay


class FakeReplay:
    _ACTIVE_STATE_DIR = None

    @staticmethod
    def load_identifiers(_path):
        return {
            "takeaway": {
                "source_geid": 104,
                "report_geid": 1104,
                "report_gcid": 282624,
                "class_name": "AI二次复核-外卖人员",
            },
            "workwear": {
                "source_geid": 103,
                "report_geid": 1103,
                "report_gcid": 282368,
                "class_name": "AI二次复核-新世界工服",
            },
        }

    @staticmethod
    def prepare(args, exclude_item_ids=None, exclude_image_sha256=None):
        items = [
            {
                "item_id": item_id,
                "source_kind": args.source_kind,
                "geid": 104 if args.source_kind == "takeaway" else 103,
                "report_geid": 1104 if args.source_kind == "takeaway" else 1103,
                "report_gcid": 282624 if args.source_kind == "takeaway" else 282368,
            }
            for item_id in ("old", "new")
            if item_id not in (exclude_item_ids or set())
        ]
        summary = {
            "source_positive_rows": 2,
            "matched_rows": len(items),
            "deduplicated_items": len(items),
            "already_reported": 2 - len(items),
            "capture_time_mismatch": 0,
            "by_device_algorithm": {"61672|m104": len(items)},
            "source_kind_filter": args.source_kind,
            "selection": "ai-positive",
            "prepared_at": "2026-08-12T00:00:00+00:00",
        }
        (args.state_dir / "manifest.json").write_text(
            json.dumps({"endpoint": args.endpoint, "summary": summary, "items": items}),
            encoding="utf-8",
        )
        return summary

    @staticmethod
    def _append(args, item, phase):
        assert args.enable_file.read_text(encoding="utf-8").strip() == ENABLE_CONTENT
        entry = {
            "phase": phase,
            "item_id": item["item_id"],
            "status": "success",
            "http_status": 200,
            "application_code": 200,
            "report_geid": item["report_geid"],
            "device": "61672",
        }
        with (args.state_dir / "ledger.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")
        return entry

    @classmethod
    def canary(cls, args):
        manifest = json.loads((args.state_dir / "manifest.json").read_text(encoding="utf-8"))
        return cls._append(args, manifest["items"][-1], "canary")

    @classmethod
    def send(cls, args):
        cache = args.state_dir / "cache"
        cache.mkdir(exist_ok=True)
        (cache / "temporary.jpg").write_bytes(b"temporary")
        manifest = json.loads((args.state_dir / "manifest.json").read_text(encoding="utf-8"))
        completed = {
            json.loads(line)["item_id"]
            for line in (args.state_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        }
        pending = [item for item in manifest["items"] if item["item_id"] not in completed]
        for item in pending:
            cls._append(args, item, "batch")
        return {"requested": len(pending), "success": len(pending), "failed": 0, "unknown": 0}


class ReportingManagerTests(unittest.TestCase):
    def test_automatic_reporting_timer_runs_every_minute(self):
        timer = (
            Path(__file__).resolve().parent
            / "ai-bot-sample-review-auto-report.timer"
        ).read_text(encoding="utf-8")
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("RandomizedDelaySec=5s", timer)
        self.assertNotIn("OnUnitActiveSec=5min", timer)

    def test_checked_in_identifier_config_has_two_isolated_algorithms(self):
        config = Path(__file__).resolve().parents[2] / "config" / "report-identifiers.json"
        identifiers = replay.load_identifiers(config)
        self.assertEqual(identifiers["takeaway"]["report_geid"], 1104)
        self.assertEqual(identifiers["takeaway"]["report_gcid"], 1104 << 8)
        self.assertEqual(identifiers["workwear"]["report_geid"], 1103)
        self.assertEqual(identifiers["workwear"]["report_gcid"], 1103 << 8)

    def test_ai_positive_selection_excludes_human_negative(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "review.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE items (
                    id TEXT, filename TEXT, source_image TEXT, source_kind TEXT,
                    source_device TEXT, source_mtime INTEGER, sha256 TEXT,
                    object_key TEXT, object_sha256 TEXT, storage_backend TEXT,
                    decision TEXT, annotations TEXT, ai_decision TEXT,
                    ai_annotations TEXT, ai_model TEXT, ai_confidence REAL,
                    ai_labeled_at TEXT, human_reviewed INTEGER,
                    human_reviewed_at TEXT, updated_at TEXT
                )
                """
            )
            base = (
                "ch1_m104_1.jpg", "", "takeaway", "61672", 1, "sha", "key", "obj",
                "oss", "pending", "[]", "positive",
                '[{"x":0.1,"y":0.1,"w":0.2,"h":0.3,"label":"takeaway"}]',
                "MiniMax-M3", 0.9, "2026-08-12T00:00:00+00:00",
            )
            connection.execute(
                "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("keep", *base, 0, "", "now"),
            )
            connection.execute(
                "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("reject", *base, 1, "now", "now"),
            )
            connection.execute("UPDATE items SET decision='negative' WHERE id='reject'")
            connection.commit()
            connection.close()
            rows = replay.read_positive_rows(database, "takeaway", "ai-positive")
            self.assertEqual([row["id"] for row in rows], ["keep"])

    def test_ai_annotation_payload_only_keeps_matching_label(self):
        item = {
            "source_kind": "takeaway",
            "report_gcid": 282624,
            "report_class_name": "AI二次复核-外卖人员",
            "ai_confidence": 0.9,
            "ai_annotations": json.dumps(
                [
                    {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "label": "takeaway"},
                    {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2, "label": "negative_person"},
                ]
            ),
        }
        boxes = replay.normalized_ai_annotations(item)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["gcid"], 282624)
        self.assertEqual(boxes[0]["class_name"], "AI二次复核-外卖人员")

    def test_successful_ids_are_scoped_to_new_report_geid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "runs" / "old"
            run.mkdir(parents=True)
            rows = [
                {"item_id": "direct-box", "status": "success", "report_geid": 104},
                {"item_id": "secondary", "status": "success", "report_geid": 1104},
                {"item_id": "failed", "status": "failed", "report_geid": 1104},
            ]
            (run / "ledger.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            self.assertEqual(successful_item_ids(root, 1104), {"secondary"})

    def test_terminal_fingerprints_deduplicate_success_and_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "runs" / "old"
            run.mkdir(parents=True)
            rows = [
                {"item_id": "sent", "image_sha256": "sha-a", "status": "success", "report_geid": 1104},
                {"item_id": "uncertain", "image_sha256": "sha-b", "status": "unknown", "report_geid": 1104},
                {"item_id": "retryable", "image_sha256": "sha-c", "status": "failed", "report_geid": 1104},
            ]
            (run / "ledger.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            item_ids, hashes = reported_fingerprints(root, 1104)
            self.assertEqual(item_ids, {"sent", "uncertain"})
            self.assertEqual(hashes, {"sha-a", "sha-b"})

    def test_automatic_enable_freezes_per_algorithm_history_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "data" / "review.sqlite3"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE items (source_kind TEXT, source_mtime INTEGER)")
            connection.executemany(
                "INSERT INTO items VALUES (?, ?)",
                [("takeaway", 100), ("takeaway", 120), ("workwear", 90)],
            )
            connection.commit()
            connection.close()
            manager = ReportingManager(root, replay_module=FakeReplay)
            state = enable(manager)
            self.assertTrue(state["enabled"])
            self.assertFalse(state["paused"])
            self.assertEqual(state["cutoffByAlgorithm"], {"takeaway": 120, "workwear": 90})
            self.assertEqual(cutoff_by_algorithm(database), state["cutoffByAlgorithm"])

    def test_prepare_only_allows_takeaway_and_workwear_and_excludes_prior_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = ReportingManager(root, replay_module=FakeReplay)
            old = root / "report-replay" / "runs" / "previous"
            old.mkdir(parents=True)
            (old / "ledger.jsonl").write_text(
                json.dumps({"item_id": "old", "status": "success", "report_geid": 1104}) + "\n",
                encoding="utf-8",
            )
            run = manager.prepare("takeaway")
            self.assertEqual(run["algorithm"], "takeaway")
            self.assertEqual(run["manifestItems"], 1)
            self.assertEqual(run["summary"]["already_reported"], 1)
            with self.assertRaises(ValueError):
                manager.prepare("door")

    def test_canary_and_batch_use_temporary_enable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ReportingManager(Path(directory), replay_module=FakeReplay)
            prepared = manager.prepare("workwear")
            run_id = prepared["runId"]
            canary = manager.canary(run_id)
            self.assertTrue(canary["canarySuccess"])
            run_dir = manager._run_dir(run_id)
            self.assertFalse((run_dir / "ENABLE_SEND").exists())
            sending = manager.start_send(run_id, canary["confirmationPhrase"])
            self.assertEqual(sending["state"], "sending")
            for _ in range(100):
                status = manager.status(run_id)
                if status["activeJob"] is None:
                    break
                time.sleep(0.01)
            completed = manager.status(run_id)["selectedRun"]
            self.assertEqual(completed["state"], "completed")
            self.assertEqual(completed["success"], 2)
            self.assertEqual(completed["remaining"], 0)
            self.assertFalse((run_dir / "ENABLE_SEND").exists())
            self.assertFalse((run_dir / "cache").exists())

    def test_cleanup_removes_stale_enable_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ReportingManager(Path(directory), replay_module=FakeReplay)
            marker = manager.report_root / "runs" / "stale" / "ENABLE_SEND"
            marker.parent.mkdir(parents=True)
            marker.write_text(ENABLE_CONTENT, encoding="utf-8")
            self.assertEqual(manager.cleanup_stale_enable_files(), 1)
            self.assertFalse(marker.exists())

    def test_legacy_completed_run_is_inferred_without_status_file(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ReportingManager(Path(directory), replay_module=FakeReplay)
            run = manager.runs_root / "legacy-takeaway"
            run.mkdir(parents=True)
            items = [
                {
                    "item_id": "done",
                    "ai_annotations": '[{"x":0.1},{"x":0.2}]',
                }
            ]
            (run / "manifest.json").write_text(
                json.dumps(
                    {
                        "summary": {
                            "source_kind_filter": "takeaway",
                            "prepared_at": "2026-08-12T00:00:00+00:00",
                        },
                        "items": items,
                    }
                ),
                encoding="utf-8",
            )
            (run / "ledger.jsonl").write_text(
                json.dumps(
                    {
                        "item_id": "done",
                        "phase": "canary",
                        "status": "success",
                        "report_geid": 1104,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            public = manager.status(run.name)["selectedRun"]
            self.assertEqual(public["state"], "completed")
            self.assertEqual(public["summary"]["ai_boxes"], 2)


if __name__ == "__main__":
    unittest.main()
