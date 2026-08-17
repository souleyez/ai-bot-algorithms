from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools.sample_review import package_server8_release


class PackageServer8ReleaseTests(unittest.TestCase):
    def test_server8_unit_runs_committed_release_read_only(self) -> None:
        unit = (Path(__file__).with_name("ai-bot-sample-review-server8.service")).read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "-v /opt/ai-bot-sample-review/current:/opt/ai-bot-sample-review:ro",
            unit,
        )
        self.assertIn("PYTHONPATH=/opt/ai-bot-sample-review", unit)
        self.assertIn("SAMPLE_REVIEW_STATIC_ROOT=/opt/ai-bot-sample-review/tools/sample_review/static", unit)
        self.assertIn("python /opt/ai-bot-sample-review/tools/sample_review/server.py", unit)
        self.assertNotIn("python /app/server.py", unit)
        for variable in (
            "DATAMAX_EXPORT_TOKEN",
            "DATAMAX_CAPTURE_EXPORT_TOKEN",
            "DATAMAX_REVIEW_TOKEN",
            "TRAINING_ASSET_TOKEN",
            "DATAMAX_CURSOR_SIGNING_KEY",
            "DATAMAX_LINEAGE_EXPORT_TOKEN",
            "DATAMAX_VALIDATION_EXPORT_TOKEN",
        ):
            self.assertIn(f"-e {variable}", unit)

    def test_server_static_root_can_follow_the_immutable_release(self) -> None:
        server = (Path(__file__).with_name("server.py")).read_text(encoding="utf-8")
        self.assertIn('os.environ.get("SAMPLE_REVIEW_STATIC_ROOT"', server)

    def test_runtime_paths_are_narrow_and_complete(self) -> None:
        paths = package_server8_release.runtime_paths(
            [
                "tools/sample_review/server.py",
                "tools/sample_review/replay_reviewed_box_reports.py",
                "tools/sample_review/test_server.py",
                "tools/sample_review/static/app.js",
                "tools/sample_review/ai-bot-sample-review-server8.service",
                "tools/sample_review/ai-bot-datamax-export.example.env",
                "tools/sample_review/install_remote.sh",
                "tools/algorithm_platform/evidence_ledger.py",
                "tools/algorithm_platform/evidence_schema.sql",
                "platform/visual-task-registry/accepted-head.json",
                "platform/contracts/ai-bot-visual-knowledge-v1.bundle.json",
                "platform/contracts/verify_visual_contract_bundle.py",
                "tmp/private.json",
            ]
        )

        self.assertEqual(
            paths,
            [
                "platform/contracts/ai-bot-visual-knowledge-v1.bundle.json",
                "platform/contracts/verify_visual_contract_bundle.py",
                "platform/visual-task-registry/accepted-head.json",
                "tools/algorithm_platform/evidence_ledger.py",
                "tools/algorithm_platform/evidence_schema.sql",
                "tools/sample_review/ai-bot-datamax-export.example.env",
                "tools/sample_review/ai-bot-sample-review-server8.service",
                "tools/sample_review/replay_reviewed_box_reports.py",
                "tools/sample_review/server.py",
                "tools/sample_review/static/app.js",
            ],
        )

    def test_archive_is_deterministic_and_bound_to_source(self) -> None:
        files = {
            "tools/sample_review/server.py": b"print('ok')\n",
            "tools/sample_review/static/app.js": b"console.log('ok')\n",
            "tools/algorithm_platform/evidence_ledger.py": b"VALUE = 1\n",
            "tools/algorithm_platform/evidence_schema.sql": b"SELECT 1;\n",
            "platform/visual-task-registry/accepted-head.json": b"{}\n",
        }
        commit = "a" * 40
        epoch = 1_700_000_000

        first = package_server8_release.build_archive_bytes(files, commit, epoch)
        second = package_server8_release.build_archive_bytes(files, commit, epoch)
        self.assertEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())
        self.assertEqual(first, second)

        with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
            names = archive.getnames()
            self.assertEqual(names, sorted(names))
            self.assertNotIn("tools/sample_review/test_server.py", names)
            manifest = json.load(archive.extractfile("release/ai-bot-sample-review-release.json"))
            self.assertEqual(manifest["schema"], "ai-bot.sample-review.release.v1")
            self.assertEqual(manifest["commit"], commit)
            self.assertEqual(manifest["source_date_epoch"], epoch)
            self.assertEqual(
                manifest["files"]["tools/sample_review/server.py"],
                hashlib.sha256(files["tools/sample_review/server.py"]).hexdigest(),
            )

        verified = package_server8_release.verify_archive_bytes(
            first,
            expected_commit=commit,
            expected_sha256=hashlib.sha256(first).hexdigest(),
        )
        self.assertEqual(verified["commit"], commit)

    def test_archive_verification_rejects_tampering_and_wrong_source(self) -> None:
        content = package_server8_release.build_archive_bytes(
            {"tools/sample_review/server.py": b"print('ok')\n"},
            "c" * 40,
            1_700_000_002,
        )
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            package_server8_release.verify_archive_bytes(
                content,
                expected_commit="c" * 40,
                expected_sha256="0" * 64,
            )
        with self.assertRaisesRegex(ValueError, "commit mismatch"):
            package_server8_release.verify_archive_bytes(
                content,
                expected_commit="d" * 40,
            )

    def test_write_release_does_not_use_untracked_worktree_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            files = {"tools/sample_review/server.py": b"from committed tree\n"}
            result = package_server8_release.write_release(
                output,
                files,
                "b" * 40,
                1_700_000_001,
            )
            self.assertTrue(result.archive.is_file())
            self.assertTrue(result.checksum.is_file())
            checksum_text = result.checksum.read_text(encoding="ascii")
            self.assertEqual(
                checksum_text,
                f"{result.sha256}  {result.archive.name}\n",
            )


if __name__ == "__main__":
    unittest.main()
