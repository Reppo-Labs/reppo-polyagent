import os
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Set required env vars before importing tool modules
os.environ.setdefault("DDB_TABLE",              "geo-trading-positions")
os.environ.setdefault("MAX_ORDER_USD",          "10.0")
os.environ.setdefault("TAKE_PROFIT_PCT",        "0.50")
os.environ.setdefault("STOP_LOSS_PCT",          "0.30")
os.environ.setdefault("MIN_BALANCE_RESERVE",    "15.0")
os.environ.setdefault("POLYGON_PRIVATE_KEY",    "0xdeadbeef")
os.environ.setdefault("POLYMARKET_WALLET_ADDRESS", "0xTestWallet")


# ── DDB helpers ───────────────────────────────────────────────────────────────

class TestDdbHelpers(unittest.TestCase):

    def _make_table_mock(self):
        return MagicMock()

    @patch("agent.tools.ddb._get_table")
    def test_get_open_positions_filters_status(self, mock_get_table):
        table = MagicMock()
        table.scan.return_value = {"Items": [
            {"token_id": "t1", "status": "open", "entry_price": Decimal("0.35"), "size_shares": Decimal("10")},
        ]}
        mock_get_table.return_value = table

        from agent.tools.ddb import get_open_positions
        result = get_open_positions()
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0]["entry_price"], float)

    @patch("agent.tools.ddb._get_table")
    def test_decimal_converted_to_float(self, mock_get_table):
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {"token_id": "t1", "entry_price": Decimal("0.38"), "size_shares": Decimal("26.32")}
        }
        mock_get_table.return_value = table

        from agent.tools.ddb import get_position_by_token
        item = get_position_by_token("t1")
        self.assertIsInstance(item["entry_price"], float)
        self.assertAlmostEqual(item["entry_price"], 0.38)


# ── get_positions ─────────────────────────────────────────────────────────────

class TestGetPositions(unittest.TestCase):

    @patch("agent.tools.positions._clob")
    @patch("agent.tools.ddb.get_open_positions")
    def test_computes_pnl_and_flags(self, mock_ddb, mock_clob):
        mock_ddb.return_value = [{
            "token_id": "tok1", "question": "Q?", "outcome": "YES",
            "entry_price": "0.30", "size_shares": "33.33",
            "source_headline": "Signal A", "crowd_score": "0.90",
        }]
        # current price is 0.45 → pnl_pct = (0.45-0.30)/0.30 = 0.50 → hit_take_profit
        book = MagicMock()
        book.bids = [MagicMock(price="0.45")]
        mock_clob.return_value.get_order_book.return_value = book

        from agent.tools.positions import get_positions
        result = get_positions()
        self.assertEqual(len(result), 1)
        pos = result[0]
        self.assertAlmostEqual(pos["pnl_pct"], 0.50, places=2)
        self.assertTrue(pos["hit_take_profit"])
        self.assertFalse(pos["hit_stop_loss"])

    @patch("agent.tools.positions._clob")
    @patch("agent.tools.ddb.get_open_positions")
    def test_hit_stop_loss(self, mock_ddb, mock_clob):
        mock_ddb.return_value = [{
            "token_id": "tok2", "question": "Q?", "outcome": "YES",
            "entry_price": "0.50", "size_shares": "20",
            "source_headline": "", "crowd_score": "0.80",
        }]
        # current 0.34 → pnl_pct = (0.34-0.50)/0.50 = -0.32 → hit_stop_loss
        book = MagicMock()
        book.bids = [MagicMock(price="0.34")]
        mock_clob.return_value.get_order_book.return_value = book

        from agent.tools.positions import get_positions
        result = get_positions()
        self.assertTrue(result[0]["hit_stop_loss"])
        self.assertFalse(result[0]["hit_take_profit"])

    @patch("agent.tools.positions._clob")
    @patch("agent.tools.ddb.get_open_positions")
    def test_clob_error_falls_back_to_entry_price(self, mock_ddb, mock_clob):
        mock_ddb.return_value = [{
            "token_id": "tok3", "question": "Q?", "outcome": "NO",
            "entry_price": "0.40", "size_shares": "25",
            "source_headline": "", "crowd_score": "0.75",
        }]
        mock_clob.return_value.get_order_book.side_effect = Exception("timeout")

        from agent.tools.positions import get_positions
        result = get_positions()
        self.assertAlmostEqual(result[0]["current_price"], 0.40)
        self.assertAlmostEqual(result[0]["pnl_pct"], 0.0)


# ── close_position ────────────────────────────────────────────────────────────

class TestClosePosition(unittest.TestCase):

    def setUp(self):
        os.environ["DRY_RUN"] = "false"

    def tearDown(self):
        os.environ["DRY_RUN"] = "true"

    @patch("agent.tools.positions.ddb.update_position_closed")
    @patch("agent.tools.positions.ddb.get_position_by_token")
    @patch("agent.tools.positions._clob")
    def test_posts_sell_order_and_updates_ddb(self, mock_clob, mock_get, mock_update):
        os.environ["DRY_RUN"] = "false"
        mock_get.return_value = {"entry_price": "0.35", "size_shares": "28.57"}
        mock_clob.return_value.create_and_post_order.return_value = {"orderId": "abc"}

        # Patch the OrderArgs import path used inside close_position
        with patch.dict("sys.modules", {
            "py_clob_client.clob_types": MagicMock(OrderArgs=MagicMock()),
            "py_clob_client.order_builder.constants": MagicMock(SELL=1),
        }):
            from agent.tools.positions import close_position
            result = close_position("tok1", 28.57, 0.58, "take_profit")

        self.assertEqual(result["status"], "closed")
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args.kwargs
        self.assertEqual(call_kwargs["close_reason"], "take_profit")

    def test_dry_run_skips_clob_and_ddb(self):
        os.environ["DRY_RUN"] = "true"
        from agent.tools.positions import close_position
        result = close_position("tok_x", 10.0, 0.55, "stop_loss")
        self.assertEqual(result["status"], "dry_run")


# ── place_order ───────────────────────────────────────────────────────────────

class TestPlaceOrder(unittest.TestCase):

    def setUp(self):
        os.environ["DRY_RUN"] = "true"

    def _market_list(self):
        return [{
            "market_id": "mkt1",
            "question":  "Will X happen?",
            "yes_token": "yes_tok",
            "no_token":  "no_tok",
            "yes_price": 0.35,
            "no_price":  0.65,
            "volume":    100000,
        }]

    @patch("agent.tools.wallet.get_open_markets")
    @patch("agent.tools.wallet.ddb.get_open_positions_for_market", return_value=[])
    @patch("agent.tools.wallet.ddb.get_position_by_token", return_value=None)
    def test_max_order_usd_hard_cap(self, *_mocks):
        markets_mock = _mocks[-1]
        markets_mock.return_value = None  # positional: last patch = get_position_by_token

        with patch("agent.tools.wallet.get_open_markets", return_value=self._market_list()), \
             patch("agent.tools.wallet.ddb.get_position_by_token", return_value=None), \
             patch("agent.tools.wallet.ddb.get_open_positions_for_market", return_value=[]):
            from agent.tools.wallet import place_order
            result = place_order("mkt1", "YES", size_usdc=999.0, limit_price=0.35)

        # DRY_RUN so no real order; but we can verify the cap log or just no exception
        self.assertEqual(result["status"], "dry_run")
        # size_usdc in result should be capped at MAX_ORDER_USD (10.0)
        self.assertLessEqual(result["size_usdc"], 10.0)

    @patch("agent.tools.wallet.get_open_markets")
    @patch("agent.tools.wallet.ddb.get_position_by_token")
    @patch("agent.tools.wallet.ddb.get_open_positions_for_market")
    def test_blocked_on_duplicate_token(self, mock_market_pos, mock_token_pos, mock_markets):
        mock_markets.return_value = self._market_list()
        mock_token_pos.return_value = {"token_id": "yes_tok", "status": "open"}  # existing!
        mock_market_pos.return_value = []

        from agent.tools.wallet import place_order
        with self.assertRaises(ValueError, msg="should block duplicate token entry"):
            place_order("mkt1", "YES", size_usdc=10.0, limit_price=0.35)

    @patch("agent.tools.wallet.get_open_markets")
    @patch("agent.tools.wallet.ddb.get_position_by_token")
    @patch("agent.tools.wallet.ddb.get_open_positions_for_market")
    def test_blocked_on_opposing_market_position(self, mock_market_pos, mock_token_pos, mock_markets):
        mock_markets.return_value = self._market_list()
        mock_token_pos.return_value = None
        mock_market_pos.return_value = [{"token_id": "no_tok", "status": "open"}]  # opposite side!

        from agent.tools.wallet import place_order
        with self.assertRaises(ValueError, msg="should block opposing position"):
            place_order("mkt1", "YES", size_usdc=10.0, limit_price=0.35)

    @patch("agent.tools.wallet.get_open_markets")
    @patch("agent.tools.wallet.ddb.get_position_by_token", return_value=None)
    @patch("agent.tools.wallet.ddb.get_open_positions_for_market", return_value=[])
    def test_limit_price_sanity_check(self, _a, _b, mock_markets):
        mock_markets.return_value = self._market_list()  # yes_price = 0.35

        from agent.tools.wallet import place_order
        with self.assertRaises(ValueError, msg="limit_price >5% from current should fail"):
            place_order("mkt1", "YES", size_usdc=10.0, limit_price=0.50)


# ── check_balance ─────────────────────────────────────────────────────────────

class TestCheckBalance(unittest.TestCase):

    @patch("agent.tools.wallet.requests.post")
    def test_ok_to_trade_true_when_balance_above_reserve(self, mock_post):
        # 20 USDC = 20_000_000 raw (6 decimals)
        mock_post.return_value.json.return_value = {"result": hex(20_000_000)}
        mock_post.return_value.raise_for_status = MagicMock()

        from agent.tools.wallet import check_balance
        result = check_balance()
        self.assertAlmostEqual(result["usdc"], 20.0)
        self.assertTrue(result["ok_to_trade"])

    @patch("agent.tools.wallet.requests.post")
    def test_ok_to_trade_false_when_balance_below_reserve(self, mock_post):
        # 5 USDC — below MIN_BALANCE_RESERVE (15.0)
        mock_post.return_value.json.return_value = {"result": hex(5_000_000)}
        mock_post.return_value.raise_for_status = MagicMock()

        from agent.tools.wallet import check_balance
        result = check_balance()
        self.assertFalse(result["ok_to_trade"])


if __name__ == "__main__":
    unittest.main()
