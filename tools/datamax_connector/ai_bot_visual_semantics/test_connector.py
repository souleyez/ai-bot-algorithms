#!/usr/bin/env python3
import base64,json,unittest
from tools.datamax_connector.ai_bot_visual_semantics import connector
class Fake:
    def request(self,path):
        if path.endswith("algorithms"):return {"algorithms":[{"algorithm_key":"takeaway_uniform","display_name":"外卖服","onboarding_state":"accepted"}]}
        return {"algorithm_key":"takeaway_uniform","visual_semantics":{"bundle_id":"b1","content_sha256":"a"*64},"task_profile":{"profile_id":"p1","content_sha256":"b"*64},"taxonomy":{"taxonomy_version_id":"t1","content_sha256":"c"*64,"entries":[]},"review_policy":{"policy_id":"r1","content_sha256":"d"*64}}
class Tests(unittest.TestCase):
    def test_emits_four_immutable_semantic_records(self):
        req={"protocol":connector.PROTOCOL,"request_id":"r1","connector_key":connector.KEY,"connector_version":connector.VERSION,"operation":"sync","settings":{"api_base_url":"http://127.0.0.1:8793"},"resource_id":"takeaway_uniform","limit":10}
        events=connector.execute(req,{"api_token":"<token>"*4},Fake());self.assertEqual(len(events[:-1]),4)
        for event in events[:-1]:
            doc=json.loads(base64.b64decode(event["item"]["content_base64"]));self.assertNotIn("credential",str(doc).lower())
if __name__=="__main__":unittest.main()
