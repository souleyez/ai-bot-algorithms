import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.sample_review import gpu_dashboard as dashboard
from tools.sample_review import gpu_probe as probe


class GpuDashboardTests(unittest.TestCase):
    def test_gpu_csv_distinguishes_zero_from_unsupported(self):
        row = probe.gpu_metrics("0, NVIDIA GeForce RTX 4090, 595.84, 0, 21448, 24564, 33, [N/A], 450\n")[0]
        self.assertEqual(row["utilization"], 0)
        self.assertIsNone(row["powerW"])
        self.assertEqual(row["memoryUsedMiB"], 21448)
        self.assertIsNone(probe.numeric("nan"))

    def test_public_snapshot_only_publishes_allowlisted_fields(self):
        raw = {"schema": dashboard.SCHEMA, "generatedAt": "2026-09-11T05:00:00Z", "secret": "hidden",
               "nodes": [{"id": "souleye", "reachable": True, "password": "hidden", "address": "private",
                          "gpus": [{"name": "RTX 4090", "utilization": 0, "uuid": "hidden"}],
                          "models": [{"name": "Qwen3.gguf", "path": "private", "sizeMiB": 2048},
                                     {"name": "/private/model.gguf", "sizeMiB": 123}],
                          "services": [{"name": "Qwen3.8 27B", "state": "active", "healthy": True,
                                        "model": "Qwen3.gguf", "args": "hidden"}],
                          "errors": ["password=hidden", "gpu_unavailable"]}]}
        public = dashboard.public_snapshot(raw)
        encoded = json.dumps(public)
        self.assertNotIn("hidden", encoded)
        self.assertNotIn("private", encoded)
        self.assertEqual(len(public["nodes"]), 2)
        self.assertFalse(public["nodes"][0]["reachable"])
        self.assertEqual(public["nodes"][0]["gpus"], [])
        self.assertEqual(public["nodes"][1]["models"], [{"name": "Qwen3.gguf", "sizeMiB": 2048}])
        self.assertEqual(public["nodes"][1]["gpus"][0]["utilization"], 0)

    def test_stopped_service_is_not_healthy(self):
        with patch.object(probe, "command", return_value="ActiveState=inactive\nMainPID=0\n"), patch.object(probe, "api") as api:
            state = probe.service("test.service", "ComfyUI / H3", 8188, "comfy")
        api.assert_not_called()
        self.assertEqual(state["state"], "inactive")
        self.assertIsNone(state["healthy"])
        self.assertIsNone(state["queueRunning"])

    def test_comfy_available_artifacts_do_not_imply_resident_models(self):
        with patch.object(probe, "command", return_value="ActiveState=active\nMainPID=42\n"), patch.object(probe, "api", side_effect=[{"system": {}}, {"queue_running": [], "queue_pending": []}]):
            state = probe.service("test.service", "ComfyUI / H3", 8188, "comfy")
        self.assertTrue(state["healthy"])
        self.assertEqual(state["model"], "")
        self.assertEqual(state["queueRunning"], 0)

    def test_missing_or_invalid_snapshot_returns_unknown_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            result = dashboard.load_snapshot(Path(directory) / "missing.json")
        self.assertEqual(result["generatedAt"], "")
        self.assertTrue(all(not row["reachable"] and not row["gpus"] for row in result["nodes"]))
        self.assertEqual(dashboard.timestamp("2026-99-11T05:00:00Z"), "")

    def test_fetch_failure_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "gpu.json"
            output.write_text("previous", encoding="utf-8")
            with patch.object(dashboard.subprocess, "run") as run:
                run.return_value.returncode = 255
                run.return_value.stdout = b""
                with self.assertRaises(ValueError):
                    dashboard.fetch(output)
            self.assertEqual(output.read_text(), "previous")

    def test_unknown_queue_is_not_reported_as_zero(self):
        with patch.object(probe, "command", return_value="ActiveState=active\nMainPID=42\n"), patch.object(probe, "api", side_effect=[{"system": {}}, ValueError("unavailable")]):
            state = probe.service("test.service", "ComfyUI / H3", 8188, "comfy")
        self.assertTrue(state["healthy"])
        self.assertIsNone(state["queueRunning"])
        self.assertIsNone(state["queuePending"])

    def test_unreachable_probe_does_not_keep_previous_metrics(self):
        public = dashboard.normalize_node("h3", "10", {"reachable": False, "gpus": [{"utilization": 99}], "errors": ["ssh_failed"]})
        self.assertEqual(public["gpus"], [])
        self.assertEqual(public["errors"], ["ssh_failed"])


if __name__ == "__main__":
    unittest.main()
