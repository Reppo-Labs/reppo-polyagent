import os
import logging
from datetime import datetime, timezone

from . import ddb

logger = logging.getLogger(__name__)

TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.50"))
STOP_LOSS_PCT   = float(os.environ.get("STOP_LOSS_PCT",   "0.30"))


def _clob():
    from .markets import get_clob_client
    return get_clob_client()


def get_positions() -> list[dict]:
    """
    Phase 1: fetch all open positions from DynamoDB, enrich with live CLOB
    best-bid price, compute P&L, and pre-calculate TP/SL flags.
    """
    open_positions = ddb.get_open_positions()
    result = []

    for pos in open_positions:
        token_id    = pos["token_id"]
        entry_price = float(pos["entry_price"])
        size_shares = float(pos["size_shares"])

        try:
            book  = _clob().get_order_book(token_id)
            bids  = book.bids or []
            # Best bid is what we'd receive selling right now
            current_price = float(bids[0].price) if bids else entry_price
        except Exception as exc:
            logger.warning("Order book unavailable for %s: %s", token_id, exc)
            current_price = entry_price

        pnl_pct = (current_price - entry_price) / entry_price
        pnl_usd = (current_price - entry_price) * size_shares

        result.append({
            "token_id":         token_id,
            "question":         pos.get("question"),
            "outcome":          pos.get("outcome"),
            "entry_price":      entry_price,
            "current_price":    round(current_price, 4),
            "size_shares":      size_shares,
            "pnl_pct":          round(pnl_pct, 4),
            "pnl_usd":          round(pnl_usd, 4),
            "hit_take_profit":  pnl_pct >= TAKE_PROFIT_PCT,
            "hit_stop_loss":    pnl_pct <= -STOP_LOSS_PCT,
            "source_headline":  pos.get("source_headline"),
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

    if dry_run:
        logger.info(
            "[DRY_RUN] close_position: token=%s size=%.2f price=%.3f reason=%s",
            token_id, size_shares, limit_price, reason,
        )
        return {"status": "dry_run", "token_id": token_id, "reason": reason}

    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import SELL

    order_args = OrderArgs(token_id=token_id, price=limit_price, size=size_shares, side=SELL)
    order_resp = _clob().create_and_post_order(order_args)

    position    = ddb.get_position_by_token(token_id)
    entry_price = float(position["entry_price"]) if position else limit_price
    pnl_usd     = round((limit_price - entry_price) * size_shares, 4)
    close_time  = datetime.now(timezone.utc).isoformat()

    ddb.update_position_closed(
        token_id=token_id,
        close_price=limit_price,
        close_reason=reason,
        close_time=close_time,
        pnl_usd=pnl_usd,
    )

    logger.info("Closed %s | reason=%s pnl_usd=%.2f", token_id, reason, pnl_usd)
    return {"status": "closed", "token_id": token_id, "pnl_usd": pnl_usd, "order": order_resp}
