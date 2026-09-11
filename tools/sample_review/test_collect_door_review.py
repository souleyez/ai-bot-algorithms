import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.sample_review import collect_door_review as collector


class CollectorTests(unittest.TestCase):
    def test_missing_pin_never_connects(self):
        client = MagicMock()
        client.get_host_keys().lookup.return_value = None
        with patch.object(collector.paramiko, "SSHClient", return_value=client):
            with self.assertRaises(RuntimeError):
                collector.connect_device(Path("known_hosts"))
        client.connect.assert_not_called()
        self.assertIsInstance(client.set_missing_host_key_policy.call_args.args[0], collector.paramiko.RejectPolicy)

    def test_missing_env_never_connects(self):
        client = MagicMock()
        with patch.object(collector.paramiko, "SSHClient", return_value=client), patch.dict(collector.os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                collector.connect_device(Path("known_hosts"))
        client.connect.assert_not_called()

    def test_scoped_import_never_runs_retention(self):
        client = MagicMock()
        with patch.object(collector, "connect_device", return_value=client), \
             patch.object(collector.sync_worker, "sync_source", return_value={"new":0}) as sync, \
             patch.object(collector.sync_worker, "purge_expired_pending") as purge:
            collector.collect(Path("known_hosts"))
        source = sync.call_args.args[1]
        self.assertEqual(source["device"], "62821")
        self.assertNotIn("channel", source)
        self.assertEqual(source["kind"], "door-state")
        self.assertTrue(collector.sync_worker.matches_source("ch14_m101_1.jpg", source))
        for filename in ("ch14_m1010_1.jpg", "s_ch14_m101_1.jpg", "ch2_m104_1.jpg"):
            self.assertFalse(collector.sync_worker.matches_source(filename, source))
        purge.assert_not_called()
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
