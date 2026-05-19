"""
Append time-series metrics to S3 for the ABC comparison dashboard.

Each agent run adds one point to dashboard/performance-history.json so the
compare page can chart portfolio / ROI / win rate over time (not just a snapshot).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

HISTORY_KEY = "dashboard/performance-history.json"
MAX_POINTS = 720  # ~7 days at 15-minute cadence


def _strategy_version() -> str:
    """
    Short SHA-256 of strategy.md so the dashboard can flag when a variant's
    policy changed mid-experiment. Empty string if the file can't be located.
    """
    bundle_root = Path(os.environ.get("LAMBDA_TASK_ROOT") or
                       Path(__file__).resolve().parent.parent)
    candidates = [
        bundle_root / "strategy.md",
        Path(__file__).resolve().parent.parent.parent / "strategy.md",
    ]
    for p in candidates:
        if p.is_file():
            return hashlib.sha256(p.read_bytes()).hexdigest()[:8]
    return ""


def _num(x) -> float | None:
    if x is None or x == "":
        return None
    try:
        v = float(x)
        return v if v == v else None  # NaN check
    except (TypeError, ValueError):
        return None


def _row_pnl(p: dict) -> float | None:
    if p.get("status") in ("open", "pending"):
        mid = _num(p.get("pnl_usd_mid"))
        if mid is not None:
            return mid
    return _num(p.get("pnl_usd"))


def compute_run_metrics(positions: list[dict]) -> dict:
    """Snapshot metrics for one Lambda run (mirrors compare-dashboard buildStats)."""
    all_rows = positions or []
    open_rows = [p for p in all_rows if p.get("status") in ("open", "pending")]
    closed = [p for p in all_rows if p.get("status") == "closed"]

    realised = sum(_num(p.get("pnl_usd")) or 0 for p in closed)
    unrealised = sum(_row_pnl(p) or 0 for p in open_rows)
    total_pnl = realised + unrealised

    def _stake_shares(p: dict) -> float:
        sz = _num(p.get("size_shares")) or 0
        if sz > 0:
            return sz
        return _num(p.get("intended_shares")) or 0

    deployed = sum(
        (_num(p.get("entry_price")) or 0) * _stake_shares(p)
        for p in all_rows
    )

    # ROI = cash-on-cash on the *fixed* starting bankroll. Using sum-of-entry-
    # notional as the denominator (the previous version) penalised
    # high-turnover variants — every closed-and-reopened position inflated the
    # denominator even though no new capital was committed. With a fixed
    # bankroll the metric answers the question viewers actually care about:
    # "how much has each $1 of capital grown / shrunk?"
    bankroll = float(os.environ.get("STARTING_BANKROLL", "80"))
    roi = (total_pnl / bankroll) if bankroll > 0 else None

    flat_closed = sum(1 for p in closed if (_num(p.get("pnl_usd")) or 0) == 0)
    decisive = [p for p in closed if (_num(p.get("pnl_usd")) or 0) != 0]
    wins = sum(1 for p in decisive if (_num(p.get("pnl_usd")) or 0) > 0)
    losses = sum(1 for p in decisive if (_num(p.get("pnl_usd")) or 0) < 0)
    # Win rate = wins ÷ (wins + losses); excludes flat $0 reconciliations.
    win_rate = (wins / len(decisive)) if decisive else 0.0

    win_pnls = [(_num(p.get("pnl_usd")) or 0) for p in decisive if (_num(p.get("pnl_usd")) or 0) > 0]
    loss_pnls = [(_num(p.get("pnl_usd")) or 0) for p in decisive if (_num(p.get("pnl_usd")) or 0) < 0]
    avg_win = (sum(win_pnls) / len(win_pnls)) if win_pnls else None
    avg_loss = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else None

    portfolio_usd = bankroll + total_pnl

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_pnl": round(total_pnl, 4),
        "realised_pnl": round(realised, 4),
        "unrealised_pnl": round(unrealised, 4),
        "portfolio_usd": round(portfolio_usd, 2),
        "starting_bankroll": bankroll,
        "roi": round(roi, 6) if roi is not None else None,
        "win_rate": round(win_rate, 4),
        "decisive_closed": len(decisive),
        "flat_closed": flat_closed,
        "avg_win_usd": round(avg_win, 4) if avg_win is not None else None,
        "avg_loss_usd": round(avg_loss, 4) if avg_loss is not None else None,
        "closed": len(closed),
        "wins": wins,
        "losses": losses,
        "open": len(open_rows),
        "trades": len(all_rows),
        "deployed": round(deployed, 2),
        "strategy_version": _strategy_version(),
    }


def append_performance_history(s3_client, bucket: str, positions: list[dict]) -> None:
    """Merge this run's metrics into dashboard/performance-history.json on S3."""
    point = compute_run_metrics(positions)
    variant = os.environ.get("AGENT_VARIANT", "")

    points: list[dict] = []
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=HISTORY_KEY)
        raw = json.loads(resp["Body"].read())
        if isinstance(raw, dict) and isinstance(raw.get("points"), list):
            points = raw["points"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchKey":
            raise
    except Exception as exc:
        logger.warning("Could not read performance history: %s", exc)

    if points and points[-1].get("ts") == point["ts"]:
        points[-1] = point
    else:
        points.append(point)
    points = points[-MAX_POINTS:]

    body = json.dumps(
        {
            "agent_variant": variant,
            "updated_at": point["ts"],
            "starting_bankroll": point["starting_bankroll"],
            "strategy_version": point["strategy_version"],
            "points": points,
        },
        default=str,
    )
    s3_client.put_object(
        Bucket=bucket,
        Key=HISTORY_KEY,
        Body=body,
        ContentType="application/json",
        CacheControl="no-cache",
    )
    logger.info(
        "Performance history updated (%d points, portfolio=$%.2f pnl=%.2f)",
        len(points),
        point["portfolio_usd"],
        point["total_pnl"],
    )
