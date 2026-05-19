import logging

from .markets import get_open_markets
from .positions import close_position, get_positions
from .wallet import check_balance, place_order

logger = logging.getLogger(__name__)

# ── Tool schemas ───────────────────────────────────────────────────────────────
#
# TOOLS is the "menu" sent to the API on every call. Claude reads these schemas
# to decide which tool to call and what arguments to pass.
#
# Each entry has three parts:
#   name         — the identifier Claude uses to request the tool
#   description  — the most important field: tells Claude WHEN to call it,
#                  HOW to use the result, and any behavioural rules.
#                  Claude cannot see your Python code — the description IS
#                  the tool's contract from its perspective.
#   input_schema — JSON Schema constraining what arguments Claude may pass.
#                  Enums, required fields, and types are enforced by the API
#                  before your Python code ever runs.

TOOLS = [
    {
        "name": "get_positions",
        # "Always call this first" is an instruction, not metadata.
        # Claude follows description text as instructions.
        "description": (
            "Get all open and pending positions from DynamoDB enriched with live CLOB best-bid prices. "
            "Returns P&L, three close-trigger flags (hit_take_profit, hit_trailing_tp, hit_stop_loss — "
            "treat ANY of them being true as a close signal), peak_pnl_pct (running high-water mark "
            "since entry), is_stale flag that is true when the live price is unavailable (all flags "
            "are null then — do NOT close stale positions), sl_mode ('percent', 'ticks', or "
            "'abs_dollar' — informational only; hit_stop_loss is already pre-computed across all "
            "three rules so you do not need to evaluate them separately), position_status ('open' "
            "for filled positions, 'pending' for resting GTCs not yet filled — skip pending in "
            "Phase 1), and theme_key (the macro cluster this position belongs to, e.g. 'iran', "
            "'oil', 'starmer'). "
            "Always call this first."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "close_position",
        # The limit_price guidance here is what causes Claude to set prices
        # correctly — it reads this description when constructing arguments.
        "description": (
            "Place a SELL limit order on Polymarket CLOB V2 with builder attribution and mark the "
            "DDB position closed. The tool tick-rounds limit_price and clamps it to the market's "
            "valid range, sets the conditional-token allowance for the first sell on this token, "
            "and verifies the position exists & is open before sending the order. "
            "Use limit_price = current_price - 0.02 for stop-loss, current_price for take-profit. "
            "Refuses to run on stale or pending positions. "
            "(Polymarket final settlements when the market resolves are applied automatically in "
            "the ledger before each run — not via this tool.)"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token_id":    {"type": "string",  "description": "From get_positions()"},
                "size_shares": {"type": "number",  "description": "Position size to sell (will be clamped to held shares)"},
                "limit_price": {"type": "number",  "description": "Minimum acceptable fill price (will be tick-rounded)"},
                # enum enforces valid values at the API level — Claude cannot pass anything else
                "reason":      {"type": "string",  "enum": ["take_profit", "trailing_take_profit", "stop_loss", "manual"]},
            },
            "required": ["token_id", "size_shares", "limit_price", "reason"],
        },
    },
    {
        "name": "get_open_markets",
        "description": (
            "Return the top active Polymarket markets sorted by 24h volume. "
            "Each entry includes market_id (conditionId), question, yes_token, no_token, "
            "yes_price, and no_price."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_balance",
        # "Abort Phase 2 if ok_to_trade is false" tells Claude what to DO
        # with the result — without this, it might call get_open_markets anyway.
        "description": (
            "Check the pUSD (Polymarket V2 collateral) balance of the trading wallet on Polygon. "
            "Returns usdc (float) and ok_to_trade (bool). "
            "Abort Phase 2 if ok_to_trade is false."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "place_order",
        # Claude constructs all arguments itself from context it has already
        # seen: market_id from get_open_markets, source_headline from the
        # signal table in the system prompt, crowd_score similarly.
        # The hard-cap note is a reminder that Python enforces the limit
        # regardless of what Claude passes — Claude isn't the last line of defence.
        "description": (
            "Place a BUY limit order on Polymarket CLOB V2 with builder attribution and record "
            "the position in DynamoDB. The tool looks up the market's tick size, neg-risk flag, "
            "and minimum order size, tick-rounds limit_price, and post-reconciles fills. "
            "Order size is hard-capped at MAX_ORDER_USD regardless of size_usdc input. "
            "REJECTS the entry — and you must move to the next-best market — when any of these "
            "happen: (a) current_price is below MIN_ENTRY_PRICE (~$0.05) — tail-priced markets "
            "have hostile microstructure for our SL; (b) we already hold MAX_PER_THEME positions "
            "in the same macro theme bucket (Iran, oil, starmer, etc.); (c) the resulting share "
            "count is below the market's minimum; (d) the limit_price is more than 5% from the "
            "live market price."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market_id":        {"type": "string", "description": "conditionId from get_open_markets()"},
                "outcome":          {"type": "string", "enum": ["YES", "NO"]},
                "size_usdc":        {"type": "number", "description": "Dollar amount to spend"},
                "limit_price":      {"type": "number", "description": "Price per share, 0.01–0.99 (will be tick-rounded)"},
                "source_headline":  {"type": "string", "description": "The crowd signal pod_name that triggered this trade"},
                "crowd_score":      {"type": "number", "description": "weighted_score at time of entry (audit trail)"},
            },
            "required": ["market_id", "outcome", "size_usdc", "limit_price"],
        },
    },
]

# ── Dispatch map ───────────────────────────────────────────────────────────────
#
# When Claude requests a tool by name (block.name), execute_tool_call looks it
# up here and calls the corresponding Python function with Claude's arguments.
# Claude never calls Python directly — it only names the tool and passes JSON.
TOOL_MAP = {
    "get_positions":    get_positions,
    "close_position":   close_position,
    "get_open_markets": get_open_markets,
    "check_balance":    check_balance,
    "place_order":      place_order,
}


def execute_tool_call(name: str, args: dict):
    fn = TOOL_MAP.get(name)
    if not fn:
        raise ValueError(f"Unknown tool: {name!r}")
    try:
        return fn(**args)
    except Exception as exc:
        # Return the error as a structured response so Claude can recover
        # (e.g. try a different market if limit_price check fails).
        # Only hard-crash on unexpected non-ValueError exceptions.
        logger.error("Tool %r failed | args=%s\n%s", name, args, exc)
        return {"error": type(exc).__name__, "message": str(exc)}
