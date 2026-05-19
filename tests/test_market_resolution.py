import os
import unittest

os.environ.setdefault("DDB_TABLE", "geo-trading-positions")
os.environ.setdefault("POLYGON_PRIVATE_KEY", "0xdeadbeef")
os.environ.setdefault("POLYMARKET_WALLET_ADDRESS", "0xTestWallet")


class TestRedemptionPricePerShare(unittest.TestCase):
    def test_open_market_returns_none(self):
        from agent.settlement import redemption_price_per_share

        payload = {"closed": False, "tokens": [{"token_id": "abc", "winner": True}]}
        self.assertIsNone(redemption_price_per_share(payload, "abc"))

    def test_winner_true(self):
        from agent.settlement import redemption_price_per_share

        payload = {
            "closed": True,
            "tokens": [
                {"token_id": "111", "outcome": "Yes", "winner": True, "price": 1},
                {"token_id": "222", "outcome": "No", "winner": False, "price": 0},
            ],
        }
        self.assertEqual(redemption_price_per_share(payload, "111"), 1.0)
        self.assertEqual(redemption_price_per_share(payload, "222"), 0.0)

    def test_fallback_price_without_winner_flag(self):
        from agent.settlement import redemption_price_per_share

        payload = {
            "closed": True,
            "tokens": [{"token_id": "99", "winner": None, "price": 1}],
        }
        self.assertEqual(redemption_price_per_share(payload, "99"), 1.0)

    def test_unknown_token(self):
        from agent.settlement import redemption_price_per_share

        payload = {"closed": True, "tokens": [{"token_id": "x", "winner": True}]}
        self.assertIsNone(redemption_price_per_share(payload, "missing"))
