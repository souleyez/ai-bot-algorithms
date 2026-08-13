#!/usr/bin/env python3
"""Create a private, image-free summary of reviewed AI box report replay."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.output.exists():
        raise RuntimeError(f"refusing to overwrite {args.output}")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    entries = [
        json.loads(line)
        for line in args.ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    item_ids = [entry["item_id"] for entry in entries]
    payload_hashes = [entry["payload_sha256"] for entry in entries]
    success = [entry for entry in entries if entry.get("status") == "success"]
    response_codes = Counter(
        f"http={entry.get('http_status')}|code={entry.get('application_code')}"
        for entry in entries
    )
    groups = Counter(
        f"{entry['device']}|m{entry['geid']}" for entry in success
    )
    channels = Counter(
        f"{entry['device']}|m{entry['geid']}|ch{entry['channel']}"
        for entry in success
    )
    phases = Counter(entry.get("phase", "") for entry in entries)
    summary = {
        "schema": "ai-bot-reviewed-report-replay-summary-v1",
        "endpoint": manifest["endpoint"],
        "prepared": manifest["summary"],
        "ledger": {
            "entries": len(entries),
            "unique_item_ids": len(set(item_ids)),
            "unique_payload_sha256": len(set(payload_hashes)),
            "success": len(success),
            "failed": sum(entry.get("status") == "failed" for entry in entries),
            "unknown": sum(entry.get("status") == "unknown" for entry in entries),
            "phases": dict(sorted(phases.items())),
            "response_codes": dict(sorted(response_codes.items())),
            "by_device_algorithm": dict(sorted(groups.items())),
            "by_device_algorithm_channel": dict(sorted(channels.items())),
            "first_attempted_at": entries[0].get("attempted_at") if entries else None,
            "last_attempted_at": entries[-1].get("attempted_at") if entries else None,
        },
    }
    if len(entries) != len(manifest["items"]):
        raise RuntimeError("ledger count does not match manifest count")
    if len(set(item_ids)) != len(entries):
        raise RuntimeError("duplicate item IDs exist in ledger")
    if len(set(payload_hashes)) != len(entries):
        raise RuntimeError("duplicate payload hashes exist in ledger")
    if len(success) != len(entries):
        raise RuntimeError("ledger contains non-success entries")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary["ledger"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
