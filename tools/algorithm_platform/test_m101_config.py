from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import m101_config
import release_worker


class ValidateUpdateTests(unittest.TestCase):
    def test_accepts_customer_fields(self) -> None:
        changes, dry_run = m101_config.validate_update(
            {
                "enabled": True,
                "channels": [3, 1, 3],
                "interval_seconds": 60,
                "confirm_delay_seconds": 8,
                "consecutive_alarm_count": 3,
                "alarm_cooldown_seconds": 600,
                "change_threshold": 0.7,
                "dry_run": True,
            }
        )
        self.assertEqual(changes["channels"], [1, 3])
        self.assertEqual(changes["interval_seconds"], 60)
        self.assertTrue(dry_run)

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaisesRegex(release_worker.PlatformError, "Unsupported"):
            m101_config.validate_update({"channels": [1], "mqtt_password": "nope"})

    def test_rejects_empty_enabled_channels(self) -> None:
        with self.assertRaisesRegex(release_worker.PlatformError, "cannot be empty"):
            m101_config.validate_update({"enabled": True, "channels": []})

    def test_allows_empty_channels_when_disabling(self) -> None:
        changes, _ = m101_config.validate_update({"enabled": False, "channels": []})
        self.assertFalse(changes["enabled"])
        self.assertEqual(changes["channels"], [])

    def test_interval_has_safe_floor(self) -> None:
        with self.assertRaisesRegex(release_worker.PlatformError, "between"):
            m101_config.validate_update({"interval_seconds": 1})


class ValidateControlTests(unittest.TestCase):
    def test_start_requires_channels(self) -> None:
        with self.assertRaisesRegex(release_worker.PlatformError, "required for start"):
            m101_config.validate_control({"device": "61672", "action": "start"})

    def test_start_normalizes_channels(self) -> None:
        device, action, channels, dry_run = m101_config.validate_control(
            {"device": "61672", "action": "START", "channels": [6, 2, 6], "dry_run": True}
        )
        self.assertEqual(device, "61672")
        self.assertEqual(action, "start")
        self.assertEqual(channels, [2, 6])
        self.assertTrue(dry_run)

    def test_stop_without_channels_means_stop_all(self) -> None:
        device, action, channels, dry_run = m101_config.validate_control(
            {"device": "61863", "action": "stop"}
        )
        self.assertEqual((device, action, channels, dry_run), ("61863", "stop", [], False))


class GeneratedApplyCodeTests(unittest.TestCase):
    def test_apply_code_is_valid_python_for_boolean_changes(self) -> None:
        code = m101_config._apply_code(
            {"enabled": True, "channels": [2], "change_threshold": 0.25},
            False,
        )
        self.assertIn("changes = {'enabled': True, 'channels': [2], 'change_threshold': 0.25}", code)
        self.assertIn("channel_action = None", code)
        self.assertIn("action_channels = []", code)
        compile(code, "<m101-remote-apply>", "exec")


class DeviceListTests(unittest.TestCase):
    def test_list_devices_does_not_expose_ssh_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            release_worker.write_json(
                runtime / "catalog.json",
                {
                    "devices": [
                        {
                            "id": "box-1",
                            "display_id": "10001",
                            "machine_code": "abc",
                            "chip_family": "rk3576",
                            "ssh_host": "secret-host",
                            "ssh_port": 22,
                        }
                    ]
                },
            )
            devices = m101_config.list_devices(runtime)
        self.assertEqual(devices[0]["display_id"], "10001")
        self.assertNotIn("ssh_host", devices[0])
        self.assertNotIn("ssh_port", devices[0])

if __name__ == "__main__":
    unittest.main()
