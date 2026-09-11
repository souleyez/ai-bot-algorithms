import json
import sqlite3
import unittest

from tools.sample_review import door_review


class DoorReviewTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE items (
            id TEXT PRIMARY KEY, source_device TEXT, source_kind TEXT,
            filename TEXT, image_path TEXT, source_mtime INTEGER, sha256 TEXT,
            human_reviewed INTEGER DEFAULT 0, decision TEXT DEFAULT 'pending')""")
        door_review.ensure_schema(self.db)
        for item_id, device, kind, filename in [
            ("a", "62821", "door-state", "ch3_m101_0001.jpg"),
            ("b", "62821", "door-state", "ch12_m101_0002.jpg"),
            ("c", "61672", "door-state", "ch2_m101_0001.jpg"),
            ("d", "62821", "workwear", "ch3_m103_0001.jpg"),
            ("e", "62821", "door-state", "s_ch3_m101_0001.jpg"),
            ("f", "62821", "door-state", "ch3_m1010_0001.jpg"),
        ]:
            self.db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,0,'pending')",
                            (item_id, device, kind, filename, item_id + ".jpg", 1789110000, item_id*64))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def command(self, **changes):
        return {"state":"open", "verdict":"uncertain", "notes":"", "expectedRevision":0, **changes}

    def test_scope_and_unlabelled_visibility(self):
        data = door_review.project_payload(self.db)
        self.assertEqual([r["id"] for r in data["items"]], ["a", "b"])
        self.assertEqual(data["channels"], [3, 12])
        self.assertEqual(data["counts"]["pending"], 2)

    def test_dynamic_channel_filter(self):
        self.assertEqual(len(door_review.project_payload(self.db, channel="12")["items"]), 1)
        self.assertEqual(door_review.project_payload(self.db, channel="14")["items"], [])

    def test_save_is_not_yolo_truth(self):
        door_review.record(self.db, "a", self.command(), "test-request-a")
        self.assertEqual(door_review.project_payload(self.db)["counts"]["reviewed"], 1)
        row = self.db.execute("SELECT * FROM items WHERE id='a'").fetchone()
        self.assertEqual(row["decision"], "pending")
        self.assertEqual(row["human_reviewed"], 0)

    def test_historical_audit_and_pending_counts(self):
        door_review.record(self.db, "a", self.command(), "test-request-a")
        door_review.record(self.db, "a", self.command(state="closed", expectedRevision=1), "test-request-b")
        data = door_review.project_payload(self.db, status="reviewed")
        self.assertEqual(data["counts"]["open"], 0)
        self.assertEqual(data["counts"]["closed"], 1)
        self.assertEqual(data["items"][0]["revision"], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM door_review_revisions").fetchone()[0], 2)

    def test_idempotent_retry(self):
        first = door_review.record(self.db, "a", self.command(), "test-request-a")
        second = door_review.record(self.db, "a", self.command(), "test-request-a")
        self.assertEqual(first["revision"], second["revision"])
        self.assertTrue(second["replayed"])

    def test_request_reuse_conflicts(self):
        door_review.record(self.db, "a", self.command(), "test-request-a")
        with self.assertRaises(door_review.Conflict):
            door_review.record(self.db, "a", self.command(state="closed"), "test-request-a")

    def test_stale_revision_conflicts(self):
        door_review.record(self.db, "a", self.command(), "test-request-a")
        with self.assertRaises(door_review.Conflict):
            door_review.record(self.db, "a", self.command(), "test-request-b")

    def test_other_sources_rejected(self):
        for item in ("c", "d", "e", "f", "missing"):
            with self.assertRaises(KeyError):
                door_review.record(self.db, item, self.command(), "test-request-" + item)

    def test_event_verdict_requires_evidence(self):
        with self.assertRaises(ValueError):
            door_review.record(self.db, "a", self.command(verdict="correct"), "test-request-a")
        result = door_review.record(self.db, "a", self.command(verdict="false_alarm", notes="相邻抓拍门状态相同，只有行人经过"), "test-request-b")
        self.assertTrue(result["saved"])

    def test_invalid_fields_rejected(self):
        for change in ({"state":"positive"}, {"verdict":"negative"}, {"expectedRevision":True}, {"notes":"x"*1001}):
            with self.assertRaises(ValueError):
                door_review.record(self.db, "a", self.command(**change), "test-request-a")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM door_review_revisions").fetchone()[0], 0)

    def test_filter_validation(self):
        for kwargs in ({"channel":"1 OR 1=1"}, {"status":"bad"}, {"after":"../bad"}):
            with self.assertRaises(ValueError):
                door_review.project_payload(self.db, **kwargs)

    def test_cursor_survives_reviewed_item_leaving_queue(self):
        door_review.record(self.db, "a", self.command(), "test-request-a")
        page = door_review.project_payload(self.db, after="a")
        self.assertEqual([row["id"] for row in page["items"]], ["b"])

    def test_capture_hash_retained(self):
        door_review.record(self.db, "a", self.command(), "test-request-a", "reviewer@example.test")
        row = self.db.execute("SELECT * FROM door_review_revisions").fetchone()
        self.assertEqual(row["captured_sha256"], "a"*64)
        self.assertEqual(json.loads(row["command_json"])["id"], "a")


if __name__ == "__main__":
    unittest.main()
