"""
Polymarket settlement helpers (public CLOB REST only — no SDK / wallet).

Used to close DynamoDB rows when a market has resolved but the order book is
gone, so dashboard P&L and win rate stay aligned with Polymarket.

Lives at package root (not under agent.tools) so tests can import it without
pulling in py_clob_client_v2 via agent.tools.__init__.
"""
import logging

import requests as _requests

logger = logging.getLogger(__name__)


def fetch_clob_market_payload(condition_id: str) -> dict | None:
    """
    Fetch a single market from the public CLOB REST API (no auth).

    Unlike Gamma's active-only listing, this endpoint returns **closed**
    markets with per-outcome `winner` flags — the authoritative signal for
    whether our conditional token redeemed at $1 or $0 after resolution.
    """
    if not condition_id or not str(condition_id).startswith("0x"):
        return None
    url = f"https://clob.polymarket.com/markets/{condition_id}"
    try:
        resp = _requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "polyagent/1.0"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("fetch_clob_market_payload(%s…) failed: %s", condition_id[:12], exc)
        return None


def redemption_price_per_share(market_payload: dict, token_id: str) -> float | None:
    """
    When `closed` is true and the outcome is finalized, return 1.0 if this
    token won or 0.0 if it lost. Returns None if the market is not closed, the
    token is unknown, or resolution is still ambiguous (e.g. disputed).
    """
    if not market_payload.get("closed"):
        return None
    for t in market_payload.get("tokens") or []:
        if str(t.get("token_id")) != str(token_id):
            continue
        if t.get("winner") is True:
            return 1.0
        if t.get("winner") is False:
            return 0.0
        try:
            p = float(t.get("price", 0))
        except (TypeError, ValueError):
            return None
        if p >= 0.99:
            return 1.0
        if p <= 0.01:
            return 0.0
        return None
    return None
