"""Unit tests for ExecutionEngine."""

import time
import unittest
from unittest.mock import MagicMock, call, patch

from exchange_client import Order
from execution import ExecutionEngine, Position


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cfg(**overrides):
    cfg = {
        "mode": "paper",
        "exchange": {"symbol": "BTC/USDT"},
        "strategy": {"sell_delay_seconds": 0},   # no sleep in tests
        "execution": {
            "order_type": "market",
            "limit_order_slippage_pct": 0.05,
            "fill_timeout_seconds": 5,
            "sell_retry_attempts": 3,
            "sell_retry_delay_seconds": 0,
            "stale_order_timeout_seconds": 2,
        },
    }
    cfg["execution"].update(overrides)
    return cfg


def _filled_order(order_id="o1", side="buy", qty=0.001, price=30_000.0, fee=0.03):
    return Order(
        order_id=order_id,
        symbol="BTC/USDT",
        side=side,
        order_type="market",
        quantity=qty,
        price=price,
        status="filled",
        filled_qty=qty,
        avg_fill_price=price,
        fee=fee,
    )


def _open_order(order_id="o1", side="buy", qty=0.001, price=30_000.0):
    return Order(
        order_id=order_id,
        symbol="BTC/USDT",
        side=side,
        order_type="market",
        quantity=qty,
        price=price,
        status="open",
        filled_qty=0.0,
        avg_fill_price=0.0,
        fee=0.0,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPlaceBuy(unittest.TestCase):

    def test_returns_order_on_success(self):
        client = MagicMock()
        order = _filled_order()
        client.place_order.return_value = order
        engine = ExecutionEngine(_make_cfg(), client)
        result = engine.place_buy(0.001, 30_000.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.order_id, "o1")

    def test_returns_none_on_exception(self):
        client = MagicMock()
        client.place_order.side_effect = Exception("network error")
        engine = ExecutionEngine(_make_cfg(), client)
        result = engine.place_buy(0.001, 30_000.0)
        self.assertIsNone(result)


class TestWaitForBuyFill(unittest.TestCase):

    def test_returns_position_when_immediately_filled(self):
        client = MagicMock()
        order = _filled_order()
        client.get_order.return_value = order
        engine = ExecutionEngine(_make_cfg(), client)
        pos = engine.wait_for_buy_fill(order)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.filled_qty, 0.001)
        self.assertAlmostEqual(pos.avg_buy_price, 30_000.0)

    def test_cancels_and_returns_none_on_timeout(self):
        client = MagicMock()
        # Always return "open" so it times out
        open_order = _open_order()
        cancelled = Order(
            **{**open_order.__dict__, "status": "cancelled"}
        )
        client.get_order.side_effect = [open_order, open_order, cancelled]
        client.cancel_order.return_value = True
        engine = ExecutionEngine(_make_cfg(fill_timeout_seconds=0), client)
        pos = engine.wait_for_buy_fill(open_order)
        self.assertIsNone(pos)
        client.cancel_order.assert_called_once()

    def test_accepts_partial_fill_as_position(self):
        """If order times out with partial fill, treat that qty as the position."""
        client = MagicMock()
        partial = Order(
            order_id="o1", symbol="BTC/USDT", side="buy",
            order_type="market", quantity=0.01, price=30_000,
            status="partially_filled", filled_qty=0.005,
            avg_fill_price=30_000, fee=0.015,
        )
        client.get_order.return_value = partial
        engine = ExecutionEngine(_make_cfg(fill_timeout_seconds=0), client)
        pos = engine.wait_for_buy_fill(partial)
        # Partial fills should still produce a position
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.filled_qty, 0.005)


class TestPlaceSell(unittest.TestCase):

    def _make_position(self, qty=0.001, buy_price=30_000.0):
        return Position(
            symbol="BTC/USDT",
            filled_qty=qty,
            avg_buy_price=buy_price,
            buy_fee=0.03,
            buy_order_id="buy_1",
            fill_timestamp=time.time(),
        )

    def test_successful_sell_returns_fee(self):
        client = MagicMock()
        sell_order = _filled_order(order_id="s1", side="sell", qty=0.001, fee=0.03)
        client.place_order.return_value = sell_order
        client.get_order.return_value = sell_order
        engine = ExecutionEngine(_make_cfg(), client)
        fee = engine.place_sell(self._make_position(), mid_price=30_100.0)
        self.assertIsNotNone(fee)
        self.assertAlmostEqual(fee, 0.03)

    def test_retries_on_placement_failure(self):
        client = MagicMock()
        sell_order = _filled_order(order_id="s1", side="sell", qty=0.001, fee=0.03)
        # Fail first two placement attempts, succeed on third
        client.place_order.side_effect = [
            Exception("timeout"), Exception("timeout"), sell_order
        ]
        client.get_order.return_value = sell_order
        engine = ExecutionEngine(_make_cfg(sell_retry_attempts=3), client)
        fee = engine.place_sell(self._make_position(), mid_price=30_100.0)
        self.assertIsNotNone(fee)
        self.assertEqual(client.place_order.call_count, 3)

    def test_returns_none_after_all_retries_fail(self):
        client = MagicMock()
        client.place_order.side_effect = Exception("always fails")
        engine = ExecutionEngine(_make_cfg(sell_retry_attempts=3), client)
        fee = engine.place_sell(self._make_position(), mid_price=30_100.0)
        self.assertIsNone(fee)

    def test_handles_partial_sell_and_re_sells_remainder(self):
        client = MagicMock()
        # First sell: partial (only 0.0005 of 0.001 filled)
        partial_sell = Order(
            order_id="s1", symbol="BTC/USDT", side="sell",
            order_type="market", quantity=0.001, price=30_100,
            status="partially_filled", filled_qty=0.0005,
            avg_fill_price=30_100, fee=0.015,
        )
        # Second sell: fills the remainder
        final_sell = _filled_order(order_id="s2", side="sell", qty=0.0005, fee=0.015)
        client.place_order.side_effect = [partial_sell, final_sell]
        client.get_order.side_effect = [partial_sell, final_sell]
        engine = ExecutionEngine(_make_cfg(sell_retry_attempts=3), client)
        fee = engine.place_sell(self._make_position(qty=0.001), mid_price=30_100.0)
        self.assertIsNotNone(fee)
        self.assertAlmostEqual(fee, 0.030, places=5)
        self.assertEqual(client.place_order.call_count, 2)


class TestSellDelay(unittest.TestCase):

    def test_does_not_sleep_when_delay_elapsed(self):
        client = MagicMock()
        engine = ExecutionEngine(_make_cfg(sell_delay_seconds=0), client)
        # fill_timestamp in the past
        pos = Position(
            symbol="BTC/USDT", filled_qty=0.001, avg_buy_price=30_000,
            buy_fee=0.0, buy_order_id="x", fill_timestamp=time.time() - 60,
        )
        start = time.time()
        engine.wait_sell_delay(pos)
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
