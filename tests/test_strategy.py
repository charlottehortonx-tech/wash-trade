"""Unit tests for Strategy."""

import time
import unittest
from unittest.mock import MagicMock, patch

from exchange_client import Balance, OrderBook
from execution import Position
from strategy import BuySignal, Strategy, get_buy_signal


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cfg():
    return {
        "mode": "paper",
        "exchange": {"symbol": "BTC/USDT"},
        "strategy": {
            "sell_delay_seconds": 0,
            "allow_overlapping_positions": False,
            "signal_poll_interval_seconds": 1,
        },
        "execution": {
            "order_type": "market",
            "fill_timeout_seconds": 5,
            "sell_retry_attempts": 3,
            "sell_retry_delay_seconds": 0,
            "stale_order_timeout_seconds": 2,
            "limit_order_slippage_pct": 0.05,
        },
        "paper": {"initial_balance_usd": 10_000, "initial_balance_btc": 0,
                  "simulated_taker_fee_pct": 0.10},
        "fees": {"taker_pct": 0.10, "maker_pct": 0.08},
        "risk": {
            "max_position_size_usd": 500,
            "max_daily_loss_usd": 200,
            "max_trades_per_hour": 10,
            "consecutive_loss_cooldown_seconds": 0,
            "consecutive_losses_threshold": 3,
            "min_balance_usd": 50,
            "min_order_book_depth_usd": 100,
            "max_slippage_pct": 5.0,
            "max_fee_pct": 5.0,
        },
        "logging": {"level": "WARNING", "log_file": "/tmp/test_bot.log"},
    }


def _make_book(mid: float = 30_000.0) -> OrderBook:
    bids = [(mid * (1 - 0.001 * i), 5.0) for i in range(10)]
    asks = [(mid * (1 + 0.001 * i), 5.0) for i in range(10)]
    return OrderBook(symbol="BTC/USDT", bids=bids, asks=asks)


class TestGetBuySignal(unittest.TestCase):

    def test_returns_none_or_signal(self):
        book = _make_book()
        results = [get_buy_signal("BTC/USDT", book) for _ in range(100)]
        signals = [r for r in results if r is not None]
        nones = [r for r in results if r is None]
        # Should sometimes fire, sometimes not
        self.assertTrue(len(signals) > 0)
        self.assertTrue(len(nones) > 0)

    def test_signal_qty_is_positive(self):
        book = _make_book(30_000.0)
        for _ in range(200):
            sig = get_buy_signal("BTC/USDT", book)
            if sig is not None:
                self.assertGreater(sig.suggested_quantity_base, 0)


class TestStrategyRunTradeCycle(unittest.TestCase):

    def _build_strategy(self, cfg):
        import logger
        logger.setup(**{"level": "WARNING", "log_file": "/tmp/test_bot.log"})

        client = MagicMock()
        client.get_balance.return_value = Balance(base=0.0, quote=5_000.0)
        client.get_order_book.return_value = _make_book()

        from risk_manager import RiskManager
        from execution import ExecutionEngine
        risk = RiskManager(cfg, client)
        engine = ExecutionEngine(cfg, client)
        strat = Strategy(cfg, client, risk, engine)
        return strat, client, risk, engine

    def test_successful_cycle_returns_true(self):
        cfg = _make_cfg()
        strat, client, risk, engine = self._build_strategy(cfg)
        signal = BuySignal(symbol="BTC/USDT", suggested_quantity_base=0.001)
        book = _make_book(30_000.0)

        pos = Position(
            symbol="BTC/USDT", filled_qty=0.001, avg_buy_price=30_000.0,
            buy_fee=0.03, buy_order_id="b1", fill_timestamp=time.time(),
        )
        engine.place_buy = MagicMock(return_value=MagicMock(order_id="b1"))
        engine.wait_for_buy_fill = MagicMock(return_value=pos)
        engine.wait_sell_delay = MagicMock()
        engine.place_sell = MagicMock(return_value=0.03)

        result = strat.run_trade_cycle(signal, book)
        self.assertTrue(result)
        engine.place_buy.assert_called_once()
        engine.wait_sell_delay.assert_called_once()
        engine.place_sell.assert_called_once()

    def test_cycle_aborts_if_buy_fails(self):
        cfg = _make_cfg()
        strat, client, risk, engine = self._build_strategy(cfg)
        signal = BuySignal(symbol="BTC/USDT", suggested_quantity_base=0.001)
        book = _make_book(30_000.0)

        engine.place_buy = MagicMock(return_value=None)
        engine.wait_for_buy_fill = MagicMock()
        result = strat.run_trade_cycle(signal, book)
        self.assertFalse(result)
        engine.wait_for_buy_fill.assert_not_called()

    def test_cycle_aborts_if_fill_fails(self):
        cfg = _make_cfg()
        strat, client, risk, engine = self._build_strategy(cfg)
        signal = BuySignal(symbol="BTC/USDT", suggested_quantity_base=0.001)
        book = _make_book(30_000.0)

        engine.place_buy = MagicMock(return_value=MagicMock(order_id="b1"))
        engine.wait_for_buy_fill = MagicMock(return_value=None)
        engine.wait_sell_delay = MagicMock()
        result = strat.run_trade_cycle(signal, book)
        self.assertFalse(result)
        engine.wait_sell_delay.assert_not_called()

    def test_pnl_recorded_correctly(self):
        cfg = _make_cfg()
        strat, client, risk, engine = self._build_strategy(cfg)
        signal = BuySignal(symbol="BTC/USDT", suggested_quantity_base=0.001)
        book = _make_book(30_000.0)

        pos = Position(
            symbol="BTC/USDT", filled_qty=0.001, avg_buy_price=30_000.0,
            buy_fee=0.03, buy_order_id="b1", fill_timestamp=time.time(),
        )
        engine.place_buy = MagicMock(return_value=MagicMock(order_id="b1"))
        engine.wait_for_buy_fill = MagicMock(return_value=pos)
        engine.wait_sell_delay = MagicMock()
        engine.place_sell = MagicMock(return_value=0.03)

        strat.run_trade_cycle(signal, book)
        # Daily PnL should be recorded (positive or negative)
        self.assertIsNotNone(risk.daily_pnl)

    def test_risk_blocks_trade_no_balance(self):
        cfg = _make_cfg()
        strat, client, risk, engine = self._build_strategy(cfg)
        client.get_balance.return_value = Balance(base=0.0, quote=10.0)  # below min

        signal = BuySignal(symbol="BTC/USDT", suggested_quantity_base=0.001)
        book = _make_book(30_000.0)

        engine.place_buy = MagicMock()
        result = strat.run_trade_cycle(signal, book)
        self.assertFalse(result)
        engine.place_buy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
