#!/usr/bin/env python3
"""
Reconcile closed positions in DDB against the Polymarket CLOB trade history.

Why this exists
---------------
Before the close_position fix, every SELL recorded `close_price = the limit
price we sent` (often the tick floor for stop-losses on cheap markets), not
the *actual* VWAP we received from the matching engine. This made our ledger
P&L look worse than the real wallet-level P&L Polymarket reports.

This script reads each closed DDB row and pulls the matching trades from
`client.get_trades(asset_id=token_id)`, filters by `taker_order_id ==
clob_order_id`, and computes the real VWAP and total filled size. It then:

  1. Prints a per-row comparison (recorded vs on-chain) and the dollar gap.
  2. If `--write` is passed, updates `close_price` and `pnl_usd` in DDB so
     the dashboard / win-rate / ROI metrics reflect ground truth.

Resolution closes (`close_reason="resolution"`) are skipped — those are
written by `reconcile_resolved_positions` from the on-chain market payload
and are already correct.

Usage
-----
    AWS_PROFILE=…                    # or AWS_ACCESS_KEY_ID/SECRET/TOKEN
    POLYGON_PRIVATE_KEY=…            # required by py_clob_client_v2 init
    POLYMARKET_WALLET_ADDRESS=…
    BUILDER_CODE=…
    SIGNATURE_TYPE=POLY_1271
    DDB_TABLE=geo-trading-positions
    DDB_REGION=eu-west-1
    python scripts/reconcile_closes.py            # read-only report
    python scripts/reconcile_closes.py --write    # apply corrections
"""
import os
import sys
import argparse
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3  # noqa: E402

from agent.tools.markets import get_clob_client  # noqa: E402
from py_clob_client_v2 import TradeParams         # noqa: E402


def _fnum(v, default=None):
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _trade_field(t, *keys, default=None):
    for k in keys:
        v = t.get(k) if isinstance(t, dict) else None
        if v not in (None, ""):
            return v
    return default


def _parse_iso(ts: str) -> float | None:
    """ISO-8601 (with optional 'Z') → epoch seconds, else None."""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fetch_sell_trades_for_close(
    client,
    asset_id: str,
    close_order_id: str | None,
    close_time_iso: str | None,
    time_window_s: int = 120,
) -> list[dict]:
    """
    Return the SELL trades that funded a specific close.

    Two strategies, in order of reliability:
      1. If `close_order_id` is stored on the row (set by the post-fix
         close_position via DDB column), filter trades by
         `taker_order_id == close_order_id`. This is unambiguous.
      2. Otherwise, fall back to: all SELL-side trades on this asset_id whose
         `match_time` is within `time_window_s` of the recorded close_time.
         This catches the historical rows that pre-date close_order_id
         tracking.

    BUY trades are always excluded — they belong to the entry, not the exit.
    """
    try:
        trades = client.get_trades(TradeParams(asset_id=asset_id))
    except Exception as exc:
        print(f"  ! get_trades({asset_id[:12]}…) failed: {exc}", file=sys.stderr)
        return []
    if not isinstance(trades, list):
        return []

    sells = [
        t for t in trades
        if isinstance(t, dict)
        and (_trade_field(t, "side", default="") or "").upper() == "SELL"
    ]

    if close_order_id:
        coid = close_order_id.lower()
        matched = [
            t for t in sells
            if (_trade_field(t, "taker_order_id", "takerOrderID", default="") or "").lower() == coid
        ]
        if matched:
            return matched
        # Fall through to time-match if the order id didn't yield trades
        # (rare — stale row, partial settle).

    # Time-match path
    close_epoch = _parse_iso(close_time_iso or "")
    if close_epoch is None:
        # No way to disambiguate further; if there's only one SELL on this
        # token, return it (single-flat-strategy heuristic).
        return sells if len(sells) == 1 else []

    out = []
    for t in sells:
        mt_raw = _trade_field(t, "match_time", "matchTime", default=None)
        try:
            mt = float(mt_raw) if mt_raw is not None else None
        except (TypeError, ValueError):
            mt = None
        if mt is None:
            continue
        if abs(mt - close_epoch) <= time_window_s:
            out.append(t)
    return out


def compute_vwap(trades: list[dict]) -> tuple[float | None, float]:
    """Return (vwap, total_size) across a trade list."""
    total_size = 0.0
    total_value = 0.0
    for t in trades:
        size  = _fnum(_trade_field(t, "size"))
        price = _fnum(_trade_field(t, "price"))
        if size and price and size > 0 and price > 0:
            total_size  += size
            total_value += size * price
    if total_size <= 0:
        return (None, 0.0)
    return (total_value / total_size, total_size)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="Apply corrections to DDB. Default: read-only report.")
    ap.add_argument("--min-gap", type=float, default=0.10,
                    help="Only show rows where |recorded_pnl - actual_pnl| >= this. Default 0.10.")
    args = ap.parse_args()

    region = os.environ.get("DDB_REGION", "eu-west-1")
    table  = os.environ.get("DDB_TABLE",  "geo-trading-positions")
    ddb    = boto3.resource("dynamodb", region_name=region).Table(table)

    print(f"Reconciling {table!r} against CLOB trade history "
          f"({'WRITE MODE' if args.write else 'read-only'}; min gap ${args.min_gap})")
    print()

    client = get_clob_client()

    # Pull all closed rows (paginated)
    rows = []
    kwargs = {}
    while True:
        page = ddb.scan(**kwargs)
        rows.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    closed = [r for r in rows if r.get("status") == "closed"
              and r.get("close_reason") != "resolution"]
    print(f"Found {len(closed)} closed non-resolution rows (out of {len(rows)} total).")
    print()

    skipped, ok, fixed, gap_rows = 0, 0, 0, []
    total_gap_dollars = 0.0

    for pos in closed:
        token_id = pos.get("token_id")
        # close_order_id is set by post-fix closes; fall back to time match
        # for legacy rows. clob_order_id is the ENTRY order — do NOT use it
        # for close reconciliation (that's how the previous version of this
        # script ended up reading buy-side trades and reporting bogus deltas).
        close_oid  = pos.get("close_order_id") or ""
        close_iso  = pos.get("close_time") or ""
        if not token_id:
            skipped += 1
            continue

        entry    = _fnum(pos.get("entry_price"))
        rec_size = _fnum(pos.get("size_shares"), 0.0)
        rec_close = _fnum(pos.get("close_price"))
        rec_pnl   = _fnum(pos.get("pnl_usd"))
        if entry is None or rec_close is None or rec_pnl is None:
            skipped += 1
            continue

        trades = fetch_sell_trades_for_close(client, token_id, close_oid, close_iso)
        vwap, filled = compute_vwap(trades)
        if vwap is None:
            skipped += 1
            continue

        actual_pnl = round((vwap - entry) * filled, 4)
        gap = actual_pnl - rec_pnl
        q = (pos.get("question") or "")[:60]

        if abs(gap) < args.min_gap:
            ok += 1
            continue

        gap_rows.append((token_id, q, entry, rec_size, rec_close, rec_pnl,
                         filled, vwap, actual_pnl, gap))
        total_gap_dollars += gap

        if args.write:
            try:
                ddb.update_item(
                    Key={"token_id": token_id},
                    UpdateExpression=(
                        "SET close_price = :cp, pnl_usd = :pn, "
                        "size_shares = :ss, fill_reconciled = :y"
                    ),
                    ExpressionAttributeValues={
                        ":cp": Decimal(str(round(vwap, 6))),
                        ":pn": Decimal(str(actual_pnl)),
                        ":ss": Decimal(str(round(filled, 6))),
                        ":y":  "true",
                    },
                )
                fixed += 1
            except Exception as exc:
                print(f"  ! update failed for {token_id[:14]}…: {exc}", file=sys.stderr)

    # ── Report ───────────────────────────────────────────────────────────────
    print(f"  {'token':<14} {'question':<60} {'entry':>7} {'size→':>9} {'close→':>9} "
          f"{'pnl→':>9} {'actual_pnl':>11} {'Δ$':>8}")
    for tid, q, entry, rec_size, rec_close, rec_pnl, filled, vwap, actual_pnl, gap in sorted(gap_rows, key=lambda r: r[-1]):
        print(f"  {tid[:14]:<14} {q:<60} {entry:>7.4f} "
              f"{rec_size:>4.1f}→{filled:<4.1f} "
              f"{rec_close:>4.4f}→{vwap:<4.4f} "
              f"{rec_pnl:>+8.2f} {actual_pnl:>+10.2f} {gap:>+7.2f}")

    print()
    print(f"Summary:")
    print(f"  rows scanned             : {len(closed)}")
    print(f"  ok (within ${args.min_gap})        : {ok}")
    print(f"  skipped (no data)        : {skipped}")
    print(f"  off by >= ${args.min_gap}         : {len(gap_rows)}")
    print(f"  total ledger correction  : ${total_gap_dollars:+.2f}  "
          f"(positive = ledger was UNDER-reporting P&L)")
    if args.write:
        print(f"  rows written to DDB      : {fixed}")
    else:
        print(f"  (read-only — pass --write to apply)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
