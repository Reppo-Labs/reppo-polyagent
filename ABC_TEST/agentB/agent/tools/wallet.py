import json
import logging
import math
import os
import time
from datetime import datetime, timezone

from py_clob_client_v2 import (
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

from . import ddb
from .markets import get_clob_client, get_market_meta, get_open_markets
from ..signals import classify_theme

logger = logging.getLogger(__name__)

# ── Concentration & tail-price guard rails ────────────────────────────────────
#
# MIN_ENTRY_PRICE  — Markets priced below this are excluded from new entries.
#                    A fixed-percent stop-loss on a $0.03 market triggers on a
#                    single tick of noise; the result is forced-sell at zero on
#                    what should be a small-stake tail bet. Lift this floor to
#                    keep the agent out of microstructure traps.
#
# MAX_PER_THEME    — Maximum simultaneously-open positions sharing a macro
#                    theme bucket (see signals.classify_theme). Prevents
#                    loading "one bet, four ways" on a correlated narrative.
MIN_ENTRY_PRICE = float(os.environ.get("MIN_ENTRY_PRICE", "0.05"))
MAX_PER_THEME   = int(os.environ.get("MAX_PER_THEME", "2"))


def check_balance() -> dict:
    """
    Read tradeable pUSD balance directly from Polygon via the pUSD ERC-20 contract.

    pUSD (0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB) is the V2 collateral token
    held in the proxy wallet. We read on-chain rather than via the SDK because
    older SDK builds occasionally miscount allowances on proxy wallets (see
    py-clob-client issues #287, #297, #319 — V2 inherits the same proxy quirk).
    """
    import requests as _requests

    pusd_address   = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    funder_address = os.environ["POLYMARKET_WALLET_ADDRESS"]

    # balanceOf(address) selector = 0x70a08231
    padded  = funder_address[2:].lower().zfill(64)
    payload = {
        "jsonrpc": "2.0", "method": "eth_call",
        "params": [{"to": pusd_address, "data": "0x70a08231" + padded}, "latest"],
        "id": 1,
    }
    rpcs = [
        os.environ.get("POLYGON_RPC", ""),
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon.llamarpc.com",
        "https://rpc.polygon.technology",
        "https://1rpc.io/matic",
    ]
    resp = None
    for rpc in rpcs:
        if not rpc:
            continue
        try:
            r = _requests.post(rpc, json=payload, timeout=10,
                               headers={"Content-Type": "application/json",
                                        "User-Agent": "polyagent/1.0"})
            r.raise_for_status()
            data = r.json()
            if "result" in data:
                resp = data
                break
        except Exception:
            continue
    raw = int(resp["result"], 16) if resp else 0
    balance = raw / 1_000_000  # pUSD has 6 decimals

    min_reserve = float(os.environ.get("MIN_BALANCE_RESERVE", "10.0"))
    logger.info("Polymarket pUSD balance: $%.2f (min reserve: $%.2f)", balance, min_reserve)

    return {
        "usdc":        round(balance, 2),
        "ok_to_trade": balance >= min_reserve,
    }


def _floor_to_tick(price: float, tick_size: float) -> float:
    """Floor a price to the nearest tick. Buyers prefer worse-for-them rounding
    so the limit is always reachable; the V2 builder rejects mis-tick prices."""
    if tick_size <= 0:
        return price
    ticks = math.floor(price / tick_size + 1e-9)
    rounded = ticks * tick_size
    # Floating-point hygiene: round to enough decimals that JSON serialisation
    # doesn't produce 0.4500000000000001 which the CLOB will reject.
    decimals = max(0, -math.floor(math.log10(tick_size)))
    return round(rounded, decimals)


def _extract_order_id(resp) -> str:
    """CLOB V2 post response shape varies across edge cases — extract orderID
    defensively so reconciliation can find it later."""
    if not isinstance(resp, dict):
        return ""
    return (
        resp.get("orderID")
        or resp.get("order_id")
        or resp.get("id")
        or ""
    )


def _extract_status(resp) -> str:
    if not isinstance(resp, dict):
        return ""
    return resp.get("status", "") or ""


def _reconcile_fill(order_id: str, intended_shares: float) -> tuple[float, str]:
    """
    Poll get_order(order_id) once to see how much of the order has matched.
    Returns (filled_shares, status). Best-effort: if the call fails or the
    response is malformed, returns (0, 'unknown').
    """
    if not order_id:
        return (0.0, "no_order_id")
    try:
        order = get_clob_client().get_order(order_id)
    except Exception as exc:
        logger.warning("get_order(%s) failed: %s", order_id[:12], exc)
        return (0.0, "lookup_failed")

    if not isinstance(order, dict):
        return (0.0, "unknown")

    matched_raw = (
        order.get("size_matched")
        or order.get("sizeMatched")
        or order.get("matched_size")
        or "0"
    )
    try:
        matched = float(matched_raw)
    except (TypeError, ValueError):
        matched = 0.0

    status = order.get("status", "unknown")
    matched = min(matched, intended_shares)
    return (matched, status)


def place_order(
    market_id: str,
    outcome: str,
    size_usdc: float,
    limit_price: float,
    source_headline: str = "",
    crowd_score: float = 0.0,
) -> dict:
    """
    Place a BUY limit order on Polymarket CLOB V2 with builder attribution and
    write the position to DynamoDB. Tick-rounds the price, enforces min order
    size, hard-caps order size, and reconciles fill status before returning.
    """
    dry_run     = os.environ.get("DRY_RUN", "false").lower() == "true"
    max_order   = float(os.environ.get("MAX_ORDER_USD", "10.0"))
    size_usdc   = min(size_usdc, max_order)  # hard cap

    # Resolve token from current market list
    markets = get_open_markets()
    market  = next((m for m in markets if m["market_id"] == market_id), None)
    if not market:
        raise ValueError(f"market_id {market_id!r} not found in active markets")

    token_id      = market["yes_token"] if outcome == "YES" else market["no_token"]
    current_price = market["yes_price"] if outcome == "YES" else market["no_price"]

    # ── Tail-price filter ─────────────────────────────────────────────────────
    # Markets priced below MIN_ENTRY_PRICE are rejected. At those prices a
    # -30% percent-based SL fires on a single tick of noise (see the
    # 2026-05-11 post-mortem on Israel-withdraws-Lebanon @0.031 → 0.001).
    if current_price < MIN_ENTRY_PRICE:
        raise ValueError(
            f"current_price {current_price:.4f} is below MIN_ENTRY_PRICE "
            f"({MIN_ENTRY_PRICE:.4f}). Tail-priced markets have hostile "
            "microstructure for our stop-loss; skip this market."
        )

    # ── Theme-cluster cap ─────────────────────────────────────────────────────
    # Reject the entry if we already hold MAX_PER_THEME positions whose
    # source_headline maps to the same macro theme bucket. This prevents the
    # "one bet, four ways" concentration that drove last week's cluster of
    # correlated stop-losses.
    theme_key = classify_theme(source_headline or market.get("question", ""))
    open_in_theme = ddb.count_open_in_theme(theme_key)
    if open_in_theme >= MAX_PER_THEME:
        raise ValueError(
            f"theme cap reached: {open_in_theme} open positions already in "
            f"theme {theme_key!r} (MAX_PER_THEME={MAX_PER_THEME}). Pick a "
            "market from a different theme bucket or end the run."
        )

    # Limit price sanity: must be within 5% of current market price BEFORE we
    # round it to a tick; otherwise a 5% threshold check on a tick-rounded
    # price could pass for an order that the LLM intended to be far off-market.
    if abs(limit_price - current_price) / max(current_price, 0.001) > 0.05:
        raise ValueError(
            f"limit_price {limit_price} is >5% from current {current_price:.3f}"
        )

    # Tick / neg_risk / min_order_size are required for a valid V2 order.
    meta           = get_market_meta(market_id)
    tick_size      = meta["tick_size"]
    neg_risk       = meta["neg_risk"]
    min_order_size = meta["min_order_size"]

    rounded_price = _floor_to_tick(limit_price, tick_size)
    if rounded_price < tick_size or rounded_price > (1 - tick_size):
        raise ValueError(
            f"limit_price {limit_price} rounds to {rounded_price} which is outside "
            f"the valid range [{tick_size}, {1 - tick_size}] for this market"
        )

    size_shares = round(size_usdc / rounded_price, 2)
    if size_shares < min_order_size:
        raise ValueError(
            f"order size {size_shares} shares is below this market's minimum "
            f"({min_order_size}). Increase size_usdc or skip this market."
        )

    # No double entry on same token
    if ddb.get_position_by_token(token_id):
        raise ValueError(f"Open position already exists for token {token_id}")

    # No opposing position in the same market
    if ddb.get_open_positions_for_market(market_id):
        raise ValueError(f"Open position already exists for market {market_id}")

    if dry_run:
        logger.info(
            "[DRY_RUN] place_order: %s %s @ %.4f ($%.2f, %.2f shares, tick=%s neg_risk=%s) — %s",
            outcome, token_id[:12] + "…", rounded_price, size_usdc, size_shares,
            tick_size, neg_risk, market["question"],
        )
        return {"status": "dry_run", "market_id": market_id, "outcome": outcome,
                "size_usdc": size_usdc, "limit_price": rounded_price,
                "size_shares": size_shares}

    # V2: builder_code is auto-applied from BuilderConfig at sign-time; no need
    # to pass it on every OrderArgs. Side is the V2 enum.
    order_args = OrderArgs(
        token_id=token_id,
        price=rounded_price,
        size=size_shares,
        side=Side.BUY,
    )
    options    = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)
    order_resp = get_clob_client().create_and_post_order(
        order_args=order_args,
        options=options,
        order_type=OrderType.GTC,
    )

    # ── Reconciliation ────────────────────────────────────────────────────────
    # The post response can return before the matching engine settles partial
    # fills. One short retry usually catches an immediate match without
    # significantly extending Lambda time. Anything still resting at this point
    # is reconciled on the next agent run during Phase 1.
    order_id    = _extract_order_id(order_resp)
    raw_status  = _extract_status(order_resp)
    filled, fill_status = _reconcile_fill(order_id, size_shares)
    if filled == 0 and raw_status.lower() in ("matched", "delayed"):
        time.sleep(1.5)
        filled, fill_status = _reconcile_fill(order_id, size_shares)

    entry_time = datetime.now(timezone.utc).isoformat()
    # AGENT_VARIANT lets a shared dashboard attribute every row to the agent
    # that opened it (used by the ABC experiment in ABC_TEST/). Empty string
    # for legacy / single-agent deploys — kept as a default to stay
    # backwards-compatible with the original GeoTrading stack.
    item = {
        "token_id":             token_id,
        "market_id":            market_id,
        "question":             market["question"],
        "market_slug":          market.get("market_slug", ""),
        # Event slug is needed to build the canonical Polymarket UI URL
        # (/event/<event_slug>/<market_slug>). Captured here at trade time
        # so the dashboard doesn't have to re-query Gamma per row.
        "event_slug":           market.get("event_slug", ""),
        "outcome":              outcome,
        "entry_price":          str(rounded_price),
        "size_shares":          str(filled),                # actual matched size
        "intended_shares":      str(size_shares),           # what we asked for
        "entry_time":           entry_time,
        "agent_variant":        os.environ.get("AGENT_VARIANT", ""),
        "source_headline":      source_headline,
        "theme_key":            theme_key,
        "crowd_score":          str(crowd_score),
        "clob_order_id":        order_id,
        "order_status":         fill_status or raw_status or "unknown",
        "tick_size":            str(tick_size),
        "neg_risk":             neg_risk,
        "tx_hash":              order_resp.get("transactionHash", "") if isinstance(order_resp, dict) else "",
        "status":               "open" if filled > 0 else "pending",
        "close_reason":         None,
        "close_price":          None,
        "close_time":           None,
        "pnl_usd":              None,
    }

    # Retry DDB write once; a failure here means a live order with no record
    for attempt in range(2):
        try:
            ddb.write_position(item)
            break
        except Exception as exc:
            if attempt == 0:
                logger.critical(
                    "DDB write failed after CLOB order (retrying). token=%s error=%s",
                    token_id, exc,
                )
            else:
                logger.critical(
                    "DDB write failed on retry. Manual reconciliation required. "
                    "token=%s item=%s", token_id, json.dumps(item, default=str),
                )
                raise

    logger.info(
        "Placed BUY: %s %s @ %.4f ($%.2f) | filled=%.2f/%.2f order_id=%s status=%s | signal=%r score=%.2f",
        outcome, token_id[:12] + "…", rounded_price, size_usdc, filled, size_shares,
        order_id[:16] if order_id else "?", fill_status or raw_status,
        source_headline, crowd_score,
    )
    return {"status": "placed", "token_id": token_id, "filled_shares": filled,
            "intended_shares": size_shares, "order_id": order_id,
            "order_status": fill_status or raw_status, "order": order_resp}
