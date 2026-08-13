#!/usr/bin/env python3
"""Managed-process connector for immutable visual semantic contracts."""
from __future__ import annotations
import base64, json, os, sys
from typing import Any, Mapping
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, HTTPHandler
from urllib.error import HTTPError, URLError

PROTOCOL="managed_connector_process/v1"; KEY="ai_bot_visual_semantics"; VERSION="1.0.0"
KINDS=("visual_semantics","task_profile","taxonomy","review_policy")

class ConnectorError(RuntimeError):
    def __init__(self, code, retryable=False): super().__init__(code); self.code=code; self.retryable=retryable
def canonical(v:Any)->bytes: return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
def base(value):
    p=urlparse(value) if isinstance(value,str) else None
    if p is None or p.scheme!="http" or p.hostname not in {"127.0.0.1","::1","localhost"} or p.port!=8792 or p.path not in {"","/"} or p.query or p.fragment: raise ConnectorError("INVALID_CONFIGURATION")
    return value.rstrip("/")
class Transport:
    def __init__(self,url,token):
        self.url=base(url); self.token=token; self.opener=build_opener(HTTPHandler())
        if len(token)<24: raise ConnectorError("AUTHENTICATION_FAILED")
    def request(self,path):
        try:
            with self.opener.open(Request(self.url+path,headers={"Authorization":f"Bearer {self.token}"}),timeout=30) as r:return json.loads(r.read())
        except HTTPError as e:
            if e.code in {401,403}: raise ConnectorError("AUTHENTICATION_FAILED") from e
            raise ConnectorError("REMOTE_UNAVAILABLE",e.code>=500) from e
        except (URLError,TimeoutError,json.JSONDecodeError) as e: raise ConnectorError("REMOTE_UNAVAILABLE",True) from e
def execute(request:Mapping[str,Any],credentials:Mapping[str,str],transport=None):
    if request.get("protocol")!=PROTOCOL or request.get("connector_key")!=KEY or request.get("connector_version")!=VERSION or request.get("operation") not in {"validate","discover","sample","sync"}: raise ConnectorError("INVALID_CONFIGURATION")
    settings=request.get("settings")
    if not isinstance(settings,dict) or set(settings)!={"api_base_url"}: raise ConnectorError("INVALID_CONFIGURATION")
    client=transport or Transport(base(settings["api_base_url"]),str(credentials.get("api_token",""))); rid=str(request.get("request_id","")); op=request["operation"]
    catalog=client.request("/api/internal/datamax/v1/algorithms"); accepted=[a for a in catalog.get("algorithms",[]) if a.get("onboarding_state")=="accepted"]
    if op=="validate": return [{"protocol":PROTOCOL,"request_id":rid,"seq":1,"type":"complete","complete":{"resources_emitted":0,"items_emitted":0}}]
    if op=="discover":
        events=[{"protocol":PROTOCOL,"request_id":rid,"seq":i+1,"type":"resource","resource":{"id":a["algorithm_key"],"name":a["display_name"],"type":"visual_semantics","selectable":True}} for i,a in enumerate(accepted)]
        events.append({"protocol":PROTOCOL,"request_id":rid,"seq":len(events)+1,"type":"complete","complete":{"resources_emitted":len(events),"items_emitted":0}}); return events
    algorithm=request.get("resource_id")
    if algorithm not in {a["algorithm_key"] for a in accepted}: raise ConnectorError("RESOURCE_NOT_FOUND")
    if request.get("cursor") or not isinstance(request.get("limit"),int) or request["limit"]<4: raise ConnectorError("INVALID_CONFIGURATION")
    docs=client.request(f"/api/internal/datamax/v1/algorithms/{quote(algorithm)}/visual-semantics")
    events=[]
    forbidden=("credential","secret","token","api_key","spend_counter","database_row")
    for seq,kind in enumerate(KINDS,1):
        document=docs.get(kind)
        if not isinstance(document,dict) or any(word in canonical(document).decode().lower() for word in forbidden): raise ConnectorError("REMOTE_UNAVAILABLE")
        digest=document.get("content_sha256"); identity=next((document.get(k) for k in ("bundle_id","profile_id","taxonomy_version_id","policy_id") if document.get(k)),None)
        if not isinstance(digest,str) or len(digest)!=64 or not isinstance(identity,str): raise ConnectorError("REMOTE_UNAVAILABLE")
        events.append({"protocol":PROTOCOL,"request_id":rid,"seq":seq,"type":"item","item":{"external_id":f"visual-semantics:{kind}:{identity}","title":f"{algorithm} {kind}","content_type":"application/json","content_base64":base64.b64encode(canonical(document)).decode(),"metadata":{"source_locator":f"aibot-semantics://{kind}/{identity}","content_sha256":digest}}})
    events.append({"protocol":PROTOCOL,"request_id":rid,"seq":5,"type":"complete","complete":{"resources_emitted":0,"items_emitted":4}}); return events
def main():
    rid="invalid"
    try:
        req=json.load(sys.stdin);rid=str(req.get("request_id","invalid"))
        with os.fdopen(3,"r",encoding="utf-8",closefd=False) as h:cred=json.load(h)
        for event in execute(req,cred):print(json.dumps(event,ensure_ascii=False,separators=(",",":")))
        return 0
    except ConnectorError as e:
        print(json.dumps({"protocol":PROTOCOL,"request_id":rid,"seq":1,"type":"error","error":{"code":e.code,"retryable":e.retryable}},separators=(",",":")));return 1
if __name__=="__main__":raise SystemExit(main())
