#!/usr/bin/env python3
"""
One-time migration: write `theme_key` onto DDB position rows that lack it.

Why: `agent.tools.wallet.place_order` started persisting `theme_key` on new
position rows as part of the theme-cap feature. Rows that pre-date that
change have `theme_key=null`. `ddb.count_open_in_theme` falls back to
deriving the theme on the fly from `source_headline`, so the cap still works
— but several reporting paths and the dashboard slicer assume the field is
materialised on every row.

This script scans the table, derives the theme from `source_headline` via
`agent.signals.classify_theme`, and writes it back where missing. Safe to
run multiple times (idempotent — skips rows that already have a value).

Usage:
    AWS_PROFILE=…              # or export AWS_* env vars
    DDB_TABLE=geo-trading-positions  \\
    DDB_REGION=eu-west-1       \\
    python scripts/backfill_theme_key.py
"""
import os
import sys
from pathlib import Path

# Ensure `agent.*` imports resolve when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402

from agent.signals import classify_theme  # noqa: E402


def main() -> int:
    region = os.environ.get("DDB_REGION", "eu-west-1")
    table  = os.environ.get("DDB_TABLE",  "geo-trading-positions")
    client = boto3.resource("dynamodb", region_name=region).Table(table)

    print(f"Backfill theme_key on {table!r} in {region!r}")

    scanned = 0
    needed  = 0
    written = 0
    last_evaluated = None

    while True:
        kwargs = {}
        if last_evaluated:
            kwargs["ExclusiveStartKey"] = last_evaluated
        page = client.scan(**kwargs)
        for item in page.get("Items", []):
            scanned += 1
            token_id = item.get("token_id")
            if not token_id:
                continue
            if item.get("theme_key"):
                continue
            needed += 1
            source = item.get("source_headline") or ""
            theme = classify_theme(source)
            try:
                client.update_item(
                    Key={"token_id": token_id},
                    UpdateExpression="SET theme_key = :t",
                    ExpressionAttributeValues={":t": theme},
                )
                written += 1
                print(f"  {token_id[:14]}…  theme={theme:<8}  src={source[:60]!r}")
            except Exception as exc:
                print(f"  ! update failed for {token_id[:14]}…: {exc}", file=sys.stderr)
        last_evaluated = page.get("LastEvaluatedKey")
        if not last_evaluated:
            break

    print(f"\nScanned {scanned} rows. Needed backfill: {needed}. Wrote: {written}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
