#!/usr/bin/env python3
"""Replay reviewed positive captures using the AI box HTTP report contract."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

try:
    from . import oss_backend
except ImportError:
    import oss_backend


DEFAULT_DATABASE = Path("/app/data/review.sqlite3")
DEFAULT_METADATA = Path("/app/report-replay/box-metadata.json")
DEFAULT_STATE_DIR = Path("/app/report-replay/state")
DEFAULT_IDENTIFIER_CONFIG = Path("/app/report-replay/report-identifiers.json")
DEFAULT_ENABLE_FILE = Path("/app/report-replay/ENABLE_SEND")
DEFAULT_ENDPOINT = "https://aibot.nwcl.com.cn/prod-api/third/aiboxall/report"
SOURCE_TO_GEID = {"workwear": 103, "takeaway": 104}
SUCCESS_CODES = {0, 200}
FILENAME_PATTERN = re.compile(r"^ch(?P<channel>\d+)_m(?P<geid>\d+)_\d+\.[^.]+$", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "canary", "send", "status"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--identifier-config", type=Path, default=DEFAULT_IDENTIFIER_CONFIG)
    parser.add_argument("--enable-file", type=Path, default=DEFAULT_ENABLE_FILE)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--source-kind", choices=("all", "workwear", "takeaway"), default="all"
    )
    parser.add_argument(
        "--selection",
        choices=("reviewed-positive", "ai-positive"),
        default="reviewed-positive",
    )
    return parser.parse_args()


def json_dump(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def parse_json_object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def normalized_detects(raw: str) -> list[dict[str, Any]]:
    try:
        value = parse_json_object(raw or "{}")
    except (ValueError, json.JSONDecodeError):
        return []
    output = []
    for item in value.get("detects") or []:
        if not isinstance(item, dict):
            continue
        try:
            confidence = float(item["conf"])
            class_id = int(item["class"])
            coordinates = [float(item[key]) for key in ("x1", "y1", "x2", "y2")]
        except (KeyError, TypeError, ValueError):
            continue
        x1, y1, x2, y2 = coordinates
        if not (0 <= confidence <= 1 and 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            continue
        output.append(
            {
                "conf": confidence,
                "gcid": class_id,
                "aid": 0,
                "cid": class_id,
                "class_name": str(item.get("class_name") or ""),
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            }
        )
    return output


def load_metadata(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    devices: dict[str, Any] = {}
    for device in data.get("devices") or []:
        device_id = str(device["device"])
        channels = {
            int(row["chNo"]): row
            for row in device.get("channels") or []
            if row.get("chNo") is not None
        }
        capture_index: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for row in device.get("capture_rows") or []:
            try:
                key = (int(row["geid"]), str(row["picName"]))
            except (KeyError, TypeError, ValueError):
                continue
            capture_index.setdefault(key, []).append(row)
        devices[device_id] = {
            "machine_code": str(device.get("configured_machine_code") or ""),
            "channels": channels,
            "captures": capture_index,
        }
    return devices


def load_identifiers(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise RuntimeError("report identifier configuration is missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("status") != "approved":
        raise RuntimeError("report identifier configuration is not approved")
    identifiers = data.get("identifiers")
    if not isinstance(identifiers, dict):
        raise RuntimeError("report identifiers are missing")
    result: dict[str, dict[str, Any]] = {}
    used_geids: set[int] = set()
    for source_kind, source_geid in SOURCE_TO_GEID.items():
        item = identifiers.get(source_kind)
        if not isinstance(item, dict):
            raise RuntimeError(f"identifier for {source_kind} is missing")
        report_geid = int(item.get("report_geid"))
        report_gcid = int(item.get("report_gcid"))
        class_name = str(item.get("class_name") or "").strip()
        if int(item.get("source_geid")) != source_geid:
            raise RuntimeError(f"source GEID mismatch for {source_kind}")
        if report_geid in SOURCE_TO_GEID.values() or report_geid in used_geids:
            raise RuntimeError(f"report GEID is not isolated for {source_kind}")
        if report_gcid != report_geid << 8:
            raise RuntimeError(f"report GCID must equal GEID << 8 for {source_kind}")
        if not class_name or len(class_name) > 80:
            raise RuntimeError(f"invalid class name for {source_kind}")
        used_geids.add(report_geid)
        result[source_kind] = {
            "source_geid": source_geid,
            "report_geid": report_geid,
            "report_gcid": report_gcid,
            "class_name": class_name,
        }
    return result


def read_positive_rows(
    database: Path,
    source_kind: str,
    selection: str,
    *,
    minimum_source_mtime: int = 0,
    minimum_ai_labeled_at: str = "",
) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        filters = [
            "source_kind IN ('workwear', 'takeaway')",
            "source_device IN ('61672', '61863')",
            "source_mtime > 0",
            "storage_backend = 'oss'",
            "object_key != ''",
        ]
        parameters: list[Any] = []
        if minimum_source_mtime > 0:
            filters.append("source_mtime > ?")
            parameters.append(minimum_source_mtime)
        if minimum_ai_labeled_at:
            filters.append("ai_labeled_at > ?")
            parameters.append(minimum_ai_labeled_at)
        if source_kind != "all":
            filters.append("source_kind = ?")
            parameters.append(source_kind)
        if selection == "ai-positive":
            filters.extend(
                [
                    "ai_decision = 'positive'",
                    "ai_annotations IS NOT NULL",
                    "TRIM(ai_annotations) NOT IN ('', '[]', '{}', 'null')",
                    "(human_reviewed = 0 OR decision = 'positive')",
                ]
            )
        else:
            filters.append(
                "((human_reviewed = 1 AND decision = 'positive') "
                "OR (human_reviewed = 0 AND ai_decision = 'positive'))"
            )
        return [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT id, filename, source_image, source_kind, source_device,
                       source_mtime, sha256, object_key, object_sha256,
                       storage_backend, decision, annotations, ai_decision,
                       ai_annotations, ai_model, ai_confidence,
                       ai_labeled_at, human_reviewed, human_reviewed_at, updated_at
                FROM items
                WHERE {' AND '.join(filters)}
                ORDER BY source_mtime, id
                """,
                parameters,
            )
        ]
    finally:
        connection.close()


def best_capture(
    captures: list[dict[str, Any]], source_mtime: int
) -> tuple[dict[str, Any] | None, int | None]:
    valid = [row for row in captures if normalized_detects(str(row.get("detects") or ""))]
    if not valid:
        return None, None
    selected = min(valid, key=lambda row: abs(int(row.get("timeStamp") or 0) - source_mtime))
    delta = abs(int(selected.get("timeStamp") or 0) - source_mtime)
    return selected, delta


def enrich_rows(
    rows: list[dict[str, Any]],
    devices: dict[str, Any],
    identifiers: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    enriched = []
    counters = {
        "source_positive_rows": len(rows),
        "invalid_filename": 0,
        "missing_device": 0,
        "missing_capture": 0,
        "capture_time_mismatch": 0,
        "capture_delta_over_5m": 0,
        "missing_channel_metadata": 0,
        "invalid_ai_annotations": 0,
    }
    for row in rows:
        filenames = [Path(row["filename"]).name]
        source_image = str(row.get("source_image") or "")
        if source_image:
            source_path = source_image.split(":", 1)[-1]
            source_name = Path(source_path).name
            if source_name and source_name not in filenames:
                filenames.append(source_name)
        event_filename = next(
            (name for name in filenames if FILENAME_PATTERN.match(name)), ""
        )
        match = FILENAME_PATTERN.match(event_filename)
        if not match:
            counters["invalid_filename"] += 1
            continue
        channel = int(match.group("channel"))
        geid = int(match.group("geid"))
        if SOURCE_TO_GEID.get(row["source_kind"]) != geid:
            counters["invalid_filename"] += 1
            continue
        device = devices.get(str(row["source_device"]))
        if not device:
            counters["missing_device"] += 1
            continue
        capture, delta = best_capture(
            device["captures"].get((geid, event_filename), []),
            int(row["source_mtime"]),
        )
        if capture is None:
            counters["missing_capture"] += 1
            continue
        if delta is not None and delta > 300:
            counters["capture_delta_over_5m"] += 1
        channel_data = device["channels"].get(channel)
        if channel_data is None:
            counters["missing_channel_metadata"] += 1
            channel_data = {"chNo": channel, "location": "", "desc": "", "ip": ""}
        candidate = {
                "item_id": row["id"],
                "sha256": row["sha256"],
                "object_key": row["object_key"],
                "object_sha256": row["object_sha256"],
                "source_kind": row["source_kind"],
                "device": str(row["source_device"]),
                "machine_code": device["machine_code"],
                "channel": channel,
                "channel_metadata": channel_data,
                "geid": geid,
                "report_geid": identifiers[row["source_kind"]]["report_geid"],
                "report_gcid": identifiers[row["source_kind"]]["report_gcid"],
                "report_class_name": identifiers[row["source_kind"]]["class_name"],
                "capture": capture,
                "capture_delta_seconds": delta,
                "human_reviewed": bool(row["human_reviewed"]),
                "ai_model": row["ai_model"],
                "ai_confidence": row["ai_confidence"],
                "ai_annotations": row["ai_annotations"],
                "source_mtime": int(row["source_mtime"]),
            }
        if not normalized_ai_annotations(candidate):
            counters["invalid_ai_annotations"] += 1
            continue
        enriched.append(candidate)
    return enriched, counters


def deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = row["sha256"] or row["item_id"]
        grouped.setdefault(key, []).append(row)
    selected = []
    for values in grouped.values():
        selected.append(
            max(
                values,
                key=lambda row: (
                    int(row["human_reviewed"]),
                    -int(row["capture_delta_seconds"]),
                    int(row["source_mtime"]),
                    row["item_id"],
                ),
            )
        )
    return sorted(selected, key=lambda row: (row["source_mtime"], row["item_id"]))


def prepare(
    args: argparse.Namespace,
    exclude_item_ids: set[str] | None = None,
    exclude_image_sha256: set[str] | None = None,
) -> dict[str, Any]:
    if not args.database.is_file() or not args.metadata.is_file():
        raise FileNotFoundError("database or metadata file is missing")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.state_dir, 0o700)
    rows = read_positive_rows(
        args.database,
        args.source_kind,
        args.selection,
        minimum_source_mtime=int(getattr(args, "minimum_source_mtime", 0) or 0),
        minimum_ai_labeled_at=str(getattr(args, "minimum_ai_labeled_at", "") or ""),
    )
    identifiers = load_identifiers(args.identifier_config)
    enriched, counters = enrich_rows(rows, load_metadata(args.metadata), identifiers)
    deduplicated = deduplicate(enriched)
    deduplicated_before_exclusion = len(deduplicated)
    excluded = exclude_item_ids or set()
    excluded_hashes = exclude_image_sha256 or set()
    already_reported = sum(
        1
        for item in deduplicated
        if item["item_id"] in excluded or item.get("sha256") in excluded_hashes
    )
    deduplicated = [
        item
        for item in deduplicated
        if item["item_id"] not in excluded and item.get("sha256") not in excluded_hashes
    ]
    summary = {
        **counters,
        "matched_rows": len(enriched),
        "deduplicated_items": len(deduplicated),
        "ai_boxes": sum(len(normalized_ai_annotations(item)) for item in deduplicated),
        "duplicate_rows_removed": len(enriched) - deduplicated_before_exclusion,
        "already_reported": already_reported,
        "by_device_algorithm": {},
        "source_kind_filter": args.source_kind,
        "selection": args.selection,
        "prepared_at": utc_now(),
        "minimum_source_mtime": int(getattr(args, "minimum_source_mtime", 0) or 0),
        "minimum_ai_labeled_at": str(getattr(args, "minimum_ai_labeled_at", "") or ""),
    }
    for row in deduplicated:
        key = f"{row['device']}|m{row['geid']}"
        summary["by_device_algorithm"][key] = summary["by_device_algorithm"].get(key, 0) + 1
    manifest = {
        "schema": "ai-bot-reviewed-report-replay-v1",
        "endpoint": args.endpoint,
        "reporting_identifiers": identifiers,
        "summary": summary,
        "items": deduplicated,
    }
    json_dump(args.state_dir / "manifest.json", manifest)
    return summary


def manifest(args: argparse.Namespace) -> dict[str, Any]:
    path = args.state_dir / "manifest.json"
    if not path.is_file():
        raise RuntimeError("manifest is missing; run prepare first")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("endpoint") != args.endpoint:
        raise RuntimeError("manifest endpoint does not match requested endpoint")
    return value


def image_payload(item: dict[str, Any]) -> tuple[str, int, int]:
    cache_root = args_state_cache()
    path = oss_backend.materialize(cache_root, item["object_key"], item["object_sha256"])
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return encoded, int(width), int(height)


def normalized_ai_annotations(item: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        annotations = json.loads(str(item.get("ai_annotations") or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(annotations, list):
        return []
    output = []
    fallback_confidence = float(item.get("ai_confidence") or 1.0)
    for box in annotations:
        if not isinstance(box, dict):
            continue
        if str(box.get("label") or "").strip().lower() != item["source_kind"]:
            continue
        try:
            x = float(box["x"])
            y = float(box["y"])
            width = float(box["w"])
            height = float(box["h"])
            confidence = float(box.get("confidence", fallback_confidence))
        except (KeyError, TypeError, ValueError):
            continue
        x2 = x + width
        y2 = y + height
        if not (
            0 <= confidence <= 1
            and 0 <= x < x2 <= 1
            and 0 <= y < y2 <= 1
        ):
            continue
        output.append(
            {
                "conf": confidence,
                "gcid": int(item["report_gcid"]),
                "aid": 0,
                "cid": int(item["report_gcid"]),
                "class_name": str(item["report_class_name"]),
                "x1": x,
                "y1": y,
                "x2": x2,
                "y2": y2,
            }
        )
    return output


_ACTIVE_STATE_DIR: Path | None = None


def args_state_cache() -> Path:
    if _ACTIVE_STATE_DIR is None:
        raise RuntimeError("state directory is not initialized")
    path = _ACTIVE_STATE_DIR / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_payload(item: dict[str, Any]) -> dict[str, Any]:
    picture, width, height = image_payload(item)
    channel = item["channel_metadata"]
    detects = normalized_ai_annotations(item)
    if not detects:
        raise RuntimeError("AI-reviewed item has no valid annotations")
    return {
        "chid": int(item["channel"]),
        "ncid": 0,
        "ip": str(channel.get("ip") or ""),
        "geid": int(item["report_geid"]),
        "sn": item["machine_code"],
        "sn32": "",
        "location": str(channel.get("location") or ""),
        "width": width,
        "height": height,
        "desc": str(channel.get("desc") or channel.get("location") or ""),
        "pic_data": picture,
        "timestamp": int(item["capture"].get("timeStamp") or item["source_mtime"]),
        "nn_output": detects,
    }


def ledger_path(args: argparse.Namespace) -> Path:
    return args.state_dir / "ledger.jsonl"


def ledger_entries(args: argparse.Namespace) -> list[dict[str, Any]]:
    path = ledger_path(args)
    if not path.is_file():
        return []
    output = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            output.append(json.loads(line))
    return output


def append_ledger(args: argparse.Namespace, entry: dict[str, Any]) -> None:
    with ledger_path(args).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def post_once(args: argparse.Namespace, item: dict[str, Any], phase: str) -> dict[str, Any]:
    payload = build_payload(item)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload_sha256 = hashlib.sha256(body).hexdigest()
    request = urllib.request.Request(
        args.endpoint,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "AI-BOT-reviewed-replay/1.0"},
        method="POST",
    )
    entry: dict[str, Any] = {
        "attempted_at": utc_now(),
        "phase": phase,
        "item_id": item["item_id"],
        "image_sha256": item["sha256"],
        "payload_sha256": payload_sha256,
        "device": item["device"],
        "geid": item["geid"],
        "report_geid": item["report_geid"],
        "channel": item["channel"],
        "event_timestamp": payload["timestamp"],
        "status": "unknown",
    }
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            response_body = response.read(65536)
            entry["http_status"] = int(response.status)
    except urllib.error.HTTPError as exc:
        response_body = exc.read(65536)
        entry["http_status"] = int(exc.code)
    except Exception as exc:
        entry["transport_error"] = type(exc).__name__ + ": " + str(exc)
        append_ledger(args, entry)
        return entry
    entry["response_sha256"] = hashlib.sha256(response_body).hexdigest()
    try:
        response_json = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        entry["status"] = "failed"
        entry["response_error"] = "non_json_response"
        append_ledger(args, entry)
        return entry
    entry["application_code"] = response_json.get("code")
    entry["message"] = str(response_json.get("msg") or "")[:300]
    entry["req_id"] = str(response_json.get("reqId") or "")[:200]
    if 200 <= entry["http_status"] < 300 and response_json.get("code") in SUCCESS_CODES:
        entry["status"] = "success"
    else:
        entry["status"] = "failed"
    append_ledger(args, entry)
    return entry


def already_terminal(entries: list[dict[str, Any]]) -> dict[str, str]:
    result = {}
    for entry in entries:
        if entry.get("status") in {"success", "unknown"}:
            result[entry["item_id"]] = entry["status"]
    return result


def require_send_enabled(args: argparse.Namespace) -> None:
    if not args.enable_file.is_file():
        raise RuntimeError("sending is disabled: enable file is absent")
    if args.enable_file.read_text(encoding="utf-8").strip() != "AUTHORIZED_BY_SOULZYN":
        raise RuntimeError("sending is disabled: enable file content is invalid")


def canary(args: argparse.Namespace) -> dict[str, Any]:
    require_send_enabled(args)
    data = manifest(args)
    previous = ledger_entries(args)
    if any(entry.get("phase") == "canary" and entry.get("status") == "success" for entry in previous):
        raise RuntimeError("a successful canary already exists; refusing duplicate canary")
    terminal = already_terminal(previous)
    candidates = [item for item in reversed(data["items"]) if item["item_id"] not in terminal]
    if not candidates:
        raise RuntimeError("no canary candidate is available")
    return post_once(args, candidates[0], "canary")


def send(args: argparse.Namespace) -> dict[str, Any]:
    require_send_enabled(args)
    data = manifest(args)
    entries = ledger_entries(args)
    if not any(entry.get("phase") == "canary" and entry.get("status") == "success" for entry in entries):
        raise RuntimeError("a successful canary is required before batch send")
    terminal = already_terminal(entries)
    pending = [item for item in data["items"] if item["item_id"] not in terminal]
    if args.limit > 0:
        pending = pending[: args.limit]
    result = {"requested": len(pending), "success": 0, "failed": 0, "unknown": 0}
    for item in pending:
        entry = post_once(args, item, "batch")
        result[entry["status"]] += 1
        if entry["status"] != "success":
            result["stopped_at_item"] = item["item_id"]
            break
        if args.delay > 0:
            time.sleep(args.delay)
    result["finished_at"] = utc_now()
    return result


def status(args: argparse.Namespace) -> dict[str, Any]:
    entries = ledger_entries(args)
    counts: dict[str, int] = {}
    for entry in entries:
        key = str(entry.get("status") or "missing")
        counts[key] = counts.get(key, 0) + 1
    data = manifest(args)
    terminal = already_terminal(entries)
    return {
        "manifest_items": len(data["items"]),
        "ledger_entries": len(entries),
        "terminal_items": len(terminal),
        "remaining": len(data["items"]) - len(terminal),
        "status_counts": counts,
    }


def main() -> None:
    global _ACTIVE_STATE_DIR
    args = parse_args()
    if args.delay < 0 or args.timeout <= 0 or args.limit < 0:
        raise ValueError("invalid numeric argument")
    _ACTIVE_STATE_DIR = args.state_dir
    if args.mode == "prepare":
        result = prepare(args)
    elif args.mode == "canary":
        result = canary(args)
    elif args.mode == "send":
        result = send(args)
    else:
        result = status(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
