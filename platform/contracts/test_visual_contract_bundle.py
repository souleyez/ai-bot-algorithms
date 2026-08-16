#!/usr/bin/env python3
from __future__ import annotations

import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "ai_bot_visual_contract_verifier", HERE / "verify_visual_contract_bundle.py"
)
assert SPEC and SPEC.loader
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class BundleVerifierTests(unittest.TestCase):
    def _bundle_path(self) -> Path:
        return HERE / "ai-bot-visual-knowledge-v1.bundle.json"

    def _write_tampered(self, bundle: dict) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="visual-contract-tamper-"))
        path = directory / "tampered.bundle.json"
        path.write_text(json.dumps(bundle), encoding="utf-8")
        self.addCleanup(directory.rmdir)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_vendored_bundle_matches_recorded_digest_and_embedded_bytes(self) -> None:
        summary = verify.verify_bundle(self._bundle_path())
        self.assertEqual(summary["bundle_id"], "ai-bot-visual-knowledge.v1")
        self.assertEqual(summary["bundle_revision"], 2)
        self.assertEqual(
            summary["content_sha256"],
            "dcaba0d4b93ccc91ad9c9874de2a4c9121ef3d62baa89a06e827aa5bdd5d8e89",
        )
        self.assertEqual(len(summary["verified_files"]), 19)
        review_schema = json.loads(
            summary["decoded_files"]["ai-bot-review-fact-v1.schema.json"]
        )
        self.assertEqual(
            review_schema["properties"]["schema_version"]["const"],
            "ai-bot-review-fact.v1",
        )

    def test_tampered_bundle_metadata_is_rejected(self) -> None:
        bundle = json.loads(self._bundle_path().read_text(encoding="utf-8"))
        bundle["files"]["ai-bot-review-fact-v1.schema.json"]["content_sha256"] = "0" * 64
        with self.assertRaises(verify.BundleVerificationError):
            verify.verify_bundle(self._write_tampered(bundle))

    def test_tampered_embedded_member_is_rejected_even_with_original_metadata(self) -> None:
        bundle = json.loads(self._bundle_path().read_text(encoding="utf-8"))
        entry = bundle["files"]["ai-bot-review-fact-v1.schema.json"]
        payload = bytearray(base64.b64decode(entry["content_base64"]))
        payload[-2] ^= 1
        entry["content_base64"] = base64.b64encode(payload).decode("ascii")
        stripped = {key: value for key, value in bundle.items() if key != "content_sha256"}
        bundle["content_sha256"] = verify.sha256_hex(
            verify.canonicalize(stripped).encode("utf-8")
        )
        with self.assertRaisesRegex(verify.BundleVerificationError, "member .* sha256"):
            verify.verify_bundle(self._write_tampered(bundle))

    def test_canonicalize_orders_keys(self) -> None:
        self.assertEqual(verify.canonicalize({"b": 1, "a": 2}), '{"a":2,"b":1}')


if __name__ == "__main__":
    unittest.main()
