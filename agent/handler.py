import json
import logging
import os

import boto3
from anthropic import Anthropic

from .agent_loop import run_tool_loop
from .signals import preprocess_signals
from .system_prompt import build_system_prompt
from .tools import TOOLS, execute_tool_call
from .tools import ddb

logging.getLogger().setLevel(logging.INFO)  # Lambda pre-configures root logger; setLevel overrides it
logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

# Sensitive env vars (ANTHROPIC_API_KEY, POLYGON_PRIVATE_KEY, PRIVY_API_KEY,
# PRIVY_WALLET_ID, POLYMARKET_WALLET_ADDRESS) are set directly on the Lambda
# function after deploy. Lambda encrypts env vars at rest via KMS at no extra
# cost. Move to Secrets Manager before deploying significant capital.


# Numeric DDB columns stored as strings (because DDB rejects float NaN/inf and
# the writers cast everything via str(...)). The dashboard JS calls .toFixed()
# on these, so we MUST serialise them as JSON numbers — not quoted strings.
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


def _upload_dashboard_snapshot(s3_client, bucket: str) -> None:
    """Write all positions to S3 so the dashboard can fetch them.

    Open positions are enriched with a fresh best-bid (current_price) and
    unrealised pnl_usd via positions.get_positions(), so the table has live
    values without the dashboard needing CLOB access of its own.
    """
    from .tools.positions import get_positions

    all_rows = ddb.scan_all_positions()

    # Live mark for open/pending positions, keyed by token_id
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
                "current_price": mark.get("current_price"),     # best bid (SL/TP basis)
                "mid_price":     mark.get("mid_price"),         # mid (Polymarket-aligned)
                "pnl_usd":       mark.get("pnl_usd", row.get("pnl_usd")),
                "pnl_usd_mid":   mark.get("pnl_usd_mid"),       # display P&L
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

    from .dashboard_meta import upload_wallet_meta

    try:
        upload_wallet_meta(s3_client, bucket)
    except Exception as exc:
        logger.warning("Wallet meta upload failed: %s", exc)


def lambda_handler(event, context):  # noqa: ARG001
    s3     = boto3.client("s3")
    bucket = os.environ["S3_BUCKET"]
    feedback_key = os.environ.get("S3_FEEDBACK_KEY", "geo-signals/feedback.csv")

    # ── Pre-flight: build context the agent will reason over ──────────────────
    #
    # The signal table is injected into the system prompt once per run.
    # Claude reads it as plain text — it has no direct access to the CSV.
    # Preprocessing happens here in Python so the prompt stays concise and
    # only contains the aggregated signal, not raw vote rows.
    #
    # FEEDBACK_CSV_PATH (optional): absolute path to a local CSV on disk.
    # When set — e.g. local harness — skip S3 for the feedback file. Lambda
    # production leaves this unset and reads geo-signals/feedback.csv.
    local_csv = os.environ.get("FEEDBACK_CSV_PATH")
    if local_csv:
        with open(local_csv, encoding="utf-8") as fh:
            feedback_csv = fh.read()
        logger.info("Loaded feedback from FEEDBACK_CSV_PATH=%s", local_csv)
    else:
        feedback_csv = s3.get_object(Bucket=bucket, Key=feedback_key)["Body"].read().decode("utf-8")
    signal_table = preprocess_signals(feedback_csv)

    # Stamp resolved markets into DDB *before* Phase 1 so P&L / win rate match
    # Polymarket once the CLOB book is gone (404) and the model cannot sell.
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

    # The system prompt is Claude's standing orders for the entire run.
    # It never changes mid-loop. It contains: the crowd signal table (dynamic,
    # injected above) + the two-phase trading workflow (static instructions).
    system = build_system_prompt(signal_table)

    logger.info("Signal table built. Starting agent loop (DRY_RUN=%s).",
                os.environ.get("DRY_RUN", "false"))

    from .tools.positions import get_positions

    try:
        get_positions()
    except Exception as exc:
        logger.warning("Pre-run pending reconcile failed: %s", exc)

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [{"role": "user", "content": "Run trading analysis."}]

    iterations = 0
    try:
        iterations, _ = run_tool_loop(
            client,
            model=MODEL,
            system=system,
            tools=TOOLS,
            messages=messages,
            execute_tool_call=execute_tool_call,
        )
    finally:
        try:
            _upload_dashboard_snapshot(s3, bucket)
        except Exception as exc:
            logger.error("Dashboard snapshot upload failed: %s", exc)

    return {"statusCode": 200, "iterations": iterations}
