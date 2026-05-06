import json
import logging
import os
from datetime import datetime, timezone

import requests

from . import ddb
from .markets import get_clob_client, get_open_markets

logger = logging.getLogger(__name__)

# USDC contract on Polygon mainnet
_USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_POLYGON_RPC   = "https://polygon-rpc.com"
# balanceOf(address) ABI selector
_BALANCE_OF_SELECTOR = "0x70a08231"


def check_balance() -> dict:
    """Read USDC balance from Polygon via JSON-RPC (no web3 dependency needed)."""
    wallet = os.environ["POLYMARKET_WALLET_ADDRESS"].lower().replace("0x", "")
    data   = _BALANCE_OF_SELECTOR + wallet.zfill(64)

    resp = requests.post(
        _POLYGON_RPC,
        json={
            "jsonrpc": "2.0",
            "method":  "eth_call",
            "params":  [{"to": _USDC_CONTRACT, "data": data}, "latest"],
            "id":      1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    raw     = int(resp.json()["result"], 16)
    balance = raw / 1_000_000  # USDC has 6 decimals

    min_reserve = float(os.environ.get("MIN_BALANCE_RESERVE", "15.0"))
    logger.info("Wallet balance: $%.2f USDC (min reserve: $%.2f)", balance, min_reserve)

    return {
        "usdc":         round(balance, 2),
        "ok_to_trade":  balance >= min_reserve,
    }


def place_order(
    market_id: str,
    outcome: str,
    size_usdc: float,
    limit_price: float,
    source_headline: str = "",
    crowd_score: float = 0.0,
) -> dict:
    """
    Place a BUY limit order on Polymarket CLOB and write position to DynamoDB.
    Hard-caps order size regardless of LLM input.
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
    size_shares   = round(size_usdc / limit_price, 2)

    # Limit price sanity: must be within 5% of current market price
    if abs(limit_price - current_price) / max(current_price, 0.001) > 0.05:
        raise ValueError(
            f"limit_price {limit_price} is >5% from current {current_price:.3f}"
        )

    # No double entry on same token
    if ddb.get_position_by_token(token_id):
        raise ValueError(f"Open position already exists for token {token_id}")

    # No opposing position in the same market
    if ddb.get_open_positions_for_market(market_id):
        raise ValueError(f"Open position already exists for market {market_id}")

    if dry_run:
        logger.info(
            "[DRY_RUN] place_order: %s %s @ %.3f ($%.2f) — %s",
            outcome, token_id[:12] + "…", limit_price, size_usdc, market["question"],
        )
        return {"status": "dry_run", "market_id": market_id, "outcome": outcome,
                "size_usdc": size_usdc, "limit_price": limit_price}

    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY

    order_args = OrderArgs(token_id=token_id, price=limit_price, size=size_shares, side=BUY)
    order_resp = get_clob_client().create_and_post_order(order_args)

    entry_time = datetime.now(timezone.utc).isoformat()
    item = {
        "token_id":        token_id,
        "market_id":       market_id,
        "question":        market["question"],
        "outcome":         outcome,
        "entry_price":     str(limit_price),
        "size_shares":     str(size_shares),
        "entry_time":      entry_time,
        "source_headline": source_headline,
        "crowd_score":     str(crowd_score),
        "status":          "open",
        "close_reason":    None,
        "close_price":     None,
        "close_time":      None,
        "pnl_usd":         None,
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
                    "token=%s item=%s", token_id, json.dumps(item),
                )
                raise

    logger.info(
        "Placed BUY: %s %s @ %.3f ($%.2f) | signal=%r score=%.2f",
        outcome, token_id[:12] + "…", limit_price, size_usdc, source_headline, crowd_score,
    )
    return {"status": "placed", "token_id": token_id, "size_shares": size_shares,
            "order": order_resp}
