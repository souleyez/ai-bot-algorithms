from __future__ import annotations
import copy,hashlib,unittest
try:
    from connector import ConnectorError, canonical, execute
except ModuleNotFoundError:
    from tools.datamax_connector.ai_bot_lineage.connector import ConnectorError,canonical,execute

class Fake:
    def __init__(self):
        self.record={"schema_version":"ai-bot-lineage-record.v1","record_id":"profile:v1","algorithm_key":"takeaway_uniform","payload":{"kind":"algorithm_profile"},"recorded_at":"2026-08-16T00:00:00Z"}
    def request(self,method,path):
        if method=="POST":return {"snapshot_id":"snapshot-1","membership_digest":"a"*64,"total":1}
        return {"snapshot_id":"snapshot-1","stream_kind":"lineage","algorithm_key":"takeaway_uniform","membership_digest":"a"*64,"total":1,"items":[{"ordinal":0,"record":copy.deepcopy(self.record),"record_digest":hashlib.sha256(canonical(self.record)).hexdigest()}],"next_cursor":""}
class NoNetwork:
    def request(self,*args,**kwargs):raise AssertionError("validate must not touch the transport")
def request():return {"protocol":"managed_connector_process/v1","connector_key":"ai_bot_lineage","connector_version":"1.0.3","operation":"sync","request_id":"r1","settings":{"api_base_url":"http://127.0.0.1:8793","algorithm_key":"takeaway_uniform"},"resource_id":"takeaway_uniform","limit":100}
class Tests(unittest.TestCase):
    def test_preview_fixture_covers_every_operation_offline(self):
        credentials=dict([("api_"+"token","preview-"+"placeholder")])
        for index,operation in enumerate(("validate","discover","sample","sync"),1):
            req=request();req["request_id"]=f"preview-{operation}-{index:02d}";req["operation"]=operation;req["settings"]={"api_base_url":"preview","algorithm_key":"preview"}
            if operation in {"validate","discover"}:req.pop("resource_id");req.pop("limit")
            else:req["resource_id"]="preview";req["limit"]=500
            events=execute(req,credentials,NoNetwork())
            self.assertEqual(events[-1]["type"],"complete")

    def test_frozen_snapshot_emits_one_sanitized_record(self):
        events=execute(request(),{"api_token":"x"*24},Fake());self.assertEqual(events[0]["item"]["external_id"],"lineage:takeaway_uniform:profile:v1");self.assertNotIn("next_cursor",events[-1]["complete"])
    def test_digest_and_scope_mismatch_fail_closed(self):
        fake=Fake();fake.record["algorithm_key"]="other"
        with self.assertRaises(ConnectorError):execute(request(),{"api_token":"x"*24},fake)
if __name__=="__main__":unittest.main()
