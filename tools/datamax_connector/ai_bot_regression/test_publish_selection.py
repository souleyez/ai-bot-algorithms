#!/usr/bin/env python3
import tempfile,unittest
from pathlib import Path
from tools.datamax_connector.ai_bot_regression.publish_selection import Publisher
class API:
    def selection(self,algorithm,selection):return {"selection_id":selection,"algorithm_key":algorithm,"content_sha256":"a"*64}
class DM:
    def __init__(self):self.calls=[];self.status="running"
    def submit_source_run(self,**request):self.calls.append(request);return "run-1"
    def source_run_status(self,run):return {"status":self.status,"source_version_id":"sv1","source_content_digest":"b"*64}
class Tests(unittest.TestCase):
    def test_requires_execute_and_approval_then_reconciles_without_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            dm=DM();p=Publisher(Path(d)/"s.db",API(),dm)
            dry=p.publish(algorithm_key="takeaway_uniform",selection_id="s1",selection_digest="a"*64,source_id="source-1",approval_ref="approval-1",execute=False);self.assertEqual(dry["status"],"dry_run");self.assertEqual(dm.calls,[])
            pending=p.publish(algorithm_key="takeaway_uniform",selection_id="s1",selection_digest="a"*64,source_id="source-1",approval_ref="approval-1",execute=True);self.assertEqual(pending["status"],"running")
            dm.status="succeeded";done=p.publish(algorithm_key="takeaway_uniform",selection_id="s1",selection_digest="a"*64,source_id="source-1",approval_ref="approval-1",execute=True);self.assertEqual(done["status"],"succeeded");self.assertEqual(len(dm.calls),1)
    def test_digest_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):Publisher(Path(d)/"s.db",API(),DM()).publish(algorithm_key="takeaway_uniform",selection_id="s1",selection_digest="c"*64,source_id="source-1",approval_ref="approval-1",execute=True)
if __name__=="__main__":unittest.main()
