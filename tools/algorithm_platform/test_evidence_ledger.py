from __future__ import annotations

import json
import sqlite3
import unittest

from tools.algorithm_platform import evidence_ledger

D = "a" * 64


def lineage(record_id: str = "profile:takeaway:v1") -> dict:
    return {
        "schema_version": "ai-bot-lineage-record.v1",
        "record_id": record_id,
        "algorithm_key": "takeaway_uniform",
        "payload": {
            "kind": "algorithm_profile", "display_name": "Takeaway uniform", "task_type": "object_detection",
            "profile_ref": "task:takeaway:v1", "profile_digest": D,
            "taxonomy_version_ref": "taxonomy:takeaway:v1", "taxonomy_digest": "b" * 64,
            "annotation_contract": "bbox.v1", "class_mapping_digest": "c" * 64,
            "current_policy_ref": "publication:review-small:v1", "current_policy_digest": "d" * 64,
        },
        "recorded_at": "2026-08-16T00:00:00Z",
    }


def validation(record_id: str = "offline:takeaway:v1") -> dict:
    return {
        "schema_version": "ai-bot-validation-record.v1", "record_id": record_id,
        "algorithm_key": "takeaway_uniform", "recorded_at": "2026-08-16T01:00:00Z",
        "payload": {
            "kind": "offline_replay", "artifact_digest": D,
            "truth_core_dataset_version_id": "truth:v1", "regression_core_dataset_version_id": "regression:v1",
            "device_ref": "offline-runner:v1", "config_digest": "b" * 64,
            "metrics_digest": "c" * 64, "evidence_digest": "d" * 64,
            "evidence_at": "2026-08-16T01:00:00Z", "status": "passed",
        },
    }


def all_lineage_records() -> list[dict]:
    base = {"schema_version": "ai-bot-lineage-record.v1", "algorithm_key": "takeaway_uniform", "recorded_at": "2026-08-16T00:00:00Z"}
    return [
        lineage(),
        {**base, "record_id": "semantics:v1", "payload": {"kind": "visual_semantics_version", "semantics_bundle_ref": "semantics:v1", "semantics_bundle_digest": D, "core_dataset_version_id": "core:semantics:v1"}},
        {**base, "record_id": "secondary:v1", "payload": {
            "kind": "secondary_review_run", "truth_core_dataset_version_id": "core:truth:v1", "observation_manifest_digest": D,
            "task_type": "object_detection", "task_profile_ref": "task:v1", "task_profile_digest": D,
            "taxonomy_version_ref": "taxonomy:v1", "taxonomy_digest": D, "annotation_contract": "bbox.v1",
            "provider_ref": "provider:v1", "model_ref": "model:v1", "inference_policy_digest": D,
            "prompt_template_digest": D, "inference_config_digest": D, "response_schema_version": "prediction:v1",
            "retrieval_policy_digest": D, "retrieved_examples_digest": D, "policy_state": "shadow", "sample_count": 1,
            "primary_agreement_ppm": 500000, "secondary_agreement_ppm": 600000,
            "per_label_metrics": [{"label_key": "courier", "sample_count": 1, "primary_agreement_ppm": 500000, "secondary_agreement_ppm": 600000}],
            "per_error_cluster_metrics": [{"error_type": "false_positive", "primary_error_count": 1, "secondary_error_count": 0}],
            "latency_p95_ms": 1000, "cost_total_microunits": 50, "agreement_metrics_digest": D,
            "latency_summary_digest": D, "cost_summary_digest": D, "evaluation_digest": D,
        }},
        {**base, "record_id": "manifest:v1", "payload": {"kind": "training_manifest", "proposal_id": "proposal:v1", "proposal_revision": 2, "manifest_ref": "manifest:v1", "manifest_digest": D, "truth_core_dataset_version_id": "core:truth:v1", "regression_core_dataset_version_id": "core:regression:v1", "visual_semantics_core_dataset_version_id": "core:semantics:v1", "member_count": 1, "membership_digest": D, "proposal_state": "approved"}},
        {**base, "record_id": "training:v1", "payload": {"kind": "training_run", "run_ref": "training:v1", "manifest_digest": D, "truth_core_dataset_version_id": "core:truth:v1", "regression_core_dataset_version_id": "core:regression:v1", "parent_model_ref": "model:parent:v1", "training_config_digest": D, "metrics_digest": D, "status": "passed"}},
        {**base, "record_id": "artifact:v1", "payload": {"kind": "model_artifact", "artifact_ref": "artifact:v1", "artifact_digest": D, "artifact_format": "rknn", "size_bytes": 1, "chip_family": "rk3576", "converter_version": "2.3.2", "toolkit_version": "2.3.2", "toolchain_digest": D, "class_mapping_digest": D, "manifest_digest": D, "acceptance_state": "validated"}},
        {**base, "record_id": "candidate:v1", "payload": {"kind": "release_candidate", "candidate_ref": "candidate:v1", "artifact_digest": D, "target_chip": "rk3576", "target_runtime": "rknn:2.3.2", "compatibility_digest": D, "gate_digest": D, "status": "accepted"}},
    ]


def all_validation_records() -> list[dict]:
    base = {"schema_version": "ai-bot-validation-record.v1", "algorithm_key": "takeaway_uniform", "recorded_at": "2026-08-16T01:00:00Z"}
    common = {"artifact_digest": D, "truth_core_dataset_version_id": "core:truth:v1", "regression_core_dataset_version_id": "core:regression:v1", "device_ref": "device:61672", "config_digest": D, "evidence_digest": D, "evidence_at": "2026-08-16T01:00:00Z", "status": "passed"}
    return [
        validation(),
        {**base, "record_id": "rknn:v1", "payload": {"kind": "rknn_smoke", **common, "target_chip": "rk3576", "toolkit_digest": D}},
        {**base, "record_id": "canary:v1", "payload": {"kind": "canary_acceptance", **common, "target_chip": "rk3576"}},
        {**base, "record_id": "deploy:v1", "payload": {"kind": "deployment_acceptance", **common, "target_chip": "rk3576", "canary_evidence_digest": D}},
        {**base, "record_id": "rollback:v1", "payload": {"kind": "rollback_evidence", **{key: value for key, value in common.items() if key != "artifact_digest"}, "from_artifact_digest": D, "to_artifact_digest": "b" * 64, "status": "rolled_back"}},
        {**base, "record_id": "field:v1", "payload": {"kind": "field_observation", **common, "window_start": "2026-08-16T00:00:00Z", "window_end": "2026-08-16T01:00:00Z"}},
    ]


class EvidenceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        evidence_ledger.migrate(self.db)

    def tearDown(self):
        self.db.close()

    def test_append_is_schema_bound_idempotent_and_immutable(self):
        first = evidence_ledger.append_record(self.db, "lineage", lineage(), "lineage-1")
        replay = evidence_ledger.append_record(self.db, "lineage", lineage(), "lineage-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE visual_evidence_records SET canonical_record_json='{}'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("DELETE FROM visual_evidence_records")

    def test_schema_secret_and_idempotency_conflict_fail_closed(self):
        bad = lineage()
        bad["payload"]["unknown"] = True
        with self.assertRaises(evidence_ledger.EvidenceError):
            evidence_ledger.append_record(self.db, "lineage", bad, "bad-1")
        secret = lineage("profile:secret:v1")
        secret["payload"]["display_name"] = "Authorization: Bearer exposed"
        with self.assertRaisesRegex(evidence_ledger.EvidenceError, "secret"):
            evidence_ledger.append_record(self.db, "lineage", secret, "bad-2")
        evidence_ledger.append_record(self.db, "lineage", lineage(), "same-key")
        with self.assertRaisesRegex(evidence_ledger.EvidenceError, "conflicts"):
            evidence_ledger.append_record(self.db, "lineage", lineage("profile:takeaway:v2"), "same-key")

    def test_snapshot_membership_is_frozen_across_later_appends(self):
        evidence_ledger.append_record(self.db, "lineage", lineage(), "lineage-1")
        snapshot = evidence_ledger.create_snapshot(self.db, "lineage", "takeaway_uniform")
        evidence_ledger.append_record(self.db, "lineage", lineage("profile:takeaway:v2"), "lineage-2")
        page = evidence_ledger.page_snapshot(self.db, snapshot["snapshot_id"], 0, 10)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["record"]["record_id"], "profile:takeaway:v1")
        self.assertIsNone(page["next_offset"])

    def test_validation_uses_its_distinct_schema_and_stream(self):
        result = evidence_ledger.append_record(self.db, "validation", validation(), "validation-1")
        self.assertEqual(len(result["record_digest"]), 64)
        with self.assertRaises(evidence_ledger.EvidenceError):
            evidence_ledger.append_record(self.db, "lineage", validation(), "wrong-stream")

    def test_every_frozen_lineage_and_validation_kind_is_executable(self):
        for index, record in enumerate(all_lineage_records()):
            evidence_ledger.append_record(self.db, "lineage", record, f"lineage-kind-{index}")
        for index, record in enumerate(all_validation_records()):
            evidence_ledger.append_record(self.db, "validation", record, f"validation-kind-{index}")
        self.assertEqual(self.db.execute("SELECT count(*) FROM visual_evidence_records").fetchone()[0], 13)


if __name__ == "__main__":
    unittest.main()
