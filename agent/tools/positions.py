import math
import os
import logging
from datetime import datetime, timezone

from py_clob_client_v2 import (
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

from . import ddb

logger = logging.getLogger(__name__)

TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.50"))
STOP_LOSS_PCT   = float(os.environ.get("STOP_LOSS_PCT",   "0.30"))

# ── Tick-based stop-loss for low-priced markets ───────────────────────────────
#
# A fixed -30% SL is well-calibrated for $0.20–$0.80 markets but breaks down at
# the bottom of the book. Example: entry=$0.031, tick=$0.001 → -30% triggers
# at $0.0217 = 9 ticks away. But normal microstructure noise on a low-volume
# tail can blow through 9 ticks in a single quote, forcing exit at near-zero
# on what should be a small option-style bet.
#
# When entry_price < LOW_PRICE_THRESHOLD, we switch from a percent-based SL to
# an absolute distance of LOW_PRICE_SL_TICKS ticks. The default values mean:
#   entry $0.03, tick 0.001, ticks=15 → SL at $0.015 (-50%, but absolute)
# rather than -30% (=$0.021, only 9 ticks). The change buys headroom without
# letting losses run unbounded.
LOW_PRICE_THRESHOLD = float(os.environ.get("LOW_PRICE_THRESHOLD", "0.10"))
LOW_PRICE_SL_TICKS  = int(  os.environ.get("LOW_PRICE_SL_TICKS",  "15"))

# ── Trailing take-profit ──────────────────────────────────────────────────────
#
# A fixed +50% TP catches home-runs but does nothing to protect winners that
# reverse before reaching the line. Trailing TP fills that gap by tracking the
# position's running peak unrealised return and exiting when too much of it is
# given back.
#
# Activation arms the trail only after a position has reached a *real* gain;
# we don't want to exit on micro-fluctuations near entry. Once armed, we exit
# if the current return falls below `peak * (1 - giveback)`.
#
# Defaults: activate at +30%, exit on 50% giveback → minimum locked-in profit
# when trailing fires is +15%. A position that reaches +50% takes the fixed
# TP first, so this only governs the +ACTIVATE → +50% band.
#
# The peak is persisted on each Phase 1 sweep into the DDB row as
# `peak_pnl_pct`. Existing positions without that attribute initialise to 0 on
# the next sweep and trail forward from there — no migration needed.
TRAIL_ACTIVATE_PCT  = float(os.environ.get("TRAIL_ACTIVATE_PCT", "0.30"))
TRAIL_GIVEBACK_PCT  = float(os.environ.get("TRAIL_GIVEBACK_PCT", "0.50"))


def _clob():
    from .markets import get_clob_client
    return get_clob_client()


def _market_meta(market_id: str) -> dict:
    from .markets import get_market_meta
    return get_market_meta(market_id)


def _floor_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    ticks = math.floor(price / tick_size + 1e-9)
    rounded = ticks * tick_size
    decimals = max(0, -math.floor(math.log10(tick_size)))
    return round(rounded, decimals)


def _reconcile_pending(pos: dict) -> dict:
    """
    For positions written with status='pending' (resting GTC unfilled at entry
    time), poll get_order to update size_shares with any new fills. Mutates the
    DDB row and returns the reconciled position dict.
    """
    order_id = pos.get("clob_order_id") or ""
    if not order_id:
        return pos

    try:
        order = _clob().get_order(order_id)
    except Exception as exc:
        logger.warning("reconcile get_order(%s) failed: %s", order_id[:12], exc)
        return pos

    if not isinstance(order, dict):
        return pos

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

    intended = float(pos.get("intended_shares") or pos.get("size_shares") or 0)
    matched = min(matched, intended) if intended else matched
    new_status_raw = order.get("status", pos.get("order_status", "unknown"))
    new_position_status = "open" if matched > 0 else "pending"

    # Only write back if anything actually changed
    old_size  = float(pos.get("size_shares") or 0)
    old_state = pos.get("status")
    if matched != old_size or new_position_status != old_state:
        ddb.update_position_fill(
            token_id=pos["token_id"],
            size_shares=matched,
            status=new_position_status,
            order_status=new_status_raw,
        )
        pos = {**pos,
               "size_shares":   matched,
               "status":        new_position_status,
               "order_status":  new_status_raw}
        logger.info(
            "Reconciled %s: filled %.2f/%.2f status=%s",
            pos["token_id"][:12] + "…", matched, intended, new_status_raw,
        )
    return pos


def get_positions() -> list[dict]:
    """
    Phase 1: fetch all open & pending positions from DynamoDB, reconcile any
    pending fills via get_order, enrich with live CLOB best-bid price, compute
    P&L, and pre-calculate TP/SL flags.

    `current_price` is None when the order book is unavailable; in that case
    `pnl_pct`, `hit_take_profit`, and `hit_stop_loss` are also None and
    `is_stale=True`. Do NOT close on stale prices — the LLM is told to skip
    these and re-evaluate on the next run.
    """
    positions = ddb.get_open_or_pending_positions()
    result = []

    for pos in positions:
        # Reconcile any unfilled GTCs first so size_shares reflects reality
        if pos.get("status") == "pending" or float(pos.get("size_shares") or 0) == 0:
            pos = _reconcile_pending(pos)

        token_id    = pos["token_id"]
        entry_price = float(pos["entry_price"])
        size_shares = float(pos.get("size_shares") or 0)

        is_stale      = False
        current_price = None    # best bid — conservative, drives SL/TP
        mid_price     = None    # (best bid + best ask)/2 — for display only
        try:
            book = _clob().get_order_book(token_id)
            # V2 returns a dict (httpx → resp.json()), not a dataclass.
            bids = (book or {}).get("bids") or []
            asks = (book or {}).get("asks") or []
            best_bid = max(float(b["price"]) for b in bids) if bids else None
            best_ask = min(float(a["price"]) for a in asks) if asks else None
            # Best bid is what we'd receive selling right now — used for
            # hit_take_profit / hit_stop_loss decisions. Mid is for the
            # dashboard / human display so our numbers align with how
            # Polymarket UI marks open positions.
            current_price = best_bid
            if best_bid is not None and best_ask is not None:
                mid_price = (best_bid + best_ask) / 2.0
            else:
                mid_price = best_bid  # one-sided book fallback
        except Exception as exc:
            logger.warning("Order book unavailable for %s: %s", token_id, exc)

        # Use the stored tick_size when available (every position written by
        # place_order has it). Fall back to 0.01 for legacy rows that don't.
        try:
            row_tick = float(pos.get("tick_size") or 0.01)
        except (TypeError, ValueError):
            row_tick = 0.01

        sl_mode = "percent"  # for telemetry only — surfaced to the LLM below

        # Prior peak from DDB (persisted across runs). Missing → 0.
        try:
            prior_peak = float(pos.get("peak_pnl_pct") or 0.0)
        except (TypeError, ValueError):
            prior_peak = 0.0

        if current_price is None:
            is_stale = True
            pnl_pct = None
            pnl_usd = None
            hit_tp  = None
            hit_sl  = None
            hit_trail = None
            peak_pnl_pct = prior_peak
        else:
            pnl_pct = (current_price - entry_price) / entry_price if entry_price else 0.0
            pnl_usd = (current_price - entry_price) * size_shares
            hit_tp  = pnl_pct >= TAKE_PROFIT_PCT

            # Tail-price markets get a tick-based SL instead of a percent SL.
            # See the LOW_PRICE_THRESHOLD docstring at module top for rationale.
            if entry_price > 0 and entry_price < LOW_PRICE_THRESHOLD and row_tick > 0:
                sl_trigger_price = entry_price - LOW_PRICE_SL_TICKS * row_tick
                hit_sl = current_price <= sl_trigger_price
                sl_mode = "ticks"
            else:
                hit_sl = pnl_pct <= -STOP_LOSS_PCT

            # Update running peak. We only ever ratchet the peak up — it
            # never decreases — so a winner that reverses keeps its earlier
            # high-water mark for the trail floor.
            peak_pnl_pct = max(prior_peak, pnl_pct)
            if peak_pnl_pct > prior_peak + 1e-6:
                try:
                    ddb.update_peak_pnl(token_id, peak_pnl_pct)
                except Exception as exc:
                    # Non-fatal: trailing TP just won't be sticky across runs.
                    logger.warning("update_peak_pnl(%s) failed: %s", token_id[:12], exc)

            # Trailing TP arms only after peak crosses ACTIVATE. Once armed,
            # we exit when current return falls below peak × (1 - GIVEBACK).
            hit_trail = False
            if peak_pnl_pct >= TRAIL_ACTIVATE_PCT:
                trail_floor = peak_pnl_pct * (1.0 - TRAIL_GIVEBACK_PCT)
                hit_trail = pnl_pct <= trail_floor

        # Mid-priced P&L for display (matches how Polymarket UI marks open
        # positions). Best-bid P&L (above) remains authoritative for SL/TP.
        if mid_price is not None and entry_price:
            pnl_usd_mid = (mid_price - entry_price) * size_shares
        else:
            pnl_usd_mid = None

        result.append({
            "token_id":         token_id,
            "market_id":        pos.get("market_id"),
            "question":         pos.get("question"),
            "outcome":          pos.get("outcome"),
            "entry_price":      entry_price,
            "current_price":    round(current_price, 4) if current_price is not None else None,
            "mid_price":        round(mid_price, 4) if mid_price is not None else None,
            "size_shares":      size_shares,
            "intended_shares":  float(pos.get("intended_shares") or size_shares),
            "pnl_pct":          round(pnl_pct, 4) if pnl_pct is not None else None,
            "pnl_usd":          round(pnl_usd, 4) if pnl_usd is not None else None,
            "pnl_usd_mid":      round(pnl_usd_mid, 4) if pnl_usd_mid is not None else None,
            "peak_pnl_pct":     round(peak_pnl_pct, 4),
            "hit_take_profit":  hit_tp,
            "hit_trailing_tp":  hit_trail,
            "hit_stop_loss":    hit_sl,
            "sl_mode":          sl_mode,
            "is_stale":         is_stale,
            "position_status":  pos.get("status"),
            "source_headline":  pos.get("source_headline"),
            "theme_key":        pos.get("theme_key"),
            "crowd_score":      pos.get("crowd_score"),
        })

    return result


def close_position(
    token_id: str,
    size_shares: float,
    limit_price: float,
    reason: str,
) -> dict:
    """Place a SELL limit order and mark the DDB position as closed."""
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    # ── Pre-check: position must exist and be open ────────────────────────────
    # Without this, an LLM-hallucinated token_id sends a SELL to the CLOB for
    # shares we don't own, wasting a round-trip and risking rate-limit penalties.
    position = ddb.get_position_by_token(token_id)
    if not position or position.get("status") not in ("open", "pending"):
        raise ValueError(
            f"No open position for token {token_id} (DDB status="
            f"{position.get('status') if position else 'missing'!r})"
        )

    held_shares = float(position.get("size_shares") or 0)
    if held_shares <= 0:
        raise ValueError(
            f"Position {token_id} has zero filled shares — nothing to close. "
            "Wait for fills or cancel the resting order."
        )
    # Don't sell more than we hold (LLM can pass stale `size_shares` from a
    # previous get_positions call that has since been partially closed).
    size_shares = min(size_shares, held_shares)

    market_id = position.get("market_id")
    if not market_id:
        raise ValueError(f"Position {token_id} missing market_id; cannot resolve tick size")

    # ── Tick-round and clamp the limit price ──────────────────────────────────
    meta      = _market_meta(market_id)
    tick_size = meta["tick_size"]
    neg_risk  = meta["neg_risk"]

    rounded_price = _floor_to_tick(limit_price, tick_size)
    rounded_price = max(rounded_price, tick_size)
    rounded_price = min(rounded_price, 1 - tick_size)

    if dry_run:
        logger.info(
            "[DRY_RUN] close_position: token=%s size=%.2f price=%.4f reason=%s",
            token_id, size_shares, rounded_price, reason,
        )
        return {"status": "dry_run", "token_id": token_id, "reason": reason,
                "size_shares": size_shares, "limit_price": rounded_price}

    # CTF allowance must be set before the first SELL on this outcome token —
    # without it the order signs but the on-chain transfer fails (issue #311).
    from .markets import ensure_conditional_allowance
    ensure_conditional_allowance(token_id)

    order_args = OrderArgs(
        token_id=token_id,
        price=rounded_price,
        size=size_shares,
        side=Side.SELL,
    )
    options = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)
    order_resp = _clob().create_and_post_order(
        order_args=order_args,
        options=options,
        order_type=OrderType.GTC,
    )

    entry_price = float(position["entry_price"])
    pnl_usd     = round((rounded_price - entry_price) * size_shares, 4)
    close_time  = datetime.now(timezone.utc).isoformat()

    ddb.update_position_closed(
        token_id=token_id,
        close_price=rounded_price,
        close_reason=reason,
        close_time=close_time,
        pnl_usd=pnl_usd,
    )

    logger.info(
        "Closed %s | reason=%s size=%.2f price=%.4f pnl_usd=%.2f",
        token_id[:12] + "…", reason, size_shares, rounded_price, pnl_usd,
    )
    return {"status": "closed", "token_id": token_id, "size_shares": size_shares,
            "limit_price": rounded_price, "pnl_usd": pnl_usd, "order": order_resp}
