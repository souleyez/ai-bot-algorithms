"""Private m101 door-state review, separate from YOLO training/publication facts."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone


PROJECT = "62821-doors"
DEVICE = "62821"
SOURCE_KIND = "door-state"
CAPTURE = re.compile(r"^ch(\d+)_m101_[\w.-]+\.jpg$", re.IGNORECASE)
STATES = {"open", "closed", "uncertain"}
VERDICTS = {"correct", "false_alarm", "uncertain"}


class Conflict(ValueError):
    pass


def ensure_schema(connection: sqlite3.Connection) -> None:
    # State/event judgements are not positive person boxes or auto-report facts.
    connection.execute("""CREATE TABLE IF NOT EXISTS door_review_revisions (
        project TEXT NOT NULL, item_id TEXT NOT NULL, revision INTEGER NOT NULL,
        request_key TEXT NOT NULL UNIQUE, command_json TEXT NOT NULL,
        door_state TEXT NOT NULL, verdict TEXT NOT NULL, notes TEXT NOT NULL,
        captured_sha256 TEXT NOT NULL, actor TEXT NOT NULL, reviewed_at TEXT NOT NULL,
        PRIMARY KEY(project, item_id, revision))""")


def scoped_rows(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute("""
        SELECT i.*, r.revision AS door_revision, r.door_state, r.verdict,
               r.notes AS door_notes, r.reviewed_at AS door_reviewed_at
        FROM items i LEFT JOIN door_review_revisions r
          ON r.project=? AND r.item_id=i.id AND r.revision=(
            SELECT MAX(r2.revision) FROM door_review_revisions r2
            WHERE r2.project=r.project AND r2.item_id=r.item_id)
        WHERE i.source_device=? AND i.source_kind=?
        ORDER BY i.source_mtime DESC, i.id
    """, (PROJECT, DEVICE, SOURCE_KIND)).fetchall()
    result = []
    for row in rows:
        match = CAPTURE.fullmatch(row["filename"])
        if not match:
            continue
        result.append({
            "id": row["id"], "channel": int(match[1]), "filename": row["filename"],
            "imageUrl": "/images/" + row["image_path"],
            "capturedAt": datetime.fromtimestamp(row["source_mtime"], timezone.utc).isoformat(),
            "revision": row["door_revision"] or 0, "state": row["door_state"] or "",
            "verdict": row["verdict"] or "", "notes": row["door_notes"] or "",
            "reviewedAt": row["door_reviewed_at"] or "",
        })
    return result


def project_payload(connection: sqlite3.Connection, *, channel: str = "all",
                    status: str = "pending", after: str = "") -> dict:
    if status not in {"pending", "reviewed", "all"}:
        raise ValueError("invalid filter")
    if after and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", after):
        raise ValueError("invalid cursor")
    if channel != "all" and not re.fullmatch(r"[1-9][0-9]{0,2}", channel):
        raise ValueError("invalid channel")
    rows = scoped_rows(connection)
    counts = {"total": len(rows), "pending": sum(not row["revision"] for row in rows)}
    counts["reviewed"] = counts["total"] - counts["pending"]
    counts.update({state: sum(row["state"] == state for row in rows) for state in STATES})
    filtered = [row for row in rows if
                (channel == "all" or row["channel"] == int(channel)) and
                (status == "all" or bool(row["revision"]) == (status == "reviewed"))]
    matched = len(filtered)
    if after:
        positions = {row["id"]: index for index, row in enumerate(rows)}
        if after not in positions:
            raise Conflict("抓拍列表已更新，请重新选择筛选条件")
        filtered = [row for row in filtered if positions[row["id"]] > positions[after]]
    page = filtered[:100]
    return {"project": PROJECT, "name": "62821 开关门复核", "device": DEVICE,
            "model": "m101", "counts": counts,
            "channels": sorted({row["channel"] for row in rows}),
            "items": page, "matched": matched,
            "nextCursor": page[-1]["id"] if page else after,
            "hasMore": len(filtered) > 100,
            "latestCaptureAt": rows[0]["capturedAt"] if rows else None}


def record(connection: sqlite3.Connection, item_id: str, payload: dict,
           request_key: str, actor: str = "") -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_key):
        raise ValueError("valid Idempotency-Key is required")
    if not isinstance(payload, dict) or set(payload) != {"state", "verdict", "notes", "expectedRevision"}:
        raise ValueError("invalid review command")
    if payload["state"] not in STATES or payload["verdict"] not in VERDICTS:
        raise ValueError("invalid state or verdict")
    if type(payload["expectedRevision"]) is not int or payload["expectedRevision"] < 0:
        raise ValueError("invalid revision")
    if not isinstance(payload["notes"], str) or len(payload["notes"]) > 1000:
        raise ValueError("invalid notes")
    command = json.dumps({"id": item_id, **payload}, sort_keys=True, ensure_ascii=False)
    connection.execute("BEGIN IMMEDIATE")
    try:
        previous = connection.execute(
            "SELECT * FROM door_review_revisions WHERE request_key=?", (request_key,)
        ).fetchone()
        if previous:
            if previous["command_json"] != command:
                raise Conflict("request key reused with different content")
            connection.commit()
            return {"saved": True, "revision": previous["revision"], "replayed": True}
        row = connection.execute("SELECT * FROM items WHERE id=? AND source_device=? AND source_kind=?",
                                 (item_id, DEVICE, SOURCE_KIND)).fetchone()
        if not row or not CAPTURE.fullmatch(row["filename"]):
            raise KeyError("capture not found in this project")
        current = connection.execute(
            "SELECT COALESCE(MAX(revision),0) FROM door_review_revisions WHERE project=? AND item_id=?",
            (PROJECT, item_id)).fetchone()[0]
        if current != payload["expectedRevision"]:
            raise Conflict("此图片已被更新，请刷新后再审")
        reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute("""INSERT INTO door_review_revisions
            (project,item_id,revision,request_key,command_json,door_state,verdict,
             notes,captured_sha256,actor,reviewed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                PROJECT, item_id, current + 1, request_key, command, payload["state"],
                payload["verdict"], payload["notes"], row["sha256"], actor[:254], reviewed_at))
        connection.commit()
        return {"saved": True, "revision": current + 1, "replayed": False}
    except Exception:
        connection.rollback()
        raise
