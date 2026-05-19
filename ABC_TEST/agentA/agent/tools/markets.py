import json
import logging
import os
from functools import lru_cache

import requests as _requests
from py_clob_client_v2 import (
    AssetType,
    BalanceAllowanceParams,
    BuilderConfig,
    ClobClient,
    SignatureTypeV2,
)

logger = logging.getLogger(__name__)


# ── SDK noise filter ──────────────────────────────────────────────────────────
#
# The V2 SDK logs an ERROR every time create_or_derive_api_key() POSTs to
# /auth/api-key and gets a 400 "Could not create api key" — which is the
# *expected* response when the key already exists (the SDK then falls back to
# derive_api_creds, succeeds, and trading proceeds normally).
#
# Each cold start triggers one of these; we were seeing 96/day in CloudWatch,
# drowning out real auth issues. We downgrade THIS specific message to INFO,
# and leave every other "request error" untouched so a genuine auth break
# still shows up as ERROR.
class _ApiKeyAlreadyExistsFilter(logging.Filter):
    _MARKER = "Could not create api key"

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if self._MARKER in msg and record.levelno == logging.ERROR:
            record.levelno = logging.INFO
            record.levelname = "INFO"
        return True


# Attach the filter once to the SDK's logger. lru_cache is the cleanest way
# to make this idempotent across warm-start invocations.
@lru_cache(maxsize=1)
def _install_sdk_log_filter() -> None:
    sdk_log = logging.getLogger("py_clob_client_v2")
    sdk_log.addFilter(_ApiKeyAlreadyExistsFilter())


_install_sdk_log_filter()

MARKET_LIMIT       = 300
MARKET_FETCH_LIMIT = 1000   # over-fetch when geo-filtering so we still fill MARKET_LIMIT
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
POLYGON_CHAIN_ID = 137


@lru_cache(maxsize=1)
def get_clob_client() -> ClobClient:
    """
    Initialise and cache the Polymarket CLOB V2 client for the Lambda lifetime.

    V2 attribution: a `bytes32` builder_code attached to BuilderConfig is auto-applied
    to every order signed by this client (see py_clob_client_v2.client.create_order).
    The HMAC builder API key flow is gone — V2 uses on-chain attribution instead.
    """
    builder_code = os.environ["BUILDER_CODE"]
    builder_config = BuilderConfig(builder_code=builder_code)
    logger.info("Builder code configured: %s...", builder_code[:10])

    # signature_type defaults to POLY_1271 (3) — the modern Polymarket
    # "deposit wallet" model used by accounts created via the website's
    # embedded-wallet flow (Privy/Turnkey). Older Magic-Link accounts use
    # POLY_PROXY (1). Override via the SIGNATURE_TYPE env var if needed.
    #
    # We learned this the hard way: with sig_type=1, the CLOB rejects orders
    # on this account with "maker address not allowed, please use the
    # deposit wallet flow". POLY_1271 is what the canonical
    # gtc_limit_buy_deposit_wallet.py example uses, and it accepts cleanly.
    sig_type_env = os.environ.get("SIGNATURE_TYPE", "POLY_1271").upper()
    sig_type = {
        "EOA":              int(SignatureTypeV2.EOA),
        "POLY_PROXY":       int(SignatureTypeV2.POLY_PROXY),
        "POLY_GNOSIS_SAFE": int(SignatureTypeV2.POLY_GNOSIS_SAFE),
        "POLY_1271":        int(SignatureTypeV2.POLY_1271),
    }.get(sig_type_env, int(SignatureTypeV2.POLY_1271))
    logger.info("ClobClient signature_type=%s (%d)", sig_type_env, sig_type)

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=POLYGON_CHAIN_ID,
        key=os.environ["POLYGON_PRIVATE_KEY"],
        signature_type=sig_type,
        funder=os.environ["POLYMARKET_WALLET_ADDRESS"],
        builder_config=builder_config,
    )
    # V2 renamed create_or_derive_api_creds → create_or_derive_api_key.
    client.set_api_creds(client.create_or_derive_api_key())

    # Approve pUSD spending on the V2 Exchange contract. No-op if already
    # approved — for accounts that bought on the Polymarket UI before, this
    # is already maxed and the call is a free pass-through.
    #
    # The SDK's get_balance_allowance can mis-report 0 for proxy wallets even
    # when the on-chain allowance is set (see py-clob-client #287/#297/#319);
    # we don't gate trading on its response. Per-token CTF allowances are
    # likewise lazily refreshed by ensure_conditional_allowance() on first sell.
    try:
        client.update_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=sig_type,
            )
        )
        logger.info("COLLATERAL allowance refreshed")
    except Exception as exc:
        logger.warning("Allowance update failed (trading may still work): %s", exc)

    # Stash sig_type on the client so other helpers (ensure_conditional_allowance)
    # use the same value without re-reading the env var.
    client._poly_sig_type = sig_type  # type: ignore[attr-defined]

    return client


# ── Market metadata cache ─────────────────────────────────────────────────────
#
# tick_size / neg_risk / min_order_size are required to construct a valid V2
# order: tick_size for price rounding, neg_risk for the right exchange contract,
# min_order_size to avoid "Size lower than minimum" rejections.
#
# `get_clob_market_info(condition_id)` returns these in one call and the SDK
# caches tick_size/neg_risk internally; we layer a Lambda-lifetime cache on top
# so we don't re-fetch per order.
_MARKET_META_CACHE: dict[str, dict] = {}


def get_market_meta(market_id: str) -> dict:
    """
    Return {'tick_size': float, 'neg_risk': bool, 'min_order_size': float}
    for the given conditionId. Cached for the Lambda lifetime.
    """
    cached = _MARKET_META_CACHE.get(market_id)
    if cached is not None:
        return cached

    info = get_clob_client().get_clob_market_info(market_id)
    # V2 schema: mts=min tick size, mos=min order size, nr=neg risk, fd=fee details
    meta = {
        "tick_size":      float(info.get("mts", 0.01)),
        "neg_risk":       bool(info.get("nr", False)),
        "min_order_size": float(info.get("mos", 5.0)),
    }
    _MARKET_META_CACHE[market_id] = meta
    return meta


def ensure_conditional_allowance(token_id: str) -> None:
    """
    Set CTF (conditional token) allowance for selling a specific outcome token.
    Required before SELL — see py-clob-client issue #311. Idempotent.
    """
    client = get_clob_client()
    sig_type = getattr(client, "_poly_sig_type", int(SignatureTypeV2.POLY_1271))
    try:
        client.update_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=sig_type,
            )
        )
        logger.info("CONDITIONAL allowance refreshed for token %s", token_id[:12] + "…")
    except Exception as exc:
        logger.warning("CONDITIONAL allowance update failed for %s: %s", token_id[:12], exc)


def get_open_markets() -> list[dict]:
    """
    Return the top MARKET_LIMIT active Polymarket markets sorted by 24hr volume.

    Uses the Gamma REST API instead of CLOB pagination — the CLOB /markets endpoint
    returns markets in creation order with hundreds of thousands of historical entries
    before reaching active ones, causing Lambda timeouts. Gamma returns only active
    markets pre-sorted by volume and includes clobTokenIds needed for order placement.

    When GEO_MARKETS_ONLY=true (default), keeps only geopolitics / international-
    relations markets so A/B/C share the same thematic universe.
    """
    from ..signals import is_geopolitics_market

    geo_only = os.environ.get("GEO_MARKETS_ONLY", "true").lower() in ("1", "true", "yes")
    fetch_limit = MARKET_FETCH_LIMIT if geo_only else MARKET_LIMIT

    resp = _requests.get(
        GAMMA_API_URL,
        params={
            "active":    "true",
            "closed":    "false",
            "order":     "volume24hr",
            "ascending": "false",
            "limit":     fetch_limit,
        },
        timeout=30,
        headers={"User-Agent": "polyagent/1.0"},
    )
    resp.raise_for_status()
    data = resp.json()

    markets = []
    for m in data:
        if not m.get("active") or m.get("closed"):
            continue
        if not m.get("enableOrderBook") or not m.get("acceptingOrders"):
            continue

        try:
            outcomes  = json.loads(m["outcomes"])
            prices    = json.loads(m["outcomePrices"])
            token_ids = json.loads(m["clobTokenIds"])
        except (KeyError, ValueError):
            continue

        try:
            yes_idx = next(i for i, o in enumerate(outcomes) if o.lower() == "yes")
            no_idx  = next(i for i, o in enumerate(outcomes) if o.lower() == "no")
        except StopIteration:
            continue

        # Polymarket UI URLs use /event/<event_slug>/<market_slug> — the
        # `market.slug` from Gamma is *only* the market half (often with a
        # numeric ID suffix). The event slug lives on each entry of
        # `market.events[]`. Pick the first event (markets almost always
        # belong to exactly one parent event); fall back to empty string.
        events = m.get("events") or []
        event_slug = events[0].get("slug", "") if events else ""
        event_title = events[0].get("title", "") if events else ""

        if geo_only and not is_geopolitics_market(
            m.get("question", ""),
            event_title,
            m.get("description", ""),
        ):
            continue

        markets.append({
            "market_id":   m["conditionId"],
            "question":    m["question"],
            "market_slug": m.get("slug", ""),
            "event_slug":  event_slug,
            "yes_token":   token_ids[yes_idx],
            "no_token":    token_ids[no_idx],
            "yes_price":   float(prices[yes_idx]),
            "no_price":    float(prices[no_idx]),
            "volume":      float(m.get("volumeNum") or m.get("volume") or 0),
        })
        if len(markets) >= MARKET_LIMIT:
            break

    logger.info(
        "Fetched %d active markets from Gamma (geo_only=%s, scanned up to %d)",
        len(markets), geo_only, fetch_limit,
    )
    return markets
