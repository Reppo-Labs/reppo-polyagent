#!/usr/bin/env python3
"""
Reconcile Agent A (and optionally B) DDB positions against live Polymarket data.

Rows closed as `order_not_found` or `order_cancelled` are cross-checked against
the Polymarket data API.  If PM still shows the position with real value, the
row is restored to `status=open` so the agent can manage risk on the next run.

Usage:
    AWS_PROFILE=... python3 scripts/reconcile_ddb_from_pm.py --agent A [--dry-run]
    AWS_PROFILE=... python3 scripts/reconcile_ddb_from_pm.py --agent B [--dry-run]
    AWS_PROFILE=... python3 scripts/reconcile_ddb_from_pm.py --agent A --agent B
"""

import argparse
import decimal
import json
import os
import sys

import boto3
import requests

AGENTS = {
    "A": {
        "table":     "abc-positions-a",
        "region":    "eu-west-1",
        "portfolio": "0x4aA88C56208864fd53035B16B7EeE6E887d5c63F",
    },
    "B": {
        "table":     "abc-positions-b",
        "region":    "eu-west-1",
        "portfolio": "0xb14Cf74847ffA6bC9EbE4030cb73C40eEc699112",
    },
}

RESTORABLE_REASONS = {"order_not_found", "order_cancelled", "no_order_id"}


def _to_float(item: dict) -> dict:
    return {k: float(v) if isinstance(v, decimal.Decimal) else v for k, v in item.items()}


def fetch_pm_positions(portfolio: str) -> dict[str, dict]:
    """Return {token_id: pm_row} for all positions with value on Polymarket."""
    url = f"https://data-api.polymarket.com/positions?user={portfolio.lower()}"
    r = requests.get(url, timeout=15, headers={"User-Agent": "polyagent-reconcile/1.0"})
    r.raise_for_status()
    by_token: dict[str, dict] = {}
    for p in r.json():
        token = p.get("asset") or p.get("token_id") or ""
        if token and float(p.get("currentValue") or 0) > 0.01:
            by_token[token] = p
    return by_token


def scan_all(table) -> list[dict]:
    items: list[dict] = []
    resp = table.scan()
    items.extend(_to_float(i) for i in resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(_to_float(i) for i in resp.get("Items", []))
    return items


def restore_position(table, row: dict, pm: dict, dry_run: bool) -> None:
    token_id = row["token_id"]
    pm_size  = float(pm.get("size") or pm.get("shares") or 0)
    # Keep original entry_price if stored; PM's avgPrice is a reasonable fallback
    entry_price = float(row.get("entry_price") or pm.get("avgPrice") or 0)

    print(
        f"  RESTORE token={token_id[:16]}… "
        f"question={str(row.get('question',''))[:50]!r} "
        f"shares={pm_size:.4f} value=${float(pm.get('currentValue',0)):.2f} "
        f"(was closed: {row.get('close_reason')})"
    )

    if dry_run:
        return

    table.update_item(
        Key={"token_id": token_id},
        UpdateExpression=(
            "SET #s = :open, size_shares = :ss, order_status = :os "
            "REMOVE close_price, close_reason, close_time, pnl_usd"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":open": "open",
            ":ss":   str(pm_size),
            ":os":   "filled",
        },
    )


def reconcile_agent(agent_id: str, dry_run: bool) -> None:
    cfg = AGENTS[agent_id]
    print(f"\n=== Agent {agent_id} | table={cfg['table']} | portfolio={cfg['portfolio']} ===")

    dynamodb = boto3.resource("dynamodb", region_name=cfg["region"])
    table    = dynamodb.Table(cfg["table"])

    print("Fetching Polymarket positions…")
    pm_by_token = fetch_pm_positions(cfg["portfolio"])
    print(f"  PM positions with value: {len(pm_by_token)}")
    for tok, p in pm_by_token.items():
        print(f"    {tok[:16]}… ${float(p.get('currentValue',0)):.2f}  {p.get('title','')[:50]}")

    print("\nScanning DDB…")
    all_rows = scan_all(table)
    closed   = [r for r in all_rows if r.get("status") == "closed"]
    open_    = [r for r in all_rows if r.get("status") in ("open", "pending")]
    print(f"  Total rows: {len(all_rows)}  (open/pending: {len(open_)}, closed: {len(closed)})")

    open_tokens = {r["token_id"] for r in open_}

    restored = skipped = 0
    for row in closed:
        token_id = row.get("token_id", "")
        reason   = row.get("close_reason", "")
        if reason not in RESTORABLE_REASONS:
            continue
        if token_id in open_tokens:
            continue
        pm = pm_by_token.get(token_id)
        if pm:
            restore_position(table, row, pm, dry_run)
            restored += 1
        else:
            skipped += 1

    label = "[DRY RUN] Would restore" if dry_run else "Restored"
    print(f"\n{label}: {restored}  |  Skipped (no PM match): {skipped}")
    print(f"Open/pending after: {len(open_) + (0 if dry_run else restored)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=["A", "B"], action="append", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing to DDB")
    args = parser.parse_args()

    for agent_id in args.agent:
        reconcile_agent(agent_id, dry_run=args.dry_run)

    if args.dry_run:
        print("\n--- DRY RUN: no DDB writes performed ---")
    else:
        print("\nDone. Re-run with --dry-run first if unsure.")


if __name__ == "__main__":
    main()
