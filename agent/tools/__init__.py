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
            "Get all open positions from DynamoDB enriched with live CLOB best-bid prices. "
            "Returns P&L and pre-computed hit_take_profit / hit_stop_loss flags. "
            "Always call this first."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "close_position",
        # The limit_price guidance here is what causes Claude to set prices
        # correctly — it reads this description when constructing arguments.
        "description": (
            "Place a SELL limit order on Polymarket CLOB and mark the DDB position closed. "
            "Use limit_price = current_price - 0.02 for stop-loss, "
            "current_price for take-profit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token_id":    {"type": "string",  "description": "From get_positions()"},
                "size_shares": {"type": "number",  "description": "Full position size to sell"},
                "limit_price": {"type": "number",  "description": "Minimum acceptable fill price"},
                # enum enforces valid values at the API level — Claude cannot pass anything else
                "reason":      {"type": "string",  "enum": ["take_profit", "stop_loss", "manual"]},
            },
            "required": ["token_id", "size_shares", "limit_price", "reason"],
        },
    },
    {
        "name": "get_open_markets",
        "description": (
            "Return the top 100 active Polymarket markets by volume. "
            "Each entry includes market_id, question, yes_token, no_token, yes_price, no_price."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_balance",
        # "Abort Phase 2 if ok_to_trade is false" tells Claude what to DO
        # with the result — without this, it might call get_open_markets anyway.
        "description": (
            "Check the USDC balance of the trading wallet on Polygon. "
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
            "Place a BUY limit order on Polymarket and record the position in DynamoDB. "
            "Order size is hard-capped at MAX_ORDER_USD in code regardless of size_usdc input."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market_id":        {"type": "string", "description": "conditionId from get_open_markets()"},
                "outcome":          {"type": "string", "enum": ["YES", "NO"]},
                "size_usdc":        {"type": "number", "description": "Dollar amount to spend"},
                "limit_price":      {"type": "number", "description": "Price per share, 0.01–0.99"},
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
        # args is the dict Claude constructed — e.g. {"token_id": "abc", "reason": "stop_loss"}
        # Python unpacks it as keyword arguments into the actual function.
        return fn(**args)
    except Exception:
        # Log full traceback and re-raise so Lambda exits non-zero.
        # A silent failure here would let the loop continue with bad state.
        logger.exception("Tool %r failed | args=%s", name, args)
        raise
