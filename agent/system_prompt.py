import os

# ── System prompt ──────────────────────────────────────────────────────────────
#
# The system prompt is Claude's standing orders for the entire run. It is sent
# on every API call and never changes mid-loop. It has two sections:
#
#   Section 1 — crowd signal table (dynamic, prepended by build_system_prompt)
#     Built fresh each run from feedback.csv by preprocess_signals().
#     Claude reads this as plain text to know what the crowd believes.
#
#   Section 2 — trading instructions (below, static except for env var values)
#     Tells Claude the two-phase workflow, the entry conditions, and the risk
#     thresholds. The numbered steps are the reason Claude calls tools in the
#     right order — it follows them like a checklist.
#
# Why plain text instead of code?
#   Claude cannot run Python or inspect your tool implementations. Instructions
#   in the system prompt are the only way to give it behavioural rules that
#   persist across the whole loop (as opposed to tool descriptions, which are
#   scoped to individual tool decisions).

_INSTRUCTIONS = """\
EACH RUN: follow this sequence exactly.

PHASE 1 — MANAGE EXISTING RISK FIRST
1. Call get_positions()
2. For every position where hit_take_profit=true or hit_stop_loss=true:
   - Call close_position() immediately
   - stop-loss:   limit_price = current_price - 0.02  (accept worse fill to guarantee exit)
   - take-profit: limit_price = current_price          (no urgency, already winning)
3. State your reasoning for each close or hold decision.

Current risk thresholds:
  Take profit : +{take_profit_pct:.0f}% return on entry price  → hit_take_profit flag
  Stop loss   : -{stop_loss_pct:.0f}% loss on entry price      → hit_stop_loss flag

PHASE 2 — SEEK NEW ENTRY (only after Phase 1 is complete)
4. Call check_balance() — abort Phase 2 entirely if ok_to_trade=false
5. Call get_open_markets()
6. Match market questions to crowd signal topics using your own reasoning.
   Handle paraphrase, abbreviation, and semantic equivalence yourself.
7. Enter ONLY when ALL of the following conditions are met simultaneously:
   - |weighted_score| > 0.70
   - max_conviction > 0.30
   - interactions >= 3
   - crowd says YES and yes_price < 0.40, OR
     crowd says NO  and yes_price > 0.60
8. Place max 1 new order per run, max ${max_order_usd} size.
   Set limit_price within 5% of the current market price.
   Include source_headline and crowd_score in the place_order call.

If no edge is found: explain why and place no order.
Think step by step. Manage existing risk before seeking new reward.
"""


def build_system_prompt(signal_table: str) -> str:
    # Risk thresholds come from env vars so they can be tuned without code changes.
    # Claude sees the resolved values (e.g. "50%"), not the variable names.
    instructions = _INSTRUCTIONS.format(
        take_profit_pct=float(os.environ.get("TAKE_PROFIT_PCT", "0.50")) * 100,
        stop_loss_pct=float(os.environ.get("STOP_LOSS_PCT", "0.30")) * 100,
        max_order_usd=os.environ.get("MAX_ORDER_USD", "10.0"),
    )
    # Signal table first so the instructions can reference it as "the signals above".
    return signal_table + instructions
