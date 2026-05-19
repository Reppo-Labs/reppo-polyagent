import os
import decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

_table = None


def _get_table():
    global _table
    if _table is None:
        # DDB_REGION allows the Lambda (eu-west-1) to reach the table in us-west-2
        region = os.environ.get("DDB_REGION", "us-west-2")
        dynamodb = boto3.resource("dynamodb", region_name=region)
        _table = dynamodb.Table(os.environ.get("DDB_TABLE", "geo-trading-positions"))
    return _table


def _to_float(item: dict) -> dict:
    """Convert DynamoDB Decimal values to float so items are JSON-serialisable."""
    out = {}
    for k, v in item.items():
        if isinstance(v, decimal.Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _scan_all_pages(**scan_kwargs) -> list[dict]:
    """DynamoDB Scan with pagination (single scan() can truncate at 1 MB)."""
    items: list[dict] = []
    while True:
        resp = _get_table().scan(**scan_kwargs)
        items.extend(_to_float(i) for i in resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        scan_kwargs = {**scan_kwargs, "ExclusiveStartKey": lek}
    return items


def get_open_positions() -> list[dict]:
    return _scan_all_pages(FilterExpression=Attr("status").eq("open"))


def get_open_or_pending_positions() -> list[dict]:
    """
    Return both fully-open and resting-but-unfilled positions so Phase 1 can
    reconcile late fills. A 'pending' row was written after a successful CLOB
    post that had not yet matched at the time we polled get_order.
    """
    return _scan_all_pages(
        FilterExpression=Attr("status").eq("open") | Attr("status").eq("pending")
    )


def count_open_in_theme(theme_key: str) -> int:
    """
    Count open/pending positions in the same macro theme bucket.

    `theme_key` is the bucket from signals.classify_theme(source_headline).
    Rows that pre-date this feature won't have a stored theme_key — we derive
    one on the fly from source_headline so legacy positions still count toward
    the cap. No DDB migration needed.
    """
    from ..signals import classify_theme  # late import to avoid agent.signals at module import time

    open_positions = get_open_or_pending_positions()
    n = 0
    for pos in open_positions:
        stored = pos.get("theme_key")
        derived = stored if stored else classify_theme(pos.get("source_headline") or "")
        if derived == theme_key:
            n += 1
    return n


def update_peak_pnl(token_id: str, peak_pnl_pct: float) -> None:
    """
    Persist a position's running peak unrealised return so trailing-TP state
    survives across Lambda runs. Stored as a string for numeric consistency
    with other PnL fields in the table.
    """
    _get_table().update_item(
        Key={"token_id": token_id},
        UpdateExpression="SET peak_pnl_pct = :p",
        ExpressionAttributeValues={":p": str(peak_pnl_pct)},
    )


def update_position_fill(
    token_id: str,
    size_shares: float,
    status: str,
    order_status: str,
) -> None:
    """Update a row's filled size and lifecycle status after CLOB reconciliation."""
    _get_table().update_item(
        Key={"token_id": token_id},
        UpdateExpression=(
            "SET size_shares = :ss, #s = :st, order_status = :os"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":ss": str(size_shares),
            ":st": status,
            ":os": order_status,
        },
    )


def scan_all_positions() -> list[dict]:
    table = _get_table()
    items = []
    resp = table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return [_to_float(i) for i in items]


def write_position(item: dict) -> None:
    _get_table().put_item(Item=item)


def update_position_closed(
    token_id: str,
    close_price: float,
    close_reason: str,
    close_time: str,
    pnl_usd: float,
    close_order_id: str | None = None,
) -> None:
    """
    Mark a row closed with the realised exit price and P&L.

    `close_order_id` is the SELL order's ID (different from `clob_order_id`,
    which is the ENTRY order). Storing it lets `scripts/reconcile_closes.py`
    audit the actual fill VWAP against the CLOB trade history without having
    to guess by time-matching trades.
    """
    expr = (
        "SET #s = :s, close_price = :cp, close_reason = :cr, "
        "close_time = :ct, pnl_usd = :pnl"
    )
    values = {
        ":s": "closed",
        ":cp": str(close_price),
        ":cr": close_reason,
        ":ct": close_time,
        ":pnl": str(pnl_usd),
    }
    if close_order_id:
        expr += ", close_order_id = :coid"
        values[":coid"] = close_order_id

    _get_table().update_item(
        Key={"token_id": token_id},
        UpdateExpression=expr,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=values,
    )


def get_open_positions_for_market(market_id: str) -> list[dict]:
    result = _get_table().query(
        IndexName="market_id-index",
        KeyConditionExpression=Key("market_id").eq(market_id),
        FilterExpression=Attr("status").eq("open") | Attr("status").eq("pending"),
    )
    return [_to_float(i) for i in result.get("Items", [])]


def get_position_by_token(token_id: str) -> dict | None:
    result = _get_table().get_item(Key={"token_id": token_id})
    item = result.get("Item")
    return _to_float(item) if item else None
