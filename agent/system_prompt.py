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
2. SKIP any position where is_stale=true OR position_status="pending":
   - is_stale=true means we couldn't fetch a live order book and current_price
     is unknown. Do NOT close — TP/SL flags are null. The next run will retry.
   - position_status="pending" means the entry order has not yet filled. There
     is nothing to sell. Do NOT close.
3. For every NON-stale, NON-pending position where ANY of these flags is true:
   hit_take_profit, hit_trailing_tp, hit_stop_loss → call close_position() now.
   - reason: pass the matching close reason exactly:
       hit_take_profit=true  → reason="take_profit"
       hit_trailing_tp=true  → reason="trailing_take_profit"
       hit_stop_loss=true    → reason="stop_loss"
     If multiple are true, prefer take_profit > trailing_take_profit > stop_loss.
   - limit_price:
       stop_loss            : current_price - 0.02   (accept worse fill to guarantee exit)
       take_profit / trail  : current_price          (no urgency, already winning)
   - The tool tick-rounds and clamps the price to a valid range, so don't worry
     about tick alignment yourself.
4. State your reasoning for each close or hold decision.

Current risk thresholds:
  Take profit (fixed)  : +{take_profit_pct:.0f}% return on entry price       → hit_take_profit flag
  Take profit (trail)  : arm at +{trail_activate_pct:.0f}%, exit on {trail_giveback_pct:.0f}% giveback from peak
                                                                              → hit_trailing_tp flag
  Stop loss (percent)  : -{stop_loss_pct:.0f}% return on entry price          → hit_stop_loss flag
  Stop loss (ticks)    : {low_price_sl_ticks} ticks when entry_price < ${low_price_threshold:.2f}
                                                                              → hit_stop_loss flag
  Stop loss (absolute) : pnl_usd <= -${max_abs_loss_usd:.2f}                  → hit_stop_loss flag
                         (folded into hit_stop_loss; backstop against the
                          tick-SL letting a tail position bleed past this dollar cap)

PHASE 2 — SEEK NEW ENTRY (only after Phase 1 is complete)
5. Call check_balance() — abort Phase 2 entirely if ok_to_trade=false
6. Call get_open_markets()
7. Match market questions to crowd signal topics using your own reasoning.
   Handle paraphrase, abbreviation, and semantic equivalence yourself.
   IMPORTANT: a crowd signal is only a valid justification for ONE candidate market.
   If you reject or fail to enter market X attributed to source_headline=S, do NOT
   pivot to a different market Y also attributed to source_headline=S — that is
   the loose-semantic-match failure mode. Pick a DIFFERENT crowd row for the
   next candidate.
8. Enter ONLY when ALL of the following conditions are met simultaneously:
   - |weighted_score| > {entry_score_threshold:.2f}
   - max_conviction > 0.30
   - interactions >= 3
   - crowd says YES and yes_price < 0.50, OR
     crowd says NO  and yes_price > 0.50
   - the market's live price is >= ${min_entry_price} (place_order rejects
     anything cheaper — tail-priced markets are filtered out automatically)
   - your existing open positions for the candidate market's macro theme bucket
     (see theme_key in get_positions output) number < {max_per_theme} —
     place_order rejects entries that would exceed this cap.
   - if the chosen-side entry price is < ${tail_price_floor:.2f} (tail entry),
     the crowd signal must additionally clear |weighted_score| > {tail_score_threshold:.2f}
     AND interactions >= {tail_interactions_threshold}. Cheap tail markets need
     stronger evidence — they have hostile microstructure and a tick of noise
     can wipe the position.
   - if the crowd topic is a LONG-HORIZON narrative ("regime falls eventually",
     "ceasefire holds", "war escalates") but the market's resolution date is
     within ~30 days, the price disagreement must be |p_blend - market_price|
     >= 0.20 (rough estimate: |weighted_score|*0.5 vs market). The short
     deadline crushes most long-horizon crowd bets — require a bigger gap.
9. Place max 1 new order per run, max ${max_order_usd} size.
   Set limit_price within 5% of the current market price.
   Include source_headline and crowd_score in the place_order call.
10. RANK candidates by |weighted_score| × max_conviction and PREFER candidates
    whose theme_key is under-represented in your current portfolio. Do not load
    multiple correlated positions on the same macro theme in a single run.
11. If place_order returns "theme cap reached" → END PHASE 2 immediately. Do NOT
    try a different theme in the same run. The next scheduled run will pick up
    again with fresh portfolio state. This prevents thrashing the API on a
    portfolio that is already at concentration limits.
12. For other place_order errors ("MIN_ENTRY_PRICE", "below this market's
    minimum", price-range, "Open position already exists") — that market is
    not tradeable. Try the next-best match attributed to a DIFFERENT
    source_headline (see step 7). If no other candidate qualifies, end the run
    with no trade.

If no edge is found: explain why and place no order.
Think step by step. Manage existing risk before seeking new reward.
"""


def build_system_prompt(signal_table: str) -> str:
    # Risk thresholds come from env vars so they can be tuned without code changes.
    # Claude sees the resolved values (e.g. "50%"), not the variable names.
    instructions = _INSTRUCTIONS.format(
        take_profit_pct=float(os.environ.get("TAKE_PROFIT_PCT", "0.50")) * 100,
        stop_loss_pct=float(os.environ.get("STOP_LOSS_PCT", "0.30")) * 100,
        trail_activate_pct=float(os.environ.get("TRAIL_ACTIVATE_PCT", "0.30")) * 100,
        trail_giveback_pct=float(os.environ.get("TRAIL_GIVEBACK_PCT", "0.50")) * 100,
        low_price_threshold=float(os.environ.get("LOW_PRICE_THRESHOLD", "0.10")),
        low_price_sl_ticks=int(os.environ.get("LOW_PRICE_SL_TICKS", "15")),
        max_order_usd=os.environ.get("MAX_ORDER_USD", "10.0"),
        min_entry_price=os.environ.get("MIN_ENTRY_PRICE", "0.05"),
        max_per_theme=os.environ.get("MAX_PER_THEME", "2"),
        entry_score_threshold=float(os.environ.get("ENTRY_SCORE_THRESHOLD", "0.70")),
        # Tier-3 dollar-loss backstop (see agent/tools/positions.py)
        max_abs_loss_usd=float(os.environ.get("MAX_ABS_LOSS_USD", "2.50")),
        # Tail-price gating: cheap entries require stronger crowd evidence
        tail_price_floor=float(os.environ.get("TAIL_PRICE_FLOOR", "0.15")),
        tail_score_threshold=float(os.environ.get("TAIL_SCORE_THRESHOLD", "0.90")),
        tail_interactions_threshold=int(os.environ.get("TAIL_INTERACTIONS_THRESHOLD", "10")),
    )
    # Signal table first so the instructions can reference it as "the signals above".
    return signal_table + instructions
