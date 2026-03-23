"""
strategy.py — Signal generation and trade orchestration.

`get_buy_signal()` is the single entry point for external signals.
Replace or extend it with your own data source (websocket, REST poll,
ML model output, etc.).

The `Strategy` class ties together signal → risk check → buy → wait → sell.
"""

import time
from dataclasses import dataclass
from typing import Optional

import logger
from exchange_client import ExchangeClient, OrderBook
from execution import ExecutionEngine, Position
from risk_manager import RiskManager


# ── Signal ────────────────────────────────────────────────────────────────────

@dataclass
class BuySignal:
    symbol: str
    suggested_quantity_base: float   # base asset quantity (e.g. BTC)
    source: str = "external"         # label for logging


def get_buy_signal(symbol: str, order_book: OrderBook) -> Optional[BuySignal]:
    """
    External signal hook.  Replace this implementation with your own logic.

    Receives the current order book so the signal can be price-aware.
    Returns a BuySignal when conditions are met, None otherwise.

    Current placeholder: fires a signal roughly once every 10 calls
    (for demo / paper-trading purposes).
    """
    import random
    if random.random() < 0.10:          # 10% chance per poll — replace with real logic
        mid = order_book.mid_price
        # Size: $100 worth at current price (capped to sensible default)
        qty = round(100.0 / mid, 8) if mid > 0 else 0.001
        return BuySignal(symbol=symbol, suggested_quantity_base=qty)
    return None


# ── Strategy orchestrator ─────────────────────────────────────────────────────

class Strategy:

    def __init__(
        self,
        cfg: dict,
        client: ExchangeClient,
        risk: RiskManager,
        engine: ExecutionEngine,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._risk = risk
        self._engine = engine
        self._symbol: str = cfg.get("exchange", {}).get("symbol", "BTC/USDT")
        self._allow_overlap: bool = bool(
            cfg.get("strategy", {}).get("allow_overlapping_positions", False)
        )
        self._poll_interval: float = float(
            cfg.get("strategy", {}).get("signal_poll_interval_seconds", 5)
        )
        ui_cfg = cfg.get("_ui", {})
        self._amount_type: str = ui_cfg.get("amount_type", "fixed")
        self._amount_value: float = float(ui_cfg.get("amount_value", 100.0))
        self._amount_pct: float = float(ui_cfg.get("percent", 0.01))

    def update_settings(
        self,
        delay: float,
        profit_target: float,
        limit_sell_offset: float,
        amount_type: str,
        amount_value: float,
    ) -> None:
        self._amount_type = amount_type
        self._amount_value = amount_value
        self._amount_pct = amount_value / 100.0
        self._engine.update_settings(delay, profit_target, limit_sell_offset)
        logger.info(
            f"[Strategy] Settings updated  amount_type={amount_type}  "
            f"amount_value={amount_value}"
        )

    # ── Quantity calculation ──────────────────────────────────────────────────

    def _calc_qty(self, mid_price: float) -> float:
        """Compute order quantity from configured amount type and value."""
        import math
        if mid_price <= 0:
            return 0.0
        if self._amount_type == "percent":
            try:
                balance = self._client.get_balance()
                raw = balance.quote * self._amount_pct / mid_price
            except Exception:
                return 0.0
        else:
            raw = self._amount_value / mid_price
        # Floor to 8 decimal places so qty * price never exceeds the budget
        return math.floor(raw * 1e8) / 1e8

    # ── Single trade cycle ────────────────────────────────────────────────────

    def run_trade_cycle(self, signal: BuySignal, book: OrderBook) -> bool:
        """
        Execute one complete buy → wait → sell cycle.
        Returns True if the trade completed successfully.
        """
        mid = book.mid_price
        qty = signal.suggested_quantity_base

        # ── 1. Risk check ─────────────────────────────────────────────────────
        ok, reason = self._risk.check_all(
            symbol=self._symbol,
            order_book=book,
            quantity_base=qty,
            mid_price=mid,
            allow_overlapping=self._allow_overlap,
        )
        if not ok:
            logger.info(f"[Strategy] Trade skipped  reason={reason}")
            return False

        self._risk.set_position_open()

        # ── 2. Place BUY ──────────────────────────────────────────────────────
        signal_time = time.time()
        logger.signal_received(self._symbol, mid)

        buy_order = self._engine.place_buy(qty, mid)
        if buy_order is None:
            self._risk.set_position_closed(realized_pnl=0.0)
            return False

        # ── 3. Wait for fill ──────────────────────────────────────────────────
        position: Optional[Position] = self._engine.wait_for_buy_fill(buy_order)
        if position is None or position.filled_qty <= 0:
            logger.error("[Strategy] Buy not filled — aborting cycle.")
            self._risk.set_position_closed(realized_pnl=0.0)
            return False

        # ── 4. 30-second delay (starts at fill, not submission) ───────────────
        self._engine.wait_sell_delay(position)

        # ── 5. Refresh book for sell ──────────────────────────────────────────
        try:
            fresh_book = self._client.get_order_book(self._symbol)
            sell_mid = fresh_book.mid_price
        except Exception:
            sell_mid = mid  # fallback

        # ── 6. Place SELL ─────────────────────────────────────────────────────
        sell_fee = self._engine.place_sell(position, sell_mid)
        if sell_fee is None:
            logger.error(
                "[Strategy] Failed to close position — bot halted. "
                "Resolve the open position manually and restart."
            )
            # Do NOT clear the open-position flag. This keeps _has_open_position=True
            # so check_all() will reject every future signal until the bot is restarted.
            return False

        # ── 7. PnL accounting ─────────────────────────────────────────────────
        sell_price = sell_mid   # best approximation without order receipt
        realized_pnl = (
            (sell_price - position.avg_buy_price) * position.filled_qty
            - position.buy_fee
            - sell_fee
        )
        logger.trade_completed(
            symbol=self._symbol,
            buy_price=position.avg_buy_price,
            sell_price=sell_price,
            qty=position.filled_qty,
            buy_fee=position.buy_fee,
            sell_fee=sell_fee,
            realized_pnl=realized_pnl,
        )
        self._risk.set_position_closed(realized_pnl=realized_pnl)
        return True

    # ── Main polling loop ─────────────────────────────────────────────────────

    def run_forever(self, stop_event=None) -> None:
        """
        Poll for signals and execute trade cycles until interrupted
        or stop_event is set (threading.Event).
        """
        logger.info(
            f"[Strategy] Starting signal loop  symbol={self._symbol}  "
            f"poll={self._poll_interval}s"
        )
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info("[Strategy] Stop event received — shutting down.")
                break
            try:
                book = self._client.get_order_book(self._symbol)
                signal = get_buy_signal(self._symbol, book)
                if signal:
                    signal.suggested_quantity_base = self._calc_qty(book.mid_price)
                    logger.info(
                        f"[Strategy] Signal received  qty={signal.suggested_quantity_base}"
                    )
                    self.run_trade_cycle(signal, book)
                else:
                    logger.debug("[Strategy] No signal this tick.")
            except KeyboardInterrupt:
                logger.info("[Strategy] KeyboardInterrupt — shutting down.")
                break
            except Exception as exc:
                logger.error("[Strategy] Unexpected error in main loop", exc)

            time.sleep(self._poll_interval)
