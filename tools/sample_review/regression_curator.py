#!/usr/bin/env python3
"""Owner-operations CLI for one immutable regression selection."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

try:
    from . import regression_store, review_revisions
except ImportError:
    import regression_store  # type: ignore
    import review_revisions  # type: ignore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--algorithm", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--selection-id")
    args = parser.parse_args()
    request = json.loads(args.input.read_text(encoding="utf-8"))
    items = request.get("items") if isinstance(request, dict) else None
    if not isinstance(items, list):
        raise SystemExit("input must be an object containing items[]")
    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    try:
        review_revisions.migrate(connection)
        selection = regression_store.create_selection(
            connection,
            algorithm_key=args.algorithm,
            items=items,
            idempotency_key=args.idempotency_key,
            selection_id=args.selection_id,
        )
        connection.commit()
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "selection_id": selection["selection_id"],
                "selection_revision": selection["selection_revision"],
                "content_sha256": selection["content_sha256"],
                "item_count": len(selection["items"]),
                "replayed": bool(selection.get("replayed")),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
