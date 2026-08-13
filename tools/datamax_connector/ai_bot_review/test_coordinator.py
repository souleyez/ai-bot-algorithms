#!/usr/bin/env python3
import tempfile,unittest
from pathlib import Path
from tools.datamax_connector.ai_bot_review.coordinator import Coordinator
class API:
    def __init__(self):self.marker=20;self.acks=[]
    def watermark(self,algorithm):return {"watermark":self.marker,"pending_changes":20,"estimated_snapshot_bytes":1000}
    def acknowledge(self,**receipt):self.acks.append(receipt)
class DataMax:
    def __init__(self):self.submits=[];self.status="succeeded"
    def submit_source_run(self,**request):self.submits.append(request);return "run-1"
    def source_run_status(self,run_id):return {"status":self.status,"snapshot_id":"snapshot-1","source_version_id":"source-v1","source_content_digest":"d"*64}
class Tests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.api=API();self.datamax=DataMax();self.clock=lambda:2_000_000_000;self.coordinator=Coordinator(Path(self.temp.name)/"state.sqlite",self.api,self.datamax,clock=self.clock)
    def tearDown(self):self.temp.cleanup()
    def test_unchanged_watermark_does_not_submit_again(self):
        first=self.coordinator.run(algorithm_key="takeaway_uniform",source_id="source-1",execute=True);self.assertEqual(first["status"],"acknowledged")
        second=self.coordinator.run(algorithm_key="takeaway_uniform",source_id="source-1",execute=True);self.assertEqual(second["reason"],"watermark_unchanged");self.assertEqual(len(self.datamax.submits),1)
    def test_crash_after_remote_success_reconciles_same_run_without_resubmit(self):
        self.datamax.status="running";first=self.coordinator.run(algorithm_key="takeaway_uniform",source_id="source-1",execute=True);self.assertEqual(first["status"],"running")
        self.datamax.status="succeeded";second=self.coordinator.run(algorithm_key="takeaway_uniform",source_id="source-1",execute=True);self.assertEqual(second["status"],"acknowledged");self.assertEqual(len(self.datamax.submits),1);self.assertEqual(len(self.api.acks),1)
    def test_dry_run_has_no_mutation(self):
        result=self.coordinator.run(algorithm_key="takeaway_uniform",source_id="source-1",execute=False);self.assertEqual(result["status"],"dry_run");self.assertEqual(self.datamax.submits,[])
if __name__=="__main__":unittest.main()
