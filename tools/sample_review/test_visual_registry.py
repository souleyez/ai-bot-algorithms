#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.sample_review import visual_registry


class VisualRegistryTests(unittest.TestCase):
    def test_loads_two_accepted_and_four_catalog_only_algorithms(self) -> None:
        registry = visual_registry.load_default_registry()
        self.assertEqual(len(registry), 6)
        self.assertEqual(
            {key for key, entry in registry.items() if entry.accepted},
            {"takeaway_uniform", "new_world_workwear"},
        )
        self.assertFalse(registry["scene_change"].accepted)

    def test_resolves_current_review_labels_through_pinned_taxonomy(self) -> None:
        registry = visual_registry.accepted_algorithms()
        self.assertEqual(registry["takeaway_uniform"].normalize_label("takeaway"), "courier")
        self.assertEqual(
            registry["new_world_workwear"].normalize_label("workwear"), "workwear-staff"
        )
        with self.assertRaises(visual_registry.RegistryValidationError):
            registry["takeaway_uniform"].normalize_label("unknown-person")

    def test_source_mapping_is_data_driven_and_unknown_fails_closed(self) -> None:
        row = {"source_kind": "upload-takeaway"}
        self.assertEqual(visual_registry.legacy_algorithm_for(row), "takeaway_uniform")
        self.assertEqual(visual_registry.legacy_algorithm_for({"source_kind": "door"}), "")

    def test_digest_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="visual-registry-") as directory:
            root = Path(directory) / "registry"
            shutil.copytree(visual_registry.DEFAULT_REGISTRY_ROOT, root)
            path = root / "algorithms" / "takeaway_uniform.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["display_name"] = "tampered"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(visual_registry.RegistryValidationError, "digest mismatch"):
                visual_registry.load_registry(root)


if __name__ == "__main__":
    unittest.main()
