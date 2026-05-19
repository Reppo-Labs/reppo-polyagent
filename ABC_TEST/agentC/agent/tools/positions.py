import math
import os
import logging
import time
from datetime import datetime, timezone

from py_clob_client_v2 import (
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
    TradeParams,
)

from ..settlement import fetch_clob_market_payload, redemption_price_per_share

from . import ddb

logger = logging.getLogger(__name__)

TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.50"))
STOP_LOSS_PCT   = float(os.environ.get("STOP_LOSS_PCT",   "0.30"))

# ── Absolute-dollar stop-loss backstop ────────────────────────────────────────
#
# The percent SL and tick SL both express loss as a *fraction* of entry. On
# cheap markets in ticks mode, a -50% drawdown is tolerated by design so a
# tail position is not whipsawed out on noise. But that policy let positions
# like Iran-regime-fall (entry $0.02, 250 shares) bleed to -$2.50 unrealised
# without a flag firing.
#
# MAX_ABS_LOSS_USD is the dollar floor that overrides both percent and tick
# logic: once pnl_usd <= -MAX_ABS_LOSS_USD, hit_stop_loss = true regardless of
# sl_mode. This keeps a single trade from dragging the experiment budget by
# more than the configured dollar cap.
MAX_ABS_LOSS_USD = float(os.environ.get("MAX_ABS_LOSS_USD", "2.50"))

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


# CLOB order statuses that mean "no fill and no longer working" — drop from dashboard.
_TERMINAL_ZERO_FILL = frozenset({
    "canceled", "cancelled", "expired", "invalid", "unmatched",
    "killed", "rejected", "failed",
})


def _order_matched_shares(order: dict, intended: float) -> float:
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
    return min(matched, intended) if intended else matched


def _order_status_str(order: dict) -> str:
    return str(order.get("status") or order.get("order_status") or "unknown").lower()


def _close_stale_order_row(pos: dict, reason: str, now: str) -> None:
    """Remove a DDB row that has no live CLOB order / no fills from the open book."""
    token_id = pos["token_id"]
    entry = float(pos.get("entry_price") or 0)
    ddb.update_position_closed(
        token_id=token_id,
        close_price=entry,
        close_reason=reason,
        close_time=now,
        pnl_usd=0.0,
    )
    logger.info(
        "Closed stale order row token=%s reason=%s question=%r",
        token_id[:12] + "…", reason, (pos.get("question") or "")[:60],
    )


def reconcile_clob_orders() -> dict:
    """
    Align DDB open/pending rows with Polymarket CLOB order state.

    The dashboard reads DynamoDB, not the Polymarket UI. Rows can linger when:
      - a resting GTC was cancelled/expired on CLOB but never reconciled;
      - place_order wrote DDB but the order id is missing or unknown to CLOB.
    """
    summary = {"checked": 0, "closed": 0, "updated": 0, "kept": 0}
    now = datetime.now(timezone.utc).isoformat()

    for pos in ddb.get_open_or_pending_positions():
        summary["checked"] += 1
        order_id = (pos.get("clob_order_id") or "").strip()
        intended = float(pos.get("intended_shares") or pos.get("size_shares") or 0)

        if not order_id:
            if float(pos.get("size_shares") or 0) == 0:
                _close_stale_order_row(pos, "no_order_id", now)
                summary["closed"] += 1
            else:
                summary["kept"] += 1
            continue

        try:
            order = _clob().get_order(order_id)
        except Exception as exc:
            err = str(exc).lower()
            if "404" in err or "not found" in err or "does not exist" in err:
                _close_stale_order_row(pos, "order_not_found", now)
                summary["closed"] += 1
            else:
                logger.warning("reconcile_clob_orders get_order(%s): %s", order_id[:12], exc)
                summary["kept"] += 1
            continue

        if not isinstance(order, dict) or order.get("error"):
            _close_stale_order_row(pos, "order_not_found", now)
            summary["closed"] += 1
            continue

        matched = _order_matched_shares(order, intended)
        ostatus = _order_status_str(order)
        raw_status = order.get("status") or order.get("order_status") or "unknown"

        if matched <= 0 and ostatus in _TERMINAL_ZERO_FILL:
            _close_stale_order_row(pos, "order_cancelled", now)
            summary["closed"] += 1
            continue

        new_position_status = "open" if matched > 0 else "pending"
        old_size = float(pos.get("size_shares") or 0)
        old_state = pos.get("status")
        if matched != old_size or new_position_status != old_state:
            ddb.update_position_fill(
                token_id=pos["token_id"],
                size_shares=matched,
                status=new_position_status,
                order_status=raw_status,
            )
            summary["updated"] += 1
            logger.info(
                "reconcile_clob_orders %s: filled %.2f/%.2f clob=%s",
                pos["token_id"][:12] + "…", matched, intended, raw_status,
            )
        else:
            summary["kept"] += 1

    return summary


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

    if not isinstance(order, dict) or order.get("error"):
        return pos

    intended = float(pos.get("intended_shares") or pos.get("size_shares") or 0)
    matched = _order_matched_shares(order, intended)
    ostatus = _order_status_str(order)
    new_status_raw = order.get("status", pos.get("order_status", "unknown"))

    if matched <= 0 and ostatus in _TERMINAL_ZERO_FILL:
        now = datetime.now(timezone.utc).isoformat()
        _close_stale_order_row(pos, "order_cancelled", now)
        return {**pos, "status": "closed", "size_shares": 0, "order_status": new_status_raw}

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
        intended_shares = float(pos.get("intended_shares") or size_shares or 0)
        # Resting GTCs: use intended size for *display* mid P&L until fills land.
        display_shares = size_shares if size_shares > 0 else intended_shares

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

            # Absolute-dollar backstop — folded into hit_stop_loss so the
            # existing Phase 1 reason="stop_loss" path handles it without
            # schema changes. Triggers when a position's dollar P&L drops
            # below -MAX_ABS_LOSS_USD even if percent / tick SL did not fire.
            if pnl_usd is not None and pnl_usd <= -MAX_ABS_LOSS_USD:
                if not hit_sl:
                    # Annotate sl_mode so the dashboard / logs know which rule
                    # actually fired the close on this position.
                    sl_mode = "abs_dollar"
                hit_sl = True

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
            pnl_usd_mid = (mid_price - entry_price) * display_shares
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


def reconcile_resolved_positions() -> dict:
    """
    Close any open/pending DDB rows whose Polymarket market has already resolved.

    After resolution the CLOB order book is often removed (404), which makes
    Phase 1 mark positions `is_stale` and prevents `close_position` from running.
    Here we read `GET /markets/{condition_id}` — still available for closed
    markets — and stamp `status=closed`, `close_price` ∈ {0,1}, `pnl_usd`, and
    `close_reason=resolution` so the dashboard win rate and P&L match Polymarket.
    """
    summary = {"checked": 0, "closed": 0, "skipped": 0}
    now = datetime.now(timezone.utc).isoformat()
    payload_cache: dict[str, dict | None] = {}

    # Full-table scan (paginated) — never miss an open row vs a single Scan page.
    open_rows = [
        r for r in ddb.scan_all_positions()
        if r.get("status") in ("open", "pending")
    ]
    logger.info("reconcile_resolved_positions: %d open/pending row(s) to evaluate", len(open_rows))

    for pos in open_rows:
        summary["checked"] += 1
        market_id = pos.get("market_id")
        token_id   = pos.get("token_id")
        if not market_id or not token_id:
            summary["skipped"] += 1
            continue

        shares = float(pos.get("size_shares") or 0)
        if shares <= 0:
            summary["skipped"] += 1
            continue

        if market_id not in payload_cache:
            payload_cache[market_id] = fetch_clob_market_payload(market_id)
        payload = payload_cache[market_id]
        if not payload:
            summary["skipped"] += 1
            continue

        settle = redemption_price_per_share(payload, token_id)
        if settle is None:
            summary["skipped"] += 1
            continue

        try:
            entry = float(pos["entry_price"])
        except (TypeError, ValueError, KeyError):
            summary["skipped"] += 1
            continue

        pnl_usd = round((settle - entry) * shares, 4)
        try:
            ddb.update_position_closed(
                token_id=token_id,
                close_price=settle,
                close_reason="resolution",
                close_time=now,
                pnl_usd=pnl_usd,
            )
        except Exception as exc:
            logger.error(
                "reconcile_resolved_positions: DDB close failed token=%s: %s",
                token_id[:12],
                exc,
            )
            continue

        summary["closed"] += 1
        q = (pos.get("question") or "")[:72]
        logger.info(
            "Resolution ledger close | %s | settle=%.1f entry=%.4f shares=%.2f pnl_usd=%.2f",
            q,
            settle,
            entry,
            shares,
            pnl_usd,
        )

    return summary


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
    close_time  = datetime.now(timezone.utc).isoformat()

    # ── Reconcile actual fill price (was: assume limit_price) ─────────────────
    #
    # The CLOB matches SELL orders at the best available BID, which is almost
    # always strictly better than our limit. Recording close_price=rounded_price
    # therefore *overstates* the realised loss whenever the book had bids above
    # the floor we sent. Polymarket's own activity ledger reflects the true
    # average fill price (taker_amount / maker_amount), so to keep our P&L in
    # sync with Polymarket we must do the same — read the trades emitted from
    # this order and compute the volume-weighted average.
    order_id = ""
    if isinstance(order_resp, dict):
        order_id = (
            order_resp.get("orderID")
            or order_resp.get("order_id")
            or order_resp.get("id")
            or ""
        )

    vwap_price, filled_shares, fill_status = _reconcile_sell_vwap(
        order_id=order_id,
        token_id=token_id,
        intended_shares=size_shares,
        limit_price=rounded_price,
    )

    pnl_usd = round((vwap_price - entry_price) * filled_shares, 4)

    ddb.update_position_closed(
        token_id=token_id,
        close_price=vwap_price,
        close_reason=reason,
        close_time=close_time,
        pnl_usd=pnl_usd,
        close_order_id=order_id,
    )

    if filled_shares + 1e-6 < size_shares:
        logger.warning(
            "Closed %s | PARTIAL FILL filled=%.2f/%.2f (status=%s) — "
            "remaining %.2f shares may still rest on the book.",
            token_id[:12] + "…", filled_shares, size_shares, fill_status,
            size_shares - filled_shares,
        )

    logger.info(
        "Closed %s | reason=%s filled=%.2f vwap=%.4f (limit=%.4f) pnl_usd=%.2f",
        token_id[:12] + "…", reason, filled_shares, vwap_price, rounded_price, pnl_usd,
    )
    return {"status": "closed", "token_id": token_id,
            "size_shares": filled_shares, "limit_price": rounded_price,
            "fill_price": vwap_price, "pnl_usd": pnl_usd, "order": order_resp}


# ── Sell-fill reconciliation helpers ──────────────────────────────────────────
#
# After posting a SELL we need to know (a) how many shares actually matched and
# (b) at what average price. The V2 SDK exposes these via two calls:
#   client.get_order(order_id)                       — size_matched
#   client.get_trades(TradeParams(asset_id=…))       — per-trade size & price
#
# Per the official docs each trade carries `taker_order_id` so we can filter
# the recent trade list down to ours. We then compute VWAP across the matched
# trades.

def _trade_field(trade: dict, *keys: str, default=None):
    """Pull the first non-empty field from a trade dict (V2 API uses snake_case)."""
    for k in keys:
        v = trade.get(k)
        if v not in (None, ""):
            return v
    return default


def _fetch_recent_trades_for_order(token_id: str, order_id: str) -> list[dict]:
    """
    Return the trades that this SELL order took part in, by paginating
    `get_trades(asset_id=token_id)` and filtering on `taker_order_id`.

    asset_id is the token (one of the YES/NO conditional tokens), which scopes
    the query enough to keep the response small. We do not filter by `id`
    server-side because that field on TradeParams refers to a single trade id,
    not the originating order id.
    """
    try:
        trades = _clob().get_trades(TradeParams(asset_id=token_id))
    except Exception as exc:
        logger.warning("get_trades(%s) failed: %s", token_id[:12], exc)
        return []

    if not isinstance(trades, list):
        return []

    oid = (order_id or "").lower()
    matched = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        toid = (_trade_field(t, "taker_order_id", "takerOrderID", default="") or "").lower()
        if toid and toid == oid:
            matched.append(t)
    return matched


def _reconcile_sell_vwap(
    *,
    order_id: str,
    token_id: str,
    intended_shares: float,
    limit_price: float,
) -> tuple[float, float, str]:
    """
    Resolve the VWAP fill price and the actual filled size for a SELL order.

    Strategy:
      1. Brief sleep so the CLOB matching engine emits trades.
      2. Call get_order(order_id) → size_matched.
      3. Call get_trades(asset_id=token_id) → filter to taker_order_id==order_id
         → compute VWAP from each trade's (size, price).
      4. If trades are not yet visible (asynchronous settlement), retry once.

    Falls back to (limit_price, intended_shares, "fallback_no_trades") when
    nothing usable is returned — that matches the previous behaviour exactly,
    so we never regress relative to the pre-fix code.
    """
    if not order_id:
        return (limit_price, intended_shares, "no_order_id")

    # The CLOB sometimes needs a beat to publish the trade. wallet.py uses
    # 1.5s after place_order; we mirror that here.
    time.sleep(1.5)

    matched_size = 0.0
    order_status = "unknown"
    try:
        order = _clob().get_order(order_id)
        if isinstance(order, dict):
            order_status = order.get("status", "unknown")
            ms_raw = (
                order.get("size_matched")
                or order.get("sizeMatched")
                or order.get("matched_size")
                or "0"
            )
            try:
                matched_size = float(ms_raw)
            except (TypeError, ValueError):
                matched_size = 0.0
    except Exception as exc:
        logger.warning("get_order(%s) failed during close reconcile: %s",
                       order_id[:12], exc)

    trades = _fetch_recent_trades_for_order(token_id, order_id)
    if not trades and matched_size > 0:
        # Order reports a fill but trade publish lags — give it one more beat.
        time.sleep(1.5)
        trades = _fetch_recent_trades_for_order(token_id, order_id)

    if not trades:
        logger.warning(
            "No trades found for SELL order %s on token %s — falling back to "
            "limit price for close. Run scripts/reconcile_closes.py to correct.",
            (order_id or "")[:14], token_id[:12],
        )
        return (limit_price, intended_shares, f"no_trades_{order_status}")

    # Sum (size * price) and total size across all trades attributed to us.
    total_size  = 0.0
    total_value = 0.0
    for t in trades:
        try:
            size  = float(_trade_field(t, "size", default=0) or 0)
            price = float(_trade_field(t, "price", default=0) or 0)
        except (TypeError, ValueError):
            continue
        if size > 0 and price > 0:
            total_size  += size
            total_value += size * price

    if total_size <= 0:
        logger.warning(
            "Trades returned but with zero usable size for order %s — falling "
            "back to limit price.", (order_id or "")[:14],
        )
        return (limit_price, intended_shares, "zero_size_trades")

    vwap = total_value / total_size
    # Clamp filled to intended in case the API rounds up.
    filled = min(total_size, intended_shares) if intended_shares else total_size
    return (vwap, filled, f"reconciled_{order_status}")
