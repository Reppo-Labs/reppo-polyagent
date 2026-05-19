"""
Agent A — current crowd data + current strategy. Lambda entry point.

This file is the entire harness for Variant A. Read top-to-bottom:

    1. Load the crowd CSV from a bundled file (Lambda) or S3 (fallback).
    2. Run preprocess_signals to build the topic-level signal table.
    3. Load strategy.md from the bundle.
    4. Compose system_prompt = signal_block + strategy_text.
    5. Run the Anthropic tool-use loop until end_turn.
    6. Write a dashboard snapshot to S3 for the monitoring page.

Variants B and C are sibling directories with the same shape. The only
differences are inside (a) this handler's `_load_signal_block` and (b) the
strategy.md sitting next to the bundle root. Execution + tools + risk
rails are intentionally duplicated into each variant so the three agents
can diverge independently without cross-contamination.
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

logging.getLogger().setLevel(logging.INFO)  # Lambda pre-sets root level; override.
logger = logging.getLogger(__name__)

AGENT_VARIANT = "A"
MODEL = "claude-sonnet-4-6"

# In Lambda, the bundle root is /var/task and contains: agent/, strategy.md,
# feedback.csv, plus pip-installed deps. Locally, $LAMBDA_TASK_ROOT is unset
# and we resolve relative to this file's package parent.
_BUNDLE_ROOT = Path(os.environ.get("LAMBDA_TASK_ROOT") or Path(__file__).resolve().parent.parent)


# ── Data adapter ──────────────────────────────────────────────────────────────

def _load_signal_block(s3, bucket: str) -> str:
    """
    Build Variant A's data block: read the Reppo feedback CSV and run
    preprocess_signals to get the topic-level signal table string.

    Precedence:
      1. FEEDBACK_CSV_PATH env (used in Lambda — bundled at /var/task/feedback.csv).
      2. Bundled file at <bundle_root>/feedback.csv.
      3. S3 fallback at s3://$S3_BUCKET/$S3_FEEDBACK_KEY.
    """
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

def _load_strategy_text() -> str:
    """Read strategy.md from the bundle root (Lambda) or the variant dir (local)."""
    candidates = [
        _BUNDLE_ROOT / "strategy.md",
        Path(__file__).resolve().parent.parent.parent / "strategy.md",
    ]
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
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
    """Write positions for this variant's DDB table to dashboard/positions.json."""
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
    # Stamp AGENT_VARIANT so every new DDB position row (place_order writes
    # this) is attributable to Agent A on the shared monitoring dashboard.
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
        "statusCode":      200,
        "agent_variant":   AGENT_VARIANT,
        "iterations":      iterations,
        "summary":         summary_text,
        "completed_at":    datetime.utcnow().isoformat() + "Z",
    }


if __name__ == "__main__":
    # Local invocation: DRY_RUN=true, S3_BUCKET set, plus either
    # FEEDBACK_CSV_PATH or a bundle root with feedback.csv next to strategy.md.
    print(json.dumps(lambda_handler({}, None), indent=2, default=str))
