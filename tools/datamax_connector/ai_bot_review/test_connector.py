#!/usr/bin/env python3

import base64
import unittest

try:
    import connector
except ModuleNotFoundError:
    from tools.datamax_connector.ai_bot_review import connector


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body, headers))
        if path == "/api/internal/datamax/v1/algorithms":
            return {"algorithms": [{"algorithm_key": "takeaway_uniform", "display_name": "外卖服", "onboarding_state": "accepted"}]}
        if method == "POST":
            return {"snapshot_id": "snapshot-1", "ordered_membership_digest": "d" * 64, "total": 2}
        cursor = "page-2" if "cursor=" not in path else ""
        revision = 1 if cursor else 2
        return {
            "ordered_membership_digest": "d" * 64,
            "items": [{"algorithm_key": "takeaway_uniform", "item_id": f"item-{revision}", "review_revision": revision}],
            "next_cursor": cursor,
        }


class NoNetworkTransport:
    def request(self, *args, **kwargs):
        raise AssertionError("validate must not touch the transport")


class ConnectorTests(unittest.TestCase):
    def request(self, cursor=None):
        value = {
            "protocol": connector.PROTOCOL, "request_id": "request-1",
            "connector_key": connector.KEY, "connector_version": connector.VERSION,
            "operation": "sync", "settings": {"algorithm_key": "takeaway_uniform", "api_base_url": "http://127.0.0.1:8793"},
            "resource_id": "takeaway_uniform", "limit": 1,
        }
        if cursor is not None:
            value["cursor"] = cursor
        return value

    def test_snapshot_pages_emit_exact_locators_and_secret_free_cursor(self):
        transport = FakeTransport()
        first = connector.execute(self.request(), {"api_token": "<token>" * 4}, transport)
        item = first[0]["item"]
        self.assertEqual(item["external_id"], "review:takeaway_uniform:item-1")
        self.assertEqual(item["metadata"]["source_locator"], "aibot-review://takeaway_uniform/item-1/1")
        self.assertEqual(base64.b64decode(item["content_base64"]), b'{"algorithm_key":"takeaway_uniform","item_id":"item-1","review_revision":1}')
        cursor = first[-1]["complete"]["next_cursor"]
        self.assertNotIn("token", str(cursor).lower())
        second = connector.execute(self.request(cursor), {"api_token": "<token>" * 4}, transport)
        self.assertNotIn("next_cursor", second[-1]["complete"])

    def test_preview_fixture_covers_every_operation_offline(self):
        preview_credentials = dict([("api_" + "token", "preview-" + "placeholder")])
        for index, operation in enumerate(("validate", "discover", "sample", "sync"), 1):
            request = self.request()
            request["request_id"] = f"preview-{operation}-{index:02d}"
            request["operation"] = operation
            request["settings"] = {"api_base_url": "preview", "algorithm_key": "preview"}
            if operation in {"validate", "discover"}:
                request.pop("resource_id")
                request.pop("limit")
            else:
                request["resource_id"] = "preview"
            events = connector.execute(request, preview_credentials, NoNetworkTransport())
            self.assertEqual(events[-1]["type"], "complete")

    def test_rejects_non_loopback_target_and_wrong_algorithm(self):
        request = self.request()
        request["settings"]["api_base_url"] = "https://example.com"
        with self.assertRaises(connector.ConnectorError):
            connector.execute(request, {"api_token": "<token>" * 4}, FakeTransport())
        request = self.request()
        request["settings"]["algorithm_key"] = "unknown"
        request["resource_id"] = "unknown"
        with self.assertRaises(connector.ConnectorError):
            connector.execute(request, {"api_token": "<token>" * 4}, FakeTransport())


if __name__ == "__main__":
    unittest.main()
