"""
Agent C — current crowd data + edge-sized Bayesian / quarter-Kelly strategy.

Composition is intentionally identical to Variant A:
    signal_block = preprocess_signals(<feedback.csv>)
Only the strategy.md sitting next to this handler differs. That is the
experimental contract — A vs C isolates the *strategy* axis with data
and harness held constant.

If you find yourself editing this handler to change the data side, fork
a new variant directory (e.g. agentD/) — do not introduce data-side
changes into Agent C, or the experiment will be confounded across two axes.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import boto3
from anthropic import Anthropic

from .agent_loop import run_tool_loop
from .signals import preprocess_signals
from .tools import TOOLS, execute_tool_call
from .tools import ddb

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

AGENT_VARIANT = "C"
MODEL = "claude-sonnet-4-6"

_BUNDLE_ROOT = Path(os.environ.get("LAMBDA_TASK_ROOT") or Path(__file__).resolve().parent.parent)


# ── Data adapter (identical to Agent A) ───────────────────────────────────────

def _load_signal_block(s3, bucket: str) -> str:
    csv_env = os.environ.get("FEEDBACK_CSV_PATH")
    if csv_env and Path(csv_env).is_file():
        raw = Path(csv_env).read_text(encoding="utf-8")
        logger.info("Loaded feedback from FEEDBACK_CSV_PATH=%s", csv_env)
    elif (_BUNDLE_ROOT / "feedback.csv").is_file():
        raw = (_BUNDLE_ROOT / "feedback.csv").read_text(encoding="utf-8")
        logger.info("Loaded feedback from bundle root")
    else:
        key = os.environ.get("S3_FEEDBACK_KEY", "geo-signals/feedback.csv")
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        logger.info("Loaded feedback from s3://%s/%s", bucket, key)

    return preprocess_signals(raw)


# ── Strategy adapter ──────────────────────────────────────────────────────────

def _render_strategy_knobs(text: str) -> str:
    """
    Substitute `${KNOB}` placeholders in strategy.md with live env values.

    Why this exists: strategy.md is the *prompt* the LLM reads. Knobs like
    MIN_EDGE / KELLY_FRACTION / MAX_NEW_ORDERS_PER_RUN are set as Lambda env
    vars on the stack — without this step the LLM would always see whatever
    number was hardcoded into the markdown file (decorative env vars). We use
    `${…}` (shell-style) instead of `{…}` so accidental curly braces inside
    markdown code blocks don't crash format-style substitution.
    """
    import re
    knobs = (
        "MIN_EDGE", "KELLY_FRACTION", "MAX_NEW_ORDERS_PER_RUN",
        "EVIDENCE_INTERACTION_CAP", "TAKE_PROFIT_PCT", "STOP_LOSS_PCT",
        "MIN_ENTRY_PRICE", "MAX_PER_THEME", "MAX_ORDER_USD",
        "MAX_ABS_LOSS_USD", "TAIL_PRICE_FLOOR", "TAIL_SCORE_THRESHOLD",
        "TAIL_INTERACTIONS_THRESHOLD", "MIN_DISAGREEMENT",
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

    signal_block  = _load_signal_block(s3, bucket)
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
        "Agent %s | DRY_RUN=%s | signal_len=%d | strategy_len=%d",
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
