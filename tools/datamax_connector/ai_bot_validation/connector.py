#!/usr/bin/env python3
"""Managed connector for frozen AI-BOT validation evidence snapshots."""
from __future__ import annotations
import base64,hashlib,json,os,sys
from typing import Any,Mapping
from urllib.error import HTTPError,URLError
from urllib.parse import quote,urlencode,urlparse
from urllib.request import HTTPHandler,Request,build_opener
PROTOCOL="managed_connector_process/v1";KEY="ai_bot_validation";VERSION="1.0.1";STREAM="validation";SCHEMA="ai-bot-validation-record.v1";MAX_LIMIT=500
class ConnectorError(RuntimeError):
    def __init__(self,code:str,retryable:bool=False):super().__init__(code);self.code=code;self.retryable=retryable
def canonical(value:Any)->bytes:return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
def valid_url(value:object)->str:
    parsed=urlparse(value) if isinstance(value,str) else None
    if parsed is None or parsed.scheme!="http" or parsed.hostname not in {"127.0.0.1","::1","localhost"} or parsed.port!=8793 or parsed.path not in {"","/"} or parsed.query or parsed.fragment:raise ConnectorError("INVALID_CONFIGURATION")
    return str(value).rstrip("/")
class Transport:
    def __init__(self,base_url:str,token:str):self.base_url=valid_url(base_url);self.token=token;self.opener=build_opener(HTTPHandler())
    def request(self,method:str,path:str)->dict[str,Any]:
        if len(self.token)<24:raise ConnectorError("AUTHENTICATION_FAILED")
        try:
            with self.opener.open(Request(self.base_url+path,method=method,headers={"Authorization":f"Bearer {self.token}"}),timeout=30) as response:
                value=json.loads(response.read())
                if not isinstance(value,dict):raise ConnectorError("REMOTE_UNAVAILABLE")
                return value
        except HTTPError as exc:
            if exc.code in {401,403}:raise ConnectorError("AUTHENTICATION_FAILED") from exc
            if exc.code==404:raise ConnectorError("RESOURCE_NOT_FOUND") from exc
            if exc.code==429:raise ConnectorError("RATE_LIMITED",True) from exc
            raise ConnectorError("REMOTE_UNAVAILABLE",exc.code>=500) from exc
        except (URLError,TimeoutError,json.JSONDecodeError) as exc:raise ConnectorError("REMOTE_UNAVAILABLE",True) from exc
def execute(request:Mapping[str,Any],credentials:Mapping[str,str],transport:Any|None=None)->list[dict[str,Any]]:
    if request.get("protocol")!=PROTOCOL or request.get("connector_key")!=KEY or request.get("connector_version")!=VERSION or request.get("operation") not in {"validate","discover","sample","sync"}:raise ConnectorError("INVALID_CONFIGURATION")
    settings=request.get("settings")
    if not isinstance(settings,dict) or set(settings)!={"api_base_url","algorithm_key"}:raise ConnectorError("INVALID_CONFIGURATION")
    algorithm=settings.get("algorithm_key")
    if not isinstance(algorithm,str) or not algorithm or len(algorithm)>64:raise ConnectorError("INVALID_CONFIGURATION")
    base_url=valid_url(settings["api_base_url"]);token=credentials.get("api_token") if isinstance(credentials,Mapping) else None
    if not isinstance(token,str) or len(token)<24:raise ConnectorError("AUTHENTICATION_FAILED")
    request_id=str(request.get("request_id",""));operation=request["operation"]
    snapshot_path=f"/api/internal/datamax/v1/evidence/{STREAM}/algorithms/{quote(algorithm)}/snapshots"
    if operation=="validate":
        return [{"protocol":PROTOCOL,"request_id":request_id,"seq":1,"type":"complete","complete":{"resources_emitted":0,"items_emitted":0}}]
    client=transport or Transport(base_url,token)
    if operation=="discover":
        client.request("POST",snapshot_path);return [{"protocol":PROTOCOL,"request_id":request_id,"seq":1,"type":"resource","resource":{"id":algorithm,"name":f"{algorithm} validation","type":"validation_evidence","selectable":True}},{"protocol":PROTOCOL,"request_id":request_id,"seq":2,"type":"complete","complete":{"resources_emitted":1,"items_emitted":0}}]
    if request.get("resource_id")!=algorithm or not isinstance(request.get("limit"),int) or not 1<=request["limit"]<=MAX_LIMIT:raise ConnectorError("INVALID_CONFIGURATION")
    cursor=request.get("cursor") or {}
    if not isinstance(cursor,dict) or set(cursor)-{"snapshot_id","membership_digest","api_cursor","total","emitted"}:raise ConnectorError("INVALID_CONFIGURATION")
    snapshot=cursor or client.request("POST",snapshot_path);extra={"limit":request["limit"]}
    if snapshot.get("api_cursor"):extra["cursor"]=snapshot["api_cursor"]
    page=client.request("GET",f"{snapshot_path}/{quote(str(snapshot['snapshot_id']))}/records?{urlencode(extra)}")
    if page.get("stream_kind")!=STREAM or page.get("algorithm_key")!=algorithm or page.get("membership_digest")!=snapshot.get("membership_digest") or page.get("total")!=snapshot.get("total"):raise ConnectorError("REMOTE_UNAVAILABLE")
    events=[]
    for item in page.get("items",[]):
        record=item.get("record") if isinstance(item,dict) else None;payload=canonical(record)
        if not isinstance(record,dict) or record.get("schema_version")!=SCHEMA or record.get("algorithm_key")!=algorithm or hashlib.sha256(payload).hexdigest()!=item.get("record_digest"):raise ConnectorError("REMOTE_UNAVAILABLE")
        record_id=record.get("record_id")
        if not isinstance(record_id,str):raise ConnectorError("REMOTE_UNAVAILABLE")
        events.append({"protocol":PROTOCOL,"request_id":request_id,"seq":len(events)+1,"type":"item","item":{"external_id":f"validation:{algorithm}:{record_id}","title":f"{algorithm} {record['payload']['kind']}","content_type":"application/json","content_base64":base64.b64encode(payload).decode("ascii"),"metadata":{"source_locator":f"aibot-validation://{algorithm}/{record_id}","record_digest":item["record_digest"]}}})
    emitted=int(snapshot.get("emitted",0))+len(events);next_api=page.get("next_cursor") or "";complete:dict[str,Any]={"resources_emitted":0,"items_emitted":len(events)}
    if next_api:complete["next_cursor"]={"snapshot_id":snapshot["snapshot_id"],"membership_digest":snapshot["membership_digest"],"api_cursor":next_api,"total":snapshot["total"],"emitted":emitted}
    elif emitted!=int(snapshot["total"]):raise ConnectorError("REMOTE_UNAVAILABLE")
    events.append({"protocol":PROTOCOL,"request_id":request_id,"seq":len(events)+1,"type":"complete","complete":complete});return events
def main()->int:
    request_id="invalid"
    try:
        request=json.load(sys.stdin);request_id=str(request.get("request_id","invalid"))
        with os.fdopen(3,"r",encoding="utf-8",closefd=False) as handle:credentials=json.load(handle)
        for event in execute(request,credentials):print(json.dumps(event,ensure_ascii=False,separators=(",",":")))
        return 0
    except ConnectorError as exc:
        print(json.dumps({"protocol":PROTOCOL,"request_id":request_id,"seq":1,"type":"error","error":{"code":exc.code,"retryable":exc.retryable}},separators=(",",":")));return 1
if __name__=="__main__":raise SystemExit(main())
