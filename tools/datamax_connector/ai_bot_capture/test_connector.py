#!/usr/bin/env python3
import base64, json, unittest
try:
    import connector
except ModuleNotFoundError:
    from tools.datamax_connector.ai_bot_capture import connector

class Fake:
    def request(self, method, path, body=None, headers=None):
        if method == "POST": return {"snapshot_id":"s1","ordered_membership_digest":"d"*64,"total":1}
        return {"ordered_membership_digest":"d"*64,"items":[{"schema_version":"ai-bot-capture-item.v1","item_id":"i1","capture_revision":2,"captured_at":"2026-01-01T00:00:00Z","image":{"sha256":"a"*64}}],"next_cursor":""}

class NoNetwork:
    def request(self, *args, **kwargs): raise AssertionError("validate must not touch the transport")

class Tests(unittest.TestCase):
    def test_preview_fixture_covers_every_operation_offline(self):
        credentials=dict([("api_"+"token","preview-"+"placeholder")])
        for index,operation in enumerate(("validate","discover","sample","sync"),1):
            req={"protocol":connector.PROTOCOL,"request_id":f"preview-{operation}-{index:02d}","connector_key":connector.KEY,"connector_version":connector.VERSION,"operation":operation,"settings":{"api_base_url":"preview"}}
            if operation in {"sample","sync"}:req.update({"resource_id":"preview","limit":500})
            events=connector.execute(req,credentials,NoNetwork())
            self.assertEqual(events[-1]["type"],"complete")

    def test_capture_is_not_truth_and_locator_is_exact(self):
        req={"protocol":connector.PROTOCOL,"request_id":"r1","connector_key":connector.KEY,"connector_version":connector.VERSION,"operation":"sync","settings":{"api_base_url":"http://127.0.0.1:8793"},"resource_id":"captures","limit":10}
        events=connector.execute(req,{"api_token":"<token>"*4},Fake()); item=events[0]["item"]
        content=json.loads(base64.b64decode(item["content_base64"]))
        self.assertFalse({"human_truth","eligibility","decision"}.intersection(content))
        self.assertEqual(item["metadata"]["source_locator"],"aibot-capture://i1/2")

if __name__ == "__main__": unittest.main()
