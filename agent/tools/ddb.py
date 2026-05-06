import os
import decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

_table = None


def _get_table():
    global _table
    if _table is None:
        dynamodb = boto3.resource("dynamodb")
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


def get_open_positions() -> list[dict]:
    result = _get_table().scan(FilterExpression=Attr("status").eq("open"))
    return [_to_float(i) for i in result.get("Items", [])]


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
) -> None:
    _get_table().update_item(
        Key={"token_id": token_id},
        UpdateExpression=(
            "SET #s = :s, close_price = :cp, close_reason = :cr, "
            "close_time = :ct, pnl_usd = :pnl"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "closed",
            ":cp": str(close_price),
            ":cr": close_reason,
            ":ct": close_time,
            ":pnl": str(pnl_usd),
        },
    )


def get_open_positions_for_market(market_id: str) -> list[dict]:
    result = _get_table().query(
        IndexName="market_id-index",
        KeyConditionExpression=Key("market_id").eq(market_id),
        FilterExpression=Attr("status").eq("open"),
    )
    return [_to_float(i) for i in result.get("Items", [])]


def get_position_by_token(token_id: str) -> dict | None:
    result = _get_table().get_item(Key={"token_id": token_id})
    item = result.get("Item")
    return _to_float(item) if item else None
