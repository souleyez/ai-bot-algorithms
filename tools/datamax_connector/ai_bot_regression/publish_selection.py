#!/usr/bin/env python3
"""Explicit, approval-bound publication of one immutable regression selection."""
from __future__ import annotations
import hashlib,json,sqlite3
from pathlib import Path
from typing import Any
def canonical(v:Any)->str:return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False)
SCHEMA="""CREATE TABLE IF NOT EXISTS regression_publication_intents(idempotency_key TEXT PRIMARY KEY,selection_id TEXT NOT NULL,selection_digest TEXT NOT NULL,source_id TEXT NOT NULL,approval_ref TEXT NOT NULL,request_fingerprint TEXT NOT NULL,status TEXT NOT NULL,run_id TEXT NOT NULL DEFAULT '',source_version_id TEXT NOT NULL DEFAULT '',source_content_digest TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"""
class Publisher:
    def __init__(self,database:Path,api:Any,datamax:Any):
        self.database=database;self.api=api;self.datamax=datamax
        with self.connect() as db:db.executescript(SCHEMA)
    def connect(self):db=sqlite3.connect(self.database);db.row_factory=sqlite3.Row;return db
    def publish(self,*,algorithm_key:str,selection_id:str,selection_digest:str,source_id:str,approval_ref:str,execute:bool=False):
        if not all(isinstance(v,str) and v and len(v)<=160 for v in (algorithm_key,selection_id,source_id,approval_ref)) or not isinstance(selection_digest,str) or len(selection_digest)!=64:raise ValueError("exact selection, source, and approval are required")
        selection=self.api.selection(algorithm_key,selection_id)
        if selection.get("content_sha256")!=selection_digest:raise ValueError("selection digest mismatch")
        fingerprint=hashlib.sha256(canonical({"algorithm":algorithm_key,"selection":selection_id,"digest":selection_digest,"source":source_id,"approval":approval_ref}).encode()).hexdigest();key=f"regression:{fingerprint}"
        if not execute:return {"status":"dry_run","idempotency_key":key,"selection_id":selection_id,"selection_digest":selection_digest,"approval_ref":approval_ref}
        with self.connect() as db:
            row=db.execute("SELECT * FROM regression_publication_intents WHERE idempotency_key=?",(key,)).fetchone()
            if row is None:
                db.execute("INSERT INTO regression_publication_intents(idempotency_key,selection_id,selection_digest,source_id,approval_ref,request_fingerprint,status) VALUES (?,?,?,?,?,?,'prepared')",(key,selection_id,selection_digest,source_id,approval_ref,fingerprint));row=db.execute("SELECT * FROM regression_publication_intents WHERE idempotency_key=?",(key,)).fetchone()
            if not row["run_id"]:
                run_id=self.datamax.submit_source_run(source_id=source_id,idempotency_key=key)
                db.execute("UPDATE regression_publication_intents SET run_id=?,status='submitted',updated_at=CURRENT_TIMESTAMP WHERE idempotency_key=?",(run_id,key));row=db.execute("SELECT * FROM regression_publication_intents WHERE idempotency_key=?",(key,)).fetchone()
            result=self.datamax.source_run_status(row["run_id"])
            if result["status"]!="succeeded":return {"status":result["status"],"run_id":row["run_id"],"idempotency_key":key}
            receipt=(result["source_version_id"],result["source_content_digest"]);stored=(row["source_version_id"],row["source_content_digest"])
            if any(stored) and stored!=receipt:raise RuntimeError("publication receipt conflict")
            db.execute("UPDATE regression_publication_intents SET status='succeeded',source_version_id=?,source_content_digest=?,updated_at=CURRENT_TIMESTAMP WHERE idempotency_key=?",(*receipt,key))
            return {"status":"succeeded","run_id":row["run_id"],"source_version_id":receipt[0],"source_content_digest":receipt[1],"idempotency_key":key}
