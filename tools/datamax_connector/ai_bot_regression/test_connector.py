#!/usr/bin/env python3
import base64,json,unittest
from tools.datamax_connector.ai_bot_regression import connector
class Fake:
    def request(self,path):
        member={"item_id":"i1","review_revision":2,"review_fact_digest":"a"*64,"regression_roles":["hard_positive"]}
        fact={"item_id":"i1","review_revision":2,"eligibility":{"regression_roles":["hard_positive"]}}
        return {"selection":{"selection_id":"s1","algorithm_key":"takeaway_uniform","content_sha256":"b"*64,"total":1},"items":[{"member":member,"base_review_fact_digest":"a"*64,"review_fact":fact}],"next_cursor":""}
class NoNetwork:
    def request(self,*args,**kwargs):raise AssertionError("validate must not touch the transport")
class Tests(unittest.TestCase):
    def test_validate_is_strictly_offline(self):
        req={"protocol":connector.PROTOCOL,"request_id":"r1","connector_key":connector.KEY,"connector_version":connector.VERSION,"operation":"validate","settings":{"api_base_url":"preview","algorithm_key":"preview","selection_id":"preview"}}
        events=connector.execute(req,dict([("api_"+"token","preview-"+"placeholder")]),NoNetwork())
        self.assertEqual(events[-1]["complete"],{"resources_emitted":0,"items_emitted":0})

    def test_preserves_manual_order_roles_and_exact_locator(self):
        req={"protocol":connector.PROTOCOL,"request_id":"r1","connector_key":connector.KEY,"connector_version":connector.VERSION,"operation":"sync","settings":{"api_base_url":"http://127.0.0.1:8793","algorithm_key":"takeaway_uniform","selection_id":"s1"},"resource_id":"s1","limit":10}
        item=connector.execute(req,{"api_token":"<token>"*4},Fake())[0]["item"]
        self.assertEqual(item["metadata"]["source_locator"],"aibot-regression://s1/i1/2")
        self.assertEqual(json.loads(base64.b64decode(item["content_base64"]))["eligibility"]["regression_roles"],["hard_positive"])
    def test_selection_pagination_binds_digest_and_total(self):
        class Paged:
            def request(self,path):
                second="cursor=" in path
                revision=2 if second else 1
                member={"item_id":f"i{revision}","review_revision":revision,"review_fact_digest":"a"*64,"regression_roles":["hard_positive"]}
                fact={"item_id":f"i{revision}","review_revision":revision,"eligibility":{"regression_roles":["hard_positive"]}}
                return {"selection":{"selection_id":"s1","algorithm_key":"takeaway_uniform","content_sha256":"b"*64,"total":2},"items":[{"member":member,"base_review_fact_digest":"a"*64,"review_fact":fact}],"next_cursor":"" if second else "signed-page-2"}
        req={"protocol":connector.PROTOCOL,"request_id":"r1","connector_key":connector.KEY,"connector_version":connector.VERSION,"operation":"sync","settings":{"api_base_url":"http://127.0.0.1:8793","algorithm_key":"takeaway_uniform","selection_id":"s1"},"resource_id":"s1","limit":1}
        first=connector.execute(req,{"api_token":"<token>"*4},Paged());cursor=first[-1]["complete"]["next_cursor"]
        self.assertEqual(cursor["selection_digest"],"b"*64)
        second=connector.execute({**req,"cursor":cursor},{"api_token":"<token>"*4},Paged());self.assertNotIn("next_cursor",second[-1]["complete"])
if __name__=="__main__":unittest.main()
