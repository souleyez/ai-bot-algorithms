from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.training_assets.materialize_manifest import (
    MaterializationError,
    Request,
    canonical_json,
    materialize,
    sha256,
)


class FakeClient:
    def __init__(self, metadata: dict[str, Any], members: list[dict[str, Any]], facts: dict[str, dict[str, Any]], images: dict[str, bytes]):
        self.metadata, self.members, self.facts, self.images = metadata, members, facts, images

    def json(self, _base: str, path: str, _token: str):
        if path.startswith("/internal/") and "/manifest?" in path:
            return copy.deepcopy(self.metadata), {"content-type": "application/json"}
        if path.startswith("/internal/") and "/members?" in path:
            second = "cursor=" in path
            selected = self.members[1:] if second else self.members[:1]
            return {"items": copy.deepcopy(selected), "next_cursor": None if second or len(self.members) == 1 else "page-2", "manifest_digest": REQUEST_DIGEST}, {"content-type": "application/json"}
        if path.endswith("/review-fact"):
            fact = copy.deepcopy(self.facts[path])
            return fact, {"content-type": "application/json", "x-review-fact-sha256": fact["content_sha256"]}
        raise AssertionError(path)

    def binary(self, _base: str, path: str, _token: str):
        payload = self.images[path]
        return payload, {"content-type": "image/jpeg", "x-content-sha256": sha256(payload)}


def fact(item: str, revision: int, image_digest: str, label: str) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "ai-bot-review-fact.v1", "algorithm_key": "takeaway_uniform",
        "item_id": item, "review_revision": revision, "image": {"sha256": image_digest},
        "human_truth": {"decision": "positive", "label_keys": [label], "tag_keys": [],
                        "boxes": [{"x": .1, "y": .2, "w": .3, "h": .4, "label_key": label}]},
    }
    value["content_sha256"] = sha256(canonical_json(value))
    return value


def fixture() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, bytes], dict[str, int]]:
    mapping = {"courier": 0}
    images = {"item-a": b"jpeg-a", "item-b": b"jpeg-b"}
    facts = {key: fact(key, 1, sha256(payload), "courier") for key, payload in images.items()}
    members = []
    fact_paths: dict[str, dict[str, Any]] = {}
    image_paths: dict[str, bytes] = {}
    for ordinal, (item, split) in enumerate((("item-a", "train"), ("item-b", "validation"))):
        prefix = f"/api/internal/training-assets/v1/algorithms/takeaway_uniform/items/{item}/revisions/1"
        fact_paths[prefix + "/review-fact"] = facts[item]
        image_paths[prefix + "/original"] = images[item]
        members.append({"ordinal": ordinal, "item_id": item, "review_revision": 1,
                        "review_fact_digest": facts[item]["content_sha256"],
                        "evidence_locator_digest": "e" * 64, "image_sha256": sha256(images[item]),
                        "capture_group_id": f"capture-{ordinal}", "split": split})
    metadata = {
        "schema_version": "ai-bot-training-manifest.v1", "proposal_id": "11111111-1111-4111-8111-111111111111",
        "proposal_revision": 2, "algorithm_key": "takeaway_uniform", "task_type": "object_detection",
        "truth_core_dataset_version_id": "truth-v1", "regression_core_dataset_version_id": "reg-v1",
        "visual_semantics_core_dataset_version_id": "sem-v1", "visual_semantics_content_sha256": "a" * 64,
        "task_profile_ref": "task:v1", "task_profile_content_sha256": "b" * 64,
        "taxonomy_version_ref": "taxonomy:v1", "taxonomy_content_sha256": "c" * 64,
        "review_policy_ref": "review:v1", "review_policy_content_sha256": "d" * 64,
        "annotation_contract": "bbox.v1", "selection_contract": "ai-bot.selection.v1",
        "selection_predicate": {"decisions": ["negative", "positive"], "require_trainable": True,
                                "include_regression_roles": [], "exclude_error_types": []},
        "split_seed": 7, "group_split_policy": "capture-group-stratified-80-10-10.v1",
        "group_split_policy_digest": "1" * 64, "split_membership_digest": "2" * 64,
        "capture_group_membership_digest": "3" * 64,
        "counts": {"positive": 2, "negative": 0, "corrected": 0, "excluded": 0, "conflict": 0},
        "parent_model_ref": None, "parent_model_digest": None,
        "training_config_digest": "4" * 64, "class_mapping_digest": sha256(canonical_json(mapping)),
        "machine_observation_policy": "human_truth_only", "creator_ref": "owner-1",
        "created_at": "2026-08-16T00:00:00Z", "manifest_state": "frozen",
    }
    return metadata, members, fact_paths, image_paths, mapping


REQUEST_DIGEST = ""


class MaterializeManifestTests(unittest.TestCase):
    def build(self):
        global REQUEST_DIGEST
        metadata, members, facts, images, mapping = fixture()
        REQUEST_DIGEST = sha256(canonical_json({**metadata, "members": members}))
        request = Request("https://platform", "https://review", metadata["proposal_id"], 2, REQUEST_DIGEST, "p" * 32, "r" * 32)
        return FakeClient(metadata, members, facts, images), request, mapping

    def test_two_materializations_are_byte_identical_and_revision_bound(self):
        client, request, mapping = self.build()
        with tempfile.TemporaryDirectory() as root:
            first, second = Path(root) / "first", Path(root) / "second"
            receipt = materialize(client, request, first, mapping)
            materialize(client, request, second, mapping)
            files = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
            self.assertEqual(files, sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file()))
            for relative in files:
                self.assertEqual((first / relative).read_bytes(), (second / relative).read_bytes())
            self.assertEqual(receipt["member_count"], 2)
            self.assertIn(b"0 0.25000000 0.40000000 0.30000000 0.40000000", (first / "labels/train/000000-item-a.txt").read_bytes())

    def test_digest_mismatch_fails_before_target_exists(self):
        client, request, mapping = self.build()
        client.members[0]["review_fact_digest"] = "f" * 64
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "result"
            with self.assertRaisesRegex(MaterializationError, "manifest digest mismatch"):
                materialize(client, request, output, mapping)
            self.assertFalse(output.exists())

    def test_capture_group_cannot_cross_splits(self):
        client, request, mapping = self.build()
        client.members[1]["capture_group_id"] = client.members[0]["capture_group_id"]
        global REQUEST_DIGEST
        REQUEST_DIGEST = sha256(canonical_json({**client.metadata, "members": client.members}))
        request = Request(request.platform_endpoint, request.review_endpoint, request.proposal_id, 2, REQUEST_DIGEST, request.platform_token, request.review_token)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(MaterializationError, "capture group crosses"):
                materialize(client, request, Path(root) / "result", mapping)

    def test_late_fact_mismatch_removes_staging_and_target(self):
        client, request, mapping = self.build()
        second_path = next(path for path in client.facts if "/item-b/" in path)
        client.facts[second_path]["content_sha256"] = "f" * 64
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "result"
            with self.assertRaisesRegex(MaterializationError, "review fact digest mismatch"):
                materialize(client, request, output, mapping)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_class_mapping_digest_is_exact(self):
        client, request, _mapping = self.build()
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(MaterializationError, "class mapping digest mismatch"):
                materialize(client, request, Path(root) / "result", {"courier": 1})


if __name__ == "__main__":
    unittest.main()
