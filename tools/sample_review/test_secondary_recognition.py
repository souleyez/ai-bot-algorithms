from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.sample_review import secondary_recognition, visual_registry


NOW = datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def policy_for(entry: visual_registry.AlgorithmEntry, **limits) -> dict:
    policy = {
        "schema_version": "ai-bot-secondary-recognition-policy.v1",
        "policy_id": f"secondary-policy:{entry.algorithm_key}.v1", "policy_revision": 1,
        "algorithm_key": entry.algorithm_key, "task_type": entry.task_type,
        "visual_semantics_bundle_ref": entry.visual_semantics_version_ref,
        "visual_semantics_bundle_content_sha256": entry.visual_semantics_content_sha256,
        "task_profile_ref": entry.task_profile_ref, "task_profile_content_sha256": entry.task_profile_content_sha256,
        "taxonomy_version_ref": entry.taxonomy_version_ref, "taxonomy_content_sha256": entry.taxonomy_content_sha256,
        "annotation_contract": entry.annotation_contract,
        "trigger_modes": ["offline_shadow_replay"], "provider_ref": "provider:reviewed-v1",
        "model_ref": "model:secondary-vision-v1", "inference_policy_digest": "a" * 64,
        "prompt_template_digest": "b" * 64, "inference_config_digest": "c" * 64,
        "response_schema_version": "ai-bot-secondary-prediction.v1",
        "retrieval": {"enabled": False, "retrieval_policy_digest": "d" * 64, "max_examples": 0,
                      "exclude_current_item": True, "exclude_capture_group": True,
                      "exclude_regression": True, "exclude_near_duplicate_groups": True},
        "limits": {"max_attempts": 2, "max_image_bytes": 1024, "max_image_pixels": 100,
                   "max_requests_per_minute": 20, "billing_currency": "CNY",
                   "daily_cost_limit_microunits": 1000, "monthly_cost_limit_microunits": 5000,
                   "retention_days": 30},
        "evaluation_gate": {"minimum_samples": 20, "minimum_secondary_agreement_ppm": 800000,
                            "minimum_improvement_ppm": 10000, "maximum_latency_p95_ms": 5000,
                            "maximum_cost_per_sample_microunits": 100,
                            "maximum_malformed_rate_ppm": 10000, "maximum_timeout_rate_ppm": 20000},
        "review_state": "accepted", "runtime_initial_state": "disabled",
        "created_at": "2026-08-16T07:00:00Z", "content_sha256": "0" * 64,
    }
    policy["limits"].update(limits)
    policy["content_sha256"] = hashlib.sha256(canonical({k: v for k, v in policy.items() if k != "content_sha256"}).encode()).hexdigest()
    return policy


def prediction(label: str) -> dict:
    return {"schema_version": "ai-bot-secondary-prediction.v1", "decision": "positive", "confidence": .8,
            "boxes": [{"x": .1, "y": .1, "w": .3, "h": .4, "label_key": label, "confidence": .8}],
            "label_keys": [label], "tag_assertions": []}


class SecondaryRecognitionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        secondary_recognition.migrate(self.db)
        self.entry = visual_registry.accepted_algorithms()["takeaway_uniform"]
        self.assertTrue(self.entry.secondary_allowed)
        self.policy = policy_for(self.entry)
        image = b"bounded-test-image"
        self.image = image
        self.image_sha = hashlib.sha256(image).hexdigest()

    def tearDown(self):
        self.db.close()

    def enable(self, state="shadow"):
        secondary_recognition.ensure_policy_runtime(self.db, self.policy, now=NOW)
        result = secondary_recognition.transition_runtime(
            self.db, self.policy, expected_revision=1, next_state="shadow",
            idempotency_key="enable-shadow", now=NOW,
        )
        if state == "human_assisted":
            result = secondary_recognition.transition_runtime(
                self.db, self.policy, expected_revision=2, next_state=state,
                idempotency_key="enable-human", now=NOW,
            )
        return result

    def assess(self, provider, key="assessment-1"):
        return secondary_recognition.run_assessment(
            self.db, self.policy, self.entry, item_id="item-1", base_review_revision=0,
            image_bytes=self.image, image_pixels=64, image_sha256=self.image_sha,
            idempotency_key=key, provider=provider, now=NOW,
        )

    def test_policy_starts_disabled_and_transition_is_cas_idempotent(self):
        runtime = secondary_recognition.ensure_policy_runtime(self.db, self.policy, now=NOW)
        self.assertEqual(runtime["runtime_state"], "disabled")
        with self.assertRaises(secondary_recognition.SecondaryRecognitionError):
            secondary_recognition.transition_runtime(
                self.db, self.policy, expected_revision=1, next_state="human_assisted",
                idempotency_key="skip-shadow", now=NOW,
            )
        first = self.enable()
        replay = secondary_recognition.transition_runtime(
            self.db, self.policy, expected_revision=1, next_state="shadow",
            idempotency_key="enable-shadow", now=NOW,
        )
        self.assertEqual(first["state_revision"], 2)
        self.assertTrue(replay["replayed"])

    def test_shadow_stamps_provenance_and_human_assisted_controls_visibility(self):
        self.enable()
        def provider(request, image):
            self.assertEqual(set(request), {"prediction_schema", "policy_id"})
            self.assertEqual(image, self.image)
            return {"prediction": prediction("courier"), "latency_ms": 400,
                    "cost_microunits": 20, "provider_receipt_ref": "receipt:opaque-1"}
        result = self.assess(provider)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(secondary_recognition.visible_observations(self.db, self.entry.algorithm_key, "item-1", 0), [])
        secondary_recognition.transition_runtime(
            self.db, self.policy, expected_revision=2, next_state="human_assisted",
            idempotency_key="enable-human", now=NOW,
        )
        visible = secondary_recognition.visible_observations(self.db, self.entry.algorithm_key, "item-1", 0)
        self.assertEqual(visible[0]["observation_role"], "secondary_multimodal")
        self.assertEqual(visible[0]["provider_ref"], self.policy["provider_ref"])
        self.assertNotIn("endpoint", canonical(visible))

    def test_provider_failures_and_budget_never_write_truth(self):
        self.enable()
        calls = 0
        def timeout(_request, _image):
            nonlocal calls; calls += 1; raise TimeoutError()
        failed = self.assess(timeout)
        self.assertEqual((failed["status"], failed["attempt_count"], calls), ("failed", 2, 2))
        self.assertEqual(self.db.execute("SELECT count(*) FROM secondary_observations").fetchone()[0], 0)
        budget_policy = policy_for(self.entry, daily_cost_limit_microunits=0, monthly_cost_limit_microunits=0)
        budget_policy["policy_id"] = "secondary-policy:takeaway_uniform.budget-v1"
        budget_policy["content_sha256"] = hashlib.sha256(canonical({k: v for k, v in budget_policy.items() if k != "content_sha256"}).encode()).hexdigest()
        secondary_recognition.ensure_policy_runtime(self.db, budget_policy, now=NOW)
        secondary_recognition.transition_runtime(self.db, budget_policy, expected_revision=1, next_state="shadow", idempotency_key="budget-shadow", now=NOW)
        called = False
        def must_not_call(_request, _image):
            nonlocal called; called = True; return {}
        result = secondary_recognition.run_assessment(
            self.db, budget_policy, self.entry, item_id="item-2", base_review_revision=0,
            image_bytes=self.image, image_pixels=64, image_sha256=self.image_sha,
            idempotency_key="budget-run", provider=must_not_call, now=NOW,
        )
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertFalse(called)

    def test_prediction_is_bounded_and_idempotency_conflicts_fail_closed(self):
        self.enable()
        provider = lambda _request, _image: {"prediction": prediction("courier")}
        first = self.assess(provider)
        replay = self.assess(provider)
        self.assertEqual(first["observation_id"], replay["observation_id"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(secondary_recognition.RuntimeIdempotencyConflict):
            secondary_recognition.run_assessment(
                self.db, self.policy, self.entry, item_id="other", base_review_revision=0,
                image_bytes=self.image, image_pixels=64, image_sha256=self.image_sha,
                idempotency_key="assessment-1", provider=provider, now=NOW,
            )
        bad = prediction("courier"); bad["boxes"][0]["x"] = .9
        result = secondary_recognition.run_assessment(
            self.db, self.policy, self.entry, item_id="item-2", base_review_revision=0,
            image_bytes=self.image, image_pixels=64, image_sha256=self.image_sha,
            idempotency_key="malformed", provider=lambda _request, _image: {"prediction": bad}, now=NOW,
        )
        self.assertEqual(result["status"], "failed")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE secondary_observations SET canonical_observation_json='{}'")

    def test_secondary_comparison_remains_separate_from_human_truth(self):
        observation = prediction("courier")
        human = {"decision": "positive", "label_keys": ["courier"], "tag_keys": [],
                 "boxes": [{"x": .6, "y": .1, "w": .3, "h": .4, "label_key": "courier"}]}
        self.assertEqual(
            secondary_recognition.compare_to_human(observation, human, self.entry),
            ["geometry_error"],
        )
        self.assertEqual(human["boxes"][0]["x"], .6)


if __name__ == "__main__":
    unittest.main()
