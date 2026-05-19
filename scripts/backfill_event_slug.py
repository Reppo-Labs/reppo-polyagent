#!/usr/bin/env python3
"""
One-time migration: write `event_slug` onto DDB position rows that lack it.

Why: Polymarket UI URLs use the form `/event/<event_slug>/<market_slug>`. The
agent now captures `event_slug` at trade time (see `agent/tools/wallet.py`),
but rows that pre-date that change have only `market_slug` — and the dashboard
falls back to `/event/<market_slug>` which 404s on most events because the
market slug alone is not the canonical event URL.

This script:
  1. Scans the DDB positions table.
  2. For each row missing `event_slug`, calls Gamma
     `/markets?condition_ids=<market_id>` to look up the parent event.
  3. Writes `event_slug` (or an empty string if the lookup fails so we don't
     retry the same row forever).

Safe to run multiple times — skips rows that already have a non-empty value.

Usage:
    AWS_PROFILE=…            # or export AWS_* env vars
    DDB_TABLE=geo-trading-positions \\
    DDB_REGION=eu-west-1     \\
    python scripts/backfill_event_slug.py
"""
import os
import sys
import time
from pathlib import Path

# Resolve `agent.*` imports when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3       # noqa: E402
import requests    # noqa: E402

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
SLEEP_BETWEEN_LOOKUPS_SEC = 0.25   # rate-limit politeness


def fetch_event_slug(market_id: str) -> str | None:
    """Return the canonical event_slug for a market, or None on failure."""
    try:
        r = requests.get(
            GAMMA_API_URL,
            params={"condition_ids": market_id},
            timeout=15,
            headers={"User-Agent": "polyagent-backfill/1.0"},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"  ! lookup failed for {market_id[:14]}…: {exc}", file=sys.stderr)
        return None

    arr = data if isinstance(data, list) else [data]
    if not arr:
        return None
    events = arr[0].get("events") or []
    if not events:
        return None
    return events[0].get("slug") or None


def main() -> int:
    region = os.environ.get("DDB_REGION", "eu-west-1")
    table  = os.environ.get("DDB_TABLE",  "geo-trading-positions")
    client = boto3.resource("dynamodb", region_name=region).Table(table)

    print(f"Backfill event_slug on {table!r} in {region!r}")

    scanned = 0
    needed  = 0
    written = 0
    skipped = 0
    last_evaluated = None

    while True:
        kwargs = {}
        if last_evaluated:
            kwargs["ExclusiveStartKey"] = last_evaluated
        page = client.scan(**kwargs)

        for item in page.get("Items", []):
            scanned += 1
            token_id  = item.get("token_id")
            market_id = item.get("market_id")
            if not token_id or not market_id:
                continue
            if item.get("event_slug"):
                continue

            needed += 1
            slug = fetch_event_slug(market_id)
            if not slug:
                # Write empty string so a re-run won't keep retrying this
                # market. If the market becomes lookup-able later (resolved
                # markets sometimes drop out of Gamma's default query),
                # this row will need a manual fix.
                slug = ""
                skipped += 1

            try:
                client.update_item(
                    Key={"token_id": token_id},
                    UpdateExpression="SET event_slug = :s",
                    ExpressionAttributeValues={":s": slug},
                )
                written += 1
                tag = f"event={slug!r}" if slug else "event=<not found>"
                print(f"  {token_id[:14]}…  {tag}  market={market_id[:14]}…")
            except Exception as exc:
                print(f"  ! update failed for {token_id[:14]}…: {exc}", file=sys.stderr)

            time.sleep(SLEEP_BETWEEN_LOOKUPS_SEC)

        last_evaluated = page.get("LastEvaluatedKey")
        if not last_evaluated:
            break

    print(
        f"\nScanned {scanned} rows. Needed backfill: {needed}. "
        f"Wrote: {written}. Of those, {skipped} had no Gamma event "
        f"(empty slug written)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
