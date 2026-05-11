import json
import logging
import os

import boto3
from anthropic import Anthropic

from .signals import preprocess_signals
from .system_prompt import build_system_prompt
from .tools import TOOLS, execute_tool_call
from .tools import ddb

logging.getLogger().setLevel(logging.INFO)  # Lambda pre-configures root logger; setLevel overrides it
logger = logging.getLogger(__name__)

MODEL          = "claude-sonnet-4-6"
MAX_ITERATIONS = 10

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

    # The system prompt is Claude's standing orders for the entire run.
    # It never changes mid-loop. It contains: the crowd signal table (dynamic,
    # injected above) + the two-phase trading workflow (static instructions).
    system = build_system_prompt(signal_table)

    logger.info("Signal table built. Starting agent loop (DRY_RUN=%s).",
                os.environ.get("DRY_RUN", "false"))

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ── Agent harness: the while loop IS the agent ────────────────────────────
    #
    # There is no framework here. An "agent" is just this loop:
    #   1. Call the API with the current conversation.
    #   2. If Claude wants to call a tool, execute it and append the result.
    #   3. Repeat until Claude signals it's done (stop_reason == "end_turn").
    #
    # The `messages` list is the agent's working memory. Every iteration adds
    # Claude's last response and the tool results, so each API call gets the
    # full history of what happened in prior turns.
    messages = [{"role": "user", "content": "Run trading analysis."}]

    iterations = 0
    while True:
        # Safety guard: a well-prompted agent completes in 4–8 tool calls.
        # If it exceeds this something is wrong — bail rather than spin.
        if iterations >= MAX_ITERATIONS:
            raise RuntimeError("agent loop safety limit exceeded (max %d iterations)" % MAX_ITERATIONS)

        # Every call sends: the full message history, the system prompt, and
        # the tool schemas. Claude uses all three to decide what to do next.
        response = client.messages.create(
            model=MODEL,
            system=system,
            tools=TOOLS,
            messages=messages,
            max_tokens=4096,
        )
        iterations += 1
        logger.info("Agent iteration %d | stop_reason=%s", iterations, response.stop_reason)

        # ── stop_reason == "end_turn": Claude is done ─────────────────────────
        #
        # Claude returns "end_turn" when it has nothing left to do — either it
        # placed an order, found no edge and explained why, or ok_to_trade was
        # false. This is the only clean exit from the loop.
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    logger.info("Agent summary:\n%s", block.text)
            break

        # ── stop_reason == "tool_use": Claude wants to call tools ─────────────
        #
        # Claude's response is a list of content blocks. Each block is either
        # a text thought or a tool_use request. We execute every tool_use block
        # and collect the results. Multiple tools can be requested in one turn.
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # block.name  → which tool Claude chose (e.g. "close_position")
            # block.input → the arguments Claude constructed from context
            #               (e.g. token_id it read from a prior get_positions result)
            # block.id    → used to match this result back to the request
            result = execute_tool_call(block.name, block.input)
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,   # Claude needs this to correlate result → request
                "content":     json.dumps(result, default=str),
            })

        # Append this turn to the conversation before looping.
        # Claude will see both its own tool requests and what they returned.
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user",      "content": tool_results})

    _upload_dashboard_snapshot(s3, bucket)
    return {"statusCode": 200, "iterations": iterations}
