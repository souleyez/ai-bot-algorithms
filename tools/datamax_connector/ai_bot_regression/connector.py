#!/usr/bin/env python3
"""Manual managed-process connector for one immutable regression selection."""
from __future__ import annotations
import base64,json,os,sys
from typing import Any,Mapping
from urllib.parse import quote,urlencode,urlparse
from urllib.request import Request,build_opener,HTTPHandler
from urllib.error import HTTPError,URLError
PROTOCOL="managed_connector_process/v1";KEY="ai_bot_regression";VERSION="1.0.2"
class ConnectorError(RuntimeError):
    def __init__(self,code,retryable=False):super().__init__(code);self.code=code;self.retryable=retryable
def canonical(v:Any)->bytes:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
def valid_url(v):
    p=urlparse(v) if isinstance(v,str) else None
    if p is None or p.scheme!="http" or p.hostname not in {"127.0.0.1","::1","localhost"} or p.port!=8793 or p.path not in {"","/"} or p.query or p.fragment:raise ConnectorError("INVALID_CONFIGURATION")
    return v.rstrip("/")
class Transport:
    def __init__(self,url,token):self.url=valid_url(url);self.token=token;self.opener=build_opener(HTTPHandler())
    def request(self,path):
        if len(self.token)<24:raise ConnectorError("AUTHENTICATION_FAILED")
        try:
            with self.opener.open(Request(self.url+path,headers={"Authorization":f"Bearer {self.token}"}),timeout=30) as r:return json.loads(r.read())
        except HTTPError as e:
            if e.code in {401,403}:raise ConnectorError("AUTHENTICATION_FAILED") from e
            if e.code==404:raise ConnectorError("RESOURCE_NOT_FOUND") from e
            raise ConnectorError("REMOTE_UNAVAILABLE",e.code>=500) from e
        except (URLError,TimeoutError,json.JSONDecodeError) as e:raise ConnectorError("REMOTE_UNAVAILABLE",True) from e
def execute(request:Mapping[str,Any],credentials:Mapping[str,str],transport=None):
    if request.get("protocol")!=PROTOCOL or request.get("connector_key")!=KEY or request.get("connector_version")!=VERSION or request.get("operation") not in {"validate","discover","sample","sync"}:raise ConnectorError("INVALID_CONFIGURATION")
    settings=request.get("settings")
    if not isinstance(settings,dict) or set(settings)!={"api_base_url","algorithm_key","selection_id"}:raise ConnectorError("INVALID_CONFIGURATION")
    algorithm=settings.get("algorithm_key");selection_id=settings.get("selection_id")
    if not all(isinstance(x,str) and 0<len(x)<=128 for x in (algorithm,selection_id)):raise ConnectorError("INVALID_CONFIGURATION")
    if not isinstance(settings["api_base_url"],str) or not settings["api_base_url"]:raise ConnectorError("INVALID_CONFIGURATION")
    auth_value=credentials.get("api_token") if isinstance(credentials,Mapping) else None
    if not isinstance(auth_value,str) or not auth_value:raise ConnectorError("AUTHENTICATION_FAILED")
    rid=str(request.get("request_id",""));op=request["operation"]
    if op=="validate":return [{"protocol":PROTOCOL,"request_id":rid,"seq":1,"type":"complete","complete":{"resources_emitted":0,"items_emitted":0}}]
    if len(auth_value)<24:raise ConnectorError("AUTHENTICATION_FAILED")
    api_base_url=valid_url(settings["api_base_url"])
    client=transport or Transport(api_base_url,auth_value)
    if op=="discover":return [{"protocol":PROTOCOL,"request_id":rid,"seq":1,"type":"resource","resource":{"id":selection_id,"name":selection_id,"type":"manual_regression","selectable":True}},{"protocol":PROTOCOL,"request_id":rid,"seq":2,"type":"complete","complete":{"resources_emitted":1,"items_emitted":0}}]
    if request.get("resource_id")!=selection_id or not isinstance(request.get("limit"),int) or not 1<=request["limit"]<=500:raise ConnectorError("INVALID_CONFIGURATION")
    cursor=request.get("cursor") or {}
    if not isinstance(cursor,dict) or set(cursor)-{"api_cursor","selection_digest","total","emitted"}:raise ConnectorError("INVALID_CONFIGURATION")
    query=urlencode({"limit":request["limit"],**({"cursor":cursor["api_cursor"]} if cursor.get("api_cursor") else {})})
    payload=client.request(f"/api/internal/datamax/v1/algorithms/{quote(algorithm)}/regression-selections/{quote(selection_id)}?{query}")
    selection=payload.get("selection");facts=payload.get("items")
    if not isinstance(selection,dict) or not isinstance(facts,list) or selection.get("selection_id")!=selection_id or selection.get("algorithm_key")!=algorithm or len(facts)>request["limit"]:raise ConnectorError("REMOTE_UNAVAILABLE")
    if cursor and (selection.get("content_sha256")!=cursor.get("selection_digest") or selection.get("total")!=cursor.get("total")):raise ConnectorError("REMOTE_UNAVAILABLE")
    events=[]
    for seq,view in enumerate(facts,1):
        member=view.get("member") if isinstance(view,dict) else None
        fact=view.get("review_fact") if isinstance(view,dict) else None
        if not isinstance(member,dict) or not isinstance(fact,dict) or view.get("base_review_fact_digest")!=member.get("review_fact_digest") or fact.get("item_id")!=member.get("item_id") or fact.get("review_revision")!=member.get("review_revision") or fact.get("eligibility",{}).get("regression_roles")!=member.get("regression_roles"):raise ConnectorError("REMOTE_UNAVAILABLE")
        item=fact["item_id"];revision=fact["review_revision"]
        events.append({"protocol":PROTOCOL,"request_id":rid,"seq":seq,"type":"item","item":{"external_id":f"regression:{selection_id}:{item}:{revision}","title":f"{algorithm} regression {item} r{revision}","content_type":"application/json","content_base64":base64.b64encode(canonical(fact)).decode(),"metadata":{"source_locator":f"aibot-regression://{selection_id}/{item}/{revision}","selection_digest":selection["content_sha256"],"base_review_fact_digest":member["review_fact_digest"]}}})
    emitted=int(cursor.get("emitted",0))+len(events);complete={"resources_emitted":0,"items_emitted":len(events)};next_api=payload.get("next_cursor") or ""
    if next_api:complete["next_cursor"]={"api_cursor":next_api,"selection_digest":selection["content_sha256"],"total":selection["total"],"emitted":emitted}
    elif emitted!=selection["total"]:raise ConnectorError("REMOTE_UNAVAILABLE")
    events.append({"protocol":PROTOCOL,"request_id":rid,"seq":len(events)+1,"type":"complete","complete":complete});return events
def main():
    rid="invalid"
    try:
        req=json.load(sys.stdin);rid=str(req.get("request_id","invalid"))
        with os.fdopen(3,"r",encoding="utf-8",closefd=False) as h:cred=json.load(h)
        for event in execute(req,cred):print(json.dumps(event,ensure_ascii=False,separators=(",",":")))
        return 0
    except ConnectorError as e:print(json.dumps({"protocol":PROTOCOL,"request_id":rid,"seq":1,"type":"error","error":{"code":e.code,"retryable":e.retryable}},separators=(",",":")));return 1
if __name__=="__main__":raise SystemExit(main())
