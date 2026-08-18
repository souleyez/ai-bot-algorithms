#!/usr/bin/env python3
import base64, json, unittest
from tools.datamax_connector.ai_bot_capture import connector

class Fake:
    def request(self, method, path, body=None, headers=None):
        if method == "POST": return {"snapshot_id":"s1","ordered_membership_digest":"d"*64,"total":1}
        return {"ordered_membership_digest":"d"*64,"items":[{"schema_version":"ai-bot-capture-item.v1","item_id":"i1","capture_revision":2,"captured_at":"2026-01-01T00:00:00Z","image":{"sha256":"a"*64}}],"next_cursor":""}

class NoNetwork:
    def request(self, *args, **kwargs): raise AssertionError("validate must not touch the transport")

class Tests(unittest.TestCase):
    def test_validate_is_strictly_offline(self):
        req={"protocol":connector.PROTOCOL,"request_id":"r1","connector_key":connector.KEY,"connector_version":connector.VERSION,"operation":"validate","settings":{"api_base_url":"http://127.0.0.1:8793"}}
        events=connector.execute(req,{"api_token":"<token>"*4},NoNetwork())
        self.assertEqual(events[-1]["complete"],{"resources_emitted":0,"items_emitted":0})

    def test_capture_is_not_truth_and_locator_is_exact(self):
        req={"protocol":connector.PROTOCOL,"request_id":"r1","connector_key":connector.KEY,"connector_version":connector.VERSION,"operation":"sync","settings":{"api_base_url":"http://127.0.0.1:8793"},"resource_id":"captures","limit":10}
        events=connector.execute(req,{"api_token":"<token>"*4},Fake()); item=events[0]["item"]
        content=json.loads(base64.b64decode(item["content_base64"]))
        self.assertFalse({"human_truth","eligibility","decision"}.intersection(content))
        self.assertEqual(item["metadata"]["source_locator"],"aibot-capture://i1/2")

if __name__ == "__main__": unittest.main()
