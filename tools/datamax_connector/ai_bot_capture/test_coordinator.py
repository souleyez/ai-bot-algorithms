#!/usr/bin/env python3
import tempfile,unittest
from pathlib import Path
from tools.datamax_connector.ai_bot_capture.coordinator import Coordinator
class API:
    def __init__(self,estimated=1000):self.estimated=estimated;self.acks=[]
    def watermark(self,algorithm):return {"watermark":200,"pending_changes":200,"estimated_snapshot_bytes":self.estimated}
    def acknowledge(self,**receipt):self.acks.append(receipt)
class DataMax:
    def __init__(self):self.submits=[]
    def submit_source_run(self,**request):self.submits.append(request);return "run-capture"
    def source_run_status(self,run_id):return {"status":"succeeded","snapshot_id":"capture-s1","source_version_id":"capture-v1","source_content_digest":"c"*64}
class Tests(unittest.TestCase):
    def test_capture_uses_separate_policy_and_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            api=API();dm=DataMax();result=Coordinator(Path(directory)/"s.db",api,dm,clock=lambda:2_000_000_000).run(algorithm_key="capture",source_id="source-c",execute=True)
            self.assertEqual(result["status"],"acknowledged");self.assertEqual(len(api.acks),1)
    def test_growth_budget_fails_before_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            api=API(estimated=100_000_000);dm=DataMax();result=Coordinator(Path(directory)/"s.db",api,dm,clock=lambda:2_000_000_000).run(algorithm_key="capture",source_id="source-c",execute=True)
            self.assertEqual(result["reason"],"growth_budget");self.assertEqual(dm.submits,[])
if __name__=="__main__":unittest.main()
