"""
Agent B — no crowd data + Phase 2 rewritten for LLM-reasoned mispricing.

Variant B has the same harness shape as Variants A and C. The two
differences from A live in this file:

    1. _load_signal_block() returns an explicit empty-signal notice
       (no CSV is read).
    2. AGENT_VARIANT is "B".

The agent loop, tool list, risk rails, dashboard snapshot, and CDK
infrastructure are otherwise identical across all three variants — they
live duplicated inside each agent directory so the three deployments
diverge cleanly when needed.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import boto3
from anthropic import Anthropic

from .agent_loop import run_tool_loop
from .tools import TOOLS, execute_tool_call
from .tools import ddb

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

AGENT_VARIANT = "B"
MODEL = "claude-sonnet-4-6"

_BUNDLE_ROOT = Path(os.environ.get("LAMBDA_TASK_ROOT") or Path(__file__).resolve().parent.parent)


# ── Data adapter ──────────────────────────────────────────────────────────────

def _load_signal_block() -> str:
    """
    Return the explicit empty-signal notice. Variant B has no curated crowd
    table; this notice tells the model unambiguously the table is absent so
    it does not hallucinate a missing input or fall back to crowd-vocabulary.

    The wording MUST stay stable across runs of Variant B — changing it
    confounds the comparison to Variant A.
    """
    return (
        "═══════════════════════════════════════════════════════════════\n"
        "CROWD SIGNAL TABLE\n"
        "═══════════════════════════════════════════════════════════════\n\n"
        "NO CROWD SIGNAL AVAILABLE FOR THIS DEPLOYMENT.\n\n"
        "Phase 2 entries must be reasoned from public market information\n"
        "(get_open_markets snapshot) plus your own world knowledge. Do not\n"
        "refer to weighted_score, max_conviction, interactions, or theme_key\n"
        "from signals — none of those exist in this run.\n\n"
        "═══════════════════════════════════════════════════════════════\n\n"
    )


# ── Strategy adapter ──────────────────────────────────────────────────────────

def _render_strategy_knobs(text: str) -> str:
    """Inject live Lambda env values into `${KNOB}` placeholders in strategy.md."""
    import re
    knobs = (
        "MIN_DISAGREEMENT", "MIN_ENTRY_PRICE", "MAX_ORDER_USD", "MAX_PER_THEME",
        "MAX_NEW_ORDERS_PER_RUN",
        "TAKE_PROFIT_PCT", "STOP_LOSS_PCT", "MAX_ABS_LOSS_USD",
    )

    def _sub(match):
        name = match.group(1)
        return os.environ.get(name, match.group(0))

    pattern = re.compile(r"\$\{(" + "|".join(knobs) + r")\}")
    return pattern.sub(_sub, text)


def _load_strategy_text() -> str:
    candidates = [
        _BUNDLE_ROOT / "strategy.md",
        Path(__file__).resolve().parent.parent.parent / "strategy.md",
    ]
    for path in candidates:
        if path.is_file():
            return _render_strategy_knobs(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"strategy.md not found in any of: {candidates!r}")


# ── Dashboard snapshot ────────────────────────────────────────────────────────

_NUMERIC_KEYS = {
    "crowd_score", "entry_price", "close_price", "size_shares",
    "intended_shares", "tick_size", "pnl_usd", "pnl_usd_mid",
    "current_price", "mid_price", "peak_pnl_pct",
}


def _coerce_numeric(item: dict) -> dict:
    out = {}
    for k, v in item.items():
        if k in _NUMERIC_KEYS:
            if v is None or v == "":
                out[k] = None
                continue
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = None
        else:
            out[k] = v
    return out


def _upload_dashboard_snapshot(s3_client, bucket: str) -> int:
    from .tools.positions import get_positions

    all_rows = ddb.scan_all_positions()
    try:
        live = {p["token_id"]: p for p in get_positions()}
    except Exception as exc:
        logger.warning("Live snapshot enrichment failed: %s", exc)
        live = {}

    snapshot = []
    for row in all_rows:
        if row.get("status") in ("open", "pending"):
            mark = live.get(row.get("token_id"), {})
            row = {
                **row,
                "current_price": mark.get("current_price"),
                "mid_price":     mark.get("mid_price"),
                "pnl_usd":       mark.get("pnl_usd", row.get("pnl_usd")),
                "pnl_usd_mid":   mark.get("pnl_usd_mid"),
                "peak_pnl_pct":  mark.get("peak_pnl_pct", row.get("peak_pnl_pct")),
                "is_stale":      mark.get("is_stale", True),
            }
        snapshot.append(_coerce_numeric(row))

    s3_client.put_object(
        Bucket=bucket,
        Key="dashboard/positions.json",
        Body=json.dumps(snapshot, default=str),
        ContentType="application/json",
        CacheControl="no-cache",
    )
    logger.info("Dashboard snapshot uploaded (%d positions)", len(snapshot))

    from .dashboard_history import append_performance_history

    try:
        append_performance_history(s3_client, bucket, snapshot)
    except Exception as exc:
        logger.warning("Performance history upload failed: %s", exc)

    from .dashboard_meta import upload_wallet_meta

    try:
        upload_wallet_meta(s3_client, bucket)
    except Exception as exc:
        logger.warning("Wallet meta upload failed: %s", exc)
    return len(snapshot)


# ── Lambda entry ──────────────────────────────────────────────────────────────

def lambda_handler(event, context):  # noqa: ARG001
    os.environ.setdefault("AGENT_VARIANT", AGENT_VARIANT)

    s3     = boto3.client("s3")
    bucket = os.environ["S3_BUCKET"]

    signal_block  = _load_signal_block()
    strategy_text = _load_strategy_text()
    system_prompt = signal_block + strategy_text

    from .tools.positions import reconcile_clob_orders, reconcile_resolved_positions

    clob_summary = reconcile_clob_orders()
    logger.info(
        "CLOB order reconciliation: closed=%d updated=%d kept=%d checked=%d",
        clob_summary["closed"],
        clob_summary["updated"],
        clob_summary["kept"],
        clob_summary["checked"],
    )

    res_summary = reconcile_resolved_positions()
    logger.info(
        "Resolution reconciliation: closed=%d checked=%d skipped=%d",
        res_summary["closed"],
        res_summary["checked"],
        res_summary["skipped"],
    )

    logger.info(
        "Agent %s | DRY_RUN=%s | signal_len=%d (empty notice) | strategy_len=%d",
        AGENT_VARIANT, os.environ.get("DRY_RUN", "false"),
        len(signal_block), len(strategy_text),
    )

    from .tools.positions import get_positions

    try:
        get_positions()
    except Exception as exc:
        logger.warning("Pre-run pending reconcile failed: %s", exc)

    client   = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [{"role": "user", "content": "Run trading analysis."}]
    iterations   = 0
    summary_text = ""

    try:
        iterations, summary_text = run_tool_loop(
            client,
            model=MODEL,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
            execute_tool_call=execute_tool_call,
        )
    finally:
        try:
            _upload_dashboard_snapshot(s3, bucket)
        except Exception as exc:
            logger.error("Dashboard snapshot upload failed: %s", exc)

    return {
        "statusCode":    200,
        "agent_variant": AGENT_VARIANT,
        "iterations":    iterations,
        "summary":       summary_text,
        "completed_at":  datetime.utcnow().isoformat() + "Z",
    }


if __name__ == "__main__":
    print(json.dumps(lambda_handler({}, None), indent=2, default=str))
