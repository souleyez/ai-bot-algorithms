"""Bounded read-only m101 import. Requires an independently trusted SSH host key."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import paramiko

try:
    from . import door_review, sync_worker
except ImportError:
    import door_review
    import sync_worker


HOST = "42.193.140.103"
PORT = 62955


def connect_device(known_hosts: Path) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.load_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    if not client.get_host_keys().lookup(f"[{HOST}]:{PORT}"):
        raise RuntimeError("trusted device host key is missing")
    names = ("AI_BOT_DEVICE_SSH_USER", "AI_BOT_DEVICE_SSH_PASSWORD")
    values = [os.environ.get(name, "") for name in names]
    if not all(values):
        raise RuntimeError("device credential environment is missing")
    try:
        client.connect(HOST, port=PORT, username=values[0], password=values[1],
                       timeout=12, banner_timeout=12, auth_timeout=12,
                       allow_agent=False, look_for_keys=False)
        return client
    except Exception:
        client.close()
        raise


def collect(known_hosts: Path, dry_run: bool = False) -> dict:
    source = {"device":door_review.DEVICE, "host":HOST, "port":PORT,
              "kind":door_review.SOURCE_KIND, "label":"62821开关门复核",
              "model":"m101", "remote_dir":"/userdata/mpp/disk",
              "filename_pattern":door_review.CAPTURE.pattern, "lookback_days":14}
    config = {"min_free_bytes":8 * 1024**3, "max_review_bytes":3 * 1024**3,
              "max_items":20000, "max_new_per_source_per_run":100,
              "remote_scan_limit":5000, "max_image_dimension":1600}
    client = connect_device(known_hosts)
    try:
        # Reuse deduplication/OSS, but never run the legacy worker's retention sweep.
        result = sync_worker.sync_source(client, source, config, dry_run)
        return {"checkedAt":sync_worker.utc_now(), "project":door_review.PROJECT,
                "dryRun":dry_run, "result":result}
    finally:
        client.close()


def main() -> None:
    import fcntl

    parser = argparse.ArgumentParser()
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with sync_worker.LOCK_PATH.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"skipped":"capture sync already running"}))
            return
        try:
            result = collect(args.known_hosts, args.dry_run)
        except Exception as exc:
            print(json.dumps({"project":door_review.PROJECT, "error":type(exc).__name__}))
            raise SystemExit(1) from None
        print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
