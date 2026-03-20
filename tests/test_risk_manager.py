"""Unit tests for RiskManager."""

import time
import unittest
from unittest.mock import MagicMock, patch

from exchange_client import Balance, Order, OrderBook
from risk_manager import RiskManager


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cfg(**risk_overrides):
    cfg = {
        "mode": "paper",
        "exchange": {"symbol": "BTC/USDT"},
        "paper": {"initial_balance_usd": 10_000, "initial_balance_btc": 0},
        "fees": {"taker_pct": 0.10, "maker_pct": 0.08},
        "risk": {
            "max_position_size_usd": 500,
            "max_daily_loss_usd": 200,
            "max_trades_per_hour": 10,
            "consecutive_loss_cooldown_seconds": 300,
            "consecutive_losses_threshold": 3,
            "min_balance_usd": 50,
            "min_order_book_depth_usd": 1_000,
            "max_slippage_pct": 0.30,
            "max_fee_pct": 0.50,
        },
    }
    cfg["risk"].update(risk_overrides)
    return cfg


def _make_book(mid: float = 30_000.0, depth_qty: float = 5.0) -> OrderBook:
    """Create a book with enough depth to pass liquidity checks."""
    bids = [(mid * (1 - 0.001 * i), depth_qty) for i in range(10)]
    asks = [(mid * (1 + 0.001 * i), depth_qty) for i in range(10)]
    return OrderBook(symbol="BTC/USDT", bids=bids, asks=asks)


def _make_client(balance_usd: float = 5_000.0) -> MagicMock:
    client = MagicMock()
    client.get_balance.return_value = Balance(base=0.0, quote=balance_usd)
    client.get_fee_rate.return_value = 0.001
    return client


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRiskManagerBasic(unittest.TestCase):

    def setUp(self):
        self.cfg = _make_cfg()
        self.client = _make_client()
        self.rm = RiskManager(self.cfg, self.client)

    def test_happy_path_approves(self):
        book = _make_book(30_000.0, depth_qty=10.0)
        ok, reason = self.rm.check_all("BTC/USDT", book, qty_base(100, 30_000), 30_000.0)
        self.assertTrue(ok, reason)

    def test_rejects_open_position(self):
        self.rm.set_position_open()
        book = _make_book()
        ok, reason = self.rm.check_all("BTC/USDT", book, qty_base(100, 30_000), 30_000.0)
        self.assertFalse(ok)
        self.assertIn("open_position", reason)

    def test_allows_overlapping_when_configured(self):
        self.rm.set_position_open()
        book = _make_book(30_000.0, depth_qty=10.0)
        ok, reason = self.rm.check_all(
            "BTC/USDT", book, qty_base(100, 30_000), 30_000.0, allow_overlapping=True
        )
        self.assertTrue(ok, reason)

    def test_rejects_balance_too_low(self):
        self.client.get_balance.return_value = Balance(base=0.0, quote=10.0)
        book = _make_book()
        ok, reason = self.rm.check_all("BTC/USDT", book, qty_base(100, 30_000), 30_000.0)
        self.assertFalse(ok)
        self.assertIn("balance_too_low", reason)

    def test_rejects_position_too_large(self):
        cfg = _make_cfg(max_position_size_usd=50)
        rm = RiskManager(cfg, self.client)
        book = _make_book(30_000.0, depth_qty=10.0)
        # $100 trade > $50 limit
        ok, reason = rm.check_all("BTC/USDT", book, qty_base(100, 30_000), 30_000.0)
        self.assertFalse(ok)
        self.assertIn("position_too_large", reason)


class TestDailyLoss(unittest.TestCase):

    def setUp(self):
        self.cfg = _make_cfg(max_daily_loss_usd=100)
        self.client = _make_client()
        self.rm = RiskManager(self.cfg, self.client)

    def test_blocks_after_daily_loss_exceeded(self):
        self.rm.set_position_open()
        self.rm.set_position_closed(realized_pnl=-150.0)  # exceeds $100 limit
        book = _make_book()
        ok, reason = self.rm.check_all("BTC/USDT", book, qty_base(100, 30_000), 30_000.0)
        self.assertFalse(ok)
        self.assertIn("daily_loss", reason)

    def test_allows_when_loss_within_limit(self):
        self.rm.set_position_open()
        self.rm.set_position_closed(realized_pnl=-50.0)
        book = _make_book(30_000.0, depth_qty=10.0)
        ok, reason = self.rm.check_all("BTC/USDT", book, qty_base(100, 30_000), 30_000.0)
        self.assertTrue(ok, reason)


class TestConsecutiveLossCooldown(unittest.TestCase):

    def setUp(self):
        self.cfg = _make_cfg(
            consecutive_losses_threshold=3,
            consecutive_loss_cooldown_seconds=600,
        )
        self.client = _make_client()
        self.rm = RiskManager(self.cfg, self.client)

    def test_cooldown_triggered_after_threshold(self):
        for _ in range(3):
            self.rm.set_position_open()
            self.rm.set_position_closed(realized_pnl=-1.0)

        self.assertEqual(self.rm.consecutive_losses, 3)
        book = _make_book()
        ok, reason = self.rm.check_all("BTC/USDT", book, qty_base(100, 30_000), 30_000.0)
        self.assertFalse(ok)
        self.assertIn("cooldown", reason)

    def test_no_cooldown_after_win_resets_streak(self):
        for _ in range(2):
            self.rm.set_position_open()
            self.rm.set_position_closed(realized_pnl=-1.0)
        self.rm.set_position_open()
        self.rm.set_position_closed(realized_pnl=+10.0)   # win resets streak

        self.assertEqual(self.rm.consecutive_losses, 0)


class TestMaxTradesPerHour(unittest.TestCase):

    def test_blocks_when_limit_hit(self):
        cfg = _make_cfg(max_trades_per_hour=3)
        client = _make_client()
        rm = RiskManager(cfg, client)
        for _ in range(3):
            rm.set_position_open()
            rm.set_position_closed(realized_pnl=1.0)

        book = _make_book(30_000.0, depth_qty=10.0)
        ok, reason = rm.check_all("BTC/USDT", book, qty_base(100, 30_000), 30_000.0)
        self.assertFalse(ok)
        self.assertIn("max_trades_per_hour", reason)


class TestLiquidityCheck(unittest.TestCase):

    def test_rejects_thin_book(self):
        cfg = _make_cfg(min_order_book_depth_usd=1_000_000)
        client = _make_client()
        rm = RiskManager(cfg, client)
        # Thin book: only $1 depth
        book = _make_book(30_000.0, depth_qty=0.00001)
        ok, reason = rm.check_all("BTC/USDT", book, qty_base(100, 30_000), 30_000.0)
        self.assertFalse(ok)
        self.assertIn("insufficient_liquidity", reason)


class TestSlippageCheck(unittest.TestCase):

    def test_rejects_high_slippage(self):
        cfg = _make_cfg(max_slippage_pct=0.01)  # tiny threshold: 0.01%
        client = _make_client()
        rm = RiskManager(cfg, client)
        # Build a book where the spread causes >0.01% slippage on a large order
        mid = 30_000.0
        # Very large qty vs small book depth
        book = _make_book(mid, depth_qty=0.001)
        ok, reason = rm.check_all("BTC/USDT", book, 0.1, mid)
        # Either slippage or liquidity check fires
        self.assertFalse(ok)


# ── Helper ────────────────────────────────────────────────────────────────────

def qty_base(usd: float, price: float) -> float:
    """Convert a USD trade size to base quantity."""
    return round(usd / price, 8)


if __name__ == "__main__":
    unittest.main()
