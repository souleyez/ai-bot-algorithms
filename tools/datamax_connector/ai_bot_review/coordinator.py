#!/usr/bin/env python3
"""Crash-safe automatic publication coordinator; no direct DataMax implementation."""
from __future__ import annotations
import hashlib,json,sqlite3
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
from tools.sample_review import review_revisions,visual_registry

def canonical(value:Any)->str:return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def now_epoch()->int:return int(datetime.now(timezone.utc).timestamp())

@dataclass(frozen=True)
class Budget:
    policy_ref:str;policy_digest:str;min_changes:int;min_interval:int;max_versions_day:int
    max_snapshot:int;max_monthly_growth:int;multiplier_ppm:int;fixed_bytes:int
    def projected(self,snapshot_bytes:int)->int:return self.fixed_bytes+(snapshot_bytes*self.multiplier_ppm//1_000_000)

def load_budget(stream:str,algorithm_key:str="")->Budget:
    root=visual_registry.DEFAULT_REGISTRY_ROOT
    if stream=="review_truth":
        entry=visual_registry.accepted_algorithms().get(algorithm_key)
        if entry is None:raise ValueError("algorithm is not accepted")
        target=entry.publication_policy_content_sha256
    elif stream=="raw_capture":target=visual_registry._load_json(root/"publication-policies"/"raw-capture-v1.json")["content_sha256"]
    else:raise ValueError("unsupported automatic stream")
    policies=[visual_registry._load_json(p) for p in (root/"publication-policies").glob("*.json")]
    policy=next((p for p in policies if p["content_sha256"]==target),None)
    if policy is None or policy["stream_kind"]!=stream:raise RuntimeError("publication policy mismatch")
    models=[visual_registry._load_json(p) for p in (root/"cost-models").glob("*.json")]
    model=next((m for m in models if m["content_sha256"]==policy["cost_model_content_sha256"]),None)
    if model is None:raise RuntimeError("cost model mismatch")
    multiplier=sum(int(model[name]) for name in ("evidence_multiplier_ppm","projection_multiplier_ppm","index_multiplier_ppm","wal_multiplier_ppm","backup_multiplier_ppm","staging_multiplier_ppm"))
    return Budget(policy["policy_id"],policy["content_sha256"],int(policy["min_eligible_changes"]),int(policy["min_interval_seconds"]),int(policy["max_versions_per_day"]),int(policy["max_snapshot_bytes"]),int(policy["max_monthly_postgres_growth_bytes"]),multiplier,int(model["fixed_bytes_per_snapshot"]))

SCHEMA="""
CREATE TABLE IF NOT EXISTS publication_state(stream TEXT,algorithm_key TEXT,watermark INTEGER NOT NULL DEFAULT 0,last_success_at INTEGER NOT NULL DEFAULT 0,versions_day TEXT NOT NULL DEFAULT '',versions_today INTEGER NOT NULL DEFAULT 0,monthly_projected_bytes INTEGER NOT NULL DEFAULT 0,policy_digest TEXT NOT NULL DEFAULT '',PRIMARY KEY(stream,algorithm_key));
CREATE TABLE IF NOT EXISTS publication_intents(idempotency_key TEXT PRIMARY KEY,stream TEXT NOT NULL,algorithm_key TEXT NOT NULL,source_id TEXT NOT NULL,watermark INTEGER NOT NULL,request_fingerprint TEXT NOT NULL,status TEXT NOT NULL,run_id TEXT NOT NULL DEFAULT '',snapshot_id TEXT NOT NULL DEFAULT '',source_version_id TEXT NOT NULL DEFAULT '',source_content_digest TEXT NOT NULL DEFAULT '',projected_bytes INTEGER NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL);
"""

class Coordinator:
    def __init__(self,database:Path,api:Any,datamax:Any,*,stream:str="review_truth",clock=now_epoch):
        self.database=database;self.api=api;self.datamax=datamax;self.stream=stream;self.clock=clock
        with self.connect() as db:db.executescript(SCHEMA)
    def connect(self):
        db=sqlite3.connect(self.database);db.row_factory=sqlite3.Row;return db
    def run(self,*,algorithm_key:str,source_id:str,execute:bool=False)->dict[str,Any]:
        budget=load_budget(self.stream,algorithm_key);watermark=self.api.watermark(algorithm_key if self.stream=="review_truth" else "")
        pending=int(watermark["pending_changes"]);marker=int(watermark["watermark"]);snapshot_bytes=int(watermark["estimated_snapshot_bytes"])
        projected=budget.projected(snapshot_bytes);now=self.clock();day=datetime.fromtimestamp(now,timezone.utc).date().isoformat()
        with self.connect() as db:
            state=db.execute("SELECT * FROM publication_state WHERE stream=? AND algorithm_key=?",(self.stream,algorithm_key)).fetchone()
            if state is None:
                db.execute("INSERT INTO publication_state(stream,algorithm_key,policy_digest) VALUES (?,?,?)",(self.stream,algorithm_key,budget.policy_digest));state=db.execute("SELECT * FROM publication_state WHERE stream=? AND algorithm_key=?",(self.stream,algorithm_key)).fetchone()
            existing=db.execute("SELECT * FROM publication_intents WHERE stream=? AND algorithm_key=? AND status NOT IN ('acknowledged','failed') ORDER BY created_at LIMIT 1",(self.stream,algorithm_key)).fetchone()
            if existing:return self._reconcile(db,existing,budget,day,now)
            reason="ready"
            versions=int(state["versions_today"]) if state["versions_day"]==day else 0
            if marker<=int(state["watermark"]):reason="watermark_unchanged"
            elif pending<budget.min_changes and now-int(state["last_success_at"])<budget.min_interval:reason="threshold_not_reached"
            elif versions>=budget.max_versions_day:reason="daily_version_budget"
            elif snapshot_bytes>budget.max_snapshot or int(state["monthly_projected_bytes"])+projected>budget.max_monthly_growth:reason="growth_budget"
            result={"status":"dry_run" if not execute else "blocked","reason":reason,"watermark":marker,"projected_bytes":projected,"policy_ref":budget.policy_ref,"policy_digest":budget.policy_digest}
            if reason!="ready" or not execute:return result
            fingerprint=hashlib.sha256(canonical({"stream":self.stream,"algorithm":algorithm_key,"source":source_id,"watermark":marker,"policy":budget.policy_digest}).encode()).hexdigest();key=f"aibot:{fingerprint}"
            db.execute("INSERT INTO publication_intents VALUES (?,?,?,?,?,?,'prepared','','','','',?,?,?)",(key,self.stream,algorithm_key,source_id,marker,fingerprint,projected,now,now));intent=db.execute("SELECT * FROM publication_intents WHERE idempotency_key=?",(key,)).fetchone()
            return self._reconcile(db,intent,budget,day,now)
    def _reconcile(self,db,row,budget,day,now):
        if not row["run_id"]:
            run_id=self.datamax.submit_source_run(source_id=row["source_id"],idempotency_key=row["idempotency_key"])
            db.execute("UPDATE publication_intents SET run_id=?,status='submitted',updated_at=? WHERE idempotency_key=?",(run_id,now,row["idempotency_key"]));row=db.execute("SELECT * FROM publication_intents WHERE idempotency_key=?",(row["idempotency_key"],)).fetchone()
        status=self.datamax.source_run_status(row["run_id"])
        if status["status"]!="succeeded":return {"status":status["status"],"run_id":row["run_id"],"idempotency_key":row["idempotency_key"]}
        receipt=(status["snapshot_id"],status["source_version_id"],status["source_content_digest"])
        stored=(row["snapshot_id"],row["source_version_id"],row["source_content_digest"])
        if any(stored) and stored!=receipt:raise RuntimeError("DataMax receipt conflict")
        db.execute("UPDATE publication_intents SET status='committed',snapshot_id=?,source_version_id=?,source_content_digest=?,updated_at=? WHERE idempotency_key=?",(*receipt,now,row["idempotency_key"]))
        self.api.acknowledge(algorithm_key=row["algorithm_key"],snapshot_id=receipt[0],source_version_id=receipt[1],source_content_digest=receipt[2],idempotency_key=row["idempotency_key"])
        state=db.execute("SELECT * FROM publication_state WHERE stream=? AND algorithm_key=?",(self.stream,row["algorithm_key"])).fetchone();versions=int(state["versions_today"]) if state["versions_day"]==day else 0
        db.execute("UPDATE publication_intents SET status='acknowledged',updated_at=? WHERE idempotency_key=?",(now,row["idempotency_key"]))
        db.execute("UPDATE publication_state SET watermark=?,last_success_at=?,versions_day=?,versions_today=?,monthly_projected_bytes=monthly_projected_bytes+?,policy_digest=? WHERE stream=? AND algorithm_key=?",(row["watermark"],now,day,versions+1,row["projected_bytes"],budget.policy_digest,self.stream,row["algorithm_key"]))
        return {"status":"acknowledged","run_id":row["run_id"],"source_version_id":receipt[1],"watermark":row["watermark"]}
