#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from tools.algorithm_platform import evidence_ledger
from tools.sample_review import regression_store, server


TOKENS = {
    "DATAMAX_EXPORT_TOKEN": "review-export-token-value-00000001",
    "DATAMAX_CAPTURE_EXPORT_TOKEN": "capture-export-token-value-000001",
    "DATAMAX_REVIEW_TOKEN": "review-queue-token-value-000000001",
    "TRAINING_ASSET_TOKEN": "training-token-value-00000000001",
    "DATAMAX_LINEAGE_EXPORT_TOKEN": "lineage-export-token-value-0000001",
    "DATAMAX_VALIDATION_EXPORT_TOKEN": "validation-export-token-value-0001",
    "DATAMAX_CURSOR_SIGNING_KEY": "cursor-signing-key-value-at-least-32-bytes",
}


class InternalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {
            "ROOT": server.ROOT,
            "STATIC_ROOT": server.STATIC_ROOT,
            "DATA_ROOT": server.DATA_ROOT,
            "IMAGE_ROOT": server.IMAGE_ROOT,
            "CACHE_ROOT": server.CACHE_ROOT,
            "DATABASE": server.DATABASE,
            "MANIFEST": server.MANIFEST,
        }
        server.ROOT = root
        server.STATIC_ROOT = root / "static"
        server.DATA_ROOT = root / "data"
        server.IMAGE_ROOT = server.DATA_ROOT / "images"
        server.CACHE_ROOT = server.DATA_ROOT / "cache"
        server.DATABASE = server.DATA_ROOT / "review.sqlite3"
        server.MANIFEST = server.DATA_ROOT / "manifest.json"
        server.IMAGE_ROOT.mkdir(parents=True)
        server.CACHE_ROOT.mkdir(parents=True)
        server.STATIC_ROOT.mkdir(parents=True)
        server.MANIFEST.write_text("[]", encoding="utf-8")
        self.old_env = {name: os.environ.get(name) for name in TOKENS}
        os.environ.update(TOKENS)
        server.initialize_database()
        self._insert_item("sample-1", "takeaway", "pending")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ReviewHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        for name, value in self.old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for name, value in self.previous.items():
            setattr(server, name, value)
        self.temporary.cleanup()

    def _insert_item(self, item_id: str, source_kind: str, decision: str) -> None:
        relative = Path("fixtures") / f"{item_id}.jpg"
        path = server.IMAGE_ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (96, 64), "navy").save(path, format="JPEG")
        digest = server.hashlib.sha256(path.read_bytes()).hexdigest()
        with server.connect() as connection:
            connection.execute(
                """INSERT INTO items(
                   id,group_name,display_index,filename,image_path,source_image,split_name,sha256,
                   decision,notes,updated_at,ingest_key,source_kind,source_device,source_mtime,file_size)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item_id, "fixture", 1, path.name, relative.as_posix(), "fixture", "", digest,
                    decision, "", server.utc_now(), f"fixture:{item_id}", source_kind, "61672", 1,
                    path.stat().st_size,
                ),
            )

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers.items())
        except HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers.items())

    def json_request(self, *args, **kwargs) -> tuple[int, dict]:
        status, body, _ = self.request(*args, **kwargs)
        return status, json.loads(body)

    def test_scoped_tokens_cannot_cross_internal_surfaces(self) -> None:
        status, payload = self.json_request(
            "GET", "/api/internal/datamax/v1/algorithms", token=TOKENS["DATAMAX_EXPORT_TOKEN"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["algorithms"]), 6)
        takeaway = next(
            item for item in payload["algorithms"]
            if item["algorithm_key"] == "takeaway_uniform"
        )
        self.assertEqual(
            takeaway["publication_policy_ref"],
            "publication-policy:review-small-v1",
        )
        self.assertRegex(takeaway["updated_at"], r"^2026-08-13T")
        self.assertRegex(
            takeaway["publication_policy_content_sha256"], r"^[a-f0-9]{64}$",
        )
        status, _ = self.json_request(
            "GET", "/api/internal/datamax/v1/algorithms", token=TOKENS["DATAMAX_REVIEW_TOKEN"]
        )
        self.assertEqual(status, 401)

    def test_lineage_snapshot_is_frozen_and_credential_scoped(self) -> None:
        record = {
            "schema_version": "ai-bot-lineage-record.v1",
            "record_id": "profile:takeaway:v1",
            "algorithm_key": "takeaway_uniform",
            "payload": {
                "kind": "algorithm_profile", "display_name": "Takeaway uniform",
                "task_type": "object_detection", "profile_ref": "task:takeaway:v1",
                "profile_digest": "a" * 64, "taxonomy_version_ref": "taxonomy:takeaway:v1",
                "taxonomy_digest": "b" * 64, "annotation_contract": "bbox.v1",
                "class_mapping_digest": "c" * 64, "current_policy_ref": "publication:review:v1",
                "current_policy_digest": "d" * 64,
            },
            "recorded_at": "2026-08-16T00:00:00Z",
        }
        with server.connect() as connection:
            evidence_ledger.append_record(connection, "lineage", record, "lineage-1")
        path = "/api/internal/datamax/v1/evidence/lineage/algorithms/takeaway_uniform/snapshots"
        status, snapshot = self.json_request(
            "POST", path, token=TOKENS["DATAMAX_LINEAGE_EXPORT_TOKEN"], body={},
        )
        self.assertEqual(status, 201)
        status, _ = self.json_request(
            "POST", path, token=TOKENS["DATAMAX_VALIDATION_EXPORT_TOKEN"], body={},
        )
        self.assertEqual(status, 401)
        status, page = self.json_request(
            "GET", f"{path}/{snapshot['snapshot_id']}/records?limit=1",
            token=TOKENS["DATAMAX_LINEAGE_EXPORT_TOKEN"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["membership_digest"], snapshot["membership_digest"])
        self.assertEqual(page["items"][0]["record"], record)
        self.assertEqual(page["next_cursor"], "")
        status, _ = self.json_request(
            "GET", "/api/internal/datamax/v1/algorithms/takeaway_uniform/review-queue",
            token=TOKENS["DATAMAX_EXPORT_TOKEN"],
        )
        self.assertEqual(status, 401)

    def test_live_review_snapshot_and_training_original_are_revision_bound(self) -> None:
        status, queue = self.json_request(
            "GET", "/api/internal/datamax/v1/algorithms/takeaway_uniform/review-queue",
            token=TOKENS["DATAMAX_REVIEW_TOKEN"],
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["item_id"] for item in queue["items"]], ["sample-1"])
        self.assertEqual(queue["items"][0]["state"], "pending")
        self.assertRegex(queue["items"][0]["captured_at"], r"Z$")
        self.assertNotIn("imageUrl", queue["items"][0])
        self.assertNotIn("sourceDevice", queue["items"][0])

        status, detail = self.json_request(
            "GET",
            "/api/internal/datamax/v1/algorithms/takeaway_uniform/review-queue/sample-1/revisions/0",
            token=TOKENS["DATAMAX_REVIEW_TOKEN"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["taxonomy_version_ref"], "takeaway-uniform-taxonomy-v1")
        self.assertEqual(detail["annotation_contract_id"], "bbox.v1")
        self.assertEqual(detail["human_truth"], {
            "decision": "pending", "label_keys": [], "tag_keys": [], "boxes": [],
        })
        self.assertEqual(detail["label_definitions"][0]["label_key"], "courier")
        self.assertEqual(detail["tag_definitions"][0]["tag_key"], "scene.outdoor")

        status, preview, preview_headers = self.request(
            "GET",
            "/api/internal/datamax/v1/algorithms/takeaway_uniform/review-queue/sample-1/revisions/0/preview",
            token=TOKENS["DATAMAX_REVIEW_TOKEN"],
        )
        self.assertEqual(status, 200)
        self.assertTrue(preview.startswith(b"\xff\xd8\xff"))
        self.assertEqual(preview_headers["Cache-Control"], "private, no-store")

        command_headers = {"Idempotency-Key": "review-command-1"}
        command_body = {
            "taxonomy_version_ref": "takeaway-uniform-taxonomy-v1",
            "decision": "positive",
            "annotations": [{"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.7, "label": "takeaway"}],
            "label_keys": ["courier"],
            "tag_keys": [],
            "expected_review_revision": 0,
        }
        status, result = self.json_request(
            "PUT", "/api/internal/datamax/v1/algorithms/takeaway_uniform/review-queue/sample-1",
            token=TOKENS["DATAMAX_REVIEW_TOKEN"], body=command_body, headers=command_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["review_revision"], 1)
        self.assertEqual(result["status"], "pending_publication")
        self.assertEqual(result["human_decision"], "positive")
        self.assertRegex(result["updated_at"], r"Z$")
        status, replay = self.json_request(
            "PUT", "/api/internal/datamax/v1/algorithms/takeaway_uniform/review-queue/sample-1",
            token=TOKENS["DATAMAX_REVIEW_TOKEN"], body=command_body, headers=command_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(replay, result)

        status, detail = self.json_request(
            "GET",
            "/api/internal/datamax/v1/algorithms/takeaway_uniform/review-queue/sample-1/revisions/1",
            token=TOKENS["DATAMAX_REVIEW_TOKEN"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["human_truth"]["decision"], "positive")
        self.assertEqual(detail["human_truth"]["label_keys"], ["courier"])
        self.assertEqual(detail["human_truth"]["boxes"][0]["label_key"], "courier")

        stale_body = {**command_body, "expected_review_revision": 0}
        status, stale = self.json_request(
            "PUT", "/api/internal/datamax/v1/algorithms/takeaway_uniform/review-queue/sample-1",
            token=TOKENS["DATAMAX_REVIEW_TOKEN"], body=stale_body,
            headers={"Idempotency-Key": "review-command-stale"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(stale["code"], "REVIEW_REVISION_STALE")

        invalid_taxonomy = {**command_body, "taxonomy_version_ref": "taxonomy:other"}
        status, _ = self.json_request(
            "PUT", "/api/internal/datamax/v1/algorithms/takeaway_uniform/review-queue/sample-1",
            token=TOKENS["DATAMAX_REVIEW_TOKEN"], body=invalid_taxonomy,
            headers={"Idempotency-Key": "review-command-taxonomy"},
        )
        self.assertEqual(status, 400)

        status, original, original_headers = self.request(
            "GET",
            "/api/internal/training-assets/v1/algorithms/takeaway_uniform/items/sample-1/revisions/1/original",
            token=TOKENS["TRAINING_ASSET_TOKEN"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(server.hashlib.sha256(original).hexdigest(), original_headers["X-Content-SHA256"])
        status, _, _ = self.request(
            "GET",
            "/api/internal/training-assets/v1/algorithms/takeaway_uniform/items/sample-1/revisions/1/original",
            token=TOKENS["DATAMAX_EXPORT_TOKEN"],
        )
        self.assertEqual(status, 401)

        status, snapshot = self.json_request(
            "POST", "/api/internal/datamax/v1/algorithms/takeaway_uniform/publication-snapshots",
            token=TOKENS["DATAMAX_EXPORT_TOKEN"], body={"lease_owner": "connector-test"},
        )
        self.assertEqual(status, 201)
        snapshot_id = snapshot["snapshot_id"]
        status, page = self.json_request(
            "GET",
            f"/api/internal/datamax/v1/algorithms/takeaway_uniform/publication-snapshots/{snapshot_id}/review-facts",
            token=TOKENS["DATAMAX_EXPORT_TOKEN"], headers={"X-Lease-Owner": "connector-test"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["items"][0]["review_revision"], 1)
        receipt = {"source_version_id": "source-version-1", "source_content_digest": "a" * 64}
        status, ack = self.json_request(
            "POST",
            f"/api/internal/datamax/v1/algorithms/takeaway_uniform/publication-snapshots/{snapshot_id}/acknowledge",
            token=TOKENS["DATAMAX_EXPORT_TOKEN"], body=receipt,
            headers={"Idempotency-Key": "source-commit-1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(ack["status"], "acknowledged")

    def test_capture_and_regression_exports_are_distinct(self) -> None:
        status, capture = self.json_request(
            "POST", "/api/internal/datamax/v1/captures/publication-snapshots",
            token=TOKENS["DATAMAX_CAPTURE_EXPORT_TOKEN"], body={"lease_owner": "capture-test"},
        )
        self.assertEqual(status, 201)
        status, page = self.json_request(
            "GET", f"/api/internal/datamax/v1/captures/publication-snapshots/{capture['snapshot_id']}/items",
            token=TOKENS["DATAMAX_CAPTURE_EXPORT_TOKEN"], headers={"X-Lease-Owner": "capture-test"},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("human_truth", page["items"][0])
        self.assertNotIn("eligibility", page["items"][0])

        command_body = {
            "taxonomy_version_ref": "takeaway-uniform-taxonomy-v1",
            "decision": "positive",
            "annotations": [{"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.7, "label": "takeaway"}],
            "label_keys": ["courier"], "tag_keys": [], "expected_review_revision": 0,
        }
        status, result = self.json_request(
            "PUT", "/api/internal/datamax/v1/algorithms/takeaway_uniform/review-queue/sample-1",
            token=TOKENS["DATAMAX_REVIEW_TOKEN"], body=command_body,
            headers={"Idempotency-Key": "regression-review-1"},
        )
        self.assertEqual(status, 200)
        with server.connect() as connection:
            selection = regression_store.create_selection(
                connection, algorithm_key="takeaway_uniform", idempotency_key="selection-1",
                items=[{
                    "item_id": "sample-1", "review_revision": 1,
                    "review_fact_digest": result["fact_digest"],
                    "regression_roles": ["hard_positive"],
                }],
            )
        status, exported = self.json_request(
            "GET",
            f"/api/internal/datamax/v1/algorithms/takeaway_uniform/regression-selections/{selection['selection_id']}",
            token=TOKENS["DATAMAX_EXPORT_TOKEN"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(exported["items"][0]["review_fact"]["eligibility"]["regression_roles"], ["hard_positive"])
        self.assertNotEqual(
            exported["items"][0]["review_fact"]["content_sha256"],
            exported["items"][0]["base_review_fact_digest"],
        )


if __name__ == "__main__":
    unittest.main()
