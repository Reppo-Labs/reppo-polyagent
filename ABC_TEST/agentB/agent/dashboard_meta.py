"""Upload portfolio identity alongside dashboard JSON."""
import json
import os
from datetime import datetime, timezone


def upload_wallet_meta(s3_client, bucket: str) -> None:
    """
    Write dashboard/meta.json next to positions.json.

    Uses POLYMARKET_WALLET_ADDRESS as the canonical portfolio address for
    this agent (signing + CLOB + Polymarket UI).
    """
    portfolio = os.environ.get("POLYMARKET_WALLET_ADDRESS", "")
    meta = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent_variant": os.environ.get("AGENT_VARIANT", ""),
        "portfolio_address": portfolio,
        "ui_hint": (
            "Dashboard rows match this portfolio on Polymarket. "
            "Pending = resting limits (Open Orders tab). "
            "Trade History = fills only."
        ),
    }
    s3_client.put_object(
        Bucket=bucket,
        Key="dashboard/meta.json",
        Body=json.dumps(meta, indent=2),
        ContentType="application/json",
        CacheControl="no-cache",
    )
