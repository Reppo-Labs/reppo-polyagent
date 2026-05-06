import os
import logging
from functools import lru_cache

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

logger = logging.getLogger(__name__)

MARKET_LIMIT = 100


@lru_cache(maxsize=1)
def get_clob_client() -> ClobClient:
    """
    Initialise and cache the Polymarket CLOB client for the Lambda lifetime.
    Called lazily so handler.py can set env vars from Secrets Manager first.
    """
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=POLYGON,
        key=os.environ["POLYGON_PRIVATE_KEY"],
        funder=os.environ["POLYMARKET_WALLET_ADDRESS"],
        signature_type=2,  # EOA signing
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def get_open_markets() -> list[dict]:
    """
    Return the top MARKET_LIMIT active Polymarket markets sorted by volume.
    Filters to active=True, closed=False only.
    """
    clob = get_clob_client()
    markets: list[dict] = []
    next_cursor = ""

    while True:
        kwargs = {"next_cursor": next_cursor} if next_cursor else {}
        resp = clob.get_markets(**kwargs)

        for m in resp.data:
            if not m.active or m.closed:
                continue

            try:
                yes_token = next(t for t in m.tokens if t.outcome.lower() == "yes")
                no_token  = next(t for t in m.tokens if t.outcome.lower() == "no")
            except StopIteration:
                continue

            markets.append({
                "market_id":  m.condition_id,
                "question":   m.question,
                "yes_token":  yes_token.token_id,
                "no_token":   no_token.token_id,
                "yes_price":  float(yes_token.price),
                "no_price":   float(no_token.price),
                "volume":     float(m.volume or 0),
            })

        if not resp.next_cursor or resp.next_cursor == "LTE=":
            break
        next_cursor = resp.next_cursor

    markets.sort(key=lambda m: m["volume"], reverse=True)
    logger.info("Fetched %d active markets (returning top %d)", len(markets), MARKET_LIMIT)
    return markets[:MARKET_LIMIT]
